"""
Phoenix Bot — Bias Momentum Follow Strategy

Port from V3 BiasMomentumFollow. Trades in the direction of multi-TF
bias when momentum confirms. Baseline validated strategy.

REGIME-AWARE: Loosens gates in golden windows (OPEN_MOMENTUM, MID_MORNING)
to maximize signal generation when edge is highest.
"""

import logging

from strategies.base_strategy import BaseStrategy, Signal
from core.candlestick_patterns import CandlestickAnalyzer, get_pattern_confluence

logger = logging.getLogger(__name__)

# Regime-specific overrides — BE AGGRESSIVE in golden windows
# Non-golden regimes use strategy config defaults (tighter gates)
_REGIME_OVERRIDES = {
    # Direction gate: 15m + 5m + 1m must ALL align (hardcoded in evaluate(), not here).
    # These overrides control momentum strength and confluence threshold per regime.
    # Goal: 2-5 signals/day on genuine trending days. Zero on choppy days.
    "OPEN_MOMENTUM": {"min_momentum": 80, "min_confluence": 5.5},
    "MID_MORNING":   {"min_momentum": 80, "min_confluence": 5.5},
    # Secondary windows
    "LATE_AFTERNOON": {"min_momentum": 75, "min_confluence": 5.0},
    # Chop zones — keep thresholds high
    "AFTERNOON_CHOP":   {"min_momentum": 80, "min_confluence": 5.5},
    "CLOSE_CHOP":       {"min_momentum": 80, "min_confluence": 5.5},
    # Off-hours — slightly looser for lab data collection
    "OVERNIGHT_RANGE": {"min_momentum": 60, "min_confluence": 4.0},
    "AFTERHOURS":      {"min_momentum": 60, "min_confluence": 4.0},
    "PREMARKET_DRIFT":  {"min_momentum": 60, "min_confluence": 4.0},
}


