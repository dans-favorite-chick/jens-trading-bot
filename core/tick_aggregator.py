"""
Phoenix Bot — Tick Aggregator

Builds bars (1m, 5m, 15m, 60m), ATR, VWAP, EMA, CVD, and multi-TF
bias from raw tick data. Single source of truth — all derived math
lives here, not in NT8.
"""

import time
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    """Accumulates ticks into bars of a given interval."""
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


class TickAggregator:
    """
    Central aggregator that processes raw ticks from the bridge and
    maintains multi-timeframe bars, indicators, and derived signals.
    """

    def __init__(self):
        # Bar builders for each timeframe
        self.bars_1m = BarBuilder(interval_seconds=60)
        self.bars_5m = BarBuilder(interval_seconds=300)
        self.bars_15m = BarBuilder(interval_seconds=900)
        self.bars_60m = BarBuilder(interval_seconds=3600)

        # ATR (14-period) per timeframe
        self.atr = {"1m": 0.0, "5m": 0.0, "15m": 0.0, "60m": 0.0}
        self._tr_history = {
            "1m": deque(maxlen=14),
            "5m": deque(maxlen=14),
            "15m": deque(maxlen=14),
            "60m": deque(maxlen=14),
        }

        # EMA 9/21 on 5m bars
        self.ema9 = 0.0
        self.ema21 = 0.0
        self._ema9_count = 0
        self._ema21_count = 0

        # VWAP (daily reset)
        self._vwap_cum_pv = 0.0  # cumulative(typical_price * volume)
        self._vwap_cum_vol = 0   # cumulative(volume)
        self._vwap_day = None
        self.vwap = 0.0

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

        # DOM analyzer (iceberg + absorption detection)
        self.dom_analyzer = DOMAnalyzer()

        # DOM depth state (updated via process_dom() from bridge)
        self.dom_bid_stack: float = 0.0
        self.dom_ask_stack: float = 0.0
        self.dom_imbalance: float = 0.5   # bid/(bid+ask)
        self.dom_bid_heavy: bool = False   # imbalance > 0.60
        self.dom_ask_heavy: bool = False   # imbalance < 0.40
        self.dom_last_update: float = 0.0

        # Multi-TF bias votes
        self.tf_bias = {"1m": "NEUTRAL", "5m": "NEUTRAL", "15m": "NEUTRAL", "60m": "NEUTRAL"}
        self.tf_votes_bullish = 0
        self.tf_votes_bearish = 0

        # Current tick data
        self.last_price = 0.0
        self.last_bid = 0.0
        self.last_ask = 0.0
        self.tick_count = 0

        # Callbacks for new bar events
        self._on_bar_callbacks: list = []

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

        # ── VWAP ────────────────────────────────────────────────────
        today = datetime.fromtimestamp(ts).date()
        if self._vwap_day != today:
            self._vwap_cum_pv  = 0.0
            self._vwap_cum_vol = 0
            self._cvd_session  = 0.0
            self._bar_buy_vol  = 0.0
            self._bar_sell_vol = 0.0
            self._vwap_day     = today

        typical = (price + (bid if bid > 0 else price) + (ask if ask > 0 else price)) / 3.0
        self._vwap_cum_pv += typical * vol
        self._vwap_cum_vol += vol
        if self._vwap_cum_vol > 0:
            self.vwap = self._vwap_cum_pv / self._vwap_cum_vol

        # ── Build bars ──────────────────────────────────────────────
        completed_bars = []
        for tf_name, builder in [("1m", self.bars_1m), ("5m", self.bars_5m),
                                  ("15m", self.bars_15m), ("60m", self.bars_60m)]:
            bar = builder.add_tick(price, vol, ts)
            if bar:
                completed_bars.append((tf_name, bar))
                self._on_bar_complete(tf_name, bar)

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
        self._tr_history[tf].append(tr)
        if len(self._tr_history[tf]) >= 2:
            self.atr[tf] = sum(self._tr_history[tf]) / len(self._tr_history[tf])

        # ── Per-bar tick delta (5m only) ─────────────────────────────
        if tf == "5m":
            self.bar_buy_vol  = self._bar_buy_vol
            self.bar_sell_vol = self._bar_sell_vol
            self.bar_delta    = self._bar_buy_vol - self._bar_sell_vol
            self._bar_buy_vol  = 0.0
            self._bar_sell_vol = 0.0

        # ── EMA 9/21 (5m only) ──────────────────────────────────────
        if tf == "5m":
            self._ema9_count += 1
            self._ema21_count += 1

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

        # ── TF Bias ─────────────────────────────────────────────────
        # 2-of-3 voting: majority of last 3 bars rising = BULLISH, falling = BEARISH
        # (Old rule required ALL 3 consecutive — way too strict for MNQ chop)
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

        # ── Fire callbacks ──────────────────────────────────────────
        for cb in self._on_bar_callbacks:
            try:
                cb(tf, bar)
            except Exception as e:
                import logging as _logging
                _logging.getLogger("TickAggregator").error(
                    f"Bar callback error on {tf} bar: {e}", exc_info=True)

    def _get_builder(self, tf: str) -> BarBuilder:
        return {"1m": self.bars_1m, "5m": self.bars_5m,
                "15m": self.bars_15m, "60m": self.bars_60m}[tf]

    def _update_tf_votes(self):
        self.tf_votes_bullish = sum(1 for v in self.tf_bias.values() if v == "BULLISH")
        self.tf_votes_bearish = sum(1 for v in self.tf_bias.values() if v == "BEARISH")

    def snapshot(self) -> dict:
        """Return current state as a dict (for dashboard and strategy evaluation)."""
        return {
            "price": self.last_price,
            "bid": self.last_bid,
            "ask": self.last_ask,
            "vwap": round(self.vwap, 2),
            "cvd": round(self.cvd, 1),
            "ema9": round(self.ema9, 2),
            "ema21": round(self.ema21, 2),
            "atr_1m": round(self.atr["1m"], 2),
            "atr_5m": round(self.atr["5m"], 2),
            "atr_15m": round(self.atr["15m"], 2),
            "atr_60m": round(self.atr["60m"], 2),
            "tf_bias": dict(self.tf_bias),
            "tf_votes_bullish": self.tf_votes_bullish,
            "tf_votes_bearish": self.tf_votes_bearish,
            "tick_count": self.tick_count,
            "bars_1m": self.bars_1m.bar_count,
            "bars_5m": self.bars_5m.bar_count,
            "bars_15m": self.bars_15m.bar_count,
            "bars_60m": self.bars_60m.bar_count,
            # ── Real tick CVD fields ─────────────────────────────────
            "bar_buy_vol":  round(self.bar_buy_vol, 0),
            "bar_sell_vol": round(self.bar_sell_vol, 0),
            "bar_delta":    round(self.bar_delta, 0),
            "cvd_method":   "tick",
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
