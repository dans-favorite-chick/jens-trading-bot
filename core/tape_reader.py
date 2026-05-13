"""
Phoenix Bot — Tape Reader (Sprint M Tier 2.3)

Pure-observation feature for detecting institutional "large prints" on
the executed tick tape. Captures trades whose size meets or exceeds a
threshold, classifies aggressor side via the quote rule, and exposes
the rolling window as a market-snapshot field.

Does NOT gate entries. Does NOT influence IQS scoring tonight. The
purpose of this first ship is to ACCUMULATE DATA on what large prints
look like in practice so future-Claude can decide:
  1. Whether large-print direction predicts subsequent price movement
  2. What threshold is "large enough" for MNQ (current default 25
     contracts; may be too high overnight or too low during RTH)
  3. Whether a +5 IQS bonus for direction-aligned recent prints
     produces statistically significant lift (the original Tier 2.3
     spec proposed this — deferred until n>=30 trades of data show
     correlation)

Per `feedback_silent_failures.md`: data flows out via `get_state()`
(consumed in `base_bot._evaluate_strategies` snapshot enrichment),
NOT as a hidden side-channel — so future debugging can see exactly
what the bot observed.

Quote rule for aggressor-side classification (Lee-Ready 1991):
  - tick `price >= ask`  -> buy aggressor (lifted the offer)
  - tick `price <= bid`  -> sell aggressor (hit the bid)
  - inside the spread    -> classified by midpoint (price > mid = buy,
                             price < mid = sell, exactly at mid = unknown)
  - missing bid or ask   -> "unknown"
"""

from __future__ import annotations

from collections import deque
from typing import Optional


# Default threshold for "large" on MNQ. Single-tick sizes during RTH
# are typically 1-10 contracts, with retail clusters at 10-25 and
# institutional aggressor prints at 50+. 25 is a defensible "above
# normal retail" floor. Tuneable per-bot via constructor.
DEFAULT_LARGE_PRINT_THRESHOLD = 25

# Rolling window of recent large prints surfaced to consumers.
# Most recent N prints are what strategies care about for short-term
# direction confirmation. 50 captures roughly the last 5-15 minutes
# of action depending on regime.
DEFAULT_HISTORY_SIZE = 50


class TapeReader:
    """Rolling capture of large executed prints + aggressor-side tag.

    Lightweight, single-threaded, per-bot. Fed by `base_bot`'s tick
    receive loop one tick at a time; queried by `_evaluate_strategies`
    via `get_state()` for snapshot enrichment.
    """

    def __init__(
        self,
        threshold_contracts: int = DEFAULT_LARGE_PRINT_THRESHOLD,
        history_size: int = DEFAULT_HISTORY_SIZE,
    ):
        self._threshold: int = int(threshold_contracts)
        self._large_prints: deque[dict] = deque(maxlen=int(history_size))
        # Session aggregate stats — surface in snapshot so operators
        # can see "what threshold would have made sense today" without
        # opening the JSONL history.
        self._session_total_volume: int = 0
        self._session_tick_count: int = 0
        self._session_largest_size: int = 0

    # ── Public API ────────────────────────────────────────────────

    def record_tick(self, tick: dict) -> Optional[dict]:
        """Ingest one tick. Returns the print dict if recorded as
        large, else None. Safe to call with malformed ticks (returns
        None and updates no state)."""
        try:
            size = int(tick.get("vol", 0) or 0)
        except (TypeError, ValueError):
            return None
        if size <= 0:
            return None

        self._session_total_volume += size
        self._session_tick_count += 1
        if size > self._session_largest_size:
            self._session_largest_size = size

        if size < self._threshold:
            return None

        try:
            price = float(tick.get("price", 0) or 0)
            bid = float(tick.get("bid", 0) or 0)
            ask = float(tick.get("ask", 0) or 0)
        except (TypeError, ValueError):
            return None

        side = self._classify_side(price, bid, ask)

        record = {
            "ts": tick.get("ts", ""),
            "price": price,
            "size": size,
            "side": side,
        }
        self._large_prints.append(record)
        return record

    def get_state(self) -> dict:
        """Snapshot field consumed by `base_bot._evaluate_strategies`.

        Format intentionally JSON-serializable end-to-end so the field
        flows through history.jsonl eval events for later forensics.
        """
        return {
            "threshold_contracts": self._threshold,
            "history_size": len(self._large_prints),
            "large_prints": list(self._large_prints),
            "session_avg_size": round(
                self._session_total_volume / self._session_tick_count, 2
            ) if self._session_tick_count > 0 else 0.0,
            "session_largest_size": self._session_largest_size,
            "session_total_volume": self._session_total_volume,
        }

    def recent_aligned(self, direction: str, lookback: int = 10) -> int:
        """Count of large prints in the last `lookback` records whose
        side aligns with the trade direction. Pure read — does not
        mutate state. Available for future IQS-bonus consumers; not
        wired tonight per the observation-only scope."""
        want = "buy" if direction.lower() == "long" else "sell"
        recent = list(self._large_prints)[-lookback:]
        return sum(1 for p in recent if p.get("side") == want)

    # ── Internal helpers ──────────────────────────────────────────

    @staticmethod
    def _classify_side(price: float, bid: float, ask: float) -> str:
        """Quote-rule aggressor-side classification.

        Standard Lee-Ready 1991 rule, with conservative fallbacks
        when quote data is missing or degenerate. Returns one of
        {"buy", "sell", "unknown"}.
        """
        if ask > 0 and price >= ask:
            return "buy"
        if bid > 0 and price <= bid:
            return "sell"
        if bid > 0 and ask > 0 and ask > bid:
            mid = (bid + ask) / 2
            if price > mid:
                return "buy"
            if price < mid:
                return "sell"
        return "unknown"
