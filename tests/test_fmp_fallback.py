"""Tests for the FMP fallback-mode feature added 2026-04-24."""

from __future__ import annotations

import pytest

from core import price_sanity as ps


@pytest.fixture(autouse=True)
def _reset():
    ps.reset()
    ps.set_thresholds(tick_pct=0.10, order_pct=0.02, warmup_n=5)
    yield
    ps.reset()


def _warmup(base=27200.0, n=5):
    for i in range(n):
        ok, _ = ps.check_tick(base + i)
        assert ok
        ps.record_accepted(base + i)


class TestModeFlips:
    def test_initial_mode_is_local_primary(self):
        assert ps.current_mode() == "local_primary"

    def test_single_divergence_does_not_flip(self):
        _warmup()
        # One bad poll → still local_primary (_DIVERGENT_POLLS_TO_FLIP = 2)
        result = ps.record_fmp_check(
            local_price=27200, fmp_price=26500,
            deviation_pct=0.025, threshold_pct=0.015,
        )
        assert result is None
        assert ps.current_mode() == "local_primary"

    def test_two_consecutive_divergences_flip_to_fmp(self):
        _warmup()
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        result = ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        assert result == "fmp_primary"
        assert ps.current_mode() == "fmp_primary"

    def test_heal_back_to_local_after_five_agrees(self):
        _warmup()
        # Force into fmp mode
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        assert ps.current_mode() == "fmp_primary"
        # Five agrees in a row
        for _ in range(5):
            ps.record_fmp_check(27200, 27195, 0.0002, 0.015)
        assert ps.current_mode() == "local_primary"

    def test_agree_after_one_divergence_resets_counter(self):
        _warmup()
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)  # 1 bad
        ps.record_fmp_check(27200, 27195, 0.0002, 0.015)  # resets
        result = ps.record_fmp_check(27200, 26500, 0.025, 0.015)  # back to 1 bad
        assert result is None
        assert ps.current_mode() == "local_primary"


class TestOrderBlocking:
    def test_order_blocked_in_fmp_primary_mode(self):
        _warmup()
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        assert ps.current_mode() == "fmp_primary"
        ok, why = ps.check_order_price(27200, "entry")
        assert not ok
        assert "fmp_primary_mode" in why

    def test_stop_target_checks_use_fmp_in_fmp_mode(self):
        _warmup()
        # Push to fmp mode with a bad local but valid external ref.
        ps.set_external_reference(27195, source="fmp:QQQ")
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        # Stop within 2% of FMP ref → allowed even though entry would be blocked.
        ok, why = ps.check_order_price(27100, "stop")
        assert ok, f"stop should be allowed vs FMP ref, got {why}"

    def test_order_allowed_after_heal(self):
        _warmup()
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        for _ in range(5):
            ps.record_fmp_check(27200, 27195, 0.0002, 0.015)
        assert ps.current_mode() == "local_primary"
        # Seed a fresh tick so order guard has a reference
        ok, _ = ps.check_tick(27200)
        assert ok
        ps.record_accepted(27200)
        ok, why = ps.check_order_price(27200, "entry")
        assert ok, f"entry should resume after heal, got {why}"


class TestSnapshotSurface:
    def test_snapshot_exposes_mode_fields(self):
        _warmup()
        snap = ps.snapshot()
        for key in ("mode", "mode_since_s", "mode_flips",
                    "consecutive_fmp_divergent", "consecutive_fmp_agree"):
            assert key in snap, f"missing snapshot key {key}"

    def test_mode_flips_increments(self):
        _warmup()
        assert ps.snapshot()["mode_flips"] == 0
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        ps.record_fmp_check(27200, 26500, 0.025, 0.015)
        assert ps.snapshot()["mode_flips"] == 1
