"""End-to-end tests for strategies.nq_lsr — the LSR strategy.

These tests stub out the BaseStrategy import so the strategy can be
exercised without the full Phoenix bot context. The real strategy
inherits from strategies.base_strategy.BaseStrategy which is provided
by the project.

Run with:
    pytest tests/test_nq_lsr.py -v
"""
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest


# ────────────────────────────────────────────────────────────────────
# Test fixtures — minimal Bar / BaseStrategy / Signal stubs
# ────────────────────────────────────────────────────────────────────
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
    delta: float = 0.0           # bar delta (buyer - seller volume)
    bar_delta: float = 0.0       # alias used by some strategies


@pytest.fixture(autouse=True)
def stub_base_strategy(monkeypatch):
    """Provide a minimal BaseStrategy + Signal so the LSR module can import."""
    import types

    # Build a real module (not MagicMock) so attribute access works
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
        enabled = True
        validated = False
        computes_own_stop = False
        computes_own_target = False

        def __init__(self, config):
            self.config = config
            self.enabled = config.get("enabled", True)
            self.validated = config.get("validated", False)

    base_mod.Signal = _Signal
    base_mod.BaseStrategy = _BaseStrategy
    monkeypatch.setitem(sys.modules, "strategies.base_strategy", base_mod)


def _ct_dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=_CT)


def _ct_ts(*args):
    return _ct_dt(*args).timestamp()


def _make_bars_1m(n=50, start_dt=None, base_price=22000.0, vol_per_bar=100):
    """Build N flat 1-min bars ending at start_dt - now."""
    if start_dt is None:
        start_dt = _ct_dt(2026, 5, 15, 9, 30)
    bars = []
    for i in range(n):
        ts = (start_dt - timedelta(minutes=n - i)).timestamp()
        p = base_price + (i % 5) * 0.5  # slight wiggle
        bars.append(Bar(open=p, high=p + 0.5, low=p - 0.5, close=p, volume=vol_per_bar,
                        start_time=ts - 60, end_time=ts, delta=0, bar_delta=0))
    return bars


def _make_market(price, vwap, atr_5m=20.0, cvd=0):
    return {
        "price": price,
        "vwap": vwap,
        "atr_5m": atr_5m,
        "cvd": cvd,
        "now_ct": _ct_dt(2026, 5, 15, 9, 30),
        "tf_votes_bullish": 0,
        "tf_votes_bearish": 0,
    }


