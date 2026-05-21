"""
Exit Sprint S1 — Stop/target sanity gate tests.

For each enabled strategy in config/strategies.py, synthesize a minimal
valid Signal and verify _sanity_check_entry accepts it. Also verifies
the gate rejects the two failure modes Jennifer flagged today:
  - LONG with stop above entry (geometry wrong)
  - target_rr=0 producing target==entry (trapped via zero-distance geometry)
"""

from __future__ import annotations

import pytest

from bots.base_bot import _sanity_check_entry
from strategies.base_strategy import Signal


TICK = 0.25


def _make_signal(direction="LONG", strategy="bias_momentum"):
    return Signal(
        direction=direction,
        stop_ticks=40,
        target_rr=2.0,
        confidence=70.0,
        entry_score=50.0,
        strategy=strategy,
        reason="test",
        confluences=[],
    )


# ─── Geometry failures (Jennifer's two flagged bugs) ────────────────────

def test_long_stop_above_entry_rejected():
    """LONG stop ABOVE entry = Jennifer's bias_momentum 600-pt phantom."""
    sig = _make_signal("LONG")
    ok, reason = _sanity_check_entry(sig, entry_price=26834.0,
                                     stop_price=27434.0,   # wrong side
                                     target_price=27000.0)
    assert not ok
    assert "geometry wrong" in reason


def test_short_stop_below_entry_rejected():
    sig = _make_signal("SHORT")
    ok, reason = _sanity_check_entry(sig, entry_price=26834.0,
                                     stop_price=26804.0,   # wrong side
                                     target_price=26600.0)
    assert not ok
    assert "geometry wrong" in reason


def test_target_at_entry_rejected_long():
    """target_rr=0 bug: target lands at entry → commission-loss trade."""
    sig = _make_signal("LONG")
    ok, reason = _sanity_check_entry(sig, entry_price=26834.0,
                                     stop_price=26804.0,
                                     target_price=26834.0)  # == entry
    assert not ok
    assert "geometry wrong" in reason


def test_target_at_entry_rejected_short():
    sig = _make_signal("SHORT")
    ok, reason = _sanity_check_entry(sig, entry_price=26834.0,
                                     stop_price=26864.0,
                                     target_price=26834.0)
    assert not ok


# ─── Distance bounds ────────────────────────────────────────────────────

def test_stop_distance_too_tight_rejected():
    """3t stop is below floor (5t)."""
    sig = _make_signal("LONG")
    ok, reason = _sanity_check_entry(sig, entry_price=26834.0,
                                     stop_price=26834.0 - 3 * TICK,
                                     target_price=26900.0)
    assert not ok
    assert "outside 5-200 range" in reason


def test_stop_distance_too_wide_rejected():
    """250t stop is above the default bracket ceiling (200t)."""
    sig = _make_signal("LONG")
    ok, reason = _sanity_check_entry(sig, entry_price=26834.0,
                                     stop_price=26834.0 - 250 * TICK,
                                     target_price=26900.0)
    assert not ok
    assert "outside 5-200 range" in reason


# ─── Managed-exit stop bound (2026-05-15 fix) ──────────────────────────

def test_managed_exit_allows_wide_stop_up_to_1000t():
    """noise_area's stop is the opposite cone boundary +2t. On wide-cone
    days this exceeds the default 200t bound; today's 11 signals dropped
    at 776t. With is_managed_exit=True the upper bound is 1000t."""
    sig = _make_signal("LONG", strategy="noise_area")
    # 776t stop — today's actual failing case
    ok, reason = _sanity_check_entry(
        sig, entry_price=29629.5,
        stop_price=29629.5 - 776 * TICK,
        target_price=None,
        is_managed_exit=True,
    )
    assert ok, f"managed-exit 776t rejected: {reason}"


