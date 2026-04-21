"""
Phoenix Bot — Geometric Chart Pattern Detector

Detects chart patterns from completed OHLC bars across multiple timeframes:
  - Triangles (ascending, descending, symmetrical)
  - Head & Shoulders / Inverse H&S
  - Double Top / Double Bottom
  - Cup & Handle
  - Channels (ascending, descending)
  - Flags / Pennants
  - Wedges (rising, falling)

Uses pivot-based swing detection, linear regression trendlines, and
slope convergence/divergence rules for classification.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

try:
    import numpy as np
except ImportError:
    np = None  # Fallback: pattern detection disabled without numpy

logger = logging.getLogger("ChartPatterns")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SwingPoint:
    index: int
    price: float
    type: str        # "HIGH" or "LOW"
    bar_time: float


@dataclass
class ChartPattern:
    pattern: str          # "ascending_triangle", "head_shoulders", etc.
    direction: str        # "LONG" or "SHORT"
    timeframe: str        # "5m", "15m", "60m"
    strength: float       # 0-100
    target_price: float   # Projected from pattern geometry
    neckline: float       # Key breakout/breakdown level
    description: str
    confirmed: bool = False
    timestamp: float = 0.0

# ---------------------------------------------------------------------------
# Per-timeframe state
# ---------------------------------------------------------------------------

class _TimeframeState:
    """Tracks bars and swing points for one timeframe."""

    def __init__(self, pivot_lookback: int = 5, max_swings: int = 60):
        self.bars: deque = deque(maxlen=200)
        self.swing_highs: deque[SwingPoint] = deque(maxlen=max_swings)
        self.swing_lows: deque[SwingPoint] = deque(maxlen=max_swings)
        self.bar_count: int = 0
        self.pivot_lookback: int = pivot_lookback

# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class ChartPatternDetector:
    """
    Geometric chart pattern detector.

    Feed completed bars via update(); read results via get_active_patterns()
    or get_confluence_score(). All public reads are thread-safe.
    """

    SLOPE_FLAT_THRESHOLD = 0.0003   # slope considered flat
    TOLERANCE_PCT = 0.005           # 0.5 % price tolerance for double top/bottom
    MIN_FLAG_BARS = 5
    MAX_FLAG_BARS = 15
    PATTERN_EXPIRY_BARS = 60        # invalidate after N bars without confirmation

    def __init__(self, tick_size: float = 0.25, pivot_lookback: int = 5):
        self._tick_size = tick_size
        self._pivot_lookback = pivot_lookback
        self._states: dict[str, _TimeframeState] = {}
        self._patterns: list[ChartPattern] = []
        self._lock = threading.Lock()
        logger.info("[CHART PATTERNS] Detector initialised (pivot_lb=%d)", pivot_lookback)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, timeframe: str, bar) -> list[ChartPattern]:
        """Feed a completed bar. Returns any newly detected patterns."""
        if np is None:
            return []

        state = self._states.setdefault(
            timeframe, _TimeframeState(self._pivot_lookback)
        )
        state.bars.append(bar)
        state.bar_count += 1

        self._detect_swings(state)

        new_patterns: list[ChartPattern] = []
        for detector in (
            self._detect_triangles,
            self._detect_head_shoulders,
            self._detect_double_top_bottom,
            self._detect_wedges,
            self._detect_channels,
            self._detect_flags_pennants,
            self._detect_cup_handle,
        ):
            found = detector(state, timeframe)
            if found:
                new_patterns.extend(found)

        if new_patterns:
            with self._lock:
                self._patterns.extend(new_patterns)
                self._prune_expired(state.bar_count)
            for p in new_patterns:
                logger.info(
                    "[CHART PATTERNS] %s on %s | dir=%s str=%.0f tgt=%.2f neck=%.2f %s",
                    p.pattern, p.timeframe, p.direction, p.strength,
                    p.target_price, p.neckline,
                    "CONFIRMED" if p.confirmed else "forming",
                )
        return new_patterns

    def get_active_patterns(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "pattern": p.pattern,
                    "direction": p.direction,
                    "timeframe": p.timeframe,
                    "strength": round(p.strength, 1),
                    "target_price": round(p.target_price, 2),
                    "neckline": round(p.neckline, 2),
                    "description": p.description,
                    "confirmed": p.confirmed,
                    "timestamp": p.timestamp,
                }
                for p in self._patterns
            ]

    def get_confluence_score(self, direction: str) -> dict:
        with self._lock:
            aligned = [p for p in self._patterns if p.direction == direction]
            opposing = [p for p in self._patterns
                        if p.direction and p.direction != direction]
        score = (sum(p.strength for p in aligned)
                 - sum(p.strength for p in opposing) * 0.5)
        return {
            "score": round(score, 1),
            "aligned": len(aligned),
            "opposing": len(opposing),
            "patterns": [p.pattern for p in aligned[:3]],
        }

    def to_dict(self) -> dict:
        active = self.get_active_patterns()
        return {
            "available": True,
            "active_patterns": active,
            "pattern_count": len(active),
        }

    # ------------------------------------------------------------------
    # Swing detection
    # ------------------------------------------------------------------

    def _detect_swings(self, state: _TimeframeState) -> None:
        bars = state.bars
        lb = state.pivot_lookback
        if len(bars) < 2 * lb + 1:
            return

        idx = len(bars) - lb - 1  # candidate bar index
        candidate = bars[idx]
        bar_time = getattr(candidate, "time", time.time())

        # Swing high
        is_high = all(
            candidate.high >= bars[idx - i].high and candidate.high >= bars[idx + i].high
            for i in range(1, lb + 1)
        )
        if is_high:
            sp = SwingPoint(state.bar_count - lb - 1, candidate.high, "HIGH", bar_time)
            if not state.swing_highs or state.swing_highs[-1].index != sp.index:
                state.swing_highs.append(sp)

        # Swing low
        is_low = all(
            candidate.low <= bars[idx - i].low and candidate.low <= bars[idx + i].low
            for i in range(1, lb + 1)
        )
        if is_low:
            sp = SwingPoint(state.bar_count - lb - 1, candidate.low, "LOW", bar_time)
            if not state.swing_lows or state.swing_lows[-1].index != sp.index:
                state.swing_lows.append(sp)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fit_line(points: list[SwingPoint]) -> tuple[float, float, float]:
        """Return (slope, intercept, r_squared) from linear regression."""
        x = np.array([p.index for p in points], dtype=float)
        y = np.array([p.price for p in points], dtype=float)
        coeffs = np.polyfit(x, y, 1)
        slope, intercept = coeffs[0], coeffs[1]
        y_pred = np.polyval(coeffs, x)
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        return slope, intercept, r2

    @staticmethod
    def _normalise_slope(slope: float, price: float) -> float:
        """Normalise slope relative to price level for comparison."""
        return slope / price if price > 0 else slope

    def _last_close(self, state: _TimeframeState) -> float:
        return state.bars[-1].close if state.bars else 0.0

    def _strength_score(self, touch_points: int, duration_bars: int,
                        r2: float, max_touches: int = 8) -> float:
        """Composite strength 0-100."""
        touch_score = min(touch_points / max_touches, 1.0) * 40
        dur_score = min(duration_bars / 80, 1.0) * 30
        fit_score = r2 * 30
        return min(touch_score + dur_score + fit_score, 100.0)

    def _prune_expired(self, current_bar: int) -> None:
        cutoff = current_bar - self.PATTERN_EXPIRY_BARS
        self._patterns = [
            p for p in self._patterns
            if p.confirmed or p.timestamp >= cutoff
        ]

    def _already_detected(self, pattern_name: str, timeframe: str) -> bool:
        for p in self._patterns:
            if p.pattern == pattern_name and p.timeframe == timeframe:
                return True
        return False

    # ------------------------------------------------------------------
    # Triangle detection
    # ------------------------------------------------------------------

    def _detect_triangles(self, state: _TimeframeState,
                          timeframe: str) -> list[ChartPattern]:
        highs = list(state.swing_highs)
        lows = list(state.swing_lows)
        if len(highs) < 2 or len(lows) < 2:
            return []

        recent_highs = highs[-4:] if len(highs) >= 4 else highs[-2:]
        recent_lows = lows[-4:] if len(lows) >= 4 else lows[-2:]

        h_slope, h_int, h_r2 = self._fit_line(recent_highs)
        l_slope, l_int, l_r2 = self._fit_line(recent_lows)

        price = self._last_close(state)
        hn = self._normalise_slope(h_slope, price)
        ln = self._normalise_slope(l_slope, price)
        flat = self.SLOPE_FLAT_THRESHOLD

        results: list[ChartPattern] = []
        duration = recent_highs[-1].index - recent_lows[0].index

        # Ascending triangle: flat highs, rising lows
        if abs(hn) < flat and ln > flat:
            if self._already_detected("ascending_triangle", timeframe):
                return results
            neckline = recent_highs[-1].price
            height = neckline - recent_lows[-1].price
            confirmed = price > neckline
            results.append(ChartPattern(
                pattern="ascending_triangle",
                direction="LONG",
                timeframe=timeframe,
                strength=self._strength_score(
                    len(recent_highs) + len(recent_lows), duration, min(h_r2, l_r2)),
                target_price=neckline + height,
                neckline=neckline,
                description=f"Ascending triangle — flat resistance {neckline:.2f}, rising lows",
                confirmed=confirmed,
                timestamp=state.bar_count,
            ))

        # Descending triangle: falling highs, flat lows
        elif hn < -flat and abs(ln) < flat:
            if self._already_detected("descending_triangle", timeframe):
                return results
            neckline = recent_lows[-1].price
            height = recent_highs[-1].price - neckline
            confirmed = price < neckline
            results.append(ChartPattern(
                pattern="descending_triangle",
                direction="SHORT",
                timeframe=timeframe,
                strength=self._strength_score(
                    len(recent_highs) + len(recent_lows), duration, min(h_r2, l_r2)),
                target_price=neckline - height,
                neckline=neckline,
                description=f"Descending triangle — flat support {neckline:.2f}, falling highs",
                confirmed=confirmed,
                timestamp=state.bar_count,
            ))

        # Symmetrical triangle: converging slopes
        elif hn < -flat and ln > flat:
            if self._already_detected("symmetrical_triangle", timeframe):
                return results
            mid = (recent_highs[-1].price + recent_lows[-1].price) / 2
            height = recent_highs[0].price - recent_lows[0].price
            direction = "LONG" if price > mid else "SHORT"
            target = mid + height if direction == "LONG" else mid - height
            results.append(ChartPattern(
                pattern="symmetrical_triangle",
                direction=direction,
                timeframe=timeframe,
                strength=self._strength_score(
                    len(recent_highs) + len(recent_lows), duration, min(h_r2, l_r2)),
                target_price=target,
                neckline=mid,
                description="Symmetrical triangle — converging trendlines",
                confirmed=False,
                timestamp=state.bar_count,
            ))

        return results

    # ------------------------------------------------------------------
    # Head & Shoulders
    # ------------------------------------------------------------------

    def _detect_head_shoulders(self, state: _TimeframeState,
                               timeframe: str) -> list[ChartPattern]:
        results: list[ChartPattern] = []
        highs = list(state.swing_highs)
        lows = list(state.swing_lows)

        # Regular H&S (bearish): need 3 highs, 2 lows between them
        if len(highs) >= 3 and len(lows) >= 2:
            ls, head, rs = highs[-3], highs[-2], highs[-1]
            nl1, nl2 = lows[-2], lows[-1]

            if (head.price > ls.price and head.price > rs.price
                    and abs(ls.price - rs.price) / head.price < 0.02
                    and nl1.index > ls.index and nl2.index > head.index
                    and not self._already_detected("head_shoulders", timeframe)):
                neckline = (nl1.price + nl2.price) / 2
                height = head.price - neckline
                price = self._last_close(state)
                confirmed = price < neckline
                duration = rs.index - ls.index
                results.append(ChartPattern(
                    pattern="head_shoulders",
                    direction="SHORT",
                    timeframe=timeframe,
                    strength=self._strength_score(5, duration, 0.8),
                    target_price=neckline - height,
                    neckline=neckline,
                    description=f"Head & Shoulders — neckline {neckline:.2f}",
                    confirmed=confirmed,
                    timestamp=state.bar_count,
                ))

        # Inverse H&S (bullish): need 3 lows, 2 highs between them
        if len(lows) >= 3 and len(highs) >= 2:
            ls, head, rs = lows[-3], lows[-2], lows[-1]
            nl1, nl2 = highs[-2], highs[-1]

            if (head.price < ls.price and head.price < rs.price
                    and abs(ls.price - rs.price) / head.price < 0.02
                    and nl1.index > ls.index and nl2.index > head.index
                    and not self._already_detected("inverse_head_shoulders", timeframe)):
                neckline = (nl1.price + nl2.price) / 2
                height = neckline - head.price
                price = self._last_close(state)
                confirmed = price > neckline
                duration = rs.index - ls.index
                results.append(ChartPattern(
                    pattern="inverse_head_shoulders",
                    direction="LONG",
                    timeframe=timeframe,
                    strength=self._strength_score(5, duration, 0.8),
                    target_price=neckline + height,
                    neckline=neckline,
                    description=f"Inverse H&S — neckline {neckline:.2f}",
                    confirmed=confirmed,
                    timestamp=state.bar_count,
                ))

        return results

    # ------------------------------------------------------------------
    # Double Top / Bottom
    # ------------------------------------------------------------------

    def _detect_double_top_bottom(self, state: _TimeframeState,
                                  timeframe: str) -> list[ChartPattern]:
        results: list[ChartPattern] = []
        highs = list(state.swing_highs)
        lows = list(state.swing_lows)
        price = self._last_close(state)
        tol = self.TOLERANCE_PCT

        # Double top
        if len(highs) >= 2 and len(lows) >= 1:
            h1, h2 = highs[-2], highs[-1]
            valley = min((l for l in lows if l.index > h1.index and l.index < h2.index),
                         key=lambda l: l.price, default=None)
            if (valley and abs(h1.price - h2.price) / h1.price < tol
                    and not self._already_detected("double_top", timeframe)):
                neckline = valley.price
                height = h1.price - neckline
                confirmed = price < neckline
                duration = h2.index - h1.index
                results.append(ChartPattern(
                    pattern="double_top",
                    direction="SHORT",
                    timeframe=timeframe,
                    strength=self._strength_score(3, duration, 0.75),
                    target_price=neckline - height,
                    neckline=neckline,
                    description=f"Double Top at ~{h1.price:.2f}, neckline {neckline:.2f}",
                    confirmed=confirmed,
                    timestamp=state.bar_count,
                ))

        # Double bottom
        if len(lows) >= 2 and len(highs) >= 1:
            l1, l2 = lows[-2], lows[-1]
            peak = max((h for h in highs if h.index > l1.index and h.index < l2.index),
                       key=lambda h: h.price, default=None)
            if (peak and abs(l1.price - l2.price) / l1.price < tol
                    and not self._already_detected("double_bottom", timeframe)):
                neckline = peak.price
                height = neckline - l1.price
                confirmed = price > neckline
                duration = l2.index - l1.index
                results.append(ChartPattern(
                    pattern="double_bottom",
                    direction="LONG",
                    timeframe=timeframe,
                    strength=self._strength_score(3, duration, 0.75),
                    target_price=neckline + height,
                    neckline=neckline,
                    description=f"Double Bottom at ~{l1.price:.2f}, neckline {neckline:.2f}",
                    confirmed=confirmed,
                    timestamp=state.bar_count,
                ))

        return results

    # ------------------------------------------------------------------
    # Wedges
    # ------------------------------------------------------------------

    def _detect_wedges(self, state: _TimeframeState,
                       timeframe: str) -> list[ChartPattern]:
        highs = list(state.swing_highs)
        lows = list(state.swing_lows)
        if len(highs) < 3 or len(lows) < 3:
            return []

        recent_highs = highs[-4:]
        recent_lows = lows[-4:]
        h_slope, h_int, h_r2 = self._fit_line(recent_highs)
        l_slope, l_int, l_r2 = self._fit_line(recent_lows)

        price = self._last_close(state)
        hn = self._normalise_slope(h_slope, price)
        ln = self._normalise_slope(l_slope, price)
        flat = self.SLOPE_FLAT_THRESHOLD
        results: list[ChartPattern] = []
        duration = recent_highs[-1].index - recent_lows[0].index

        # Both slopes same direction and converging
        converging = abs(hn) > abs(ln) if hn > 0 else abs(hn) < abs(ln)

        # Rising wedge (bearish): both up, converging
        if hn > flat and ln > flat and converging:
            if self._already_detected("rising_wedge", timeframe):
                return []
            neckline = recent_lows[-1].price
            height = recent_highs[-1].price - neckline
            results.append(ChartPattern(
                pattern="rising_wedge",
                direction="SHORT",
                timeframe=timeframe,
                strength=self._strength_score(
                    len(recent_highs) + len(recent_lows), duration, min(h_r2, l_r2)),
                target_price=neckline - height,
                neckline=neckline,
                description="Rising wedge — bearish reversal",
                confirmed=price < neckline,
                timestamp=state.bar_count,
            ))

        # Falling wedge (bullish): both down, converging
        elif hn < -flat and ln < -flat and not converging:
            if self._already_detected("falling_wedge", timeframe):
                return []
            neckline = recent_highs[-1].price
            height = neckline - recent_lows[-1].price
            results.append(ChartPattern(
                pattern="falling_wedge",
                direction="LONG",
                timeframe=timeframe,
                strength=self._strength_score(
                    len(recent_highs) + len(recent_lows), duration, min(h_r2, l_r2)),
                target_price=neckline + height,
                neckline=neckline,
                description="Falling wedge — bullish reversal",
                confirmed=price > neckline,
                timestamp=state.bar_count,
            ))

        return results

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def _detect_channels(self, state: _TimeframeState,
                         timeframe: str) -> list[ChartPattern]:
        highs = list(state.swing_highs)
        lows = list(state.swing_lows)
        if len(highs) < 3 or len(lows) < 3:
            return []

        recent_highs = highs[-4:]
        recent_lows = lows[-4:]
        h_slope, h_int, h_r2 = self._fit_line(recent_highs)
        l_slope, l_int, l_r2 = self._fit_line(recent_lows)

        price = self._last_close(state)
        hn = self._normalise_slope(h_slope, price)
        ln = self._normalise_slope(l_slope, price)
        flat = self.SLOPE_FLAT_THRESHOLD
        results: list[ChartPattern] = []

        # Parallel = slopes within 30% of each other
        if abs(hn) < flat and abs(ln) < flat:
            return []
        if min(abs(hn), abs(ln)) == 0:
            return []
        ratio = abs(hn - ln) / max(abs(hn), abs(ln))
        if ratio > 0.30:
            return []

        duration = recent_highs[-1].index - recent_lows[0].index

        if hn > flat and ln > flat:
            name = "ascending_channel"
            direction = "LONG"
        elif hn < -flat and ln < -flat:
            name = "descending_channel"
            direction = "SHORT"
        else:
            return []

        if self._already_detected(name, timeframe):
            return []

        channel_width = recent_highs[-1].price - recent_lows[-1].price
        neckline = recent_lows[-1].price if direction == "LONG" else recent_highs[-1].price
        target = price + channel_width if direction == "LONG" else price - channel_width

        results.append(ChartPattern(
            pattern=name,
            direction=direction,
            timeframe=timeframe,
            strength=self._strength_score(
                len(recent_highs) + len(recent_lows), duration, min(h_r2, l_r2)),
            target_price=target,
            neckline=neckline,
            description=f"{name.replace('_', ' ').title()} — parallel trendlines",
            confirmed=False,
            timestamp=state.bar_count,
        ))
        return results

    # ------------------------------------------------------------------
    # Flags & Pennants
    # ------------------------------------------------------------------

    def _detect_flags_pennants(self, state: _TimeframeState,
                               timeframe: str) -> list[ChartPattern]:
        bars = state.bars
        if len(bars) < 20:
            return []

        highs = list(state.swing_highs)
        lows = list(state.swing_lows)
        if len(highs) < 2 or len(lows) < 2:
            return []

        # Only consider recent swings within flag window
        recent_highs = [h for h in highs if h.index > state.bar_count - self.MAX_FLAG_BARS]
        recent_lows = [l for l in lows if l.index > state.bar_count - self.MAX_FLAG_BARS]
        if len(recent_highs) < 2 or len(recent_lows) < 2:
            return []

        consol_bars = recent_highs[-1].index - recent_lows[0].index
        if not (self.MIN_FLAG_BARS <= abs(consol_bars) <= self.MAX_FLAG_BARS):
            return []

        # Detect prior impulse move (pole)
        pole_start = max(0, len(bars) - self.MAX_FLAG_BARS - 10)
        pole_end = len(bars) - self.MAX_FLAG_BARS
        if pole_end <= pole_start:
            return []

        pole_bars = list(bars)[pole_start:pole_end]
        if not pole_bars:
            return []
        pole_low = min(b.low for b in pole_bars)
        pole_high = max(b.high for b in pole_bars)
        pole_range = pole_high - pole_low

        consol_high = max(h.price for h in recent_highs)
        consol_low = min(l.price for l in recent_lows)
        consol_range = consol_high - consol_low

        # Consolidation should be tight relative to pole
        if consol_range == 0 or pole_range / consol_range < 2.0:
            return []

        h_slope, _, h_r2 = self._fit_line(recent_highs)
        l_slope, _, l_r2 = self._fit_line(recent_lows)
        price = self._last_close(state)
        hn = self._normalise_slope(h_slope, price)
        ln = self._normalise_slope(l_slope, price)
        flat = self.SLOPE_FLAT_THRESHOLD

        # Determine prior trend direction from pole
        pole_close = pole_bars[-1].close
        pole_open = pole_bars[0].open
        bullish_pole = pole_close > pole_open

        results: list[ChartPattern] = []

        # Pennant: converging lines
        if hn < -flat and ln > flat:
            name = "pennant"
        # Flag: roughly parallel
        elif abs(hn - ln) / max(abs(hn), abs(ln), 1e-9) < 0.30:
            name = "flag"
        else:
            return []

        full_name = f"bull_{name}" if bullish_pole else f"bear_{name}"
        if self._already_detected(full_name, timeframe):
            return []

        direction = "LONG" if bullish_pole else "SHORT"
        neckline = consol_high if bullish_pole else consol_low
        target = neckline + pole_range if bullish_pole else neckline - pole_range

        results.append(ChartPattern(
            pattern=full_name,
            direction=direction,
            timeframe=timeframe,
            strength=self._strength_score(
                len(recent_highs) + len(recent_lows), abs(consol_bars), min(h_r2, l_r2)),
            target_price=target,
            neckline=neckline,
            description=f"{full_name.replace('_', ' ').title()} — continuation pattern",
            confirmed=price > consol_high if bullish_pole else price < consol_low,
            timestamp=state.bar_count,
        ))
        return results

    # ------------------------------------------------------------------
    # Cup & Handle
    # ------------------------------------------------------------------

    def _detect_cup_handle(self, state: _TimeframeState,
                           timeframe: str) -> list[ChartPattern]:
        lows = list(state.swing_lows)
        highs = list(state.swing_highs)
        if len(lows) < 3 or len(highs) < 2:
            return []

        if self._already_detected("cup_handle", timeframe):
            return []

        # Cup: two highs at similar level with a lower low between them
        h1, h2 = highs[-2], highs[-1]
        cup_lows = [l for l in lows if l.index > h1.index and l.index < h2.index]
        if not cup_lows:
            return []

        cup_bottom = min(cup_lows, key=lambda l: l.price)
        rim_level = (h1.price + h2.price) / 2

        if abs(h1.price - h2.price) / rim_level > 0.015:
            return []

        cup_depth = rim_level - cup_bottom.price
        if cup_depth <= 0:
            return []

        # Handle: small pullback after h2 (most recent lows)
        handle_lows = [l for l in lows if l.index > h2.index]
        if not handle_lows:
            return []

        handle_low = min(handle_lows, key=lambda l: l.price)
        handle_depth = rim_level - handle_low.price

        # Handle should retrace < 50% of cup depth
        if handle_depth > cup_depth * 0.5 or handle_depth < 0:
            return []

        price = self._last_close(state)
        duration = h2.index - h1.index
        confirmed = price > rim_level

        return [ChartPattern(
            pattern="cup_handle",
            direction="LONG",
            timeframe=timeframe,
            strength=self._strength_score(len(cup_lows) + 2, duration, 0.7),
            target_price=rim_level + cup_depth,
            neckline=rim_level,
            description=f"Cup & Handle — rim {rim_level:.2f}, depth {cup_depth:.2f}",
            confirmed=confirmed,
            timestamp=state.bar_count,
        )]
