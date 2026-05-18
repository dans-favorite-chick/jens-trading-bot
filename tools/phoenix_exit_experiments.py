"""
Phoenix Exit Methodology Experiments — Phase 13
================================================

Takes the entries from a phoenix_real_backtest run (entry_ts + direction
per trade) and re-walks the 1m bars with 8 different exit policies to
find which extracts the most P&L from the same entry signals.

The MFE/MAE analysis from STRATEGY_DEEP_DIVE_2026-05-18.md showed that
Phoenix's strategies correctly identify directional moves (MFE/MAE > 1.1
for 7 of 9 strategies) but capture only 0.1%-13% of the available move.
This script tests whether different exit methods can recover that lost
P&L using the SAME entries.

Exit policies tested:
  1. baseline         — strategy's actual exit (from input CSV)
  2. fixed_2x_target  — same stop, target = 2x current target distance
  3. trail_atr_1x     — trailing stop at 1x ATR(14) from high-water,
                         activates after 1R favorable
  4. trail_atr_3x     — chandelier (3x ATR from high-water)
  5. scale_out_1r     — exit 50% at 1R, 50% runs with BE stop
  6. time_15min       — hold up to 15 min, then close at market
  7. time_30min       — hold up to 30 min, then close at market
  8. mfe_oracle_75    — exit at 75% of trade's MFE (peek-ahead UPPER BOUND;
                         not a realistic strategy — sanity check on the
                         theoretical ceiling)

USAGE
-----
    python tools/phoenix_exit_experiments.py \\
        --trades backtest_results/phoenix_real_2025.csv \\
        --strategies vwap_pullback_v2,es_nq_confluence,spring_setup \\
        --out backtest_results/phoenix_exit_experiments.csv

Output:
  - Per-strategy × per-exit-policy: total P&L, win rate, avg/trade, max DD
  - Recommendation: which exit policy maximizes P&L for each strategy
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("exit_experiments")

TICK_SIZE = 0.25
TICK_VALUE = 0.50


# ════════════════════════════════════════════════════════════════════
# Section 1: Per-trade ATR computation
# ════════════════════════════════════════════════════════════════════

def compute_atr_at(mnq_1m_indexed: pd.DataFrame, entry_ts, period: int = 14) -> float:
    """Compute Wilder ATR over the `period` 1m bars ENDING at entry_ts.

    Returns ATR in price points. If insufficient history, returns 0.
    """
    history = mnq_1m_indexed[mnq_1m_indexed.index < entry_ts].tail(period + 1)
    if len(history) < period + 1:
        return 0.0
    trs = []
    closes = history['close'].values
    highs = history['high'].values
    lows = history['low'].values
    for i in range(1, len(history)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    # Simple average (close enough to Wilder for this purpose)
    return float(sum(trs) / len(trs))


# ════════════════════════════════════════════════════════════════════
# Section 2: Exit policies
# ════════════════════════════════════════════════════════════════════

@dataclass
class ExitResult:
    """Outcome of applying one exit policy to one trade."""
    exit_ts: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_ticks: float = 0.0
    pnl_dollars: float = 0.0
    hold_min: float = 0.0


def _close_trade(direction: str, entry_price: float, exit_price: float,
                  contracts_factor: float = 1.0) -> tuple[float, float]:
    """Return (pnl_ticks, pnl_dollars). contracts_factor allows for
    partial fills (e.g., scale_out_1r uses 0.5 on the first leg)."""
    if direction == "LONG":
        ticks = (exit_price - entry_price) / TICK_SIZE
    else:
        ticks = (entry_price - exit_price) / TICK_SIZE
    return ticks * contracts_factor, ticks * contracts_factor * TICK_VALUE


def policy_fixed_target(direction: str, entry_price: float, stop_price: float,
                         target_price: float, forward_bars: pd.DataFrame,
                         entry_ts) -> ExitResult:
    """Walk forward until stop or target hits. Conservative stop-first
    on same-bar."""
    res = ExitResult()
    for row in forward_bars.itertuples(index=True):
        if direction == "LONG":
            if row.low <= stop_price:
                res.exit_price, res.exit_reason = stop_price, "stop"
                res.exit_ts = row.Index
                break
            if row.high >= target_price:
                res.exit_price, res.exit_reason = target_price, "target"
                res.exit_ts = row.Index
                break
        else:
            if row.high >= stop_price:
                res.exit_price, res.exit_reason = stop_price, "stop"
                res.exit_ts = row.Index
                break
            if row.low <= target_price:
                res.exit_price, res.exit_reason = target_price, "target"
                res.exit_ts = row.Index
                break
    else:
        if not forward_bars.empty:
            last = forward_bars.iloc[-1]
            res.exit_price, res.exit_reason = last.close, "time_exit"
            res.exit_ts = forward_bars.index[-1]
    if res.exit_ts is not None:
        res.pnl_ticks, res.pnl_dollars = _close_trade(direction, entry_price, res.exit_price)
        res.hold_min = (res.exit_ts - entry_ts).total_seconds() / 60.0
    return res


def policy_trail_atr(direction: str, entry_price: float, stop_price: float,
                      target_price: float, atr: float, atr_mult: float,
                      forward_bars: pd.DataFrame, entry_ts,
                      activate_at_r: float = 1.0) -> ExitResult:
    """Trailing stop at atr_mult × ATR from high-water mark.
    Activates only after price reaches activate_at_r times the initial risk.
    Until activation, behaves like fixed stop/target.

    For LONG: trail = high_water - atr_mult * atr; raise stop to max(stop, trail)
              when high_water - entry >= activate_at_r * (entry - initial_stop)
    For SHORT: mirror.
    """
    res = ExitResult()
    initial_risk = abs(entry_price - stop_price)
    activation_threshold = activate_at_r * initial_risk
    current_stop = stop_price
    high_water = entry_price  # for LONG = highest high; for SHORT = lowest low
    activated = False
    for row in forward_bars.itertuples(index=True):
        if direction == "LONG":
            high_water = max(high_water, row.high)
            if not activated and (high_water - entry_price) >= activation_threshold:
                activated = True
            if activated:
                trail = high_water - atr_mult * atr
                current_stop = max(current_stop, trail)
            # Check stop FIRST (conservative)
            if row.low <= current_stop:
                res.exit_price, res.exit_reason = current_stop, "trail" if activated else "stop"
                res.exit_ts = row.Index
                break
            # Hard target (still respected)
            if row.high >= target_price:
                res.exit_price, res.exit_reason = target_price, "target"
                res.exit_ts = row.Index
                break
        else:  # SHORT
            high_water = min(high_water, row.low)
            if not activated and (entry_price - high_water) >= activation_threshold:
                activated = True
            if activated:
                trail = high_water + atr_mult * atr
                current_stop = min(current_stop, trail)
            if row.high >= current_stop:
                res.exit_price, res.exit_reason = current_stop, "trail" if activated else "stop"
                res.exit_ts = row.Index
                break
            if row.low <= target_price:
                res.exit_price, res.exit_reason = target_price, "target"
                res.exit_ts = row.Index
                break
    else:
        if not forward_bars.empty:
            last = forward_bars.iloc[-1]
            res.exit_price, res.exit_reason = last.close, "time_exit"
            res.exit_ts = forward_bars.index[-1]
    if res.exit_ts is not None:
        res.pnl_ticks, res.pnl_dollars = _close_trade(direction, entry_price, res.exit_price)
        res.hold_min = (res.exit_ts - entry_ts).total_seconds() / 60.0
    return res


def policy_scale_out_1r(direction: str, entry_price: float, stop_price: float,
                         target_price: float, forward_bars: pd.DataFrame,
                         entry_ts) -> ExitResult:
    """Exit 50% at 1R favorable; remaining 50% has BE stop, runs to
    target (or time exit). Combined P&L = 0.5 × first_leg + 0.5 × runner_leg.

    Encodes the rule: take some profit, ride a runner with no-loss
    protection. Common pro pattern.
    """
    res = ExitResult()
    initial_risk = abs(entry_price - stop_price)
    first_leg_target = (entry_price + initial_risk if direction == "LONG"
                         else entry_price - initial_risk)
    first_leg_done = False
    first_leg_pnl_ticks = 0.0
    current_stop = stop_price  # raises to entry after first leg
    second_leg_pnl_ticks = 0.0
    second_leg_done = False

    for row in forward_bars.itertuples(index=True):
        if not first_leg_done:
            if direction == "LONG":
                if row.low <= current_stop:
                    # Full stop on both legs
                    first_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                           current_stop, contracts_factor=0.5)
                    second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                            current_stop, contracts_factor=0.5)
                    res.exit_price, res.exit_reason = current_stop, "stop_both"
                    res.exit_ts = row.Index
                    second_leg_done = True
                    break
                if row.high >= first_leg_target:
                    first_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                           first_leg_target, contracts_factor=0.5)
                    first_leg_done = True
                    current_stop = entry_price  # break-even on runner
            else:  # SHORT
                if row.high >= current_stop:
                    first_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                           current_stop, contracts_factor=0.5)
                    second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                            current_stop, contracts_factor=0.5)
                    res.exit_price, res.exit_reason = current_stop, "stop_both"
                    res.exit_ts = row.Index
                    second_leg_done = True
                    break
                if row.low <= first_leg_target:
                    first_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                           first_leg_target, contracts_factor=0.5)
                    first_leg_done = True
                    current_stop = entry_price
        else:
            # Second leg: BE stop, runs to target
            if direction == "LONG":
                if row.low <= current_stop:
                    second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                            current_stop, contracts_factor=0.5)
                    res.exit_price, res.exit_reason = current_stop, "scale_runner_be"
                    res.exit_ts = row.Index
                    second_leg_done = True
                    break
                if row.high >= target_price:
                    second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                            target_price, contracts_factor=0.5)
                    res.exit_price, res.exit_reason = target_price, "scale_runner_target"
                    res.exit_ts = row.Index
                    second_leg_done = True
                    break
            else:
                if row.high >= current_stop:
                    second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                            current_stop, contracts_factor=0.5)
                    res.exit_price, res.exit_reason = current_stop, "scale_runner_be"
                    res.exit_ts = row.Index
                    second_leg_done = True
                    break
                if row.low <= target_price:
                    second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                            target_price, contracts_factor=0.5)
                    res.exit_price, res.exit_reason = target_price, "scale_runner_target"
                    res.exit_ts = row.Index
                    second_leg_done = True
                    break
    if not second_leg_done and first_leg_done and not forward_bars.empty:
        # Second leg time-exit
        last = forward_bars.iloc[-1]
        second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                 last.close, contracts_factor=0.5)
        res.exit_price, res.exit_reason = last.close, "scale_runner_time"
        res.exit_ts = forward_bars.index[-1]
    elif not first_leg_done and not forward_bars.empty:
        # Time-exited before first leg
        last = forward_bars.iloc[-1]
        first_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                last.close, contracts_factor=0.5)
        second_leg_pnl_ticks, _ = _close_trade(direction, entry_price,
                                                 last.close, contracts_factor=0.5)
        res.exit_price, res.exit_reason = last.close, "scale_time_both"
        res.exit_ts = forward_bars.index[-1]
    res.pnl_ticks = first_leg_pnl_ticks + second_leg_pnl_ticks
    res.pnl_dollars = res.pnl_ticks * TICK_VALUE
    if res.exit_ts is not None:
        res.hold_min = (res.exit_ts - entry_ts).total_seconds() / 60.0
    return res


def policy_time_only(direction: str, entry_price: float, stop_price: float,
                      forward_bars: pd.DataFrame, entry_ts,
                      hold_minutes: int) -> ExitResult:
    """Exit at market after N minutes (stop still respected so we don't
    sit through a disaster, but no fixed target). Useful for capturing
    fast moves without target ceiling."""
    res = ExitResult()
    deadline = entry_ts + pd.Timedelta(minutes=hold_minutes)
    for row in forward_bars.itertuples(index=True):
        if row.Index > deadline:
            res.exit_price, res.exit_reason = row.open, "time_exit"
            res.exit_ts = row.Index
            break
        if direction == "LONG":
            if row.low <= stop_price:
                res.exit_price, res.exit_reason = stop_price, "stop"
                res.exit_ts = row.Index
                break
        else:
            if row.high >= stop_price:
                res.exit_price, res.exit_reason = stop_price, "stop"
                res.exit_ts = row.Index
                break
    else:
        if not forward_bars.empty:
            last = forward_bars.iloc[-1]
            res.exit_price, res.exit_reason = last.close, "time_exit_end"
            res.exit_ts = forward_bars.index[-1]
    if res.exit_ts is not None:
        res.pnl_ticks, res.pnl_dollars = _close_trade(direction, entry_price, res.exit_price)
        res.hold_min = (res.exit_ts - entry_ts).total_seconds() / 60.0
    return res


def policy_mfe_oracle(direction: str, entry_price: float, stop_price: float,
                       forward_bars: pd.DataFrame, entry_ts,
                       mfe_fraction: float = 0.75) -> ExitResult:
    """ORACLE policy: peek ahead to find the trade's MFE, then exit at
    `mfe_fraction` of the favorable move. NOT a realistic strategy — this
    is the theoretical UPPER BOUND on what perfect timing could achieve."""
    res = ExitResult()
    if forward_bars.empty:
        return res
    if direction == "LONG":
        max_high = forward_bars.high.max()
        mfe = max_high - entry_price
        # Stop still respected pre-MFE
        target_oracle = entry_price + mfe_fraction * mfe
        for row in forward_bars.itertuples(index=True):
            if row.low <= stop_price:
                res.exit_price, res.exit_reason = stop_price, "stop"
                res.exit_ts = row.Index
                break
            if row.high >= target_oracle:
                res.exit_price, res.exit_reason = target_oracle, "oracle"
                res.exit_ts = row.Index
                break
    else:
        min_low = forward_bars.low.min()
        mfe = entry_price - min_low
        target_oracle = entry_price - mfe_fraction * mfe
        for row in forward_bars.itertuples(index=True):
            if row.high >= stop_price:
                res.exit_price, res.exit_reason = stop_price, "stop"
                res.exit_ts = row.Index
                break
            if row.low <= target_oracle:
                res.exit_price, res.exit_reason = target_oracle, "oracle"
                res.exit_ts = row.Index
                break
    else_block_executed = res.exit_ts is None
    if else_block_executed and not forward_bars.empty:
        last = forward_bars.iloc[-1]
        res.exit_price, res.exit_reason = last.close, "time_exit"
        res.exit_ts = forward_bars.index[-1]
    if res.exit_ts is not None:
        res.pnl_ticks, res.pnl_dollars = _close_trade(direction, entry_price, res.exit_price)
        res.hold_min = (res.exit_ts - entry_ts).total_seconds() / 60.0
    return res


# ════════════════════════════════════════════════════════════════════
# Section 3: Runner — apply each policy to each trade
# ════════════════════════════════════════════════════════════════════

def run_experiments(trades_df: pd.DataFrame, mnq_1m_df: pd.DataFrame,
                     max_hold_min: int = 240) -> pd.DataFrame:
    """For each trade in trades_df, apply each exit policy. Returns a
    long-format DataFrame: (strategy, trade_id, policy, pnl_dollars, ...).
    """
    mnq_indexed = mnq_1m_df.set_index('ts').sort_index()
    results = []

    total = len(trades_df)
    for i, row in enumerate(trades_df.itertuples(index=False), 1):
        if i % 500 == 0:
            logger.info(f"[Experiments] processed {i:,}/{total:,} trades")
        # Forward bar window
        max_ts = row.entry_ts + pd.Timedelta(minutes=max_hold_min)
        forward = mnq_indexed[(mnq_indexed.index > row.entry_ts) &
                                (mnq_indexed.index <= max_ts)]
        if forward.empty:
            continue
        entry_price = row.entry_price
        stop_price = row.stop_price
        target_price = row.target_price
        direction = row.direction
        atr = compute_atr_at(mnq_indexed, row.entry_ts, period=14)
        if atr <= 0:
            atr = abs(entry_price - stop_price)  # fallback

        # 1. Baseline (replicate the input — uses stop/target from CSV)
        base = policy_fixed_target(direction, entry_price, stop_price,
                                     target_price, forward, row.entry_ts)
        # 2. Fixed 2x target (same stop, target distance 2x)
        if direction == "LONG":
            wider_target = entry_price + 2 * (target_price - entry_price)
        else:
            wider_target = entry_price - 2 * (entry_price - target_price)
        wider = policy_fixed_target(direction, entry_price, stop_price,
                                      wider_target, forward, row.entry_ts)
        # 3. Trail ATR 1x (activates at 1R)
        trail1 = policy_trail_atr(direction, entry_price, stop_price,
                                    target_price, atr, 1.0, forward, row.entry_ts)
        # 4. Trail ATR 3x (chandelier)
        trail3 = policy_trail_atr(direction, entry_price, stop_price,
                                    target_price, atr, 3.0, forward, row.entry_ts)
        # 5. Scale out 1R
        scale = policy_scale_out_1r(direction, entry_price, stop_price,
                                      target_price, forward, row.entry_ts)
        # 6 & 7. Time exits
        t15 = policy_time_only(direction, entry_price, stop_price, forward,
                                row.entry_ts, 15)
        t30 = policy_time_only(direction, entry_price, stop_price, forward,
                                row.entry_ts, 30)
        # 8. MFE oracle 75% (upper bound)
        oracle = policy_mfe_oracle(direction, entry_price, stop_price, forward,
                                     row.entry_ts, 0.75)

        for policy_name, r in [
            ("baseline", base), ("fixed_2x_target", wider),
            ("trail_atr_1x", trail1), ("trail_atr_3x", trail3),
            ("scale_out_1r", scale), ("time_15min", t15),
            ("time_30min", t30), ("mfe_oracle_75", oracle),
        ]:
            results.append({
                "strategy": row.strategy,
                "direction": direction,
                "entry_ts": row.entry_ts,
                "entry_price": entry_price,
                "policy": policy_name,
                "exit_reason": r.exit_reason,
                "pnl_ticks": r.pnl_ticks,
                "pnl_dollars": r.pnl_dollars,
                "hold_min": r.hold_min,
            })
    return pd.DataFrame(results)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot: per-strategy × per-policy total P&L, WR, avg, max DD."""
    if df.empty:
        return pd.DataFrame()
    summary = df.groupby(['strategy', 'policy']).agg(
        n=('pnl_dollars', 'count'),
        total=('pnl_dollars', 'sum'),
        avg=('pnl_dollars', 'mean'),
        wins=('pnl_dollars', lambda s: (s > 0).sum()),
        avg_hold=('hold_min', 'mean'),
    ).round(2)
    summary['wr%'] = (summary.wins / summary.n * 100).round(1)
    # Max drawdown per (strategy, policy) requires the cumulative P&L series
    dd_rows = []
    for (strat, pol), g in df.sort_values('entry_ts').groupby(['strategy', 'policy']):
        cum = g['pnl_dollars'].cumsum()
        max_dd = (cum.cummax() - cum).max()
        dd_rows.append({'strategy': strat, 'policy': pol, 'max_dd': max_dd})
    dd_df = pd.DataFrame(dd_rows).set_index(['strategy', 'policy'])
    summary = summary.join(dd_df).round(2)
    return summary[['n', 'wr%', 'total', 'avg', 'avg_hold', 'max_dd']]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True,
                     help="Path to phoenix_real_backtest output CSV")
    ap.add_argument("--mnq", default="data/historical/mnq_1min_databento.csv")
    ap.add_argument("--strategies",
                     default="vwap_pullback_v2,es_nq_confluence,spring_setup,vwap_band_pullback,bias_momentum,ib_breakout",
                     help="Comma-separated; restrict experiments to these")
    ap.add_argument("--out", default="backtest_results/phoenix_exit_experiments.csv")
    ap.add_argument("--summary-out",
                     default="backtest_results/phoenix_exit_summary.csv")
    args = ap.parse_args()

    logger.info(f"[main] loading trades from {args.trades}")
    trades = pd.read_csv(args.trades, parse_dates=['entry_ts', 'exit_ts'])
    keep = [s.strip() for s in args.strategies.split(',') if s.strip()]
    if keep:
        trades = trades[trades.strategy.isin(keep)].copy()
    logger.info(f"[main] {len(trades):,} trades to experiment on "
                f"(strategies: {sorted(trades.strategy.unique())})")

    logger.info(f"[main] loading MNQ 1m bars from {args.mnq}")
    mnq = pd.read_csv(args.mnq, parse_dates=['ts_utc'])
    mnq = mnq.rename(columns={'ts_utc': 'ts'})
    logger.info(f"[main] {len(mnq):,} 1m bars loaded")

    logger.info(f"[main] running 8 exit policies on each trade...")
    results = run_experiments(trades, mnq)
    out_path = ROOT / args.out
    out_path.parent.mkdir(exist_ok=True)
    results.to_csv(out_path, index=False)
    logger.info(f"[main] wrote {len(results):,} (trade × policy) rows to {out_path}")

    summary = summarize(results)
    sum_path = ROOT / args.summary_out
    summary.to_csv(sum_path)
    logger.info(f"[main] wrote summary to {sum_path}")

    print()
    print("=" * 110)
    print("EXIT POLICY COMPARISON — per strategy × per policy")
    print("=" * 110)
    print()
    print(summary.to_string())
    print()
    print("Best policy per strategy (by total P&L):")
    print()
    for strat in summary.index.get_level_values('strategy').unique():
        s = summary.loc[strat].sort_values('total', ascending=False)
        baseline_total = s.loc['baseline', 'total'] if 'baseline' in s.index else 0
        best = s.iloc[0]
        best_name = s.index[0]
        lift = best['total'] - baseline_total
        lift_pct = (lift / abs(baseline_total) * 100) if baseline_total != 0 else float('inf')
        print(f"  {strat:25s} -> {best_name:20s} ${best['total']:+.0f} "
              f"(baseline ${baseline_total:+.0f}, lift ${lift:+.0f} = "
              f"{lift_pct:+.0f}%)")


if __name__ == "__main__":
    main()
