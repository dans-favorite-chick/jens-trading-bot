"""
Phoenix Bot — Inside Bar Breakout (Phase 13)
============================================

Backtested 5 years on MNQ Databento data (2021-05-17 -> 2026-05-17):
  +$11,300 baseline, PF 4.88, 6/6 years positive.
  Source: tools/phoenix_new_strategy_lab.py (strategy "g_inside_bar_breakout").
  Promoted to production class per Phase 13 ship plan.

ENTRY LOGIC
-----------
Window: 08:45 - 14:00 CT, evaluated only on 5m bar-close boundaries
(now_ct.minute % 5 == 0).
Trigger:
  PARENT     = bars_5m[-3]   (the "outer" reference bar)
  INSIDE     = bars_5m[-2]   (must be inside parent: high<=parent.high AND low>=parent.low)
  CURRENT    = bars_5m[-1]   (must break inside high/low by >= 1 tick)

  direction = LONG  if  current.close > inside.high + 1 tick
  direction = SHORT if  current.close < inside.low  - 1 tick

Inside-bar quality gates:
  - inside_range >= 4 ticks  (avoid micro-bars that are just noise)
  - inside_range <= 0.85 * parent_range  (must actually be tighter)

Stop placement:
  Opposite extreme of the INSIDE bar -+ 1 tick.
  Clamped to [6, 30] ticks — outside that range, signal is SKIPPED.
Target placement (legacy lab):
  entry +- 2 * stop_distance.   Phase 13 ship overrides to
  chandelier(50, 3x, 1R) via core.exit_policies.

Bar dedup: doesn't fire twice on the same 5m bar boundary.

PHASE 13 OVERRIDES (core/exit_policies.PHASE_13_EXIT_ASSIGNMENTS):
  exit_policy = chandelier(lookback_bars=50, atr_mult=3.0, activate_r=1.0)
  order_type  = limit_5s (Section U.2 — market orders chase RTH-open breakouts)
  entry_mode  = first_touch
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal
from config.settings import TICK_SIZE
from core.confluence_gates import tf5m_es_gate

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")
_TICK = TICK_SIZE


def _in_window(now_ct: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, end_hhmm.split(":"))
    t = now_ct.time()
    return dtime(sh, sm) <= t < dtime(eh, em)


class InsideBarBreakout(BaseStrategy):
    """5m inside-bar breakout, fires 08:45-14:00 CT on 5m bar closes."""

    name = "g_inside_bar_breakout"
    computes_own_stop = True
    computes_own_target = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._last_signal_bar_ts: float = 0.0
        self._window_start_ct = config.get("window_start_ct", "08:45")
        self._window_end_ct = config.get("window_end_ct", "14:00")
        self._min_inside_range_ticks = int(config.get("min_inside_range_ticks", 4))
        self._max_inside_range_ratio = float(config.get("max_inside_range_ratio", 0.85))
        self._break_buffer_ticks = int(config.get("break_buffer_ticks", 1))
        self._stop_buffer_ticks = int(config.get("stop_buffer_ticks", 1))
        self._min_stop_ticks = int(config.get("min_stop_ticks", 6))
        self._max_stop_ticks = int(config.get("max_stop_ticks", 30))
        self._target_rr = float(config.get("target_rr", 2.0))

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:
        logger.debug(f"[EVAL] {self.name}: entered evaluate()")

        if not bars_5m or len(bars_5m) < 3:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_5m_bars")
            return None

        now_ct = market.get("now_ct")
        if now_ct is None:
            try:
                now_ct = datetime.fromtimestamp(
                    float(bars_5m[-1].end_time), tz=_CT
                )
            except (AttributeError, TypeError, ValueError):
                logger.debug(f"[EVAL] {self.name}: SKIP bar_end_time_unreadable")
                return None

        if not _in_window(now_ct, self._window_start_ct, self._window_end_ct):
            logger.debug(f"[EVAL] {self.name}: SKIP outside_window")
            return None

        # Only evaluate on 5m bar-close boundary (minute % 5 == 0)
        if now_ct.minute % 5 != 0:
            logger.debug(f"[EVAL] {self.name}: SKIP off_5m_boundary")
            return None

        # Per-bar dedup
        try:
            bar_ts = float(bars_5m[-1].end_time)
        except (AttributeError, TypeError, ValueError):
            logger.debug(f"[EVAL] {self.name}: SKIP bar_end_time_unreadable")
            return None
        if bar_ts == self._last_signal_bar_ts:
            logger.debug(f"[EVAL] {self.name}: SKIP same_bar_dedup")
            return None

        parent = bars_5m[-3]
        inside = bars_5m[-2]
        current = bars_5m[-1]

        ph = float(getattr(parent, "high", 0) or 0)
        pl = float(getattr(parent, "low", 0) or 0)
        ih = float(getattr(inside, "high", 0) or 0)
        il = float(getattr(inside, "low", 0) or 0)
        cc = float(getattr(current, "close", 0) or 0)
        if ph <= 0 or pl <= 0 or ih <= 0 or il <= 0 or cc <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP bad_bar_values")
            return None

        irng = ih - il
        prng = ph - pl
        if irng <= 0 or prng <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP non_positive_range")
            return None

        # Inside-bar containment requirement
        if not (ih <= ph and il >= pl):
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL not_inside")
            return None
        # Inside-bar must have meaningful range
        if irng < self._min_inside_range_ticks * _TICK:
            logger.debug(
                f"[EVAL] {self.name}: NO_SIGNAL inside_too_small "
                f"({irng/_TICK:.0f}t < {self._min_inside_range_ticks}t)"
            )
            return None
        # Inside-bar must be tighter than parent
        if irng > self._max_inside_range_ratio * prng:
            logger.debug(
                f"[EVAL] {self.name}: NO_SIGNAL inside_not_tighter "
                f"(irng={irng/_TICK:.0f}t, prng={prng/_TICK:.0f}t, "
                f"ratio={irng/prng:.2f})"
            )
            return None

        price = float(market.get("price") or cc)
        brk = self._break_buffer_ticks * _TICK
        direction: Optional[str] = None
        if cc > ih + brk:
            direction = "LONG"
            stop = il - self._stop_buffer_ticks * _TICK
            stop_dist = price - stop
        elif cc < il - brk:
            direction = "SHORT"
            stop = ih + self._stop_buffer_ticks * _TICK
            stop_dist = stop - price
        else:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_break")
            return None

        # 2026-05-22 pt8 (per agent a9e3773f Agent B recommendation):
        # Mirror e_multi_day_breakout — same structure (5m close beyond
        # prior extreme), same 08:45-14:00 CT window. The tf5m+ES gate
        # is the universal-alpha breakout gate that lifted
        # e_multi_day_breakout from 77.8% → 95.97% WR. No reason to
        # believe g_inside_bar_breakout's edge differs structurally;
        # this gate cannot hurt and may lift the already-70% baseline.
        # Behind require_tf5m_es_gate (default True) per pt6 convention.
        _passed, _ = tf5m_es_gate(
            market, direction,
            strategy_name=self.name, config=self.config, logger=logger,
        )
        if not _passed:
            return None

        if stop_dist < self._min_stop_ticks * _TICK or \
           stop_dist > self._max_stop_ticks * _TICK:
            logger.debug(
                f"[EVAL] {self.name}: SKIP stop_out_of_band "
                f"({stop_dist/_TICK:.0f}t)"
            )
            return None

        if direction == "LONG":
            target = round(price + stop_dist * self._target_rr, 2)
        else:
            target = round(price - stop_dist * self._target_rr, 2)
        stop = round(stop, 2)
        stop_ticks = int(round(stop_dist / _TICK))

        self._last_signal_bar_ts = bar_ts
        confluences = [
            f"inside_range={irng/_TICK:.0f}t",
            f"parent_range={prng/_TICK:.0f}t",
            f"inside_parent_ratio={irng/prng:.2f}",
        ]
        reason = (
            f"Inside-bar breakout {direction}: close={cc:.2f} broke "
            f"inside={'high' if direction=='LONG' else 'low'} "
            f"[{il:.2f}, {ih:.2f}]"
        )
        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} entry={price:.2f} "
            f"stop={stop} target={target} (ib=[{il:.2f},{ih:.2f}])"
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
            entry_type="LIMIT",  # Phase 13 limit_5s override (Section U.2)
            entry_price=price,
            stop_price=stop,
            target_price=target,
            metadata={
                "sub_strategy": "inside_bar_breakout",
                "inside_high": ih,
                "inside_low": il,
                "parent_high": ph,
                "parent_low": pl,
            },
        )
