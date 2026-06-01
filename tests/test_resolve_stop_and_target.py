"""
Tests for tools.phoenix_real_backtest._resolve_stop_and_target.

This helper resolves a strategy Signal into concrete (stop_price,
target_price). The Phase 1.5B fix on 2026-06-01 changed it to refuse
to synthesize a zero-distance target for managed-exit strategies
(target_rr=0.0 + target_price=None) -- previously such Signals were
silently rewritten to target_price = entry_price, causing every trade
to "hit target" on the entry bar with 0 ticks of P&L.

See logs/oracle/research/2026-06-01_noise_area_investigation.md for the
root cause walkthrough.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pytest

from tools.phoenix_real_backtest import _resolve_stop_and_target


@dataclass
class _StubSignal:
    """Minimal Signal-shaped object for the resolver."""
    direction: str
    target_rr: Optional[float] = 0.0
    stop_ticks: Optional[int] = 0
    stop_price: Optional[float] = None
    target_price: Optional[float] = None


# ---------------------------------------------------------------------------
# Happy paths -- strategy provides both stop and target
# ---------------------------------------------------------------------------

class TestPassthrough:
    def test_long_both_provided(self):
        sig = _StubSignal(
            direction="LONG", target_rr=2.0,
            stop_price=100.0, target_price=110.0,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=105.0)
        assert stop == 100.0
        assert tgt == 110.0

    def test_short_both_provided(self):
        sig = _StubSignal(
            direction="SHORT", target_rr=2.0,
            stop_price=110.0, target_price=100.0,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=105.0)
        assert stop == 110.0
        assert tgt == 100.0


# ---------------------------------------------------------------------------
# Stop provided, target_price NOT provided, but target_rr positive
#   -> synthesize target at RR multiple
# ---------------------------------------------------------------------------

class TestSynthesizeTargetFromRR:
    def test_long_rr_two_to_one(self):
        # entry 100, stop 98 -> stop_dist 2. RR 2.0 -> target 104.
        sig = _StubSignal(
            direction="LONG", target_rr=2.0,
            stop_price=98.0, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        assert stop == 98.0
        assert tgt == pytest.approx(104.0)

    def test_short_rr_three_to_one(self):
        # entry 100, stop 102 -> stop_dist 2. RR 3.0 -> target 94.
        sig = _StubSignal(
            direction="SHORT", target_rr=3.0,
            stop_price=102.0, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        assert stop == 102.0
        assert tgt == pytest.approx(94.0)


# ---------------------------------------------------------------------------
# THE FIX: target_rr == 0.0 AND target_price is None
#   -> use +inf / -inf sentinel so target never triggers
# ---------------------------------------------------------------------------

class TestManagedExitNoTarget:
    def test_long_target_rr_zero_returns_positive_inf(self):
        """noise_area pattern: managed exit, no bracket target."""
        sig = _StubSignal(
            direction="LONG", target_rr=0.0,
            stop_price=98.0, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        assert stop == 98.0
        # target must NOT be entry_price (the original bug)
        assert tgt != 100.0
        # specifically: it should be +inf so the simulator's target-hit
        # check (high >= target) never fires
        assert math.isinf(tgt) and tgt > 0

    def test_short_target_rr_zero_returns_negative_inf(self):
        sig = _StubSignal(
            direction="SHORT", target_rr=0.0,
            stop_price=102.0, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        assert stop == 102.0
        # target must NOT be entry_price
        assert tgt != 100.0
        assert math.isinf(tgt) and tgt < 0

    def test_long_target_rr_none_returns_positive_inf(self):
        """Defensive: target_rr explicitly None should behave like 0."""
        sig = _StubSignal(
            direction="LONG", target_rr=None,
            stop_price=98.0, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        assert stop == 98.0
        assert math.isinf(tgt) and tgt > 0

    def test_target_inf_does_not_trigger_via_high_check(self):
        """The whole point of the inf sentinel: bar.high >= inf is always
        False. Confirms the sentinel choice is mathematically sound for
        downstream simulate_trade()."""
        sig = _StubSignal(
            direction="LONG", target_rr=0.0,
            stop_price=98.0, target_price=None,
        )
        _, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        # The simulator does: if row.high >= target_price: break (target hit)
        for synthetic_high in (100.0, 105.0, 1000.0, 1e9, 1e15):
            assert not (synthetic_high >= tgt), (
                f"high={synthetic_high} should NOT trigger target_price={tgt}"
            )


# ---------------------------------------------------------------------------
# Legacy fallback: neither stop_price nor target_price provided
# ---------------------------------------------------------------------------

class TestLegacyFallback:
    def test_long_legacy_with_rr(self):
        # entry 100, stop_ticks 8 -> stop_dist 2 (at $0.25/tick). RR 1.5 -> target 103.
        sig = _StubSignal(
            direction="LONG", target_rr=1.5, stop_ticks=8,
            stop_price=None, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        assert stop == pytest.approx(98.0)
        assert tgt == pytest.approx(103.0)

    def test_long_legacy_zero_rr_returns_inf(self):
        sig = _StubSignal(
            direction="LONG", target_rr=0.0, stop_ticks=8,
            stop_price=None, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        assert stop == pytest.approx(98.0)
        assert math.isinf(tgt) and tgt > 0

    def test_short_legacy_with_rr(self):
        sig = _StubSignal(
            direction="SHORT", target_rr=2.0, stop_ticks=4,
            stop_price=None, target_price=None,
        )
        stop, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        # stop_dist = 4 * 0.25 = 1
        assert stop == pytest.approx(101.0)
        # target = 100 - 1 * 2 = 98
        assert tgt == pytest.approx(98.0)


# ---------------------------------------------------------------------------
# Regression: the original bug behavior. Confirms it would have triggered.
# ---------------------------------------------------------------------------

class TestRegressionOriginalBug:
    def test_old_buggy_behavior_no_longer_occurs(self):
        """If the resolver still produced target_price = entry_price * something
        for the noise_area shape, this test would catch it."""
        sig = _StubSignal(
            direction="LONG", target_rr=0.0,
            stop_price=98.0, target_price=None,
        )
        _, tgt = _resolve_stop_and_target(sig, entry_price=100.0)
        # The old code did: target_price = entry_price + stop_dist * 0 = 100.0
        # That instantly triggered "high >= 100" on the entry bar.
        # New code: target = +inf.
        assert tgt > 100.0 + 1e9, (
            "Resolver must NOT return a finite near-entry target for "
            "managed-exit signals; got %r" % (tgt,)
        )
