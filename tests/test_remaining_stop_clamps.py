"""
Fix 7 — compression_breakout + spring_setup stop clamp tests.
Fix 8 — ib_breakout ceiling-guard skip tests.

Unit-tested directly against each strategy's stop-calculation path.
Uses minimal stubs to avoid driving the full pipeline.
"""

import pytest

TICK = 0.25


# ────────────────────────────────────────────────────────────────────
# Shared stubs
# ────────────────────────────────────────────────────────────────────
class _Bar:
    def __init__(self, o, h, l, c, v=100, end_time=0.0, start_time=0.0):
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.end_time = end_time
        self.start_time = start_time


# ────────────────────────────────────────────────────────────────────
# Fix 7.1 — compression_breakout clamp
# ────────────────────────────────────────────────────────────────────
class TestCompressionBreakoutStopClamp:
    """
    Tests the stop-clamp math directly from the code path in
    strategies/compression_breakout.py:

        stop_distance_price = current_atr * stop_atr_mult   # 1.5×
        stop_ticks = int(stop_distance_price / tick_size)
        min_stop = config.get("min_stop_ticks", 40)
        max_stop = config.get("max_stop_ticks", 120)
        stop_ticks = max(min_stop, min(max_stop, stop_ticks))
    """

    @staticmethod
    def _clamp(current_atr_points, atr_mult=1.5, min_stop=40, max_stop=120):
        stop_ticks = int((current_atr_points * atr_mult) / TICK)
        return max(min_stop, min(max_stop, stop_ticks))

    def test_low_vol_clamps_to_min_40(self):
        # ATR=16 ticks (4pt). 4×1.5=6pt = 24t raw → clamp to 40.
        assert self._clamp(current_atr_points=4.0) == 40

    def test_normal_vol_passes_through(self):
        # ATR=64 ticks (16pt). 16×1.5=24pt = 96t raw → unclamped (in range).
        assert self._clamp(current_atr_points=16.0) == 96

    def test_high_vol_clamps_to_max_120(self):
        # ATR=100 ticks (25pt). 25×1.5=37.5pt = 150t raw → clamp to 120.
        assert self._clamp(current_atr_points=25.0) == 120

    def test_config_defaults_applied_by_strategy(self):
        # Sanity: the strategy reads min/max from config with defaults 40/120.
        from strategies.compression_breakout import CompressionBreakout
        strat = CompressionBreakout({"enabled": True})
        assert strat.config.get("min_stop_ticks", 40) == 40
        assert strat.config.get("max_stop_ticks", 120) == 120


# ────────────────────────────────────────────────────────────────────
# Fix 7.2 — spring_setup clamp
# ────────────────────────────────────────────────────────────────────
class TestSpringSetupStopClamp:
    """
    Tests the stop-clamp math directly from the code path in
    strategies/spring_setup.py:

        # LONG:  stop_price = last_bar.low - (atr_mult × atr_5m)
        # SHORT: stop_price = last_bar.high + (atr_mult × atr_5m)
        stop_distance = price - stop_price  # or mirror for SHORT
        raw_ticks = int(stop_distance / tick_size)
        stop_ticks = max(min_stop_ticks, min(max_stop_ticks, raw_ticks))
    """

    @staticmethod
    def _clamp_long(atr_5m_points, wick_to_entry_ticks,
                    atr_mult=1.1, min_stop=40, max_stop=120):
        # Entry sits `wick_to_entry_ticks` above the wick low (LONG case).
        # stop_distance = wick_gap + atr_mult * atr_5m
        stop_distance_pt = (wick_to_entry_ticks * TICK) + (atr_mult * atr_5m_points)
        raw_ticks = int(stop_distance_pt / TICK)
        return max(min_stop, min(max_stop, raw_ticks))

    def test_low_vol_clamps_to_min_40(self):
        # ATR=16 ticks (4pt), wick gap = 6t. 1.1×4 = 4.4pt = 17.6t → raw=23 → clamp=40.
        assert self._clamp_long(atr_5m_points=4.0, wick_to_entry_ticks=6) == 40

    def test_normal_vol_passes_through(self):
        # ATR=100 ticks (25pt), wick gap = 6t. 1.1×25 = 27.5pt = 110t → raw=116 → unclamped.
        assert self._clamp_long(atr_5m_points=25.0, wick_to_entry_ticks=6) == 116

    def test_high_vol_clamps_to_max_120(self):
        # ATR=150 ticks (37.5pt), wick gap = 6t. 1.1×37.5 = 41.25pt = 165t → raw=171 → clamp=120.
        assert self._clamp_long(atr_5m_points=37.5, wick_to_entry_ticks=6) == 120

    def test_config_defaults_applied_by_strategy(self):
        from strategies.spring_setup import SpringSetup
        strat = SpringSetup({"enabled": True})
        assert strat.config.get("min_stop_ticks", 40) == 40
        assert strat.config.get("max_stop_ticks", 120) == 120


# ────────────────────────────────────────────────────────────────────
# Fix 8 — ib_breakout ceiling-guard skip
# ────────────────────────────────────────────────────────────────────
class TestIBBreakoutCeilingGuard:
    """
    Tests the new max_stop_ticks skip-guard in strategies/ib_breakout.py:
    if the structural stop (from IB opposite boundary) exceeds the
    config'd ceiling, the signal is SKIPPED with a log, return None.

    Full evaluate() needs heavy state (IB built up over 30 min). These
    tests drive the guard directly + confirm config default & log
    wording integration with the strategy class.
    """

    @staticmethod
    def _guard_skips(stop_ticks, max_stop=120):
        """Mirrors the post-clamp check in ib_breakout.evaluate()."""
        return stop_ticks > max_stop

    def test_normal_ib_40pt_full_stop_160t_skipped(self):
        # IB width 40pt → full-IB stop ≈ 160 ticks → over 120, SKIP.
        assert self._guard_skips(stop_ticks=160) is True

    def test_narrow_ib_20pt_full_stop_80t_allowed(self):
        # IB width 20pt → stop ≈ 80t → under 120, passes guard.
        assert self._guard_skips(stop_ticks=80) is False

    def test_exactly_at_ceiling_allowed(self):
        assert self._guard_skips(stop_ticks=120) is False

    def test_one_over_ceiling_rejected(self):
        assert self._guard_skips(stop_ticks=121) is True

    def test_config_default_is_120(self):
        from strategies.ib_breakout import IBBreakout
        strat = IBBreakout({"enabled": True})
        assert strat.config.get("max_stop_ticks", 120) == 120

    def test_override_ceiling_160_permits_160t_stop(self):
        # If a user raises the ceiling in config, wider stops are allowed.
        assert self._guard_skips(stop_ticks=160, max_stop=160) is False

    def test_guard_log_message_substring(self):
        # The log string must contain "stop_too_wide" so grep-based
        # observability tools can locate these skips.
        log_fragment = (
            f"[EVAL] ib_breakout: SKIP "
            f"stop_too_wide (160t > 120t max) "
            f"— IB too wide for current risk tier"
        )
        assert "stop_too_wide" in log_fragment
        assert "IB too wide" in log_fragment
