"""
Phase 4B integration tests: TickAggregator ↔ SessionLevelsAggregator.

Covers:
  - snapshot() includes session levels fields
  - session levels stay None before the market open captures data
  - session levels flow through to snapshot once populated
  - init failure in SessionLevelsAggregator degrades gracefully
  - bar callback dispatch triggers session_levels.update()

Run: pytest tests/test_tick_aggregator.py -v
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.tick_aggregator import TickAggregator


# ─── Helpers ────────────────────────────────────────────────────────
@dataclass
class _FakeBar:
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    tick_count: int = 0
    start_time: float = 0.0
    end_time: float = 0.0


def _fresh_aggregator() -> TickAggregator:
    """TickAggregator with session_levels stubbed to a MagicMock so we
    control what get_levels_dict returns from test to test."""
    a = TickAggregator(bot_name="test_bot")
    a.session_levels = MagicMock()
    a.session_levels.get_levels_dict.return_value = {}
    return a


# ═══════════════════════════════════════════════════════════════════
# Spec test 1: snapshot includes session levels fields
# ═══════════════════════════════════════════════════════════════════
def test_snapshot_includes_session_levels_fields():
    a = _fresh_aggregator()
    a.session_levels.get_levels_dict.return_value = {
        "now_ct": datetime(2026, 4, 21, 8, 35),
        "pivot_pp": 25275.0,
        "prior_day_poc": 25275.0,
        "pmh": 25320.0,
        "rth_open_price": 26300.0,
        "opening_type": "OPEN_DRIVE",
    }
    snap = a.snapshot()
    for key in ("pivot_pp", "prior_day_poc", "pmh", "rth_open_price", "opening_type"):
        assert key in snap, f"missing {key} in snapshot"
    assert snap["opening_type"] == "OPEN_DRIVE"
    assert snap["pivot_pp"] == 25275.0


# ═══════════════════════════════════════════════════════════════════
# Spec test 2: session levels are None before market open
# ═══════════════════════════════════════════════════════════════════
def test_snapshot_session_levels_none_before_market_open():
    a = _fresh_aggregator()
    a.session_levels.get_levels_dict.return_value = {
        "now_ct": datetime(2026, 4, 21, 6, 0),
        "pmh": None,
        "pml": None,
        "rth_open_price": None,
        "rth_5min_high": None,
        "opening_type": None,
    }
    snap = a.snapshot()
    assert snap["pmh"] is None
    assert snap["rth_open_price"] is None
    assert snap["opening_type"] is None


# ═══════════════════════════════════════════════════════════════════
# Spec test 3: session levels populated after 08:35 classify trigger
# ═══════════════════════════════════════════════════════════════════
def test_snapshot_session_levels_populated_after_835():
    a = _fresh_aggregator()
    a.session_levels.get_levels_dict.return_value = {
        "now_ct": datetime(2026, 4, 21, 9, 0),
        "rth_open_price": 26300.0,
        "rth_5min_high": 26325.0,
        "rth_5min_low": 26298.0,
        "rth_5min_close": 26320.0,
        "opening_type": "OPEN_DRIVE",
    }
    snap = a.snapshot()
    assert snap["rth_5min_high"] == 26325.0
    assert snap["opening_type"] == "OPEN_DRIVE"


# ═══════════════════════════════════════════════════════════════════
# Spec test 4: SessionLevelsAggregator init failure is non-fatal
# ═══════════════════════════════════════════════════════════════════
def test_session_levels_init_failure_does_not_crash_aggregator():
    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic init failure")

    # Force the SessionLevelsAggregator ctor to raise so the tick_aggregator
    # wiring must swallow it and fall back to session_levels=None.
    with patch(
        "core.session_levels_aggregator.SessionLevelsAggregator",
        side_effect=_boom,
    ):
        a = TickAggregator(bot_name="test_bot")
        assert a.session_levels is None
        # snapshot() still works — no session-levels keys but no crash.
        snap = a.snapshot()
        assert "price" in snap
        assert "opening_type" not in snap  # never enriched when disabled


# ═══════════════════════════════════════════════════════════════════
# Spec test 5: bar callback dispatch triggers session_levels.update()
# ═══════════════════════════════════════════════════════════════════
def test_on_bar_callback_triggers_session_levels_update():
    a = _fresh_aggregator()
    bar = _FakeBar(open=100, high=105, low=99, close=103,
                   volume=1000, end_time=1745251200)  # 2026-04-21 ~midnight epoch

    # Directly invoke the on-bar-complete dispatcher for a 1m bar.
    a._on_bar_complete("1m", bar)

    a.session_levels.update.assert_called_once()
    kwargs = a.session_levels.update.call_args.kwargs
    assert kwargs["bar_1m"] is bar
    assert kwargs["bar_5m"] is None
    assert isinstance(kwargs["now_ct"], datetime)


# ═══════════════════════════════════════════════════════════════════
# Bonus: 5m bar path
# ═══════════════════════════════════════════════════════════════════
def test_on_bar_callback_passes_5m_bar_correctly():
    a = _fresh_aggregator()
    bar = _FakeBar(open=100, high=105, low=99, close=103, end_time=1745251200)
    a._on_bar_complete("5m", bar)
    kwargs = a.session_levels.update.call_args.kwargs
    assert kwargs["bar_5m"] is bar
    assert kwargs["bar_1m"] is None


# ═══════════════════════════════════════════════════════════════════
# Bonus: update error is logged but does not break the callback chain
# ═══════════════════════════════════════════════════════════════════
def test_session_levels_update_error_does_not_propagate():
    a = _fresh_aggregator()
    a.session_levels.update.side_effect = RuntimeError("boom")
    cb = MagicMock()
    a.on_bar(cb)
    bar = _FakeBar(open=1, high=2, low=1, close=2, end_time=1745251200)
    # Must not raise — the wiring catches and logs.
    a._on_bar_complete("1m", bar)
    cb.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
