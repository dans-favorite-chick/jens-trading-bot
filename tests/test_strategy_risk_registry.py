"""
Phase C — Per-strategy risk registry tests.

Covers:
- Init creates all 16 STRATEGY_KEYS
- get() returns distinct RiskManager instances
- Per-strategy daily-cap isolation (one hitting cap doesn't affect others)
- record_trade_result updates cumulative P&L
- Balance crosses floor → halt + persist
- reenable() clears + persists
- New registry instance recovers halt state from disk
- Nested opening_session keys work correctly
"""

from __future__ import annotations

import json
import os

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import (
    PER_STRATEGY_ACCOUNT_SIZE,
    PER_STRATEGY_DAILY_LOSS_CAP,
    PER_STRATEGY_FLOOR,
    STRATEGY_HALT_STATE_FILE,
)
from core.strategy_risk_registry import (
    StrategyRiskRegistry,
    STRATEGY_KEYS,
    _key_for,
)


@pytest.fixture
def halt_file_tmp(tmp_path, monkeypatch):
    """Redirect halt-state persistence to a tmp_path."""
    tmp_halt = tmp_path / "strategy_halts.json"
    monkeypatch.setattr(
        "core.strategy_risk_registry.STRATEGY_HALT_STATE_FILE",
        str(tmp_halt),
    )
    return tmp_halt


@pytest.fixture
def registry(halt_file_tmp):
    return StrategyRiskRegistry()


# ═══════════════════════════════════════════════════════════════════
# Init + key alignment
# ═══════════════════════════════════════════════════════════════════

class TestInit:
    def test_all_strategy_keys_initialized(self, registry):
        keys = registry.known_keys()
        assert set(keys) == set(STRATEGY_KEYS)

    def test_strategy_keys_count_matches_account_routing(self):
        from config.account_routing import STRATEGY_ACCOUNT_MAP

        # Count: 6 opening_session subs + 10 flat top-level = 16
        flat = [k for k, v in STRATEGY_ACCOUNT_MAP.items()
                if k != "_default" and isinstance(v, str)]
        subs = []
        for k, v in STRATEGY_ACCOUNT_MAP.items():
            if isinstance(v, dict):
                subs.extend([f"{k}.{s}" for s in v.keys()])
        expected = sorted(flat + subs)
        # Registry also includes runtime aliases that strategies register under
        # bare names ("compression_breakout", "opening_session") — these are
        # parent keys not present in the routing map. Allow them.
        actual = sorted(k for k in STRATEGY_KEYS
                        if k not in {"compression_breakout", "opening_session"})
        assert actual == expected

    def test_all_initial_balances_equal_starting_size(self, registry):
        for key in STRATEGY_KEYS:
            if "." in key:
                strat, sub = key.split(".", 1)
                assert registry.current_balance(strat, sub) == PER_STRATEGY_ACCOUNT_SIZE
            else:
                assert registry.current_balance(key) == PER_STRATEGY_ACCOUNT_SIZE


# ═══════════════════════════════════════════════════════════════════
# Instance isolation
# ═══════════════════════════════════════════════════════════════════

class TestInstanceIsolation:
    def test_distinct_instances(self, registry):
        rm_a = registry.get("bias_momentum")
        rm_b = registry.get("spring_setup")
        assert rm_a is not rm_b

    def test_same_key_returns_same_instance(self, registry):
        first = registry.get("bias_momentum")
        second = registry.get("bias_momentum")
        assert first is second

    def test_opening_session_sub_strategies_distinct(self, registry):
        a = registry.get("opening_session", "open_drive")
        b = registry.get("opening_session", "orb")
        assert a is not b

    def test_daily_cap_isolation(self, registry):
        # Push bias_momentum past its daily cap.
        registry.record_trade_result(
            "bias_momentum",
            -(PER_STRATEGY_DAILY_LOSS_CAP + 10),  # over the cap
        )
        rm_bias = registry.get("bias_momentum")
        rm_spring = registry.get("spring_setup")

        # bias can't trade (hit daily cap)
        assert rm_bias.can_trade() == (False, "Daily loss limit hit ($-210.00 / -$200.00)") \
               or rm_bias.can_trade()[0] is False
        # spring is untouched
        assert rm_spring.can_trade()[0] is True


# ═══════════════════════════════════════════════════════════════════
# Balance tracking + floor halt
# ═══════════════════════════════════════════════════════════════════

