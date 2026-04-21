"""
Phoenix Bot — Roadmap v4 integration tests.

Covers the Mon-Sat deliverables:
- Signal dataclass extensions (entry_type, stop_type, target_type, etc.)
- OIF writer STOPMARKET + atomic bracket orders
- ORB strategy entry logic
- Noise Area sigma_open math + signal logic
- Circuit-breaker telegram throttle

Run: python -m unittest tests.test_roadmap_v4 -v
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class _Bar:
    """Lightweight Bar stand-in for strategy tests."""
    open: float
    high: float
    low: float
    close: float
    volume: int = 100
    tick_count: int = 1
    start_time: float = 0.0
    end_time: float = 0.0


# ═════════════════════════════════════════════════════════════════════
# Signal dataclass — new fields present & defaulted correctly
# ═════════════════════════════════════════════════════════════════════
class TestSignalExtensions(unittest.TestCase):
    def test_defaults(self):
        from strategies.base_strategy import Signal
        s = Signal(
            direction="LONG", stop_ticks=10, target_rr=2.0,
            confidence=60, entry_score=50,
            strategy="test", reason="unit", confluences=[],
        )
        self.assertEqual(s.entry_type, "LIMIT")
        self.assertEqual(s.stop_type, "STOPMARKET")
        self.assertEqual(s.target_type, "LIMIT")
        self.assertIsNone(s.entry_price)
        self.assertIsNone(s.stop_price)
        self.assertIsNone(s.target_price)
        self.assertEqual(s.metadata, {})

    def test_overrides(self):
        from strategies.base_strategy import Signal
        s = Signal(
            direction="SHORT", stop_ticks=8, target_rr=1.5,
            confidence=60, entry_score=50,
            strategy="orb", reason="test", confluences=[],
            entry_type="STOPMARKET",
            entry_price=22000.0, stop_price=22050.0, target_price=21900.0,
            exit_trigger="price_returns_inside_noise_area",
            eod_flat_time_et="10:55",
            metadata={"UB": 22100, "LB": 21950},
        )
        self.assertEqual(s.entry_type, "STOPMARKET")
        self.assertEqual(s.entry_price, 22000.0)
        self.assertEqual(s.metadata["UB"], 22100)
        d = s.to_dict()
        self.assertIn("entry_type", d)
        self.assertIn("exit_trigger", d)


# ═════════════════════════════════════════════════════════════════════
# OIF writer — STOPMARKET + bracket atomic write
# ═════════════════════════════════════════════════════════════════════
class TestOIFWriter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Monkey-patch OIF paths to our temp dir
        import bridge.oif_writer as oif
        self._orig_incoming = oif.OIF_INCOMING
        oif.OIF_INCOMING = self.tmpdir

    def tearDown(self):
        import bridge.oif_writer as oif
        oif.OIF_INCOMING = self._orig_incoming
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bracket_atomic_write_three_files(self):
        from bridge.oif_writer import write_bracket_order
        paths = write_bracket_order(
            direction="LONG", qty=1, entry_type="LIMIT",
            entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
            trade_id="testA", account="Sim101",
        )
        self.assertEqual(len(paths), 3)
        # All three .txt files present, no .tmp leftovers
        txt_files = [f for f in os.listdir(self.tmpdir) if f.endswith(".txt")]
        tmp_files = [f for f in os.listdir(self.tmpdir) if f.endswith(".tmp")]
        self.assertEqual(len(txt_files), 3)
        self.assertEqual(len(tmp_files), 0)

    def test_bracket_uses_stopmarket_for_stops(self):
        from bridge.oif_writer import write_bracket_order
        write_bracket_order(
            direction="LONG", qty=1, entry_type="LIMIT",
            entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
            trade_id="stopcheck", account="Sim101",
        )
        # Read the stop file
        stop_files = [f for f in os.listdir(self.tmpdir) if "_stop.txt" in f]
        self.assertEqual(len(stop_files), 1)
        with open(os.path.join(self.tmpdir, stop_files[0])) as fh:
            content = fh.read()
        self.assertIn("STOPMARKET", content)
        self.assertIn("21950.00", content)  # stop price
        # Must NOT be the ambiguous STOP order type
        parts = content.split(";")
        self.assertIn("STOPMARKET", parts)
        self.assertNotIn("STOP", [p for p in parts if p == "STOP"])

    def test_bracket_short_direction_sides_correct(self):
        from bridge.oif_writer import write_bracket_order
        write_bracket_order(
            direction="SHORT", qty=1, entry_type="LIMIT",
            entry_price=22000.0, stop_price=22050.0, target_price=21900.0,
            trade_id="shortcheck", account="Sim101",
        )
        entry_f = [f for f in os.listdir(self.tmpdir) if "_entry.txt" in f][0]
        stop_f = [f for f in os.listdir(self.tmpdir) if "_stop.txt" in f][0]
        target_f = [f for f in os.listdir(self.tmpdir) if "_target.txt" in f][0]
        entry = open(os.path.join(self.tmpdir, entry_f)).read()
        stop = open(os.path.join(self.tmpdir, stop_f)).read()
        target = open(os.path.join(self.tmpdir, target_f)).read()
        # SHORT entry side = SELL, exit side (stop/target) = BUY
        self.assertIn(";SELL;", entry)
        self.assertIn(";BUY;", stop)
        self.assertIn(";BUY;", target)

    def test_bracket_managed_exit_no_target(self):
        """Noise Area passes target_price=None — bracket should still stage entry+stop."""
        from bridge.oif_writer import write_bracket_order
        paths = write_bracket_order(
            direction="LONG", qty=1, entry_type="LIMIT",
            entry_price=22000.0, stop_price=21950.0, target_price=None,
            trade_id="mgd", account="Sim101",
        )
        self.assertEqual(len(paths), 2)  # entry + stop only


# ═════════════════════════════════════════════════════════════════════
# ORB strategy — 15m OR, 5m close confirmation
# ═════════════════════════════════════════════════════════════════════
class TestORBStrategy(unittest.TestCase):
    def _make_or_bars(self, base_price=22000.0, or_high=22050.0, or_low=21970.0):
        """Return 15 1m bars filling the OR range, timestamps at today's 9:30 ET = 8:30 CT."""
        today = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
        bars = []
        for i in range(15):
            t = today + timedelta(minutes=i)
            # Alternate pushing into high/low so the OR fills out
            if i == 3:
                close = or_high
            elif i == 7:
                close = or_low
            else:
                close = base_price
            bars.append(_Bar(
                open=base_price, high=or_high, low=or_low, close=close,
                volume=100, start_time=t.timestamp(), end_time=(t + timedelta(seconds=59)).timestamp(),
            ))
        return bars

    def test_or_not_set_returns_none_with_few_bars(self):
        from strategies.orb import OpeningRangeBreakout
        strat = OpeningRangeBreakout({"or_duration_minutes": 15})
        bars = self._make_or_bars()[:5]  # Only 5 bars → OR not ready
        sig = strat.evaluate(
            market={"price": 22000.0, "atr_5m": 15.0},
            bars_5m=[], bars_1m=bars, session_info={},
        )
        self.assertIsNone(sig)
        self.assertFalse(strat._or_set)

    def test_or_size_filter_rejects_too_tight(self):
        from strategies.orb import OpeningRangeBreakout
        strat = OpeningRangeBreakout({"or_duration_minutes": 15, "min_or_size_points": 10})
        # OR = 22001 - 21999 = 2pts, below min 10
        bars = self._make_or_bars(base_price=22000, or_high=22001.0, or_low=21999.0)
        breakout_5m = _Bar(
            open=22001, high=22005, low=22000, close=22005,
            start_time=time.time() - 300, end_time=time.time(),
        )
        sig = strat.evaluate(
            market={"price": 22005, "atr_5m": 15.0},
            bars_5m=[breakout_5m], bars_1m=bars, session_info={},
        )
        self.assertIsNone(sig)

    def test_orb_long_signal_on_5m_break(self):
        from strategies.orb import OpeningRangeBreakout
        strat = OpeningRangeBreakout({
            "or_duration_minutes": 15, "min_or_size_points": 5,
            "max_or_size_points": 200, "max_stop_points": 100,
        })
        bars_1m = self._make_or_bars(base_price=22000, or_high=22030, or_low=21990)  # 40pt OR
        # 5m bar closes above OR high → long signal
        breakout_5m = _Bar(
            open=22025, high=22040, low=22020, close=22035,
            start_time=time.time() - 300, end_time=time.time(),
        )
        sig = strat.evaluate(
            market={"price": 22035, "atr_5m": 15.0},
            bars_5m=[breakout_5m], bars_1m=bars_1m, session_info={},
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "LONG")
        self.assertEqual(sig.entry_type, "STOPMARKET")
        self.assertEqual(sig.entry_price, 22030.25)  # OR high + 1 tick
        self.assertAlmostEqual(sig.stop_price, 21989.50, places=2)  # OR low - 2 tick buffer
        # One-trade-per-day check
        sig2 = strat.evaluate(
            market={"price": 22045, "atr_5m": 15.0},
            bars_5m=[breakout_5m], bars_1m=bars_1m, session_info={},
        )
        self.assertIsNone(sig2)

    def test_orb_short_signal_on_5m_break(self):
        from strategies.orb import OpeningRangeBreakout
        strat = OpeningRangeBreakout({
            "or_duration_minutes": 15, "min_or_size_points": 5,
            "max_or_size_points": 200, "max_stop_points": 100,
        })
        bars_1m = self._make_or_bars(base_price=22000, or_high=22030, or_low=21990)
        breakout_5m = _Bar(
            open=21995, high=21998, low=21980, close=21985,
            start_time=time.time() - 300, end_time=time.time(),
        )
        sig = strat.evaluate(
            market={"price": 21985, "atr_5m": 15.0},
            bars_5m=[breakout_5m], bars_1m=bars_1m, session_info={},
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "SHORT")
        self.assertEqual(sig.entry_type, "STOPMARKET")


