"""
Phoenix Bot — Exit Methodology Comparator

Holds the ENTRY rule constant (strong ES/NQ confluence signal) and compares
multiple EXIT methodologies head-to-head on the same trade set.

Exits tested:
  1. Fixed stop + fixed target (baseline)
  2. ATR-based stop + ATR-based target (multiple multipliers)
  3. Chandelier exit (trailing stop = HHV - X × ATR)
  4. Signal flip exit (close when confluence inverts)
  5. Time-based exit (close after N bars)
  6. VWAP cross exit (close when price crosses VWAP)
  7. RSI overbought exit (close longs when RSI > 70)
  8. Hybrid: partial at fixed target + chandelier on runner
  9. Hybrid: initial fixed stop, trail after +1R achieved
  10. Volume climax exit (close on volume spike + adverse candle)

USAGE:
    python tools/exit_methodology_comparator.py
"""

from __future__ import annotations
import csv
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
HISTORICAL_DIR = PROJECT_ROOT / "data" / "historical"
OUTPUT_CSV = HISTORICAL_DIR / "exit_methodology_comparison.csv"
TRADES_CSV = HISTORICAL_DIR / "exit_methodology_trades.csv"

TICK_SIZE = 0.25
TICK_VALUE = 0.50
SLIPPAGE_TICKS = 0.5

# Fixed entry rule (held constant across all exit tests)
ENTRY_BOOST_THRESHOLD = 7
ENTRY_CORR_MIN = 0.85
ENTRY_NY_PM_ONLY = True
ENTRY_DIRECTION = "LONG"  # known to have strongest edge from prior grid search


# ──────────────────────────────────────────────────────────────────
# Data loading (same as v2 strategy backtester)
# ──────────────────────────────────────────────────────────────────

def find_last_file(prefix):
    matches = list(HISTORICAL_DIR.glob(f"{prefix}*.Last"))
    if not matches:
        return None
    matches.sort()
    return matches[-1]


def parse_last_file(path):
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
                volume = int(parts[2]) if len(parts) >= 3 else 1
            except (ValueError, IndexError):
                continue
            ts = None
            try:
                if " " in ts_str:
                    date_part, time_part = ts_str.split(" ", 1)
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
            yield (ts, price, volume)