class TestBalanceAndFloor:
    def test_record_trade_updates_cumulative(self, registry):
        registry.record_trade_result("bias_momentum", +50.0)
        registry.record_trade_result("bias_momentum", -20.0)
        assert registry.current_balance("bias_momentum") == PER_STRATEGY_ACCOUNT_SIZE + 30.0

    def test_floor_hit_triggers_halt(self, registry):
        # $2,000 - $500 = $1,500 = exactly the floor → halt
        hit = registry.record_trade_result("dom_pullback", -500.0)
        assert hit is True
        assert registry.is_halted("dom_pullback") is True

    def test_floor_halt_persists_across_instance(self, registry, halt_file_tmp):
        registry.record_trade_result("ib_breakout", -600.0)  # below floor
        assert registry.is_halted("ib_breakout") is True

        # New instance loads the persisted halt
        new_reg = StrategyRiskRegistry()
        assert new_reg.is_halted("ib_breakout") is True

    def test_floor_halt_reason_persists(self, registry):
        registry.record_trade_result("noise_area", -550.0)
        reason = registry.halt_reason("noise_area")
        assert reason is not None
        assert "floor" in reason.lower()

    def test_reenable_clears_halt_and_persists(self, registry, halt_file_tmp):
        registry.record_trade_result("compression_breakout_15m", -600.0)
        assert registry.is_halted("compression_breakout_15m") is True

        cleared = registry.reenable("compression_breakout_15m")
        assert cleared is True
        assert registry.is_halted("compression_breakout_15m") is False

        # Verify persistence
        new_reg = StrategyRiskRegistry()
        assert new_reg.is_halted("compression_breakout_15m") is False

    def test_reenable_returns_false_if_not_halted(self, registry):
        assert registry.reenable("vwap_pullback") is False


# ═══════════════════════════════════════════════════════════════════
# Nested opening_session sub-strategies
# ═══════════════════════════════════════════════════════════════════

class TestNestedSubStrategies:
    def test_sub_strategy_halt_isolation(self, registry):
        registry.record_trade_result(
            "opening_session", -600.0, sub_strategy="open_drive",
        )
        # open_drive is halted, but other sub-strategies are fine.
        assert registry.is_halted("opening_session", "open_drive") is True
        assert registry.is_halted("opening_session", "orb") is False
        assert registry.is_halted("opening_session", "premarket_breakout") is False

    def test_sub_strategy_balance_tracking(self, registry):
        registry.record_trade_result(
            "opening_session", +75.0, sub_strategy="open_test_drive",
        )
        assert registry.current_balance(
            "opening_session", "open_test_drive",
        ) == PER_STRATEGY_ACCOUNT_SIZE + 75.0
        # sibling unchanged
        assert registry.current_balance(
            "opening_session", "orb",
        ) == PER_STRATEGY_ACCOUNT_SIZE


# ═══════════════════════════════════════════════════════════════════
# Snapshot + daily reset
# ═══════════════════════════════════════════════════════════════════

class TestSnapshot:
    def test_snapshot_contains_all_keys(self, registry):
        snap = registry.snapshot()
        assert set(snap.keys()) == set(STRATEGY_KEYS)

    def test_snapshot_entry_shape(self, registry):
        snap = registry.snapshot()
        entry = snap["bias_momentum"]
        for field in ("daily_pnl", "cumulative_pnl", "current_balance",
                      "trades_today", "halted", "halt_reason"):
            assert field in entry


class TestDailyReset:
    def test_daily_reset_clears_daily_state(self, registry):
        registry.record_trade_result("bias_momentum", -50.0)
        rm = registry.get("bias_momentum")
        assert rm.state.daily_pnl != 0.0

        registry.daily_reset()
        assert rm.state.daily_pnl == 0.0

    def test_daily_reset_preserves_cumulative(self, registry):
        registry.record_trade_result("bias_momentum", -50.0)
        bal_before = registry.current_balance("bias_momentum")
        registry.daily_reset()
        assert registry.current_balance("bias_momentum") == bal_before

    def test_daily_reset_preserves_halts(self, registry):
        registry.record_trade_result("dom_pullback", -600.0)
        assert registry.is_halted("dom_pullback") is True
        registry.daily_reset()
        assert registry.is_halted("dom_pullback") is True


# ═══════════════════════════════════════════════════════════════════
# Unknown-key fallback logs a WARNING (don't silently route)
# ═══════════════════════════════════════════════════════════════════

class TestUnknownKeyFallback:
    def test_unknown_key_logs_warning(self, registry, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="StrategyRiskRegistry")
        registry.get("never_heard_of_this")
        assert any("unknown strategy key" in rec.message for rec in caplog.records)


# ═══════════════════════════════════════════════════════════════════
# _key_for helper
# ═══════════════════════════════════════════════════════════════════

class TestKeyFor:
    def test_flat(self):
        assert _key_for("bias_momentum") == "bias_momentum"

    def test_nested(self):
        assert _key_for("opening_session", "orb") == "opening_session.orb"

    def test_none_sub_is_flat(self):
        assert _key_for("bias_momentum", None) == "bias_momentum"