# ═════════════════════════════════════════════════════════════════════
# Noise Area strategy — sigma_open math + warmup gate
# ═════════════════════════════════════════════════════════════════════
class TestNoiseArea(unittest.TestCase):
    def test_sigma_open_mean_excludes_today(self):
        from strategies.noise_area import NoiseAreaMomentum
        strat = NoiseAreaMomentum({})
        # 14 samples for minute 30: last one should be excluded
        strat.sigma_open_table[30] = [0.001] * 14 + [999.0]  # today = 999
        sigma = strat._get_sigma_open(30)
        self.assertIsNotNone(sigma)
        # 14-day window shifted: excludes 999
        self.assertAlmostEqual(sigma, 0.001, places=5)

    def test_insufficient_history_returns_none(self):
        from strategies.noise_area import NoiseAreaMomentum
        strat = NoiseAreaMomentum({})
        strat.sigma_open_table[30] = [0.001] * 5  # Only 5 samples
        self.assertIsNone(strat._get_sigma_open(30))

    def test_warmup_gate_blocks_when_few_buckets(self):
        """With only 5 minute-buckets populated the strategy should stay silent."""
        from strategies.noise_area import NoiseAreaMomentum
        strat = NoiseAreaMomentum({"min_noise_history_days": 10})
        # 5 buckets, each with 13 samples → passes per-minute gate but fails bucket-count gate
        for mod in [0, 30, 60, 90, 120]:
            strat.sigma_open_table[mod] = [0.002] * 13
        # Craft a single 1m bar that would otherwise trigger evaluation
        now_ct = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        bar = _Bar(
            open=22000, high=22010, low=21990, close=22005,
            end_time=now_ct.timestamp(), start_time=(now_ct - timedelta(seconds=59)).timestamp(),
        )
        sig = strat.evaluate(
            market={"price": 22005, "vwap": 22000, "avwap_pd_close": 22000},
            bars_5m=[], bars_1m=[bar], session_info={},
        )
        self.assertIsNone(sig)

    def test_seed_history_clamps_to_30(self):
        from strategies.noise_area import NoiseAreaMomentum
        strat = NoiseAreaMomentum({})
        strat.seed_history({30: [0.001] * 50})  # Inject 50 samples
        self.assertEqual(len(strat.sigma_open_table[30]), 30)


# ═════════════════════════════════════════════════════════════════════
# Circuit-breaker throttle — prevent telegram spam
# ═════════════════════════════════════════════════════════════════════
class TestBreakerThrottle(unittest.TestCase):
    def test_alert_throttle_dedups_within_hour(self):
        from core.circuit_breakers import CircuitBreakers, BreakerEvent
        cb = CircuitBreakers(observe_mode=True)

        ev = BreakerEvent(
            breaker_type="TICK_GAP", ts=datetime.now(), severity="CRITICAL",
            reason="test",
        )
        with patch("core.telegram_notifier.notify_alert") as mock_tg:
            cb._alert_new_criticals([ev])
            cb._alert_new_criticals([ev])
            cb._alert_new_criticals([ev])
        # Only first call should dispatch
        self.assertEqual(len(cb._alert_throttle), 1)


if __name__ == "__main__":
    unittest.main()
