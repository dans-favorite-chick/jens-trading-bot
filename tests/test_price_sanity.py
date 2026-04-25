"""Tests for core.price_sanity — the post-2026-04-24 defensive layer.

Covers:
  * Warmup behavior (first N ticks accepted unconditionally).
  * Tick rejection on large deviation from rolling median.
  * Order rejection on deviation from last accepted tick.
  * Stale-tick path (last tick older than 30s → falls back to external ref).
  * No-reference path (rejects conservatively).
"""

from __future__ import annotations

import time

import pytest

from core import price_sanity as ps


@pytest.fixture(autouse=True)
def _reset_sanity():
    """Each test gets a clean sanity state."""
    ps.reset()
    ps.set_thresholds(tick_pct=0.10, order_pct=0.02, warmup_n=10)
    yield
    ps.reset()


def _warmup(base: float = 27200.0, n: int = 10):
    for i in range(n):
        ok, _ = ps.check_tick(base + i)
        assert ok is True
        ps.record_accepted(base + i)


class TestTickIngress:
    def test_warmup_accepts_all(self):
        # First `warmup_n` ticks accepted unconditionally, even if wild.
        for price in [27200, 7100, 50000, 27250]:
            ok, why = ps.check_tick(price)
            assert ok is True, f"warmup should accept {price}, got {why}"
            ps.record_accepted(price)

    def test_post_warmup_rejects_big_deviation(self):
        _warmup()
        ok, why = ps.check_tick(7150)
        assert ok is False
        assert "deviation" in why
        assert "10.0%" in why

    def test_post_warmup_accepts_normal_move(self):
        _warmup()
        # +6% move — noisy but plausible session range
        ok, _ = ps.check_tick(28820)
        assert ok is True

    def test_non_positive_rejected(self):
        ok, why = ps.check_tick(0)
        assert ok is False
        assert "non_positive" in why
        ok, why = ps.check_tick(-100)
        assert ok is False

    def test_rejection_counter_increments(self):
        _warmup()
        ps.record_rejected(7150, "test_reason")
        snap = ps.snapshot()
        assert snap["ticks_rejected"] == 1
        assert "test_reason" in snap["last_rejection_reason"]


class TestOrderGuard:
    def test_good_order_within_tolerance(self):
        _warmup()
        ok, _ = ps.check_order_price(27430, "entry")
        assert ok is True

    def test_corrupt_order_rejected(self):
        _warmup()
        ok, why = ps.check_order_price(7150, "entry")
        assert ok is False
        assert "deviation" in why
        assert "2.0%" in why

    def test_order_slightly_out_of_tolerance(self):
        _warmup()
        # 2.5% off — over the 2% order threshold
        ok, _ = ps.check_order_price(27200 * 1.025, "entry")
        assert ok is False

    def test_stale_tick_falls_back_to_external(self):
        _warmup()
        # Make the last accepted tick look stale by backdating.
        ps._state.last_accepted_ts = time.time() - 60
        # No external ref yet → reject.
        ok, why = ps.check_order_price(27200, "entry")
        assert ok is False
        assert "no_usable_reference" in why or "deviation" in why
        # Now publish an FMP-style external reference.
        ps.set_external_reference(27180, "fmp:QQQ")
        ok, _ = ps.check_order_price(27200, "entry")
        assert ok is True

    def test_no_reference_rejects(self):
        # Fresh state, no warmup, no external.
        ok, why = ps.check_order_price(27200, "entry")
        assert ok is False
        assert "no_usable_reference" in why


class TestExternalReference:
    def test_external_ref_recorded_in_snapshot(self):
        ps.set_external_reference(27300, source="fmp:QQQ")
        snap = ps.snapshot()
        assert snap["external_ref_price"] == 27300
        assert snap["external_ref_source"] == "fmp:QQQ"

    def test_external_ref_rejects_nonsense(self):
        ps.set_external_reference(0, "fmp:QQQ")
        ps.set_external_reference(-5, "fmp:QQQ")
        assert ps.snapshot()["external_ref_price"] == 0.0


class TestRecoveryScenario:
    """Replay the 2026-04-24 incident: alternating real + corrupt ticks.

    Validates that once the guard is in place, a stream that oscillates
    between ~27,175 and ~7,175 will have the corrupt half rejected so
    downstream last_price / VWAP / signals stay clean.
    """

    def test_april_24_incident_replay(self):
        # Warmup from good ticks.
        _warmup(base=27160, n=10)
        real = [27163.75, 27175.50, 27175.00, 27340.75, 27296.50, 27294.75]
        corrupt = [7171.00, 7158.00, 7151.25, 7167.00, 7175.00]

        accepted = 0
        rejected = 0
        for p in real + corrupt:
            ok, why = ps.check_tick(p)
            if ok:
                ps.record_accepted(p)
                accepted += 1
            else:
                ps.record_rejected(p, why)
                rejected += 1

        # Every real price accepted, every corrupt one rejected.
        assert accepted == len(real)
        assert rejected == len(corrupt)

        # Last accepted price should be a real one, not a corrupted one.
        assert ps.snapshot()["last_accepted_price"] >= 27000
