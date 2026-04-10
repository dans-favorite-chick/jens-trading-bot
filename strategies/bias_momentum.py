"""
Phoenix Bot — Bias Momentum Follow Strategy

Port from V3 BiasMomentumFollow. Trades in the direction of multi-TF
bias when momentum confirms. Baseline validated strategy.
"""

from strategies.base_strategy import BaseStrategy, Signal


class BiasMomentumFollow(BaseStrategy):
    name = "bias_momentum"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        min_confluence = self.config.get("min_confluence", 3.0)
        min_tf_votes = self.config.get("min_tf_votes", 3)
        min_momentum = self.config.get("min_momentum", 55)
        stop_ticks = self.config.get("stop_ticks", 9)
        target_rr = self.config.get("target_rr", 2.0)
        max_hold = self.config.get("max_hold_min", 25)

        # Need enough bars
        if len(bars_5m) < 5:
            return None

        # ── Multi-TF alignment check ───────────────────────────────
        tf_bias = market.get("tf_bias", {})
        bullish_votes = market.get("tf_votes_bullish", 0)
        bearish_votes = market.get("tf_votes_bearish", 0)

        if bullish_votes >= min_tf_votes:
            direction = "LONG"
            votes = bullish_votes
        elif bearish_votes >= min_tf_votes:
            direction = "SHORT"
            votes = bearish_votes
        else:
            return None  # Not enough alignment

        # ── Momentum check ──────────────────────────────────────────
        # Use price vs VWAP + EMA trend as momentum proxy
        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        ema9 = market.get("ema9", 0)
        ema21 = market.get("ema21", 0)
        cvd = market.get("cvd", 0)

        momentum_score = 0
        confluences = []

        # Price vs VWAP
        if direction == "LONG" and price > vwap:
            momentum_score += 20
            confluences.append("Price above VWAP")
        elif direction == "SHORT" and price < vwap:
            momentum_score += 20
            confluences.append("Price below VWAP")

        # EMA alignment
        if direction == "LONG" and ema9 > ema21:
            momentum_score += 20
            confluences.append("EMA9 > EMA21")
        elif direction == "SHORT" and ema9 < ema21:
            momentum_score += 20
            confluences.append("EMA9 < EMA21")

        # CVD confirmation
        if direction == "LONG" and cvd > 0:
            momentum_score += 15
            confluences.append("CVD positive")
        elif direction == "SHORT" and cvd < 0:
            momentum_score += 15
            confluences.append("CVD negative")

        # Recent bar direction
        if len(bars_5m) >= 2:
            last_bar = bars_5m[-1]
            prev_bar = bars_5m[-2]
            if direction == "LONG" and last_bar.close > prev_bar.close:
                momentum_score += 15
                confluences.append("Rising 5m bars")
            elif direction == "SHORT" and last_bar.close < prev_bar.close:
                momentum_score += 15
                confluences.append("Falling 5m bars")

        # ATR check (avoid vol fade)
        atr_5m = market.get("atr_5m", 0)
        if atr_5m > 0:
            momentum_score += 10
            confluences.append(f"ATR={atr_5m:.1f}")

        if momentum_score < min_momentum:
            return None

        # ── Confluence score ────────────────────────────────────────
        confluence = votes + (momentum_score / 30)  # Normalize momentum to ~0-2.7
        if confluence < min_confluence:
            return None

        # ── Build signal ────────────────────────────────────────────
        confluences.append(f"TF votes: {votes}/4")
        confluences.append(f"Regime: {session_info.get('regime', '?')}")

        # Entry score (0-60) for risk sizing
        entry_score = min(60, momentum_score + votes * 5)

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=momentum_score,
            entry_score=entry_score,
            strategy=self.name,
            reason=f"Bias momentum {direction} — {votes}/4 TF aligned, momentum {momentum_score}",
            confluences=confluences,
        )
