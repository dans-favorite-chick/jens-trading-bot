"""Tests for ORB Fade strategy + confirmation stop helper.

Run with:
    pytest tests/test_orb_fade.py -v
"""
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.confirmation_stop import compute_confirmation_stop


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


# ────────────────────────────────────────────────────────────────────
# compute_confirmation_stop tests
# ────────────────────────────────────────────────────────────────────
class TestConfirmationStop:
    def test_long_stop_below_swing_low(self):
        """Long stop should sit at-or-below the lowest low in the lookback window."""
        bars = [
            Bar(22000, 22005, 21995, 22002, 100, 0, i)
            for i in range(5)
        ]
        # Force the lowest bar to be one of them
        bars[2].low = 21990
        ticks, sp, note = compute_confirmation_stop(
            direction="LONG",
            entry_price=22005,
            bars_1m=bars,
            lookback_bars=5,
            buffer_ticks=2,
        )
        # Stop = 21990 - 0.5 = 21989.5; distance = 15.5pt = 62 ticks.
        # max_ticks default is 60 — clamps DOWN to 60. New sp = 22005 - 15 = 21990.
        # So sp == 21990 (clamped). Test for <=.
        assert ticks <= 60
        assert sp <= 21990.0

    def test_short_stop_above_swing_high(self):
        bars = [Bar(22000, 22002, 21998, 22000, 100, 0, i) for i in range(5)]
        bars[1].high = 22010
        ticks, sp, note = compute_confirmation_stop(
            direction="SHORT",
            entry_price=22000,
            bars_1m=bars,
            lookback_bars=5,
        )
        assert sp > 22010
        assert ticks <= 60

    def test_clamped_up_to_min(self):
        """If swing is very close to entry, stop should be at min_ticks."""
        bars = [Bar(22000, 22001, 21999.75, 22000.25, 100, 0, i) for i in range(5)]
        ticks, sp, note = compute_confirmation_stop(
            direction="LONG",
            entry_price=22000,
            bars_1m=bars,
            lookback_bars=5,
            buffer_ticks=2,
            min_ticks=8,
            max_ticks=60,
        )
        assert ticks >= 8

    def test_insufficient_bars_falls_back(self):
        ticks, sp, note = compute_confirmation_stop(
            direction="LONG",
            entry_price=22000,
            bars_1m=[],
            lookback_bars=5,
        )
        assert ticks == 20
        assert "insufficient_bars" in note

    def test_wrong_side_swing_falls_back(self):
        """If swing extreme is on the WRONG side of entry, fall back."""
        # LONG entry at 22000, but swing low is ABOVE entry (somehow)
        bars = [Bar(22005, 22010, 22003, 22006, 100, 0, i) for i in range(5)]
        ticks, sp, note = compute_confirmation_stop(
            direction="LONG",
            entry_price=22000,
            bars_1m=bars,
            lookback_bars=5,
        )
        assert "wrong_side" in note or ticks == 20


# ────────────────────────────────────────────────────────────────────
# ORB Fade tests
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def stub_base_strategy(monkeypatch):
    base_mod = types.ModuleType("strategies.base_strategy")

    @dataclass
    class _Signal:
        direction: str
        stop_ticks: int
        target_rr: float
        confidence: float
        entry_score: float
        strategy: str
        reason: str
        confluences: list
        atr_stop_override: bool = False
        entry_type: str = "MARKET"
        entry_price: float = 0.0
        stop_price: float = 0.0
        target_price: float = 0.0
        eod_flat_time_et: str = ""
        metadata: dict = field(default_factory=dict)
        trade_id: str = "test"

    class _BaseStrategy:
        name = "base"
        computes_own_stop = False
        computes_own_target = False
        def __init__(self, config):
            self.config = config
            self.enabled = config.get("enabled", True)
            self.validated = config.get("validated", False)

    base_mod.Signal = _Signal
    base_mod.BaseStrategy = _BaseStrategy
    monkeypatch.setitem(sys.modules, "strategies.base_strategy", base_mod)


def _ct_dt(*args):
    return datetime(*args, tzinfo=_CT)


def _config(**overrides):
    base = {"enabled": True, "bar_freshness_sec": 10**9}
    base.update(overrides)
    return base


