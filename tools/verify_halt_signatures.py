"""
Phoenix — Halt signature live verification

Triggers each halt path with synthetic state and verifies the
[HALT:<strategy>] / [CAP:<scope>:<account>] log line actually fires
through the real logging stack (not just the test caplog).

Output: out/halt_verify_<YYYY-MM-DD>.md (PASS/FAIL per signature)

Sprint A's Fix E added these log signatures and unit tests verify the
log lines fire when the logging code is called directly via caplog.
This tool exercises the production call chain end-to-end and writes
a PASS/FAIL report. If any signature fails, watcher_agent's grep
cannot detect the corresponding events.

Side-effects:
  - Strategy halt is triggered against a synthetic key
    "_verify_halt_synthetic" then immediately re-enabled. The real
    strategy_halts.json on disk is left in its prior state.
  - RiskManager state lives only inside this process (no persistence).
"""
from __future__ import annotations

import io
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CT = ZoneInfo("America/Chicago")


def _data_root() -> Path:
    """Use cwd if it has an out/ dir (tests) OR a logs/ dir (production
    project root). Fall back to package ROOT otherwise."""
    cwd = Path.cwd()
    if (cwd / "out").exists() or (cwd / "logs").exists():
        return cwd
    if (ROOT / "logs").exists():
        return ROOT
    return cwd


def capture_log_during(trigger_fn, expected_substr: str) -> tuple[bool, str]:
    """Run trigger_fn() with a captured log handler attached to root.

    Returns (matched, full_log_text). Matched is True iff `expected_substr`
    appeared in any captured line.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        try:
            trigger_fn()
        except Exception as e:
            return (False, f"trigger raised: {type(e).__name__}: {e}")
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)
    log_text = buf.getvalue()
    return (expected_substr in log_text, log_text)


# ─── Trigger functions (one per signature) ──────────────────────────

def trigger_strategy_halt():
    """Synthetic per-strategy floor breach.

    Uses a key that is NOT in the registry's STRATEGY_KEYS list so the
    halt doesn't accidentally affect a real strategy. We add it to the
    registry's _halted set manually-cleared after.
    """
    from core.strategy_risk_registry import StrategyRiskRegistry
    reg = StrategyRiskRegistry()
    SYN_KEY = "_verify_halt_synthetic"
    # If a previous run left this key in halt state, clear it first so
    # halt() doesn't early-return.
    reg._halted.discard(SYN_KEY)
    reg._halt_reasons.pop(SYN_KEY, None)
    try:
        reg.halt(SYN_KEY, sub_strategy=None,
                 reason="verify_halt_signatures.py synthetic trigger")
    finally:
        # Clean up — never leave the synthetic key persisted.
        reg._halted.discard(SYN_KEY)
        reg._halt_reasons.pop(SYN_KEY, None)
        try:
            reg._save_halt_state()
        except Exception:
            pass


def trigger_daily_cap():
    """Synthetic daily cap breach via RiskManager.can_trade()."""
    from core.risk_manager import RiskManager
    rm = RiskManager()
    rm.state.daily_pnl = -(rm._daily_limit + 1.0)
    rm.can_trade(vix=0.0, account="_verify_synthetic")


def trigger_weekly_cap():
    """Synthetic weekly cap breach via RiskManager.can_trade()."""
    from core.risk_manager import RiskManager
    from config.settings import WEEKLY_LOSS_LIMIT
    rm = RiskManager()
    # Daily must be safe so can_trade reaches the weekly check
    rm.state.daily_pnl = 0.0
    rm.state.weekly_pnl = -(WEEKLY_LOSS_LIMIT + 1.0)
    rm.can_trade(vix=0.0, account="_verify_synthetic")


def trigger_bot_kill():
    """Synthetic bot-level kill switch via RiskManager.can_trade()."""
    from core.risk_manager import RiskManager
    rm = RiskManager()
    rm.state.killed = True
    rm.state.kill_reason = "verify_halt_signatures.py synthetic trigger"
    rm.can_trade(vix=0.0, account="_verify_synthetic")


def main() -> int:
    today = datetime.now(CT).date()
    out = _data_root() / f"out/halt_verify_{today}.md"
    out.parent.mkdir(exist_ok=True)
    L = []
    L.append(f"# Phoenix Halt Signature Verification - {today}")
    L.append("")
    L.append("Synthetic triggers for each halt/cap path. Confirms the log line")
    L.append("actually fires through the real logging stack — not just the test caplog.")
    L.append("")
    L.append("| Signature | Trigger | Result | Log line (truncated) |")
    L.append("|---|---|---|---|")

    checks = [
        ("[HALT:<strategy>]",      trigger_strategy_halt, "[HALT:_verify_halt_synthetic]"),
        ("[HALT:bot]",             trigger_bot_kill,      "[HALT:bot]"),
        ("[CAP:daily:<account>]",  trigger_daily_cap,     "[CAP:daily:_verify_synthetic]"),
        ("[CAP:weekly:<account>]", trigger_weekly_cap,    "[CAP:weekly:_verify_synthetic]"),
    ]

    overall_pass = True
    for sig, trigger, expected in checks:
        ok, log_text = capture_log_during(trigger, expected)
        status = "PASS" if ok else "FAIL"
        if not ok:
            overall_pass = False
        # Find the relevant captured line to show: prefer one matching
        # the full expected signature, fall back to last line, then any line.
        relevant = ""
        for line in log_text.splitlines():
            if expected in line:
                relevant = line
                break
        if not relevant and log_text.strip():
            relevant = log_text.strip().splitlines()[-1]
        relevant = relevant.replace("|", r"\|")[:120]
        L.append(f"| `{sig}` | `{trigger.__name__}()` | {status} | `{relevant}` |")

    L.append("")
    L.append(f"**Overall:** {'ALL PASS' if overall_pass else 'ONE OR MORE FAILED'}")
    L.append("")
    L.append("If any check failed, the corresponding halt path is not logging the")
    L.append("expected signature in production code. Watcher_agent's grep cannot")
    L.append("detect those events. Investigate before relying on halt alerts.")
    L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Overall: {'PASS' if overall_pass else 'FAIL'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
