"""Wilson-CI promotion guardrail (#22, 2026-05-13).

A strategy can only carry `validated=True` once it has reached TENTATIVE
(n>=100). Otherwise we're promoting on noise — the live-record case
was `ib_breakout` with 8 trades / 75% WR / 95% CI 41-93%.

These tests pin:
1. The eligibility function flags premature promotions.
2. The function ignores retired strategies (they can't promote anyway).
3. The current STRATEGIES dict is clean (regression guard).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.validation_tracker import check_promotion_eligibility, _TENTATIVE_N


def _trades(per_strategy: dict[str, int]) -> list[dict]:
    """Build a fake trades list with N records per strategy."""
    out = []
    for name, n in per_strategy.items():
        out.extend([{"strategy": name}] * n)
    return out


def test_validated_below_tentative_is_flagged():
    trades = _trades({"foo": 8})  # below TENTATIVE
    cfg = {"foo": {"validated": True, "enabled": True}}
    v = check_promotion_eligibility(trades, cfg)
    assert len(v) == 1
    name, n, reason = v[0]
    assert name == "foo"
    assert n == 8
    assert "below TENTATIVE" in reason


def test_validated_at_tentative_threshold_passes():
    """Exactly n=100 should be OK — TENTATIVE is inclusive."""
    trades = _trades({"foo": _TENTATIVE_N})
    cfg = {"foo": {"validated": True}}
    assert check_promotion_eligibility(trades, cfg) == []


def test_validated_well_above_tentative_passes():
    trades = _trades({"foo": 300})
    cfg = {"foo": {"validated": True}}
    assert check_promotion_eligibility(trades, cfg) == []


def test_unvalidated_strategy_skipped_regardless_of_count():
    """A lab strategy below TENTATIVE is fine — only validated=True
    triggers the check."""
    trades = _trades({"foo": 5})
    cfg = {"foo": {"validated": False}}
    assert check_promotion_eligibility(trades, cfg) == []


def test_retired_strategy_skipped_even_when_validated():
    """A retired strategy can't promote into prod regardless of the
    validated flag — the load loop also gates on enabled=True.
    Guardrail should not noisily complain about it."""
    trades = _trades({"foo": 50})
    cfg = {"foo": {"validated": True, "retired": True}}
    assert check_promotion_eligibility(trades, cfg) == []


def test_no_trades_at_all_is_flagged_as_zero():
    """A validated=True strategy with literally zero trades is the
    worst-case promotion-on-vibes — should flag clearly."""
    cfg = {"foo": {"validated": True}}
    v = check_promotion_eligibility([], cfg)
    assert len(v) == 1
    assert v[0][1] == 0


def test_multiple_violators_returned_in_order():
    trades = _trades({"a": 5, "b": 10})
    cfg = {
        "a": {"validated": True},
        "b": {"validated": True},
        "c": {"validated": False},  # excluded
    }
    v = check_promotion_eligibility(trades, cfg)
    names = sorted(name for name, _, _ in v)
    assert names == ["a", "b"]


# ── Regression guard: current config must be clean ─────────────────────

def test_current_strategies_pass_guardrail_after_2026_05_13_demotion():
    """After 2026-05-13's demotion of ib_breakout, every validated=True
    strategy in STRATEGIES must either be retired or backed by trade
    data this test can verify is plausibly >=TENTATIVE-eligible.

    We don't load the actual trade history here (CI may run in a clean
    env), but we DO assert that any non-retired validated=True strategy
    is in the documented set the team has manually OK'd.
    """
    from config.strategies import STRATEGIES
    OK_VALIDATED = {
        # n=292, TENTATIVE — bias_momentum is the long-baseline strategy
        "bias_momentum",
        # n=235, TENTATIVE — spring_setup is enabled=False but stays
        # validated=True so flipping enabled to True doesn't require a
        # re-promotion vote.
        "spring_setup",
    }
    actually_validated = {
        n for n, c in STRATEGIES.items()
        if c.get("validated", False) and not c.get("retired", False)
    }
    assert actually_validated == OK_VALIDATED, (
        f"validated-set drift! Expected {OK_VALIDATED}, got "
        f"{actually_validated}. Update OK_VALIDATED if a new promotion "
        f"is intentional and passes tools/validation_tracker.py "
        f"--check-promotion. Otherwise demote until n>=100."
    )
