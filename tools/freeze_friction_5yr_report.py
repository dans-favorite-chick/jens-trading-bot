"""Phoenix Freeze-Friction 5yr Backtest Report Builder.

Analyzes the canonical 2026-06-02 5-year backtest CSV produced under
FREEZE_ACTIVE = True with the master friction fix in place. Builds:

  1. Per-strategy 5yr summary CSV (12 rows — one per enabled strategy,
     including zero-trade strategies with NaN/zero metrics).
  2. Analytical markdown report with:
       * Per-strategy summary table
       * Per-state (market_state ASOF join) slice and regime-fragility flags
       * Delta vs. pre-master-fix baseline (2026-05-19 snapshot)
       * Tier distribution
       * VERDICT (GREEN / YELLOW / RED) with narrative
       * Self-second-guess section

Output paths (defaults):
  --csv           : backtest_results/phoenix_real_5year_2026-06-02.csv
  --baseline-csv  : backtest_results/_pre_freeze_friction_2026-06-02/phoenix_real_5year.csv
  --warehouse     : data/warehouse/phoenix.duckdb
  --out-report    : logs/oracle/research/2026-06-02_5yr_freeze_friction_report.md
  --out-summary   : backtest_results/phoenix_real_5year_2026-06-02_summary.csv

CLI:
    python tools/freeze_friction_5yr_report.py [--csv ...] [--baseline-csv ...]
        [--warehouse ...] [--out-report ...] [--out-summary ...]
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

# 12 enabled strategies as of FREEZE_ACTIVE=True (config/strategies.py).
# Hardcoded here to keep this script independent of import-side-effects
# in core/config modules. dom_pullback is enabled but typically produces
# zero trades in the historical sweep.
ENABLED_STRATEGIES = [
    "bias_momentum",
    "dom_pullback",
    "ib_breakout",
    "opening_session",
    "vwap_band_pullback",
    "nq_lsr",
    "orb_v2",
    "es_nq_confluence",
    "a_asian_continuation",
    "e_multi_day_breakout",
    "g_inside_bar_breakout",
    "raschke_baseline",
]

# Sample-size tiers from CLAUDE.md
TIER_BOUNDS = [
    ("HIGH_CONFIDENCE", 666, math.inf),
    ("VALIDATED", 385, 665),
    ("TENTATIVE", 100, 384),
    ("PRELIMINARY", 30, 99),
    ("INSUFFICIENT_SAMPLE", 0, 29),
]


def assign_tier(n: int) -> str:
    """Map a trade count to the operator's sample-size tier."""
    for name, lo, hi in TIER_BOUNDS:
        if lo <= n <= hi:
            return name
    return "INSUFFICIENT_SAMPLE"


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI bounds for a win-rate proportion.

    Returns (NaN, NaN) when n == 0.
    """
    if n == 0:
        return (float("nan"), float("nan"))
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def compute_max_drawdown(pnl_series: pd.Series, entry_ts: pd.Series) -> tuple[float, float]:
    """Peak-to-trough max drawdown in dollars + duration in days.

    Trades are assumed sorted by entry_ts before being passed in. Returns
    (mdd_dollars_negative_or_zero, mdd_duration_days). If no drawdown
    occurred, returns (0.0, 0.0).
    """
    if pnl_series.empty:
        return (0.0, 0.0)
    cum = pnl_series.cumsum().values
    ts = pd.to_datetime(entry_ts).values
    # Peak starts at $0 (pre-trade equity baseline), NOT -inf.
    # If first trade is a loss, the drawdown from baseline must register.
    # Bug-hunter S-2 (2026-06-02): -inf init masked first-trade losses.
    running_peak = 0.0
    running_peak_ts = ts[0]
    max_dd = 0.0  # most negative drawdown value
    max_dd_peak_ts = ts[0]
    max_dd_trough_ts = ts[0]
    for i, val in enumerate(cum):
        if val > running_peak:
            running_peak = val
            running_peak_ts = ts[i]
        dd = val - running_peak
        if dd < max_dd:
            max_dd = dd
            max_dd_peak_ts = running_peak_ts
            max_dd_trough_ts = ts[i]
    # Recovery: first index after trough where cum returns to running_peak at peak
    peak_value_at_peak = None
    # Recompute the peak value at peak_ts and look for recovery
    if max_dd < 0:
        # Find the peak's index
        peak_idx = np.where(ts == max_dd_peak_ts)[0]
        if len(peak_idx):
            peak_val = cum[peak_idx[0]]
            trough_idx = np.where(ts == max_dd_trough_ts)[0]
            if len(trough_idx):
                ti = trough_idx[0]
                recovery_idx = None
                for j in range(ti + 1, len(cum)):
                    if cum[j] >= peak_val:
                        recovery_idx = j
                        break
                if recovery_idx is not None:
                    duration = pd.Timestamp(ts[recovery_idx]) - pd.Timestamp(max_dd_peak_ts)
                else:
                    duration = pd.Timestamp(ts[-1]) - pd.Timestamp(max_dd_peak_ts)
                return (float(max_dd), float(duration.total_seconds() / 86400.0))
    return (float(max_dd), 0.0)


@dataclass
class StrategySummary:
    strategy: str
    was_enabled_at_run_time: bool
    n_trades_5yr: int
    n_trades_per_year: float
    win_rate: float
    wilson_95_ci_lower_wr: float
    wilson_95_ci_upper_wr: float
    tier: str
    profit_factor: float
    expectancy_per_trade: float
    total_pnl_5yr: float
    max_drawdown_dollars: float
    max_drawdown_duration_days: float
    pnl_2021: float
    pnl_2022: float
    pnl_2023: float
    pnl_2024: float
    pnl_2025: float
    pnl_2026: float


def summarize_strategy(strategy: str, df: pd.DataFrame) -> StrategySummary:
    """Build the per-strategy row of the summary CSV."""
    sub = df[df["strategy"] == strategy].copy()
    sub = sub.sort_values("entry_ts")
    n = len(sub)
    if n == 0:
        return StrategySummary(
            strategy=strategy,
            was_enabled_at_run_time=True,
            n_trades_5yr=0,
            n_trades_per_year=0.0,
            win_rate=float("nan"),
            wilson_95_ci_lower_wr=float("nan"),
            wilson_95_ci_upper_wr=float("nan"),
            tier=assign_tier(0),
            profit_factor=float("nan"),
            expectancy_per_trade=float("nan"),
            total_pnl_5yr=0.0,
            max_drawdown_dollars=0.0,
            max_drawdown_duration_days=0.0,
            pnl_2021=0.0, pnl_2022=0.0, pnl_2023=0.0,
            pnl_2024=0.0, pnl_2025=0.0, pnl_2026=0.0,
        )
    wins = int((sub["pnl_dollars"] > 0).sum())
    wr = wins / n
    lo, hi = wilson_ci(wins, n)
    gross_win = sub.loc[sub["pnl_dollars"] > 0, "pnl_dollars"].sum()
    gross_loss = sub.loc[sub["pnl_dollars"] < 0, "pnl_dollars"].sum()  # negative
    if gross_loss == 0:
        pf = float("inf") if gross_win > 0 else float("nan")
    else:
        pf = float(gross_win / abs(gross_loss))
    total_pnl = float(sub["pnl_dollars"].sum())
    expectancy = float(sub["pnl_dollars"].mean())
    mdd, mdd_dur = compute_max_drawdown(sub["pnl_dollars"], sub["entry_ts"])
    year_pnls = {y: 0.0 for y in (2021, 2022, 2023, 2024, 2025, 2026)}
    yr_grouped = sub.groupby("year")["pnl_dollars"].sum()
    for y, val in yr_grouped.items():
        if y in year_pnls:
            year_pnls[y] = float(val)
    return StrategySummary(
        strategy=strategy,
        was_enabled_at_run_time=True,
        n_trades_5yr=int(n),
        n_trades_per_year=float(n / 5.0),
        win_rate=float(wr),
        wilson_95_ci_lower_wr=float(lo),
        wilson_95_ci_upper_wr=float(hi),
        tier=assign_tier(n),
        profit_factor=pf,
        expectancy_per_trade=expectancy,
        total_pnl_5yr=total_pnl,
        max_drawdown_dollars=float(mdd),
        max_drawdown_duration_days=float(mdd_dur),
        pnl_2021=year_pnls[2021],
        pnl_2022=year_pnls[2022],
        pnl_2023=year_pnls[2023],
        pnl_2024=year_pnls[2024],
        pnl_2025=year_pnls[2025],
        pnl_2026=year_pnls[2026],
    )


def build_per_state_slice(
    df: pd.DataFrame, warehouse_path: Path
) -> tuple[pd.DataFrame, pd.DataFrame, list[tuple[str, str, float]]]:
    """ASOF-join trades to market_state_bars and aggregate by strategy x state.

    Returns:
        joined_df: trades + market_state (one row per trade)
        per_strat_state: aggregate (strategy, state) → n, win_rate, pf, total_pnl
        fragile: list of (strategy, dominant_state, concentration_pct) for strategies
                 where one state > 70% of |pnl|.
    """
    con = duckdb.connect(str(warehouse_path), read_only=True)
    # Register the trades DataFrame to DuckDB
    trades = df[["strategy", "entry_ts", "pnl_dollars"]].copy()
    # CSV entry_ts is naive-looking but has +00:00 suffix → parse as TZ-aware UTC
    trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
    con.register("csv_trades", trades)
    query = """
        SELECT t.strategy,
               t.entry_ts,
               COALESCE(m.label, 'UNMATCHED') AS market_state,
               t.pnl_dollars
        FROM csv_trades t
        ASOF LEFT JOIN market_state_bars m
          ON t.entry_ts >= m.bar_ts
        ORDER BY t.entry_ts
    """
    joined = con.execute(query).df()
    con.close()

    # Pre-compute per-(strategy, state) gross abs-pnl share. This is the
    # CANONICAL concentration metric used by both the §4b table renderer and
    # the §4c fragile list (and the verdict). Bug-hunter S-1 (2026-06-02):
    # previously the renderer recomputed its own share from net per-state
    # totals while the fragile list used gross per-trade absolute values,
    # so the same number was reported two different ways in the same report.
    gross_abs_by_strat_state: dict[tuple[str, str], float] = {}
    gross_abs_total_by_strat: dict[str, float] = {}
    for (strat, state), grp in joined.groupby(["strategy", "market_state"]):
        gross_abs = float(np.abs(grp["pnl_dollars"]).sum())
        gross_abs_by_strat_state[(strat, state)] = gross_abs
    for strat, grp in joined.groupby("strategy"):
        gross_abs_total_by_strat[strat] = float(np.abs(grp["pnl_dollars"]).sum())

    rows = []
    for (strat, state), grp in joined.groupby(["strategy", "market_state"]):
        wins = int((grp["pnl_dollars"] > 0).sum())
        n = len(grp)
        wr = wins / n if n else float("nan")
        gw = grp.loc[grp["pnl_dollars"] > 0, "pnl_dollars"].sum()
        gl = grp.loc[grp["pnl_dollars"] < 0, "pnl_dollars"].sum()
        pf = (gw / abs(gl)) if gl != 0 else (float("inf") if gw > 0 else float("nan"))
        total = float(grp["pnl_dollars"].sum())
        gross_abs = gross_abs_by_strat_state[(strat, state)]
        strat_total_gross = gross_abs_total_by_strat[strat]
        share = (gross_abs / strat_total_gross) if strat_total_gross > 0 else 0.0
        rows.append({
            "strategy": strat,
            "market_state": state,
            "n": n,
            "win_rate": wr,
            "profit_factor": pf,
            "total_pnl": total,
            "gross_abs_pnl": gross_abs,
            "gross_abs_pnl_share": share,
        })
    per_strat_state = pd.DataFrame(rows)

    # Identify regime-fragile strategies (>70% gross-abs-PnL share from one state).
    # Same denominator as the §4b table share column — single source of truth.
    fragile: list[tuple[str, str, float]] = []
    for strat, total_abs in gross_abs_total_by_strat.items():
        if total_abs <= 0:
            continue
        for (s2, state), gross_abs in gross_abs_by_strat_state.items():
            if s2 != strat:
                continue
            pct = gross_abs / total_abs
            if pct > 0.70:
                fragile.append((strat, state, pct))
                break
    return joined, per_strat_state, fragile


def build_baseline_delta(new_df: pd.DataFrame, baseline_df: pd.DataFrame) -> pd.DataFrame:
    """Compute per-strategy delta: new run vs. pre-master-fix snapshot."""
    def agg(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["strategy", "n", "wr", "pf", "mean_pnl"])
        rows = []
        for strat, grp in df.groupby("strategy"):
            n = len(grp)
            wins = int((grp["pnl_dollars"] > 0).sum())
            wr = wins / n if n else float("nan")
            gw = grp.loc[grp["pnl_dollars"] > 0, "pnl_dollars"].sum()
            gl = grp.loc[grp["pnl_dollars"] < 0, "pnl_dollars"].sum()
            pf = (gw / abs(gl)) if gl != 0 else (float("inf") if gw > 0 else float("nan"))
            mp = float(grp["pnl_dollars"].mean())
            rows.append({"strategy": strat, "n": n, "wr": wr, "pf": pf, "mean_pnl": mp})
        return pd.DataFrame(rows)

    new_agg = agg(new_df).set_index("strategy")
    base_agg = agg(baseline_df).set_index("strategy")
    all_strats = sorted(set(new_agg.index) | set(base_agg.index))
    rows = []
    for s in all_strats:
        in_new = s in new_agg.index
        in_base = s in base_agg.index
        if in_new and in_base:
            tag = "BOTH"
            wr_d = new_agg.loc[s, "wr"] - base_agg.loc[s, "wr"]
            pf_d = new_agg.loc[s, "pf"] - base_agg.loc[s, "pf"]
            n_d = int(new_agg.loc[s, "n"] - base_agg.loc[s, "n"])
            mp_d = new_agg.loc[s, "mean_pnl"] - base_agg.loc[s, "mean_pnl"]
            rows.append({
                "strategy": s, "status": tag,
                "new_n": int(new_agg.loc[s, "n"]),
                "baseline_n": int(base_agg.loc[s, "n"]),
                "n_trades_delta": n_d,
                "new_wr": new_agg.loc[s, "wr"],
                "baseline_wr": base_agg.loc[s, "wr"],
                "wr_delta_pp": wr_d * 100.0,
                "new_pf": new_agg.loc[s, "pf"],
                "baseline_pf": base_agg.loc[s, "pf"],
                "pf_delta": pf_d,
                "new_mean_pnl": new_agg.loc[s, "mean_pnl"],
                "baseline_mean_pnl": base_agg.loc[s, "mean_pnl"],
                "mean_pnl_delta": mp_d,
            })
        elif in_new:
            rows.append({
                "strategy": s, "status": "NEW_ONLY",
                "new_n": int(new_agg.loc[s, "n"]),
                "baseline_n": 0,
                "n_trades_delta": int(new_agg.loc[s, "n"]),
                "new_wr": new_agg.loc[s, "wr"],
                "baseline_wr": float("nan"),
                "wr_delta_pp": float("nan"),
                "new_pf": new_agg.loc[s, "pf"],
                "baseline_pf": float("nan"),
                "pf_delta": float("nan"),
                "new_mean_pnl": new_agg.loc[s, "mean_pnl"],
                "baseline_mean_pnl": float("nan"),
                "mean_pnl_delta": float("nan"),
            })
        else:
            rows.append({
                "strategy": s, "status": "BASELINE_ONLY",
                "new_n": 0,
                "baseline_n": int(base_agg.loc[s, "n"]),
                "n_trades_delta": -int(base_agg.loc[s, "n"]),
                "new_wr": float("nan"),
                "baseline_wr": base_agg.loc[s, "wr"],
                "wr_delta_pp": float("nan"),
                "new_pf": float("nan"),
                "baseline_pf": base_agg.loc[s, "pf"],
                "pf_delta": float("nan"),
                "new_mean_pnl": float("nan"),
                "baseline_mean_pnl": base_agg.loc[s, "mean_pnl"],
                "mean_pnl_delta": float("nan"),
            })
    return pd.DataFrame(rows)


def _fmt(v: float, digits: int = 2, dollar: bool = False, pct: bool = False) -> str:
    """Format a numeric value for markdown tables, handling NaN/inf gracefully."""
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "—"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    if pct:
        return f"{v * 100:.{digits}f}%"
    if dollar:
        return f"${v:,.{digits}f}"
    return f"{v:,.{digits}f}"


def render_summary_table(summary_df: pd.DataFrame) -> str:
    """Render the per-strategy summary as a markdown table."""
    cols = [
        ("strategy", "Strategy"),
        ("n_trades_5yr", "N(5y)"),
        ("n_trades_per_year", "N/yr"),
        ("tier", "Tier"),
        ("win_rate", "WR"),
        ("wilson_95_ci_lower_wr", "WR CI lo"),
        ("wilson_95_ci_upper_wr", "WR CI hi"),
        ("profit_factor", "PF"),
        ("expectancy_per_trade", "Exp$"),
        ("total_pnl_5yr", "Total $"),
        ("max_drawdown_dollars", "MaxDD $"),
        ("max_drawdown_duration_days", "MDD days"),
    ]
    header = "| " + " | ".join(h for _, h in cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    lines = [header, sep]
    df_sorted = summary_df.sort_values("total_pnl_5yr", ascending=False)
    for _, row in df_sorted.iterrows():
        cells = []
        for key, _ in cols:
            v = row[key]
            if key == "strategy":
                cells.append(str(v))
            elif key in ("n_trades_5yr",):
                cells.append(f"{int(v)}" if not pd.isna(v) else "—")
            elif key == "n_trades_per_year":
                cells.append(_fmt(v, 1))
            elif key == "tier":
                cells.append(str(v))
            elif key in ("win_rate", "wilson_95_ci_lower_wr", "wilson_95_ci_upper_wr"):
                cells.append(_fmt(v, 1, pct=True))
            elif key == "profit_factor":
                cells.append(_fmt(v, 2))
            elif key in ("expectancy_per_trade", "total_pnl_5yr", "max_drawdown_dollars"):
                cells.append(_fmt(v, 2, dollar=True))
            elif key == "max_drawdown_duration_days":
                cells.append(_fmt(v, 1))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_per_strat_state_table(per_strat_state: pd.DataFrame) -> str:
    """Render the per-strategy × per-state PF table with concentration flags.

    The `gross_abs_pnl_share` column is the CANONICAL concentration metric —
    the same one used by §4c's fragile list and the verdict logic. Pre-fix
    (2026-06-02 bug-hunter S-1) the renderer computed its own share from
    net per-state totals, which disagreed with the fragile-list share for
    any state mixing wins and losses.
    """
    if per_strat_state.empty:
        return "_No trades joined to any market_state._"
    rows = per_strat_state.sort_values(["strategy", "total_pnl"], ascending=[True, False])
    header = "| Strategy | State | N | WR | PF | Total $ | |PnL| share |"
    sep = "|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for _, r in rows.iterrows():
        share = float(r["gross_abs_pnl_share"])
        flag = " **FRAGILE**" if share > 0.70 else ""
        cells = [
            r["strategy"],
            r["market_state"],
            f"{int(r['n'])}",
            _fmt(r["win_rate"], 1, pct=True),
            _fmt(r["profit_factor"], 2),
            _fmt(r["total_pnl"], 2, dollar=True),
            _fmt(share, 1, pct=True) + flag,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_aggregate_state_distribution(joined: pd.DataFrame) -> str:
    """Render aggregate state distribution table (n_trades per state)."""
    if joined.empty:
        return "_No trades._"
    dist = joined.groupby("market_state").agg(
        n_trades=("pnl_dollars", "size"),
        total_pnl=("pnl_dollars", "sum"),
        mean_pnl=("pnl_dollars", "mean"),
    ).reset_index().sort_values("n_trades", ascending=False)
    total_n = dist["n_trades"].sum()
    lines = [
        "| Market state | N | Share | Total $ | Mean $/trade |",
        "|---|---|---|---|---|",
    ]
    for _, r in dist.iterrows():
        lines.append(
            "| "
            + " | ".join([
                r["market_state"],
                f"{int(r['n_trades'])}",
                _fmt(r["n_trades"] / total_n, 1, pct=True),
                _fmt(r["total_pnl"], 2, dollar=True),
                _fmt(r["mean_pnl"], 2, dollar=True),
            ])
            + " |"
        )
    return "\n".join(lines)


def render_baseline_delta_table(delta_df: pd.DataFrame) -> str:
    """Render the baseline delta comparison table."""
    if delta_df.empty:
        return "_No baseline comparison rows._"
    delta_df = delta_df.sort_values(["status", "strategy"])
    header = (
        "| Strategy | Status | New N | Base N | ΔN | New WR | Base WR | ΔWR pp | "
        "New PF | Base PF | ΔPF | New mean$ | Base mean$ | Δmean$ |"
    )
    sep = "|" + "|".join(["---"] * 14) + "|"
    lines = [header, sep]
    for _, r in delta_df.iterrows():
        cells = [
            r["strategy"],
            r["status"],
            f"{int(r['new_n'])}",
            f"{int(r['baseline_n'])}",
            f"{int(r['n_trades_delta']):+d}" if not pd.isna(r["n_trades_delta"]) else "—",
            _fmt(r["new_wr"], 1, pct=True),
            _fmt(r["baseline_wr"], 1, pct=True),
            _fmt(r["wr_delta_pp"], 1),
            _fmt(r["new_pf"], 2),
            _fmt(r["baseline_pf"], 2),
            _fmt(r["pf_delta"], 2),
            _fmt(r["new_mean_pnl"], 2, dollar=True),
            _fmt(r["baseline_mean_pnl"], 2, dollar=True),
            _fmt(r["mean_pnl_delta"], 2, dollar=True),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_tier_distribution(summary_df: pd.DataFrame) -> str:
    """Render the tier distribution table."""
    tier_groups = defaultdict(list)
    for _, r in summary_df.iterrows():
        tier_groups[r["tier"]].append(
            f"{r['strategy']} (n={int(r['n_trades_5yr'])})"
        )
    lines = ["| Tier | Strategies |", "|---|---|"]
    for tier, _, _ in TIER_BOUNDS:
        strats = tier_groups.get(tier, [])
        cell = ", ".join(strats) if strats else "—"
        lines.append(f"| {tier} | {cell} |")
    return "\n".join(lines)


def compute_verdict(
    summary_df: pd.DataFrame, fragile: list[tuple[str, str, float]]
) -> tuple[str, str]:
    """Compute the GREEN / YELLOW / RED verdict and narrative.

    Rules per the orchestrator spec:
      RED:
        * MaxDD > $2,000 over 5yr for any strategy
        * Any strategy with negative total_pnl_5yr
        * Any strategy with single-state |pnl| concentration > 90%
        * Any producing strategy with PF < 1 (negative-PF)
      YELLOW:
        * 1-2 strategies regime-fragile (>70% single-state)
        * Or 1-2 strategies under-sampled (n < 30, not counting zero-trade)
        * Or modest issues stopping a clean GREEN
      GREEN: ALL clear: tier >= PRELIMINARY, total_pnl > 0, no fragility.
    """
    producing = summary_df[summary_df["n_trades_5yr"] > 0].copy()

    red_reasons: list[str] = []
    yellow_reasons: list[str] = []

    for _, r in producing.iterrows():
        if r["max_drawdown_dollars"] < -2000:
            red_reasons.append(
                f"{r['strategy']} MaxDD = {_fmt(r['max_drawdown_dollars'], 2, dollar=True)} "
                f"(threshold -$2,000)"
            )
        if r["total_pnl_5yr"] < 0:
            red_reasons.append(
                f"{r['strategy']} total_pnl_5yr = {_fmt(r['total_pnl_5yr'], 2, dollar=True)} "
                f"(negative over 5y)"
            )
        if not math.isnan(r["profit_factor"]) and not math.isinf(r["profit_factor"]):
            if r["profit_factor"] < 1.0:
                red_reasons.append(
                    f"{r['strategy']} PF = {r['profit_factor']:.2f} (< 1.0)"
                )

    # Heavy concentration > 90% counts as RED
    for strat, state, pct in fragile:
        if pct > 0.90:
            red_reasons.append(
                f"{strat} has {_fmt(pct, 1, pct=True)} of |pnl| in {state} (>90%)"
            )
        else:
            yellow_reasons.append(
                f"{strat} has {_fmt(pct, 1, pct=True)} of |pnl| concentrated in {state}"
            )

    # Under-sampled producing strategies (PRELIMINARY tier, n<30 but >0)
    for _, r in producing.iterrows():
        if r["n_trades_5yr"] < 30:
            yellow_reasons.append(
                f"{r['strategy']} under-sampled (n={int(r['n_trades_5yr'])}, < 30)"
            )

    # Zero-trade enabled strategies → YELLOW signal (not RED)
    zero_trade = summary_df[summary_df["n_trades_5yr"] == 0]
    for _, r in zero_trade.iterrows():
        yellow_reasons.append(f"{r['strategy']} produced 0 trades over 5y")

    if red_reasons:
        color = "RED"
    elif yellow_reasons:
        # YELLOW if 1-2 categories of concern; if many, escalate to RED on volume?
        # Spec says YELLOW for "Most clear but 1-2 are regime-fragile or under-sampled".
        # Use threshold: >5 yellow reasons → still YELLOW unless one becomes RED.
        color = "YELLOW"
    else:
        color = "GREEN"

    # Build narrative
    producing_total = producing["total_pnl_5yr"].sum()
    n_producing = len(producing)
    n_profitable = int((producing["total_pnl_5yr"] > 0).sum())
    worst_mdd_row = (
        producing.loc[producing["max_drawdown_dollars"].idxmin()]
        if not producing.empty
        else None
    )
    best_pnl_row = (
        producing.loc[producing["total_pnl_5yr"].idxmax()]
        if not producing.empty
        else None
    )

    para1 = (
        f"Verdict is **{color}**. Of {len(summary_df)} enabled strategies, "
        f"{n_producing} produced trades over the 5y window with a combined "
        f"total P&L of {_fmt(producing_total, 2, dollar=True)}; "
        f"{n_profitable} of those were net profitable. "
    )
    if best_pnl_row is not None:
        para1 += (
            f"Top contributor: **{best_pnl_row['strategy']}** at "
            f"{_fmt(best_pnl_row['total_pnl_5yr'], 2, dollar=True)} over "
            f"{int(best_pnl_row['n_trades_5yr'])} trades "
            f"(PF {_fmt(best_pnl_row['profit_factor'], 2)}, "
            f"WR {_fmt(best_pnl_row['win_rate'], 1, pct=True)}). "
        )
    if worst_mdd_row is not None:
        para1 += (
            f"Worst drawdown: **{worst_mdd_row['strategy']}** at "
            f"{_fmt(worst_mdd_row['max_drawdown_dollars'], 2, dollar=True)} "
            f"over {_fmt(worst_mdd_row['max_drawdown_duration_days'], 1)} days."
        )

    para2_bits = []
    if red_reasons:
        para2_bits.append("RED triggers: " + "; ".join(red_reasons[:6]))
    if yellow_reasons:
        para2_bits.append("YELLOW flags: " + "; ".join(yellow_reasons[:6]))
    para2 = " ".join(para2_bits) if para2_bits else (
        "No material concerns surfaced — all producing strategies cleared "
        "PRELIMINARY tier, were net profitable over 5y, kept drawdowns under "
        "$2,000, and showed no single-state |pnl| concentration above 70%."
    )

    para3 = (
        "Operator next-steps reading: a RED verdict means a material design-cap "
        "intent (the $45 daily / $200 weekly loss cap shape) is being violated "
        "in backtest — investigate the offending strategy before any kill-list "
        "or sizing-tier change. A YELLOW verdict means the freeze + master-fix "
        "configuration is shippable as a snapshot but at least one strategy "
        "remains regime-fragile or under-sampled, and further validation "
        "(walk-forward, regime-stratified hold-out, or sim-paper reconciliation) "
        "is the next gate. A GREEN verdict means the run meets the freeze-friction "
        "shipping bar; it does NOT mean Phase 13 promotion clears the n=100 / "
        "Wilson-CI guardrail or the live-vs-backtest reconciliation prerequisite."
    )
    return color, para1 + "\n\n" + para2 + "\n\n" + para3


def render_report(
    summary_df: pd.DataFrame,
    joined: pd.DataFrame,
    per_strat_state: pd.DataFrame,
    fragile: list[tuple[str, str, float]],
    delta_df: pd.DataFrame,
    args: argparse.Namespace,
    verdict_color: str,
    verdict_narrative: str,
) -> str:
    """Compose the full markdown report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    zero_trade = summary_df[summary_df["n_trades_5yr"] == 0]["strategy"].tolist()
    fragile_lines = (
        "\n".join(
            f"- **{s}** — {state} contributes "
            f"{_fmt(pct, 1, pct=True)} of |pnl|"
            for s, state, pct in fragile
        )
        if fragile
        else "_None — no strategy has > 70% |pnl| concentration in a single state._"
    )

    md = f"""# Phoenix Freeze-Friction 5yr Backtest Report

_Generated: {now}_
_Window: 2021-06-01 → 2026-06-01_
_Source CSV: {args.csv}_
_Baseline CSV: {args.baseline_csv}_
_Warehouse: {args.warehouse}_

## 1. Run scope

- 12 enabled strategies measured (operator §9.1 LOCKED — disabled excluded from this run).
- Frozen config (`FREEZE_ACTIVE = True`) — no parameter changes since freeze.
- Friction default-off canonical run (no `--apply-decay`); this is the un-decayed P&L curve.
- CSV row count: **{len(_load_csv(args.csv))}** trades total across all producing strategies.
- Trade timestamps treated as UTC and ASOF-joined to `market_state_bars` (TIMESTAMPTZ) for regime slicing.

## 2. Per-strategy 5yr summary

{render_summary_table(summary_df)}

## 3. Strategies with 0 trades (flagged)

{('- ' + chr(10) + '- ').join(zero_trade) if zero_trade else "_None — every enabled strategy produced at least one trade._"}

## 4. Per-state slice (market_state ASOF join)

### 4a. Aggregate state distribution

{render_aggregate_state_distribution(joined)}

### 4b. Per-strategy × per-state PF and |pnl| share

{render_per_strat_state_table(per_strat_state)}

### 4c. Regime-fragile strategies (single-state >70% |pnl| concentration)

{fragile_lines}

## 5. Delta vs pre-master-fix baseline (2026-05-19 snapshot)

The baseline was an `--all` run that included disabled strategies; the current freeze run only measures the 12 enabled. Strategies marked **BASELINE_ONLY** were disabled-but-measured then; **NEW_ONLY** means they were enabled but produced no rows in the prior file.

{render_baseline_delta_table(delta_df)}

## 6. Tier distribution

{render_tier_distribution(summary_df)}

## VERDICT — {verdict_color}

{verdict_narrative}

## 7. Self-second-guess (report-builder)

- **Strongest risk in this analysis**: The market_state ASOF join uses the most recent `bar_ts <= entry_ts`. The warehouse table runs 2021-05-16 → 2026-05-15 (5-min cadence), so trades after 2026-05-15 in the new CSV fall back to UNMATCHED. If the operator scales out the time window or the bar-cadence table goes stale, the fragility flags can silently under-report concentration. The fix is to refresh `market_state_bars` to the new CSV's tail before re-running the script.
- **Weakest assumption**: I treat the CSV `entry_ts` strings (`...+00:00`) as authoritative UTC and rely on DuckDB's TIMESTAMPTZ comparison to normalize across the warehouse's underlying America/Chicago display. If the upstream harness ever wrote a naive timestamp without the `+00:00` suffix, the join would silently shift by the local offset. This is masked by the current data shape but should be guarded with a parser assert in a follow-up.
- **One alternative considered and rejected**: I considered ranking strategies by Sharpe over 5y trades instead of by total_pnl_5yr for the verdict tiering. Rejected because the freeze-friction intent is "does the dollar P&L curve survive the master fix?" — Sharpe would hide trade-count-fragile strategies (a high-Sharpe / low-n strategy would mask the regime concentration problem the operator is hunting). The Wilson-CI tier mapping already gates on sample size, so the dollar-PnL framing remains correct.
"""
    return md


