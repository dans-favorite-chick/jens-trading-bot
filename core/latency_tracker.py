"""
Phoenix Bot — Latency Tracker (P4-3)

In-process latency telemetry for the tick → signal → OIF pipeline.

Per F-23 in `docs/audits/SYNTHESIS_2026-05-24.md`: we had no live
fill-latency telemetry — backtests assume 2 ticks of slippage flat
but live execution timing was a black box.

Scope (this module): tick-in (bridge receives tick) → OIF-out
(base_bot writes/sends OIF trade command). NT8 ack latency (OIF
written → NT8 outgoing/ ack file) is OUT OF SCOPE here — flagged
as P4-3.2 future work.

Stages tracked:
  - "tick_to_bar"    — bridge tick receive → bar close in bot
  - "bar_to_signal"  — bar close → strategy emits signal
  - "signal_to_oif"  — signal → OIF/trade command written to bridge

Design constraints (per spec):
  - Stdlib only (no prometheus_client / opentelemetry).
  - < 0.1ms per record on the hot path (no IO, no logging).
  - Rolling window of last 1000 records per stage (deque maxlen).
  - Thread-safe — bridge runs in asyncio, bots run async, but the
    tracker may be touched from multiple threads (e.g. dashboard
    HTTP thread calling summary()).
  - In-process only. Bridge has its own tracker, each bot has its
    own. Aggregation across processes is future work.
  - Periodic summaries only — never per-record log (would itself
    be a latency source).

Usage:

    from core.latency_tracker import get_latency_tracker

    tracker = get_latency_tracker()
    t_in = time.time()
    # ... do work ...
    t_out = time.time()
    tracker.record("tick_to_bar", t_in, t_out)

    # Periodically (dashboard / health endpoint):
    stats = tracker.summary()
    # {"tick_to_bar": {"count": ..., "p99_ms": ...}, ...}
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, Optional


# Rolling window size per stage. 1000 records is enough for stable
# p99 estimates while keeping memory bounded (~24 KB per stage
# assuming 24-byte floats in CPython).
_WINDOW_SIZE = 1000

# Known stage names. Not enforced — record() accepts any string so
# tests and future stages don't need a code change. Listed here for
# documentation and so summary() returns a stable shape even when
# a stage has no samples yet.
KNOWN_STAGES = ("tick_to_bar", "bar_to_signal", "signal_to_oif")


def _percentile(sorted_samples: list, p: float) -> float:
    """Compute the p-th percentile of a pre-sorted list of floats.

    Uses linear interpolation between closest ranks (numpy-equivalent
    method='linear'). p is in [0, 100]. Returns 0.0 for an empty
    input so summary() never raises.
    """
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_samples[0]
    # Clamp p to [0, 100]
    if p <= 0:
        return sorted_samples[0]
    if p >= 100:
        return sorted_samples[-1]
    # Linear interpolation between two nearest ranks
    rank = (p / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * frac


def _empty_stage_stats() -> Dict[str, float]:
    """Default stats dict for a stage with zero samples.

    Returned by summary() for known stages with no records so the
    JSON shape is stable — dashboards don't have to special-case
    missing keys.
    """
    return {
        "count": 0,
        "mean_ms": 0.0,
        "p50_ms": 0.0,
        "p90_ms": 0.0,
        "p99_ms": 0.0,
        "max_ms": 0.0,
    }


class LatencyTracker:
    """Thread-safe in-process latency tracker with rolling windows.

    One deque per stage, each maxlen=_WINDOW_SIZE. A single Lock
    guards both the per-stage deque dict and the "pending in"
    timestamps for the t_out=None API.

    Records are stored as latency in milliseconds (float), not as
    (t_in, t_out) pairs — keeps summary() math cheap.
    """

    __slots__ = ("_samples", "_pending", "_lock")

    def __init__(self) -> None:
        # Lazy-init per-stage deque on first record() — keeps memory
        # zero until a stage is actually used.
        self._samples: Dict[str, Deque[float]] = {}
        # When record() is called with t_out=None, stash t_in here so
        # a later record(stage, t_in_stashed_unused, t_out) can close
        # it out. NOT thread-keyed — single global stash per stage,
        # since the spec only needs a simple stamp-then-close pattern.
        self._pending: Dict[str, float] = {}
        self._lock = threading.Lock()

    def record(
        self,
        stage: str,
        t_in: float,
        t_out: Optional[float] = None,
    ) -> None:
        """Record a latency sample for `stage`.

        If `t_out` is None, stash `t_in` as a pending start timestamp
        for this stage. The next call to record(stage, _, t_out) with
        a real t_out will use the stashed value (the t_in arg in the
        closing call is ignored when there's a pending stash).

        Hot-path constraint: no IO, no logging, no allocations beyond
        deque append + the (cheap) lock acquire. The lock is held for
        only the deque/dict mutations — math happens outside.

        Stage names are not validated — any string works.
        """
        # Stage-pending bookkeeping happens under the lock.
        if t_out is None:
            with self._lock:
                self._pending[stage] = t_in
            return

        # Resolve the effective t_in: a previously stashed value wins
        # over the arg (so the close-call doesn't need to re-pass it).
        with self._lock:
            stashed = self._pending.pop(stage, None)
            effective_t_in = stashed if stashed is not None else t_in
            # Lazy-init the deque for this stage.
            dq = self._samples.get(stage)
            if dq is None:
                dq = deque(maxlen=_WINDOW_SIZE)
                self._samples[stage] = dq
            # Compute outside the lock would be marginally faster but
            # the arithmetic is sub-microsecond and the deque append
            # already needs the lock — net is still well under 0.1ms.
            latency_ms = (t_out - effective_t_in) * 1000.0
            dq.append(latency_ms)

    def summary(self) -> Dict[str, Dict[str, float]]:
        """Return per-stage stats for all known + observed stages.

        Shape:
            {
                "tick_to_bar":   {count, mean_ms, p50_ms, p90_ms, p99_ms, max_ms},
                "bar_to_signal": {...},
                "signal_to_oif": {...},
            }

        Known stages always appear (with zeros if no samples) so the
        dashboard JSON shape is stable. Any extra stages that were
        record()-ed are also included.

        Safe to call from any thread. Snapshots the deques under the
        lock then computes outside so the hot path isn't blocked
        during percentile math.
        """
        # Snapshot per-stage sample lists under the lock — short
        # critical section so record() callers aren't blocked.
        with self._lock:
            snapshot: Dict[str, list] = {
                stage: list(dq) for stage, dq in self._samples.items()
            }

        # Build result with KNOWN_STAGES first (stable order, stable
        # shape) then any other stages we've observed.
        result: Dict[str, Dict[str, float]] = {}
        for stage in KNOWN_STAGES:
            samples = snapshot.get(stage, [])
            result[stage] = self._compute_stats(samples)
        for stage, samples in snapshot.items():
            if stage not in result:
                result[stage] = self._compute_stats(samples)
        return result

    @staticmethod
    def _compute_stats(samples: list) -> Dict[str, float]:
        """Compute count/mean/p50/p90/p99/max from a sample list.

        Returns _empty_stage_stats() shape for an empty list — never
        raises. samples is mutated in place (sorted) for percentile
        math; caller must pass a private copy (summary() does this).
        """
        n = len(samples)
        if n == 0:
            return _empty_stage_stats()
        samples.sort()
        total = 0.0
        for v in samples:
            total += v
        return {
            "count": n,
            "mean_ms": total / n,
            "p50_ms": _percentile(samples, 50),
            "p90_ms": _percentile(samples, 90),
            "p99_ms": _percentile(samples, 99),
            "max_ms": samples[-1],
        }

    def reset(self) -> None:
        """Drop all samples and pending stamps. Test-only helper."""
        with self._lock:
            self._samples.clear()
            self._pending.clear()


# ─── Singleton accessor ─────────────────────────────────────────────
# In-process singleton so bridge and bot import the same tracker.
# Note: bridge and bots are separate processes — each gets its own
# singleton. Cross-process aggregation is P4-3.2 future work.

_singleton: Optional[LatencyTracker] = None
_singleton_lock = threading.Lock()


def get_latency_tracker() -> LatencyTracker:
    """Return the process-wide LatencyTracker singleton.

    Lazy-initialized on first call. Thread-safe via double-checked
    locking — once the singleton exists, no lock is taken.
    """
    global _singleton
    # Fast path: already initialized.
    if _singleton is not None:
        return _singleton
    # Slow path: initialize under lock.
    with _singleton_lock:
        if _singleton is None:
            _singleton = LatencyTracker()
        return _singleton


def _reset_singleton_for_tests() -> None:
    """Test-only: drop the singleton so the next get_latency_tracker()
    builds a fresh one. Required for tests that need isolation."""
    global _singleton
    with _singleton_lock:
        _singleton = None
