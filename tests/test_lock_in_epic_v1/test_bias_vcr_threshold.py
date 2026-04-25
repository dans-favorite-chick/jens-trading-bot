"""Lock-in test for Fix B (2026-04-24): bias_momentum VCR threshold drop.

VCR threshold must remain 1.2 (was 1.5). Close-pos thresholds 0.65/0.35
(was 0.75/0.25). SHORT mirror path remains in the code.
"""

from __future__ import annotations

import importlib

from config.strategies import STRATEGIES


def test_bias_momentum_vcr_threshold():
    cfg = STRATEGIES["bias_momentum"]
    assert cfg["vcr_threshold"] == 1.2
    assert cfg["explosive_close_pos_long"] == 0.65
    assert cfg["explosive_close_pos_short"] == 0.35
    assert cfg["enabled"] is True


def test_bias_momentum_strategy_reads_config_vcr():
    """Verify the strategy file references the new config keys, not the
    old hardcoded 1.5/0.75/0.25 values."""
    src_path = importlib.resources.files("strategies").joinpath("bias_momentum.py")
    src = src_path.read_text(encoding="utf-8")
    # New config keys must be referenced
    assert "vcr_threshold" in src
    assert "explosive_close_pos_long" in src
    assert "explosive_close_pos_short" in src
    # The explosive_long / explosive_short logic must reference the
    # config-driven threshold variable, not a hardcoded 1.5.
    assert "_vcr >= _vcr_threshold" in src
    # And the new close-pos variables are used in the inequalities
    assert "_close_pos_long" in src
    assert "_close_pos_short" in src


def test_bias_momentum_short_mirror_path_present():
    """SHORT mirror was never removed; we just lowered the bypass thresholds.
    Confirm the short branch still exists."""
    src_path = importlib.resources.files("strategies").joinpath("bias_momentum.py")
    src = src_path.read_text(encoding="utf-8")
    assert 'direction = "SHORT"' in src
    assert "explosive_short" in src
    assert "ema_stack_short" in src


def test_bias_momentum_vwap_short_gate_logged():
    """Verify the SHORT VWAP gate path produces a distinct log line so
    graders can detect short-side activity."""
    src_path = importlib.resources.files("strategies").joinpath("bias_momentum.py")
    src = src_path.read_text(encoding="utf-8")
    assert "BLOCKED gate:vwap_short" in src
    assert "BLOCKED gate:vwap_long" in src