def _strategy_config(**overrides):
    """Standard test config — disables freshness so fixed dates don't get rejected."""
    base = {
        "enabled": True,
        "bar_freshness_sec": 10**9,  # effectively disabled
    }
    base.update(overrides)
    return base


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────
class TestLSREntryGates:
    def test_strategy_imports(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config())
        assert s.name == "nq_lsr"

    def test_returns_none_outside_session_window(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config(
            session_windows_ct=[("08:30", "11:00")],
        ))
        m = _make_market(22000, 22000)
        m["now_ct"] = _ct_dt(2026, 5, 15, 13, 0)  # outside window
        bars = _make_bars_1m()
        result = s.evaluate(m, [], bars, {})
        assert result is None

    def test_returns_none_with_too_few_bars(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config())
        m = _make_market(22000, 22000)
        result = s.evaluate(m, [], [], {})
        assert result is None

    def test_returns_none_no_active_levels(self):
        """Without any tracked levels, no sweep can be detected."""
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config())
        m = _make_market(22000, 22000)
        # 50 flat bars within today's session — no PDH from yesterday,
        # no PSH/PSL until 8:30, no ORH/ORL until 8:45, no swings detected yet
        m["now_ct"] = _ct_dt(2026, 5, 15, 8, 32)
        bars = _make_bars_1m(50, start_dt=m["now_ct"])
        result = s.evaluate(m, [], bars, {})
        assert result is None

    def test_long_sweep_with_full_confluence_fires(self):
        """Build a scenario with PDH/PDL set + a clear long sweep + CVD div + volume."""
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config())

        now = _ct_dt(2026, 5, 15, 9, 30)
        m = _make_market(21997.5, 22002.0)
        m["now_ct"] = now

        bars = []

        # Yesterday's RTH bars — establish PDL at 21995
        for i in range(30):
            ts = _ct_dt(2026, 5, 14, 9, 0).timestamp() + i * 60
            low = 21995.0 if i == 15 else 22010.0
            bars.append(Bar(open=22020, high=22030, low=low, close=22020,
                            volume=100, start_time=ts - 60, end_time=ts,
                            delta=0, bar_delta=0))

        # Today's bars leading up to sweep — positive delta accumulating
        for i in range(20):
            ts = (now - timedelta(minutes=21 - i)).timestamp()
            bars.append(Bar(open=22001, high=22002, low=22000, close=22001,
                            volume=150, start_time=ts - 60, end_time=ts,
                            delta=50, bar_delta=50))

        # SWEEP BAR: low pierces 21995 PDL by 4 ticks, closes BACK ABOVE with strong rejection
        # Range = 21998.5 - 21994 = 4.5pt; lower wick = 21997.5 - 21994 = 3.5pt → 78% rejection
        ts = now.timestamp()
        sweep_bar = Bar(
            open=21998.0, high=21998.5, low=21994.0,
            close=21997.5,                            # strong rejection close
            volume=500,                               # 3.3x avg of 150
            start_time=ts - 60, end_time=ts,
            delta=200, bar_delta=200,                 # positive — absorption
        )
        bars.append(sweep_bar)

        m["price"] = sweep_bar.close
        m["vwap"] = 22002.0

        result = s.evaluate(m, [], bars, {})

        assert result is not None, "Expected a Signal but got None"
        assert result.direction == "LONG"
        assert result.entry_price == 21997.5
        assert result.stop_price < sweep_bar.low
        assert 8 <= result.stop_ticks <= 30

    def test_sweep_with_negative_delta_no_divergence_skipped(self):
        """A sweep where bar_delta is negative (sellers winning) should NOT fire."""
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config())

        now = _ct_dt(2026, 5, 15, 9, 30)
        m = _make_market(21997.5, 22002)
        m["now_ct"] = now
        bars = []
        for i in range(30):
            ts = _ct_dt(2026, 5, 14, 9, 0).timestamp() + i * 60
            low = 21995.0 if i == 15 else 22010.0
            bars.append(Bar(open=22020, high=22030, low=low, close=22020,
                            volume=100, start_time=ts - 60, end_time=ts,
                            delta=0, bar_delta=0))
        for i in range(20):
            ts = (now - timedelta(minutes=21 - i)).timestamp()
            bars.append(Bar(open=22001, high=22002, low=22000, close=22001,
                            volume=150, start_time=ts - 60, end_time=ts,
                            delta=-100, bar_delta=-100))
        ts = now.timestamp()
        bars.append(Bar(open=21998, high=21998.5, low=21994.0, close=21997.5,
                        volume=500, start_time=ts - 60, end_time=ts,
                        delta=-300, bar_delta=-300))
        m["price"] = 21997.5
        result = s.evaluate(m, [], bars, {})
        assert result is None, "Expected None when no CVD divergence — got a signal"

    def test_sweep_with_wide_stop_rejected(self):
        """If structural stop > max_stop_ticks, signal must be rejected."""
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config(max_stop_ticks=10))

        now = _ct_dt(2026, 5, 15, 9, 30)
        m = _make_market(21995.5, 22002)
        m["now_ct"] = now
        bars = []
        for i in range(30):
            ts = _ct_dt(2026, 5, 14, 9, 0).timestamp() + i * 60
            low = 21995.0 if i == 15 else 22010.0
            bars.append(Bar(open=22020, high=22030, low=low, close=22020,
                            volume=100, start_time=ts - 60, end_time=ts,
                            delta=0, bar_delta=0))
        for i in range(20):
            ts = (now - timedelta(minutes=21 - i)).timestamp()
            bars.append(Bar(open=22001, high=22002, low=22000, close=22001,
                            volume=150, start_time=ts - 60, end_time=ts,
                            delta=50, bar_delta=50))
        # Sweep bar with HUGE wick — close - low = 15.5pt = 62 ticks > max 10
        ts = now.timestamp()
        bars.append(Bar(open=21996, high=21997, low=21980.0, close=21995.5,
                        volume=500, start_time=ts - 60, end_time=ts,
                        delta=200, bar_delta=200))
        m["price"] = 21995.5
        result = s.evaluate(m, [], bars, {})
        assert result is None, "Expected None due to stop too wide"

    def test_daily_max_trades_enforced(self):
        from strategies.nq_lsr import NQLiquiditySweepReversal
        s = NQLiquiditySweepReversal(_strategy_config(max_trades_per_day=1))
        s._trades_today = 1
        s._trade_date = _ct_dt(2026, 5, 15, 9, 30).strftime("%Y-%m-%d")

        m = _make_market(22000, 22000)
        m["now_ct"] = _ct_dt(2026, 5, 15, 9, 35)
        bars = _make_bars_1m()
        result = s.evaluate(m, [], bars, {})
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