def test_managed_exit_still_rejects_over_1000t():
    """The wider bound still exists — a 1500t stop is clearly broken
    even for a managed-exit strategy."""
    sig = _make_signal("LONG", strategy="noise_area")
    ok, reason = _sanity_check_entry(
        sig, entry_price=29629.5,
        stop_price=29629.5 - 1500 * TICK,
        target_price=None,
        is_managed_exit=True,
    )
    assert not ok
    assert "outside 5-1000 range" in reason
    assert "managed mode" in reason


def test_managed_exit_still_rejects_under_5t():
    """Lower bound (5t) applies regardless of mode — too-tight stops
    are noise sweeps no matter the strategy."""
    sig = _make_signal("LONG", strategy="noise_area")
    ok, reason = _sanity_check_entry(
        sig, entry_price=29629.5,
        stop_price=29629.5 - 3 * TICK,
        target_price=None,
        is_managed_exit=True,
    )
    assert not ok
    assert "outside 5-1000 range" in reason


def test_bracket_strategy_unaffected_by_managed_flag():
    """Default (is_managed_exit=False) keeps the original 200t cap so
    ordinary bracket strategies don't accidentally widen their cap."""
    sig = _make_signal("LONG", strategy="bias_momentum")
    ok, reason = _sanity_check_entry(
        sig, entry_price=29629.5,
        stop_price=29629.5 - 250 * TICK,
        target_price=29700.0,
        is_managed_exit=False,
    )
    assert not ok
    assert "5-200" in reason
    assert "bracket mode" in reason


def test_default_keyword_is_bracket_mode():
    """Backward-compat: omitting is_managed_exit keeps the strict 200t cap."""
    sig = _make_signal("LONG", strategy="bias_momentum")
    ok, reason = _sanity_check_entry(
        sig, entry_price=29629.5,
        stop_price=29629.5 - 250 * TICK,
        target_price=29700.0,
    )
    assert not ok
    assert "5-200" in reason


# ─── Managed-exit: target_price=None is tolerated ───────────────────────

def test_managed_exit_none_target_passes():
    """Noise area / ORB set target_price=None. Sanity gate must accept."""
    sig = _make_signal("LONG", "noise_area")
    ok, reason = _sanity_check_entry(sig, entry_price=26834.0,
                                     stop_price=26834.0 - 40 * TICK,
                                     target_price=None)
    assert ok, f"managed exit rejected: {reason}"


# ─── Per-strategy synthetic happy-path ──────────────────────────────────

@pytest.mark.parametrize("strategy,direction,stop_ticks", [
    ("bias_momentum",       "LONG",  120),
    ("bias_momentum",       "SHORT", 120),
    ("spring_setup",        "LONG",   60),
    ("vwap_pullback",       "LONG",   80),
    ("vwap_band_pullback",  "LONG",   60),
    ("compression_breakout","LONG",   50),
    ("noise_area",          "LONG",   40),
    # ("dom_pullback",      "LONG",   60),  # deleted 2026-05-21
    ("ib_breakout",         "LONG",   80),
    ("orb",                 "LONG",   40),
    ("opening_session",     "LONG",   50),
])
def test_each_enabled_strategy_synthetic_signal_passes(strategy, direction, stop_ticks):
    """Every strategy's stop-ticks in its config range produces a valid bracket."""
    sig = Signal(
        direction=direction,
        stop_ticks=stop_ticks,
        target_rr=2.0,
        confidence=70.0,
        entry_score=50.0,
        strategy=strategy,
        reason="synthetic",
        confluences=[],
    )
    entry = 26800.0
    if direction == "LONG":
        stop = entry - stop_ticks * TICK
        target = entry + stop_ticks * TICK * sig.target_rr
    else:
        stop = entry + stop_ticks * TICK
        target = entry - stop_ticks * TICK * sig.target_rr
    ok, reason = _sanity_check_entry(sig, entry, stop, target)
    assert ok, f"{strategy} {direction} {stop_ticks}t failed: {reason}"
