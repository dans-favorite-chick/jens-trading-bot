"""
Phoenix Bot — Trend-Following Pullback Strategy v2 (CORRECTED)

CORRECTIONS FROM v1:
  1. POSITIVE GAMMA LOGIC FIXED:
     OLD (WRONG): "Only trade in direction AWAY from HVL"
     NEW (CORRECT): "Trade WITHIN the gamma band toward mean (HVL)"
       - LONG ok if price near put_wall, target = HVL or call_wall
       - SHORT ok if price near call_wall, target = HVL or put_wall
       - SKIP if price in middle of band (no edge)
       - SKIP if breakout above call_wall (chasing fails in pos gamma)

  2. HOLD TIME EXTENDED:
     OLD: 20-min time stop (scalping logic)
     NEW: No time stop for trend following — Chandelier trail handles exits
       - Day-trading hold times of 1-4 hours expected
       - Static target at 3R provides upside cap
       - Session close enforced as final cutoff

THE RECIPE:
  LAYER 1: HTF bias via MTFTrendDetector (3-method, no single-bar gating)
  LAYER 2: 15m pullback detection (price near 21-EMA + RSI in pullback zone)
  LAYER 3: 5m entry trigger (bar close past prior bar's extreme)
  LAYER 4: Regime-aware logic (correct positive/negative gamma rules)
  EXIT:    ChandelierExitManager (BE@1R, trail@1.5R, tighten@2R)

RESEARCH BASIS:
  - FlashAlpha GEX guide: "In positive gamma, the Put Wall and Call Wall
    define the gamma band where price tends to pin"
  - Modigin GEX: "Put walls offer similar logic for longs [in positive gamma]"
  - GEX Metrix: positive gamma = trade within the band
  - Power Trading Group AutoPilot V3 (1,045 NQ trades): hold for hours,
    not minutes, with multi-target scaling
  - QuantifiedStrategies: "day trading focuses on hourly to 15-minute
    timeframe" with multi-hour hold times
"""

from dataclasses import dataclass
from typing import Optional, List


@dataclass
class TrendFollowingSignal:
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    stop_ticks: int
    target_rr: float
    confidence: float
    confluences: list
    reason: str
    regime: str  # "NEG_GAMMA" | "POS_GAMMA_LOW_BAND" | "POS_GAMMA_HIGH_BAND"