def aggregate_to_ohlc(ticks_iter, bar_minutes=5):
    bars = []
    current_bucket_start = None
    bucket = None
    for ts, price, volume in ticks_iter:
        bucket_start = ts.replace(second=0, microsecond=0)
        minute_floor = (bucket_start.minute // bar_minutes) * bar_minutes
        bucket_start = bucket_start.replace(minute=minute_floor)
        if bucket_start != current_bucket_start:
            if bucket is not None:
                bars.append(bucket)
            current_bucket_start = bucket_start
            bucket = {
                "ts": bucket_start, "open": price, "high": price, "low": price,
                "close": price, "volume": volume, "tick_count": 1,
            }
        else:
            bucket["high"] = max(bucket["high"], price)
            bucket["low"] = min(bucket["low"], price)
            bucket["close"] = price
            bucket["volume"] += volume
            bucket["tick_count"] += 1
    if bucket is not None:
        bars.append(bucket)
    return bars


def pair_bars(nq_bars, es_bars):
    es_by_ts = {b["ts"]: b for b in es_bars}
    paired = []
    for nq_bar in nq_bars:
        ts = nq_bar["ts"]
        if ts in es_by_ts:
            paired.append({
                "ts": ts, "hour": ts.hour,
                "nq_open": nq_bar["open"], "nq_high": nq_bar["high"],
                "nq_low": nq_bar["low"], "nq_close": nq_bar["close"],
                "nq_volume": nq_bar["volume"],
                "es_close": es_by_ts[ts]["close"],
            })
    return paired


# ──────────────────────────────────────────────────────────────────
# Indicators
# ──────────────────────────────────────────────────────────────────

def add_atr(bars, period=14):
    """Average True Range."""
    if len(bars) < period + 1:
        return
    tr_list = [0]  # first bar has no prior
    for i in range(1, len(bars)):
        h_l = bars[i]["nq_high"] - bars[i]["nq_low"]
        h_pc = abs(bars[i]["nq_high"] - bars[i-1]["nq_close"])
        l_pc = abs(bars[i]["nq_low"] - bars[i-1]["nq_close"])
        tr_list.append(max(h_l, h_pc, l_pc))
    
    for i, bar in enumerate(bars):
        if i < period:
            bar["atr"] = 0
            bar["atr_ticks"] = 0
        else:
            window = tr_list[i - period + 1:i + 1]
            atr = sum(window) / period
            bar["atr"] = atr
            bar["atr_ticks"] = atr / TICK_SIZE


def add_rsi(bars, period=14):
    """Relative Strength Index."""
    if len(bars) < period + 1:
        return
    closes = [b["nq_close"] for b in bars]
    gains = [0]
    losses = [0]
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    
    for i, bar in enumerate(bars):
        if i < period:
            bar["rsi"] = 50
            continue
        avg_gain = sum(gains[i - period + 1:i + 1]) / period
        avg_loss = sum(losses[i - period + 1:i + 1]) / period
        if avg_loss == 0:
            bar["rsi"] = 100
        else:
            rs = avg_gain / avg_loss
            bar["rsi"] = 100 - (100 / (1 + rs))


def add_vwap(bars):
    """VWAP with daily reset."""
    cumulative_pv = 0
    cumulative_v = 0
    current_date = None
    for bar in bars:
        bar_date = bar["ts"].date()
        if bar_date != current_date:
            current_date = bar_date
            cumulative_pv = 0
            cumulative_v = 0
        typical = (bar["nq_high"] + bar["nq_low"] + bar["nq_close"]) / 3
        cumulative_pv += typical * bar["nq_volume"]
        cumulative_v += bar["nq_volume"]
        bar["vwap"] = cumulative_pv / cumulative_v if cumulative_v > 0 else bar["nq_close"]


def add_volume_avg(bars, period=20):
    """Rolling average volume."""
    for i, bar in enumerate(bars):
        if i < period:
            bar["volume_avg"] = bar["nq_volume"]
        else:
            window = [bars[j]["nq_volume"] for j in range(i - period + 1, i + 1)]
            bar["volume_avg"] = sum(window) / period
        bar["volume_ratio"] = bar["nq_volume"] / max(1, bar["volume_avg"])


# ──────────────────────────────────────────────────────────────────
# ES/NQ confluence (same as v2)
# ──────────────────────────────────────────────────────────────────

def pearson(x, y):
    n = len(x)
    if n < 2: return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((y[i] - my) ** 2 for i in range(n)))
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def add_confluence(bars, z_window=20, corr_window=60):
    if len(bars) < max(z_window, corr_window) + 5:
        return
    nq_returns = [0.0]
    es_returns = [0.0]
    for i in range(1, len(bars)):
        nq_returns.append((bars[i]["nq_close"] - bars[i-1]["nq_close"]) / bars[i-1]["nq_close"])
        es_returns.append((bars[i]["es_close"] - bars[i-1]["es_close"]) / bars[i-1]["es_close"])
    
    for i, bar in enumerate(bars):
        bar["spread_z"] = 0.0
        bar["correlation"] = 0.0
        bar["boost_long"] = 0
        bar["boost_short"] = 0
        if i < max(z_window, corr_window):
            continue
        spread_window = [nq_returns[j] - es_returns[j] for j in range(i - z_window, i + 1)]
        mean_s = sum(spread_window) / len(spread_window)
        var_s = sum((s - mean_s) ** 2 for s in spread_window) / max(1, len(spread_window) - 1)
        std_s = math.sqrt(var_s) if var_s > 0 else 1e-9
        current_spread = nq_returns[i] - es_returns[i]
        bar["spread_z"] = (current_spread - mean_s) / std_s
        bar["correlation"] = pearson(nq_returns[i - corr_window:i + 1],
                                      es_returns[i - corr_window:i + 1])
        if bar["correlation"] > 0.85:
            if bar["spread_z"] < -1.5: bar["boost_long"] += 5
            elif bar["spread_z"] < -1.0: bar["boost_long"] += 3
            elif bar["spread_z"] < -0.5: bar["boost_long"] += 1
            if bar["spread_z"] > 1.5: bar["boost_short"] += 5
            elif bar["spread_z"] > 1.0: bar["boost_short"] += 3
            elif bar["spread_z"] > 0.5: bar["boost_short"] += 1


