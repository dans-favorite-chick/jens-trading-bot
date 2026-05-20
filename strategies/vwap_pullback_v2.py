"""
Phoenix Bot — VWAP Pullback V2 (NQ-Tuned)
==========================================

DROP-IN ALTERNATIVE to strategies/vwap_pullback.py with one critical fix:
the `skip_on_stop_clamp` pattern that rejects every signal when natural
ATR stop exceeds max_stop_ticks=120 is replaced with confirmation-bar
fallback.

This is the SAME fix pattern that applies to 9 strategies in the bot.
V2 demonstrates the pattern in a single self-contained file so you can
validate the approach before applying it to other files.

WHY THIS STRATEGY MATTERS
-------------------------
VWAP pullback is one of the highest-edge NQ setups in 2026:
- VWAP acts as institutional fair-value benchmark
- Pullbacks to VWAP on trend days have 60-65% WR per multiple sources
- Confirmation-bar entries (bounce candle on TF agreement) are tight
- Adds a CONTINUATION setup to complement LSR's REVERSAL setups

The original strategy already has all the right confluence checks
(VWAP proximity, pullback excursion, bounce candle, TF votes, CVD).
It just gets rejected at the stop-clamp gate.

WHAT V2 CHANGES
---------------
1. **stop_fallback_mode="confirmation"** — when natural ATR stop > max,
   use confirmation-bar fallback instead of skipping
2. **max_stop_ticks raised to 200** (was 120) — gives ATR more room
3. **min_stop_ticks lowered to 16** (was 40) — NQ-appropriate floor
4. All prices snapped to tick grid via snap_to_tick

WHAT V2 KEEPS
-------------
- VWAP proximity gate (price within 60 ticks of VWAP)
- Pullback excursion requirement (≥8 ticks from VWAP recently)
- Bounce candle confirmation (REQUIRED)
- EMA structure check (trend intact)
- CVD direction check
- TF vote requirement (2 non-trend, 1 trend)
- Trend-day MQ bias override

NAME
----
This strategy's `name = "vwap_pullback_v2"`. The original
`vwap_pullback` can keep running alongside.

DEPENDENCIES
------------
- strategies.base_strategy — BaseStrategy + Signal
- core.confirmation_stop — for stop fallback
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal
from core.confirmation_stop import compute_confirmation_stop, snap_to_tick

logger = logging.getLogger(__name__)
_CT = ZoneInfo("America/Chicago")

TICK_SIZE = 0.25


class VWAPPullbackV2(BaseStrategy):
    """VWAP pullback with confirmation-stop fallback for NQ 2026."""

    name = "vwap_pullback_v2"
    computes_own_stop = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._trades_today: int = 0
        self._trade_date: Optional[str] = None
        self._last_signal_bar_ts: float = 0

    def _maybe_reset_daily(self, now_ct: datetime):
        today = now_ct.strftime("%Y-%m-%d")
        if self._trade_date != today:
            self._trade_date = today
            self._trades_today = 0
            self._last_signal_bar_ts = 0

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:

        # ── Daily reset + cap ──────────────────────────────────────
        now_ct = market.get("now_ct")
        if not isinstance(now_ct, datetime):
            now_ct = datetime.now(_CT)
        self._maybe_reset_daily(now_ct)

        max_trades = int(self.config.get("max_trades_per_day", 4))
        if self._trades_today >= max_trades:
            return None

        # ── 2026-05-20 PHASE 13 SHIP AUDIT: session window gate ─────
        # PHOENIX_BEST_PLAN §J.2 (hidden insight): vwap_pullback_v2's
        # edge comes from the 17:00-04:59 CT overnight session ONLY —
        # tested as +$10K/5y if restricted to that window vs roughly
        # flat in RTH. Per-strategy windows can be overridden via the
        # session_windows_ct config key. Defaults to the J.2 window so
        # the plan's +$10K/5y is captured out of the box.
        sw = self.config.get("session_windows_ct",
                              [("17:00", "23:59"), ("00:00", "04:59")])
        hh = now_ct.hour
        mm = now_ct.minute
        cur_hhmm = f"{hh:02d}:{mm:02d}"
        _in_window = False
        for _start, _end in sw:
            # Allow normal ordering only (start <= end). The default
            # config above splits the overnight session across midnight
            # to handle the CT day-rollover cleanly.
            if _start <= cur_hhmm <= _end:
                _in_window = True
                break
        if not _in_window:
            logger.debug(
                f"[EVAL] {self.name}: SKIP outside_session_window "
                f"now={cur_hhmm} windows={sw}"
            )
            return None

        # ── Data sanity ────────────────────────────────────────────
        if not bars_1m or len(bars_1m) < 2:
            return None

        price = float(market.get("price", 0) or 0)
        vwap = float(market.get("vwap", 0) or 0)
        ema9 = float(market.get("ema9", 0) or 0)
        ema21 = float(market.get("ema21", 0) or 0)
        cvd = float(market.get("cvd", 0) or 0)
        bullish = int(market.get("tf_votes_bullish", 0) or 0)
        bearish = int(market.get("tf_votes_bearish", 0) or 0)
        day_type = market.get("day_type", "UNKNOWN")
        mq_bias = market.get("mq_direction_bias", "NEUTRAL")
        trend_day = (day_type == "TREND")

        # CRITICAL: sanitize numeric inputs against NaN/Inf
        import math as _math
        if not _math.isfinite(price) or price <= 0:
            logger.warning(f"[EVAL] {self.name}: SKIP non_finite_price={price}")
            return None
        if not _math.isfinite(vwap) or vwap <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP missing_vwap_or_price")
            return None
        # Tolerate corrupt EMAs/CVD/ATR by zeroing them — strategy degrades gracefully
        if not _math.isfinite(ema9):
            ema9 = 0
        if not _math.isfinite(ema21):
            ema21 = 0
        if not _math.isfinite(cvd):
            cvd = 0

        # Per-bar dedup
        last_bar = bars_1m[-1]
        try:
            last_bar_ts = float(last_bar.end_time)
        except (AttributeError, TypeError, ValueError):
            return None
        if last_bar_ts == self._last_signal_bar_ts:
            return None

        # ── VWAP proximity gate ────────────────────────────────────
        max_vwap_dist = int(self.config.get("max_vwap_dist_ticks", 60))
        vwap_dist_ticks = abs(price - vwap) / TICK_SIZE
        if vwap_dist_ticks > max_vwap_dist:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED vwap_dist {vwap_dist_ticks:.0f}t > {max_vwap_dist}t"
            )
            return None

        # ── Pullback excursion check ───────────────────────────────
        # Confirms price WAS away from VWAP recently (not just meandering)
        recent_highs = [float(b.high) for b in bars_1m[-5:]]
        recent_lows = [float(b.low) for b in bars_1m[-5:]]
        max_dist_above = (max(recent_highs) - vwap) / TICK_SIZE
        max_dist_below = (vwap - min(recent_lows)) / TICK_SIZE

        # ── Direction selection ────────────────────────────────────
        min_tf_votes = 1 if trend_day else int(self.config.get("min_tf_votes", 2))

        direction = None
        confluences = []
        if trend_day and mq_bias == "LONG" and max_dist_above >= 8:
            direction = "LONG"
            confluences.append(f"TREND day MQ LONG — {max_dist_above:.0f}t excursion")
        elif trend_day and mq_bias == "SHORT" and max_dist_below >= 8:
            direction = "SHORT"
            confluences.append(f"TREND day MQ SHORT — {max_dist_below:.0f}t excursion")
        elif bullish >= min_tf_votes and max_dist_above >= 8:
            direction = "LONG"
            confluences.append(f"Bullish TF: {bullish}/4")
            confluences.append(f"Pullback from {max_dist_above:.0f}t above VWAP")
        elif bearish >= min_tf_votes and max_dist_below >= 8:
            direction = "SHORT"
            confluences.append(f"Bearish TF: {bearish}/4")
            confluences.append(f"Pullback from {max_dist_below:.0f}t below VWAP")
        else:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_pullback_pattern")
            return None

        confluences.append(f"Near VWAP ({vwap_dist_ticks:.0f}t away)")

        # ── EMA structure check ────────────────────────────────────
        score = 30  # base score
        if direction == "LONG" and ema9 > ema21:
            score += 10
            confluences.append("EMA9 > EMA21 (trend intact)")
        elif direction == "SHORT" and ema9 < ema21:
            score += 10
            confluences.append("EMA9 < EMA21 (trend intact)")

        # ── CVD direction ──────────────────────────────────────────
        if direction == "LONG" and cvd > 0:
            score += 10
            confluences.append(f"CVD positive ({cvd:+.0f})")
        elif direction == "SHORT" and cvd < 0:
            score += 10
            confluences.append(f"CVD negative ({cvd:+.0f})")

        # ── Bounce candle REQUIRED ─────────────────────────────────
        has_bounce = False
        last_open = float(last_bar.open)
        last_close = float(last_bar.close)
        if direction == "LONG" and last_close > last_open:
            score += 10
            has_bounce = True
            confluences.append("Bounce candle (bullish)")
        elif direction == "SHORT" and last_close < last_open:
            score += 10
            has_bounce = True
            confluences.append("Bounce candle (bearish)")

        if not has_bounce:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_bounce_candle")
            return None

        # ── Stop calculation with CONFIRMATION FALLBACK ────────────
        # This is the key V2 change: instead of rejecting on stop_clamp,
        # use confirmation-bar stop when natural ATR exceeds max.
        atr_5m = float(market.get("atr_5m", 0) or 0)
        if not _math.isfinite(atr_5m) or atr_5m < 0:
            atr_5m = 0  # treat NaN/Inf/negative as missing → confirmation stop
        stop_atr_mult = float(self.config.get("stop_atr_mult", 2.0))
        max_stop = int(self.config.get("max_stop_ticks", 200))   # was 120
        min_stop = int(self.config.get("min_stop_ticks", 16))    # was 40

        # Compute natural ATR stop distance
        if atr_5m > 0:
            natural_atr_distance = atr_5m * stop_atr_mult
            natural_atr_ticks = int(natural_atr_distance / TICK_SIZE)
        else:
            natural_atr_ticks = 0

        # FALLBACK: when natural exceeds max, switch to confirmation stop
        if natural_atr_ticks > max_stop:
            stop_ticks, stop_price, stop_note = compute_confirmation_stop(
                direction=direction,
                entry_price=price,
                bars_1m=bars_1m,
                lookback_bars=5,
                buffer_ticks=2,
                tick_size=TICK_SIZE,
                min_ticks=min_stop,
                max_ticks=max_stop,
            )
            confluences.append(f"Vol-regime fallback: {stop_note}")
        elif natural_atr_ticks > 0:
            stop_ticks = max(min_stop, min(max_stop, natural_atr_ticks))
            if direction == "LONG":
                stop_price = snap_to_tick(price - stop_ticks * TICK_SIZE, TICK_SIZE)
            else:
                stop_price = snap_to_tick(price + stop_ticks * TICK_SIZE, TICK_SIZE)
            stop_note = f"ATR-based {stop_ticks}t ({stop_atr_mult}× ATR_5m={atr_5m:.1f})"
            confluences.append(stop_note)
        else:
            # No ATR available — confirmation stop only
            stop_ticks, stop_price, stop_note = compute_confirmation_stop(
                direction=direction,
                entry_price=price,
                bars_1m=bars_1m,
                lookback_bars=5,
                buffer_ticks=2,
                tick_size=TICK_SIZE,
                min_ticks=min_stop,
                max_ticks=max_stop,
            )
            confluences.append(f"No ATR: {stop_note}")

        # ── Target ─────────────────────────────────────────────────
        target_rr = float(self.config.get("target_rr", 1.8))
        if target_rr <= 0 or not _math.isfinite(target_rr):
            logger.warning(f"[EVAL] {self.name}: bad target_rr={target_rr}, using 1.8")
            target_rr = 1.8
        stop_distance = abs(price - stop_price)
        if direction == "LONG":
            target_price = snap_to_tick(price + stop_distance * target_rr, TICK_SIZE)
        else:
            target_price = snap_to_tick(price - stop_distance * target_rr, TICK_SIZE)

        # Snap entry
        entry_price = snap_to_tick(price, TICK_SIZE)

        # Confidence
        confluences.append(f"Regime: {session_info.get('regime', '?')}" if session_info else "Regime: ?")
        confidence = min(100.0, score + (10 if has_bounce else 0))
        entry_score = min(60.0, score * 0.75)

        # Update state
        self._trades_today += 1
        self._last_signal_bar_ts = last_bar_ts

        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} entry={entry_price:.2f} "
            f"stop={stop_price:.2f} ({stop_ticks}t) target={target_price:.2f}"
        )

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=confidence,
            entry_score=entry_score,
            strategy=self.name,
            reason=f"VWAP Pullback V2 {direction} — bounce confirmed at VWAP",
            confluences=confluences,
            atr_stop_override=True,
            entry_type="MARKET",
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            eod_flat_time_et="14:30",
            metadata={
                "vwap": vwap,
                "vwap_dist_ticks": vwap_dist_ticks,
                "max_excursion_ticks": max_dist_above if direction == "LONG" else max_dist_below,
                "stop_note": stop_note,
                "trend_day": trend_day,
                "mq_bias": mq_bias,
            },
        )
