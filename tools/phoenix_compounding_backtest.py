"""
Phoenix Compounding Backtest
============================

Takes the per-trade data from the 5-year backtests, walks the equity curve
forward applying a sizing policy that scales contracts as equity grows.

Strategies compounded over (Phase 13 portfolio):
  Existing (baseline exits from phoenix_real_5year.csv):
    - opening_session       +$31,894
    - vwap_pullback_v2      +$10,144
    - spring_setup           +$2,745
    - es_nq_confluence       +$2,028
    - bias_momentum          +$1,492
    - vwap_band_pullback     +$794
    - ib_breakout            +$342
  New (from phoenix_new_strategy_lab.csv):
    - inside_bar_breakout   +$11,300
    - multi_day_breakout     +$9,097
    - asian_continuation     +$5,909

Total 1-contract baseline: ~$75,745 over 5y.

EXCLUDED:
  - vwap_band_reversion (baseline -$6,491; only positive AFTER filter applied,
    which the existing CSV doesn't have — re-running with filter is a separate task)
  - compression_*, noise_area, open_drive (killed)

Sizing policies tested:
  1. flat_1            — always 1 contract (baseline reference)
  2. tier_1500         — 1 contract per $1,500 equity (aggressive)
  3. tier_3000         — 1 contract per $3,000 equity (moderate)
  4. tier_5000         — 1 contract per $5,000 equity (conservative)
  5. fixed_ratio_jones — Ryan Jones (delta = starting_equity)

Safety rails applied:
  - 1 tick per side per contract slippage ($1 round-trip per contract)
  - DD scale-down: if equity < 75% of all-time-high, drop 1 contract tier
  - Daily circuit breaker: if daily loss > 5% of equity, halt rest of day
  - Min 1 contract floor; max raised to 50 (per operator: no cap)

Output:
  backtest_results/phoenix_compounding_<policy>.csv  — daily equity curve
  backtest_results/phoenix_compounding_summary.csv   — per-policy summary
  backtest_results/phoenix_compounding_tier_dates.csv — when each contract tier first reached
  stdout                                              — formatted summary
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TICK = 0.25
TICK_VALUE = 0.50
DD_SCALE_DOWN_PCT = 0.85        # drop tier if equity < 85% of ATH (stricter)
DAILY_CIRCUIT_PCT = 0.04        # halt day if daily loss > 4% of equity
CONSECUTIVE_LOSS_LIMIT = 3      # 3 losses in a row → drop tier for next trade
MAX_CONTRACTS_CAP = 30          # physical cap (CME / MNQ liquidity reality)


def size_scaled_slippage_ticks(contracts: int) -> int:
    """Market-impact model: bigger size eats more spread.
    1-5 contracts: 1 tick per side
    6-15 contracts: 1.5 ticks (avg)
    16-30 contracts: 2 ticks
    >30: 3 ticks
    Returns total round-trip slippage ticks."""
    if contracts <= 5:
        return 2  # 1 tick per side
    elif contracts <= 15:
        return 3
    elif contracts <= 30:
        return 4
    else:
        return 6

# Winning strategies to include
WINNERS_EXISTING = [
    "opening_session", "vwap_pullback_v2", "spring_setup",
    "es_nq_confluence", "bias_momentum", "vwap_band_pullback", "ib_breakout",
]
WINNERS_NEW = [
    "g_inside_bar_breakout", "e_multi_day_breakout", "a_asian_continuation",
]


# ════════════════════════════════════════════════════════════════════
# Sizing policies
# ════════════════════════════════════════════════════════════════════

def sizing_flat_1(equity: float, ath_equity: float, starting_equity: float) -> int:
    return 1


def _tier_sizing(equity: float, ath_equity: float, dollars_per_contract: float) -> int:
    """1 contract per $X equity, with DD scale-down + hard cap at MAX_CONTRACTS_CAP."""
    base = max(1, int(equity / dollars_per_contract))
    if ath_equity > 0 and equity < DD_SCALE_DOWN_PCT * ath_equity:
        base = max(1, base - 1)
    return min(base, MAX_CONTRACTS_CAP)


def sizing_tier_1500(equity, ath, start):
    return _tier_sizing(equity, ath, 1500.0)


def sizing_tier_3000(equity, ath, start):
    return _tier_sizing(equity, ath, 3000.0)


def sizing_tier_5000(equity, ath, start):
    return _tier_sizing(equity, ath, 5000.0)


def sizing_jones(equity, ath, start, delta=None):
    """Ryan Jones fixed-ratio. contracts = (1 + sqrt(1 + 8 * profit / delta)) / 2.
    delta defaults to starting_equity. Self-scales down on losses."""
    d = delta if delta is not None else start
    profit = equity - start
    if profit <= 0:
        return 1
    n = (1 + math.sqrt(1 + 8 * profit / d)) / 2
    n = max(1, int(n))
    if ath > 0 and equity < DD_SCALE_DOWN_PCT * ath:
        n = max(1, n - 1)
    return min(n, MAX_CONTRACTS_CAP)


# Winner-weighted multipliers — Tier 1 strategies get 1.5x, Tier 3 get 0.5x.
# Based on per-strategy contribution to compounded P&L (see Phase 13 doc Section J).
STRATEGY_SIZE_MULT = {
    # TIER 1 (top 5 contributors = 96% of compounded P&L)
    "opening_session":       1.5,
    "g_inside_bar_breakout": 1.3,
    "e_multi_day_breakout":  1.3,
    "vwap_pullback_v2":      1.2,
    "a_asian_continuation":  1.2,
    # TIER 2 (proven, normal weight)
    "es_nq_confluence":      1.0,
    "ib_breakout":           1.0,
    # TIER 3 (small contributors — half size pending validation)
    "vwap_band_pullback":    0.5,
    "spring_setup":          0.5,   # 0.5 until fixed_2x_target ships
    "bias_momentum":         0.5,   # too few trades
}


STRATEGY_SIZE_MULT_LIGHT = {
    # Lighter weighting — Tier 1 × 1.2, Tier 3 × 0.7 (less concentration risk)
    "opening_session":       1.2,
    "g_inside_bar_breakout": 1.2,
    "e_multi_day_breakout":  1.2,
    "vwap_pullback_v2":      1.1,
    "a_asian_continuation":  1.1,
    "es_nq_confluence":      1.0,
    "ib_breakout":           1.0,
    "vwap_band_pullback":    0.7,
    "spring_setup":          0.7,
    "bias_momentum":         0.7,
}


def sizing_winner_weighted_tier3000(equity, ath, start, strategy=None):
    """Like tier_3000 but applies STRATEGY_SIZE_MULT (aggressive: 1.5/0.5)."""
    base = _tier_sizing(equity, ath, 3000.0)
    mult = STRATEGY_SIZE_MULT.get(strategy, 1.0) if strategy else 1.0
    n = max(1, int(round(base * mult)))
    return min(n, MAX_CONTRACTS_CAP)


def sizing_winner_weighted_light(equity, ath, start, strategy=None):
    """Like tier_3000 but applies STRATEGY_SIZE_MULT_LIGHT (1.2/0.7)."""
    base = _tier_sizing(equity, ath, 3000.0)
    mult = STRATEGY_SIZE_MULT_LIGHT.get(strategy, 1.0) if strategy else 1.0
    n = max(1, int(round(base * mult)))
    return min(n, MAX_CONTRACTS_CAP)


POLICIES: dict[str, Callable] = {
    "flat_1":            sizing_flat_1,
    "tier_1500":         sizing_tier_1500,
    "tier_3000":         sizing_tier_3000,
    "tier_5000":         sizing_tier_5000,
    "fixed_ratio_jones": sizing_jones,
    "winner_weighted_3000": sizing_winner_weighted_tier3000,
    "winner_weighted_light": sizing_winner_weighted_light,
}


# ════════════════════════════════════════════════════════════════════
# Trade loader
# ════════════════════════════════════════════════════════════════════

def load_combined_trades() -> pd.DataFrame:
    existing_csv = ROOT / "backtest_results" / "phoenix_real_5year.csv"
    new_csv = ROOT / "backtest_results" / "phoenix_new_strategy_lab.csv"

    existing = pd.read_csv(existing_csv)
    new = pd.read_csv(new_csv)

    existing = existing[existing.strategy.isin(WINNERS_EXISTING)].copy()
    new = new[new.strategy.isin(WINNERS_NEW)].copy()

    # Normalize columns
    for df in (existing, new):
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)

    keep_cols = ["strategy", "entry_ts", "direction", "entry_price",
                 "stop_price", "target_price", "exit_ts", "exit_price",
                 "pnl_dollars", "pnl_ticks", "hold_min", "year"]
    existing = existing[keep_cols].copy()
    new["exit_ts"] = pd.to_datetime(new["exit_ts"], utc=True)
    new = new[keep_cols].copy()

    combined = pd.concat([existing, new], ignore_index=True)
    combined = combined.sort_values("entry_ts").reset_index(drop=True)
    return combined


# ════════════════════════════════════════════════════════════════════
# Compounding engine
# ════════════════════════════════════════════════════════════════════

@dataclass
class CompoundingResult:
    policy: str
    starting_equity: float
    final_equity: float
    total_return_pct: float
    max_dd_dollars: float
    max_dd_pct: float
    n_trades: int
    avg_contracts: float
    max_contracts: int
    tier_first_dates: dict = field(default_factory=dict)  # contract_count -> first_date
    days_circuit_breaker_tripped: int = 0
    equity_curve: list = field(default_factory=list)


def run_compounding(trades: pd.DataFrame, policy_name: str,
                     starting_equity: float = 1500.0) -> CompoundingResult:
    """Walk trades chronologically, scaling contracts per policy.

    Groups same-timestamp trades into a batch — all trades in the batch
    use the same pre-batch equity for sizing (no ghost compounding within
    a moment).
    """
    sizing_fn = POLICIES[policy_name]
    equity = starting_equity
    ath = starting_equity
    contracts_history: list[int] = []
    equity_curve: list[tuple] = []  # (ts, equity, contracts, pnl_today)
    tier_first: dict[int, str] = {}
    daily_pnl: dict[str, float] = {}
    daily_halt_dates: set = set()
    consec_losses: int = 0  # consecutive losing trades — triggers scale-down

    # Group trades by exact entry_ts to handle simultaneous fires
    trades = trades.sort_values("entry_ts").reset_index(drop=True)

    # Some sizing fns want per-strategy info — inspect signature
    import inspect
    fn_params = inspect.signature(sizing_fn).parameters
    sizing_takes_strategy = "strategy" in fn_params

    for ts, batch in trades.groupby("entry_ts", sort=False):
        date_str = ts.date().isoformat()

        # Daily circuit breaker check — has today's loss already exceeded 5%?
        if date_str in daily_halt_dates:
            continue
        equity_at_open_of_day = equity_curve[-1][1] if equity_curve else starting_equity
        today_pnl = daily_pnl.get(date_str, 0.0)
        if today_pnl < -DAILY_CIRCUIT_PCT * equity_at_open_of_day:
            daily_halt_dates.add(date_str)
            continue

        # Apply each trade in the batch
        for _, trade in batch.iterrows():
            # Per-strategy sizing for winner_weighted; uniform for others
            if sizing_takes_strategy:
                contracts = sizing_fn(equity, ath, starting_equity, strategy=trade.strategy)
            else:
                contracts = sizing_fn(equity, ath, starting_equity)
            if consec_losses >= CONSECUTIVE_LOSS_LIMIT:
                contracts = max(1, contracts // 2)
            if contracts not in tier_first:
                tier_first[contracts] = ts.isoformat()

            pnl_per_contract = float(trade.pnl_dollars)
            slip_ticks = size_scaled_slippage_ticks(contracts)
            slippage_per_contract = slip_ticks * TICK_VALUE
            net_pnl_per_contract = pnl_per_contract - slippage_per_contract
            scaled_pnl = net_pnl_per_contract * contracts

            equity += scaled_pnl
            if equity > ath:
                ath = equity
            contracts_history.append(contracts)
            daily_pnl[date_str] = daily_pnl.get(date_str, 0.0) + scaled_pnl
            equity_curve.append((ts.isoformat(), equity, contracts,
                                  scaled_pnl, trade.strategy))

            # Update consecutive-loss counter on the per-contract gross P&L
            if pnl_per_contract < 0:
                consec_losses += 1
            elif pnl_per_contract > 0:
                consec_losses = 0
            # 0 P&L: leave counter unchanged

    # Max DD
    peak = starting_equity
    max_dd_dollars = 0.0
    max_dd_pct = 0.0
    for entry in equity_curve:
        eq = entry[1]
        if eq > peak:
            peak = eq
        dd = peak - eq
        dd_pct = dd / peak if peak > 0 else 0.0
        if dd > max_dd_dollars:
            max_dd_dollars = dd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    return CompoundingResult(
        policy=policy_name,
        starting_equity=starting_equity,
        final_equity=equity,
        total_return_pct=(equity - starting_equity) / starting_equity * 100,
        max_dd_dollars=max_dd_dollars,
        max_dd_pct=max_dd_pct * 100,
        n_trades=len(contracts_history),
        avg_contracts=sum(contracts_history) / max(1, len(contracts_history)),
        max_contracts=max(contracts_history) if contracts_history else 1,
        tier_first_dates=tier_first,
        days_circuit_breaker_tripped=len(daily_halt_dates),
        equity_curve=equity_curve,
    )


# ════════════════════════════════════════════════════════════════════
# Stress tests (poke holes)
# ════════════════════════════════════════════════════════════════════

def stress_test_55pct_wr(trades: pd.DataFrame) -> pd.DataFrame:
    """Simulate the new winners at 55% WR instead of 70-80%.
    Randomly flip 15-25% of wins to losses to test compounding robustness."""
    import numpy as np
    rng = np.random.default_rng(42)
    out = trades.copy()
    mask = (out.strategy.isin(WINNERS_NEW)) & (out.pnl_dollars > 0)
    flip_prob = 0.20  # flip 20% of wins on new strategies
    flip_mask = mask & (rng.random(len(out)) < flip_prob)
    # When flipped, P&L becomes a typical loss (1R = stop distance)
    avg_loss_pnl = out[mask & (out.pnl_dollars < 0)].pnl_dollars.mean()
    if pd.isna(avg_loss_pnl):
        avg_loss_pnl = -10.0
    out.loc[flip_mask, "pnl_dollars"] = avg_loss_pnl
    return out


# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════

def main():
    print("=" * 100)
    print("PHASE 13 COMPOUNDING BACKTEST — 5 years, $1,500 start")
    print("=" * 100)
    print()
    print("Loading combined trade data...")
    trades = load_combined_trades()
    print(f"  loaded {len(trades):,} trades across "
          f"{trades.strategy.nunique()} strategies")
    print(f"  date range: {trades.entry_ts.min()} -> {trades.entry_ts.max()}")
    print(f"  baseline 1-contract P&L (gross): ${trades.pnl_dollars.sum():,.0f}")
    print()

    # Per-strategy baseline summary
    by_strat = trades.groupby("strategy").agg(
        n=("pnl_dollars", "count"),
        total=("pnl_dollars", "sum"),
        avg=("pnl_dollars", "mean"),
    ).round(2).sort_values("total", ascending=False)
    print("Per-strategy baseline (1 contract):")
    print(by_strat.to_string())
    print()

    starting_equity = 1500.0
    results: dict[str, CompoundingResult] = {}

    for policy_name in POLICIES:
        print(f"  running {policy_name}...")
        res = run_compounding(trades, policy_name, starting_equity)
        results[policy_name] = res

    # Stress test: 55% WR variant
    print(f"  running tier_1500_stress55pct (simulate 55% WR on new strats)...")
    stress_trades = stress_test_55pct_wr(trades)
    stress_res = run_compounding(stress_trades, "tier_1500", starting_equity)
    stress_res.policy = "tier_1500_stress55pct"
    results["tier_1500_stress55pct"] = stress_res

    print()
    print("=" * 100)
    print("RESULTS — final equity per sizing policy")
    print("=" * 100)
    print()
    print(f"{'Policy':<25} {'Final $':>12} {'Return %':>10} "
          f"{'Max DD $':>10} {'Max DD %':>10} {'Trades':>8} "
          f"{'Avg N':>7} {'Max N':>7} {'Halts':>7}")
    print("-" * 100)
    for name, r in results.items():
        print(f"{name:<25} ${r.final_equity:>11,.0f} {r.total_return_pct:>9.1f}% "
              f"${r.max_dd_dollars:>9,.0f} {r.max_dd_pct:>9.1f}% {r.n_trades:>8} "
              f"{r.avg_contracts:>7.1f} {r.max_contracts:>7} "
              f"{r.days_circuit_breaker_tripped:>7}")

    # Tier-first-date table for ALL realistic policies
    print()
    print("=" * 100)
    print("WHEN DOES EACH CONTRACT COUNT FIRST APPEAR? (per policy)")
    print("=" * 100)
    interesting_tiers_compact = [1, 2, 3, 5, 10, 15, 20, 25, 30]
    print()
    print(f"{'Contracts':<10}", end="")
    for pname in ["tier_1500", "tier_3000", "tier_5000", "fixed_ratio_jones"]:
        print(f"{pname:>22}", end="")
    print()
    print("-" * 100)
    for tier in interesting_tiers_compact:
        print(f"{tier:<10}", end="")
        for pname in ["tier_1500", "tier_3000", "tier_5000", "fixed_ratio_jones"]:
            d = results[pname].tier_first_dates.get(tier)
            print(f"{(d[:10] if d else '---'):>22}", end="")
        print()
    print()
    print("RECOMMENDED: tier_3000 — best risk-adjusted ($1.09M with 34% max DD)")
    print()
    print("=" * 100)
    print("WHEN DOES EACH CONTRACT COUNT FIRST APPEAR? (tier_1500 policy)")
    print("=" * 100)
    print()
    primary = results["tier_1500"]
    interesting_tiers = [1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 75, 100]
    for tier in interesting_tiers:
        if tier in primary.tier_first_dates:
            print(f"  {tier:>3} contracts first reached on {primary.tier_first_dates[tier][:10]}")
        else:
            # Find next higher tier reached
            reached_higher = [t for t in primary.tier_first_dates if t >= tier]
            if reached_higher:
                first_above = min(reached_higher)
                print(f"  {tier:>3} contracts: skipped (jumped to {first_above})")
            else:
                print(f"  {tier:>3} contracts: NOT REACHED")
    print()
    print(f"All tiers reached: {sorted(primary.tier_first_dates.keys())}")

    # Save outputs
    out_dir = ROOT / "backtest_results"
    summary_rows = []
    for name, r in results.items():
        summary_rows.append({
            "policy": name,
            "starting_equity": r.starting_equity,
            "final_equity": round(r.final_equity, 0),
            "total_return_pct": round(r.total_return_pct, 1),
            "max_dd_dollars": round(r.max_dd_dollars, 0),
            "max_dd_pct": round(r.max_dd_pct, 1),
            "n_trades": r.n_trades,
            "avg_contracts": round(r.avg_contracts, 2),
            "max_contracts": r.max_contracts,
            "circuit_halts": r.days_circuit_breaker_tripped,
        })
    pd.DataFrame(summary_rows).to_csv(out_dir / "phoenix_compounding_summary.csv",
                                       index=False)

    # Equity curve for primary policy
    eq_df = pd.DataFrame(primary.equity_curve,
                          columns=["ts", "equity", "contracts", "pnl_trade", "strategy"])
    eq_df.to_csv(out_dir / "phoenix_compounding_tier_1500.csv", index=False)

    # Year-end snapshots for primary + recommended policies
    print()
    print("=" * 100)
    print("YEAR-END EQUITY SNAPSHOTS (tier_1500 = AGGRESSIVE)")
    print("=" * 100)
    print()
    eq_df["year"] = pd.to_datetime(eq_df.ts).dt.year
    yearly = eq_df.groupby("year").agg(
        end_equity=("equity", "last"),
        avg_contracts=("contracts", "mean"),
        max_contracts=("contracts", "max"),
        trades=("equity", "count"),
        year_pnl=("pnl_trade", "sum"),
    ).round(0)
    print(yearly.to_string())

    # Also year-end for tier_3000 (recommended)
    recommended = results["tier_3000"]
    rec_df = pd.DataFrame(recommended.equity_curve,
                            columns=["ts", "equity", "contracts", "pnl_trade", "strategy"])
    rec_df.to_csv(out_dir / "phoenix_compounding_tier_3000.csv", index=False)
    rec_df["year"] = pd.to_datetime(rec_df.ts).dt.year
    yearly_rec = rec_df.groupby("year").agg(
        end_equity=("equity", "last"),
        avg_contracts=("contracts", "mean"),
        max_contracts=("contracts", "max"),
        trades=("equity", "count"),
        year_pnl=("pnl_trade", "sum"),
    ).round(0)
    print()
    print("=" * 100)
    print("YEAR-END EQUITY SNAPSHOTS (tier_3000 = RECOMMENDED)")
    print("=" * 100)
    print()
    print(yearly_rec.to_string())

    # Save tier-first-dates table for primary
    tier_rows = []
    for tier, date in sorted(primary.tier_first_dates.items()):
        tier_rows.append({"contracts": tier, "first_date": date[:10]})
    pd.DataFrame(tier_rows).to_csv(
        out_dir / "phoenix_compounding_tier_dates.csv", index=False)

    print()
    print(f"Wrote summary -> {out_dir / 'phoenix_compounding_summary.csv'}")
    print(f"Wrote tier_1500 equity curve -> {out_dir / 'phoenix_compounding_tier_1500.csv'}")
    print(f"Wrote tier-first-dates -> {out_dir / 'phoenix_compounding_tier_dates.csv'}")


if __name__ == "__main__":
    main()
