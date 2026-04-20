"""
Phoenix Bot — Anomaly-based Circuit Breakers

Monitors bot health and trading patterns. On anomaly: pause new entries,
alert, require user acknowledgment to resume. Existing position management
continues during halt (exits per exit rules).

Breaker types:
  SIGNAL_RATE     Signal count > 3σ above rolling 30d mean for time-of-day
  TICK_GAP        No ticks for > 60s during RTH
  SLIPPAGE_SPIKE  Last 3 trades slippage > 2× rolling 7d median
  WIN_RATE_CRASH  Last 10 trades WR < 20% (only after 30+ history)
  DOM_DISCONNECT  No DOM update for > 30s during RTH
  EMERGENCY_HALT  User manually created memory/.HALT marker file

Modes:
  observe_mode=True  → Log + alert only, DO NOT halt. First 2 weeks default.
  observe_mode=False → Active: halt + require ack.
"""

from __future__ import annotations

import logging
import os
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("CircuitBreakers")

PHOENIX_ROOT = Path(__file__).parent.parent
HALT_MARKER_FILE = PHOENIX_ROOT / "memory" / ".HALT"


@dataclass
class BreakerEvent:
    breaker_type: str
    ts: datetime
    severity: str    # WARN | CRITICAL
    reason: str
    metadata: dict = field(default_factory=dict)


