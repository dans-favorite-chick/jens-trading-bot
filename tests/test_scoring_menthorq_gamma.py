"""
S3 / B33 — tests for the rewired core.structural_bias.score_menthorq_gamma.

The scorer used to read Path A (stale data/menthorq/menthorq_daily.json via
market_snapshot["menthorq"] → gex_regime + HVL + CR/PS numerics). It now
reads Path B directly: market_snapshot["gamma_regime"] (a GammaRegime enum
populated by bots/base_bot._enrich_market_with_gamma from fresh
data/menthorq/gamma/*_levels.txt + B27 classify_regime).

These tests pin:
  1. The scorer does NOT touch the legacy menthorq_daily.json file.
  2. It reads gamma_regime from the snapshot (enum OR string).
  3. It gracefully handles a missing / UNKNOWN / None gamma_regime.
  4. Optional gamma_nearest_wall proximity bumps the score.

Run: pytest tests/test_scoring_menthorq_gamma.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.menthorq_gamma import GammaRegime
from core.structural_bias import WEIGHTS, score_menthorq_gamma


# ══════════════════════════════════════════════════════════════════════
# Path B wiring — reads gamma_regime from the snapshot
# ══════════════════════════════════════════════════════════════════════
class TestReadsFromSnapshot:
    def test_positive_strong_scores_positive(self):
        snap = {"gamma_regime": GammaRegime.POSITIVE_STRONG}
        pts, reason = score_menthorq_gamma(snap, price=26000.0)
        assert pts > 0
        assert "POS_STRONG" in reason

    def test_positive_normal_scores_positive(self):
        snap = {"gamma_regime": GammaRegime.POSITIVE_NORMAL}
        pts, _ = score_menthorq_gamma(snap, price=26000.0)
        assert pts > 0

    def test_negative_strong_scores_negative(self):
        snap = {"gamma_regime": GammaRegime.NEGATIVE_STRONG}
        pts, reason = score_menthorq_gamma(snap, price=25000.0)
        assert pts < 0
        assert "NEG_STRONG" in reason

    def test_negative_normal_scores_negative(self):
        snap = {"gamma_regime": GammaRegime.NEGATIVE_NORMAL}
        pts, _ = score_menthorq_gamma(snap, price=25000.0)
        assert pts < 0

    def test_neutral_scores_zero_with_context(self):
        snap = {"gamma_regime": GammaRegime.NEUTRAL}
        pts, reason = score_menthorq_gamma(snap, price=25500.0)
        assert pts == 0
        assert "NEUTRAL" in reason

    def test_accepts_string_regime_for_back_compat(self):
        # Legacy / test harnesses may pass the enum value or name as a string.
        snap = {"gamma_regime": "POSITIVE_STRONG"}
        pts, _ = score_menthorq_gamma(snap, price=26000.0)
        assert pts > 0


# ══════════════════════════════════════════════════════════════════════
# Graceful degradation
# ══════════════════════════════════════════════════════════════════════
class TestGracefulDegradation:
    def test_empty_snapshot_returns_zero(self):
        assert score_menthorq_gamma({}, 25000.0) == (0, "no MQ data")

    def test_none_snapshot_returns_zero(self):
        assert score_menthorq_gamma(None, 25000.0) == (0, "no MQ data")

    def test_missing_gamma_regime_returns_zero(self):
        snap = {"close": 25000.0, "price": 25000.0}  # no gamma_regime key
        pts, reason = score_menthorq_gamma(snap, 25000.0)
        assert pts == 0
        assert "no gamma_regime" in reason

    def test_unknown_regime_returns_zero(self):
        snap = {"gamma_regime": GammaRegime.UNKNOWN}
        pts, reason = score_menthorq_gamma(snap, 25000.0)
        assert pts == 0
        assert "UNKNOWN" in reason


# ══════════════════════════════════════════════════════════════════════
# Path A retirement — scorer must NOT read menthorq_daily.json
# ══════════════════════════════════════════════════════════════════════
class TestPathARetired:
    def test_does_not_open_menthorq_daily_json(self):
        """The old scorer read market_snapshot['menthorq'] which originated
        from data/menthorq/menthorq_daily.json. The new scorer should not
        touch that dict at all — prove it by passing a snapshot whose
        'menthorq' sub-dict is poisoned. A correctly-rewired scorer ignores
        it entirely and goes to gamma_regime.
        """
        poisoned = {
            "menthorq": {
                "gex_regime": "POSITIVE",          # old Path A field
                "hvl": 99999,                      # would bias score under old impl
                "call_resistance_all": 25000,
                "put_support_all": 25000,
            },
            "gamma_regime": GammaRegime.NEGATIVE_STRONG,  # Path B truth
        }
        pts, reason = score_menthorq_gamma(poisoned, price=25100.0)
        # If Path A were still wired, the HVL/CR/PS combo would push pts
        # positive. With Path B authoritative, result is strongly negative.
        assert pts < 0, f"expected Path B to dominate; got pts={pts} reason={reason!r}"
        assert "NEG_STRONG" in reason

    def test_scorer_makes_no_file_io(self):
        """Belt-and-braces: sanity-check that score_menthorq_gamma does not
        perform any ``open()`` call (Path A's legacy json.load was the only
        reason it ever would have)."""
        snap = {"gamma_regime": GammaRegime.POSITIVE_NORMAL}
        with patch("builtins.open", side_effect=AssertionError(
            "score_menthorq_gamma must not open any file (Path A retired)"
        )):
            pts, _ = score_menthorq_gamma(snap, price=26000.0)
        assert pts > 0


# ══════════════════════════════════════════════════════════════════════
# Optional wall-proximity enrichment
# ══════════════════════════════════════════════════════════════════════
class TestWallProximity:
    def test_near_call_resistance_adds_bearish(self):
        snap = {
            "gamma_regime": GammaRegime.NEUTRAL,
            "gamma_nearest_wall": ("call_resistance", 2.0),
        }
        pts, reason = score_menthorq_gamma(snap, price=26500.0)
        assert pts < 0
        assert "call_resistance" in reason

    def test_near_put_support_adds_bullish(self):
        snap = {
            "gamma_regime": GammaRegime.NEUTRAL,
            "gamma_nearest_wall": ("put_support", 1.5),
        }
        pts, reason = score_menthorq_gamma(snap, price=25000.0)
        assert pts > 0
        assert "put_support" in reason

    def test_far_wall_ignored(self):
        snap = {
            "gamma_regime": GammaRegime.NEUTRAL,
            "gamma_nearest_wall": ("call_resistance", 50.0),
        }
        pts, _ = score_menthorq_gamma(snap, price=25500.0)
        assert pts == 0


# ══════════════════════════════════════════════════════════════════════
# Clamping — score never exceeds menthorq_gamma weight
# ══════════════════════════════════════════════════════════════════════
class TestClamping:
    def test_score_bounded_by_weight(self):
        max_w = WEIGHTS["menthorq_gamma"]
        for regime in GammaRegime:
            snap = {
                "gamma_regime": regime,
                "gamma_nearest_wall": ("put_support", 0.5),  # stack bullish
            }
            pts, _ = score_menthorq_gamma(snap, price=25000.0)
            assert -max_w <= pts <= max_w


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
