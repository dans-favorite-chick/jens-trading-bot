"""
Tests for phoenix_bot.core.position_manager — PositionManager
"""

import sys
import os

# Add project root so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from core.position_manager import PositionManager, DOLLAR_PER_TICK, TICK_SIZE


# ─── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def pm():
    """Fresh PositionManager for each test."""
    return PositionManager()


def _open_long(pm, entry=18500.0, stop=18496.0, target=18508.0, contracts=1):
    """Helper to open a LONG position with sensible defaults."""
    return pm.open_position(
        trade_id="test-001",
        direction="LONG",
        entry_price=entry,
        contracts=contracts,
        stop_price=stop,
        target_price=target,
        strategy="test_strat",
        reason="unit test",
    )


def _open_short(pm, entry=18500.0, stop=18504.0, target=18492.0, contracts=1):
    """Helper to open a SHORT position with sensible defaults."""
    return pm.open_position(
        trade_id="test-002",
        direction="SHORT",
        entry_price=entry,
        contracts=contracts,
        stop_price=stop,
        target_price=target,
        strategy="test_strat",
        reason="unit test",
    )


# ─── is_flat ──────────────────────────────────────────────────────────

class TestIsFlat:

    def test_is_flat_initially(self, pm):
        assert pm.is_flat is True

    def test_not_flat_after_open(self, pm):
        _open_long(pm)
        assert pm.is_flat is False


# ─── open_position() ─────────────────────────────────────────────────

class TestOpenPosition:

    def test_open_position_sets_position(self, pm):
        result = _open_long(pm, entry=18500.0)
        assert result is True
        assert pm.position is not None
        assert pm.position.direction == "LONG"
        assert pm.position.entry_price == 18500.0

    def test_open_position_rejects_same_strategy_duplicate(self, pm):
        # Phase C (2026-04-21) multi-position refactor: PositionManager now
        # supports concurrent positions from DIFFERENT strategies. The
        # invariant that remains is per-strategy uniqueness — opening a
        # second position for the SAME strategy must reject.
        _open_long(pm)  # strategy="test_strat"
        result = pm.open_position(
            trade_id="test-dup",
            direction="SHORT",
            entry_price=18510.0,
            contracts=1,
            stop_price=18514.0,
            target_price=18502.0,
            strategy="test_strat",  # same strategy — must reject
            reason="dup",
        )
        assert result is False
        # Original position unchanged
        assert pm.position.direction == "LONG"
        assert pm.active_count == 1

    def test_open_position_allows_different_strategies_concurrently(self, pm):
        # Phase C: different strategies trade independently on their own
        # NT8 sub-accounts and may hold positions concurrently.
        _open_long(pm)  # strategy="test_strat"
        result = pm.open_position(
            trade_id="test-strat-b",
            direction="SHORT",
            entry_price=18510.0,
            contracts=1,
            stop_price=18514.0,
            target_price=18502.0,
            strategy="different_strat",
            reason="concurrent",
        )
        assert result is True
        assert pm.active_count == 2
        assert pm.is_flat_for("test_strat") is False
        assert pm.is_flat_for("different_strat") is False
        assert pm.is_flat_for("never_opened") is True


# ─── close_position() ────────────────────────────────────────────────

