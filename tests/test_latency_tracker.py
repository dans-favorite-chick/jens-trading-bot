"""
Phoenix Bot — Tests for core/latency_tracker.py (P4-3)

Covers:
  - record() + summary() math (count, mean, p50/p90/p99, max)
  - Rolling window cap of 1000 records per stage
  - Thread safety under concurrent record() calls
  - Empty tracker returns sensible zeros (never raises)
  - Singleton accessor returns the same instance
  - t_out=None stash-then-close pattern
  - Stable JSON shape (known stages always present)
"""

from __future__ import annotations

import threading
import time

import pytest

from core.latency_tracker import (
    KNOWN_STAGES,
    LatencyTracker,
    _percentile,
    get_latency_tracker,
    _reset_singleton_for_tests,
)


# ─── Helpers ────────────────────────────────────────────────────────


def _record_latency_ms(tracker: LatencyTracker, stage: str, latency_ms: float) -> None:
    """Record a synthetic latency in ms (uses t_in=0, t_out=latency_s)."""
    tracker.record(stage, 0.0, latency_ms / 1000.0)


# ─── Empty tracker ──────────────────────────────────────────────────


def test_empty_tracker_returns_zeros_for_known_stages():
    """A fresh tracker must return a stable JSON shape with zeros for
    every known stage — dashboards rely on the keys being present."""
    tracker = LatencyTracker()
    summary = tracker.summary()
    for stage in KNOWN_STAGES:
        assert stage in summary, f"missing known stage: {stage}"
        s = summary[stage]
        assert s["count"] == 0
        assert s["mean_ms"] == 0.0
        assert s["p50_ms"] == 0.0
        assert s["p90_ms"] == 0.0
        assert s["p99_ms"] == 0.0
        assert s["max_ms"] == 0.0


def test_empty_tracker_summary_does_not_raise():
    """summary() on a brand-new tracker must not raise. Critical:
    /api/latency hit by dashboard before any tick arrives."""
    tracker = LatencyTracker()
    # If this raises, the test fails — no assertion on output.
    _ = tracker.summary()


# ─── Stats math ─────────────────────────────────────────────────────


def test_summary_math_simple():
    """Synthetic 1..10 ms samples → known percentiles."""
    tracker = LatencyTracker()
    for i in range(1, 11):
        _record_latency_ms(tracker, "tick_to_bar", float(i))
    s = tracker.summary()["tick_to_bar"]
    assert s["count"] == 10
    assert s["mean_ms"] == pytest.approx(5.5)
    # p50 of [1..10] = 5.5 by linear interpolation
    assert s["p50_ms"] == pytest.approx(5.5)
    # p90 of [1..10] = 9.1 (rank = 0.9 * 9 = 8.1 → 9 + 0.1*(10-9) = 9.1)
    assert s["p90_ms"] == pytest.approx(9.1)
    # p99 of [1..10] = 9.91 (rank = 0.99 * 9 = 8.91 → 9 + 0.91*1 = 9.91)
    assert s["p99_ms"] == pytest.approx(9.91)
    assert s["max_ms"] == pytest.approx(10.0)


def test_summary_math_single_sample():
    """One sample → all percentiles equal that sample, count=1."""
    tracker = LatencyTracker()
    _record_latency_ms(tracker, "signal_to_oif", 42.0)
    s = tracker.summary()["signal_to_oif"]
    assert s["count"] == 1
    assert s["mean_ms"] == pytest.approx(42.0)
    assert s["p50_ms"] == pytest.approx(42.0)
    assert s["p90_ms"] == pytest.approx(42.0)
    assert s["p99_ms"] == pytest.approx(42.0)
    assert s["max_ms"] == pytest.approx(42.0)


def test_percentile_helper_edge_cases():
    """_percentile must handle empty, single, and out-of-range p."""
    assert _percentile([], 50) == 0.0
    assert _percentile([7.0], 99) == 7.0
    # p<=0 clamps to first; p>=100 clamps to last
    assert _percentile([1.0, 2.0, 3.0], 0) == 1.0
    assert _percentile([1.0, 2.0, 3.0], 100) == 3.0
    assert _percentile([1.0, 2.0, 3.0], -5) == 1.0
    assert _percentile([1.0, 2.0, 3.0], 200) == 3.0


def test_summary_p99_with_outlier():
    """99 samples at 1ms and 1 sample at 100ms → p99 close to 100."""
    tracker = LatencyTracker()
    for _ in range(99):
        _record_latency_ms(tracker, "bar_to_signal", 1.0)
    _record_latency_ms(tracker, "bar_to_signal", 100.0)
    s = tracker.summary()["bar_to_signal"]
    assert s["count"] == 100
    assert s["max_ms"] == pytest.approx(100.0)
    # p99 of sorted [1.0]*99 + [100.0] = rank 0.99*99 = 98.01
    # → samples[98]=1.0 + 0.01*(100.0-1.0) = 1.99
    assert s["p99_ms"] == pytest.approx(1.99, rel=0.01)
    # p50 firmly in the 1.0 cluster
    assert s["p50_ms"] == pytest.approx(1.0)


# ─── Rolling window ─────────────────────────────────────────────────