def is_ny_pm(bar):
    return 13 <= bar["hour"] <= 14


def signal_fires(bar):
    """Fixed entry rule."""
    if bar["correlation"] < ENTRY_CORR_MIN:
        return False
    if ENTRY_NY_PM_ONLY and not is_ny_pm(bar):
        return False
    if ENTRY_DIRECTION == "LONG":
        return bar["boost_long"] >= ENTRY_BOOST_THRESHOLD
    return bar["boost_short"] >= ENTRY_BOOST_THRESHOLD


# ──────────────────────────────────────────────────────────────────
# Exit methodologies
# ──────────────────────────────────────────────────────────────────

class ExitRule:
    """Base — subclasses implement check_exit."""
    name = "base"
    
    def __init__(self):
        self.state = {}  # per-trade state if needed
    
    def init_trade(self, position, bars, entry_idx):
        """Called when a position opens. Set initial stop/target."""
        pass
    
    def check_exit(self, position, bars, i):
        """Return (exit_price, reason) or None."""
        return None


class FixedStopTarget(ExitRule):
    def __init__(self, stop_ticks, target_ticks):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.target_ticks = target_ticks
        self.name = f"Fixed_stop{stop_ticks}_tgt{target_ticks}"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + self.target_ticks * TICK_SIZE
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        if bar["nq_high"] >= position["target_price"]:
            return (position["target_price"], "target")
        return None


class ATRStopTarget(ExitRule):
    def __init__(self, stop_mult, target_mult):
        super().__init__()
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.name = f"ATR_stop{stop_mult}x_tgt{target_mult}x"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        atr = bars[entry_idx]["atr"]
        if atr <= 0:
            atr = 4 * TICK_SIZE  # fallback for warmup
        position["stop_price"] = entry - self.stop_mult * atr
        position["target_price"] = entry + self.target_mult * atr
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        if bar["nq_high"] >= position["target_price"]:
            return (position["target_price"], "target")
        return None


class ChandelierExit(ExitRule):
    def __init__(self, lookback_bars, atr_mult, initial_stop_ticks=20):
        super().__init__()
        self.lookback = lookback_bars
        self.atr_mult = atr_mult
        self.initial_stop_ticks = initial_stop_ticks
        self.name = f"Chandelier_LB{lookback_bars}_mult{atr_mult}"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        # Initial stop is fixed; trail kicks in as price moves up
        position["stop_price"] = entry - self.initial_stop_ticks * TICK_SIZE
        position["target_price"] = entry + 1000 * TICK_SIZE  # very far — chandelier does the work
        position["highest_high"] = bars[entry_idx]["nq_high"]
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        # Update highest high since entry
        entry_idx = position["entry_idx"]
        lookback_start = max(entry_idx, i - self.lookback)
        position["highest_high"] = max(bars[j]["nq_high"]
                                       for j in range(lookback_start, i + 1))
        # Compute trailing stop
        atr = bars[i]["atr"]
        if atr <= 0:
            atr = 4 * TICK_SIZE
        candidate_stop = position["highest_high"] - self.atr_mult * atr
        # One-way ratchet
        position["stop_price"] = max(position["stop_price"], candidate_stop)
        # Check stop hit
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "chandelier_stop")
        return None


class SignalFlipExit(ExitRule):
    def __init__(self, stop_ticks, exit_boost_threshold):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.exit_boost_threshold = exit_boost_threshold  # exit if boost drops below this
        self.name = f"SignalFlip_stop{stop_ticks}_exit_thr{exit_boost_threshold}"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + 1000 * TICK_SIZE  # signal exit does the work
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        # Signal flip check: boost dropped below threshold OR opposite signal fires
        if bar["boost_long"] < self.exit_boost_threshold or bar["boost_short"] >= 5:
            return (bar["nq_close"], "signal_flip")
        return None


class TimeExit(ExitRule):
    def __init__(self, stop_ticks, target_ticks, max_bars):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.target_ticks = target_ticks
        self.max_bars = max_bars
        self.name = f"Time_stop{stop_ticks}_tgt{target_ticks}_max{max_bars}b"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + self.target_ticks * TICK_SIZE
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        if bar["nq_high"] >= position["target_price"]:
            return (position["target_price"], "target")
        if i - position["entry_idx"] >= self.max_bars:
            return (bar["nq_close"], "time")
        return None