class BiasMomentumFollow(BaseStrategy):
    name = "bias_momentum"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        # Get regime and apply overrides for golden windows
        regime = session_info.get("regime", "UNKNOWN")
        # skip_regime_overrides: lab bot sets this to bypass hardcoded regime gates
        if self.config.get("skip_regime_overrides", False):
            overrides = {}
        else:
            overrides = _REGIME_OVERRIDES.get(regime, {})

        min_confluence = overrides.get("min_confluence", self.config.get("min_confluence", 5.5))
        min_momentum = overrides.get("min_momentum", self.config.get("min_momentum", 80))
        # B14: NQ-calibrated ATR stop params (replaces fixed stop_ticks). Stop is
        # computed at the end, after direction is known. Regime overrides (if any
        # are added later) can still tighten/loosen this — ATR is the base floor.
        target_rr = self.config.get("target_rr", 5.0)
        # 2026-04-25 §4.1: advisor-guided RR tier override. When market_advisor
        # has classified the regime (TRENDING / CHOPPY / OVEREXTENDED), use its
        # suggested_rr_tier instead of the static config value. Choppy = 2:1,
        # trending = 3:1, overextended = 1.5:1 (per Jennifer's policy). Falls
        # back to config target_rr if advisor unavailable.
        _adv = market.get("advisor_guidance") or {}
        _adv_rr = _adv.get("suggested_rr_tier")
        if _adv_rr and float(_adv_rr) > 0:
            _orig_rr = target_rr
            target_rr = float(_adv_rr)
            if abs(target_rr - _orig_rr) >= 0.5:
                logger.debug(
                    f"[EVAL] {self.name}: advisor RR override "
                    f"{_orig_rr:.1f} -> {target_rr:.1f} "
                    f"(regime={_adv.get('market_regime')})"
                )

        # Minimal warmup — only need 1 bar to have data, use what we have
        if len(bars_1m) < 1:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        # ── Direction: Multi-TF EMA vote ──────────────────────────────
        # Direction is determined by EMA-based timeframe votes only.
        # Session CVD is NOT used for direction — it can stay negative all day
        # even on strong bullish trending days (e.g. if opening volatile selling
        # accumulated a large negative CVD that the rally doesn't fully reverse).
        # CVD is used as a MOMENTUM SCORING factor below, not direction.

        tf_bias = market.get("tf_bias", {})
        cvd = market.get("cvd", 0)
        bar_delta = market.get("bar_delta", 0)
        # Use the live snapshot price first. `market["close"]` is not a
        # guaranteed field on the runtime snapshot, and falling back to 0.0
        # turns the EMA/VWAP gates into false rejects.
        price = market.get("price", 0.0) or market.get("close", 0.0)
        vwap = market.get("vwap", 0.0)

        # ── Direction — EMA Stack + Explosive Bypass ────────────────────────
        #
        # TFs tell us HOW confident to be, not WHETHER to trade.
        # The direction gate depends on day type:
        #
        # TREND days (high conviction, Q-Score 5, MQ LONG/SHORT):
        #   Direction = MQ bias → if LONG day, we only go LONG, period.
        #   If MQ bias NEUTRAL, fall back to 1m bar direction.
        #   TF votes add +15 pts each to momentum score (bonus, not gate).
        #   Rationale: on a TREND day the context IS the signal. We want early
        #   entries, not late confirmation after the move has already run.
        #
        # Non-TREND days (RANGE/VOLATILE/UNKNOWN):
        #   Need at least 2/4 TFs agreeing. Prevents fighting random chop.
        #   Still lighter than the old all-3-must-agree requirement.

        # ── Direction Gate — 3-layer EMA Stack system ───────────────
        #
        # OLD system: binary "2 of 3 closes rising" on 1m/5m/15m/60m.
        #   Problem: winners and losers had identical TF vote counts (1.4 vs 1.5/4).
        #   The 60m is 12:1 ratio to the 5m — it confirms 30-60 min AFTER the move.
        #
        # NEW system: EMA stack + explosive bypass:
        #   Layer A — 5m EMA stack: structural trend (EMA9 > EMA21 on 5m)
        #   Layer B — VWAP side: session bias (price above/below VWAP)
        #   Explosive bypass: when vol surge + strong delta + extreme close all fire,
        #     drop Layer B requirement to catch breakouts before VWAP confirms.
        #   15m EMA stack: context bonus (+20 pts) — replaces 60m, NOT a hard gate.
        #   60m tf_bias: kept as small bonus only — NEVER a blocking gate.
        #
        # TREND days: MQ/CR context still sets direction (unchanged).
        # Non-TREND days: use EMA stack instead of TF vote count.

        ema9    = market.get("ema9",    0.0)
        ema21   = market.get("ema21",   0.0)
        ema9_15 = market.get("ema9_15m",  0.0)
        ema21_15= market.get("ema21_15m", 0.0)
        bias_1m = tf_bias.get("1m",  "NEUTRAL")
        bias_5m = tf_bias.get("5m",  "NEUTRAL")
        bias_60m= tf_bias.get("60m", "NEUTRAL")  # display/context only, NOT a gate

        day_type   = market.get("day_type", "UNKNOWN")
        mq_bias    = market.get("mq_direction_bias", "NEUTRAL")
        trend_day  = (day_type == "TREND")
        cr_verdict = market.get("cr_verdict", "UNKNOWN")

        # 5m EMA stack — the primary structural signal
        ema_stack_long  = (ema9 > 0 and ema21 > 0 and ema9 > ema21)
        ema_stack_short = (ema9 > 0 and ema21 > 0 and ema9 < ema21)

        # Explosive bypass: high-conviction breakout bar → enter before VWAP confirms
        # 2026-04-24 Jennifer: lowered thresholds (VCR 1.5→1.2, close-pos 0.75/0.25→0.65/0.35)
        # so the bypass triggers in normal-vol bars, not just true climaxes. The 99%
        # rejection rate on VWAP_GATE was driven by VCR almost never clearing 1.5x.
        # Caller-supplied config knobs let us re-tighten without redeploying code.
        _vcr_threshold = float(self.config.get("vcr_threshold", 1.2))
        _close_pos_long = float(self.config.get("explosive_close_pos_long", 0.65))
        _close_pos_short = float(self.config.get("explosive_close_pos_short", 0.35))
        _avg_vol    = float(market.get("avg_vol_5m", 0.0) or 0.0)
        _bar_delta  = float(market.get("bar_delta", 0.0) or 0.0)
        _vcr        = float(market.get("vol_climax_ratio", 1.0) or 1.0)
        _bar_range  = 0.0
        if bars_5m:
            _last5 = bars_5m[-1]
            _bar_range = _last5.high - _last5.low
            _close_pos5 = ((_last5.close - _last5.low) / _bar_range) if _bar_range > 0 else 0.5
        else:
            _close_pos5 = 0.5
        # Explosive long: vol elevated + buying delta + close in upper part of bar
        explosive_long  = (_vcr >= _vcr_threshold and _bar_delta > 0
                           and _close_pos5 >= _close_pos_long and _avg_vol > 0)
        # Explosive short: vol elevated + selling delta + close in lower part of bar
        explosive_short = (_vcr >= _vcr_threshold and _bar_delta < 0
                           and _close_pos5 <= _close_pos_short and _avg_vol > 0)

        if trend_day:
            # TREND mode: direction from MQ context (unchanged)
            if mq_bias == "LONG":
                direction = "LONG"
            elif mq_bias == "SHORT":
                direction = "SHORT"
            else:
                direction = "LONG" if bias_1m == "BULLISH" else ("SHORT" if bias_1m == "BEARISH" else None)
            if direction is None:
                self._last_reject = f"TREND day: no 1m direction (1m={bias_1m}, MQ={mq_bias})"
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:trend_day_no_direction")
                return None
        else:
            # Non-TREND: EMA stack gate (Layer A) + VWAP side (Layer B)
            # Explosive bypass skips Layer B when breakout fingerprint fires.
            vwap = market.get("vwap", 0.0)
            price_above_vwap = (price > vwap and vwap > 0)
            price_below_vwap = (price < vwap and vwap > 0)

            # Determine direction from EMA stack (primary) or explosive signal
            if ema_stack_long or explosive_long:
                direction = "LONG"
            elif ema_stack_short or explosive_short:
                direction = "SHORT"
            else:
                self._last_reject = (
                    f"EMA_STACK: 5m EMA9={ema9:.1f} EMA21={ema21:.1f} "
                    f"(no stack — need EMA9{'>'if ema9>0 else '<'}EMA21 or explosive bar)")
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:ema_stack")
                return None

            # Layer B: VWAP side check (unless explosive bypass active)
            _explosive_active = (explosive_long if direction == "LONG" else explosive_short)
            if not _explosive_active:
                if direction == "LONG" and not price_above_vwap and vwap > 0:
                    self._last_reject = (
                        f"VWAP_GATE: price {price:.2f} below VWAP {vwap:.2f} — "
                        f"no explosive bypass active (VCR={_vcr:.1f}, delta={_bar_delta:+.0f})")
                    logger.debug(f"[EVAL] {self.name}: BLOCKED gate:vwap_long")
                    return None
                elif direction == "SHORT" and not price_below_vwap and vwap > 0:
                    self._last_reject = (
                        f"VWAP_GATE: price {price:.2f} above VWAP {vwap:.2f} — "
                        f"no explosive bypass active (VCR={_vcr:.1f}, delta={_bar_delta:+.0f})")
                    logger.debug(f"[EVAL] {self.name}: BLOCKED gate:vwap_short")
                    return None

        # ── CVD Gate — afternoon/chop regimes (HARD BLOCK) ──────────────────
        # Direction is now known. Check institutional flow in chop windows.
        # CVD = session cumulative delta. On 2026-04-15 CVD was -92M all afternoon
        # while price showed micro-bullish signals → distribution. 43/43 entries lost.
        #
        # NOTE: 60m tf_bias unreliable intraday (NEUTRAL even on bullish days).
        # CVD is the correct session-level directional filter.
        _chop_regimes_cvd = ("LATE_AFTERNOON", "CLOSE_CHOP", "AFTERNOON_CHOP")
        if regime in _chop_regimes_cvd:
            if direction == "LONG" and cvd <= 0:
                self._last_reject = (
                    f"BIAS_MOM: CVD={cvd/1e6:.1f}M (net selling) in {regime} — "
                    f"institutional flow opposes LONG. Wait for CVD to turn positive.")
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:cvd_chop_long")
                return None
            elif direction == "SHORT" and cvd >= 0:
                self._last_reject = (
                    f"BIAS_MOM: CVD=+{cvd/1e6:.1f}M (net buying) in {regime} — "
                    f"institutional flow opposes SHORT. Wait for CVD to turn negative.")
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:cvd_chop_short")
                return None

        # ── Momentum scoring ─────────────────────────────────────────
        momentum_score = 0
        confluences = [f"Regime: {regime}"]
        if trend_day:
            confluences.append(f"TREND day (MQ={mq_bias}, C/R={cr_verdict})")

        # ── EMA Stack scoring ────────────────────────────────────────
        # 5m stack (structural — already passed the gate, now score its quality)
        if direction == "LONG":
            if ema9 > 0 and price > ema9 > ema21:
                momentum_score += 25  # Full stack: price > EMA9 > EMA21
                confluences.append(f"5m full stack: price > EMA9({ema9:.0f}) > EMA21({ema21:.0f})")
            elif ema_stack_long:
                momentum_score += 15  # EMA9 > EMA21 but price below EMA9 (pullback in trend)
                confluences.append(f"5m EMA9({ema9:.0f}) > EMA21({ema21:.0f}) — pullback entry")
        else:
            if ema9 > 0 and price < ema9 < ema21:
                momentum_score += 25
                confluences.append(f"5m full stack: price < EMA9({ema9:.0f}) < EMA21({ema21:.0f})")
            elif ema_stack_short:
                momentum_score += 15
                confluences.append(f"5m EMA9({ema9:.0f}) < EMA21({ema21:.0f}) — pullback entry")

        # 15m EMA stack — context timeframe (3:1 to 5m), bonus not gate
        if ema9_15 > 0 and ema21_15 > 0:
            if direction == "LONG" and ema9_15 > ema21_15:
                _ctx = "full" if price > ema9_15 else "partial"
                momentum_score += 20
                confluences.append(f"15m EMA stack {_ctx} ({ema9_15:.0f} > {ema21_15:.0f}) — context bullish")
            elif direction == "SHORT" and ema9_15 < ema21_15:
                _ctx = "full" if price < ema9_15 else "partial"
                momentum_score += 20
                confluences.append(f"15m EMA stack {_ctx} ({ema9_15:.0f} < {ema21_15:.0f}) — context bearish")
            elif direction == "LONG" and ema9_15 < ema21_15:
                momentum_score -= 10
                confluences.append(f"15m EMA bearish context ({ema9_15:.0f} < {ema21_15:.0f}) — headwind")
            elif direction == "SHORT" and ema9_15 > ema21_15:
                momentum_score -= 10
                confluences.append(f"15m EMA bullish context ({ema9_15:.0f} > {ema21_15:.0f}) — headwind")

        # 60m bias — small bonus for context alignment, never a penalty large enough to block
        if direction == "LONG" and bias_60m == "BULLISH":
            momentum_score += 8
            confluences.append("60m context bullish (+8)")
        elif direction == "SHORT" and bias_60m == "BEARISH":
            momentum_score += 8
            confluences.append("60m context bearish (+8)")

        # Explosive bypass: flag in confluences and add scoring bonus
        if _explosive_active if not trend_day else False:
            momentum_score += 15
            confluences.append(f"EXPLOSIVE BAR: VCR={_vcr:.1f}x, delta={_bar_delta:+.0f}, close@{_close_pos5:.0%} — bypass active")

        # Price vs VWAP (already used as gate on non-TREND days, now score the quality)
        if direction == "LONG" and price > vwap and vwap > 0:
            momentum_score += 20
            confluences.append("Price above VWAP")
        elif direction == "SHORT" and price < vwap and vwap > 0:
            momentum_score += 20
            confluences.append("Price below VWAP")

        # CVD and bar_delta confirmation.
        # IMPORTANT: Session CVD (cumulative) can stay negative all day even on
        # strong bullish days if the open had heavy selling. Do NOT use session
        # CVD sign for direction — only for a small momentum bonus/penalty.
        # bar_delta (current bar only) is more reliable as a real-time signal.
        #
        # Session CVD: small bonus/penalty (+10/-5) — directional hint only
        if direction == "LONG" and cvd > 0:
            momentum_score += 10
            confluences.append("CVD positive")
        elif direction == "LONG" and cvd < 0:
            momentum_score -= 5   # Small penalty — session sold more, but not a blocker
            confluences.append(f"CVD net bearish (session sold heavy)")
        elif direction == "SHORT" and cvd < 0:
            momentum_score += 10
            confluences.append("CVD negative")
        elif direction == "SHORT" and cvd > 0:
            momentum_score -= 5
            confluences.append(f"CVD net bullish (session bought heavy)")

        # bar_delta (current bar): stronger real-time signal (+15/-10)
        if direction == "LONG" and bar_delta > 0:
            momentum_score += 15
            confluences.append(f"Bar delta bullish ({bar_delta:+,.0f})")
        elif direction == "LONG" and bar_delta < 0:
            momentum_score -= 10
            confluences.append(f"Bar delta bearish ({bar_delta:+,.0f})")
        elif direction == "SHORT" and bar_delta < 0:
            momentum_score += 15
            confluences.append(f"Bar delta bearish ({bar_delta:+,.0f})")
        elif direction == "SHORT" and bar_delta > 0:
            momentum_score -= 10
            confluences.append(f"Bar delta bullish ({bar_delta:+,.0f})")

        # ── MACD Histogram (5m) ──────────────────────────────────────────
        # The histogram (MACD_line - signal_line) measures acceleration of the
        # EMA9-EMA21 spread. Positive + growing = trend accelerating. Positive
        # but shrinking = trend decelerating (momentum peak likely near).
        # This catches what bar_delta and CVD miss: the multi-bar momentum arc.
        # Only score when MACD is warm (9+ signal bars = ~30+ five-minute bars).
        macd_hist      = market.get("macd_histogram", 0.0) or 0.0
        macd_hist_prev = market.get("macd_histogram_prev", 0.0) or 0.0
        macd_warm      = market.get("macd_warm", False)
        if macd_warm:
            if direction == "LONG":
                if macd_hist > 0 and macd_hist >= macd_hist_prev:
                    momentum_score += 12
                    confluences.append(f"MACD hist bullish+expanding ({macd_hist:.3f})")
                elif macd_hist > 0 and macd_hist < macd_hist_prev:
                    momentum_score -= 8
                    confluences.append(f"MACD hist bullish but shrinking ({macd_hist:.3f}←{macd_hist_prev:.3f})")
                elif macd_hist < 0:
                    momentum_score -= 15
                    confluences.append(f"MACD hist NEGATIVE ({macd_hist:.3f}) — bearish pressure on LONG")
            else:  # SHORT
                if macd_hist < 0 and macd_hist <= macd_hist_prev:
                    momentum_score += 12
                    confluences.append(f"MACD hist bearish+expanding ({macd_hist:.3f})")
                elif macd_hist < 0 and macd_hist > macd_hist_prev:
                    momentum_score -= 8
                    confluences.append(f"MACD hist bearish but shrinking ({macd_hist:.3f}←{macd_hist_prev:.3f})")
                elif macd_hist > 0:
                    momentum_score -= 15
                    confluences.append(f"MACD hist POSITIVE ({macd_hist:.3f}) — bullish pressure on SHORT")

        # ── DOM Directional Check ────────────────────────────────────────
        # DOM imbalance shows the live order book: bid/(bid+ask).
        # >0.60 = buyers stacking bids (bullish), <0.40 = sellers stacking asks (bearish).
        # Not a hard gate — noise in individual ticks — but a meaningful scoring factor.
        # dom_signal.direction tracks absorption events (smart money entering passively).
        dom_imbal  = float(market.get("dom_imbalance", 0.5) or 0.5)
        dom_signal = market.get("dom_signal", {}) or {}
        dom_dir    = dom_signal.get("direction") if isinstance(dom_signal, dict) else None
        dom_str    = float(dom_signal.get("strength", 0) or 0) if isinstance(dom_signal, dict) else 0
        if direction == "LONG":
            if dom_imbal > 0.60:
                momentum_score += 10
                confluences.append(f"DOM bid-heavy ({dom_imbal:.2f}) — buyers stacking")
            elif dom_imbal < 0.40:
                momentum_score -= 12
                confluences.append(f"DOM ask-heavy ({dom_imbal:.2f}) — sellers stacking vs LONG")
            if dom_dir == "LONG" and dom_str >= 40:
                momentum_score += 15
                confluences.append(f"DOM absorption LONG (str={dom_str:.0f}) — smart money buying")
            elif dom_dir == "SHORT" and dom_str >= 40:
                momentum_score -= 10
                confluences.append(f"DOM absorption SHORT (str={dom_str:.0f}) — smart money selling")
        else:  # SHORT
            if dom_imbal < 0.40:
                momentum_score += 10
                confluences.append(f"DOM ask-heavy ({dom_imbal:.2f}) — sellers stacking")
            elif dom_imbal > 0.60:
                momentum_score -= 12
                confluences.append(f"DOM bid-heavy ({dom_imbal:.2f}) — buyers stacking vs SHORT")
            if dom_dir == "SHORT" and dom_str >= 40:
                momentum_score += 15
                confluences.append(f"DOM absorption SHORT (str={dom_str:.0f}) — smart money selling")
            elif dom_dir == "LONG" and dom_str >= 40:
                momentum_score -= 10
                confluences.append(f"DOM absorption LONG (str={dom_str:.0f}) — smart money buying")

        # ── Volume Analysis ──────────────────────────────────────────────
        # Volume Climax: last 5m bar was 2.5x+ average → exhaustion risk on momentum entries
        # Volume Dry-Up: pullback bars are low-volume → sellers absent → safe entry
        # Delta Divergence: price making new highs but delta declining → distribution
        # VSA Absorption: wide range, high volume, close in middle → reversal warning
        avg_vol       = float(market.get("avg_vol_5m", 0.0) or 0.0)
        climax_ratio  = float(market.get("vol_climax_ratio", 1.0) or 1.0)
        vsa           = market.get("vsa_signal_5m", "NEUTRAL") or "NEUTRAL"
        delta_hist    = market.get("delta_history_5m", []) or []
        high_hist     = market.get("high_history_5m", []) or []
        low_hist      = market.get("low_history_5m", []) or []

        if avg_vol > 0:
            # Volume climax — spike bar at extension = exhaustion, not momentum
            if climax_ratio >= 2.5 and regime not in ("OPEN_MOMENTUM",):
                momentum_score -= 20
                confluences.append(f"VOL CLIMAX {climax_ratio:.1f}x avg — exhaustion risk")
            elif climax_ratio >= 1.8:
                confluences.append(f"Vol elevated {climax_ratio:.1f}x avg")

            # Volume dry-up on pullback — low-vol counter-move = sellers absent, safe entry
            if len(bars_5m) >= 3:
                _recent3 = list(bars_5m)[-3:]
                _dryup = sum(
                    1 for b in _recent3
                    if b.volume < avg_vol * 0.55 and (
                        (direction == "LONG"  and b.close < b.open) or
                        (direction == "SHORT" and b.close > b.open)
                    )
                )
                if _dryup >= 2 and regime in ("OPEN_MOMENTUM", "MID_MORNING"):
                    momentum_score += 15
                    confluences.append(f"Vol dry-up pullback ({_dryup}/3 bars low-vol) — sellers absent")
                elif _dryup >= 2:
                    momentum_score += 8
                    confluences.append(f"Vol dry-up pullback ({_dryup}/3 bars)")

        # VSA absorption — high-volume wide-bar closes mid-range = supply absorbing demand
        if vsa == "ABSORPTION":
            momentum_score -= 20
            confluences.append("VSA ABSORPTION: high-vol wide-bar mid-close — reversal warning")
        elif vsa == "EFFORT_UP" and direction == "LONG":
            momentum_score += 8
            confluences.append("VSA effort bar up — buyers in control")
        elif vsa == "EFFORT_DOWN" and direction == "SHORT":
            momentum_score += 8
            confluences.append("VSA effort bar down — sellers in control")
        elif vsa in ("TEST_UP",) and direction == "LONG":
            momentum_score += 10
            confluences.append("VSA low-vol test — buyers defended level")
        elif vsa in ("TEST_DOWN",) and direction == "SHORT":
            momentum_score += 10
            confluences.append("VSA low-vol test — sellers defended level")

        # Delta divergence — price HH but delta declining = distribution top
        if len(delta_hist) >= 3 and len(high_hist) >= 3:
            _n = 3
            _dh = delta_hist[-_n:]
            _hh = high_hist[-_n:]
            _ll = low_hist[-_n:] if len(low_hist) >= 3 else []
            # Bearish: price HH but delta falling 35%+
            _price_hh = all(_hh[i] <= _hh[i+1] for i in range(_n-1))
            _delta_down = _dh[-1] < _dh[0] * 0.65 and _dh[0] != 0
            if direction == "LONG" and _price_hh and _delta_down and _dh[0] > 0:
                momentum_score -= 20
                confluences.append(f"DELTA DIV: price HH but delta fading ({_dh[0]:+.0f}→{_dh[-1]:+.0f}) — distribution")
            # Bullish: price LL but delta recovering (accumulation under the lows)
            if _ll:
                _price_ll = all(_ll[i] >= _ll[i+1] for i in range(_n-1))
                _delta_up = _dh[-1] > _dh[0] * 0.65 and _dh[0] < 0
                if direction == "SHORT" and _price_ll and _delta_up and _dh[0] < 0:
                    momentum_score -= 20
                    confluences.append(f"DELTA DIV: price LL but delta recovering ({_dh[0]:+.0f}→{_dh[-1]:+.0f}) — accumulation")
            # Confirmatory: delta expanding in trade direction
            if direction == "LONG" and _price_hh and len(_dh) >= 2 and _dh[-1] > _dh[0] and _dh[-1] > 0:
                momentum_score += 10
                confluences.append(f"Delta expanding with price ({_dh[0]:+.0f}→{_dh[-1]:+.0f}) — momentum confirmed")
            elif direction == "SHORT" and len(_ll) >= 2 and all(_ll[i] >= _ll[i+1] for i in range(_n-1)) and _dh[-1] < _dh[0] and _dh[-1] < 0:
                momentum_score += 10
                confluences.append(f"Delta declining with price ({_dh[0]:+.0f}→{_dh[-1]:+.0f}) — momentum confirmed")

        # ── VWAP Band Location ────────────────────────────────────────────
        # Don't chase entries that are already at statistical extremes (±2σ).
        # The 1σ band is the ideal pullback reload zone on trend days.
        vwap_std    = float(market.get("vwap_std", 0.0) or 0.0)
        vwap_upper2 = float(market.get("vwap_upper2", 0.0) or 0.0)
        vwap_lower2 = float(market.get("vwap_lower2", 0.0) or 0.0)
        vwap_upper1 = float(market.get("vwap_upper1", 0.0) or 0.0)
        vwap_lower1 = float(market.get("vwap_lower1", 0.0) or 0.0)
        avwap_pdc   = float(market.get("avwap_pd_close", 0.0) or 0.0)
        if vwap_std > 0 and price > 0:
            if direction == "LONG":
                if vwap_upper2 > 0 and price >= vwap_upper2:
                    momentum_score -= 15
                    confluences.append(f"Above VWAP+2σ ({vwap_upper2:.1f}) — extended, chasing")
                elif vwap_upper1 > 0 and price >= vwap_upper1:
                    momentum_score -= 8
                    confluences.append(f"Above VWAP+1σ ({vwap_upper1:.1f}) — slightly extended")
                elif vwap_lower1 > 0 and price <= vwap_lower1:
                    momentum_score += 10
                    confluences.append(f"At/below VWAP-1σ ({vwap_lower1:.1f}) — pullback reload zone")
            else:  # SHORT
                if vwap_lower2 > 0 and price <= vwap_lower2:
                    momentum_score -= 15
                    confluences.append(f"Below VWAP-2σ ({vwap_lower2:.1f}) — extended, chasing short")
                elif vwap_lower1 > 0 and price <= vwap_lower1:
                    momentum_score -= 8
                    confluences.append(f"Below VWAP-1σ ({vwap_lower1:.1f}) — slightly extended short")
                elif vwap_upper1 > 0 and price >= vwap_upper1:
                    momentum_score += 10
                    confluences.append(f"At/above VWAP+1σ ({vwap_upper1:.1f}) — pullback reload zone for short")
        # Prior-day close AVWAP — institutional positioning bias
        if avwap_pdc > 0 and price > 0:
            if direction == "LONG" and price < avwap_pdc:
                momentum_score -= 10
                confluences.append(f"Below prior-close AVWAP ({avwap_pdc:.1f}) — prior-session sellers in control")
            elif direction == "SHORT" and price > avwap_pdc:
                momentum_score -= 10
                confluences.append(f"Above prior-close AVWAP ({avwap_pdc:.1f}) — prior-session buyers in control")

        # ── Menthor Q Level Proximity ────────────────────────────────────
        # Warn when price is already near a key Q resistance (for LONG) or support
        # (for SHORT). Entering near a wall = less runway, higher reversal risk.
        # Applied as a scoring penalty only — not a hard gate.
        mq_resistance = market.get("mq_nearest_resistance", 0.0) or 0.0
        mq_support    = market.get("mq_nearest_support", 0.0) or 0.0
        mq_hvl        = market.get("mq_hvl", 0.0) or 0.0
        if price > 0:
            from config.settings import TICK_SIZE as _ts_mq
            if direction == "LONG" and mq_resistance > 0:
                dist_to_wall = (mq_resistance - price) / _ts_mq
                if dist_to_wall < 20:   # within 5 points of resistance wall
                    momentum_score -= 15
                    confluences.append(f"Near Q resistance {mq_resistance:.2f} ({dist_to_wall:.0f}t away) — low runway")
                elif dist_to_wall < 40:  # within 10 points
                    momentum_score -= 5
                    confluences.append(f"Approaching Q resistance {mq_resistance:.2f} ({dist_to_wall:.0f}t)")
            elif direction == "SHORT" and mq_support > 0:
                dist_to_wall = (price - mq_support) / _ts_mq
                if dist_to_wall < 20:
                    momentum_score -= 15
                    confluences.append(f"Near Q support {mq_support:.2f} ({dist_to_wall:.0f}t away) — low runway")
                elif dist_to_wall < 40:
                    momentum_score -= 5
                    confluences.append(f"Approaching Q support {mq_support:.2f} ({dist_to_wall:.0f}t)")
            # HVL awareness — the gamma flip level
            if mq_hvl > 0:
                dist_to_hvl = abs(price - mq_hvl) / _ts_mq
                if dist_to_hvl < 16:  # within 4 points of HVL
                    confluences.append(f"Near HVL {mq_hvl:.2f} ({dist_to_hvl:.0f}t) — expect volatility")

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

        # TREND days: threshold = 20 (just ONE basic signal — VWAP, EMA, or rising bars).
        # The day context IS the primary signal. Don't require full confirmation.
        # Non-TREND days: keep full threshold (80) to filter chop.
        effective_min_momentum = 20 if trend_day else min_momentum
        if momentum_score < effective_min_momentum:
            self._last_reject = (f"MOMENTUM: score={momentum_score} need={effective_min_momentum} "
                                 f"({'TREND ' if trend_day else ''}{', '.join(confluences[1:])})")
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL momentum_score={momentum_score}<{effective_min_momentum}")
            return None

        # ── EMA9 Extension Gate ────────────────────────────────────────
        # Prevent chasing when price is already far extended from EMA9.
        # OPEN_MOMENTUM / MID_MORNING: skip — prime momentum windows, entries fine even if extended.
        # All other regimes: if price > threshold from EMA9, the move has likely already peaked.
        #
        # Thresholds derived from 2,278 live 1m bars (5 days, Apr 11-15 2026):
        #   Extension run P75 = 67t — 75% of EMA9 extension runs reverse before this point.
        #   Below: regime P90 (blocks only the most extreme 10% of bars per regime).
        #   Using P90 per regime to be permissive for trend entries but block clear extremes.
        #
        #   LATE_AFTERNOON P90=161t  → gate=120t (conservative — known chop window)
        #   AFTERNOON_CHOP P90=167t  → handled by allowed_strategies whitelist, gate=120t
        #   OVERNIGHT_RANGE P90=81t  → gate=70t  (thin market, tighter)
        #   Others                   → gate= max_ema_dist_ticks config default (60t)
        #
        # Lab bot sets max_ema_dist_ticks=999 to disable (collect all data).
        _golden_regimes = ("OPEN_MOMENTUM", "MID_MORNING")
        _regime_gates = {
            "LATE_AFTERNOON":   120,   # P75=116t; 120t blocks top ~30% most extended
            "AFTERNOON_CHOP":   120,   # Also gated by allowed_strategies whitelist
            "OVERNIGHT_RANGE":   70,   # Thin market; P90=81t
            "AFTERHOURS":        70,
            "PREMARKET_DRIFT":   80,
        }
        _max_ema_dist = self.config.get("max_ema_dist_ticks",
                                        _regime_gates.get(regime, 100))
        if ema9 > 0 and regime not in _golden_regimes and _max_ema_dist > 0:
            from config.settings import TICK_SIZE as _ts
            _ema_dist = abs(price - ema9) / _ts
            if _ema_dist > _max_ema_dist:
                self._last_reject = (f"BIAS_MOM: Price {_ema_dist:.0f}t from EMA9 "
                                     f"(max {_max_ema_dist}t in {regime}) — chasing, wait for pullback")
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:ema9_extension")
                return None

        # ── Confluence score ────────────────────────────────────────
        # TREND days: skip confluence gate entirely. Direction comes from MQ bias (context),
        # not TF votes, so the votes+momentum formula is meaningless. The momentum threshold
        # (20pts = ONE signal) is sufficient — day context IS the primary confluence.
        votes = market.get("tf_votes_bullish" if direction == "LONG" else "tf_votes_bearish", 0)
        if not trend_day:
            confluence = votes + (momentum_score / 30)
            if confluence < min_confluence:
                self._last_reject = (f"CONFLUENCE: score={confluence:.1f} need={min_confluence} "
                                     f"votes={votes} momentum={momentum_score}")
                logger.debug(f"[EVAL] {self.name}: NO_SIGNAL confluence={confluence:.1f}<{min_confluence}")
                return None

        confluences.append(f"TF: {votes}/4 {'bull' if direction == 'LONG' else 'bear'}")
        confluences.append(f"Momentum: {momentum_score}")

        # B14: NQ-calibrated ATR-anchored stop (replaces fixed stop_ticks=20).
        from strategies._nq_stop import compute_atr_stop
        from config.settings import TICK_SIZE as _ts_stop
        atr_5m = market.get("atr_5m", 0) or 0
        last_5m = bars_5m[-1] if bars_5m else (bars_1m[-1] if bars_1m else None)
        stop_ticks, stop_price, atr_override, stop_note = compute_atr_stop(
            direction=direction,
            entry_price=price if price > 0 else market.get("price", 0),
            last_5m_bar=last_5m,
            atr_5m_points=atr_5m,
            tick_size=_ts_stop,
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
            confidence=momentum_score,
            entry_score=min(60, int(momentum_score * 0.75)),
            strategy=self.name,
            reason=f"Bias Momentum {direction} — {votes}/4 TF, score {momentum_score}, regime {regime}",
            confluences=confluences,
        )
        sig.atr_stop_override = atr_override
        if atr_override and stop_price is not None:
            sig.stop_price = stop_price
        return sig
