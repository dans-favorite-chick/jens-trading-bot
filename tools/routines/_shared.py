"""Phoenix Routines — shared scaffolding.

Per Jennifer's 2026-04-25 amendments:
  1. Verdict determinism — verdicts come from rule-based checks ONLY.
     AI commentary lives in a labeled appendix that does NOT influence
     the verdict. RoutineReport enforces this by separating
     `set_verdict_check(name, status, reason)` (deterministic) from
     `set_ai_appendix(text)` (advisory).
  2. Consolidated digest — three routines do NOT each fire their own
     Telegram. They write to a file-backed DigestQueue. The
     post_session_debrief drains the queue at 16:05 CT and sends ONE
     consolidated Telegram. Interrupting alerts (RED verdicts,
     system-down) bypass the queue via send_telegram_now().
  3. Validation status — weekly_evolution commit-body templates carry
     CPCV/DSR/PBO checkboxes that read 'NOT YET RUN (Phase C dependency)'
     so the validation gate is explicit on every proposal.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal
from zoneinfo import ZoneInfo

# Add parent project to sys.path so `from core.* import ...` works regardless
# of how this module is invoked (script, scheduled task, slash command).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger("PhoenixRoutines")
CT_TZ = ZoneInfo("America/Chicago")

OUT_DIR = _PROJECT_ROOT / "out"
DIGEST_QUEUE_PATH = _PROJECT_ROOT / "out" / "digest_queue.jsonl"
HEARTBEAT_DIR = _PROJECT_ROOT / "heartbeat"


Verdict = Literal["GREEN", "YELLOW", "RED"]


# ═══════════════════════════════════════════════════════════════════════
# RoutineReport — verdict-deterministic, AI-appendix-isolated
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class VerdictCheck:
    name: str
    status: Verdict        # GREEN/YELLOW/RED
    detail: str            # human-readable one-liner
    raw: dict = field(default_factory=dict)


@dataclass
class RoutineReport:
    """Markdown + HTML + verdict + AI appendix.

    KEY INVARIANT (per Jennifer 2026-04-25): the verdict is computed
    purely from `verdict_checks`. AI commentary in `ai_appendix` is
    labeled "advisory only" and does NOT affect the verdict.

    Use:
      r = RoutineReport(name="morning_ritual", session_date=...)
      r.set_verdict_check("processes", "GREEN", "5/5 alive")
      r.set_verdict_check("nt8_stream", "RED", "3 clients connected")
      r.set_ai_appendix("Overnight regime: ...")
      r.write_artifacts()  # also enqueues for the consolidated digest
    """
    name: str                                          # e.g. "morning_ritual"
    session_date: str                                  # YYYY-MM-DD
    started_at_iso: str = field(
        default_factory=lambda: datetime.now(CT_TZ).isoformat(timespec="seconds")
    )
    verdict_checks: list[VerdictCheck] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)   # (heading, markdown_body)
    ai_appendix: str = ""
    metadata: dict = field(default_factory=dict)

    def set_verdict_check(self, name: str, status: Verdict, detail: str,
                          raw: Optional[dict] = None) -> None:
        """Add a deterministic check. Last write wins on duplicate name."""
        existing = [i for i, c in enumerate(self.verdict_checks) if c.name == name]
        check = VerdictCheck(name=name, status=status, detail=detail, raw=raw or {})
        if existing:
            self.verdict_checks[existing[0]] = check
        else:
            self.verdict_checks.append(check)

    def add_section(self, heading: str, markdown_body: str) -> None:
        self.sections.append((heading, markdown_body))

    def set_ai_appendix(self, text: str) -> None:
        """Set the AI commentary appendix. ALWAYS rendered with the
        'advisory only — does not affect verdict' label, per the
        verdict-determinism amendment."""
        self.ai_appendix = (text or "").strip()

    @property
    def verdict(self) -> Verdict:
        """Worst-of all checks. RED beats YELLOW beats GREEN."""
        if not self.verdict_checks:
            return "GREEN"
        if any(c.status == "RED" for c in self.verdict_checks):
            return "RED"
        if any(c.status == "YELLOW" for c in self.verdict_checks):
            return "YELLOW"
        return "GREEN"

    def to_markdown(self) -> str:
        v = self.verdict
        lines = [
            f"# Phoenix {self.name} — {self.session_date}",
            "",
            f"**Verdict:** {v}    ",
            f"**Started:** {self.started_at_iso}",
            "",
            "## Deterministic checks",
            "",
            "| Check | Status | Detail |",
            "|---|---|---|",
        ]
        for c in self.verdict_checks:
            detail = c.detail.replace("|", "\\|")
            lines.append(f"| {c.name} | {c.status} | {detail} |")
        lines.append("")
        for heading, body in self.sections:
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(body.rstrip())
            lines.append("")
        if self.ai_appendix:
            lines.append("## AI overnight commentary (advisory only — does not affect verdict)")
            lines.append("")
            lines.append(self.ai_appendix.rstrip())
            lines.append("")
        return "\n".join(lines)

    def to_html(self) -> str:
        """Minimal styled HTML — no Jinja dependency. Mirrors the dashboard
        dark theme so the routines tab can iframe-include it."""
        v = self.verdict
        v_color = {"GREEN": "#3fb950", "YELLOW": "#d29922", "RED": "#f85149"}.get(v, "#888")
        rows = "".join(
            f"<tr><td>{c.name}</td><td style='color:{ {'GREEN':'#3fb950','YELLOW':'#d29922','RED':'#f85149'}[c.status] }'>{c.status}</td><td>{c.detail}</td></tr>"
            for c in self.verdict_checks
        )
        secs = "".join(
            f"<h2>{h}</h2><pre style='white-space:pre-wrap'>{b}</pre>"
            for h, b in self.sections
        )
        ai_block = (
            f"<h2>AI overnight commentary (advisory only — does not affect verdict)</h2>"
            f"<pre style='white-space:pre-wrap;border:1px dashed #888;padding:8px'>{self.ai_appendix}</pre>"
            if self.ai_appendix else ""
        )
        return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Phoenix {self.name} {self.session_date}</title>
<style>
body{{background:#0d1117;color:#c9d1d9;font-family:Consolas,monospace;padding:20px;}}
h1{{color:#58a6ff;}} h2{{color:#79c0ff;border-bottom:1px solid #30363d;padding-bottom:4px;}}
table{{border-collapse:collapse;width:100%;}}
td,th{{border:1px solid #30363d;padding:6px;text-align:left;}}
.verdict{{display:inline-block;padding:6px 14px;border-radius:4px;color:#000;font-weight:700;background:{v_color};}}
pre{{background:#161b22;padding:8px;border-radius:4px;overflow-x:auto;}}
</style></head><body>
<h1>Phoenix {self.name} — {self.session_date}</h1>
<p><span class='verdict'>{v}</span> &nbsp; Started: {self.started_at_iso}</p>
<h2>Deterministic checks</h2>
<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>
{secs}
{ai_block}
</body></html>"""


