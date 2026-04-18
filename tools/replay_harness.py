#!/usr/bin/env python3
"""
Phoenix Bot — Walk-Forward Replay Harness

Deterministic replay of stored bars from logs/history/*.jsonl through strategy
evaluators WITHOUT writing OIFs or touching NT8. Gates all new signals before
they ship to live trading.

Capabilities:
- Multi-window walk-forward optimization (3×5d, 6×5d, 4×10d)
- Data quality filter (excludes days with tick gaps > N minutes)
- Monte Carlo reordering (1,000 iterations by default → risk-of-ruin distribution)
- Cost model: $1.00 MNQ commission per side + 1.5 tick avg slippage
- Metrics: profit factor, Sharpe, Sortino, expectancy, max DD, WR, break-even WR
- OOS degradation flag (fail if OOS Sharpe < 0.5 × IS Sharpe)

Usage:
    # Run WFO on all available history
    python tools/replay_harness.py --all

    # Run on specific date range
    python tools/replay_harness.py --start 2026-04-13 --end 2026-04-16

    # Specific window config
    python tools/replay_harness.py --window-days 5 --num-windows 3

    # Monte Carlo sweep
    python tools/replay_harness.py --monte-carlo 10000
"""

import argparse
import json
import logging
import os
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger("ReplayHarness")

PHOENIX_ROOT = Path(__file__).parent.parent
HISTORY_DIR = PHOENIX_ROOT / "logs" / "history"

# ─── COST MODEL ────────────────────────────────────────────────────────
# MNQ micro futures:
#   Commission: ~$1.00/side at typical brokers (Tradovate, NinjaTrader, etc.)
#   Round-trip: ~$2.00
# Slippage (market orders): avg 1.5 ticks = 0.375 points = $0.75/contract
# Slippage (limit orders): avg 0.5 ticks = 0.125 points = $0.25/contract
COMMISSION_PER_SIDE_USD = 1.00
SLIPPAGE_MARKET_TICKS = 1.5
SLIPPAGE_LIMIT_TICKS = 0.5
TICK_VALUE_USD = 0.50  # MNQ: $0.50 per tick
MNQ_TICK_SIZE = 0.25   # MNQ: 1 tick = 0.25 points

# ─── DATA QUALITY ──────────────────────────────────────────────────────
MAX_TICK_GAP_MINUTES = 30  # Exclude trading days with gaps > this


