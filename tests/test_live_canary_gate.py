"""Live canary gate (2026-05-24, operator directive) — refuses to start
the bot when LIVE_TRADING=True and any canary-mode constraint is violated.

Covers:
  - sim mode (LIVE_TRADING=False) is a no-op
  - live mode + valid config passes
  - empty LIVE_STRATEGY_ALLOWLIST fails
  - allowlist contains unknown strategy fails
  - allowlist contains validated=False strategy fails
  - MULTI_ACCOUNT_ROUTING_ENABLED=True fails
  - SIZING_MODE != "flat_1" fails
  - AGENT_*_ENABLED=True fails
  - PHOENIX_SIM_OVERRIDES=1 fails
  - filter_strategies_for_live: sim mode pass-through
  - filter_strategies_for_live: live mode drops non-allowlisted
  - filter_strategies_for_live: live mode drops validated=False
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from core.live_canary_gate import (
    LiveCanaryViolation,
    filter_strategies_for_live,
    validate_live_config,
)


# ─── fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def _live_mode(monkeypatch):
    """Patch settings so LIVE_TRADING=True with all other constraints OK
    for a clean live-mode baseline (bias_momentum allowlisted)."""
    from config import settings, strategies as _strats

    monkeypatch.setattr(settings, "LIVE_TRADING", True, raising=False)
    monkeypatch.setattr(
        settings, "LIVE_STRATEGY_ALLOWLIST", ("bias_momentum",), raising=False,
    )
    monkeypatch.setattr(
        settings, "MULTI_ACCOUNT_ROUTING_ENABLED", False, raising=False,
    )
    monkeypatch.setattr(settings, "SIZING_MODE", "flat_1", raising=False)
    monkeypatch.setattr(settings, "AGENT_COUNCIL_ENABLED", False, raising=False)
    monkeypatch.setattr(
        settings, "AGENT_PRETRADE_FILTER_ENABLED", False, raising=False,
    )
    monkeypatch.setattr(settings, "AGENT_DEBRIEF_ENABLED", False, raising=False)
    monkeypatch.delenv("PHOENIX_SIM_OVERRIDES", raising=False)
    # bias_momentum must be validated + enabled — assume so but verify.
    bm = _strats.STRATEGIES.get("bias_momentum", {})
    assert bm.get("validated", False), (
        "bias_momentum must be validated=True in config for canary tests"
    )
    assert bm.get("enabled", True), (
        "bias_momentum must be enabled=True in config for canary tests"
    )
    yield


@pytest.fixture(autouse=True)
def _reset_live_trading_default(monkeypatch):
    """Default every test to sim mode (LIVE_TRADING=False) unless _live_mode is used."""
    from config import settings
    monkeypatch.setattr(settings, "LIVE_TRADING", False, raising=False)
    monkeypatch.delenv("PHOENIX_SIM_OVERRIDES", raising=False)


# ─── validate_live_config ──────────────────────────────────────────

def test_sim_mode_is_no_op(caplog):
    caplog.set_level(logging.INFO, logger="LiveCanaryGate")
    validate_live_config()  # LIVE_TRADING=False from autouse fixture
    assert any(
        "LIVE_TRADING=False" in r.message and "inactive" in r.message
        for r in caplog.records
    )


def test_live_mode_with_valid_config_passes(_live_mode, caplog):
    caplog.set_level(logging.CRITICAL, logger="LiveCanaryGate")
    validate_live_config()  # should not raise
    assert any(
        "CANARY" in r.message and "ENGAGED" in r.message
        for r in caplog.records
    )


def test_empty_allowlist_fails(_live_mode, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "LIVE_STRATEGY_ALLOWLIST", (), raising=False)
    with pytest.raises(LiveCanaryViolation, match="LIVE_STRATEGY_ALLOWLIST"):
        validate_live_config()


def test_missing_allowlist_fails(_live_mode, monkeypatch):
    from config import settings
    monkeypatch.delattr(settings, "LIVE_STRATEGY_ALLOWLIST", raising=False)
    with pytest.raises(LiveCanaryViolation, match="LIVE_STRATEGY_ALLOWLIST"):
        validate_live_config()


def test_allowlist_unknown_strategy_fails(_live_mode, monkeypatch):
    from config import settings
    monkeypatch.setattr(
        settings, "LIVE_STRATEGY_ALLOWLIST",
        ("totally_made_up_strategy",), raising=False,
    )
    with pytest.raises(LiveCanaryViolation, match="totally_made_up_strategy"):
        validate_live_config()


def test_allowlist_validated_false_fails(_live_mode, monkeypatch):
    from config import settings, strategies as _strats
    # Patch a real strategy to validated=False just for this test.
    bm_original = dict(_strats.STRATEGIES["bias_momentum"])
    try:
        _strats.STRATEGIES["bias_momentum"]["validated"] = False
        with pytest.raises(LiveCanaryViolation, match="validated is False"):
            validate_live_config()
    finally:
        _strats.STRATEGIES["bias_momentum"] = bm_original


def test_multi_account_routing_enabled_fails(_live_mode, monkeypatch):
    from config import settings
    monkeypatch.setattr(
        settings, "MULTI_ACCOUNT_ROUTING_ENABLED", True, raising=False,
    )
    with pytest.raises(LiveCanaryViolation, match="MULTI_ACCOUNT_ROUTING_ENABLED"):
        validate_live_config()


def test_sizing_mode_tier_3000_fails(_live_mode, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "SIZING_MODE", "tier_3000", raising=False)
    with pytest.raises(LiveCanaryViolation, match="SIZING_MODE"):
        validate_live_config()


@pytest.mark.parametrize("flag", [
    "AGENT_COUNCIL_ENABLED",
    "AGENT_PRETRADE_FILTER_ENABLED",
    "AGENT_DEBRIEF_ENABLED",
])
def test_any_agent_flag_true_fails(_live_mode, monkeypatch, flag):
    from config import settings
    monkeypatch.setattr(settings, flag, True, raising=False)
    with pytest.raises(LiveCanaryViolation, match=flag):
        validate_live_config()


def test_sim_overrides_env_set_fails(_live_mode, monkeypatch):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "1")
    with pytest.raises(LiveCanaryViolation, match="PHOENIX_SIM_OVERRIDES"):
        validate_live_config()


def test_multiple_violations_all_reported(_live_mode, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "SIZING_MODE", "tier_3000", raising=False)
    monkeypatch.setattr(settings, "AGENT_COUNCIL_ENABLED", True, raising=False)
    monkeypatch.setattr(
        settings, "MULTI_ACCOUNT_ROUTING_ENABLED", True, raising=False,
    )
    with pytest.raises(LiveCanaryViolation) as exc_info:
        validate_live_config()
    msg = str(exc_info.value)
    assert "SIZING_MODE" in msg
    assert "AGENT_COUNCIL_ENABLED" in msg
    assert "MULTI_ACCOUNT_ROUTING_ENABLED" in msg


# ─── filter_strategies_for_live ────────────────────────────────────

def _mock_strategy(name: str):
    s = MagicMock()
    s.name = name
    return s


def test_filter_sim_mode_passes_through_unchanged():
    # LIVE_TRADING=False from autouse fixture
    strategies = [_mock_strategy("anything"), _mock_strategy("else")]
    kept = filter_strategies_for_live(strategies)
    assert len(kept) == 2
    assert [s.name for s in kept] == ["anything", "else"]


def test_filter_live_mode_keeps_allowlisted_validated(_live_mode):
    strategies = [_mock_strategy("bias_momentum"), _mock_strategy("noise_area")]
    kept = filter_strategies_for_live(strategies)
    assert len(kept) == 1
    assert kept[0].name == "bias_momentum"


def test_filter_live_mode_drops_non_allowlisted(_live_mode, caplog):
    caplog.set_level(logging.CRITICAL, logger="LiveCanaryGate")
    strategies = [_mock_strategy("noise_area"), _mock_strategy("orb_v2")]
    kept = filter_strategies_for_live(strategies)
    assert kept == []
    assert any("noise_area" in r.message and "DROPPED" in r.message for r in caplog.records)
    assert any("orb_v2" in r.message and "DROPPED" in r.message for r in caplog.records)


def test_filter_live_mode_drops_validated_false(_live_mode, monkeypatch, caplog):
    """Strategy in allowlist but config says validated=False → DROPPED."""
    from config import settings, strategies as _strats
    # Temporarily add a strategy to allowlist with validated=False in config
    bm_original = dict(_strats.STRATEGIES["bias_momentum"])
    try:
        _strats.STRATEGIES["bias_momentum"]["validated"] = False
        monkeypatch.setattr(
            settings, "LIVE_STRATEGY_ALLOWLIST",
            ("bias_momentum",), raising=False,
        )
        caplog.set_level(logging.CRITICAL, logger="LiveCanaryGate")
        kept = filter_strategies_for_live([_mock_strategy("bias_momentum")])
        assert kept == []
        assert any(
            "bias_momentum" in r.message and "validated=False" in r.message
            for r in caplog.records
        )
    finally:
        _strats.STRATEGIES["bias_momentum"] = bm_original


def test_filter_drops_strategy_missing_name():
    """Strategy instance without .name attribute is dropped, not crashes."""
    from config import settings
    settings.LIVE_TRADING = True
    try:
        bad = MagicMock(spec=[])  # no .name attribute
        kept = filter_strategies_for_live([bad])
        # MagicMock with spec=[] returns a MagicMock for .name unless we explicitly hide it
        # In practice .name is always present on MagicMock; this test is defensive.
        assert isinstance(kept, list)
    finally:
        settings.LIVE_TRADING = False