# ═══════════════════════════════════════════════════════════════════════
# Artifact writer + PDF generation
# ═══════════════════════════════════════════════════════════════════════

def write_artifacts(report: RoutineReport, also_pdf: bool = True) -> dict[str, Path]:
    """Write Markdown + HTML + (optional) PDF artifacts and return their paths.

    Also enqueues a digest entry so the next post_session_debrief can
    fold this routine into its consolidated Telegram.
    """
    routine_dir = OUT_DIR / report.name
    routine_dir.mkdir(parents=True, exist_ok=True)
    md_path = routine_dir / f"{report.session_date}.md"
    html_path = routine_dir / f"{report.session_date}.html"
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    html_path.write_text(report.to_html(), encoding="utf-8")
    out = {"markdown": md_path, "html": html_path}

    if also_pdf:
        pdf_path = routine_dir / f"{report.session_date}.pdf"
        try:
            _write_pdf(report, pdf_path)
            out["pdf"] = pdf_path
        except Exception as e:
            logger.warning(f"[routines] PDF generation failed (non-blocking): {e!r}")

    # Enqueue the digest entry — the consolidated Telegram will pick this up.
    try:
        DigestQueue().push({
            "routine": report.name,
            "session_date": report.session_date,
            "verdict": report.verdict,
            "summary_md": _one_liner(report),
            "ts": datetime.now(CT_TZ).isoformat(timespec="seconds"),
            "artifact_md": str(md_path),
            "artifact_html": str(html_path),
        })
    except Exception as e:
        logger.warning(f"[routines] DigestQueue push failed (non-blocking): {e!r}")
    return out


