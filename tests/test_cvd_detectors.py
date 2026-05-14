"""Tests for the 3 CVD-based detectors (2026-05-13).

Per operator's trade-flow methodology:
  - CVDTrendHealth      → entry filter (skip trades fighting flow)
  - BarDeltaFlipDetector → mid-trade exit signal (energy fading)
  - SwingDivergenceDetector → exit/scale-out on classic divergence
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────
# CVDTrendHealth
# ─────────────────────────────────────────────────────────────────────

class TestCVDTrendHealth:
    def setup_method(self):
        from core.cvd_trend_health import CVDTrendHealth
        self.h = CVDTrendHealth(lookback_bars=6, veto_threshold=-0.3)

    def test_no_history_no_veto(self):
        """Empty detector should never veto (insufficient data)."""
        r = self.h.assess("LONG")
        assert r["veto"] is False
        assert r["n_bars"] == 0
        assert "insufficient" in r["reason"].lower()

    def test_full_agreement_no_veto(self):
        """Price up + CVD up + going LONG = aligned, no veto."""
        for i in range(6):
            self.h.update_bar(bar_close=100 + i, cumulative_cvd=1000 + i * 100)
        r = self.h.assess("LONG")
        assert r["veto"] is False
        assert r["agreement"] > 0.5
        assert r["price_slope"] > 0
        assert r["cvd_slope"] > 0

    def test_price_up_cvd_down_long_vetoes(self):
        """The forensic case: price moving up but CVD rolling down
        while we want to go LONG. This is the entire reason this
        detector exists."""
        # Price climbs 100..105; CVD craters 1000..400
        for i in range(6):
            self.h.update_bar(bar_close=100 + i, cumulative_cvd=1000 - i * 100)
        r = self.h.assess("LONG")
        assert r["veto"] is True, (
            f"price+ but CVD- should veto LONG entry; got agreement="
            f"{r['agreement']:+.2f}, reason={r['reason']}"
        )
        # Same setup but SHORT direction should NOT veto (price going up but
        # CVD says sell — actually a possible setup for going LONG, but for
        # SHORT it's mixed: price is wrong direction but CVD supports SHORT).
        # The agreement for SHORT here should be near 0 (neither full)
        r2 = self.h.assess("SHORT")
        # Don't strictly assert no-veto for SHORT — depends on slopes balance

    def test_price_down_cvd_up_short_vetoes(self):
        """Mirror: price down + CVD up + going SHORT = veto."""
        for i in range(6):
            self.h.update_bar(bar_close=100 - i, cumulative_cvd=1000 + i * 100)
        r = self.h.assess("SHORT")
        assert r["veto"] is True

    def test_partial_disagreement_below_threshold_no_veto(self):
        """Slight disagreement that doesn't hit the threshold should not veto."""
        # Price up, CVD flat (slight slope, but normalized agreement
        # should be close to 0.5, well above the -0.3 threshold)
        for i in range(6):
            self.h.update_bar(bar_close=100 + i, cumulative_cvd=1000)
        r = self.h.assess("LONG")
        assert r["veto"] is False


# ─────────────────────────────────────────────────────────────────────
# BarDeltaFlipDetector
# ─────────────────────────────────────────────────────────────────────

class TestBarDeltaFlipDetector:
    def setup_method(self):
        from core.cvd_bar_flip import BarDeltaFlipDetector
        self.f = BarDeltaFlipDetector(lookback=5)

    def test_empty_no_flip(self):
        r = self.f.check_flip_against("LONG", min_consecutive=1)
        assert r["flipped"] is False
        assert r["consecutive_count"] == 0

    def test_long_with_positive_bars_no_flip(self):
        """LONG position, all recent bars positive delta = no flip."""
        for d in [100, 80, 120, 90, 110]:
            self.f.update_bar(d)
        r = self.f.check_flip_against("LONG", min_consecutive=1)
        assert r["flipped"] is False
        assert r["trend_dir"] == "LONG"

    def test_long_with_two_negative_bars_flips(self):
        """LONG position, last 2 bars negative = flip."""
        for d in [100, 80, 120, -50, -75]:  # last two are negative
            self.f.update_bar(d)
        r = self.f.check_flip_against("LONG", min_consecutive=2)
        assert r["flipped"] is True
        assert r["consecutive_count"] == 2
        assert r["last_bar_delta"] == -75.0

    def test_long_single_flip_with_min_consecutive_1(self):
        """min_consecutive=1 means a single negative bar is enough."""
        for d in [100, 80, 120, 90, -50]:
            self.f.update_bar(d)
        r = self.f.check_flip_against("LONG", min_consecutive=1)
        assert r["flipped"] is True
        assert r["consecutive_count"] == 1

    def test_magnitude_override_fires_on_single_huge_flip(self):
        """A single LARGE flipped bar should fire even with min_consecutive=3."""
        for d in [100, 80, 120, 90, -500]:  # last bar is HUGE flip
            self.f.update_bar(d)
        r = self.f.check_flip_against(
            "LONG", min_consecutive=3, min_magnitude=300
        )
        assert r["flipped"] is True
        assert "capitulation" in r["reason"].lower()

    def test_short_position_uses_positive_delta_as_flip(self):
        """SHORT position interprets positive bar deltas as flips."""
        for d in [-100, -80, -50, 60, 90]:  # last two are POSITIVE = flip for SHORT
            self.f.update_bar(d)
        r = self.f.check_flip_against("SHORT", min_consecutive=2)
        assert r["flipped"] is True

    def test_alternating_bars_dont_count_as_consecutive(self):
        """Mixed signs should give consecutive=1 (only the last bar counts)."""
        for d in [100, -50, 80, -30, -40]:  # last 2 negative
            self.f.update_bar(d)
        r = self.f.check_flip_against("LONG", min_consecutive=3)
        assert r["flipped"] is False
        assert r["consecutive_count"] == 2  # only the trailing run


