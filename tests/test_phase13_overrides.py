"""
tests/test_phase13_overrides.py — Phase 13 wiring regression tests
====================================================================

2026-05-20 (Phase 13 Ship Audit Pt 2, B-007):

The Phase 13 ship audit on this date added critical wiring that had
ZERO test coverage prior to this file:

  1. `_apply_phase13_overrides()` (bots/base_bot.py) — sets entry_type,
     entry_mode, and tags signal for deferred target recompute when
     entry/stop prices aren't yet set on the Signal.

  2. `recompute_phase13_target()` — called from the trade execution path
     AFTER local stop_price + entry_price are computed; produces the
     correct Phase 13 target_price that the silent-no-op bug previously
     missed.

  3. `_PolicyPosAdapter` + `_PolicyBarAdapter` — map Position/bar field
     names to the names ChandelierPolicy.should_exit / TimeExitPolicy
     .should_exit expect.

Today's spring_setup 1.5R-vs-3R bug (commit a03086e fix) would have
been caught by these tests if they had existed. Adding them now
prevents the next refactor from silently breaking the same path.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bots.base_bot import (
    _apply_phase13_overrides,
    recompute_phase13_target,
    _PolicyPosAdapter,
    _PolicyBarAdapter,
)
from core.exit_policies import PHASE_13_EXIT_ASSIGNMENTS


# ─── _apply_phase13_overrides ─────────────────────────────────────


def test_apply_overrides_sets_entry_mode_for_retest_strategy():
    """Retest-tagged strategies get signal.entry_mode = 'retest' even
    though the wait-loop isn't implemented yet (W-005)."""
    sig = SimpleNamespace(strategy="spring_setup", direction="SHORT")
    _apply_phase13_overrides(sig)
    assert getattr(sig, "entry_mode", None) == "retest"


def test_apply_overrides_sets_entry_mode_first_touch_for_default():
    """Non-retest strategies get entry_mode='first_touch'."""
    sig = SimpleNamespace(strategy="vwap_pullback_v2", direction="LONG")
    _apply_phase13_overrides(sig)
    assert getattr(sig, "entry_mode", None) == "first_touch"


def test_apply_overrides_sets_entry_type_limit_for_limit_5s():
    """limit_5s strategies get entry_type='LIMIT'."""
    sig = SimpleNamespace(
        strategy="g_inside_bar_breakout", direction="LONG",
        entry_type="MARKET",
    )
    _apply_phase13_overrides(sig)
    assert getattr(sig, "entry_type", None) == "LIMIT"


def test_apply_overrides_flags_deferred_target_when_prices_missing():
    """The whole point of the deferred-recompute mechanism: when
    Signal has no entry_price/stop_price (most strategies emit prices
    later), the override step 2 should set _phase13_target_deferred=True
    rather than silently bail."""
    sig = SimpleNamespace(strategy="spring_setup", direction="SHORT")
    # NO entry_price / stop_price on this signal
    _apply_phase13_overrides(sig)
    assert getattr(sig, "_phase13_target_deferred", False) is True


def test_apply_overrides_overrides_target_when_prices_set_at_emit():
    """If Signal already has entry_price + stop_price (some strategies
    pre-compute them, e.g. ORB), the override should apply IMMEDIATELY
    and NOT set the deferred tag."""
    sig = SimpleNamespace(
        strategy="spring_setup", direction="LONG",
        entry_price=24000.0, stop_price=23990.0, target_price=24015.0,
    )
    _apply_phase13_overrides(sig)
    # spring_setup → fixed_rr(rr=3.0) → target = entry + 3 * 10 = 24030
    assert sig.target_price == 24030.0
    # AND the deferred tag should NOT be set (we already applied)
    assert getattr(sig, "_phase13_target_deferred", False) is False


def test_apply_overrides_silent_noop_for_unmapped_strategy():
    """Strategies not in PHASE_13_EXIT_ASSIGNMENTS pass through untouched."""
    sig = SimpleNamespace(
        strategy="not_a_phase13_strategy", direction="LONG",
        entry_price=100.0, stop_price=95.0, target_price=110.0,
    )
    _apply_phase13_overrides(sig)
    # Untouched
    assert sig.target_price == 110.0


# ─── recompute_phase13_target ─────────────────────────────────────


def test_recompute_spring_setup_3r_long():
    """spring_setup → fixed_rr(rr=3.0) → target = entry + 3 * stop_dist."""
    new_target = recompute_phase13_target(
        "spring_setup", "LONG",
        entry_price=24000.0, stop_price=23990.0,
    )
    # stop_dist = 10, 3R = 30, target = 24000 + 30 = 24030
    assert new_target == 24030.0


