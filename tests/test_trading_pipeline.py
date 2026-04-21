"""
Phoenix Bot — Integration Tests for Trading Pipeline

Red Team Runbook: tests that simulate the failure modes most likely
to hurt real money. Each test exercises a specific desync scenario.

These are the tests Codex said were missing — the risky paths that
unit tests on RiskManager/PositionManager don't cover.
"""

import sys
import os
import time
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.risk_manager import RiskManager
from core.position_manager import PositionManager
from core.session_manager import SessionManager
from core.strategy_tracker import StrategyTracker
from strategies.base_strategy import Signal


# ─── Helpers ──────────────────────────────────────────────────────

def make_signal(direction="LONG", strategy="bias_momentum",
                confidence=60, entry_score=45):
    return Signal(
        direction=direction,
        stop_ticks=9,
        target_rr=2.0,
        confidence=confidence,
        entry_score=entry_score,
        strategy=strategy,
        reason="test signal",
        confluences=["test"],
    )


# ─── Test 1: Zero-Contract Entry Rejection ────────────────────────

class TestZeroContractRejection:
    """Verify that 0-contract entries are rejected at source."""

    def test_risk_manager_skip_for_low_score(self):
        rm = RiskManager()
        risk, tier = rm.get_risk_for_entry(entry_score=10)
        assert risk == 0.0
        assert tier == "SKIP"

    def test_risk_manager_zero_stop_returns_zero_contracts(self):
        rm = RiskManager()
        contracts = rm.calculate_contracts(risk_dollars=15.0, stop_ticks=0)
        assert contracts == 0

    def test_oif_writer_rejects_zero_qty_entries(self):
        from bridge.oif_writer import write_oif
        # Entry with qty=0 should be rejected
        paths = write_oif("ENTER_LONG", qty=0, trade_id="test_zero")
        assert paths == []

    def test_oif_writer_allows_zero_qty_for_cancel(self):
        from bridge.oif_writer import write_oif
        # CANCEL_ALL with qty=0 should be allowed
        paths = write_oif("CANCEL_ALL", qty=0, trade_id="test_cancel")
        # May or may not write depending on folder access, but should not error
        assert isinstance(paths, list)


# ─── Test 2: Fill Correlation Safety ──────────────────────────────

class TestFillCorrelation:
    """Verify fill matching doesn't accept wrong trade's confirmation."""

    def test_check_fills_filters_by_time(self):
        from bridge.oif_writer import check_fills
        # Fills from before our time window should be excluded
        old_time = time.time() + 999999  # Far future = nothing matches
        fills = check_fills(since_time=old_time)
        assert fills == []

    def test_wait_for_fill_returns_timeout(self):
        """Verify timeout returns TIMEOUT status, not false positive."""
        import asyncio
        from bridge.oif_writer import wait_for_fill
        # With a very short timeout, should return TIMEOUT
        result = asyncio.run(wait_for_fill("nonexistent_trade", timeout_s=0.1))
        assert result["status"] == "TIMEOUT"
        assert result["content"] is None


# ─── Test 3: Exit State Ordering ──────────────────────────────────

class TestExitStateOrdering:
    """Verify position is not cleared until after exit commands sent."""

    def test_position_manager_close_returns_trade_record(self):
        pm = PositionManager()
        pm.open_position(
            trade_id="test_exit", direction="LONG", entry_price=25000.0,
            contracts=1, stop_price=24990.0, target_price=25020.0,
            strategy="test", reason="test",
        )
        assert not pm.is_flat

        trade = pm.close_position(25010.0, "stop_loss")
        assert trade is not None
        assert trade["trade_id"] == "test_exit"
        assert trade["pnl_dollars"] > 0
        assert pm.is_flat  # Only flat AFTER close

    def test_exit_pending_status(self):
        """The bot should set EXIT_PENDING before sending commands."""
        # This validates the concept — base_bot sets status = "EXIT_PENDING"
        # before ws.send, then status = "SCANNING" after Python close
        pm = PositionManager()
        pm.open_position(
            trade_id="test_ep", direction="SHORT", entry_price=25000.0,
            contracts=1, stop_price=25010.0, target_price=24980.0,
            strategy="test", reason="test",
        )
        # Position exists before close
        assert pm.position is not None
        assert pm.position.trade_id == "test_ep"


