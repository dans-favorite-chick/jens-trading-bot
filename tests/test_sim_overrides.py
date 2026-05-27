"""Tests for the sim_overrides opt-in channel (P0-2, F-08, F-09).

Covers:
- env unset → no-op, status = inactive
- env "0" → no-op, status = inactive
- env "1" + empty overrides → active with zero counts
- env "1" + settings override → applies + logs CRITICAL
- env "1" + strategy override → applies + logs CRITICAL
- env "1" + unknown setting key → warning, skip
- env "1" + unknown strategy → warning, skip
- env "1" + LIVE_TRADING=True → raises SimOverrideLiveConflict
"""
from __future__ import annotations

import importlib
import logging

import pytest

from core import sim_overrides_loader as loader


@pytest.fixture(autouse=True)
def _reset_environment(monkeypatch):
    """Each test starts with no PHOENIX_SIM_OVERRIDES and LIVE_TRADING=False."""
    monkeypatch.delenv("PHOENIX_SIM_OVERRIDES", raising=False)
    from config import settings
    monkeypatch.setattr(settings, "LIVE_TRADING", False, raising=False)
    yield


@pytest.fixture
def _reload_sim_overrides_module():
    """Force-reload config/sim_overrides.py between tests so SETTINGS_OVERRIDES /
    STRATEGY_OVERRIDES patches don't leak across tests."""
    import config.sim_overrides as _so
    importlib.reload(_so)
    yield _so
    importlib.reload(_so)


def test_env_unset_is_no_op(caplog):
    caplog.set_level(logging.INFO, logger="SIM_OVERRIDES")
    status = loader.load_and_apply_sim_overrides()
    assert status == {
        "active": False,
        "settings_count": 0,
        "strategies_count": 0,
        "applied_settings": [],
        "applied_strategies": [],
    }
    assert any("sim_overrides: none" in r.message for r in caplog.records)


def test_env_zero_is_no_op(monkeypatch, caplog):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "0")
    caplog.set_level(logging.INFO, logger="SIM_OVERRIDES")
    status = loader.load_and_apply_sim_overrides()
    assert status["active"] is False


def test_env_one_with_empty_overrides_active(monkeypatch, _reload_sim_overrides_module, caplog):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "1")
    caplog.set_level(logging.INFO, logger="SIM_OVERRIDES")
    status = loader.load_and_apply_sim_overrides()
    assert status["active"] is True
    assert status["settings_count"] == 0
    assert status["strategies_count"] == 0


def test_env_one_with_live_trading_raises(monkeypatch):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "1")
    from config import settings
    monkeypatch.setattr(settings, "LIVE_TRADING", True, raising=False)
    with pytest.raises(loader.SimOverrideLiveConflict):
        loader.load_and_apply_sim_overrides()


def test_settings_override_applied(monkeypatch, _reload_sim_overrides_module, caplog):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "1")
    caplog.set_level(logging.CRITICAL, logger="SIM_OVERRIDES")
    from config import settings, sim_overrides
    original = settings.DAILY_LOSS_LIMIT
    sim_overrides.SETTINGS_OVERRIDES = {"DAILY_LOSS_LIMIT": 9_999_999}
    try:
        status = loader.load_and_apply_sim_overrides()
        assert status["settings_count"] == 1
        assert "DAILY_LOSS_LIMIT" in status["applied_settings"]
        assert settings.DAILY_LOSS_LIMIT == 9_999_999
        assert any("DAILY_LOSS_LIMIT" in r.message for r in caplog.records)
    finally:
        # Restore baseline so other tests in the suite aren't polluted.
        settings.DAILY_LOSS_LIMIT = original
        sim_overrides.SETTINGS_OVERRIDES = {}


def test_unknown_settings_key_skipped(monkeypatch, _reload_sim_overrides_module, caplog):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "1")
    caplog.set_level(logging.WARNING, logger="SIM_OVERRIDES")
    from config import sim_overrides
    sim_overrides.SETTINGS_OVERRIDES = {"THIS_SETTING_DOES_NOT_EXIST": 42}
    try:
        status = loader.load_and_apply_sim_overrides()
        assert status["settings_count"] == 0
        assert any("does not exist" in r.message for r in caplog.records)
    finally:
        sim_overrides.SETTINGS_OVERRIDES = {}


def test_strategy_override_applied(monkeypatch, _reload_sim_overrides_module, caplog):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "1")
    caplog.set_level(logging.CRITICAL, logger="SIM_OVERRIDES")
    from config import strategies, sim_overrides
    # Pick the first real strategy for the test
    real_strategy = next(iter(strategies.STRATEGIES))
    original = strategies.STRATEGIES[real_strategy].get("validated")
    sim_overrides.STRATEGY_OVERRIDES = {real_strategy: {"validated": "TEST_SENTINEL"}}
    try:
        status = loader.load_and_apply_sim_overrides()
        assert status["strategies_count"] == 1
        assert real_strategy in status["applied_strategies"]
        assert strategies.STRATEGIES[real_strategy]["validated"] == "TEST_SENTINEL"
    finally:
        strategies.STRATEGIES[real_strategy]["validated"] = original
        sim_overrides.STRATEGY_OVERRIDES = {}


def test_unknown_strategy_skipped(monkeypatch, _reload_sim_overrides_module, caplog):
    monkeypatch.setenv("PHOENIX_SIM_OVERRIDES", "1")
    caplog.set_level(logging.WARNING, logger="SIM_OVERRIDES")
    from config import sim_overrides
    sim_overrides.STRATEGY_OVERRIDES = {"definitely_not_a_real_strategy": {"foo": "bar"}}
    try:
        status = loader.load_and_apply_sim_overrides()
        assert status["strategies_count"] == 0
        assert any("not in STRATEGIES" in r.message for r in caplog.records)
    finally:
        sim_overrides.STRATEGY_OVERRIDES = {}
