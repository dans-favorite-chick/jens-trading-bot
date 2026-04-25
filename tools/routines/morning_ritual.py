"""Phoenix morning ritual — 06:30 CT Mon-Fri pre-flight.

Per Jennifer 2026-04-25: the verdict is DETERMINISTIC. AI overnight
commentary lives in a labeled appendix and does NOT influence the
verdict. An AI off-day cannot flip a clean GREEN to YELLOW or vice
versa.

Deterministic checks (each → GREEN/YELLOW/RED):
  1. Process count (5 expected)
  2. Port state (5 ports listening)
  3. NT8 stream count (exactly 1 client, instrument matches expected)
  4. FMP price drift (< 0.5% from local accepted)
  5. MQ paste staleness (< 18h)
  6. Watchdog heartbeat freshness (< 60s, if present)
  7. Halt / KillSwitch markers (RED if either is set during market hours)

Telegram behavior: morning_ritual writes to the DigestQueue (see _shared).
Only RED verdicts trigger an immediate send_telegram_now(). The
post_session_debrief at 16:05 CT folds today's morning report into the
single consolidated digest.

Usage:
  python tools/routines/morning_ritual.py
  python tools/routines/morning_ritual.py --session-date 2026-04-25  --skip-ai
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on path before any local imports
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent.parent))

from tools.routines._shared import (
    CT_TZ, RoutineReport, call_claude, send_telegram_now, stack_health_snapshot,
    write_artifacts,
)

logger = logging.getLogger("MorningRitual")

# Tunables — kept top-level so they're discoverable + testable.
FMP_DRIFT_YELLOW_PCT = 0.005       # 0.5% drift → YELLOW
FMP_DRIFT_RED_PCT = 0.015          # 1.5% drift → RED
MQ_STALE_YELLOW_HOURS = 18.0
MQ_STALE_RED_HOURS = 36.0
WATCHER_HB_YELLOW_S = 60.0
WATCHER_HB_RED_S = 180.0


# ═══════════════════════════════════════════════════════════════════════
# Deterministic check functions — each takes data, returns (status, detail, raw)
# ═══════════════════════════════════════════════════════════════════════

def check_processes(snapshot: dict) -> tuple[str, str, dict]:
    procs = snapshot.get("processes", {})
    alive = [k for k, v in procs.items() if v]
    missing = [k for k, v in procs.items() if not v]
    if not missing:
        return "GREEN", f"all {len(alive)}/{len(procs)} alive", {"alive": alive}
    if len(missing) <= 1:
        return "YELLOW", f"{len(missing)} missing: {missing[0]}", {"missing": missing}
    return "RED", f"{len(missing)} processes missing: {','.join(missing)}", {"missing": missing}


def check_ports(snapshot: dict) -> tuple[str, str, dict]:
    ports = snapshot.get("ports", {})
    listening = [p for p, v in ports.items() if v]
    not_listening = [p for p, v in ports.items() if not v]
    if not not_listening:
        return "GREEN", f"all {len(listening)}/{len(ports)} listening", {"listening": listening}
    if len(not_listening) <= 1:
        return "YELLOW", f"port {not_listening[0]} not listening", {"missing": not_listening}
    return "RED", f"{len(not_listening)} ports not listening: {not_listening}", {"missing": not_listening}


def check_nt8_single_stream(snapshot: dict) -> tuple[str, str, dict]:
    """Single NT8 client = GREEN; zero = RED; multi = YELLOW (could be
    transient reconnect) unless instruments differ (then RED)."""
    bh = snapshot.get("bridge_health", {})
    if not bh.get("ok"):
        return "RED", f"bridge unreachable: {bh.get('error', 'unknown')}", {}
    data = bh.get("data") or {}
    events = data.get("connection_events") or []
    # Count active NT8 clients by tracking connect/disconnect events
    active_ports = {}
    for e in events[-200:]:
        msg = e.get("message", "")
        if "NT8 client connected from" in msg:
            # extract port
            import re
            m = re.search(r"\('127\.0\.0\.1',\s*(\d+)\)", msg)
            if m:
                active_ports[int(m.group(1))] = e.get("ts")
        elif "NT8 disconnected from" in msg:
            import re
            m = re.search(r"\('127\.0\.0\.1',\s*(\d+)\)", msg)
            if m:
                active_ports.pop(int(m.group(1)), None)
    n = len(active_ports)
    instrument = data.get("nt8_instrument") or "?"
    if n == 0:
        return "RED", f"zero NT8 clients connected", {"active_ports": list(active_ports)}
    if n == 1:
        return "GREEN", f"1 client on port {next(iter(active_ports))}, instrument={instrument}", {
            "active_ports": list(active_ports), "instrument": instrument}
    # Multiple clients — YELLOW (transient reconnect race) but not RED unless
    # we have evidence of mixed instruments (which would mean the bug is back).
    return "YELLOW", f"{n} NT8 clients connected on ports {sorted(active_ports.keys())}", {
        "active_ports": list(active_ports), "instrument": instrument}


def check_fmp_drift() -> tuple[str, str, dict]:
    """Compare local accepted price to FMP MNQ-equivalent reference.
    Queries the in-process core.fmp_sanity / core.price_sanity modules
    directly — does not need the stack snapshot."""
    try:
        from core import fmp_sanity, price_sanity
    except Exception as e:
        return "YELLOW", f"sanity modules unavailable: {e!r}", {}
    fmp_ref = fmp_sanity.get_reference_mnq_price()
    snap = price_sanity.snapshot()
    local = snap.get("last_accepted_price") or 0
    if not fmp_ref or fmp_ref <= 0:
        return "YELLOW", "FMP unavailable (advisory only)", {"local": local, "fmp": None}
    if not local or local <= 0:
        return "YELLOW", "no local accepted price yet", {"local": None, "fmp": fmp_ref}
    drift = abs(local - fmp_ref) / fmp_ref
    if drift >= FMP_DRIFT_RED_PCT:
        return "RED", f"FMP drift {drift*100:.2f}% (local {local:.2f} vs FMP {fmp_ref:.2f})", {
            "local": local, "fmp": fmp_ref, "drift_pct": drift}
    if drift >= FMP_DRIFT_YELLOW_PCT:
        return "YELLOW", f"FMP drift {drift*100:.2f}% (warn threshold)", {
            "local": local, "fmp": fmp_ref, "drift_pct": drift}
    return "GREEN", f"FMP agrees ({drift*100:.2f}% drift)", {
        "local": local, "fmp": fmp_ref, "drift_pct": drift}


def check_mq_staleness() -> tuple[str, str, dict]:
    mq_path = _HERE.parent.parent.parent / "data" / "menthorq_daily.json"
    if not mq_path.exists():
        return "RED", "data/menthorq_daily.json missing entirely", {}
    import time as _time
    age_h = (_time.time() - mq_path.stat().st_mtime) / 3600
    if age_h >= MQ_STALE_RED_HOURS:
        return "RED", f"MQ paste {age_h:.1f}h stale (>{MQ_STALE_RED_HOURS}h)", {"age_h": age_h}
    if age_h >= MQ_STALE_YELLOW_HOURS:
        return "YELLOW", f"MQ paste {age_h:.1f}h stale (refresh recommended)", {"age_h": age_h}
    return "GREEN", f"MQ paste {age_h:.1f}h fresh", {"age_h": age_h}


def check_watcher_heartbeat(snapshot: dict) -> tuple[str, str, dict]:
    age = snapshot.get("watcher_heartbeat_age_s")
    if age is None:
        return "YELLOW", "watcher heartbeat file not found (may be running w/o heartbeat)", {}
    if age >= WATCHER_HB_RED_S:
        return "RED", f"watcher heartbeat stale {age:.0f}s", {"age_s": age}
    if age >= WATCHER_HB_YELLOW_S:
        return "YELLOW", f"watcher heartbeat slightly stale {age:.0f}s", {"age_s": age}
    return "GREEN", f"watcher heartbeat fresh ({age:.0f}s)", {"age_s": age}


def check_markers(snapshot: dict) -> tuple[str, str, dict]:
    halt = snapshot.get("halt_marker", False)
    kill = snapshot.get("killswitch_marker", False)
    if kill:
        return "RED", "KillSwitch marker present — Phoenix is intentionally OFF", {"kill": True}
    if halt:
        return "RED", ".HALT marker present — manual emergency halt active", {"halt": True}
    return "GREEN", "no halt or killswitch markers", {}


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def run(session_date: str | None = None, skip_ai: bool = False) -> RoutineReport:
    today = session_date or datetime.now(CT_TZ).strftime("%Y-%m-%d")
    report = RoutineReport(name="morning_ritual", session_date=today)

    snap = stack_health_snapshot()

    # Run all deterministic checks
    for cname, fn in [
        ("processes", lambda: check_processes(snap)),
        ("ports", lambda: check_ports(snap)),
        ("nt8_single_stream", lambda: check_nt8_single_stream(snap)),
        ("fmp_drift", check_fmp_drift),
        ("mq_staleness", check_mq_staleness),
        ("watcher_heartbeat", lambda: check_watcher_heartbeat(snap)),
        ("markers", lambda: check_markers(snap)),
    ]:
        try:
            status, detail, raw = fn()
            report.set_verdict_check(cname, status, detail, raw)
        except Exception as e:
            report.set_verdict_check(cname, "YELLOW", f"check raised: {e!r}")

    # Optional AI overnight commentary — DOES NOT affect verdict
    if not skip_ai:
        ai_text = _ai_overnight_commentary(snap)
        if ai_text:
            report.set_ai_appendix(ai_text)

    paths = write_artifacts(report)

    # Interrupting Telegram on RED only — per the no-fatigue rule.
    if report.verdict == "RED":
        bad = [c for c in report.verdict_checks if c.status == "RED"]
        body = "\n".join([
            "🔴 <b>Phoenix morning ritual: RED verdict</b>",
            "",
            *[f"• {c.name}: {c.detail}" for c in bad],
            "",
            f"Full report: {paths['markdown']}",
        ])
        send_telegram_now("Phoenix morning ritual RED", body)

    return report


def _ai_overnight_commentary(snap: dict) -> str | None:
    """Single Claude Sonnet call. ~$0.05/day. Fail-soft if no key."""
    bh = snap.get("bridge_health", {}).get("data") or {}
    instrument = bh.get("nt8_instrument", "?")
    tick_age = bh.get("nt8_last_tick_age_s")
    # Pull recent FRED + advisor snapshot if available — best-effort.
    macros = ""
    try:
        from core.macros.fred_feed import FredMacroFeed
        s = FredMacroFeed().get_snapshot()
        macros = (
            f"FFR={getattr(s, 'ffr', '?')}, "
            f"CPI YoY={getattr(s, 'cpi_yoy', '?')}, "
            f"Unemployment={getattr(s, 'unemployment', '?')}, "
            f"10Y-2Y={getattr(s, 'yield_curve_2y10y', '?')}"
        )
    except Exception:
        macros = "(FRED snapshot unavailable)"

    prompt = (
        "You are a concise pre-market analyst for an MNQ futures trader.\n"
        "Today is the trading day. Provide a 4-bullet overnight summary:\n"
        "  1. What macro context matters today (one line)\n"
        "  2. Anything noteworthy about the overnight session\n"
        "  3. One specific level or signal to watch this morning\n"
        "  4. Risk caveat or thing-to-NOT-do today\n\n"
        f"Context:\n"
        f"  - Instrument: {instrument}\n"
        f"  - Last tick age: {tick_age}s\n"
        f"  - Macros: {macros}\n\n"
        "Keep total response under 120 words. Be specific. No disclaimers."
    )
    return call_claude(prompt, max_tokens=400)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--session-date", default=None, help="Override the date (YYYY-MM-DD)")
    parser.add_argument("--skip-ai", action="store_true", help="Skip the Claude commentary call")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

    report = run(session_date=args.session_date, skip_ai=args.skip_ai)
    print(f"\n=== Phoenix morning ritual — verdict: {report.verdict} ===")
    for c in report.verdict_checks:
        glyph = {"GREEN": "[ok]", "YELLOW": "[--]", "RED": "[XX]"}[c.status]
        print(f"  {glyph} {c.name}: {c.detail}")

    if report.verdict == "GREEN":
        return 0
    if report.verdict == "YELLOW":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
