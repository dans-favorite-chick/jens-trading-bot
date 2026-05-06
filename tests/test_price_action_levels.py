"""Sprint I — tests for core/price_action_levels.

Covers:
- PriceActionLevels.is_stale logic
- _compute_poc / _compute_value_area / _compute_hvn_levels / _compute_lvn_levels
- _swing_pivots
- _classify_structure_bias (BULLISH/BEARISH/NEUTRAL paths)
- _classify_volatility (HIGH/NORMAL/LOW paths)
- find_nearest_htf_level (closest wins, tier tiebreak, no-data-in-range)
- build_levels_from_aggregator (with mocked aggregator)
- LevelTier enum sanity

All tests use synthetic data — no live APIs, no disk I/O.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.price_action_levels import (
    LevelTier,
    PriceActionLevels,
    PriceLevel,
    _classify_structure_bias,
    _classify_volatility,
    _compute_hvn_levels,
    _compute_lvn_levels,
    _compute_poc,
    _compute_value_area,
    _swing_pivots,
    build_levels_from_aggregator,
    find_nearest_htf_level,
)


@dataclass
class _Bar:
    """Minimal Bar surrogate for tests — matches Phoenix's Bar interface."""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 100


# ═══════════════════════════════════════════════════════════════════
# PriceActionLevels.is_stale
# ═══════════════════════════════════════════════════════════════════
class TestIsStale:
    def test_empty_levels_is_stale(self):
        assert PriceActionLevels().is_stale is True

    def test_with_prior_day_high_not_stale(self):
        lv = PriceActionLevels(prior_day_high=27800.0)
        assert lv.is_stale is False

    def test_with_session_poc_not_stale(self):
        lv = PriceActionLevels(session_poc=27800.0)
        assert lv.is_stale is False

    def test_with_session_vwap_not_stale(self):
        lv = PriceActionLevels(session_vwap=27800.0)
        assert lv.is_stale is False

    def test_only_swing_pivot_still_stale(self):
        """Swing pivots alone aren't enough — stale until at least one
        of {PDH, POC, VWAP} populates."""
        lv = PriceActionLevels(swing_high_5m=27800.0, swing_low_5m=27700.0)
        assert lv.is_stale is True


# ═══════════════════════════════════════════════════════════════════
# _compute_poc
# ═══════════════════════════════════════════════════════════════════
class TestComputePOC:
    def test_empty_returns_none(self):
        assert _compute_poc([]) is None

    def test_single_bar_returns_close_bucket(self):
        # Single bar with close 27800.30 → bucket 27800.25
        bars = [_Bar(close=27800.30, volume=500)]
        assert _compute_poc(bars) == 27800.25

    def test_max_volume_bucket_wins(self):
        bars = [
            _Bar(close=27800, volume=100),
            _Bar(close=27800, volume=200),  # this bucket wins
            _Bar(close=27810, volume=150),
            _Bar(close=27820, volume=120),
        ]
        assert _compute_poc(bars) == 27800

    def test_zero_volume_bars_ignored(self):
        bars = [
            _Bar(close=27800, volume=0),
            _Bar(close=27810, volume=500),
        ]
        assert _compute_poc(bars) == 27810

    def test_buckets_to_quarter_tick(self):
        # 27800.30 should round to 27800.25; 27800.40 should round to 27800.50
        bars = [
            _Bar(close=27800.30, volume=100),
            _Bar(close=27800.30, volume=100),
            _Bar(close=27800.40, volume=80),
        ]
        # The first two go to 27800.25 (200 vol), third to 27800.50 (80 vol)
        assert _compute_poc(bars) == 27800.25


# ═══════════════════════════════════════════════════════════════════
# _compute_value_area
# ═══════════════════════════════════════════════════════════════════
class TestComputeValueArea:
    def test_empty_returns_none_none(self):
        assert _compute_value_area([]) == (None, None)

    def test_vah_above_val(self):
        # Bell-shaped distribution centered at 27810
        bars = []
        for px, vol in [
            (27800, 50), (27805, 100), (27810, 300),
            (27815, 100), (27820, 50),
        ]:
            bars.append(_Bar(close=px, volume=vol))
        vah, val = _compute_value_area(bars)
        assert vah is not None and val is not None
        assert vah >= val

    def test_value_area_contains_poc(self):
        bars = []
        for px, vol in [
            (27800, 50), (27805, 100), (27810, 300),
            (27815, 100), (27820, 50),
        ]:
            bars.append(_Bar(close=px, volume=vol))
        vah, val = _compute_value_area(bars)
        poc = _compute_poc(bars)
        assert val <= poc <= vah


