"""Tier-aware sizing in SimpleSizer (#23, 2026-05-13).

Statistical confidence should drive size. A 50-trade strategy gets
1× contracts; a 400-trade validated strategy earns 1.5×; a 700-trade
HIGH_CONFIDENCE strategy can take 2×. The conviction-threshold and
daily-loss gates are unchanged — tier multiplier only kicks in on
the approved path.

Note: small-account base size is 1, so a 1.5× multiplier rounds DOWN
to 1 (int floor). The multiplier is reported separately in the result
so the caller can audit it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.simple_sizing import SimpleSizer, reset_sizer


@pytest.fixture(autouse=True)
def _reset():
    """Per-test isolation — SimpleSizer is a singleton via get_sizer()."""
    reset_sizer()
    yield
    reset_sizer()


def _approved_sizer(base_contracts: int = 1, score: int = 85) -> SimpleSizer:
    """A sizer pre-loaded with a known base contract count."""
    s = SimpleSizer()
    s.config["contracts_per_trade"] = base_contracts
    s.config["veto_low_conviction_threshold"] = 50  # always pass
    return s


# ── Multiplier lookup ──────────────────────────────────────────────────

def test_multiplier_unknown_or_none_is_1x():
    assert SimpleSizer._tier_multiplier(None) == 1.0
    assert SimpleSizer._tier_multiplier("") == 1.0
    assert SimpleSizer._tier_multiplier("UNKNOWN_TIER") == 1.0


def test_multiplier_each_tier_value():
    assert SimpleSizer._tier_multiplier("INSUFFICIENT_SAMPLE") == 1.0
    assert SimpleSizer._tier_multiplier("PRELIMINARY") == 1.0
    assert SimpleSizer._tier_multiplier("TENTATIVE") == 1.0
    assert SimpleSizer._tier_multiplier("VALIDATED") == 1.5
    assert SimpleSizer._tier_multiplier("HIGH_CONFIDENCE") == 2.0


def test_multiplier_case_insensitive():
    """Avoid accidental case-mismatch bugs at the caller boundary."""
    assert SimpleSizer._tier_multiplier("validated") == 1.5
    assert SimpleSizer._tier_multiplier("Validated") == 1.5


# ── Behavior at base=1 (small account) ─────────────────────────────────

def test_base_1_validated_rounds_down_to_1():
    """Small-account base=1 contract; even 1.5× still yields 1 (floor)."""
    s = _approved_sizer(base_contracts=1)
    out = s.size_trade(signal_score=85, strategy_tier="VALIDATED")
    assert out["take_trade"] is True
    assert out["contracts"] == 1
    assert out["tier_multiplier"] == 1.5


def test_base_1_high_confidence_yields_2():
    s = _approved_sizer(base_contracts=1)
    out = s.size_trade(signal_score=85, strategy_tier="HIGH_CONFIDENCE")
    assert out["contracts"] == 2
    assert out["tier_multiplier"] == 2.0


# ── Behavior at base=2 (full account) ──────────────────────────────────

def test_base_2_tentative_stays_at_2():
    s = _approved_sizer(base_contracts=2)
    out = s.size_trade(signal_score=85, strategy_tier="TENTATIVE")
    assert out["contracts"] == 2


def test_base_2_validated_scales_to_3():
    """2 * 1.5 = 3."""
    s = _approved_sizer(base_contracts=2)
    out = s.size_trade(signal_score=85, strategy_tier="VALIDATED")
    assert out["contracts"] == 3


def test_base_2_high_confidence_scales_to_4():
    s = _approved_sizer(base_contracts=2)
    out = s.size_trade(signal_score=85, strategy_tier="HIGH_CONFIDENCE")
    assert out["contracts"] == 4


# ── Max-loss budget scales with contracts ──────────────────────────────

def test_max_loss_dollars_scales_with_contracts():
    """If we're risking 2 contracts, the max-loss dollar budget is 2x
    the single-contract budget. Otherwise the caller would size up but
    still gate on a single-contract risk number."""
    s = _approved_sizer(base_contracts=1)
    s.config["max_loss_per_trade_usd"] = 5.0
    out = s.size_trade(signal_score=85, strategy_tier="HIGH_CONFIDENCE")
    assert out["contracts"] == 2
    assert out["max_loss_dollars"] == 10.0  # 2 * 5


# ── Default (no tier specified) preserves legacy behavior ─────────────

def test_no_tier_arg_preserves_legacy_size():
    """Existing call sites passing only positional args must keep working
    with their original behavior — 1.0x multiplier."""
    s = _approved_sizer(base_contracts=1)
    out = s.size_trade(signal_score=85)  # no strategy_tier arg
    assert out["contracts"] == 1
    assert out["tier_multiplier"] == 1.0


# ── Tier doesn't bypass gates ──────────────────────────────────────────

def test_low_conviction_rejected_regardless_of_tier():
    """Even HIGH_CONFIDENCE tier can't bypass the veto_low_conviction
    threshold. Tier is the SIZE knob, not a quality bypass."""
    s = SimpleSizer()
    s.config["veto_low_conviction_threshold"] = 80
    out = s.size_trade(signal_score=70, strategy_tier="HIGH_CONFIDENCE")
    assert out["take_trade"] is False
    assert out["contracts"] == 0