class TestORBFade:
    def test_imports_and_inits(self):
        from strategies.orb_fade import ORBFade
        s = ORBFade(_config())
        assert s.name == "orb_fade"

    def test_returns_none_outside_window(self):
        from strategies.orb_fade import ORBFade
        s = ORBFade(_config())
        m = {
            "rth_15min_high": 22050,
            "rth_15min_low": 22000,
            "price": 22075,
            "vwap": 22025,
            "now_ct": _ct_dt(2026, 5, 15, 13, 0),  # outside 08:45-12:00
        }
        bars = [Bar(0, 0, 0, 0, 100, 0, 0) for _ in range(20)]
        assert s.evaluate(m, [], bars, {}) is None

    def test_returns_none_no_breakout(self):
        """If no recent bar broke OR, no fade signal."""
        from strategies.orb_fade import ORBFade
        s = ORBFade(_config())
        now = _ct_dt(2026, 5, 15, 9, 30)
        m = {
            "rth_15min_high": 22050,
            "rth_15min_low": 22000,
            "price": 22025,  # inside OR
            "vwap": 22025,
            "now_ct": now,
        }
        bars = []
        for i in range(20):
            ts = (now - timedelta(minutes=20 - i)).timestamp()
            # All bars stay inside OR
            bars.append(Bar(22025, 22030, 22020, 22025, 100, ts - 60, ts, delta=0))
        assert s.evaluate(m, [], bars, {}) is None

    def test_fade_fires_on_failed_long_breakout(self):
        """Multi-bar pattern: breakout bar closed above OR, then retrace bar
        closed back inside with rejection wick + CVD diverged → SHORT fade."""
        from strategies.orb_fade import ORBFade
        s = ORBFade(_config())
        now = _ct_dt(2026, 5, 15, 9, 30)
        m = {
            "rth_15min_high": 22050,
            "rth_15min_low": 22000,
            "price": 22045,  # back inside OR
            "vwap": 22025,
            "now_ct": now,
        }
        bars = []
        # 17 background bars inside OR with negative delta accumulating
        for i in range(17):
            ts = (now - timedelta(minutes=19 - i)).timestamp()
            bars.append(Bar(22030, 22035, 22025, 22030, 150, ts - 60, ts,
                            delta=-30, bar_delta=-30))
        # Bar -2 (the breakout): closed AT 22055 (5pt above OR_high=22050)
        # with elevated volume (the fake breakout)
        ts = (now - timedelta(minutes=1)).timestamp()
        bars.append(Bar(22045, 22057, 22045, 22055, 350, ts - 60, ts,
                        delta=-50, bar_delta=-50))
        # Bar -1 (current/retrace): high=22052 (still tagging), low=22043,
        # close=22045 (BACK INSIDE OR with strong upper-wick rejection)
        ts = now.timestamp()
        bars.append(Bar(22054, 22052, 22043, 22045, 200, ts - 60, ts,
                        delta=-100, bar_delta=-100))
        # Range = 9, upper wick = 22052-22045 = 7, wick_pct = 78% — strong rejection

        sig = s.evaluate(m, [], bars, {})
        assert sig is not None, "Expected SHORT fade signal"
        assert sig.direction == "SHORT"
        # Stop should be reasonable (clamped to max_stop_ticks=30 by default)
        assert sig.stop_ticks <= 30
        assert sig.stop_ticks >= 8

    def test_fade_skipped_when_cvd_confirms_breakout(self):
        """Multi-bar breakout pattern, but CVD positive → real breakout, no fade."""
        from strategies.orb_fade import ORBFade
        s = ORBFade(_config())
        now = _ct_dt(2026, 5, 15, 9, 30)
        m = {
            "rth_15min_high": 22050,
            "rth_15min_low": 22000,
            "price": 22045,
            "vwap": 22025,
            "now_ct": now,
        }
        bars = []
        # 17 background bars with POSITIVE delta (real buying)
        for i in range(17):
            ts = (now - timedelta(minutes=19 - i)).timestamp()
            bars.append(Bar(22030, 22035, 22025, 22030, 150, ts - 60, ts,
                            delta=+50, bar_delta=+50))
        # Breakout bar — closed above OR with positive delta
        ts = (now - timedelta(minutes=1)).timestamp()
        bars.append(Bar(22045, 22057, 22045, 22055, 350, ts - 60, ts,
                        delta=+100, bar_delta=+100))
        # Retrace bar — minor pullback but CVD still positive overall
        ts = now.timestamp()
        bars.append(Bar(22054, 22052, 22043, 22045, 200, ts - 60, ts,
                        delta=+80, bar_delta=+80))
        sig = s.evaluate(m, [], bars, {})
        assert sig is None, "Expected None when CVD confirms breakout"

    def test_fade_skipped_when_current_bar_outside_or(self):
        """If current bar hasn't retraced back inside OR yet, no fade."""
        from strategies.orb_fade import ORBFade
        s = ORBFade(_config())
        now = _ct_dt(2026, 5, 15, 9, 30)
        m = {
            "rth_15min_high": 22050,
            "rth_15min_low": 22000,
            "price": 22055,  # STILL above OR_high
            "vwap": 22025,
            "now_ct": now,
        }
        bars = []
        for i in range(17):
            ts = (now - timedelta(minutes=19 - i)).timestamp()
            bars.append(Bar(22030, 22035, 22025, 22030, 150, ts - 60, ts,
                            delta=-30, bar_delta=-30))
        ts = (now - timedelta(minutes=1)).timestamp()
        bars.append(Bar(22045, 22057, 22045, 22055, 350, ts - 60, ts,
                        delta=-50, bar_delta=-50))
        # Current bar still ABOVE OR
        ts = now.timestamp()
        bars.append(Bar(22055, 22060, 22053, 22057, 200, ts - 60, ts,
                        delta=-100, bar_delta=-100))
        sig = s.evaluate(m, [], bars, {})
        assert sig is None, "Expected None — current bar still outside OR"

    def test_daily_max_enforced(self):
        from strategies.orb_fade import ORBFade
        s = ORBFade(_config(max_trades_per_day=1))
        s._trades_today = 1
        s._trade_date = _ct_dt(2026, 5, 15, 9, 30).strftime("%Y-%m-%d")
        m = {
            "rth_15min_high": 22050,
            "rth_15min_low": 22000,
            "price": 22025,
            "now_ct": _ct_dt(2026, 5, 15, 9, 35),
        }
        bars = [Bar(22025, 22030, 22020, 22025, 100, 0, 0) for _ in range(20)]
        assert s.evaluate(m, [], bars, {}) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
