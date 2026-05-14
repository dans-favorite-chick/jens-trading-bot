"""Position anti-mutation invariant (#3, 2026-05-13).

The R-multiple framework (Van Tharp) requires R = abs(entry - initial
stop) to be a FIXED number computed at entry. Today's 8-second
fast-abort bug was a symptom of this invariant being violated:
BE_STOP recomputed stop_dist from a `stop_price` that had already
been mutated by TRAIL, shrinking R to ~1 tick within 1 second of fill.

These tests enforce that mutating `stop_price` after construction
NEVER changes `r_distance`. This prevents the bug class entirely.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _open():
    """Construct a representative LONG position."""
    from core.position_manager import Position
    return Position(
        trade_id="t", direction="LONG", entry_price=100.0, entry_time=0,
        contracts=1, stop_price=90.0, target_price=120.0,
        strategy="x", reason="r", market_snapshot={},
        initial_stop_price=90.0,
    )


def test_r_distance_initial_value():
    """r_distance should equal abs(entry - initial_stop) at construction."""
    pos = _open()
    assert pos.r_distance == 10.0


def test_r_distance_unchanged_when_stop_mutated():
    """The whole point of the invariant — mutating stop_price must NOT
    change r_distance. TRAIL and BE_STOP rely on this."""
    pos = _open()
    assert pos.r_distance == 10.0  # baseline
    # Simulate TRAIL moving the stop close to entry
    pos.stop_price = 99.0
    assert pos.r_distance == 10.0, (
        f"r_distance moved when stop_price was mutated. Expected 10.0, "
        f"got {pos.r_distance}. The TRAIL/BE_STOP family will use this "
        f"to compute R-multiples — if it moves, all downstream R math "
        f"breaks (this was the 2026-05-13 fast-abort bug)."
    )
    # Simulate BE_STOP moving stop above entry (locking in profit)
    pos.stop_price = 105.0
    assert pos.r_distance == 10.0


def test_r_distance_unchanged_when_initial_stop_price_mutated():
    """Even if a caller mistakenly mutates initial_stop_price post-
    construction, r_distance should remain anchored to the FROZEN value
    captured in __post_init__."""
    pos = _open()
    assert pos.r_distance == 10.0
    # Someone misuses the field — should NOT affect R
    pos.initial_stop_price = 50.0
    assert pos.r_distance == 10.0, (
        "r_distance reads from _initial_stop_frozen, not initial_stop_price — "
        "if a buggy caller mutates initial_stop_price (e.g. accidental "
        "reassignment), R must still be anchored to the entry-time value."
    )


def test_fallback_when_initial_stop_unspecified():
    """If a legacy caller constructs a Position WITHOUT initial_stop_price,
    __post_init__ should fall back to stop_price as the R anchor."""
    from core.position_manager import Position
    pos = Position(
        trade_id="legacy", direction="LONG", entry_price=100.0, entry_time=0,
        contracts=1, stop_price=90.0, target_price=120.0,
        strategy="x", reason="r", market_snapshot={},
        # No initial_stop_price set — should fall back to stop_price
    )
    assert pos.r_distance == 10.0  # falls back to stop_price
    assert pos.initial_stop_price == 90.0


def test_open_position_via_manager_sets_invariant():
    """End-to-end: PositionManager.open_position must capture R correctly."""
    from core.position_manager import PositionManager
    pm = PositionManager()
    ok = pm.open_position(
        trade_id="e2e", direction="LONG", entry_price=29592.0,
        contracts=1, stop_price=29562.75, target_price=30141.0,
        strategy="bias_momentum", reason="test",
    )
    assert ok
    pos = pm.get_position("e2e")
    assert pos.r_distance == 29.25  # 117 ticks at 0.25 tick size

    # Now mutate stop_price (simulating TRAIL); r_distance must stay
    pos.stop_price = 29585.0
    assert pos.r_distance == 29.25, (
        "End-to-end r_distance must stay anchored after TRAIL mutates "
        "stop_price. This is the 2026-05-13 fast-abort bug-class fix."
    )


def test_r_distance_zero_for_ill_formed_position():
    """Defensive: a Position with malformed inputs shouldn't crash callers
    that read r_distance — return 0.0 instead."""
    from core.position_manager import Position
    pos = Position(
        trade_id="bad", direction="LONG", entry_price=0.0, entry_time=0,
        contracts=1, stop_price=0.0, target_price=0.0,
        strategy="x", reason="r", market_snapshot={},
    )
    # With entry=stop=0, r_distance is 0; should not raise
    assert pos.r_distance == 0.0
