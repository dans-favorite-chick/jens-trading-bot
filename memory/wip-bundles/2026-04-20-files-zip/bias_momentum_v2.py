"""
Phoenix Bot — Bias Momentum v2 (Upgrade)

UPGRADES OVER V1:
  1. HTF (1-hour) 3-method bias filter (replaces single-bar HTF gating)
  2. Pullback entry logic (waits for pullback to 21-EMA instead of chasing)
  3. Chandelier trailing exit (22-bar, 3x ATR, tightens to 2.5x at +2R)
  4. Break-even at +1R (locks in scratch on every trade that moves)
  5. MenthorQ regime awareness (different params in pos/neg gamma)

BACKWARD COMPATIBILITY:
  This file does NOT replace your existing bias_momentum.py. It's a separate
  file you can drop in as "bias_momentum_v2" and A/B test in lab.

  Once 50+ trades validate it outperforms v1, swap the import in base_bot.

RESEARCH BASIS (from deep research round 4):
  - Trade with the Pros MTF studies: 58% WR aligned vs 39% non-aligned
  - Power Trading Group AutoPilot V3: 69.8% WR over 1,045 NQ trades using
    similar HTF + pullback + trail pattern
  - EMA Slope Pro (TradingView): multi-bar slope detection solves
    single-bar false signal problem
  - Charles Le Beau Chandelier: 22-bar, 3x ATR trailing stop
"""

from dataclasses import dataclass
from typing import Optional, List
import math


@dataclass
class BiasMomentumV2Signal:
    """Output of bias_momentum v2 evaluation."""
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    stop_ticks: int
    target_rr: float
    confidence: float
    entry_score: float
    confluences: list
    reason: str
    regime: str


