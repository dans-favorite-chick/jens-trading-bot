"""
Phoenix Bot -- Candlestick Pattern Recognition Engine

Detects classic and modern candlestick patterns on completed bars and
returns them as structured data that strategies use as confluence factors.

Patterns are grouped by bar count:
  - Single-bar:  Hammer, Inverted Hammer, Shooting Star, Hanging Man,
                 Doji variants, Marubozu
  - Two-bar:     Engulfing, Piercing Line, Dark Cloud Cover, Tweezers
  - Three-bar:   Morning/Evening Star, Three White Soldiers/Black Crows,
                 Harami
  - Chart (10+): Bull/Bear Flag, Double Top/Bottom
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _body(bar) -> float:
    """Absolute body size in price units."""
    return abs(bar.close - bar.open)


def _body_ticks(bar, tick_size: float) -> float:
    return _body(bar) / tick_size if tick_size > 0 else 0.0


def _upper_wick(bar) -> float:
    return bar.high - max(bar.open, bar.close)


def _lower_wick(bar) -> float:
    return min(bar.open, bar.close) - bar.low


def _upper_wick_ticks(bar, tick_size: float) -> float:
    return _upper_wick(bar) / tick_size if tick_size > 0 else 0.0


def _lower_wick_ticks(bar, tick_size: float) -> float:
    return _lower_wick(bar) / tick_size if tick_size > 0 else 0.0


def _is_bullish(bar) -> bool:
    return bar.close > bar.open


def _is_bearish(bar) -> bool:
    return bar.close < bar.open


def _midpoint(bar) -> float:
    return (bar.open + bar.close) / 2.0


def _bar_range(bar) -> float:
    return bar.high - bar.low


def _small_body(bar, tick_size: float, threshold: float = 3.0) -> bool:
    return _body_ticks(bar, tick_size) < threshold


def _within_ticks(a: float, b: float, tick_size: float, n: float = 1.0) -> bool:
    return abs(a - b) <= n * tick_size


def _recent_trend(bars, lookback: int = 5) -> str:
    """Return 'UP', 'DOWN', or 'FLAT' based on last *lookback* bars."""
    if len(bars) < lookback + 1:
        return "FLAT"
    segment = bars[-(lookback + 1):]
    ups = sum(1 for i in range(1, len(segment)) if segment[i].close > segment[i - 1].close)
    downs = len(segment) - 1 - ups
    if ups >= lookback * 0.7:
        return "UP"
    if downs >= lookback * 0.7:
        return "DOWN"
    return "FLAT"


# ---------------------------------------------------------------------------
# CandlestickAnalyzer
# ---------------------------------------------------------------------------

class CandlestickAnalyzer:
    """
    Detects candlestick patterns on completed bar data.
    Patterns are scored by reliability and direction.

    Used by strategies as additional confluence: if a strategy signal
    aligns with a candlestick pattern, confidence increases.
    """

    def analyze(self, bars: list, tick_size: float = 0.25) -> list[dict]:
        """
        Analyze recent bars for patterns.

        Args:
            bars: List of Bar objects (most recent last)
            tick_size: Instrument tick size for wick/body calculations

        Returns:
            List of detected patterns sorted by strength (strongest first):
            [{
                "pattern": "Bullish Engulfing",
                "direction": "LONG",
                "strength": 75,
                "bar_index": -1,
                "description": "Current bar engulfs prior bearish bar",
            }]
        """
        if not bars:
            return []

        results: list[dict] = []

        # Single-bar patterns (need >= 1 bar)
        if len(bars) >= 1:
            results.extend(self._detect_single(bars, tick_size))

        # Two-bar patterns (need >= 2)
        if len(bars) >= 2:
            results.extend(self._detect_two_bar(bars, tick_size))

        # Three-bar patterns (need >= 3)
        if len(bars) >= 3:
            results.extend(self._detect_three_bar(bars, tick_size))

        # Chart patterns (need >= 10)
        if len(bars) >= 10:
            results.extend(self._detect_chart_patterns(bars, tick_size))

        # Sort strongest first
        results.sort(key=lambda p: p["strength"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Single-bar patterns
    # ------------------------------------------------------------------
    def _detect_single(self, bars: list, ts: float) -> list[dict]:
        patterns: list[dict] = []
        bar = bars[-1]
        body = _body_ticks(bar, ts)
        upper = _upper_wick_ticks(bar, ts)
        lower = _lower_wick_ticks(bar, ts)
        trend = _recent_trend(bars)

        # --- Doji variants ---
        if body < 1.1:  # open ~= close within 1 tick
            if lower >= 4 and upper < 1.5:
                # Dragonfly Doji
                patterns.append({
                    "pattern": "Dragonfly Doji",
                    "direction": "LONG",
                    "strength": 60,
                    "bar_index": -1,
                    "description": "Open/close near high, long lower wick -- bullish reversal",
                })
            elif upper >= 4 and lower < 1.5:
                # Gravestone Doji
                patterns.append({
                    "pattern": "Gravestone Doji",
                    "direction": "SHORT",
                    "strength": 60,
                    "bar_index": -1,
                    "description": "Open/close near low, long upper wick -- bearish reversal",
                })
            elif upper >= 4 and lower >= 4:
                # Long-legged Doji
                patterns.append({
                    "pattern": "Long-Legged Doji",
                    "direction": "NEUTRAL",
                    "strength": 45,
                    "bar_index": -1,
                    "description": "Long wicks both sides -- high indecision",
                })
            else:
                # Standard Doji
                direction = "SHORT" if trend == "UP" else ("LONG" if trend == "DOWN" else "NEUTRAL")
                patterns.append({
                    "pattern": "Doji",
                    "direction": direction,
                    "strength": 45,
                    "bar_index": -1,
                    "description": "Open equals close -- indecision, potential reversal",
                })

        # --- Hammer / Hanging Man ---
        if body > 0 and lower >= 2 * body and upper < body:
            if trend == "DOWN":
                patterns.append({
                    "pattern": "Hammer",
                    "direction": "LONG",
                    "strength": 65,
                    "bar_index": -1,
                    "description": "Small body near top, long lower wick after downtrend -- bullish reversal",
                })
            elif trend == "UP":
                patterns.append({
                    "pattern": "Hanging Man",
                    "direction": "SHORT",
                    "strength": 55,
                    "bar_index": -1,
                    "description": "Hammer shape at top of uptrend -- bearish warning",
                })
            else:
                # No clear trend -- still note the shape
                patterns.append({
                    "pattern": "Hammer",
                    "direction": "LONG",
                    "strength": 55,
                    "bar_index": -1,
                    "description": "Small body near top, long lower wick -- potential bullish reversal",
                })

        # --- Inverted Hammer / Shooting Star ---
        if body > 0 and upper >= 2 * body and lower < body:
            if trend == "DOWN":
                patterns.append({
                    "pattern": "Inverted Hammer",
                    "direction": "LONG",
                    "strength": 55,
                    "bar_index": -1,
                    "description": "Small body near bottom, long upper wick after downtrend -- bullish",
                })
            elif trend == "UP":
                patterns.append({
                    "pattern": "Shooting Star",
                    "direction": "SHORT",
                    "strength": 65,
                    "bar_index": -1,
                    "description": "Small body near bottom, long upper wick at top of uptrend -- bearish reversal",
                })
            else:
                patterns.append({
                    "pattern": "Shooting Star",
                    "direction": "SHORT",
                    "strength": 55,
                    "bar_index": -1,
                    "description": "Small body near bottom, long upper wick -- potential bearish reversal",
                })

        # --- Marubozu ---
        if body >= 4 and upper < 1.1 and lower < 1.1:
            if _is_bullish(bar):
                patterns.append({
                    "pattern": "Bullish Marubozu",
                    "direction": "LONG",
                    "strength": 70,
                    "bar_index": -1,
                    "description": "Full bullish body, no wicks -- strong buying momentum",
                })
            else:
                patterns.append({
                    "pattern": "Bearish Marubozu",
                    "direction": "SHORT",
                    "strength": 70,
                    "bar_index": -1,
                    "description": "Full bearish body, no wicks -- strong selling momentum",
                })

        return patterns

    # ------------------------------------------------------------------
    # Two-bar patterns
    # ------------------------------------------------------------------
    def _detect_two_bar(self, bars: list, ts: float) -> list[dict]:
        patterns: list[dict] = []
        prev, cur = bars[-2], bars[-1]
        prev_body = _body(prev)
        cur_body = _body(cur)
        prev_top = max(prev.open, prev.close)
        prev_bot = min(prev.open, prev.close)
        cur_top = max(cur.open, cur.close)
        cur_bot = min(cur.open, cur.close)

        # --- Bullish Engulfing ---
        if (_is_bearish(prev) and _is_bullish(cur)
                and cur_bot <= prev_bot and cur_top >= prev_top
                and cur_body > prev_body):
            patterns.append({
                "pattern": "Bullish Engulfing",
                "direction": "LONG",
                "strength": 75,
                "bar_index": -2,
                "description": "Current bullish bar engulfs prior bearish bar",
            })

        # --- Bearish Engulfing ---
        if (_is_bullish(prev) and _is_bearish(cur)
                and cur_top >= prev_top and cur_bot <= prev_bot
                and cur_body > prev_body):
            patterns.append({
                "pattern": "Bearish Engulfing",
                "direction": "SHORT",
                "strength": 75,
                "bar_index": -2,
                "description": "Current bearish bar engulfs prior bullish bar",
            })

        # --- Piercing Line ---
        if (_is_bearish(prev) and _is_bullish(cur)
                and cur.open < prev.low
                and cur.close > _midpoint(prev)
                and cur.close < prev.open):
            patterns.append({
                "pattern": "Piercing Line",
                "direction": "LONG",
                "strength": 65,
                "bar_index": -2,
                "description": "Opens below prior low, closes above prior midpoint -- bullish reversal",
            })

        # --- Dark Cloud Cover ---
        if (_is_bullish(prev) and _is_bearish(cur)
                and cur.open > prev.high
                and cur.close < _midpoint(prev)
                and cur.close > prev.open):
            patterns.append({
                "pattern": "Dark Cloud Cover",
                "direction": "SHORT",
                "strength": 65,
                "bar_index": -2,
                "description": "Opens above prior high, closes below prior midpoint -- bearish reversal",
            })

        # --- Tweezer Top ---
        if _within_ticks(prev.high, cur.high, ts, 1.0):
            if _is_bullish(prev) and _is_bearish(cur):
                patterns.append({
                    "pattern": "Tweezer Top",
                    "direction": "SHORT",
                    "strength": 60,
                    "bar_index": -2,
                    "description": "Matching highs with bearish reversal -- resistance rejection",
                })

        # --- Tweezer Bottom ---
        if _within_ticks(prev.low, cur.low, ts, 1.0):
            if _is_bearish(prev) and _is_bullish(cur):
                patterns.append({
                    "pattern": "Tweezer Bottom",
                    "direction": "LONG",
                    "strength": 60,
                    "bar_index": -2,
                    "description": "Matching lows with bullish reversal -- support holding",
                })

        # --- Bullish Harami ---
        if (_is_bearish(prev) and _is_bullish(cur)
                and cur_top <= prev_top and cur_bot >= prev_bot
                and _small_body(cur, ts)):
            patterns.append({
                "pattern": "Bullish Harami",
                "direction": "LONG",
                "strength": 55,
                "bar_index": -2,
                "description": "Small bullish bar inside prior bearish bar -- potential reversal",
            })

        # --- Bearish Harami ---
        if (_is_bullish(prev) and _is_bearish(cur)
                and cur_top <= prev_top and cur_bot >= prev_bot
                and _small_body(cur, ts)):
            patterns.append({
                "pattern": "Bearish Harami",
                "direction": "SHORT",
                "strength": 55,
                "bar_index": -2,
                "description": "Small bearish bar inside prior bullish bar -- potential reversal",
            })

        return patterns

    # ------------------------------------------------------------------
    # Three-bar patterns
    # ------------------------------------------------------------------
    def _detect_three_bar(self, bars: list, ts: float) -> list[dict]:
        patterns: list[dict] = []
        b1, b2, b3 = bars[-3], bars[-2], bars[-1]
        b1_mid = _midpoint(b1)

        # --- Morning Star ---
        if (_is_bearish(b1)
                and _small_body(b2, ts)
                and _is_bullish(b3)
                and b3.close > b1_mid):
            patterns.append({
                "pattern": "Morning Star",
                "direction": "LONG",
                "strength": 80,
                "bar_index": -3,
                "description": "Bearish bar, small star, bullish bar closing above bar-1 midpoint",
            })

        # --- Evening Star ---
        if (_is_bullish(b1)
                and _small_body(b2, ts)
                and _is_bearish(b3)
                and b3.close < b1_mid):
            patterns.append({
                "pattern": "Evening Star",
                "direction": "SHORT",
                "strength": 80,
                "bar_index": -3,
                "description": "Bullish bar, small star, bearish bar closing below bar-1 midpoint",
            })

        # --- Three White Soldiers ---
        if (all(_is_bullish(b) for b in (b1, b2, b3))
                and b2.close > b1.close and b3.close > b2.close
                and min(b2.open, b2.close) >= min(b1.open, b1.close)
                and min(b3.open, b3.close) >= min(b2.open, b2.close)):
            # Each opens within prior body
            if (b2.open >= min(b1.open, b1.close) and b2.open <= max(b1.open, b1.close)
                    and b3.open >= min(b2.open, b2.close) and b3.open <= max(b2.open, b2.close)):
                patterns.append({
                    "pattern": "Three White Soldiers",
                    "direction": "LONG",
                    "strength": 85,
                    "bar_index": -3,
                    "description": "Three consecutive bullish bars closing higher -- strong bullish momentum",
                })

        # --- Three Black Crows ---
        if (all(_is_bearish(b) for b in (b1, b2, b3))
                and b2.close < b1.close and b3.close < b2.close):
            if (b2.open <= max(b1.open, b1.close) and b2.open >= min(b1.open, b1.close)
                    and b3.open <= max(b2.open, b2.close) and b3.open >= min(b2.open, b2.close)):
                patterns.append({
                    "pattern": "Three Black Crows",
                    "direction": "SHORT",
                    "strength": 85,
                    "bar_index": -3,
                    "description": "Three consecutive bearish bars closing lower -- strong bearish momentum",
                })

        return patterns

    # ------------------------------------------------------------------
    # Chart patterns (multi-bar, need 10+ bars)
    # ------------------------------------------------------------------
    def _detect_chart_patterns(self, bars: list, ts: float) -> list[dict]:
        patterns: list[dict] = []
        patterns.extend(self._detect_flag(bars, ts))
        patterns.extend(self._detect_double_top_bottom(bars, ts))
        return patterns

    def _detect_flag(self, bars: list, ts: float) -> list[dict]:
        """Detect bull/bear flag: strong move (pole) then tight consolidation."""
        patterns: list[dict] = []
        if len(bars) < 10:
            return patterns

        # Look for pole in bars[-10:-4] and flag in bars[-4:]
        pole_bars = bars[-10:-4]
        flag_bars = bars[-4:]

        # Pole: measure net move and range
        pole_move = pole_bars[-1].close - pole_bars[0].open
        pole_range = max(b.high for b in pole_bars) - min(b.low for b in pole_bars)

        # Flag: tight range consolidation
        flag_high = max(b.high for b in flag_bars)
        flag_low = min(b.low for b in flag_bars)
        flag_range = flag_high - flag_low

        if pole_range <= 0 or flag_range <= 0:
            return patterns

        # Flag should be less than 50% of pole range (tight consolidation)
        if flag_range > pole_range * 0.5:
            return patterns

        # Pole must be significant (at least 8 ticks)
        if abs(pole_move) < 8 * ts:
            return patterns

        cur = bars[-1]

        # --- Bull Flag ---
        if pole_move > 0 and cur.close > flag_high:
            patterns.append({
                "pattern": "Bull Flag",
                "direction": "LONG",
                "strength": 75,
                "bar_index": -10,
                "description": "Strong up-move then tight consolidation, breakout above flag high",
            })

        # --- Bear Flag ---
        if pole_move < 0 and cur.close < flag_low:
            patterns.append({
                "pattern": "Bear Flag",
                "direction": "SHORT",
                "strength": 75,
                "bar_index": -10,
                "description": "Strong down-move then tight consolidation, breakout below flag low",
            })

        return patterns

    def _detect_double_top_bottom(self, bars: list, ts: float) -> list[dict]:
        """Detect double top / double bottom over recent bars."""
        patterns: list[dict] = []
        if len(bars) < 10:
            return patterns

        window = bars[-15:] if len(bars) >= 15 else bars[-10:]
        highs = [(i, b.high) for i, b in enumerate(window)]
        lows = [(i, b.low) for i, b in enumerate(window)]

        # --- Double Top ---
        # Find two peaks that are within 2 ticks, separated by at least 3 bars
        for i in range(len(highs)):
            for j in range(i + 3, len(highs)):
                h1, h2 = highs[i][1], highs[j][1]
                if _within_ticks(h1, h2, ts, 2.0):
                    # Ensure there is a dip between (valley lower than both peaks by 3+ ticks)
                    mid_lows = [window[k].low for k in range(highs[i][0] + 1, highs[j][0])]
                    if mid_lows:
                        valley = min(mid_lows)
                        peak = max(h1, h2)
                        if (peak - valley) >= 3 * ts:
                            # Current bar should be breaking below the valley
                            cur = bars[-1]
                            if cur.close < valley:
                                patterns.append({
                                    "pattern": "Double Top",
                                    "direction": "SHORT",
                                    "strength": 70,
                                    "bar_index": -len(window),
                                    "description": f"Two matching highs near {peak:.2f} with break below neckline",
                                })
                                return patterns  # One match is enough

        # --- Double Bottom ---
        for i in range(len(lows)):
            for j in range(i + 3, len(lows)):
                l1, l2 = lows[i][1], lows[j][1]
                if _within_ticks(l1, l2, ts, 2.0):
                    mid_highs = [window[k].high for k in range(lows[i][0] + 1, lows[j][0])]
                    if mid_highs:
                        peak = max(mid_highs)
                        trough = min(l1, l2)
                        if (peak - trough) >= 3 * ts:
                            cur = bars[-1]
                            if cur.close > peak:
                                patterns.append({
                                    "pattern": "Double Bottom",
                                    "direction": "LONG",
                                    "strength": 70,
                                    "bar_index": -len(window),
                                    "description": f"Two matching lows near {trough:.2f} with break above neckline",
                                })
                                return patterns

        return patterns


# ---------------------------------------------------------------------------
# Confluence helper
# ---------------------------------------------------------------------------

def get_pattern_confluence(patterns: list[dict], direction: str) -> dict:
    """
    Given detected patterns and a trade direction, return confluence score.

    Args:
        patterns: Output from CandlestickAnalyzer.analyze()
        direction: "LONG" or "SHORT"

    Returns: {
        "aligned_patterns":  [patterns matching direction],
        "opposing_patterns": [patterns against direction],
        "net_score":         sum of aligned strengths - sum of opposing strengths,
        "strongest_aligned": highest-strength pattern in our direction (or None),
        "strongest_opposed": highest-strength pattern against direction (or None),
        "description":       human-readable summary string,
    }
    """
    opposite = "SHORT" if direction == "LONG" else "LONG"

    aligned = [p for p in patterns if p["direction"] == direction]
    opposing = [p for p in patterns if p["direction"] == opposite]

    aligned_score = sum(p["strength"] for p in aligned)
    opposing_score = sum(p["strength"] for p in opposing)

    strongest_aligned = aligned[0] if aligned else None
    strongest_opposed = opposing[0] if opposing else None

    # Build description
    if aligned and not opposing:
        names = " + ".join(p["pattern"] for p in aligned[:3])
        desc = f"{names} confirm {direction}"
    elif aligned and opposing:
        a_names = " + ".join(p["pattern"] for p in aligned[:2])
        o_names = " + ".join(p["pattern"] for p in opposing[:1])
        desc = f"{a_names} confirm {direction} (vs {o_names})"
    elif opposing:
        o_names = " + ".join(p["pattern"] for p in opposing[:2])
        desc = f"Warning: {o_names} oppose {direction}"
    else:
        desc = "No candlestick patterns detected"

    return {
        "aligned_patterns": aligned,
        "opposing_patterns": opposing,
        "net_score": aligned_score - opposing_score,
        "strongest_aligned": strongest_aligned,
        "strongest_opposed": strongest_opposed,
        "description": desc,
    }