class TestClosePosition:

    def test_close_long_pnl_correct(self, pm):
        # Entry 18500, exit 18502 = +2.0 points = 8 ticks
        # 8 ticks * $0.50/tick * 1 contract = +$4.00
        _open_long(pm, entry=18500.0, contracts=1)
        trade = pm.close_position(exit_price=18502.0, exit_reason="target_hit")

        assert trade is not None
        expected_ticks = (18502.0 - 18500.0) / TICK_SIZE  # 8.0
        expected_gross = expected_ticks * DOLLAR_PER_TICK * 1  # 8.0 * 0.50 = $4.00
        assert trade["pnl_ticks"] == expected_ticks
        assert trade["gross_pnl"] == expected_gross
        # pnl_dollars is NET of commission (B13): gross - round-trip commission
        assert trade["pnl_dollars"] == round(expected_gross - trade["commission"], 2)
        assert trade["result"] == "WIN"

    def test_close_short_pnl_correct(self, pm):
        # Entry 18500, exit 18498 = +2.0 points = 8 ticks profit for SHORT
        # 8 ticks * $0.50/tick * 1 contract = +$4.00
        _open_short(pm, entry=18500.0, contracts=1)
        trade = pm.close_position(exit_price=18498.0, exit_reason="target_hit")

        assert trade is not None
        expected_ticks = (18500.0 - 18498.0) / TICK_SIZE  # 8.0
        expected_gross = expected_ticks * DOLLAR_PER_TICK * 1  # $4.00
        assert trade["pnl_ticks"] == expected_ticks
        assert trade["gross_pnl"] == expected_gross
        # pnl_dollars is NET of commission (B13)
        assert trade["pnl_dollars"] == round(expected_gross - trade["commission"], 2)
        assert trade["result"] == "WIN"

    def test_close_position_returns_none_when_flat(self, pm):
        trade = pm.close_position(exit_price=18500.0, exit_reason="manual")
        assert trade is None

    def test_close_position_makes_flat(self, pm):
        _open_long(pm)
        pm.close_position(exit_price=18502.0, exit_reason="test")
        assert pm.is_flat is True

    def test_close_position_appends_to_history(self, pm):
        _open_long(pm)
        pm.close_position(exit_price=18502.0, exit_reason="test")
        assert len(pm.trade_history) == 1


# ─── check_exits() ───────────────────────────────────────────────────

class TestCheckExits:

    def test_stop_loss_long_when_price_at_or_below_stop(self, pm):
        _open_long(pm, entry=18500.0, stop=18496.0)
        assert pm.check_exits(current_price=18496.0) == "stop_loss"
        # Also below stop
        assert pm.check_exits(current_price=18494.0) == "stop_loss"

    def test_stop_loss_short_when_price_at_or_above_stop(self, pm):
        _open_short(pm, entry=18500.0, stop=18504.0)
        assert pm.check_exits(current_price=18504.0) == "stop_loss"
        assert pm.check_exits(current_price=18506.0) == "stop_loss"

    def test_target_hit_long_when_price_at_or_above_target(self, pm):
        _open_long(pm, entry=18500.0, target=18508.0)
        assert pm.check_exits(current_price=18508.0) == "target_hit"
        assert pm.check_exits(current_price=18510.0) == "target_hit"

    def test_target_hit_short_when_price_at_or_below_target(self, pm):
        _open_short(pm, entry=18500.0, target=18492.0)
        assert pm.check_exits(current_price=18492.0) == "target_hit"
        assert pm.check_exits(current_price=18490.0) == "target_hit"

    def test_no_exit_when_price_between_stop_and_target(self, pm):
        _open_long(pm, entry=18500.0, stop=18496.0, target=18508.0)
        assert pm.check_exits(current_price=18500.0) is None
        assert pm.check_exits(current_price=18504.0) is None

    def test_no_exit_when_flat(self, pm):
        assert pm.check_exits(current_price=18500.0) is None


# ─── unrealized_pnl() ────────────────────────────────────────────────

class TestUnrealizedPnl:

    def test_unrealized_pnl_long(self, pm):
        _open_long(pm, entry=18500.0, contracts=2)
        # Price up 1 point = 4 ticks; 4 * $0.50 * 2 contracts = $4.00
        pnl = pm.unrealized_pnl(current_price=18501.0)
        assert pnl == pytest.approx(4.0)

    def test_unrealized_pnl_short(self, pm):
        _open_short(pm, entry=18500.0, contracts=2)
        # Price down 1 point = 4 ticks; 4 * $0.50 * 2 contracts = $4.00
        pnl = pm.unrealized_pnl(current_price=18499.0)
        assert pnl == pytest.approx(4.0)

    def test_unrealized_pnl_negative_long(self, pm):
        _open_long(pm, entry=18500.0, contracts=1)
        # Price down 2 points = -8 ticks; -8 * $0.50 = -$4.00
        pnl = pm.unrealized_pnl(current_price=18498.0)
        assert pnl == pytest.approx(-4.0)

    def test_unrealized_pnl_zero_when_flat(self, pm):
        assert pm.unrealized_pnl(current_price=18500.0) == 0.0
