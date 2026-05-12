"""Sprint M Tier 1 — context-alignment IQS bonuses.

Four 5-point sub-bonuses on footprint_cvd_reversal's IQS scoring:
  a. structural_bias_aligned   (market["structural_bias"]["label"])
  b. sweep_aligned             (market["sweep_state"]["watches"])
  c. multi_tf_cvd_aligned      (computed from bars_history + latest)
  d. poc_migration_aligned     (computed from bars_history POC trend)

These tests verify each sub-bonus fires in the right conditions and
doesn't fire in the wrong ones. They use the pure-function form
`_score_context_bonuses` directly — no full strategy spin-up needed.

Run: python -m unittest tests.test_footprint_cvd_context_bonuses -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies.footprint_cvd_reversal import _score_context_bonuses


def _make_bar(delta: float = 0.0, poc: float = 29200.0,
              cvd_session: float = 0.0) -> dict:
    return {
        "delta": delta,
        "poc": poc,
        "cvd_session": cvd_session,
        "open": 29200.0, "high": 29205.0, "low": 29195.0, "close": 29200.0,
        "total_volume": 1000,
        "buy_volume": 500, "sell_volume": 500,
    }


def _flat_history(n: int = 20) -> list[dict]:
    """N flat bars — no delta, no POC migration, neutral cvd."""
    return [_make_bar() for _ in range(n)]


# ═══════════════════════════════════════════════════════════════════
# (a) structural_bias_aligned
# ═══════════════════════════════════════════════════════════════════
class TestStructuralBiasBonus(unittest.TestCase):

    def test_long_with_bullish_bias_gets_bonus(self):
        market = {"structural_bias": {"label": "BULLISH"}}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "long"
        )
        self.assertTrue(debug["structural_bias_aligned"])
        self.assertGreaterEqual(bonus, 5)

    def test_short_with_strong_bearish_bias_gets_bonus(self):
        market = {"structural_bias": {"label": "STRONG_BEARISH"}}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "short"
        )
        self.assertTrue(debug["structural_bias_aligned"])

    def test_long_with_bearish_bias_no_bonus(self):
        # Counter-bias trade: no bonus (but no penalty either).
        market = {"structural_bias": {"label": "BEARISH"}}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "long"
        )
        self.assertFalse(debug["structural_bias_aligned"])

    def test_neutral_bias_no_bonus(self):
        market = {"structural_bias": {"label": "NEUTRAL"}}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "long"
        )
        self.assertFalse(debug["structural_bias_aligned"])

    def test_missing_structural_bias_no_crash(self):
        # Pre-Sprint-M markets don't have structural_bias field.
        bonus, debug = _score_context_bonuses(
            {}, _make_bar(), _flat_history(), "long"
        )
        self.assertFalse(debug["structural_bias_aligned"])
        self.assertEqual(debug["structural_bias_label"], "")


# ═══════════════════════════════════════════════════════════════════
# (b) sweep_aligned
# ═══════════════════════════════════════════════════════════════════
class TestSweepBonus(unittest.TestCase):

    def test_long_with_down_sweep_gets_bonus(self):
        """A down-sweep took out stops below price = trapped shorts =
        LONG reversal setup."""
        market = {"sweep_state": {
            "watches": [{"pivot": 29180.0, "break_direction": "down"}]
        }}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "long"
        )
        self.assertTrue(debug["sweep_aligned"])
        self.assertEqual(debug["sweep_pivot"], 29180.0)

    def test_short_with_up_sweep_gets_bonus(self):
        market = {"sweep_state": {
            "watches": [{"pivot": 29220.0, "break_direction": "up"}]
        }}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "short"
        )
        self.assertTrue(debug["sweep_aligned"])

    def test_long_with_up_sweep_no_bonus(self):
        """Wrong-direction sweep — no alignment."""
        market = {"sweep_state": {
            "watches": [{"pivot": 29220.0, "break_direction": "up"}]
        }}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "long"
        )
        self.assertFalse(debug["sweep_aligned"])

    def test_empty_watches_no_bonus(self):
        market = {"sweep_state": {"watches": []}}
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "long"
        )
        self.assertFalse(debug["sweep_aligned"])


# ═══════════════════════════════════════════════════════════════════
# (c) multi_tf_cvd_aligned
# ═══════════════════════════════════════════════════════════════════
class TestMultiTFCvdBonus(unittest.TestCase):

    def _bullish_history(self, n: int = 12) -> list[dict]:
        # Every bar has positive delta ⇒ all 3 windows + session positive.
        return [_make_bar(delta=20.0) for _ in range(n)]

    def _bearish_history(self, n: int = 12) -> list[dict]:
        return [_make_bar(delta=-20.0) for _ in range(n)]

    def test_long_with_all_positive_cvd_gets_bonus(self):
        latest = _make_bar(cvd_session=500.0)
        bonus, debug = _score_context_bonuses(
            {}, latest, self._bullish_history(), "long"
        )
        self.assertTrue(debug["multi_tf_cvd_aligned"])
        self.assertGreater(debug["cvd_short"], 0)
        self.assertGreater(debug["cvd_medium"], 0)

    def test_short_with_all_negative_cvd_gets_bonus(self):
        latest = _make_bar(cvd_session=-500.0)
        bonus, debug = _score_context_bonuses(
            {}, latest, self._bearish_history(), "short"
        )
        self.assertTrue(debug["multi_tf_cvd_aligned"])

    def test_mixed_cvd_no_bonus(self):
        # short window positive, medium negative ⇒ misaligned.
        history = (
            [_make_bar(delta=-30.0) for _ in range(7)]  # baseline bearish
            + [_make_bar(delta=+20.0) for _ in range(3)]  # recent flip
        )
        latest = _make_bar(cvd_session=200.0)
        bonus, debug = _score_context_bonuses({}, latest, history, "long")
        self.assertFalse(debug["multi_tf_cvd_aligned"])

    def test_insufficient_history_no_bonus(self):
        # Fewer than 10 bars: insufficient for multi-TF alignment.
        latest = _make_bar(cvd_session=500.0)
        bonus, debug = _score_context_bonuses(
            {}, latest, [_make_bar(delta=20.0) for _ in range(5)], "long"
        )
        self.assertFalse(debug["multi_tf_cvd_aligned"])

    def test_meaningless_magnitude_no_bonus(self):
        # All deltas tiny — direction agrees but signal is noise.
        history = [_make_bar(delta=0.1) for _ in range(12)]
        latest = _make_bar(cvd_session=0.5)  # too small
        bonus, debug = _score_context_bonuses({}, latest, history, "long")
        self.assertFalse(debug["multi_tf_cvd_aligned"])


# ═══════════════════════════════════════════════════════════════════
# (d) poc_migration_aligned
# ═══════════════════════════════════════════════════════════════════
class TestPocMigrationBonus(unittest.TestCase):

    def test_long_with_rising_poc_gets_bonus(self):
        # 3 bars with rising POC (29200 → 29202 → 29205 = +5pt, +20 ticks)
        history = (
            [_make_bar(poc=29200.0)] * 17
            + [_make_bar(poc=29200.0), _make_bar(poc=29202.0), _make_bar(poc=29205.0)]
        )
        bonus, debug = _score_context_bonuses(
            {}, _make_bar(), history, "long"
        )
        self.assertTrue(debug["poc_migration_aligned"])
        self.assertGreater(debug["poc_migration_ticks"], 0)

    def test_short_with_falling_poc_gets_bonus(self):
        history = (
            [_make_bar(poc=29200.0)] * 17
            + [_make_bar(poc=29200.0), _make_bar(poc=29197.0), _make_bar(poc=29193.0)]
        )
        bonus, debug = _score_context_bonuses(
            {}, _make_bar(), history, "short"
        )
        self.assertTrue(debug["poc_migration_aligned"])

    def test_long_with_falling_poc_no_bonus(self):
        history = (
            [_make_bar(poc=29200.0)] * 17
            + [_make_bar(poc=29200.0), _make_bar(poc=29197.0), _make_bar(poc=29193.0)]
        )
        bonus, debug = _score_context_bonuses(
            {}, _make_bar(), history, "long"
        )
        self.assertFalse(debug["poc_migration_aligned"])

    def test_static_poc_no_bonus(self):
        # POC unchanged across window — no migration signal.
        bonus, debug = _score_context_bonuses(
            {}, _make_bar(), [_make_bar(poc=29200.0)] * 5, "long"
        )
        self.assertFalse(debug["poc_migration_aligned"])

    def test_tiny_migration_below_threshold_no_bonus(self):
        # 1-tick migration (0.25 pt) — below the 2-tick threshold.
        history = (
            [_make_bar(poc=29200.0)] * 17
            + [_make_bar(poc=29200.0), _make_bar(poc=29200.25), _make_bar(poc=29200.25)]
        )
        bonus, debug = _score_context_bonuses(
            {}, _make_bar(), history, "long"
        )
        self.assertFalse(debug["poc_migration_aligned"])


# ═══════════════════════════════════════════════════════════════════
# Aggregation: cap at +20, all four sub-bonuses can stack
# ═══════════════════════════════════════════════════════════════════
class TestAggregation(unittest.TestCase):

    def test_all_four_aligned_gives_20(self):
        market = {
            "structural_bias": {"label": "BULLISH"},
            "sweep_state": {"watches": [
                {"pivot": 29180.0, "break_direction": "down"}
            ]},
        }
        history = (
            [_make_bar(delta=20.0, poc=29198.0)] * 17
            + [_make_bar(delta=20.0, poc=29200.0)]
            + [_make_bar(delta=20.0, poc=29202.0)]
            + [_make_bar(delta=20.0, poc=29205.0)]
        )
        latest = _make_bar(cvd_session=400.0)
        bonus, debug = _score_context_bonuses(market, latest, history, "long")
        self.assertEqual(bonus, 20,
                         f"all four aligned should max at 20, got {bonus}; {debug}")

    def test_no_alignments_gives_zero(self):
        bonus, debug = _score_context_bonuses(
            {}, _make_bar(), _flat_history(), "long"
        )
        self.assertEqual(bonus, 0)

    def test_two_aligned_gives_10(self):
        # Bias + sweep aligned, CVD/POC neutral.
        market = {
            "structural_bias": {"label": "BULLISH"},
            "sweep_state": {"watches": [
                {"pivot": 29180.0, "break_direction": "down"}
            ]},
        }
        bonus, debug = _score_context_bonuses(
            market, _make_bar(), _flat_history(), "long"
        )
        self.assertEqual(bonus, 10)


# ═══════════════════════════════════════════════════════════════════
# Tier 1.1 — C# adaptive ratio static check
# ═══════════════════════════════════════════════════════════════════
class TestAdaptiveImbalanceRatioCSharp(unittest.TestCase):
    """The C# can't be unit-tested from Python directly, but we can verify
    the source file has the right shape: the fixed constant was removed,
    the adaptive method exists, and the emit path uses the per-bar value."""

    def setUp(self):
        self.src = (Path(__file__).parent.parent
                    / "ninjatrader" / "TickStreamer.cs").read_text(
            encoding="utf-8"
        )

    def test_fixed_constant_removed(self):
        # The old `private const double IMBALANCE_RATIO = 3.0;` line
        # should no longer exist. (Strict literal match — if someone
        # adds it back, regression.)
        self.assertNotIn(
            "private const double IMBALANCE_RATIO     = 3.0",
            self.src,
            "fixed IMBALANCE_RATIO constant should have been removed "
            "(Sprint M Tier 1.1)",
        )

    def test_adaptive_method_present(self):
        self.assertIn("GetAdaptiveImbalanceRatio", self.src,
                      "GetAdaptiveImbalanceRatio method missing")

    def test_per_bar_variable_used_in_loop(self):
        # The imbalance-detection branches must compare against the
        # per-bar `imbalanceRatio` variable, not the old constant.
        self.assertIn("askVol >= imbalanceRatio * bidVol", self.src)
        self.assertIn("bidVol >= imbalanceRatio * askVol", self.src)

    def test_ratio_emitted_in_json(self):
        # The chosen per-bar ratio must be emitted as "imbalance_ratio"
        # so the strategy can record it for forensics.
        self.assertIn('"imbalance_ratio"', self.src)


if __name__ == "__main__":
    unittest.main()
