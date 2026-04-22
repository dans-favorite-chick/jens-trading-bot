"""
B75 — CANCELALLORDERS is unsafe in Phoenix.

NT8 ATI ignores the account field on CANCELALLORDERS and cancels every
pending order on every connected account. This caused the 2026-04-22
orphan-long incidents: prod_bot's Sim101 exit flow wiped sim_bot's
OCO protection on SimSpring Setup + SimNoise Area.

Fix: write_oif("CANCEL_ALL") returns [] with an error log and does
NOT emit CANCELALLORDERS. Exit paths rely on NT8 OCO auto-cancel —
when EXIT MARKET flattens the position, NT8 auto-cancels the
orphaned stop/target legs.

This test locks in the behavior so no future regression can re-enable
the cross-account nuke.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCancelAllBlocked(unittest.TestCase):
    def setUp(self):
        # Ensure LIVE_ACCOUNT is unset so the B59 guard doesn't preempt.
        self._env = patch.dict(os.environ, {"LIVE_ACCOUNT": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_write_oif_cancel_all_returns_empty_and_writes_no_file(self):
        """B75: write_oif('CANCEL_ALL', ...) must refuse and return []."""
        from bridge.oif_writer import write_oif
        with tempfile.TemporaryDirectory() as tmp:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmp
            try:
                paths = write_oif(
                    "CANCEL_ALL", qty=0,
                    trade_id="test_b75",
                    account="SimBias Momentum",
                )
                # No OIF files written
                self.assertEqual(paths, [])
                files_in_tmp = os.listdir(tmp)
                self.assertEqual(
                    files_in_tmp, [],
                    f"write_oif(CANCEL_ALL) must NOT write files — got {files_in_tmp}"
                )
            finally:
                oif.OIF_INCOMING = _orig

    def test_exit_path_still_works(self):
        """Sanity: EXIT action still writes a CLOSEPOSITION OIF."""
        from bridge.oif_writer import write_oif
        with tempfile.TemporaryDirectory() as tmp:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmp
            try:
                paths = write_oif(
                    "EXIT", qty=1,
                    trade_id="test_b75_exit",
                    account="SimBias Momentum",
                )
                # CLOSEPOSITION OIF should be written
                self.assertEqual(len(paths), 1)
                content = open(paths[0]).read().strip()
                self.assertTrue(content.startswith("CLOSEPOSITION;SimBias Momentum"),
                                f"EXIT should emit CLOSEPOSITION, got: {content}")
            finally:
                oif.OIF_INCOMING = _orig

    def test_cancel_all_orders_line_still_raises_on_missing_account(self):
        """B58: explicit None account must still raise (belt-and-suspenders)."""
        from bridge.oif_writer import cancel_all_orders_line
        with self.assertRaises(ValueError):
            cancel_all_orders_line(account=None)

    def test_cancel_all_orders_line_logs_deprecation_when_called_directly(self):
        """B75: direct calls to cancel_all_orders_line emit ERROR log."""
        from bridge.oif_writer import cancel_all_orders_line
        with self.assertLogs("OIF", level="ERROR") as captured:
            line = cancel_all_orders_line(account="SimBias Momentum")
        # Deprecation log captured
        msgs = " ".join(captured.output)
        self.assertIn("CANCELALLORDERS_DEPRECATED", msgs)
        # Line still well-formed (in case NT8's own code paths need it)
        self.assertTrue(line.startswith("CANCELALLORDERS;SimBias Momentum;"))


if __name__ == "__main__":
    unittest.main()
