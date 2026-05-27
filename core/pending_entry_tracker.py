"""
Phoenix Bot — Pending Entry Lifecycle Tracker (P1-7)

Single source of truth for every LIMIT entry order Phoenix has submitted
but NT8 has not yet filled. Every such entry MUST end in one of four
terminal states:

    "filled"             — NT8 reported a fill ack.
    "cancelled"          — Phoenix cancelled (operator/runtime/etc).
    "timeout_cancelled"  — Sweeper cancelled because age > timeout.
    "adopted"            — Startup reconciliation found an NT8 order Phoenix
                           didn't place (typically post-restart). If the
                           adopted order is later filled the state stays
                           "adopted"; if Phoenix later cancels it the
                           state transitions to "adopted_cancelled".
    "flattened"          — Emergency flatten cancelled it.

The MARKET entry path does NOT go through this tracker — market orders
fill immediately or fail at submission. Only LIMIT (and stop-limit) entries
have a meaningful "pending" window.

Why a dedicated tracker exists alongside ``PositionManager._pending_entries``:
the legacy per-account dict in ``core/position_manager.py`` (added in Fix A,
2026-04-23) is keyed by ACCOUNT and only ever holds ONE pending entry per
account. Lazy expiry happens via ``has_pending_entry`` on the next
signal. P1-7 needs:

  - per-trade granularity (multiple in-flight limits across accounts),
  - explicit terminal-state semantics rather than "popped silently",
  - a periodic sweeper that runs even when no new signals fire (a quiet
    market is exactly where the old lazy-expiry pattern failed: an
    in-flight limit could age past the strategy's thesis without any
    code path noticing).

The two trackers coexist. The position-manager dict is the per-account
entry-gate (does NT8 already have a working limit on this account?) and
stays. This tracker is the per-trade lifecycle ledger. ``_trade_entry``
registers in both; the sweeper here is the authoritative timeout/cancel
path. Both clear themselves on fill.

Singleton accessor pattern matches ``core/latency_tracker.py``.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("PendingEntryTracker")


# Terminal-state literals (kept as plain strings for cheap JSON serialisation
# into trade_memory and for log-grep friendliness).
TERMINAL_FILLED = "filled"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_TIMEOUT_CANCELLED = "timeout_cancelled"
TERMINAL_ADOPTED = "adopted"
TERMINAL_ADOPTED_CANCELLED = "adopted_cancelled"
TERMINAL_FLATTENED = "flattened"

VALID_TERMINAL_STATES = frozenset({
    TERMINAL_FILLED,
    TERMINAL_CANCELLED,
    TERMINAL_TIMEOUT_CANCELLED,
    TERMINAL_ADOPTED,
    TERMINAL_ADOPTED_CANCELLED,
    TERMINAL_FLATTENED,
})


@dataclass
class PendingEntry:
    """A single in-flight LIMIT entry.

    Field names mirror the spec contract and the position-manager pending
    dict so existing log greps continue to work. ``terminal_state`` starts
    as None and may be set EXACTLY ONCE — the first transition wins and any
    subsequent attempt is logged + ignored (defends against the
    fill-vs-timeout race).
    """
    trade_id: str
    strategy: str
    account: str
    instrument: str
    side: str              # "BUY" / "SELL"
    qty: int
    limit_price: float
    oif_filename: str = ""  # Best-effort capture from oif_writer; may be empty
    nt8_order_id: Optional[str] = None
    submitted_at: float = field(default_factory=time.time)
    timeout_s: float = 90.0
    terminal_state: Optional[str] = None
    terminal_reason: Optional[str] = None
    terminal_at: Optional[float] = None
    # adopted=True flags startup-adopted entries so a later fill stays as
    # "adopted" and a later cancel becomes "adopted_cancelled" instead of
    # plain "cancelled". Tracked separately from terminal_state so the
    # transition logic is clean.
    adopted: bool = False


class PendingEntryTracker:
    """Thread-safe registry of in-flight LIMIT entries.

    Two read-paths touch this concurrently:
      - the sweeper coroutine (sweep_once) walking every entry,
      - the OIF / fill-ack path (mark_filled, mark_cancelled) updating one
        entry by trade_id.

    A single Lock guards _entries. Critical sections are short — at most
    one dict write or a single read-and-mutate.
    """

    __slots__ = ("_entries", "_lock")

    def __init__(self) -> None:
        self._entries: dict[str, PendingEntry] = {}
        self._lock = threading.Lock()

    # ─── Registration ────────────────────────────────────────────────────
    def register(
        self,
        trade_id: str,
        strategy: str,
        account: str,
        instrument: str,
        side: str,
        qty: int,
        limit_price: float,
        timeout_s: float,
        oif_filename: str = "",
        nt8_order_id: Optional[str] = None,
        adopted: bool = False,
    ) -> PendingEntry:
        """Add a new PendingEntry. Returns the stored dataclass.

        Re-registering the same trade_id is permitted (idempotent on the
        rare case where a retry hits this path twice) — the existing
        record's submitted_at + state are PRESERVED so a sweep already
        in flight isn't reset by a stale call.
        """
        if not trade_id:
            raise ValueError("PendingEntryTracker.register: trade_id is required")
        with self._lock:
            existing = self._entries.get(trade_id)
            if existing is not None:
                # Idempotent — don't reset submitted_at. Caller's intent is
                # almost certainly "register if not present".
                return existing
            pe = PendingEntry(
                trade_id=trade_id,
                strategy=strategy,
                account=account,
                instrument=instrument,
                side=side,
                qty=int(qty),
                limit_price=float(limit_price),
                oif_filename=oif_filename or "",
                nt8_order_id=nt8_order_id,
                submitted_at=time.time(),
                timeout_s=float(timeout_s),
                adopted=bool(adopted),
            )
            self._entries[trade_id] = pe
            return pe

    # ─── Lookup ──────────────────────────────────────────────────────────
    def get(self, trade_id: str) -> Optional[PendingEntry]:
        with self._lock:
            return self._entries.get(trade_id)

    def all_pending(self) -> list[PendingEntry]:
        """Snapshot of every entry that has not yet reached a terminal
        state. Returned as a fresh list — safe to iterate without holding
        the lock."""
        with self._lock:
            return [e for e in self._entries.values() if e.terminal_state is None]

    def all_entries(self) -> list[PendingEntry]:
        """Snapshot of every entry regardless of terminal_state. Useful
        for diagnostics, audit, and tests that need to assert what the
        sweeper closed out."""
        with self._lock:
            return list(self._entries.values())

    # ─── Terminal transitions ────────────────────────────────────────────
    def _set_terminal(
        self,
        trade_id: str,
        state: str,
        reason: str = "",
    ) -> Optional[PendingEntry]:
        """Internal helper: atomically set terminal_state if not already set.

        Returns the entry on success, None if entry not found or already
        terminal. The "first transition wins" rule defends against
        concurrent fill-vs-timeout: the second call sees terminal_state is
        non-None and bails out with a WARNING log.
        """
        if state not in VALID_TERMINAL_STATES:
            raise ValueError(f"invalid terminal_state {state!r}")
        with self._lock:
            pe = self._entries.get(trade_id)
            if pe is None:
                return None
            if pe.terminal_state is not None:
                logger.warning(
                    "[PENDING:%s] already terminal=%s, ignoring duplicate "
                    "transition to %s (reason=%s)",
                    trade_id, pe.terminal_state, state, reason,
                )
                return None
            pe.terminal_state = state
            pe.terminal_reason = reason or None
            pe.terminal_at = time.time()
            return pe

    def mark_filled(self, trade_id: str, reason: str = "fill_ack") -> Optional[PendingEntry]:
        """Transition to terminal=filled (or "adopted" if the entry was
        adopted and is now filling).

        Returns the updated entry or None if no such pending entry.
        """
        with self._lock:
            pe = self._entries.get(trade_id)
            if pe is None:
                return None
        # Adopted-then-fills stays in TERMINAL_ADOPTED per the contract.
        target = TERMINAL_ADOPTED if pe.adopted else TERMINAL_FILLED
        return self._set_terminal(trade_id, target, reason)

    def mark_cancelled(
        self,
        trade_id: str,
        reason: str = "cancelled",
    ) -> Optional[PendingEntry]:
        """Transition to terminal=cancelled (or "adopted_cancelled" for
        adopted entries Phoenix cancels later)."""
        with self._lock:
            pe = self._entries.get(trade_id)
            if pe is None:
                return None
        target = TERMINAL_ADOPTED_CANCELLED if pe.adopted else TERMINAL_CANCELLED
        return self._set_terminal(trade_id, target, reason)

    def mark_timeout_cancelled(
        self,
        trade_id: str,
        reason: str = "timeout",
    ) -> Optional[PendingEntry]:
        """Transition to terminal=timeout_cancelled. Used by the sweeper."""
        return self._set_terminal(trade_id, TERMINAL_TIMEOUT_CANCELLED, reason)

    def mark_flattened(
        self,
        trade_id: str,
        reason: str = "emergency_flatten",
    ) -> Optional[PendingEntry]:
        """Transition to terminal=flattened. Used by the emergency-flatten
        path."""
        return self._set_terminal(trade_id, TERMINAL_FLATTENED, reason)

    # ─── Sweeper ─────────────────────────────────────────────────────────
    def sweep_once(self, now: Optional[float] = None) -> list[PendingEntry]:
        """Find entries whose age exceeds their timeout and are not yet
        terminal. Marks them ``timeout_cancelled`` atomically and returns
        the list — caller is responsible for issuing the CANCEL OIFs and
        recording the trade_memory row.

        Splitting "mark" from "act" keeps this module free of OIF / IO
        coupling and lets the unit tests assert pure state transitions.
        """
        t = time.time() if now is None else now
        expired: list[PendingEntry] = []
        with self._lock:
            for pe in self._entries.values():
                if pe.terminal_state is not None:
                    continue
                if (t - pe.submitted_at) <= pe.timeout_s:
                    continue
                # Atomic mark inside the lock — no other thread can grab
                # this entry between the check and the set.
                pe.terminal_state = TERMINAL_TIMEOUT_CANCELLED
                pe.terminal_reason = (
                    f"age {t - pe.submitted_at:.1f}s > timeout {pe.timeout_s:.1f}s"
                )
                pe.terminal_at = t
                expired.append(pe)
        for pe in expired:
            logger.warning(
                "[PENDING_TIMEOUT:%s] %s %s %d @ %.2f on %s exceeded "
                "timeout %.1fs (age %.1fs) — marked timeout_cancelled",
                pe.trade_id, pe.strategy, pe.side, pe.qty,
                pe.limit_price, pe.account, pe.timeout_s,
                t - pe.submitted_at,
            )
        return expired

    # ─── Bulk flatten ────────────────────────────────────────────────────
    def mark_all_flattened(
        self,
        reason: str = "emergency_flatten",
        account: Optional[str] = None,
    ) -> list[PendingEntry]:
        """Set every non-terminal entry (optionally scoped to one account)
        to ``flattened``. Returns the list of entries that transitioned.
        Caller emits CANCEL OIFs for each.
        """
        flattened: list[PendingEntry] = []
        with self._lock:
            for pe in self._entries.values():
                if pe.terminal_state is not None:
                    continue
                if account is not None and pe.account != account:
                    continue
                pe.terminal_state = TERMINAL_FLATTENED
                pe.terminal_reason = reason
                pe.terminal_at = time.time()
                flattened.append(pe)
        for pe in flattened:
            logger.warning(
                "[PENDING_FLATTEN:%s] %s on %s — marked flattened (%s)",
                pe.trade_id, pe.strategy, pe.account, reason,
            )
        return flattened

    # ─── Persistence helpers ─────────────────────────────────────────────
    def attach_nt8_order_id(self, trade_id: str, order_id: str) -> bool:
        """Best-effort attach of the NT8 order_id once it's captured from
        outgoing/. Returns True if attached, False if no such pending."""
        if not trade_id or not order_id:
            return False
        with self._lock:
            pe = self._entries.get(trade_id)
            if pe is None:
                return False
            pe.nt8_order_id = str(order_id)
            return True

    def reset(self) -> None:
        """Test-only: drop every entry."""
        with self._lock:
            self._entries.clear()


# ─── Singleton accessor ──────────────────────────────────────────────────
_singleton: Optional[PendingEntryTracker] = None
_singleton_lock = threading.Lock()


def get_pending_entry_tracker() -> PendingEntryTracker:
    """Return the process-wide PendingEntryTracker singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PendingEntryTracker()
        return _singleton


def _reset_singleton_for_tests() -> None:
    """Test-only: drop the singleton so the next get_pending_entry_tracker()
    builds a fresh one."""
    global _singleton
    with _singleton_lock:
        _singleton = None
