"""
Phoenix Bot — DOM Absorption Pullback Strategy

Replicates the user's manual trading pattern:

  Entry Setup (all required):
    1. Context: MenthorQ LONG bias (or 2/4 TF vote majority)
    2. Level: Price at EMA9 or VWAP — the pullback touches a key level
    3. Pullback quality: The pullback bars are QUIET (small body, weak volume)
       Not an aggressive reversal — just a clean, low-volume retest
    4. DOM absorption: Sell orders being PULLED or EATEN at the level
       The key signal — sellers can't push through, buyers absorbing
    5. Bounce confirmation: Current bar closes strongly (close near high)
       with volume >= pullback bar (buyers stepping in decisively)

  Exit: Managed by base_bot smart exit — EMA extension + DOM stalling + wick

User description:
  "I bought in on a pullback, I entered the trade when I confirmed momentum
   picking up, the sell orders on the depth of market started getting pulled /
   and eaten through. I knew my direction was bullish bc of the menthor q road
   map... so I enter. It took off."
"""

from strategies.base_strategy import BaseStrategy, Signal


class DOMPullback(BaseStrategy):
    name = "dom_pullback"

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        stop_ticks        = self.config.get("stop_ticks", 10)
        target_rr         = self.config.get("target_rr", 2.5)
        min_dom_str       = self.config.get("min_dom_strength", 40)
        max_ema_dist      = self.config.get("max_ema_dist_ticks", 28)   # Data P25 = 26-40t
        max_vwap_dist     = self.config.get("max_vwap_dist_ticks", 20)  # Data P25 = ~20t
        max_pb_body_ticks = self.config.get("max_pb_body_ticks", 10)   # pullback bar max body
        min_bounce_body   = self.config.get("min_bounce_body_ticks", 3) # bounce bar min body

        # Need at least 5 bars: 2+ trend bars, 1-2 pullback bars, 1 bounce bar
        if len(bars_1m) < 5:
            return None

        from config.settings import TICK_SIZE
        tick_size = TICK_SIZE

        price     = market.get("price", 0)
        vwap      = market.get("vwap", 0)
        ema9      = market.get("ema9", 0)
        ema21     = market.get("ema21", 0)
        bar_delta = market.get("bar_delta", 0)
        cvd       = market.get("cvd", 0)

        mq_bias   = market.get("mq_direction_bias", "NEUTRAL")
        day_type  = market.get("day_type", "UNKNOWN")
        trend_day = (day_type == "TREND")

        # DOM — use raw imbalance ratio only (boolean flags are unreliable)
        dom_signal   = market.get("dom_signal", {}) or {}
        dom_dir      = dom_signal.get("direction") if isinstance(dom_signal, dict) else None
        dom_strength = float(dom_signal.get("strength", 0)) if isinstance(dom_signal, dict) else 0
        dom_imbal    = float(market.get("dom_imbalance", 0.5) or 0.5)

        # ── Direction ──────────────────────────────────────────────────
        if mq_bias == "LONG":
            direction = "LONG"
        elif mq_bias == "SHORT":
            direction = "SHORT"
        else:
            bullish = market.get("tf_votes_bullish", 0)
            bearish = market.get("tf_votes_bearish", 0)
            if bullish >= 2:
                direction = "LONG"
            elif bearish >= 2:
                direction = "SHORT"
            else:
                self._last_reject = (f"DOM_PB: No direction "
                                     f"(MQ={mq_bias}, TF bull={bullish} bear={bearish})")
                return None

        confluences = []

        # ── Gate 1: Trend structure intact ────────────────────────────
        # LONG: ema9 > ema21 (short-term trend bullish), price above VWAP
        # The pullback happens WITHIN an uptrend — we're not catching a falling knife
        if direction == "LONG":
            if ema9 > 0 and ema21 > 0 and ema9 > ema21:
                confluences.append("EMA9 > EMA21 (trend intact)")
            elif ema9 > 0 and ema21 > 0:
                self._last_reject = "DOM_PB: EMA9 < EMA21 — trend not intact for LONG"
                return None
            if vwap > 0 and price < vwap:
                # Price below VWAP is OK only on strong TREND days (MQ LONG)
                if not (trend_day and mq_bias == "LONG"):
                    self._last_reject = f"DOM_PB: Price below VWAP ({price:.2f} < {vwap:.2f}) on non-TREND day"
                    return None
        elif direction == "SHORT":
            if ema9 > 0 and ema21 > 0 and ema9 < ema21:
                confluences.append("EMA9 < EMA21 (trend intact)")
            elif ema9 > 0 and ema21 > 0:
                self._last_reject = "DOM_PB: EMA9 > EMA21 — trend not intact for SHORT"
                return None

        # ── Gate 2: Price at pullback level ───────────────────────────
        near_ema9 = False
        near_vwap = False

        if ema9 > 0:
            ema9_dist = abs(price - ema9) / tick_size
            if ema9_dist <= max_ema_dist:
                near_ema9 = True
                confluences.append(f"At EMA9 ({ema9_dist:.0f}t)")

        if vwap > 0:
            vwap_dist = abs(price - vwap) / tick_size
            if vwap_dist <= max_vwap_dist:
                near_vwap = True
                confluences.append(f"At VWAP ({vwap_dist:.0f}t)")

        if not near_ema9 and not near_vwap:
            self._last_reject = (f"DOM_PB: Not at level "
                                 f"(EMA9={abs(price-ema9)/tick_size:.0f}t, "
                                 f"VWAP={abs(price-vwap)/tick_size:.0f}t)")
            return None

        # ── Gate 3: Genuine pullback (price WAS extended before) ──────
        level = ema9 if near_ema9 else vwap
        highs_6 = [b.high for b in bars_1m[-6:]]
        lows_6  = [b.low  for b in bars_1m[-6:]]
        max_above = (max(highs_6) - level) / tick_size
        max_below = (level - min(lows_6)) / tick_size

        if direction == "LONG" and max_above < 8:
            self._last_reject = f"DOM_PB: No pullback — was never >8t above EMA/VWAP"
            return None
        if direction == "SHORT" and max_below < 8:
            self._last_reject = f"DOM_PB: No pullback — was never >8t below EMA/VWAP"
            return None

        dist_str = f"{max_above:.0f}t" if direction == "LONG" else f"{max_below:.0f}t"
        confluences.append(f"Pullback {dist_str} from {'high' if direction == 'LONG' else 'low'}")

        # ── Gate 4: Pullback bar quality — QUIET, not aggressive ──────
        # The bar(s) making the pullback should be small-bodied with low volume.
        # A strong, large down bar into EMA9 = real selling, not a clean retest.
        # We want: price drifted back to EMA9 on weak volume (no conviction selling)
        bounce_bar   = bars_1m[-1]   # Current bar — should be the bounce
        pullback_bar = bars_1m[-2]   # Last completed bar — should be the pullback

        pb_body  = abs(pullback_bar.close - pullback_bar.open) / tick_size
        pb_range = (pullback_bar.high - pullback_bar.low) / tick_size

        # Pullback bar must NOT be a strong, decisive counter-trend bar
        if pb_body > max_pb_body_ticks:
            self._last_reject = (f"DOM_PB: Pullback bar too strong "
                                 f"(body={pb_body:.0f}t > {max_pb_body_ticks}t) — aggressive reversal, not pullback")
            return None

        # Check if pullback is on lighter volume than prior trend bars
        prior_bars = bars_1m[-5:-2]  # 3 bars before the pullback
        avg_prior_vol = sum(b.volume for b in prior_bars) / len(prior_bars) if prior_bars else 0
        quiet_pb = (avg_prior_vol == 0 or pullback_bar.volume <= avg_prior_vol * 1.2)

        if quiet_pb:
            confluences.append(f"Quiet pullback (pb body={pb_body:.0f}t)")
        else:
            confluences.append(f"Pullback (high vol — weaker signal)")

        # ── Gate 5: DOM absorption — sellers being pulled/absorbed ────
        # RAW imbalance ratio only (not the boolean flag — unreliable threshold)
        dom_confirmed = False

        if direction == "LONG":
            if dom_imbal >= 0.60:   # Clearly bid-heavy: buyers overwhelming sellers
                dom_confirmed = True
                confluences.append(f"DOM bid-heavy ({dom_imbal:.2f}) — sell orders absorbed")
            if dom_dir == "LONG" and dom_strength >= min_dom_str:
                dom_confirmed = True
                confluences.append(f"DOM absorption LONG ({dom_strength:.0f}%)")
        elif direction == "SHORT":
            if dom_imbal <= 0.40:   # Clearly ask-heavy: sellers overwhelming buyers
                dom_confirmed = True
                confluences.append(f"DOM ask-heavy ({dom_imbal:.2f}) — buy orders absorbed")
            if dom_dir == "SHORT" and dom_strength >= min_dom_str:
                dom_confirmed = True
                confluences.append(f"DOM absorption SHORT ({dom_strength:.0f}%)")

        if not dom_confirmed:
            self._last_reject = (f"DOM_PB: No DOM signal "
                                 f"(dir={dom_dir}, str={dom_strength:.0f}, "
                                 f"imbal={dom_imbal:.2f})")
            return None

        # ── Gate 6: Bounce bar quality — decisive, strong body ────────
        # The bounce bar (current bar) must show buyers/sellers stepping in hard.
        # Not just any green candle — needs a real body, close near the high.
        bounce_body = abs(bounce_bar.close - bounce_bar.open) / tick_size
        bounce_range = (bounce_bar.high - bounce_bar.low) / tick_size

        if direction == "LONG":
            if bounce_bar.close <= bounce_bar.open:
                self._last_reject = "DOM_PB: Bounce bar is bearish — no confirmation"
                return None
            if bounce_body < min_bounce_body:
                self._last_reject = f"DOM_PB: Bounce body too small ({bounce_body:.0f}t < {min_bounce_body}t)"
                return None
            # Close should be in upper half of bar (not just a wick)
            if bounce_range > 0:
                close_position = (bounce_bar.close - bounce_bar.low) / (bounce_bar.high - bounce_bar.low)
                if close_position < 0.4:
                    self._last_reject = f"DOM_PB: Bounce close in lower half (pos={close_position:.0%}) — wick, not body"
                    return None
            confluences.append(f"Bounce bar strong ({bounce_body:.0f}t body, "
                               f"close at {((bounce_bar.close - bounce_bar.low) / max(bounce_range * tick_size, tick_size)):.0%})")
        elif direction == "SHORT":
            if bounce_bar.close >= bounce_bar.open:
                self._last_reject = "DOM_PB: Bounce bar is bullish — no confirmation"
                return None
            if bounce_body < min_bounce_body:
                self._last_reject = f"DOM_PB: Bounce body too small ({bounce_body:.0f}t < {min_bounce_body}t)"
                return None
            if bounce_range > 0:
                close_position = (bounce_bar.high - bounce_bar.close) / (bounce_bar.high - bounce_bar.low)
                if close_position < 0.4:
                    self._last_reject = f"DOM_PB: Bounce close in upper half — wick, not body"
                    return None
            confluences.append(f"Bounce bar strong ({bounce_body:.0f}t body)")

        # ── Gate 7: Bar delta — momentum accelerating ─────────────────
        # "Confirmed momentum picking up" — bar_delta strongly in our direction
        delta_ok = (direction == "LONG" and bar_delta > 0) or \
                   (direction == "SHORT" and bar_delta < 0)

        if delta_ok:
            confluences.append(f"Delta {'bullish' if direction == 'LONG' else 'bearish'} "
                               f"({bar_delta:+,.0f})")
        else:
            # Weak signal — bar delta against, but DOM confirmed. Log but allow.
            confluences.append(f"⚠ Delta against ({bar_delta:+,.0f}) — DOM absorbing")

        # ── Gate 8: 300-tick bar microstructure ───────────────────────
        # The user watches DOM levels on a 300-tick chart for precision timing.
        # When sellers try to push price through EMA9 but fail, the 300t bars
        # show: slow, doji-like bars (quiet selling) → then a fast up bar (buyers in).
        # This is the same pattern as 1m but with 5-10x better timing resolution.
        tick_bars = market.get("bars_tick", [])   # 300-tick bars (dicts: o,h,l,c,v)
        tick_confirmed = False
        tick_note = ""

        if len(tick_bars) >= 2:
            tb_last = tick_bars[-1]
            tb_prev = tick_bars[-2]
            # Last 300t bar should be bullish/bearish in trade direction
            if direction == "LONG":
                tb_bull = tb_last["c"] > tb_last["o"]   # green bar
                tb_close_up = tb_last["c"] > tb_prev["c"]   # closing higher
                if tb_bull and tb_close_up:
                    tick_confirmed = True
                    tick_note = f"300t bar bullish (c={tb_last['c']:.2f} > prev {tb_prev['c']:.2f})"
            else:
                tb_bear = tb_last["c"] < tb_last["o"]   # red bar
                tb_close_dn = tb_last["c"] < tb_prev["c"]
                if tb_bear and tb_close_dn:
                    tick_confirmed = True
                    tick_note = f"300t bar bearish (c={tb_last['c']:.2f} < prev {tb_prev['c']:.2f})"

        if tick_confirmed:
            confluences.append(tick_note)
        elif tick_bars:
            confluences.append("300t bar not confirming (weaker signal)")

        # ── Score ─────────────────────────────────────────────────────
        score = 45   # Base: 7 gates passed — high quality setup
        score += min(15, int(dom_strength * 0.2))  # DOM strength (0-15)
        if quiet_pb:         score += 8    # Clean pullback — no aggressive sellers
        if delta_ok:         score += 8    # Momentum confirming
        if near_ema9:        score += 5    # EMA9 > VWAP as pullback level
        if trend_day:        score += 8
        if tick_confirmed:   score += 8    # 300t bar microstructure confirms
        if cvd > 0 and direction == "LONG":  score += 5
        if cvd < 0 and direction == "SHORT": score += 5
        if bounce_bar.volume > pullback_bar.volume:
            score += 5   # Volume picking up on bounce = buyers stepping in
            confluences.append("Volume accelerating on bounce")

        if trend_day and mq_bias == direction:
            confluences.append(f"TREND day (MQ={mq_bias})")

        confluences.append(f"Regime: {session_info.get('regime', '?')}")

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=score,
            entry_score=min(60, score),
            strategy=self.name,
            reason=(f"DOM pullback {direction} — DOM {dom_strength:.0f}%/{dom_imbal:.2f} "
                    f"at {'EMA9' if near_ema9 else 'VWAP'}, "
                    f"pb quiet ({pb_body:.0f}t), bounce ({bounce_body:.0f}t)"),
            confluences=confluences,
        )
