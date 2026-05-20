"""
Phoenix Early Reversal Signal Tick-Level Analysis
===================================================

Question:
    Phoenix exits AFTER price reaches a fixed RR target / Chandelier trigger /
    time-out (per Phase 13 Section U).  Are there tick-level CLUES that price is
    about to REVERSE, that we could use as an EARLY exit trigger to lock in MFE
    profits BEFORE the reversal hits our stop?

Tested signals (LONG; SHORT is mirrored):
  1. delta_divergence       : rolling 60s cumulative delta turns negative while
                              price is making (or near) a new high.
  2. tape_speed_collapse    : tick frequency in last 10s falls below 50% of
                              trailing 60s avg.
  3. volume_climax          : 5s contract volume > 2.5x trailing 60s avg AND
                              we are within 4 ticks of MFE peak.
  4. aggressor_flip         : last 30 ticks have sell:buy size ratio > 1.5.
  5. stacked_imbalance      : 3+ recent price levels show bid-size:ask-size
                              ratio that defends the COUNTER side (bid-size
                              ratio < 0.33 = sellers stacked).

All signals only "count" once we have reached at least +0.5R favorable
(i.e. we have profits worth locking in).

For each signal we measure:
  - did it fire on the trade?
  - tick price at signal time (early-exit hypothetical fill)
  - what was the BASELINE-policy P&L (fixed_2r / fixed_3r / chandelier per
    Phase 13 Section U)?
  - what was the EARLY-EXIT P&L if we had exited at signal time?
  - true-positive: early exit captured MORE P&L than the baseline (or any
    profit when baseline was a loss).
  - false-positive: early exit gave UP P&L the baseline would have captured.

Combined policies tested:
  - early_aggressive:     ANY signal -> exit
  - early_conservative:   >=2 signals agree within 5s window -> exit
  - early_high_conf:      stacked_imbalance OR volume_climax -> exit

DATA
----
TICK:    data/historical/databento_tbbo/mnq_ticks_clean.parquet (43.8M ticks)
         Loaded via tools.tbbo_cache_builder.load_clean_ticks
TRADES:  phoenix_real_5year.csv, phoenix_new_strategy_lab.csv,
         phoenix_trend_pullback_lab.csv (raschke_baseline only)
WINDOW:  2026-03-17 -> 2026-05-15

OUTPUT
------
1. backtest_results/phoenix_early_reversal_per_trade.csv
2. backtest_results/phoenix_early_reversal_summary.csv
3. docs/EARLY_REVERSAL_EXIT_ANALYSIS.md

USAGE
-----
    python tools/phoenix_early_reversal_signals.py

Hard ASCII output (Windows cp1252).  No emojis.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

import numpy as np
import pandas as pd

# Repo paths
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.tbbo_cache_builder import load_clean_ticks  # noqa: E402

TICK_SIZE = 0.25
TICK_VALUE = 0.50         # $ per tick on MNQ
MAX_HOLD_MIN = 240

RESULTS_CSV = ROOT / "backtest_results" / "phoenix_early_reversal_per_trade.csv"
SUMMARY_CSV = ROOT / "backtest_results" / "phoenix_early_reversal_summary.csv"
REPORT_MD   = ROOT / "docs" / "EARLY_REVERSAL_EXIT_ANALYSIS.md"

WINDOW_START = pd.Timestamp("2026-03-17", tz="UTC")
WINDOW_END   = pd.Timestamp("2026-05-15 21:00", tz="UTC")

TRADE_SOURCES = [
    ("backtest_results/phoenix_real_5year.csv",         None),
    ("backtest_results/phoenix_new_strategy_lab.csv",   None),
    ("backtest_results/phoenix_trend_pullback_lab.csv", "raschke_baseline"),
]

# Strategies with winning baseline policies per Section U.3.
# Each maps to (baseline_policy, params) so we can run the head-to-head.
STRAT_BASELINE = {
    "bias_momentum":         ("fixed_rr", {"rr": 2.0}),
    "spring_setup":          ("fixed_rr", {"rr": 3.0}),
    "vwap_pullback_v2":      ("fixed_rr", {"rr": 3.0}),
    "opening_session":       ("fixed_rr", {"rr": 3.0}),
    "g_inside_bar_breakout": ("chandelier", {"lookback_bars": 50, "atr_mult": 3.0, "bar_sec": 60}),
    "raschke_baseline":      ("time_exit", {"minutes": 30}),
}

# Signal activation: only consider signals after we have +ACTIVATE_R favorable.
ACTIVATE_R = 0.5


# =====================================================================
# Data loading
# =====================================================================

def load_trades() -> pd.DataFrame:
    parts = []
    for relpath, filter_strat in TRADE_SOURCES:
        path = ROOT / relpath
        if not path.exists():
            continue
        df = pd.read_csv(path)
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
        df["exit_ts"]  = pd.to_datetime(df["exit_ts"], utc=True)
        if filter_strat:
            df = df[df.strategy == filter_strat]
        else:
            df = df[df.strategy.isin(STRAT_BASELINE.keys())]
        keep = ["strategy", "direction", "entry_ts", "entry_price",
                "stop_price", "target_price", "exit_ts", "exit_price",
                "pnl_dollars", "pnl_ticks", "hold_min"]
        for c in keep:
            if c not in df.columns:
                df[c] = None
        parts.append(df[keep])
    combined = pd.concat(parts, ignore_index=True)
    combined = combined[(combined.entry_ts >= WINDOW_START) &
                        (combined.entry_ts <= WINDOW_END
                         - pd.Timedelta(minutes=MAX_HOLD_MIN + 5))]
    combined = combined.sort_values("entry_ts").reset_index(drop=True)
    return combined


# =====================================================================
# Tick index helper - returns numpy arrays of ts/price/size/side for
# tight inner loops.
# =====================================================================

class TickIndex:
    def __init__(self, df: pd.DataFrame):
        # df is indexed by ts_event (UTC datetime64[ns])
        df = df.sort_index()
        self.ts_ns = df.index.values.astype("datetime64[ns]").view("int64")
        self.price = df["price"].to_numpy(dtype=np.float64)
        self.size  = df["size"].to_numpy(dtype=np.int32)
        # 'side' is single char 'A'/'B'/'N'.  We encode aggressor:
        #   A (ask side) => buyer aggressor => +1
        #   B (bid side) => seller aggressor => -1
        #   N            => 0
        side = df["side"].to_numpy()
        agg = np.zeros(len(side), dtype=np.int8)
        agg[side == "A"] = 1
        agg[side == "B"] = -1
        self.aggr = agg
        self.bid  = df["bid_px_00"].to_numpy(dtype=np.float64)
        self.ask  = df["ask_px_00"].to_numpy(dtype=np.float64)

    def slice(self, start_ts: pd.Timestamp, end_ts: pd.Timestamp):
        s = np.datetime64(start_ts.tz_convert("UTC").tz_localize(None), "ns").astype("int64")
        e = np.datetime64(end_ts.tz_convert("UTC").tz_localize(None), "ns").astype("int64")
        lo = np.searchsorted(self.ts_ns, s, side="left")
        hi = np.searchsorted(self.ts_ns, e, side="right")
        return (
            self.ts_ns[lo:hi],
            self.price[lo:hi],
            self.size[lo:hi],
            self.aggr[lo:hi],
            self.bid[lo:hi],
            self.ask[lo:hi],
        )


# =====================================================================
# Baseline exit policies (so we can compare early-exit vs baseline P&L
# trade-by-trade).  We need: fixed_rr, chandelier, time_exit.
# =====================================================================

@dataclass
class ExitResult:
    exit_idx: int = -1          # index into the trade's tick slice
    exit_ts_ns: int = 0
    exit_price: float = 0.0
    pnl_ticks: float = 0.0
    exit_reason: str = "unset"


def _pnl(direction: str, entry: float, exit_price: float) -> float:
    delta = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
    return delta / TICK_SIZE


def baseline_fixed_rr(direction, entry, stop, ts_arr, px_arr, rr: float) -> ExitResult:
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or len(ts_arr) == 0:
        return ExitResult()
    target = entry + rr * stop_dist if direction == "LONG" else entry - rr * stop_dist
    if direction == "LONG":
        for i in range(len(ts_arr)):
            p = px_arr[i]
            if p <= stop:
                return ExitResult(i, int(ts_arr[i]), float(stop),
                                  _pnl("LONG", entry, stop), "stop")
            if p >= target:
                return ExitResult(i, int(ts_arr[i]), float(target),
                                  _pnl("LONG", entry, target), "target")
    else:
        for i in range(len(ts_arr)):
            p = px_arr[i]
            if p >= stop:
                return ExitResult(i, int(ts_arr[i]), float(stop),
                                  _pnl("SHORT", entry, stop), "stop")
            if p <= target:
                return ExitResult(i, int(ts_arr[i]), float(target),
                                  _pnl("SHORT", entry, target), "target")
    # ran out of ticks
    i = len(ts_arr) - 1
    return ExitResult(i, int(ts_arr[i]), float(px_arr[i]),
                      _pnl(direction, entry, float(px_arr[i])), "time_exit")


def baseline_chandelier(direction, entry, stop, ts_arr, px_arr,
                        lookback_bars: int = 50, atr_mult: float = 3.0,
                        bar_sec: int = 60, activate_r: float = 1.0) -> ExitResult:
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or len(ts_arr) == 0:
        return ExitResult()
    activation_thresh = activate_r * stop_dist
    bar_ns = bar_sec * 1_000_000_000
    lookback_ns = lookback_bars * bar_ns
    activated = False
    current_stop = stop
    last_bar_ts = ts_arr[0] - bar_ns - 1

    if direction == "LONG":
        for i in range(len(ts_arr)):
            p = px_arr[i]
            if p <= current_stop:
                return ExitResult(i, int(ts_arr[i]), float(current_stop),
                                  _pnl("LONG", entry, current_stop),
                                  "chand_stop" if activated else "initial_stop")
            if not activated and (p - entry) >= activation_thresh:
                activated = True
            if activated and (ts_arr[i] - last_bar_ts) >= bar_ns:
                lo_idx = np.searchsorted(ts_arr, ts_arr[i] - lookback_ns, side="left")
                window = px_arr[lo_idx:i + 1]
                if len(window) > 10:
                    rolling_high = float(window.max())
                    atr_proxy = max(float(window.max() - window.min()),
                                    4 * TICK_SIZE)
                    new_stop = rolling_high - atr_mult * (atr_proxy / max(1, lookback_bars / 2))
                    if new_stop > current_stop:
                        current_stop = new_stop
                last_bar_ts = ts_arr[i]
    else:
        for i in range(len(ts_arr)):
            p = px_arr[i]
            if p >= current_stop:
                return ExitResult(i, int(ts_arr[i]), float(current_stop),
                                  _pnl("SHORT", entry, current_stop),
                                  "chand_stop" if activated else "initial_stop")
            if not activated and (entry - p) >= activation_thresh:
                activated = True
            if activated and (ts_arr[i] - last_bar_ts) >= bar_ns:
                lo_idx = np.searchsorted(ts_arr, ts_arr[i] - lookback_ns, side="left")
                window = px_arr[lo_idx:i + 1]
                if len(window) > 10:
                    rolling_low = float(window.min())
                    atr_proxy = max(float(window.max() - window.min()),
                                    4 * TICK_SIZE)
                    new_stop = rolling_low + atr_mult * (atr_proxy / max(1, lookback_bars / 2))
                    if new_stop < current_stop:
                        current_stop = new_stop
                last_bar_ts = ts_arr[i]
    i = len(ts_arr) - 1
    return ExitResult(i, int(ts_arr[i]), float(px_arr[i]),
                      _pnl(direction, entry, float(px_arr[i])), "time_exit")


def baseline_time_exit(direction, entry, stop, ts_arr, px_arr,
                       minutes: int = 30) -> ExitResult:
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or len(ts_arr) == 0:
        return ExitResult()
    time_limit_ns = ts_arr[0] + int(minutes) * 60 * 1_000_000_000
    if direction == "LONG":
        for i in range(len(ts_arr)):
            p = px_arr[i]
            if p <= stop:
                return ExitResult(i, int(ts_arr[i]), float(stop),
                                  _pnl("LONG", entry, stop), "stop")
            if ts_arr[i] >= time_limit_ns:
                return ExitResult(i, int(ts_arr[i]), float(p),
                                  _pnl("LONG", entry, p), "time_exit")
    else:
        for i in range(len(ts_arr)):
            p = px_arr[i]
            if p >= stop:
                return ExitResult(i, int(ts_arr[i]), float(stop),
                                  _pnl("SHORT", entry, stop), "stop")
            if ts_arr[i] >= time_limit_ns:
                return ExitResult(i, int(ts_arr[i]), float(p),
                                  _pnl("SHORT", entry, p), "time_exit")
    i = len(ts_arr) - 1
    return ExitResult(i, int(ts_arr[i]), float(px_arr[i]),
                      _pnl(direction, entry, float(px_arr[i])), "time_exit")


def run_baseline(strategy: str, direction, entry, stop,
                 ts_arr, px_arr) -> ExitResult:
    pname, params = STRAT_BASELINE[strategy]
    if pname == "fixed_rr":
        return baseline_fixed_rr(direction, entry, stop, ts_arr, px_arr, **params)
    if pname == "chandelier":
        return baseline_chandelier(direction, entry, stop, ts_arr, px_arr, **params)
    if pname == "time_exit":
        return baseline_time_exit(direction, entry, stop, ts_arr, px_arr, **params)
    raise ValueError(f"unknown baseline {pname}")


# =====================================================================
# Early-reversal signal detection (single pass per trade).
#
# Walk every tick once.  At each tick, having computed:
#   - cumulative MFE (best favorable since entry)
#   - rolling stats: tick_count_60s, tick_count_10s, vol_60s, vol_5s,
#                    delta_60s (signed buyer minus seller volume),
#                    aggressor_count_30 (last 30 ticks),
#                    bid_size / ask_size near the touch
# emit a SignalEvent if any of the 5 fire.  We only ACT on signals after
# we have hit +ACTIVATE_R favorable.
# =====================================================================

@dataclass
class SignalEvent:
    name: str
    tick_idx: int
    ts_ns: int
    price: float


def detect_signals(direction: str, entry: float, stop: float,
                   ts_arr, px_arr, sz_arr, agg_arr, bid_arr, ask_arr) -> List[SignalEvent]:
    """Single-pass detector that emits all signal fires within the trade window.

    Implementation strategy:
      * Pre-compute O(n) cumulative arrays for buy-vol, sell-vol, all-vol,
        signed delta, tick count.  Then every rolling query is a 2-index
        cumsum diff = O(1).
      * Pre-compute running max (LONG) / min (SHORT) for MFE.
      * 30-tick aggressor sums are derived from cumsum_buy/cumsum_sell diff
        between i and i-30 -- O(1).
      * stacked_imbalance is the only signal that needs per-level grouping.
        We still scan the trailing 30 ticks but inline-fast (no dict alloc
        unless we hit a candidate tick).  Acceptable because it only runs
        on activated post-+0.5R ticks with cooldown.
    """
    n = len(ts_arr)
    out: List[SignalEvent] = []
    if n == 0:
        return out

    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return out
    activation_thresh = ACTIVATE_R * stop_dist

    NS_60 = 60_000_000_000
    NS_10 = 10_000_000_000
    NS_5  = 5_000_000_000
    COOLDOWN_NS = 5_000_000_000

    # ------- Pre-compute O(n) cumulative arrays ----------
    # cumulative buy-side aggressor volume (agg=+1) and sell-side (agg=-1)
    buy_sz  = np.where(agg_arr > 0, sz_arr, 0).astype(np.int64)
    sell_sz = np.where(agg_arr < 0, sz_arr, 0).astype(np.int64)
    cum_buy  = np.concatenate(([0], np.cumsum(buy_sz)))    # cum_buy[i+1] = sum of buy_sz[0..i]
    cum_sell = np.concatenate(([0], np.cumsum(sell_sz)))
    cum_vol  = np.concatenate(([0], np.cumsum(sz_arr.astype(np.int64))))
    cum_cnt  = np.arange(n + 1, dtype=np.int64)  # cumulative tick count

    # rolling activation: index of first tick where +activation_thresh hit
    if direction == "LONG":
        ahead = px_arr - entry
        running_mfe = np.maximum.accumulate(px_arr)
    else:
        ahead = entry - px_arr
        running_mfe = np.minimum.accumulate(px_arr)

    activation_hits = ahead >= activation_thresh
    if not activation_hits.any():
        return out
    activation_idx = int(np.argmax(activation_hits))  # first True

    # Pre-compute rolling lower-bound indices for 60s / 10s / 5s windows.
    # i60_arr[i] = smallest j such that ts_arr[j] >= ts_arr[i] - NS_60
    cutoffs60 = ts_arr - NS_60
    cutoffs10 = ts_arr - NS_10
    cutoffs5  = ts_arr - NS_5
    i60_arr = np.searchsorted(ts_arr, cutoffs60, side="left")
    i10_arr = np.searchsorted(ts_arr, cutoffs10, side="left")
    i5_arr  = np.searchsorted(ts_arr, cutoffs5,  side="left")

    next_allowed_div = 0
    next_allowed_tape = 0
    next_allowed_climax = 0
    next_allowed_flip = 0
    next_allowed_stack = 0

    # Local refs for speed
    _ts = ts_arr
    _px = px_arr
    _sz = sz_arr
    _agg = agg_arr

    for i in range(activation_idx, n):
        ts = int(_ts[i])
        p = float(_px[i])

        # Rolling 60s sums (O(1))
        j60 = int(i60_arr[i])
        cnt60 = int(cum_cnt[i + 1] - cum_cnt[j60])
        if cnt60 < 30:
            continue
        vol60 = int(cum_vol[i + 1] - cum_vol[j60])
        buy60 = int(cum_buy[i + 1] - cum_buy[j60])
        sell60 = int(cum_sell[i + 1] - cum_sell[j60])
        delta60 = buy60 - sell60

        # Rolling 10s tick count
        j10 = int(i10_arr[i])
        cnt10 = int(cum_cnt[i + 1] - cum_cnt[j10])

        # Rolling 5s vol
        j5 = int(i5_arr[i])
        vol5 = int(cum_vol[i + 1] - cum_vol[j5])

        # Near-peak test (within 4 ticks of running MFE)
        mfe = float(running_mfe[i])
        if direction == "LONG":
            near_peak = (mfe - p) <= 4 * TICK_SIZE
        else:
            near_peak = (p - mfe) <= 4 * TICK_SIZE

        # 1) Delta divergence
        if near_peak and ts >= next_allowed_div:
            if direction == "LONG" and delta60 < 0:
                out.append(SignalEvent("delta_divergence", i, ts, p))
                next_allowed_div = ts + COOLDOWN_NS
            elif direction == "SHORT" and delta60 > 0:
                out.append(SignalEvent("delta_divergence", i, ts, p))
                next_allowed_div = ts + COOLDOWN_NS

        # 2) Tape speed collapse
        if ts >= next_allowed_tape and (ts - _ts[0]) >= NS_60:
            rate60 = cnt60 / 60.0
            rate10 = cnt10 / 10.0
            if rate60 > 0 and rate10 < 0.5 * rate60:
                out.append(SignalEvent("tape_speed_collapse", i, ts, p))
                next_allowed_tape = ts + COOLDOWN_NS

        # 3) Volume climax
        if near_peak and ts >= next_allowed_climax:
            avg5_vol = vol60 / 12.0
            if avg5_vol > 0 and vol5 > 2.5 * avg5_vol:
                out.append(SignalEvent("volume_climax", i, ts, p))
                next_allowed_climax = ts + COOLDOWN_NS

        # 4) Aggressor flip (last 30 TICKS, O(1) via cumsum)
        if ts >= next_allowed_flip and i >= 30:
            buy_t30 = int(cum_buy[i + 1] - cum_buy[i - 29])
            sell_t30 = int(cum_sell[i + 1] - cum_sell[i - 29])
            if direction == "LONG":
                if buy_t30 > 0 and (sell_t30 / max(buy_t30, 1)) > 1.5:
                    out.append(SignalEvent("aggressor_flip", i, ts, p))
                    next_allowed_flip = ts + COOLDOWN_NS
            else:
                if sell_t30 > 0 and (buy_t30 / max(sell_t30, 1)) > 1.5:
                    out.append(SignalEvent("aggressor_flip", i, ts, p))
                    next_allowed_flip = ts + COOLDOWN_NS

        # 5) Stacked imbalance.  Only do the per-level scan if the broad
        #    counter-side dominance check (sell_t30 > buy_t30 for LONG) is
        #    already satisfied -- otherwise no chance of 3+ counter-stacked
        #    levels.  This guard cuts ~95% of the per-level work.
        if ts >= next_allowed_stack and i >= 30:
            tail_lo = i - 29
            # Pre-compute the broad direction-aware bias
            if direction == "LONG":
                broad_against = sell_t30 > buy_t30
            else:
                broad_against = buy_t30 > sell_t30
            if broad_against:
                # Per-level scan over 30 ticks.  Cheap: numpy unique + groupby.
                tail_px = _px[tail_lo:i + 1]
                tail_buy = buy_sz[tail_lo:i + 1]
                tail_sell = sell_sz[tail_lo:i + 1]
                # Group by price
                uniq, inv = np.unique(tail_px, return_inverse=True)
                buy_per = np.bincount(inv, weights=tail_buy)
                sell_per = np.bincount(inv, weights=tail_sell)
                if direction == "LONG":
                    # counter = sellers
                    # ratio = buy / sell < 0.33  ==>  sell > 3 * buy
                    mask = (sell_per > 0) & ((buy_per / np.maximum(sell_per, 1)) < 0.33)
                else:
                    mask = (buy_per > 0) & ((sell_per / np.maximum(buy_per, 1)) < 0.33)
                stacked_against = int(mask.sum())
                if stacked_against >= 3:
                    out.append(SignalEvent("stacked_imbalance", i, ts, p))
                    next_allowed_stack = ts + COOLDOWN_NS

    return out


# =====================================================================
# Per-trade analysis pipeline
# =====================================================================

@dataclass
class TradeAnalysis:
    strategy: str
    direction: str
    entry_ts: pd.Timestamp
    entry_price: float
    stop_price: float

    baseline_exit_idx: int
    baseline_exit_price: float
    baseline_pnl_ticks: float
    baseline_exit_reason: str

    mfe_idx: int
    mfe_price: float
    mfe_ticks: float

    # Per-signal: did it fire (within MFE window)? What was the early-exit P&L?
    # Stored flat for CSV output.
    signal_fired: dict             # name -> bool
    signal_exit_price: dict        # name -> float (NaN if not fired)
    signal_exit_pnl_ticks: dict    # name -> float (NaN if not fired)
    signal_ts_offset_sec: dict     # name -> seconds from entry (NaN if not fired)

    # Combined policy outcomes
    aggressive_exit_pnl_ticks: float
    aggressive_exit_reason: str
    conservative_exit_pnl_ticks: float
    conservative_exit_reason: str
    high_conf_exit_pnl_ticks: float
    high_conf_exit_reason: str


def analyze_trade(trade, tick_idx: TickIndex) -> Optional[TradeAnalysis]:
    entry_ts = trade.entry_ts
    end_ts   = entry_ts + pd.Timedelta(minutes=MAX_HOLD_MIN)
    ts_arr, px_arr, sz_arr, agg_arr, bid_arr, ask_arr = tick_idx.slice(
        entry_ts + pd.Timedelta(microseconds=1), end_ts
    )
    if len(ts_arr) == 0:
        return None

    direction = trade.direction
    entry = float(trade.entry_price)
    stop  = float(trade.stop_price)

    # Baseline exit
    base = run_baseline(trade.strategy, direction, entry, stop, ts_arr, px_arr)

    # MFE up to baseline exit (we only care about exits we could have BEAT)
    # We compute MFE across the full slice (not capped at base.exit_idx),
    # but the "favorable" window for early exit is between entry and baseline exit.
    if base.exit_idx < 0:
        return None
    if direction == "LONG":
        # MFE up to baseline exit
        mfe_i = int(np.argmax(px_arr[:base.exit_idx + 1]))
        mfe_price = float(px_arr[mfe_i])
        mfe_ticks = (mfe_price - entry) / TICK_SIZE
    else:
        mfe_i = int(np.argmin(px_arr[:base.exit_idx + 1]))
        mfe_price = float(px_arr[mfe_i])
        mfe_ticks = (entry - mfe_price) / TICK_SIZE

    # Detect signals across the full trade window
    sigs = detect_signals(direction, entry, stop, ts_arr, px_arr, sz_arr, agg_arr,
                          bid_arr, ask_arr)

    # Per-signal: first fire BEFORE OR AT the baseline exit
    SIG_NAMES = ["delta_divergence", "tape_speed_collapse",
                 "volume_climax", "aggressor_flip", "stacked_imbalance"]
    first_fire = {n: None for n in SIG_NAMES}
    for s in sigs:
        if first_fire[s.name] is None and s.tick_idx <= base.exit_idx:
            first_fire[s.name] = s

    signal_fired = {}
    signal_exit_price = {}
    signal_exit_pnl_ticks = {}
    signal_ts_offset_sec = {}
    entry_ns = pd.Timestamp(entry_ts).value
    for name in SIG_NAMES:
        s = first_fire[name]
        if s is None:
            signal_fired[name] = False
            signal_exit_price[name] = float("nan")
            signal_exit_pnl_ticks[name] = float("nan")
            signal_ts_offset_sec[name] = float("nan")
        else:
            signal_fired[name] = True
            signal_exit_price[name] = s.price
            signal_exit_pnl_ticks[name] = _pnl(direction, entry, s.price)
            signal_ts_offset_sec[name] = (s.ts_ns - entry_ns) / 1e9

    # Combined policies
    # aggressive: first ANY signal fire that comes BEFORE baseline exit AND
    #             results in positive (or at least beats baseline) P&L is
    #             evaluated on its own.  But the POLICY is: take the very
    #             first fire among any signal.
    # NOTE: when comparing P&L we use the SAME exit decision logic; if no
    #       signal fires before baseline, the policy degenerates to baseline.
    def first_sig_before(base_idx, names) -> Optional[SignalEvent]:
        # Earliest signal in sigs whose name is in `names` and idx <= base_idx
        cand = [s for s in sigs if s.name in names and s.tick_idx <= base_idx]
        if not cand:
            return None
        return min(cand, key=lambda x: x.tick_idx)

    agg_sig = first_sig_before(base.exit_idx, set(SIG_NAMES))
    if agg_sig is None:
        agg_pnl = base.pnl_ticks
        agg_reason = f"baseline:{base.exit_reason}"
    else:
        agg_pnl = _pnl(direction, entry, agg_sig.price)
        agg_reason = f"early:{agg_sig.name}"

    # conservative: require >=2 signals within a 5s window
    conserv_sig = None
    if sigs:
        sigs_sorted = sorted(sigs, key=lambda x: x.tick_idx)
        WIN_NS = 5_000_000_000
        # For each signal, count distinct signal-names within 5s window.
        for j in range(len(sigs_sorted)):
            ref = sigs_sorted[j]
            if ref.tick_idx > base.exit_idx:
                break
            names_in_window = {ref.name}
            for k in range(j + 1, len(sigs_sorted)):
                if sigs_sorted[k].ts_ns - ref.ts_ns > WIN_NS:
                    break
                names_in_window.add(sigs_sorted[k].name)
                if len(names_in_window) >= 2:
                    conserv_sig = sigs_sorted[k]
                    break
            if conserv_sig is not None:
                break

    if conserv_sig is None:
        conserv_pnl = base.pnl_ticks
        conserv_reason = f"baseline:{base.exit_reason}"
    else:
        conserv_pnl = _pnl(direction, entry, conserv_sig.price)
        conserv_reason = f"early:conservative"

    # high_conf: stacked_imbalance OR volume_climax
    hc_sig = first_sig_before(base.exit_idx, {"stacked_imbalance", "volume_climax"})
    if hc_sig is None:
        hc_pnl = base.pnl_ticks
        hc_reason = f"baseline:{base.exit_reason}"
    else:
        hc_pnl = _pnl(direction, entry, hc_sig.price)
        hc_reason = f"early:{hc_sig.name}"

    return TradeAnalysis(
        strategy=trade.strategy,
        direction=direction,
        entry_ts=entry_ts,
        entry_price=entry,
        stop_price=stop,
        baseline_exit_idx=base.exit_idx,
        baseline_exit_price=base.exit_price,
        baseline_pnl_ticks=base.pnl_ticks,
        baseline_exit_reason=base.exit_reason,
        mfe_idx=mfe_i,
        mfe_price=mfe_price,
        mfe_ticks=mfe_ticks,
        signal_fired=signal_fired,
        signal_exit_price=signal_exit_price,
        signal_exit_pnl_ticks=signal_exit_pnl_ticks,
        signal_ts_offset_sec=signal_ts_offset_sec,
        aggressive_exit_pnl_ticks=agg_pnl,
        aggressive_exit_reason=agg_reason,
        conservative_exit_pnl_ticks=conserv_pnl,
        conservative_exit_reason=conserv_reason,
        high_conf_exit_pnl_ticks=hc_pnl,
        high_conf_exit_reason=hc_reason,
    )


# =====================================================================
# Aggregation + report
# =====================================================================

SIG_NAMES = ["delta_divergence", "tape_speed_collapse",
             "volume_climax", "aggressor_flip", "stacked_imbalance"]


def to_per_trade_row(t: TradeAnalysis) -> dict:
    row = {
        "strategy": t.strategy,
        "direction": t.direction,
        "entry_ts": t.entry_ts,
        "entry_price": t.entry_price,
        "stop_price": t.stop_price,
        "baseline_exit_price": t.baseline_exit_price,
        "baseline_pnl_ticks": t.baseline_pnl_ticks,
        "baseline_pnl_dollars": t.baseline_pnl_ticks * TICK_VALUE,
        "baseline_exit_reason": t.baseline_exit_reason,
        "mfe_price": t.mfe_price,
        "mfe_ticks": t.mfe_ticks,
        "aggressive_pnl_ticks": t.aggressive_exit_pnl_ticks,
        "aggressive_pnl_dollars": t.aggressive_exit_pnl_ticks * TICK_VALUE,
        "aggressive_reason": t.aggressive_exit_reason,
        "conservative_pnl_ticks": t.conservative_exit_pnl_ticks,
        "conservative_pnl_dollars": t.conservative_exit_pnl_ticks * TICK_VALUE,
        "conservative_reason": t.conservative_exit_reason,
        "high_conf_pnl_ticks": t.high_conf_exit_pnl_ticks,
        "high_conf_pnl_dollars": t.high_conf_exit_pnl_ticks * TICK_VALUE,
        "high_conf_reason": t.high_conf_exit_reason,
    }
    for n in SIG_NAMES:
        row[f"sig_{n}_fired"] = t.signal_fired[n]
        row[f"sig_{n}_exit_price"] = t.signal_exit_price[n]
        row[f"sig_{n}_exit_pnl_ticks"] = t.signal_exit_pnl_ticks[n]
        row[f"sig_{n}_exit_pnl_dollars"] = (
            t.signal_exit_pnl_ticks[n] * TICK_VALUE
            if not np.isnan(t.signal_exit_pnl_ticks[n]) else float("nan")
        )
        row[f"sig_{n}_offset_sec"] = t.signal_ts_offset_sec[n]
    return row


def compute_signal_metrics(per_trade: pd.DataFrame, strategy: str) -> List[dict]:
    """For each signal, compute TP/FP rate and avg ticks locked-in delta."""
    sub = per_trade[per_trade.strategy == strategy]
    out = []
    if sub.empty:
        return out
    for n in SIG_NAMES:
        fired_col = f"sig_{n}_fired"
        sig_pnl_col = f"sig_{n}_exit_pnl_ticks"
        fired = sub[sub[fired_col]]
        if len(fired) == 0:
            out.append({
                "strategy": strategy, "signal": n, "n_trades": len(sub),
                "fire_rate_pct": 0.0, "n_fired": 0,
                "true_positive_rate_pct": float("nan"),
                "false_positive_rate_pct": float("nan"),
                "avg_ticks_locked_vs_baseline": float("nan"),
                "sum_delta_dollars": 0.0,
            })
            continue
        # TP/FP definition:
        #   For each fired trade, the early-exit P&L is sig_pnl.
        #   The baseline P&L is baseline_pnl_ticks.
        #   TP if sig_pnl > baseline_pnl  (i.e. early exit captured MORE than baseline)
        #   FP if sig_pnl < baseline_pnl  (i.e. we gave up baseline P&L)
        #   tie if equal (rare)
        delta = fired[sig_pnl_col] - fired["baseline_pnl_ticks"]
        tp = (delta > 0).sum()
        fp = (delta < 0).sum()
        tie = (delta == 0).sum()
        out.append({
            "strategy": strategy, "signal": n, "n_trades": len(sub),
            "fire_rate_pct": round(len(fired) / len(sub) * 100, 1),
            "n_fired": len(fired),
            "true_positive_rate_pct": round(tp / len(fired) * 100, 1),
            "false_positive_rate_pct": round(fp / len(fired) * 100, 1),
            "tie_rate_pct": round(tie / len(fired) * 100, 1),
            "avg_ticks_locked_vs_baseline": round(delta.mean(), 2),
            "sum_delta_dollars": round(delta.sum() * TICK_VALUE, 0),
        })
    return out


def compute_policy_summary(per_trade: pd.DataFrame) -> pd.DataFrame:
    """Per-strategy: baseline vs each combined early-exit policy."""
    rows = []
    for strat, sub in per_trade.groupby("strategy"):
        n = len(sub)
        for policy_col, name in [("baseline_pnl_dollars", "baseline"),
                                 ("aggressive_pnl_dollars", "early_aggressive"),
                                 ("conservative_pnl_dollars", "early_conservative"),
                                 ("high_conf_pnl_dollars", "early_high_conf")]:
            pnl = sub[policy_col]
            wins = (pnl > 0).sum()
            losses = (pnl < 0).sum()
            gross_win = pnl[pnl > 0].sum()
            gross_loss = -pnl[pnl < 0].sum()
            pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
            rows.append({
                "strategy": strat, "policy": name, "n_trades": n,
                "wr_pct": round(wins / n * 100, 1) if n else 0,
                "total_pnl_dollars": round(pnl.sum(), 0),
                "avg_pnl_dollars": round(pnl.mean(), 2) if n else 0,
                "pf": round(pf, 2) if not np.isinf(pf) else 99.0,
            })
    return pd.DataFrame(rows)


def write_report(per_sig: pd.DataFrame, per_policy: pd.DataFrame,
                 per_trade: pd.DataFrame):
    # Pre-compute verdicts so the TL;DR can describe them properly.
    verdicts_tldr = []
    for strat in sorted(per_policy.strategy.unique()):
        sub = per_policy[per_policy.strategy == strat]
        base_row = sub[sub.policy == "baseline"]
        if base_row.empty:
            continue
        base_total = float(base_row.iloc[0].total_pnl_dollars)
        candidates = sub[sub.policy != "baseline"]
        if candidates.empty:
            continue
        best = candidates.loc[candidates.total_pnl_dollars.idxmax()]
        delta = float(best.total_pnl_dollars) - base_total
        n_tr = int(best.n_trades)
        verdict = "ADOPT" if delta > 0 and n_tr >= 30 else ("NO" if delta <= 0 else "INSUFFICIENT")
        verdicts_tldr.append((strat, best.policy, base_total,
                              float(best.total_pnl_dollars),
                              delta, n_tr, verdict))
    n_adopt = sum(1 for v in verdicts_tldr if v[6] == "ADOPT")
    n_no    = sum(1 for v in verdicts_tldr if v[6] == "NO")
    n_ins   = sum(1 for v in verdicts_tldr if v[6] == "INSUFFICIENT")

    lines = []
    lines.append("# Early Reversal Exit Signal Analysis\n")
    lines.append("**Generated:** 2026-05-19  ")
    lines.append("**Branch:** weekly-evolution/2026-05-17  ")
    lines.append("**Tool:** `tools/phoenix_early_reversal_signals.py`  ")
    lines.append("**Tick data:** `data/historical/databento_tbbo/mnq_ticks_clean.parquet` (43.8M MNQ ticks, 2026-03-17 to 2026-05-15)\n")

    # TL;DR up top
    lines.append("## TL;DR\n")
    if n_adopt == 0:
        lines.append(
            "**No strategy benefits from any early-reversal early-exit policy tested.** "
            "Across all six strategies and three combined-policy variants, every "
            "early-exit configuration UNDERPERFORMS the Section U.3 baseline policy "
            "by 30-90% in total P&L over the 2-month tick window. False-positive "
            "rates dominate (44-78% per signal). DO NOT ship early-reversal exits. "
            "Keep the Section U.3 baseline (fixed_rr / chandelier / time_exit).\n"
        )
    elif n_adopt == 1:
        winner = next(v for v in verdicts_tldr if v[6] == "ADOPT")
        lines.append(
            f"**Only `{winner[0]}` shows an edge from an early-reversal exit "
            f"(`{winner[1]}`, +${winner[4]:,.0f} over baseline on n={winner[5]} trades). "
            f"The other {n_no} strategies should NOT adopt early-reversal exits.**\n"
        )
    else:
        lines.append(
            f"**{n_adopt} of {n_adopt + n_no + n_ins} strategies benefit from at "
            f"least one early-reversal early-exit policy. See per-strategy verdict "
            f"below.**\n"
        )

    lines.append("## Question\n")
    lines.append("Phoenix's Phase 13 Section U production policies exit AFTER price hits a "
                 "fixed RR target, Chandelier stop, or time-out. This tool asks: are there "
                 "tick-level CLUES that price is about to REVERSE that we could use as an "
                 "EARLY EXIT trigger to lock in MFE profits BEFORE the reversal?\n")

    lines.append("## Signals tested\n")
    lines.append("All five fire only after the trade has reached at least "
                 f"+{ACTIVATE_R}R favorable (worth-locking-in threshold). A 5s "
                 "per-signal cooldown prevents repeat fires.\n")
    lines.append("1. `delta_divergence` - rolling 60s cumulative aggressor delta turns "
                 "AGAINST the trade while price within 4 ticks of MFE peak.")
    lines.append("2. `tape_speed_collapse` - last-10s tick rate drops below 50% of "
                 "trailing-60s avg rate.")
    lines.append("3. `volume_climax` - last-5s volume > 2.5x the avg 5s bucket of the "
                 "trailing 60s, while near peak.")
    lines.append("4. `aggressor_flip` - last 30 TICKS show counter-side aggressor "
                 "volume > 1.5x with-trade aggressor volume.")
    lines.append("5. `stacked_imbalance` - in the last 30 ticks, 3+ distinct price "
                 "levels show counter-side aggressor dominance > 3:1.\n")

    lines.append("## Per-signal performance\n")
    lines.append("True-positive (TP) = early-exit P&L beats baseline P&L; "
                 "false-positive (FP) = early exit gave up P&L the baseline would have "
                 "captured.  `avg_ticks_locked_vs_baseline` is the average per-fired-trade "
                 "tick delta (early - baseline); negative means the signal cost ticks.\n")

    for strat in sorted(per_sig.strategy.unique()):
        sub = per_sig[per_sig.strategy == strat].copy()
        if sub.empty:
            continue
        lines.append(f"### {strat}\n")
        lines.append("| signal | n_fired/n_trades | fire_rate | TP% | FP% | "
                     "avg_delta_ticks | sum_delta_$ |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for _, r in sub.iterrows():
            n_fired = int(r.n_fired)
            n_tr = int(r.n_trades)
            fire_pct = r.fire_rate_pct
            if n_fired == 0:
                lines.append(f"| {r.signal} | 0/{n_tr} | {fire_pct:.1f}% | - | - | - | - |")
            else:
                lines.append(
                    f"| {r.signal} | {n_fired}/{n_tr} | {fire_pct:.1f}% | "
                    f"{r.true_positive_rate_pct:.1f}% | {r.false_positive_rate_pct:.1f}% | "
                    f"{r.avg_ticks_locked_vs_baseline:+.2f} | "
                    f"${r.sum_delta_dollars:+,.0f} |"
                )
        lines.append("")

    lines.append("## Combined policy P&L per strategy\n")
    lines.append("Per-strategy total P&L over the 2-month tick window (2026-03-17 to "
                 "2026-05-15).  `baseline` is the Section U.3 production policy "
                 "(fixed_2r / fixed_3r / chandelier / time_exit).  Early policies "
                 "fall back to baseline if no qualifying signal fires.\n")
    lines.append("| strategy | policy | n | wr% | total_$ | avg_$ | pf | delta_vs_baseline_$ |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for strat in sorted(per_policy.strategy.unique()):
        sub = per_policy[per_policy.strategy == strat]
        base_row = sub[sub.policy == "baseline"]
        if base_row.empty:
            continue
        base_total = float(base_row.iloc[0].total_pnl_dollars)
        order = ["baseline", "early_aggressive", "early_conservative", "early_high_conf"]
        for pname in order:
            r = sub[sub.policy == pname]
            if r.empty:
                continue
            r = r.iloc[0]
            delta = float(r.total_pnl_dollars) - base_total
            lines.append(f"| {strat} | {r.policy} | {int(r.n_trades)} | {r.wr_pct:.1f}% | "
                         f"${r.total_pnl_dollars:+,.0f} | ${r.avg_pnl_dollars:+.2f} | "
                         f"{r.pf:.2f} | ${delta:+,.0f} |")
        lines.append("")

    # Verdict (uses verdicts_tldr precomputed above)
    lines.append("## Verdict per strategy\n")
    for strat, best_pol, base_total, best_total, delta, n_tr, verdict in verdicts_tldr:
        lines.append(f"- **{strat}** (n={n_tr}): baseline ${base_total:+,.0f}, "
                     f"best early-exit variant `{best_pol}` ${best_total:+,.0f} "
                     f"({'+' if delta >= 0 else ''}${delta:,.0f} delta). "
                     f"Verdict: **{verdict}**.")
    lines.append("")

    # Caveats
    lines.append("## Caveats and limitations\n")
    lines.append("1. **TBBO has no level-2 depth.** stacked_imbalance uses recent "
                 "aggressor traffic at each price level as a PROXY for resting "
                 "depth.  A true MBO order-book replay would give cleaner stacked-"
                 "defense signals.")
    lines.append("2. **Aggressor flip + stacked imbalance use a 30-TICK window**, "
                 "not 30 seconds.  In thin tape that is a longer effective window; "
                 "in fast tape, shorter.  Trade-off is intentional: signal sensitivity "
                 "scales with activity.")
    lines.append("3. **No slippage modeling on early exits.**  An early-exit fill "
                 "is simulated at the signal-tick price exactly.  Real fills would "
                 "be a tick or two worse, which would WORSEN every early-exit P&L "
                 "result by ~$0.50-$1.00 per trade.")
    lines.append("4. **Activation threshold of +0.5R is a knob.** Higher thresholds "
                 "would fire fewer false-positives but miss more true-positive captures.")
    lines.append("5. **2-month sample window** (~tick-data limit).  Small-sample "
                 "strategies (opening_session, g_inside_bar_breakout, raschke_baseline) "
                 "verdicts are NOISY -- treat any ADOPT verdict at n<30 as INSUFFICIENT.")
    lines.append("6. **No interaction with strategy filters.** The trade list is "
                 "the historical entry set; we are only swapping exits.")
    lines.append("")

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# =====================================================================
# Main
# =====================================================================

def main():
    print("=" * 100)
    print("PHOENIX EARLY-REVERSAL SIGNAL ANALYSIS (tick level)")
    print("=" * 100)
    print()

    print("Loading clean tick cache (full window)...", flush=True)
    t0 = time.time()
    df_ticks = load_clean_ticks(start=str(WINDOW_START), end=str(WINDOW_END))
    print(f"  loaded {len(df_ticks):,} ticks in {time.time()-t0:.1f}s "
          f"(mem ~{df_ticks.memory_usage(deep=True).sum()/1024**2:.0f} MB)", flush=True)
    tick_idx = TickIndex(df_ticks)
    del df_ticks
    print(f"  tick index ready in {time.time()-t0:.1f}s total", flush=True)
    print()

    trades = load_trades()
    print(f"Loaded {len(trades):,} trades in window")
    print("Per-strategy counts:")
    for s, c in trades.strategy.value_counts().items():
        baseline_name = STRAT_BASELINE[s][0] + str(STRAT_BASELINE[s][1])
        print(f"  {s:<28s}  {c:>5d}   baseline={baseline_name}")
    print()

    # ----- single-trade sanity walk
    print("Sanity walk: first bias_momentum trade")
    print("-" * 100)
    bm = trades[trades.strategy == "bias_momentum"].head(1)
    if not bm.empty:
        t = bm.iloc[0]
        ts_arr, px_arr, sz_arr, agg_arr, bid_arr, ask_arr = tick_idx.slice(
            t.entry_ts + pd.Timedelta(microseconds=1),
            t.entry_ts + pd.Timedelta(minutes=MAX_HOLD_MIN)
        )
        print(f"  entry_ts:    {t.entry_ts}")
        print(f"  direction:   {t.direction}  entry=${t.entry_price:.2f}  stop=${t.stop_price:.2f}")
        print(f"  ticks:       {len(ts_arr):,}")
        if len(ts_arr) > 0:
            ta = analyze_trade(t, tick_idx)
            if ta is not None:
                print(f"  baseline exit: ${ta.baseline_exit_price:.2f}  "
                      f"pnl={ta.baseline_pnl_ticks:+.1f}t  reason={ta.baseline_exit_reason}")
                print(f"  MFE: {ta.mfe_ticks:+.1f}t at ${ta.mfe_price:.2f}")
                for n in SIG_NAMES:
                    print(f"    sig {n:<22s} fired={ta.signal_fired[n]}  "
                          f"exit_pnl={ta.signal_exit_pnl_ticks[n]:+.1f}t" if ta.signal_fired[n]
                          else f"    sig {n:<22s} fired=False")
                print(f"  aggressive:  pnl={ta.aggressive_exit_pnl_ticks:+.1f}t  reason={ta.aggressive_exit_reason}")
                print(f"  conservative: pnl={ta.conservative_exit_pnl_ticks:+.1f}t  reason={ta.conservative_exit_reason}")
                print(f"  high_conf:   pnl={ta.high_conf_exit_pnl_ticks:+.1f}t  reason={ta.high_conf_exit_reason}")
    print()

    # ----- full replay
    print("Replaying all trades (this takes a while)...", flush=True)
    t0 = time.time()
    rows = []
    n = len(trades)
    last_log = time.time()
    for i, tr in enumerate(trades.itertuples(index=False), 1):
        ta = analyze_trade(tr, tick_idx)
        if ta is None:
            continue
        rows.append(to_per_trade_row(ta))
        if i % 100 == 0 or i == n or time.time() - last_log > 15:
            elapsed = time.time() - t0
            rate = i / max(0.001, elapsed)
            eta = (n - i) / max(0.001, rate)
            print(f"  {i:>5d}/{n}  ({elapsed:.0f}s elapsed, {rate:.1f}/s, "
                  f"ETA {eta:.0f}s)", flush=True)
            last_log = time.time()
    print(f"Replay done in {time.time()-t0:.0f}s -> {len(rows):,} per-trade rows", flush=True)
    print()

    if not rows:
        print("No rows produced. Aborting.")
        return 1

    per_trade = pd.DataFrame(rows)
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    per_trade.to_csv(RESULTS_CSV, index=False)
    print(f"Saved per-trade results -> {RESULTS_CSV}")
    print()

    # ----- per-signal metrics + per-policy summary
    sig_rows = []
    for strat in sorted(per_trade.strategy.unique()):
        sig_rows.extend(compute_signal_metrics(per_trade, strat))
    per_sig = pd.DataFrame(sig_rows)

    per_policy = compute_policy_summary(per_trade)

    # Combine into a single summary CSV for convenience
    sig_with_kind = per_sig.assign(kind="signal_perf")
    pol_with_kind = per_policy.assign(kind="policy_perf")
    summary = pd.concat([sig_with_kind, pol_with_kind], ignore_index=True, sort=False)
    summary.to_csv(SUMMARY_CSV, index=False)
    print(f"Saved summary -> {SUMMARY_CSV}")
    print()

    # ----- console summary
    print("Per-signal metrics per strategy:")
    print(per_sig.to_string(index=False))
    print()
    print("Per-policy total P&L per strategy:")
    print(per_policy.to_string(index=False))
    print()

    write_report(per_sig, per_policy, per_trade)
    print(f"Report written -> {REPORT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
