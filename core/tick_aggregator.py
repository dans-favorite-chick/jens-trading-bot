"""
Phoenix Bot — Tick Aggregator

Builds bars (1m, 5m, 15m, 60m), ATR, VWAP, EMA, CVD, and multi-TF
bias from raw tick data. Single source of truth — all derived math
lives here, not in NT8.

Also builds tick-count bars (e.g., 512t) that complete when N trades
accumulate rather than when a time boundary is crossed. Tick bars
normalize for activity — bars form faster during OPEN_MOMENTUM and
slower during AFTERNOON_CHOP, filtering noise automatically.
"""

import json
import logging
import os
import time
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

_log = logging.getLogger("TickAggregator")

from core.dom_analyzer import DOMAnalyzer


@dataclass
class Bar:
    """A completed OHLCV bar."""
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: int = 0
    tick_count: int = 0
    start_time: float = 0.0
    end_time: float = 0.0


@dataclass
class BarBuilder:
    """Accumulates ticks into bars of a given time interval."""
    interval_seconds: int
    current: Bar = field(default_factory=Bar)
    completed: deque = field(default_factory=lambda: deque(maxlen=200))
    bar_count: int = 0
    _started: bool = False

    def add_tick(self, price: float, volume: int, timestamp: float) -> Bar | None:
        """
        Add a tick. Returns a completed Bar when the interval boundary
        is crossed, else None.
        """
        if not self._started:
            self._start_new_bar(price, volume, timestamp)
            return None

        # Check if this tick belongs to the next bar
        elapsed = timestamp - self.current.start_time
        if elapsed >= self.interval_seconds:
            completed = self.current
            completed.end_time = timestamp
            self.completed.append(completed)
            self.bar_count += 1
            self._start_new_bar(price, volume, timestamp)
            return completed

        # Update current bar
        self.current.high = max(self.current.high, price)
        self.current.low = min(self.current.low, price)
        self.current.close = price
        self.current.volume += volume
        self.current.tick_count += 1
        return None

    def _start_new_bar(self, price: float, volume: int, timestamp: float):
        self.current = Bar(
            open=price, high=price, low=price, close=price,
            volume=volume, tick_count=1, start_time=timestamp,
        )
        self._started = True


@dataclass
class TickBarBuilder:
    """
    Accumulates ticks into bars that complete every N *trades* (not seconds).

    Benefits over time bars for MNQ:
      - Bars form faster during OPEN_MOMENTUM (high activity) → more signal resolution
      - Bars form slower during AFTERNOON_CHOP (low activity) → noise suppression
      - Every bar has roughly equal information density
      - Cleaner wick/spring patterns (no thin time bars from low-volume periods)

    Recommended sizes for MNQ:
      233t  — very fast (~15-30s bars at open), equivalent to 1m in low vol
      512t  — medium  (~30-60s bars at open), best balance for signal detection
      1000t — slow    (~1-2 min at open),     swing-trade equivalent
    """
    tick_threshold: int        # Number of trades per bar
    current: Bar = field(default_factory=Bar)
    completed: deque = field(default_factory=lambda: deque(maxlen=200))
    bar_count: int = 0
    _started: bool = False
    _current_ticks: int = 0   # Tick counter for current bar

    def add_tick(self, price: float, volume: int, timestamp: float) -> Bar | None:
        """
        Add a tick. Returns a completed Bar when tick_threshold is reached.
        Each call = one trade event, regardless of volume size.
        """
        if not self._started:
            self._start_new_bar(price, volume, timestamp)
            return None

        # Update current bar with this tick
        self.current.high  = max(self.current.high, price)
        self.current.low   = min(self.current.low,  price)
        self.current.close = price
        self.current.volume    += volume
        self.current.tick_count += 1
        self._current_ticks    += 1

        # Check if threshold reached
        if self._current_ticks >= self.tick_threshold:
            completed = self.current
            completed.end_time = timestamp
            self.completed.append(completed)
            self.bar_count += 1
            self._start_new_bar(price, volume, timestamp)
            return completed

        return None

    def _start_new_bar(self, price: float, volume: int, timestamp: float):
        self.current = Bar(
            open=price, high=price, low=price, close=price,
            volume=volume, tick_count=1, start_time=timestamp,
        )
        self._started = True
        self._current_ticks = 1  # Count the opening tick


