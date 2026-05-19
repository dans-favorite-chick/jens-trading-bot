"""
Phoenix Footprint Confluence Attribution Analysis
===================================================

Tests the Phase 13 Section R.5 hypothesis: does footprint confluence
(delta direction, POC location, imbalance strength) improve per-strategy
WR/expectancy?

METHOD:
  1. Load 2-month MNQ TBBO footprint data (Mar 17 - May 15, 2026)
  2. Load all Phoenix winning-strategy trades from prior backtests
  3. Filter trades to footprint window
  4. For each trade, look up the 5m footprint bar containing entry_ts
  5. Compute footprint signals at entry:
       - delta_strength: |delta| / total_volume
       - delta_aligned: did bar delta match trade direction?
       - poc_relative: where POC sits vs entry price (above/at/below)
       - poc_aligned: does POC location support trade direction?
  6. Bucket trades by footprint signal strength
  7. Compute conditional WR / total $ / per-trade $ per bucket per strategy
  8. Simulate filter rules:
       VETO contradicted trades (kill bad-footprint setups)
       SIZE BOOST confirmed trades (1.5x size on aligned footprint)

OUTPUTS:
  backtest_results/phoenix_footprint_attribution.csv
  backtest_results/phoenix_footprint_filter_simulation.csv
  stdout summary

CAVEATS (honest framing):
  - Only 2 months of footprint data available
  - Only vwap_pullback_v2 has n>30 (190 trades) for statistical weight
  - Other strategies have n=7-28 — directional/hypothesis-generating only
  - Reliable strategy-by-strategy conclusions require forward data accumulation
    (snapshot recorder collecting now) or paid backfill via Databento MBO

  This first-pass analysis tells us IF the hypothesis is plausible and
  WHICH strategies most clearly benefit, NOT what the production lift
  will be.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

FOOTPRINT_CSV = ROOT / "data" / "historical" / "databento_tbbo" / "mnq_footprint_5m.csv"
WINDOW_START = pd.Timestamp("2026-03-17", tz="UTC")
WINDOW_END = pd.Timestamp("2026-05-15", tz="UTC")

# Trade source files (all from Phase 13 backtests)
TRADE_SOURCES = [
    ("backtest_results/phoenix_real_5year.csv", None),
    ("backtest_results/phoenix_new_strategy_lab.csv", None),
    ("backtest_results/phoenix_trend_pullback_lab.csv", "raschke_baseline"),
]

WINNERS = {
    "opening_session", "vwap_pullback_v2", "spring_setup",
    "es_nq_confluence", "bias_momentum", "vwap_band_pullback", "ib_breakout",
    "g_inside_bar_breakout", "e_multi_day_breakout", "a_asian_continuation",
    "raschke_baseline",
}

# Footprint signal thresholds
DELTA_STRENGTH_THRESHOLD = 0.20   # |delta|/volume > 20% = "strong"
POC_PROXIMITY_TICKS = 8           # POC within 8t of entry = "neutral", else above/below


def load_footprint() -> pd.DataFrame:
    """Load per-5m-bar footprint with delta + POC."""
    df = pd.read_csv(FOOTPRINT_CSV)
    df["bar_5m"] = pd.to_datetime(df["bar_5m"], utc=True)
    df = df.sort_values("bar_5m").reset_index(drop=True)
    return df


def load_trades() -> pd.DataFrame:
    """Load winning-strategy trades from all backtest sources."""
    parts = []
    for relpath, filter_strat in TRADE_SOURCES:
        path = ROOT / relpath
        if not path.exists():
            continue
        df = pd.read_csv(path)
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
        if filter_strat:
            df = df[df.strategy == filter_strat]
        else:
            df = df[df.strategy.isin(WINNERS)]
        keep = ["strategy", "direction", "entry_ts", "entry_price",
                "pnl_dollars", "pnl_ticks"]
        parts.append(df[keep])
    combined = pd.concat(parts, ignore_index=True)
    combined = combined[(combined.entry_ts >= WINDOW_START) &
                         (combined.entry_ts <= WINDOW_END)]
    return combined.sort_values("entry_ts").reset_index(drop=True)


def attach_footprint(trades: pd.DataFrame, fp: pd.DataFrame) -> pd.DataFrame:
    """For each trade, find the 5m footprint bar containing entry_ts + compute signals."""
    rows = []
    for tr in trades.itertuples(index=False):
        # Find the 5m bar where bar_5m <= entry_ts < bar_5m + 5min
        bar_start = tr.entry_ts.floor("5min")
        match = fp[fp.bar_5m == bar_start]
        if len(match) == 0:
            rows.append({"footprint_state": "no_data", "delta_strength": None,
                          "delta_aligned": None, "poc_relative": "no_data",
                          "poc_aligned": None})
            continue
        bar = match.iloc[0]
        delta = float(bar.delta)
        vol = float(bar.total_volume)
        poc = float(bar.poc)
        entry = float(tr.entry_price)
        direction = tr.direction  # "LONG" / "SHORT"
        trade_sign = 1 if direction == "LONG" else -1

        delta_strength = abs(delta) / max(vol, 1)
        delta_dir_sign = 1 if delta > 0 else (-1 if delta < 0 else 0)
        delta_aligned = (delta_dir_sign == trade_sign) if delta_dir_sign != 0 else None

        # POC relative to entry — tick-quantized
        poc_diff = poc - entry  # positive = POC above entry
        poc_diff_ticks = poc_diff / 0.25
        if abs(poc_diff_ticks) < POC_PROXIMITY_TICKS:
            poc_relative = "near"
        elif poc_diff > 0:
            poc_relative = "above"
        else:
            poc_relative = "below"

        # POC aligned: for LONG we want POC at-or-below entry (accumulation),
        # for SHORT at-or-above (distribution)
        if direction == "LONG":
            poc_aligned = poc_relative in ("near", "below")
        else:
            poc_aligned = poc_relative in ("near", "above")

        # Combined footprint state
        if delta_aligned is None or delta_strength < 0.05:
            state = "neutral"
        elif delta_aligned and poc_aligned and delta_strength >= DELTA_STRENGTH_THRESHOLD:
            state = "strongly_confirmed"
        elif delta_aligned and (poc_aligned or delta_strength >= DELTA_STRENGTH_THRESHOLD):
            state = "confirmed"
        elif (not delta_aligned) and (not poc_aligned) and delta_strength >= DELTA_STRENGTH_THRESHOLD:
            state = "strongly_contradicted"
        elif (not delta_aligned):
            state = "contradicted"
        else:
            state = "mixed"

        rows.append({
            "footprint_state": state,
            "delta_strength": round(delta_strength, 3),
            "delta_aligned": delta_aligned,
            "poc_relative": poc_relative,
            "poc_aligned": poc_aligned,
        })
    return pd.DataFrame(rows)


def main():
    print("=" * 100)
    print("PHOENIX FOOTPRINT ATTRIBUTION — Phase 13 Section R.5 hypothesis test")
    print("=" * 100)
    print()
    print(f"Footprint window: {WINDOW_START.date()} -> {WINDOW_END.date()}  (2 months)")
    print()

    fp = load_footprint()
    print(f"Footprint bars loaded: {len(fp):,}")
    trades = load_trades()
    print(f"Winning-strategy trades in window: {len(trades):,}")
    print()

    print("=" * 100)
    print("SAMPLE SIZE PER STRATEGY (n>=30 needed for statistical weight)")
    print("=" * 100)
    counts = trades.groupby("strategy").size().sort_values(ascending=False)
    for s, n in counts.items():
        marker = "OK " if n >= 30 else "*  " if n >= 15 else "!! "
        print(f"  [{marker}] {s:30s}  n={n}")
    print()
    print("Legend:")
    print("  OK  = robust sample (statistical weight)")
    print("  *   = borderline (directional but not conclusive)")
    print("  !!  = too small (anecdotal only)")
    print()

    fp_signals = attach_footprint(trades, fp)
    enriched = pd.concat([trades.reset_index(drop=True),
                          fp_signals], axis=1)

    print("=" * 100)
    print("OVERALL: Trade outcomes by footprint state")
    print("=" * 100)
    print()
    overall = enriched.groupby("footprint_state").agg(
        n=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda s: (s > 0).sum()),
        total_pnl=("pnl_dollars", "sum"),
        avg_pnl=("pnl_dollars", "mean"),
    ).round(2)
    overall["wr_pct"] = (overall.wins / overall.n * 100).round(1)
    overall["pct_of_trades"] = (overall.n / overall.n.sum() * 100).round(1)
    overall = overall.sort_values("avg_pnl", ascending=False)
    print(overall[["n", "pct_of_trades", "wr_pct", "total_pnl", "avg_pnl"]].to_string())
    print()

    print("=" * 100)
    print("PER-STRATEGY: footprint state breakdown (focus on n>=30 strategies)")
    print("=" * 100)
    print()
    for s in counts.index:
        s_df = enriched[enriched.strategy == s]
        if len(s_df) < 5:
            continue
        print(f"--- {s}  (n={len(s_df)}) ---")
        per = s_df.groupby("footprint_state").agg(
            n=("pnl_dollars", "count"),
            wr_pct=("pnl_dollars", lambda x: round((x > 0).mean() * 100, 1)),
            total=("pnl_dollars", lambda x: round(x.sum(), 0)),
            avg=("pnl_dollars", lambda x: round(x.mean(), 2)),
        )
        per = per.sort_values("avg", ascending=False)
        print(per.to_string())
        print()

    print("=" * 100)
    print("FILTER SIMULATION: how does removing 'contradicted' trades affect total $?")
    print("=" * 100)
    print()
    sim_rows = []
    for s in counts.index:
        s_df = enriched[enriched.strategy == s]
        if len(s_df) < 5:
            continue
        baseline_n = len(s_df)
        baseline_pnl = s_df.pnl_dollars.sum()
        baseline_wr = (s_df.pnl_dollars > 0).mean() * 100
        # Veto strongly_contradicted only
        keep_strict = s_df[s_df.footprint_state != "strongly_contradicted"]
        # Veto both contradicted and strongly_contradicted
        keep_relaxed = s_df[~s_df.footprint_state.isin(
            ["strongly_contradicted", "contradicted"])]
        sim_rows.append({
            "strategy": s,
            "baseline_n": baseline_n,
            "baseline_wr": round(baseline_wr, 1),
            "baseline_pnl": round(baseline_pnl, 0),
            "veto_strong_n": len(keep_strict),
            "veto_strong_wr": round((keep_strict.pnl_dollars > 0).mean() * 100, 1) if len(keep_strict) > 0 else 0,
            "veto_strong_pnl": round(keep_strict.pnl_dollars.sum(), 0),
            "veto_strong_lift": round(keep_strict.pnl_dollars.sum() - baseline_pnl, 0),
            "veto_relaxed_n": len(keep_relaxed),
            "veto_relaxed_wr": round((keep_relaxed.pnl_dollars > 0).mean() * 100, 1) if len(keep_relaxed) > 0 else 0,
            "veto_relaxed_pnl": round(keep_relaxed.pnl_dollars.sum(), 0),
            "veto_relaxed_lift": round(keep_relaxed.pnl_dollars.sum() - baseline_pnl, 0),
        })
    sim_df = pd.DataFrame(sim_rows).sort_values("baseline_n", ascending=False)
    print(sim_df.to_string(index=False))
    print()

    print("=" * 100)
    print("SIZE BOOST SIMULATION: 1.5x size on strongly_confirmed trades")
    print("=" * 100)
    print()
    boost_rows = []
    for s in counts.index:
        s_df = enriched[enriched.strategy == s]
        if len(s_df) < 5:
            continue
        boosted_pnl = s_df.apply(
            lambda r: r.pnl_dollars * 1.5 if r.footprint_state == "strongly_confirmed"
            else r.pnl_dollars,
            axis=1,
        ).sum()
        baseline_pnl = s_df.pnl_dollars.sum()
        boost_rows.append({
            "strategy": s,
            "n": len(s_df),
            "strongly_confirmed_n": (s_df.footprint_state == "strongly_confirmed").sum(),
            "baseline_pnl": round(baseline_pnl, 0),
            "size_boost_pnl": round(boosted_pnl, 0),
            "boost_lift": round(boosted_pnl - baseline_pnl, 0),
        })
    boost_df = pd.DataFrame(boost_rows).sort_values("n", ascending=False)
    print(boost_df.to_string(index=False))
    print()

    out_dir = ROOT / "backtest_results"
    enriched.to_csv(out_dir / "phoenix_footprint_attribution.csv", index=False)
    sim_df.to_csv(out_dir / "phoenix_footprint_filter_simulation.csv", index=False)
    boost_df.to_csv(out_dir / "phoenix_footprint_size_boost_simulation.csv", index=False)
    print(f"Wrote attribution -> {out_dir / 'phoenix_footprint_attribution.csv'}")
    print(f"Wrote VETO sim    -> {out_dir / 'phoenix_footprint_filter_simulation.csv'}")
    print(f"Wrote BOOST sim   -> {out_dir / 'phoenix_footprint_size_boost_simulation.csv'}")


if __name__ == "__main__":
    main()
