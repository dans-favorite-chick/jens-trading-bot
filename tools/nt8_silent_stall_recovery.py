"""NT8 silent-stall auto-recovery (P1-4, F-10).

Polls http://127.0.0.1:8767/health every 30 s. When the bridge reports
`nt8_status == "live"` AND `tick_rate_10s == 0` continuously for more
than `STALL_THRESHOLD_S` (default 180 s), attempts an OUTSIDE-the-bot
recovery of NinjaTrader 8:

  1. SIGTERM NinjaTrader.exe     (PowerShell Stop-Process -Name NinjaTrader)
  2. Wait 30 s for the process to release file handles / sockets
  3. Relaunch via the PhoenixBoot shortcut         (Start-Process)
  4. Wait 60 s for NT8 to come back up + TickStreamer to reconnect
  5. POST a 60 s no-new-entries gate to the dashboard command queue
     (POST /api/commands  with {"type":"halt_new_entries","duration_s":60})
  6. Log every step at CRITICAL with ISO-8601 timestamps
  7. Send a Telegram alert via core/telegram_notifier
  8. Backoff: after a recovery, wait BACKOFF_S (default 300 s) before
     another stall can re-trigger recovery — rate-limits recursive loops

──────────────────────────────────────────────────────────────────────
Safety interlocks
──────────────────────────────────────────────────────────────────────
- Env flag `PHOENIX_NT8_AUTO_RECOVERY=1` is REQUIRED for the daemon
  to actually act. Without the flag the daemon still polls + detects
  + logs ("WOULD RESTART NT8 ...") but skips the kill / relaunch /
  halt-gate steps. Operator must explicitly opt in.
- `--dry-run` flag has the same effect as the env flag being unset
  (logs the intent, takes no NT8 actions) — useful for one-shot
  manual smoke tests without modifying the environment.
- `--simulate-stall` injects a synthetic live+zero-ticks health
  response so the trigger logic can be verified end-to-end without
  waiting for a real stall.
- If the bridge health endpoint is UNREACHABLE the daemon will NOT
  act. A down bridge ≠ a down NT8 — restarting NT8 when the bridge
  is the failed component would mask the real bug, and bridge
  recovery is the operator's job (or watcher_agent's).

──────────────────────────────────────────────────────────────────────
Why a separate daemon (not a bot-internal feature)
──────────────────────────────────────────────────────────────────────
If Python is the thing that hung, an in-process recovery code path
will never run. All NT8 process operations therefore go through
PowerShell via subprocess — no psutil, no Python-side process
introspection — so this daemon can recover even when the bot is
deaf.

Owned by the P1-4 fix for KNOWN_ISSUES.md F-10 (NT8 silent-stall
costs the trading window).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

# Project root + .env load (so TELEGRAM_TOKEN/CHAT_ID are visible).
PHOENIX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHOENIX_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=PHOENIX_ROOT / ".env", override=True)
except Exception:  # pragma: no cover — defensive only
    pass


# ═══════════════════════════════════════════════════════════════════════
# Constants — every magic number above the fold so a future operator
# (or a debugging Claude session) can audit them in one place.
# ═══════════════════════════════════════════════════════════════════════

BRIDGE_HEALTH_URL = "http://127.0.0.1:8767/health"
DASHBOARD_COMMANDS_URL = "http://127.0.0.1:5000/api/commands"
LOGS_DIR = PHOENIX_ROOT / "logs"
LOG_PATH = LOGS_DIR / "nt8_silent_stall_recovery.log"

POLL_INTERVAL_S = 30           # how often we hit /health
STALL_THRESHOLD_S = 180        # live+zero-ticks must persist > this to trigger
BACKOFF_S = 300                # wait this long after a recovery before another
KILL_WAIT_S = 30               # gap between Stop-Process and relaunch
RELAUNCH_WAIT_S = 60           # gap between relaunch and dashboard halt-gate
HALT_NEW_ENTRIES_DURATION_S = 60
HEALTH_HTTP_TIMEOUT_S = 5
DASHBOARD_HTTP_TIMEOUT_S = 5

# The operator's existing PhoenixBoot shortcut. Single source of truth
# for "how do I bring NT8 back up" — if the operator moves it, only
# this constant changes. Both .lnk (Start-Process) and .bat are fine.
PHOENIX_BOOT_SHORTCUT = PHOENIX_ROOT / "PhoenixStart.bat"

ENV_FLAG = "PHOENIX_NT8_AUTO_RECOVERY"


# ═══════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════

def _configure_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("NT8SilentStallRecovery")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
    )
    fh = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


logger = _configure_logging()


# ═══════════════════════════════════════════════════════════════════════
# Health probe
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class HealthSnapshot:
    reachable: bool
    nt8_status: str = ""
    tick_rate_10s: float = 0.0
    raw: dict | None = None


def fetch_health(url: str = BRIDGE_HEALTH_URL,
                 timeout: float = HEALTH_HTTP_TIMEOUT_S,
                 _opener: Callable[[str, float], dict] | None = None
                 ) -> HealthSnapshot:
    """Fetch /health. Returns reachable=False on any I/O error.

    `_opener` is a test seam — tests inject a lambda that returns a
    dict directly so they don't have to spin up an HTTP server.
    """
    try:
        if _opener is not None:
            data = _opener(url, timeout)
        else:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        return HealthSnapshot(
            reachable=True,
            nt8_status=str(data.get("nt8_status", "")),
            tick_rate_10s=float(data.get("tick_rate_10s", 0.0)),
            raw=data,
        )
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        logger.warning(f"health endpoint unreachable: {e!r}")
        return HealthSnapshot(reachable=False)


# ═══════════════════════════════════════════════════════════════════════
# Recovery actions — every one is small, swappable for tests via the
# RecoveryActions class so the daemon doesn't actually kill NT8 in CI.
# ═══════════════════════════════════════════════════════════════════════

class RecoveryActions:
    """Bundle of side-effecting calls. Tests replace this whole object
    with a recording double to assert the sequence without firing.
    """

    def kill_nt8(self) -> bool:
        """SIGTERM NinjaTrader.exe via PowerShell. Returns True if the
        Stop-Process call exited 0 OR if the process was not running
        (idempotent — "already dead" == "kill succeeded" for our
        recovery sequencing).
        """
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "Stop-Process -Name NinjaTrader -Force -ErrorAction "
                 "SilentlyContinue; exit 0"],
                capture_output=True, timeout=20, text=True,
            )
            logger.critical(
                "Stop-Process NinjaTrader exit=%s stdout=%r stderr=%r",
                r.returncode, r.stdout[:200], r.stderr[:200],
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.error(f"kill_nt8 subprocess failed: {e!r}")
            return False

    def relaunch_nt8(self) -> bool:
        """Start the PhoenixBoot shortcut (which brings NT8 back up
        along with the rest of the stack). Returns True if the
        Start-Process call exited 0.
        """
        if not PHOENIX_BOOT_SHORTCUT.exists():
            logger.error(
                f"relaunch shortcut missing: {PHOENIX_BOOT_SHORTCUT}"
            )
            return False
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"Start-Process -FilePath "
                 f"'{PHOENIX_BOOT_SHORTCUT}'; exit 0"],
                capture_output=True, timeout=20, text=True,
            )
            logger.critical(
                "Start-Process PhoenixBoot exit=%s stdout=%r stderr=%r",
                r.returncode, r.stdout[:200], r.stderr[:200],
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.error(f"relaunch_nt8 subprocess failed: {e!r}")
            return False

    def post_halt_new_entries(self,
                              duration_s: int = HALT_NEW_ENTRIES_DURATION_S
                              ) -> bool:
        """POST the no-new-entries gate command to the dashboard
        command queue. Per task spec: dashboard /api/commands receives
        {"type":"halt_new_entries", "duration_s": N}. Returns True on
        HTTP 2xx, False otherwise.
        """
        body = json.dumps(
            {"type": "halt_new_entries", "duration_s": int(duration_s)}
        ).encode("utf-8")
        req = urllib.request.Request(
            DASHBOARD_COMMANDS_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=DASHBOARD_HTTP_TIMEOUT_S
            ) as resp:
                ok = 200 <= resp.status < 300
                logger.critical(
                    "halt_new_entries POST status=%s duration_s=%s",
                    resp.status, duration_s,
                )
                return ok
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.error(f"halt_new_entries POST failed: {e!r}")
            return False

    def send_telegram(self, text: str) -> bool:
        """Reuse core.telegram_notifier.send_sync. Defensive import so
        the daemon still runs in test envs that strip the module.
        """
        try:
            from core import telegram_notifier  # type: ignore
            return bool(telegram_notifier.send_sync(text))
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(f"telegram send skipped: {e!r}")
            return False

    def sleep(self, seconds: float) -> None:
        """Indirection point so tests can shrink sleeps to zero
        without monkeypatching time.sleep globally.
        """
        time.sleep(seconds)


# ═══════════════════════════════════════════════════════════════════════
# Daemon
# ═══════════════════════════════════════════════════════════════════════

class StallRecoveryDaemon:
    """The trigger state machine + the recovery sequence.

    Public surface tests use:
      - `process_health(snapshot, now)`  — feeds one health observation
                                            and returns True iff a
                                            recovery was triggered
                                            (or WOULD have been, in
                                            dry-run mode).
      - `run_forever()`                   — production entry point.
    """

    def __init__(
        self,
        actions: Optional[RecoveryActions] = None,
        *,
        env_flag_set: Optional[bool] = None,
        dry_run: bool = False,
        stall_threshold_s: int = STALL_THRESHOLD_S,
        backoff_s: int = BACKOFF_S,
    ) -> None:
        self.actions = actions or RecoveryActions()
        # Resolve the env-flag interlock at construction time. Tests
        # pass it explicitly; the run_forever() entry point reads the
        # actual environment variable.
        if env_flag_set is None:
            env_flag_set = os.environ.get(ENV_FLAG, "").strip() == "1"
        self.env_flag_set = bool(env_flag_set)
        self.dry_run = bool(dry_run)
        self.stall_threshold_s = stall_threshold_s
        self.backoff_s = backoff_s

        # Trigger state
        self._stall_started_ts: float | None = None
        self._last_recovery_ts: float | None = None

    # ─────────────────────────────────────────────────────────────────
    # Pure state machine — no I/O, no sleeps, easy to test
    # ─────────────────────────────────────────────────────────────────

    def _is_stalled(self, snap: HealthSnapshot) -> bool:
        return (
            snap.reachable
            and snap.nt8_status == "live"
            and snap.tick_rate_10s == 0.0
        )

    def process_health(self, snap: HealthSnapshot,
                       now: float | None = None) -> bool:
        """Feed one health observation. Returns True iff this call
        triggered (or would-have-triggered) a recovery.
        """
        if now is None:
            now = time.time()

        if not snap.reachable:
            # Bridge down — refuse to act, and reset stall timing so
            # we don't accumulate stall seconds across an outage.
            self._stall_started_ts = None
            logger.info(
                "health endpoint unreachable; refusing to act "
                "(bridge restart is the operator's job)"
            )
            return False

        if not self._is_stalled(snap):
            if self._stall_started_ts is not None:
                logger.info(
                    "stall cleared (nt8_status=%s tick_rate_10s=%s)",
                    snap.nt8_status, snap.tick_rate_10s,
                )
            self._stall_started_ts = None
            return False

        # We are observing a stall. Start the timer if needed.
        if self._stall_started_ts is None:
            self._stall_started_ts = now
            logger.warning(
                "stall observed (live + 0 ticks) — timer started"
            )

        stall_age = now - self._stall_started_ts
        if stall_age <= self.stall_threshold_s:
            return False

        # Backoff check: if we just recovered, don't loop.
        if (self._last_recovery_ts is not None
                and now - self._last_recovery_ts < self.backoff_s):
            remaining = self.backoff_s - (now - self._last_recovery_ts)
            logger.warning(
                "stall persists but in backoff window "
                "(%.0fs remaining); skipping recovery",
                remaining,
            )
            return False

        # Trigger.
        self._do_recovery(stall_age, now)
        return True

    # ─────────────────────────────────────────────────────────────────
    # Recovery sequence
    # ─────────────────────────────────────────────────────────────────

    def _do_recovery(self, stall_age: float, now: float) -> None:
        ts_iso = datetime.fromtimestamp(now).isoformat(timespec="seconds")
        will_act = self.env_flag_set and not self.dry_run

        if not will_act:
            reason = (
                "dry-run flag" if self.dry_run
                else f"env {ENV_FLAG} not set to 1"
            )
            logger.critical(
                "[%s] WOULD RESTART NT8 (stall=%.0fs > %.0fs) — "
                "no action taken (%s)",
                ts_iso, stall_age, self.stall_threshold_s, reason,
            )
            # Still record the "trigger" for backoff so the operator
            # doesn't see this CRITICAL line spam every 30s.
            self._last_recovery_ts = now
            self._stall_started_ts = None
            self.actions.send_telegram(
                f"[Phoenix] NT8 silent-stall detected ({stall_age:.0f}s "
                f"live+0-ticks). Auto-recovery is DISABLED ({reason}). "
                "Manual intervention required."
            )
            return

        logger.critical(
            "[%s] RECOVERY START — stall_age=%.0fs threshold=%.0fs",
            ts_iso, stall_age, self.stall_threshold_s,
        )
        self.actions.send_telegram(
            f"[Phoenix] NT8 silent-stall recovery STARTING "
            f"(stall_age={stall_age:.0f}s). Restarting NinjaTrader.exe."
        )

        killed = self.actions.kill_nt8()
        logger.critical("step 1/5: kill_nt8 -> %s", killed)
        self.actions.sleep(KILL_WAIT_S)

        relaunched = self.actions.relaunch_nt8()
        logger.critical("step 3/5: relaunch_nt8 -> %s", relaunched)
        self.actions.sleep(RELAUNCH_WAIT_S)

        halted = self.actions.post_halt_new_entries(
            HALT_NEW_ENTRIES_DURATION_S
        )
        logger.critical("step 5/5: halt_new_entries -> %s", halted)

        # Record the recovery so the backoff window starts now, even if
        # one of the steps failed — a failed recovery still consumed
        # the operator's attention budget.
        self._last_recovery_ts = now
        self._stall_started_ts = None

        end_iso = datetime.fromtimestamp(
            time.time()).isoformat(timespec="seconds")
        logger.critical(
            "[%s] RECOVERY COMPLETE killed=%s relaunched=%s halted=%s",
            end_iso, killed, relaunched, halted,
        )
        self.actions.send_telegram(
            f"[Phoenix] NT8 recovery complete. "
            f"killed={killed} relaunched={relaunched} "
            f"halt_gate={halted}. Backoff active for "
            f"{self.backoff_s}s before another trigger."
        )

    # ─────────────────────────────────────────────────────────────────
    # Loop
    # ─────────────────────────────────────────────────────────────────

    def run_forever(self,
                    health_fetcher: Callable[[], HealthSnapshot]
                    = fetch_health) -> None:  # pragma: no cover — loop
        logger.info(
            "daemon start: env_flag=%s dry_run=%s threshold=%ss backoff=%ss",
            self.env_flag_set, self.dry_run,
            self.stall_threshold_s, self.backoff_s,
        )
        while True:
            try:
                snap = health_fetcher()
                self.process_health(snap)
            except Exception as e:
                # Never let the daemon die on a transient bug.
                logger.exception(f"loop iteration failed: {e!r}")
            time.sleep(POLL_INTERVAL_S)


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="NT8 silent-stall auto-recovery daemon (P1-4)."
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Detect + log, but NEVER kill or relaunch NT8.",
    )
    p.add_argument(
        "--simulate-stall", action="store_true",
        help="Inject a synthetic live+zero-ticks /health response "
             "every poll to verify trigger logic without a real stall.",
    )
    p.add_argument(
        "--once", action="store_true",
        help="Process exactly one health observation and exit "
             "(diagnostic; pairs with --simulate-stall).",
    )
    p.add_argument(
        "--threshold-s", type=int, default=STALL_THRESHOLD_S,
        help=f"Override stall threshold seconds "
             f"(default {STALL_THRESHOLD_S}).",
    )
    p.add_argument(
        "--backoff-s", type=int, default=BACKOFF_S,
        help=f"Override post-recovery backoff seconds "
             f"(default {BACKOFF_S}).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    daemon = StallRecoveryDaemon(
        dry_run=args.dry_run,
        stall_threshold_s=args.threshold_s,
        backoff_s=args.backoff_s,
    )

    if args.simulate_stall:
        # Pretend we've been stalled long enough to trigger by
        # backdating the stall start. Useful smoke test.
        snap = HealthSnapshot(
            reachable=True, nt8_status="live", tick_rate_10s=0.0,
            raw={"_simulated": True},
        )
        daemon._stall_started_ts = (
            time.time() - args.threshold_s - 1
        )
        daemon.process_health(snap)
        return 0

    if args.once:
        daemon.process_health(fetch_health())
        return 0

    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
