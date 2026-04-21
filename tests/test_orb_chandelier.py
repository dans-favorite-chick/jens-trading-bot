"""
Phoenix Bot — Tests for ORB Chandelier trail + universal EoD hook + per-signal scale-out.

Covers the Option B (spec-accurate) ORB exit-handling path added on 2026-04-19.
Run: python -m unittest tests.test_orb_chandelier -v
"""

from __future__ import annotations

import sys
import time
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class _Bar:
    open: float
    high: float
    low: float
    close: float
    volume: int = 100
    tick_count: int = 1
    start_time: float = 0.0
    end_time: float = 0.0


# ═════════════════════════════════════════════════════════════════════
# ChandelierTrailState unit tests
# ═════════════════════════════════════════════════════════════════════
class TestChandelierLong(unittest.TestCase):
    def test_long_ratchets_up_with_rising_highs(self):
        from core.chandelier_exit import ChandelierTrailState
        state = ChandelierTrailState(direction="LONG", entry_price=100.0, atr_mult=3.0)
        # 5 bars with rising highs; ATR constant at 1.0
        bars = [
            (101, 100),   # high, low
            (102, 100.5),
            (103, 101),
            (104, 102),
            (105, 103),
        ]
        prev_trail = 0.0
        for h, l in bars:
            trail = state.update(h, l, atr=1.0)
            # Trail must never drop (monotonic non-decreasing for LONG)
            self.assertGreaterEqual(trail, prev_trail)
            prev_trail = trail
        # After the last bar, trail = 105 - 3*1 = 102
        self.assertAlmostEqual(state.current_trail, 102.0, places=2)

    def test_long_trail_does_not_drop_when_high_retraces(self):
        from core.chandelier_exit import ChandelierTrailState
        state = ChandelierTrailState(direction="LONG", entry_price=100.0, atr_mult=3.0)
        state.update(110, 100, atr=2.0)   # trail = 110 - 6 = 104
        self.assertAlmostEqual(state.current_trail, 104.0)
        # Price retraces — high of next bar is only 108
        state.update(108, 105, atr=2.0)
        # highest_high is still 110; trail must not drop below 104
        self.assertGreaterEqual(state.current_trail, 104.0)


class TestChandelierShort(unittest.TestCase):
    def test_short_ratchets_down_with_falling_lows(self):
        from core.chandelier_exit import ChandelierTrailState
        state = ChandelierTrailState(direction="SHORT", entry_price=100.0, atr_mult=3.0)
        bars = [
            (99, 98),
            (98, 97),
            (97, 96),
            (96, 95),
            (95, 94),
        ]
        prev_trail = float("inf")
        for h, l in bars:
            trail = state.update(h, l, atr=1.0)
            # Trail must never rise (monotonic non-increasing for SHORT)
            self.assertLessEqual(trail, prev_trail)
            prev_trail = trail
        # After the last bar, trail = 94 + 3*1 = 97
        self.assertAlmostEqual(state.current_trail, 97.0, places=2)


class TestChandelierShouldExit(unittest.TestCase):
    def test_long_exit_triggers_on_violation(self):
        from core.chandelier_exit import ChandelierTrailState
        state = ChandelierTrailState(direction="LONG", entry_price=100.0, atr_mult=3.0)
        state.update(105, 100, atr=1.0)   # trail = 105 - 3 = 102
        self.assertFalse(state.should_exit(102.5))   # Above trail
        self.assertTrue(state.should_exit(101.9))    # Below trail → exit
        self.assertTrue(state.should_exit(102.0))    # At trail → exit (<=)

    def test_short_exit_triggers_on_violation(self):
        from core.chandelier_exit import ChandelierTrailState
        state = ChandelierTrailState(direction="SHORT", entry_price=100.0, atr_mult=3.0)
        state.update(100, 95, atr=1.0)    # trail = 95 + 3 = 98
        self.assertFalse(state.should_exit(97.5))    # Below trail
        self.assertTrue(state.should_exit(98.1))     # Above trail → exit
        self.assertTrue(state.should_exit(98.0))     # At trail → exit (>=)

    def test_uninitialized_does_not_exit(self):
        from core.chandelier_exit import ChandelierTrailState
        state = ChandelierTrailState(direction="LONG", entry_price=100.0)
        self.assertFalse(state.should_exit(50.0))   # Nothing happens before any update
        # With zero ATR, update returns 0 and state stays uninitialized for trail purposes
        state.update(105, 100, atr=0.0)
        self.assertFalse(state.should_exit(50.0))


