"""
Phoenix Bot — Multi-Day Breakout (Phase 13)
===========================================

Backtested 5 years on MNQ Databento data (2021-05-17 -> 2026-05-17):
  +$9,097 baseline, PF 6.79, 6/6 years positive.
  Source: tools/phoenix_new_strategy_lab.py (strategy "e_multi_day_breakout").
  Promoted to production class per Phase 13 ship plan.

ENTRY LOGIC
-----------
Window: 08:45 - 13:00 CT.
Trigger: a 5m close that BREAKS the highest high (LONG) or lowest low
(SHORT) of the prior THREE RTH (08:30-15:00 CT) sessions by >= 1 tick.

Stop placement:
  Opposite extreme of the breakout 5m bar + 2 ticks buffer.
  Clamped to [6, 30] ticks — outside that range, signal is SKIPPED.
Target placement (legacy lab):
  entry +- 2 * stop_distance.   Phase 13 ship overrides to
  chandelier(50, 3x, 1R) via core.exit_policies. The lab target is
  effectively replaced by a 10R-wide bracket placeholder by the
  ChandelierPolicy, which then trails dynamically.

Fires AT MOST once per calendar day.

DATA DEPENDENCY
---------------
Maintains a rolling list of (date_str, rth_high) and (date_str, rth_low)
tuples from previous RTH sessions, computed in-place from `bars_1m`.
This is "warm" once the strategy has seen >= 3 prior RTH days; lab tool
ran for 5 years so warmup is not the constraining factor in backtest.

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

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")
_TICK = TICK_SIZE


def _in_window(now_ct: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, end_hhmm.split(":"))
    t = now_ct.time()
    return dtime(sh, sm) <= t < dtime(eh, em)


class MultiDayBreakout(BaseStrategy):
    """3-day RTH high/low breakout, fires 08:45-13:00 CT."""

    name = "e_multi_day_breakout"
    computes_own_stop = True
    computes_own_target = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._fired_date: Optional[str] = None
        self._window_start_ct = config.get("window_start_ct", "08:45")
        self._window_end_ct = config.get("window_end_ct", "13:00")
        self._lookback_days = int(config.get("lookback_days", 3))
        self._stop_buffer_ticks = int(config.get("stop_buffer_ticks", 2))
        self._min_stop_ticks = int(config.get("min_stop_ticks", 6))
        self._max_stop_ticks = int(config.get("max_stop_ticks", 30))
        self._target_rr = float(config.get("target_rr", 2.0))
        self._break_buffer_ticks = int(config.get("break_buffer_ticks", 1))
        # Internal state — per-day rolling RTH highs/lows.
        # Each is (date_str, value). Kept short — last 10 days.
        self._rth_highs: list = []
        self._rth_lows: list = []
        self._cur_rth_date: Optional[str] = None
        self._cur_rth_high: Optional[float] = None
        self._cur_rth_low: Optional[float] = None

        # 2026-05-21 SHIP AUDIT pt4 (Bug 1): cold-start backfill.
        # Without this, the strategy's `lookback_days` warmup gate
        # (`if len(_rth_highs) < lookback_days: SKIP warmup`) means
        # the strategy CANNOT FIRE for 2-3 sessions after every bot
        # restart. Today's full RTH was silent for this reason.
        # Load prior days from history JSONL files at construction time.
        self._backfill_from_history()

    def _backfill_from_history(self) -> None:
        """Read the last `lookback_days + 2` history files and seed
        `_rth_highs`/`_rth_lows` so the strategy can fire on first eval
        post-restart.

        Reads `logs/history/<date>_<bot>.jsonl` style files. Each line
        is a JSON event; we scan for BAR events with `rth=True` (or
        equivalent) and aggregate H/L per date. Falls back gracefully
        if files don't exist or schema differs."""
        import json as _json
        import os as _os
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        _hist_dir = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "logs", "history",
        )
        if not _os.path.isdir(_hist_dir):
            return  # no history dir — strategy will warm naturally
        # Walk back from yesterday up to lookback_days + 2 calendar days
        # (extra cushion in case some days don't have RTH data).
        scanned = 0
        today_ct = _dt.now(_CT).strftime("%Y-%m-%d")
        d = _dt.now(_CT).date() - _td(days=1)
        per_day_hl: dict[str, tuple[float, float]] = {}
        max_days_back = self._lookback_days + 4
        while scanned < max_days_back and len(per_day_hl) < self._lookback_days:
            date_str = d.strftime("%Y-%m-%d")
            scanned += 1
            d -= _td(days=1)
            # Try common history filename patterns
            candidates = [
                _os.path.join(_hist_dir, f"{date_str}_sim.jsonl"),
                _os.path.join(_hist_dir, f"{date_str}_prod.jsonl"),
                _os.path.join(_hist_dir, f"{date_str}.jsonl"),
            ]
            hi: Optional[float] = None
            lo: Optional[float] = None
            for path in candidates:
                if not _os.path.exists(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = _json.loads(line)
                            except Exception:
                                continue
                            # Look for bar-level price events. Schema may vary
                            # bot-to-bot; try a few common keys.
                            h_val = rec.get("rth_high") or rec.get("high")
                            l_val = rec.get("rth_low") or rec.get("low")
                            ts_s = rec.get("ts") or rec.get("now_ct")
                            # Filter to RTH hours (08:30-15:00 CT)
                            if ts_s:
                                try:
                                    t = _dt.fromisoformat(str(ts_s).replace("Z", "+00:00"))
                                    t_ct = t.astimezone(_CT)
                                    if not (8 <= t_ct.hour < 15 or
                                            (t_ct.hour == 8 and t_ct.minute >= 30)):
                                        continue
                                except Exception:
                                    pass
                            if isinstance(h_val, (int, float)) and h_val > 0:
                                hi = max(hi, float(h_val)) if hi is not None else float(h_val)
                            if isinstance(l_val, (int, float)) and l_val > 0:
                                lo = min(lo, float(l_val)) if lo is not None else float(l_val)
                except Exception:
                    continue
                if hi is not None and lo is not None:
                    break  # got data from this file; don't double-process
            if hi is not None and lo is not None:
                per_day_hl[date_str] = (hi, lo)
        # Insert oldest first so list order = chronological
        for date_str in sorted(per_day_hl.keys()):
            hi, lo = per_day_hl[date_str]
            self._rth_highs.append((date_str, hi))
            self._rth_lows.append((date_str, lo))
        if self._rth_highs:
            logger.info(
                f"[{self.name}] backfilled {len(self._rth_highs)} prior RTH "
                f"days from history (warmup complete)"
            )
        else:
            logger.warning(
                f"[{self.name}] history backfill found 0 prior RTH days — "
                f"strategy will be in warmup for {self._lookback_days} sessions"
            )

    # ── RTH range maintenance (per-evaluate, idempotent) ───────────

    def _update_rth_history(self, bars_1m: list, now_ct: datetime) -> None:
        """Rebuild current-day RTH high/low from bars_1m. When the calendar
        day rolls, push prior day's range to history and reset."""
        date_str = now_ct.strftime("%Y-%m-%d")
        if self._cur_rth_date is not None and self._cur_rth_date != date_str:
            # Day-roll: archive prior day's range
            if self._cur_rth_high is not None and self._cur_rth_low is not None:
                self._rth_highs.append((self._cur_rth_date, self._cur_rth_high))
                self._rth_lows.append((self._cur_rth_date, self._cur_rth_low))
                # Keep last 10 days
                self._rth_highs = self._rth_highs[-10:]
                self._rth_lows = self._rth_lows[-10:]
            self._cur_rth_high = None
            self._cur_rth_low = None
        self._cur_rth_date = date_str

        # Scan today's RTH 1m bars
        for b in bars_1m:
            try:
                bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
            except (AttributeError, TypeError, ValueError):
                continue
            if bt.date() != now_ct.date():
                continue
            tt = bt.time()
            if tt < dtime(8, 30) or tt >= dtime(15, 0):
                continue
            bh = float(getattr(b, "high", 0) or 0)
            bl = float(getattr(b, "low", 0) or 0)
            if bh <= 0 or bl <= 0:
                continue
            self._cur_rth_high = bh if self._cur_rth_high is None else max(self._cur_rth_high, bh)
            self._cur_rth_low = bl if self._cur_rth_low is None else min(self._cur_rth_low, bl)

    # ── Main evaluate ──────────────────────────────────────────────

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:
        logger.debug(f"[EVAL] {self.name}: entered evaluate()")

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

        self._update_rth_history(bars_1m, now_ct)

        if not _in_window(now_ct, self._window_start_ct, self._window_end_ct):
            logger.debug(f"[EVAL] {self.name}: SKIP outside_window")
            return None

        date_str = now_ct.strftime("%Y-%m-%d")
        if self._fired_date == date_str:
            logger.debug(f"[EVAL] {self.name}: SKIP already_fired_today")
            return None

        if len(self._rth_highs) < self._lookback_days or \
           len(self._rth_lows) < self._lookback_days:
            logger.debug(
                f"[EVAL] {self.name}: SKIP warmup "
                f"({len(self._rth_highs)}/{self._lookback_days} days)"
            )
            return None

        three_high = max(h for _, h in self._rth_highs[-self._lookback_days:])
        three_low = min(l for _, l in self._rth_lows[-self._lookback_days:])

        if not bars_5m:
            logger.debug(f"[EVAL] {self.name}: SKIP no_5m_bars")
            return None
        last5 = bars_5m[-1]
        close5 = float(getattr(last5, "close", 0) or 0)
        high5 = float(getattr(last5, "high", 0) or 0)
        low5 = float(getattr(last5, "low", 0) or 0)
        if close5 <= 0 or high5 <= 0 or low5 <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP bad_5m_bar")
            return None

        price = float(market.get("price") or close5)
        break_pad = self._break_buffer_ticks * _TICK

        direction: Optional[str] = None
        if close5 > three_high + break_pad:
            direction = "LONG"
        elif close5 < three_low - break_pad:
            direction = "SHORT"
        if direction is None:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_break")
            return None

        # Stop at opposite extreme of breakout 5m bar + buffer
        if direction == "LONG":
            stop = low5 - self._stop_buffer_ticks * _TICK
            stop_dist = price - stop
        else:
            stop = high5 + self._stop_buffer_ticks * _TICK
            stop_dist = stop - price

        if stop_dist < self._min_stop_ticks * _TICK or \
           stop_dist > self._max_stop_ticks * _TICK:
            logger.debug(
                f"[EVAL] {self.name}: SKIP stop_out_of_band "
                f"({stop_dist/_TICK:.0f}t not in [{self._min_stop_ticks}, "
                f"{self._max_stop_ticks}])"
            )
            return None

        if direction == "LONG":
            target = round(price + stop_dist * self._target_rr, 2)
        else:
            target = round(price - stop_dist * self._target_rr, 2)
        stop = round(stop, 2)
        stop_ticks = int(round(stop_dist / _TICK))

        self._fired_date = date_str
        confluences = [
            f"3d_high={three_high:.2f}",
            f"3d_low={three_low:.2f}",
            f"break_5m_close={close5:.2f}",
        ]
        reason = (
            f"3-day breakout {direction}: close5={close5:.2f} broke "
            f"{'3dH' if direction=='LONG' else '3dL'}="
            f"{three_high if direction == 'LONG' else three_low:.2f}"
        )
        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} entry={price:.2f} "
            f"stop={stop} target={target} (3d_high={three_high:.2f} "
            f"3d_low={three_low:.2f})"
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
                "sub_strategy": "multi_day_breakout",
                "three_high": three_high,
                "three_low": three_low,
                "lookback_days": self._lookback_days,
            },
        )
