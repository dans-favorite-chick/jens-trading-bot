"""
report.py — Phase 3 reporting: readouts + multi-tier validation comparison.

Consumes the macro trade set (with MAE/MFE + regime + time-of-day attached),
the WFA windows/summary tables (Phase 1.1), and the microstructure-flagged
trade set (Phase 2), and emits:

  * Phase 1 per-strategy headline metrics (Net, PF, Sharpe, Max DD duration, ...)
  * Phase 1.2 MAE/MFE-derived stop/target per strategy
  * Phase 1.3 per-regime breakdown
  * Phase 1.4 time-of-day breakdown
  * Phase 1.1 walk-forward robustness (OOS PF degradation flags)
  * Phase 2 microstructure with/without lift table
  * MULTI-TIER comparison: 5y macro baseline  vs  matched tick sub-period
    baseline  vs  sub-period with each microstructure filter applied.

All printed output is ASCII-only (cp1252 console). CSVs are written to
``paths.OUT_DIR``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from tools.portfolio_backtest import analytics, paths

_BAR = "=" * 100
_RULE = "-" * 100


def _hdr(title: str) -> None:
    print()
    print(_BAR)
    print(title)
    print(_BAR)


# ════════════════════════════════════════════════════════════════════
# Phase 1 readouts
# ════════════════════════════════════════════════════════════════════

def phase1_strategy_summary(macro: pd.DataFrame) -> pd.DataFrame:
    """Per-strategy headline table for the full macro period."""
    rows = []
    for strat, sub in macro.groupby("strategy"):
        s = analytics.summarize(sub)
        s["strategy"] = strat
        rows.append(s)
    cols = ["strategy", "n", "net_pnl", "win_rate", "profit_factor",
            "expectancy", "sharpe", "sortino", "max_dd",
            "max_dd_dur_trades", "max_tuw_days", "max_consec_losses"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)[cols].sort_values("net_pnl", ascending=False)


def phase1_stop_target(macro: pd.DataFrame) -> pd.DataFrame:
    """MAE/MFE-derived stop/target suggestion per strategy (Phase 1.2)."""
    rows = []
    for strat, sub in macro.groupby("strategy"):
        if not {"mae_ticks", "mfe_ticks"}.issubset(sub.columns):
            continue
        st = analytics.optimal_stop_target(sub)
        rows.append({
            "strategy": strat, "n": st.n_trades, "winners": st.n_winners,
            "stop_ticks": st.suggested_stop_ticks,
            "target_ticks": st.suggested_target_ticks,
            "winner_mae_p50": st.winner_mae_p50,
            "winner_mae_p90": st.winner_mae_p90,
            "mfe_p50": st.mfe_p50, "mfe_p75": st.mfe_p75,
        })
    return pd.DataFrame(rows)


def print_phase1(macro: pd.DataFrame, out_dir: Path) -> None:
    _hdr("PHASE 1 - 5-YEAR MACRO ROBUSTNESS")

    summ = phase1_strategy_summary(macro)
    print("\n[1.x] Per-strategy headline metrics (friction-net):")
    print(summ.to_string(index=False) if not summ.empty else "  (no trades)")
    summ.to_csv(out_dir / "phase1_strategy_summary.csv", index=False)

    st = phase1_stop_target(macro)
    print("\n[1.2] MAE/MFE-derived baseline stop/target (ticks):")
    print(st.to_string(index=False) if not st.empty else "  (MAE/MFE not attached)")
    if not st.empty:
        st.to_csv(out_dir / "phase1_stop_target.csv", index=False)

    if "regime" in macro.columns:
        print("\n[1.3] Performance by volatility regime:")
        for strat, sub in macro.groupby("strategy"):
            bt = analytics.bucket_table(sub, "regime")
            if not bt.empty:
                print(f"\n  -- {strat} --")
                print(bt.to_string(index=False))
        analytics.bucket_table(macro, "regime").to_csv(
            out_dir / "phase1_regime_portfolio.csv", index=False)

    if "tod_bucket" in macro.columns:
        print("\n[1.4] Performance by time-of-day (portfolio):")
        tod = analytics.bucket_table(macro, "tod_bucket")
        print(tod.to_string(index=False) if not tod.empty else "  (none)")
        tod.to_csv(out_dir / "phase1_time_of_day.csv", index=False)

    print("\n[1.5] Drawdown/consecutive-loss stats are in the headline table "
          "(max_dd, max_dd_dur_trades, max_tuw_days, max_consec_losses).")


# ════════════════════════════════════════════════════════════════════
# Phase 1.1 WFA readout
# ════════════════════════════════════════════════════════════════════

def print_wfa(wfa_summary: Optional[pd.DataFrame],
              wfa_windows: Optional[pd.DataFrame]) -> None:
    _hdr("PHASE 1.1 - WALK-FORWARD ANALYSIS (12m IS / 3m OOS)")
    if wfa_summary is None or wfa_summary.empty:
        print("  (no WFA results available - run wfa.py or pass --skip-wfa)")
        return
    print("\nPer-strategy robustness (degraded = OOS PF < 0.80 * IS PF):")
    print(wfa_summary.to_string(index=False))
    if wfa_windows is not None and not wfa_windows.empty:
        flagged = wfa_windows[wfa_windows["degraded"] == True]  # noqa: E712
        print(f"\nFlagged windows (>20% OOS PF degradation): "
              f"{len(flagged)} / {len(wfa_windows)}")


# ════════════════════════════════════════════════════════════════════
# Phase 2 + multi-tier comparison
# ════════════════════════════════════════════════════════════════════

def print_microstructure_lift(lift: Optional[pd.DataFrame]) -> None:
    _hdr("PHASE 2 - MICROSTRUCTURE FILTER LIFT (tick sub-period)")
    if lift is None or lift.empty:
        print("  (no microstructure lift table available)")
        return
    print(lift.to_string(index=False))
    print("\nNote: 2.3 DOM stop-hunt analysis is NOT COMPUTABLE - TBBO is "
          "top-of-book only; no Level-2 depth data exists. 2.1 intermarket "
          "is bar-level (no MES tick data).")


def build_comparison_table(macro: pd.DataFrame,
                           micro: Optional[pd.DataFrame],
                           tick_start: str, tick_end: str) -> pd.DataFrame:
    """MULTI-TIER table per strategy:
        5y baseline | tick-subperiod baseline | + absorption | + trail | + intermarket
    Each cell reports (PF, net$). Built from analytics.summarize on subsets.
    """
    ts = pd.Timestamp(tick_start, tz="UTC")
    te = pd.Timestamp(tick_end, tz="UTC")
    rows = []
    strategies = sorted(macro["strategy"].unique())
    for strat in strategies:
        full = macro[macro["strategy"] == strat]
        # Build the sub-period mask on `full`'s OWN index to avoid pandas'
        # reindex-on-mismatched-bool-mask warning (report.py:159 previously
        # mixed masks built on the un-filtered `macro` index with `full[...]`).
        full_e = pd.to_datetime(full["entry_ts"], utc=True)
        sub = full[(full_e >= ts) & (full_e <= te)]
        row = {
            "strategy": strat,
            "5y_n": len(full),
            "5y_pf": round(analytics.profit_factor(full["pnl_dollars"].to_numpy()), 2),
            "5y_net": round(float(full["pnl_dollars"].sum()), 0),
            "sub_n": len(sub),
            "sub_pf": round(analytics.profit_factor(sub["pnl_dollars"].to_numpy()), 2) if len(sub) else float("nan"),
            "sub_net": round(float(sub["pnl_dollars"].sum()), 0) if len(sub) else 0.0,
        }
        if micro is not None and not micro.empty:
            ms = micro[micro["strategy"] == strat]
            if "absorption_confirms" in ms.columns:
                kept = ms[ms["absorption_confirms"] == True]  # noqa: E712
                row["absorp_n"] = len(kept)
                row["absorp_pf"] = round(analytics.profit_factor(kept["pnl_dollars"].to_numpy()), 2) if len(kept) else float("nan")
            if "trail_adj_pnl_dollars" in ms.columns and len(ms):
                row["trail_net"] = round(float(ms["trail_adj_pnl_dollars"].sum()), 0)
            if "intermarket_confirms" in ms.columns:
                kept = ms[ms["intermarket_confirms"] == True]  # noqa: E712
                row["inter_n"] = len(kept)
                row["inter_pf"] = round(analytics.profit_factor(kept["pnl_dollars"].to_numpy()), 2) if len(kept) else float("nan")
        rows.append(row)
    return pd.DataFrame(rows)


def print_comparison(macro: pd.DataFrame, micro: Optional[pd.DataFrame],
                     tick_start: str, tick_end: str, out_dir: Path) -> None:
    _hdr("PHASE 3 - MULTI-TIER VALIDATION: 5Y BASELINE vs TICK SUB-PERIOD "
         "(with/without microstructure)")
    tbl = build_comparison_table(macro, micro, tick_start, tick_end)
    print(tbl.to_string(index=False) if not tbl.empty else "  (no data)")
    if not tbl.empty:
        tbl.to_csv(out_dir / "phase3_multitier_comparison.csv", index=False)
    print(f"\nTick sub-period: {tick_start} .. {tick_end}")
    print("Columns: 5y_* = full macro baseline; sub_* = same strategy over the "
          "tick window; absorp_/trail_/inter_ = sub-period WITH that filter.")


# ════════════════════════════════════════════════════════════════════
# Top-level
# ════════════════════════════════════════════════════════════════════

def build_report(macro: pd.DataFrame,
                 wfa_summary: Optional[pd.DataFrame] = None,
                 wfa_windows: Optional[pd.DataFrame] = None,
                 micro: Optional[pd.DataFrame] = None,
                 micro_lift: Optional[pd.DataFrame] = None,
                 tick_start: str = "2026-03-17",
                 tick_end: str = "2026-05-15",
                 out_dir: Optional[Path] = None) -> None:
    out_dir = Path(out_dir) if out_dir else paths.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print_phase1(macro, out_dir)
    print_wfa(wfa_summary, wfa_windows)
    print_microstructure_lift(micro_lift)
    print_comparison(macro, micro, tick_start, tick_end, out_dir)

    _hdr("DONE")
    print(f"All CSVs written under: {out_dir}")
