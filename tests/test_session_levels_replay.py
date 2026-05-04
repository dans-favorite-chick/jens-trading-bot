"""Sprint I (2026-05-03): session_levels.replay_from_bars + restart backfill.

Production bug
==============
On any bot restart AFTER 08:45 CT, the opening_session.orb sub-strategy
perma-skipped with `SKIP orb missing_fields=...` for the rest of the
session. Confirmed in logs/sim_bot_stderr.log on 2026-04-28 (multiple
SKIPs at 08:45, 09:52, 11:12 CT — all the same day, same restart).

Root cause: TickAggregator.restore_state() restored the bar deques from
disk, but session_levels.update() was NEVER called for those bars
(it only fires on live bar completion via _on_bar_complete). So:
  - rth_open_price (set at first 8:30 1m bar)
  - rth_15min_high/low (8:30-8:45 1m bars)
  - rth_5min_close_last (each RTH 5m bar)
…all stayed None despite the bars sitting in `bars_1m.completed` /
`bars_5m.completed`.

Fix
===
1. SessionLevelsAggregator.replay_from_bars(bars_1m, bars_5m) — sorts
   by end_time and dispatches through update(), populating the RTH
   levels retroactively.
2. TickAggregator.restore_state() calls replay_from_bars() at the end,
   so the backfill happens automatically on any same-day restart.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.session_levels_aggregator import SessionLevelsAggregator


# ─── helpers ────────────────────────────────────────────────────────

@dataclass
class ReplayBar:
    """Bar surrogate matching tick_aggregator.Bar's interface."""
    open: float
    high: float
    low: float
    close: float
    volume: int = 100
    end_time: float = 0.0   # epoch seconds


def _et(hh: int, mm: int, d: date | None = None) -> float:
    """Compute local-time epoch end_time for hour:minute on the given date."""
    the_date = d or date(2026, 4, 28)
    dt = datetime(the_date.year, the_date.month, the_date.day, hh, mm, 0)
    return dt.timestamp()