# ═══════════════════════════════════════════════════════════════════
# _compute_hvn_levels / _compute_lvn_levels
# ═══════════════════════════════════════════════════════════════════
class TestVolumeNodes:
    def test_empty_returns_empty(self):
        assert _compute_hvn_levels([]) == []
        assert _compute_lvn_levels([]) == []

    def test_hvn_finds_local_max_above_avg(self):
        """A bucket with 3x average volume surrounded by low volume = HVN."""
        bars = []
        # Background: each bucket has 100 volume
        for px in [27800, 27805, 27815, 27820]:
            bars.append(_Bar(close=px, volume=100))
        # Spike at 27810: 600 volume (well above 1.5x avg)
        bars.append(_Bar(close=27810, volume=600))
        hvns = _compute_hvn_levels(bars, n=3)
        assert 27810 in hvns

    def test_lvn_finds_local_min_below_avg(self):
        """A bucket with very low volume surrounded by high = LVN."""
        bars = []
        for px in [27800, 27805, 27815, 27820]:
            bars.append(_Bar(close=px, volume=600))
        # Dip at 27810: small volume (below 0.5x avg)
        bars.append(_Bar(close=27810, volume=10))
        lvns = _compute_lvn_levels(bars, n=3)
        assert 27810 in lvns

    def test_hvn_capped_at_n(self):
        """Returns at most n HVNs even if more exist."""
        bars = []
        # 5 spikes — request only top 2
        for px, vol in [
            (27800, 50), (27805, 800),
            (27810, 50), (27815, 700),
            (27820, 50), (27825, 600),
            (27830, 50), (27835, 500),
            (27840, 50), (27845, 400),
            (27850, 50),
        ]:
            bars.append(_Bar(close=px, volume=vol))
        hvns = _compute_hvn_levels(bars, n=2)
        assert len(hvns) <= 2


# ═══════════════════════════════════════════════════════════════════
# _swing_pivots
# ═══════════════════════════════════════════════════════════════════
class TestSwingPivots:
    def test_empty_returns_none_none(self):
        assert _swing_pivots([]) == (None, None)

    def test_too_few_bars_returns_none(self):
        bars = [_Bar(high=100, low=99) for _ in range(3)]
        assert _swing_pivots(bars) == (None, None)

    def test_returns_max_high_min_low(self):
        bars = [
            _Bar(high=100, low=98),
            _Bar(high=105, low=99),
            _Bar(high=103, low=97),  # lowest low
            _Bar(high=110, low=104),  # highest high
            _Bar(high=108, low=103),
            _Bar(high=106, low=102),
        ]
        sh, sl = _swing_pivots(bars)
        assert sh == 110
        assert sl == 97

    def test_lookback_window_respected(self):
        # 25 bars; lookback=5 should only consider last 5
        bars = [_Bar(high=200, low=190) for _ in range(20)]  # ignored
        bars += [
            _Bar(high=110, low=100),
            _Bar(high=105, low=95),
            _Bar(high=108, low=98),
            _Bar(high=112, low=102),
            _Bar(high=109, low=101),
        ]
        sh, sl = _swing_pivots(bars, lookback=5)
        assert sh == 112
        assert sl == 95


# ═══════════════════════════════════════════════════════════════════
# _classify_structure_bias
# ═══════════════════════════════════════════════════════════════════
class TestStructureBias:
    def test_neutral_when_no_price(self):
        lv = PriceActionLevels(session_vwap=27800)
        assert _classify_structure_bias(lv, None) == "NEUTRAL"

    def test_neutral_when_no_vwap(self):
        lv = PriceActionLevels(prior_day_high=27800)
        assert _classify_structure_bias(lv, 27850) == "NEUTRAL"

    def test_bullish_above_vwap_and_pdh(self):
        lv = PriceActionLevels(session_vwap=27800, prior_day_high=27850)
        assert _classify_structure_bias(lv, 27860) == "BULLISH"

    def test_bearish_below_vwap_and_pdl(self):
        lv = PriceActionLevels(session_vwap=27800, prior_day_low=27750)
        assert _classify_structure_bias(lv, 27740) == "BEARISH"

    def test_neutral_above_vwap_below_pdh(self):
        """Above VWAP but not above PDH — not yet bullish."""
        lv = PriceActionLevels(session_vwap=27800, prior_day_high=27850)
        assert _classify_structure_bias(lv, 27820) == "NEUTRAL"

    def test_neutral_below_vwap_above_pdl(self):
        """Below VWAP but not below PDL — not yet bearish."""
        lv = PriceActionLevels(session_vwap=27800, prior_day_low=27750)
        assert _classify_structure_bias(lv, 27780) == "NEUTRAL"


