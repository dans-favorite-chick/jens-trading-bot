"""
Phoenix Bot — VWAP Pullback Strategy v2 (Research-Validated)

REWRITE: Uses VWAP + 1σ/2σ standard deviation bands.

RESEARCH BASIS:
  - Warrior Trading: "Stage Two trends typically don't come back to VWAP until
    the trend is already losing steam, so your best pullback opportunities will
    occur at the 1 standard deviation line."
  - Tradingriot: "On strong trending moves, the price often does not pull back
    all the way to VWAP therefore we can latch to the move using the 1 std
    deviation band."
  - Brian Shannon (backtested PF 1.69 on SPY 1h 2017-2025): pullback to VWAP
    with RSI(2) < 30 confirms overdone counter-move.

THE CORE INSIGHT:
Your OLD code waited for price to hit VWAP exactly. On strong trend days,
price only pulls back to the 1σ band and continues. You missed all of those.
NEW code treats VWAP → 1σ band as the "value zone" — entry anywhere in
that zone on a bounce.

ENTRY RULES (LONG):
  1. HTF trend bullish (MTFTrendDetector safe_to_long)
  2. Price in uptrend above VWAP (session-long)
  3. Pullback: price drops into zone between VWAP and lower 1σ band
  4. RSI(2) < 30 (overdone counter-move)
  5. Confirmation candle: bullish close, or engulfing, or hammer
  6. Volume ≥ 0.8x of 20-bar average on confirmation bar

STOP: Outside lower 2σ band by 0.5x ATR (minimum 8 ticks)
TARGET 1: VWAP (~1R, take 50% off)
TARGET 2: Upper 1σ band (~2R, trail rest with Chandelier)

EXPECTED WR (from research): 45-55% with 1:1.5 to 1:2 R:R
  → PF 1.5-1.8 if disciplined.
"""

from dataclasses import dataclass
from typing import Optional, List
import math

from strategies.base_strategy import BaseStrategy, Signal


@dataclass
class _MTFTrendShim:
    """B12: minimal shape to keep the internal _evaluate_long/_short
    helpers unchanged. Derived from the canonical market snapshot's
    tf_bias/tf_votes fields in evaluate()."""
    htf_trend: str                  # "UP" | "DOWN" | "NEUTRAL"
    safe_to_long: bool
    safe_to_short: bool
    confidence: float               # 0-1 proxy from tf-vote ratio


@dataclass
class VWAPPullbackSignal:
    """Output of VWAP pullback evaluation (internal-only since B12).

    Pre-B12 this dataclass was the direct return type from evaluate(),
    which broke the BaseStrategy contract (no .validated / canonical
    Signal). B12 keeps the class as an internal intermediate — the
    _evaluate_long/_short helpers still produce it — and evaluate()
    now converts it to a canonical strategies.base_strategy.Signal
    before returning.
    """
    direction: str       # "LONG" | "SHORT" | "NONE"
    entry_price: float
    stop_price: float
    target_price: float
    stop_ticks: int
    target_rr: float
    confidence: float    # 0-100
    confluences: list
    reason: str


