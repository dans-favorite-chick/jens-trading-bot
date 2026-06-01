"""
run_portfolio_backtest.py — top-level orchestrator for the 3-phase framework.

Phase 1 (macro, 5y OHLCV)  -> drive real strategy classes via phoenix_real_backtest,
                              attach MAE/MFE + regime + time-of-day, summarize.
Phase 1.1 (WFA)            -> load or run walk-forward results (wfa.py).
Phase 2 (microstructure)   -> overlay tick-level filters on the tick sub-period.
Phase 3 (report)           -> readouts + multi-tier validation comparison.

USAGE
-----
  # Full 5y, all strategies, friction on, load WFA if present, micro overlay on:
  python tools/portfolio_backtest/run_portfolio_backtest.py --strategies all \
      --start 2021-05-17 --end 2026-05-15

  # Iterate on the report without re-running the heavy backtest:
  python tools/portfolio_backtest/run_portfolio_backtest.py --macro load

Friction (commission + exchange + 2-tick slippage) is ON by default (Phase 3
"frictional reality"); pass --no-friction for a gross/canonical reproduction.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.portfolio_backtest import paths, analytics, report  # noqa: E402

MACRO_TRADES_CSV = paths.OUT_DIR / "macro_trades.csv"


def run_macro(strategies, start, end, friction, warmup) -> "pd.DataFrame":
    import pandas as pd
    import tools.phoenix_real_backtest as prb

    prb.APPLY_EXECUTION_DECAY = bool(friction)
    if friction:
        print(f"[macro] friction ON: -${prb._round_turn_friction_dollars():.2f}/round-turn/contract")

    t0 = time.time()
    pipeline = prb.CSVEnrichmentPipeline(
        mnq_1m_csv=str(paths.MNQ_1M_CSV), mnq_5m_csv=str(paths.MNQ_5M_CSV),
        mes_1m_csv=str(paths.MES_1M_CSV), mes_5m_csv=str(paths.MES_5M_CSV),
        start=start, end=end,
    )
    names = prb.TESTABLE_STRATEGIES if strategies == "all" else \
        [s.strip() for s in strategies.split(",") if s.strip()]
    strat_objs = prb.instantiate_strategies(names)
    print(f"[macro] {len(strat_objs)} strategies; running backtest...")
    trades = prb.run_backtest(pipeline, strat_objs, warmup_min=warmup)
    df = prb.analyze_results(trades)
    print(f"[macro] {len(df)} trades in {time.time()-t0:.0f}s")

    if not df.empty:
        df = analytics.compute_mae_mfe(df, pipeline.mnq_1m_df)
        regimes = analytics.classify_daily_regimes(pipeline.mnq_1m_df)
        df = analytics.attach_regime(df, regimes)
        df = analytics.attach_time_of_day(df)
    paths.OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(MACRO_TRADES_CSV, index=False)
    print(f"[macro] cached -> {MACRO_TRADES_CSV}")
    # Warehouse sidecar (per duckdb_warehouse_layout memory; contract schema_version=1)
    from tools.portfolio_backtest.sidecar import emit_sidecar
    emit_sidecar(
        MACRO_TRADES_CSV,
        strategy=None,
        params={"strategies": names, "start": start, "end": end,
                "warmup": warmup, "friction_on": bool(friction)},
        lookback_start=start, lookback_end=end,
        friction_per_rt_usd=(prb._round_turn_friction_dollars() if friction else 0.0),
        logical_group="portfolio_macro",
    )
    return df


def load_macro() -> "pd.DataFrame":
    import pandas as pd
    if not MACRO_TRADES_CSV.exists():
        raise FileNotFoundError(f"no cached macro trades at {MACRO_TRADES_CSV}; "
                                f"run with --macro run first")
    df = pd.read_csv(MACRO_TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
    print(f"[macro] loaded {len(df)} cached trades from {MACRO_TRADES_CSV}")
    return df


def get_wfa(mode, strategies, start, end, grid, friction):
    """Return (summary_df, windows_df) or (None, None)."""
    import pandas as pd
    win_csv = paths.OUT_DIR / "wfa_windows.csv"
    sum_csv = paths.OUT_DIR / "wfa_summary.csv"
    if mode == "skip":
        return None, None
    if mode == "load":
        w = pd.read_csv(win_csv) if win_csv.exists() else None
        s = pd.read_csv(sum_csv) if sum_csv.exists() else None
        if s is None:
            print("[wfa] no cached WFA results to load (skipping section)")
        return s, w
    # mode == 'run'
    try:
        from tools.portfolio_backtest import wfa
    except Exception as e:
        print(f"[wfa] module not available ({e!r}); skipping")
        return None, None
    if strategies == "all":
        from tools.phoenix_real_backtest import TESTABLE_STRATEGIES
        names = list(TESTABLE_STRATEGIES)
    else:
        names = [s.strip() for s in strategies.split(",") if s.strip()]
    windows = wfa.run_wfa(
        strategies=names,
        start=start, end=end, grid=grid, apply_friction=friction,
    )
    summary = wfa.summarize_wfa(windows)
    return summary, windows


def run_micro(macro, tick_start, tick_end):
    """Apply microstructure filters to tick-covered trades. Returns (micro_df, lift_df)."""
    try:
        from tools.portfolio_backtest import microstructure as ms
    except Exception as e:
        print(f"[micro] module not available ({e!r}); skipping Phase 2")
        return None, None
    import pandas as pd
    import tools.phoenix_real_backtest as prb

    e = pd.to_datetime(macro["entry_ts"], utc=True)
    ts = pd.Timestamp(tick_start, tz="UTC")
    te = pd.Timestamp(tick_end, tz="UTC")
    sub = macro[(e >= ts) & (e <= te)].copy()
    print(f"[micro] {len(sub)} trades within tick coverage {tick_start}..{tick_end}")
    if sub.empty:
        return None, None

    # 1m MNQ + MES bars for cluster + intermarket filters.
    mnq_1m = prb._load_bars_from_csv(str(paths.MNQ_1M_CSV))
    mes_1m = prb._load_bars_from_csv(str(paths.MES_1M_CSV))
    try:
        sub = ms.apply_absorption_filter(sub)
        sub = ms.apply_delta_cluster_trail(sub, mnq_1m)
        sub = ms.apply_intermarket_filter(sub, mnq_1m, mes_1m)
        lift = ms.microstructure_lift_table(sub)
    except Exception as exc:
        print(f"[micro] filter error: {exc!r}; returning partial")
        return sub, None
    return sub, lift


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="all")
    ap.add_argument("--start", default="2021-05-17")
    ap.add_argument("--end", default="2026-05-15")
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--no-friction", action="store_true",
                    help="disable round-turn friction (gross/canonical run)")
    ap.add_argument("--macro", choices=["run", "load"], default="run")
    ap.add_argument("--wfa", choices=["skip", "load", "run"], default="load")
    ap.add_argument("--wfa-grid", choices=["lean", "full"], default="full")
    ap.add_argument("--skip-micro", action="store_true")
    ap.add_argument("--tick-start", default="2026-03-17")
    ap.add_argument("--tick-end", default="2026-05-15")
    args = ap.parse_args()

    print(paths.summary())
    paths.verify(require_ticks=not args.skip_micro)
    friction = not args.no_friction

    macro = (load_macro() if args.macro == "load"
             else run_macro(args.strategies, args.start, args.end, friction, args.warmup))
    if macro.empty:
        print("[run] no macro trades; aborting report.")
        return 1

    wfa_summary, wfa_windows = get_wfa(args.wfa, args.strategies, args.start,
                                       args.end, args.wfa_grid, friction)

    micro, micro_lift = (None, None)
    if not args.skip_micro:
        micro, micro_lift = run_micro(macro, args.tick_start, args.tick_end)

    report.build_report(
        macro=macro, wfa_summary=wfa_summary, wfa_windows=wfa_windows,
        micro=micro, micro_lift=micro_lift,
        tick_start=args.tick_start, tick_end=args.tick_end,
        out_dir=paths.OUT_DIR,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
