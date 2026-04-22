"""
WS-C: assert BE-move writes a stop-modification OIF to NT8.

Today `_trail_stop` and the rider BE-trigger (base_bot.py ~938) mutate
`pos.stop_price` in Python only. The only path that reaches NT8 is
`_scale_out_trade` -> `write_be_stop`, gated on `original_contracts >= 2`.
Single-contract rider trades never get a real NT8-side stop move.

Tests marked xfail(strict=True) until the wiring lands.
"""
from __future__ import annotations

import pytest


class TestMoveStopToBeStatePure:
    """Unit: move_stop_to_be updates Python state correctly and safely."""

    def test_move_long_stop_up_to_be(self):
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t1", direction="LONG", entry_price=100.0, contracts=1,
            stop_price=98.0, target_price=104.0, strategy="test",
            reason="unit", account="Sim101",
        )
        pm.move_stop_to_be(be_price=100.0, trade_id="t1")
        pos = pm._positions["t1"]
        assert pos.stop_price == 100.0
        assert pos.be_stop_active is True

    def test_move_long_stop_refused_if_would_worsen(self):
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t2", direction="LONG", entry_price=100.0, contracts=1,
            stop_price=99.0, target_price=104.0, strategy="test",
            reason="unit", account="Sim101",
        )
        # Attempt to move BE DOWN to 98 — must be refused (safety rule)
        pm.move_stop_to_be(be_price=98.0, trade_id="t2")
        pos = pm._positions["t2"]
        assert pos.stop_price == 99.0  # unchanged


class TestBEMoveWritesOIF:

    def test_write_modify_stop_signature(self):
        from bridge.oif_writer import write_modify_stop  # noqa: F401

    def test_rider_be_trigger_emits_modify_stop_oif(self):
        """When BE trigger fires, base_bot should write a stop-modify OIF."""
        # Placeholder: the full integration test requires a running base_bot
        # harness. Once write_modify_stop exists, add it here and assert the
        # OIF is written to the incoming folder with action=PLACE_STOP_* and
        # price=entry + 2 ticks.
        from bridge.oif_writer import write_modify_stop
        paths = write_modify_stop(
            direction="LONG",
            new_stop_price=100.0,
            n_contracts=1,
            trade_id="rider_be_test",
            account="Sim101",
            old_stop_order_id="oif_old",
        )
        assert isinstance(paths, list) and len(paths) >= 1
