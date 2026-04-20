"""
Phoenix Bot — Spring Setup Strategy

Port from MNQ v5 Elite Spring pattern. The "Rule of Three":
1. Spring wick (liquidity grab at S/R) >= 6 ticks
2. VWAP reclaim (price back above/below VWAP after sweep)
3. Delta flip (CVD direction confirms reversal)

All three must confirm. Stop = structure low/high +/- buffer (not wick multiplier).
Target = 1.5:1 RR minimum.

v2 fixes (2026-04-14):
- TF alignment gate: spring must fire WITH dominant trend (3/4 TF votes)
- Structure-based stop: stop placed at min/max(last_bar, prev_bar) low/high ±2t
  rather than wick×1.5 — avoids getting stopped at exact session low
"""

import logging

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)


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
        # FIX 1: TF alignment gate — spring must fire WITH dominant trend direction
        require_tf_alignment = self.config.get("require_tf_alignment", True)
        min_tf_votes = self.config.get("min_tf_votes", 3)
        # FIX 2: Structure-based stop — stop at bar low/high rather than wick×mult
        stop_at_structure = self.config.get("stop_at_structure", True)
        structure_buffer_ticks = self.config.get("structure_buffer_ticks", 2)
        from config.settings import TICK_SIZE
        tick_size = TICK_SIZE

        if len(bars_1m) < 2:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
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
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_spring_wick")
            return None

        # ── TREND Day Direction Gate ─────────────────────────────────
        # On TREND days, springs fire WITH the trend only — not against it.
        # A bearish wick on a High-Conviction Bullish TREND day is NOT a
        # reversal signal — it's a pullback before continuation. Shorting it
        # has a historically terrible WR. Block it hard regardless of lab/prod mode.
        day_type = market.get("day_type", "UNKNOWN")
        mq_bias = market.get("mq_direction_bias", "NEUTRAL")
        if day_type == "TREND":
            if direction == "SHORT" and mq_bias == "LONG":
                self._last_reject = "TREND day + MQ LONG: spring SHORTs blocked (counter-trend)"
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:trend_day_counter_trend_short")
                return None
            elif direction == "LONG" and mq_bias == "SHORT":
                self._last_reject = "TREND day + MQ SHORT: spring LONGs blocked (counter-trend)"
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:trend_day_counter_trend_long")
                return None
            # No MQ bias set but still a TREND day — use C/R verdict if available
            elif mq_bias == "NEUTRAL":
                cr_verdict = market.get("cr_verdict", "UNKNOWN")
                if direction == "SHORT" and cr_verdict == "CONTINUATION":
                    self._last_reject = "TREND day (CONTINUATION): spring SHORTs blocked"
                    logger.debug(f"[EVAL] {self.name}: BLOCKED gate:trend_day_continuation_short")
                    return None

        # ── FIX 1: TF Alignment Gate ─────────────────────────────────
        # Spring must fire WITH the dominant trend — counter-trend springs have
        # 16% WR. Require at least 3/4 TFs aligned with direction.
        if require_tf_alignment:
            bullish_votes = market.get("tf_votes_bullish", 0)
            bearish_votes = market.get("tf_votes_bearish", 0)
            if direction == "LONG" and bullish_votes < min_tf_votes:
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:tf_alignment_long")
                return None  # Counter-trend long — not enough TF alignment
            elif direction == "SHORT" and bearish_votes < min_tf_votes:
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:tf_alignment_short")
                return None  # Counter-trend short — not enough TF alignment
            votes = bullish_votes if direction == "LONG" else bearish_votes
            confluences.append(f"TF aligned: {votes}/{min_tf_votes}+ votes")

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
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:vwap_not_reclaimed")
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
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:delta_not_confirming")
                return None  # Delta not confirming
        else:
            delta_confirmed = True

        # ── ATR-Based Stop (anchored to wick extreme) ─────────────────
        # Research: 1.0–1.2 × 5m ATR from the wick extreme is the validated
        # range for reversal patterns. Anchoring to the wick low/high (not entry)
        # ensures the stop is BELOW the level that was already defended by liquidity.
        #
        # Stop placement:
        #   LONG:  stop_price = last_bar.low  − (atr_mult × ATR_5m)
        #   SHORT: stop_price = last_bar.high + (atr_mult × ATR_5m)
        # Then compute stop_ticks = (price − stop_price) / tick_size
        #
        # Setting signal.atr_stop_override = True tells base_bot NOT to apply
        # the global ATR override on top of this (it's already ATR-based).
        atr_mult = self.config.get("atr_stop_multiplier", 1.1)
        max_stop_ticks = self.config.get("max_stop_ticks", 40)   # $20 risk cap at 1 contract
        min_stop_ticks = self.config.get("min_stop_ticks", 8)
        atr_5m = market.get("atr_5m", 0) or 0
        atr_stop_override = False

        if atr_5m > 0:
            if direction == "LONG":
                stop_price = last_bar.low - (atr_mult * atr_5m)
                stop_distance = price - stop_price
            else:  # SHORT
                stop_price = last_bar.high + (atr_mult * atr_5m)
                stop_distance = stop_price - price

            if stop_distance > 0:
                raw_ticks = int(stop_distance / tick_size)
                stop_ticks = max(min_stop_ticks, min(max_stop_ticks, raw_ticks))
                wick_ref = "low" if direction == "LONG" else "high"
                confluences.append(
                    f"ATR stop: wick {wick_ref} {atr_mult}xATR5m({atr_5m:.1f}pt) = {stop_ticks}t"
                    + (f" [capped from {raw_ticks}t]" if raw_ticks > max_stop_ticks else "")
                )
                atr_stop_override = True
            else:
                # Price already past the wick extreme — stale signal
                logger.debug(f"[EVAL] {self.name}: NO_SIGNAL price_past_wick_extreme")
                return None
        else:
            # ATR not available — fall back to structure stop
            if direction == "LONG":
                structure_low = min(last_bar.low, prev_bar.low)
                stop_distance = price - structure_low + (structure_buffer_ticks * tick_size)
            else:
                structure_high = max(last_bar.high, prev_bar.high)
                stop_distance = structure_high - price + (structure_buffer_ticks * tick_size)
            stop_ticks = max(min_stop_ticks, int(stop_distance / tick_size))
            confluences.append(f"Structure stop (ATR unavailable): {stop_ticks}t")

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
        # Bonus for strong TF alignment
        if require_tf_alignment:
            votes = market.get("tf_votes_bullish" if direction == "LONG" else "tf_votes_bearish", 0)
            if votes >= 4:
                entry_score += 5  # Full 4/4 TF alignment bonus

        # Volume climax on the wick bar = institutional sweep (highest quality spring)
        # Smart money uses high-volume wicks to sweep stops and enter large positions.
        # A spring on a climax bar is MORE valid, not less.
        vol_climax_ratio = float(market.get("vol_climax_ratio", 1.0) or 1.0)
        if vol_climax_ratio >= 2.0:
            entry_score += 10
            confluences.append(f"Volume climax on spring ({vol_climax_ratio:.1f}x avg) — institutional sweep")
        elif vol_climax_ratio >= 1.5:
            entry_score += 5
            confluences.append(f"Above-avg volume on spring ({vol_climax_ratio:.1f}x)")

        # VSA confirmation: high-volume absorption bar at spring level = accumulation
        vsa = market.get("vsa_signal_5m", "NEUTRAL") or "NEUTRAL"
        if (vsa == "EFFORT_UP" and direction == "LONG") or (vsa == "EFFORT_DOWN" and direction == "SHORT"):
            entry_score += 8
            confluences.append(f"VSA effort bar confirms spring direction")
        elif vsa == "ABSORPTION":
            # Absorption at the wick level = supply being absorbed = bullish for LONG spring
            entry_score += 5
            confluences.append("VSA absorption at spring level — supply being soaked")

        confluences.append(f"Regime: {session_info.get('regime', '?')}")

        price = market.get("price", 0)
        logger.info(f"[EVAL] {self.name}: SIGNAL {direction} entry={price:.2f}")
        sig = Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=min(100, entry_score * 1.5),
            entry_score=min(60, entry_score),
            strategy=self.name,
            reason=f"Spring {direction} — wick {wick_ticks:.0f}t, VWAP {'ok' if vwap_confirmed else 'no'}, CVD {'ok' if delta_confirmed else 'no'}",
            confluences=confluences,
        )
        sig.atr_stop_override = atr_stop_override
        return sig
