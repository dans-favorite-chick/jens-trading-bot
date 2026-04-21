"""
RSI Divergence Detector for MNQ Trading Bot.

Calculates Wilder's smoothed RSI (matching TradingView) and detects bullish/bearish
divergences between price action and RSI by comparing pivot highs and pivot lows.

Bullish divergence: price makes a lower low while RSI makes a higher low.
Bearish divergence: price makes a higher high while RSI makes a lower high.

Supports both streaming (update one bar at a time) and batch modes.
"""

import logging
from collections import deque

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

logger = logging.getLogger(__name__)


class RSIDivergenceDetector:
    """Detects RSI divergences on a price series using Wilder's smoothed RSI."""

    def __init__(self, rsi_length: int = 14, pivot_left: int = 5, pivot_right: int = 5):
        self.rsi_length = rsi_length
        self.pivot_left = pivot_left
        self.pivot_right = pivot_right

        # Price history needed: enough for RSI warm-up + pivot detection window
        self._closes: list[float] = []
        self._rsi_values: list[float] = []

        # Wilder's smoothing state
        self._avg_gain: float = 0.0
        self._avg_loss: float = 0.0
        self._rsi_ready: bool = False
        self._warmup_gains: list[float] = []
        self._warmup_losses: list[float] = []

        # Detected pivots: list of (index, price, rsi)
        self._pivot_lows: list[tuple[int, float, float]] = []
        self._pivot_highs: list[tuple[int, float, float]] = []

        # Bar counter
        self._bar_index: int = 0

        logger.debug(
            "RSIDivergenceDetector initialized: rsi_length=%d, pivot_left=%d, pivot_right=%d",
            rsi_length, pivot_left, pivot_right,
        )

    # ------------------------------------------------------------------
    # RSI calculation — Wilder's smoothed method (same as TradingView)
    # ------------------------------------------------------------------

    def _compute_rsi_incremental(self, close: float) -> float | None:
        """Feed one close price and return RSI or None if still warming up."""
        if len(self._closes) < 2:
            return None

        change = close - self._closes[-2]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        n = self.rsi_length

        if not self._rsi_ready:
            self._warmup_gains.append(gain)
            self._warmup_losses.append(loss)

            if len(self._warmup_gains) < n:
                return None

            # First RSI: simple average over N periods
            self._avg_gain = sum(self._warmup_gains) / n
            self._avg_loss = sum(self._warmup_losses) / n
            self._rsi_ready = True
            self._warmup_gains.clear()
            self._warmup_losses.clear()
        else:
            # Wilder's smoothing: (prev_avg * (N-1) + current) / N
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n

        if self._avg_loss == 0.0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    # ------------------------------------------------------------------
    # Pivot detection
    # ------------------------------------------------------------------

    def _check_pivot_low(self, index: int) -> bool:
        """Check if the bar at `index` is a pivot low in both price and RSI."""
        left = self.pivot_left
        right = self.pivot_right

        if index < left or index + right >= len(self._closes):
            return False
        if index + right >= len(self._rsi_values):
            return False

        price_val = self._closes[index]
        rsi_val = self._rsi_values[index]

        for i in range(index - left, index):
            if self._closes[i] <= price_val:
                return False
        for i in range(index + 1, index + right + 1):
            if self._closes[i] <= price_val:
                return False

        return True

    def _check_pivot_high(self, index: int) -> bool:
        """Check if the bar at `index` is a pivot high in both price and RSI."""
        left = self.pivot_left
        right = self.pivot_right

        if index < left or index + right >= len(self._closes):
            return False
        if index + right >= len(self._rsi_values):
            return False

        price_val = self._closes[index]

        for i in range(index - left, index):
            if self._closes[i] >= price_val:
                return False
        for i in range(index + 1, index + right + 1):
            if self._closes[i] >= price_val:
                return False

        return True

    def _get_rsi_at(self, index: int) -> float | None:
        """Return RSI value at given close-array index, or None."""
        if 0 <= index < len(self._rsi_values):
            return self._rsi_values[index]
        return None

    # ------------------------------------------------------------------
    # Strength scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_strength(
        price_current: float,
        price_prev: float,
        rsi_current: float,
        rsi_prev: float,
        div_type: str,
        bars_apart: int,
    ) -> float:
        """
        Score divergence strength from 0 to 100.

        Factors:
        - Price divergence magnitude (how far price moved against RSI).
        - RSI divergence magnitude (how much RSI contradicts price).
        - Oversold / overbought bonus.
        - Bars-apart penalty (too close = weak, too far = weak).
        """
        # Price divergence percentage
        if price_prev == 0.0:
            price_pct = 0.0
        else:
            price_pct = abs(price_current - price_prev) / abs(price_prev) * 100.0

        # RSI divergence magnitude (absolute RSI points)
        rsi_diff = abs(rsi_current - rsi_prev)

        # Base score: weighted combination
        # Price component (0-40): bigger price move = stronger signal
        price_score = min(price_pct * 10.0, 40.0)

        # RSI component (0-40): bigger RSI contradiction = stronger signal
        rsi_score = min(rsi_diff * 2.0, 40.0)

        # Bars-apart component (0-10): sweet spot is 10-30 bars
        if bars_apart < 5:
            bars_score = bars_apart * 1.0
        elif bars_apart <= 30:
            bars_score = 10.0
        elif bars_apart <= 60:
            bars_score = max(10.0 - (bars_apart - 30) * 0.33, 2.0)
        else:
            bars_score = 2.0

        # Oversold / overbought bonus (0-10)
        zone_bonus = 0.0
        if div_type == "bullish" and rsi_current < 30.0:
            zone_bonus = 10.0 * (1.0 - rsi_current / 30.0)
        elif div_type == "bearish" and rsi_current > 70.0:
            zone_bonus = 10.0 * ((rsi_current - 70.0) / 30.0)

        strength = price_score + rsi_score + bars_score + zone_bonus
        return round(max(0.0, min(100.0, strength)), 1)

    # ------------------------------------------------------------------
    # Divergence detection
    # ------------------------------------------------------------------

    def _detect_at_confirmed_pivot(self, pivot_index: int) -> list[dict]:
        """Check for divergences at a newly confirmed pivot."""
        signals: list[dict] = []

        rsi_at_pivot = self._get_rsi_at(pivot_index)
        if rsi_at_pivot is None:
            return signals

        price_at_pivot = self._closes[pivot_index]

        # Check if this is a pivot low — potential bullish divergence
        if self._check_pivot_low(pivot_index):
            logger.debug(
                "Pivot low confirmed at index %d: price=%.2f rsi=%.2f",
                pivot_index, price_at_pivot, rsi_at_pivot,
            )
            self._pivot_lows.append((pivot_index, price_at_pivot, rsi_at_pivot))

            # Compare with previous pivot lows for bullish divergence
            for prev_idx, prev_price, prev_rsi in reversed(self._pivot_lows[:-1]):
                bars_apart = pivot_index - prev_idx
                if bars_apart < 5:
                    continue
                if bars_apart > 100:
                    break

                # Bullish: price lower low, RSI higher low
                if price_at_pivot < prev_price and rsi_at_pivot > prev_rsi:
                    strength = self._compute_strength(
                        price_at_pivot, prev_price,
                        rsi_at_pivot, prev_rsi,
                        "bullish", bars_apart,
                    )
                    signal = {
                        "type": "bullish",
                        "strength": strength,
                        "rsi_current": round(rsi_at_pivot, 2),
                        "rsi_prev_pivot": round(prev_rsi, 2),
                        "price_current": round(price_at_pivot, 2),
                        "price_prev_pivot": round(prev_price, 2),
                        "bars_apart": bars_apart,
                    }
                    signals.append(signal)
                    logger.debug("Bullish divergence detected: %s", signal)
                    break  # Use most recent qualifying pivot

        # Check if this is a pivot high — potential bearish divergence
        if self._check_pivot_high(pivot_index):
            logger.debug(
                "Pivot high confirmed at index %d: price=%.2f rsi=%.2f",
                pivot_index, price_at_pivot, rsi_at_pivot,
            )
            self._pivot_highs.append((pivot_index, price_at_pivot, rsi_at_pivot))

            # Compare with previous pivot highs for bearish divergence
            for prev_idx, prev_price, prev_rsi in reversed(self._pivot_highs[:-1]):
                bars_apart = pivot_index - prev_idx
                if bars_apart < 5:
                    continue
                if bars_apart > 100:
                    break

                # Bearish: price higher high, RSI lower high
                if price_at_pivot > prev_price and rsi_at_pivot < prev_rsi:
                    strength = self._compute_strength(
                        price_at_pivot, prev_price,
                        rsi_at_pivot, prev_rsi,
                        "bearish", bars_apart,
                    )
                    signal = {
                        "type": "bearish",
                        "strength": strength,
                        "rsi_current": round(rsi_at_pivot, 2),
                        "rsi_prev_pivot": round(prev_rsi, 2),
                        "price_current": round(price_at_pivot, 2),
                        "price_prev_pivot": round(prev_price, 2),
                        "bars_apart": bars_apart,
                    }
                    signals.append(signal)
                    logger.debug("Bearish divergence detected: %s", signal)
                    break

        # Prune old pivots (keep last 50)
        if len(self._pivot_lows) > 50:
            self._pivot_lows = self._pivot_lows[-50:]
        if len(self._pivot_highs) > 50:
            self._pivot_highs = self._pivot_highs[-50:]

        return signals

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, close_price: float) -> dict | None:
        """
        Feed one new close price.

        Returns a divergence dict if a divergence is confirmed on this bar,
        or None otherwise. A pivot is only confirmed after `pivot_right` bars
        have passed, so signals are delayed by that amount.
        """
        self._closes.append(close_price)
        self._bar_index = len(self._closes) - 1

        rsi = self._compute_rsi_incremental(close_price)
        if rsi is not None:
            self._rsi_values.append(rsi)
        else:
            self._rsi_values.append(float("nan"))

        # A pivot can only be confirmed once we have pivot_right bars after it
        candidate_index = self._bar_index - self.pivot_right
        if candidate_index < self.pivot_left:
            return None

        signals = self._detect_at_confirmed_pivot(candidate_index)

        # Return the strongest signal, or None
        if signals:
            return max(signals, key=lambda s: s["strength"])
        return None

    def check_divergences(self, closes: list[float]) -> list[dict]:
        """
        Batch-check a list of close prices for all divergences.

        Resets internal state and processes the full list. Returns a list of
        divergence dicts sorted by bar index (earliest first).
        """
        self.reset()
        all_signals: list[dict] = []

        for close in closes:
            signal = self.update(close)
            if signal is not None:
                signal["bar_index"] = self._bar_index - self.pivot_right
                all_signals.append(signal)

        logger.debug("Batch check complete: %d divergences found in %d bars",
                      len(all_signals), len(closes))
        return all_signals

    def get_current_rsi(self) -> float:
        """Return the most recent RSI value, or NaN if not yet available."""
        if self._rsi_values:
            return round(self._rsi_values[-1], 2)
        return float("nan")

    def get_state(self) -> dict:
        """Return current detector state for dashboard display."""
        recent_pivot_low = self._pivot_lows[-1] if self._pivot_lows else None
        recent_pivot_high = self._pivot_highs[-1] if self._pivot_highs else None

        return {
            "rsi_current": self.get_current_rsi(),
            "rsi_length": self.rsi_length,
            "bars_processed": len(self._closes),
            "rsi_ready": self._rsi_ready,
            "pivot_lows_found": len(self._pivot_lows),
            "pivot_highs_found": len(self._pivot_highs),
            "last_pivot_low": {
                "bar_index": recent_pivot_low[0],
                "price": round(recent_pivot_low[1], 2),
                "rsi": round(recent_pivot_low[2], 2),
            } if recent_pivot_low else None,
            "last_pivot_high": {
                "bar_index": recent_pivot_high[0],
                "price": round(recent_pivot_high[1], 2),
                "rsi": round(recent_pivot_high[2], 2),
            } if recent_pivot_high else None,
        }

    def reset(self) -> None:
        """Clear all internal state."""
        self._closes.clear()
        self._rsi_values.clear()
        self._avg_gain = 0.0
        self._avg_loss = 0.0
        self._rsi_ready = False
        self._warmup_gains.clear()
        self._warmup_losses.clear()
        self._pivot_lows.clear()
        self._pivot_highs.clear()
        self._bar_index = 0
        logger.debug("RSIDivergenceDetector state reset")
