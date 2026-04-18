"""
Phoenix Bot — VWAP Pullback Strategy

Enters on first pullback to VWAP in a trending market.
Best during MID_MORNING regime (9:30-11:00 CST).

Logic: TF bias says bullish → price pulled back to/below VWAP → bounce candle confirms.
The entry is ON the pullback touch, not after price already reclaimed.
"""

from strategies.base_strategy import BaseStrategy, Signal


class VWAPPullback(BaseStrategy):
    name = "vwap_pullback"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        stop_ticks = self.config.get("stop_ticks", 8)
        target_rr = self.config.get("target_rr", 1.8)
        day_type = market.get("day_type", "UNKNOWN")
        mq_bias  = market.get("mq_direction_bias", "NEUTRAL")
        trend_day = (day_type == "TREND")
        # TREND days: 1 TF vote sufficient (context gives direction). Non-TREND: need 2.
        min_tf_votes = 1 if trend_day else self.config.get("min_tf_votes", 2)

        if len(bars_1m) < 2:
            return None

        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        ema9 = market.get("ema9", 0)
        ema21 = market.get("ema21", 0)
        cvd = market.get("cvd", 0)
        bullish = market.get("tf_votes_bullish", 0)
        bearish = market.get("tf_votes_bearish", 0)

        if vwap <= 0:
            return None

        confluences = []
        direction = None
        from config.settings import TICK_SIZE
        tick_size = TICK_SIZE

        # Price must be near VWAP (within 6 ticks — wider zone for pullback detection)
        vwap_dist_ticks = abs(price - vwap) / tick_size
        if vwap_dist_ticks > 6:
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
            return None  # No bounce = no entry, wait for confirmation

        confluences.append(f"Regime: {session_info.get('regime', '?')}")

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=score,
            entry_score=min(60, score),
            strategy=self.name,
            reason=f"VWAP pullback {direction} — {vwap_dist_ticks:.0f}t from VWAP, score {score}",
            confluences=confluences,
        )
