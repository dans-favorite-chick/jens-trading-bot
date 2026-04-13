"""
Phoenix Bot — Initial Balance Breakout Strategy

The most statistically validated NQ strategy:
- 96.2% of NQ days break the Initial Balance
- 74.56% win rate on 15-min ORB
- Narrow IB (< 0.5x ATR): 98.7% break probability, bigger extensions
- Wide IB (> 1.5x ATR): 66.7% break, smaller extensions

REGIME-AWARE: Only trades during OPEN_MOMENTUM and MID_MORNING.
"""

from datetime import datetime, timezone
from strategies.base_strategy import BaseStrategy, Signal
from config.settings import TICK_SIZE
from core.candlestick_patterns import CandlestickAnalyzer, get_pattern_confluence

# Regime-specific overrides — only fire in morning windows
_REGIME_OVERRIDES = {
    "OPEN_MOMENTUM": {"allowed": True, "min_confluence": 2.0},
    "MID_MORNING":   {"allowed": True, "min_confluence": 2.5},
    # All other regimes: not allowed
}


class IBBreakout(BaseStrategy):
    name = "ib_breakout"

    def __init__(self, config: dict):
        super().__init__(config)
        self._ib_high: float | None = None
        self._ib_low: float | None = None
        self._ib_set: bool = False
        self._ib_date: str | None = None
        self._traded_today: dict = {"LONG": False, "SHORT": False}
        self._ib_bars_1m: list = []  # 1m bars collected during IB window

    def _reset_daily(self, today: str):
        """Reset IB tracking for a new day."""
        self._ib_high = None
        self._ib_low = None
        self._ib_set = False
        self._ib_date = today
        self._traded_today = {"LONG": False, "SHORT": False}
        self._ib_bars_1m = []

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        regime = session_info.get("regime", "UNKNOWN")
        if self.config.get("skip_regime_overrides", False):
            overrides = {"allowed": True}
        else:
            overrides = _REGIME_OVERRIDES.get(regime, {})

        # Only trade in allowed regimes (unless all_regimes override is set)
        if not self.config.get("all_regimes", False) and not overrides.get("allowed", False):
            return None

        # Need at least some bars
        if len(bars_1m) < 1:
            return None

        # Config params
        ib_minutes = self.config.get("ib_minutes", 30)
        target_extension = self.config.get("target_extension", 1.5)
        max_ib_width_atr = self.config.get("max_ib_width_atr_mult", 1.5)
        stop_at_midpoint = self.config.get("stop_at_ib_midpoint", False)

        # ── Detect current date, reset IB daily ─────────────────────
        price = market.get("price", 0)
        if price <= 0:
            return None

        # Use last 1m bar timestamp to determine date
        last_bar = bars_1m[-1]
        try:
            bar_dt = datetime.fromtimestamp(last_bar.end_time)
        except (OSError, ValueError, TypeError):
            bar_dt = datetime.now()
        today = bar_dt.strftime("%Y-%m-%d")

        if self._ib_date != today:
            self._reset_daily(today)

        # ── Step 1: Build the Initial Balance (first 30 min) ─────────
        # IB = high and low of the first N 1-minute bars after session open
        # OPEN_MOMENTUM starts at 8:30 CST, IB covers 8:30-9:00
        if not self._ib_set:
            # Count completed 1m bars in this session
            # We track bars we've seen during IB building
            ib_bar_count = ib_minutes  # 30 bars for 30 minutes of 1m bars

            # Accumulate 1m bars into our IB tracking list
            # Only add bars we haven't seen yet
            seen_count = len(self._ib_bars_1m)
            if len(bars_1m) > seen_count:
                for bar in bars_1m[seen_count:]:
                    self._ib_bars_1m.append(bar)

            # Update running IB high/low from all collected bars
            for bar in self._ib_bars_1m:
                if self._ib_high is None or bar.high > self._ib_high:
                    self._ib_high = bar.high
                if self._ib_low is None or bar.low < self._ib_low:
                    self._ib_low = bar.low

            # IB is set once we have enough bars
            if len(self._ib_bars_1m) >= ib_bar_count:
                self._ib_set = True

            # Not set yet — still building
            return None

        # ── IB is set — validate width ──────────────────────────────
        ib_width = self._ib_high - self._ib_low
        if ib_width <= 0:
            return None

        # Check IB width vs ATR — skip if too wide (low break-extension)
        atr_5m = market.get("atr_5m", 0)
        atr_1m = market.get("atr_1m", 0)
        atr = atr_5m if atr_5m > 0 else atr_1m
        if atr > 0 and ib_width > (max_ib_width_atr * atr):
            return None  # IB too wide, reduced edge

        # ── Step 2: Watch for breakout ──────────────────────────────
        # Use last completed 1m bar close for breakout confirmation
        last_close = bars_1m[-1].close

        direction = None
        if last_close > self._ib_high and not self._traded_today["LONG"]:
            direction = "LONG"
        elif last_close < self._ib_low and not self._traded_today["SHORT"]:
            direction = "SHORT"

        if direction is None:
            return None

        # Mark direction as traded for today
        self._traded_today[direction] = True

        # ── Step 3: Calculate stop and target ───────────────────────
        ib_mid = (self._ib_high + self._ib_low) / 2.0

        if direction == "LONG":
            stop_price = ib_mid if stop_at_midpoint else self._ib_low
            stop_distance = price - stop_price
            target_price = self._ib_high + (ib_width * target_extension)
        else:  # SHORT
            stop_price = ib_mid if stop_at_midpoint else self._ib_high
            stop_distance = stop_price - price
            target_price = self._ib_low - (ib_width * target_extension)

        if stop_distance <= 0:
            return None

        stop_ticks = max(4, int(stop_distance / TICK_SIZE))
        target_distance = abs(target_price - price)
        target_rr = target_distance / stop_distance if stop_distance > 0 else 1.5

        # ── Confidence scoring ──────────────────────────────────────
        confluences = [f"Regime: {regime}"]
        confidence = 50  # Base confidence for IB breakout

        # Narrow IB bonus (higher break probability and extension)
        if atr > 0:
            ib_atr_ratio = ib_width / atr
            if ib_atr_ratio < 0.5:
                confidence += 20
                confluences.append(f"Narrow IB ({ib_atr_ratio:.2f}x ATR) — high extension")
            elif ib_atr_ratio < 1.0:
                confidence += 10
                confluences.append(f"Normal IB ({ib_atr_ratio:.2f}x ATR)")

        # VWAP confirmation
        vwap = market.get("vwap", 0)
        if vwap > 0:
            if direction == "LONG" and price > vwap:
                confidence += 10
                confluences.append("Price above VWAP")
            elif direction == "SHORT" and price < vwap:
                confidence += 10
                confluences.append("Price below VWAP")

        # CVD confirmation
        cvd = market.get("cvd", 0)
        if direction == "LONG" and cvd > 0:
            confidence += 10
            confluences.append("CVD positive")
        elif direction == "SHORT" and cvd < 0:
            confidence += 10
            confluences.append("CVD negative")

        # ── Candlestick pattern confluence ────────────────────────────
        analyzer = CandlestickAnalyzer()
        candle_bars = bars_1m[-20:] if len(bars_1m) >= 20 else bars_1m
        patterns = analyzer.analyze(candle_bars, tick_size=TICK_SIZE)
        pattern_conf = get_pattern_confluence(patterns, direction)
        if pattern_conf["net_score"] > 30:
            confidence += 15
            confluences.append(f"Candle pattern: {pattern_conf['description']}")
        elif pattern_conf["net_score"] < -30:
            confidence -= 10
            opposed = pattern_conf["strongest_opposed"]
            opposed_name = opposed["pattern"] if opposed else "unknown"
            confluences.append(f"Warning: opposing pattern {opposed_name}")

        confluences.append(f"IB: {self._ib_low:.2f} - {self._ib_high:.2f} (width={ib_width:.2f})")
        confluences.append(f"Stop: {stop_ticks} ticks, Target RR: {target_rr:.2f}")

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=min(100, confidence),
            entry_score=min(60, int(confidence * 0.65)),
            strategy=self.name,
            reason=f"IB Breakout {direction} — price {'above' if direction == 'LONG' else 'below'} IB {'high' if direction == 'LONG' else 'low'}, regime {regime}",
            confluences=confluences,
        )
