"""
Finding D — serialization tests: initial_stop_price survives close and scale-out.

Pins the invariant added by the 2026-06-02 audit:
  - close_position() must emit BOTH stop_price (current/trailed) AND
    initial_stop_price (immutable R reference set at open).
  - scale_out_partial() must emit initial_stop_price in the partial
    trade dict.

Run: pytest tests/test_position_manager_initial_stop_serialization.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from core.position_manager import PositionManager


# ─── shared helper ────────────────────────────────────────────────────────────

def _open(pm: PositionManager,
          trade_id: str = "t1",
          direction: str = "LONG",
          entry: float = 20100.0,
          stop: float = 20080.0,
          target: float = 20140.0,
          contracts: int = 1) -> bool:
    return pm.open_position(
        trade_id=trade_id,
        direction=direction,
        entry_price=entry,
        contracts=contracts,
        stop_price=stop,
        target_price=target,
        strategy="bias_momentum",
        reason="test",
        market_snapshot={},
    )


# ─── Finding D / test 1: close_position emits initial_stop_price after trail ──

class TestClosePositionEmitsInitialStopPrice:

    def test_close_position_emits_initial_stop_price_after_trail(self):
        """Trailed stop must NOT overwrite the immutable R reference.

        After open (initial stop = 20080) we simulate a stop trail to
        20095 by directly mutating pos.stop_price. close_position()
        must return a trade dict where:
          - stop_price == 20095.0  (the trailed value)
          - initial_stop_price == 20080.0  (the original, frozen at open)
          - they differ, proving the two fields carry independent data
        """
        pm = PositionManager()
        assert _open(pm, trade_id="t1", stop=20080.0)

        # Simulate a trail — mutate the live stop directly.
        pos = pm._positions["t1"]
        pos.stop_price = 20095.0

        trade = pm.close_position(
            exit_price=20120.0,
            exit_reason="test_exit",
            trade_id="t1",
        )

        assert trade is not None, "close_position() returned None — position lookup failed"
        assert "initial_stop_price" in trade, (
            "trade dict is missing 'initial_stop_price' — Finding D fix not applied"
        )
        assert "stop_price" in trade, "trade dict missing 'stop_price'"

        assert trade["stop_price"] == pytest.approx(20095.0), (
            f"stop_price should reflect the trailed value; got {trade['stop_price']}"
        )
        assert trade["initial_stop_price"] == pytest.approx(20080.0), (
            f"initial_stop_price should equal the original stop; got {trade['initial_stop_price']}"
        )
        assert trade["stop_price"] != trade["initial_stop_price"], (
            "stop_price and initial_stop_price are equal — either the trail "
            "wasn't recorded, or the wrong field was frozen"
        )


# ─── Finding D / test 2: scale_out_partial emits initial_stop_price ───────────

class TestScaleOutPartialEmitsInitialStopPrice:

    def test_scale_out_partial_emits_initial_stop_price(self):
        """Partial trade record must carry initial_stop_price.

        scale_out_partial() builds its own trade dict independently of
        close_position(). Both must carry the immutable R reference.

        Open 2 contracts with stop=20080. Scale out 1 contract. Assert
        the returned partial_trade dict contains initial_stop_price==20080.
        """
        pm = PositionManager()
        assert _open(pm, trade_id="t2", stop=20080.0, contracts=2)

        partial_trade = pm.scale_out_partial(
            exit_price=20115.0,
            n_contracts=1,
            exit_reason="scale_out_test",
            trade_id="t2",
        )

        assert partial_trade is not None, (
            "scale_out_partial() returned None — position lookup or guard failed"
        )
        assert "initial_stop_price" in partial_trade, (
            "partial_trade dict is missing 'initial_stop_price' — "
            "Finding D fix not applied to scale_out_partial()"
        )
        assert partial_trade["initial_stop_price"] == pytest.approx(20080.0), (
            f"initial_stop_price in partial_trade should equal entry stop 20080.0; "
            f"got {partial_trade['initial_stop_price']}"
        )
        # Position should still be open with 1 contract remaining.
        assert "t2" in pm._positions, "Position should still be open after partial scale-out"
        assert pm._positions["t2"].contracts == 1, (
            "Remaining contracts should be 1 after scaling out 1 of 2"
        )
