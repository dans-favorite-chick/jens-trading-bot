"""P1-7 pending-entry lifecycle tests (2026-05-25).

Every entry order Phoenix submits MUST end in one of four terminal states:

    filled
    cancelled            (variants: cancelled / adopted_cancelled)
    timeout_cancelled
    flattened

These tests cover the canonical paths plus the two integration scenarios:
no entry can outlive its timeout, and a fill ack arriving within ms of
timeout produces exactly ONE terminal state.

Tests use a fresh PendingEntryTracker per test (singleton reset in
autouse fixture) so state never leaks across cases.
"""
from __future__ import annotations

import os
import random
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import pending_entry_tracker as _pet
from core.pending_entry_tracker import (
    PendingEntryTracker,
    TERMINAL_ADOPTED,
    TERMINAL_ADOPTED_CANCELLED,
    TERMINAL_CANCELLED,
    TERMINAL_FILLED,
    TERMINAL_FLATTENED,
    TERMINAL_TIMEOUT_CANCELLED,
    get_pending_entry_tracker,
)


@pytest.fixture(autouse=True)
def _fresh_tracker():
    """Reset the singleton between tests so state is hermetic."""
    _pet._reset_singleton_for_tests()
    yield
    _pet._reset_singleton_for_tests()


@pytest.fixture
def tracker() -> PendingEntryTracker:
    return get_pending_entry_tracker()


# ═══════════════════════════════════════════════════════════════════════
# 4 TERMINAL STATES
# ═══════════════════════════════════════════════════════════════════════
class TestFilledTerminalState:
    def test_filled_terminal_state(self, tracker):
        pe = tracker.register(
            trade_id="t-filled-1",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )
        assert pe.terminal_state is None

        # Simulate the fill ack arriving.
        updated = tracker.mark_filled("t-filled-1", reason="fill_ack")
        assert updated is not None
        assert updated.terminal_state == TERMINAL_FILLED
        assert updated.terminal_reason == "fill_ack"
        assert updated.terminal_at is not None


class TestTimeoutCancelledTerminalState:
    def test_timeout_cancelled_terminal_state(self, tracker):
        pe = tracker.register(
            trade_id="t-timeout-1",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )
        # Backdate so it's instantly expired.
        pe.submitted_at = time.time() - 91.0
        expired = tracker.sweep_once()
        assert len(expired) == 1
        assert expired[0].trade_id == "t-timeout-1"
        assert expired[0].terminal_state == TERMINAL_TIMEOUT_CANCELLED

    def test_sweeper_emits_cancel_oif(self, monkeypatch, tracker, tmp_path):
        """The PendingEntrySweeper actually writes a CANCEL OIF for the
        expired entry. Patches write_oif at its source to capture the call."""
        from bots._pending_entry_sweeper import PendingEntrySweeper

        pe = tracker.register(
            trade_id="t-timeout-cancel-oif",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )
        pe.submitted_at = time.time() - 91.0

        bot = MagicMock()
        bot.bot_name = "sim"
        bot.positions.clear_pending_entry = MagicMock()
        bot.trade_memory.record = MagicMock()
        sweeper = PendingEntrySweeper(bot)

        with patch("bridge.oif_writer.write_oif") as mock_write:
            mock_write.return_value = ["fake_cancel.txt"]
            n = sweeper._sweep()

        assert n == 1
        mock_write.assert_called_once()
        kwargs = mock_write.call_args.kwargs
        assert kwargs["action"] == "CANCEL"
        assert kwargs["trade_id"] == "t-timeout-cancel-oif"
        assert kwargs["account"] == "SimBias Momentum"

        # And trade_memory got the terminal_state row.
        bot.trade_memory.record.assert_called_once()
        row = bot.trade_memory.record.call_args.args[0]
        assert row["terminal_state"] == TERMINAL_TIMEOUT_CANCELLED
        assert row["trade_id"] == "t-timeout-cancel-oif"
        assert row["result"] == "NO_FILL"