# ═════════════════════════════════════════════════════════════════════
# Signal / Position wiring
# ═════════════════════════════════════════════════════════════════════
class TestORBSignalWiring(unittest.TestCase):
    def _make_or_bars(self, base=22000, or_high=22030, or_low=21990):
        today = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
        bars = []
        for i in range(15):
            t = today + timedelta(minutes=i)
            close = or_high if i == 3 else or_low if i == 7 else base
            bars.append(_Bar(
                open=base, high=or_high, low=or_low, close=close,
                start_time=t.timestamp(), end_time=(t + timedelta(seconds=59)).timestamp(),
            ))
        return bars

    def test_orb_signal_carries_chandelier_spec(self):
        from strategies.orb import OpeningRangeBreakout
        strat = OpeningRangeBreakout({
            "or_duration_minutes": 15, "min_or_size_points": 5,
            "max_or_size_points": 200, "max_stop_points": 100,
        })
        bars_1m = self._make_or_bars()
        breakout_5m = _Bar(open=22025, high=22040, low=22020, close=22035,
                           start_time=time.time() - 300, end_time=time.time())
        sig = strat.evaluate(
            market={"price": 22035, "atr_5m": 15.0},
            bars_5m=[breakout_5m], bars_1m=bars_1m, session_info={},
        )
        self.assertIsNotNone(sig)
        # Per-signal fields for Option B:
        self.assertEqual(sig.scale_out_rr, 1.0)
        self.assertEqual(sig.exit_trigger, "chandelier_trail_3atr")
        self.assertIsNotNone(sig.trail_config)
        self.assertEqual(sig.trail_config["atr_mult"], 3.0)
        self.assertEqual(sig.trail_config["atr_period"], 14)
        self.assertEqual(sig.trail_config["atr_timeframe"], "5m")


class TestPositionTrailInit(unittest.TestCase):
    def test_open_position_with_chandelier_creates_trail_state(self):
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t1", direction="LONG", entry_price=22000.0,
            contracts=1, stop_price=21950.0, target_price=22050.0,
            strategy="orb", reason="test",
            exit_trigger="chandelier_trail_3atr",
            eod_flat_time_et="15:55",
            scale_out_rr=1.0,
            trail_config={"atr_mult": 3.0, "atr_period": 14, "atr_timeframe": "5m"},
        )
        pos = pm.position
        self.assertIsNotNone(pos.trail_state)
        self.assertEqual(pos.trail_state.direction, "LONG")
        self.assertEqual(pos.trail_state.atr_mult, 3.0)
        self.assertEqual(pos.scale_out_rr, 1.0)
        self.assertEqual(pos.eod_flat_time_et, "15:55")

    def test_open_position_without_chandelier_has_no_trail_state(self):
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t2", direction="LONG", entry_price=22000.0,
            contracts=1, stop_price=21950.0, target_price=22100.0,
            strategy="bias_momentum", reason="test",
        )
        self.assertIsNone(pm.position.trail_state)


