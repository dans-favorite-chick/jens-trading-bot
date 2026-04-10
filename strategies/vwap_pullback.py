"""
Phoenix Bot — VWAP Pullback Strategy

Enters on first pullback to VWAP in a trending market.
Best during MID_MORNING regime (9:30-11:00 CST).
"""

from strategies.base_strategy import BaseStrategy, Signal


class VWAPPullback(BaseStrategy):
    name = "vwap_pullback"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        min_confluence = self.config.get("min_confluence", 3.2)
        min_tf_votes = self.config.get("min_tf_votes", 3)
        stop_ticks = self.config.get("stop_ticks", 8)
        target_rr = self.config.get("target_rr", 1.8)

        if len(bars_1m) < 3:
            return None

        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        ema9 = market.get("ema9", 0)
        cvd = market.get("cvd", 0)
        bullish = market.get("tf_votes_bullish", 0)
        bearish = market.get("tf_votes_bearish", 0)

        if vwap <= 0:
            return None

        confluences = []
        direction = None

        # Bullish pullback: price was above VWAP, pulled back to touch it, bouncing
        vwap_distance = abs(price - vwap)
        tick_size = 0.25
        vwap_dist_ticks = vwap_distance / tick_size

        # Price near VWAP (within 4 ticks)
        if vwap_dist_ticks > 4:
            return None

        confluences.append(f"Near VWAP ({vwap_dist_ticks:.0f}t away)")

        # Determine direction from TF bias
        if bullish >= min_tf_votes and price >= vwap:
            direction = "LONG"
            confluences.append(f"Bullish TF: {bullish}/4")
        elif bearish >= min_tf_votes and price <= vwap:
            direction = "SHORT"
            confluences.append(f"Bearish TF: {bearish}/4")
        else:
            return None

        # EMA confirmation
        if direction == "LONG" and ema9 > vwap:
            confluences.append("EMA9 above VWAP")
        elif direction == "SHORT" and ema9 < vwap:
            confluences.append("EMA9 below VWAP")

        # CVD confirmation
        score = 35  # Base
        if direction == "LONG" and cvd > 0:
            score += 10
            confluences.append("CVD positive")
        elif direction == "SHORT" and cvd < 0:
            score += 10
            confluences.append("CVD negative")

        # Recent bar showing bounce
        last = bars_1m[-1]
        if direction == "LONG" and last.close > last.open:
            score += 10
            confluences.append("Bounce candle (bullish)")
        elif direction == "SHORT" and last.close < last.open:
            score += 10
            confluences.append("Bounce candle (bearish)")

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