class CircuitBreakers:
    """
    Stateful breaker monitor. Call check_*() methods on relevant events.
    Halt decisions buffered into self.events. get_halt_status() returns
    whether any breaker has fired.
    """

    def __init__(self, observe_mode: bool = True):
        self.observe_mode = observe_mode
        self.events: deque[BreakerEvent] = deque(maxlen=200)
        self.halted: bool = False
        self.halted_reason: str = ""
        self.halted_at: Optional[datetime] = None

        # Rolling data for detection
        self._signals_per_hour: dict[int, deque[int]] = defaultdict(lambda: deque(maxlen=30))  # 30 days
        self._signals_this_hour: int = 0
        self._current_hour_key: Optional[int] = None
        self._last_tick_ts: Optional[datetime] = None
        self._last_dom_ts: Optional[datetime] = None
        self._recent_slippages: deque[float] = deque(maxlen=20)
        self._recent_trade_outcomes: deque[str] = deque(maxlen=10)  # "WIN" | "LOSS"
        self._total_trades_lifetime: int = 0
        # Telegram alert throttle: {breaker_type: last_alert_ts_epoch}
        self._alert_throttle: dict[str, float] = {}

    def _is_rth(self, ts: datetime) -> bool:
        """Is timestamp during RTH (08:30-15:00 CDT)?"""
        # Naive: just check hour. DST handling deferred.
        if ts.weekday() >= 5:
            return False  # Weekend
        h = ts.hour
        return 8 <= h < 15

    # ─── Signal rate tracking ──────────────────────────────────────────

    def record_signal(self, ts: datetime = None) -> None:
        """Called when any strategy generates a signal."""
        if ts is None:
            ts = datetime.now()
        hour_key = ts.hour
        if self._current_hour_key is None:
            self._current_hour_key = hour_key
        if hour_key != self._current_hour_key:
            # Roll over: record the completed hour's count
            self._signals_per_hour[self._current_hour_key].append(self._signals_this_hour)
            self._signals_this_hour = 0
            self._current_hour_key = hour_key
        self._signals_this_hour += 1

    def check_signal_rate(self, ts: datetime = None) -> Optional[BreakerEvent]:
        """Is current hour's signal count > 3σ above mean?"""
        if ts is None:
            ts = datetime.now()
        hour_key = ts.hour
        history = self._signals_per_hour.get(hour_key, deque())
        if len(history) < 7:
            return None  # Need baseline
        mean = statistics.mean(history)
        sd = statistics.stdev(history) if len(history) > 1 else 0
        if sd == 0:
            return None
        z = (self._signals_this_hour - mean) / sd
        if z > 3:
            ev = BreakerEvent(
                breaker_type="SIGNAL_RATE",
                ts=ts,
                severity="CRITICAL",
                reason=f"Hour-of-day {hour_key}: {self._signals_this_hour} signals vs mean {mean:.1f} (z={z:.1f})",
                metadata={"z_score": z, "hour_mean": mean},
            )
            self.events.append(ev)
            return ev
        return None

    # ─── Tick gap ──────────────────────────────────────────────────────

    def record_tick(self, ts: datetime = None) -> None:
        self._last_tick_ts = ts or datetime.now()

    def check_tick_gap(self, ts: datetime = None) -> Optional[BreakerEvent]:
        if ts is None:
            ts = datetime.now()
        if not self._is_rth(ts) or self._last_tick_ts is None:
            return None
        gap_s = (ts - self._last_tick_ts).total_seconds()
        if gap_s > 60:
            ev = BreakerEvent(
                breaker_type="TICK_GAP",
                ts=ts,
                severity="CRITICAL",
                reason=f"No ticks for {gap_s:.0f}s during RTH",
                metadata={"gap_seconds": gap_s},
            )
            self.events.append(ev)
            return ev
        return None

    # ─── DOM disconnect ────────────────────────────────────────────────

    def record_dom(self, ts: datetime = None) -> None:
        self._last_dom_ts = ts or datetime.now()

    def check_dom_gap(self, ts: datetime = None) -> Optional[BreakerEvent]:
        if ts is None:
            ts = datetime.now()
        if not self._is_rth(ts) or self._last_dom_ts is None:
            return None
        gap_s = (ts - self._last_dom_ts).total_seconds()
        if gap_s > 30:
            ev = BreakerEvent(
                breaker_type="DOM_DISCONNECT",
                ts=ts,
                severity="WARN",
                reason=f"No DOM update for {gap_s:.0f}s during RTH",
                metadata={"gap_seconds": gap_s},
            )
            self.events.append(ev)
            return ev
        return None

    # ─── Slippage spike ────────────────────────────────────────────────

    def record_slippage(self, slippage_ticks: float) -> None:
        self._recent_slippages.append(slippage_ticks)

    def check_slippage_spike(self) -> Optional[BreakerEvent]:
        if len(self._recent_slippages) < 10:
            return None
        last_3 = list(self._recent_slippages)[-3:]
        avg_recent = statistics.mean(last_3)
        baseline = statistics.median(list(self._recent_slippages)[:-3])
        if baseline <= 0.5:
            return None
        if avg_recent > baseline * 2.0:
            ev = BreakerEvent(
                breaker_type="SLIPPAGE_SPIKE",
                ts=datetime.now(),
                severity="WARN",
                reason=f"Last 3 slippage avg {avg_recent:.1f}t > 2× baseline {baseline:.1f}t",
                metadata={"recent": last_3, "baseline": baseline},
            )
            self.events.append(ev)
            return ev
        return None

    # ─── Win rate crash ────────────────────────────────────────────────

    def record_trade_outcome(self, outcome: str) -> None:
        self._recent_trade_outcomes.append(outcome)
        self._total_trades_lifetime += 1

    def check_wr_crash(self) -> Optional[BreakerEvent]:
        if self._total_trades_lifetime < 30:
            return None  # Need baseline history
        if len(self._recent_trade_outcomes) < 10:
            return None
        wins = sum(1 for o in self._recent_trade_outcomes if o == "WIN")
        wr = wins / len(self._recent_trade_outcomes)
        if wr < 0.2:
            ev = BreakerEvent(
                breaker_type="WIN_RATE_CRASH",
                ts=datetime.now(),
                severity="CRITICAL",
                reason=f"Last 10 trades WR {wr:.0%} < 20% threshold",
                metadata={"wr": wr, "wins": wins},
            )
            self.events.append(ev)
            return ev
        return None

    # ─── Emergency halt marker ─────────────────────────────────────────

    def check_emergency_halt(self) -> Optional[BreakerEvent]:
        """Check if user created memory/.HALT file."""
        if HALT_MARKER_FILE.exists():
            ev = BreakerEvent(
                breaker_type="EMERGENCY_HALT",
                ts=datetime.now(),
                severity="CRITICAL",
                reason=f"User-created .HALT marker at {HALT_MARKER_FILE}",
                metadata={"path": str(HALT_MARKER_FILE)},
            )
            self.events.append(ev)
            return ev
        return None

    # ─── Halt decision ─────────────────────────────────────────────────

    def should_halt(self) -> bool:
        """Check all breakers. If observe_mode, always returns False but still logs."""
        events = [
            self.check_tick_gap(), self.check_dom_gap(),
            self.check_slippage_spike(), self.check_wr_crash(),
            self.check_emergency_halt(),
        ]
        critical = [e for e in events if e and e.severity == "CRITICAL"]
        was_halted = self.halted
        if critical:
            for ev in critical:
                logger.error(f"[BREAKER {ev.breaker_type}] {ev.reason}")
            self._alert_new_criticals(critical)
            if self.observe_mode:
                logger.warning("[BREAKERS] OBSERVE MODE — not halting despite critical breakers")
                return False
            # Active mode: halt
            self.halted = True
            self.halted_at = datetime.now()
            self.halted_reason = ", ".join(e.breaker_type for e in critical)
            if not was_halted:
                self._alert_halt_transition(critical)
            return True
        # Just log warnings
        for ev in [e for e in events if e and e.severity == "WARN"]:
            logger.warning(f"[BREAKER {ev.breaker_type}] {ev.reason}")
        return self.halted  # stay halted if previously halted

    def _alert_new_criticals(self, events: list) -> None:
        """
        Fire a Telegram alert for critical breakers, rate-limited to once per
        (breaker_type, 1h window). Keeps observe-mode transparent without spam.
        """
        now_s = datetime.now().timestamp()
        for ev in events:
            last = self._alert_throttle.get(ev.breaker_type, 0)
            if now_s - last < 3600:  # 1h dedup
                continue
            self._alert_throttle[ev.breaker_type] = now_s
            mode_label = "OBSERVE" if self.observe_mode else "ACTIVE"
            try:
                import asyncio
                from core import telegram_notifier as _tg
                asyncio.ensure_future(_tg.notify_alert(
                    f"BREAKER ({mode_label})",
                    f"{ev.breaker_type}: {ev.reason}",
                ))
            except Exception as e:
                logger.debug(f"[BREAKER TG] alert dispatch failed: {e}")

    def _alert_halt_transition(self, events: list) -> None:
        """One-shot Telegram alert the moment we flip from running → halted."""
        try:
            import asyncio
            from core import telegram_notifier as _tg
            types = ", ".join(e.breaker_type for e in events)
            asyncio.ensure_future(_tg.notify_alert(
                "BOT HALTED",
                f"Trading halted. Triggers: {types}. Clear memory/.HALT or run "
                f"circuit_breakers.acknowledge_halt() to resume.",
            ))
        except Exception as e:
            logger.debug(f"[BREAKER TG] halt alert failed: {e}")

    def acknowledge_halt(self) -> None:
        """User has reviewed and cleared the halt."""
        if self.halted:
            logger.info(f"[BREAKERS] Halt acknowledged by user after {self.halted_reason}")
        self.halted = False
        self.halted_reason = ""
        self.halted_at = None
        # Also remove .HALT marker if present
        if HALT_MARKER_FILE.exists():
            try:
                HALT_MARKER_FILE.unlink()
            except Exception:
                pass

    def get_state(self) -> dict:
        """Dashboard snapshot."""
        return {
            "observe_mode": self.observe_mode,
            "halted": self.halted,
            "halted_reason": self.halted_reason,
            "halted_at": self.halted_at.isoformat() if self.halted_at else None,
            "recent_events": [
                {
                    "type": e.breaker_type,
                    "ts": e.ts.isoformat(),
                    "severity": e.severity,
                    "reason": e.reason,
                }
                for e in list(self.events)[-10:]
            ],
        }
