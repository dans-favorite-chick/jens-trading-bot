"""
P5b — OIF writer correctness tests (B2 + B3 + B4 + B5).

Covers the verification-sprint bugs fixed in commit `fix(P5b)`:
- B2: CANCEL_ALL semicolon count 15 → 13
- B3: STOP → STOPMARKET at all stop-loss order-type sites
- B4: CANCELALLORDERS account-scoped, empty raises ValueError
- B5: cancel_single_order_line produces valid NT8 OIF

Run: python -m pytest tools/verification_2026_04_18/test_p5b_oif_correctness.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestB3StopMarket(unittest.TestCase):
    """B3 — every stop-loss path must emit STOPMARKET, not bare STOP."""

    def test_build_stop_line_long_side_uses_stopmarket(self):
        from bridge.oif_writer import _build_stop_line
        line = _build_stop_line("SELL", 1, 22000.0, account="Sim101")
        self.assertIn("STOPMARKET", line)
        # Must NOT be a bare STOP token
        parts = line.split(";")
        self.assertIn("STOPMARKET", parts)
        self.assertNotIn("STOP", [p for p in parts if p == "STOP"])

    def test_build_stop_line_short_side_uses_stopmarket(self):
        from bridge.oif_writer import _build_stop_line
        line = _build_stop_line("BUY", 1, 22050.0, account="Sim101")
        self.assertIn("STOPMARKET", line)
        self.assertIn("22050.00", line)

    def test_build_entry_line_stopmarket_path(self):
        """entry_type='STOPMARKET' (ORB breakout entry) emits STOPMARKET, not STOP."""
        from bridge.oif_writer import _build_entry_line
        line = _build_entry_line("BUY", 1, "STOPMARKET",
                                 limit_price=0.0, stop_price=22030.25,
                                 account="Sim101")
        parts = line.split(";")
        self.assertIn("STOPMARKET", parts)
        self.assertNotIn("STOP", [p for p in parts if p == "STOP"])

    def test_build_entry_line_limit_path_no_stop_leakage(self):
        """LIMIT entry must never contain STOP or STOPMARKET token."""
        from bridge.oif_writer import _build_entry_line
        line = _build_entry_line("BUY", 1, "LIMIT",
                                 limit_price=22000.0, stop_price=0.0,
                                 account="Sim101")
        parts = line.split(";")
        self.assertIn("LIMIT", parts)
        self.assertNotIn("STOPMARKET", parts)
        self.assertNotIn("STOP", parts)

    def test_build_entry_line_market_path_no_stop_leakage(self):
        from bridge.oif_writer import _build_entry_line
        line = _build_entry_line("BUY", 1, "MARKET",
                                 limit_price=0.0, stop_price=0.0,
                                 account="Sim101")
        parts = line.split(";")
        self.assertIn("MARKET", parts)
        self.assertNotIn("STOPMARKET", parts)
        self.assertNotIn("STOP", parts)

    def test_place_stop_sell_action_routes_to_stopmarket(self):
        """write_oif(PLACE_STOP_SELL) emits STOPMARKET (not bare STOP)."""
        tmpdir = tempfile.mkdtemp()
        try:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmpdir
            try:
                paths = oif.write_oif(
                    "PLACE_STOP_SELL", qty=1, stop_price=21950.0,
                    trade_id="b3_sell", account="Sim101",
                )
                self.assertEqual(len(paths), 1)
                content = open(paths[0]).read()
                self.assertIn("STOPMARKET", content)
                self.assertIn("21950.00", content)
            finally:
                oif.OIF_INCOMING = _orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_place_stop_buy_action_routes_to_stopmarket(self):
        tmpdir = tempfile.mkdtemp()
        try:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmpdir
            try:
                paths = oif.write_oif(
                    "PLACE_STOP_BUY", qty=1, stop_price=22050.0,
                    trade_id="b3_buy", account="Sim101",
                )
                self.assertEqual(len(paths), 1)
                content = open(paths[0]).read()
                self.assertIn("STOPMARKET", content)
                self.assertIn("22050.00", content)
            finally:
                oif.OIF_INCOMING = _orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestB2CancelAllSemicolonCount(unittest.TestCase):
    """B2 — CANCELALLORDERS must have exactly 13 semicolons per NT8 ATI spec."""

    def test_cancel_all_orders_line_has_13_semicolons(self):
        from bridge.oif_writer import cancel_all_orders_line
        line = cancel_all_orders_line("Sim101")
        self.assertEqual(line.count(";"), 13)

    def test_cancel_all_orders_line_starts_with_command(self):
        from bridge.oif_writer import cancel_all_orders_line
        line = cancel_all_orders_line("Sim101")
        self.assertTrue(line.startswith("CANCELALLORDERS;"))

    def test_cancel_all_action_path_uses_13_semi_form(self):
        """write_oif(CANCEL_ALL) writes a 13-semi line to disk."""
        tmpdir = tempfile.mkdtemp()
        try:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmpdir
            try:
                paths = oif.write_oif("CANCEL_ALL")
                self.assertEqual(len(paths), 1)
                content = open(paths[0]).read().rstrip("\n")
                self.assertEqual(content.count(";"), 13)
                # Old broken form had INSTRUMENT field populated
                self.assertNotIn("MNQM6", content)
            finally:
                oif.OIF_INCOMING = _orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestB4CancelAllAccountScoping(unittest.TestCase):
    """B4 — CANCELALLORDERS must carry account name; empty raises ValueError.

    Background: NT8's no-args CANCELALLORDERS cancels across EVERY connected
    account. Test_05 on 2026-04-19 verified this against a real brokerage
    account. The account field is load-bearing for safety.
    """

    def test_cancel_all_orders_line_contains_account_name(self):
        from bridge.oif_writer import cancel_all_orders_line
        line = cancel_all_orders_line("Sim101")
        self.assertIn("Sim101", line)

    def test_cancel_all_orders_line_empty_account_raises(self):
        from bridge.oif_writer import cancel_all_orders_line
        with self.assertRaises(ValueError):
            cancel_all_orders_line("")

    def test_cancel_all_orders_line_whitespace_account_raises(self):
        from bridge.oif_writer import cancel_all_orders_line
        with self.assertRaises(ValueError):
            cancel_all_orders_line("   ")

    def test_cancel_all_orders_line_default_uses_configured_account(self):
        from bridge.oif_writer import cancel_all_orders_line
        from config.settings import ACCOUNT
        line = cancel_all_orders_line()  # No account → use settings default
        self.assertIn(ACCOUNT, line)

    def test_cancel_all_action_never_emits_no_args_form(self):
        """The no-args form `CANCELALLORDERS;;;;;;;;;;;;;` is safety-critical to avoid."""
        tmpdir = tempfile.mkdtemp()
        try:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmpdir
            try:
                paths = oif.write_oif("CANCEL_ALL")
                content = open(paths[0]).read().rstrip("\n")
                # Must have non-empty first field after CANCELALLORDERS
                fields = content.split(";")
                self.assertEqual(fields[0], "CANCELALLORDERS")
                self.assertNotEqual(fields[1], "", "Account field must NEVER be empty")
            finally:
                oif.OIF_INCOMING = _orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestB5CancelSingleOrder(unittest.TestCase):
    """B5 — new cancel_single_order_line() produces valid NT8 CANCEL OIF."""

    def test_cancel_single_order_line_populates_order_id_at_field_10(self):
        """NT8 ATI spec: ORDER ID is at field position 10."""
        from bridge.oif_writer import cancel_single_order_line
        line = cancel_single_order_line("oif_abc123")
        fields = line.split(";")
        self.assertEqual(fields[0], "CANCEL")
        self.assertEqual(fields[10], "oif_abc123")

    def test_cancel_single_order_line_empty_order_id_raises(self):
        from bridge.oif_writer import cancel_single_order_line
        with self.assertRaises(ValueError):
            cancel_single_order_line("")

    def test_cancel_single_order_line_whitespace_raises(self):
        from bridge.oif_writer import cancel_single_order_line
        with self.assertRaises(ValueError):
            cancel_single_order_line("   ")

    def test_cancel_action_via_write_oif_wires_trade_id_to_order_id(self):
        """write_oif("CANCEL", trade_id=X) emits CANCEL OIF with X at field 10."""
        tmpdir = tempfile.mkdtemp()
        try:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmpdir
            try:
                paths = oif.write_oif("CANCEL", trade_id="my_trade_xyz")
                self.assertEqual(len(paths), 1)
                content = open(paths[0]).read().rstrip("\n")
                fields = content.split(";")
                self.assertEqual(fields[0], "CANCEL")
                self.assertEqual(fields[10], "my_trade_xyz")
            finally:
                oif.OIF_INCOMING = _orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_cancel_action_without_trade_id_fails_cleanly(self):
        """CANCEL without trade_id must return [] and log an error, not crash."""
        import bridge.oif_writer as oif
        paths = oif.write_oif("CANCEL", trade_id="")
        self.assertEqual(paths, [])


class TestRegressionNoOldFormats(unittest.TestCase):
    """Regression guards — bugs must not silently return."""

    def test_no_bare_stop_in_place_stop_sell_output(self):
        tmpdir = tempfile.mkdtemp()
        try:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmpdir
            try:
                paths = oif.write_oif(
                    "PLACE_STOP_SELL", qty=1, stop_price=21950.0,
                    trade_id="regression", account="Sim101",
                )
                content = open(paths[0]).read().rstrip("\n")
                fields = content.split(";")
                # No field may equal bare "STOP"
                for i, f in enumerate(fields):
                    self.assertNotEqual(
                        f, "STOP",
                        f"Field {i} is bare STOP (pre-B3 format): {content}",
                    )
            finally:
                oif.OIF_INCOMING = _orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_15_semi_cancelall_anywhere(self):
        """Grep the generated CANCELALLORDERS line; if it ever emits 15 semis again, fail."""
        tmpdir = tempfile.mkdtemp()
        try:
            import bridge.oif_writer as oif
            _orig = oif.OIF_INCOMING
            oif.OIF_INCOMING = tmpdir
            try:
                paths = oif.write_oif("CANCEL_ALL")
                content = open(paths[0]).read().rstrip("\n")
                self.assertNotEqual(
                    content.count(";"), 15,
                    "CANCEL_ALL regressed to 15-semi pre-B2 form: " + content,
                )
            finally:
                oif.OIF_INCOMING = _orig
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
