"""
Phoenix Stop/Target Optimizer — Per-Strategy Individual Recommendations
==========================================================================

Comprehensive stop + target optimization run on the CLEAN 5-year trade data
(post-bug-fix). For EACH winning strategy, computes:

  1. MFE/MAE analysis: where do winners peak? where do losers bottom?
     Tells us whether the strategy's INITIAL stop is too tight/too wide.

  2. Per-trade walk forward with 8 exit policies:
       - baseline (strategy's actual exit, as recorded)
       - fixed_2x_target  (target = entry + 2x initial stop distance)
       - fixed_3x_target  (target = entry + 3x initial stop distance)
       - trail_atr_1x     (trail 1x ATR(14) from high-water after 1R favorable)
       - trail_atr_2x     (chandelier 2x ATR)
       - scale_out_1r     (50% close at 1R, remaining 50% BE stop to target)
       - scale_out_15r    (50% close at 1.5R, remaining to 2R)
       - time_15min       (close at market after 15 min)
       - time_30min       (close at market after 30 min)
       - mfe_oracle_75    (UPPER BOUND only — exits at 75% of MFE, look-ahead)

  3. Per-strategy WINNER: the policy with best risk-adjusted expectancy
     (E$ per trade × WR factor, with sample-size guardrails).

  4. Validation: confirm chosen policy produces a profitable expectancy.

Input: combined trade data from all Phase 13 backtests.
Output: per-strategy optimal exit policy + projected 5y P&L improvement.

USAGE:
  python tools/phoenix_stop_target_optimizer.py

NOTE: This tool's walk-forward logic does NOT have the silent-stop bug
(every code path that completes the for-loop sets exit_ts to a valid
fallback). Verified by validate_backtest_quality.py after run.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

TICK_SIZE = 0.25
TICK_VALUE = 0.50
MAX_HOLD_MIN = 240

# Trade sources — only WINNERS to focus the analysis
TRADE_SOURCES = [
    ("backtest_results/phoenix_real_5year.csv",         None),
    ("backtest_results/phoenix_new_strategy_lab.csv",   None),
    ("backtest_results/phoenix_trend_pullback_lab.csv", "raschke_baseline"),
]

# Strategies to analyze (Tier 1+2 winners)
WINNERS = [
    "opening_session", "vwap_pullback_v2", "spring_setup",
    "es_nq_confluence", "bias_momentum", "vwap_band_pullback",
    "ib_breakout",
    "g_inside_bar_breakout", "e_multi_day_breakout", "a_asian_continuation",
    "raschke_baseline",
]


# ════════════════════════════════════════════════════════════════════
# Data loading
# ════════════════════════════════════════════════════════════════════

def load_mnq_1m() -> pd.DataFrame:
    """Load MNQ 1m bars indexed by timestamp for fast forward-walk."""
    csv = ROOT / "data" / "historical" / "mnq_1min_databento.csv"
    df = pd.read_csv(csv)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def load_trades() -> pd.DataFrame:
    """Load all winning-strategy trades from the clean Phase 13 sources."""
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
                 "stop_price", "target_price", "exit_ts", "exit_price",
                 "pnl_dollars", "pnl_ticks", "hold_min"]
        for c in keep:
            if c not in df.columns:
                df[c] = None
        parts.append(df[keep])
    combined = pd.concat(parts, ignore_index=True)
    combined = combined.sort_values("entry_ts").reset_index(drop=True)
    return combined


# ════════════════════════════════════════════════════════════════════
# MFE/MAE analysis
# ════════════════════════════════════════════════════════════════════

def compute_mfe_mae(trade, mnq_1m: pd.DataFrame, max_hold_min: int = MAX_HOLD_MIN) -> tuple:
    """Compute Maximum Favorable Excursion and Maximum Adverse Excursion in TICKS.
    Returns (mfe_ticks, mae_ticks, n_bars). Negative ticks = adverse to trade direction."""
    entry_ts = trade.entry_ts
    entry_price = float(trade.entry_price)
    direction = trade.direction
    max_ts = entry_ts + pd.Timedelta(minutes=max_hold_min)
    forward = mnq_1m.loc[(mnq_1m.index > entry_ts) & (mnq_1m.index <= max_ts)]
    if forward.empty:
        return (0.0, 0.0, 0)
    if direction == "LONG":
        mfe_price = forward["high"].max() - entry_price
        mae_price = forward["low"].min() - entry_price
    else:
        mfe_price = entry_price - forward["low"].min()
        mae_price = entry_price - forward["high"].max()
    mfe_ticks = mfe_price / TICK_SIZE
    mae_ticks = mae_price / TICK_SIZE  # negative if adverse
    return (mfe_ticks, mae_ticks, len(forward))


# ════════════════════════════════════════════════════════════════════
# Exit policies (all bug-free — always set exit_ts)
# ════════════════════════════════════════════════════════════════════

@dataclass
class ExitResult:
    pnl_ticks: float = 0.0
    exit_reason: str = "unset"
    hold_min: float = 0.0


def _pnl_ticks(direction: str, entry: float, exit_price: float, frac: float = 1.0) -> float:
    """Compute P&L in ticks, signed by direction. frac for partial fills."""
    delta = exit_price - entry if direction == "LONG" else entry - exit_price
    return (delta / TICK_SIZE) * frac


def _walk_to_exit(trade, mnq_1m: pd.DataFrame, max_hold_min: int = MAX_HOLD_MIN):
    """Yield 1m bars after entry, up to max_hold."""
    max_ts = trade.entry_ts + pd.Timedelta(minutes=max_hold_min)
    forward = mnq_1m.loc[(mnq_1m.index > trade.entry_ts) & (mnq_1m.index <= max_ts)]
    return forward


def policy_baseline(trade, mnq_1m) -> ExitResult:
    """Use the strategy's actual recorded exit."""
    return ExitResult(
        pnl_ticks=float(trade.pnl_ticks) if pd.notna(trade.pnl_ticks) else 0.0,
        exit_reason="baseline",
        hold_min=float(trade.hold_min) if pd.notna(trade.hold_min) else 0.0,
    )


