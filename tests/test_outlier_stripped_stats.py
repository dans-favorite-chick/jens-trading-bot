"""Outlier-stripped P&L stats (#4, 2026-05-13).

The deep-dive that motivated this: bias_momentum's apparent +$808 net
was dominated by 2 outlier trades — without them the strategy was
flat/negative. Naive `mean` and `sum` are not enough; the operator
needs to see the median, IQR, and a "without the biggest trades"
view to detect that pattern.

These tests pin the new helpers in tools/validation_tracker.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.validation_tracker import _percentile, _summary_stats


# ── _percentile ────────────────────────────────────────────────────────

def test_percentile_empty_is_zero():
    assert _percentile([], 50) == 0.0


def test_percentile_single_element():
    assert _percentile([42.0], 0) == 42.0
    assert _percentile([42.0], 50) == 42.0
    assert _percentile([42.0], 100) == 42.0


def test_percentile_median_of_known_series():
    # 1,2,3,4,5 → median=3
    assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0


def test_percentile_linear_interp_between_points():
    # 4 elements, p=25 → rank=0.75 → 1 + 0.75*(2-1) = 1.75
    assert _percentile([1.0, 2.0, 3.0, 4.0], 25) == 1.75


# ── _summary_stats ─────────────────────────────────────────────────────

def test_summary_empty_returns_zeros():
    s = _summary_stats([])
    assert s["mean"] == 0.0
    assert s["median"] == 0.0
    assert s["sum_stripped"] == 0.0
    assert s["single_trade_concentration"] == 0.0
    assert s["n_outliers"] == 0


def test_summary_naive_mean_matches_python_mean():
    pnls = [10.0, -5.0, 20.0, -15.0]
    s = _summary_stats(pnls)
    assert s["mean"] == 2.5  # sum=10, n=4


def test_summary_strips_top_magnitude_trades():
    """Strip-top-10% on 10 trades = drop the 1 biggest |trade|."""
    # 9 small trades + 1 monster — monster should be stripped
    pnls = [1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 4.0, -4.0, 5.0, 500.0]
    s = _summary_stats(pnls, strip_top_pct=10.0)
    # Sum naive = 505; sum stripped should drop the 500 outlier → 5.0
    assert s["sum_stripped"] == 5.0
    assert s["n_outliers"] == 1


def test_summary_concentration_flags_dominant_single_trade():
    """If one trade equals (or exceeds) the net, concentration >= 1.0 —
    a giant red flag the operator MUST see."""
    # 9 break-even trades + 1 +$1000 → net=$1000, concentration=1.0
    pnls = [0.0] * 9 + [1000.0]
    s = _summary_stats(pnls)
    assert s["single_trade_concentration"] == 1.0


def test_summary_concentration_below_threshold_when_distributed():
    """If 4 trades all contribute equally, concentration ≈ 0.25."""
    pnls = [100.0, 100.0, 100.0, 100.0]  # net = 400
    s = _summary_stats(pnls)
    assert s["single_trade_concentration"] == pytest.approx(0.25, abs=0.01)


def test_summary_handles_zero_net_without_divide_by_zero():
    """Equal wins and losses → net=0. Concentration must not divide
    by zero — should return 0.0."""
    pnls = [100.0, -100.0]
    s = _summary_stats(pnls)
    assert s["single_trade_concentration"] == 0.0


def test_summary_bias_momentum_like_pattern():
    """Realistic bias_momentum pattern: many small losses + a couple
    of giant wins. The naive sum looks profitable; the stripped sum
    reveals it's flat or negative."""
    # 20 small losses (-$10 each) + 2 monster wins (+$500 each)
    pnls = [-10.0] * 20 + [500.0, 500.0]
    s = _summary_stats(pnls, strip_top_pct=10.0)  # strips top ~2
    # Naive net: -200 + 1000 = +800 (looks like edge)
    # After stripping the 2 monsters: -200 (true picture)
    assert s["sum_stripped"] < 0
    # And concentration is huge — one trade carries ~62% of the net
    assert s["single_trade_concentration"] >= 0.5


def test_summary_median_is_robust_to_outliers():
    """The median should NOT move when an outlier is added — that's
    the whole point of using it instead of mean."""
    base = [1.0, 2.0, 3.0, 4.0, 5.0]
    s_base = _summary_stats(base)
    base_with_outlier = base + [10000.0]
    s_outlier = _summary_stats(base_with_outlier)
    # Mean explodes; median barely moves
    assert s_outlier["mean"] > s_base["mean"] * 100
    assert abs(s_outlier["median"] - s_base["median"]) <= 1.0
