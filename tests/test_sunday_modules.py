"""
Phoenix Bot — Unit tests for Sunday-built modules.

Covers: footprint_builder, footprint_patterns, pinning_detector,
opex_calendar, es_confirmation, structural_bias.

Run: python -m unittest tests.test_sunday_modules -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, date, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


# ═════════════════════════════════════════════════════════════════════
# footprint_builder
# ═════════════════════════════════════════════════════════════════════
class TestFootprintBuilder(unittest.TestCase):
    def test_empty_accumulator_no_current_bar(self):
        from bridge.footprint_builder import FootprintAccumulator
        acc = FootprintAccumulator()
        self.assertIsNone(acc.current_bar())

    def test_tick_at_ask_classified_as_buy(self):
        from bridge.footprint_builder import FootprintAccumulator
        acc = FootprintAccumulator()
        tick = {"price": 100.25, "bid": 100.00, "ask": 100.25, "vol": 10, "ts": "2026-04-17T10:00:00"}
        acc.process_tick(tick)
        bar = acc.current_bar()
        self.assertIsNotNone(bar)
        self.assertGreater(bar.buy_volume(), 0)
        self.assertEqual(bar.sell_volume(), 0)

    def test_tick_at_bid_classified_as_sell(self):
        from bridge.footprint_builder import FootprintAccumulator
        acc = FootprintAccumulator()
        tick = {"price": 100.00, "bid": 100.00, "ask": 100.25, "vol": 10, "ts": "2026-04-17T10:00:00"}
        acc.process_tick(tick)
        bar = acc.current_bar()
        self.assertGreater(bar.sell_volume(), 0)
        self.assertEqual(bar.buy_volume(), 0)

    def test_close_bar_moves_to_completed(self):
        from bridge.footprint_builder import FootprintAccumulator
        acc = FootprintAccumulator()
        acc.process_tick({"price": 100.25, "bid": 100, "ask": 100.25, "vol": 10, "ts": "2026-04-17T10:00:00"})
        bar = acc.close_bar()
        self.assertIsNotNone(bar)
        self.assertIsNone(acc.current_bar())
        self.assertEqual(len(acc.completed_bars), 1)


# ═════════════════════════════════════════════════════════════════════
# footprint_patterns
# ═════════════════════════════════════════════════════════════════════
class TestFootprintPatterns(unittest.TestCase):
    def _empty_bar(self):
        from bridge.footprint_builder import FootprintBar
        return FootprintBar(ts_open=datetime.now(), ts_close=datetime.now(),
                            bar_length_s=60, open_price=100, high=101, low=99,
                            close=100, total_volume=0)

    def test_empty_bar_no_signal(self):
        from core.footprint_patterns import detect_stacked_imbalance
        self.assertIsNone(detect_stacked_imbalance(self._empty_bar()))

    def test_stacked_imbalance_detected(self):
        from core.footprint_patterns import detect_stacked_imbalance
        bar = self._empty_bar()
        # 3 consecutive buckets with 3:1 buy imbalance
        bar.ask_volume_at_price = {100.00: 90, 100.25: 90, 100.50: 90}
        bar.bid_volume_at_price = {100.00: 10, 100.25: 10, 100.50: 10}
        sig = detect_stacked_imbalance(bar)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "BULLISH")

    def test_delta_divergence_bearish(self):
        from core.footprint_patterns import detect_delta_divergence
        from bridge.footprint_builder import FootprintBar
        def mk(high, low, delta):
            b = FootprintBar(ts_open=datetime.now(), ts_close=datetime.now(),
                             bar_length_s=60, open_price=100, high=high, low=low,
                             close=(high + low) / 2, total_volume=100)
            # Synthesize delta via bid/ask imbalance
            if delta > 0:
                b.ask_volume_at_price = {100: delta}
                b.bid_volume_at_price = {}
            else:
                b.bid_volume_at_price = {100: abs(delta)}
                b.ask_volume_at_price = {}
            return b
        bars = [mk(100, 99, 50), mk(101, 100, 80), mk(102, 101, 100), mk(103, 102, 90), mk(104, 103, 30)]
        sig = detect_delta_divergence(bars)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "BEARISH")


# ═════════════════════════════════════════════════════════════════════
# pinning_detector
# ═════════════════════════════════════════════════════════════════════
class TestPinningDetector(unittest.TestCase):
    def test_outside_window_returns_no_risk(self):
        from core.pinning_detector import PinningDetector
        d = PinningDetector()
        ts = datetime(2026, 4, 17, 10, 30)  # Morning, not in pin window
        state = d.update(ts, 26500, {"call_resistance_0dte": 26500, "regime": "POSITIVE"})
        self.assertFalse(state.pin_risk_active)

    def test_in_window_near_level_activates_pin_risk(self):
        from core.pinning_detector import PinningDetector
        d = PinningDetector()
        ts = datetime(2026, 4, 17, 14, 0)
        state = d.update(ts, 26500, {"call_resistance_0dte": 26502, "regime": "POSITIVE"})
        self.assertTrue(state.pin_risk_active)

    def test_neg_gamma_regime_disables_pin_risk(self):
        from core.pinning_detector import PinningDetector
        d = PinningDetector()
        ts = datetime(2026, 4, 17, 14, 0)
        state = d.update(ts, 26500, {"call_resistance_0dte": 26502, "regime": "NEGATIVE"})
        self.assertFalse(state.pin_risk_active)


# ═════════════════════════════════════════════════════════════════════
# opex_calendar
# ═════════════════════════════════════════════════════════════════════
class TestOpExCalendar(unittest.TestCase):
    def test_third_friday_detected(self):
        from core.opex_calendar import third_friday_of_month, is_opex_day
        tf = third_friday_of_month(2026, 4)
        self.assertEqual(tf, date(2026, 4, 17))
        self.assertTrue(is_opex_day(date(2026, 4, 17)))

    def test_non_opex_day_returns_neutral(self):
        from core.opex_calendar import get_opex_status
        status = get_opex_status(datetime(2026, 4, 18, 14, 0))  # Saturday, day after OpEx
        self.assertFalse(status.is_opex_day)
        self.assertEqual(status.size_reduction_factor, 1.0)

    def test_triple_witching_has_more_restrictive_rules(self):
        from core.opex_calendar import get_opex_status
        # June 2026 3rd Friday (quarter-end)
        status = get_opex_status(datetime(2026, 6, 19, 14, 45))
        self.assertTrue(status.is_triple_witching)
        self.assertTrue(status.in_last_hour_window)
        self.assertLess(status.size_reduction_factor, 1.0)
        self.assertGreater(status.conviction_threshold_bonus, 0)
        self.assertTrue(status.veto_continuation_patterns)


# ═════════════════════════════════════════════════════════════════════
# es_confirmation
# ═════════════════════════════════════════════════════════════════════
class TestESConfirmation(unittest.TestCase):
    def test_no_es_file_returns_unavailable(self):
        from core.es_confirmation import check_confirmation, ES_REGIME_FILE
        # Ensure no test file exists
        if ES_REGIME_FILE.exists():
            ES_REGIME_FILE.unlink()
        result = check_confirmation("POSITIVE")
        self.assertFalse(result.es_data_available)
        self.assertEqual(result.confluence_adjust, 0)

    def test_aligned_regimes_add_bonus(self):
        from core.es_confirmation import check_confirmation, seed_es_regime_file, ES_REGIME_FILE
        seed_es_regime_file("POSITIVE", net_gex_bn=4.2)
        try:
            result = check_confirmation("POSITIVE")
            self.assertTrue(result.aligned)
            self.assertEqual(result.confluence_adjust, 5)
        finally:
            if ES_REGIME_FILE.exists():
                ES_REGIME_FILE.unlink()

    def test_diverged_regimes_penalty(self):
        from core.es_confirmation import check_confirmation, seed_es_regime_file, ES_REGIME_FILE
        seed_es_regime_file("NEGATIVE", net_gex_bn=-3.0)
        try:
            result = check_confirmation("POSITIVE")
            self.assertFalse(result.aligned)
            self.assertEqual(result.confluence_adjust, -5)
        finally:
            if ES_REGIME_FILE.exists():
                ES_REGIME_FILE.unlink()


# ═════════════════════════════════════════════════════════════════════
# structural_bias (composite)
# ═════════════════════════════════════════════════════════════════════
class TestStructuralBias(unittest.TestCase):
    def test_empty_snapshot_returns_neutral(self):
        from core.structural_bias import compute_structural_bias
        bias = compute_structural_bias({})
        self.assertEqual(bias.label, "NEUTRAL")
        self.assertEqual(bias.score, 0)

    def test_bullish_scenario_produces_bullish_label(self):
        from core.structural_bias import compute_structural_bias
        snap = {
            "close": 26820.0,
            "swing_state": {"trend": "UP", "last_high_class": "HH", "last_low_class": "HL",
                            "last_bos_direction": "UP", "last_bos_ago_s": 500},
            "footprint_signals": [{"pattern": "STACKED_IMBALANCE_BUY", "direction": "BULLISH",
                                   "severity": 0.8, "price": 26815}],
            "chart_patterns_v1": [{"pattern_name": "bull_flag", "direction": "LONG", "confidence": 80}],
            "menthorq": {"gex_regime": "POSITIVE", "hvl": 25290,
                         "call_resistance_all": 26500, "put_support_all": 24000,
                         "allow_longs": True, "allow_shorts": False, "age_hours": 0.1},
        }
        bias = compute_structural_bias(snap)
        self.assertIn(bias.label, ("BULLISH", "STRONG_BULLISH"))
        self.assertGreater(bias.score, 0)

    def test_reasoning_trail_non_empty_on_active_components(self):
        from core.structural_bias import compute_structural_bias
        # Multiple bearish components → meaningful score
        snap = {
            "close": 26500,
            "swing_state": {"trend": "DOWN", "last_high_class": "LH", "last_low_class": "LL",
                            "last_bos_direction": "DOWN", "last_bos_ago_s": 500},
            "footprint_signals": [{"pattern": "STACKED_IMBALANCE_SELL", "direction": "BEARISH",
                                   "severity": 0.8, "price": 26500}],
            "chart_patterns_v1": [{"pattern_name": "bear_flag", "direction": "SHORT", "confidence": 75}],
        }
        bias = compute_structural_bias(snap)
        self.assertIn(bias.label, ("BEARISH", "STRONG_BEARISH"))
        trail = bias.reasoning_trail()
        self.assertIn("swing_structure", trail)

    def test_vetoes_recorded_separately(self):
        from core.structural_bias import compute_structural_bias
        snap = {
            "close": 26500,
            "pinning_state": {"pin_risk_active": True, "pin_level_name": "0DTE_CR", "pinning_level": 26500},
            "menthorq": {"age_hours": 48},  # Stale
        }
        bias = compute_structural_bias(snap)
        self.assertEqual(len(bias.vetoes), 2)
        self.assertTrue(any("PIN_RISK" in v for v in bias.vetoes))
        self.assertTrue(any("STALE" in v for v in bias.vetoes))


# ═════════════════════════════════════════════════════════════════════
# Test runner
# ═════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
