"""
Phoenix Bot — Smart Money Concepts (SMC) Pattern Detector

Detects institutional trading patterns in real-time:
  - Market Structure Breaks (MSB/BOS) — higher high/lower low shifts
  - Liquidity Sweeps — stop hunts above/below key levels
  - Fair Value Gaps (FVG) — imbalance zones price tends to fill
  - Order Blocks — last opposing candle before a strong move

These patterns are what Lux Algo charges $200/mo to visualize.
We compute them from raw bars and feed as confluence to strategies.
"""

import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("SMC")


@dataclass
class SwingPoint:
    """A confirmed swing high or low."""
    type: str        # "high" or "low"
    price: float
    bar_index: int
    swept: bool = False  # True if liquidity was taken


@dataclass
class SMCSignal:
    """A detected SMC pattern."""
    pattern: str        # "MSB_BULLISH", "MSB_BEARISH", "SWEEP_LOW", "SWEEP_HIGH", "FVG_BULL", "FVG_BEAR", "OB_BULL", "OB_BEAR"
    direction: str      # "LONG" or "SHORT"
    price: float        # Price at detection
    strength: float     # 0-100
    description: str
    bar_index: int


class SMCDetector:
    """
    Real-time Smart Money Concepts pattern detector.
    Fed completed bars (1m or 5m), maintains swing structure,
    and emits signals when institutional patterns are detected.
    """

    def __init__(self, swing_lookback: int = 5, fvg_min_gap_ticks: float = 2.0,
                 tick_size: float = 0.25):
        self.swing_lookback = swing_lookback  # Bars to confirm a swing point
        self.tick_size = tick_size
        self.fvg_min_gap = fvg_min_gap_ticks * tick_size

        # State
        self._bars: deque = deque(maxlen=200)
        self._bar_count = 0
        self._swing_highs: deque[SwingPoint] = deque(maxlen=50)
        self._swing_lows: deque[SwingPoint] = deque(maxlen=50)
        self._last_structure = "NEUTRAL"  # "BULLISH", "BEARISH", "NEUTRAL"
        self._active_fvgs: list[dict] = []  # Unfilled FVGs
        self._active_obs: list[dict] = []   # Active order blocks
        self._signals: deque[SMCSignal] = deque(maxlen=20)  # Recent signals

    # ─── Core: Process Bar ─────────────────────────────────────────
    def update(self, bar) -> list[SMCSignal]:
        """Process a completed bar. Returns list of new SMC signals."""
        self._bars.append(bar)
        self._bar_count += 1
        signals = []

        if len(self._bars) < self.swing_lookback * 2 + 1:
            return signals

        # 1. Detect swing points
        self._detect_swings()

        # 2. Check for market structure breaks
        msb = self._check_structure_break(bar)
        if msb:
            signals.append(msb)

        # 3. Check for liquidity sweeps
        sweep = self._check_liquidity_sweep(bar)
        if sweep:
            signals.append(sweep)

        # 4. Check for fair value gaps
        fvg = self._check_fvg()
        if fvg:
            signals.append(fvg)

        # 5. Update FVG fills
        self._update_fvg_fills(bar)

        # 6. Detect order blocks
        ob = self._check_order_block()
        if ob:
            signals.append(ob)

        for s in signals:
            self._signals.append(s)
            logger.info(f"[SMC] {s.pattern} @ {s.price:.2f} — {s.description}")

        return signals

    # ─── Swing Detection ───────────────────────────────────────────
    def _detect_swings(self):
        """Detect confirmed swing highs and lows using lookback window."""
        bars = list(self._bars)
        lb = self.swing_lookback
        if len(bars) < 2 * lb + 1:
            return

        # Check the bar at position -lb-1 (center of window)
        center = len(bars) - lb - 1
        center_bar = bars[center]

        # Swing high: center bar's high is highest in window
        window_highs = [b.high for b in bars[center - lb:center + lb + 1]]
        if center_bar.high == max(window_highs):
            # Confirm it's not a duplicate
            if (not self._swing_highs or
                    abs(self._swing_highs[-1].price - center_bar.high) > self.tick_size * 2):
                self._swing_highs.append(SwingPoint(
                    type="high", price=center_bar.high, bar_index=self._bar_count - lb - 1
                ))

        # Swing low: center bar's low is lowest in window
        window_lows = [b.low for b in bars[center - lb:center + lb + 1]]
        if center_bar.low == min(window_lows):
            if (not self._swing_lows or
                    abs(self._swing_lows[-1].price - center_bar.low) > self.tick_size * 2):
                self._swing_lows.append(SwingPoint(
                    type="low", price=center_bar.low, bar_index=self._bar_count - lb - 1
                ))

    # ─── Market Structure Break (MSB/BOS) ──────────────────────────
    def _check_structure_break(self, bar) -> SMCSignal | None:
        """Detect break of market structure — higher high or lower low shift."""
        if len(self._swing_highs) < 2 or len(self._swing_lows) < 2:
            return None

        prev_high = self._swing_highs[-2].price
        last_high = self._swing_highs[-1].price
        prev_low = self._swing_lows[-2].price
        last_low = self._swing_lows[-1].price

        # Bullish MSB: price breaks above the most recent swing high
        if bar.close > last_high and self._last_structure != "BULLISH":
            self._last_structure = "BULLISH"
            # Strength based on how decisively it broke
            break_distance = (bar.close - last_high) / self.tick_size
            strength = min(80, 40 + break_distance * 5)
            return SMCSignal(
                pattern="MSB_BULLISH",
                direction="LONG",
                price=bar.close,
                strength=strength,
                description=f"Bullish structure break above {last_high:.2f} (+{break_distance:.0f}t)",
                bar_index=self._bar_count,
            )

        # Bearish MSB: price breaks below the most recent swing low
        if bar.close < last_low and self._last_structure != "BEARISH":
            self._last_structure = "BEARISH"
            break_distance = (last_low - bar.close) / self.tick_size
            strength = min(80, 40 + break_distance * 5)
            return SMCSignal(
                pattern="MSB_BEARISH",
                direction="SHORT",
                price=bar.close,
                strength=strength,
                description=f"Bearish structure break below {last_low:.2f} (-{break_distance:.0f}t)",
                bar_index=self._bar_count,
            )

        return None

    # ─── Liquidity Sweep ───────────────────────────────────────────
    def _check_liquidity_sweep(self, bar) -> SMCSignal | None:
        """Detect stop hunt — price sweeps a swing level then reverses."""
        # Sweep of swing low (bullish): bar wicks below swing low but closes above
        for sp in reversed(list(self._swing_lows)[-5:]):
            if sp.swept:
                continue
            if bar.low < sp.price and bar.close > sp.price:
                # Swept below and reclaimed — classic stop hunt
                sp.swept = True
                sweep_depth = (sp.price - bar.low) / self.tick_size
                if sweep_depth >= 1:  # At least 1 tick below
                    strength = min(90, 50 + sweep_depth * 8)
                    return SMCSignal(
                        pattern="SWEEP_LOW",
                        direction="LONG",
                        price=bar.close,
                        strength=strength,
                        description=f"Liquidity sweep below {sp.price:.2f} ({sweep_depth:.0f}t below, reclaimed)",
                        bar_index=self._bar_count,
                    )

        # Sweep of swing high (bearish): bar wicks above swing high but closes below
        for sp in reversed(list(self._swing_highs)[-5:]):
            if sp.swept:
                continue
            if bar.high > sp.price and bar.close < sp.price:
                sp.swept = True
                sweep_depth = (bar.high - sp.price) / self.tick_size
                if sweep_depth >= 1:
                    strength = min(90, 50 + sweep_depth * 8)
                    return SMCSignal(
                        pattern="SWEEP_HIGH",
                        direction="SHORT",
                        price=bar.close,
                        strength=strength,
                        description=f"Liquidity sweep above {sp.price:.2f} ({sweep_depth:.0f}t above, rejected)",
                        bar_index=self._bar_count,
                    )

        return None

    # ─── Fair Value Gap (FVG) ──────────────────────────────────────
    def _check_fvg(self) -> SMCSignal | None:
        """Detect 3-bar imbalance gaps (FVG). Price tends to fill these."""
        bars = list(self._bars)
        if len(bars) < 3:
            return None

        b1, b2, b3 = bars[-3], bars[-2], bars[-1]

        # Bullish FVG: gap between bar1's high and bar3's low
        gap_bull = b3.low - b1.high
        if gap_bull > self.fvg_min_gap:
            fvg = {"type": "FVG_BULL", "top": b3.low, "bottom": b1.high,
                   "bar_index": self._bar_count, "filled": False}
            self._active_fvgs.append(fvg)
            return SMCSignal(
                pattern="FVG_BULL", direction="LONG", price=b3.close,
                strength=min(70, 30 + (gap_bull / self.tick_size) * 5),
                description=f"Bullish FVG: {b1.high:.2f}-{b3.low:.2f} ({gap_bull/self.tick_size:.0f}t gap)",
                bar_index=self._bar_count,
            )

        # Bearish FVG: gap between bar3's high and bar1's low
        gap_bear = b1.low - b3.high
        if gap_bear > self.fvg_min_gap:
            fvg = {"type": "FVG_BEAR", "top": b1.low, "bottom": b3.high,
                   "bar_index": self._bar_count, "filled": False}
            self._active_fvgs.append(fvg)
            return SMCSignal(
                pattern="FVG_BEAR", direction="SHORT", price=b3.close,
                strength=min(70, 30 + (gap_bear / self.tick_size) * 5),
                description=f"Bearish FVG: {b3.high:.2f}-{b1.low:.2f} ({gap_bear/self.tick_size:.0f}t gap)",
                bar_index=self._bar_count,
            )

        return None

    def _update_fvg_fills(self, bar):
        """Mark FVGs as filled when price trades through them."""
        for fvg in self._active_fvgs:
            if fvg["filled"]:
                continue
            if fvg["type"] == "FVG_BULL" and bar.low <= fvg["bottom"]:
                fvg["filled"] = True
            elif fvg["type"] == "FVG_BEAR" and bar.high >= fvg["top"]:
                fvg["filled"] = True
        # Purge old filled FVGs
        self._active_fvgs = [f for f in self._active_fvgs
                             if not f["filled"] and self._bar_count - f["bar_index"] < 100]

    # ─── Order Block Detection ─────────────────────────────────────
    def _check_order_block(self) -> SMCSignal | None:
        """Detect order blocks — last opposing candle before a strong move."""
        bars = list(self._bars)
        if len(bars) < 5:
            return None

        # Look at the last 5 bars for a strong move preceded by opposing candle
        recent = bars[-5:]

        # Bullish OB: bearish candle followed by 2+ strong bullish candles
        for i in range(len(recent) - 3):
            b0 = recent[i]      # Potential OB candle (bearish)
            b1 = recent[i + 1]  # First move candle
            b2 = recent[i + 2]  # Second move candle

            # b0 is bearish, b1 and b2 are bullish
            if (b0.close < b0.open and b1.close > b1.open and b2.close > b2.open):
                move = b2.close - b0.low
                body0 = abs(b0.close - b0.open)
                if move > body0 * 2:  # Strong move (2x the OB candle body)
                    # Don't duplicate recent OBs
                    if any(ob["type"] == "OB_BULL" and abs(ob["price"] - b0.low) < self.tick_size * 3
                           for ob in self._active_obs[-5:]):
                        continue
                    ob = {"type": "OB_BULL", "price": b0.low, "top": b0.open,
                          "bar_index": self._bar_count - (len(recent) - i)}
                    self._active_obs.append(ob)
                    return SMCSignal(
                        pattern="OB_BULL", direction="LONG", price=b0.low,
                        strength=min(75, 40 + (move / self.tick_size) * 3),
                        description=f"Bullish OB at {b0.low:.2f}-{b0.open:.2f} (move={move/self.tick_size:.0f}t)",
                        bar_index=self._bar_count,
                    )

        # Bearish OB: bullish candle followed by 2+ strong bearish candles
        for i in range(len(recent) - 3):
            b0 = recent[i]
            b1 = recent[i + 1]
            b2 = recent[i + 2]

            if (b0.close > b0.open and b1.close < b1.open and b2.close < b2.open):
                move = b0.high - b2.close
                body0 = abs(b0.close - b0.open)
                if move > body0 * 2:
                    if any(ob["type"] == "OB_BEAR" and abs(ob["price"] - b0.high) < self.tick_size * 3
                           for ob in self._active_obs[-5:]):
                        continue
                    ob = {"type": "OB_BEAR", "price": b0.high, "bottom": b0.open,
                          "bar_index": self._bar_count - (len(recent) - i)}
                    self._active_obs.append(ob)
                    return SMCSignal(
                        pattern="OB_BEAR", direction="SHORT", price=b0.high,
                        strength=min(75, 40 + (move / self.tick_size) * 3),
                        description=f"Bearish OB at {b0.open:.2f}-{b0.high:.2f} (move={move/self.tick_size:.0f}t)",
                        bar_index=self._bar_count,
                    )

        return None

    # ─── Query API ─────────────────────────────────────────────────
    def get_confluence_score(self, direction: str) -> dict:
        """Get SMC confluence for a trade direction. Used by strategies."""
        recent = [s for s in self._signals if self._bar_count - s.bar_index <= 10]
        aligned = [s for s in recent if s.direction == direction]
        opposing = [s for s in recent if s.direction != direction]

        score = sum(s.strength for s in aligned) / max(1, len(aligned)) if aligned else 0
        strongest = max(aligned, key=lambda s: s.strength) if aligned else None

        return {
            "score": round(score, 1),
            "aligned_count": len(aligned),
            "opposing_count": len(opposing),
            "strongest_pattern": strongest.pattern if strongest else None,
            "strongest_description": strongest.description if strongest else None,
            "patterns": [{"pattern": s.pattern, "strength": s.strength,
                         "description": s.description} for s in aligned],
        }

    def get_state(self) -> dict:
        """Full state for dashboard."""
        return {
            "structure": self._last_structure,
            "swing_highs": [{"price": sp.price, "swept": sp.swept} for sp in list(self._swing_highs)[-5:]],
            "swing_lows": [{"price": sp.price, "swept": sp.swept} for sp in list(self._swing_lows)[-5:]],
            "active_fvgs": [f for f in self._active_fvgs if not f["filled"]][-5:],
            "active_obs": self._active_obs[-5:],
            "recent_signals": [{"pattern": s.pattern, "direction": s.direction,
                                "price": s.price, "strength": s.strength,
                                "description": s.description}
                               for s in list(self._signals)[-10:]],
            "bar_count": self._bar_count,
        }

    def to_dict(self) -> dict:
        """Compact state for bot state push."""
        return self.get_state()
