"""Lock-in test for Fix A (2026-04-24): ORB ATR-adaptive max_or_size.

Formula must remain: min(max(80pt, ATR×4), 150pt).

If anyone changes the floor from 80pt, the multiplier from 4×, or the
hard cap from 150pt, this test should fail loudly. To intentionally
override, add `# DO_NOT_REGRESS_OVERRIDE` to the relevant config line
AND update this test in the same commit.
"""

from __future__ import annotations

import importlib

from config.strategies import STRATEGIES


def test_orb_config_values_locked():
    cfg = STRATEGIES["orb"]
    # Floor (used when ATR unavailable)
    assert cfg["max_or_size_points"] == 80, "floor must remain 80pt"
    # ATR multiplier
    assert cfg["max_or_size_atr_mult"] == 4.0, "atr multiplier must remain 4.0"
    # Hard cap
    assert cfg["max_or_size_hard_cap_points"] == 150, "hard cap must remain 150pt"
    # Strategy enabled
    assert cfg["enabled"] is True


def test_orb_clamp_low_atr_falls_back_to_floor():
    """When ATR is small, the cap is the 80pt floor."""
    floor = STRATEGIES["orb"]["max_or_size_points"]
    mult = STRATEGIES["orb"]["max_or_size_atr_mult"]
    hard_cap = STRATEGIES["orb"]["max_or_size_hard_cap_points"]
    atr = 10.0  # small
    cap = min(max(floor, atr * mult), hard_cap)
    assert cap == 80.0


def test_orb_clamp_normal_atr_uses_multiplier():
    floor = STRATEGIES["orb"]["max_or_size_points"]
    mult = STRATEGIES["orb"]["max_or_size_atr_mult"]
    hard_cap = STRATEGIES["orb"]["max_or_size_hard_cap_points"]
    atr = 25.0
    cap = min(max(floor, atr * mult), hard_cap)
    assert cap == 100.0   # 25*4 = 100, > floor


def test_orb_clamp_high_atr_clamped_to_hard_cap():
    floor = STRATEGIES["orb"]["max_or_size_points"]
    mult = STRATEGIES["orb"]["max_or_size_atr_mult"]
    hard_cap = STRATEGIES["orb"]["max_or_size_hard_cap_points"]
    atr = 200.0  # absurdly high → would exceed hard cap
    cap = min(max(floor, atr * mult), hard_cap)
    assert cap == 150.0


def test_orb_strategy_uses_adaptive_formula():
    """Verify the live strategy code uses the same formula. We don't
    instantiate the strategy here (would need full base wiring); instead
    we read the source and assert the formula bits are present.
    """
    src_path = importlib.resources.files("strategies").joinpath("orb.py")
    src = src_path.read_text(encoding="utf-8")
    # Required substrings
    assert "max_or_size_atr_mult" in src
    assert "max_or_size_hard_cap_points" in src
    assert "atr_5m * max_or_size_atr_mult" in src or "atr_5m*max_or_size_atr_mult" in src
