"""Phoenix post-session debrief — 16:05 CT Mon-Fri.

Chains to PhoenixGrading (which fires at 16:00 CT and writes
out/grades/YYYY-MM-DD.json). Five minutes later this routine:
  1. Reads today's grade
  2. Computes risk metrics from logs/trade_memory.json
  3. Scans logs for new error signatures vs 7-day baseline
  4. Reuses the existing AI debrief (agents/session_debriefer.py)
  5. Assembles a PDF
  6. Drains the DigestQueue and sends ONE consolidated Telegram (folds in
     this morning's morning_ritual report). Per Jennifer's no-fatigue rule.

Usage:
  python tools/routines/post_session_debrief.py
  python tools/routines/post_session_debrief.py --session-date 2026-04-25
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent.parent))

from tools.routines._shared import (
    CT_TZ, RoutineReport, send_consolidated_digest, write_artifacts,
    DigestQueue,
)

logger = logging.getLogger("PostSessionDebrief")

PROJECT_ROOT = _HERE.parent.parent.parent
GRADES_DIR = PROJECT_ROOT / "out" / "grades"
TRADE_MEMORY_PATH = PROJECT_ROOT / "logs" / "trade_memory.json"
LOG_DIR = PROJECT_ROOT / "logs"


# ═══════════════════════════════════════════════════════════════════════
# Risk metrics — pure stdlib computation from trade_memory.json
# ═══════════════════════════════════════════════════════════════════════

def compute_risk_metrics(trades: list[dict]) -> dict:
    """Sharpe, max drawdown, profit factor, win rate, total P&L.

    All computed in pure stdlib so this module never adds a numpy/pandas
    dependency. Returns a dict ready for markdown rendering.
    """
    pnls = []
    for t in trades:
        try:
            v = t.get("pnl") or t.get("realized_pnl") or t.get("P&L") or 0
            pnls.append(float(v))
        except (TypeError, ValueError):
            continue
    if not pnls:
        return {"trades": 0, "total_pnl": 0.0, "win_rate": None,
                "profit_factor": None, "sharpe": None, "max_drawdown": 0.0}

    total = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls) if pnls else 0
    pf = (sum(wins) / sum(losses)) if losses else float("inf") if wins else None

    # Per-trade Sharpe (mean / stdev) — annualization is meaningless on
    # one day; we report the raw daily ratio.
    if len(pnls) >= 2:
        mean = total / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        sd = var ** 0.5
        sharpe = (mean / sd) if sd > 0 else None
    else:
        sharpe = None

    # Max drawdown on cumulative equity curve
    eq = []
    s = 0.0
    for p in pnls:
        s += p
        eq.append(s)
    peak = -float("inf")
    max_dd = 0.0
    for e in eq:
        if e > peak:
            peak = e
        max_dd = min(max_dd, e - peak)

    return {
        "trades": len(pnls),
        "total_pnl": round(total, 2),
        "win_rate": round(win_rate, 3),
        "profit_factor": (round(pf, 2) if pf and pf != float("inf") else pf),
        "sharpe": (round(sharpe, 2) if sharpe is not None else None),
        "max_drawdown": round(max_dd, 2),
        "wins": len(wins),
        "losses": len(losses),
    }


# ═══════════════════════════════════════════════════════════════════════
# Log scan for new error signatures
# ═══════════════════════════════════════════════════════════════════════

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d+\s*")
_ERR_RE = re.compile(r"\b(ERROR|EXCEPTION|CRITICAL|Traceback)\b")


def scan_new_error_signatures(logs: list[Path], baseline_days: int = 7) -> dict:
    """Returns {new_signatures: [...], total_errors_today: N, baseline_signatures: M}."""
    today_sigs = set()
    today_count = 0
    today_str = datetime.now(CT_TZ).strftime("%Y-%m-%d")
    for p in logs:
        if not p.exists():
            continue
        try:
            with p.open(encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if today_str not in line:
                        continue
                    if not _ERR_RE.search(line):
                        continue
                    today_count += 1
                    # Strip timestamp; signature = first 100 chars of message
                    sig = _TS_RE.sub("", line).strip()[:100]
                    today_sigs.add(sig)
        except OSError:
            continue

    # Baseline: prior baseline_days days' unique error sigs
    baseline_sigs = set()
    cutoff = (datetime.now(CT_TZ) - timedelta(days=baseline_days)).strftime("%Y-%m-%d")
    for p in logs:
        if not p.exists():
            continue
        try:
            with p.open(encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if today_str in line:
                        continue
                    # Crude date filter: only lines newer than cutoff
                    if line[:10] < cutoff:
                        continue
                    if not _ERR_RE.search(line):
                        continue
                    sig = _TS_RE.sub("", line).strip()[:100]
                    baseline_sigs.add(sig)
        except OSError:
            continue

    new_today = sorted(today_sigs - baseline_sigs)
    return {
        "new_signatures": new_today[:20],
        "total_errors_today": today_count,
        "baseline_signatures": len(baseline_sigs),
        "today_signatures": len(today_sigs),
    }


# ═══════════════════════════════════════════════════════════════════════
# Main runner
# ═══════════════════════════════════════════════════════════════════════

def run(session_date: str | None = None, wait_for_grade_s: int = 120) -> RoutineReport:
    today = session_date or datetime.now(CT_TZ).strftime("%Y-%m-%d")
    report = RoutineReport(name="post_session_debrief", session_date=today)

    # 1. Wait for / read today's grade
    grade_path = GRADES_DIR / f"{today}.json"
    grade = _wait_for_grade(grade_path, wait_for_grade_s)
    if grade:
        passed = sum(1 for r in grade.get("results", []) if r.get("overall_pass"))
        total = len(grade.get("results", []))
        report.set_verdict_check(
            "grade_present",
            "GREEN" if total > 0 else "YELLOW",
            f"score: {passed}/{total} predictions passed",
            grade,
        )
        # Build markdown table from grade results
        rows = "\n".join(
            f"| {r.get('prediction_id')} | {r.get('label')} | {'PASS' if r.get('overall_pass') else 'FAIL'} | {r.get('qual_observation', '')[:60]} |"
            for r in grade.get("results", [])
        )
        body = f"| ID | Label | Result | Detail |\n|---|---|---|---|\n{rows}"
        report.add_section("Today's grade", body)
    else:
        report.set_verdict_check(
            "grade_present", "YELLOW",
            f"grade file {grade_path.name} not found after {wait_for_grade_s}s wait",
        )

    # 2. Risk metrics
    trades_today = _load_today_trades(today)
    metrics = compute_risk_metrics(trades_today)
    body = (
        f"- Trades: {metrics['trades']} ({metrics['wins']} wins / {metrics['losses']} losses)\n"
        f"- Total P&L: ${metrics['total_pnl']}\n"
        f"- Win rate: {metrics['win_rate']}\n"
        f"- Profit factor: {metrics['profit_factor']}\n"
        f"- Daily Sharpe: {metrics['sharpe']}\n"
        f"- Max drawdown: ${metrics['max_drawdown']}\n"
    )
    report.add_section("Risk metrics", body)
    if metrics["trades"] == 0:
        report.set_verdict_check("trades_today", "YELLOW", "no trades booked today")
    else:
        report.set_verdict_check(
            "trades_today", "GREEN",
            f"{metrics['trades']} trades, P&L ${metrics['total_pnl']}",
            metrics,
        )

    # 3. New error signatures
    err_scan = scan_new_error_signatures([
        LOG_DIR / "sim_bot_stdout.log",
        LOG_DIR / "prod_bot_stdout.log",
        LOG_DIR / "watchdog.log",
    ])
    if err_scan["new_signatures"]:
        report.set_verdict_check(
            "new_errors", "YELLOW",
            f"{len(err_scan['new_signatures'])} new error signature(s) vs 7-day baseline",
            err_scan,
        )
        report.add_section(
            "New error signatures (vs 7-day baseline)",
            "\n".join(f"- `{s[:140]}`" for s in err_scan["new_signatures"]),
        )
    else:
        report.set_verdict_check(
            "new_errors", "GREEN",
            f"no new error signatures ({err_scan['total_errors_today']} errors today; all known)",
            err_scan,
        )

    # 4. AI debrief — reuse the existing session_debriefer if present
    ai_text = _run_existing_ai_debriefer(today)
    if ai_text:
        report.set_ai_appendix(ai_text)

    # 5. Write artifacts (MD + HTML + PDF)
    paths = write_artifacts(report)

    # 6. Drain digest queue and send ONE consolidated Telegram
    extra = [
        f"<i>Today's debrief artifacts:</i>",
        f"  • <code>{paths.get('markdown')}</code>",
    ]
    if "pdf" in paths:
        extra.append(f"  • <code>{paths['pdf']}</code>")
    send_consolidated_digest(extra_lines=extra)

    return report


def _wait_for_grade(grade_path: Path, timeout_s: int) -> dict | None:
    """PhoenixGrading runs at 16:00; we run at 16:05. Most of the time
    the grade is already there. If not, poll briefly."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if grade_path.exists():
            try:
                return json.loads(grade_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(2)
    return None


def _load_today_trades(today: str) -> list[dict]:
    if not TRADE_MEMORY_PATH.exists():
        return []
    try:
        data = json.loads(TRADE_MEMORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    trades = data.get("trades") if isinstance(data, dict) else data
    if not isinstance(trades, list):
        return []
    out = []
    for t in trades:
        # Support multiple timestamp shapes
        for k in ("exit_time", "entry_time", "ts"):
            v = t.get(k)
            if isinstance(v, str) and v.startswith(today):
                out.append(t)
                break
    return out


def _run_existing_ai_debriefer(session_date: str) -> str | None:
    """Best-effort wrapper around agents/session_debriefer.py. Returns
    the markdown debrief text or None on any failure."""
    debrief_path = LOG_DIR / "ai_debrief" / f"{session_date}.md"
    if debrief_path.exists():
        try:
            return debrief_path.read_text(encoding="utf-8")[:8000]
        except OSError:
            return None
    # Try invoking the debriefer programmatically. Many implementations
    # exist; we match the most common signature gracefully.
    try:
        from agents import session_debriefer
        if hasattr(session_debriefer, "run_for_date"):
            return session_debriefer.run_for_date(session_date)
        if hasattr(session_debriefer, "main"):
            session_debriefer.main()
            if debrief_path.exists():
                return debrief_path.read_text(encoding="utf-8")[:8000]
    except Exception as e:
        logger.warning(f"[debrief] AI debriefer invocation failed: {e!r}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--session-date", default=None, help="Override the date (YYYY-MM-DD)")
    parser.add_argument("--wait", type=int, default=120, help="Seconds to wait for grade file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    report = run(session_date=args.session_date, wait_for_grade_s=args.wait)
    print(f"\n=== Phoenix post-session debrief — verdict: {report.verdict} ===")
    for c in report.verdict_checks:
        glyph = {"GREEN": "[ok]", "YELLOW": "[--]", "RED": "[XX]"}[c.status]
        print(f"  {glyph} {c.name}: {c.detail}")
    return 0 if report.verdict == "GREEN" else (1 if report.verdict == "YELLOW" else 2)


if __name__ == "__main__":
    sys.exit(main())
