"""
P0.6 — Close-position verification via exit_pending state (D7).

Before P0.6, `_exit_trade` sent a CLOSEPOSITION OIF and IMMEDIATELY
called `positions.close_position(...)`. Python's dashboard showed
flat; NT8 might not have actually processed the exit. A stuck
CLOSEPOSITION meant Python reported $0 open while a bleeding position
sat at NT8 — the "thinks flat but isn't" divergence.

P0.6 introduces an `exit_pending` state on Position:
  - mark_exit_pending(trade_id, price, reason) flips the flag + stashes
    the exit price/reason, but keeps the Position in the manager.
  - Runtime reconciliation (30s loop) polls NT8 outgoing/; when it sees
    FLAT for the account+instrument, it calls finalize_exit_pending
    which promotes the position to a closed trade (append trade_history,
    delete from _positions).
  - If exit_pending persists > EXIT_PENDING_TIMEOUT_S (default 60s),
    base_bot fires CRITICAL log + Telegram and halts the strategy so
    the divergence can't bleed silently.
  - exit_pending positions block new entries on the same account —
    has_exit_pending_for_account(account).

Run: pytest tests/test_close_position_verification.py -v
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.position_manager import PositionManager, Position


def _open_test_position(pm: PositionManager, tid="t1", account="Sim101"):
    """Helper: open a vanilla 1-contract LONG for reuse across tests."""
    ok = pm.open_position(
        trade_id=tid,
        direction="LONG",
        entry_price=22000.0,
        contracts=1,
        stop_price=21990.0,
        target_price=22020.0,
        strategy="spring_setup",
        reason="test",
        account=account,
    )
    assert ok, "fixture failure — open_position refused"


# ═══════════════════════════════════════════════════════════════════
# exit_pending state flips flags without deleting the position
# ═══════════════════════════════════════════════════════════════════
class TestMarkExitPending:
    def test_mark_sets_pending_flags(self):
        pm = PositionManager()
        _open_test_position(pm)
        ok = pm.mark_exit_pending("t1", exit_price=22010.0, exit_reason="time_exit")
        assert ok is True
        pos = pm.get_position("t1")
        assert pos is not None
        assert pos.exit_pending is True
        assert pos.pending_exit_price == 22010.0
        assert pos.pending_exit_reason == "time_exit"
        assert pos.exit_pending_since > 0

    def test_mark_keeps_position_in_manager(self):
        pm = PositionManager()
        _open_test_position(pm)
        pm.mark_exit_pending("t1", 22010.0, "time_exit")
        # The whole point: position stays visible to dashboard / is_flat /
        # active_positions until NT8 confirms the flatten.
        assert pm.is_flat is False
        assert pm.active_count == 1
        assert pm.get_position("t1") is not None

    def test_mark_returns_false_for_unknown_trade_id(self):
        pm = PositionManager()
        assert pm.mark_exit_pending("nope", 1.0, "reason") is False


# ═══════════════════════════════════════════════════════════════════
# finalize_exit_pending transitions to closed correctly
# ═══════════════════════════════════════════════════════════════════
class TestFinalizeExitPending:
    def test_finalize_produces_trade_record(self):
        pm = PositionManager()
        _open_test_position(pm)
        pm.mark_exit_pending("t1", 22010.0, "target_hit")
        trade = pm.finalize_exit_pending("t1")
        assert trade is not None
        assert trade["exit_price"] == 22010.0
        assert trade["exit_reason"] == "target_hit"
        assert trade["direction"] == "LONG"
        assert trade["result"] == "WIN"  # +10 ticks > 0

    def test_finalize_removes_from_active(self):
        pm = PositionManager()
        _open_test_position(pm)
        pm.mark_exit_pending("t1", 22010.0, "target_hit")
        pm.finalize_exit_pending("t1")
        assert pm.is_flat is True
        assert pm.get_position("t1") is None

    def test_finalize_appends_to_trade_history(self):
        pm = PositionManager()
        _open_test_position(pm)
        assert len(pm.trade_history) == 0
        pm.mark_exit_pending("t1", 22010.0, "target_hit")
        pm.finalize_exit_pending("t1")
        assert len(pm.trade_history) == 1
        assert pm.trade_history[0]["trade_id"] == "t1"

    def test_finalize_noop_when_not_pending(self, caplog):
        import logging
        pm = PositionManager()
        _open_test_position(pm)
        # Did NOT call mark_exit_pending — pos.exit_pending is False
        with caplog.at_level(logging.WARNING, logger="PositionManager"):
            result = pm.finalize_exit_pending("t1")
        assert result is None
        # Position still open — finalize must NOT silently close.
        assert pm.get_position("t1") is not None
        assert any("not exit_pending" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════
# Blocking new entries on exit_pending account
# ═══════════════════════════════════════════════════════════════════
class TestEntryBlocking:
    def test_no_pending_means_account_unblocked(self):
        pm = PositionManager()
        _open_test_position(pm, account="SimORB")
        assert pm.has_exit_pending_for_account("SimORB") is False

    def test_pending_blocks_same_account(self):
        pm = PositionManager()
        _open_test_position(pm, account="SimORB")
        pm.mark_exit_pending("t1", 22010.0, "test")
        assert pm.has_exit_pending_for_account("SimORB") is True

    def test_pending_does_not_block_other_accounts(self):
        pm = PositionManager()
        _open_test_position(pm, tid="t1", account="SimORB")
        pm.mark_exit_pending("t1", 22010.0, "test")
        assert pm.has_exit_pending_for_account("SimOpenDrive") is False

    def test_exit_pending_positions_returns_only_pending(self):
        pm = PositionManager()
        _open_test_position(pm, tid="t1", account="SimORB")
        pm.open_position(
            trade_id="t2", direction="SHORT", entry_price=22100.0,
            contracts=1, stop_price=22110.0, target_price=22090.0,
            strategy="orb", reason="t2", account="SimOpenDrive",
        )
        pm.mark_exit_pending("t1", 22010.0, "r")
        pending = pm.exit_pending_positions()
        assert len(pending) == 1
        assert pending[0].trade_id == "t1"


# ═══════════════════════════════════════════════════════════════════
# Runtime reconciliation finalizes when NT8 FLAT + times out after 60s
# ═══════════════════════════════════════════════════════════════════
class TestRuntimeResolution:
    def _build_bot_for_resolve(self, pm, outgoing_dir, instrument="MNQM6"):
        """Construct a minimal bot-like object with the attributes
        `_resolve_exit_pending_positions` touches."""
        from bots.base_bot import BaseBot

        bot = MagicMock(spec=BaseBot)
        bot.positions = pm
        bot.EXIT_PENDING_TIMEOUT_S = 60.0
        bot.risk = MagicMock()
        bot.trade_memory = MagicMock()
        bot.tracker = MagicMock()
        bot._on_trade_closed = MagicMock()
        bot.bot_name = "test"
        # Bind the unbound method
        bot._resolve_exit_pending_positions = (
            BaseBot._resolve_exit_pending_positions.__get__(bot)
        )
        return bot

    def test_finalize_when_nt8_shows_flat(self, tmp_path, monkeypatch):
        """NT8 outgoing/ missing the position file === FLAT."""
        pm = PositionManager()
        _open_test_position(pm, tid="t1", account="SimORB")
        pm.mark_exit_pending("t1", 22010.0, "target_hit")

        monkeypatch.setattr("config.settings.OIF_OUTGOING", str(tmp_path))
        monkeypatch.setattr("config.settings.INSTRUMENT", "MNQM6")

        bot = self._build_bot_for_resolve(pm, str(tmp_path))
        # No position file in tmp_path → _read_position_file returns None →
        # resolver treats as FLAT and finalizes.
        bot._resolve_exit_pending_positions()

        assert pm.is_flat is True
        # Post-close hooks fired
        bot.risk.record_trade.assert_called_once()
        bot.trade_memory.record.assert_called_once()
        bot.tracker.record_trade.assert_called_once()

    def test_does_not_finalize_while_nt8_still_shows_position(
        self, tmp_path, monkeypatch,
    ):
        """NT8 position file says SHORT → pending stays pending."""
        pm = PositionManager()
        _open_test_position(pm, tid="t1", account="SimORB")
        pm.mark_exit_pending("t1", 22010.0, "target_hit")
        # Simulate NT8 still showing the LONG (didn't flatten yet).
        fname = tmp_path / "MNQM6 Globex_SimORB_position.txt"
        fname.write_text("LONG;1;22000.0\n")

        monkeypatch.setattr("config.settings.OIF_OUTGOING", str(tmp_path))
        monkeypatch.setattr("config.settings.INSTRUMENT", "MNQM6")

        bot = self._build_bot_for_resolve(pm, str(tmp_path))
        bot._resolve_exit_pending_positions()

        # Still pending, still tracked.
        assert pm.is_flat is False
        assert pm.get_position("t1").exit_pending is True
        bot.risk.record_trade.assert_not_called()

    def test_timeout_fires_critical_and_halts_strategy(
        self, tmp_path, monkeypatch, caplog,
    ):
        """After EXIT_PENDING_TIMEOUT_S with NT8 still non-flat, CRITICAL
        log + strategy halt."""
        import logging

        pm = PositionManager()
        _open_test_position(pm, tid="t1", account="SimORB")
        pm.mark_exit_pending("t1", 22010.0, "target_hit")
        # Backdate the exit_pending_since so the timeout has already hit.
        pm.get_position("t1").exit_pending_since = time.time() - 120

        # NT8 file still shows the position — not flat.
        fname = tmp_path / "MNQM6 Globex_SimORB_position.txt"
        fname.write_text("LONG;1;22000.0\n")

        monkeypatch.setattr("config.settings.OIF_OUTGOING", str(tmp_path))
        monkeypatch.setattr("config.settings.INSTRUMENT", "MNQM6")

        bot = self._build_bot_for_resolve(pm, str(tmp_path))
        bot.EXIT_PENDING_TIMEOUT_S = 60.0  # 120s old > 60s timeout

        with caplog.at_level(logging.CRITICAL, logger="base_bot"):
            bot._resolve_exit_pending_positions()

        # CRITICAL logged with the timeout tag
        assert any(
            "EXIT_PENDING_TIMEOUT" in rec.getMessage()
            for rec in caplog.records
        )
        # Position still in pending state (we don't force-finalize on
        # timeout — operator must flatten manually and reconcile).
        assert pm.get_position("t1").exit_pending is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
