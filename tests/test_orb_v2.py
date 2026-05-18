"""Tests for ORB v2 — drop-in for the standalone ORB strategy.

The whole point: V1 rejects nearly every signal on NQ 2026 via
`gate:stop_distance_too_wide` (OR opposite > 25pt = 100t). V2 should
fire on those same setups via confirmation-stop fallback.

Run with:
    pytest tests/test_orb_v2.py -v
"""
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

_ET = ZoneInfo("America/New_York")
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


def _et_dt(*args):
    return datetime(*args, tzinfo=_ET)


def _build_nq_orb_scenario(or_width_pts=50, breakout_direction="LONG",
                            cvd_aligned=True):
    """Build an NQ-typical ORB scenario.

    OR width=50pt would normally trigger V1's stop_distance_too_wide
    rejection. V2 should fire via confirmation-stop fallback.
    """
    now = _et_dt(2026, 5, 15, 10, 30)  # 30 min after open
    bars_1m = []
    bars_5m = []

    or_low = 22000.0
    or_high = or_low + or_width_pts

    # Session opens at 09:30 ET. Build 15 1m bars in the OR window
    # to establish the OR.
    session_open = _et_dt(2026, 5, 15, 9, 30)
    for i in range(15):
        ts = (session_open + timedelta(minutes=i)).timestamp()
        # Bars span the OR range
        if i < 5:
            # First 5 bars touch high
            high = or_high - i * 0.5
            low = or_low + 10
        elif i < 10:
            # Middle bars touch low
            high = or_high - 10
            low = or_low + i * 0.5
        else:
            # Last 5 bars middle of range
            high = or_high - 5
            low = or_low + 5
        delta = (5 if cvd_aligned and breakout_direction == "LONG"
                 else -5 if cvd_aligned and breakout_direction == "SHORT"
                 else 0)
        bars_1m.append(Bar(
            open=or_low + 25, high=high, low=low, close=or_low + 25,
            volume=200, start_time=ts - 60, end_time=ts,
            delta=delta, bar_delta=delta,
        ))

    # Then add some bars between OR-set and breakout
    for i in range(15, 60):
        ts = (session_open + timedelta(minutes=i)).timestamp()
        # Price hovers near OR_high before breakout
        if breakout_direction == "LONG":
            base = or_high - 5
        else:
            base = or_low + 5
        delta_val = (50 if cvd_aligned and breakout_direction == "LONG"
                     else -50 if cvd_aligned and breakout_direction == "SHORT"
                     else 0)
        bars_1m.append(Bar(
            open=base, high=base + 3, low=base - 3, close=base,
            volume=150, start_time=ts - 60, end_time=ts,
            delta=delta_val, bar_delta=delta_val,
        ))

    # Build 5m bars from 1m (rough aggregation — just sample every 5th)
    for i in range(0, len(bars_1m), 5):
        chunk = bars_1m[i:i+5]
        if not chunk:
            continue
        bars_5m.append(Bar(
            open=chunk[0].open,
            high=max(b.high for b in chunk),
            low=min(b.low for b in chunk),
            close=chunk[-1].close,
            volume=sum(b.volume for b in chunk),
            start_time=chunk[0].start_time,
            end_time=chunk[-1].end_time,
            delta=sum(b.delta for b in chunk),
            bar_delta=sum(b.bar_delta for b in chunk),
        ))

    # Now overwrite the LAST 5m bar to be the breakout
    if breakout_direction == "LONG":
        breakout_close = or_high + 3
    else:
        breakout_close = or_low - 3
    bars_5m[-1] = Bar(
        open=or_high - 5 if breakout_direction == "LONG" else or_low + 5,
        high=or_high + 5 if breakout_direction == "LONG" else or_low + 5,
        low=or_high - 5 if breakout_direction == "LONG" else or_low - 5,
        close=breakout_close,
        volume=400,
        start_time=bars_5m[-1].start_time,
        end_time=bars_5m[-1].end_time,
        delta=bars_5m[-1].delta,
        bar_delta=bars_5m[-1].bar_delta,
    )

    return bars_1m, bars_5m, or_high, or_low, now


