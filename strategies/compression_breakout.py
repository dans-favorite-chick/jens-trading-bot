"""
Phoenix Bot — Compression Breakout Strategy  (PRE-explosion entry)

Detects the coil-then-explode pattern and enters DURING the coil —
before the explosion bar fires — so we ride the explosion itself.

Three moves on 2026-04-13 illustrate the opportunity:

  08:49  Squeeze 1: spring/reversal at open (handled by bias_momentum/spring_setup)
  11:07  Squeeze 2: 5-bar coil → 57pt explosion → 109pt total move  ($218 MNQ)
  14:51  Squeeze 3: 12-bar coil → 42pt explosion → 101pt total move ($202 MNQ)

PRE vs POST entry comparison (why pre matters):
  Squeeze 2 PRE: entry 25288.50, stop $14, 506t profit available
  Squeeze 2 POST: entry 25310.75, stop $58, 417t profit available
  Squeeze 3 PRE: entry 25471.25, stop $32, 476t profit available
  Squeeze 3 POST: entry 25509.50, stop $108, 323t profit available

Pre-explosion entry gives 2-4x tighter stops AND catches the explosion
bar itself as profit — the biggest bar of the entire move.

═══════════════════════════════════════════════════════════════════════
THE THREE PHASES (context only — we never wait for phase 3 to enter):

  COIL   : Market compresses. Consecutive bars with range well below
            session baseline ATR. Price holds its level — sellers
            failing to push down (or buyers failing to push up).

  SIGNAL : Pre-explosion tells appear inside the coil:
            A) VRR Absorption  — massive volume, tiny range = institutional
               absorption. Someone is loading a position against the flow.
            B) Exhaustion Turn — after 5+ consecutive one-direction bars,
               the first reversal signals the move has exhausted itself.
            C) Close Breakout  — bar closes at highest (or lowest) level
               of the last 5 coil bars = buyers (sellers) quietly taking
               control inside the apparently quiet coil.
            D) ATR Declining   — coil range is shrinking bar-over-bar =
               energy is building. Not directional alone but validates quality.

  EXPLOSION: The bar we ride through, not enter at.

═══════════════════════════════════════════════════════════════════════
ENTRY LOGIC:

  1. Coil established: N consecutive tight bars (range <= baseline * tight_mult)
  2. ONE OR MORE pre-explosion signals fire on the CURRENT bar
  3. Direction confirmed by TF votes OR by the exhaustion/close signal itself
  4. Enter at current bar close. Stop at coil structural boundary (tight!).
  5. Target: 3:1 RR minimum — these moves run 400+ ticks

KEY DESIGN — pre-explosion ATR:
  We compute baseline ATR from bars BEFORE the current bar, NOT from
  market["atr_1m"] which includes the current bar. This keeps the tight-bar
  threshold accurate through the full coil life.

REGIME-AWARE:
  Primary (OPEN_MOMENTUM, MID_MORNING):  3 coil bars, tight_mult 0.90
  Afternoon (AFTERNOON_CHOP):            5 coil bars, tight_mult 1.20
  Late/Close (LATE_AFTERNOON, CLOSE_CHOP): 5-6 coil bars, tight_mult 1.50

NOTE: validated=False — run in lab bot to build sample before prod promotion.
"""

from strategies.base_strategy import BaseStrategy, Signal

# ── Regime-specific thresholds ────────────────────────────────────────────────
# tight_mult: a bar qualifies as "tight" if range <= baseline_atr * tight_mult
#   Primary session: bars need to be noticeably below baseline (0.90x)
#   Afternoon: market is quieter, 1.20-1.50x catches the genuine coil pattern
_REGIME_PARAMS = {
    "OPEN_MOMENTUM":  {"min_coil_bars": 3, "min_tf_votes": 2, "tight_mult": 0.90},
    "MID_MORNING":    {"min_coil_bars": 3, "min_tf_votes": 2, "tight_mult": 0.90},
    "AFTERNOON_CHOP": {"min_coil_bars": 5, "min_tf_votes": 2, "tight_mult": 1.20},
    "LATE_AFTERNOON": {"min_coil_bars": 5, "min_tf_votes": 2, "tight_mult": 1.50},
    "CLOSE_CHOP":     {"min_coil_bars": 6, "min_tf_votes": 2, "tight_mult": 1.50},
    # Overnight/premarket/afterhours: not traded — too thin, random breaks
}