class VWAPCrossExit(ExitRule):
    def __init__(self, stop_ticks):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.name = f"VWAPCross_stop{stop_ticks}"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + 1000 * TICK_SIZE
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        # Exit when close drops below VWAP (for longs)
        if i > position["entry_idx"] and bar["nq_close"] < bar.get("vwap", bar["nq_close"]):
            return (bar["nq_close"], "vwap_cross")
        return None


class RSIExit(ExitRule):
    def __init__(self, stop_ticks, rsi_threshold):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.rsi_threshold = rsi_threshold
        self.name = f"RSI_stop{stop_ticks}_thr{rsi_threshold}"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + 1000 * TICK_SIZE
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        # Exit when RSI overbought (longs)
        if i > position["entry_idx"] and bar.get("rsi", 50) > self.rsi_threshold:
            return (bar["nq_close"], "rsi_overbought")
        return None


class PartialThenChandelier(ExitRule):
    """50% out at fixed target, runner trails with chandelier."""
    def __init__(self, stop_ticks, partial_target_ticks, chandelier_lookback, chandelier_mult):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.partial_target_ticks = partial_target_ticks
        self.chandelier_lookback = chandelier_lookback
        self.chandelier_mult = chandelier_mult
        self.name = (f"PartialChandelier_stop{stop_ticks}_partial{partial_target_ticks}_"
                    f"LB{chandelier_lookback}_mult{chandelier_mult}")
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + self.partial_target_ticks * TICK_SIZE
        position["partial_taken"] = False
        position["partial_pnl"] = 0
        position["highest_high"] = bars[entry_idx]["nq_high"]
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        # Stage 1: have we taken partial yet?
        if not position["partial_taken"]:
            if bar["nq_low"] <= position["stop_price"]:
                return (position["stop_price"], "stop")
            if bar["nq_high"] >= position["target_price"]:
                # Take partial — record 50% P&L, switch to chandelier on remainder
                partial_pnl_ticks = (position["target_price"] - position["entry_price"]) / TICK_SIZE
                position["partial_pnl"] = partial_pnl_ticks * 0.5  # 50% size
                position["partial_taken"] = True
                # Move stop to BE
                position["stop_price"] = position["entry_price"]
                return None
        else:
            # Stage 2: chandelier trail on runner
            entry_idx = position["entry_idx"]
            lookback_start = max(entry_idx, i - self.chandelier_lookback)
            position["highest_high"] = max(bars[j]["nq_high"]
                                          for j in range(lookback_start, i + 1))
            atr = bars[i]["atr"]
            if atr <= 0: atr = 4 * TICK_SIZE
            candidate_stop = position["highest_high"] - self.chandelier_mult * atr
            position["stop_price"] = max(position["stop_price"], candidate_stop)
            if bar["nq_low"] <= position["stop_price"]:
                # Exit runner
                runner_pnl_ticks = (position["stop_price"] - position["entry_price"]) / TICK_SIZE
                # Combine partial + runner — return blended exit price
                blended_ticks = position["partial_pnl"] + (runner_pnl_ticks * 0.5)
                blended_exit_price = position["entry_price"] + blended_ticks * TICK_SIZE
                return (blended_exit_price, "partial_chandelier_complete")
        return None


class TrailAfter1R(ExitRule):
    """Initial fixed stop. Once +1R achieved, move to BE then trail by ATR."""
    def __init__(self, stop_ticks, atr_trail_mult, max_target_ticks=64):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.atr_trail_mult = atr_trail_mult
        self.max_target_ticks = max_target_ticks
        self.name = f"TrailAfter1R_stop{stop_ticks}_trail{atr_trail_mult}x"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + self.max_target_ticks * TICK_SIZE
        position["one_r_hit"] = False
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        # Stop check
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        # Hard target
        if bar["nq_high"] >= position["target_price"]:
            return (position["target_price"], "max_target")
        # +1R check (1R = stop_ticks of profit)
        entry = position["entry_price"]
        if not position["one_r_hit"]:
            if bar["nq_high"] >= entry + self.stop_ticks * TICK_SIZE:
                position["one_r_hit"] = True
                # Move stop to BE
                position["stop_price"] = entry
        else:
            # Trail by ATR
            atr = bars[i]["atr"]
            if atr <= 0: atr = 4 * TICK_SIZE
            candidate_stop = bar["nq_high"] - self.atr_trail_mult * atr
            position["stop_price"] = max(position["stop_price"], candidate_stop)
        return None


