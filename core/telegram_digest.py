"""
Phoenix - Telegram alert digest (Sprint D F4, 2026-05-04).

Buffers low-priority alerts (NEWS_EVENT, SCALE_OUT, STRATEGY_DECAY_WARNING,
BREAKER_OBSERVE) and flushes on time- or size-based triggers. Real-time
categories (HALT, KILL_SWITCH, EXIT_TIMEOUT, BOT_FAILED, RECOVERY_MODE,
BRIDGE_DOWN, DESYNC) bypass the buffer and ping immediately.

Designed as drop-in for `core.telegram_notifier.notify_alert`:

    from core.telegram_digest import classify_alert, get_digest

    cat = classify_alert(alert_type)
    if cat == "real_time":
        send_immediately(message)
    elif cat == "digest":
        digest = get_digest()
        flush_text = digest.queue_alert(alert_type, message, strategy)
        if flush_text is not None:
            send_immediately(flush_text)
    else:  # unknown -> default to real-time (fail-safe)
        send_immediately(message)

The single-process digest singleton in `get_digest()` is intentional:
the bot is a single-process daemon, and digest state across processes
makes no sense (each process has its own buffer).
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock
from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


# Hard-coded category classification. Real-time categories MUST stay
# real-time; they're the meaningful operator-action signals. Digest
# categories are the "good to know, but not now" alerts that bunch up
# during normal operation.
REAL_TIME_CATEGORIES: frozenset[str] = frozenset({
    "HALT", "KILL_SWITCH", "EXIT_TIMEOUT", "EXIT_TIMEOUT_RESOLVED",
    "EXIT_TIMEOUT_STILL_STUCK", "BOT_FAILED", "RECOVERY_MODE",
    "RECOVERY_EXITED", "BRIDGE_DOWN", "DESYNC", "STRATEGY_DECAY_CRITICAL",
})
DIGEST_CATEGORIES: frozenset[str] = frozenset({
    "NEWS_EVENT", "SCALE_OUT", "STRATEGY_DECAY_WARNING",
    "BREAKER_OBSERVE", "FRED_REGIME_SHIFT",
})


def classify_alert(alert_type: str) -> str:
    """Return 'real_time' | 'digest' | 'unknown'.

    Unknown categories fall through to real-time at the call site
    (fail-safe default — better to be slightly noisy than to drop
    something the operator needs to see)."""
    if alert_type in REAL_TIME_CATEGORIES:
        return "real_time"
    if alert_type in DIGEST_CATEGORIES:
        return "digest"
    return "unknown"


class AlertDigest:
    """Thread-safe queue with time + size flush triggers.

    queue_alert() returns None if no flush is needed, or the formatted
    digest string if a flush should be sent. Caller is responsible for
    actually dispatching that string via the telegram send path.
    """
    def __init__(self,
                 flush_interval_s: float = 4 * 3600,
                 flush_size: int = 10):
        self._queue: deque = deque()
        self._lock = Lock()
        self.flush_interval_s = float(flush_interval_s)
        self.flush_size = int(flush_size)
        self.last_flush_ts = time.time()

    def queue_alert(self, category: str, message: str,
                    strategy: str = "") -> str | None:
        """Append to the buffer. Return the formatted flush message if
        the time- or size-trigger has fired; else None."""
        now = time.time()
        with self._lock:
            self._queue.append({
                "ts": datetime.now(CT).isoformat(timespec="seconds"),
                "category": category,
                "strategy": strategy,
                "message": message,
            })
            should_flush = (
                len(self._queue) >= self.flush_size
                or (now - self.last_flush_ts) >= self.flush_interval_s
            )
            if not should_flush:
                return None
            return self._flush_locked(now)

    def force_flush(self) -> str | None:
        """Flush regardless of triggers. Returns the formatted digest or
        None if the buffer is empty. Useful at session-close or shutdown."""
        with self._lock:
            if not self._queue:
                return None
            return self._flush_locked(time.time())

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    def _flush_locked(self, now: float) -> str:
        """Caller MUST hold self._lock. Drains queue, builds the message."""
        items = list(self._queue)
        self._queue.clear()
        self.last_flush_ts = now
        return self._format_digest(items)

    @staticmethod
    def _format_digest(items: list[dict]) -> str:
        """Group items by category, show counts and up to 3 examples
        per category. Caps at ~80 chars per example to keep telegrams
        from blowing past message-length limits."""
        if not items:
            return "Phoenix digest (empty)"
        by_cat: dict[str, list[dict]] = {}
        for item in items:
            by_cat.setdefault(item["category"], []).append(item)
        lines = [f"\U0001F4CB Phoenix digest ({len(items)} alerts):"]
        for cat, msgs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {cat}: {len(msgs)}")
            for m in msgs[:3]:
                truncated = m["message"][:80]
                if len(m["message"]) > 80:
                    truncated += "..."
                lines.append(f"    - {truncated}")
            if len(msgs) > 3:
                lines.append(f"    ... and {len(msgs) - 3} more")
        return "\n".join(lines)


# Module-level singleton. Lazy-initialized so tests can patch the
# defaults via core.telegram_digest._DIGEST = AlertDigest(...).
_DIGEST: AlertDigest | None = None


def get_digest() -> AlertDigest:
    """Return the process-singleton digest. First call initializes it
    with default flush_interval_s=4h, flush_size=10."""
    global _DIGEST
    if _DIGEST is None:
        _DIGEST = AlertDigest()
    return _DIGEST


def reset_digest_for_tests() -> None:
    """Clear the singleton. Test-only helper."""
    global _DIGEST
    _DIGEST = None
