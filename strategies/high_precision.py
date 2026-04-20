"""
Phoenix Bot — High Precision Only Strategy

Very selective — requires high TF alignment and momentum.
Quick target, tight stop.
"""

import logging

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class HighPrecisionOnly(BaseStrategy):
    name = "high_precision_only"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        min_confluence = self.config.get("min_confluence", 3.5)
        min_tf_votes = self.config.get("min_tf_votes", 4)
        min_precision = self.config.get("min_precision", 55)
        stop_ticks = self.config.get("stop_ticks", 8)
        target_rr = self.config.get("target_rr", 1.5)

        if len(bars_1m) < 3:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        ema9 = market.get("ema9", 0)
        ema21 = market.get("ema21", 0)
        cvd = market.get("cvd", 0)
        bullish = market.get("tf_votes_bullish", 0)
        bearish = market.get("tf_votes_bearish", 0)

        # Need strong TF alignment
        if bullish >= min_tf_votes:
            direction = "LONG"
        elif bearish >= min_tf_votes:
            direction = "SHORT"
        else:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:tf_votes_insufficient")
            return None

        confluences = [f"TF alignment: {max(bullish,bearish)}/4"]
        score = 0

        # Price vs VWAP
        if direction == "LONG" and price > vwap:
            score += 15
            confluences.append("Above VWAP")
        elif direction == "SHORT" and price < vwap:
            score += 15
            confluences.append("Below VWAP")

        # EMA stack
        if direction == "LONG" and ema9 > ema21:
            score += 15
            confluences.append("EMA9 > EMA21")
        elif direction == "SHORT" and ema9 < ema21:
            score += 15
            confluences.append("EMA9 < EMA21")

        # CVD
        if direction == "LONG" and cvd > 0:
            score += 10
            confluences.append("CVD positive")
        elif direction == "SHORT" and cvd < 0:
            score += 10
            confluences.append("CVD negative")

        # Recent candle precision
        if len(bars_1m) >= 2:
            last = bars_1m[-1]
            body = abs(last.close - last.open)
            total = last.high - last.low if last.high > last.low else 0.01
            precision = (body / total) * 100
            if precision >= min_precision:
                score += 15
                confluences.append(f"Candle precision: {precision:.0f}%")

        if score < 30:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL score={score}<30")
            return None

        confluences.append(f"Regime: {session_info.get('regime', '?')}")

        logger.info(f"[EVAL] {self.name}: SIGNAL {direction} entry={price:.2f}")
        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=score,
            entry_score=min(60, score),
            strategy=self.name,
            reason=f"High precision {direction} — score {score}, {max(bullish,bearish)}/4 TF",
            confluences=confluences,
        )
