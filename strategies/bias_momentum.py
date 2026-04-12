"""
Phoenix Bot — Bias Momentum Follow Strategy

Port from V3 BiasMomentumFollow. Trades in the direction of multi-TF
bias when momentum confirms. Baseline validated strategy.

REGIME-AWARE: Loosens gates in golden windows (OPEN_MOMENTUM, MID_MORNING)
to maximize signal generation when edge is highest.
"""

from strategies.base_strategy import BaseStrategy, Signal
from core.candlestick_patterns import CandlestickAnalyzer, get_pattern_confluence

# Regime-specific overrides — BE AGGRESSIVE in golden windows
# Non-golden regimes use strategy config defaults (tighter gates)
_REGIME_OVERRIDES = {
    "OPEN_MOMENTUM": {"min_tf_votes": 2, "min_momentum": 35, "min_confluence": 2.0},
    "MID_MORNING":   {"min_tf_votes": 2, "min_momentum": 35, "min_confluence": 2.0},
    "LATE_AFTERNOON": {"min_tf_votes": 2, "min_momentum": 45, "min_confluence": 2.5},
    # Lab bot regimes — still need TF alignment but lower thresholds to practice
    "OVERNIGHT_RANGE": {"min_tf_votes": 2, "min_momentum": 40, "min_confluence": 2.5},
    "AFTERHOURS":      {"min_tf_votes": 2, "min_momentum": 40, "min_confluence": 2.5},
    "PREMARKET_DRIFT":  {"min_tf_votes": 2, "min_momentum": 40, "min_confluence": 2.5},
    "AFTERNOON_CHOP":   {"min_tf_votes": 2, "min_momentum": 45, "min_confluence": 2.5},
    "CLOSE_CHOP":       {"min_tf_votes": 2, "min_momentum": 45, "min_confluence": 2.5},
}


class BiasMomentumFollow(BaseStrategy):
    name = "bias_momentum"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        # Get regime and apply overrides for golden windows
        regime = session_info.get("regime", "UNKNOWN")
        overrides = _REGIME_OVERRIDES.get(regime, {})

        min_confluence = overrides.get("min_confluence", self.config.get("min_confluence", 3.0))
        min_tf_votes = overrides.get("min_tf_votes", self.config.get("min_tf_votes", 3))
        min_momentum = overrides.get("min_momentum", self.config.get("min_momentum", 55))
        stop_ticks = self.config.get("stop_ticks", 9)
        target_rr = self.config.get("target_rr", 2.0)

        # Minimal warmup — only need 1 bar to have data, use what we have
        if len(bars_1m) < 1:
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
        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        ema9 = market.get("ema9", 0)
        ema21 = market.get("ema21", 0)
        cvd = market.get("cvd", 0)

        momentum_score = 0
        confluences = [f"Regime: {regime}"]

        # Price vs VWAP
        if direction == "LONG" and price > vwap and vwap > 0:
            momentum_score += 20
            confluences.append("Price above VWAP")
        elif direction == "SHORT" and price < vwap and vwap > 0:
            momentum_score += 20
            confluences.append("Price below VWAP")

        # EMA alignment (use whatever bars are available)
        if ema9 > 0 and ema21 > 0:
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

        # Recent bar direction (use 5m if available, else 1m)
        bars = bars_5m if len(bars_5m) >= 2 else bars_1m
        if len(bars) >= 2:
            last_bar = bars[-1]
            prev_bar = bars[-2]
            if direction == "LONG" and last_bar.close > prev_bar.close:
                momentum_score += 15
                confluences.append("Rising bars")
            elif direction == "SHORT" and last_bar.close < prev_bar.close:
                momentum_score += 15
                confluences.append("Falling bars")

        # ATR check (avoid vol fade)
        atr_5m = market.get("atr_5m", 0)
        atr_1m = market.get("atr_1m", 0)
        atr = atr_5m if atr_5m > 0 else atr_1m
        if atr > 0:
            momentum_score += 10
            confluences.append(f"ATR={atr:.1f}")

        # ── Opening Range first-candle boost ────────────────────────
        # First completed 5m candle after open predicts day direction ~65%
        if regime in ("OPEN_MOMENTUM",) and len(bars_5m) >= 1:
            first_bar = bars_5m[0]  # First 5m bar of the session
            if direction == "LONG" and first_bar.close > first_bar.open:
                momentum_score += 20
                confluences.append("First 5m candle bullish (opening range signal)")
            elif direction == "SHORT" and first_bar.close < first_bar.open:
                momentum_score += 20
                confluences.append("First 5m candle bearish (opening range signal)")

        # ── Candlestick pattern confluence ────────────────────────────
        analyzer = CandlestickAnalyzer()
        candle_bars = bars_1m[-20:] if len(bars_1m) >= 20 else bars_1m
        patterns = analyzer.analyze(candle_bars)
        pattern_conf = get_pattern_confluence(patterns, direction)
        if pattern_conf["net_score"] > 30:
            momentum_score += 15
            confluences.append(f"Candle pattern: {pattern_conf['description']}")
        elif pattern_conf["net_score"] < -30:
            momentum_score -= 10  # Opposing pattern reduces confidence
            opposed = pattern_conf["strongest_opposed"]
            opposed_name = opposed["pattern"] if opposed else "unknown"
            confluences.append(f"Warning: opposing pattern {opposed_name}")

        if momentum_score < min_momentum:
            return None

        # ── Confluence score ────────────────────────────────────────
        confluence = votes + (momentum_score / 30)
        if confluence < min_confluence:
            return None

        confluences.append(f"TF: {votes}/4 {'bull' if direction == 'LONG' else 'bear'}")
        confluences.append(f"Momentum: {momentum_score}")

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=momentum_score,
            entry_score=min(60, int(momentum_score * 0.75)),
            strategy=self.name,
            reason=f"Bias Momentum {direction} — {votes}/4 TF, score {momentum_score}, regime {regime}",
            confluences=confluences,
        )