def test_recompute_spring_setup_3r_short():
    """spring_setup SHORT: target = entry - 3R."""
    new_target = recompute_phase13_target(
        "spring_setup", "SHORT",
        entry_price=24000.0, stop_price=24010.0,
    )
    # stop_dist = 10, target = 24000 - 30 = 23970
    assert new_target == 23970.0


def test_recompute_bias_momentum_2r():
    """bias_momentum → fixed_rr(rr=2.0)."""
    new_target = recompute_phase13_target(
        "bias_momentum", "LONG",
        entry_price=24000.0, stop_price=23990.0,
    )
    # stop_dist = 10, 2R = 20, target = 24020
    assert new_target == 24020.0


def test_recompute_unknown_strategy_returns_none():
    """Strategies not in PHASE_13_EXIT_ASSIGNMENTS should return None."""
    new_target = recompute_phase13_target(
        "not_a_phase13_strategy", "LONG", 100.0, 95.0,
    )
    assert new_target is None


def test_recompute_chandelier_returns_wide_placeholder():
    """ChandelierPolicy.compute_initial_target returns entry ± 10*stop_dist
    (per the 'effectively no target' design — chandelier exits via trail)."""
    new_target = recompute_phase13_target(
        "g_inside_bar_breakout", "LONG",
        entry_price=24000.0, stop_price=23994.0,
    )
    # stop_dist = 6, 10R = 60, target = 24060
    assert new_target == 24060.0


# ─── _PolicyPosAdapter ────────────────────────────────────────────


def test_pos_adapter_maps_field_names():
    """Adapter maps Position.initial_stop_price → pos.initial_stop and
    Position.entry_time → pos.entry_ts."""
    real_pos = SimpleNamespace(
        entry_price=24000.0,
        initial_stop_price=23990.0,
        stop_price=23985.0,  # different from initial_stop_price
        direction="LONG",
        entry_time=1716200000.0,  # epoch seconds
    )
    adapter = _PolicyPosAdapter(real_pos)
    assert adapter.entry_price == 24000.0
    assert adapter.initial_stop == 23990.0   # NOT stop_price
    assert adapter.direction == "LONG"
    assert adapter.entry_ts == 1716200000.0


def test_pos_adapter_falls_back_to_stop_price_when_initial_stop_zero():
    """B-008 fix: if initial_stop_price == 0.0 (Position reconstructed
    from disk without __post_init__), fall back to stop_price."""
    real_pos = SimpleNamespace(
        entry_price=24000.0,
        initial_stop_price=0.0,   # the bug-triggering value
        stop_price=23990.0,
        direction="LONG",
        entry_time=1716200000.0,
    )
    adapter = _PolicyPosAdapter(real_pos)
    # Should fall back to stop_price=23990.0, not stay at 0.0
    assert adapter.initial_stop == 23990.0


def test_pos_adapter_policy_state_is_persistent_dict():
    """policy_state mutations on the adapter should persist on the
    real position (so per-bar policy state survives across calls)."""
    real_pos = SimpleNamespace(
        entry_price=24000.0,
        initial_stop_price=23990.0,
        stop_price=23990.0,
        direction="LONG",
        entry_time=1716200000.0,
    )
    a1 = _PolicyPosAdapter(real_pos)
    a1.policy_state["test_key"] = "test_value"
    # Reconstruct adapter — should see the same state
    a2 = _PolicyPosAdapter(real_pos)
    assert a2.policy_state.get("test_key") == "test_value"


# ─── _PolicyBarAdapter ────────────────────────────────────────────


def test_bar_adapter_converts_datetime_end_time_to_epoch():
    """TimeExitPolicy.should_exit subtracts end_time from entry_ts;
    both must be the same type (float epoch seconds)."""
    from datetime import datetime, timezone
    fake_bar = SimpleNamespace(
        high=24050.0, low=23990.0, close=24025.0,
        end_time=datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc),
    )
    adapter = _PolicyBarAdapter(fake_bar)
    assert isinstance(adapter.end_time, float)
    # The actual conversion: any reasonable epoch should be > 1.7B (2024+).
    # We're not asserting an exact value — just confirming it round-tripped
    # to a sensible float.
    assert adapter.end_time > 1_700_000_000  # > Sept 2023
    assert adapter.end_time < 2_000_000_000  # < May 2033


def test_bar_adapter_passes_through_float_end_time():
    """If bar.end_time is already a float, keep it as-is."""
    fake_bar = SimpleNamespace(
        high=24050.0, low=23990.0, close=24025.0,
        end_time=1779373800.0,
    )
    adapter = _PolicyBarAdapter(fake_bar)
    assert adapter.end_time == 1779373800.0