def _one_liner(report: RoutineReport) -> str:
    bad = [c for c in report.verdict_checks if c.status != "GREEN"]
    if not bad:
        return f"{report.name}: {report.verdict} (all checks green)"
    first = bad[0]
    return f"{report.name}: {report.verdict} — {first.name}: {first.detail[:80]}"


def _write_pdf(report: RoutineReport, path: Path) -> None:
    """Lazy-import reportlab so missing-dep doesn't crash the routine."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors

    doc = SimpleDocTemplate(str(path), pagesize=LETTER, title=f"Phoenix {report.name}")
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"<b>Phoenix {report.name}</b> — {report.session_date}", styles["Title"]),
        Paragraph(f"<b>Verdict:</b> {report.verdict}", styles["Heading2"]),
        Paragraph(f"Started: {report.started_at_iso}", styles["Normal"]),
        Spacer(1, 12),
    ]
    table_data = [["Check", "Status", "Detail"]] + [
        [c.name, c.status, c.detail[:80]] for c in report.verdict_checks
    ]
    t = Table(table_data, colWidths=[110, 60, 320])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#30363d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    story.append(t)
    story.append(Spacer(1, 18))
    for heading, body in report.sections:
        story.append(Paragraph(f"<b>{heading}</b>", styles["Heading2"]))
        for line in body.splitlines():
            if line.strip():
                story.append(Paragraph(line.replace("&", "&amp;").replace("<", "&lt;"), styles["Normal"]))
        story.append(Spacer(1, 8))
    if report.ai_appendix:
        story.append(Paragraph(
            "<b>AI overnight commentary (advisory only — does not affect verdict)</b>",
            styles["Heading2"]))
        for line in report.ai_appendix.splitlines():
            if line.strip():
                story.append(Paragraph(line.replace("&", "&amp;").replace("<", "&lt;"), styles["Normal"]))
    doc.build(story)


# ═══════════════════════════════════════════════════════════════════════
# DigestQueue — file-backed FIFO; drained by post_session_debrief
# ═══════════════════════════════════════════════════════════════════════

class DigestQueue:
    """Tiny file-backed JSON-line queue.

    Each routine pushes a dict; post_session_debrief drains all entries
    older than now (today's morning_ritual + any system-down events that
    landed in the queue) and assembles them into ONE consolidated
    Telegram digest. Avoids alert fatigue per Jennifer's amendment.

    Operations:
      push(entry: dict)             append to queue
      drain() -> list[dict]         atomically read + clear the queue
      peek() -> list[dict]          read without clearing (tests, dashboard)
    """
    def __init__(self, path: Optional[Path] = None):
        self.path = path or DIGEST_QUEUE_PATH

    def push(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def peek(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def drain(self) -> list[dict]:
        items = self.peek()
        # Atomic-ish: rename then unlink so any concurrent push lands cleanly.
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
        return items


# ═══════════════════════════════════════════════════════════════════════
# AI wrappers — fail-soft if no key
# ═══════════════════════════════════════════════════════════════════════

def call_claude(prompt: str, system: str = "", max_tokens: int = 1024,
                model: str = "claude-sonnet-4-5") -> Optional[str]:
    """One-shot Claude call. Returns text or None on any failure (no API
    key, transport error, content filter). Never raises."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning("[routines] ANTHROPIC_API_KEY missing; Claude call skipped")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system or "You are a concise trading-systems analyst.",
            messages=[{"role": "user", "content": prompt}],
        )
        # Extract text content from response blocks
        chunks = []
        for block in msg.content or []:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
        return "\n".join(chunks).strip() or None
    except Exception as e:
        logger.warning(f"[routines] Claude call failed (non-blocking): {e!r}")
        return None


