"""
Opening Session sub-strategy P&L breakdown
============================================

Re-runs the opening_session strategy on the 5-year Databento data and
captures sub_name from each emitted Signal's metadata. Lets us see which
of the 6 sub-evaluators are producing the $31,894 total:
  - open_drive          (08:30-09:00 CT, OPEN_DRIVE type)
  - open_test_drive     (08:45-12:00 CT, OPEN_TEST_DRIVE type)
  - open_auction_in     (09:30-12:30 CT, OPEN_AUCTION_IN type)
  - open_auction_out    (08:45-11:00 CT, OPEN_AUCTION_OUT type)
  - premarket_breakout  (08:30-08:45 CT, any type)
  - orb                 (08:45-14:30 CT, any type)
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import (
    CSVEnrichmentPipeline,
    simulate_trade,
    instantiate_strategies,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("opening_session_breakdown")
logger.setLevel(logging.INFO)


def main():
    data_dir = ROOT / "data" / "historical"
    logger.info("[main] Loading pipeline (5 years)")
    pipeline = CSVEnrichmentPipeline(
        mnq_1m_csv=str(data_dir / "mnq_1min_databento.csv"),
        mnq_5m_csv=str(data_dir / "mnq_5min_databento.csv"),
        mes_1m_csv=str(data_dir / "mes_1min_databento.csv"),
        mes_5m_csv=str(data_dir / "mes_5min_databento.csv"),
        start="2021-05-17", end="2026-05-17",
    )

    strategies = instantiate_strategies(["opening_session"])
    if "opening_session" not in strategies:
        logger.error("Failed to instantiate opening_session")
        return
    strat = strategies["opening_session"]

    mnq_1m_df = pipeline.mnq_1m_df.copy()
    trades = []
    active = None
    cycle_count = 0
    signals_by_sub: dict[str, int] = {}
    t0 = time.time()

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1
        if cycle_count < 300:  # warmup
            continue
        # If we have an active trade still in progress, skip eval
        if active is not None:
            if active.get("exit_ts") is not None and eval_ts >= active["exit_ts"]:
                active = None
            else:
                continue
        try:
            sig = strat.evaluate(market, bars_5m, bars_1m, session_info)
        except Exception as e:
            logger.debug(f"eval err {eval_ts}: {e!r}")
            continue
        if sig is None:
            continue
        sub_name = (sig.metadata or {}).get("sub_name", "unknown")
        signals_by_sub[sub_name] = signals_by_sub.get(sub_name, 0) + 1
        entry_price = sig.entry_price if sig.entry_price else market["price"]
        if sig.stop_price is not None and sig.target_price is not None:
            stop_price = sig.stop_price
            target_price = sig.target_price
        else:
            stop_dist = sig.stop_ticks * 0.25
            if sig.direction == "LONG":
                stop_price = entry_price - stop_dist
                target_price = entry_price + stop_dist * sig.target_rr
            else:
                stop_price = entry_price + stop_dist
                target_price = entry_price - stop_dist * sig.target_rr
        tr = simulate_trade(
            signal_strategy="opening_session",
            signal_direction=sig.direction,
            entry_ts=eval_ts,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            mnq_1m_df=mnq_1m_df,
        )
        active = {
            "exit_ts": tr.exit_ts,
            "sub_name": sub_name,
        }
        trades.append({
            "sub_name": sub_name,
            "direction": sig.direction,
            "entry_ts": eval_ts,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "exit_ts": tr.exit_ts,
            "exit_price": tr.exit_price,
            "exit_reason": tr.exit_reason,
            "pnl_dollars": tr.pnl_dollars,
            "pnl_ticks": tr.pnl_ticks,
            "hold_min": tr.hold_min,
            "year": eval_ts.year,
            "hour_ct": eval_ts.tz_convert("America/Chicago").hour,
            "weekday": eval_ts.tz_convert("America/Chicago").strftime("%a"),
            "opening_type": (sig.metadata or {}).get("opening_type", "?"),
            "reason": sig.reason,
        })

    elapsed = time.time() - t0
    logger.info(f"[main] {cycle_count:,} cycles in {elapsed:.0f}s. "
                f"Total signals: {len(trades)}, by sub: {signals_by_sub}")

    df = pd.DataFrame(trades)
    out_path = ROOT / "backtest_results" / "opening_session_sub_breakdown.csv"
    df.to_csv(out_path, index=False)
    logger.info(f"[main] wrote {len(df)} trades to {out_path}")

    print()
    print("=" * 100)
    print("OPENING SESSION SUB-STRATEGY BREAKDOWN (5 years)")
    print("=" * 100)
    print()
    print("Total trades:", len(df))
    print("Total P&L:    $", round(df.pnl_dollars.sum(), 0))
    print()
    print("=== Per-sub-strategy ===")
    agg = df.groupby("sub_name").agg(
        n=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda s: (s > 0).sum()),
        total=("pnl_dollars", "sum"),
        avg=("pnl_dollars", "mean"),
        max_dd=("pnl_dollars", lambda s: (s.cumsum().cummax() - s.cumsum()).max()),
        avg_hold=("hold_min", "mean"),
    ).round(2)
    agg["wr_pct"] = (agg.wins / agg.n * 100).round(1)
    gross_win = df[df.pnl_dollars > 0].groupby("sub_name").pnl_dollars.sum()
    gross_loss = -df[df.pnl_dollars < 0].groupby("sub_name").pnl_dollars.sum()
    agg["pf"] = (gross_win / gross_loss).round(2)
    agg = agg.sort_values("total", ascending=False)
    print(agg[["n", "wr_pct", "total", "avg", "pf", "max_dd", "avg_hold"]].to_string())

    print()
    print("=== Per-sub × per-year ===")
    pivot = df.pivot_table(
        index="sub_name", columns="year", values="pnl_dollars",
        aggfunc="sum", fill_value=0,
    ).round(0)
    print(pivot.to_string())

    print()
    print("=== Per-sub × per-hour-CT ===")
    hr_pivot = df.pivot_table(
        index="sub_name", columns="hour_ct", values="pnl_dollars",
        aggfunc="sum", fill_value=0,
    ).round(0)
    print(hr_pivot.to_string())


if __name__ == "__main__":
    main()