# ═══════════════════════════════════════════════════════════════════
# _classify_volatility
# ═══════════════════════════════════════════════════════════════════
class TestVolatilityRegime:
    def test_normal_when_missing_data(self):
        assert _classify_volatility(None, 10) == "NORMAL"
        assert _classify_volatility(15, None) == "NORMAL"
        assert _classify_volatility(15, 0) == "NORMAL"

    def test_high_at_or_above_1_5x(self):
        assert _classify_volatility(15, 10) == "HIGH"  # 1.5x exactly
        assert _classify_volatility(20, 10) == "HIGH"  # 2x

    def test_low_at_or_below_0_7x(self):
        assert _classify_volatility(7, 10) == "LOW"   # 0.7x
        assert _classify_volatility(5, 10) == "LOW"

    def test_normal_in_middle_range(self):
        assert _classify_volatility(10, 10) == "NORMAL"
        assert _classify_volatility(12, 10) == "NORMAL"
        assert _classify_volatility(8, 10) == "NORMAL"


# ═══════════════════════════════════════════════════════════════════
# find_nearest_htf_level
# ═══════════════════════════════════════════════════════════════════
class TestFindNearestHTFLevel:
    def test_returns_none_when_no_levels_in_range(self):
        lv = PriceActionLevels(prior_day_high=28000)
        # Price 27800 is 200 points from PDH = far outside 12-tick window
        assert find_nearest_htf_level(27800, lv) is None

    def test_returns_pdh_in_range(self):
        lv = PriceActionLevels(prior_day_high=27800)
        result = find_nearest_htf_level(27801.0, lv, max_distance_ticks=8)
        assert result is not None
        assert result.label == "PDH"
        assert result.tier == LevelTier.TIER_1
        assert result.side == "resistance"

    def test_returns_pdl_in_range(self):
        lv = PriceActionLevels(prior_day_low=27800)
        result = find_nearest_htf_level(27799.0, lv, max_distance_ticks=8)
        assert result is not None
        assert result.label == "PDL"
        assert result.tier == LevelTier.TIER_1

    def test_returns_poc_in_range(self):
        lv = PriceActionLevels(session_poc=27800)
        result = find_nearest_htf_level(27800.5, lv, max_distance_ticks=8)
        assert result is not None
        assert result.label == "POC"
        assert result.tier == LevelTier.TIER_1

    def test_tier1_beats_tier2_on_tie(self):
        """When PDH and HVN are equidistant, PDH (tier 1) wins."""
        lv = PriceActionLevels(
            prior_day_high=27810,
            hvn_levels=[27790],
        )
        result = find_nearest_htf_level(27800, lv, max_distance_ticks=50)
        # |27800 - 27810| == |27800 - 27790| == 10
        assert result is not None
        assert result.tier == LevelTier.TIER_1
        assert result.label == "PDH"

    def test_closer_tier2_beats_farther_tier1(self):
        """Closeness wins over tier rank."""
        lv = PriceActionLevels(
            prior_day_high=27820,    # 20 away
            hvn_levels=[27802],      # 2 away
        )
        result = find_nearest_htf_level(27800, lv, max_distance_ticks=100)
        assert result is not None
        assert result.label.startswith("HVN_")

    def test_vwap_returns_tier2(self):
        lv = PriceActionLevels(session_vwap=27800)
        result = find_nearest_htf_level(27801.0, lv, max_distance_ticks=8)
        assert result is not None
        assert result.label == "VWAP"
        assert result.tier == LevelTier.TIER_2

    def test_lvn_returns_tier3(self):
        lv = PriceActionLevels(lvn_levels=[27800])
        result = find_nearest_htf_level(27801.0, lv, max_distance_ticks=8)
        assert result is not None
        assert result.label.startswith("LVN_")
        assert result.tier == LevelTier.TIER_3


