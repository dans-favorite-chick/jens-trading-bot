"""Regression tests — every bug found by the hole-poke audit gets a
permanent test here so it can never recur.

Run with:
    pytest tests/test_regression_holes.py -v
"""
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

_CT = ZoneInfo("America/Chicago")


@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    start_time: float
    end_time: float
    delta: float = 0.0
    bar_delta: float = 0.0


@pytest.fixture(autouse=True)
def stub_base_strategy(monkeypatch):
    base_mod = types.ModuleType("strategies.base_strategy")

    @dataclass
    class _Signal:
        direction: str = ""
        stop_ticks: int = 0
        target_rr: float = 0.0
        confidence: float = 0.0
        entry_score: float = 0.0
        strategy: str = ""
        reason: str = ""
        confluences: list = field(default_factory=list)
        atr_stop_override: bool = False
        entry_type: str = "MARKET"
        entry_price: float = 0.0
        stop_price: float = 0.0
        target_price: float = 0.0
        eod_flat_time_et: str = ""
        metadata: dict = field(default_factory=dict)
        trade_id: str = "test"

    class _BaseStrategy:
        def __init__(self, c):
            self.config = c
            self.enabled = c.get("enabled", True)
            self.validated = c.get("validated", False)

    base_mod.Signal = _Signal
    base_mod.BaseStrategy = _BaseStrategy
    monkeypatch.setitem(sys.modules, "strategies.base_strategy", base_mod)


def _ct_dt(*args):
    return datetime(*args, tzinfo=_CT)


def _build_pdl_sweep_bars(delta_per_bar=50, sweep_delta=200, now=None):
    """Build a standard PDL-sweep bar set for testing."""
    if now is None:
        now = _ct_dt(2026, 5, 15, 9, 30)
    bars = []
    # Yesterday RTH with PDL=21995
    for i in range(30):
        ts = _ct_dt(2026, 5, 14, 9, 0).timestamp() + i * 60
        low = 21995.0 if i == 15 else 22010.0
        bars.append(Bar(22020, 22030, low, 22020, 100, ts - 60, ts,
                        delta=0, bar_delta=0))
    # Today's bars with positive delta (absorption building)
    for i in range(20):
        ts = (now - timedelta(minutes=21 - i)).timestamp()
        bars.append(Bar(22001, 22002, 22000, 22001, 150, ts - 60, ts,
                        delta=delta_per_bar, bar_delta=delta_per_bar))
    # Sweep bar
    ts = now.timestamp()
    bars.append(Bar(21998, 21998.5, 21994.0, 21997.5, 500, ts - 60, ts,
                    delta=sweep_delta, bar_delta=sweep_delta))
    return bars, now


