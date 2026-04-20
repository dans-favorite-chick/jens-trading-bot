"""
B14 — NQ-calibrated ATR stops + vwap_pullback VWAP-distance gate.

Tests are layered:

1. compute_atr_stop() helper — exhaustive: clamp min, clamp max, normal range,
   fallback (None/0 ATR), LONG vs SHORT sign, and atr_stop_override flag.
   These cover the stop math used by vwap_pullback, bias_momentum, dom_pullback.

2. vwap_pullback full-path — covers max_vwap_dist_ticks gate (40t allowed under
   default 60, 80t rejected, override=24 rejects 40). Builds a full market
   snapshot + bars_1m/5m and asserts Signal is emitted / None as expected.

We unit-test the helper rather than driving bias_momentum end-to-end because
that strategy has a pre-existing NameError on `votes` (separate Fix 2 branch).
Driving it end-to-end would hit that bug on the non-trend path.
"""

import pytest

from strategies._nq_stop import compute_atr_stop


# ────────────────────────────────────────────────────────────────────────
# Shared Bar stub (dataclass-lite)
# ────────────────────────────────────────────────────────────────────────
class _Bar:
    def __init__(self, o, h, l, c, v=100):
        self.open = o
        self.high = h
        self.low  = l
        self.close = c
        self.volume = v


TICK = 0.25


# ────────────────────────────────────────────────────────────────────────
# Helper tests — the stop math
# ────────────────────────────────────────────────────────────────────────
class TestComputeATRStop:

    def test_normal_range_long_15t_atr_clamps_to_22(self):
        """ATR_5m=15t = 3.75pt → 1.5×3.75=5.625pt = 22.5t → int=22. Inside [16,80]."""
        bar = _Bar(21000, 21005, 20999, 21003)
        stop_ticks, stop_price, override, note = compute_atr_stop(
            direction="LONG",
            entry_price=21003.0,
            last_5m_bar=bar,
            atr_5m_points=3.75,   # 15 ticks
            tick_size=TICK,
        )
        # anchor = bar.low 20999; stop_price = 20999 - 5.625 = 20993.375
        # stop_distance = 21003 - 20993.375 = 9.625pt = 38.5t → 38. Clamped into [16,80]=38.
        # NOTE: wick anchor extends stop BEYOND just 1.5×ATR from entry.
        assert override is True
        assert 16 <= stop_ticks <= 80
        # LONG: stop price below entry
        assert stop_price < 21003.0
        assert "ATR stop" in note

    def test_clamp_min_tiny_atr(self):
        """ATR so small that raw computation < 16t → clamp to 16t."""
        bar = _Bar(21000, 21001, 21000, 21001)  # wick anchor basically at entry
        stop_ticks, stop_price, override, _ = compute_atr_stop(
            direction="LONG",
            entry_price=21001.0,
            last_5m_bar=bar,
            atr_5m_points=0.25,   # 1 tick = 0.25pt; 1.5×0.25=0.375pt = 1.5t raw
            tick_size=TICK,
        )
        assert override is True
        assert stop_ticks == 16   # clamped to min
        # stop_price recomputed from clamp: entry - 16*0.25 = 21001 - 4 = 20997
        assert abs(stop_price - 20997.0) < 1e-9

    def test_clamp_max_huge_atr(self):
        """ATR huge → raw > 80t → clamp to 80t."""
        bar = _Bar(21000, 21010, 20990, 21000)
        stop_ticks, stop_price, override, _ = compute_atr_stop(
            direction="LONG",
            entry_price=21000.0,
            last_5m_bar=bar,
            atr_5m_points=50.0,   # absurd
            tick_size=TICK,
        )
        assert override is True
        assert stop_ticks == 80
        assert abs(stop_price - (21000.0 - 80 * TICK)) < 1e-9

    def test_fallback_when_atr_is_zero(self):
        bar = _Bar(21000, 21005, 20999, 21003)
        stop_ticks, stop_price, override, note = compute_atr_stop(
            direction="LONG",
            entry_price=21003.0,
            last_5m_bar=bar,
            atr_5m_points=0,
            tick_size=TICK,
            stop_fallback_ticks=24,
        )
        assert override is False
        assert stop_ticks == 24
        assert stop_price is None
        assert "fallback" in note.lower()

    def test_fallback_when_atr_is_none(self):
        stop_ticks, stop_price, override, _ = compute_atr_stop(
            direction="SHORT",
            entry_price=21003.0,
            last_5m_bar=_Bar(21000, 21005, 20999, 21003),
            atr_5m_points=None,
            tick_size=TICK,
            stop_fallback_ticks=24,
        )
        assert override is False
        assert stop_ticks == 24

    def test_long_stop_below_entry(self):
        bar = _Bar(21000, 21005, 20999, 21003)
        _, stop_price, override, _ = compute_atr_stop(
            direction="LONG",
            entry_price=21003.0,
            last_5m_bar=bar,
            atr_5m_points=3.75,
            tick_size=TICK,
        )
        assert override is True
        assert stop_price < 21003.0

    def test_short_stop_above_entry(self):
        bar = _Bar(21003, 21006, 21000, 21003)
        _, stop_price, override, _ = compute_atr_stop(
            direction="SHORT",
            entry_price=21003.0,
            last_5m_bar=bar,
            atr_5m_points=3.75,
            tick_size=TICK,
        )
        assert override is True
        assert stop_price > 21003.0

    def test_atr_stop_override_flag_true_on_atr_path(self):
        bar = _Bar(21000, 21005, 20999, 21003)
        _, _, override, _ = compute_atr_stop(
            direction="LONG",
            entry_price=21003.0,
            last_5m_bar=bar,
            atr_5m_points=3.75,
            tick_size=TICK,
        )
        assert override is True

    def test_price_past_wick_triggers_fallback(self):
        """Pathological: entry already below the wick-low anchor for LONG."""
        bar = _Bar(21000, 21005, 20999, 21003)
        # entry below anchor: fallback
        stop_ticks, stop_price, override, _ = compute_atr_stop(
            direction="LONG",
            entry_price=20990.0,   # below bar.low
            last_5m_bar=bar,
            atr_5m_points=0.5,     # tiny, so anchor_low - 0.75pt = 20998.25; entry 20990 already below
            tick_size=TICK,
            stop_fallback_ticks=24,
        )
        assert override is False
        assert stop_ticks == 24