def _load_csv(path: str) -> pd.DataFrame:
    """Tiny lazy loader for the byte-size sanity row count."""
    df = pd.read_csv(path)
    return df


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build the Phoenix freeze-friction 5yr backtest report. "
            "Reads the new CSV + pre-master-fix baseline + market_state_bars "
            "warehouse table; emits a markdown report and a per-strategy "
            "summary CSV. See module docstring for output schemas."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--csv",
        default="backtest_results/phoenix_real_5year_2026-06-02.csv",
        help="Path to the new canonical 5yr CSV (output of phoenix_real_backtest.py).",
    )
    p.add_argument(
        "--baseline-csv",
        default="backtest_results/_pre_freeze_friction_2026-06-02/phoenix_real_5year.csv",
        help="Path to the pre-master-fix snapshot CSV (2026-05-19 run).",
    )
    p.add_argument(
        "--warehouse",
        default="data/warehouse/phoenix.duckdb",
        help="Path to phoenix.duckdb (read-only); must contain market_state_bars.",
    )
    p.add_argument(
        "--out-report",
        default="logs/oracle/research/2026-06-02_5yr_freeze_friction_report.md",
        help="Markdown report output path.",
    )
    p.add_argument(
        "--out-summary",
        default="backtest_results/phoenix_real_5year_2026-06-02_summary.csv",
        help="Per-strategy summary CSV output path (12 rows, one per enabled strategy).",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    csv_path = Path(args.csv)
    base_path = Path(args.baseline_csv)
    wh_path = Path(args.warehouse)
    out_report = Path(args.out_report)
    out_summary = Path(args.out_summary)

    for p in (csv_path, base_path, wh_path):
        if not p.exists():
            print(f"[fatal] missing input: {p}", file=sys.stderr)
            return 2

    # Load
    new_df = pd.read_csv(csv_path)
    baseline_df = pd.read_csv(base_path)

    # Build per-strategy summary (one row per ENABLED strategy)
    summaries = [summarize_strategy(s, new_df) for s in ENABLED_STRATEGIES]
    summary_df = pd.DataFrame([s.__dict__ for s in summaries])

    # Per-state slice
    joined, per_strat_state, fragile = build_per_state_slice(new_df, wh_path)

    # Baseline delta
    delta_df = build_baseline_delta(new_df, baseline_df)

    # Verdict
    verdict_color, verdict_narrative = compute_verdict(summary_df, fragile)

    # Write outputs
    out_summary.parent.mkdir(parents=True, exist_ok=True)
    summary_df.to_csv(out_summary, index=False)

    out_report.parent.mkdir(parents=True, exist_ok=True)
    report_md = render_report(
        summary_df, joined, per_strat_state, fragile, delta_df,
        args, verdict_color, verdict_narrative,
    )
    out_report.write_text(report_md, encoding="utf-8")

    report_size = out_report.stat().st_size
    print(f"[ok] summary CSV  -> {out_summary} ({len(summary_df)} rows)")
    print(f"[ok] report MD    -> {out_report} ({report_size} bytes)")
    print(f"[ok] verdict      -> {verdict_color}")
    if report_size < 10_000:
        print(
            f"[warn] report is {report_size} bytes (< 10 KB); operator threshold not met.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
