"""
B27 tests for core/menthorq_gamma.py — schema extension, parser
aliases with K/M/B suffix, 6-value enum, Net GEX primary classifier
with HVL fallback, regime multipliers.

Run: pytest tests/test_menthorq_gamma.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.menthorq_gamma import (
    GammaLevels,
    GammaRegime,
    classify_regime,
    load_gamma_for_date,
    parse_gamma_paste,
    regime_multipliers,
)


# ═══════════════════════════════════════════════════════════════════
# Parser — Net GEX / Total GEX / IV
# ═══════════════════════════════════════════════════════════════════
class TestParserNetGex:
    def test_parses_net_gex_positive_integer(self):
        g = parse_gamma_paste("$NQ: Net GEX, 3920000")
        assert g.net_gex == 3_920_000

    def test_parses_net_gex_negative_integer(self):
        g = parse_gamma_paste("$NQ: Net GEX, -4500000")
        assert g.net_gex == -4_500_000

    def test_parses_net_gex_with_M_suffix(self):
        g = parse_gamma_paste("$NQ: Net GEX, 3.92M")
        assert g.net_gex == pytest.approx(3_920_000)

    def test_parses_net_gex_with_negative_M_suffix(self):
        g = parse_gamma_paste("$NQ: Net GEX, -3.92M")
        assert g.net_gex == pytest.approx(-3_920_000)

    def test_parses_net_gex_with_K_and_B_suffix(self):
        g = parse_gamma_paste("$NQ: Net GEX, 500K, Total GEX, 2.5B")
        assert g.net_gex == pytest.approx(500_000)
        assert g.total_gex == pytest.approx(2_500_000_000)

    def test_parses_total_gex_and_iv_fields(self):
        g = parse_gamma_paste("$NQ: Net GEX, 3920000, Total GEX, 9330000, IV, 19.42")
        assert g.net_gex == 3_920_000
        assert g.total_gex == 9_330_000
        assert g.iv_30d == pytest.approx(19.42)

    def test_iv_accepts_30d_suffix_alias(self):
        g = parse_gamma_paste("$NQ: IV 30D, 21.15")
        assert g.iv_30d == pytest.approx(21.15)


# ═══════════════════════════════════════════════════════════════════
# Schema — has_net_gex_classification + is_complete invariance
# ═══════════════════════════════════════════════════════════════════
class TestSchema:
    def test_has_net_gex_classification_true_when_present(self):
        g = parse_gamma_paste("$NQ: Net GEX, 1000000")
        assert g.has_net_gex_classification is True

    def test_has_net_gex_classification_false_when_none(self):
        g = parse_gamma_paste("$NQ: HVL, 25000")
        assert g.has_net_gex_classification is False

    def test_is_complete_unchanged_when_net_gex_missing(self):
        # Tier 1 walls present, no Net GEX → still complete.
        paste = (
            "$NQ: Call Resistance, 26500, Put Support, 25000, HVL, 25275, "
            "Call Resistance 0DTE, 26800, Put Support 0DTE, 26560, "
            "HVL 0DTE, 26700, Gamma Wall 0DTE, 26800"
        )
        g = parse_gamma_paste(paste)
        assert g.is_complete is True

    def test_is_complete_does_not_require_net_gex(self):
        paste = (
            "$NQ: Call Resistance, 26500, Put Support, 25000, HVL, 25275, "
            "Call Resistance 0DTE, 26800, Put Support 0DTE, 26560, "
            "HVL 0DTE, 26700, Gamma Wall 0DTE, 26800, Net GEX, 1000000"
        )
        g = parse_gamma_paste(paste)
        assert g.is_complete is True
        assert g.has_net_gex_classification is True


# ═══════════════════════════════════════════════════════════════════
# classify_regime — Net GEX PRIMARY path (authoritative)
# ═══════════════════════════════════════════════════════════════════
class TestClassifyRegimeNetGex:
    def _mk(self, net_gex: float) -> GammaLevels:
        return parse_gamma_paste(f"$NQ: HVL, 25000, Net GEX, {net_gex}")

    def test_classify_positive_strong_when_net_gex_above_3M(self):
        # Price is unused when Net GEX present — pass any value.
        assert classify_regime(0, self._mk(3_500_000)) is GammaRegime.POSITIVE_STRONG
        assert classify_regime(0, self._mk(10_000_000)) is GammaRegime.POSITIVE_STRONG

    def test_classify_positive_normal_when_net_gex_500K_to_3M(self):
        assert classify_regime(0, self._mk(600_000)) is GammaRegime.POSITIVE_NORMAL
        assert classify_regime(0, self._mk(2_999_999)) is GammaRegime.POSITIVE_NORMAL

    def test_classify_neutral_when_net_gex_within_500K_of_zero(self):
        assert classify_regime(0, self._mk(0)) is GammaRegime.NEUTRAL
        assert classify_regime(0, self._mk(100_000)) is GammaRegime.NEUTRAL
        assert classify_regime(0, self._mk(-400_000)) is GammaRegime.NEUTRAL

    def test_classify_negative_normal_when_net_gex_neg_500K_to_neg_3M(self):
        assert classify_regime(0, self._mk(-600_000)) is GammaRegime.NEGATIVE_NORMAL
        assert classify_regime(0, self._mk(-2_999_999)) is GammaRegime.NEGATIVE_NORMAL

    def test_classify_negative_strong_when_net_gex_below_neg_3M(self):
        assert classify_regime(0, self._mk(-3_500_000)) is GammaRegime.NEGATIVE_STRONG
        assert classify_regime(0, self._mk(-10_000_000)) is GammaRegime.NEGATIVE_STRONG


# ═══════════════════════════════════════════════════════════════════
# classify_regime — HVL FALLBACK path (no Net GEX in paste)
# ═══════════════════════════════════════════════════════════════════
class TestClassifyRegimeHVLFallback:
    def test_classify_falls_back_to_hvl_proxy_when_net_gex_none(self):
        g = parse_gamma_paste("$NQ: HVL, 25000")
        # Price well above HVL + buffer (8 ticks * 0.25 = 2 pts)
        assert classify_regime(25100, g) is GammaRegime.POSITIVE_NORMAL
        # Price well below HVL - buffer
        assert classify_regime(24900, g) is GammaRegime.NEGATIVE_NORMAL

    def test_classify_hvl_proxy_neutral_within_buffer(self):
        g = parse_gamma_paste("$NQ: HVL, 25000")
        # Price inside the [HVL - buffer, HVL + buffer] band
        assert classify_regime(25001, g) is GammaRegime.NEUTRAL
        assert classify_regime(25000, g) is GammaRegime.NEUTRAL

    def test_classify_unknown_when_both_net_gex_and_hvl_missing(self):
        # Paste with only walls, no HVL and no Net GEX.
        g = parse_gamma_paste("$NQ: Call Resistance, 26500, Put Support, 25000")
        assert classify_regime(26000, g) is GammaRegime.UNKNOWN

    def test_classify_unknown_when_levels_is_none(self):
        assert classify_regime(26000, None) is GammaRegime.UNKNOWN


# ═══════════════════════════════════════════════════════════════════
# regime_multipliers — new 6-value coverage
# ═══════════════════════════════════════════════════════════════════
class TestRegimeMultipliers:
    def test_positive_strong_has_tightest_stop(self):
        strong = regime_multipliers(GammaRegime.POSITIVE_STRONG)
        normal = regime_multipliers(GammaRegime.POSITIVE_NORMAL)
        assert strong["stop"] < normal["stop"]
        assert strong["target_rr"] < normal["target_rr"]

    def test_neutral_reduces_size(self):
        neutral = regime_multipliers(GammaRegime.NEUTRAL)
        pos = regime_multipliers(GammaRegime.POSITIVE_NORMAL)
        assert neutral["size"] < pos["size"]

    def test_negative_strong_has_widest_stop(self):
        strong = regime_multipliers(GammaRegime.NEGATIVE_STRONG)
        normal = regime_multipliers(GammaRegime.NEGATIVE_NORMAL)
        assert strong["stop"] > normal["stop"]
        assert strong["target_rr"] > normal["target_rr"]

    def test_every_regime_has_multipliers(self):
        for r in GammaRegime:
            m = regime_multipliers(r)
            assert set(m.keys()) == {"size", "stop", "target_rr"}


# ═══════════════════════════════════════════════════════════════════
# End-to-end with today's real data (2026-04-21)
# ═══════════════════════════════════════════════════════════════════
_REAL_LEVELS_2026_04_21 = (
    "$NQM2026: Call Resistance, 26500, Put Support, 25000, HVL, 25275, "
    "1D Min, 26421.44, 1D Max, 27076.06, Call Resistance 0DTE, 26800, "
    "Put Support 0DTE, 26560, HVL 0DTE, 26700, Gamma Wall 0DTE, 26800, "
    "GEX 1, 27000, GEX 2, 26750, GEX 3, 26900, GEX 4, 26600, GEX 5, 26850, "
    "GEX 6, 26250, GEX 7, 26400, GEX 8, 26100, GEX 9, 27250, GEX 10, 26200"
)


class TestRealData20260421:
    def test_real_2026_04_21_data_without_net_gex_classifies_via_hvl(self):
        # Today's actual file on disk — no Net GEX yet. Classifier must
        # fall back to HVL proxy (effective HVL = hvl_0dte = 26700).
        g = parse_gamma_paste(_REAL_LEVELS_2026_04_21)
        assert g.has_net_gex_classification is False
        assert g.is_complete is True
        # Price 26700+3 (above effective HVL + 2pt buffer) → POSITIVE_NORMAL
        assert classify_regime(26703, g) is GammaRegime.POSITIVE_NORMAL
        # Price 25000 (well below effective HVL) → NEGATIVE_NORMAL
        assert classify_regime(25000, g) is GammaRegime.NEGATIVE_NORMAL

    def test_real_2026_04_21_data_with_net_gex_appended_classifies_positive_strong(self):
        # Jennifer's planned append: add Net GEX 3.92M to the same paste.
        paste = _REAL_LEVELS_2026_04_21 + ", Net GEX, 3920000, Total GEX, 9330000, IV, 19.42"
        g = parse_gamma_paste(paste)
        assert g.has_net_gex_classification is True
        assert g.net_gex == 3_920_000
        # Price is irrelevant when Net GEX present; regime is POSITIVE_STRONG.
        assert classify_regime(26000, g) is GammaRegime.POSITIVE_STRONG
        assert classify_regime(0, g) is GammaRegime.POSITIVE_STRONG

    def test_load_gamma_for_date_preserves_net_gex_through_cache(self, tmp_path):
        # Simulate the real file-loading path with Net GEX present.
        from datetime import date as _date
        levels_path = tmp_path / "2026-04-21_levels.txt"
        levels_path.write_text(
            _REAL_LEVELS_2026_04_21 + ", Net GEX, 3920000, IV, 19.42\n",
            encoding="utf-8",
        )
        g = load_gamma_for_date(tmp_path, _date(2026, 4, 21))
        assert g is not None
        assert g.net_gex == 3_920_000
        assert g.iv_30d == pytest.approx(19.42)
        assert classify_regime(26000, g) is GammaRegime.POSITIVE_STRONG


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