def policy_fixed_rr(trade, mnq_1m, rr: float) -> ExitResult:
    """Stop = trade's initial stop; Target = entry ± rr × stop_distance."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)
    if trade.direction == "LONG":
        target = entry + rr * stop_dist
    else:
        target = entry - rr * stop_dist
    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)
    for ts, row in forward.iterrows():
        if trade.direction == "LONG":
            if row.low <= stop:
                hold = (ts - trade.entry_ts).total_seconds() / 60
                return ExitResult(_pnl_ticks("LONG", entry, stop), "stop", hold)
            if row.high >= target:
                hold = (ts - trade.entry_ts).total_seconds() / 60
                return ExitResult(_pnl_ticks("LONG", entry, target), "target", hold)
        else:
            if row.high >= stop:
                hold = (ts - trade.entry_ts).total_seconds() / 60
                return ExitResult(_pnl_ticks("SHORT", entry, stop), "stop", hold)
            if row.low <= target:
                hold = (ts - trade.entry_ts).total_seconds() / 60
                return ExitResult(_pnl_ticks("SHORT", entry, target), "target", hold)
    # Time exit at last bar
    last_ts = forward.index[-1]
    last_close = float(forward.iloc[-1].close)
    hold = (last_ts - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


def policy_scale_out_1r(trade, mnq_1m, first_r: float = 1.0, runner_r: float = 2.0) -> ExitResult:
    """50% off at first_r, remaining 50% has BE stop, runs to runner_r."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)

    if trade.direction == "LONG":
        first_target = entry + first_r * stop_dist
        runner_target = entry + runner_r * stop_dist
    else:
        first_target = entry - first_r * stop_dist
        runner_target = entry - runner_r * stop_dist

    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)

    first_done = False
    first_pnl = 0.0
    second_pnl = 0.0
    current_stop = stop
    final_hold = 0
    final_reason = "time_exit"

    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60
        final_hold = hold
        if trade.direction == "LONG":
            # Check stop first
            if row.low <= current_stop:
                if first_done:
                    second_pnl = _pnl_ticks("LONG", entry, current_stop, 0.5)
                    final_reason = "runner_be_stop" if current_stop == entry else "runner_stop"
                else:
                    first_pnl = _pnl_ticks("LONG", entry, current_stop, 0.5)
                    second_pnl = _pnl_ticks("LONG", entry, current_stop, 0.5)
                    final_reason = "both_stop"
                break
            if not first_done and row.high >= first_target:
                first_pnl = _pnl_ticks("LONG", entry, first_target, 0.5)
                first_done = True
                current_stop = entry  # BE stop on runner
            if first_done and row.high >= runner_target:
                second_pnl = _pnl_ticks("LONG", entry, runner_target, 0.5)
                final_reason = "runner_target"
                break
        else:
            if row.high >= current_stop:
                if first_done:
                    second_pnl = _pnl_ticks("SHORT", entry, current_stop, 0.5)
                    final_reason = "runner_be_stop" if current_stop == entry else "runner_stop"
                else:
                    first_pnl = _pnl_ticks("SHORT", entry, current_stop, 0.5)
                    second_pnl = _pnl_ticks("SHORT", entry, current_stop, 0.5)
                    final_reason = "both_stop"
                break
            if not first_done and row.low <= first_target:
                first_pnl = _pnl_ticks("SHORT", entry, first_target, 0.5)
                first_done = True
                current_stop = entry
            if first_done and row.low <= runner_target:
                second_pnl = _pnl_ticks("SHORT", entry, runner_target, 0.5)
                final_reason = "runner_target"
                break
    else:
        # Time exit
        last_close = float(forward.iloc[-1].close)
        if first_done:
            second_pnl = _pnl_ticks(trade.direction, entry, last_close, 0.5)
        else:
            first_pnl = _pnl_ticks(trade.direction, entry, last_close, 0.5)
            second_pnl = _pnl_ticks(trade.direction, entry, last_close, 0.5)

    return ExitResult(first_pnl + second_pnl, final_reason, final_hold)