class VolumeClimaxExit(ExitRule):
    """Exit on volume spike + adverse candle (climax reversal)."""
    def __init__(self, stop_ticks, target_ticks, volume_ratio_threshold=3.0):
        super().__init__()
        self.stop_ticks = stop_ticks
        self.target_ticks = target_ticks
        self.volume_ratio_threshold = volume_ratio_threshold
        self.name = f"VolClimax_stop{stop_ticks}_tgt{target_ticks}_volX{volume_ratio_threshold}"
    
    def init_trade(self, position, bars, entry_idx):
        entry = position["entry_price"]
        position["stop_price"] = entry - self.stop_ticks * TICK_SIZE
        position["target_price"] = entry + self.target_ticks * TICK_SIZE
    
    def check_exit(self, position, bars, i):
        bar = bars[i]
        if bar["nq_low"] <= position["stop_price"]:
            return (position["stop_price"], "stop")
        if bar["nq_high"] >= position["target_price"]:
            return (position["target_price"], "target")
        # Climax exit: volume spike + bearish candle (close < open)
        if (i > position["entry_idx"]
            and bar.get("volume_ratio", 1) > self.volume_ratio_threshold
            and bar["nq_close"] < bar["nq_open"]):
            return (bar["nq_close"], "volume_climax")
        return None


# ──────────────────────────────────────────────────────────────────
# Backtest runner
# ──────────────────────────────────────────────────────────────────

def simulate_with_exit(bars, exit_rule):
    trades = []
    open_position = None
    i = 0
    n = len(bars)
    
    while i < n - 1:
        row = bars[i]
        
        if open_position:
            exit_result = exit_rule.check_exit(open_position, bars, i)
            if exit_result:
                exit_price, exit_reason = exit_result
                close_trade(open_position, exit_price, exit_reason,
                          row["ts"], i - open_position["entry_idx"])
                trades.append(open_position)
                open_position = None
                i += 1
                continue
            i += 1
            continue
        
        if signal_fires(row) and i + 1 < n:
            entry_price = bars[i + 1]["nq_open"] + SLIPPAGE_TICKS * TICK_SIZE
            open_position = {
                "entry_idx": i + 1,
                "entry_ts": bars[i + 1]["ts"],
                "entry_price": entry_price,
                "direction": "LONG",
                "mfe_ticks": 0,
                "mae_ticks": 0,
            }
            exit_rule.init_trade(open_position, bars, i + 1)
            i = i + 2
            continue
        i += 1
    
    if open_position:
        last_bar = bars[n - 1]
        close_trade(open_position, last_bar["nq_close"], "eod",
                  last_bar["ts"], n - 1 - open_position["entry_idx"])
        trades.append(open_position)
    
    return trades


def close_trade(position, exit_price, exit_reason, exit_ts, bars_held):
    pnl_ticks = (exit_price - position["entry_price"]) / TICK_SIZE
    position["exit_price"] = exit_price
    position["exit_reason"] = exit_reason
    position["exit_ts"] = exit_ts
    position["bars_held"] = bars_held
    position["pnl_ticks"] = pnl_ticks
    position["pnl_dollars"] = pnl_ticks * TICK_VALUE


