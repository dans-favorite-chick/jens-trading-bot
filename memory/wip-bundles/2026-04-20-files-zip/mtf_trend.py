"""
Phoenix Bot — Multi-Timeframe Trend Detection (Research-Validated)

SOLVES: The "single red candle false signal" problem.

THE PROBLEM:
Naive MTF systems gate on "is this bar bullish?" which produces false signals
when a single counter-trend candle appears within an established trend. The bot
then refuses to trade for an entire HTF period.

THE SOLUTION (3-method combined detection):
Use THREE independent measurements of trend, require 2 of 3 to agree.

  Method 1: EMA Slope (over N bars on HTF) — single bar barely moves slope
  Method 2: HH/HL Swing Structure — single bar doesn't break structure
  Method 3: Price vs EMA Stack — single bar doesn't break EMA ordering

This approach is backed by:
  - EMA Slope Pro (TradingView, 2026): "Slope Lookback Bars: Default 10. 
    Increase to smooth volatility spikes"
  - Trade with the Pros MTF research: "58% WR vs 39% for non-aligned trades"
  - Multi-indicator fusion research (FMZQuant, Medium)

USAGE:
    from core.mtf_trend import MTFTrendDetector

    detector = MTFTrendDetector(bars_60m, bars_15m, bars_5m)
    result = detector.detect()

    if result.safe_to_long and result.confidence >= 0.66:
        # Safe to take long signals
        ...
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict


@dataclass
class TrendResult:
    """Output of a multi-timeframe trend detection."""
    htf_trend: str              # BULLISH | BEARISH | NEUTRAL
    mtf_trend: str
    ltf_trend: str
    confidence: float           # 0.0-1.0, fraction of HTF methods agreeing
    methods_agreement: Dict = field(default_factory=dict)
    safe_to_long: bool = False  # True iff HTF + MTF agree bullish (or MTF neutral)
    safe_to_short: bool = False
    reason: str = ""


class MTFTrendDetector:
    """
    Three-method trend detection across multiple timeframes.

    Each timeframe gets analyzed with all 3 methods independently.
    The bot can then check if HTF/MTF/LTF all agree before trading.
    """

    def __init__(self, htf_bars: List, mtf_bars: List, ltf_bars: List):
        """
        Args:
            htf_bars: Higher timeframe bars (e.g., 60-min) — bias/trend layer
            mtf_bars: Middle timeframe bars (e.g., 15-min) — setup layer
            ltf_bars: Lower timeframe bars (e.g., 5-min) — trigger layer
        """
        self.htf_bars = htf_bars
        self.mtf_bars = mtf_bars
        self.ltf_bars = ltf_bars

    def detect(self) -> TrendResult:
        """Run all 3 methods on all 3 timeframes, return combined result."""
        htf_trend, htf_methods = self._detect_trend(self.htf_bars)
        mtf_trend, mtf_methods = self._detect_trend(self.mtf_bars)
        ltf_trend, ltf_methods = self._detect_trend(self.ltf_bars)

        # Confidence = fraction of HTF methods agreeing on the majority direction
        if htf_trend == "BULLISH":
            confidence = sum(1 for v in htf_methods.values() if v == "BULLISH") / 3.0
        elif htf_trend == "BEARISH":
            confidence = sum(1 for v in htf_methods.values() if v == "BEARISH") / 3.0
        else:
            confidence = 0.0

        # Gate: HTF must agree with direction, MTF must not oppose
        # (MTF can be NEUTRAL — it doesn't have to actively confirm)
        safe_to_long = (
            htf_trend == "BULLISH"
            and mtf_trend != "BEARISH"
            and confidence >= 0.66  # At least 2 of 3 HTF methods agree
        )
        safe_to_short = (
            htf_trend == "BEARISH"
            and mtf_trend != "BULLISH"
            and confidence >= 0.66
        )

        reason = (
            f"HTF={htf_trend} ({int(confidence * 3)}/3 methods), "
            f"MTF={mtf_trend}, LTF={ltf_trend}"
        )

        return TrendResult(
            htf_trend=htf_trend,
            mtf_trend=mtf_trend,
            ltf_trend=ltf_trend,
            confidence=confidence,
            methods_agreement={
                "HTF": htf_methods,
                "MTF": mtf_methods,
                "LTF": ltf_methods,
            },
            safe_to_long=safe_to_long,
            safe_to_short=safe_to_short,
            reason=reason,
        )

    def _detect_trend(self, bars: List) -> tuple[str, dict]:
        """Run 3 methods on a single timeframe, return majority vote."""
        if not bars or len(bars) < 50:
            return "NEUTRAL", {
                "slope": "NEUTRAL",
                "structure": "NEUTRAL",
                "stack": "NEUTRAL",
            }

        method_results = {
            "slope": self._method_ema_slope(bars),
            "structure": self._method_hh_hl_structure(bars),
            "stack": self._method_ema_stack(bars),
        }

        bullish_votes = sum(1 for v in method_results.values() if v == "BULLISH")
        bearish_votes = sum(1 for v in method_results.values() if v == "BEARISH")

        # 2 of 3 agreement = confirmed trend
        if bullish_votes >= 2:
            return "BULLISH", method_results
        elif bearish_votes >= 2:
            return "BEARISH", method_results
        else:
            return "NEUTRAL", method_results

    # ─── METHOD 1: EMA SLOPE ─────────────────────────────────────────────

    def _method_ema_slope(
        self,
        bars: List,
        ema_period: int = 50,
        slope_lookback: int = 10,
    ) -> str:
        """
        EMA slope over N bars. Single bar barely affects slope.
        Threshold: 0.05% per bar = sustained trend.
        """
        if len(bars) < ema_period + slope_lookback:
            return "NEUTRAL"

        emas = self._calc_ema_series(bars, ema_period)
        if len(emas) < slope_lookback:
            return "NEUTRAL"

        current_ema = emas[-1]
        past_ema = emas[-slope_lookback]
        if past_ema == 0:
            return "NEUTRAL"

        # Slope normalized as % per bar
        slope = (current_ema - past_ema) / past_ema / slope_lookback

        if slope > 0.0005:      # +0.05% per bar sustained rise
            return "BULLISH"
        elif slope < -0.0005:   # -0.05% per bar sustained fall
            return "BEARISH"
        else:
            return "NEUTRAL"

    # ─── METHOD 2: HH/HL STRUCTURE ───────────────────────────────────────

    def _method_hh_hl_structure(
        self,
        bars: List,
        lookback: int = 20,
        swing_window: int = 3,
    ) -> str:
        """
        Detect Higher-Highs/Higher-Lows or Lower-Highs/Lower-Lows pattern.

        A swing high = bar with N bars on each side that are all lower.
        A swing low = bar with N bars on each side that are all higher.

        Single counter-trend bar doesn't break swing structure.
        """
        needed = lookback + swing_window * 2
        if len(bars) < needed:
            return "NEUTRAL"

        recent = bars[-needed:]
        swing_highs = []
        swing_lows = []

        for i in range(swing_window, len(recent) - swing_window):
            curr = recent[i]
            left = recent[i - swing_window:i]
            right = recent[i + 1:i + swing_window + 1]

            if all(curr.high >= b.high for b in left) and \
               all(curr.high >= b.high for b in right):
                swing_highs.append(curr.high)

            if all(curr.low <= b.low for b in left) and \
               all(curr.low <= b.low for b in right):
                swing_lows.append(curr.low)

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "NEUTRAL"

        last_two_highs = swing_highs[-2:]
        last_two_lows = swing_lows[-2:]

        # Higher highs AND higher lows = bullish
        if last_two_highs[1] > last_two_highs[0] and \
           last_two_lows[1] > last_two_lows[0]:
            return "BULLISH"
        # Lower highs AND lower lows = bearish
        if last_two_highs[1] < last_two_highs[0] and \
           last_two_lows[1] < last_two_lows[0]:
            return "BEARISH"

        return "NEUTRAL"

    # ─── METHOD 3: EMA STACK ─────────────────────────────────────────────

    def _method_ema_stack(self, bars: List) -> str:
        """
        Check EMA(20), EMA(50), EMA(200) ordering.

        Bullish stack: price > EMA20 > EMA50 > EMA200
        Bearish stack: price < EMA20 < EMA50 < EMA200
        """
        if len(bars) >= 200:
            periods = [20, 50, 200]
        elif len(bars) >= 50:
            periods = [9, 21, 50]  # Fallback for shorter histories
        else:
            return "NEUTRAL"

        if len(bars) < max(periods):
            return "NEUTRAL"

        emas = [self._calc_ema_series(bars, p)[-1] for p in periods]
        price = bars[-1].close

        # Bullish stack: price above EMAs, EMAs in descending order
        if price > emas[0] and all(emas[i] > emas[i + 1] for i in range(len(emas) - 1)):
            return "BULLISH"
        # Bearish stack: price below EMAs, EMAs in ascending order
        if price < emas[0] and all(emas[i] < emas[i + 1] for i in range(len(emas) - 1)):
            return "BEARISH"

        return "NEUTRAL"

    # ─── HELPER: EMA SERIES ──────────────────────────────────────────────

    def _calc_ema_series(self, bars: List, period: int) -> List[float]:
        """Calculate EMA series over all bars."""
        if not bars:
            return []
        closes = [b.close for b in bars]
        if len(closes) < period:
            return closes

        sma = sum(closes[:period]) / period
        ema_values = [sma]
        k = 2.0 / (period + 1)

        for close in closes[period:]:
            ema_values.append(close * k + ema_values[-1] * (1 - k))

        return ema_values

    # ─── CONVENIENCE METHODS ─────────────────────────────────────────────

    def htf_slope_pct_per_bar(self) -> float:
        """Return current HTF slope as % per bar. Useful for logging."""
        if len(self.htf_bars) < 60:
            return 0.0
        emas = self._calc_ema_series(self.htf_bars, 50)
        if len(emas) < 10:
            return 0.0
        return (emas[-1] - emas[-10]) / emas[-10] / 10 * 100