# ════════════════════════════════════════════════════════════════════
# Hole #1: CVD gate no-op when delta data is missing
# ════════════════════════════════════════════════════════════════════
class TestHole1_CVDNoOp:
    def test_lsr_skips_when_all_deltas_zero(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({"enabled": True, "bar_freshness_sec": 10**9})
        bars, now = _build_pdl_sweep_bars(delta_per_bar=0, sweep_delta=0)
        m = {"price": 21997.5, "vwap": 22002, "atr_5m": 20, "cvd": 0, "now_ct": now}
        result = s.evaluate(m, [], bars, {})
        assert result is None, "Strategy should refuse to fire when delta=0 (gate broken)"

    def test_orb_fade_skips_when_all_deltas_zero(self):
        from strategies.orb_fade import ORBFade
        s = ORBFade({"enabled": True, "bar_freshness_sec": 10**9})
        now = _ct_dt(2026, 5, 15, 9, 30)
        bars = []
        for i in range(17):
            ts = (now - timedelta(minutes=19 - i)).timestamp()
            bars.append(Bar(22030, 22035, 22025, 22030, 150, ts - 60, ts,
                            delta=0, bar_delta=0))
        ts = (now - timedelta(minutes=1)).timestamp()
        bars.append(Bar(22045, 22057, 22045, 22055, 350, ts - 60, ts,
                        delta=0, bar_delta=0))
        ts = now.timestamp()
        bars.append(Bar(22054, 22052, 22043, 22045, 200, ts - 60, ts,
                        delta=0, bar_delta=0))
        m = {"rth_15min_high": 22050, "rth_15min_low": 22000, "price": 22045,
             "vwap": 22025, "now_ct": now, "avg_1min_volume": 150}
        sig = s.evaluate(m, [], bars, {})
        assert sig is None, "ORB-FADE should refuse to fire when delta=0"


# ════════════════════════════════════════════════════════════════════
# Hole #2: Stop price not on tick grid
# ════════════════════════════════════════════════════════════════════
class TestHole2_TickGrid:
    def test_confirmation_stop_snaps_to_grid(self):
        from core.confirmation_stop import compute_confirmation_stop, snap_to_tick
        bars = [Bar(22000.0, 22001.50, 21999.13, 22000.0, 100, 0, i) for i in range(5)]
        ticks, sp, note = compute_confirmation_stop(
            direction="LONG", entry_price=22001.0,
            bars_1m=bars, lookback_bars=5, buffer_ticks=2, tick_size=0.25,
        )
        # Must be a multiple of 0.25
        assert abs((sp / 0.25) - round(sp / 0.25)) < 1e-9, \
            f"stop_price {sp} is not on the 0.25 tick grid"

    def test_snap_to_tick_correctness(self):
        from core.confirmation_stop import snap_to_tick
        assert snap_to_tick(22000.13, 0.25) == 22000.25
        assert snap_to_tick(22000.50, 0.25) == 22000.50
        assert snap_to_tick(22000.63, 0.25) == 22000.75  # rounds to nearest
        assert snap_to_tick(22000.00, 0.25) == 22000.00

    def test_lsr_signal_prices_all_on_grid(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({"enabled": True, "bar_freshness_sec": 10**9})
        bars, now = _build_pdl_sweep_bars()
        m = {"price": 21997.5, "vwap": 22002.0, "atr_5m": 20, "cvd": 0, "now_ct": now}
        result = s.evaluate(m, [], bars, {})
        assert result is not None
        for p_name, p in [("entry", result.entry_price),
                          ("stop", result.stop_price),
                          ("target", result.target_price)]:
            assert abs((p / 0.25) - round(p / 0.25)) < 1e-9, \
                f"{p_name}={p} not on 0.25 grid"


# ════════════════════════════════════════════════════════════════════
# Hole #3: Plateau swing detection
# ════════════════════════════════════════════════════════════════════
class TestHole3_PlateauSwings:
    def test_plateau_high_detected(self):
        from core.liquidity_levels import LiquidityLevelTracker
        t = LiquidityLevelTracker(swing_lookback_bars=3, swing_peak_window=2)
        ts_base = _ct_dt(2026, 5, 15, 9, 0).timestamp()
        bars = []
        for i in range(15):
            ts = ts_base + i * 60
            if i in (5, 6, 7):  # plateau peak
                h = 22020.0
            else:
                h = 22000 + (8 - abs(6 - i)) * 3
            l = h - 5
            bars.append(Bar(h - 1, h, l, h - 0.5, 100, ts - 60, ts))
        t.refresh_swing_levels(bars, current_price=21980)
        swing_highs = [lv for lv in t.all_levels().values()
                       if lv.name.startswith("SwingH") and lv.price == 22020.0]
        assert len(swing_highs) >= 1, "Plateau high at 22020 should be detected"


# ════════════════════════════════════════════════════════════════════
# Hole #4: consumed_at preserved across update_pdh_pdl recall
# ════════════════════════════════════════════════════════════════════
class TestHole4_ConsumedAtPreserved:
    def test_consumed_at_preserved_on_recall(self):
        from core.liquidity_levels import LiquidityLevelTracker
        ts_base = _ct_dt(2026, 5, 15, 9, 0).timestamp()
        t = LiquidityLevelTracker()
        bars = [Bar(22000, 22050, 21990, 22000, 100, 0, ts_base + 1)]
        t.update_pdh_pdl(bars)
        t.mark_level_consumed("PDH")
        consumed_before = t.get("PDH").consumed_at
        assert consumed_before is not None
        # Re-call with the same bars
        t.update_pdh_pdl(bars)
        consumed_after = t.get("PDH").consumed_at
        assert consumed_after == consumed_before, \
            "consumed_at must be preserved across re-call with same price"


# ════════════════════════════════════════════════════════════════════
# Hole #5: ORB-FADE zero-range current bar
# ════════════════════════════════════════════════════════════════════
class TestHole5_ZeroRangeCurrentBar:
    def test_orb_fade_skips_zero_range_current_bar(self):
        from strategies.orb_fade import ORBFade
        s = ORBFade({"enabled": True, "bar_freshness_sec": 10**9})
        now = _ct_dt(2026, 5, 15, 9, 30)
        bars = []
        for i in range(17):
            ts = (now - timedelta(minutes=19 - i)).timestamp()
            bars.append(Bar(22030, 22035, 22025, 22030, 150, ts - 60, ts,
                            delta=-30, bar_delta=-30))
        ts = (now - timedelta(minutes=1)).timestamp()
        bars.append(Bar(22045, 22057, 22045, 22055, 350, ts - 60, ts,
                        delta=-50, bar_delta=-50))
        ts = now.timestamp()
        # Zero-range current bar
        bars.append(Bar(22049, 22049, 22049, 22049, 100, ts - 60, ts,
                        delta=-100, bar_delta=-100))
        m = {"rth_15min_high": 22050, "rth_15min_low": 22000, "price": 22049,
             "vwap": 22025, "now_ct": now, "avg_1min_volume": 150}
        sig = s.evaluate(m, [], bars, {})
        assert sig is None, "Zero-range current bar must not produce a fade signal"


# ════════════════════════════════════════════════════════════════════
# Hole #7: LSR target on wrong side of entry (HVN case)
# ════════════════════════════════════════════════════════════════════
class TestHole7_TargetSide:
    def test_lsr_long_target_above_entry_when_poc_below(self):
        """When VP POC is below entry on a LONG signal, target should fall
        through to a valid case (not output target < entry)."""
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({"enabled": True, "bar_freshness_sec": 10**9})
        bars, now = _build_pdl_sweep_bars()
        m = {
            "price": 21997.5, "vwap": 22002, "atr_5m": 20, "cvd": 0,
            "now_ct": now,
            "volume_profile_5d": {
                "poc": 21980.0,  # BELOW entry — invalid target for LONG
                "hvn_levels": [21995.0],
                "lvn_levels": [],
                "vah": 22010, "val": 21980,
            },
        }
        result = s.evaluate(m, [], bars, {})
        assert result is not None
        assert result.direction == "LONG"
        assert result.target_price > result.entry_price, \
            f"LONG target must be ABOVE entry: target={result.target_price}, entry={result.entry_price}"

    def test_lsr_short_target_below_entry_when_poc_above(self):
        """Mirror: SHORT signal must have target BELOW entry even if POC is above."""
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({"enabled": True, "bar_freshness_sec": 10**9})

        # Build a PDH sweep scenario for SHORT
        now = _ct_dt(2026, 5, 15, 9, 30)
        bars = []
        for i in range(30):
            ts = _ct_dt(2026, 5, 14, 9, 0).timestamp() + i * 60
            high = 22055.0 if i == 15 else 22020.0
            bars.append(Bar(22000, high, 21995, 22010, 100, ts - 60, ts,
                            delta=0, bar_delta=0))
        for i in range(20):
            ts = (now - timedelta(minutes=21 - i)).timestamp()
            bars.append(Bar(22045, 22046, 22043, 22045, 150, ts - 60, ts,
                            delta=-50, bar_delta=-50))
        # SHORT sweep at PDH=22055: bar high pierces, close back below
        ts = now.timestamp()
        bars.append(Bar(22050, 22057, 22050, 22052.5, 500, ts - 60, ts,
                        delta=-200, bar_delta=-200))

        m = {
            "price": 22052.5, "vwap": 22035, "atr_5m": 20, "cvd": 0,
            "now_ct": now,
            "volume_profile_5d": {
                "poc": 22080.0,  # ABOVE entry — invalid for SHORT
                "hvn_levels": [22055.0],
                "lvn_levels": [],
                "vah": 22090, "val": 22020,
            },
        }
        result = s.evaluate(m, [], bars, {})
        assert result is not None
        assert result.direction == "SHORT"
        assert result.target_price < result.entry_price, \
            f"SHORT target must be BELOW entry: target={result.target_price}, entry={result.entry_price}"


# ════════════════════════════════════════════════════════════════════
# Hole #9: Corrupt bar (high < low) handled gracefully
# ════════════════════════════════════════════════════════════════════
class TestHole9_CorruptBars:
    def test_detect_sweep_rejects_inverted_bar(self):
        from core.liquidity_levels import detect_sweep, LiquidityLevel
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        bad = Bar(22001, 21998, 22000, 21999, 100, 0, 100)  # high < low
        assert detect_sweep(level, bad) is None


# ════════════════════════════════════════════════════════════════════
# Hole #10: ORB-FADE zero-range BREAKOUT bar (not just current)
# ════════════════════════════════════════════════════════════════════
class TestHole10_ZeroRangeBreakout:
    def test_zero_range_breakout_bar_ignored(self):
        from strategies.orb_fade import ORBFade
        s = ORBFade({"enabled": True, "bar_freshness_sec": 10**9})
        now = _ct_dt(2026, 5, 15, 9, 30)
        bars = []
        for i in range(17):
            ts = (now - timedelta(minutes=19 - i)).timestamp()
            bars.append(Bar(22030, 22035, 22025, 22030, 150, ts - 60, ts,
                            delta=-30, bar_delta=-30))
        # Zero-range "breakout" bar — should be ignored
        ts = (now - timedelta(minutes=1)).timestamp()
        bars.append(Bar(22055, 22055, 22055, 22055, 350, ts - 60, ts,
                        delta=-50, bar_delta=-50))
        # Normal current bar
        ts = now.timestamp()
        bars.append(Bar(22045, 22050, 22043, 22045, 200, ts - 60, ts,
                        delta=-100, bar_delta=-100))
        m = {"rth_15min_high": 22050, "rth_15min_low": 22000, "price": 22045,
             "vwap": 22025, "now_ct": now, "avg_1min_volume": 150}
        sig = s.evaluate(m, [], bars, {})
        # Should NOT find a breakout (zero-range was skipped)
        assert sig is None, "Zero-range breakout bar should not produce a fade signal"


# ════════════════════════════════════════════════════════════════════
# Hole #13: Edge config — max_stop_ticks=0
# ════════════════════════════════════════════════════════════════════
class TestHole13_EdgeConfig:
    def test_max_stop_zero_blocks_all_signals(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({
            "enabled": True, "bar_freshness_sec": 10**9, "max_stop_ticks": 0,
        })
        bars, now = _build_pdl_sweep_bars()
        m = {"price": 21997.5, "vwap": 22002, "atr_5m": 20, "cvd": 0, "now_ct": now}
        assert s.evaluate(m, [], bars, {}) is None


# ════════════════════════════════════════════════════════════════════
# Hole #14: Negative volume data
# ════════════════════════════════════════════════════════════════════
class TestHole14_NegativeVolume:
    def test_volume_profile_ignores_negative_volume(self):
        # 2026-05-17: V2 deployment renamed LSR bar-profile to
        # core.volume_profile_lsr to avoid colliding with the existing
        # tick-streaming core.volume_profile (Phase 1 commit 9a5de35).
        from core.volume_profile_lsr import VolumeProfileBuilder
        b = VolumeProfileBuilder()
        bars = [
            Bar(22000, 22001, 21999, 22000, 100, 0, 0),
            Bar(22001, 22002, 22000, 22001, -50, 0, 0),  # corrupt
            Bar(22002, 22003, 22001, 22002, 200, 0, 0),
        ]
        prof = b.build_from_bars(bars)
        assert prof is not None
        assert prof.total_volume == 300, \
            f"Negative-volume bar must be skipped: got total={prof.total_volume}"


# ════════════════════════════════════════════════════════════════════
# Hole #15 + #22: NaN delta values fool the CVD gate
# ════════════════════════════════════════════════════════════════════
class TestHole15_NaNDeltas:
    def test_lsr_skips_on_nan_deltas(self):
        """NaN delta_sum silently passes direction checks → strategy fires blind."""
        import math
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({"enabled": True, "bar_freshness_sec": 10**9})
        bars, now = _build_pdl_sweep_bars(delta_per_bar=float('nan'),
                                          sweep_delta=float('nan'))
        m = {"price": 21997.5, "vwap": 22002, "atr_5m": 20, "cvd": 0, "now_ct": now}
        result = s.evaluate(m, [], bars, {})
        assert result is None, "LSR should skip on NaN delta values"

    def test_orb_fade_skips_on_nan_deltas(self):
        import math
        from strategies.orb_fade import ORBFade
        s = ORBFade({"enabled": True, "bar_freshness_sec": 10**9})
        now = _ct_dt(2026, 5, 15, 9, 30)
        bars = []
        for i in range(17):
            ts = (now - timedelta(minutes=19 - i)).timestamp()
            bars.append(Bar(22030, 22035, 22025, 22030, 150, ts - 60, ts,
                            delta=float('nan'), bar_delta=float('nan')))
        ts = (now - timedelta(minutes=1)).timestamp()
        bars.append(Bar(22045, 22057, 22045, 22055, 350, ts - 60, ts,
                        delta=float('nan'), bar_delta=float('nan')))
        ts = now.timestamp()
        bars.append(Bar(22054, 22052, 22043, 22045, 200, ts - 60, ts,
                        delta=float('nan'), bar_delta=float('nan')))
        m = {"rth_15min_high": 22050, "rth_15min_low": 22000, "price": 22045,
             "vwap": 22025, "now_ct": now, "avg_1min_volume": 150}
        sig = s.evaluate(m, [], bars, {})
        assert sig is None, "ORB-FADE should skip on NaN delta values"

    def test_orb_v2_skips_on_nan_deltas(self):
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
        open_dt = datetime(2026, 5, 15, 9, 30, tzinfo=_ET)
        bars_1m = []
        for i in range(15):
            ts = (open_dt + timedelta(minutes=i)).timestamp()
            high = 22050 if i == 5 else 22020
            low = 22000 if i == 10 else 22020
            bars_1m.append(Bar(22020, high, low, 22020, 200, ts - 60, ts,
                                delta=float('nan'), bar_delta=float('nan')))
        for i in range(15, 30):
            ts = (open_dt + timedelta(minutes=i)).timestamp()
            bars_1m.append(Bar(22030, 22035, 22025, 22030, 100, ts - 60, ts,
                                delta=float('nan'), bar_delta=float('nan')))
        bars_5m = [Bar(22020, 22060, 22020, 22055, 800,
                       bars_1m[24].start_time, bars_1m[28].end_time,
                       delta=float('nan'), bar_delta=float('nan'))]
        m = {"price": 22055, "atr_5m": 15,
             "now_ct": (open_dt + timedelta(minutes=30)).astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is None, "ORB v2 should skip on NaN delta values"


# ════════════════════════════════════════════════════════════════════
# Hole #16: Infinity in market price
# ════════════════════════════════════════════════════════════════════
class TestHole16_InfinityPrice:
    def test_lsr_skips_inf_price(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({"enabled": True, "bar_freshness_sec": 10**9})
        bars, now = _build_pdl_sweep_bars()
        m = {"price": float('inf'), "vwap": 22002, "atr_5m": 20, "cvd": 0, "now_ct": now}
        result = s.evaluate(m, [], bars, {})
        assert result is None, "LSR should reject infinite price"

    def test_lsr_skips_nan_price(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal({"enabled": True, "bar_freshness_sec": 10**9})
        bars, now = _build_pdl_sweep_bars()
        m = {"price": float('nan'), "vwap": 22002, "atr_5m": 20, "cvd": 0, "now_ct": now}
        result = s.evaluate(m, [], bars, {})
        assert result is None, "LSR should reject NaN price"

    def test_orb_v2_skips_inf_price(self):
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        # Minimal scaffold — price guard fires before OR setup
        m = {"price": float('inf'), "atr_5m": 15, "now_ct": _ct_dt(2026, 5, 15, 10, 0)}
        # Need at least 1 bar to not be filtered on `not bars_1m`
        bars = [Bar(22020, 22050, 22000, 22030, 100, 0, 1)]
        result = s.evaluate(m, [], bars, {})
        assert result is None

    def test_vwap_v2_skips_inf_price(self):
        from strategies.vwap_pullback_v2 import VWAPPullbackV2
        s = VWAPPullbackV2({"enabled": True})
        bars = [Bar(22000, 22010, 21990, 22000, 100, 0, i) for i in range(5)]
        m = {"price": float('inf'), "vwap": 22010,
             "now_ct": _ct_dt(2026, 5, 15, 10, 0)}
        assert s.evaluate(m, [], bars, {}) is None


# ════════════════════════════════════════════════════════════════════
# Hole #17: Compression v2 window must reset across days
# ════════════════════════════════════════════════════════════════════
class TestHole17_CompressionWindowDailyReset:
    def test_window_resets_on_new_day(self):
        from strategies.compression_breakout_v2 import CompressionBreakoutV2
        s = CompressionBreakoutV2({"enabled": True})

        yesterday = _ct_dt(2026, 5, 14, 9, 0)
        y_ts = yesterday.timestamp()
        bars_y = [Bar(22000, 22020, 21980, 22010, 1000,
                      y_ts + i * 300 - 300, y_ts + i * 300) for i in range(58)]
        # Eval yesterday
        s.evaluate({"now_ct": _ct_dt(2026, 5, 14, 14, 0), "atr_5m": 5},
                   bars_y, [], {})
        history_after_yesterday = len(s._window.bar_history)

        # Now eval today — new date triggers reset
        bars_t = list(bars_y)
        today = _ct_dt(2026, 5, 15, 9, 0)
        t_ts = today.timestamp()
        for i in range(50):
            bars_t.append(Bar(22000, 22060, 21950, 22030, 1500,
                              t_ts + i * 300 - 300, t_ts + i * 300))
        s.evaluate({"now_ct": _ct_dt(2026, 5, 15, 14, 0), "atr_5m": 5},
                   bars_t, [], {})

        # Window must NOT carry yesterday's compressed count forward.
        # After today's wide-range bars, compressed_count should be 0 or low.
        assert s._window.compressed_count() <= 1, \
            f"Window leaked yesterday's compression: {s._window.compressed_count()} compressed bars"


# ════════════════════════════════════════════════════════════════════
# Hole #25: Negative target_rr in config (operator typo)
# ════════════════════════════════════════════════════════════════════
class TestHole25_NegativeTargetRR:
    def test_orb_v2_uses_default_on_negative_rr(self):
        from strategies.orb_v2 import ORBv2
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
        s = ORBv2({"enabled": True, "target_rr": -2.0})
        open_dt = datetime(2026, 5, 15, 9, 30, tzinfo=_ET)
        bars_1m = []
        for i in range(15):
            ts = (open_dt + timedelta(minutes=i)).timestamp()
            high = 22050 if i == 5 else 22020
            low = 22000 if i == 10 else 22020
            bars_1m.append(Bar(22020, high, low, 22020, 200, ts - 60, ts,
                                delta=20, bar_delta=20))
        for i in range(15, 30):
            ts = (open_dt + timedelta(minutes=i)).timestamp()
            bars_1m.append(Bar(22030, 22035, 22025, 22030, 100, ts - 60, ts,
                                delta=20, bar_delta=20))
        bars_5m = [Bar(22020, 22060, 22020, 22055, 800,
                       bars_1m[24].start_time, bars_1m[28].end_time,
                       delta=80, bar_delta=80)]
        m = {"price": 22055, "atr_5m": 15,
             "now_ct": (open_dt + timedelta(minutes=30)).astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is not None
        # Target must be on correct side (above entry for LONG)
        assert result.target_price > result.entry_price, \
            f"Negative target_rr should fall back to default, not produce wrong-side target"


# ════════════════════════════════════════════════════════════════════
# Hole #24: NaN in bar OHLC fields propagate through detect_sweep
# ════════════════════════════════════════════════════════════════════
class TestHole24_NaNInBarFields:
    def test_detect_sweep_rejects_nan_bar(self):
        from core.liquidity_levels import detect_sweep, LiquidityLevel
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        nan_bar = Bar(22001, float('nan'), 21998, 22000, 100, 0, 100)
        assert detect_sweep(level, nan_bar) is None

    def test_detect_sweep_rejects_inf_bar(self):
        from core.liquidity_levels import detect_sweep, LiquidityLevel
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        inf_bar = Bar(22001, float('inf'), 21998, 22000, 100, 0, 100)
        assert detect_sweep(level, inf_bar) is None


# ════════════════════════════════════════════════════════════════════
# Hole #27: All strategies must init with empty config
# ════════════════════════════════════════════════════════════════════
class TestHole27_EmptyConfig:
    def test_all_strategies_init_empty_config(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        from strategies.orb_v2 import ORBv2
        from strategies.orb_fade import ORBFade
        from strategies.compression_breakout_v2 import CompressionBreakoutV2
        from strategies.vwap_pullback_v2 import VWAPPullbackV2
        # Must not raise
        for cls in [NQLiquiditySweepReversal, ORBv2, ORBFade,
                    CompressionBreakoutV2, VWAPPullbackV2]:
            s = cls({})
            assert s is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])