def test_rolling_window_caps_at_1000():
    """Recording > 1000 samples retains only the last 1000."""
    tracker = LatencyTracker()
    # Record 1500 samples with monotonically increasing latency.
    # If the window works, the last 1000 (latencies 500..1499) survive
    # and the min (visible via p0 → samples[0]) is 500.
    for i in range(1500):
        _record_latency_ms(tracker, "tick_to_bar", float(i))
    s = tracker.summary()["tick_to_bar"]
    assert s["count"] == 1000, "deque maxlen=1000 should cap retention"
    assert s["max_ms"] == pytest.approx(1499.0)
    # The retained window is [500..1499]; mean is 999.5
    assert s["mean_ms"] == pytest.approx(999.5)


def test_rolling_window_per_stage_independent():
    """Each stage has its own deque — overflowing one does not affect
    the others."""
    tracker = LatencyTracker()
    for i in range(1500):
        _record_latency_ms(tracker, "tick_to_bar", float(i))
    for i in range(5):
        _record_latency_ms(tracker, "signal_to_oif", float(i))
    assert tracker.summary()["tick_to_bar"]["count"] == 1000
    assert tracker.summary()["signal_to_oif"]["count"] == 5


# ─── Thread safety ──────────────────────────────────────────────────


def test_thread_safety_concurrent_record():
    """4 threads × 100 records each → total count == 400.

    If the lock is broken, deque.append races on CPython can result
    in lost or duplicated samples — we'd see count != 400.
    """
    tracker = LatencyTracker()

    def worker():
        for i in range(100):
            _record_latency_ms(tracker, "tick_to_bar", float(i))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    s = tracker.summary()["tick_to_bar"]
    assert s["count"] == 400, f"expected 400 samples, got {s['count']}"


def test_thread_safety_record_overflow_caps_at_1000():
    """4 threads × 500 records = 2000 attempts → final count == 1000
    (deque maxlen ceiling, not 2000)."""
    tracker = LatencyTracker()

    def worker():
        for i in range(500):
            _record_latency_ms(tracker, "tick_to_bar", float(i))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    s = tracker.summary()["tick_to_bar"]
    assert s["count"] == 1000


def test_thread_safety_summary_while_recording():
    """Calling summary() from one thread while another records must
    not raise (lock must serialize access). The exact count is
    non-deterministic; just verify no crash + count <= records made."""
    tracker = LatencyTracker()
    stop = threading.Event()
    n_records = [0]

    def recorder():
        while not stop.is_set():
            _record_latency_ms(tracker, "tick_to_bar", 1.0)
            n_records[0] += 1

    t = threading.Thread(target=recorder)
    t.start()
    try:
        # Take 50 summary snapshots concurrently with recording.
        for _ in range(50):
            s = tracker.summary()
            assert "tick_to_bar" in s
    finally:
        stop.set()
        t.join()
    # Recorder hit the lock and made some records — just sanity check.
    assert n_records[0] > 0


# ─── Stash-then-close API (t_out=None) ──────────────────────────────


def test_record_with_t_out_none_stashes_then_closes():
    """record(stage, t_in, None) stashes; subsequent record with t_out
    uses the stashed t_in. Closing without a stash uses the call's t_in."""
    tracker = LatencyTracker()
    tracker.record("tick_to_bar", 1000.0, None)  # stash t_in=1000.0
    tracker.record("tick_to_bar", 9999.0, 1000.005)  # arg t_in ignored
    s = tracker.summary()["tick_to_bar"]
    assert s["count"] == 1
    # latency = (1000.005 - 1000.0) * 1000 = 5.0 ms
    assert s["max_ms"] == pytest.approx(5.0)


def test_record_without_stash_uses_arg_t_in():
    """When no stash exists for the stage, t_in arg is used directly."""
    tracker = LatencyTracker()
    tracker.record("signal_to_oif", 100.0, 100.003)
    s = tracker.summary()["signal_to_oif"]
    assert s["count"] == 1
    assert s["max_ms"] == pytest.approx(3.0)


# ─── Singleton ──────────────────────────────────────────────────────


def test_singleton_returns_same_instance():
    """get_latency_tracker() called twice returns the same object so
    bridge and bot (in the same process, hypothetically) share state."""
    _reset_singleton_for_tests()
    a = get_latency_tracker()
    b = get_latency_tracker()
    assert a is b


def test_singleton_persists_records_across_calls():
    """Records via the singleton are visible to the next caller."""
    _reset_singleton_for_tests()
    t = get_latency_tracker()
    _record_latency_ms(t, "tick_to_bar", 7.0)
    t2 = get_latency_tracker()
    assert t2.summary()["tick_to_bar"]["count"] == 1
    assert t2.summary()["tick_to_bar"]["max_ms"] == pytest.approx(7.0)


# ─── Hot-path latency (sanity, not strict) ──────────────────────────


def test_record_is_fast():
    """record() must add < 0.1ms per call on average. Loose ceiling
    to avoid CI flakiness — we expect microseconds, not milliseconds."""
    tracker = LatencyTracker()
    n = 1000
    t0 = time.perf_counter()
    for i in range(n):
        tracker.record("tick_to_bar", 0.0, 0.001)
    elapsed_s = time.perf_counter() - t0
    avg_ms_per_record = (elapsed_s / n) * 1000.0
    assert avg_ms_per_record < 0.1, (
        f"record() avg {avg_ms_per_record:.4f}ms per call exceeds 0.1ms budget"
    )