class TrendFollowingPullback:
    """Trend-following with pullback entry. Regime-aware gamma logic CORRECTED."""

    name = "trend_following_pullback"

    def __init__(self, config: dict):
        self.config = config or {}

    def evaluate(
        self,
        market: dict,
        bars_5m: List,
        bars_15m: List,
        mtf_trend_result,        # TrendResult from MTFTrendDetector
        menthorq_levels: dict,   # {"hvl": ..., "call_wall": ..., "put_wall": ...}
        session_info: dict,
    ) -> Optional[TrendFollowingSignal]:
        """Evaluate trend-following pullback setup with corrected gamma logic."""

        # ── LAYER 1: HTF bias check ─────────────────────────────────────
        if not (mtf_trend_result.safe_to_long or mtf_trend_result.safe_to_short):
            return None
        if mtf_trend_result.confidence < 0.66:
            return None

        # ── Get current price ───────────────────────────────────────────
        current_price = market.get("price") or (
            bars_5m[-1].close if bars_5m else None
        )
        if current_price is None:
            return None

        # ── REGIME CLASSIFICATION (CORRECTED LOGIC) ─────────────────────
        regime, regime_params = self._classify_regime_corrected(
            current_price,
            menthorq_levels,
            mtf_trend_result,
        )
        if regime_params is None:
            return None  # SKIP: no edge in this gamma zone

        # ── LAYER 2: 15m pullback ───────────────────────────────────────
        if len(bars_15m) < 30:
            return None
        if not self._detect_pullback_15m(bars_15m, mtf_trend_result):
            return None

        # ── LAYER 3: 5m entry trigger ───────────────────────────────────
        if len(bars_5m) < 50:
            return None

        ref = bars_5m[-1]
        prior = bars_5m[-2]

        avg_volume_20 = sum(b.volume for b in bars_5m[-20:]) / 20
        volume_ratio = ref.volume / avg_volume_20 if avg_volume_20 > 0 else 0
        if volume_ratio < regime_params["min_volume_ratio"]:
            return None

        atr_5m = self._calc_atr(bars_5m, 14)
        if atr_5m is None or atr_5m <= 0:
            return None

        # Direction-specific evaluation
        target_rr = regime_params["target_rr"]

        if mtf_trend_result.safe_to_long and regime_params["allow_long"]:
            return self._evaluate_long(
                ref, prior, atr_5m, target_rr,
                menthorq_levels, regime_params,
                mtf_trend_result, regime, volume_ratio,
            )

        if mtf_trend_result.safe_to_short and regime_params["allow_short"]:
            return self._evaluate_short(
                ref, prior, atr_5m, target_rr,
                menthorq_levels, regime_params,
                mtf_trend_result, regime, volume_ratio,
            )

        return None

    # ─── REGIME LOGIC (CORRECTED) ──────────────────────────────────────

    def _classify_regime_corrected(
        self,
        price: float,
        menthorq_levels: dict,
        mtf_trend_result,
    ) -> tuple[str, Optional[dict]]:
        """
        CORRECTED gamma regime classifier.

        Returns (regime_name, params) where params is None to skip.
        """
        hvl = menthorq_levels.get("hvl")
        call_wall = menthorq_levels.get("call_wall") or menthorq_levels.get("call_resistance")
        put_wall = menthorq_levels.get("put_wall") or menthorq_levels.get("put_support")

        # If no MenthorQ data available, default to negative-gamma rules
        if hvl is None:
            return "UNKNOWN_NEG", {
                "target_rr": self.config.get("target_rr_neg_gamma", 2.5),
                "min_volume_ratio": 1.2,
                "allow_long": True,
                "allow_short": True,
                "max_distance_to_resistance_atr": None,
            }

        # ── NEGATIVE GAMMA REGIME (price below HVL) ────────────────────
        # Trends amplified — trade momentum, both directions ok
        if price < hvl:
            return "NEG_GAMMA", {
                "target_rr": self.config.get("target_rr_neg_gamma", 2.5),
                "min_volume_ratio": 1.2,
                "allow_long": True,
                "allow_short": True,
                "max_distance_to_resistance_atr": None,
            }

        # ── POSITIVE GAMMA REGIME (price above HVL) ────────────────────
        # Within gamma band — mean-reversion only, NOT chasing
        if call_wall is None or put_wall is None:
            # No walls defined — be conservative
            return "POS_GAMMA_NO_WALLS", None

        # Calculate position within the gamma band
        # band_position: 0.0 = at put_wall, 1.0 = at call_wall, 0.5 = at HVL
        band_height = call_wall - put_wall
        if band_height <= 0:
            return "POS_GAMMA_INVALID", None

        band_position = (price - put_wall) / band_height

        # ── Above call wall: NO TRADES (chasing breakout in pos gamma) ──
        if price > call_wall:
            return "POS_GAMMA_ABOVE_WALL", None

        # ── Below put wall: regime probably flipping, skip ──────────────
        if price < put_wall:
            return "POS_GAMMA_BELOW_WALL", None

        # ── Within gamma band: rules depend on position ─────────────────
        # Lower 30% of band: longs ok (near put wall support)
        if band_position < 0.30:
            return "POS_GAMMA_LOW_BAND", {
                "target_rr": self.config.get("target_rr_pos_gamma", 1.5),
                "min_volume_ratio": 1.4,
                "allow_long": True,
                "allow_short": False,  # Don't short into put wall support
                "max_distance_to_resistance_atr": None,
                "target_override": hvl,  # Take profit at HVL
            }

        # Upper 30% of band: shorts ok (near call wall resistance)
        if band_position > 0.70:
            return "POS_GAMMA_HIGH_BAND", {
                "target_rr": self.config.get("target_rr_pos_gamma", 1.5),
                "min_volume_ratio": 1.4,
                "allow_long": False,  # Don't long into call wall resistance
                "allow_short": True,
                "max_distance_to_resistance_atr": None,
                "target_override": hvl,  # Take profit at HVL
            }

        # Middle of band: chop zone, no edge
        return "POS_GAMMA_MID_BAND", None

    # ─── PULLBACK DETECTION (15m) ───────────────────────────────────────

    def _detect_pullback_15m(self, bars_15m: List, mtf_result) -> bool:
        """Detect valid pullback on 15m chart."""
        if len(bars_15m) < 25:
            return False

        ema_21 = self._calc_ema(bars_15m, 21)
        if ema_21 is None:
            return False

        last = bars_15m[-1]
        rsi_14 = self._calc_rsi(bars_15m, 14)
        if rsi_14 is None:
            return False

        if mtf_result.safe_to_long:
            pct_below_ema = (ema_21 - last.close) / ema_21 * 100
            near_ema = -1.0 <= pct_below_ema <= 0.5
            rsi_pullback = 35 <= rsi_14 <= 55

            if last.high != last.low:
                close_pos = (last.close - last.low) / (last.high - last.low)
                bounce_forming = close_pos >= 0.5
            else:
                bounce_forming = False

            return near_ema and rsi_pullback and bounce_forming

        if mtf_result.safe_to_short:
            pct_above_ema = (last.close - ema_21) / ema_21 * 100
            near_ema = -0.5 <= pct_above_ema <= 1.0
            rsi_pullback = 45 <= rsi_14 <= 65

            if last.high != last.low:
                close_pos = (last.close - last.low) / (last.high - last.low)
                bounce_forming = close_pos <= 0.5
            else:
                bounce_forming = False

            return near_ema and rsi_pullback and bounce_forming

        return False

    # ─── ENTRY EVALUATION ──────────────────────────────────────────────

    def _evaluate_long(
        self, ref, prior, atr, target_rr, menthorq_levels, regime_params,
        mtf_result, regime, volume_ratio,
    ) -> Optional[TrendFollowingSignal]:
        """Evaluate long entry."""
        # Confirmation: bullish bar past prior high
        if ref.close <= prior.high:
            return None
        if ref.high != ref.low:
            close_pos = (ref.close - ref.low) / (ref.high - ref.low)
            if close_pos < 0.6:
                return None

        # Build signal
        entry = ref.close
        stop_distance = atr * 1.5
        min_stop = 8 * 0.25
        if stop_distance < min_stop:
            stop_distance = min_stop

        stop = entry - stop_distance

        # Use override target (HVL) for positive gamma, else standard RR target
        target_override = regime_params.get("target_override")
        if target_override is not None and target_override > entry:
            target = min(target_override, entry + stop_distance * target_rr)
        else:
            target = entry + stop_distance * target_rr

        stop_ticks = int(stop_distance / 0.25)

        confluences = [
            f"HTF={mtf_result.htf_trend} (conf={mtf_result.confidence:.2f})",
            f"Regime: {regime}",
            f"15m pullback to 21-EMA confirmed",
            f"5m bullish close past prior high ({ref.close:.2f} > {prior.high:.2f})",
            f"Volume {volume_ratio:.2f}x avg",
            f"ATR={atr:.2f}, stop={stop_distance:.2f}",
            f"Target={target:.2f}, RR≈{(target - entry) / stop_distance:.2f}",
        ]

        return TrendFollowingSignal(
            direction="LONG",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=75.0,
            confluences=confluences,
            reason=f"Trend-following LONG at {entry:.2f} ({regime})",
            regime=regime,
        )

    def _evaluate_short(
        self, ref, prior, atr, target_rr, menthorq_levels, regime_params,
        mtf_result, regime, volume_ratio,
    ) -> Optional[TrendFollowingSignal]:
        """Evaluate short entry (mirror of long)."""
        if ref.close >= prior.low:
            return None
        if ref.high != ref.low:
            close_pos = (ref.close - ref.low) / (ref.high - ref.low)
            if close_pos > 0.4:
                return None

        entry = ref.close
        stop_distance = atr * 1.5
        min_stop = 8 * 0.25
        if stop_distance < min_stop:
            stop_distance = min_stop

        stop = entry + stop_distance

        target_override = regime_params.get("target_override")
        if target_override is not None and target_override < entry:
            target = max(target_override, entry - stop_distance * target_rr)
        else:
            target = entry - stop_distance * target_rr

        stop_ticks = int(stop_distance / 0.25)

        confluences = [
            f"HTF={mtf_result.htf_trend} (conf={mtf_result.confidence:.2f})",
            f"Regime: {regime}",
            f"15m pullback to 21-EMA confirmed",
            f"5m bearish close past prior low ({ref.close:.2f} < {prior.low:.2f})",
            f"Volume {volume_ratio:.2f}x avg",
            f"ATR={atr:.2f}, stop={stop_distance:.2f}",
            f"Target={target:.2f}, RR≈{(entry - target) / stop_distance:.2f}",
        ]

        return TrendFollowingSignal(
            direction="SHORT",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=75.0,
            confluences=confluences,
            reason=f"Trend-following SHORT at {entry:.2f} ({regime})",
            regime=regime,
        )

    # ─── HELPERS ────────────────────────────────────────────────────────

    def _calc_atr(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        tr = []
        for i in range(1, len(bars)):
            c, p = bars[i], bars[i - 1]
            tr.append(max(
                c.high - c.low,
                abs(c.high - p.close),
                abs(c.low - p.close),
            ))
        return sum(tr[-period:]) / min(len(tr), period) if tr else None

    def _calc_ema(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period:
            return None
        closes = [b.close for b in bars]
        ema = sum(closes[:period]) / period
        k = 2.0 / (period + 1)
        for c in closes[period:]:
            ema = c * k + ema * (1 - k)
        return ema

    def _calc_rsi(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        closes = [b.close for b in bars]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        if len(gains) < period:
            return None
        avg_g = sum(gains[-period:]) / period
        avg_l = sum(losses[-period:]) / period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100 - (100 / (1 + rs))
