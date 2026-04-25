"""
Phoenix Bot — WatcherAgent + InvestigatorAgent

Autonomous monitoring daemon that runs alongside the existing trading stack.
Unlike tools/watchdog.py (internal, fast, narrow: process/port/WS probes at
5s cadence with auto-restart), this module does SEMANTIC audits at slower
cadence (60s spot + 10-30min deep) and routes anomalies to an AI-driven
InvestigatorAgent that can restart processes, execute bounded file actions,
or save suggested patches for human review.

Design goals (per Jennifer 2026-04-24):
  * Runs side-by-side with tools/watchdog.py (not a replacement).
  * Registered as a Windows Scheduled Task so it survives reboots
    (see tools/register_watcher_task.ps1).
  * Desktop KillSwitch + PhoenixStart shortcuts control the Task.
  * When 3 consecutive restart attempts fail, paging escalates to
    Twilio SMS + Telegram.
  * Read-only against the trading code. Reads state via: bridge HTTP
    health, dashboard API, OIF folders, trade_memory.json, log tails,
    OS process list. Never imports bots/ or strategies/.

Severity mapping (per prompt):
  MINOR      → log only
  MAJOR      → log + InvestigatorAgent
  RED_ALERT  → log + InvestigatorAgent + SMS + Telegram

Usage:
  python tools/watcher_agent.py                    # Run forever
  python tools/watcher_agent.py --once             # Run one spot + one deep, exit
  python tools/watcher_agent.py --dry-run          # Don't fire SMS / restart / file actions
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dt_time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env from project root before reading any credentials.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════
# Constants — all paths absolute + NT8 paths hardcoded per prompt
# ═══════════════════════════════════════════════════════════════════════

PHOENIX_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PHOENIX_ROOT / "logs"
INCIDENTS_DIR = LOGS_DIR / "incidents"
PATCHES_DIR = LOGS_DIR / "suggested_patches"
HALT_MARKER = PHOENIX_ROOT / "memory" / ".HALT"
KILLSWITCH_MARKER = PHOENIX_ROOT / "memory" / ".KILL_SWITCH_ENGAGED"

NT8_DATA_ROOT = Path(r"C:\Users\Trading PC\Documents\NinjaTrader 8")
OIF_INCOMING = NT8_DATA_ROOT / "incoming"
OIF_OUTGOING = NT8_DATA_ROOT / "outgoing"

BRIDGE_HEALTH_URL = "http://127.0.0.1:8767/health"
DASHBOARD_STATUS_URL = "http://127.0.0.1:5000/api/status"
DASHBOARD_STRATEGY_RISK_URL = "http://127.0.0.1:5000/api/strategy-risk"

PRIMARY_LOGS = {
    "bridge": LOGS_DIR / "bridge_stdout.log",
    "sim_bot": LOGS_DIR / "sim_bot_stdout.log",
    "prod_bot": LOGS_DIR / "prod_bot_stdout.log",
    "watchdog": LOGS_DIR / "watchdog.log",
}

PROCESS_SIGNATURES = {
    "bridge": "bridge_server.py",
    "sim_bot": "sim_bot.py",
    "prod_bot": "prod_bot.py",
    "dashboard": "dashboard/server.py",
    "watchdog": "tools/watchdog.py",
}

# Market hours (Chicago timezone, Mon-Fri)
CT_TZ = ZoneInfo("America/Chicago")
MARKET_OPEN = dt_time(8, 30)
MARKET_CLOSE = dt_time(15, 0)

# Cadences
SPOT_INTERVAL_S = 60
DEEP_INTERVAL_MIN_S = 10 * 60
DEEP_INTERVAL_MAX_S = 30 * 60

# Thresholds
TICK_FRESH_WARN_S = 2 * 60
TICK_FRESH_CRIT_S = 5 * 60
STALE_OIF_AGE_S = 30
LOG_TAIL_LINES = 50
RISK_PER_TRADE_MAX_USD = 200.0     # Per Jennifer 2026-04-24: $200/day replaces legacy $20
DAILY_STRATEGY_LOSS_CAP_USD = 200.0
DAILY_STRATEGY_FLOOR_USD = 1500.0
NO_TRADES_WARN_MIN = 60

# Error keywords in log scan
LOG_ERROR_PATTERNS = re.compile(
    r"\b(ERROR|EXCEPTION|CRITICAL|Traceback)\b", re.IGNORECASE
)
# Known-benign CRITICAL patterns we do NOT want to page on
BENIGN_CRITICAL = re.compile(
    r"PRICE_SANITY|MenthorQ.*stale|FMP fallback|MODE FLIP"
)


# ═══════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════

def _configure_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"watcher_{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = logging.getLogger("WatcherAgent")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


logger = _configure_logging()


# ═══════════════════════════════════════════════════════════════════════
# Finding dataclass
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Finding:
    severity: str       # "MINOR" | "MAJOR" | "RED_ALERT"
    category: str       # e.g., "tick_freshness", "stale_oif"
    detail: str         # Human-readable one-liner
    context: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(CT_TZ).isoformat())

    def as_sms(self) -> str:
        cst = datetime.fromisoformat(self.timestamp).strftime("%Y-%m-%d %H:%M:%S CT")
        return (
            f"PHOENIX BOT {self.severity}\n"
            f"Time: {cst}\n"
            f"Issue: {self.category}\n"
            f"Detail: {self.detail[:140]}\n"
            f"Check logs/incidents/ for full report."
        )


# ═══════════════════════════════════════════════════════════════════════
# Alerting (Twilio SMS + Telegram)
# ═══════════════════════════════════════════════════════════════════════

class Alerter:
    """Thin wrapper around Twilio + Telegram. Fails soft if creds missing."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._twilio_client = None
        self._twilio_from = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
        self._twilio_to = os.environ.get("TWILIO_TO_NUMBER", "").strip()
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        tok = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        if sid and tok and self._twilio_from and self._twilio_to:
            try:
                from twilio.rest import Client as TwilioClient
                self._twilio_client = TwilioClient(sid, tok)
                logger.info("[Alerter] Twilio client ready")
            except Exception as e:
                logger.warning(f"[Alerter] Twilio init failed: {e!r}")
        else:
            logger.warning("[Alerter] Twilio creds missing — SMS disabled")

        self._tg_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        self._tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if self._tg_token and self._tg_chat:
            logger.info("[Alerter] Telegram ready")
        else:
            logger.warning("[Alerter] Telegram creds missing — Telegram disabled")

    def sms(self, message: str) -> bool:
        """Send SMS via Twilio. Returns True on success."""
        if self.dry_run:
            logger.info(f"[Alerter:DRY-RUN] SMS would send: {message[:100]}")
            return True
        if not self._twilio_client:
            logger.warning("[Alerter] SMS skipped (no Twilio client)")
            return False
        try:
            msg = self._twilio_client.messages.create(
                body=message, from_=self._twilio_from, to=self._twilio_to,
            )
            logger.info(f"[Alerter] SMS sent sid={msg.sid}")
            return True
        except Exception as e:
            logger.error(f"[Alerter] SMS failed: {e!r}")
            return False

    def telegram(self, message: str) -> bool:
        """Send Telegram message via bot HTTP API. Returns True on success."""
        if self.dry_run:
            logger.info(f"[Alerter:DRY-RUN] Telegram would send: {message[:100]}")
            return True
        if not (self._tg_token and self._tg_chat):
            return False
        try:
            url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
            data = json.dumps({
                "chat_id": self._tg_chat,
                "text": message,
                "parse_mode": "HTML",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True
                logger.warning(f"[Alerter] Telegram status {resp.status}")
        except Exception as e:
            logger.error(f"[Alerter] Telegram failed: {e!r}")
        return False

    def page_red_alert(self, finding: Finding) -> None:
        """Fire both SMS and Telegram for a RED_ALERT finding."""
        msg = finding.as_sms()
        tg = f"🚨 <b>PHOENIX RED ALERT</b>\n<code>{finding.category}</code>\n{finding.detail}"
        self.sms(msg)
        self.telegram(tg)


# ═══════════════════════════════════════════════════════════════════════
# InvestigatorAgent
# ═══════════════════════════════════════════════════════════════════════

INVESTIGATOR_SYSTEM_PROMPT = """You are an expert trading systems engineer. A monitoring agent has detected an
anomaly in a live trading system. Analyze the finding, determine the root cause,
and if a fix is possible via Python code or file system action, provide it.
If not, provide a detailed incident report.

The trading system is a Python bot connected to NinjaTrader 8 via WebSocket and
OIF files. Processes: bridge_server.py, sim_bot.py, prod_bot.py, dashboard/server.py,
tools/watchdog.py. Strategies in strategies/*.py. AI agents in agents/.

Respond with a JSON object containing these exact keys:
{
  "root_cause": "<one-paragraph explanation>",
  "fix_available": "yes" | "no",
  "fix_type": "code_patch" | "file_action" | "restart_process" | "manual_required",
  "fix_detail": "<specifics: code snippet, file path, process name, or instructions>",
  "incident_summary": "<2-3 sentence human-readable summary>"
}

For fix_type=restart_process, fix_detail must be one of: sim_bot, prod_bot, bridge.
For fix_type=file_action, fix_detail must describe the path + operation
  (e.g., 'delete /path/to/stale_oif.txt'). Only these file operations are
  permitted: delete stale OIF in incoming/ older than 5 min, clear lock
  files in memory/. Never suggest editing trade_memory.json.
For fix_type=code_patch, do NOT auto-apply; the patch will be saved for
  human review. Include the full file path + a unified diff in fix_detail.
"""

RESTARTABLE_PROCESSES = {"sim_bot", "prod_bot", "bridge"}
PROCESS_LAUNCH_COMMANDS = {
    "sim_bot": [sys.executable, str(PHOENIX_ROOT / "bots" / "sim_bot.py")],
    "prod_bot": [sys.executable, str(PHOENIX_ROOT / "bots" / "prod_bot.py")],
    "bridge": [sys.executable, str(PHOENIX_ROOT / "bridge" / "bridge_server.py")],
}


class InvestigatorAgent:
    """Gemini-powered incident investigator.

    Called by WatcherAgent for MAJOR and RED_ALERT findings. Submits the
    finding to Gemini, parses the response, and — if the suggested fix
    type is within the allow-list — executes it. Always writes a full
    incident report to logs/incidents/.
    """

    def __init__(self, alerter: Alerter, dry_run: bool = False):
        self.alerter = alerter
        self.dry_run = dry_run
        self._model = None
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
        if api_key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=api_key)
                self._model = genai.GenerativeModel("gemini-2.5-flash")
                logger.info("[Investigator] Gemini client ready")
            except Exception as e:
                logger.error(f"[Investigator] Gemini init failed: {e!r}")
        else:
            logger.warning("[Investigator] GOOGLE_API_KEY missing — degraded mode (no AI analysis)")

    # ── AI call ────────────────────────────────────────────────────
    def _ask_gemini(self, finding: Finding) -> dict:
        """Submit the finding to Gemini and parse the structured response.

        Degraded mode (no API key / network failure): returns a minimal
        stub response tagged manual_required so the caller still writes
        a report and the human can investigate.
        """
        if self._model is None:
            return {
                "root_cause": "AI investigator unavailable (no GOOGLE_API_KEY or init failure)",
                "fix_available": "no",
                "fix_type": "manual_required",
                "fix_detail": "Investigate manually. Gemini not reachable.",
                "incident_summary": f"{finding.severity}/{finding.category}: {finding.detail}",
            }
        payload = asdict(finding)
        prompt = (
            INVESTIGATOR_SYSTEM_PROMPT
            + "\n\nFINDING:\n" + json.dumps(payload, indent=2, default=str)
        )
        try:
            resp = self._model.generate_content(prompt)
            text = (resp.text or "").strip()
            # Gemini often wraps JSON in ```json fences; strip them.
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                raise ValueError(f"no JSON object in response: {text[:200]}")
            return json.loads(m.group(0))
        except Exception as e:
            logger.error(f"[Investigator] Gemini call failed: {e!r}")
            return {
                "root_cause": f"Gemini call failed: {e!r}",
                "fix_available": "no",
                "fix_type": "manual_required",
                "fix_detail": str(e)[:500],
                "incident_summary": f"{finding.severity}/{finding.category}: {finding.detail}",
            }

    # ── Fix execution ──────────────────────────────────────────────
    def _execute_restart(self, target: str) -> tuple[bool, str]:
        if target not in RESTARTABLE_PROCESSES:
            return False, f"restart not allowed for '{target}' (allow-list: {RESTARTABLE_PROCESSES})"
        if self.dry_run:
            return True, f"DRY-RUN: would restart {target}"
        # Find current pid, kill, respawn.
        killed_pid = None
        sig = PROCESS_SIGNATURES.get({"sim_bot": "sim_bot", "prod_bot": "prod_bot",
                                      "bridge": "bridge"}[target], "")
        # Simple sig: key into PROCESS_SIGNATURES
        sig = PROCESS_SIGNATURES.get(target)
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmd = " ".join(proc.info.get("cmdline") or [])
                    if sig and sig in cmd:
                        proc.terminate()
                        killed_pid = proc.info["pid"]
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            return False, f"process lookup failed: {e!r}"

        cmd = PROCESS_LAUNCH_COMMANDS.get(target)
        if not cmd:
            return False, f"no launch command registered for {target}"
        try:
            stdout = open(LOGS_DIR / f"{target}_stdout.log", "ab")
            stderr = open(LOGS_DIR / f"{target}_stderr.log", "ab")
            # CREATE_NEW_PROCESS_GROUP (0x00000200) so we don't die with parent
            creationflags = 0x00000200 if sys.platform == "win32" else 0
            new_proc = subprocess.Popen(
                cmd, cwd=str(PHOENIX_ROOT),
                stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True, f"killed pid={killed_pid}, spawned pid={new_proc.pid}"
        except Exception as e:
            return False, f"spawn failed: {e!r}"

    def _execute_file_action(self, detail: str) -> tuple[bool, str]:
        """Only permits narrow whitelisted file operations.

        Allowed:
          * Delete a file in OIF_INCOMING whose basename starts with 'oif'
            and whose mtime is older than 5 minutes.
          * Delete a lock file in PHOENIX_ROOT/memory/ whose basename ends
            with '.lock'.
        Everything else → reject.
        """
        if self.dry_run:
            return True, f"DRY-RUN: would execute file action: {detail}"
        # Parse "delete <path>". Support three forms:
        #   delete "C:\path with spaces\file.txt"
        #   delete 'C:\path\file.txt'
        #   delete C:\path\file.txt           (everything after "delete ")
        m = re.search(r'delete\s+"([^"]+)"', detail, re.IGNORECASE)
        if not m:
            m = re.search(r"delete\s+'([^']+)'", detail, re.IGNORECASE)
        if not m:
            # Greedy — take everything after "delete " to end-of-string
            m = re.search(r"delete\s+(.+?)\s*$", detail, re.IGNORECASE | re.DOTALL)
        if not m:
            return False, f"could not parse file action: {detail!r}"
        target = Path(m.group(1).strip())
        if not target.exists():
            return False, f"target does not exist: {target}"

        # Allow-list check
        allow = False
        if target.is_file():
            if target.parent.resolve() == OIF_INCOMING.resolve():
                if target.name.startswith("oif"):
                    age = time.time() - target.stat().st_mtime
                    if age >= 5 * 60:
                        allow = True
                    else:
                        return False, f"OIF file {target.name} too fresh ({age:.0f}s) — must be ≥5min"
            elif target.parent.resolve() == (PHOENIX_ROOT / "memory").resolve():
                if target.name.endswith(".lock"):
                    allow = True
        if not allow:
            return False, f"path not in file-action allow-list: {target}"

        try:
            target.unlink()
            return True, f"deleted {target}"
        except Exception as e:
            return False, f"delete failed: {e!r}"

    def _save_suggested_patch(self, detail: str, finding: Finding) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = PATCHES_DIR / f"patch_{ts}.py"
        path.write_text(
            f"# Suggested patch from InvestigatorAgent at {ts}\n"
            f"# Finding category: {finding.category}\n"
            f"# Severity: {finding.severity}\n"
            f"# Detail: {finding.detail}\n"
            f"# DO NOT AUTO-APPLY — human review required.\n\n"
            f"{detail}\n",
            encoding="utf-8",
        )
        return str(path)

    # ── Main entry point ───────────────────────────────────────────
    def investigate(self, finding: Finding) -> dict:
        """Analyze a finding, execute bounded fix, write report."""
        ai = self._ask_gemini(finding)
        action_result = {"attempted": False, "success": False, "detail": ""}
        fix_type = (ai.get("fix_type") or "manual_required").strip()

        if fix_type == "restart_process":
            target = (ai.get("fix_detail") or "").strip().lower()
            action_result["attempted"] = True
            ok, msg = self._execute_restart(target)
            action_result["success"] = ok
            action_result["detail"] = msg
            logger.info(f"[Investigator] restart {target} ok={ok} msg={msg}")
        elif fix_type == "file_action":
            action_result["attempted"] = True
            ok, msg = self._execute_file_action(ai.get("fix_detail", ""))
            action_result["success"] = ok
            action_result["detail"] = msg
            logger.info(f"[Investigator] file_action ok={ok} msg={msg}")
        elif fix_type == "code_patch":
            action_result["attempted"] = True
            path = self._save_suggested_patch(ai.get("fix_detail", ""), finding)
            action_result["success"] = True
            action_result["detail"] = f"saved to {path}"
            logger.info(f"[Investigator] code_patch saved: {path}")
        else:
            action_result["detail"] = "manual required — no auto-action taken"

        self._write_report(finding, ai, action_result)
        return {"ai": ai, "action": action_result}

    def _write_report(self, finding: Finding, ai: dict, action: dict) -> None:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = INCIDENTS_DIR / f"incident_{ts}.txt"
        lines = [
            f"Phoenix Bot Incident Report",
            f"Generated: {datetime.now(CT_TZ).isoformat()}",
            f"",
            f"=== FINDING ===",
            f"Severity  : {finding.severity}",
            f"Category  : {finding.category}",
            f"Timestamp : {finding.timestamp}",
            f"Detail    : {finding.detail}",
            f"Context   :",
            json.dumps(finding.context, indent=2, default=str),
            f"",
            f"=== AI ANALYSIS (Gemini) ===",
            f"Root cause     : {ai.get('root_cause', '')}",
            f"Fix available  : {ai.get('fix_available', '')}",
            f"Fix type       : {ai.get('fix_type', '')}",
            f"Fix detail     : {ai.get('fix_detail', '')}",
            f"Summary        : {ai.get('incident_summary', '')}",
            f"",
            f"=== ACTION TAKEN ===",
            f"Attempted : {action['attempted']}",
            f"Success   : {action['success']}",
            f"Detail    : {action['detail']}",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"[Investigator] report written: {path}")


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _now_ct() -> datetime:
    return datetime.now(CT_TZ)


def _is_market_hours(now: Optional[datetime] = None) -> bool:
    now = now or _now_ct()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _probe_url(url: str, timeout: float = 3.0) -> tuple[bool, Optional[dict]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        try:
            return True, json.loads(body)
        except json.JSONDecodeError:
            return True, {"raw": body[:500]}
    except Exception as e:
        return False, {"error": str(e)}


def _tail(path: Path, n: int = LOG_TAIL_LINES) -> list[str]:
    if not path.exists():
        return []
    try:
        # Efficient tail: seek backwards
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 4096 * max(1, n // 50 + 1))
            f.seek(max(0, size - chunk), os.SEEK_SET)
            data = f.read().decode("utf-8", errors="ignore")
        return data.splitlines()[-n:]
    except Exception as e:
        logger.warning(f"[tail] {path}: {e!r}")
        return []


def _find_process(signature: str) -> Optional[int]:
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
                if signature in cmd:
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        logger.warning(f"[_find_process] psutil error: {e!r}")
    return None


# ═══════════════════════════════════════════════════════════════════════
# WatcherAgent
# ═══════════════════════════════════════════════════════════════════════

class WatcherAgent:
    """External monitor daemon. See module docstring."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.alerter = Alerter(dry_run=dry_run)
        self.investigator = InvestigatorAgent(self.alerter, dry_run=dry_run)
        self._stop_event = threading.Event()
        self._spot_thread: Optional[threading.Thread] = None
        self._deep_thread: Optional[threading.Thread] = None

        # Stateful trackers
        self._no_trades_consecutive: int = 0
        self._restart_failure_counts: dict[str, int] = {}   # For 3-strike SMS escalation
        self._last_trade_ts: Optional[float] = None
        self._last_deep_run: Optional[datetime] = None

    # ── Severity dispatcher ────────────────────────────────────────
    def _handle(self, finding: Finding) -> None:
        logger.info(f"[{finding.severity}] {finding.category}: {finding.detail}")
        if finding.severity == "MINOR":
            return
        # MAJOR + RED_ALERT both invoke InvestigatorAgent
        try:
            self.investigator.investigate(finding)
        except Exception as e:
            logger.error(f"[Watcher] investigator failed: {e!r}")
        if finding.severity == "RED_ALERT":
            self.alerter.page_red_alert(finding)

    # ═══════════════════════════════════════════════════════════════
    # SPOT CHECKS (60s cadence)
    # ═══════════════════════════════════════════════════════════════

    def _check_bridge_health(self) -> list[Finding]:
        findings: list[Finding] = []
        ok, data = _probe_url(BRIDGE_HEALTH_URL)
        if not ok:
            findings.append(Finding(
                severity="RED_ALERT", category="bridge_down",
                detail="Bridge health endpoint unreachable (processes may be dead)",
                context={"error": data},
            ))
            return findings

        # Tick freshness
        tick_age = (data or {}).get("nt8_last_tick_age_s")
        hb_age = (data or {}).get("nt8_last_heartbeat_age_s")
        if tick_age is not None and _is_market_hours():
            if tick_age > TICK_FRESH_CRIT_S:
                findings.append(Finding(
                    severity="RED_ALERT", category="tick_freshness",
                    detail=f"No ticks for {tick_age:.0f}s during market hours (>{TICK_FRESH_CRIT_S}s threshold)",
                    context={"tick_age_s": tick_age, "nt8_status": data.get("nt8_status")},
                ))
            elif tick_age > TICK_FRESH_WARN_S:
                findings.append(Finding(
                    severity="MINOR", category="tick_freshness",
                    detail=f"Tick age {tick_age:.0f}s (warn threshold {TICK_FRESH_WARN_S}s)",
                    context={"tick_age_s": tick_age},
                ))

        # 2026-04-25 §4.4: SILENT_STALL escalation. Bridge already detects this
        # condition (TCP heartbeats fresh but ticks stale = NT8 data subscription
        # frozen / chart locked up) and emits an event. We escalate it from
        # MINOR -> RED_ALERT during active market hours because it means the
        # bot is trading on stale data while NT8 LOOKS connected.
        # Detection: scan the most recent connection_events for SILENT_STALL.
        events = (data or {}).get("connection_events") or []
        recent_stall = None
        for ev in reversed(events[-30:]):
            msg = (ev or {}).get("message") or ""
            if "SILENT_STALL" in msg and "cleared" not in msg.lower():
                recent_stall = ev
                break
            if "SILENT_STALL cleared" in msg:
                # Most recent stall has been cleared; nothing to escalate.
                break
        if recent_stall:
            sev = "RED_ALERT" if _is_market_hours() else "MINOR"
            findings.append(Finding(
                severity=sev, category="silent_stall",
                detail=(
                    f"NT8 SILENT_STALL active: heartbeats fresh but ticks stale. "
                    f"Bot is trading on stale data while NT8 LOOKS connected. "
                    f"Investigate NT8 data subscription / chart lock-up."
                ),
                context={
                    "stall_event_ts": recent_stall.get("ts"),
                    "stall_message": recent_stall.get("message"),
                    "tick_age_s": tick_age,
                    "heartbeat_age_s": hb_age,
                    "in_market_hours": _is_market_hours(),
                },
            ))
        return findings

    def _check_oif_folders(self) -> list[Finding]:
        findings: list[Finding] = []
        if not OIF_INCOMING.exists():
            findings.append(Finding(
                severity="MAJOR", category="oif_incoming_missing",
                detail=f"OIF incoming folder missing: {OIF_INCOMING}",
                context={"path": str(OIF_INCOMING)},
            ))
            return findings
        now = time.time()
        stale = []
        for p in OIF_INCOMING.glob("oif*.txt"):
            try:
                age = now - p.stat().st_mtime
                if age > STALE_OIF_AGE_S:
                    stale.append({"file": p.name, "age_s": round(age, 1)})
            except OSError:
                continue
        if stale:
            findings.append(Finding(
                severity="MAJOR", category="stale_oif",
                detail=f"{len(stale)} stale OIF file(s) in incoming/ (>{STALE_OIF_AGE_S}s — possible execution failure)",
                context={"files": stale[:10]},
            ))
        return findings

    def _check_processes(self) -> list[Finding]:
        """Verify critical processes running. Track restart-failure count
        per process; escalate to SMS after 3 consecutive misses even if
        watchdog normally handles it (we're the safety-net-of-the-safety-net)."""
        findings: list[Finding] = []
        critical = ["bridge", "sim_bot", "prod_bot"]
        for name in critical:
            if KILLSWITCH_MARKER.exists():
                # KillSwitch engaged — processes down is by design
                continue
            sig = PROCESS_SIGNATURES[name]
            pid = _find_process(sig)
            if pid is None:
                self._restart_failure_counts[name] = self._restart_failure_counts.get(name, 0) + 1
                consecutive = self._restart_failure_counts[name]
                sev = "RED_ALERT" if consecutive >= 3 else "MAJOR"
                findings.append(Finding(
                    severity=sev,
                    category="process_down",
                    detail=(
                        f"{name} not running (consecutive checks missed: {consecutive}). "
                        + ("Watchdog has failed to restart after 3 tries — paging." if consecutive >= 3 else "")
                    ),
                    context={"process": name, "signature": sig, "consecutive_misses": consecutive},
                ))
            else:
                if self._restart_failure_counts.get(name, 0) > 0:
                    logger.info(f"[Watcher] {name} recovered (pid={pid})")
                self._restart_failure_counts[name] = 0
        return findings

    def _check_log_tails(self) -> list[Finding]:
        """Scan recent log lines for ERROR/EXCEPTION/CRITICAL/Traceback.

        2026-04-25: dedupe added. Previously every spot-check would re-flag
        the SAME historical error line forever (e.g., a STOP_SANITY_FAIL
        from 16:00 yesterday kept paging on every 60s sweep through today).
        Now we hash each error line + remember which we've already flagged;
        only NEW error lines fire a finding.
        """
        if not hasattr(self, "_seen_error_hashes"):
            self._seen_error_hashes: set[str] = set()
        import hashlib as _h

        findings: list[Finding] = []
        for name, path in PRIMARY_LOGS.items():
            tail = _tail(path, LOG_TAIL_LINES)
            new_error_lines = []
            for ln in tail:
                if not LOG_ERROR_PATTERNS.search(ln):
                    continue
                if BENIGN_CRITICAL.search(ln):
                    continue
                # Hash the line minus its leading timestamp so duplicate
                # log content (with shifted timestamps) is also dedupped.
                # Timestamp pattern: YYYY-MM-DD HH:MM:SS,mmm
                stripped = re.sub(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d+\s*", "", ln)
                h = _h.sha1(stripped.encode("utf-8", errors="ignore")).hexdigest()[:16]
                if h in self._seen_error_hashes:
                    continue
                self._seen_error_hashes.add(h)
                new_error_lines.append(ln)
            if new_error_lines:
                findings.append(Finding(
                    severity="MAJOR", category="log_errors",
                    detail=f"{len(new_error_lines)} NEW error/exception line(s) in {name} log tail",
                    context={"log": name, "sample": new_error_lines[-3:],
                             "deduped_seen_total": len(self._seen_error_hashes)},
                ))
        # Bound the dedupe set so it doesn't grow unbounded over a long uptime.
        if len(self._seen_error_hashes) > 5000:
            # Drop oldest 1000; in practice we don't track ordering, so we
            # just clip the set arbitrarily — losing some history is fine
            # because the bot's actual log persists and a deeper investigation
            # always re-reads the file directly.
            self._seen_error_hashes = set(list(self._seen_error_hashes)[-4000:])
        return findings

    def run_spot_checks(self) -> list[Finding]:
        findings = []
        findings += self._check_bridge_health()
        findings += self._check_oif_folders()
        findings += self._check_processes()
        findings += self._check_log_tails()
        for f in findings:
            self._handle(f)
        return findings

    # ═══════════════════════════════════════════════════════════════
    # DEEP CHECKS (10-30min cadence)
    # ═══════════════════════════════════════════════════════════════

    def _load_recent_trades(self, hours_back: float = 2.0) -> list[dict]:
        path = LOGS_DIR / "trade_memory.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[deep] trade_memory load failed: {e!r}")
            return []
        trades = data.get("trades") if isinstance(data, dict) else data
        if not isinstance(trades, list):
            return []
        cutoff = time.time() - hours_back * 3600
        out = []
        for t in trades:
            try:
                ts = t.get("exit_time") or t.get("entry_time") or 0
                if isinstance(ts, str):
                    ts_epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                else:
                    ts_epoch = float(ts)
                if ts_epoch >= cutoff:
                    out.append(t)
            except Exception:
                continue
        return out

    def _check_trade_logic(self) -> list[Finding]:
        findings: list[Finding] = []
        recent = self._load_recent_trades()
        for t in recent:
            direction = str(t.get("direction", "")).upper()
            snapshot = t.get("market_snapshot") or {}
            bias = str(snapshot.get("multi_tf_bias", "")).upper() or \
                   str(snapshot.get("tf_bias_tick", "")).upper()
            pretrade = str(t.get("pretrade_filter_result", "")).upper()
            if direction == "SHORT" and bias == "BULLISH":
                findings.append(Finding(
                    severity="MAJOR", category="logic_mismatch",
                    detail=f"SHORT taken while multi-TF bias BULLISH (trade {t.get('trade_id')})",
                    context={"trade": t},
                ))
            elif direction == "LONG" and bias == "BEARISH":
                findings.append(Finding(
                    severity="MAJOR", category="logic_mismatch",
                    detail=f"LONG taken while multi-TF bias BEARISH (trade {t.get('trade_id')})",
                    context={"trade": t},
                ))
            if pretrade in ("SIT_OUT", "BLOCK"):
                findings.append(Finding(
                    severity="RED_ALERT", category="pretrade_violation",
                    detail=f"Entry fired despite pretrade filter = {pretrade} (trade {t.get('trade_id')})",
                    context={"trade": t},
                ))
        return findings

    def _check_trade_frequency(self) -> list[Finding]:
        """MINOR on first 60-min dry spell, MAJOR on second consecutive."""
        if not _is_market_hours():
            return []
        recent = self._load_recent_trades(hours_back=1.0)
        if not recent:
            self._no_trades_consecutive += 1
            sev = "MAJOR" if self._no_trades_consecutive >= 2 else "MINOR"
            return [Finding(
                severity=sev, category="no_trades",
                detail=f"No trades in last {NO_TRADES_WARN_MIN}min (consecutive dry-deep-check count: {self._no_trades_consecutive})",
                context={"consecutive_checks": self._no_trades_consecutive},
            )]
        self._no_trades_consecutive = 0
        return []

    def _check_risk_rules(self) -> list[Finding]:
        findings: list[Finding] = []
        recent = self._load_recent_trades(hours_back=24.0)
        for t in recent:
            pnl = t.get("pnl") or t.get("realized_pnl") or 0
            try:
                pnl_f = float(pnl)
            except (TypeError, ValueError):
                continue
            if pnl_f < -RISK_PER_TRADE_MAX_USD:
                findings.append(Finding(
                    severity="RED_ALERT", category="risk_breach_trade",
                    detail=f"Trade loss ${pnl_f:.2f} exceeds per-trade cap ${RISK_PER_TRADE_MAX_USD}",
                    context={"trade": t},
                ))
        # Daily floor via dashboard strategy-risk
        ok, data = _probe_url(DASHBOARD_STRATEGY_RISK_URL)
        if ok and isinstance(data, dict):
            for strat, info in (data.get("strategies") or {}).items():
                bal = info.get("balance") if isinstance(info, dict) else None
                try:
                    if bal is not None and float(bal) < DAILY_STRATEGY_FLOOR_USD:
                        findings.append(Finding(
                            severity="RED_ALERT", category="risk_breach_floor",
                            detail=f"Strategy '{strat}' balance ${float(bal):.2f} below floor ${DAILY_STRATEGY_FLOOR_USD}",
                            context={"strategy": strat, "balance": bal},
                        ))
                except (TypeError, ValueError):
                    continue
        return findings

    def _check_ai_health(self) -> list[Finding]:
        """Probe Gemini Flash via a minimal completion to confirm the
        council/pretrade pipeline's AI dependency is reachable."""
        findings: list[Finding] = []
        try:
            import google.generativeai as genai
            key = os.environ.get("GOOGLE_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
            if not key:
                findings.append(Finding(
                    severity="MAJOR", category="ai_key_missing",
                    detail="GOOGLE_API_KEY / GEMINI_API_KEY not set; council + pretrade degraded",
                ))
                return findings
            genai.configure(api_key=key)
            model = genai.GenerativeModel("gemini-2.5-flash")
            resp = model.generate_content("Respond with exactly: OK")
            if "OK" not in (resp.text or "").upper():
                findings.append(Finding(
                    severity="MAJOR", category="ai_unresponsive",
                    detail=f"Gemini probe returned unexpected: {(resp.text or '')[:100]}",
                ))
        except Exception as e:
            findings.append(Finding(
                severity="MAJOR", category="ai_unresponsive",
                detail=f"Gemini probe failed: {e!r}",
            ))
        return findings

    def _check_fmp_sanity_mode(self) -> list[Finding]:
        """If price_sanity is in fmp_primary mode for >10 min, escalate —
        the NT8 stream hasn't healed. We poll the bridge bot_heartbeats
        indirectly via reading the price_sanity snapshot file if exposed,
        otherwise we scan the sim_bot log tail for mode-flip markers."""
        findings: list[Finding] = []
        for log in (PRIMARY_LOGS["sim_bot"], PRIMARY_LOGS["prod_bot"]):
            tail = _tail(log, 500)
            # Look for most recent MODE FLIP line
            flips = [ln for ln in tail if "MODE FLIP" in ln]
            if not flips:
                continue
            last = flips[-1]
            if "fmp_primary" in last:
                findings.append(Finding(
                    severity="MAJOR", category="fmp_fallback_engaged",
                    detail="price_sanity is in fmp_primary mode (new entries soft-blocked)",
                    context={"last_flip_line": last},
                ))
                break
        return findings

    def run_deep_checks(self) -> list[Finding]:
        findings = []
        findings += self._check_trade_logic()
        findings += self._check_trade_frequency()
        findings += self._check_risk_rules()
        findings += self._check_ai_health()
        findings += self._check_fmp_sanity_mode()
        for f in findings:
            self._handle(f)
        self._last_deep_run = _now_ct()
        return findings

    # ═══════════════════════════════════════════════════════════════
    # Threading
    # ═══════════════════════════════════════════════════════════════

    def _spot_loop(self) -> None:
        while not self._stop_event.is_set():
            start = time.time()
            try:
                self.run_spot_checks()
            except Exception as e:
                logger.error(f"[Watcher] spot loop error: {e!r}")
            elapsed = time.time() - start
            self._stop_event.wait(max(1, SPOT_INTERVAL_S - elapsed))

    def _deep_loop(self) -> None:
        # First deep check after a small initial delay so the stack has
        # time to warm up post-launch.
        self._stop_event.wait(90)
        while not self._stop_event.is_set():
            try:
                self.run_deep_checks()
            except Exception as e:
                logger.error(f"[Watcher] deep loop error: {e!r}")
            wait_s = random.randint(DEEP_INTERVAL_MIN_S, DEEP_INTERVAL_MAX_S)
            logger.info(f"[Watcher] next deep check in {wait_s}s")
            self._stop_event.wait(wait_s)

    def start(self) -> None:
        next_spot = datetime.now() + (datetime.fromtimestamp(SPOT_INTERVAL_S) - datetime.fromtimestamp(0))
        banner = (
            "=" * 60 + "\n"
            " PHOENIX WatcherAgent\n"
            " Spot checks every 60s, deep checks every 10-30 min (random)\n"
            f" Started at {_now_ct().isoformat()}\n"
            f" KillSwitch marker watched at: {KILLSWITCH_MARKER}\n"
            f" Dry-run: {self.dry_run}\n"
            + "=" * 60
        )
        print(banner)
        logger.info("WatcherAgent started")
        self._spot_thread = threading.Thread(target=self._spot_loop, daemon=True, name="spot_loop")
        self._deep_thread = threading.Thread(target=self._deep_loop, daemon=True, name="deep_loop")
        self._spot_thread.start()
        self._deep_thread.start()

        def _handle_sigterm(signum, frame):
            logger.info(f"Signal {signum} received; shutting down")
            self._stop_event.set()
        try:
            signal.signal(signal.SIGINT, _handle_sigterm)
            signal.signal(signal.SIGTERM, _handle_sigterm)
        except Exception:
            pass

        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self._stop_event.set()
        logger.info("WatcherAgent stopped")

    def stop(self) -> None:
        self._stop_event.set()


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="Phoenix Bot WatcherAgent")
    parser.add_argument("--once", action="store_true",
                        help="Run one spot + one deep cycle, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't send SMS / restart processes / execute file actions")
    args = parser.parse_args()

    agent = WatcherAgent(dry_run=args.dry_run)
    if args.once:
        logger.info("--once mode: running one spot + one deep check")
        agent.run_spot_checks()
        agent.run_deep_checks()
        return 0
    agent.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
