"""
Phoenix ES/NQ Confluence Attribution Analysis
==============================================

Question: If we used ES↔NQ alignment as a CONFLUENCE FACTOR (VETO or
CONFIRMATION) on existing strategies — instead of as its own standalone
strategy — does it help or hurt?

Method:
  1. Load every trade from phoenix_real_5year.csv + phoenix_new_strategy_lab.csv
  2. For each trade entry, look up MES return over the prior N minutes
  3. Compute MNQ return same window
  4. Bucket trades by alignment:
       - "aligned"   : MES and MNQ moved same direction as trade
       - "divergent" : MES and MNQ moved opposite directions
       - "weak"      : both moves <10bp (no info)
       - "wrong"     : both moved opposite to trade direction
  5. Compute conditional P&L per strategy per bucket
  6. Report whether filtering on "aligned only" improves total P&L

This tests the role-based confluence framework hypothesis:
  ES/NQ alignment as VETO → kill divergent trades
  ES/NQ alignment as SIZING modifier → bigger size on aligned
  ES/NQ alignment as CONFIRMATION → score bump on aligned
"""
from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOOKBACK_MIN = 5      # window for return computation
WEAK_BP = 10          # threshold for "no info" — both moves under 10bp


def load_mes_1m() -> pd.DataFrame:
    """Load MES 1m bars indexed by ts."""
    csv = ROOT / "data" / "historical" / "mes_1min_databento.csv"
    df = pd.read_csv(csv)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts").sort_index()
    return df


def load_mnq_1m() -> pd.DataFrame:
    csv = ROOT / "data" / "historical" / "mnq_1min_databento.csv"
    df = pd.read_csv(csv)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts").sort_index()
    return df


def load_trades() -> pd.DataFrame:
    existing = pd.read_csv(ROOT / "backtest_results" / "phoenix_real_5year.csv")
    new = pd.read_csv(ROOT / "backtest_results" / "phoenix_new_strategy_lab.csv")
    for df in (existing, new):
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    keep_cols = ["strategy", "direction", "entry_ts", "pnl_dollars",
                  "pnl_ticks", "year"]
    e = existing[keep_cols].copy()
    n = new[keep_cols].copy()
    combined = pd.concat([e, n], ignore_index=True)
    # Filter to winners only
    WINNERS = {
        "opening_session", "vwap_pullback_v2", "spring_setup",
        "es_nq_confluence", "bias_momentum", "vwap_band_pullback", "ib_breakout",
        "g_inside_bar_breakout", "e_multi_day_breakout", "a_asian_continuation",
    }
    combined = combined[combined.strategy.isin(WINNERS)]
    return combined.sort_values("entry_ts").reset_index(drop=True)


def compute_alignment(trades: pd.DataFrame, mnq: pd.DataFrame,
                      mes: pd.DataFrame, lookback_min: int = LOOKBACK_MIN) -> pd.DataFrame:
    """For each trade, compute MES & MNQ N-min returns + alignment label."""
    rows = []
    for tr in trades.itertuples(index=False):
        entry_ts = tr.entry_ts
        prior_ts = entry_ts - pd.Timedelta(minutes=lookback_min)
        # Get closest MNQ bar at or before each timestamp
        try:
            mnq_now = mnq.loc[:entry_ts].iloc[-1]["close"]
            mnq_prior = mnq.loc[:prior_ts].iloc[-1]["close"]
            mes_now = mes.loc[:entry_ts].iloc[-1]["close"]
            mes_prior = mes.loc[:prior_ts].iloc[-1]["close"]
        except (KeyError, IndexError):
            rows.append({"alignment": "no_data", "mnq_ret_bp": None,
                          "mes_ret_bp": None})
            continue
        mnq_ret_bp = (mnq_now - mnq_prior) / mnq_prior * 10000
        mes_ret_bp = (mes_now - mes_prior) / mes_prior * 10000
        # Alignment bucketing
        if abs(mnq_ret_bp) < WEAK_BP and abs(mes_ret_bp) < WEAK_BP:
            align = "weak"
        elif (mnq_ret_bp > 0) != (mes_ret_bp > 0):
            align = "divergent"
        else:
            # Same direction
            trade_dir = 1 if tr.direction == "LONG" else -1
            mnq_dir = 1 if mnq_ret_bp > 0 else -1
            if mnq_dir == trade_dir:
                align = "aligned"
            else:
                align = "wrong"  # both indices moved opposite to trade
        rows.append({"alignment": align, "mnq_ret_bp": round(mnq_ret_bp, 1),
                      "mes_ret_bp": round(mes_ret_bp, 1)})
    return pd.DataFrame(rows)


