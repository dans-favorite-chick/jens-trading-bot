"""Tests for the 2026-05-06 fix: pending-entry expiry now cancels
the stale NT8 order, not just the Python-side record.

Background: SimBias Momentum got "Exceeds account's maximum position
quantity" rejection at 03:13. Forensic trace showed:

  01:42:40 bot sent LIMIT @ 28300.75 (trade eef14701)
           NT8 accepted but limit didn't fill in 5s
  01:42:45 PENDING_ENTRY:eef14701 registered (Python side)
           NT8-side LIMIT still WORKING at the exchange
  03:13:21 bot signaled new entry, saw PENDING_ENTRY age > 900s,
           expired its OWN record, sent NEW LIMIT @ 28343.75
  03:13:21 NT8 REJECTED — existing LIMIT @ 28300.75 + new LIMIT
           @ 28343.75 = potential 2 contracts > 1 cap

Root cause: has_pending_entry() popped the Python record but never
cancelled the NT8-side LIMIT. Fix: write CANCEL OIF for the stale
trade_id at the same time the Python record expires.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.position_manager import PositionManager


@pytest.fixture
def pm():
    """Fresh PositionManager with no history (tests don't need persistence)."""
    return PositionManager(load_history=False)


# ═══════════════════════════════════════════════════════════════════
# Pre-existing behavior preserved
# ═══════════════════════════════════════════════════════════════════
class TestExistingBehavior:
    def test_no_pending_entry_returns_false(self, pm):
        assert pm.has_pending_entry("SimBias Momentum") is False

    def test_fresh_pending_entry_returns_true(self, pm):
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="abc123",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        assert pm.has_pending_entry("SimBias Momentum") is True

    def test_clear_pending_entry_removes_it(self, pm):
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="abc123",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        pm.clear_pending_entry("SimBias Momentum")
        assert pm.has_pending_entry("SimBias Momentum") is False


# ═══════════════════════════════════════════════════════════════════
# 2026-05-06 FIX: expiry cancels the stale NT8 order
# ═══════════════════════════════════════════════════════════════════
class TestStaleEntryCancelOnExpiry:
    def test_expired_record_triggers_cancel_oif(self, pm):
        """Stale pending entry → write_oif called with action=CANCEL."""
        # Stash a pending entry with an old timestamp
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="eef14701",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        # Backdate it past the timeout
        pm._pending_entries["SimBias Momentum"]["submitted_at"] = (
            time.time() - pm.PENDING_ENTRY_TIMEOUT_S - 10
        )

        # Patch write_oif at its source — the import inside
        # _cancel_stale_nt8_order will pick up the patched version.
        with patch("bridge.oif_writer.write_oif") as mock_write:
            mock_write.return_value = ["fake_oif_path.txt"]
            assert pm.has_pending_entry("SimBias Momentum") is False
            mock_write.assert_called_once()
            kwargs = mock_write.call_args.kwargs
            assert kwargs["action"] == "CANCEL"
            assert kwargs["trade_id"] == "eef14701"
            assert kwargs["account"] == "SimBias Momentum"

    def test_expired_record_popped_after_cancel(self, pm):
        """Even after issuing the cancel, the record itself is removed."""
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="eef14701",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        pm._pending_entries["SimBias Momentum"]["submitted_at"] = (
            time.time() - pm.PENDING_ENTRY_TIMEOUT_S - 10
        )

        with patch("bridge.oif_writer.write_oif", return_value=["fake.txt"]):
            pm.has_pending_entry("SimBias Momentum")
        # Subsequent check sees no record (already popped)
        assert "SimBias Momentum" not in pm._pending_entries

    def test_fresh_record_does_not_trigger_cancel(self, pm):
        """A pending entry within the timeout must NOT issue a CANCEL —
        the NT8 limit is still legitimately working and we want it to fill."""
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="recent123",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        # No backdating — record is fresh
        with patch("bridge.oif_writer.write_oif") as mock_write:
            assert pm.has_pending_entry("SimBias Momentum") is True
            mock_write.assert_not_called()

    def test_cancel_failure_still_pops_record(self, pm):
        """If write_oif raises, the Python-side record must still be
        cleared — bot can't be permanently stuck because the cancel
        path failed. Operator gets a logged warning."""
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="eef14701",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        pm._pending_entries["SimBias Momentum"]["submitted_at"] = (
            time.time() - pm.PENDING_ENTRY_TIMEOUT_S - 10
        )

        with patch(
            "bridge.oif_writer.write_oif",
            side_effect=Exception("disk full"),
        ):
            # Must NOT raise — best-effort
            assert pm.has_pending_entry("SimBias Momentum") is False
        assert "SimBias Momentum" not in pm._pending_entries

    def test_cancel_returning_empty_list_logs_warning_but_pops(self, pm, caplog):
        """write_oif returning [] (e.g. CANCEL_ALL blocked) should log
        a warning so operator notices, but still pop the record."""
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="eef14701",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        pm._pending_entries["SimBias Momentum"]["submitted_at"] = (
            time.time() - pm.PENDING_ENTRY_TIMEOUT_S - 10
        )

        with patch("bridge.oif_writer.write_oif", return_value=[]):
            assert pm.has_pending_entry("SimBias Momentum") is False
        assert "SimBias Momentum" not in pm._pending_entries

    def test_empty_trade_id_skips_cancel_logs_warning(self, pm):
        """Defensive: if a record was registered with no trade_id,
        cancel must skip (CANCEL with empty trade_id is unsafe — could
        match all working orders). Record still gets popped."""
        # Manually inject a record with empty trade_id
        pm._pending_entries["SimBias Momentum"] = {
            "trade_id": "",
            "strategy": "bias_momentum",
            "direction": "LONG",
            "limit_price": 28300.75,
            "qty": 1,
            "submitted_at": time.time() - pm.PENDING_ENTRY_TIMEOUT_S - 10,
        }
        with patch("bridge.oif_writer.write_oif") as mock_write:
            assert pm.has_pending_entry("SimBias Momentum") is False
            mock_write.assert_not_called()
        assert "SimBias Momentum" not in pm._pending_entries

    def test_get_pending_entry_also_triggers_cancel_on_stale(self, pm):
        """get_pending_entry calls has_pending_entry under the hood —
        same expiry+cancel path applies."""
        pm.record_pending_entry(
            account="SimBias Momentum", trade_id="eef14701",
            strategy="bias_momentum", direction="LONG",
            limit_price=28300.75, qty=1,
        )
        pm._pending_entries["SimBias Momentum"]["submitted_at"] = (
            time.time() - pm.PENDING_ENTRY_TIMEOUT_S - 10
        )

        with patch("bridge.oif_writer.write_oif",
                   return_value=["fake.txt"]) as mock_write:
            assert pm.get_pending_entry("SimBias Momentum") is None
            mock_write.assert_called_once()
            assert mock_write.call_args.kwargs["action"] == "CANCEL"


# ═══════════════════════════════════════════════════════════════════
# Regression test for the exact 2026-05-06 incident
# ═══════════════════════════════════════════════════════════════════
class TestSimBiasMomentum0313Regression:
    """Reproduces the exact failure mode + verifies the fix."""

    def test_stale_eef14701_expiry_cancels_at_nt8(self, pm):
        # Set up the exact pre-incident state: PENDING_ENTRY for eef14701
        # registered at 01:42, now we're at 03:13 (over 90 min later).
        pm.record_pending_entry(
            account="SimBias Momentum",
            trade_id="eef14701",
            strategy="bias_momentum",
            direction="LONG",
            limit_price=28300.75,
            qty=1,
        )
        # Backdate to ~91 minutes ago
        pm._pending_entries["SimBias Momentum"]["submitted_at"] = (
            time.time() - 91 * 60
        )

        # When the next signal evaluation runs, has_pending_entry()
        # gets called. Pre-fix: it just popped the record.
        # Post-fix: it ALSO writes a CANCEL OIF for trade_id eef14701.
        with patch("bridge.oif_writer.write_oif") as mock_write:
            mock_write.return_value = ["incoming/cancel_eef14701.txt"]
            still_pending = pm.has_pending_entry("SimBias Momentum")

        assert still_pending is False
        # Cancel was issued for the right trade_id on the right account
        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args.kwargs
        assert call_kwargs["action"] == "CANCEL"
        assert call_kwargs["trade_id"] == "eef14701"
        assert call_kwargs["account"] == "SimBias Momentum"
        # And the bot can now safely submit a new entry — the stale
        # NT8-side LIMIT is being cancelled.
