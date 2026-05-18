"""
Phoenix Bot — ES/NQ Divergence STANDALONE STRATEGY Backtester (v2 OHLC)

v2 fixes the OHLC limitation from v1 by parsing the raw .Last files
directly and aggregating to proper 5-min OHLC bars. This gives accurate
intrabar stop and target fills — critical for honest results.

USAGE:
    python tools/strategy_backtest_es_nq_v2.py

INPUTS (read from data/historical/):
    MNQ 06-26.Last  (or whatever .Last file is present)
    MES 06-26.Last

OUTPUTS (written to data/historical/):
    es_nq_strategy_summary_v2.csv  (one row per config)
    es_nq_strategy_trades_v2.csv   (one row per trade)
"""

from __future__ import annotations
import csv
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from itertools import product

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
HISTORICAL_DIR = PROJECT_ROOT / "data" / "historical"
SUMMARY_CSV = HISTORICAL_DIR / "es_nq_strategy_summary_v2.csv"
TRADES_CSV = HISTORICAL_DIR / "es_nq_strategy_trades_v2.csv"

TICK_SIZE = 0.25
TICK_VALUE = 0.50  # MNQ tick = $0.50
SLIPPAGE_TICKS = 0.5


# ──────────────────────────────────────────────────────────────────
# .Last file parsing → 5-min OHLC bars
# ──────────────────────────────────────────────────────────────────

def find_last_file(prefix):
    """Find a file matching '<prefix>*.Last' in the historical dir."""
    matches = list(HISTORICAL_DIR.glob(f"{prefix}*.Last"))
    if not matches:
        return None
    matches.sort()
    return matches[-1]  # most recent contract


def parse_last_file(path):
    """
    Parse a NinjaTrader .Last file (semicolon-delimited ticks).
    Format: YYYYMMDD HHMMSS xxxxxx;price;volume
    
    Yields: (timestamp_datetime, price_float)
    """
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(";")
            if len(parts) < 2:
                continue
            try:
                ts_str = parts[0]
                price = float(parts[1])
            except (ValueError, IndexError):
                continue
            
            # Parse timestamp — try several formats
            ts = None
            # Format: "YYYYMMDD HHMMSS xxxxxx" (with optional fractional)
            try:
                if " " in ts_str:
                    date_part, time_part = ts_str.split(" ", 1)
                    # time_part may be "HHMMSS" or "HHMMSS ffffff"
                    time_clean = time_part.replace(" ", "")
                    if len(time_clean) >= 6:
                        ts = datetime(
                            int(date_part[0:4]), int(date_part[4:6]), int(date_part[6:8]),
                            int(time_clean[0:2]), int(time_clean[2:4]), int(time_clean[4:6]),
                        )
                elif len(ts_str) >= 14:
                    ts = datetime(
                        int(ts_str[0:4]), int(ts_str[4:6]), int(ts_str[6:8]),
                        int(ts_str[8:10]), int(ts_str[10:12]), int(ts_str[12:14]),
                    )
            except (ValueError, IndexError):
                continue
            
            if ts is None:
                continue
            yield (ts, price)


