"""MAE/MFE/R-Multiple tracking (#2, 2026-05-13).

Sweeney's Maximum Adverse Excursion methodology (Campaign Trading, 1996):
- MAE = worst intra-trade price against the position
- MFE = best intra-trade price for the position
- mfe_capture_pct = realized profit / max favorable move (was money left on table?)
- r_multiple = realized P&L / initial R (Van Tharp framework)

All four are now persisted per closed trade so future strategy analysis
can answer "did we leave money on the table?" and "was the initial stop
calibrated to the actual MAE of winners?"
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _long():
    from core.position_manager import Position
    return Position(
        trade_id="t", direction="LONG", entry_price=100.0, entry_time=0,
        contracts=1, stop_price=90.0, target_price=120.0,
        strategy="x", reason="r", market_snapshot={},
        initial_stop_price=90.0,
    )


def _short():
    from core.position_manager import Position
    return Position(
        trade_id="t", direction="SHORT", entry_price=100.0, entry_time=0,
        contracts=1, stop_price=110.0, target_price=80.0,
        strategy="x", reason="r", market_snapshot={},
        initial_stop_price=110.0,
    )


# ── MAE/MFE basics ─────────────────────────────────────────────────────

def test_mae_mfe_uninitialized_returns_zero():
    pos = _long()
    assert pos.mae_ticks == 0.0
    assert pos.mfe_ticks == 0.0


def test_long_mae_tracks_lowest_price():
    pos = _long()
    pos.update_mae_mfe(99.5)
    pos.update_mae_mfe(101.0)
    pos.update_mae_mfe(98.0)   # new low
    pos.update_mae_mfe(99.0)
    assert pos.mae_price == 98.0
    # MAE in ticks: (entry - mae) / 0.25 = (100 - 98) / 0.25 = 8
    assert pos.mae_ticks == 8.0


def test_long_mfe_tracks_highest_price():
    pos = _long()
    pos.update_mae_mfe(101.0)
    pos.update_mae_mfe(103.5)   # new high
    pos.update_mae_mfe(102.0)
    assert pos.mfe_price == 103.5
    # MFE in ticks: (mfe - entry) / 0.25 = (103.5 - 100) / 0.25 = 14
    assert pos.mfe_ticks == 14.0


def test_short_mae_tracks_highest_price():
    """For SHORT positions, MAE is the HIGHEST adverse price."""
    pos = _short()
    pos.update_mae_mfe(99.0)
    pos.update_mae_mfe(102.0)   # adverse for SHORT
    pos.update_mae_mfe(101.0)
    assert pos.mae_price == 102.0
    assert pos.mae_ticks == 8.0  # (102 - 100) / 0.25


def test_short_mfe_tracks_lowest_price():
    """For SHORT positions, MFE is the LOWEST favorable price."""
    pos = _short()
    pos.update_mae_mfe(99.0)
    pos.update_mae_mfe(96.5)   # favorable for SHORT
    pos.update_mae_mfe(98.0)
    assert pos.mfe_price == 96.5
    assert pos.mfe_ticks == 14.0  # (100 - 96.5) / 0.25


def test_first_tick_seeds_to_entry_price():
    """Without an update yet, mae/mfe should equal entry once first tick fires."""
    pos = _long()
    pos.update_mae_mfe(100.0)  # exactly at entry
    assert pos.mae_price == 100.0
    assert pos.mfe_price == 100.0
    assert pos._mae_mfe_initialized is True


def test_mae_mfe_persisted_in_close_trade_record():
    """When PositionManager.close_position fires, the trade record must
    carry mae_price/mfe_price/mae_ticks/mfe_ticks/r_distance/
    mfe_capture_pct/r_multiple fields."""
    from core.position_manager import PositionManager
    pm = PositionManager()
    pm.open_position(
        trade_id="closer", direction="LONG", entry_price=100.0,
        contracts=1, stop_price=90.0, target_price=120.0,
        strategy="x", reason="t",
    )
    pos = pm.get_position("closer")
    # Simulate price movement
    pos.update_mae_mfe(99.0)
    pos.update_mae_mfe(102.0)
    pos.update_mae_mfe(98.0)   # MAE low
    pos.update_mae_mfe(105.0)  # MFE high
    pos.update_mae_mfe(103.0)  # exit price
    pm.close_position(exit_price=103.0, exit_reason="test")
    last = pm.trade_history[-1]
    assert last["mae_price"] == 98.0
    assert last["mfe_price"] == 105.0
    assert last["mae_ticks"] == 8.0  # (100 - 98) / 0.25
    assert last["mfe_ticks"] == 20.0  # (105 - 100) / 0.25
    assert last["r_distance"] == 10.0  # |100 - 90|
    assert "r_multiple" in last
    assert "mfe_capture_pct" in last