_BASELINE_LOOKBACK    = 15    # Bars used to compute pre-coil session ATR baseline
_VRR_LOOKBACK         = 30    # Bars for session VRR (volume/range) baseline
_VRR_ABSORPTION_MULT  = 3.0   # VRR spike threshold: must be >= 3x session avg
_EXHAUSTION_MIN_BARS  = 5     # Consecutive same-direction bars before reversal fires
_CLOSE_LOOKBACK       = 5     # Compare current close to last N coil-bar closes


class CompressionBreakout(BaseStrategy):
    name = "compression_breakout"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        regime = session_info.get("regime", "UNKNOWN")
        regime_params = _REGIME_PARAMS.get(regime)
        if not regime_params:
            return None  # Overnight, premarket, afterhours — skip

        # ── Pull config (strategies.py None values fall through to regime defaults) ──
        min_coil_bars = self.config.get("min_coil_bars") or regime_params["min_coil_bars"]
        min_tf_votes  = self.config.get("min_tf_votes",   regime_params["min_tf_votes"])
        tight_mult    = self.config.get("tight_mult")     or regime_params["tight_mult"]
        target_rr     = self.config.get("target_rr",      3.0)
        stop_buffer_ticks = self.config.get("stop_buffer_ticks", 3)

        # Need enough bars for a clean baseline
        if len(bars_1m) < _BASELINE_LOOKBACK + 2:
            return None

        # ─────────────────────────────────────────────────────────────────────
        # STEP 1: Baseline ATR — computed from bars BEFORE the current bar
        #         This keeps the tight-bar threshold accurate and uncontaminated
        #         by whatever the current bar is doing.
        # ─────────────────────────────────────────────────────────────────────
        curr_bar = bars_1m[-1]
        curr_range = curr_bar.high - curr_bar.low

        lookback_end   = len(bars_1m) - 1          # index of curr_bar
        lookback_start = max(0, lookback_end - _BASELINE_LOOKBACK)
        baseline_bars  = bars_1m[lookback_start:lookback_end]   # excludes curr_bar

        if len(baseline_bars) < 3:
            return None

        baseline_atr = sum(b.high - b.low for b in baseline_bars) / len(baseline_bars)
        if baseline_atr <= 0:
            return None

        tight_threshold = baseline_atr * tight_mult

        # ─────────────────────────────────────────────────────────────────────
        # STEP 2: Is the current bar tight? (Must be inside the coil.)
        # ─────────────────────────────────────────────────────────────────────
        if curr_range > tight_threshold:
            self._last_reject = (
                f"CURR BAR NOT TIGHT: range={curr_range:.1f}pt > "
                f"threshold={tight_threshold:.1f}pt "
                f"(baseline={baseline_atr:.1f}pt, tight_mult={tight_mult}, "
                f"regime={regime})"
            )
            return None

        # ─────────────────────────────────────────────────────────────────────
        # STEP 3: Count consecutive tight bars ending at the current bar.
        #         coil_bars[0] = curr_bar (most recent), coil_bars[-1] = oldest.
        #         Walk backwards through bars_1m[:-1] counting tight bars.
        # ─────────────────────────────────────────────────────────────────────
        coil_bars   = [curr_bar]
        coil_lows   = [curr_bar.low]
        coil_highs  = [curr_bar.high]
        coil_closes = [curr_bar.close]

        for bar in reversed(bars_1m[:-1]):
            if bar.high - bar.low <= tight_threshold:
                coil_bars.append(bar)
                coil_lows.append(bar.low)
                coil_highs.append(bar.high)
                coil_closes.append(bar.close)
            else:
                break  # Non-tight bar breaks the coil sequence

        n_coil    = len(coil_bars)
        coil_low  = min(coil_lows)
        coil_high = max(coil_highs)

        if n_coil < min_coil_bars:
            self._last_reject = (
                f"COIL: only {n_coil} tight bar(s), need {min_coil_bars} | "
                f"threshold={tight_threshold:.1f}pt baseline={baseline_atr:.1f}pt "
                f"regime={regime}"
            )
            return None

        coil_avg_range = sum(b.high - b.low for b in coil_bars) / n_coil
        coil_depth_pct = 100 * coil_avg_range / baseline_atr  # how tight vs baseline

        # ─────────────────────────────────────────────────────────────────────
        # STEP 4: Pre-explosion signals
        #
        # Each signal produces a directional vote (LONG / SHORT / None) and
        # a description string added to signals_fired. We need at least one
        # signal before proceeding.
        # ─────────────────────────────────────────────────────────────────────
        signals_fired = []   # human-readable list for confluences
        bull_vote = 0        # signal directional score
        bear_vote = 0

        # ── Signal A: VRR Absorption ──────────────────────────────────────────
        # Volume/Range Ratio anomaly: huge volume but tiny range = someone is
        # absorbing all the selling (or buying) — the spring is being loaded.
        #
        # Squeeze 2 example: VRR of 26M vs session average ~250K-2.5M = 10-26x
        # The exploding absorption bar at 10:58 preceded a 109pt bullish move.
        #
        # Direction logic:
        #   Bar closes UP (sellers absorbed) → LONG (buyers winning)
        #   Bar closes DOWN (buyers absorbed) → SHORT (sellers winning)
        # ─────────────────────────────────────────────────────────────────────
        if curr_range > 0 and len(bars_1m) >= _VRR_LOOKBACK + 1:
            curr_vrr = curr_bar.volume / curr_range

            vrr_start = max(0, len(bars_1m) - _VRR_LOOKBACK - 1)
            session_bars = bars_1m[vrr_start:-1]    # exclude curr_bar
            session_vrrs = [
                b.volume / (b.high - b.low)
                for b in session_bars
                if b.high - b.low > 0 and b.volume > 0
            ]

            if session_vrrs:
                avg_vrr = sum(session_vrrs) / len(session_vrrs)
                if avg_vrr > 0:
                    vrr_ratio = curr_vrr / avg_vrr
                    if vrr_ratio >= _VRR_ABSORPTION_MULT:
                        # Bar direction tells us what's being absorbed
                        bar_is_bull = curr_bar.close >= curr_bar.open
                        if bar_is_bull:
                            bull_vote += 2
                            signals_fired.append(
                                f"VRR Absorption LONG: {curr_vrr:,.0f} vol/{curr_range:.1f}pt "
                                f"= {vrr_ratio:.1f}x session avg (bull bar — sellers absorbed)"
                            )
                        else:
                            bear_vote += 2
                            signals_fired.append(
                                f"VRR Absorption SHORT: {curr_vrr:,.0f} vol/{curr_range:.1f}pt "
                                f"= {vrr_ratio:.1f}x session avg (bear bar — buyers absorbed)"
                            )

        # ── Signal B: Exhaustion Turn ─────────────────────────────────────────
        # After 5+ consecutive same-direction bars in the coil, the first bar
        # that reverses signals exhaustion of that directional pressure.
        #
        # Squeeze 3 example: 7 consecutive BEAR bars (14:37-14:43) inside the
        # coil, sellers pushing but coil held. First BULL bar at 14:44 = sellers
        # exhausted = LONG signal 7 minutes before the 42pt explosion.
        #
        # coil_bars[0] = curr_bar. coil_bars[1:] = prior coil bars (most recent first).
        # ─────────────────────────────────────────────────────────────────────
        prev_coil = coil_bars[1:]   # bars before current, still in coil, reverse chronological

        if len(prev_coil) >= _EXHAUSTION_MIN_BARS:
            # Count consecutive bear bars immediately before current
            consec_bears = 0
            for b in prev_coil:
                if b.close < b.open:
                    consec_bears += 1
                else:
                    break

            if consec_bears >= _EXHAUSTION_MIN_BARS and curr_bar.close > curr_bar.open:
                bull_vote += 3   # Exhaustion is the strongest directional signal
                signals_fired.append(
                    f"Exhaustion LONG: {consec_bears} consecutive bear coil bars "
                    f"→ first bull bar (sellers exhausted)"
                )

            # Count consecutive bull bars immediately before current
            consec_bulls = 0
            for b in prev_coil:
                if b.close > b.open:
                    consec_bulls += 1
                else:
                    break

            if consec_bulls >= _EXHAUSTION_MIN_BARS and curr_bar.close < curr_bar.open:
                bear_vote += 3
                signals_fired.append(
                    f"Exhaustion SHORT: {consec_bulls} consecutive bull coil bars "
                    f"→ first bear bar (buyers exhausted)"
                )

        # ── Signal C: Close Breakout (momentum building inside coil) ──────────
        # Current bar closes at the HIGHEST level of the last 5 coil closes
        # on a bullish bar = buyers quietly taking control despite the quiet range.
        # Opposite for short. Think of it as a "stealth breakout" inside the coil.
        # ─────────────────────────────────────────────────────────────────────
        if len(coil_closes) > _CLOSE_LOOKBACK:
            prev_closes = coil_closes[1:_CLOSE_LOOKBACK + 1]  # 5 closes before current
            if prev_closes:
                curr_close = curr_bar.close
                if curr_bar.close > curr_bar.open and curr_close >= max(prev_closes):
                    bull_vote += 1
                    signals_fired.append(
                        f"Highest coil close: {curr_close:.2f} >= "
                        f"max of prior {len(prev_closes)} = {max(prev_closes):.2f} "
                        f"(buyers taking control quietly)"
                    )
                elif curr_bar.close < curr_bar.open and curr_close <= min(prev_closes):
                    bear_vote += 1
                    signals_fired.append(
                        f"Lowest coil close: {curr_close:.2f} <= "
                        f"min of prior {len(prev_closes)} = {min(prev_closes):.2f} "
                        f"(sellers taking control quietly)"
                    )

        # ── Signal D: ATR Declining (deepening compression) ───────────────────
        # Coil is getting tighter over time = energy is building.
        # Not directional on its own but validates coil quality.
        # Compare early-coil average range vs late-coil average range.
        # ─────────────────────────────────────────────────────────────────────
        atr_declining = False
        if n_coil >= 6:
            mid          = n_coil // 2
            recent_half  = coil_bars[:mid]       # [0..mid-1]: most recent bars
            older_half   = coil_bars[mid:]        # [mid..n-1]: older bars
            recent_atr   = sum(b.high - b.low for b in recent_half) / len(recent_half)
            older_atr    = sum(b.high - b.low for b in older_half)  / len(older_half)
            if older_atr > 0 and recent_atr < older_atr * 0.85:  # 15%+ compression
                atr_declining = True
                signals_fired.append(
                    f"ATR declining: older={older_atr:.1f}pt → recent={recent_atr:.1f}pt "
                    f"({100*(1-recent_atr/older_atr):.0f}% compression deepening)"
                )

        # ─────────────────────────────────────────────────────────────────────
        # STEP 5: Require at least one directional signal (A, B, or C).
        #         Signal D alone is not sufficient — it's a quality multiplier.
        # ─────────────────────────────────────────────────────────────────────
        directional_fired = bull_vote > 0 or bear_vote > 0
        if not directional_fired:
            reason = "no directional signals (no VRR/exhaustion/close-breakout)"
            if signals_fired:
                reason = f"only non-directional signal(s): {'; '.join(signals_fired)}"
            self._last_reject = (
                f"NO SIGNAL: coil={n_coil} bars but {reason} | "
                f"regime={regime}"
            )
            return None

        # ─────────────────────────────────────────────────────────────────────
        # STEP 6: Direction — blend signal votes with TF votes.
        #
        #   Signal votes:  exhaustion=3, VRR=2, close-breakout=1
        #   TF votes:      added directly to bull/bear score
        #
        # Rules:
        #   - Need net advantage of at least 1 in the winning direction
        #   - Exhaustion signal can carry a trade with only 1 TF vote (high quality)
        #   - All other signals require min_tf_votes for confirmation
        # ─────────────────────────────────────────────────────────────────────
        tf_bullish = market.get("tf_votes_bullish", 0)
        tf_bearish = market.get("tf_votes_bearish", 0)

        total_bull = bull_vote + tf_bullish
        total_bear = bear_vote + tf_bearish

        exhaustion_long  = any("Exhaustion LONG"  in s for s in signals_fired)
        exhaustion_short = any("Exhaustion SHORT" in s for s in signals_fired)

        # Exhaustion gets a reduced TF votes requirement (it's already a strong signal)
        exhaustion_tf_min = max(1, min_tf_votes - 1)

        if total_bull > total_bear:
            if exhaustion_long and tf_bullish >= exhaustion_tf_min:
                direction = "LONG"
                votes = tf_bullish
            elif tf_bullish >= min_tf_votes:
                direction = "LONG"
                votes = tf_bullish
            else:
                self._last_reject = (
                    f"DIRECTION: LONG signals (score={total_bull}) but "
                    f"tf_bullish={tf_bullish} < required={min_tf_votes} "
                    f"(exhaustion={'yes' if exhaustion_long else 'no'}, "
                    f"exhaustion_min={exhaustion_tf_min})"
                )
                return None
        elif total_bear > total_bull:
            if exhaustion_short and tf_bearish >= exhaustion_tf_min:
                direction = "SHORT"
                votes = tf_bearish
            elif tf_bearish >= min_tf_votes:
                direction = "SHORT"
                votes = tf_bearish
            else:
                self._last_reject = (
                    f"DIRECTION: SHORT signals (score={total_bear}) but "
                    f"tf_bearish={tf_bearish} < required={min_tf_votes} "
                    f"(exhaustion={'yes' if exhaustion_short else 'no'}, "
                    f"exhaustion_min={exhaustion_tf_min})"
                )
                return None
        else:
            # Tied — conflicting signals, skip
            self._last_reject = (
                f"DIRECTION: tied (bull_score={total_bull} bear_score={total_bear}) "
                f"— conflicting signals, skipping"
            )
            return None

        # ─────────────────────────────────────────────────────────────────────
        # STEP 7: VWAP sanity check
        # Allow tolerance: price may be right at VWAP during coil accumulation.
        # ─────────────────────────────────────────────────────────────────────
        price = market.get("price", 0)
        vwap  = market.get("vwap", 0)

        if vwap > 0:
            vwap_tolerance = baseline_atr * 0.5
            if direction == "LONG" and price < vwap - vwap_tolerance:
                self._last_reject = (
                    f"VWAP: LONG but price {price:.2f} below VWAP {vwap:.2f} "
                    f"by more than {vwap_tolerance:.1f}pt"
                )
                return None
            elif direction == "SHORT" and price > vwap + vwap_tolerance:
                self._last_reject = (
                    f"VWAP: SHORT but price {price:.2f} above VWAP {vwap:.2f} "
                    f"by more than {vwap_tolerance:.1f}pt"
                )
                return None

        # ─────────────────────────────────────────────────────────────────────
        # STEP 8: Stop from coil structural boundary
        #
        # This is the PRE-explosion advantage: the stop sits at the coil edge,
        # which is naturally close because the coil IS the tight range.
        # Post-explosion stops must clear the entire coil + explosion bar.
        # ─────────────────────────────────────────────────────────────────────
        tick_size   = 0.25
        stop_buffer = stop_buffer_ticks * tick_size

        if direction == "LONG":
            stop_price    = coil_low - stop_buffer
            stop_distance = price - stop_price
        else:
            stop_price    = coil_high + stop_buffer
            stop_distance = stop_price - price

        # Floor: at least 0.5x baseline ATR of stop room (min viable stop)
        stop_distance = max(stop_distance, baseline_atr * 0.5)
        stop_ticks    = max(4, int(stop_distance / tick_size))

        # Cap for risk management.
        # Coil boundaries are usually tight (8-20t pre-explosion).
        # Cap protects against edge cases where price drifted from entry.
        max_stop_ticks = self.config.get("max_stop_ticks", 40)
        stop_ticks     = min(stop_ticks, max_stop_ticks)

        # ─────────────────────────────────────────────────────────────────────
        # STEP 9: Confidence score
        #
        # Base: 45 (lower than post-explosion because we're predicting, not
        #           confirming — but signals + stop tightness compensate)
        # ─────────────────────────────────────────────────────────────────────
        confidence = 45

        # Coil depth and length
        n_dir_signals = len([s for s in [
            any("VRR"        in s for s in signals_fired),
            any("Exhaustion" in s for s in signals_fired),
            any("close"      in s for s in signals_fired),
        ] if s])

        confidence += min(15, n_coil * 2)          # More coil bars = more loaded spring
        confidence += min(20, n_dir_signals * 8)   # Signal count (max 3 × 8 = 24, capped 20)
        confidence += min(10, votes * 2)            # TF alignment

        if vwap > 0:
            if (direction == "LONG" and price > vwap) or (direction == "SHORT" and price < vwap):
                confidence += 10   # VWAP confirms direction

        if exhaustion_long or exhaustion_short:
            confidence += 5   # Exhaustion is historically the strongest signal

        if atr_declining:
            confidence += 5   # Deep compression = more energy stored

        if coil_depth_pct < 60:
            confidence += 10
            coil_label = "DEEP"
        elif coil_depth_pct < 80:
            confidence += 5
            coil_label = "MODERATE"
        else:
            coil_label = "SHALLOW"

        confidence = min(100, confidence)

        # ── Build confluences list ────────────────────────────────────────────
        coil_range_str = f"[{coil_low:.2f} – {coil_high:.2f}]"
        confluences = [
            f"PRE-EXPLOSION ENTRY — entering inside the coil, before explosion fires",
            f"Regime: {regime}",
            f"Coil: {n_coil} bars ({coil_label}, {coil_depth_pct:.0f}% of baseline ATR)",
            f"Baseline ATR: {baseline_atr:.1f}pt | Tight threshold: {tight_threshold:.1f}pt",
            f"Coil zone: {coil_range_str}",
            f"Stop: {stop_price:.2f} ({stop_ticks}t, coil {'low' if direction == 'LONG' else 'high'} + {stop_buffer_ticks}t buffer)",
            f"TF votes: {votes}/4 {'bull' if direction == 'LONG' else 'bear'}",
        ] + signals_fired

        if vwap > 0:
            vwap_pos = "above" if price > vwap else "at/below"
            confluences.append(f"Price {vwap_pos} VWAP ({price:.2f} vs {vwap:.2f})")

        signal_names = []
        if any("VRR"        in s for s in signals_fired): signal_names.append("VRR-absorption")
        if any("Exhaustion" in s for s in signals_fired): signal_names.append("exhaustion-turn")
        if any("close"      in s for s in signals_fired): signal_names.append("close-breakout")
        if atr_declining:                                  signal_names.append("ATR-declining")

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=confidence,
            entry_score=min(60, int(confidence * 0.75)),
            strategy=self.name,
            reason=(
                f"Pre-Explosion {direction} — "
                f"{n_coil}-bar {coil_label} coil, "
                f"{', '.join(signal_names)}, "
                f"stop={stop_ticks}t at coil boundary, "
                f"regime={regime}"
            ),
            confluences=confluences,
        )
