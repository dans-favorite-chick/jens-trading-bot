"""
WS-C: assert chandelier trail moves the NT8 stop via OIF.

These tests encode the intended behavior after the stop-modify wiring
lands. They are marked xfail against HEAD because the wiring is not
yet implemented — `_trail_stop` and the chandelier update block mutate
`pos.stop_price` in Python only; no OIF is written.

When the fix lands, remove the xfail markers.
"""
from __future__ import annotations

import pytest

from core.chandelier_exit import ChandelierTrailState


class TestChandelierStateRatchets:
    """Pure unit — chandelier trail only moves in favorable direction."""

    def test_long_trail_ratchets_up(self):
        s = ChandelierTrailState(direction="LONG", entry_price=100.0, atr_mult=3.0)
        # bar 1: high=101, atr=1 → trail = 101 - 3 = 98
        s.update(bar_high=101.0, bar_low=99.0, atr=1.0)
        assert s.current_trail == pytest.approx(98.0)
        # bar 2: high=103 → trail = 103 - 3 = 100 (ratcheted up)
        s.update(bar_high=103.0, bar_low=100.0, atr=1.0)
        assert s.current_trail == pytest.approx(100.0)
        # bar 3: high=102 (lower) → trail STAYS at 100 (ratchet rule)
        s.update(bar_high=102.0, bar_low=99.5, atr=1.0)
        assert s.current_trail == pytest.approx(100.0)

    def test_short_trail_ratchets_down(self):
        s = ChandelierTrailState(direction="SHORT", entry_price=100.0, atr_mult=3.0)
        s.update(bar_high=101.0, bar_low=99.0, atr=1.0)
        assert s.current_trail == pytest.approx(102.0)  # 99 + 3
        s.update(bar_high=100.0, bar_low=97.0, atr=1.0)
        assert s.current_trail == pytest.approx(100.0)  # 97 + 3 — ratcheted down
        s.update(bar_high=99.0, bar_low=98.0, atr=1.0)
        assert s.current_trail == pytest.approx(100.0)  # stays

    def test_should_exit_long(self):
        s = ChandelierTrailState(direction="LONG", entry_price=100.0, atr_mult=3.0)
        s.update(bar_high=110.0, bar_low=109.0, atr=1.0)
        assert s.current_trail == pytest.approx(107.0)
        assert not s.should_exit(108.0)
        assert s.should_exit(107.0)
        assert s.should_exit(106.0)


class TestChandelierWritesOIF:
    """Behavior test — asserts the missing OIF wiring.

    xfail(strict=True): these currently raise because write_modify_stop
    does not exist. When implemented, the strict flag flips them to
    XPASS and forces removal of the marker.
    """

    def test_write_modify_stop_exists_in_oif_writer(self):
        from bridge.oif_writer import write_modify_stop  # noqa: F401 — import asserts existence

    def test_chandelier_trail_emits_modify_stop_oif(self):
        """After each ratchet, a cancel+place_stop OIF pair should be queued."""
        from bridge.oif_writer import write_modify_stop
        # Expected signature (per audit doc): direction, new_stop_price,
        # n_contracts, trade_id, account, old_stop_order_id -> list[str]
        paths = write_modify_stop(
            direction="LONG",
            new_stop_price=100.0,
            n_contracts=1,
            trade_id="test_ws_c",
            account="Sim101",
            old_stop_order_id="oif_old",
        )
        assert isinstance(paths, list) and len(paths) >= 1
