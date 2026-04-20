"""
Phoenix Bot — VWAP Band Pullback Strategy (research-backed)

Ported from fix/b12-vwap-pullback-base-strategy as a NEW strategy
(not a replacement) so the existing vwap_pullback (VWAP-proximity
version) and this band-based version can run in parallel in lab
for head-to-head data collection.

RESEARCH BASIS:
  - Warrior Trading: "Stage Two trends typically don't come back
    to VWAP until the trend is already losing steam; your best
    pullback opportunities occur at the 1 standard deviation line."
  - Tradingriot: "On strong trending moves, the price often does
    not pull back all the way to VWAP — we can latch to the move
    using the 1 std deviation band."
  - Brian Shannon (backtested PF 1.69 on SPY 1h 2017-2025):
    pullback to VWAP with RSI(2) < 30 confirms overdone counter-move.

CORE INSIGHT:
  vwap_pullback (the original) waits for price to touch VWAP exactly.
  On strong trend days price only pulls back to the 1σ band and
  continues — those setups are missed. This strategy treats the
  VWAP → 1σ band as the "value zone" and enters anywhere in that
  zone on a bounce.

ENTRY RULES (LONG; SHORT is the mirror):
  1. HTF trend bullish (>=3/4 TF votes bullish AND bullish > bearish)
  2. Bar low touched value zone: VWAP → lower_1σ, OR
     deeper pullback: lower_1σ → lower_2σ
  3. Bar close > bar midpoint (bullish reversal bar)
  4. Bar close >= lower_1σ (bounce completion)
  5. RSI(2) < 30 (oversold at dip)
  6. Volume >= 0.8× 20-bar average

STOP:
  Natural stop = outside 2σ band by 0.5× ATR. Clamped to NQ research
  band [40, 120] ticks. If natural stop would exceed 120 ticks,
  signal is SKIPPED (mirrors ib_breakout ceiling guard).

TARGET:
  target_rr × stop_distance (default 2:1). Managed exit handles
  partials + trailing.

Adapted from b12 with Fix 5 EVAL logging + Fix 8-style ceiling guard.
Lab-stage only until 50+ trades validate the algorithm.

EXPECTED (per b12 header): WR 45-55% at 1:1.5–2 RR → PF 1.5-1.8.
"""

from dataclasses import dataclass
from typing import Optional, List
import logging
import math

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)


@dataclass
class _MTFTrendShim:
    """Minimal MTF-trend shape derived from market snapshot tf_votes."""
    htf_trend: str                  # "UP" | "DOWN" | "NEUTRAL"
    safe_to_long: bool
    safe_to_short: bool
    confidence: float               # 0-1 proxy from tf-vote ratio


@dataclass
class _BandSignal:
    """Internal intermediate emitted by _evaluate_long/_short before
    conversion to the canonical BaseStrategy.Signal."""
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    stop_ticks: int
    target_rr: float
    confidence: float
    confluences: list
    reason: str


