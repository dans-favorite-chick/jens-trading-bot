"""
B12 — VWAPPullback must inherit BaseStrategy and return canonical Signal.

Pre-B12 state: class VWAPPullback (no base) with a non-canonical
VWAPPullbackSignal return type → base_bot.load_strategies skipped it
with a WARN, leaving vwap_pullback disabled at runtime.

Run: python -m unittest tests.test_b12_vwap_pullback_base -v
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class _Bar:
    open: float
    high: float
    low: float
    close: float
    volume: int = 100


class TestInheritance(unittest.TestCase):
    def test_vwap_pullback_is_basestrategy_subclass(self):
        from strategies.base_strategy import BaseStrategy
        from strategies.vwap_pullback import VWAPPullback
        self.assertTrue(issubclass(VWAPPullback, BaseStrategy),
                        "VWAPPullback must inherit BaseStrategy (B12 fix)")

    def test_instance_has_validated_attribute(self):
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({"enabled": True, "validated": False})
        self.assertFalse(inst.validated)
        self.assertTrue(inst.enabled)

    def test_instance_exposes_params(self):
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({"enabled": True, "validated": False, "rsi_period": 2})
        params = inst.params
        self.assertIn("rsi_period", params)
        self.assertNotIn("enabled", params)
        self.assertNotIn("validated", params)


class TestCanonicalSignalReturn(unittest.TestCase):
    def _bars(self, n=60, base=22000, trend="flat"):
        out = []
        for i in range(n):
            if trend == "up":
                close = base + i * 0.5
            elif trend == "down":
                close = base - i * 0.5
            else:
                close = base + (i % 3 - 1) * 2
            out.append(_Bar(
                open=close - 0.25, high=close + 0.5, low=close - 0.5,
                close=close, volume=100,
            ))
        return out

    def test_no_signal_when_insufficient_bars(self):
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({})
        sig = inst.evaluate(
            market={"tf_votes_bullish": 3, "tf_votes_bearish": 0},
            bars_5m=self._bars(20),
            bars_1m=[],
            session_info={},
        )
        self.assertIsNone(sig)

    def test_no_signal_when_tf_votes_neutral(self):
        """With no clear HTF majority, safe_to_long/safe_to_short both false → None."""
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({})
        sig = inst.evaluate(
            market={"tf_votes_bullish": 2, "tf_votes_bearish": 2},
            bars_5m=self._bars(60),
            bars_1m=[],
            session_info={},
        )
        self.assertIsNone(sig)

    def test_signature_matches_basestrategy(self):
        """evaluate(market, bars_5m, bars_1m, session_info) — canonical."""
        import inspect
        from strategies.vwap_pullback import VWAPPullback
        sig = inspect.signature(VWAPPullback.evaluate)
        params = list(sig.parameters.keys())
        # self + 4 canonical args
        self.assertEqual(params[:5], ["self", "market", "bars_5m", "bars_1m", "session_info"])


class TestMTFTrendShimDerivation(unittest.TestCase):
    def test_strong_bullish_tf_votes_produces_safe_to_long(self):
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({})
        shim = inst._derive_mtf_trend_from_market({
            "tf_votes_bullish": 4, "tf_votes_bearish": 0,
        })
        self.assertTrue(shim.safe_to_long)
        self.assertFalse(shim.safe_to_short)
        self.assertEqual(shim.htf_trend, "UP")
        self.assertAlmostEqual(shim.confidence, 1.0, places=3)

    def test_strong_bearish_tf_votes_produces_safe_to_short(self):
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({})
        shim = inst._derive_mtf_trend_from_market({
            "tf_votes_bullish": 0, "tf_votes_bearish": 4,
        })
        self.assertTrue(shim.safe_to_short)
        self.assertFalse(shim.safe_to_long)
        self.assertEqual(shim.htf_trend, "DOWN")

    def test_neutral_tf_votes_produces_neutral(self):
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({})
        shim = inst._derive_mtf_trend_from_market({
            "tf_votes_bullish": 2, "tf_votes_bearish": 2,
        })
        self.assertFalse(shim.safe_to_long)
        self.assertFalse(shim.safe_to_short)
        self.assertEqual(shim.htf_trend, "NEUTRAL")

    def test_missing_tf_votes_defaults_safely(self):
        """If market doesn't supply tf_votes_*, no signal — don't crash."""
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({})
        shim = inst._derive_mtf_trend_from_market({})
        self.assertFalse(shim.safe_to_long)
        self.assertFalse(shim.safe_to_short)


class TestCanonicalConversion(unittest.TestCase):
    def test_to_canonical_preserves_fields(self):
        from strategies.vwap_pullback import VWAPPullback, VWAPPullbackSignal
        from strategies.base_strategy import Signal
        inst = VWAPPullback({})
        v2 = VWAPPullbackSignal(
            direction="LONG",
            entry_price=22000.0,
            stop_price=21980.0,
            target_price=22040.0,
            stop_ticks=80,
            target_rr=2.0,
            confidence=70.0,
            confluences=["VWAP", "RSI low"],
            reason="test",
        )
        canonical = inst._to_canonical(v2)
        self.assertIsInstance(canonical, Signal)
        self.assertEqual(canonical.direction, "LONG")
        self.assertEqual(canonical.entry_price, 22000.0)
        self.assertEqual(canonical.stop_price, 21980.0)
        self.assertEqual(canonical.target_price, 22040.0)
        self.assertEqual(canonical.stop_ticks, 80)
        self.assertEqual(canonical.target_rr, 2.0)
        self.assertEqual(canonical.entry_type, "LIMIT")  # v4 matrix row 3
        self.assertEqual(canonical.stop_type, "STOPMARKET")  # Signal default
        self.assertEqual(canonical.strategy, "vwap_pullback")
        self.assertTrue(canonical.atr_stop_override)

    def test_to_canonical_confluences_are_copied_not_shared(self):
        """Defensive: convert should copy the list, not alias it."""
        from strategies.vwap_pullback import VWAPPullback, VWAPPullbackSignal
        inst = VWAPPullback({})
        confl = ["a", "b"]
        v2 = VWAPPullbackSignal(
            direction="LONG", entry_price=1, stop_price=1, target_price=1,
            stop_ticks=1, target_rr=1, confidence=1, confluences=confl,
            reason="",
        )
        canonical = inst._to_canonical(v2)
        confl.append("c")
        self.assertEqual(len(canonical.confluences), 2)


class TestBaseBotLoaderAcceptsIt(unittest.TestCase):
    """The defensive base_bot loader (from earlier tonight) would now
    ACCEPT VWAPPullback as a conforming strategy. isinstance check
    against BaseStrategy should pass."""

    def test_base_bot_guard_would_accept(self):
        from strategies.base_strategy import BaseStrategy
        from strategies.vwap_pullback import VWAPPullback
        inst = VWAPPullback({"enabled": True, "validated": False})
        self.assertIsInstance(inst, BaseStrategy,
                              "base_bot.load_strategies isinstance guard must accept this")


if __name__ == "__main__":
    unittest.main()