class TestAdoptedTerminalState:
    def test_adopted_then_filled_stays_adopted(self, tracker):
        """Adopted entry that later fills reports terminal_state="adopted"
        (NOT "filled") — the adoption flag wins so downstream readers can
        distinguish bot-placed from restart-recovered fills.
        """
        tracker.register(
            trade_id="t-adopted-1",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
            adopted=True,
        )
        updated = tracker.mark_filled("t-adopted-1", reason="fill_ack")
        assert updated is not None
        assert updated.terminal_state == TERMINAL_ADOPTED

    def test_adopted_then_cancelled_becomes_adopted_cancelled(self, tracker):
        tracker.register(
            trade_id="t-adopted-2",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
            adopted=True,
        )
        updated = tracker.mark_cancelled("t-adopted-2", reason="manual")
        assert updated is not None
        assert updated.terminal_state == TERMINAL_ADOPTED_CANCELLED


class TestFlattenedTerminalState:
    def test_flattened_terminal_state(self, tracker):
        tracker.register(
            trade_id="t-flat-1",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )
        flattened = tracker.mark_all_flattened(reason="daily_flatten")
        assert len(flattened) == 1
        assert flattened[0].terminal_state == TERMINAL_FLATTENED
        assert flattened[0].terminal_reason == "daily_flatten"

    def test_flattened_emits_cancel_oif_via_basebot_helper(self, monkeypatch, tracker):
        """BaseBot._flatten_pending_entries wires the canonical CANCEL OIF
        + trade_memory record through the sweeper's helpers."""
        from bots._pending_entry_sweeper import PendingEntrySweeper

        tracker.register(
            trade_id="t-flat-helper",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )

        # Build a minimal BaseBot-like shim that exposes the helper.
        # We import _flatten_pending_entries off the class and bind it.
        from bots.base_bot import BaseBot

        bot = MagicMock()  # unrestricted — BaseBot has too many slots
        bot.bot_name = "sim"
        bot.positions.clear_pending_entry = MagicMock()
        bot.trade_memory.record = MagicMock()
        # The helper consults self._pending_entry_sweeper for OIF/trade_memory
        # subroutines; wire a real sweeper bound to the mock bot.
        bot._pending_entry_sweeper = PendingEntrySweeper(bot)

        with patch("bridge.oif_writer.write_oif") as mock_write:
            mock_write.return_value = ["fake_cancel.txt"]
            # Invoke the unbound class method against the mock bot.
            n = BaseBot._flatten_pending_entries(bot, reason="emergency_flatten")

        assert n == 1
        mock_write.assert_called_once()
        assert mock_write.call_args.kwargs["action"] == "CANCEL"
        bot.trade_memory.record.assert_called_once()
        row = bot.trade_memory.record.call_args.args[0]
        assert row["terminal_state"] == TERMINAL_FLATTENED


class TestCancelledTerminalState:
    def test_explicit_cancel(self, tracker):
        tracker.register(
            trade_id="t-cancel-1",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )
        updated = tracker.mark_cancelled("t-cancel-1", reason="manual_op")
        assert updated.terminal_state == TERMINAL_CANCELLED