class VwapBandPullback(BaseStrategy):
    """VWAP + 1σ/2σ band pullback with RSI(2) filter."""

    name = "vwap_band_pullback"

    def __init__(self, config: dict):
        super().__init__(config or {})

    def evaluate(
        self,
        market: dict,
        bars_5m: List,
        bars_1m: list,
        session_info: dict,
    ) -> Optional[Signal]:
        mtf = self._derive_mtf_trend_from_market(market)

        min_bars            = self.config.get("min_bars", 50)
        rsi_period          = self.config.get("rsi_period", 2)
        rsi_long_threshold  = self.config.get("rsi_long_threshold", 30)
        rsi_short_threshold = self.config.get("rsi_short_threshold", 70)
        atr_period          = self.config.get("atr_period", 14)
        target_rr           = self.config.get("target_rr", 2.0)
        min_volume_ratio    = self.config.get("min_volume_ratio", 0.8)
        min_stop_ticks      = self.config.get("min_stop_ticks", 40)
        max_stop_ticks      = self.config.get("max_stop_ticks", 120)

        if not bars_5m or len(bars_5m) < min_bars:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        if not (mtf.safe_to_long or mtf.safe_to_short):
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:htf_not_aligned")
            return None

        ref = bars_5m[-1]

        bands = self._calc_vwap_bands(bars_5m)
        vwap, upper_1sigma, lower_1sigma, upper_2sigma, lower_2sigma = bands
        if vwap is None:
            logger.debug(f"[EVAL] {self.name}: SKIP vwap_bands_unavailable")
            return None

        atr = self._calc_atr(bars_5m, atr_period)
        if atr is None or atr <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP atr_unavailable")
            return None

        rsi = self._calc_rsi(bars_5m, rsi_period)
        if rsi is None:
            logger.debug(f"[EVAL] {self.name}: SKIP rsi_unavailable")
            return None

        avg_volume_20 = sum(b.volume for b in bars_5m[-20:]) / 20
        volume_ratio = ref.volume / avg_volume_20 if avg_volume_20 > 0 else 0

        candidate: Optional[_BandSignal] = None

        if mtf.safe_to_long:
            candidate = self._evaluate_long(
                ref, vwap, lower_1sigma, lower_2sigma,
                rsi, rsi_long_threshold, atr,
                volume_ratio, min_volume_ratio, target_rr, mtf,
            )
        elif mtf.safe_to_short:
            candidate = self._evaluate_short(
                ref, vwap, upper_1sigma, upper_2sigma,
                rsi, rsi_short_threshold, atr,
                volume_ratio, min_volume_ratio, target_rr, mtf,
            )

        if candidate is None:
            # Individual helpers emit specific reject reasons already.
            return None

        # Fix 8-style ceiling guard: if natural 2σ-band stop would exceed
        # the NQ research ceiling, skip the signal rather than trade an
        # over-ceiling stop.
        if candidate.stop_ticks > max_stop_ticks:
            logger.info(
                f"[EVAL] {self.name}: SKIP "
                f"stop_too_wide ({candidate.stop_ticks}t > {max_stop_ticks}t max) "
                f"— 2σ band too wide for current risk tier"
            )
            return None

        # Fix 7-style min clamp: enforce 40t noise floor.
        if candidate.stop_ticks < min_stop_ticks:
            tick_size = 0.25
            if candidate.direction == "LONG":
                candidate.stop_price = candidate.entry_price - (min_stop_ticks * tick_size)
                candidate.target_price = candidate.entry_price + (
                    min_stop_ticks * tick_size * candidate.target_rr
                )
            else:
                candidate.stop_price = candidate.entry_price + (min_stop_ticks * tick_size)
                candidate.target_price = candidate.entry_price - (
                    min_stop_ticks * tick_size * candidate.target_rr
                )
            candidate.stop_ticks = min_stop_ticks
            candidate.confluences.append(
                f"Stop clamped to min {min_stop_ticks}t (NQ noise floor)"
            )

        logger.info(
            f"[EVAL] {self.name}: SIGNAL {candidate.direction} "
            f"entry={candidate.entry_price:.2f} stop={candidate.stop_price:.2f}"
        )
        return self._to_canonical(candidate)

    # ─── Helpers ───────────────────────────────────────────────────────

    def _derive_mtf_trend_from_market(self, market: dict) -> "_MTFTrendShim":
        bullish = int(market.get("tf_votes_bullish", 0) or 0)
        bearish = int(market.get("tf_votes_bearish", 0) or 0)
        total = max(bullish + bearish, 1)
        safe_to_long = bullish >= 3 and bullish > bearish
        safe_to_short = bearish >= 3 and bearish > bullish
        if safe_to_long:
            htf = "UP"
        elif safe_to_short:
            htf = "DOWN"
        else:
            htf = "NEUTRAL"
        return _MTFTrendShim(
            htf_trend=htf,
            safe_to_long=safe_to_long,
            safe_to_short=safe_to_short,
            confidence=max(bullish, bearish) / total,
        )

    def _to_canonical(self, v: _BandSignal) -> Signal:
        return Signal(
            direction=v.direction,
            stop_ticks=v.stop_ticks,
            target_rr=v.target_rr,
            confidence=v.confidence,
            entry_score=55.0,
            strategy=self.name,
            reason=v.reason,
            confluences=list(v.confluences),
            atr_stop_override=True,
            entry_type="LIMIT",
            entry_price=v.entry_price,
            stop_price=v.stop_price,
            target_price=v.target_price,
        )

    def _evaluate_long(
        self, ref, vwap, lower_1sigma, lower_2sigma,
        rsi, rsi_threshold, atr,
        volume_ratio, min_volume_ratio, target_rr, mtf,
    ) -> Optional[_BandSignal]:
        bar_low = ref.low
        bar_close = ref.close

        touched_zone = (bar_low <= vwap) and (bar_low >= lower_1sigma)
        deep_pullback = (bar_low < lower_1sigma) and (bar_low > lower_2sigma)
        if not (touched_zone or deep_pullback):
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_pullback_into_zone_long")
            return None

        bar_midpoint = (ref.high + ref.low) / 2
        if bar_close < bar_midpoint:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_bullish_reversal_bar")
            return None

        if bar_close < lower_1sigma:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL close_below_lower_1sigma")
            return None

        if rsi > rsi_threshold:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:rsi_not_oversold ({rsi:.1f}>{rsi_threshold})")
            return None

        if volume_ratio < min_volume_ratio:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:volume_low ({volume_ratio:.2f}<{min_volume_ratio})")
            return None

        entry = bar_close
        stop = lower_2sigma - (atr * 0.5)
        stop_distance = entry - stop

        tick_size = 0.25
        stop_ticks = int(stop_distance / tick_size)
        target = entry + stop_distance * target_rr

        return _BandSignal(
            direction="LONG",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=70.0,
            confluences=[
                f"HTF={mtf.htf_trend} (conf={mtf.confidence:.2f})",
                f"VWAP={vwap:.2f}, lower_1σ={lower_1sigma:.2f}",
                f"Pullback to {'1σ zone' if touched_zone else 'deep (2σ)'}",
                f"Close {bar_close:.2f} bounced above lower_1σ",
                f"RSI(2)={rsi:.1f} (oversold < {rsi_threshold})",
                f"Volume {volume_ratio:.2f}x avg",
                f"ATR={atr:.2f}, raw_stop={stop_distance:.2f}",
            ],
            reason=f"VWAP band pullback LONG at {entry:.2f} (RSI={rsi:.1f})",
        )

    def _evaluate_short(
        self, ref, vwap, upper_1sigma, upper_2sigma,
        rsi, rsi_threshold, atr,
        volume_ratio, min_volume_ratio, target_rr, mtf,
    ) -> Optional[_BandSignal]:
        bar_high = ref.high
        bar_close = ref.close

        touched_zone = (bar_high >= vwap) and (bar_high <= upper_1sigma)
        deep_pullback = (bar_high > upper_1sigma) and (bar_high < upper_2sigma)
        if not (touched_zone or deep_pullback):
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_pullback_into_zone_short")
            return None

        bar_midpoint = (ref.high + ref.low) / 2
        if bar_close > bar_midpoint:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_bearish_reversal_bar")
            return None

        if bar_close > upper_1sigma:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL close_above_upper_1sigma")
            return None

        if rsi < rsi_threshold:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:rsi_not_overbought ({rsi:.1f}<{rsi_threshold})")
            return None

        if volume_ratio < min_volume_ratio:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:volume_low ({volume_ratio:.2f}<{min_volume_ratio})")
            return None

        entry = bar_close
        stop = upper_2sigma + (atr * 0.5)
        stop_distance = stop - entry

        tick_size = 0.25
        stop_ticks = int(stop_distance / tick_size)
        target = entry - stop_distance * target_rr

        return _BandSignal(
            direction="SHORT",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=70.0,
            confluences=[
                f"HTF={mtf.htf_trend} (conf={mtf.confidence:.2f})",
                f"VWAP={vwap:.2f}, upper_1σ={upper_1sigma:.2f}",
                f"Pullback to {'1σ zone' if touched_zone else 'deep (2σ)'}",
                f"Close {bar_close:.2f} rejected below upper_1σ",
                f"RSI(2)={rsi:.1f} (overbought > {rsi_threshold})",
                f"Volume {volume_ratio:.2f}x avg",
                f"ATR={atr:.2f}, raw_stop={stop_distance:.2f}",
            ],
            reason=f"VWAP band pullback SHORT at {entry:.2f} (RSI={rsi:.1f})",
        )

    # ─── Indicator calculations ────────────────────────────────────────

    def _calc_vwap_bands(self, bars: List):
        if not bars:
            return None, None, None, None, None
        cumulative_pv = 0.0
        cumulative_v = 0.0
        cumulative_pv_sq = 0.0
        for b in bars:
            typical = (b.high + b.low + b.close) / 3
            pv = typical * b.volume
            cumulative_pv += pv
            cumulative_v += b.volume
            cumulative_pv_sq += (typical ** 2) * b.volume
        if cumulative_v <= 0:
            return None, None, None, None, None
        vwap = cumulative_pv / cumulative_v
        variance = (cumulative_pv_sq / cumulative_v) - (vwap ** 2)
        std = math.sqrt(max(variance, 0))
        return (
            vwap,
            vwap + std,
            vwap - std,
            vwap + 2 * std,
            vwap - 2 * std,
        )

    def _calc_atr(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        tr_values = []
        for i in range(1, len(bars)):
            curr, prev = bars[i], bars[i - 1]
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close),
            )
            tr_values.append(tr)
        if not tr_values:
            return None
        return sum(tr_values[-period:]) / min(len(tr_values), period)

    def _calc_rsi(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        closes = [b.close for b in bars]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff >= 0:
                gains.append(diff); losses.append(0)
            else:
                gains.append(0); losses.append(abs(diff))
        if len(gains) < period:
            return None
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