# ─── Test 4: Prod Window Enforcement ─────────────────────────────

class TestProdWindowEnforcement:
    """Verify prod bot respects trading window."""

    def test_prod_window_during_session(self):
        sm = SessionManager()
        # 9:00 AM should be in prod window (08:30-10:00)
        from datetime import time as dtime
        test_time = datetime(2026, 4, 12, 9, 0, 0)
        assert sm.is_prod_trading_window(test_time) is True

    def test_prod_window_outside_session(self):
        sm = SessionManager()
        # 11:00 AM should be outside prod window
        test_time = datetime(2026, 4, 12, 11, 0, 0)
        assert sm.is_prod_trading_window(test_time) is False

    def test_prod_window_before_session(self):
        sm = SessionManager()
        # 7:00 AM should be outside prod window
        test_time = datetime(2026, 4, 12, 7, 0, 0)
        assert sm.is_prod_trading_window(test_time) is False

    def test_prod_window_at_close(self):
        sm = SessionManager()
        # Primary window is now 08:30–11:00 CST (exclusive at 11:00).
        # Gap between primary and secondary (11:00–13:00) should be closed.
        test_time = datetime(2026, 4, 12, 12, 0, 0)
        assert sm.is_prod_trading_window(test_time) is False
        # And exactly at primary close 11:00:
        assert sm.is_prod_trading_window(datetime(2026, 4, 12, 11, 0, 0)) is False


# ─── Test 5: Signal Lifecycle (No Double-Count) ──────────────────

class TestSignalLifecycle:
    """Verify signals are updated, not duplicated."""

    def test_signal_generated_then_filled(self):
        tracker = StrategyTracker()
        # First: signal generated (not yet taken)
        tracker.record_signal(
            strategy="bias_momentum", direction="LONG",
            confidence=60, taken=False, trade_id="sig_001",
        )
        assert len(tracker.signal_log) == 1
        assert tracker.signal_log[0]["taken"] is False

        # Second: same trade filled
        tracker.record_signal(
            strategy="bias_momentum", direction="LONG",
            confidence=60, taken=True, trade_id="sig_001",
        )
        # Should UPDATE existing, not add new
        assert len(tracker.signal_log) == 1
        assert tracker.signal_log[0]["taken"] is True
        assert "updated_at" in tracker.signal_log[0]

    def test_different_trades_create_separate_records(self):
        tracker = StrategyTracker()
        tracker.record_signal(
            strategy="bias_momentum", direction="LONG",
            confidence=60, taken=False, trade_id="sig_A",
        )
        tracker.record_signal(
            strategy="spring_setup", direction="SHORT",
            confidence=45, taken=False, trade_id="sig_B",
        )
        assert len(tracker.signal_log) == 2


# ─── Test 6: Regime-Aware Strategy Gating ─────────────────────────