# ═══════════════════════════════════════════════════════════════════
# build_levels_from_aggregator (integration with mocked aggregator)
# ═══════════════════════════════════════════════════════════════════
class TestBuildLevelsFromAggregator:
    def _make_agg(self, **overrides):
        """Build a SimpleNamespace mimicking TickAggregator's surface."""
        # 30 5m bars: bell-curve volume distribution centered at 27810
        from collections import namedtuple
        bars_5m_data = []
        for px, vol in [
            (27795, 50), (27800, 100), (27805, 150),
            (27810, 400), (27815, 150), (27820, 100), (27825, 50),
        ] * 4:  # 28 bars
            bars_5m_data.append(_Bar(open=px, high=px + 1, low=px - 1, close=px, volume=vol))
        # Pad with a few more bars to exceed lookback
        for _ in range(5):
            bars_5m_data.append(_Bar(open=27810, high=27815, low=27805, close=27810, volume=100))

        # BarBuilder surrogate with .completed deque-like list
        bars_5m_surrogate = SimpleNamespace(completed=bars_5m_data)
        bars_15m_surrogate = SimpleNamespace(completed=bars_5m_data[-10:])

        # session_levels surrogate
        sl = SimpleNamespace(
            prior_day_high=27850.0,
            prior_day_low=27750.0,
            prior_day_close=27800.0,
            prior_day_poc=27810.0,
        )

        defaults = {
            "vwap": 27810.0,
            "vwap_std": 5.0,
            "vwap_upper1": 27815.0,
            "vwap_lower1": 27805.0,
            "vwap_upper2": 27820.0,
            "vwap_lower2": 27800.0,
            "last_price": 27860.0,  # bullish: above VWAP + above PDH
            "atr_5m": 8.0,
            "atr_5m_baseline": 10.0,
            "bars_5m": bars_5m_surrogate,
            "bars_15m": bars_15m_surrogate,
            "session_levels": sl,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_populates_vwap_block(self):
        levels = build_levels_from_aggregator(self._make_agg())
        assert levels.session_vwap == 27810.0
        assert levels.vwap_upper_1sd == 27815.0
        assert levels.vwap_lower_1sd == 27805.0
        assert levels.vwap_upper_2sd == 27820.0
        assert levels.vwap_lower_2sd == 27800.0

    def test_populates_prior_day_block(self):
        levels = build_levels_from_aggregator(self._make_agg())
        assert levels.prior_day_high == 27850.0
        assert levels.prior_day_low == 27750.0
        assert levels.prior_day_close == 27800.0

    def test_populates_session_poc_from_bars(self):
        levels = build_levels_from_aggregator(self._make_agg())
        # 27810 has highest volume in synthetic distribution
        assert levels.session_poc == 27810.0
        assert levels.session_vah is not None
        assert levels.session_val is not None

    def test_classifies_bullish_when_price_above_pdh(self):
        levels = build_levels_from_aggregator(self._make_agg(last_price=27860.0))
        assert levels.structure_bias == "BULLISH"

    def test_classifies_volatility_normal(self):
        # atr 8, baseline 10 → ratio 0.8 → NORMAL
        levels = build_levels_from_aggregator(self._make_agg())
        assert levels.volatility_regime == "NORMAL"

    def test_no_vwap_data_skips_band_population(self):
        agg = self._make_agg(vwap=0.0, vwap_std=0.0)
        levels = build_levels_from_aggregator(agg)
        assert levels.session_vwap is None
        assert levels.vwap_upper_1sd is None

    def test_handles_missing_session_levels(self):
        """If aggregator has no session_levels, prior_day fields stay None."""
        agg = self._make_agg(session_levels=None)
        levels = build_levels_from_aggregator(agg)
        assert levels.prior_day_high is None
        assert levels.prior_day_low is None

    def test_handles_empty_bars(self):
        """No bars → no HVNs, no LVNs, no swing pivots."""
        agg = self._make_agg(
            bars_5m=SimpleNamespace(completed=[]),
            bars_15m=SimpleNamespace(completed=[]),
        )
        levels = build_levels_from_aggregator(agg)
        assert levels.hvn_levels == []
        assert levels.lvn_levels == []
        assert levels.swing_high_5m is None
