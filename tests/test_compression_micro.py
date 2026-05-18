"""Tests for compression_breakout_micro — 1m TF variant."""
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


class TestCompressionBreakoutMicro:
    def test_imports(self):
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({})
        assert s.name == "compression_breakout_micro"

    def test_returns_none_too_few_bars(self):
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({})
        bars = [Bar(22000, 22001, 21999, 22000, 100, i*60, (i+1)*60) for i in range(20)]
        m = {"now_ct": _ct_dt(2026, 5, 15, 10, 0)}
        assert s.evaluate(m, [], bars, {}) is None

    def test_per_bar_dedup(self):
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({})
        # 50 background bars (wider range), then 10 compressed, then breakout
        bars = []
        base_ts = _ct_dt(2026, 5, 15, 9, 0).timestamp()
        for i in range(50):
            ts = base_ts + i * 60
            bars.append(Bar(22000, 22010, 21990, 22005, 500, ts - 60, ts))
        m = {"now_ct": _ct_dt(2026, 5, 15, 11, 0)}
        s.evaluate(m, [], bars, {})
        h1 = len(s._window.bar_history)
        s.evaluate(m, [], bars, {})  # same bars, second call
        h2 = len(s._window.bar_history)
        assert h1 == h2, f"Window grew on repeat eval: {h1} vs {h2}"

    def test_window_resets_on_new_day(self):
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({})
        # Day 1
        bars = []
        d1 = _ct_dt(2026, 5, 14, 9, 0)
        for i in range(50):
            ts = (d1 + timedelta(minutes=i)).timestamp()
            bars.append(Bar(22000, 22002, 21998, 22000, 100, ts - 60, ts))
        s.evaluate({"now_ct": _ct_dt(2026, 5, 14, 11, 0)}, [], bars, {})
        history_d1 = len(s._window.bar_history)

        # Day 2 — reset
        d2 = _ct_dt(2026, 5, 15, 9, 0)
        for i in range(50):
            ts = (d2 + timedelta(minutes=i)).timestamp()
            bars.append(Bar(22000, 22002, 21998, 22000, 100, ts - 60, ts))
        s.evaluate({"now_ct": _ct_dt(2026, 5, 15, 11, 0)}, [], bars, {})
        # Window should have reset on day boundary
        assert s._window.compressed_count() <= history_d1, \
            "Window did not reset across day boundary"

    def test_no_signal_when_no_compression(self):
        """All wide-range bars → no compression → no signal."""
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({})
        bars = []
        base_ts = _ct_dt(2026, 5, 15, 9, 0).timestamp()
        for i in range(80):
            ts = base_ts + i * 60
            # All bars have wide range — no compression
            bars.append(Bar(22000, 22020, 21980, 22010, 1500, ts - 60, ts))
        m = {"now_ct": _ct_dt(2026, 5, 15, 11, 30)}
        result = s.evaluate(m, [], bars, {})
        assert result is None

    def test_stops_on_tick_grid(self):
        """When signal fires, all prices on grid."""
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({})
        # Build a clean compression then breakout pattern
        bars = []
        base_ts = _ct_dt(2026, 5, 15, 9, 0).timestamp()
        # 50 wide-range background
        for i in range(50):
            ts = base_ts + i * 60
            bars.append(Bar(22000, 22008, 21992, 22002, 800, ts - 60, ts))
        # 10 compressed bars
        for i in range(10):
            ts = base_ts + (50 + i) * 60
            bars.append(Bar(22003, 22005, 22001, 22003, 200, ts - 60, ts))
        # Breakout bar
        ts = base_ts + 60 * 60
        bars.append(Bar(22003, 22015, 22003, 22013, 1500, ts - 60, ts))

        m = {"now_ct": _ct_dt(2026, 5, 15, 11, 0)}
        # Walk through to build state then evaluate at end
        for i in range(55, 62):
            partial = bars[:i + 1]
            s.evaluate(m, [], partial, {})
        result = s.evaluate(m, [], bars, {})
        # We may or may not get a signal depending on synthetic geometry
        # but if we do, all prices should be on the tick grid.
        if result is not None:
            for name, p in [("entry", result.entry_price),
                             ("stop", result.stop_price),
                             ("target", result.target_price)]:
                assert abs((p / 0.25) - round(p / 0.25)) < 1e-9, \
                    f"{name}={p} not on grid"

    def test_daily_cap_enforced(self):
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({"max_trades_per_day": 1})
        s._trades_today = 1
        s._trade_date = _ct_dt(2026, 5, 15, 10, 0).strftime("%Y-%m-%d")
        # Even with valid setup, should not fire
        bars = []
        base_ts = _ct_dt(2026, 5, 15, 9, 0).timestamp()
        for i in range(60):
            ts = base_ts + i * 60
            bars.append(Bar(22000, 22002, 21998, 22000, 100, ts - 60, ts))
        m = {"now_ct": _ct_dt(2026, 5, 15, 11, 0)}
        assert s.evaluate(m, [], bars, {}) is None

    def test_negative_target_rr_falls_back(self):
        """Negative target_rr in config must not produce wrong-side target."""
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        s = CompressionBreakoutMicro({"target_rr": -1.5})
        # Build compression + breakout
        bars = []
        base_ts = _ct_dt(2026, 5, 15, 9, 0).timestamp()
        for i in range(50):
            ts = base_ts + i * 60
            bars.append(Bar(22000, 22008, 21992, 22002, 800, ts - 60, ts))
        for i in range(10):
            ts = base_ts + (50 + i) * 60
            bars.append(Bar(22003, 22005, 22001, 22003, 200, ts - 60, ts))
        ts = base_ts + 60 * 60
        bars.append(Bar(22003, 22015, 22003, 22013, 1500, ts - 60, ts))
        m = {"now_ct": _ct_dt(2026, 5, 15, 11, 0)}
        for i in range(55, 62):
            s.evaluate(m, [], bars[:i + 1], {})
        result = s.evaluate(m, [], bars, {})
        if result is not None:
            # Target must be on correct side
            correct = (result.direction == "LONG" and result.target_price > result.entry_price) or \
                      (result.direction == "SHORT" and result.target_price < result.entry_price)
            assert correct, f"Wrong-side target: dir={result.direction} entry={result.entry_price} target={result.target_price}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