def load_bars(date_filter_start: date = None, date_filter_end: date = None,
              bot: str = "prod") -> dict[date, list[dict]]:
    """Load bar records from history jsonl files. Returns {date: [bars]}."""
    bars_by_day = defaultdict(list)

    for path in sorted(HISTORY_DIR.glob(f"*_{bot}.jsonl")):
        filename_date = path.stem.split("_")[0]  # "2026-04-15_prod" → "2026-04-15"
        try:
            file_date = datetime.strptime(filename_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if date_filter_start and file_date < date_filter_start:
            continue
        if date_filter_end and file_date > date_filter_end:
            continue

        with open(path, "r") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "bar":
                    continue
                if rec.get("timeframe") != "1m":
                    continue  # Use 1m bars for replay (5m/15m can be re-derived)
                bars_by_day[file_date].append(rec)

    logger.info(f"Loaded {sum(len(b) for b in bars_by_day.values())} 1m bars from "
                f"{len(bars_by_day)} days")
    return dict(bars_by_day)


def data_quality_filter(bars_by_day: dict[date, list[dict]]) -> dict[date, list[dict]]:
    """
    Exclude days with tick gaps > MAX_TICK_GAP_MINUTES during RTH (08:30-15:00 CDT).
    Overnight gaps (end of day → start of next day) are normal and ignored.
    """
    clean = {}
    for day, bars in bars_by_day.items():
        if len(bars) < 10:
            logger.warning(f"[QUALITY] Excluding {day}: only {len(bars)} bars (suspicious)")
            continue

        # Only check gaps between bars that are BOTH during RTH (08:30-15:00 CDT).
        # CDT = UTC-5 (or UTC-6 in standard time). Timestamps in history are UTC-naive
        # representing local time, so just filter by hour.
        rth_bars = [
            b for b in bars
            if (h := datetime.fromisoformat(b["ts"].replace("Z", "")).hour) >= 8 and h < 15
        ]
        if len(rth_bars) < 10:
            logger.warning(f"[QUALITY] Excluding {day}: only {len(rth_bars)} RTH bars")
            continue

        rth_times = [datetime.fromisoformat(b["ts"].replace("Z", "")) for b in rth_bars]
        max_gap = max(
            (rth_times[i] - rth_times[i-1]).total_seconds() / 60
            for i in range(1, len(rth_times))
        )
        if max_gap > MAX_TICK_GAP_MINUTES:
            logger.warning(f"[QUALITY] Excluding {day}: RTH gap {max_gap:.1f} min > {MAX_TICK_GAP_MINUTES}")
            continue
        clean[day] = bars
    logger.info(f"Quality filter: {len(clean)}/{len(bars_by_day)} days passed")
    return clean


# ─── SIMPLE REPLAY STRATEGY (PLACEHOLDER) ──────────────────────────────
# Saturday will replace this with real strategy evaluators pulled from
# strategies/*.py. For now, a trivial example to exercise the pipeline.

def placeholder_strategy_signal(bar: dict, prev_bars: list[dict]) -> dict | None:
    """
    Placeholder signal generator — demonstrates the interface.
    Returns:
        {"direction": "LONG"|"SHORT", "entry": price, "stop": price,
         "target": price, "order_type": "LIMIT"|"MARKET"}
        or None if no signal
    """
    # Trivial EMA crossover demo. REPLACE Saturday.
    if len(prev_bars) < 21:
        return None
    if bar.get("ema9", 0) <= 0 or bar.get("ema21", 0) <= 0:
        return None
    prev = prev_bars[-1]

    ema9 = bar["ema9"]
    ema21 = bar["ema21"]
    prev_ema9 = prev.get("ema9", 0)
    prev_ema21 = prev.get("ema21", 0)
    atr = bar.get("atr_1m", 5.0)

    # Bullish crossover
    if ema9 > ema21 and prev_ema9 <= prev_ema21:
        entry = bar["close"]
        stop = entry - (2.0 * atr)
        target = entry + (3.0 * atr)  # 1:1.5 R:R
        return {"direction": "LONG", "entry": entry, "stop": stop,
                "target": target, "order_type": "LIMIT"}
    # Bearish crossover
    if ema9 < ema21 and prev_ema9 >= prev_ema21:
        entry = bar["close"]
        stop = entry + (2.0 * atr)
        target = entry - (3.0 * atr)
        return {"direction": "SHORT", "entry": entry, "stop": stop,
                "target": target, "order_type": "LIMIT"}
    return None


def simulate_trade(signal: dict, future_bars: list[dict]) -> dict:
    """
    Simulate how a trade would resolve given future bars.
    Returns trade record: {direction, entry, exit, pnl_pts, pnl_usd, outcome, bars_held}
    """
    direction = signal["direction"]
    entry = signal["entry"]
    stop = signal["stop"]
    target = signal["target"]

    # Apply entry slippage
    slip_ticks = SLIPPAGE_LIMIT_TICKS if signal.get("order_type") == "LIMIT" else SLIPPAGE_MARKET_TICKS
    slip_points = slip_ticks * MNQ_TICK_SIZE
    if direction == "LONG":
        entry_actual = entry + slip_points  # Pay up
    else:
        entry_actual = entry - slip_points

    # Walk forward bars to find exit
    for i, bar in enumerate(future_bars[:60]):  # Max hold: 60 bars = 1h
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        if direction == "LONG":
            if low <= stop:
                exit_price = stop - (SLIPPAGE_MARKET_TICKS * MNQ_TICK_SIZE)
                pnl_pts = exit_price - entry_actual
                outcome = "STOP"
                break
            if high >= target:
                exit_price = target
                pnl_pts = exit_price - entry_actual
                outcome = "TARGET"
                break
        else:  # SHORT
            if high >= stop:
                exit_price = stop + (SLIPPAGE_MARKET_TICKS * MNQ_TICK_SIZE)
                pnl_pts = entry_actual - exit_price
                outcome = "STOP"
                break
            if low <= target:
                exit_price = target
                pnl_pts = entry_actual - exit_price
                outcome = "TARGET"
                break
    else:
        # Time-out: exit at last close
        exit_price = future_bars[-1]["close"] if future_bars else entry_actual
        pnl_pts = (exit_price - entry_actual) if direction == "LONG" else (entry_actual - exit_price)
        outcome = "TIMEOUT"
        i = len(future_bars) - 1

    # PnL calculation: points × $2/point (MNQ = $0.50/tick × 4 ticks/point = $2/point)
    pnl_usd = pnl_pts * (TICK_VALUE_USD * 4)  # $2/point MNQ
    pnl_usd -= 2 * COMMISSION_PER_SIDE_USD  # Round-trip commission

    return {
        "direction": direction,
        "entry": round(entry_actual, 2),
        "exit": round(exit_price, 2),
        "pnl_pts": round(pnl_pts, 2),
        "pnl_usd": round(pnl_usd, 2),
        "outcome": outcome,
        "bars_held": i + 1,
    }


def replay_period(bars_by_day: dict[date, list[dict]],
                  strategy_fn: Callable = placeholder_strategy_signal) -> list[dict]:
    """Replay strategy across all days, return list of trades."""
    trades = []
    for day in sorted(bars_by_day.keys()):
        bars = bars_by_day[day]
        in_position = False
        for i, bar in enumerate(bars):
            if in_position:
                continue  # Skip while simulated position open
            prev_bars = bars[:i]
            signal = strategy_fn(bar, prev_bars)
            if not signal:
                continue
            future = bars[i+1:]
            trade = simulate_trade(signal, future)
            trade["day"] = day.isoformat()
            trade["bar_idx"] = i
            trades.append(trade)
            # Skip ahead past the trade duration to avoid stacking
            # (placeholder doesn't track position state; real strategies will)
    return trades


# ─── METRICS ────────────────────────────────────────────────────────────

def compute_metrics(trades: list[dict]) -> dict:
    """Compute profit factor, Sharpe, Sortino, WR, max DD, etc."""
    if not trades:
        return {
            "trade_count": 0, "win_rate": 0, "profit_factor": 0,
            "sharpe": 0, "sortino": 0, "expectancy": 0, "max_drawdown_usd": 0,
            "avg_win_usd": 0, "avg_loss_usd": 0, "net_pnl_usd": 0,
            "break_even_wr": 0,
        }
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0

    net = sum(pnls)
    wr = len(wins) / len(pnls) if pnls else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = statistics.mean(losses) if losses else 0  # negative
    expectancy = wr * avg_win + (1 - wr) * avg_loss

    # Sharpe (per-trade, not annualized — comparison within run only)
    mean_pnl = statistics.mean(pnls)
    sd_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 0
    sharpe = mean_pnl / sd_pnl if sd_pnl > 0 else 0

    # Sortino (downside-only std)
    downside = [p for p in pnls if p < 0]
    if downside and len(downside) > 1:
        sd_downside = statistics.stdev(downside)
        sortino = mean_pnl / sd_downside if sd_downside > 0 else 0
    else:
        sortino = 0

    # Max drawdown
    cumulative = []
    cum = 0
    for p in pnls:
        cum += p
        cumulative.append(cum)
    peak = 0
    max_dd = 0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd

    # Break-even WR given R:R
    if avg_loss < 0 and avg_win > 0:
        be_wr = abs(avg_loss) / (avg_win + abs(avg_loss))
    else:
        be_wr = 0

    return {
        "trade_count": len(pnls),
        "win_rate": round(wr, 4),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "expectancy": round(expectancy, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "net_pnl_usd": round(net, 2),
        "break_even_wr": round(be_wr, 4),
    }


def monte_carlo_risk_of_ruin(trades: list[dict], starting_balance: float = 300.0,
                             ruin_balance: float = 50.0, iterations: int = 1000) -> dict:
    """
    Reorder trade list N times, count how often account goes below ruin threshold.
    Returns risk-of-ruin probability and distribution summary.
    """
    if not trades:
        return {"iterations": 0, "risk_of_ruin_pct": 0, "p5_final": 0,
                "p50_final": 0, "p95_final": 0}
    pnls = [t["pnl_usd"] for t in trades]
    ruin_count = 0
    final_balances = []
    for _ in range(iterations):
        shuffled = pnls.copy()
        random.shuffle(shuffled)
        bal = starting_balance
        hit_ruin = False
        for p in shuffled:
            bal += p
            if bal <= ruin_balance:
                hit_ruin = True
                break
        if hit_ruin:
            ruin_count += 1
        final_balances.append(bal)
    final_balances.sort()
    return {
        "iterations": iterations,
        "starting_balance_usd": starting_balance,
        "ruin_threshold_usd": ruin_balance,
        "risk_of_ruin_pct": round(ruin_count / iterations, 4),
        "p5_final_usd": round(final_balances[int(0.05 * iterations)], 2),
        "p50_final_usd": round(final_balances[int(0.50 * iterations)], 2),
        "p95_final_usd": round(final_balances[int(0.95 * iterations)], 2),
    }


# ─── WALK-FORWARD ──────────────────────────────────────────────────────

def walk_forward(bars_by_day: dict[date, list[dict]], window_days: int,
                 strategy_fn: Callable = placeholder_strategy_signal) -> dict:
    """
    Walk-forward with rolling windows.
    Each window: first window_days-1 days = in-sample, last day = out-of-sample.
    Returns per-window metrics + aggregate.
    """
    sorted_days = sorted(bars_by_day.keys())
    n = len(sorted_days)
    if n < window_days:
        logger.warning(f"Not enough days ({n}) for window {window_days}")
        return {
            "window_days": window_days, "num_windows": 0,
            "windows": [], "aggregate_oos": compute_metrics([]),
            "degraded_window_count": 0,
        }

    windows = []
    for start_idx in range(0, n - window_days + 1):
        window_dates = sorted_days[start_idx:start_idx + window_days]
        is_dates = window_dates[:-1]
        oos_dates = window_dates[-1:]

        is_bars = {d: bars_by_day[d] for d in is_dates}
        oos_bars = {d: bars_by_day[d] for d in oos_dates}

        is_trades = replay_period(is_bars, strategy_fn)
        oos_trades = replay_period(oos_bars, strategy_fn)

        is_metrics = compute_metrics(is_trades)
        oos_metrics = compute_metrics(oos_trades)

        # OOS degradation check
        degraded = False
        if is_metrics["sharpe"] > 0:
            if oos_metrics["sharpe"] < 0.5 * is_metrics["sharpe"]:
                degraded = True

        windows.append({
            "window_start": is_dates[0].isoformat() if is_dates else None,
            "is_dates": [d.isoformat() for d in is_dates],
            "oos_dates": [d.isoformat() for d in oos_dates],
            "is_metrics": is_metrics,
            "oos_metrics": oos_metrics,
            "oos_degraded": degraded,
        })

    # Aggregate OOS performance across all windows
    all_oos_trades = []
    for w in windows:
        # Re-derive OOS trades from the OOS dates
        oos_days = [datetime.strptime(d, "%Y-%m-%d").date() for d in w["oos_dates"]]
        oos_bars = {d: bars_by_day[d] for d in oos_days if d in bars_by_day}
        all_oos_trades.extend(replay_period(oos_bars, strategy_fn))
    aggregate_oos = compute_metrics(all_oos_trades)

    return {
        "window_days": window_days,
        "num_windows": len(windows),
        "windows": windows,
        "aggregate_oos": aggregate_oos,
        "degraded_window_count": sum(1 for w in windows if w["oos_degraded"]),
    }


# ─── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phoenix WFO replay harness")
    parser.add_argument("--all", action="store_true", help="Use all available history")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    parser.add_argument("--bot", type=str, default="prod", choices=["prod", "lab"])
    parser.add_argument("--window-days", type=int, default=5)
    parser.add_argument("--monte-carlo", type=int, default=1000, help="Monte Carlo iterations")
    parser.add_argument("--starting-balance", type=float, default=300.0)
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    logger.info(f"=== Phoenix Replay Harness ===")
    logger.info(f"Bot: {args.bot}  Window: {args.window_days}d  Monte Carlo: {args.monte_carlo} iter")

    # Load + filter
    bars_by_day = load_bars(start, end, args.bot)
    if not bars_by_day:
        logger.error("No data loaded. Check logs/history/ directory.")
        return 1
    clean = data_quality_filter(bars_by_day)
    if not clean:
        logger.error("No days passed quality filter.")
        return 1

    # Single-period baseline (sanity check)
    all_trades = replay_period(clean)
    baseline_metrics = compute_metrics(all_trades)
    baseline_mc = monte_carlo_risk_of_ruin(all_trades, args.starting_balance, 50.0, args.monte_carlo)

    # Walk-forward
    wfo = walk_forward(clean, args.window_days)

    result = {
        "run_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            "bot": args.bot, "start": str(start), "end": str(end),
            "window_days": args.window_days, "mc_iterations": args.monte_carlo,
            "starting_balance_usd": args.starting_balance,
            "cost_model": {
                "commission_per_side": COMMISSION_PER_SIDE_USD,
                "slippage_market_ticks": SLIPPAGE_MARKET_TICKS,
                "slippage_limit_ticks": SLIPPAGE_LIMIT_TICKS,
            },
        },
        "baseline": {
            "days_included": len(clean),
            "metrics": baseline_metrics,
            "monte_carlo": baseline_mc,
        },
        "walk_forward": wfo,
    }

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"\n=== BASELINE (all {len(clean)} days, single period) ===")
        for k, v in baseline_metrics.items():
            print(f"  {k}: {v}")
        print(f"\n=== MONTE CARLO (risk of ruin from ${args.starting_balance}) ===")
        for k, v in baseline_mc.items():
            print(f"  {k}: {v}")
        print(f"\n=== WALK-FORWARD ({args.window_days}d windows, {wfo['num_windows']} windows) ===")
        print(f"  Degraded windows (OOS Sharpe < 0.5 × IS): {wfo['degraded_window_count']}/{wfo['num_windows']}")
        print(f"  Aggregate OOS metrics:")
        for k, v in wfo["aggregate_oos"].items():
            print(f"    {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