def aggregate_to_ohlc(ticks_iter, bar_minutes=5):
    """
    Aggregate ticks into OHLC bars.
    Each bar represents a fixed bar_minutes window.
    
    Returns: list of dicts with keys ts, open, high, low, close, tick_count
    """
    bars = []
    current_bucket_start = None
    bucket = None
    
    for ts, price in ticks_iter:
        # Floor to bar_minutes boundary
        bucket_start = ts.replace(second=0, microsecond=0)
        minute_floor = (bucket_start.minute // bar_minutes) * bar_minutes
        bucket_start = bucket_start.replace(minute=minute_floor)
        
        if bucket_start != current_bucket_start:
            if bucket is not None:
                bars.append(bucket)
            current_bucket_start = bucket_start
            bucket = {
                "ts": bucket_start,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "tick_count": 1,
            }
        else:
            bucket["high"] = max(bucket["high"], price)
            bucket["low"] = min(bucket["low"], price)
            bucket["close"] = price
            bucket["tick_count"] += 1
    
    if bucket is not None:
        bars.append(bucket)
    
    return bars


def pair_bars(nq_bars, es_bars):
    """
    Pair NQ and ES bars by timestamp. Returns only bars present in BOTH.
    """
    es_by_ts = {b["ts"]: b for b in es_bars}
    paired = []
    for nq_bar in nq_bars:
        ts = nq_bar["ts"]
        if ts in es_by_ts:
            paired.append({
                "ts": ts,
                "hour": ts.hour,
                "nq_open": nq_bar["open"],
                "nq_high": nq_bar["high"],
                "nq_low": nq_bar["low"],
                "nq_close": nq_bar["close"],
                "es_open": es_by_ts[ts]["open"],
                "es_high": es_by_ts[ts]["high"],
                "es_low": es_by_ts[ts]["low"],
                "es_close": es_by_ts[ts]["close"],
            })
    return paired


# ──────────────────────────────────────────────────────────────────
# Confluence computation
# ──────────────────────────────────────────────────────────────────

def compute_confluence(bars, z_window=20, corr_window=60):
    """
    Compute ES/NQ confluence per bar:
      - spread_z: z-score of NQ-ES return spread
      - correlation: rolling correlation
      - smt_bullish / smt_bearish: SMT divergence flags
      - boost_long / boost_short: aggregate signals
    """
    if len(bars) < max(z_window, corr_window) + 5:
        return bars
    
    nq_returns = [0.0]
    es_returns = [0.0]
    for i in range(1, len(bars)):
        nq_returns.append((bars[i]["nq_close"] - bars[i-1]["nq_close"]) / bars[i-1]["nq_close"])
        es_returns.append((bars[i]["es_close"] - bars[i-1]["es_close"]) / bars[i-1]["es_close"])
    
    # Compute spread (NQ returns - beta-adjusted ES returns)
    # Simplification: use raw spread (NQ_ret - ES_ret) — beta close to 1 in returns space
    for i, bar in enumerate(bars):
        bar["spread_z"] = 0.0
        bar["correlation"] = 0.0
        bar["smt_bullish"] = False
        bar["smt_bearish"] = False
        bar["boost_long"] = 0
        bar["boost_short"] = 0
        
        if i < max(z_window, corr_window):
            continue
        
        # Z-score of return spread
        spread_window = [nq_returns[j] - es_returns[j] for j in range(i - z_window, i + 1)]
        mean_s = sum(spread_window) / len(spread_window)
        var_s = sum((s - mean_s) ** 2 for s in spread_window) / max(1, len(spread_window) - 1)
        std_s = math.sqrt(var_s) if var_s > 0 else 1e-9
        current_spread = nq_returns[i] - es_returns[i]
        bar["spread_z"] = (current_spread - mean_s) / std_s
        
        # Rolling correlation
        nq_w = nq_returns[i - corr_window:i + 1]
        es_w = es_returns[i - corr_window:i + 1]
        bar["correlation"] = pearson(nq_w, es_w)
        
        # SMT divergence — basic version
        # Bullish SMT: NQ made lower low than recent, but ES didn't
        recent_window = 10
        if i >= recent_window:
            nq_recent_low = min(b["nq_low"] for b in bars[i - recent_window:i])
            es_recent_low = min(b["es_low"] for b in bars[i - recent_window:i])
            if bars[i]["nq_low"] < nq_recent_low and bars[i]["es_low"] > es_recent_low:
                bar["smt_bullish"] = True
            
            nq_recent_high = max(b["nq_high"] for b in bars[i - recent_window:i])
            es_recent_high = max(b["es_high"] for b in bars[i - recent_window:i])
            if bars[i]["nq_high"] > nq_recent_high and bars[i]["es_high"] < es_recent_high:
                bar["smt_bearish"] = True
        
        # Aggregate boost scores
        # LONG boost: NQ underperforming (negative z) + correlation high + smt_bullish
        if bar["correlation"] > 0.85:
            if bar["spread_z"] < -1.5:
                bar["boost_long"] += 5
            elif bar["spread_z"] < -1.0:
                bar["boost_long"] += 3
            elif bar["spread_z"] < -0.5:
                bar["boost_long"] += 1
            
            if bar["smt_bullish"]:
                bar["boost_long"] += 5
            
            if bar["spread_z"] > 1.5:
                bar["boost_short"] += 5
            elif bar["spread_z"] > 1.0:
                bar["boost_short"] += 3
            elif bar["spread_z"] > 0.5:
                bar["boost_short"] += 1
            
            if bar["smt_bearish"]:
                bar["boost_short"] += 5
    
    return bars


def pearson(x, y):
    """Pearson correlation coefficient."""
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((y[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def is_ny_pm(row):
    return 13 <= row["hour"] <= 14


# ──────────────────────────────────────────────────────────────────
# Strategy backtester
# ──────────────────────────────────────────────────────────────────

class StrategyConfig:
    def __init__(self, name, direction, entry_threshold, entry_timing,
                 stop_ticks, target_rule, target_value,
                 time_filter, corr_min, max_hold_bars=20,
                 pullback_ticks=4, pullback_wait_bars=3):
        self.name = name
        self.direction = direction
        self.entry_threshold = entry_threshold
        self.entry_timing = entry_timing
        self.stop_ticks = stop_ticks
        self.target_rule = target_rule
        self.target_value = target_value
        self.time_filter = time_filter
        self.corr_min = corr_min
        self.max_hold_bars = max_hold_bars
        self.pullback_ticks = pullback_ticks
        self.pullback_wait_bars = pullback_wait_bars
    
    def signal_fires(self, row):
        if row["correlation"] < self.corr_min:
            return False
        if self.time_filter == "ny_pm_only" and not is_ny_pm(row):
            return False
        if self.direction == "LONG":
            return row["boost_long"] >= self.entry_threshold
        return row["boost_short"] >= self.entry_threshold


def simulate_strategy(bars, config):
    trades = []
    open_position = None
    i = 0
    n = len(bars)
    
    while i < n - 1:
        row = bars[i]
        
        if open_position:
            exit_result = check_exit(bars, i, open_position, config)
            if exit_result:
                exit_price, exit_reason, exit_idx = exit_result
                close_trade(open_position, exit_price, exit_reason,
                          bars[exit_idx]["ts"], exit_idx - open_position["entry_idx"])
                trades.append(open_position)
                open_position = None
                i = exit_idx + 1
                continue
            i += 1
            continue
        
        if config.signal_fires(row):
            entry = attempt_entry(bars, i, config)
            if entry:
                open_position = entry
                i = entry["entry_idx"] + 1
                continue
        i += 1
    
    if open_position:
        last_bar = bars[n - 1]
        close_trade(open_position, last_bar["nq_close"], "eod",
                  last_bar["ts"], n - 1 - open_position["entry_idx"])
        trades.append(open_position)
    
    return trades


def attempt_entry(bars, signal_idx, config):
    if signal_idx + 1 >= len(bars):
        return None
    
    signal_bar = bars[signal_idx]
    next_bar = bars[signal_idx + 1]
    direction_sign = 1 if config.direction == "LONG" else -1
    
    if config.entry_timing == "market":
        entry_price = next_bar["nq_open"] + (SLIPPAGE_TICKS * TICK_SIZE * direction_sign)
        entry_idx = signal_idx + 1
    
    elif config.entry_timing == "bar_close":
        if signal_idx + 1 >= len(bars):
            return None
        confirm_bar = bars[signal_idx + 1]
        if config.direction == "LONG":
            confirmed = confirm_bar["nq_close"] > signal_bar["nq_close"]
        else:
            confirmed = confirm_bar["nq_close"] < signal_bar["nq_close"]
        if not confirmed:
            return None
        entry_price = confirm_bar["nq_close"] + (SLIPPAGE_TICKS * TICK_SIZE * direction_sign)
        entry_idx = signal_idx + 1
    
    elif config.entry_timing == "pullback":
        limit_price = signal_bar["nq_close"] - (config.pullback_ticks * TICK_SIZE * direction_sign)
        filled = False
        entry_idx = None
        entry_price = None
        for k in range(1, config.pullback_wait_bars + 1):
            if signal_idx + k >= len(bars):
                break
            check_bar = bars[signal_idx + k]
            if config.direction == "LONG":
                if check_bar["nq_low"] <= limit_price:
                    entry_price = limit_price
                    entry_idx = signal_idx + k
                    filled = True
                    break
            else:
                if check_bar["nq_high"] >= limit_price:
                    entry_price = limit_price
                    entry_idx = signal_idx + k
                    filled = True
                    break
        if not filled:
            return None
    else:
        return None
    
    stop_price = entry_price - (config.stop_ticks * TICK_SIZE * direction_sign)
    
    if config.target_rule == "fixed_rr":
        target_ticks = config.stop_ticks * config.target_value
    else:
        target_ticks = config.target_value
    
    target_price = entry_price + (target_ticks * TICK_SIZE * direction_sign)
    
    return {
        "entry_idx": entry_idx,
        "entry_ts": bars[entry_idx]["ts"],
        "entry_price": entry_price,
        "direction": config.direction,
        "stop_price": stop_price,
        "target_price": target_price,
        "config_name": config.name,
        "mfe_ticks": 0,
        "mae_ticks": 0,
    }


def check_exit(bars, i, position, config):
    """Check exits using PROPER OHLC — intrabar stops and targets fire correctly."""
    if i <= position["entry_idx"]:
        return None
    
    bar = bars[i]
    bars_held = i - position["entry_idx"]
    entry_price = position["entry_price"]
    
    # Update MFE/MAE
    if position["direction"] == "LONG":
        mfe = (bar["nq_high"] - entry_price) / TICK_SIZE
        mae = (bar["nq_low"] - entry_price) / TICK_SIZE
    else:
        mfe = (entry_price - bar["nq_low"]) / TICK_SIZE
        mae = (entry_price - bar["nq_high"]) / TICK_SIZE
    
    position["mfe_ticks"] = max(position["mfe_ticks"], mfe)
    position["mae_ticks"] = min(position["mae_ticks"], mae)
    
    # Stop/target hit check — INTRABAR (uses high/low not close)
    if position["direction"] == "LONG":
        # Conservative: if both hit in same bar, assume stop filled first
        stop_hit = bar["nq_low"] <= position["stop_price"]
        target_hit = bar["nq_high"] >= position["target_price"]
        if stop_hit and target_hit:
            return (position["stop_price"], "stop_then_target_same_bar", i)
        if stop_hit:
            return (position["stop_price"], "stop", i)
        if target_hit:
            return (position["target_price"], "target", i)
    else:  # SHORT
        stop_hit = bar["nq_high"] >= position["stop_price"]
        target_hit = bar["nq_low"] <= position["target_price"]
        if stop_hit and target_hit:
            return (position["stop_price"], "stop_then_target_same_bar", i)
        if stop_hit:
            return (position["stop_price"], "stop", i)
        if target_hit:
            return (position["target_price"], "target", i)
    
    if bars_held >= config.max_hold_bars:
        return (bar["nq_close"], "time_exit", i)
    
    return None


def close_trade(position, exit_price, exit_reason, exit_ts, bars_held):
    direction_sign = 1 if position["direction"] == "LONG" else -1
    pnl_ticks = (exit_price - position["entry_price"]) / TICK_SIZE * direction_sign
    
    position["exit_price"] = exit_price
    position["exit_reason"] = exit_reason
    position["exit_ts"] = exit_ts
    position["bars_held"] = bars_held
    position["pnl_ticks"] = pnl_ticks
    position["pnl_dollars"] = pnl_ticks * TICK_VALUE


# ──────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────

def compute_metrics(trades, config_name):
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["pnl_dollars"] > 0]
    losses = [t for t in trades if t["pnl_dollars"] < 0]
    
    total_win = sum(t["pnl_dollars"] for t in wins)
    total_loss = abs(sum(t["pnl_dollars"] for t in losses))
    total_pnl = sum(t["pnl_dollars"] for t in trades)
    
    running = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running += t["pnl_dollars"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    
    return {
        "config": config_name,
        "n_trades": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": 100 * len(wins) / n if n else 0,
        "avg_win_dollars": total_win / len(wins) if wins else 0,
        "avg_loss_dollars": -total_loss / len(losses) if losses else 0,
        "profit_factor": total_win / total_loss if total_loss > 0 else float("inf"),
        "expectancy_dollars": total_pnl / n,
        "expectancy_ticks": sum(t["pnl_ticks"] for t in trades) / n,
        "total_pnl_dollars": total_pnl,
        "max_drawdown_dollars": max_dd,
        "avg_bars_held": statistics.mean([t["bars_held"] for t in trades]),
        "avg_mfe_ticks": statistics.mean([t.get("mfe_ticks", 0) for t in trades]),
        "avg_mae_ticks": statistics.mean([t.get("mae_ticks", 0) for t in trades]),
        "stop_exits": sum(1 for t in trades if "stop" in t["exit_reason"]),
        "target_exits": sum(1 for t in trades if "target" in t["exit_reason"]),
        "time_exits": sum(1 for t in trades if t["exit_reason"] == "time_exit"),
    }


# ──────────────────────────────────────────────────────────────────
# Configuration generator
# ──────────────────────────────────────────────────────────────────

def generate_configs():
    configs = []
    for direction in ["LONG", "SHORT"]:
        for entry_threshold in [5, 7]:
            for entry_timing in ["market", "bar_close", "pullback"]:
                for stop_ticks in [12, 16, 20, 24]:
                    for target_rule, target_value in [
                        ("fixed_rr", 1.0), ("fixed_rr", 1.5), ("fixed_rr", 2.0),
                        ("fixed_ticks", 24), ("fixed_ticks", 32),
                    ]:
                        for time_filter in ["all_day", "ny_pm_only"]:
                            for corr_min in [0.85, 0.90]:
                                name = (f"{direction}_thr{entry_threshold}_"
                                       f"{entry_timing}_stop{stop_ticks}_"
                                       f"tgt{target_rule}{target_value}_"
                                       f"{time_filter}_corr{corr_min}")
                                configs.append(StrategyConfig(
                                    name=name, direction=direction,
                                    entry_threshold=entry_threshold,
                                    entry_timing=entry_timing,
                                    stop_ticks=stop_ticks,
                                    target_rule=target_rule, target_value=target_value,
                                    time_filter=time_filter, corr_min=corr_min,
                                ))
    return configs


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 75)
    print("Phoenix — ES/NQ Standalone Strategy Backtester v2 (proper OHLC)")
    print("=" * 75)
    
    nq_path = find_last_file("MNQ")
    es_path = find_last_file("MES")
    if not nq_path or not es_path:
        print(f"❌ Could not find MNQ or MES .Last files in {HISTORICAL_DIR}")
        return
    
    print(f"\nParsing {nq_path.name}...")
    nq_bars = aggregate_to_ohlc(parse_last_file(nq_path), bar_minutes=5)
    print(f"  → {len(nq_bars):,} NQ 5-min bars")
    
    print(f"Parsing {es_path.name}...")
    es_bars = aggregate_to_ohlc(parse_last_file(es_path), bar_minutes=5)
    print(f"  → {len(es_bars):,} ES 5-min bars")
    
    print("Pairing bars by timestamp...")
    bars = pair_bars(nq_bars, es_bars)
    print(f"  → {len(bars):,} paired bars")
    
    if len(bars) < 100:
        print("❌ Not enough paired bars for backtest. Check data alignment.")
        return
    
    print("Computing ES/NQ confluence per bar...")
    compute_confluence(bars)
    
    configs = generate_configs()
    print(f"\nGenerated {len(configs)} configurations")
    print("Running backtest simulations (with proper OHLC)...\n")
    
    all_results = []
    all_trades = []
    
    for idx, config in enumerate(configs):
        if (idx + 1) % 100 == 0:
            print(f"  ... {idx + 1}/{len(configs)} configs tested")
        trades = simulate_strategy(bars, config)
        if len(trades) < 10:
            continue
        metrics = compute_metrics(trades, config.name)
        if metrics:
            all_results.append(metrics)
            for t in trades:
                t["config"] = config.name
            all_trades.extend(trades)
    
    print(f"\n{len(all_results)} configs produced valid backtests "
          f"(out of {len(configs)} tested; {len(configs) - len(all_results)} had <10 trades)")
    
    # Rank by expectancy
    ranked = sorted(all_results, key=lambda r: r["expectancy_dollars"], reverse=True)
    
    # Top 20
    print("\n" + "=" * 75)
    print("  TOP 20 STRATEGIES BY EXPECTANCY PER TRADE")
    print("=" * 75)
    
    for i, m in enumerate(ranked[:20], 1):
        print(f"\n  [{i}] {m['config']}")
        print(f"      trades: {m['n_trades']:4d}  |  WR: {m['win_rate']:5.1f}%  |  "
              f"PF: {m['profit_factor']:5.2f}  |  "
              f"expectancy: ${m['expectancy_dollars']:+7.2f}/trade")
        print(f"      total P&L: ${m['total_pnl_dollars']:+8.2f}  |  "
              f"max DD: ${m['max_drawdown_dollars']:.2f}  |  "
              f"avg hold: {m['avg_bars_held']:.1f} bars")
        print(f"      avg win: ${m['avg_win_dollars']:+.2f}  |  "
              f"avg loss: ${m['avg_loss_dollars']:+.2f}  |  "
              f"MFE: {m['avg_mfe_ticks']:+.1f}t  |  MAE: {m['avg_mae_ticks']:+.1f}t")
        print(f"      exits: {m['stop_exits']} stops, {m['target_exits']} targets, "
              f"{m['time_exits']} time")
    
    # Insights
    print("\n" + "=" * 75)
    print("  STRATEGY INSIGHTS — what works best across all configs")
    print("=" * 75)
    
    def filtered_stats(filter_fn, label_fn):
        results = []
        for m in all_results:
            if filter_fn(m):
                results.append(m)
        if not results:
            return None
        return {
            "n": len(results),
            "median_expectancy": statistics.median([m["expectancy_dollars"] for m in results]),
            "best_expectancy": max(m["expectancy_dollars"] for m in results),
            "median_wr": statistics.median([m["win_rate"] for m in results]),
            "median_pf": statistics.median([m["profit_factor"] for m in results
                                            if m["profit_factor"] != float("inf")]),
        }
    
    print("\n  Entry Timing:")
    for timing in ["market", "bar_close", "pullback"]:
        s = filtered_stats(lambda m, t=timing: f"_{t}_" in m["config"], None)
        if s:
            print(f"    {timing:10s}: n={s['n']:3d}  median_exp=${s['median_expectancy']:+.2f}  "
                  f"best=${s['best_expectancy']:+.2f}  "
                  f"median_WR={s['median_wr']:.1f}%  median_PF={s['median_pf']:.2f}")
    
    print("\n  Stop Distance:")
    for stop_t in [12, 16, 20, 24]:
        s = filtered_stats(lambda m, t=stop_t: f"_stop{t}_" in m["config"], None)
        if s:
            print(f"    stop={stop_t:2d}t: n={s['n']:3d}  median_exp=${s['median_expectancy']:+.2f}  "
                  f"best=${s['best_expectancy']:+.2f}  "
                  f"median_WR={s['median_wr']:.1f}%")
    
    print("\n  Target Rule:")
    for rule in ["fixed_rr1.0", "fixed_rr1.5", "fixed_rr2.0", "fixed_ticks24", "fixed_ticks32"]:
        s = filtered_stats(lambda m, r=rule: f"_tgt{r}_" in m["config"], None)
        if s:
            print(f"    {rule:16s}: n={s['n']:3d}  median_exp=${s['median_expectancy']:+.2f}  "
                  f"best=${s['best_expectancy']:+.2f}  median_WR={s['median_wr']:.1f}%")
    
    print("\n  Time Filter:")
    for tf in ["all_day", "ny_pm_only"]:
        s = filtered_stats(lambda m, t=tf: f"_{t}_" in m["config"], None)
        if s:
            print(f"    {tf:15s}: n={s['n']:3d}  median_exp=${s['median_expectancy']:+.2f}  "
                  f"best=${s['best_expectancy']:+.2f}  median_WR={s['median_wr']:.1f}%")
    
    print("\n  Direction:")
    for direction in ["LONG", "SHORT"]:
        s = filtered_stats(lambda m, d=direction: m["config"].startswith(d), None)
        if s:
            print(f"    {direction:5s}: n={s['n']:3d}  median_exp=${s['median_expectancy']:+.2f}  "
                  f"best=${s['best_expectancy']:+.2f}  median_WR={s['median_wr']:.1f}%")
    
    print("\n  Correlation Gate:")
    for corr in [0.85, 0.90]:
        s = filtered_stats(lambda m, c=corr: f"_corr{c}" in m["config"], None)
        if s:
            print(f"    corr>{corr}: n={s['n']:3d}  median_exp=${s['median_expectancy']:+.2f}  "
                  f"best=${s['best_expectancy']:+.2f}  median_WR={s['median_wr']:.1f}%")
    
    # Best config deep-dive
    if ranked:
        best = ranked[0]
        print("\n" + "=" * 75)
        print(f"  🏆 RECOMMENDED CONFIG: {best['config']}")
        print("=" * 75)
        print(f"\n  Total trades:         {best['n_trades']}")
        print(f"  Win rate:             {best['win_rate']:.1f}%")
        print(f"  Profit factor:        {best['profit_factor']:.2f}")
        print(f"  Expectancy per trade: ${best['expectancy_dollars']:+.2f} "
              f"({best['expectancy_ticks']:+.2f} ticks)")
        print(f"  Total P&L:            ${best['total_pnl_dollars']:+.2f}")
        print(f"  Max drawdown:         ${best['max_drawdown_dollars']:.2f}")
        print(f"  Avg bars held:        {best['avg_bars_held']:.1f}")
        print(f"  Avg win:              ${best['avg_win_dollars']:+.2f}")
        print(f"  Avg loss:             ${best['avg_loss_dollars']:+.2f}")
        rr = abs(best['avg_win_dollars'] / best['avg_loss_dollars']) if best['avg_loss_dollars'] else 0
        print(f"  Realized R:R:         1:{rr:.2f}")
        print(f"\n  MFE/MAE Analysis (informs target/stop placement):")
        print(f"    Avg MFE: {best['avg_mfe_ticks']:+.1f}t  (avg max-favorable move)")
        print(f"    Avg MAE: {best['avg_mae_ticks']:+.1f}t  (avg max-adverse move)")
        print(f"\n  Exit breakdown:")
        print(f"    Stops:      {best['stop_exits']:4d}  ({100*best['stop_exits']/best['n_trades']:.1f}%)")
        print(f"    Targets:    {best['target_exits']:4d}  ({100*best['target_exits']/best['n_trades']:.1f}%)")
        print(f"    Time exits: {best['time_exits']:4d}  ({100*best['time_exits']/best['n_trades']:.1f}%)")
    
    # Save CSVs
    print(f"\nWriting summary to {SUMMARY_CSV.name}...")
    if all_results:
        all_keys = sorted({k for r in all_results for k in r.keys()})
        with SUMMARY_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys)
            writer.writeheader()
            for r in all_results:
                writer.writerow(r)
    
    print(f"Writing {len(all_trades)} trades to {TRADES_CSV.name}...")
    if all_trades:
        for t in all_trades:
            t["entry_ts"] = t["entry_ts"].isoformat() if hasattr(t["entry_ts"], "isoformat") else str(t["entry_ts"])
            t["exit_ts"] = t["exit_ts"].isoformat() if hasattr(t["exit_ts"], "isoformat") else str(t["exit_ts"])
        all_keys = sorted({k for t in all_trades for k in t.keys()})
        with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys)
            writer.writeheader()
            for t in all_trades:
                writer.writerow(t)
    
    print("\n✓ Done.")


if __name__ == "__main__":
    main()
