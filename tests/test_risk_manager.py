"""
Tests for phoenix_bot.core.risk_manager — RiskManager
"""

import sys
import os
import time

# Add project root so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from core.risk_manager import RiskManager


# ─── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def rm():
    """Fresh RiskManager for each test."""
    return RiskManager()


# ─── can_trade() ───────────────────────────────────────────────────────

class TestCanTrade:

    def test_can_trade_returns_true_when_no_limits_hit(self, rm):
        allowed, reason = rm.can_trade(vix=20.0)
        assert allowed is True
        assert reason == "OK"

    def test_can_trade_blocks_when_daily_loss_limit_hit(self, rm):
        # Daily loss limit is $45; push daily_pnl to -$45
        rm.state.daily_pnl = -45.0
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "Daily loss limit" in reason

    def test_can_trade_blocks_when_weekly_loss_limit_hit(self, rm):
        # Weekly loss limit is $150
        rm.state.weekly_pnl = -150.0
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "Weekly loss limit" in reason

    def test_can_trade_blocks_when_max_trades_reached(self, rm):
        # Default max is 6
        rm.state.trades_today = 6
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "Max trades" in reason

    def test_can_trade_blocks_when_vix_extreme(self, rm):
        allowed, reason = rm.can_trade(vix=40.0)
        assert allowed is False
        assert "VIX extreme" in reason

    def test_can_trade_allows_when_vix_below_extreme(self, rm):
        allowed, reason = rm.can_trade(vix=39.9)
        assert allowed is True
        assert reason == "OK"


# ─── record_trade() ───────────────────────────────────────────────────

class TestRecordTrade:

    def test_record_trade_updates_daily_pnl(self, rm):
        rm.record_trade(10.0)
        assert rm.state.daily_pnl == 10.0

        rm.record_trade(-5.0)
        assert rm.state.daily_pnl == 5.0

    def test_record_trade_triggers_recovery_mode_at_negative_30(self, rm):
        assert rm.state.recovery_mode is False
        # Push daily P&L to exactly -$30
        rm.record_trade(-30.0)
        assert rm.state.recovery_mode is True

    def test_record_trade_triggers_cooloff_after_3_consecutive_losses(self, rm):
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        assert rm.state.cooloff_until == 0.0  # Not yet

        rm.record_trade(-5.0)  # 3rd consecutive loss
        assert rm.state.cooloff_until > time.time()

    def test_record_trade_win_resets_consecutive_losses(self, rm):
        rm.record_trade(-5.0)
        rm.record_trade(-5.0)
        assert rm.state.consecutive_losses == 2

        rm.record_trade(10.0)
        assert rm.state.consecutive_losses == 0


# ─── get_risk_for_entry() ─────────────────────────────────────────────

class TestGetRiskForEntry:

    def test_tier_a_plus_for_score_50_and_above(self, rm):
        risk, tier = rm.get_risk_for_entry(entry_score=55.0, vix=20.0)
        assert risk == 15.0
        assert "A++" in tier

    def test_tier_b_for_score_40_to_49(self, rm):
        risk, tier = rm.get_risk_for_entry(entry_score=45.0, vix=20.0)
        assert risk == 12.0
        assert "B" in tier

    def test_tier_c_for_score_30_to_39(self, rm):
        risk, tier = rm.get_risk_for_entry(entry_score=35.0, vix=20.0)
        assert risk == 8.0
        assert "C" in tier

    def test_skip_for_score_below_30(self, rm):
        risk, tier = rm.get_risk_for_entry(entry_score=25.0, vix=20.0)
        assert risk == 0.0
        assert tier == "SKIP"

    def test_vix_high_reduces_risk_50_percent(self, rm):
        # VIX_HIGH = 30; A++ base = $15 -> $7.50
        risk, tier = rm.get_risk_for_entry(entry_score=55.0, vix=30.0)
        assert risk == 7.5
        assert "VIX-reduced" in tier

    def test_recovery_mode_reduces_risk_50_percent(self, rm):
        rm.state.recovery_mode = True
        # A++ base = $15, normal VIX -> $15 * 0.5 = $7.50
        risk, tier = rm.get_risk_for_entry(entry_score=55.0, vix=20.0)
        assert risk == 7.5
        assert "recovery" in tier

    def test_vix_high_and_recovery_stack(self, rm):
        rm.state.recovery_mode = True
        # A++ base $15 * 0.5 (VIX) * 0.5 (recovery) = $3.75
        risk, tier = rm.get_risk_for_entry(entry_score=55.0, vix=30.0)
        assert risk == 3.75


# ─── reset_daily() ────────────────────────────────────────────────────

class TestResetDaily:

    def test_reset_daily_clears_all_state(self, rm):
        # Dirty up the state
        rm.state.daily_pnl = -40.0
        rm.state.trades_today = 5
        rm.state.wins_today = 2
        rm.state.losses_today = 3
        rm.state.consecutive_losses = 3
        rm.state.recovery_mode = True
        rm.state.cooloff_until = time.time() + 9999
        rm.state.killed = True
        rm.state.kill_reason = "test"

        rm.reset_daily()

        assert rm.state.daily_pnl == 0.0
        assert rm.state.trades_today == 0
        assert rm.state.wins_today == 0
        assert rm.state.losses_today == 0
        assert rm.state.consecutive_losses == 0
        assert rm.state.recovery_mode is False
        assert rm.state.cooloff_until == 0.0
        assert rm.state.killed is False
        assert rm.state.kill_reason == ""


# ─── calculate_contracts() ────────────────────────────────────────────

class TestCalculateContracts:

    def test_calculate_contracts_basic_math(self, rm):
        # $15 risk, 8 tick stop, TICK_SIZE=0.25 -> dollar_per_tick = 0.50
        # risk_per_contract = 8 * 0.50 = $4.00
        # contracts = int(15 / 4) = 3
        result = rm.calculate_contracts(risk_dollars=15.0, stop_ticks=8)
        assert result == 3

    def test_calculate_contracts_rounds_down(self, rm):
        # $10 risk, 8 tick stop -> risk_per_contract = $4
        # 10 / 4 = 2.5 -> int(2.5) = 2
        result = rm.calculate_contracts(risk_dollars=10.0, stop_ticks=8)
        assert result == 2

    def test_calculate_contracts_minimum_one(self, rm):
        # $1 risk, 8 tick stop -> 1/4 = 0.25 -> int = 0 -> max(1, 0) = 1
        result = rm.calculate_contracts(risk_dollars=1.0, stop_ticks=8)
        assert result == 1

    def test_calculate_contracts_zero_stop_returns_zero(self, rm):
        result = rm.calculate_contracts(risk_dollars=15.0, stop_ticks=0)
        assert result == 0
