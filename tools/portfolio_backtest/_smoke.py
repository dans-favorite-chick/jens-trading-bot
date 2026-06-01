"""
_smoke.py — end-to-end integration check for the portfolio_backtest framework.

Proves that FROM THE WORKTREE we can:
  1. resolve data via paths.DATA_ROOT (main checkout),
  2. import + drive Phoenix's REAL strategy classes through the existing
     phoenix_real_backtest harness,
  3. feed the resulting trades through the new analytics layer.

Run a short window so it finishes in seconds:
    python tools/portfolio_backtest/_smoke.py --strategies bias_momentum \
        --start 2026-04-01 --end 2026-05-15
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # worktree root
sys.path.insert(0, str(ROOT))

from tools.portfolio_backtest import paths, analytics  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="bias_momentum")
    ap.add_argument("--start", default="2026-04-01")
    ap.add_argument("--end", default="2026-05-15")
    ap.add_argument("--warmup", type=int, default=300)
    args = ap.parse_args()

    print(paths.summary())
    paths.verify(require_ticks=False)

    from tools.phoenix_real_backtest import (
        CSVEnrichmentPipeline, instantiate_strategies, run_backtest,
        analyze_results,
    )

    t0 = time.time()
    pipeline = CSVEnrichmentPipeline(
        mnq_1m_csv=str(paths.MNQ_1M_CSV),
        mnq_5m_csv=str(paths.MNQ_5M_CSV),
        mes_1m_csv=str(paths.MES_1M_CSV),
        mes_5m_csv=str(paths.MES_5M_CSV),
        start=args.start, end=args.end,
    )
    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    strategies = instantiate_strategies(names)
    print(f"[smoke] strategies ready: {list(strategies)}")
    trades = run_backtest(pipeline, strategies, warmup_min=args.warmup)
    df = analyze_results(trades)
    print(f"[smoke] {len(df)} trades in {time.time()-t0:.0f}s")

    if df.empty:
        print("[smoke] no trades produced in window (widen --start/--end). "
              "Stack still validated: pipeline + strategies ran cleanly.")
        return 0

    df = analytics.compute_mae_mfe(df, pipeline.mnq_1m_df)
    regimes = analytics.classify_daily_regimes(pipeline.mnq_1m_df)
    df = analytics.attach_regime(df, regimes)
    df = analytics.attach_time_of_day(df)

    print("\n[smoke] per-strategy summary:")
    for strat, sub in df.groupby("strategy"):
        s = analytics.summarize(sub)
        print(f"  {strat:18} n={s['n']:4d} net=${s['net_pnl']:>9.0f} "
              f"PF={s['profit_factor']:>5.2f} WR={s['win_rate']:.1%} "
              f"Sharpe={s['sharpe']:>6.2f} maxDD=${s['max_dd']:.0f} "
              f"TUW={s['max_tuw_days']}d")
        st = analytics.optimal_stop_target(sub)
        print(f"    MAE/MFE stop={st.suggested_stop_ticks}t target="
              f"{st.suggested_target_ticks}t (winners' MAE p90={st.winner_mae_p90}t, "
              f"MFE p75={st.mfe_p75}t)")
    print("\n[smoke] by time-of-day:")
    print(analytics.bucket_table(df, "tod_bucket").to_string(index=False))
    print("\n[smoke] by regime:")
    print(analytics.bucket_table(df, "regime").to_string(index=False))
    print("\n[smoke] OK - full stack validated end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
