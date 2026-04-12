"""
Phoenix Bot — Spring Setup Strategy

Port from MNQ v5 Elite Spring pattern. The "Rule of Three":
1. Spring wick (liquidity grab at S/R) >= 6 ticks
2. VWAP reclaim (price back above/below VWAP after sweep)
3. Delta flip (CVD direction confirms reversal)

All three must confirm. Stop = 1.5x wick size. Target = 1.5:1 RR.
"""

from strategies.base_strategy import BaseStrategy, Signal


class SpringSetup(BaseStrategy):
    name = "spring_setup"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        stop_mult = self.config.get("stop_multiplier", 1.5)
        target_rr = self.config.get("target_rr", 1.5)
        min_wick = self.config.get("min_wick_ticks", 6)
        require_vwap = self.config.get("require_vwap_reclaim", True)
        require_delta = self.config.get("require_delta_flip", True)
        max_hold = self.config.get("max_hold_min", 15)
        from config.settings import TICK_SIZE
        tick_size = TICK_SIZE

        if len(bars_1m) < 2:
            return None  # Need at least 2 bars to detect wick pattern

        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        cvd = market.get("cvd", 0)

        # ── Detect spring wick in recent 1m bars ────────────────────
        # A spring = long lower wick (bullish) or long upper wick (bearish)
        # that sweeps below/above recent support/resistance then reclaims

        last_bar = bars_1m[-1]
        prev_bar = bars_1m[-2]

        # Bullish spring: long lower wick, close near high
        body_size = abs(last_bar.close - last_bar.open)
        lower_wick = min(last_bar.open, last_bar.close) - last_bar.low
        upper_wick = last_bar.high - max(last_bar.open, last_bar.close)

        lower_wick_ticks = lower_wick / tick_size
        upper_wick_ticks = upper_wick / tick_size

        direction = None
        wick_ticks = 0
        confluences = []

        # Bullish spring: long lower wick + close near high
        if (lower_wick_ticks >= min_wick and
                last_bar.close > last_bar.open and
                lower_wick > body_size * 1.5):
            direction = "LONG"
            wick_ticks = lower_wick_ticks
            confluences.append(f"Bullish spring wick: {wick_ticks:.0f}t")

        # Bearish spring: long upper wick + close near low
        elif (upper_wick_ticks >= min_wick and
              last_bar.close < last_bar.open and
              upper_wick > body_size * 1.5):
            direction = "SHORT"
            wick_ticks = upper_wick_ticks
            confluences.append(f"Bearish spring wick: {wick_ticks:.0f}t")

        if direction is None:
            return None

        # ── Rule 2: VWAP reclaim ────────────────────────────────────
        vwap_confirmed = False
        if require_vwap:
            if direction == "LONG" and price > vwap:
                vwap_confirmed = True
                confluences.append("VWAP reclaimed (above)")
            elif direction == "SHORT" and price < vwap:
                vwap_confirmed = True
                confluences.append("VWAP reclaimed (below)")
            else:
                return None  # VWAP not reclaimed
        else:
            vwap_confirmed = True

        # ── Rule 3: Delta flip ──────────────────────────────────────
        delta_confirmed = False
        if require_delta:
            if direction == "LONG" and cvd > 0:
                delta_confirmed = True
                confluences.append(f"CVD positive: {cvd:.0f}")
            elif direction == "SHORT" and cvd < 0:
                delta_confirmed = True
                confluences.append(f"CVD negative: {cvd:.0f}")
            else:
                return None  # Delta not confirming
        else:
            delta_confirmed = True

        # ── Calculate stop and score ────────────────────────────────
        stop_ticks = int(wick_ticks * stop_mult)
        stop_ticks = max(stop_ticks, 6)  # Minimum 6 tick stop

        # Entry score based on wick quality + confirmations
        entry_score = 30  # Base for having a spring
        if wick_ticks >= 10:
            entry_score += 10
        elif wick_ticks >= 8:
            entry_score += 5
        if vwap_confirmed:
            entry_score += 10
        if delta_confirmed:
            entry_score += 10

        confluences.append(f"Stop: {stop_ticks}t (wick x{stop_mult})")
        confluences.append(f"Regime: {session_info.get('regime', '?')}")

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=min(100, entry_score * 1.5),
            entry_score=min(60, entry_score),
            strategy=self.name,
            reason=f"Spring {direction} — wick {wick_ticks:.0f}t, VWAP {'ok' if vwap_confirmed else 'no'}, CVD {'ok' if delta_confirmed else 'no'}",
            confluences=confluences,
        )
