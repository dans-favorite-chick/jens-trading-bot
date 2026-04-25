"""Tests for core.bridge.stream_validator (Phase B+ Section 1).

Each of the three signals (static band, cross-client MAD, tick grid)
exercised independently + a combined replay of the 2026-04-24 incident.
"""

from __future__ import annotations

import pytest

from core.bridge.stream_validator import (
    StreamValidator,
    get_validator,
)


@pytest.fixture
def sv() -> StreamValidator:
    """Fresh validator with explicit thresholds for deterministic tests."""
    return StreamValidator(
        window_n=30,
        mad_threshold_pct=0.05,
        default_tick_size=0.25,
        quarantine_after_n_rejects=3,
    )


# ─── Signal 1: Static price band ────────────────────────────────────

class TestStaticBand:
    def test_in_band_mnq_accepted(self, sv: StreamValidator) -> None:
        # 27432.50 is within MNQ band (15000..35000) and on the 0.25 grid.
        assert sv.on_tick(port=55117, instrument="MNQM6", price=27432.50) is True

    def test_out_of_band_mnq_rejected(self, sv: StreamValidator) -> None:
        # 7,000 is the rogue-stream price class from the 2026-04-24 incident.
        # MNQ band min is 15,000 — must be rejected.
        assert sv.on_tick(port=55779, instrument="MNQM6", price=7000.0) is False
        assert "static band" in (sv.quarantine_reason(55779) or "")

    def test_es_and_mes_independent_bands(self, sv: StreamValidator) -> None:
        # ES band is 4000..8000. 5000 is in band; 14000 is way above
        # (and would be in MNQ band, but ES is what matters here).
        assert sv.on_tick(port=1, instrument="ESM6", price=5000.00) is True
        assert sv.on_tick(port=2, instrument="ESM6", price=14000.00) is False
        # MES band is 4000..8000 too. 6000 is in band.
        assert sv.on_tick(port=3, instrument="MESM6", price=6000.00) is True


# ─── Signal 2: Cross-client MAD ─────────────────────────────────────

class TestCrossClientMAD:
    def test_single_client_always_passes_mad(self, sv: StreamValidator) -> None:
        # With only one connected port, there are no peers — MAD signal
        # bootstraps as "always pass". (Static band still applies, so we
        # use an in-band price.)
        for _ in range(10):
            assert sv.on_tick(port=99, instrument="MNQM6", price=27432.50) is True

    def test_two_clients_agreeing_both_pass(self, sv: StreamValidator) -> None:
        # Seed two clients with consistent MNQ prices in the same band.
        # All ticks should be accepted; neither port should be quarantined.
        for i in range(8):
            assert sv.on_tick(
                port=10, instrument="MNQM6", price=27430.00 + i * 0.25
            ) is True
            assert sv.on_tick(
                port=11, instrument="MNQM6", price=27430.50 + i * 0.25
            ) is True
        assert sv.is_quarantined(10) is False
        assert sv.is_quarantined(11) is False

    def test_diverging_client_rejected(self, sv: StreamValidator) -> None:
        # Use a band-bypass instrument so MAD is the SOLE signal that fires.
        # ``XYZM6`` has no entry in the bands table, so static-band
        # passes. Tick-grid uses default 0.25 throughout. Then a peer
        # streams ~10000 while the diverging client streams ~9300 —
        # 7% drift, above the 5% MAD threshold.
        for i in range(6):
            assert sv.on_tick(
                port=20, instrument="XYZM6", price=10000.00 + i * 0.25
            ) is True
        # Diverging client: ~7% below peer median. Static band silent
        # (no XYZ band), tick-grid passes (multiple of 0.25), MAD trips.
        assert sv.on_tick(port=21, instrument="XYZM6", price=9300.00) is False
        reason = sv.quarantine_reason(21) or ""
        assert "MAD" in reason or "cross-client" in reason


# ─── Signal 3: Tick-grid alignment ──────────────────────────────────

class TestTickGrid:
    def test_aligned_price_accepted(self, sv: StreamValidator) -> None:
        # 27432.25 / 0.25 = 109729.0 exactly — clean tick.
        assert sv.on_tick(port=1, instrument="MNQM6", price=27432.25) is True

    def test_off_grid_rejected(self, sv: StreamValidator) -> None:
        # 27432.13 is NOT a multiple of 0.25 — must be rejected.
        assert sv.on_tick(port=1, instrument="MNQM6", price=27432.13) is False
        assert "tick-grid" in (sv.quarantine_reason(1) or "")

    def test_custom_tick_size(self, sv: StreamValidator) -> None:
        # When a caller declares a different tick size (e.g. 0.10),
        # 27432.10 becomes a clean tick.
        assert sv.on_tick(
            port=1, instrument="MNQM6", price=27432.10, tick_size=0.10
        ) is True


# ─── Combined: April 24 incident replay ─────────────────────────────

class TestCombinedAprilReplay:
    def test_alternating_real_and_corrupt_ticks(self) -> None:
        """Replay the 2026-04-24 incident: alternating real (~27432)
        and corrupt (~7192) ticks both labeled MNQM6. Only the real
        ones should be accepted; the corrupt ones should be rejected
        AND the rogue port quarantined within a few cycles.
        """
        sv = StreamValidator(
            window_n=30,
            mad_threshold_pct=0.05,
            default_tick_size=0.25,
            quarantine_after_n_rejects=3,
        )
        real_port = 55117
        rogue_port = 55779

        real_results: list[bool] = []
        rogue_results: list[bool] = []
        for i in range(8):
            real_results.append(
                sv.on_tick(port=real_port, instrument="MNQM6",
                           price=27432.50 + (i % 4) * 0.25)
            )
            rogue_results.append(
                sv.on_tick(port=rogue_port, instrument="MNQM6",
                           price=7192.25 + (i % 4) * 0.25)
            )

        # Every real tick was accepted; every rogue tick rejected.
        assert all(real_results), f"real ticks rejected: {real_results}"
        assert not any(rogue_results), f"rogue ticks accepted: {rogue_results}"

        # Rogue port has been promoted to quarantine.
        assert sv.is_quarantined(rogue_port) is True
        assert sv.is_quarantined(real_port) is False

        # Snapshot exposes the quarantined-since timestamp + reason.
        snap = sv.health_snapshot()
        assert snap["ports"][rogue_port]["quarantined"] is True
        assert snap["ports"][rogue_port]["quarantined_since_ts"] is not None
        assert snap["ports"][rogue_port]["ticks_rejected"] >= 3
        assert snap["ports"][real_port]["ticks_accepted"] == 8


# ─── Module-level singleton ─────────────────────────────────────────

class TestSingleton:
    def test_get_validator_returns_same_instance(self) -> None:
        a = get_validator()
        b = get_validator()
        assert a is b
        # And it's a real StreamValidator with bands loaded
        assert isinstance(a, StreamValidator)
        snap = a.health_snapshot()
        assert "bands_loaded" in snap
