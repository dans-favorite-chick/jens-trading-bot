"""
Diagnose why a strategy "silently stops" firing during 5y backtest.

Monkeypatches the strategy's evaluate() to capture the `_last_reject`
reason every time it returns None. Aggregates reject reasons per day
to reveal what gate is permanently blocking.

USAGE:
  python tools/diag_silent_stop.py --strategy bias_momentum --start 2021-05-17 --end 2021-06-15
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import (
    CSVEnrichmentPipeline,
    instantiate_strategies,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    args = ap.parse_args()

    data_dir = ROOT / "data" / "historical"
    pipeline = CSVEnrichmentPipeline(
        mnq_1m_csv=str(data_dir / "mnq_1min_databento.csv"),
        mnq_5m_csv=str(data_dir / "mnq_5min_databento.csv"),
        mes_1m_csv=str(data_dir / "mes_1min_databento.csv"),
        mes_5m_csv=str(data_dir / "mes_5min_databento.csv"),
        start=args.start,
        end=args.end,
    )
    strats = instantiate_strategies([args.strategy])
    strat = strats[args.strategy]
    name = args.strategy

    # Per-day reject reason counter
    rejects_per_day: dict[str, Counter] = defaultdict(Counter)
    signals_per_day: dict[str, int] = defaultdict(int)
    skips_per_day: dict[str, int] = defaultdict(int)  # active position still open
    exceptions_per_day: dict[str, int] = defaultdict(int)

    active = None
    cycle_count = 0
    print(f"Running diagnostic on {name} from {args.start} to {args.end}...")
    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1
        if cycle_count < 300:
            continue
        date_str = eval_ts.tz_convert("America/Chicago").strftime("%Y-%m-%d")

        if active is not None:
            if active.get("exit_ts") is not None and eval_ts >= active["exit_ts"]:
                active = None
            else:
                skips_per_day[date_str] += 1
                continue

        try:
            sig = strat.evaluate(market, bars_5m, bars_1m, session_info)
        except Exception as e:
            exceptions_per_day[date_str] += 1
            rejects_per_day[date_str][f"EXCEPTION:{type(e).__name__}"] += 1
            continue

        if sig is None:
            reason = getattr(strat, "_last_reject", None) or "no_reason_captured"
            # Truncate long reasons to first 60 chars for readability
            reason_short = reason[:60].split(":")[0] if ":" in reason else reason[:60]
            rejects_per_day[date_str][reason_short] += 1
        else:
            signals_per_day[date_str] += 1
            # Mark active so we don't re-fire; fake exit_ts 4hrs out
            active = {"exit_ts": eval_ts + pd.Timedelta(hours=4)}

    # Report
    all_dates = sorted(set(rejects_per_day.keys()) | set(signals_per_day.keys())
                        | set(skips_per_day.keys()))
    print()
    print("=" * 100)
    print(f"DAILY ACTIVITY for {name}")
    print("=" * 100)
    print(f"{'date':12s}  {'signals':>7s}  {'skipped':>7s}  {'rejected':>8s}  {'top reject reason':40s}")
    print("-" * 100)
    for d in all_dates:
        sigs = signals_per_day.get(d, 0)
        skips = skips_per_day.get(d, 0)
        rejs = sum(rejects_per_day.get(d, {}).values())
        top = rejects_per_day.get(d, Counter()).most_common(1)
        top_str = f"{top[0][0]} ({top[0][1]})" if top else "-"
        print(f"{d:12s}  {sigs:>7d}  {skips:>7d}  {rejs:>8d}  {top_str:40s}")

    print()
    print("=" * 100)
    print("OVERALL REJECT REASONS (across all days)")
    print("=" * 100)
    overall = Counter()
    for d, c in rejects_per_day.items():
        overall.update(c)
    for reason, n in overall.most_common(15):
        print(f"  {n:>6d}  {reason}")

    print()
    print(f"Total cycles: {cycle_count:,}")
    print(f"Total signals: {sum(signals_per_day.values())}")
    print(f"Total skips (active): {sum(skips_per_day.values())}")
    print(f"Total rejections: {sum(overall.values())}")
    print(f"Total exceptions: {sum(exceptions_per_day.values())}")


if __name__ == "__main__":
    main()
