"""Tests for core.volume_profile_lsr.

NOTE 2026-05-17: imported from core.volume_profile_lsr (Phase 1 V2
deployment renamed the LSR bar-profile module to avoid collision with
the existing tick-streaming core.volume_profile module). See Phase 1
commit 9a5de35.

Run with:
    pytest tests/test_volume_profile_lsr.py -v
"""
from dataclasses import dataclass

import pytest

from core.volume_profile_lsr import VolumeProfileBuilder, VolumeProfile


@dataclass
class Bar:
    high: float
    low: float
    open: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    end_time: float = 0.0


class TestVolumeProfileBuilder:
    def test_empty_returns_none(self):
        b = VolumeProfileBuilder()
        assert b.build_from_bars([]) is None

    def test_poc_at_concentrated_price(self):
        """A series of bars all sitting on the same price should put POC there."""
        b = VolumeProfileBuilder(price_resolution=1.0)
        # 5 bars all at 22000 ± a tick, with 100 volume each
        bars = [Bar(high=22000, low=22000, volume=100, end_time=i * 60) for i in range(5)]
        prof = b.build_from_bars(bars)
        assert prof is not None
        assert prof.poc == 22000.0
        assert prof.total_volume == 500

    def test_value_area_contains_70pct(self):
        b = VolumeProfileBuilder(price_resolution=1.0, value_area_pct=0.70)
        # 22000 has heavy volume, 22005 and 21995 have medium, edges have small
        bars = (
            [Bar(high=22000, low=22000, volume=1000, end_time=0)] * 5
            + [Bar(high=22005, low=22005, volume=200, end_time=0)] * 5
            + [Bar(high=21995, low=21995, volume=200, end_time=0)] * 5
            + [Bar(high=22010, low=22010, volume=10, end_time=0)] * 5
            + [Bar(high=21990, low=21990, volume=10, end_time=0)] * 5
        )
        prof = b.build_from_bars(bars)
        assert prof is not None
        assert prof.poc == 22000.0
        # Value area should not include the extreme edges (only 10 volume each)
        assert prof.val >= 21995
        assert prof.vah <= 22005

    def test_hvn_identifies_peaks(self):
        b = VolumeProfileBuilder(price_resolution=1.0, hvn_count=3, peak_neighborhood=1)
        # Histogram with two clear peaks at 22000 and 22020
        bars = []
        for _ in range(10):
            bars.append(Bar(high=22000, low=22000, volume=500, end_time=0))
        for _ in range(8):
            bars.append(Bar(high=22020, low=22020, volume=500, end_time=0))
        # Add some fluff around them
        bars += [Bar(high=22001, low=22001, volume=50, end_time=0)] * 2
        bars += [Bar(high=22019, low=22019, volume=50, end_time=0)] * 2
        bars += [Bar(high=22021, low=22021, volume=50, end_time=0)] * 2

        prof = b.build_from_bars(bars)
        assert prof is not None
        # Top 2 HVNs should include 22000 and 22020
        top_two = set(prof.hvn_levels[:2])
        assert 22000.0 in top_two
        assert 22020.0 in top_two

    def test_lvn_identifies_valleys(self):
        b = VolumeProfileBuilder(price_resolution=1.0, lvn_count=3, peak_neighborhood=1)
        # Two HVNs at 22000 and 22020 with a thin zone between
        bars = []
        for _ in range(20):
            bars.append(Bar(high=22000, low=22000, volume=500, end_time=0))
        for _ in range(20):
            bars.append(Bar(high=22020, low=22020, volume=500, end_time=0))
        # Thin volume in the middle
        for p in range(22005, 22016):
            bars.append(Bar(high=float(p), low=float(p), volume=20, end_time=0))

        prof = b.build_from_bars(bars)
        assert prof is not None
        # An LVN should sit somewhere in the 22005-22015 range
        assert any(22005 <= lvn <= 22015 for lvn in prof.lvn_levels)

    def test_volume_distributed_across_bar_range(self):
        """A wide bar with volume should distribute it across multiple buckets."""
        b = VolumeProfileBuilder(price_resolution=1.0)
        # One bar: 22000 to 22005, volume 1000
        bars = [Bar(high=22005, low=22000, volume=1000, end_time=0)]
        prof = b.build_from_bars(bars)
        assert prof is not None
        # 6 buckets get ~1000/6 = ~166 each (22000, 22001, ..., 22005)
        for p in range(22000, 22006):
            assert prof.histogram.get(float(p), 0) > 100

    def test_zero_volume_bars_ignored(self):
        b = VolumeProfileBuilder(price_resolution=1.0)
        bars = [
            Bar(high=22000, low=22000, volume=0, end_time=0),
            Bar(high=22005, low=22005, volume=100, end_time=0),
        ]
        prof = b.build_from_bars(bars)
        assert prof is not None
        assert prof.poc == 22005.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