class BiasMomentumV2:
    """
    Upgraded bias momentum strategy.

    Uses the MTFTrendDetector for HTF alignment, waits for pullback
    before entering, hands off exit management to ChandelierExitManager.
    """

    name = "bias_momentum_v2"

    def __init__(self, config: dict):
        self.config = config or {}

    def evaluate(
        self,
        market: dict,
        bars_5m: List,
        bars_15m: List,
        mtf_trend_result,      # TrendResult from MTFTrendDetector
        menthorq_levels: dict,
        session_info: dict,
    ) -> Optional[BiasMomentumV2Signal]:
        """
        Evaluate bias momentum with v2 upgrades.
        """
        # ── LAYER 1: HTF bias (must agree on 2 of 3 methods) ───────────
        if not (mtf_trend_result.safe_to_long or mtf_trend_result.safe_to_short):
            return None
        if mtf_trend_result.confidence < 0.66:
            return None

        # ── Regime awareness ────────────────────────────────────────────
        current_price = market.get("price") or (bars_5m[-1].close if bars_5m else None)
        if current_price is None:
            return None

        hvl = menthorq_levels.get("hvl")
        regime = self._classify_regime(current_price, hvl)

        # Regime params
        target_rr = (
            self.config.get("target_rr_neg_gamma", 2.0)
            if regime == "NEG_GAMMA"
            else self.config.get("target_rr_pos_gamma", 1.5)
        )
        min_volume_ratio = 1.2 if regime == "NEG_GAMMA" else 1.4

        # ── LAYER 2: Pullback on 15m required ───────────────────────────
        if len(bars_15m) < 25:
            return None

        pullback_ok = self._check_pullback_15m(bars_15m, mtf_trend_result)
        if not pullback_ok:
            return None

        # ── LAYER 3: 5m entry trigger ───────────────────────────────────
        if len(bars_5m) < 50:
            return None

        ref = bars_5m[-1]
        prior = bars_5m[-2]

        # Volume check
        avg_vol = sum(b.volume for b in bars_5m[-20:]) / 20
        volume_ratio = ref.volume / avg_vol if avg_vol > 0 else 0
        if volume_ratio < min_volume_ratio:
            return None

        # ATR for stops
        atr_5m = self._calc_atr(bars_5m, 14)
        if atr_5m is None or atr_5m <= 0:
            return None

        # CVD alignment (if available)
        cvd = market.get("cvd", 0)

        # ── LAYER 4: Direction-specific evaluation ─────────────────────
        if mtf_trend_result.safe_to_long:
            # Bullish bar closing past prior bar's high
            if ref.close <= prior.high:
                return None
            # Bar closed in upper half
            if ref.high != ref.low:
                close_pos = (ref.close - ref.low) / (ref.high - ref.low)
                if close_pos < 0.55:
                    return None
            # CVD should not be deeply negative
            if cvd < -1000:
                return None

            # Call resistance proximity check
            call_res = menthorq_levels.get("call_resistance")
            if call_res is not None:
                distance_atr = (call_res - ref.close) / atr_5m
                if 0 < distance_atr < 0.5:
                    return None  # Too close to resistance

            entry = ref.close
            stop_distance = atr_5m * 1.5
            min_stop = 8 * 0.25
            if stop_distance < min_stop:
                stop_distance = min_stop
            stop = entry - stop_distance
            target = entry + stop_distance * target_rr
            stop_ticks = int(stop_distance / 0.25)

            return BiasMomentumV2Signal(
                direction="LONG",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                stop_ticks=stop_ticks,
                target_rr=target_rr,
                confidence=75.0,
                entry_score=60.0,
                confluences=[
                    f"HTF={mtf_trend_result.htf_trend}, conf={mtf_trend_result.confidence:.2f}",
                    f"Regime: {regime}",
                    f"15m pullback confirmed",
                    f"5m close {ref.close:.2f} past prior high {prior.high:.2f}",
                    f"Volume {volume_ratio:.2f}x avg",
                    f"CVD={cvd}",
                    f"ATR={atr_5m:.2f}, stop={stop_distance:.2f}",
                    f"RR={target_rr:.1f}",
                ],
                reason=f"Bias momentum LONG at {entry:.2f} ({regime})",
                regime=regime,
            )

        if mtf_trend_result.safe_to_short:
            if ref.close >= prior.low:
                return None
            if ref.high != ref.low:
                close_pos = (ref.close - ref.low) / (ref.high - ref.low)
                if close_pos > 0.45:
                    return None
            if cvd > 1000:
                return None

            put_sup = menthorq_levels.get("put_support")
            if put_sup is not None:
                distance_atr = (ref.close - put_sup) / atr_5m
                if 0 < distance_atr < 0.5:
                    return None

            entry = ref.close
            stop_distance = atr_5m * 1.5
            min_stop = 8 * 0.25
            if stop_distance < min_stop:
                stop_distance = min_stop
            stop = entry + stop_distance
            target = entry - stop_distance * target_rr
            stop_ticks = int(stop_distance / 0.25)

            return BiasMomentumV2Signal(
                direction="SHORT",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                stop_ticks=stop_ticks,
                target_rr=target_rr,
                confidence=75.0,
                entry_score=60.0,
                confluences=[
                    f"HTF={mtf_trend_result.htf_trend}, conf={mtf_trend_result.confidence:.2f}",
                    f"Regime: {regime}",
                    f"15m pullback confirmed",
                    f"5m close {ref.close:.2f} past prior low {prior.low:.2f}",
                    f"Volume {volume_ratio:.2f}x avg",
                    f"CVD={cvd}",
                    f"ATR={atr_5m:.2f}, stop={stop_distance:.2f}",
                    f"RR={target_rr:.1f}",
                ],
                reason=f"Bias momentum SHORT at {entry:.2f} ({regime})",
                regime=regime,
            )

        return None

    def _classify_regime(self, price: float, hvl: Optional[float]) -> str:
        if hvl is None:
            return "UNKNOWN"
        return "NEG_GAMMA" if price < hvl else "POS_GAMMA"

    def _check_pullback_15m(self, bars_15m: List, mtf_result) -> bool:
        """Same pullback check as trend_following_pullback."""
        if len(bars_15m) < 22:
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
            return near_ema and rsi_pullback

        if mtf_result.safe_to_short:
            pct_above_ema = (last.close - ema_21) / ema_21 * 100
            near_ema = -0.5 <= pct_above_ema <= 1.0
            rsi_pullback = 45 <= rsi_14 <= 65
            return near_ema and rsi_pullback

        return False

    def _calc_atr(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        tr = []
        for i in range(1, len(bars)):
            c, p = bars[i], bars[i - 1]
            tr.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
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
