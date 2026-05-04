"""Fix E (2026-05-03): HALT/CAP log signatures for watcher detection.

Watcher_agent's grep needs `[HALT:<strategy>]` and `[CAP:<scope>:<account>]`
patterns to detect halts. Previously halts were silent or used a
non-greppable format. These tests verify each signature fires when
its trigger condition is met.
"""
from __future__ import annotations

import logging

import pytest

from core.risk_manager import RiskManager, RiskState
import core.strategy_risk_registry as srr_module
from core.strategy_risk_registry import StrategyRiskRegistry


@pytest.fixture
def fresh_registry(tmp_path, monkeypatch):
    """Each test gets a registry with its own empty halts file so
    halt state from other tests / previous runs doesn't bleed in."""
    halts_file = tmp_path / "strategy_halts.json"
    monkeypatch.setattr(srr_module, "_HALTS_FILE", str(halts_file), raising=False)
    reg = StrategyRiskRegistry()
    # If the module exposes the path differently, also clear loaded state
    reg._halted = set()
    reg._halt_reasons = {}
    return reg


# ─── [HALT:<strategy>] — strategy floor breach ───────────────────────

def test_strategy_floor_halt_emits_signature(fresh_registry, caplog):
    """When StrategyRiskRegistry.halt() is called, a [HALT:<key>] log
    line at CRITICAL level must appear."""
    caplog.set_level(logging.CRITICAL)
    fresh_registry.halt("test_strategy", reason="balance $1400 <= floor $1500")
    matches = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and "[HALT:test_strategy]" in r.message
    ]
    assert matches, f"No [HALT:test_strategy] log found in: {[r.message for r in caplog.records]}"


def test_strategy_halt_includes_reason(fresh_registry, caplog):
    """Halt log line must contain the reason for forensic clarity."""
    caplog.set_level(logging.CRITICAL)
    fresh_registry.halt("foo", reason="custom-reason-XYZ")
    msg = next(r.message for r in caplog.records if "[HALT:foo]" in r.message)
    assert "custom-reason-XYZ" in msg


def test_strategy_halt_idempotent_no_duplicate_log(fresh_registry, caplog):
    """Calling halt() twice on the same key must not log twice."""
    caplog.set_level(logging.CRITICAL)
    fresh_registry.halt("dupe_test", reason="first")
    fresh_registry.halt("dupe_test", reason="second")
    matches = [r for r in caplog.records if "[HALT:dupe_test]" in r.message]
    # The halt() method early-returns when key is already halted, so
    # only the first call logs.
    assert len(matches) == 1


# ─── [CAP:daily:<account>] — daily cap breach ────────────────────────

def test_daily_cap_emits_signature(caplog):
    """When can_trade() returns False due to daily cap, [CAP:daily:<account>]
    appears at CRITICAL on the first poll after the cap is breached."""
    caplog.set_level(logging.CRITICAL)
    rm = RiskManager()
    rm.set_daily_limit(200.0)
    rm.state.daily_pnl = -250.0  # over the cap
    can, reason = rm.can_trade(account="Sim101")
    assert can is False
    matches = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and "[CAP:daily:Sim101]" in r.message
    ]
    assert matches, f"No [CAP:daily:Sim101] log: {[r.message for r in caplog.records]}"


def test_daily_cap_log_once_only(caplog):
    """Repeat polls after cap breach must not spam the log."""
    caplog.set_level(logging.CRITICAL)
    rm = RiskManager()
    rm.set_daily_limit(200.0)
    rm.state.daily_pnl = -250.0
    rm.can_trade(account="Sim101")
    rm.can_trade(account="Sim101")
    rm.can_trade(account="Sim101")
    matches = [r for r in caplog.records if "[CAP:daily:Sim101]" in r.message]
    assert len(matches) == 1


# ─── [CAP:weekly:<account>] — weekly cap breach ──────────────────────

def test_weekly_cap_emits_signature(caplog):
    """Weekly cap breach → [CAP:weekly:<account>] CRITICAL log."""
    caplog.set_level(logging.CRITICAL)
    rm = RiskManager()
    rm.state.daily_pnl = 0.0       # daily clean
    rm.state.weekly_pnl = -500.0   # weekly over limit (default WEEKLY_LOSS_LIMIT is much smaller)
    can, _ = rm.can_trade(account="SimBias Momentum")
    assert can is False
    matches = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and "[CAP:weekly:SimBias Momentum]" in r.message
    ]
    assert matches


# ─── [HALT:bot] — kill switch engaged ────────────────────────────────

def test_kill_switch_emits_halt_bot(caplog):
    """When kill switch is engaged, [HALT:bot] CRITICAL log appears."""
    caplog.set_level(logging.CRITICAL)
    rm = RiskManager()
    rm.state.killed = True
    rm.state.kill_reason = "manual emergency"
    can, _ = rm.can_trade()
    assert can is False
    matches = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and "[HALT:bot]" in r.message
    ]
    assert matches