def main():
    print("=" * 100)
    print("ES/NQ CONFLUENCE ATTRIBUTION ANALYSIS")
    print("=" * 100)
    print()
    print(f"Loading data (this may take ~30s)...")
    mes = load_mes_1m()
    mnq = load_mnq_1m()
    trades = load_trades()
    print(f"  trades: {len(trades):,} across {trades.strategy.nunique()} strategies")
    print(f"  MES bars: {len(mes):,}  MNQ bars: {len(mnq):,}")
    print()
    print(f"Computing {LOOKBACK_MIN}-min returns + alignment for each trade...")
    align = compute_alignment(trades, mnq, mes, LOOKBACK_MIN)
    df = pd.concat([trades.reset_index(drop=True), align], axis=1)

    print()
    print("=" * 100)
    print("OVERALL: Trade count + P&L per alignment bucket")
    print("=" * 100)
    print()
    overall = df.groupby("alignment").agg(
        n=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda s: (s > 0).sum()),
        total=("pnl_dollars", "sum"),
        avg=("pnl_dollars", "mean"),
    ).round(2)
    overall["wr_pct"] = (overall.wins / overall.n * 100).round(1)
    overall["pct_of_trades"] = (overall.n / overall.n.sum() * 100).round(1)
    print(overall[["n", "pct_of_trades", "wr_pct", "total", "avg"]].to_string())
    print()

    print("=" * 100)
    print("PER-STRATEGY: P&L per alignment bucket")
    print("=" * 100)
    print()
    # Per-strategy pivot
    per_strat = df.groupby(["strategy", "alignment"]).agg(
        n=("pnl_dollars", "count"),
        total=("pnl_dollars", "sum"),
    ).round(0)
    per_strat["wr_pct"] = (df[df.pnl_dollars > 0].groupby(
        ["strategy", "alignment"]).size().reindex(per_strat.index, fill_value=0)
                            / per_strat["n"] * 100).round(1)
    print(per_strat.to_string())
    print()

    print("=" * 100)
    print("FILTER SIMULATION: What happens if we KEEP ONLY 'aligned' trades?")
    print("=" * 100)
    print()
    rows = []
    for strat in sorted(df.strategy.unique()):
        s_df = df[df.strategy == strat]
        baseline_n = len(s_df)
        baseline_pnl = s_df.pnl_dollars.sum()
        # Filter: keep only aligned
        aligned = s_df[s_df.alignment == "aligned"]
        # Also test: keep aligned + weak (i.e., veto only divergent + wrong)
        keep_not_divergent = s_df[~s_df.alignment.isin(["divergent", "wrong"])]
        rows.append({
            "strategy": strat,
            "baseline_n": baseline_n,
            "baseline_pnl": round(baseline_pnl, 0),
            "aligned_only_n": len(aligned),
            "aligned_only_pnl": round(aligned.pnl_dollars.sum(), 0),
            "aligned_only_lift": round(aligned.pnl_dollars.sum() - baseline_pnl, 0),
            "no_divergent_n": len(keep_not_divergent),
            "no_divergent_pnl": round(keep_not_divergent.pnl_dollars.sum(), 0),
            "no_divergent_lift": round(keep_not_divergent.pnl_dollars.sum() - baseline_pnl, 0),
        })
    sim_df = pd.DataFrame(rows)
    sim_df = sim_df.sort_values("baseline_pnl", ascending=False)
    print(sim_df.to_string(index=False))
    print()

    # Save
    out_csv = ROOT / "backtest_results" / "phoenix_es_nq_attribution.csv"
    df.to_csv(out_csv, index=False)
    sim_csv = ROOT / "backtest_results" / "phoenix_es_nq_filter_simulation.csv"
    sim_df.to_csv(sim_csv, index=False)
    print(f"Wrote attribution -> {out_csv}")
    print(f"Wrote filter sim  -> {sim_csv}")


if __name__ == "__main__":
    main()
