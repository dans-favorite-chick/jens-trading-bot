"""P0.6 (2026-05-08) — write_modify_stop must wait for NT8 acceptance
before cancelling the old stop.

Background: overnight 2026-05-07/08, trade `114c7222` on SimBias Momentum
trailed its stop from 28768.50 to 28795.38. The OIF flow committed the
new stop and the cancel-old-stop in the same microsecond. NT8's ATI
consumed both files (so verify_consumed passed) but the new stop was
REJECTED by NT8's account-level max-position-quantity guard. The cancel
of the old stop went through anyway, leaving the position with NO
active protective stop while Phoenix logged STOP_MOVED as success.

The fix: after committing the new stop, wait for outgoing/ to publish a
WORKING-file confirming NT8 accepted the order. Only then commit the
cancel. If no WORKING file appears within timeout, return early
(cancel skipped) and raise a Telegram alert; the OLD stop stays active.

This file exercises the NT8 rejection path with the bypass flag flipped
back to False. The default-True bypass in conftest keeps existing tests
(scale_out_no_race, be_move) on the legacy two-file commit semantics.
"""
from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bridge.oif_writer as oif


@pytest.fixture
def acceptance_check_enabled(monkeypatch):
    """Flip bypass off for tests in this file so the verification gate runs."""
    monkeypatch.setattr(
        oif, "_PYTEST_BYPASS_NT8_ACCEPTANCE_CHECK", False, raising=False,
    )
    yield


class TestNT8RejectionPreservesOldStop:
    def test_no_working_file_returns_only_new_stop(
        self, acceptance_check_enabled, monkeypatch,
    ):
        """When NT8 never publishes a WORKING file, cancel must be skipped."""
        # Force scan_outgoing_for_order_id to time out fast and return None
        # (simulates NT8 rejecting the new stop with no WORKING ack).
        monkeypatch.setattr(
            oif, "scan_outgoing_for_order_id",
            lambda account, expected_price, tolerance=0.01, timeout_s=2.5: None,
        )

        paths = oif.write_modify_stop(
            direction="LONG",
            new_stop_price=28795.38,
            n_contracts=1,
            trade_id="reject_test",
            account="Sim101",
            old_stop_order_id="oif_old_xyz",
        )

        assert len(paths) == 1, (
            "When NT8 rejects the new stop, ONLY the new-stop OIF should be "
            "written. The cancel of the old stop must be skipped to preserve "
            "protection."
        )
        assert "stop_replace" in os.path.basename(paths[0])
        # No stop_cancel file must exist on disk anywhere — that's the bug.
        for fname in os.listdir(os.path.dirname(paths[0])):
            assert "stop_cancel" not in fname, (
                f"P0.6 violated: stop_cancel file was written despite NT8 "
                f"rejection. File: {fname}. This re-introduces the unprotected "
                f"position bug from 2026-05-08."
            )

    def test_working_file_present_commits_both(
        self, acceptance_check_enabled, monkeypatch,
    ):
        """When NT8 publishes a WORKING file, both new+cancel must commit."""
        monkeypatch.setattr(
            oif, "scan_outgoing_for_order_id",
            lambda account, expected_price, tolerance=0.01, timeout_s=2.5: "FAKE_NEW_OID_abcdef",
        )

        paths = oif.write_modify_stop(
            direction="LONG",
            new_stop_price=28795.38,
            n_contracts=1,
            trade_id="accept_test",
            account="Sim101",
            old_stop_order_id="oif_old_xyz",
        )

        assert len(paths) == 2, (
            "When NT8 acknowledges the new stop, both files must commit."
        )
        names = [os.path.basename(p) for p in paths]
        assert any("stop_replace" in n for n in names)
        assert any("stop_cancel" in n for n in names)


class TestBypassFlagDefault:
    def test_conftest_sets_bypass_true_by_default(self):
        """Sanity check: the autouse fixture must set bypass = True so
        existing tests don't have to mock scan_outgoing_for_order_id."""
        assert oif._PYTEST_BYPASS_NT8_ACCEPTANCE_CHECK is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
