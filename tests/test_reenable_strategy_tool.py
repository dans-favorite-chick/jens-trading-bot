"""
Tests for tools/reenable_strategy.py — strategy halt recovery CLI.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.strategy_risk_registry import StrategyRiskRegistry
from tools import reenable_strategy


@pytest.fixture
def halt_file_tmp(tmp_path, monkeypatch):
    """Redirect halt-state persistence to a tmp_path in both modules."""
    tmp_halt = tmp_path / "strategy_halts.json"
    monkeypatch.setattr(
        "core.strategy_risk_registry.STRATEGY_HALT_STATE_FILE",
        str(tmp_halt),
    )
    return tmp_halt


def _halt(keys: list[str]):
    """Helper: spin up a registry, halt the given keys, persist, return path."""
    reg = StrategyRiskRegistry()
    for key in keys:
        if "." in key:
            s, sub = key.split(".", 1)
        else:
            s, sub = key, None
        reg.halt(s, sub, reason=f"test halt {key}")
    return reg


class TestListMode:
    def test_no_halts_prints_none_message(self, halt_file_tmp, capsys):
        rc = reenable_strategy.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "No halted strategies." in out

    def test_list_prints_count_and_lines(self, halt_file_tmp, capsys):
        _halt(["bias_momentum", "opening_session.orb", "vwap_pullback"])
        rc = reenable_strategy.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "3 halted" in out
        assert "bias_momentum" in out
        assert "opening_session.orb" in out
        assert "vwap_pullback" in out
        # Each halted key appears on its own output line
        halt_lines = [ln for ln in out.splitlines() if ln.startswith("  ")]
        assert len(halt_lines) == 3


class TestClearOne:
    def test_clear_specific_key_removes_halt(self, halt_file_tmp, capsys):
        _halt(["bias_momentum", "opening_session.orb"])
        rc = reenable_strategy.main(["bias_momentum"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Re-enabled" in out

        # New registry reloads from disk → halt for bias_momentum is gone,
        # but opening_session.orb should still be halted.
        reg = StrategyRiskRegistry()
        assert not reg.is_halted("bias_momentum")
        assert reg.is_halted("opening_session", "orb")

    def test_clear_subkey_with_dot(self, halt_file_tmp, capsys):
        _halt(["opening_session.orb"])
        rc = reenable_strategy.main(["opening_session.orb"])
        capsys.readouterr()
        assert rc == 0
        reg = StrategyRiskRegistry()
        assert not reg.is_halted("opening_session", "orb")

    def test_clear_unknown_key_exits_1(self, halt_file_tmp, capsys):
        _halt(["bias_momentum"])
        rc = reenable_strategy.main(["never_halted_key"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "not halted" in out.lower()


class TestClearAll:
    def test_all_clears_everything(self, halt_file_tmp, capsys):
        _halt(["bias_momentum", "opening_session.orb", "vwap_pullback"])
        rc = reenable_strategy.main(["--all"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Cleared 3" in out

        reg = StrategyRiskRegistry()
        assert not reg._halted

    def test_all_on_empty_is_noop(self, halt_file_tmp, capsys):
        rc = reenable_strategy.main(["--all"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Cleared 0" in out
