"""
Backtest Data Quality Validator
=================================

Checks ALL backtest result CSVs for the silent-stop bug pattern + other
quality issues. Run after EVERY backtest re-run, and before trusting any
P&L conclusion.

What it checks per-strategy:
  1. SILENT STOP: did the strategy fire trades through the END of the data
     range, or did it stop prematurely?
  2. NAT EXIT: any trades with exit_ts=None/NaT? (smoking gun for the bug)
  3. ZERO-DURATION: any trades with hold_min=0? (likely simulation failure)
  4. SAMPLE SIZE: warn if n < 30 (below statistical-validity floor)

Exit codes:
  0 = all good
  1 = warnings only (LOW n, slight gap)
  2 = errors (active silent-stop, NaT exits)

USAGE:
  python tools/validate_backtest_quality.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

# Labs to check + expected data end date
DATA_END = pd.Timestamp("2026-05-15", tz="UTC")

LAB_FILES = [
    ("backtest_results/phoenix_real_5year.csv",            "5y existing strategies"),
    ("backtest_results/phoenix_new_strategy_lab.csv",      "New strategies (a-g)"),
    ("backtest_results/phoenix_trend_pullback_lab.csv",    "Raschke trend-pullback"),
    ("backtest_results/phoenix_mean_reversion_lab.csv",    "Mean-reversion lab"),
    ("backtest_results/phoenix_1m_timeframe_lab.csv",      "1m timeframe lab"),
    ("backtest_results/opening_session_sub_breakdown.csv", "Opening session subs"),
    ("backtest_results/phoenix_sr_strategy_lab.csv",       "S/R zone strategy lab"),
    ("backtest_results/phoenix_failed_hold_lab.csv",       "Failed-hold continuation lab"),
]

# Thresholds
STUCK_GAP_DAYS = 60      # gap > 60d from data end = likely stuck
SLIGHT_GAP_DAYS = 14     # 14-60d = warning
MIN_SAMPLE_N = 30        # Wilson 95% CI floor


def check_lab(path: Path, label: str) -> tuple[int, list[str]]:
    """Returns (exit_code, messages)."""
    if not path.exists():
        return (0, [f"  [SKIP] {label}: file missing"])

    df = pd.read_csv(path)
    if "entry_ts" not in df.columns:
        return (0, [f"  [SKIP] {label}: no entry_ts column"])

    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True, errors="coerce")
    if "exit_ts" in df.columns:
        df["exit_ts"] = pd.to_datetime(df["exit_ts"], utc=True, errors="coerce")

    # Group column
    gcol = "strategy" if "strategy" in df.columns else (
        "sub_name" if "sub_name" in df.columns else df.columns[0]
    )

    messages = []
    worst_code = 0

    for s in sorted(df[gcol].dropna().unique()):
        sdf = df[df[gcol] == s]
        n = len(sdf)
        last = sdf["entry_ts"].max()
        gap_days = (DATA_END - last).days if pd.notna(last) else 9999
        nat_exits = int(sdf["exit_ts"].isna().sum()) if "exit_ts" in df.columns else 0
        zero_holds = (
            int((sdf["hold_min"] == 0).sum())
            if "hold_min" in df.columns
            else 0
        )

        # Severity
        flags = []
        code = 0
        if nat_exits > 0:
            flags.append(f"NaT_exits={nat_exits}")
            code = max(code, 2)
        if gap_days > STUCK_GAP_DAYS:
            flags.append(f"STUCK ({gap_days}d gap)")
            code = max(code, 2)
        elif gap_days > SLIGHT_GAP_DAYS:
            flags.append(f"slight gap ({gap_days}d)")
            code = max(code, 1)
        if zero_holds > n * 0.05 and n > 10:
            flags.append(f"zero_holds={zero_holds}")
            code = max(code, 1)
        if n < MIN_SAMPLE_N:
            flags.append(f"LOW_n={n}")
            code = max(code, 1)

        worst_code = max(worst_code, code)
        symbol = "FAIL" if code == 2 else ("WARN" if code == 1 else " OK ")
        flag_str = ", ".join(flags) if flags else "clean"
        messages.append(f"  [{symbol}] {s:30s}  n={n:>5}  last={last.date() if pd.notna(last) else 'NaT'}  {flag_str}")

    return (worst_code, messages)


def main() -> int:
    print("=" * 100)
    print("BACKTEST DATA QUALITY VALIDATOR")
    print("=" * 100)
    print(f"Data end reference: {DATA_END.date()}")
    print(f"Stuck threshold:    >{STUCK_GAP_DAYS}d gap from end")
    print(f"Warning threshold:  >{SLIGHT_GAP_DAYS}d gap from end")
    print(f"Min sample size:    {MIN_SAMPLE_N} (Wilson 95% CI floor)")
    print()

    overall = 0
    for relpath, label in LAB_FILES:
        path = ROOT / relpath
        print(f"=== {label}: {relpath} ===")
        code, msgs = check_lab(path, label)
        for m in msgs:
            print(m)
        overall = max(overall, code)
        print()

    print("=" * 100)
    if overall == 0:
        print("RESULT: ALL CLEAN")
    elif overall == 1:
        print("RESULT: WARNINGS (small samples or slight gaps — review)")
    else:
        print("RESULT: ERRORS (silent-stop bug present — re-run affected labs)")
    print("=" * 100)

    return overall


if __name__ == "__main__":
    sys.exit(main())