def compute_metrics(trades, name):
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["pnl_dollars"] > 0]
    losses = [t for t in trades if t["pnl_dollars"] < 0]
    total_win = sum(t["pnl_dollars"] for t in wins)
    total_loss = abs(sum(t["pnl_dollars"] for t in losses))
    total_pnl = sum(t["pnl_dollars"] for t in trades)
    
    running, peak, max_dd = 0, 0, 0
    for t in trades:
        running += t["pnl_dollars"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    
    return {
        "exit_rule": name,
        "n_trades": n,
        "win_rate": 100 * len(wins) / n if n else 0,
        "avg_win_dollars": total_win / len(wins) if wins else 0,
        "avg_loss_dollars": -total_loss / len(losses) if losses else 0,
        "profit_factor": total_win / total_loss if total_loss > 0 else float("inf"),
        "expectancy_dollars": total_pnl / n,
        "expectancy_ticks": sum(t["pnl_ticks"] for t in trades) / n,
        "total_pnl_dollars": total_pnl,
        "max_drawdown_dollars": max_dd,
        "avg_bars_held": statistics.mean([t["bars_held"] for t in trades]),
    }


# ──────────────────────────────────────────────────────────────────
# Configuration generator
# ──────────────────────────────────────────────────────────────────

def all_exit_rules():
    rules = []
    
    # Baseline: fixed stop+target
    for stop, tgt in [(12, 24), (16, 32), (20, 40), (16, 48), (12, 36)]:
        rules.append(FixedStopTarget(stop, tgt))
    
    # ATR family
    for stop_m, tgt_m in [(1.0, 2.0), (1.5, 2.0), (1.5, 3.0), (2.0, 3.0), (2.0, 4.0)]:
        rules.append(ATRStopTarget(stop_m, tgt_m))
    
    # Chandelier
    for lookback, mult in [(10, 2.5), (10, 3.0), (15, 2.5), (22, 3.0), (22, 2.5)]:
        rules.append(ChandelierExit(lookback, mult, initial_stop_ticks=20))
    
    # Signal flip
    for stop, thr in [(16, 0), (16, 3), (20, 0), (20, 3)]:
        rules.append(SignalFlipExit(stop, thr))
    
    # Time exit
    for stop, tgt, max_b in [(16, 32, 10), (16, 32, 15), (20, 48, 20)]:
        rules.append(TimeExit(stop, tgt, max_b))
    
    # VWAP cross
    for stop in [16, 20, 24]:
        rules.append(VWAPCrossExit(stop))
    
    # RSI overbought
    for stop, rsi_thr in [(16, 70), (20, 70), (16, 75)]:
        rules.append(RSIExit(stop, rsi_thr))
    
    # Hybrid: partial + chandelier
    for stop, partial, lb, mult in [
        (16, 24, 10, 2.5),
        (16, 32, 15, 3.0),
        (20, 32, 22, 3.0),
    ]:
        rules.append(PartialThenChandelier(stop, partial, lb, mult))
    
    # Trail after +1R
    for stop, trail_m in [(16, 1.5), (16, 2.0), (20, 1.5), (20, 2.0)]:
        rules.append(TrailAfter1R(stop, trail_m))
    
    # Volume climax
    for stop, tgt, vol_x in [(16, 32, 3.0), (20, 40, 2.5)]:
        rules.append(VolumeClimaxExit(stop, tgt, vol_x))
    
    return rules


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 75)
    print("Phoenix — Exit Methodology Comparator")
    print("=" * 75)
    print(f"\nEntry rule held constant:")
    print(f"  Direction: {ENTRY_DIRECTION}")
    print(f"  boost_{ENTRY_DIRECTION.lower()} >= {ENTRY_BOOST_THRESHOLD}")
    print(f"  Correlation >= {ENTRY_CORR_MIN}")
    print(f"  NY PM only: {ENTRY_NY_PM_ONLY}")
    
    nq_path = find_last_file("MNQ")
    es_path = find_last_file("MES")
    if not nq_path or not es_path:
        print(f"\n❌ Missing .Last files in {HISTORICAL_DIR}")
        return
    
    print(f"\nParsing {nq_path.name}...")
    nq_bars = aggregate_to_ohlc(parse_last_file(nq_path), bar_minutes=5)
    print(f"  {len(nq_bars):,} NQ bars")
    print(f"Parsing {es_path.name}...")
    es_bars = aggregate_to_ohlc(parse_last_file(es_path), bar_minutes=5)
    print(f"  {len(es_bars):,} ES bars")
    
    bars = pair_bars(nq_bars, es_bars)
    print(f"  {len(bars):,} paired bars")
    
    print("Computing indicators (ATR, RSI, VWAP, volume avg)...")
    add_atr(bars)
    add_rsi(bars)
    add_vwap(bars)
    add_volume_avg(bars)
    
    print("Computing ES/NQ confluence...")
    add_confluence(bars)
    
    # Count entry signals
    n_signals = sum(1 for b in bars if signal_fires(b))
    print(f"\n  → {n_signals} entry signals will fire under the fixed rule")
    
    rules = all_exit_rules()
    print(f"\nTesting {len(rules)} exit methodologies on the same entry set...\n")
    
    all_results = []
    all_trades = []
    
    for rule in rules:
        trades = simulate_with_exit(bars, rule)
        if len(trades) < 10:
            print(f"  ⚠ skipped {rule.name} (only {len(trades)} trades)")
            continue
        metrics = compute_metrics(trades, rule.name)
        if metrics:
            all_results.append(metrics)
            for t in trades:
                t["exit_rule"] = rule.name
            all_trades.extend(trades)
    
    # Rank by expectancy
    ranked = sorted(all_results, key=lambda r: r["expectancy_dollars"], reverse=True)
    
    print("\n" + "=" * 75)
    print(f"  EXIT METHODOLOGY RANKING — {len(ranked)} exit rules tested")
    print("=" * 75)
    
    for i, m in enumerate(ranked, 1):
        print(f"\n  [{i:2d}] {m['exit_rule']}")
        print(f"        trades: {m['n_trades']:4d}  WR: {m['win_rate']:5.1f}%  "
              f"PF: {m['profit_factor']:5.2f}  "
              f"expectancy: ${m['expectancy_dollars']:+7.2f}/trade")
        print(f"        total P&L: ${m['total_pnl_dollars']:+8.2f}  "
              f"max DD: ${m['max_drawdown_dollars']:.2f}  "
              f"avg hold: {m['avg_bars_held']:.1f} bars  "
              f"avg win: ${m['avg_win_dollars']:+.2f}  avg loss: ${m['avg_loss_dollars']:+.2f}")
    
    # Grouped comparison
    print("\n" + "=" * 75)
    print("  GROUPED COMPARISON — best per family")
    print("=" * 75)
    
    families = {
        "Fixed stop/target": "Fixed_",
        "ATR-based": "ATR_",
        "Chandelier trail": "Chandelier_",
        "Signal flip": "SignalFlip_",
        "Time-based": "Time_",
        "VWAP cross": "VWAPCross_",
        "RSI overbought": "RSI_",
        "Partial + chandelier hybrid": "PartialChandelier_",
        "Trail after +1R": "TrailAfter1R_",
        "Volume climax": "VolClimax_",
    }
    
    for family_name, prefix in families.items():
        family_results = [r for r in all_results if r["exit_rule"].startswith(prefix)]
        if not family_results:
            continue
        best_in_family = max(family_results, key=lambda r: r["expectancy_dollars"])
        print(f"\n  {family_name}:")
        print(f"    best: {best_in_family['exit_rule']}")
        print(f"      expectancy: ${best_in_family['expectancy_dollars']:+.2f}/trade  "
              f"WR: {best_in_family['win_rate']:.1f}%  "
              f"PF: {best_in_family['profit_factor']:.2f}")
    
    # Winner deep-dive
    if ranked:
        winner = ranked[0]
        print("\n" + "=" * 75)
        print(f"  🏆 WINNING EXIT METHODOLOGY: {winner['exit_rule']}")
        print("=" * 75)
        print(f"\n  Across {winner['n_trades']} trades:")
        print(f"    Win rate:             {winner['win_rate']:.1f}%")
        print(f"    Profit factor:        {winner['profit_factor']:.2f}")
        print(f"    Expectancy per trade: ${winner['expectancy_dollars']:+.2f} "
              f"({winner['expectancy_ticks']:+.2f} ticks)")
        print(f"    Total P&L:            ${winner['total_pnl_dollars']:+.2f}")
        print(f"    Max drawdown:         ${winner['max_drawdown_dollars']:.2f}")
        print(f"    Avg bars held:        {winner['avg_bars_held']:.1f}")
        print(f"    Avg win:              ${winner['avg_win_dollars']:+.2f}")
        print(f"    Avg loss:             ${winner['avg_loss_dollars']:+.2f}")
    
    # Save CSVs
    print(f"\nWriting results to {OUTPUT_CSV.name}...")
    if all_results:
        all_keys = sorted({k for r in all_results for k in r.keys()})
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
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