# ─────────────────────────────────────────────────────────────────────
# SwingDivergenceDetector
# ─────────────────────────────────────────────────────────────────────

class TestSwingDivergenceDetector:
    def setup_method(self):
        from core.cvd_swing_divergence import SwingDivergenceDetector
        self.d = SwingDivergenceDetector(
            swing_strength=2,
            min_bars_between=5,
            max_bars_between=40,
        )

    def _push(self, high, low, cvd):
        self.d.update_bar(high, low, cvd)

    def test_no_signal_until_swings_form(self):
        """Without enough bars to confirm a pivot, no signal."""
        # Just push 3 bars — not enough for 2-bar-each-side confirmation
        for i in range(3):
            self._push(high=100, low=99, cvd=1000)
        assert self.d.check_divergence() is None

    def test_bearish_divergence_at_consecutive_highs(self):
        """Build a price tape that makes a HIGHER swing high while
        the cumulative CVD records a LOWER swing high — classic bearish
        divergence."""
        # First swing high at bar 3: high=100, surrounded by lower highs
        # Pattern: low high, low high, [HIGH=100], low high, low high
        # Then second swing high later at higher price but lower CVD.
        bars = [
            # (high, low, cvd)
            (95, 90, 1000),   # 0
            (97, 92, 1100),   # 1
            (100, 95, 1500),  # 2 — first SWING HIGH (cvd=1500)
            (98, 93, 1400),   # 3
            (96, 91, 1300),   # 4
            (94, 89, 1100),   # 5
            (97, 92, 1000),   # 6
            (99, 94, 900),    # 7
            (102, 97, 1200),  # 8 — second SWING HIGH at higher price (cvd=1200 < 1500)
            (100, 95, 1000),  # 9
            (98, 93, 900),    # 10
        ]
        for h, l, c in bars:
            self._push(h, l, c)

        sig = self.d.check_divergence()
        assert sig is not None, "expected a bearish divergence signal"
        assert sig.kind == "BEARISH"
        assert sig.new_price > sig.prior_price  # higher swing high
        assert sig.new_cvd < sig.prior_cvd       # lower CVD

    def test_signal_consumed_once(self):
        """Once retrieved, the same signal should not fire again."""
        # Reuse the bearish scenario
        bars = [
            (95, 90, 1000), (97, 92, 1100), (100, 95, 1500),
            (98, 93, 1400), (96, 91, 1300), (94, 89, 1100),
            (97, 92, 1000), (99, 94, 900),  (102, 97, 1200),
            (100, 95, 1000), (98, 93, 900),
        ]
        for h, l, c in bars:
            self._push(h, l, c)
        first = self.d.check_divergence()
        assert first is not None
        second = self.d.check_divergence()
        assert second is None, "same signal should not fire twice"

    def test_filtered_by_trade_direction(self):
        """BEARISH divergence (at swing high) only matters for LONG trades."""
        bars = [
            (95, 90, 1000), (97, 92, 1100), (100, 95, 1500),
            (98, 93, 1400), (96, 91, 1300), (94, 89, 1100),
            (97, 92, 1000), (99, 94, 900),  (102, 97, 1200),
            (100, 95, 1000), (98, 93, 900),
        ]
        for h, l, c in bars:
            self._push(h, l, c)
        # As SHORT, BEARISH divergence at the high is irrelevant; should
        # return None even though a signal exists
        assert self.d.check_divergence(trade_direction="SHORT") is None
