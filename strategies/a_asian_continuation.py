"""
Phoenix Bot — Asian Session Continuation (Phase 13)
====================================================

Backtested 5 years on MNQ Databento data (2021-05-17 -> 2026-05-17):
  +$5,909 baseline, PF 8.29, 6/6 years positive.
  Source: tools/phoenix_new_strategy_lab.py (strategy "a_asian_continuation").
  Promoted to production class per Phase 13 ship plan.

ENTRY LOGIC
-----------
Window: 03:00 - 08:00 CT (after the Asian session range is built).
Trigger: a 5m close BEYOND the overnight 17:00-08:30 CT range, padded
by 0.5 * ATR_5m to filter shallow probes.

  direction = LONG  if  close_5m > overnight_high + 0.5*ATR_5m
  direction = SHORT if  close_5m < overnight_low  - 0.5*ATR_5m

Stop placement:
  distance = min(distance_to_opposite_range_edge, 14 ticks),
             clamped to >= 6 ticks (avoid sub-noise scalps).
Target placement (legacy lab):
  entry +- 2 * stop_distance.   Phase 13 ship overrides to time_exit(30m)
  via core.exit_policies (the lab target acts as a wide bracket placeholder).

Fires AT MOST once per calendar day.

DATA DEPENDENCY
---------------
Requires `market["atr_5m"]` and a self-maintained overnight high/low
window. The window is rebuilt every evaluate() call from `bars_1m`
covering the 17:00-08:30 CT span; falls back to NO_SIGNAL if fewer
than ~5 hours of bars are available.

PHASE 13 OVERRIDES (core/exit_policies.PHASE_13_EXIT_ASSIGNMENTS):
  exit_policy = time_exit(minutes=30)
  order_type  = market
  entry_mode  = first_touch
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal
from config.settings import TICK_SIZE

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")
_TICK = TICK_SIZE  # 0.25 on MNQ


def _in_window(now_ct: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, end_hhmm.split(":"))
    t = now_ct.time()
    return dtime(sh, sm) <= t < dtime(eh, em)


class AsianContinuation(BaseStrategy):
    """Overnight 17:00-08:30 CT range break continuation, fires 03:00-08:00 CT."""

    name = "a_asian_continuation"
    # Strategy computes its own stop/target — Phase 13 override may
    # rewrite target_price via time_exit policy in base_bot, but the
    # initial bracket comes from here.
    computes_own_stop = True
    computes_own_target = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._fired_date: Optional[str] = None
        self._window_start_ct = config.get("window_start_ct", "03:00")
        self._window_end_ct = config.get("window_end_ct", "08:00")
        self._range_break_atr_mult = float(config.get("range_break_atr_mult", 0.5))
        self._max_stop_ticks = int(config.get("max_stop_ticks", 14))
        self._min_stop_ticks = int(config.get("min_stop_ticks", 6))
        self._target_rr = float(config.get("target_rr", 2.0))
        self._min_overnight_range_ticks = int(
            config.get("min_overnight_range_ticks", 8)
        )

    # ── Overnight range helper ─────────────────────────────────────

    @staticmethod
    def _overnight_range(bars_1m: list, now_ct: datetime) -> Optional[tuple]:
        """Compute overnight 17:00 (prev day) - 08:30 CT (today) high/low
        from `bars_1m`. Returns (high, low) or None if insufficient bars."""
        if not bars_1m:
            return None
        # Window end = today 08:30 CT (or now if still inside the overnight)
        today = now_ct.date()
        # Overnight starts at PREV day 17:00 CT
        on_start = datetime.combine(today, dtime(17, 0), tzinfo=_CT) - timedelta(days=1)
        on_end = datetime.combine(today, dtime(8, 30), tzinfo=_CT)
        hi: Optional[float] = None
        lo: Optional[float] = None
        for b in bars_1m:
            try:
                bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
            except (AttributeError, TypeError, ValueError):
                continue
            if bt < on_start or bt >= on_end:
                continue
            bh = float(getattr(b, "high", 0) or 0)
            bl = float(getattr(b, "low", 0) or 0)
            if bh <= 0 or bl <= 0:
                continue
            hi = bh if hi is None else max(hi, bh)
            lo = bl if lo is None else min(lo, bl)
        if hi is None or lo is None:
            return None
        return (hi, lo)

    # ── Main evaluate ──────────────────────────────────────────────

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:
        logger.debug(f"[EVAL] {self.name}: entered evaluate()")

        # Resolve "now" in CT — prefer market.now_ct if base_bot enriches,
        # else infer from the last bar.
        now_ct = market.get("now_ct")
        if now_ct is None:
            if not bars_1m:
                logger.debug(f"[EVAL] {self.name}: SKIP no_now_ct_no_bars")
                return None
            try:
                now_ct = datetime.fromtimestamp(
                    float(bars_1m[-1].end_time), tz=_CT
                )
            except (AttributeError, TypeError, ValueError):
                logger.debug(f"[EVAL] {self.name}: SKIP bar_end_time_unreadable")
                return None

        if not _in_window(now_ct, self._window_start_ct, self._window_end_ct):
            logger.debug(f"[EVAL] {self.name}: SKIP outside_window")
            return None

        date_str = now_ct.strftime("%Y-%m-%d")
        if self._fired_date == date_str:
            logger.debug(f"[EVAL] {self.name}: SKIP already_fired_today")
            return None

        on = self._overnight_range(bars_1m, now_ct)
        if on is None:
            logger.debug(f"[EVAL] {self.name}: SKIP overnight_range_unavailable")
            return None
        on_high, on_low = on
        on_range = on_high - on_low
        if on_range < self._min_overnight_range_ticks * _TICK:
            logger.debug(
                f"[EVAL] {self.name}: SKIP overnight_range_too_tight "
                f"({on_range/_TICK:.0f}t < {self._min_overnight_range_ticks}t)"
            )
            return None

        atr = float(market.get("atr_5m") or 0)
        if atr <= 0:
            atr = 2.0  # fallback so the 0.5*ATR pad is at least 1pt

        if not bars_5m:
            logger.debug(f"[EVAL] {self.name}: SKIP no_5m_bars")
            return None
        last5 = bars_5m[-1]
        close5 = float(getattr(last5, "close", 0) or 0)
        if close5 <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP bad_5m_close")
            return None

        price = float(market.get("price") or close5)
        pad = self._range_break_atr_mult * atr

        direction: Optional[str] = None
        if close5 > on_high + pad:
            direction = "LONG"
        elif close5 < on_low - pad:
            direction = "SHORT"
        if direction is None:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_range_break")
            return None

        # Stop sizing
        if direction == "LONG":
            raw = price - on_low
        else:
            raw = on_high - price
        stop_dist = min(raw, self._max_stop_ticks * _TICK)
        stop_dist = max(stop_dist, self._min_stop_ticks * _TICK)
        if stop_dist <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP non_positive_stop_dist")
            return None
        if direction == "LONG":
            stop = round(price - stop_dist, 2)
            target = round(price + stop_dist * self._target_rr, 2)
        else:
            stop = round(price + stop_dist, 2)
            target = round(price - stop_dist * self._target_rr, 2)

        self._fired_date = date_str
        stop_ticks = int(round(stop_dist / _TICK))
        confluences = [
            f"overnight_range={on_range/_TICK:.0f}t",
            f"close5_break_pad={pad/_TICK:.0f}t",
            f"atr_5m={atr:.2f}",
        ]
        reason = (
            f"Asian-session continuation {direction}: close5={close5:.2f} "
            f"broke ON {'high' if direction=='LONG' else 'low'} "
            f"[{on_low:.2f}, {on_high:.2f}] by {pad/_TICK:.0f}t pad"
        )
        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} entry={price:.2f} "
            f"stop={stop} target={target} (ON=[{on_low:.2f},{on_high:.2f}])"
        )
        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=self._target_rr,
            confidence=70.0,
            entry_score=40.0,
            strategy=self.name,
            reason=reason,
            confluences=confluences,
            atr_stop_override=True,
            entry_type="MARKET",
            entry_price=price,
            stop_price=stop,
            target_price=target,
            metadata={
                "sub_strategy": "asian_continuation",
                "on_high": on_high,
                "on_low": on_low,
                "atr_5m": atr,
            },
        )