class TickAggregator:
    """
    Central aggregator that processes raw ticks from the bridge and
    maintains multi-timeframe bars, indicators, and derived signals.

    Bar series:
      Time-based: 1m, 5m, 15m, 60m  — trend direction & TF bias
      Tick-based: 512t (configurable) — entry timing precision
    """

    def __init__(self, bot_name: str = "bot"):
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
        try:
            from config.settings import TICK_BAR_SIZE, TICK_BAR_ENABLED
            _tick_size   = TICK_BAR_SIZE
            _tick_enabled = TICK_BAR_ENABLED
        except ImportError:
            _tick_size   = 512
            _tick_enabled = True

        self.bot_name = bot_name
        self._tick_bar_size    = _tick_size
        self._tick_bar_enabled = _tick_enabled
        self._tick_bar_label   = f"{_tick_size}t"

        # Bar builders for each timeframe
        self.bars_1m  = BarBuilder(interval_seconds=60)
        self.bars_5m  = BarBuilder(interval_seconds=300)
        self.bars_15m = BarBuilder(interval_seconds=900)
        self.bars_60m = BarBuilder(interval_seconds=3600)

        # Tick-count bar builder (entry timing — see module docstring)
        self.bars_tick = TickBarBuilder(tick_threshold=_tick_size) if _tick_enabled else None

        # ATR (14-period) per timeframe, plus tick bar series
        self.atr = {"1m": 0.0, "5m": 0.0, "15m": 0.0, "60m": 0.0, "tick": 0.0}
        self._tr_history = {
            "1m":   deque(maxlen=14),
            "5m":   deque(maxlen=14),
            "15m":  deque(maxlen=14),
            "60m":  deque(maxlen=14),
            "tick": deque(maxlen=14),
        }

        # EMA 5/9/21 on 5m bars
        # EMA5: precision entry timing (price stays 3-5x closer to EMA5 than EMA9)
        # EMA9: trend structure + pullback level (primary)
        # EMA21: swing level, better for afternoon regime and 2:1+ targets
        self.ema5 = 0.0
        self.ema9 = 0.0
        self.ema21 = 0.0
        self._ema5_count = 0
        self._ema9_count = 0
        self._ema21_count = 0

        # EMA 9/21 on 15m bars — context timeframe
        # The 15m is a 3:1 ratio to the 5m signal TF (optimal for intraday futures).
        # Used to replace the laggy 60m TF bias as a structural context filter.
        # "Price > EMA9_15m > EMA21_15m" = 15m trend established (medium-term context).
        self.ema9_15m  = 0.0
        self.ema21_15m = 0.0
        self._ema9_15m_count  = 0
        self._ema21_15m_count = 0

        # MACD (5m bars, using EMA9/EMA21 spread — no extra periods needed)
        # macd_line     = ema9 - ema21 (the raw spread)
        # macd_signal   = 9-period EMA of macd_line
        # macd_histogram = macd_line - macd_signal  (acceleration of trend momentum)
        # Warm: needs 21 (EMA21) + 9 (signal) = 30 bars minimum (~2.5 hrs)
        self.macd_line:      float = 0.0
        self.macd_signal:    float = 0.0
        self.macd_histogram: float = 0.0
        self.macd_histogram_prev: float = 0.0  # previous bar histogram (for slope)
        self._macd_signal_count: int = 0        # signal-line warm-up counter

        # VWAP (daily reset)
        self._vwap_cum_pv = 0.0   # cumulative(typical * volume)
        self._vwap_cum_pv2 = 0.0  # cumulative(typical² * volume)  — needed for σ²
        self._vwap_cum_vol = 0    # cumulative(volume)
        self._vwap_day = None
        self.vwap = 0.0

        # VWAP standard deviation bands (σ = sqrt(E[x²] - E[x]²), volume-weighted)
        # ±1σ: fair value zone / pullback reload level
        # ±2σ: statistical exhaustion / do-not-chase zone
        self.vwap_std     = 0.0   # current σ
        self.vwap_upper1  = 0.0   # VWAP + 1σ
        self.vwap_lower1  = 0.0   # VWAP - 1σ
        self.vwap_upper2  = 0.0   # VWAP + 2σ
        self.vwap_lower2  = 0.0   # VWAP - 2σ

        # Anchored VWAP — prior day's key levels (set via set_avwap_anchors())
        # AVWAP anchored to PDH/PDL/PDC shows where price has been trading relative
        # to those institutional reference levels since session open.
        self._avwap_pd_high  = {"pv": 0.0, "pv2": 0.0, "vol": 0, "val": 0.0, "active": False}
        self._avwap_pd_low   = {"pv": 0.0, "pv2": 0.0, "vol": 0, "val": 0.0, "active": False}
        self._avwap_pd_close = {"pv": 0.0, "pv2": 0.0, "vol": 0, "val": 0.0, "active": False}
        self.avwap_pd_high   = 0.0  # prior day high AVWAP
        self.avwap_pd_low    = 0.0  # prior day low AVWAP
        self.avwap_pd_close  = 0.0  # prior day close AVWAP

        # CVD (Cumulative Volume Delta) — real ask/bid classification
        # Each tick: price >= ask → buy aggressor, price <= bid → sell aggressor, else split
        self._cvd_session = 0.0
        self.cvd = 0.0

        # Per-5m-bar tick delta accumulators (reset on each 5m bar close)
        self._bar_buy_vol: float = 0.0
        self._bar_sell_vol: float = 0.0
        # Completed bar values (available after each 5m close)
        self.bar_buy_vol: float = 0.0
        self.bar_sell_vol: float = 0.0
        self.bar_delta: float = 0.0   # bar_buy_vol - bar_sell_vol

        # ── Volume Analysis ─────────────────────────────────────────
        # Rolling 5m bar volume history for relative volume comparisons
        self._vol_history_5m: deque = deque(maxlen=20)   # last 20 bar volumes
        self.avg_vol_5m: float = 0.0                      # rolling average
        self.vol_climax_ratio: float = 1.0                # last bar / avg (>2.5 = climax)

        # Delta history for divergence detection (price vs buying pressure)
        self._delta_history_5m: deque = deque(maxlen=10)  # last 10 bar_delta values
        self._high_history_5m: deque  = deque(maxlen=10)  # last 10 bar highs
        self._low_history_5m: deque   = deque(maxlen=10)  # last 10 bar lows

        # VSA (Volume Spread Analysis) signal on last completed 5m bar
        # "ABSORPTION" | "EFFORT_UP" | "EFFORT_DOWN" | "TEST_UP" | "TEST_DOWN" | "NEUTRAL"
        self.vsa_signal_5m: str = "NEUTRAL"

        # DOM analyzer (iceberg + absorption detection)
        self.dom_analyzer = DOMAnalyzer()

        # DOM depth state (updated via process_dom() from bridge)
        self.dom_bid_stack: float = 0.0
        self.dom_ask_stack: float = 0.0
        self.dom_imbalance: float = 0.5   # bid/(bid+ask)
        self.dom_bid_heavy: bool = False   # imbalance > 0.60
        self.dom_ask_heavy: bool = False   # imbalance < 0.40
        self.dom_last_update: float = 0.0

        # Multi-TF bias votes (time-based)
        self.tf_bias = {"1m": "NEUTRAL", "5m": "NEUTRAL", "15m": "NEUTRAL", "60m": "NEUTRAL"}
        self.tf_votes_bullish = 0
        self.tf_votes_bearish = 0

        # Tick bar bias (independent — tracks entry-level momentum)
        self.tf_bias_tick = "NEUTRAL"    # "BULLISH" | "BEARISH" | "NEUTRAL"

        # Current tick data
        self.last_price = 0.0
        self.last_bid = 0.0
        self.last_ask = 0.0
        self.tick_count = 0

        # Callbacks for new bar events
        self._on_bar_callbacks: list = []

        # ── Phase 4B: Session levels aggregator ─────────────────────
        # Feeds prior-day OHLC + volume profile + pivots + live opening
        # levels into snapshot() for the opening_session strategy family.
        # Failures never crash bot startup — falls back to None.
        self.session_levels = None
        try:
            from core.session_levels_aggregator import SessionLevelsAggregator
            self.session_levels = SessionLevelsAggregator(bot_name=bot_name)
            self.session_levels.load_prior_day()
            _log.info(
                f"[SESSION_LEVELS] init bot={bot_name} "
                f"high={self.session_levels.prior_day_high} "
                f"low={self.session_levels.prior_day_low} "
                f"poc={self.session_levels.prior_day_poc} "
                f"pp={self.session_levels.pivot_pp}"
            )
        except Exception as e:
            _log.warning(f"[SESSION_LEVELS] init failed ({e!r}); disabled")
            self.session_levels = None

    def on_bar(self, callback):
        """Register a callback: callback(timeframe: str, bar: Bar)"""
        self._on_bar_callbacks.append(callback)

    def process_tick(self, tick: dict) -> dict:
        """
        Process a raw tick from the bridge.

        Args:
            tick: {"type": "tick", "price": 18527.5, "bid": ..., "ask": ..., "vol": 1, "ts": "..."}

        Returns:
            dict with current state snapshot
        """
        price = tick.get("price", 0)
        bid = tick.get("bid", 0)
        ask = tick.get("ask", 0)
        vol = tick.get("vol", 0)
        ts_str = tick.get("ts", "")

        if price <= 0:
            return self.snapshot()

        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            ts = time.time()

        prev_price = self.last_price  # Save BEFORE updating for CVD fallback
        self.last_price = price
        self.last_bid = bid
        self.last_ask = ask
        self.tick_count += 1

        # ── CVD — real ask/bid tick classification ───────────────────
        # TickStreamer sends bid/ask on every tick so we can classify precisely:
        #   price >= ask  → buy aggressor (lifted the offer)
        #   price <= bid  → sell aggressor (hit the bid)
        #   else          → mid-market, split 50/50
        if bid > 0 and ask > 0:
            if price >= ask:
                buy_vol, sell_vol = vol, 0
            elif price <= bid:
                buy_vol, sell_vol = 0, vol
            else:
                buy_vol = vol * 0.5
                sell_vol = vol * 0.5
        else:
            # Fallback when bid/ask not available: use price direction vs PREVIOUS price
            buy_vol = vol if prev_price and price > prev_price else 0
            sell_vol = vol if prev_price and price < prev_price else 0

        self._cvd_session += buy_vol - sell_vol
        self.cvd = self._cvd_session

        # Accumulate into current 5m bar delta
        self._bar_buy_vol  += buy_vol
        self._bar_sell_vol += sell_vol

        # ── VWAP + σ bands + Anchored VWAP ─────────────────────────
        today = datetime.fromtimestamp(ts).date()
        if self._vwap_day != today:
            self._vwap_cum_pv  = 0.0
            self._vwap_cum_pv2 = 0.0
            self._vwap_cum_vol = 0
            self._cvd_session  = 0.0
            self._bar_buy_vol  = 0.0
            self._bar_sell_vol = 0.0
            self._vwap_day     = today

        typical = (price + (bid if bid > 0 else price) + (ask if ask > 0 else price)) / 3.0
        self._vwap_cum_pv  += typical * vol
        self._vwap_cum_pv2 += typical * typical * vol
        self._vwap_cum_vol += vol
        if self._vwap_cum_vol > 0:
            self.vwap = self._vwap_cum_pv / self._vwap_cum_vol
            # σ² = E[x²] - E[x]²  (volume-weighted variance, no history scan needed)
            _variance = (self._vwap_cum_pv2 / self._vwap_cum_vol) - (self.vwap * self.vwap)
            self.vwap_std    = math.sqrt(max(0.0, _variance))  # guard fp rounding noise
            self.vwap_upper1 = self.vwap + self.vwap_std
            self.vwap_lower1 = self.vwap - self.vwap_std
            self.vwap_upper2 = self.vwap + 2.0 * self.vwap_std
            self.vwap_lower2 = self.vwap - 2.0 * self.vwap_std

        # ── Anchored VWAP accumulators ───────────────────────────────
        for _anchor in (self._avwap_pd_high, self._avwap_pd_low, self._avwap_pd_close):
            if _anchor["active"]:
                _anchor["pv"]  += typical * vol
                _anchor["pv2"] += typical * typical * vol
                _anchor["vol"] += vol
                if _anchor["vol"] > 0:
                    _anchor["val"] = _anchor["pv"] / _anchor["vol"]
        self.avwap_pd_high  = self._avwap_pd_high["val"]
        self.avwap_pd_low   = self._avwap_pd_low["val"]
        self.avwap_pd_close = self._avwap_pd_close["val"]

        # ── Build bars ──────────────────────────────────────────────
        completed_bars = []
        for tf_name, builder in [("1m", self.bars_1m), ("5m", self.bars_5m),
                                  ("15m", self.bars_15m), ("60m", self.bars_60m)]:
            bar = builder.add_tick(price, vol, ts)
            if bar:
                completed_bars.append((tf_name, bar))
                self._on_bar_complete(tf_name, bar)

        # Tick bar — builds on every trade event regardless of time
        if self.bars_tick is not None:
            tick_bar = self.bars_tick.add_tick(price, vol, ts)
            if tick_bar:
                self._on_bar_complete(self._tick_bar_label, tick_bar)

        return self.snapshot()

    def process_dom(self, dom: dict):
        """Update DOM depth state from a bridge dom message."""
        self.dom_bid_stack = float(dom.get("bid_stack", 0))
        self.dom_ask_stack = float(dom.get("ask_stack", 0))
        total = self.dom_bid_stack + self.dom_ask_stack
        self.dom_imbalance = (self.dom_bid_stack / total) if total > 0 else 0.5
        self.dom_bid_heavy = self.dom_imbalance > 0.60
        self.dom_ask_heavy = self.dom_imbalance < 0.40
        self.dom_last_update = time.time()

        # Feed DOM data to the analyzer for iceberg/absorption detection
        self.dom_analyzer.process_dom(dom, self.last_price, 0)

    def _on_bar_complete(self, tf: str, bar: Bar):
        """Update indicators when a bar completes."""
        # ── ATR ─────────────────────────────────────────────────────
        tr = bar.high - bar.low  # True Range (simplified — no prev close gap)

        # Tick bars use the "tick" ATR slot, not the label slot
        atr_key = "tick" if tf == self._tick_bar_label else tf
        if atr_key in self._tr_history:
            self._tr_history[atr_key].append(tr)
            if len(self._tr_history[atr_key]) >= 2:
                self.atr[atr_key] = sum(self._tr_history[atr_key]) / len(self._tr_history[atr_key])

        # ── Per-bar tick delta (5m only) ─────────────────────────────
        if tf == "5m":
            self.bar_buy_vol  = self._bar_buy_vol
            self.bar_sell_vol = self._bar_sell_vol
            self.bar_delta    = self._bar_buy_vol - self._bar_sell_vol
            self._bar_buy_vol  = 0.0
            self._bar_sell_vol = 0.0

            # ── Volume Analysis (5m bars) ────────────────────────────
            # Relative volume (climax / dry-up detection)
            self._vol_history_5m.append(bar.volume)
            if len(self._vol_history_5m) >= 5:
                self.avg_vol_5m = sum(self._vol_history_5m) / len(self._vol_history_5m)
                self.vol_climax_ratio = (bar.volume / self.avg_vol_5m
                                         if self.avg_vol_5m > 0 else 1.0)

            # Delta + price history for divergence detection
            self._delta_history_5m.append(self.bar_delta)
            self._high_history_5m.append(bar.high)
            self._low_history_5m.append(bar.low)

            # VSA (Volume Spread Analysis)
            # High-volume wide-range bar with close in middle = ABSORPTION (reversal warning)
            # High-volume wide-range bar closing at extreme = EFFORT (continuation)
            # Low-volume narrow-range bar = TEST (spring/dry-up, continuation)
            if self.avg_vol_5m > 0 and self.atr.get("5m", 0) > 0:
                _bar_range  = bar.high - bar.low
                _close_pos  = ((bar.close - bar.low) / _bar_range
                               if _bar_range > 0 else 0.5)
                _vol_ratio  = bar.volume / self.avg_vol_5m
                _range_ratio = _bar_range / self.atr["5m"]
                if _vol_ratio >= 2.0 and _range_ratio >= 1.5:
                    if   _close_pos >= 0.70: self.vsa_signal_5m = "EFFORT_UP"
                    elif _close_pos <= 0.30: self.vsa_signal_5m = "EFFORT_DOWN"
                    else:                    self.vsa_signal_5m = "ABSORPTION"
                elif _vol_ratio <= 0.50 and _range_ratio <= 0.60:
                    if   _close_pos >= 0.60: self.vsa_signal_5m = "TEST_UP"
                    elif _close_pos <= 0.40: self.vsa_signal_5m = "TEST_DOWN"
                    else:                    self.vsa_signal_5m = "NEUTRAL"
                else:
                    self.vsa_signal_5m = "NEUTRAL"

        # ── EMA 5/9/21 (5m only) ────────────────────────────────────
        if tf == "5m":
            self._ema5_count += 1
            self._ema9_count += 1
            self._ema21_count += 1

            # EMA5 — precision timing (price reverts to EMA5 3-5x more reliably than EMA9)
            if self._ema5_count <= 5:
                self.ema5 = (self.ema5 * (self._ema5_count - 1) + bar.close) / self._ema5_count
            else:
                k5 = 2.0 / (5 + 1)
                self.ema5 = bar.close * k5 + self.ema5 * (1 - k5)

            if self._ema9_count <= 9:
                self.ema9 = (self.ema9 * (self._ema9_count - 1) + bar.close) / self._ema9_count
            else:
                k9 = 2.0 / (9 + 1)
                self.ema9 = bar.close * k9 + self.ema9 * (1 - k9)

            if self._ema21_count <= 21:
                self.ema21 = (self.ema21 * (self._ema21_count - 1) + bar.close) / self._ema21_count
            else:
                k21 = 2.0 / (21 + 1)
                self.ema21 = bar.close * k21 + self.ema21 * (1 - k21)

            # ── MACD (5m bars only) ──────────────────────────────────
            # Uses existing EMA9/EMA21 — no new periods needed.
            # macd_line = spread between the two EMAs we already track.
            # macd_signal = 9-period EMA of that spread (warm after 30 bars).
            if self._ema9_count >= 9 and self._ema21_count >= 9:
                _macd_line = self.ema9 - self.ema21
                self._macd_signal_count += 1
                if self._macd_signal_count <= 9:
                    # Simple average warm-up for signal line
                    self.macd_signal = (self.macd_signal * (self._macd_signal_count - 1) + _macd_line) / self._macd_signal_count
                else:
                    k_macd = 2.0 / (9 + 1)
                    self.macd_signal = _macd_line * k_macd + self.macd_signal * (1 - k_macd)
                self.macd_line = _macd_line
                self.macd_histogram_prev = self.macd_histogram
                self.macd_histogram = _macd_line - self.macd_signal

        # ── EMA 9/21 on 15m bars (context timeframe) ────────────────
        # 15m is the correct context TF for 5m entries (3:1 ratio).
        # "price > EMA9_15m > EMA21_15m" = medium-term uptrend established.
        # Responds ~3x faster than 60m, still filters intraday noise.
        if tf == "15m":
            self._ema9_15m_count  += 1
            self._ema21_15m_count += 1
            if self._ema9_15m_count <= 9:
                self.ema9_15m = (self.ema9_15m * (self._ema9_15m_count - 1) + bar.close) / self._ema9_15m_count
            else:
                self.ema9_15m = bar.close * (2.0 / 10) + self.ema9_15m * (8.0 / 10)
            if self._ema21_15m_count <= 21:
                self.ema21_15m = (self.ema21_15m * (self._ema21_15m_count - 1) + bar.close) / self._ema21_15m_count
            else:
                self.ema21_15m = bar.close * (2.0 / 22) + self.ema21_15m * (20.0 / 22)

        # ── TF Bias ─────────────────────────────────────────────────
        # 2-of-3 voting: majority of last 3 bars rising = BULLISH, falling = BEARISH
        if tf == self._tick_bar_label:
            # Tick bars have their own bias tracker
            tick_bars = list(self.bars_tick.completed)
            if len(tick_bars) >= 3:
                closes = [b.close for b in tick_bars[-3:]]
                rises = sum(1 for i in range(len(closes)-1) if closes[i+1] > closes[i])
                falls = sum(1 for i in range(len(closes)-1) if closes[i+1] < closes[i])
                if rises >= 2:
                    self.tf_bias_tick = "BULLISH"
                elif falls >= 2:
                    self.tf_bias_tick = "BEARISH"
                else:
                    self.tf_bias_tick = "NEUTRAL"
        else:
            bars = list(self._get_builder(tf).completed)
            if len(bars) >= 3:
                recent_closes = [b.close for b in bars[-3:]]
                rises = sum(1 for i in range(len(recent_closes) - 1) if recent_closes[i + 1] > recent_closes[i])
                falls = sum(1 for i in range(len(recent_closes) - 1) if recent_closes[i + 1] < recent_closes[i])
                if rises >= 2:
                    self.tf_bias[tf] = "BULLISH"
                elif falls >= 2:
                    self.tf_bias[tf] = "BEARISH"
                else:
                    self.tf_bias[tf] = "NEUTRAL"

            self._update_tf_votes()

        # ── Phase 4B: Update session levels BEFORE callbacks fire ──
        # So any downstream strategy callback sees fresh PMH/PML/RTH
        # opening levels / opening_type in the next snapshot() read.
        if self.session_levels is not None and tf in ("1m", "5m"):
            try:
                from datetime import datetime as _dt
                bar_now = _dt.fromtimestamp(bar.end_time) if bar.end_time else _dt.now()
                self.session_levels.update(
                    now_ct=bar_now,
                    bar_1m=bar if tf == "1m" else None,
                    bar_5m=bar if tf == "5m" else None,
                )
            except Exception as e:
                _log.warning(f"[SESSION_LEVELS] update on {tf} bar failed: {e!r}")

        # ── Fire callbacks ──────────────────────────────────────────
        for cb in self._on_bar_callbacks:
            try:
                cb(tf, bar)
            except Exception as e:
                import logging as _logging
                _logging.getLogger("TickAggregator").error(
                    f"Bar callback error on {tf} bar: {e}", exc_info=True)

    def _get_builder(self, tf: str):
        m = {"1m": self.bars_1m, "5m": self.bars_5m,
             "15m": self.bars_15m, "60m": self.bars_60m}
        if tf == self._tick_bar_label and self.bars_tick:
            return self.bars_tick
        return m[tf]

    def _update_tf_votes(self):
        self.tf_votes_bullish = sum(1 for v in self.tf_bias.values() if v == "BULLISH")
        self.tf_votes_bearish = sum(1 for v in self.tf_bias.values() if v == "BEARISH")

    def set_avwap_anchors(self, pd_high: float = 0.0, pd_low: float = 0.0,
                          pd_close: float = 0.0) -> None:
        """Set prior-day anchor prices for AVWAP calculation.

        Call once per session after the session resets (e.g. at RTH open 8:30 CT).
        Passing 0 for any anchor disables that AVWAP line.

        Anchor data comes from NT8 via a "session_info" message (High[1], Low[1], Close[1]
        on a daily bar series in TickStreamer.cs), or from a daily data lookup at session start.

        Args:
            pd_high:  Prior day's high price
            pd_low:   Prior day's low price
            pd_close: Prior day's close / settlement price
        """
        for _anchor, _val in (
            (self._avwap_pd_high,  pd_high),
            (self._avwap_pd_low,   pd_low),
            (self._avwap_pd_close, pd_close),
        ):
            if _val > 0:
                _anchor["pv"]     = 0.0
                _anchor["pv2"]    = 0.0
                _anchor["vol"]    = 0
                _anchor["val"]    = _val   # initialize at anchor price until first tick arrives
                _anchor["active"] = True
            else:
                _anchor["active"] = False
                _anchor["val"]    = 0.0

        self.avwap_pd_high  = self._avwap_pd_high["val"]
        self.avwap_pd_low   = self._avwap_pd_low["val"]
        self.avwap_pd_close = self._avwap_pd_close["val"]
        _log.info(f"[AVWAP] Anchors set — PDH={pd_high:.2f} PDL={pd_low:.2f} PDC={pd_close:.2f}")

    def snapshot(self) -> dict:
        """Return current state as a dict (for dashboard and strategy evaluation)."""
        base = {
            "price": self.last_price,
            "bid": self.last_bid,
            "ask": self.last_ask,
            "vwap":         round(self.vwap, 2),
            "vwap_std":     round(self.vwap_std,    2),
            "vwap_upper1":  round(self.vwap_upper1, 2),
            "vwap_lower1":  round(self.vwap_lower1, 2),
            "vwap_upper2":  round(self.vwap_upper2, 2),
            "vwap_lower2":  round(self.vwap_lower2, 2),
            "avwap_pd_high":  round(self.avwap_pd_high,  2),
            "avwap_pd_low":   round(self.avwap_pd_low,   2),
            "avwap_pd_close": round(self.avwap_pd_close, 2),
            "cvd": round(self.cvd, 1),
            "ema5":      round(self.ema5, 2),
            "ema9":      round(self.ema9, 2),
            "ema21":     round(self.ema21, 2),
            "ema9_15m":  round(self.ema9_15m, 2),
            "ema21_15m": round(self.ema21_15m, 2),
            "macd_line":      round(self.macd_line, 4),
            "macd_signal":    round(self.macd_signal, 4),
            "macd_histogram": round(self.macd_histogram, 4),
            "macd_histogram_prev": round(self.macd_histogram_prev, 4),
            "macd_warm": self._macd_signal_count >= 9,
            "atr_1m":   round(self.atr["1m"],   2),
            "atr_5m":   round(self.atr["5m"],   2),
            "atr_15m":  round(self.atr["15m"],  2),
            "atr_60m":  round(self.atr["60m"],  2),
            "atr_tick": round(self.atr["tick"],  2),   # ATR of the tick bar series
            "tf_bias": dict(self.tf_bias),
            "tf_bias_tick": self.tf_bias_tick,          # Entry-level tick bias
            "tick_bar_size": self._tick_bar_size,
            "tf_votes_bullish": self.tf_votes_bullish,
            "tf_votes_bearish": self.tf_votes_bearish,
            "tick_count": self.tick_count,
            "bars_1m":   self.bars_1m.bar_count,
            "bars_5m":   self.bars_5m.bar_count,
            "bars_15m":  self.bars_15m.bar_count,
            "bars_60m":  self.bars_60m.bar_count,
            "bars_tick": self.bars_tick.bar_count if self.bars_tick else 0,
            # ── Real tick CVD fields ─────────────────────────────────
            "bar_buy_vol":  round(self.bar_buy_vol, 0),
            "bar_sell_vol": round(self.bar_sell_vol, 0),
            "bar_delta":    round(self.bar_delta, 0),
            "cvd_method":   "tick",
            # ── Volume analysis ──────────────────────────────────────
            "avg_vol_5m":        round(self.avg_vol_5m, 0),
            "vol_climax_ratio":  round(self.vol_climax_ratio, 2),
            "vsa_signal_5m":     self.vsa_signal_5m,
            "delta_history_5m":  list(self._delta_history_5m),
            "high_history_5m":   list(self._high_history_5m),
            "low_history_5m":    list(self._low_history_5m),
            # ── DOM depth fields ─────────────────────────────────────
            "dom_bid_stack": round(self.dom_bid_stack, 0),
            "dom_ask_stack": round(self.dom_ask_stack, 0),
            "dom_imbalance": round(self.dom_imbalance, 3),
            "dom_bid_heavy": self.dom_bid_heavy,
            "dom_ask_heavy": self.dom_ask_heavy,
            "dom_depth":     round(self.dom_bid_stack, 0),   # backward compat
            # ── DOM analyzer (iceberg/absorption) ────────────────────
            "dom_signal": self.dom_analyzer.get_dom_signal(),
            "regime":        None,   # set by session_manager, placeholder here
        }

        # ── Phase 4B: enrich with session levels (prior day + live opening)
        if self.session_levels is not None:
            try:
                base.update(self.session_levels.get_levels_dict())
            except Exception as e:
                _log.warning(f"[SESSION_LEVELS] get_levels_dict failed: {e!r}")

        return base

    # ─── State Persistence (survive restarts) ──────────────────────
    def save_state(self, path: str) -> None:
        """Save completed bars + indicator state to disk. Called on every bar."""
        try:
            def _bars_to_list(builder: BarBuilder) -> list:
                return [
                    {"o": b.open, "h": b.high, "l": b.low, "c": b.close,
                     "v": b.volume, "tc": b.tick_count,
                     "st": b.start_time, "et": b.end_time}
                    for b in builder.completed
                ]

            state = {
                "saved_at": time.time(),
                "saved_day": datetime.now().strftime("%Y-%m-%d"),
                "bars_1m":  _bars_to_list(self.bars_1m),
                "bars_5m":  _bars_to_list(self.bars_5m),
                "bars_15m": _bars_to_list(self.bars_15m),
                "bars_60m": _bars_to_list(self.bars_60m),
                "bars_tick": _bars_to_list(self.bars_tick) if self.bars_tick else [],
                "bar_counts": {
                    "1m":   self.bars_1m.bar_count,
                    "5m":   self.bars_5m.bar_count,
                    "15m":  self.bars_15m.bar_count,
                    "60m":  self.bars_60m.bar_count,
                    "tick": self.bars_tick.bar_count if self.bars_tick else 0,
                },
                "atr": dict(self.atr),
                "tr_history": {k: list(v) for k, v in self._tr_history.items()},
                "ema5": self.ema5,
                "ema5_count": self._ema5_count,
                "ema9": self.ema9,
                "ema21": self.ema21,
                "ema9_count": self._ema9_count,
                "ema21_count": self._ema21_count,
                "ema9_15m":       self.ema9_15m,
                "ema21_15m":      self.ema21_15m,
                "ema9_15m_count":  self._ema9_15m_count,
                "ema21_15m_count": self._ema21_15m_count,
                "vwap": self.vwap,
                "vwap_cum_pv":  self._vwap_cum_pv,
                "vwap_cum_pv2": self._vwap_cum_pv2,
                "vwap_cum_vol": self._vwap_cum_vol,
                "vwap_std":     self.vwap_std,
                "vwap_day": str(self._vwap_day) if self._vwap_day else None,
                "avwap_pd_high_state":  dict(self._avwap_pd_high),
                "avwap_pd_low_state":   dict(self._avwap_pd_low),
                "avwap_pd_close_state": dict(self._avwap_pd_close),
                "cvd_session": self._cvd_session,
                "avg_vol_5m":       self.avg_vol_5m,
                "vol_climax_ratio": self.vol_climax_ratio,
                "vol_history_5m":   list(self._vol_history_5m),
                "delta_history_5m": list(self._delta_history_5m),
                "high_history_5m":  list(self._high_history_5m),
                "low_history_5m":   list(self._low_history_5m),
                "vsa_signal_5m":    self.vsa_signal_5m,
                "tf_bias": dict(self.tf_bias),
                "tf_bias_tick": self.tf_bias_tick,
                "macd_line": self.macd_line,
                "macd_signal": self.macd_signal,
                "macd_histogram": self.macd_histogram,
                "macd_histogram_prev": self.macd_histogram_prev,
                "macd_signal_count": self._macd_signal_count,
                "last_price": self.last_price,
                "tick_count": self.tick_count,
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, path)  # Atomic write
        except Exception as e:
            _log.debug(f"State save error: {e}")

    def restore_state(self, path: str) -> bool:
        """Restore bars + indicators from disk. Call BEFORE processing ticks.

        Returns True if state was successfully restored.
        """
        try:
            if not os.path.exists(path):
                return False

            with open(path, "r") as f:
                state = json.load(f)

            # Only restore same-day state (indicators reset on new day)
            saved_day = state.get("saved_day")
            today = datetime.now().strftime("%Y-%m-%d")
            if saved_day != today:
                _log.info(f"[RESTORE] Stale state from {saved_day}, skipping (today={today})")
                return False

            # Check freshness — reject state older than 2 hours
            saved_at = state.get("saved_at", 0)
            if time.time() - saved_at > 7200:
                _log.info("[RESTORE] State older than 2 hours, skipping")
                return False

            def _list_to_bars(builder: BarBuilder, bar_list: list):
                builder.completed.clear()
                for b in bar_list:
                    bar = Bar(
                        open=b["o"], high=b["h"], low=b["l"], close=b["c"],
                        volume=b["v"], tick_count=b.get("tc", 0),
                        start_time=b.get("st", 0), end_time=b.get("et", 0),
                    )
                    builder.completed.append(bar)
                builder._started = False  # Will re-start on next tick

            _list_to_bars(self.bars_1m,  state.get("bars_1m",  []))
            _list_to_bars(self.bars_5m,  state.get("bars_5m",  []))
            _list_to_bars(self.bars_15m, state.get("bars_15m", []))
            _list_to_bars(self.bars_60m, state.get("bars_60m", []))
            if self.bars_tick:
                _list_to_bars(self.bars_tick, state.get("bars_tick", []))

            counts = state.get("bar_counts", {})
            self.bars_1m.bar_count  = counts.get("1m",  len(self.bars_1m.completed))
            self.bars_5m.bar_count  = counts.get("5m",  len(self.bars_5m.completed))
            self.bars_15m.bar_count = counts.get("15m", len(self.bars_15m.completed))
            self.bars_60m.bar_count = counts.get("60m", len(self.bars_60m.completed))
            if self.bars_tick:
                self.bars_tick.bar_count = counts.get("tick", len(self.bars_tick.completed))

            # Restore ATR — merge into current dict so new keys (e.g. "tick")
            # added after the state was saved are preserved rather than dropped.
            saved_atr = state.get("atr", {})
            for k, v in saved_atr.items():
                self.atr[k] = v
            # Guarantee new keys always exist (forward-compat with old save files)
            self.atr.setdefault("tick", 0.0)

            for k, v in state.get("tr_history", {}).items():
                if k in self._tr_history:
                    self._tr_history[k] = deque(v, maxlen=14)

            # Restore EMAs
            self.ema5 = state.get("ema5", 0.0)
            self._ema5_count = state.get("ema5_count", 0)
            self.ema9 = state.get("ema9", 0.0)
            self.ema21 = state.get("ema21", 0.0)
            self._ema9_count = state.get("ema9_count", 0)
            self._ema21_count = state.get("ema21_count", 0)
            self.ema9_15m  = state.get("ema9_15m", 0.0)
            self.ema21_15m = state.get("ema21_15m", 0.0)
            self._ema9_15m_count  = state.get("ema9_15m_count", 0)
            self._ema21_15m_count = state.get("ema21_15m_count", 0)

            # Restore VWAP + σ bands + AVWAP
            self.vwap              = state.get("vwap", 0.0)
            self._vwap_cum_pv      = state.get("vwap_cum_pv",  0.0)
            self._vwap_cum_pv2     = state.get("vwap_cum_pv2", 0.0)
            self._vwap_cum_vol     = state.get("vwap_cum_vol", 0)
            self.vwap_std          = state.get("vwap_std", 0.0)
            self.vwap_upper1       = self.vwap + self.vwap_std
            self.vwap_lower1       = self.vwap - self.vwap_std
            self.vwap_upper2       = self.vwap + 2.0 * self.vwap_std
            self.vwap_lower2       = self.vwap - 2.0 * self.vwap_std
            for _attr, _key in (
                (self._avwap_pd_high,  "avwap_pd_high_state"),
                (self._avwap_pd_low,   "avwap_pd_low_state"),
                (self._avwap_pd_close, "avwap_pd_close_state"),
            ):
                _saved = state.get(_key)
                if _saved:
                    _attr.update(_saved)
            self.avwap_pd_high  = self._avwap_pd_high["val"]
            self.avwap_pd_low   = self._avwap_pd_low["val"]
            self.avwap_pd_close = self._avwap_pd_close["val"]
            vday = state.get("vwap_day")
            if vday and vday != "None":
                from datetime import date
                self._vwap_day = date.fromisoformat(vday)

            # Restore CVD + volume analysis
            self._cvd_session       = state.get("cvd_session", 0.0)
            self.cvd                = self._cvd_session
            self.avg_vol_5m         = state.get("avg_vol_5m", 0.0)
            self.vol_climax_ratio   = state.get("vol_climax_ratio", 1.0)
            self.vsa_signal_5m      = state.get("vsa_signal_5m", "NEUTRAL")
            _vh = state.get("vol_history_5m", [])
            self._vol_history_5m    = deque(_vh, maxlen=20)
            _dh = state.get("delta_history_5m", [])
            self._delta_history_5m  = deque(_dh, maxlen=10)
            _hh = state.get("high_history_5m", [])
            self._high_history_5m   = deque(_hh, maxlen=10)
            _lh = state.get("low_history_5m", [])
            self._low_history_5m    = deque(_lh, maxlen=10)
            self.tf_bias      = state.get("tf_bias",      self.tf_bias)
            self.tf_bias_tick = state.get("tf_bias_tick", "NEUTRAL")
            self._update_tf_votes()

            # Restore MACD
            self.macd_line           = state.get("macd_line", 0.0)
            self.macd_signal         = state.get("macd_signal", 0.0)
            self.macd_histogram      = state.get("macd_histogram", 0.0)
            self.macd_histogram_prev = state.get("macd_histogram_prev", 0.0)
            self._macd_signal_count  = state.get("macd_signal_count", 0)

            self.last_price = state.get("last_price", 0.0)
            self.tick_count = state.get("tick_count", 0)

            tick_bars = len(self.bars_tick.completed) if self.bars_tick else 0
            n_bars = (len(self.bars_1m.completed) + len(self.bars_5m.completed) +
                      len(self.bars_15m.completed) + len(self.bars_60m.completed))
            _log.info(f"[RESTORE] Loaded {n_bars} time bars + {tick_bars} tick bars | "
                      f"ATR_5m={self.atr['5m']:.2f} ATR_tick={self.atr['tick']:.2f} "
                      f"EMA9={self.ema9:.2f} VWAP={self.vwap:.2f} CVD={self.cvd:.0f} "
                      f"TF={self.tf_bias} TF_tick={self.tf_bias_tick}")
            return True

        except Exception as e:
            _log.warning(f"[RESTORE] Failed: {e}")
            return False