class TestORBv2:
    def test_imports(self):
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        assert s.name == "orb_v2"

    def test_fires_on_wide_or_long_breakout_with_cvd_aligned(self):
        """V1 would reject 50pt OR via stop_distance_too_wide (50pt > 25pt cap).
        V2 should fire via confirmation-stop fallback."""
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        bars_1m, bars_5m, or_high, or_low, now = _build_nq_orb_scenario(
            or_width_pts=50, breakout_direction="LONG", cvd_aligned=True
        )
        m = {"price": or_high + 3, "atr_5m": 15, "now_ct": now.astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is not None, "V2 should fire on 50pt OR (V1 would skip on stop_too_wide)"
        assert result.direction == "LONG"
        # Stop should be reasonable (confirmation fallback)
        assert result.stop_ticks <= 60, f"Stop ticks {result.stop_ticks} should be ≤ max_stop=60"
        assert result.stop_ticks >= 12, f"Stop ticks {result.stop_ticks} should be ≥ min=12"

    def test_skips_when_cvd_misaligned(self):
        """If 5-bar delta_sum opposes breakout direction, no fire."""
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        bars_1m, bars_5m, or_high, or_low, now = _build_nq_orb_scenario(
            or_width_pts=50, breakout_direction="LONG", cvd_aligned=False
        )
        # Force CVD to be negative on recent bars
        for b in bars_1m[-10:]:
            b.delta = -100
            b.bar_delta = -100
        m = {"price": or_high + 3, "atr_5m": 15, "now_ct": now.astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is None, "V2 should skip LONG breakout when CVD is negative (leaves it for orb_fade)"

    def test_skips_when_cvd_data_missing(self):
        """If all deltas are exactly 0, can't evaluate the gate → skip."""
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        bars_1m, bars_5m, or_high, or_low, now = _build_nq_orb_scenario(
            or_width_pts=50, breakout_direction="LONG", cvd_aligned=True
        )
        for b in bars_1m:
            b.delta = 0
            b.bar_delta = 0
        m = {"price": or_high + 3, "atr_5m": 15, "now_ct": now.astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is None, "V2 should refuse to fire when delta data missing"

    def test_fires_short_when_cvd_aligned_short(self):
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        bars_1m, bars_5m, or_high, or_low, now = _build_nq_orb_scenario(
            or_width_pts=50, breakout_direction="SHORT", cvd_aligned=True
        )
        m = {"price": or_low - 3, "atr_5m": 15, "now_ct": now.astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is not None
        assert result.direction == "SHORT"

    def test_or_too_tight_blocks(self):
        """OR < 11pt should be skipped (low-vol day)."""
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        bars_1m, bars_5m, or_high, or_low, now = _build_nq_orb_scenario(
            or_width_pts=8, breakout_direction="LONG", cvd_aligned=True
        )
        m = {"price": or_high + 3, "atr_5m": 5, "now_ct": now.astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is None, "OR width 8pt is below min 11pt"

    def test_prices_on_tick_grid(self):
        """All emitted prices must be multiples of 0.25."""
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        bars_1m, bars_5m, or_high, or_low, now = _build_nq_orb_scenario(
            or_width_pts=50, breakout_direction="LONG", cvd_aligned=True
        )
        m = {"price": or_high + 3, "atr_5m": 15, "now_ct": now.astimezone(_CT)}
        result = s.evaluate(m, bars_5m, bars_1m, {})
        assert result is not None
        for name, p in [("entry", result.entry_price),
                        ("stop", result.stop_price),
                        ("target", result.target_price)]:
            assert abs((p / 0.25) - round(p / 0.25)) < 1e-9, \
                f"{name}={p} not on tick grid"

    def test_daily_dedup(self):
        """Once traded today, no second fire."""
        from strategies.orb_v2 import ORBv2
        s = ORBv2({"enabled": True})
        bars_1m, bars_5m, or_high, or_low, now = _build_nq_orb_scenario(
            or_width_pts=50, breakout_direction="LONG", cvd_aligned=True
        )
        m = {"price": or_high + 3, "atr_5m": 15, "now_ct": now.astimezone(_CT)}
        r1 = s.evaluate(m, bars_5m, bars_1m, {})
        r2 = s.evaluate(m, bars_5m, bars_1m, {})
        assert r1 is not None
        assert r2 is None, "Second fire same day should be blocked"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