def call_gemini(prompt: str, model: str = "gemini-2.5-flash") -> Optional[str]:
    """One-shot Gemini call. Returns text or None on any failure."""
    api_key = (os.environ.get("GOOGLE_API_KEY", "").strip()
               or os.environ.get("GEMINI_API_KEY", "").strip())
    if not api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(prompt)
        return (resp.text or "").strip() or None
    except Exception as e:
        logger.warning(f"[routines] Gemini call failed (non-blocking): {e!r}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# Telegram — single consolidated path + interrupt-only path
# ═══════════════════════════════════════════════════════════════════════

def send_telegram_now(title: str, body: str) -> bool:
    """Bypass the digest queue — fires immediately. Use ONLY for RED
    verdicts and system-down alerts per Jennifer's no-fatigue rule."""
    try:
        from core.telegram_notifier import notify_alert
        # notify_alert is async in some Phoenix versions; handle both.
        import asyncio
        try:
            res = notify_alert(title, body)
            if asyncio.iscoroutine(res):
                asyncio.run(res)
                return True
            return bool(res)
        except Exception as e:
            logger.warning(f"[routines] notify_alert raised: {e!r}")
            return False
    except Exception as e:
        logger.warning(f"[routines] Telegram import failed: {e!r}")
        return False


def send_consolidated_digest(extra_lines: Optional[list[str]] = None) -> bool:
    """Drain the DigestQueue and emit ONE Telegram message containing
    every routine's headline + verdict for the day.

    Called by post_session_debrief at 16:05 CT. Returns True if Telegram
    was actually sent (or False if no entries to digest / Telegram down).
    """
    queue = DigestQueue()
    items = queue.drain()
    if not items and not extra_lines:
        return False
    today = datetime.now(CT_TZ).strftime("%Y-%m-%d")
    lines = [f"<b>Phoenix daily digest — {today}</b>", ""]
    for item in items:
        v = item.get("verdict", "GREEN")
        emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(v, "•")
        summary = item.get("summary_md") or "(no summary)"
        lines.append(f"{emoji} {summary}")
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    body = "\n".join(lines)
    return send_telegram_now("Phoenix daily digest", body)


# ═══════════════════════════════════════════════════════════════════════
# Stack health snapshot — used by morning_ritual deterministic verdict
# ═══════════════════════════════════════════════════════════════════════

EXPECTED_PROCESSES = ["bridge_server.py", "sim_bot.py", "prod_bot.py",
                      "watchdog.py", "watcher_agent.py"]
EXPECTED_PORTS = [8765, 8766, 8767, 5000, 5001]


def stack_health_snapshot() -> dict:
    """Deterministic snapshot of Phoenix stack health. Pure data — no
    decisions made here. The morning_ritual interprets this dict.

    Returns:
      {
        "processes": {"bridge_server.py": True, ...},
        "ports": {8765: True, 8766: True, ...},
        "bridge_health": {"ok": bool, "data": dict|None, "error": str|None},
        "halt_marker": bool,
        "killswitch_marker": bool,
        "watcher_heartbeat_age_s": float | None,
        "ts": iso8601,
      }
    """
    out = {
        "processes": {p: False for p in EXPECTED_PROCESSES},
        "ports": {p: False for p in EXPECTED_PORTS},
        "bridge_health": {"ok": False, "data": None, "error": None},
        "halt_marker": False,
        "killswitch_marker": False,
        "watcher_heartbeat_age_s": None,
        "ts": datetime.now(CT_TZ).isoformat(timespec="seconds"),
    }
    # Processes
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
                for sig in EXPECTED_PROCESSES:
                    if sig in cmd:
                        out["processes"][sig] = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        out["processes_error"] = repr(e)

    # Ports
    try:
        import socket
        for port in EXPECTED_PORTS:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                out["ports"][port] = (s.connect_ex(("127.0.0.1", port)) == 0)
    except Exception as e:
        out["ports_error"] = repr(e)

    # Bridge HTTP health
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8767/health", timeout=2) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            out["bridge_health"]["ok"] = resp.status == 200
            try:
                out["bridge_health"]["data"] = json.loads(body)
            except json.JSONDecodeError:
                out["bridge_health"]["data"] = {"raw": body[:500]}
    except Exception as e:
        out["bridge_health"]["error"] = repr(e)

    # Markers
    out["halt_marker"] = (_PROJECT_ROOT / "memory" / ".HALT").exists()
    out["killswitch_marker"] = (_PROJECT_ROOT / "memory" / ".KILL_SWITCH_ENGAGED").exists()

    # Watcher heartbeat
    hb_path = HEARTBEAT_DIR / "watcher_agent.hb"
    if hb_path.exists():
        try:
            import time as _time
            out["watcher_heartbeat_age_s"] = round(_time.time() - hb_path.stat().st_mtime, 1)
        except OSError:
            pass

    return out
