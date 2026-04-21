"""
Phoenix Bot — VWAP Pullback Strategy

Enters on first pullback to VWAP in a trending market.
Best during MID_MORNING regime (9:30-11:00 CST).

Logic: TF bias says bullish → price pulled back to/below VWAP → bounce candle confirms.
The entry is ON the pullback touch, not after price already reclaimed.
"""

import logging

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class VWAPPullback(BaseStrategy):
    name = "vwap_pullback"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        target_rr = self.config.get("target_rr", 1.8)
        # B14: NQ-calibrated ATR stop params (replaces fixed stop_ticks).
        stop_atr_mult       = self.config.get("stop_atr_mult", 2.0)
        min_stop_ticks      = self.config.get("min_stop_ticks", 40)
        max_stop_ticks      = self.config.get("max_stop_ticks", 120)
        stop_fallback_ticks = self.config.get("stop_fallback_ticks", 64)
        day_type = market.get("day_type", "UNKNOWN")
        mq_bias  = market.get("mq_direction_bias", "NEUTRAL")
        trend_day = (day_type == "TREND")
        # TREND days: 1 TF vote sufficient (context gives direction). Non-TREND: need 2.
        min_tf_votes = 1 if trend_day else self.config.get("min_tf_votes", 2)

        if len(bars_1m) < 2:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        ema9 = market.get("ema9", 0)
        ema21 = market.get("ema21", 0)
        cvd = market.get("cvd", 0)
        bullish = market.get("tf_votes_bullish", 0)
        bearish = market.get("tf_votes_bearish", 0)

        if vwap <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        confluences = []
        direction = None
        from config.settings import TICK_SIZE
        tick_size = TICK_SIZE

        # B14: config-driven VWAP proximity gate (was hardcoded 6t — too tight, caused
        # zero fills in prod). Default 60t = 15pts is more realistic for NQ pullbacks.
        max_vwap_dist = self.config.get("max_vwap_dist_ticks", 60)
        vwap_dist_ticks = abs(price - vwap) / tick_size
        if vwap_dist_ticks > max_vwap_dist:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED "
                f"gate:vwap_dist_too_far ({vwap_dist_ticks:.1f}t > {max_vwap_dist}t)"
            )
            return None

        # Check for pullback history: recent bars must show price WAS away from VWAP
        # (confirms this is a pullback, not just meandering near VWAP)
        recent_highs = [b.high for b in bars_1m[-5:]]
        recent_lows = [b.low for b in bars_1m[-5:]]
        max_dist_above = (max(recent_highs) - vwap) / tick_size
        max_dist_below = (vwap - min(recent_lows)) / tick_size

        # Bullish pullback: price was above VWAP, pulled back to it
        # TREND days: MQ bias sets direction. Non-TREND: need TF vote majority.
        if trend_day and mq_bias == "LONG" and max_dist_above >= 8:
            direction = "LONG"
            confluences.append(f"TREND day (MQ LONG) pullback — {max_dist_above:.0f}t from VWAP")
        elif trend_day and mq_bias == "SHORT" and max_dist_below >= 8:
            direction = "SHORT"
            confluences.append(f"TREND day (MQ SHORT) pullback — {max_dist_below:.0f}t from VWAP")
        elif bullish >= min_tf_votes and max_dist_above >= 8:
            direction = "LONG"
            confluences.append(f"Bullish TF: {bullish}/4")
            confluences.append(f"Pullback from {max_dist_above:.0f}t above VWAP")
        elif bearish >= min_tf_votes and max_dist_below >= 8:
            direction = "SHORT"
            confluences.append(f"Bearish TF: {bearish}/4")
            confluences.append(f"Pullback from {max_dist_below:.0f}t below VWAP")
        else:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_pullback_detected")
            return None

        confluences.append(f"Near VWAP ({vwap_dist_ticks:.0f}t away)")

        # EMA confirmation (trend structure intact)
        score = 30  # Base
        if direction == "LONG" and ema9 > ema21:
            score += 10
            confluences.append("EMA9 > EMA21 (trend intact)")
        elif direction == "SHORT" and ema9 < ema21:
            score += 10
            confluences.append("EMA9 < EMA21 (trend intact)")

        # CVD confirmation (buyers/sellers still present)
        if direction == "LONG" and cvd > 0:
            score += 10
            confluences.append("CVD positive")
        elif direction == "SHORT" and cvd < 0:
            score += 10
            confluences.append("CVD negative")

        # Bounce candle confirmation (REQUIRED — must show reversal)
        last = bars_1m[-1]
        has_bounce = False
        if direction == "LONG" and last.close > last.open:
            score += 10
            has_bounce = True
            confluences.append("Bounce candle (bullish)")
        elif direction == "SHORT" and last.close < last.open:
            score += 10
            has_bounce = True
            confluences.append("Bounce candle (bearish)")

        if not has_bounce:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_bounce_candle")
            return None  # No bounce = no entry, wait for confirmation

        confluences.append(f"Regime: {session_info.get('regime', '?')}")

        # B14: NQ-calibrated ATR-anchored stop (replaces fixed stop_ticks).
        from strategies._nq_stop import compute_atr_stop
        atr_5m = market.get("atr_5m", 0) or 0
        last_5m = bars_5m[-1] if bars_5m else None
        stop_ticks, stop_price, atr_override, stop_note = compute_atr_stop(
            direction=direction,
            entry_price=price,
            last_5m_bar=last_5m,
            atr_5m_points=atr_5m,
            tick_size=tick_size,
            stop_atr_mult=self.config.get("stop_atr_mult", 2.0),
            min_stop_ticks=self.config.get("min_stop_ticks", 40),
            max_stop_ticks=self.config.get("max_stop_ticks", 120),
            stop_fallback_ticks=self.config.get("stop_fallback_ticks", 64),
        )
        confluences.append(stop_note)

        logger.info(f"[EVAL] {self.name}: SIGNAL {direction} entry={price:.2f}")
        sig = Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=score,
            entry_score=min(60, score),
            strategy=self.name,
            reason=f"VWAP pullback {direction} — {vwap_dist_ticks:.0f}t from VWAP, score {score}",
            confluences=confluences,
        )
        sig.atr_stop_override = atr_override
        if atr_override and stop_price is not None:
            sig.stop_price = stop_price
        return sig
