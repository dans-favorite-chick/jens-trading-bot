"""
Phoenix Bot — ATR-based ZigZag Swing Pivot Detector

Replaces the naive 2-of-3 close rule with structural swing detection.
Pivots are confirmed when price exceeds prior extreme by ATR × multiplier.

Research basis (2026):
- Williams Fractals (5-bar pattern) = too noisy in chop, false signals common
- ATR-based ZigZag = adapts to volatility, recommended for intraday futures

Output: running sequence of (time, price, pivot_type) where pivot_type ∈
  {"HH", "HL", "LH", "LL"}. From the sequence we derive:
  - Trend: HH+HL = UP, LH+LL = DOWN, otherwise SIDEWAYS
  - BOS: price breaks previous swing extreme in trend direction
  - CHoCH: price breaks opposite pivot (first time) = potential reversal

Dual-write with old tf_bias: this module computes structural bias ALONGSIDE
existing tf_bias. Strategies continue using tf_bias until WFO validation approves cutover.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("SwingDetector")

# ─── ATR multiplier for pivot confirmation ──────────────────────────
# Research default: 1.5 × ATR. Higher = fewer but more significant pivots.
DEFAULT_ATR_MULT = 1.5


@dataclass
class Pivot:
    """A confirmed swing high or swing low."""
    ts: datetime
    price: float
    kind: str              # "HIGH" or "LOW"
    classification: str    # "HH", "HL", "LH", "LL" — relative to prior same-kind pivot
    bar_idx: int           # Index in the bar series for reference


@dataclass
class SwingState:
    """
    Rolling state for swing detection over a 5m bar stream.
    Call update(bar) once per completed bar. Internal state advances.
    """
    atr_mult: float = DEFAULT_ATR_MULT
    # Internal state
    direction: str = "UNKNOWN"           # "UP" (seeking HH), "DOWN" (seeking LL), or "UNKNOWN"
    running_high: float = 0.0
    running_high_ts: Optional[datetime] = None
    running_high_idx: int = 0
    running_low: float = 0.0
    running_low_ts: Optional[datetime] = None
    running_low_idx: int = 0
    pivots: list[Pivot] = field(default_factory=list)
    # Last BOS / CHoCH events for the bias output
    last_bos_ts: Optional[datetime] = None
    last_bos_direction: str = ""
    last_choch_ts: Optional[datetime] = None
    last_choch_direction: str = ""

    def _record_pivot(self, kind: str, price: float, ts: datetime, bar_idx: int) -> None:
        """Add a pivot, classifying relative to prior same-kind pivot."""
        prior_same_kind = [p for p in self.pivots if p.kind == kind]
        if not prior_same_kind:
            # First pivot of this kind — can't classify HH vs LH yet
            classification = "FIRST_" + kind
        else:
            last = prior_same_kind[-1]
            if kind == "HIGH":
                classification = "HH" if price > last.price else "LH"
            else:  # LOW
                classification = "HL" if price > last.price else "LL"

        pivot = Pivot(ts=ts, price=price, kind=kind,
                      classification=classification, bar_idx=bar_idx)
        self.pivots.append(pivot)

        # Cap pivot history to last 50 (memory bound)
        if len(self.pivots) > 50:
            self.pivots = self.pivots[-50:]

        # BOS / CHoCH detection — compare to previous pivot of OPPOSITE kind
        self._detect_bos_choch(pivot)

    def _detect_bos_choch(self, new_pivot: Pivot) -> None:
        """
        BOS: new pivot breaks in direction of current trend → continuation
        CHoCH: new pivot breaks against trend (first time) → potential reversal
        """
        # Need at least 2 prior pivots (one each kind) to decide
        if len(self.pivots) < 3:
            return

        # Previous 2 pivots of SAME kind as new_pivot
        same_kind = [p for p in self.pivots[:-1] if p.kind == new_pivot.kind]
        if not same_kind:
            return
        last_same = same_kind[-1]

        if new_pivot.kind == "HIGH":
            # New swing high — check if it broke prior high (BOS up) or is lower (LH = possible CHoCH)
            if new_pivot.classification == "HH":
                # Up-trend continuation
                self.last_bos_ts = new_pivot.ts
                self.last_bos_direction = "UP"
            elif new_pivot.classification == "LH":
                # Failed to break prior high — watch for CHoCH if prior trend was UP
                if self._current_trend() == "UP":
                    self.last_choch_ts = new_pivot.ts
                    self.last_choch_direction = "DOWN_PENDING"
        else:  # LOW
            if new_pivot.classification == "LL":
                self.last_bos_ts = new_pivot.ts
                self.last_bos_direction = "DOWN"
            elif new_pivot.classification == "HL":
                if self._current_trend() == "DOWN":
                    self.last_choch_ts = new_pivot.ts
                    self.last_choch_direction = "UP_PENDING"

    def _current_trend(self) -> str:
        """Infer trend from last 2 pivots of each kind."""
        highs = [p for p in self.pivots if p.kind == "HIGH"]
        lows = [p for p in self.pivots if p.kind == "LOW"]
        if len(highs) >= 2 and len(lows) >= 2:
            hh = highs[-1].classification == "HH"
            hl = lows[-1].classification == "HL"
            lh = highs[-1].classification == "LH"
            ll = lows[-1].classification == "LL"
            if hh and hl:
                return "UP"
            if lh and ll:
                return "DOWN"
        return "SIDEWAYS"

    def update(self, bar, bar_idx: int, atr: float) -> Optional[Pivot]:
        """
        Feed one completed bar. ATR is the 5m ATR at this bar time.
        Returns newly-confirmed Pivot if one just formed, else None.
        """
        if atr <= 0:
            return None

        threshold = atr * self.atr_mult
        high = bar.high
        low = bar.low
        ts = bar.ts if hasattr(bar, "ts") else datetime.now()

        # Track running extremes
        if high > self.running_high:
            self.running_high = high
            self.running_high_ts = ts
            self.running_high_idx = bar_idx
        if low < self.running_low or self.running_low == 0.0:
            self.running_low = low
            self.running_low_ts = ts
            self.running_low_idx = bar_idx

        new_pivot: Optional[Pivot] = None

        # Direction-specific pivot confirmation
        if self.direction == "UP":
            # Currently seeking HH. A swing LOW confirms if price pulls back > threshold from running_high
            if (self.running_high - low) >= threshold:
                # Confirm running_high as swing high, reset direction
                self._record_pivot("HIGH", self.running_high, self.running_high_ts,
                                    self.running_high_idx)
                new_pivot = self.pivots[-1]
                self.direction = "DOWN"
                self.running_low = low
                self.running_low_ts = ts
                self.running_low_idx = bar_idx
        elif self.direction == "DOWN":
            if (high - self.running_low) >= threshold:
                self._record_pivot("LOW", self.running_low, self.running_low_ts,
                                    self.running_low_idx)
                new_pivot = self.pivots[-1]
                self.direction = "UP"
                self.running_high = high
                self.running_high_ts = ts
                self.running_high_idx = bar_idx
        else:  # UNKNOWN — pick a direction once we have any confirmed move
            if (high - self.running_low) >= threshold:
                self.direction = "UP"
            elif (self.running_high - low) >= threshold:
                self.direction = "DOWN"

        return new_pivot

    def to_dict(self) -> dict:
        """Serializable snapshot for dashboard / market snapshot enrichment."""
        trend = self._current_trend()
        highs = [p for p in self.pivots if p.kind == "HIGH"]
        lows = [p for p in self.pivots if p.kind == "LOW"]
        return {
            "trend": trend,
            "last_high": highs[-1].price if highs else 0.0,
            "last_high_class": highs[-1].classification if highs else "",
            "last_low": lows[-1].price if lows else 0.0,
            "last_low_class": lows[-1].classification if lows else "",
            "pivot_count": len(self.pivots),
            "last_bos_direction": self.last_bos_direction,
            "last_bos_ago_s": (datetime.now() - self.last_bos_ts).total_seconds() if self.last_bos_ts else -1,
            "last_choch_direction": self.last_choch_direction,
            "last_choch_ago_s": (datetime.now() - self.last_choch_ts).total_seconds() if self.last_choch_ts else -1,
            "direction_seeking": self.direction,
        }


def bias_from_swings(state: SwingState) -> str:
    """
    Convert swing state to a BULLISH / BEARISH / NEUTRAL bias label.
    This REPLACES the naive 2-of-3 close count from tick_aggregator.
    """
    trend = state._current_trend()
    if trend == "UP":
        # Recent CHoCH flips it to caution
        if state.last_choch_direction == "DOWN_PENDING":
            return "NEUTRAL"  # Warning, pending confirmation
        return "BULLISH"
    elif trend == "DOWN":
        if state.last_choch_direction == "UP_PENDING":
            return "NEUTRAL"
        return "BEARISH"
    return "NEUTRAL"
