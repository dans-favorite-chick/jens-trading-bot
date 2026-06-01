"""
_run_phase13_4.py - Wire 4 Phase 13 lab strategies through this framework
without touching phoenix_real_backtest.py (the harness's class_map doesn't
include them, but the production class files DO exist in strategies/).

Imports the 4 production classes directly, instantiates them with their
existing configs from config/strategies.py, and runs them through the SAME
CSVEnrichmentPipeline + run_backtest path the other 14 strategies use, so
the resulting trades are apples-to-apples comparable.

Output:
  * tools .../OUT_DIR/phase13_trades.csv  -- 4-strategy snapshot
  * tools .../OUT_DIR/macro_trades.csv    -- updated portfolio trade log
    (idempotent: drops any prior rows for these 4 strategies before concat)

Console: per-strategy friction-net headline metrics.

Usage:
  python tools/portfolio_backtest/_run_phase13_4.py \
      --start 2021-05-17 --end 2026-05-15
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd

from tools.portfolio_backtest import paths, analytics
import tools.phoenix_real_backtest as prb

# Phase 13 lab strategies - production classes exist in strategies/ but
# aren't in phoenix_real_backtest.instantiate_strategies' class_map.
from strategies.raschke_baseline import RaschkeBaseline
from strategies.g_inside_bar_breakout import InsideBarBreakout
from strategies.e_multi_day_breakout import MultiDayBreakout
from strategies.a_asian_continuation import AsianContinuation

from config.strategies import STRATEGIES


LAB_CLASSES = {
    "raschke_baseline": RaschkeBaseline,
    "g_inside_bar_breakout": InsideBarBreakout,
    "e_multi_day_breakout": MultiDayBreakout,
    "a_asian_continuation": AsianContinuation,
}


def instantiate_lab_strategies() -> dict:
    """Build the 4 strategy instances using their canonical configs."""
    out = {}
    for name, cls in LAB_CLASSES.items():
        if name not in STRATEGIES:
            print(f"[lab] WARN no config for {name}; skipping")
            continue
        cfg = dict(STRATEGIES[name])
        cfg["is_prod_bot"] = False
        try:
            out[name] = cls(cfg)
            print(f"[lab] instantiated {name}")
        except Exception as exc:
            print(f"[lab] FAILED to instantiate {name}: {exc!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-05-17")
    ap.add_argument("--end", default="2026-05-15")
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--no-friction", action="store_true",
                    help="disable round-turn execution friction (default ON)")
    a = ap.parse_args()

    prb.APPLY_EXECUTION_DECAY = not a.no_friction
    if prb.APPLY_EXECUTION_DECAY:
        print(f"[lab] friction ON: -${prb._round_turn_friction_dollars():.2f}/RT/contract")

    print(paths.summary())
    paths.verify(require_ticks=False)

    t0 = time.time()
    pipeline = prb.CSVEnrichmentPipeline(
        mnq_1m_csv=str(paths.MNQ_1M_CSV), mnq_5m_csv=str(paths.MNQ_5M_CSV),
        mes_1m_csv=str(paths.MES_1M_CSV), mes_5m_csv=str(paths.MES_5M_CSV),
        start=a.start, end=a.end,
    )
    strategies = instantiate_lab_strategies()
    if not strategies:
        print("[lab] no strategies instantiated; aborting.")
        return 1
    print(f"[lab] running backtest for {list(strategies)}...")
    trades = prb.run_backtest(pipeline, strategies, warmup_min=a.warmup)
    df = prb.analyze_results(trades)
    print(f"[lab] {len(df)} trades in {time.time()-t0:.0f}s")

    if df.empty:
        print("[lab] no trades produced - nothing to merge.")
        return 1

    # Analytics overlay (same as the orchestrator does for the other 14)
    df = analytics.compute_mae_mfe(df, pipeline.mnq_1m_df)
    regimes = analytics.classify_daily_regimes(pipeline.mnq_1m_df)
    df = analytics.attach_regime(df, regimes)
    df = analytics.attach_time_of_day(df)

    paths.OUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = paths.OUT_DIR / "phase13_trades.csv"
    df.to_csv(snapshot, index=False)
    print(f"[lab] standalone snapshot -> {snapshot}")
    # Warehouse sidecar for the standalone phase13_trades.csv
    from tools.portfolio_backtest.sidecar import emit_sidecar
    _friction = prb._round_turn_friction_dollars() if prb.APPLY_EXECUTION_DECAY else 0.0
    emit_sidecar(
        snapshot,
        strategy=None,
        params={"strategies": list(strategies.keys()),
                "per_strategy": {n: dict(STRATEGIES[n]) for n in strategies},
                "start": a.start, "end": a.end, "warmup": a.warmup,
                "friction_on": prb.APPLY_EXECUTION_DECAY},
        lookback_start=a.start, lookback_end=a.end,
        friction_per_rt_usd=_friction,
        logical_group="phase13_trades",
    )

    macro_path = paths.OUT_DIR / "macro_trades.csv"
    if macro_path.exists():
        existing = pd.read_csv(macro_path, parse_dates=["entry_ts", "exit_ts"])
        before = len(existing)
        existing = existing[~existing["strategy"].isin(LAB_CLASSES.keys())]
        print(f"[lab] merging: existing {before} -> {len(existing)} "
              f"(dropped any prior phase13 rows) + {len(df)} new")
        combined = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df
    combined.to_csv(macro_path, index=False)
    print(f"[lab] updated -> {macro_path} ({len(combined)} total trades)")
    # Warehouse sidecar for the merged macro_trades.csv (this run merged phase13 in)
    emit_sidecar(
        macro_path,
        strategy=None,
        params={"merge_source": "_run_phase13_4",
                "phase13_strategies_added": list(LAB_CLASSES.keys()),
                "start": a.start, "end": a.end,
                "friction_on": prb.APPLY_EXECUTION_DECAY},
        lookback_start=a.start, lookback_end=a.end,
        friction_per_rt_usd=_friction,
        logical_group="portfolio_macro",
        notes="phase13 lab strategies merged into existing macro_trades.csv",
    )

    # Per-strategy headline (friction net)
    print()
    print("=" * 100)
    print("PHASE 13 LAB STRATEGIES - FRICTION-NET 5y RESULTS THROUGH THIS FRAMEWORK")
    print("=" * 100)
    for strat, sub in df.groupby("strategy"):
        s = analytics.summarize(sub)
        print(f"  {strat:26} n={s['n']:5d}  net=${s['net_pnl']:>10.0f}  "
              f"PF={s['profit_factor']:>5.2f}  WR={s['win_rate']:.1%}  "
              f"Sharpe={s['sharpe']:>6.2f}  maxDD=${s['max_dd']:.0f}  "
              f"TUW={s['max_tuw_days']}d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