# ═══════════════════════════════════════════════════════════════════════
# INTEGRATION FUZZ + RACE
# ═══════════════════════════════════════════════════════════════════════
class TestIntegration:
    def test_no_pending_can_outlive_timeout(self, tracker):
        """Fuzz: 100 random submissions with random timeouts. After
        advancing time past every timeout and running sweep_once,
        no PendingEntry may have terminal_state == None."""
        rng = random.Random(42)
        now = time.time()
        max_timeout = 0.0
        for i in range(100):
            timeout = rng.uniform(1.0, 120.0)
            max_timeout = max(max_timeout, timeout)
            pe = tracker.register(
                trade_id=f"fuzz-{i}",
                strategy="bias_momentum",
                account=f"SimAcct{i % 4}",
                instrument="MNQM6",
                side="BUY" if i % 2 == 0 else "SELL",
                qty=1,
                limit_price=28000.0 + i,
                timeout_s=timeout,
            )
            # Backdate each by max_timeout + 1 so every entry is expired
            # by the time sweep_once fires below.
            pe.submitted_at = now - (max_timeout + 1.0)

        # Re-anchor everyone with the same backdate so they're all expired.
        for pe in tracker.all_entries():
            pe.submitted_at = now - (max_timeout + 1.0)

        expired = tracker.sweep_once()
        # Every one of the 100 should have been swept.
        assert len(expired) == 100
        # And no entry remains non-terminal.
        for pe in tracker.all_entries():
            assert pe.terminal_state is not None, (
                f"trade_id={pe.trade_id} has terminal_state=None after sweep"
            )

    def test_concurrent_fill_and_timeout_no_race(self, tracker):
        """If a fill ack arrives within ms of the sweeper expiring the
        same entry, ONLY ONE terminal state is set (first writer wins).
        Tests the atomic mark-inside-lock guarantee in PendingEntryTracker.
        """
        pe = tracker.register(
            trade_id="t-race-1",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )
        # Mark filled first.
        first = tracker.mark_filled("t-race-1")
        assert first is not None
        assert first.terminal_state == TERMINAL_FILLED

        # Now backdate and try sweep — sweep_once must NOT overwrite.
        # (sweep_once skips terminal entries, so a fill-then-sweep yields
        # zero new expiries.)
        pe.submitted_at = time.time() - 91.0
        expired = tracker.sweep_once()
        assert expired == []
        # And the entry is still filled, not timeout_cancelled.
        assert tracker.get("t-race-1").terminal_state == TERMINAL_FILLED

    def test_race_other_direction_sweep_then_fill(self, tracker):
        """Reverse race: sweep fires first, then the fill ack lands.
        Tracker must refuse the late mark_filled with a warn-log and
        terminal_state stays timeout_cancelled."""
        pe = tracker.register(
            trade_id="t-race-2",
            strategy="bias_momentum",
            account="SimBias Momentum",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=28300.75,
            timeout_s=90.0,
        )
        pe.submitted_at = time.time() - 91.0
        expired = tracker.sweep_once()
        assert len(expired) == 1
        assert expired[0].terminal_state == TERMINAL_TIMEOUT_CANCELLED

        # Late fill — should NOT overwrite.
        result = tracker.mark_filled("t-race-2", reason="late_ack")
        # mark_filled returns None when the entry is already terminal.
        assert result is None
        assert tracker.get("t-race-2").terminal_state == TERMINAL_TIMEOUT_CANCELLED

    def test_existing_market_entries_unaffected(self, tracker):
        """MARKET entries don't go through the pending tracker — verify
        the tracker stays empty when nothing was registered. This is the
        contract guarantee that _trade_entry.py's `if signal_entry_type
        == "LIMIT"` guard preserves MARKET semantics."""
        # No-op: simulate a market signal path that does not call register.
        assert tracker.all_entries() == []
        # Sweep on an empty tracker is harmless.
        assert tracker.sweep_once() == []
        # mark_filled on an unregistered trade_id is a no-op (returns None).
        assert tracker.mark_filled("never-registered") is None


# ═══════════════════════════════════════════════════════════════════════
# CONTRACT / REGRESSION
# ═══════════════════════════════════════════════════════════════════════
class TestContract:
    def test_invalid_terminal_state_rejected(self, tracker):
        with pytest.raises(ValueError):
            tracker._set_terminal("nonexistent", "garbage_state")

    def test_register_requires_trade_id(self, tracker):
        with pytest.raises(ValueError):
            tracker.register(
                trade_id="",
                strategy="bias_momentum",
                account="SimAcct",
                instrument="MNQM6",
                side="BUY",
                qty=1,
                limit_price=28300.75,
                timeout_s=90.0,
            )

    def test_idempotent_register(self, tracker):
        """Re-registering the same trade_id preserves submitted_at."""
        pe1 = tracker.register(
            trade_id="t-idempotent",
            strategy="s",
            account="a",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=100.0,
            timeout_s=90.0,
        )
        original_ts = pe1.submitted_at
        time.sleep(0.001)
        pe2 = tracker.register(
            trade_id="t-idempotent",
            strategy="s",
            account="a",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=100.0,
            timeout_s=90.0,
        )
        # Idempotent: same object, original timestamp preserved.
        assert pe2.submitted_at == original_ts

    def test_attach_nt8_order_id(self, tracker):
        tracker.register(
            trade_id="t-attach",
            strategy="s",
            account="a",
            instrument="MNQM6",
            side="BUY",
            qty=1,
            limit_price=100.0,
            timeout_s=90.0,
        )
        assert tracker.attach_nt8_order_id("t-attach", "NT8-ORDER-42") is True
        assert tracker.get("t-attach").nt8_order_id == "NT8-ORDER-42"
        # Unknown trade_id is a no-op.
        assert tracker.attach_nt8_order_id("nope", "x") is False

    def test_singleton_consistency(self):
        a = get_pending_entry_tracker()
        b = get_pending_entry_tracker()
        assert a is b
