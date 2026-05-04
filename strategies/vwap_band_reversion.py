"""
Phoenix Bot — VWAP Band Pure-Reversion Strategy
================================================

Pure mean-reversion at the 2.1σ VWAP band. SHORT at upper-band touch
with bearish reversal confirmation; LONG at lower-band touch with
bullish reversal. Distinct from `vwap_band_pullback` (trend-aligned
pullback into 1σ zone) — this strategy fades extremes regardless of
HTF trend, with regime + ADX filters that skip trend days.

DESIGNED 2026-05-03 per operator request. Lab-only until 50+ trades
validate the algorithm.

RESEARCH BASIS
--------------
1. Brian Shannon ("Maximum Trading Gains with Anchored VWAP", 2021):
   2σ band touches in range-bound sessions revert to VWAP with high
   probability; trend days walk one band and the strategy must skip.
2. John Carter ("Mastering the Trade", 2nd ed., 2012): VWAP+stddev
   bands as intraday support/resistance — touches at 2.0-2.5σ are the
   most actionable mean-reversion entries on liquid index futures.
3. Empirical (out/strategy_overhaul_2026-05-03.md §VWAP-band research):
   In 11 days of MNQM6 5m bars, 2.1σ upper-band touches reverted to
   VWAP within 30 min in 36% of cases; lower-band touches reverted
   in 87% of cases. Asymmetry is a known feature of NQ's long-bias
   drift; expect SHORTs to underperform LONGs and rely on the
   regime + day_type filters to avoid catastrophic trend-day shorts.

WHY 2.1σ (not 2.0 or 2.5)
--------------------------
- 2.0σ contains ~95.4% of normal data; 2.1σ contains ~96.4%.
- 2.1σ filters slightly more noise than 2.0 without losing meaningful
  trade frequency (283 upper touches vs 300 at 2.0σ in test window —
  6% drop in count, no quality loss).
- 2.5σ is too rare (214 touches) and price often pierces and goes
  further on event days.
- Operator requested 2.1-2.2; 2.1 chosen as the more aggressive
  endpoint to maximize signal frequency for lab-stage validation.

ENTRY RULES
-----------
LONG (mirror for SHORT):
  1. day_type != "TREND" (regime filter — trend-day reversion fails)
  2. Time NOT in 08:30-09:30 CT (open volatility filter)
  3. Reference 5m bar low touched lower 2.1σ band
  4. Bar close > bar midpoint (bullish reversal candle)
  5. Bar close ABOVE lower 2.1σ band (bounce completion, not pierce)
  6. Volume >= 0.7 × 20-bar average

STOP
----
Outside lower 2.5σ band + 0.5 × ATR(14). Clamped to [30, 100] ticks.
If natural stop > 100t, signal SKIPPED (vol regime mismatch).

TARGET
------
VWAP itself. Default ~50% of stop distance — managed exit can extend.
Configurable via `target_at_vwap=True` (default) — when False, falls
back to opposite-band target (more greedy, rarely hits).

EXPECTED PERFORMANCE
--------------------
Per-strategy expectations at 2.1σ on MNQM6 in 5-10 trading days:
  - 1-3 trades/day after filters
  - LONG WR 60-70% (high revert rate at lower band)
  - SHORT WR 35-45% (NQ long-bias drift)
  - Combined WR 50-55%, PF 1.3-1.6 if RR holds at 1.5-2.0
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")


@dataclass
class _ReversionSignal:
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    stop_ticks: int
    target_rr: float
    confidence: float
    confluences: list
    reason: str


class VwapBandReversion(BaseStrategy):
    """Pure mean-reversion at 2.1σ VWAP band."""

    name = "vwap_band_reversion"

    def __init__(self, config: dict):
        super().__init__(config or {})

    def evaluate(
        self,
        market: dict,
        bars_5m: List,
        bars_1m: list,
        session_info: dict,
    ) -> Optional[Signal]:
        # Config knobs
        sigma             = float(self.config.get("sigma", 2.1))
        outer_sigma       = float(self.config.get("outer_sigma", 2.5))
        atr_stop_buffer   = float(self.config.get("atr_stop_buffer", 0.5))
        min_bars          = int(self.config.get("min_bars", 30))
        atr_period        = int(self.config.get("atr_period", 14))
        min_volume_ratio  = float(self.config.get("min_volume_ratio", 0.7))
        min_stop_ticks    = int(self.config.get("min_stop_ticks", 30))
        max_stop_ticks    = int(self.config.get("max_stop_ticks", 100))
        target_rr_fallback = float(self.config.get("target_rr", 1.5))
        target_at_vwap    = bool(self.config.get("target_at_vwap", True))
        block_windows     = self.config.get("block_windows", [("08:30", "09:30")])

        # Filter 1: TREND day — skip
        day_type = market.get("day_type", "UNKNOWN")
        if day_type == "TREND":
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:trend_day_skip")
            return None

        # Filter 2: time-of-day block (08:30-09:30 CT default — open volatility)
        if block_windows:
            now_ct = datetime.now(_CT)
            hhmm = now_ct.strftime("%H:%M")
            for start, end in block_windows:
                if start <= hhmm <= end:
                    logger.debug(
                        f"[EVAL] {self.name}: BLOCKED gate:time_window {start}-{end} CT"
                    )
                    return None

        # Warmup
        if not bars_5m or len(bars_5m) < min_bars:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        ref = bars_5m[-1]

        # Compute VWAP + bands at entry sigma + outer sigma
        bands = self._calc_vwap_bands(bars_5m, sigma, outer_sigma)
        if bands is None:
            logger.debug(f"[EVAL] {self.name}: SKIP vwap_unavailable")
            return None
        vwap, upper_entry, lower_entry, upper_outer, lower_outer = bands

        atr = self._calc_atr(bars_5m, atr_period)
        if atr is None or atr <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP atr_unavailable")
            return None

        # Volume floor
        if len(bars_5m) >= 20:
            avg_vol_20 = sum(b.volume for b in bars_5m[-20:]) / 20
            vol_ratio = ref.volume / avg_vol_20 if avg_vol_20 > 0 else 0.0
        else:
            vol_ratio = 1.0  # warmup — be permissive

        if vol_ratio < min_volume_ratio:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED gate:volume_low "
                f"({vol_ratio:.2f} < {min_volume_ratio})"
            )
            return None

        # Detect upper-band touch with bearish reversal
        candidate: Optional[_ReversionSignal] = self._evaluate_short(
            ref, vwap, upper_entry, upper_outer, atr,
            vol_ratio, target_rr_fallback, target_at_vwap, sigma,
        )
        if candidate is None:
            candidate = self._evaluate_long(
                ref, vwap, lower_entry, lower_outer, atr,
                vol_ratio, target_rr_fallback, target_at_vwap, sigma,
            )

        if candidate is None:
            return None

        # Apply stop ceiling — skip rather than clamp
        if candidate.stop_ticks > max_stop_ticks:
            logger.info(
                f"[EVAL] {self.name}: SKIP stop_too_wide "
                f"({candidate.stop_ticks}t > {max_stop_ticks}t) — "
                f"vol regime mismatch, skip rather than clamp"
            )
            return None

        # Apply stop floor — clamp to noise floor
        if candidate.stop_ticks < min_stop_ticks:
            tick_size = 0.25
            if candidate.direction == "LONG":
                candidate.stop_price = candidate.entry_price - (min_stop_ticks * tick_size)
            else:
                candidate.stop_price = candidate.entry_price + (min_stop_ticks * tick_size)
            candidate.stop_ticks = min_stop_ticks
            candidate.confluences.append(
                f"Stop clamped to noise-floor {min_stop_ticks}t"
            )

        logger.info(
            f"[EVAL] {self.name}: SIGNAL {candidate.direction} "
            f"entry={candidate.entry_price:.2f} "
            f"stop={candidate.stop_price:.2f} target={candidate.target_price:.2f}"
        )
        return self._to_canonical(candidate)

    # ─── helpers ───────────────────────────────────────────────────────

    def _evaluate_short(
        self, ref, vwap, upper_entry, upper_outer, atr,
        vol_ratio, target_rr, target_at_vwap, sigma,
    ) -> Optional[_ReversionSignal]:
        """SHORT at upper band touch + bearish reversal."""
        bar_high  = ref.high
        bar_low   = ref.low
        bar_close = ref.close

        # Touch the upper-entry band (high >= upper_entry)
        if bar_high < upper_entry:
            return None

        # Bearish reversal: close below midpoint AND close below upper_entry
        bar_mid = (bar_high + bar_low) / 2
        if bar_close >= bar_mid:
            return None
        if bar_close >= upper_entry:
            # Pierced and held above — trend-extension, not reversion
            return None

        entry = bar_close
        stop = upper_outer + atr * 0.5
        if stop <= entry:
            return None
        stop_distance = stop - entry
        tick_size = 0.25
        stop_ticks = int(stop_distance / tick_size)

        if target_at_vwap:
            target = vwap
            actual_rr = (entry - target) / stop_distance if stop_distance > 0 else 0
        else:
            target = entry - stop_distance * target_rr
            actual_rr = target_rr

        return _ReversionSignal(
            direction="SHORT",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=actual_rr,
            confidence=60.0,
            confluences=[
                f"VWAP={vwap:.2f}, upper_{sigma}σ={upper_entry:.2f}",
                f"Bar high {bar_high:.2f} touched upper band",
                f"Bearish reversal: close {bar_close:.2f} < mid {bar_mid:.2f}",
                f"Volume {vol_ratio:.2f}x avg",
                f"Stop @ outer band {upper_outer:.2f} + 0.5*ATR({atr:.2f})",
                f"Target @ VWAP {vwap:.2f} (RR={actual_rr:.2f})" if target_at_vwap
                else f"Target {target:.2f} (RR={actual_rr:.2f})",
            ],
            reason=f"VWAP {sigma}σ upper-band SHORT reversion at {entry:.2f}",
        )

    def _evaluate_long(
        self, ref, vwap, lower_entry, lower_outer, atr,
        vol_ratio, target_rr, target_at_vwap, sigma,
    ) -> Optional[_ReversionSignal]:
        """LONG at lower band touch + bullish reversal."""
        bar_high  = ref.high
        bar_low   = ref.low
        bar_close = ref.close

        if bar_low > lower_entry:
            return None

        bar_mid = (bar_high + bar_low) / 2
        if bar_close <= bar_mid:
            return None
        if bar_close <= lower_entry:
            return None

        entry = bar_close
        stop = lower_outer - atr * 0.5
        if stop >= entry:
            return None
        stop_distance = entry - stop
        tick_size = 0.25
        stop_ticks = int(stop_distance / tick_size)

        if target_at_vwap:
            target = vwap
            actual_rr = (target - entry) / stop_distance if stop_distance > 0 else 0
        else:
            target = entry + stop_distance * target_rr
            actual_rr = target_rr

        return _ReversionSignal(
            direction="LONG",
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            stop_ticks=stop_ticks,
            target_rr=actual_rr,
            confidence=70.0,  # higher confidence on LONGs (87% historical revert)
            confluences=[
                f"VWAP={vwap:.2f}, lower_{sigma}σ={lower_entry:.2f}",
                f"Bar low {bar_low:.2f} touched lower band",
                f"Bullish reversal: close {bar_close:.2f} > mid {bar_mid:.2f}",
                f"Volume {vol_ratio:.2f}x avg",
                f"Stop @ outer band {lower_outer:.2f} - 0.5*ATR({atr:.2f})",
                f"Target @ VWAP {vwap:.2f} (RR={actual_rr:.2f})" if target_at_vwap
                else f"Target {target:.2f} (RR={actual_rr:.2f})",
            ],
            reason=f"VWAP {sigma}σ lower-band LONG reversion at {entry:.2f}",
        )

    def _to_canonical(self, v: _ReversionSignal) -> Signal:
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

    # ─── indicator math (matches vwap_band_pullback style) ───────────

    def _calc_vwap_bands(self, bars: List, entry_sigma: float, outer_sigma: float):
        if not bars:
            return None
        cum_pv = 0.0
        cum_v = 0.0
        cum_pv_sq = 0.0
        for b in bars:
            typical = (b.high + b.low + b.close) / 3
            pv = typical * b.volume
            cum_pv += pv
            cum_v += b.volume
            cum_pv_sq += (typical ** 2) * b.volume
        if cum_v <= 0:
            return None
        vwap = cum_pv / cum_v
        variance = (cum_pv_sq / cum_v) - (vwap ** 2)
        std = math.sqrt(max(variance, 0))
        return (
            vwap,
            vwap + entry_sigma * std,
            vwap - entry_sigma * std,
            vwap + outer_sigma * std,
            vwap - outer_sigma * std,
        )

    def _calc_atr(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        trs = []
        for i in range(1, len(bars)):
            curr, prev = bars[i], bars[i - 1]
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close),
            )
            trs.append(tr)
        if not trs:
            return None
        return sum(trs[-period:]) / min(len(trs), period)