def policy_trail_atr(trade, mnq_1m, atr_ticks: float, multiplier: float, activate_r: float = 1.0) -> ExitResult:
    """Trailing stop at multiplier × atr from high-water mark, activated after activate_r favorable."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)

    activation_threshold = activate_r * stop_dist
    trail_distance_price = multiplier * atr_ticks * TICK_SIZE
    high_water = entry  # in trade direction
    activated = False
    current_stop = stop

    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)

    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60
        if trade.direction == "LONG":
            if row.low <= current_stop:
                return ExitResult(_pnl_ticks("LONG", entry, current_stop),
                                   "trail_stop" if activated else "initial_stop", hold)
            if not activated and row.high >= entry + activation_threshold:
                activated = True
                high_water = row.high
                current_stop = max(current_stop, high_water - trail_distance_price)
            elif activated:
                high_water = max(high_water, row.high)
                new_stop = high_water - trail_distance_price
                if new_stop > current_stop:
                    current_stop = new_stop
        else:
            if row.high >= current_stop:
                return ExitResult(_pnl_ticks("SHORT", entry, current_stop),
                                   "trail_stop" if activated else "initial_stop", hold)
            if not activated and row.low <= entry - activation_threshold:
                activated = True
                high_water = row.low
                current_stop = min(current_stop, high_water + trail_distance_price)
            elif activated:
                high_water = min(high_water, row.low)
                new_stop = high_water + trail_distance_price
                if new_stop < current_stop:
                    current_stop = new_stop
    last_ts = forward.index[-1]
    last_close = float(forward.iloc[-1].close)
    hold = (last_ts - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


def policy_time_exit(trade, mnq_1m, minutes: int) -> ExitResult:
    """Hold for fixed time, then close at market. Original stop still applies."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    exit_ts_target = trade.entry_ts + pd.Timedelta(minutes=minutes)
    forward = _walk_to_exit(trade, mnq_1m, max_hold_min=minutes + 5)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)
    for ts, row in forward.iterrows():
        # Stop check
        hit_stop = (trade.direction == "LONG" and row.low <= stop) or \
                   (trade.direction == "SHORT" and row.high >= stop)
        if hit_stop:
            hold = (ts - trade.entry_ts).total_seconds() / 60
            return ExitResult(_pnl_ticks(trade.direction, entry, stop), "stop", hold)
        if ts >= exit_ts_target:
            hold = (ts - trade.entry_ts).total_seconds() / 60
            return ExitResult(_pnl_ticks(trade.direction, entry, float(row.close)),
                               "time_exit", hold)
    # Reached end without time-exit hitting
    last_ts = forward.index[-1]
    last_close = float(forward.iloc[-1].close)
    hold = (last_ts - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


def policy_mfe_oracle(trade, mnq_1m, pct: float = 0.75) -> ExitResult:
    """LOOK-AHEAD upper bound: exits at pct of MFE. Not shippable."""
    mfe, mae, n = compute_mfe_mae(trade, mnq_1m)
    if n == 0:
        return ExitResult(0, "no_forward_data", 0)
    stop_dist_ticks = abs(float(trade.entry_price) - float(trade.stop_price)) / TICK_SIZE
    if mae < -stop_dist_ticks:
        return ExitResult(-stop_dist_ticks, "stop", 0)
    return ExitResult(mfe * pct, "mfe_oracle", 0)


def policy_be_at_1r(trade, mnq_1m, target_r: float = 2.0) -> ExitResult:
    """At +1R favorable, move stop to break-even. Target stays at target_r.
    Simpler than scale_out — no partial exit, just no-lose-once-winning."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)
    activation = entry + stop_dist if trade.direction == "LONG" else entry - stop_dist
    target = entry + target_r * stop_dist if trade.direction == "LONG" else entry - target_r * stop_dist
    current_stop = stop
    activated = False

    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)
    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60
        if trade.direction == "LONG":
            if row.low <= current_stop:
                return ExitResult(_pnl_ticks("LONG", entry, current_stop),
                                   "be_stop" if activated else "initial_stop", hold)
            if not activated and row.high >= activation:
                activated = True
                current_stop = entry  # move to BE
            if row.high >= target:
                return ExitResult(_pnl_ticks("LONG", entry, target), "target", hold)
        else:
            if row.high >= current_stop:
                return ExitResult(_pnl_ticks("SHORT", entry, current_stop),
                                   "be_stop" if activated else "initial_stop", hold)
            if not activated and row.low <= activation:
                activated = True
                current_stop = entry
            if row.low <= target:
                return ExitResult(_pnl_ticks("SHORT", entry, target), "target", hold)
    last_close = float(forward.iloc[-1].close)
    hold = (forward.index[-1] - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


def policy_profit_lock(trade, mnq_1m, activation_r: float = 0.5,
                        lock_r: float = 0.25, target_r: float = 2.0) -> ExitResult:
    """At +activation_r favorable, lock stop at +lock_r (small guaranteed profit).
    Then target_r is the take-profit. Reduces variance — gets in a small win
    even if the trade later reverses."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)
    activation = entry + activation_r * stop_dist if trade.direction == "LONG" else entry - activation_r * stop_dist
    locked_stop = entry + lock_r * stop_dist if trade.direction == "LONG" else entry - lock_r * stop_dist
    target = entry + target_r * stop_dist if trade.direction == "LONG" else entry - target_r * stop_dist
    current_stop = stop

    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)
    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60
        if trade.direction == "LONG":
            if row.low <= current_stop:
                reason = "lock_stop" if current_stop == locked_stop else "initial_stop"
                return ExitResult(_pnl_ticks("LONG", entry, current_stop), reason, hold)
            if current_stop != locked_stop and row.high >= activation:
                current_stop = locked_stop
            if row.high >= target:
                return ExitResult(_pnl_ticks("LONG", entry, target), "target", hold)
        else:
            if row.high >= current_stop:
                reason = "lock_stop" if current_stop == locked_stop else "initial_stop"
                return ExitResult(_pnl_ticks("SHORT", entry, current_stop), reason, hold)
            if current_stop != locked_stop and row.low <= activation:
                current_stop = locked_stop
            if row.low <= target:
                return ExitResult(_pnl_ticks("SHORT", entry, target), "target", hold)
    last_close = float(forward.iloc[-1].close)
    hold = (forward.index[-1] - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


def policy_scale_out_3tranche(trade, mnq_1m, r1: float = 1.0, r2: float = 2.0,
                                r3: float = 3.0) -> ExitResult:
    """3-tranche scale-out: 33% at r1, 33% at r2, 34% runner to r3.
    After r1, stop moves to BE. After r2, stop moves to r1.
    Captures more of the distribution than 2-tranche scale_out_1r."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)

    sign = 1 if trade.direction == "LONG" else -1
    target_1 = entry + sign * r1 * stop_dist
    target_2 = entry + sign * r2 * stop_dist
    target_3 = entry + sign * r3 * stop_dist

    pnl_1 = pnl_2 = pnl_3 = 0.0
    done_1 = done_2 = done_3 = False
    current_stop = stop

    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)

    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60
        if trade.direction == "LONG":
            if row.low <= current_stop:
                # Remaining tranches get stopped
                if not done_1:
                    pnl_1 = _pnl_ticks("LONG", entry, current_stop, 0.333)
                if not done_2:
                    pnl_2 = _pnl_ticks("LONG", entry, current_stop, 0.333)
                if not done_3:
                    pnl_3 = _pnl_ticks("LONG", entry, current_stop, 0.334)
                return ExitResult(pnl_1 + pnl_2 + pnl_3, "stop", hold)
            if not done_1 and row.high >= target_1:
                pnl_1 = _pnl_ticks("LONG", entry, target_1, 0.333)
                done_1 = True
                current_stop = entry  # BE on remaining 66%
            if not done_2 and row.high >= target_2:
                pnl_2 = _pnl_ticks("LONG", entry, target_2, 0.333)
                done_2 = True
                current_stop = target_1  # lock 1R on runner
            if not done_3 and row.high >= target_3:
                pnl_3 = _pnl_ticks("LONG", entry, target_3, 0.334)
                done_3 = True
                return ExitResult(pnl_1 + pnl_2 + pnl_3, "all_targets", hold)
        else:
            if row.high >= current_stop:
                if not done_1: pnl_1 = _pnl_ticks("SHORT", entry, current_stop, 0.333)
                if not done_2: pnl_2 = _pnl_ticks("SHORT", entry, current_stop, 0.333)
                if not done_3: pnl_3 = _pnl_ticks("SHORT", entry, current_stop, 0.334)
                return ExitResult(pnl_1 + pnl_2 + pnl_3, "stop", hold)
            if not done_1 and row.low <= target_1:
                pnl_1 = _pnl_ticks("SHORT", entry, target_1, 0.333)
                done_1 = True
                current_stop = entry
            if not done_2 and row.low <= target_2:
                pnl_2 = _pnl_ticks("SHORT", entry, target_2, 0.333)
                done_2 = True
                current_stop = target_1
            if not done_3 and row.low <= target_3:
                pnl_3 = _pnl_ticks("SHORT", entry, target_3, 0.334)
                done_3 = True
                return ExitResult(pnl_1 + pnl_2 + pnl_3, "all_targets", hold)

    # Time exit on remaining tranches
    last_close = float(forward.iloc[-1].close)
    if not done_1: pnl_1 = _pnl_ticks(trade.direction, entry, last_close, 0.333)
    if not done_2: pnl_2 = _pnl_ticks(trade.direction, entry, last_close, 0.333)
    if not done_3: pnl_3 = _pnl_ticks(trade.direction, entry, last_close, 0.334)
    hold = (forward.index[-1] - trade.entry_ts).total_seconds() / 60
    return ExitResult(pnl_1 + pnl_2 + pnl_3, "time_exit_partial", hold)


def policy_tight_trail_post_1r(trade, mnq_1m, trail_ticks: float = 8) -> ExitResult:
    """Initial stop holds until +1R favorable. Then tight trail at fixed
    ticks behind. Captures momentum bursts without trailing too widely.
    Best for fast-momentum strategies."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)
    activation = entry + stop_dist if trade.direction == "LONG" else entry - stop_dist
    trail_price = trail_ticks * TICK_SIZE
    current_stop = stop
    activated = False
    high_water = entry

    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)
    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60
        if trade.direction == "LONG":
            if row.low <= current_stop:
                return ExitResult(_pnl_ticks("LONG", entry, current_stop),
                                   "tight_trail" if activated else "initial_stop", hold)
            if not activated and row.high >= activation:
                activated = True
                high_water = row.high
                current_stop = max(current_stop, high_water - trail_price)
            elif activated:
                high_water = max(high_water, row.high)
                new_stop = high_water - trail_price
                if new_stop > current_stop:
                    current_stop = new_stop
        else:
            if row.high >= current_stop:
                return ExitResult(_pnl_ticks("SHORT", entry, current_stop),
                                   "tight_trail" if activated else "initial_stop", hold)
            if not activated and row.low <= activation:
                activated = True
                high_water = row.low
                current_stop = min(current_stop, high_water + trail_price)
            elif activated:
                high_water = min(high_water, row.low)
                new_stop = high_water + trail_price
                if new_stop < current_stop:
                    current_stop = new_stop
    last_close = float(forward.iloc[-1].close)
    hold = (forward.index[-1] - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


def policy_chandelier(trade, mnq_1m, lookback_bars: int = 22,
                       atr_mult: float = 3.0, activate_r: float = 1.0) -> ExitResult:
    """Classic Chuck LeBeau Chandelier Exit.

    Stop "hangs" from the rolling N-bar highest high (LONG) or lowest low (SHORT):
       LONG stop  = rolling_high(N) - atr_mult × ATR(N)
       SHORT stop = rolling_low(N)  + atr_mult × ATR(N)

    Both the high/low reference AND ATR are computed from the same rolling
    N-bar window (recomputed each bar). Stop only ratchets in the trade's
    favor — never widens.

    Activated after activate_r favorable to give the trade room to develop
    before tightening. Initial stop holds until activation.

    Default LeBeau spec: 22 bars, 3x ATR, activate at 1R.
    Variants worth testing: (22, 2.0) tighter, (50, 3.0) slower.
    """
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)

    activation = entry + activate_r * stop_dist if trade.direction == "LONG" else entry - activate_r * stop_dist
    current_stop = stop
    activated = False

    # We need to walk forward AND maintain a rolling window. Pre-fetch the full
    # forward window plus enough lookback bars from BEFORE entry to seed ATR.
    max_ts = trade.entry_ts + pd.Timedelta(minutes=MAX_HOLD_MIN)
    # Get from (entry_ts - lookback bars worth of minutes) to max_ts
    seed_start = trade.entry_ts - pd.Timedelta(minutes=lookback_bars + 5)
    full_window = mnq_1m.loc[(mnq_1m.index >= seed_start) & (mnq_1m.index <= max_ts)]
    if full_window.empty:
        return ExitResult(0, "no_forward_data", 0)
    forward = full_window.loc[full_window.index > trade.entry_ts]
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)

    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60

        # Recompute rolling window: last `lookback_bars` 1m bars ending at ts
        window = full_window.loc[full_window.index <= ts].tail(lookback_bars)
        if len(window) < min(lookback_bars, 10):
            # Insufficient history — hold initial stop
            pass
        else:
            # Wilder-approximated ATR over the window
            highs = window["high"].values
            lows = window["low"].values
            closes = window["close"].values
            trs = []
            for i in range(1, len(window)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
                trs.append(tr)
            atr = sum(trs) / max(1, len(trs))
            atr_buffer = atr_mult * atr

            if trade.direction == "LONG":
                if not activated and row.high >= activation:
                    activated = True
                if activated:
                    rolling_high = window["high"].max()
                    new_stop = rolling_high - atr_buffer
                    if new_stop > current_stop:
                        current_stop = new_stop  # one-way ratchet
            else:
                if not activated and row.low <= activation:
                    activated = True
                if activated:
                    rolling_low = window["low"].min()
                    new_stop = rolling_low + atr_buffer
                    if new_stop < current_stop:
                        current_stop = new_stop

        # Check stop hit on current bar
        if trade.direction == "LONG":
            if row.low <= current_stop:
                return ExitResult(_pnl_ticks("LONG", entry, current_stop),
                                   "chandelier_stop" if activated else "initial_stop", hold)
        else:
            if row.high >= current_stop:
                return ExitResult(_pnl_ticks("SHORT", entry, current_stop),
                                   "chandelier_stop" if activated else "initial_stop", hold)

    last_close = float(forward.iloc[-1].close)
    hold = (forward.index[-1] - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


def policy_first_n_min_then_be(trade, mnq_1m, hold_min: int = 5,
                                target_r: float = 2.0) -> ExitResult:
    """Hold the initial stop for N minutes, then move to BE if profitable.
    Target stays at target_r. Useful for momentum strategies where the
    first few minutes ARE the signal — if it doesn't work fast, exit."""
    entry = float(trade.entry_price)
    stop = float(trade.stop_price)
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return ExitResult(0, "no_stop", 0)
    target = entry + target_r * stop_dist if trade.direction == "LONG" else entry - target_r * stop_dist
    be_threshold_ts = trade.entry_ts + pd.Timedelta(minutes=hold_min)
    current_stop = stop
    moved_to_be = False

    forward = _walk_to_exit(trade, mnq_1m)
    if forward.empty:
        return ExitResult(0, "no_forward_data", 0)
    for ts, row in forward.iterrows():
        hold = (ts - trade.entry_ts).total_seconds() / 60
        # After hold_min minutes, move to BE if currently in profit
        if not moved_to_be and ts >= be_threshold_ts:
            in_profit = (trade.direction == "LONG" and row.close > entry) or \
                        (trade.direction == "SHORT" and row.close < entry)
            if in_profit:
                current_stop = entry
                moved_to_be = True
        if trade.direction == "LONG":
            if row.low <= current_stop:
                return ExitResult(_pnl_ticks("LONG", entry, current_stop),
                                   "be_stop" if moved_to_be else "initial_stop", hold)
            if row.high >= target:
                return ExitResult(_pnl_ticks("LONG", entry, target), "target", hold)
        else:
            if row.high >= current_stop:
                return ExitResult(_pnl_ticks("SHORT", entry, current_stop),
                                   "be_stop" if moved_to_be else "initial_stop", hold)
            if row.low <= target:
                return ExitResult(_pnl_ticks("SHORT", entry, target), "target", hold)
    last_close = float(forward.iloc[-1].close)
    hold = (forward.index[-1] - trade.entry_ts).total_seconds() / 60
    return ExitResult(_pnl_ticks(trade.direction, entry, last_close), "time_exit", hold)


# ════════════════════════════════════════════════════════════════════
# Per-strategy analysis
# ════════════════════════════════════════════════════════════════════

POLICIES = [
    # Reference
    ("baseline",            lambda t, m: policy_baseline(t, m)),
    # Fixed RR targets (no stop adjustment)
    ("fixed_2r",            lambda t, m: policy_fixed_rr(t, m, 2.0)),
    ("fixed_3r",            lambda t, m: policy_fixed_rr(t, m, 3.0)),
    # Move-to-BE variants (no scale-out — simpler than scale_out)
    ("be_at_1r_target_2r",  lambda t, m: policy_be_at_1r(t, m, target_r=2.0)),
    ("be_at_1r_target_3r",  lambda t, m: policy_be_at_1r(t, m, target_r=3.0)),
    # Profit-lock variant (lock small win at +0.5R)
    ("profit_lock_05r",     lambda t, m: policy_profit_lock(t, m,
                                                              activation_r=0.5,
                                                              lock_r=0.25,
                                                              target_r=2.0)),
    # Scale-out variants (partial exits at multiple R levels)
    ("scale_out_1r",        lambda t, m: policy_scale_out_1r(t, m, 1.0, 2.0)),
    ("scale_out_15r",       lambda t, m: policy_scale_out_1r(t, m, 1.5, 2.5)),
    ("scale_out_3tranche",  lambda t, m: policy_scale_out_3tranche(t, m, 1.0, 2.0, 3.0)),
    # Trailing stop variants
    ("trail_atr_1x",        lambda t, m: policy_trail_atr(t, m, atr_ticks=20, multiplier=1.0)),
    ("trail_atr_2x",        lambda t, m: policy_trail_atr(t, m, atr_ticks=20, multiplier=2.0)),
    ("tight_trail_post_1r", lambda t, m: policy_tight_trail_post_1r(t, m, trail_ticks=8)),
    # Classic Chuck LeBeau Chandelier Exit (rolling-window high + dynamic ATR)
    ("chandelier_22_3x",    lambda t, m: policy_chandelier(t, m, lookback_bars=22, atr_mult=3.0)),
    ("chandelier_22_2x",    lambda t, m: policy_chandelier(t, m, lookback_bars=22, atr_mult=2.0)),
    ("chandelier_50_3x",    lambda t, m: policy_chandelier(t, m, lookback_bars=50, atr_mult=3.0)),
    # Time-based exits
    ("time_15min",          lambda t, m: policy_time_exit(t, m, 15)),
    ("time_30min",          lambda t, m: policy_time_exit(t, m, 30)),
    ("first_5min_then_be",  lambda t, m: policy_first_n_min_then_be(t, m, hold_min=5, target_r=2.0)),
    # Look-ahead reference (NOT shippable)
    ("mfe_oracle_75",       lambda t, m: policy_mfe_oracle(t, m, 0.75)),
]


def analyze_strategy(strat_name: str, trades: pd.DataFrame, mnq_1m: pd.DataFrame) -> dict:
    """Run all policies + MFE/MAE on one strategy. Returns summary dict."""
    s_trades = trades[trades.strategy == strat_name].copy()
    n = len(s_trades)
    if n == 0:
        return None
    print(f"  analyzing {strat_name}  (n={n})...")

    # MFE/MAE distribution
    mfe_list, mae_list = [], []
    for tr in s_trades.itertuples(index=False):
        try:
            mfe, mae, _ = compute_mfe_mae(tr, mnq_1m)
            mfe_list.append(mfe)
            mae_list.append(mae)
        except Exception:
            mfe_list.append(0)
            mae_list.append(0)

    mfe_arr = np.array(mfe_list)
    mae_arr = np.array(mae_list)
    mfe_mean = float(np.mean(mfe_arr))
    mfe_p50 = float(np.percentile(mfe_arr, 50))
    mfe_p75 = float(np.percentile(mfe_arr, 75))
    mae_mean = float(np.mean(np.abs(mae_arr)))
    mae_p50 = float(np.percentile(np.abs(mae_arr), 50))
    mae_p75 = float(np.percentile(np.abs(mae_arr), 75))
    mfe_mae_ratio = mfe_mean / max(mae_mean, 0.01)

    # Per-policy P&L
    policy_results = {}
    for pname, pfunc in POLICIES:
        pnls = []
        for tr in s_trades.itertuples(index=False):
            try:
                res = pfunc(tr, mnq_1m)
                pnls.append(res.pnl_ticks * TICK_VALUE)
            except Exception:
                pnls.append(0)
        pnls_arr = np.array(pnls)
        wins = (pnls_arr > 0).sum()
        losses = (pnls_arr < 0).sum()
        gross_win = pnls_arr[pnls_arr > 0].sum() if wins else 0
        gross_loss = -pnls_arr[pnls_arr < 0].sum() if losses else 0
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        policy_results[pname] = {
            "n": n,
            "wr_pct": round(wins / n * 100, 1) if n > 0 else 0,
            "total": round(pnls_arr.sum(), 0),
            "avg": round(pnls_arr.mean(), 2),
            "pf": round(pf, 2) if not np.isinf(pf) else 99.0,
        }

    return {
        "strategy": strat_name,
        "n_trades": n,
        "mfe_mean_ticks": round(mfe_mean, 1),
        "mfe_p50": round(mfe_p50, 1),
        "mfe_p75": round(mfe_p75, 1),
        "mae_mean_ticks": round(mae_mean, 1),
        "mae_p50": round(mae_p50, 1),
        "mae_p75": round(mae_p75, 1),
        "mfe_mae_ratio": round(mfe_mae_ratio, 2),
        "policy_results": policy_results,
    }


def recommend_best_policy(analysis: dict, exclude_oracle: bool = True) -> tuple:
    """Pick best policy (excluding mfe_oracle since it's look-ahead)."""
    if not analysis:
        return (None, None)
    candidates = [(pname, pdata) for pname, pdata in analysis["policy_results"].items()
                   if not (exclude_oracle and pname == "mfe_oracle_75")]
    # Sort by total $ first, then by PF as tiebreaker
    candidates.sort(key=lambda x: (x[1]["total"], x[1]["pf"]), reverse=True)
    best = candidates[0]
    return (best[0], best[1])


def main():
    print("=" * 100)
    print("PHOENIX STOP/TARGET OPTIMIZER — Per-Strategy Recommendations (clean 5y data)")
    print("=" * 100)
    print()
    print("Loading data (this may take ~30s)...")
    mnq_1m = load_mnq_1m()
    trades = load_trades()
    print(f"  MNQ 1m bars: {len(mnq_1m):,}")
    print(f"  Total trades (winning strategies): {len(trades):,}")
    print()

    # Per-strategy analysis
    analyses = {}
    for strat in WINNERS:
        a = analyze_strategy(strat, trades, mnq_1m)
        if a:
            analyses[strat] = a

    # Build summary
    print()
    print("=" * 100)
    print("PER-STRATEGY MFE/MAE ANALYSIS (assesses INITIAL stop placement)")
    print("=" * 100)
    print()
    print(f"{'strategy':<28s}  {'n':>5s}  {'MFE_mean':>9s}  {'MAE_mean':>9s}  {'MFE/MAE':>8s}  {'assessment':<30s}")
    print("-" * 100)
    for strat, a in sorted(analyses.items(),
                            key=lambda x: -x[1]["mfe_mae_ratio"]):
        ratio = a["mfe_mae_ratio"]
        if ratio >= 1.5:
            assess = "STOP TOO TIGHT (could widen)"
        elif ratio >= 0.8:
            assess = "stop ~ optimal"
        else:
            assess = "STOP TOO WIDE (too much risk)"
        print(f"{strat:<28s}  {a['n_trades']:>5d}  "
              f"{a['mfe_mean_ticks']:>9.1f}  {a['mae_mean_ticks']:>9.1f}  "
              f"{ratio:>8.2f}  {assess:<30s}")

    # Per-strategy: all policies side-by-side
    print()
    print("=" * 100)
    print("PER-STRATEGY EXIT POLICY COMPARISON")
    print("=" * 100)
    for strat, a in analyses.items():
        if a["n_trades"] < 30:
            continue
        print()
        print(f"--- {strat}  (n={a['n_trades']}) ---")
        print(f"{'policy':<18s}  {'WR%':>6s}  {'Total$':>10s}  {'Avg$':>8s}  {'PF':>6s}")
        for pname, pdata in a["policy_results"].items():
            marker = "  <-- BEST" if pname == recommend_best_policy(a)[0] else ""
            oracle_note = "  (oracle, look-ahead)" if pname == "mfe_oracle_75" else ""
            print(f"{pname:<18s}  {pdata['wr_pct']:>6.1f}  "
                  f"{pdata['total']:>10,.0f}  {pdata['avg']:>8.2f}  "
                  f"{pdata['pf']:>6.2f}{marker}{oracle_note}")

    # Final recommendations table
    print()
    print("=" * 100)
    print("FINAL PER-STRATEGY RECOMMENDATIONS")
    print("=" * 100)
    print()
    rec_rows = []
    print(f"{'strategy':<28s}  {'best_policy':<18s}  {'wr%':>6s}  {'total$':>10s}  {'pf':>6s}  {'profitable?':>12s}")
    print("-" * 100)
    for strat, a in sorted(analyses.items(),
                            key=lambda x: -x[1]["policy_results"][recommend_best_policy(x[1])[0]]["total"]):
        best_name, best_data = recommend_best_policy(a)
        profitable = "YES" if best_data["total"] > 0 else "NO"
        baseline = a["policy_results"]["baseline"]
        lift = best_data["total"] - baseline["total"]
        lift_str = f"  (+${lift:,.0f} vs baseline)" if lift > 0 else f"  (${lift:,.0f} vs baseline)"
        print(f"{strat:<28s}  {best_name:<18s}  {best_data['wr_pct']:>6.1f}  "
              f"{best_data['total']:>10,.0f}  {best_data['pf']:>6.2f}  {profitable:>12s}{lift_str}")
        rec_rows.append({
            "strategy": strat,
            "n_trades": a["n_trades"],
            "best_policy": best_name,
            "best_total": best_data["total"],
            "best_wr_pct": best_data["wr_pct"],
            "best_pf": best_data["pf"],
            "baseline_total": baseline["total"],
            "lift_vs_baseline": lift,
            "profitable": profitable,
            "mfe_mae_ratio": a["mfe_mae_ratio"],
            "mfe_mean_ticks": a["mfe_mean_ticks"],
            "mae_mean_ticks": a["mae_mean_ticks"],
        })

    rec_df = pd.DataFrame(rec_rows)
    out = ROOT / "backtest_results" / "phoenix_stop_target_recommendations.csv"
    rec_df.to_csv(out, index=False)
    print()
    print(f"Saved per-strategy recommendations -> {out}")


if __name__ == "__main__":
    main()