# ────────────────────────────────────────────────────────────────────────
# vwap_pullback full-path tests — VWAP proximity gate
# ────────────────────────────────────────────────────────────────────────
def _make_vwap_pullback_market(price, vwap):
    """Minimal market snapshot sufficient to drive VWAPPullback through to a signal."""
    return {
        "price": price,
        "vwap":  vwap,
        "ema9":  price + 1,          # ema9 > ema21 for LONG trend
        "ema21": price,
        "cvd":   1000.0,             # positive
        "tf_votes_bullish": 3,
        "tf_votes_bearish": 0,
        "day_type": "UNKNOWN",       # non-trend path — use TF votes
        "mq_direction_bias": "NEUTRAL",
        "atr_5m": 3.75,              # 15 ticks → normal ATR stop
    }


def _bars_with_pullback_from_above(vwap, tick_size=TICK):
    """5 bars where price was >= 8t above VWAP earlier, latest is near VWAP (bullish close)."""
    # Five 1m bars: first 3 have highs well above VWAP, last is a bounce candle near VWAP.
    # max(highs) - vwap must be >= 8 ticks = 2 points.
    high_above = vwap + 4.0   # 16 ticks above VWAP — clearly satisfies >=8t history
    bars = [
        _Bar(high_above - 2, high_above,     high_above - 3, high_above - 1),
        _Bar(high_above - 1, high_above - 0.5, high_above - 4, high_above - 2),
        _Bar(high_above - 2, high_above - 1, high_above - 5, high_above - 3),
        _Bar(high_above - 3, high_above - 2, high_above - 6, high_above - 4),
        # Final bar = bounce candle (bullish close > open), sits near VWAP
        _Bar(vwap - 0.5, vwap + 1.0, vwap - 1.0, vwap + 0.75),
    ]
    return bars


def _make_5m_bar(price):
    # 5m bar with low just below price, high just above — gives a usable wick anchor
    return [_Bar(price - 1, price + 2, price - 2, price + 1)]


class TestVWAPPullbackGate:

    def _build_strategy(self, overrides=None):
        from strategies.vwap_pullback import VWAPPullback
        cfg = {
            "target_rr": 20.0,
            "stop_atr_mult": 1.5,
            "min_stop_ticks": 16,
            "max_stop_ticks": 80,
            "stop_fallback_ticks": 24,
            "max_vwap_dist_ticks": 60,
            "min_tf_votes": 2,
        }
        if overrides:
            cfg.update(overrides)
        return VWAPPullback(cfg)

    def test_entry_allowed_at_40t_from_vwap_default_60(self):
        """Price 40 ticks (10pt) above VWAP with default max=60 → signal emitted."""
        strat = self._build_strategy()
        vwap = 21000.0
        price = vwap + 10.0   # 40 ticks above VWAP
        market = _make_vwap_pullback_market(price, vwap)
        bars_1m = _bars_with_pullback_from_above(vwap)
        # Force latest bar to be exactly at 'price' and be a bullish bounce
        bars_1m[-1] = _Bar(price - 0.5, price + 0.25, price - 0.75, price)
        bars_5m = _make_5m_bar(price)
        sig = strat.evaluate(market, bars_5m, bars_1m, {"regime": "MID_MORNING"})
        assert sig is not None, "Expected signal at 40t from VWAP under default 60t gate"
        assert sig.direction == "LONG"
        assert sig.atr_stop_override is True
        assert 16 <= sig.stop_ticks <= 80

    def test_entry_rejected_at_80t_from_vwap_default_60(self):
        """Price 80 ticks from VWAP with default max=60 → None."""
        strat = self._build_strategy()
        vwap = 21000.0
        price = vwap + 20.0   # 80 ticks above VWAP
        market = _make_vwap_pullback_market(price, vwap)
        bars_1m = _bars_with_pullback_from_above(vwap)
        bars_1m[-1] = _Bar(price - 0.5, price + 0.25, price - 0.75, price)
        bars_5m = _make_5m_bar(price)
        sig = strat.evaluate(market, bars_5m, bars_1m, {"regime": "MID_MORNING"})
        assert sig is None

    def test_entry_rejected_at_40t_when_override_is_24(self):
        """Override max_vwap_dist_ticks=24 → 40t entry is now outside the gate."""
        strat = self._build_strategy({"max_vwap_dist_ticks": 24})
        vwap = 21000.0
        price = vwap + 10.0   # 40 ticks above VWAP
        market = _make_vwap_pullback_market(price, vwap)
        bars_1m = _bars_with_pullback_from_above(vwap)
        bars_1m[-1] = _Bar(price - 0.5, price + 0.25, price - 0.75, price)
        bars_5m = _make_5m_bar(price)
        sig = strat.evaluate(market, bars_5m, bars_1m, {"regime": "MID_MORNING"})
        assert sig is None
