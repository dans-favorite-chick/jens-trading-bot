"""
Phoenix Bot — Raschke Baseline Trend-Pullback (Phase 13)
========================================================

Backtested 5 years on MNQ Databento data (2021-05-17 -> 2026-05-17):
  +$12,779 baseline, PF 4.10, 6/6 years positive.
  Source: tools/phoenix_trend_pullback_lab.py (variant "raschke_baseline").
  Promoted to production class per Phase 13 ship plan.

ENTRY LOGIC (Linda Raschke's canonical 20-EMA pullback)
-------------------------------------------------------
Window: 08:30 - 15:00 CT (RTH), evaluated only on 5m bar-close boundaries
(now_ct.minute % 5 == 0).

TREND FILTER (ADX proxy via EMA spread):
  e21 = EMA(5m close, period=21)
  e50 = EMA(5m close, period=50)
  spread = e21 - e50
  threshold = trend_spread_atr * atr_5m   (baseline: 0.3 * ATR_5m)
  direction = LONG  if spread >  threshold
  direction = SHORT if spread < -threshold
  else: no trend, NO_SIGNAL.

PULLBACK DETECTION:
  Look at the 3 most-recent CLOSED 5m bars before the current one.
  Find the bar that TOUCHED EMA21 in trend direction:
    LONG: bar.low <= e21 + 2t buffer AND bar.close > e21
    SHORT: bar.high >= e21 - 2t buffer AND bar.close < e21

ENTRY TRIGGER:
  Current 5m bar close must BREAK the pullback bar high (LONG) or
  low (SHORT) by at least 1 tick.

Stop placement:
  Opposite extreme of the pullback bar -+ 1 tick.
  Clamped to [6, 40] ticks — outside that range, signal is SKIPPED.
Target placement (legacy lab):
  entry +- 2 * stop_distance.   Phase 13 ship overrides to
  time_exit(30m) via core.exit_policies (lab target serves as a wide
  bracket placeholder).

PHASE 13 OVERRIDES (core/exit_policies.PHASE_13_EXIT_ASSIGNMENTS):
  exit_policy = time_exit(minutes=30)
  order_type  = market
  entry_mode  = retest (Section V.1 pilot: +$119 / 60d in entry-retest analyzer)
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal
from config.settings import TICK_SIZE
from core.confluence_gates import regime_veto, tf60m_es_gate

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")
_TICK = TICK_SIZE


def _in_rth(now_ct: datetime) -> bool:
    t = now_ct.time()
    return dtime(8, 30) <= t < dtime(15, 0)


class _EmaState:
    """Self-contained EMA state, updated once per new 5m bar close."""

    def __init__(self, periods=(9, 21, 50)):
        self._periods = tuple(periods)
        self._ema: dict[int, Optional[float]] = {p: None for p in periods}
        self._alpha: dict[int, float] = {p: 2.0 / (p + 1) for p in periods}
        self._last_bar_end_ts: Optional[float] = None
        self._bars_seen: int = 0

    def update(self, last_5m_bar) -> None:
        try:
            end_ts = float(getattr(last_5m_bar, "end_time", 0) or 0)
        except (TypeError, ValueError):
            return
        if end_ts == 0 or end_ts == self._last_bar_end_ts:
            return
        try:
            close = float(getattr(last_5m_bar, "close", 0) or 0)
        except (TypeError, ValueError):
            return
        if close <= 0:
            return
        self._last_bar_end_ts = end_ts
        self._bars_seen += 1
        for p in self._periods:
            cur = self._ema[p]
            a = self._alpha[p]
            self._ema[p] = close if cur is None else a * close + (1 - a) * cur

    def get(self, period: int) -> Optional[float]:
        v = self._ema.get(period)
        if v is None:
            return None
        return v if self._bars_seen >= period else None


class RaschkeBaseline(BaseStrategy):
    """20-EMA trend-pullback (Raschke baseline variant)."""

    name = "raschke_baseline"
    computes_own_stop = True
    computes_own_target = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._ema = _EmaState(periods=(9, 21, 50))
        self._last_signal_bar_ts: float = 0.0
        self._trend_spread_atr = float(config.get("trend_spread_atr", 0.3))
        self._ema_ref_period = int(config.get("ema_ref_period", 21))
        self._touch_buffer_ticks = int(config.get("touch_buffer_ticks", 2))
        self._break_buffer_ticks = int(config.get("break_buffer_ticks", 1))
        self._stop_buffer_ticks = int(config.get("stop_buffer_ticks", 1))
        self._min_stop_ticks = int(config.get("min_stop_ticks", 6))
        self._max_stop_ticks = int(config.get("max_stop_ticks", 40))
        self._target_rr = float(config.get("target_rr", 2.0))
        # Pullback search window — last N closed 5m bars (excluding current)
        self._pullback_lookback = int(config.get("pullback_lookback", 3))

    # ── Trend classifier ──────────────────────────────────────────

    def _trend_direction(self, atr_5m: float) -> Optional[str]:
        e21 = self._ema.get(21)
        e50 = self._ema.get(50)
        if e21 is None or e50 is None or atr_5m <= 0:
            return None
        spread = e21 - e50
        threshold = self._trend_spread_atr * atr_5m
        if spread > threshold:
            return "LONG"
        if spread < -threshold:
            return "SHORT"
        return None  # In chop

    # ── Pullback detection ────────────────────────────────────────

    def _find_pullback_bar(self, bars_5m: list, ema_ref: float, direction: str):
        """Search bars_5m[-(lookback+1):-1] (the N closed bars before current).
        Return the pullback bar (or None)."""
        n = self._pullback_lookback
        if len(bars_5m) < n + 1:
            return None
        scan = list(bars_5m)[-(n + 1):-1]
        buffer = self._touch_buffer_ticks * _TICK
        # Search newest-first so we pick the most recent pullback
        for b in reversed(scan):
            bl = float(getattr(b, "low", 0) or 0)
            bh = float(getattr(b, "high", 0) or 0)
            bc = float(getattr(b, "close", 0) or 0)
            if bl <= 0 or bh <= 0 or bc <= 0:
                continue
            if direction == "LONG" and bl <= ema_ref + buffer and bc > ema_ref:
                return b
            if direction == "SHORT" and bh >= ema_ref - buffer and bc < ema_ref:
                return b
        return None

    # ── Main evaluate ──────────────────────────────────────────────

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:
        logger.debug(f"[EVAL] {self.name}: entered evaluate()")

        # Per a16cf0ef research: OPEN_MOMENTUM regime drags -$7.43/trade
        # on raschke_baseline (pullback strategy struggles when market
        # is in straight-up/down momentum mode with no retracements).
        _passed, _ = regime_veto(
            market, ("OPEN_MOMENTUM",),
            strategy_name=self.name, config=self.config, logger=logger,
        )
        if not _passed:
            return None

        # Keep EMA state warm on every eval cycle (uses 5m bar end_time
        # dedup to avoid double-updating on the same bar).
        if bars_5m:
            self._ema.update(bars_5m[-1])

        if not bars_5m or len(bars_5m) < self._pullback_lookback + 1:
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

        if not _in_rth(now_ct):
            logger.debug(f"[EVAL] {self.name}: SKIP outside_rth")
            return None

        # Only evaluate on 5m bar-close boundary
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

        atr = float(market.get("atr_5m") or 0)
        if atr <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP atr_unavailable")
            return None

        direction = self._trend_direction(atr)
        if direction is None:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_trend")
            return None

        # Universal confluence gate: tf_60m + es_correlation must agree.
        # Per a16cf0ef research: WR lifts from baseline to ~54% on
        # raschke_baseline when both voters align with trend direction.
        _passed, _ = tf60m_es_gate(
            market, direction,
            strategy_name=self.name, config=self.config, logger=logger,
        )
        if not _passed:
            return None

        ema_ref = self._ema.get(self._ema_ref_period)
        if ema_ref is None:
            logger.debug(f"[EVAL] {self.name}: SKIP ema_ref_unavailable")
            return None

        pb = self._find_pullback_bar(bars_5m, ema_ref, direction)
        if pb is None:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_pullback_bar")
            return None

        pb_high = float(getattr(pb, "high", 0) or 0)
        pb_low = float(getattr(pb, "low", 0) or 0)
        if pb_high <= 0 or pb_low <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP bad_pullback_bar")
            return None

        current = bars_5m[-1]
        cc = float(getattr(current, "close", 0) or 0)
        if cc <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP bad_current_close")
            return None

        brk = self._break_buffer_ticks * _TICK
        if direction == "LONG":
            if cc <= pb_high + brk:
                logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_break")
                return None
        else:
            if cc >= pb_low - brk:
                logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_break")
                return None

        price = float(market.get("price") or cc)
        if direction == "LONG":
            stop = pb_low - self._stop_buffer_ticks * _TICK
            stop_dist = price - stop
        else:
            stop = pb_high + self._stop_buffer_ticks * _TICK
            stop_dist = stop - price

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
        e21 = self._ema.get(21) or 0.0
        e50 = self._ema.get(50) or 0.0
        confluences = [
            f"e21={e21:.2f}",
            f"e50={e50:.2f}",
            f"spread={e21 - e50:+.2f}",
            f"atr_5m={atr:.2f}",
            f"pullback=[{pb_low:.2f},{pb_high:.2f}]",
        ]
        reason = (
            f"Raschke {direction}: trend e21-e50={e21 - e50:+.2f} > "
            f"{self._trend_spread_atr}*ATR; pullback to "
            f"{pb_low if direction == 'LONG' else pb_high:.2f}; "
            f"break by close5={cc:.2f}"
        )
        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} entry={price:.2f} "
            f"stop={stop} target={target}"
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
                "sub_strategy": "raschke_baseline",
                "e21": e21,
                "e50": e50,
                "atr_5m": atr,
                "pullback_high": pb_high,
                "pullback_low": pb_low,
            },
        )
