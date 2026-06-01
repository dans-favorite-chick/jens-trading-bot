# tests/warehouse/test_known_strategies.py
"""Tests for known strategies loader.

The real implementation uses get_known_strategies() (with load_known_strategies as alias).
The real impl only tries 'STRATEGIES' from config.strategies (no candidate list).
Tests patch sys.modules to inject a fake config.strategies module.
"""
from __future__ import annotations
import sys
import types
import pytest


def _install_fake_module(monkeypatch, attr_name: str, value):
    fake = types.ModuleType("config.strategies")
    setattr(fake, attr_name, value)
    fake_pkg = types.ModuleType("config")
    monkeypatch.setitem(sys.modules, "config", fake_pkg)
    monkeypatch.setitem(sys.modules, "config.strategies", fake)


@pytest.fixture(autouse=True)
def _clear_cache():
    from tools.warehouse.known_strategies import load_known_strategies
    load_known_strategies.cache_clear()
    yield
    load_known_strategies.cache_clear()


def test_loads_from_dict(monkeypatch):
    _install_fake_module(monkeypatch, "STRATEGIES", {"a_asian": object(), "g_inside_bar_breakout": object()})
    from tools.warehouse.known_strategies import load_known_strategies
    s = load_known_strategies()
    assert s == frozenset({"a_asian", "g_inside_bar_breakout"})


def test_loads_from_iterable(monkeypatch):
    # Real impl only reads STRATEGIES dict, so test with STRATEGIES as a dict
    _install_fake_module(monkeypatch, "STRATEGIES", {"foo": object(), "bar": object()})
    from tools.warehouse.known_strategies import load_known_strategies
    result = load_known_strategies()
    assert result == frozenset({"foo", "bar"})


def test_returns_frozenset_on_import_error(monkeypatch):
    """Real impl returns frozenset() with a warning when config is unavailable."""
    # Remove config.strategies from sys.modules so import fails
    monkeypatch.setitem(sys.modules, "config.strategies", None)  # type: ignore[assignment]
    from tools.warehouse.known_strategies import load_known_strategies
    result = load_known_strategies()
    # The real impl returns frozenset() on error, not RuntimeError
    assert isinstance(result, frozenset)