# ═════════════════════════════════════════════════════════════════════
# Universal EoD flat hook — simulates the base_bot check
# ═════════════════════════════════════════════════════════════════════
class TestUniversalEoDFlat(unittest.TestCase):
    """
    The universal EoD hook is inline in base_bot.py. This test reproduces
    the decision logic standalone to verify correctness. If you change the
    inline hook, update this test to stay in sync.
    """

    def _would_flatten(self, pos_eod_str: str, current_et: datetime) -> bool:
        """Mirrors the base_bot universal EoD comparison."""
        return current_et.strftime("%H:%M") >= pos_eod_str

    def test_before_eod_does_not_flatten(self):
        from zoneinfo import ZoneInfo
        et = datetime(2026, 4, 20, 10, 30, tzinfo=ZoneInfo("America/New_York"))  # 10:30 ET
        self.assertFalse(self._would_flatten("10:55", et))

    def test_at_eod_flattens(self):
        from zoneinfo import ZoneInfo
        et = datetime(2026, 4, 20, 10, 55, tzinfo=ZoneInfo("America/New_York"))  # 10:55 ET
        self.assertTrue(self._would_flatten("10:55", et))

    def test_after_eod_flattens(self):
        from zoneinfo import ZoneInfo
        et = datetime(2026, 4, 20, 14, 0, tzinfo=ZoneInfo("America/New_York"))   # 14:00 ET
        self.assertTrue(self._would_flatten("10:55", et))

    def test_lab_eod_at_1555(self):
        from zoneinfo import ZoneInfo
        et_before = datetime(2026, 4, 20, 15, 54, tzinfo=ZoneInfo("America/New_York"))
        et_at = datetime(2026, 4, 20, 15, 55, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(self._would_flatten("15:55", et_before))
        self.assertTrue(self._would_flatten("15:55", et_at))


# ═════════════════════════════════════════════════════════════════════
# ORB partial at 1.0R — verifies scale-out override works
# ═════════════════════════════════════════════════════════════════════
class TestORBScaleOutAtOneR(unittest.TestCase):
    def test_scale_rr_reads_from_position(self):
        """
        Mirrors the base_bot scale-out decision:
          _scale_rr = getattr(pos, "scale_out_rr", None) or SCALE_OUT_RR
        """
        from config.settings import SCALE_OUT_RR
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t_orb", direction="LONG", entry_price=22000.0,
            contracts=2, stop_price=21990.0, target_price=22020.0,
            strategy="orb", reason="test",
            scale_out_rr=1.0,
            exit_trigger="chandelier_trail_3atr",
            trail_config={"atr_mult": 3.0},
        )
        pos = pm.position
        _scale_rr = getattr(pos, "scale_out_rr", None) or SCALE_OUT_RR
        self.assertEqual(_scale_rr, 1.0)

    def test_scale_rr_falls_back_to_global_for_non_orb(self):
        from config.settings import SCALE_OUT_RR
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t_other", direction="LONG", entry_price=22000.0,
            contracts=2, stop_price=21990.0, target_price=22020.0,
            strategy="bias_momentum", reason="test",
        )
        pos = pm.position
        _scale_rr = getattr(pos, "scale_out_rr", None) or SCALE_OUT_RR
        self.assertEqual(_scale_rr, SCALE_OUT_RR)

    def test_should_scale_out_fires_at_one_r_for_orb(self):
        """The existing helper _should_scale_out must accept the per-signal RR."""
        from bots.base_bot import _should_scale_out
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t_orb2", direction="LONG", entry_price=22000.0,
            contracts=2, stop_price=21990.0, target_price=22020.0,  # stop=10pt
            strategy="orb", reason="test",
            scale_out_rr=1.0,
        )
        pos = pm.position
        # At +10pt (1.0R), scale-out should fire with scale_rr=1.0
        self.assertTrue(_should_scale_out(pos, price=22010.0, scale_rr=1.0))
        # At +9pt (0.9R) it should NOT fire
        self.assertFalse(_should_scale_out(pos, price=22009.0, scale_rr=1.0))
        # Same position with global 1.5R would NOT fire at +10pt
        self.assertFalse(_should_scale_out(pos, price=22010.0, scale_rr=1.5))


# ═════════════════════════════════════════════════════════════════════
# Fallback: bracket stop still protects if Chandelier fails
# ═════════════════════════════════════════════════════════════════════
class TestChandelierFallbackBracketStop(unittest.TestCase):
    def test_bracket_stop_persists_through_chandelier_attach(self):
        """Opening a position with a Chandelier trail must still set the
        bracket stop_price — the trail is an ADDITIONAL protection layer."""
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t_fb", direction="LONG", entry_price=22000.0,
            contracts=1, stop_price=21950.0, target_price=22100.0,
            strategy="orb", reason="test",
            exit_trigger="chandelier_trail_3atr",
            trail_config={"atr_mult": 3.0},
        )
        pos = pm.position
        # Both mechanisms alive:
        self.assertEqual(pos.stop_price, 21950.0)      # bracket stop intact
        self.assertIsNotNone(pos.trail_state)          # trail attached

    def test_position_opens_even_if_trail_config_missing(self):
        """A malformed Signal (exit_trigger set but trail_config missing) must
        not crash position open — it just won't have a trail state."""
        from core.position_manager import PositionManager
        pm = PositionManager()
        pm.open_position(
            trade_id="t_fb2", direction="LONG", entry_price=22000.0,
            contracts=1, stop_price=21950.0, target_price=22100.0,
            strategy="orb", reason="test",
            exit_trigger="chandelier_trail_3atr",
            trail_config=None,
        )
        self.assertIsNotNone(pm.position)
        self.assertIsNone(pm.position.trail_state)


if __name__ == "__main__":
    unittest.main()