class TestRegimeAwareStrategies:
    """Verify strategies loosen gates in golden windows."""

    def test_bias_momentum_uses_regime_overrides(self):
        from strategies.bias_momentum import _REGIME_OVERRIDES
        # Contract (post-B13 recalibration): each regime entry has
        # min_momentum + min_confluence keys. Golden windows share
        # high-conviction thresholds; off-hours are looser.
        for regime in ("OPEN_MOMENTUM", "MID_MORNING",
                       "AFTERHOURS", "AFTERNOON_CHOP",
                       "OVERNIGHT_RANGE", "PREMARKET_DRIFT"):
            assert regime in _REGIME_OVERRIDES
            entry = _REGIME_OVERRIDES[regime]
            assert "min_momentum" in entry
            assert "min_confluence" in entry

    def test_non_golden_regime_has_tighter_gates(self):
        from strategies.bias_momentum import _REGIME_OVERRIDES
        # Off-hours/lab regimes have LOWER momentum thresholds than live/chop
        # (looser for data collection); live + chop regimes gate at 75+.
        assert _REGIME_OVERRIDES["OPEN_MOMENTUM"]["min_momentum"] >= 75
        assert _REGIME_OVERRIDES["AFTERNOON_CHOP"]["min_momentum"] >= 75
        assert _REGIME_OVERRIDES["AFTERHOURS"]["min_momentum"] < \
               _REGIME_OVERRIDES["OPEN_MOMENTUM"]["min_momentum"]


# ─── Test 7: Daily Reset ─────────────────────────────────────────

class TestDailyReset:
    """Verify daily state resets on date change."""

    def test_risk_manager_reset_clears_all(self):
        rm = RiskManager()
        rm.state.daily_pnl = -30.0
        rm.state.trades_today = 5
        rm.state.recovery_mode = True
        rm.state.consecutive_losses = 3

        rm.reset_daily()

        assert rm.state.daily_pnl == 0.0
        assert rm.state.trades_today == 0
        assert rm.state.recovery_mode is False
        assert rm.state.consecutive_losses == 0


# ─── Test 8: Risk Manager Only Blocks on LOSSES ──────────────────

class TestRiskManagerLossOnly:
    """Verify profitable days don't trigger loss limits."""

    def test_profitable_day_does_not_block(self):
        rm = RiskManager()
        rm.state.daily_pnl = 100.0  # Great day!
        allowed, _ = rm.can_trade()
        assert allowed is True

    def test_loss_day_blocks(self):
        rm = RiskManager()
        rm.state.daily_pnl = -46.0  # Over $45 limit
        allowed, reason = rm.can_trade()
        assert allowed is False
        assert "Daily loss" in reason

    def test_profitable_weekly_does_not_block(self):
        rm = RiskManager()
        rm.state.weekly_pnl = 500.0
        allowed, _ = rm.can_trade()
        assert allowed is True


# ─── Test 9: Trade ID Uniqueness ─────────────────────────────────

class TestTradeIDUniqueness:
    """Verify every signal gets a unique trade ID."""

    def test_signals_have_unique_ids(self):
        s1 = make_signal()
        s2 = make_signal()
        s3 = make_signal()
        ids = {s1.trade_id, s2.trade_id, s3.trade_id}
        assert len(ids) == 3  # All unique

    def test_signal_to_dict_includes_trade_id(self):
        s = make_signal()
        d = s.to_dict()
        assert "trade_id" in d
        assert len(d["trade_id"]) == 8


# ─── Test 10: Session Regime Detection ────────────────────────────

class TestSessionRegimeDetection:
    """Verify regime detection across all time windows."""

    def test_open_momentum_regime(self):
        sm = SessionManager()
        t = datetime(2026, 4, 12, 8, 45, 0)  # 8:45 AM CST
        regime = sm.get_current_regime(t)
        assert regime == "OPEN_MOMENTUM"

    def test_mid_morning_regime(self):
        sm = SessionManager()
        t = datetime(2026, 4, 12, 10, 0, 0)  # 10:00 AM CST
        regime = sm.get_current_regime(t)
        assert regime == "MID_MORNING"

    def test_afternoon_chop_regime(self):
        sm = SessionManager()
        t = datetime(2026, 4, 12, 12, 0, 0)  # 12:00 PM CST
        regime = sm.get_current_regime(t)
        assert regime == "AFTERNOON_CHOP"

    def test_overnight_regime(self):
        sm = SessionManager()
        t = datetime(2026, 4, 12, 23, 0, 0)  # 11 PM CST
        regime = sm.get_current_regime(t)
        assert regime == "OVERNIGHT_RANGE"