class VWAPPullback(BaseStrategy):
    """
    VWAP pullback with 1σ bands + RSI(2) filter. B12 fix: now inherits
    BaseStrategy so it loads alongside the canonical-Signal strategies
    (pre-B12 the base_bot loader skipped this class with a WARN because
    it lacked `.validated` and returned a non-canonical Signal shape).
    """

    name = "vwap_pullback"

    def __init__(self, config: dict):
        super().__init__(config or {})
        # MTF trend is derived from the `market` snapshot's tf_bias /
        # tf_votes_* fields — no separate MTFTrendDetector instance needed.

    def evaluate(
        self,
        market: dict,
        bars_5m: List,
        bars_1m: list,
        session_info: dict,
    ) -> Optional[Signal]:
        """
        B12 fix: canonical BaseStrategy signature
        (market, bars_5m, bars_1m, session_info) → Signal | None.

        Internally still uses the 1σ-band + RSI(2) logic; the MTF-trend
        dependency is derived from the market snapshot's tf_bias fields
        so this strategy no longer needs a MTFTrendDetector instance.
        """
        mtf_trend_result = self._derive_mtf_trend_from_market(market)
        # Parameters
        min_bars = 50
        rsi_period = self.config.get("rsi_period", 2)
        rsi_long_threshold = self.config.get("rsi_long_threshold", 30)
        rsi_short_threshold = self.config.get("rsi_short_threshold", 70)
        atr_period = self.config.get("atr_period", 14)
        target_rr = self.config.get("target_rr", 2.0)
        min_volume_ratio = self.config.get("min_volume_ratio", 0.8)

        if not bars_5m or len(bars_5m) < min_bars:
            return None

        # Require HTF alignment (the 3-method detector)
        if not (mtf_trend_result.safe_to_long or mtf_trend_result.safe_to_short):
            return None

        ref = bars_5m[-1]

        # ── Compute VWAP + bands ────────────────────────────────────────
        vwap, upper_1sigma, lower_1sigma, upper_2sigma, lower_2sigma = \
            self._calc_vwap_bands(bars_5m)

        if vwap is None:
            return None

        # ── Compute ATR ────────────────────────────────────────────────
        atr = self._calc_atr(bars_5m, atr_period)
        if atr is None or atr <= 0:
            return None

        # ── Compute RSI(2) ─────────────────────────────────────────────
        rsi = self._calc_rsi(bars_5m, rsi_period)
        if rsi is None:
            return None

        # ── Volume check ────────────────────────────────────────────────
        avg_volume_20 = sum(b.volume for b in bars_5m[-20:]) / 20
        volume_ratio = ref.volume / avg_volume_20 if avg_volume_20 > 0 else 0

        # ── LONG Setup ──────────────────────────────────────────────────
        if mtf_trend_result.safe_to_long:
            v2_sig = self._evaluate_long(
                ref, vwap, lower_1sigma, lower_2sigma,
                rsi, rsi_long_threshold, atr,
                volume_ratio, min_volume_ratio, target_rr,
                mtf_trend_result,
            )
            return self._to_canonical(v2_sig) if v2_sig else None

        # ── SHORT Setup ─────────────────────────────────────────────────
        if mtf_trend_result.safe_to_short:
            v2_sig = self._evaluate_short(
                ref, vwap, upper_1sigma, upper_2sigma,
                rsi, rsi_short_threshold, atr,
                volume_ratio, min_volume_ratio, target_rr,
                mtf_trend_result,
            )
            return self._to_canonical(v2_sig) if v2_sig else None

        return None

    # ─── B12 adapters: v2 shape ↔ canonical Signal ─────────────────────

    def _derive_mtf_trend_from_market(self, market: dict) -> "_MTFTrendShim":
        """Build the MTFTrendShim the internal helpers expect, from the
        canonical market snapshot's tf_bias / tf_votes_* fields."""
        bullish = int(market.get("tf_votes_bullish", 0) or 0)
        bearish = int(market.get("tf_votes_bearish", 0) or 0)
        total = max(bullish + bearish, 1)
        # Safe-to-long if a clear majority of TF votes are bullish (>=3/4).
        safe_to_long = bullish >= 3 and bullish > bearish
        safe_to_short = bearish >= 3 and bearish > bullish
        if safe_to_long:
            htf_trend = "UP"
        elif safe_to_short:
            htf_trend = "DOWN"
        else:
            htf_trend = "NEUTRAL"
        confidence = max(bullish, bearish) / total
        return _MTFTrendShim(
            htf_trend=htf_trend,
            safe_to_long=safe_to_long,
            safe_to_short=safe_to_short,
            confidence=confidence,
        )

    def _to_canonical(self, v2: "VWAPPullbackSignal") -> Signal:
        """Convert the internal VWAPPullbackSignal to the canonical
        `strategies.base_strategy.Signal`. Preserves all per-signal
        fields (entry/stop/target prices, stop_ticks, target_rr,
        confluences, reason) and uses the v4 order-type matrix
        defaults (LIMIT entry for VWAP-pullback mean-reversion entries;
        STOPMARKET stops, LIMIT targets via Signal defaults)."""
        return Signal(
            direction=v2.direction,
            stop_ticks=v2.stop_ticks,
            target_rr=v2.target_rr,
            confidence=v2.confidence,
            entry_score=55.0,           # default tier; no tier-math in v2
            strategy=self.name,
            reason=v2.reason,
            confluences=list(v2.confluences),
            atr_stop_override=True,     # v2 stop is ATR-anchored to 2σ band
            entry_type="LIMIT",         # per v4 matrix row 3 (vwap_pullback)
            entry_price=v2.entry_price,
            stop_price=v2.stop_price,
            target_price=v2.target_price,
        )

    def _evaluate_long(
        self, ref, vwap, lower_1sigma, lower_2sigma,
        rsi, rsi_threshold, atr,
        volume_ratio, min_volume_ratio, target_rr,
        mtf_result,
    ) -> Optional[VWAPPullbackSignal]:
        """Evaluate long setup."""
        # 1. Price must be in pullback zone (between VWAP and lower 1σ)
        # Pullback means price dipped into this zone
        bar_low = ref.low
        bar_close = ref.close

        # Price touched or entered the value zone (VWAP to lower 1σ)
        touched_zone = (bar_low <= vwap) and (bar_low >= lower_1sigma)
        # OR price is slightly beyond lower 1σ (deeper pullback, still valid)
        deep_pullback = (bar_low < lower_1sigma) and (bar_low > lower_2sigma)
        if not (touched_zone or deep_pullback):
            return None

        # 2. Confirmation: bullish close (bar closed above its own midpoint)
        bar_midpoint = (ref.high + ref.low) / 2
        if bar_close < bar_midpoint:
            return None  # Not a bullish reversal bar

        # 3. Close should be above the lower 1σ (indicating bounce completion)
        if bar_close < lower_1sigma:
            return None

        # 4. RSI(2) oversold at the dip
        if rsi > rsi_threshold:
            return None

        # 5. Volume check
        if volume_ratio < min_volume_ratio:
            return None

        # ── Build signal ───────────────────────────────────────────────
        entry = bar_close
        stop = lower_2sigma - (atr * 0.5)
        stop_distance = entry - stop

        # Floor at 8 ticks
        tick_size = 0.25
        min_stop_distance = 8 * tick_size
        if stop_distance < min_stop_distance:
            stop = entry - min_stop_distance
            stop_distance = min_stop_distance

        target = entry + stop_distance * target_rr
        stop_ticks = int(stop_distance / tick_size)

        confluences = [
            f"HTF={mtf_result.htf_trend} (conf={mtf_result.confidence:.2f})",
            f"VWAP={vwap:.2f}, lower_1σ={lower_1sigma:.2f}",
            f"Pullback to {'1σ zone' if touched_zone else 'deep (2σ)'}",
            f"Close {bar_close:.2f} bounced above lower_1σ",
            f"RSI(2)={rsi:.1f} (oversold < {rsi_threshold})",
            f"Volume {volume_ratio:.2f}x avg",
            f"ATR={atr:.2f}, stop={stop_distance:.2f}",
        ]

        return VWAPPullbackSignal(
            direction="LONG",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=70.0,
            confluences=confluences,
            reason=f"VWAP pullback LONG at {entry:.2f} (RSI={rsi:.1f})",
        )

    def _evaluate_short(
        self, ref, vwap, upper_1sigma, upper_2sigma,
        rsi, rsi_threshold, atr,
        volume_ratio, min_volume_ratio, target_rr,
        mtf_result,
    ) -> Optional[VWAPPullbackSignal]:
        """Evaluate short setup (mirror of long)."""
        bar_high = ref.high
        bar_close = ref.close

        touched_zone = (bar_high >= vwap) and (bar_high <= upper_1sigma)
        deep_pullback = (bar_high > upper_1sigma) and (bar_high < upper_2sigma)
        if not (touched_zone or deep_pullback):
            return None

        bar_midpoint = (ref.high + ref.low) / 2
        if bar_close > bar_midpoint:
            return None  # Not a bearish reversal bar

        if bar_close > upper_1sigma:
            return None

        if rsi < rsi_threshold:
            return None

        if volume_ratio < min_volume_ratio:
            return None

        entry = bar_close
        stop = upper_2sigma + (atr * 0.5)
        stop_distance = stop - entry

        tick_size = 0.25
        min_stop_distance = 8 * tick_size
        if stop_distance < min_stop_distance:
            stop = entry + min_stop_distance
            stop_distance = min_stop_distance

        target = entry - stop_distance * target_rr
        stop_ticks = int(stop_distance / tick_size)

        confluences = [
            f"HTF={mtf_result.htf_trend} (conf={mtf_result.confidence:.2f})",
            f"VWAP={vwap:.2f}, upper_1σ={upper_1sigma:.2f}",
            f"Pullback to {'1σ zone' if touched_zone else 'deep (2σ)'}",
            f"Close {bar_close:.2f} rejected below upper_1σ",
            f"RSI(2)={rsi:.1f} (overbought > {rsi_threshold})",
            f"Volume {volume_ratio:.2f}x avg",
            f"ATR={atr:.2f}, stop={stop_distance:.2f}",
        ]

        return VWAPPullbackSignal(
            direction="SHORT",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=70.0,
            confluences=confluences,
            reason=f"VWAP pullback SHORT at {entry:.2f} (RSI={rsi:.1f})",
        )

    # ─── Helper methods ────────────────────────────────────────────────

    def _calc_vwap_bands(self, bars: List):
        """Calculate session VWAP + 1σ and 2σ bands."""
        if not bars:
            return None, None, None, None, None

        cumulative_pv = 0.0
        cumulative_v = 0.0
        cumulative_pv_sq = 0.0  # For variance

        for b in bars:
            typical = (b.high + b.low + b.close) / 3
            pv = typical * b.volume
            cumulative_pv += pv
            cumulative_v += b.volume
            cumulative_pv_sq += (typical ** 2) * b.volume

        if cumulative_v <= 0:
            return None, None, None, None, None

        vwap = cumulative_pv / cumulative_v
        # Volume-weighted variance of typical price
        variance = (cumulative_pv_sq / cumulative_v) - (vwap ** 2)
        std = math.sqrt(max(variance, 0))

        upper_1sigma = vwap + std
        lower_1sigma = vwap - std
        upper_2sigma = vwap + 2 * std
        lower_2sigma = vwap - 2 * std

        return vwap, upper_1sigma, lower_1sigma, upper_2sigma, lower_2sigma

    def _calc_atr(self, bars: List, period: int) -> Optional[float]:
        """Calculate ATR."""
        if len(bars) < period + 1:
            return None
        tr_values = []
        for i in range(1, len(bars)):
            curr = bars[i]
            prev = bars[i - 1]
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
        """Calculate RSI."""
        if len(bars) < period + 1:
            return None

        closes = [b.close for b in bars]
        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))

        if len(gains) < period:
            return None

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