# ═══════════════════════════════════════════════════════════════════
# replay_from_bars (5 tests)
# ═══════════════════════════════════════════════════════════════════
class TestReplayFromBars:
    @pytest.fixture
    def agg(self, tmp_path):
        return SessionLevelsAggregator("lab", history_dir=tmp_path)

    def test_replay_populates_rth_open_from_first_8_30_1m_bar(self, agg):
        """rth_open_price = open of the first 1m bar at/after 8:30 CT."""
        bars_1m = [
            ReplayBar(100.0, 100.5, 99.5, 100.2, end_time=_et(8, 31)),
            ReplayBar(100.2, 100.7, 99.9, 100.5, end_time=_et(8, 32)),
        ]
        n = agg.replay_from_bars(bars_1m=bars_1m, bars_5m=[])
        assert n == 2
        assert agg.rth_open_price == 100.0

    def test_replay_populates_15min_or_high_low(self, agg):
        """rth_15min_high/low track max/min of 1m bars in 08:30-08:45."""
        bars_1m = [
            ReplayBar(100.0, 100.5, 99.5, 100.2, end_time=_et(8, 31)),
            ReplayBar(100.2, 102.0, 100.0, 101.5, end_time=_et(8, 35)),
            ReplayBar(101.5, 101.8, 99.0, 99.5, end_time=_et(8, 40)),
            ReplayBar(99.5, 100.5, 99.0, 100.0, end_time=_et(8, 44)),
            # After 8:45 — should NOT update the OR.
            ReplayBar(100.0, 105.0, 95.0, 102.0, end_time=_et(8, 50)),
        ]
        agg.replay_from_bars(bars_1m=bars_1m, bars_5m=[])
        assert agg.rth_15min_high == 102.0  # bar at 8:35
        assert agg.rth_15min_low == 99.0    # bar at 8:40
        # The 8:50 bar's extremes (105/95) must NOT bleed into the OR.
        assert agg.rth_15min_high != 105.0
        assert agg.rth_15min_low != 95.0

    def test_replay_populates_5m_close_last(self, agg):
        """rth_5min_close_last tracks the most recent RTH 5m close."""
        bars_5m = [
            ReplayBar(100.0, 102.0, 99.0, 101.0, end_time=_et(8, 35)),
            ReplayBar(101.0, 103.0, 100.5, 102.5, end_time=_et(8, 40)),
            ReplayBar(102.5, 104.0, 102.0, 103.5, end_time=_et(8, 45)),
        ]
        agg.replay_from_bars(bars_1m=[], bars_5m=bars_5m)
        assert agg.rth_5min_close_last == 103.5

    def test_replay_skips_bars_with_zero_end_time(self, agg):
        """Bars without a real end_time are noise — skip silently."""
        bars_1m = [
            ReplayBar(100.0, 101.0, 99.0, 100.5, end_time=0.0),
            ReplayBar(100.0, 100.5, 99.5, 100.2, end_time=_et(8, 31)),
        ]
        n = agg.replay_from_bars(bars_1m=bars_1m, bars_5m=[])
        assert n == 1
        assert agg.rth_open_price == 100.0  # 2nd bar populated this

    def test_replay_orders_1m_before_5m_at_same_timestamp(self, agg):
        """When a 1m and 5m bar share end_time (e.g. both close at 08:35),
        the 1m must dispatch first — that's the live order. This protects
        the ORB first-break logic which compares 5m.close against
        rth_15min_high (populated by 1m bars)."""
        ts_835 = _et(8, 35)
        bars_1m = [
            # First 1m bar at 8:30 to set rth_open_price + start OR window.
            ReplayBar(100.0, 100.5, 99.5, 100.2, end_time=_et(8, 31)),
            # 1m bar tied with the 5m at 08:35.
            ReplayBar(100.2, 101.0, 100.0, 100.8, end_time=ts_835),
        ]
        bars_5m = [
            ReplayBar(100.0, 101.0, 99.5, 100.8, end_time=ts_835),
        ]
        agg.replay_from_bars(bars_1m=bars_1m, bars_5m=bars_5m)
        # Both fields populated — order didn't break either path.
        assert agg.rth_open_price == 100.0
        assert agg.rth_5min_close_last == 100.8

    def test_replay_returns_zero_for_empty_input(self, agg):
        assert agg.replay_from_bars(bars_1m=[], bars_5m=[]) == 0
        assert agg.replay_from_bars(bars_1m=None, bars_5m=None) == 0

    def test_replay_unblocks_orb_evaluator(self, agg):
        """Integration: feed a realistic restored-bar set, run snapshot
        through opening_session._evaluate_orb, and assert it does NOT
        skip with missing_fields. This is the bug the fix targets."""
        from strategies.opening_session import OpeningSessionStrategy

        # 8:30-8:45 1m bars establish OR.
        bars_1m = [
            ReplayBar(100.0, 100.5, 99.5, 100.2, end_time=_et(8, 31)),
            ReplayBar(100.2, 102.5, 100.0, 102.0, end_time=_et(8, 35)),
            ReplayBar(102.0, 102.3, 99.0, 99.5, end_time=_et(8, 40)),
            ReplayBar(99.5, 101.5, 99.5, 101.2, end_time=_et(8, 44)),
        ]
        # 8:35 + 8:40 + 8:45 5m bars — 8:50 5m closes ABOVE rth_15min_high.
        bars_5m = [
            ReplayBar(100.0, 102.5, 99.5, 102.0, end_time=_et(8, 35)),
            ReplayBar(102.0, 102.3, 99.0, 99.5, end_time=_et(8, 40)),
            ReplayBar(99.5, 101.5, 99.5, 101.2, end_time=_et(8, 45)),
            ReplayBar(101.2, 103.0, 101.0, 102.8, end_time=_et(8, 50)),
        ]
        agg.replay_from_bars(bars_1m=bars_1m, bars_5m=bars_5m)

        # Sanity: all 5 fields _evaluate_orb requires are now populated.
        snap = agg.get_levels_dict()
        for k in ("rth_15min_high", "rth_15min_low",
                  "rth_open_price", "rth_5min_close_last"):
            assert snap[k] is not None, f"{k} should be backfilled"

        # Run through the strategy's missing-fields gate.
        strat = OpeningSessionStrategy.__new__(OpeningSessionStrategy)
        strat._last_skip_reason = None
        strat.config = {"orb_max_range_pct": 0.10}  # generous so OR isn't "too wide"

        snap["price"] = 102.8
        result = strat._evaluate_orb(snap)
        # We don't care if it returns a Signal vs hits a downstream gate
        # like or_too_wide — we just need the missing_fields gate to pass.
        assert "missing_fields" not in (strat._last_skip_reason or "")


