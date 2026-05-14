"""Regime gating matrix loader (#7, 2026-05-13).

Tests the YAML loader + typed accessors. Doesn't run with the real
memory/procedural/regime_matrix.yaml — uses tmp_path fixtures so the
operator-editable file doesn't drift these tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.regime_matrix import (
    KNOWN_REGIMES,
    RegimeMatrix,
    StrategyState,
    load_matrix,
    _parse_state,
)


# ── _parse_state ───────────────────────────────────────────────────────

def test_parse_known_strings():
    assert _parse_state("ON") == StrategyState.ON
    assert _parse_state("on") == StrategyState.ON
    assert _parse_state("  on  ") == StrategyState.ON
    assert _parse_state("REDUCED") == StrategyState.REDUCED
    assert _parse_state("OFF") == StrategyState.OFF


def test_parse_unknown_string_defaults_to_reduced():
    """Don't enable a strategy on a typo — conservative fallback."""
    assert _parse_state("MAYBE") == StrategyState.REDUCED
    assert _parse_state("") == StrategyState.REDUCED


def test_parse_non_string_defaults_to_reduced():
    assert _parse_state(None) == StrategyState.REDUCED
    assert _parse_state(123) == StrategyState.REDUCED


def test_parse_bool_handles_yaml_11_on_off_quirk():
    """PyYAML 1.1 parses unquoted ON/OFF as booleans, not strings.
    The real regime_matrix.yaml uses unquoted ON/OFF — must work."""
    assert _parse_state(True) == StrategyState.ON
    assert _parse_state(False) == StrategyState.OFF


# ── load_matrix happy path ─────────────────────────────────────────────

def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_load_matrix_parses_strategy_matrix_block(tmp_path):
    p = tmp_path / "regime.yaml"
    _write_yaml(p, """
strategy_matrix:
  vwap_pullback:
    POS_GEX_LOW_VIX: ON
    NEG_GEX_HIGH_VIX: OFF
    UNKNOWN: REDUCED
  bias_momentum:
    POS_GEX_LOW_VIX: REDUCED
    NEG_GEX_HIGH_VIX: ON
""")
    m = load_matrix(p)
    assert m.state("vwap_pullback", "POS_GEX_LOW_VIX") == StrategyState.ON
    assert m.state("vwap_pullback", "NEG_GEX_HIGH_VIX") == StrategyState.OFF
    assert m.state("bias_momentum", "NEG_GEX_HIGH_VIX") == StrategyState.ON


def test_load_matrix_accepts_top_level_dict_without_wrapper(tmp_path):
    """If the YAML is just `strat: { regime: state }` without the
    `strategy_matrix:` wrapper, the loader should still work."""
    p = tmp_path / "regime.yaml"
    _write_yaml(p, """
vwap_pullback:
  POS_GEX_LOW_VIX: ON
""")
    m = load_matrix(p)
    assert m.state("vwap_pullback", "POS_GEX_LOW_VIX") == StrategyState.ON


# ── load_matrix failure modes ──────────────────────────────────────────

def test_missing_file_returns_empty_matrix(tmp_path):
    m = load_matrix(tmp_path / "does_not_exist.yaml")
    assert m.by_strategy == {}


def test_corrupted_yaml_returns_empty_matrix(tmp_path):
    p = tmp_path / "bad.yaml"
    _write_yaml(p, "{ this: is not [ valid yaml")
    m = load_matrix(p)
    assert m.by_strategy == {}


def test_malformed_top_level_returns_empty(tmp_path):
    """YAML that's just a string or list — not a dict — must not crash."""
    p = tmp_path / "scalar.yaml"
    _write_yaml(p, "just_a_string")
    m = load_matrix(p)
    assert m.by_strategy == {}


# ── Default-state semantics ────────────────────────────────────────────

def test_unknown_strategy_defaults_to_on():
    """A new strategy not yet listed in the matrix shouldn't be silently
    disabled — operator gets to opt in to gating per-strategy."""
    m = RegimeMatrix(by_strategy={})
    assert m.state("new_strategy", "POS_GEX_LOW_VIX") == StrategyState.ON
    assert m.is_active("new_strategy", "POS_GEX_LOW_VIX") is True


def test_unknown_regime_defaults_to_reduced():
    """A regime not in the matrix is treated conservatively (slow it
    down) until the operator updates the matrix."""
    m = RegimeMatrix(by_strategy={"x": {"POS_GEX_LOW_VIX": StrategyState.ON}})
    assert m.state("x", "NEW_REGIME") == StrategyState.REDUCED


# ── Convenience predicates ─────────────────────────────────────────────

def test_is_active_false_when_off():
    m = RegimeMatrix(by_strategy={"x": {"r": StrategyState.OFF}})
    assert m.is_active("x", "r") is False


def test_requires_higher_score_only_when_reduced():
    m = RegimeMatrix(by_strategy={"x": {
        "r1": StrategyState.ON,
        "r2": StrategyState.REDUCED,
        "r3": StrategyState.OFF,
    }})
    assert m.requires_higher_score("x", "r1") is False
    assert m.requires_higher_score("x", "r2") is True
    assert m.requires_higher_score("x", "r3") is False


# ── Real-file smoke check ──────────────────────────────────────────────

def test_real_yaml_loads_without_error():
    """Sanity: the operator-editable YAML at memory/procedural/ must
    parse without errors. If this breaks, the matrix is malformed and
    the bot will silently fall back to empty (= no gating) in prod."""
    from config.regime_matrix import _DEFAULT_PATH
    if not _DEFAULT_PATH.exists():
        pytest.skip("regime_matrix.yaml not present in this env")
    m = load_matrix()
    # Spot-check: vwap_pullback is in the file and has a POS_GEX_LOW_VIX state
    assert "vwap_pullback" in m.by_strategy
    assert m.state("vwap_pullback", "POS_GEX_LOW_VIX") in {
        StrategyState.ON, StrategyState.REDUCED, StrategyState.OFF,
    }


def test_known_regimes_constant_is_complete():
    """The constant should list every regime referenced in the real YAML
    so callers can fail loudly on regime-name typos."""
    expected = {
        "POS_GEX_LOW_VIX", "POS_GEX_HIGH_VIX",
        "NEG_GEX_LOW_VIX", "NEG_GEX_HIGH_VIX",
        "UNKNOWN",
    }
    assert KNOWN_REGIMES == expected