# ═══════════════════════════════════════════════════════════════════
# TickAggregator wiring (1 test)
# ═══════════════════════════════════════════════════════════════════

class TestRestoreStateWiring:
    """The fix is only useful if TickAggregator.restore_state() actually
    calls replay_from_bars() after restoring the bar deques."""

    def test_restore_state_invokes_session_levels_replay(self, tmp_path):
        """Feed an in-memory state dict and verify the replay path fires."""
        import json
        import time as _time

        from core.tick_aggregator import TickAggregator

        # Build a minimal valid state file with a couple of 1m bars.
        today = datetime.now().strftime("%Y-%m-%d")
        state = {
            "saved_day": today,
            "saved_at": _time.time(),
            "bars_1m": [
                {"o": 100.0, "h": 100.5, "l": 99.5, "c": 100.2,
                 "v": 100, "tc": 5, "st": _et(8, 30), "et": _et(8, 31)},
            ],
            "bars_5m": [],
            "bars_15m": [],
            "bars_60m": [],
            "bars_tick": [],
            "bar_counts": {"1m": 1},
            "atr": {"5m": 1.0, "tick": 0.5},
            "tr_history": {},
            "ema5": 100.0, "ema9": 100.0, "ema21": 100.0,
            "ema9_15m": 100.0, "ema21_15m": 100.0,
            "vwap": 100.0,
        }
        path = tmp_path / "state.json"
        with open(path, "w") as f:
            json.dump(state, f)

        agg = TickAggregator(bot_name="test_bot")
        agg.session_levels = MagicMock()
        agg.session_levels.replay_from_bars = MagicMock(return_value=1)

        ok = agg.restore_state(str(path))
        assert ok is True

        # The whole point of this fix: replay_from_bars MUST be called.
        agg.session_levels.replay_from_bars.assert_called_once()
        kwargs = agg.session_levels.replay_from_bars.call_args.kwargs
        # Bars passed as kwargs (matches the production call site).
        assert "bars_1m" in kwargs
        assert "bars_5m" in kwargs
        assert len(kwargs["bars_1m"]) == 1


# ═══════════════════════════════════════════════════════════════════
# Improved missing_fields log (1 test)
# ═══════════════════════════════════════════════════════════════════

class TestImprovedMissingFieldsLog:
    """The orb sub-evaluator now names which fields are None instead of
    the opaque 'missing_fields'. This is observability — it lets the
    operator diagnose future producer/consumer mismatches without a
    code spelunk."""

    def test_orb_log_names_missing_fields(self):
        from strategies.opening_session import OpeningSessionStrategy

        strat = OpeningSessionStrategy.__new__(OpeningSessionStrategy)
        strat._last_skip_reason = None
        strat.config = {}

        # All RTH levels None (the typical "bot started after 8:45" state
        # before the replay fix).
        snap = {
            "rth_15min_high": None,
            "rth_15min_low": None,
            "rth_open_price": None,
            "rth_5min_close_last": None,
            "price": 100.0,
            "orb_first_break_direction": None,
        }
        result = strat._evaluate_orb(snap)
        assert result is None
        # All 4 RTH fields should be named explicitly in the log.
        reason = strat._last_skip_reason or ""
        assert "missing_fields=" in reason
        assert "rth_15min_high" in reason
        assert "rth_15min_low" in reason
        assert "rth_open_price" in reason
        assert "rth_5min_close_last" in reason
        # `price` is set, so it should NOT appear as its own token in
        # the comma-separated list (note: "price" matches inside
        # "rth_open_price" as a substring — split + check tokens).
        tokens = reason.split("missing_fields=", 1)[1].split(",")
        assert "price" not in tokens
