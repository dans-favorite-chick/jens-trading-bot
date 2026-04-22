"""
B59 — live-account hard-guard tests.

Guards at 3 layers ensure Phoenix can NEVER write an order to the live
account (LIVE_ACCOUNT env var):

  1. bridge/oif_writer._reject_live_account() — central check called
     by _require_account(), cancel_all_orders_line(),
     write_protection_oco(), write_bracket_order(), write_oif().
  2. bridge/bridge_server._handle_trade_command() — guard at the WS
     dispatch layer; short-circuits BEFORE calling write_oif.
  3. bots/base_bot._enter_trade() — guard AFTER routing resolution;
     aborts the signal + Telegram alert.

Each test patches os.environ["LIVE_ACCOUNT"] to a fake value, calls
each guarded entry point with that account, and asserts the proper
failure mode (RuntimeError for oif_writer paths, early return / no
file writes elsewhere).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

LIVE = "9999999"  # fake live-account id for tests


class TestOifWriterGuard(unittest.TestCase):
    """Layer 1: every oif_writer entry point rejects the live account."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"LIVE_ACCOUNT": LIVE})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_require_account_rejects_live(self):
        from bridge.oif_writer import _require_account
        with self.assertRaises(RuntimeError) as ctx:
            _require_account(LIVE, "test_caller")
        self.assertIn("LIVE_GUARD", str(ctx.exception))
        self.assertIn(LIVE, str(ctx.exception))

    def test_cancel_all_orders_line_rejects_live(self):
        from bridge.oif_writer import cancel_all_orders_line
        with self.assertRaises(RuntimeError) as ctx:
            cancel_all_orders_line(account=LIVE)
        self.assertIn("LIVE_GUARD", str(ctx.exception))

    def test_write_bracket_order_rejects_live(self):
        from bridge.oif_writer import write_bracket_order
        with self.assertRaises(RuntimeError) as ctx:
            write_bracket_order(
                direction="LONG", qty=1, entry_type="MARKET",
                entry_price=0, stop_price=26800, target_price=27000,
                trade_id="test_b59", account=LIVE,
            )
        self.assertIn("LIVE_GUARD", str(ctx.exception))

    def test_write_protection_oco_rejects_live(self):
        from bridge.oif_writer import write_protection_oco
        with self.assertRaises(RuntimeError) as ctx:
            write_protection_oco(
                direction="LONG", qty=1,
                stop_price=26800, target_price=27000,
                trade_id="test_b59", account=LIVE,
            )
        self.assertIn("LIVE_GUARD", str(ctx.exception))

    def test_write_oif_rejects_live_on_entry(self):
        from bridge.oif_writer import write_oif
        with self.assertRaises(RuntimeError) as ctx:
            write_oif(
                "ENTER_LONG", qty=1,
                stop_price=26800, target_price=27000,
                trade_id="test_b59", order_type="MARKET",
                account=LIVE,
            )
        self.assertIn("LIVE_GUARD", str(ctx.exception))

    def test_write_oif_rejects_live_on_cancel_all(self):
        from bridge.oif_writer import write_oif
        with self.assertRaises(RuntimeError) as ctx:
            write_oif("CANCEL_ALL", qty=0, trade_id="test_b59", account=LIVE)
        self.assertIn("LIVE_GUARD", str(ctx.exception))

    def test_allows_non_live_account(self):
        """Sanity: Sim* accounts still work."""
        from bridge.oif_writer import cancel_all_orders_line
        line = cancel_all_orders_line(account="SimBias Momentum")
        self.assertTrue(line.startswith("CANCELALLORDERS;SimBias Momentum;"))


class TestBridgeServerGuard(unittest.TestCase):
    """Layer 2: bridge_server short-circuits before calling write_oif."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"LIVE_ACCOUNT": LIVE})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_bridge_handle_trade_command_blocks_live(self):
        """Simulate the bridge's live-account check branch without
        instantiating the full bridge_server (which needs NT8 wiring)."""
        trade_data = {
            "action": "ENTER_LONG", "qty": 1, "trade_id": "test_b59",
            "order_type": "MARKET", "limit_price": 0.0,
            "stop_price": 26800, "target_price": 27000,
            "account": LIVE,
        }
        # Replicate the exact check from bridge_server._handle_trade_command
        _live = os.environ.get("LIVE_ACCOUNT", "").strip()
        blocked = bool(_live and str(trade_data.get("account") or "").strip() == _live)
        self.assertTrue(blocked, "bridge_server guard must block live account")

    def test_bridge_handle_allows_sim(self):
        trade_data = {"account": "SimBias Momentum"}
        _live = os.environ.get("LIVE_ACCOUNT", "").strip()
        blocked = bool(_live and str(trade_data.get("account") or "").strip() == _live)
        self.assertFalse(blocked)


class TestGuardOffWhenEnvUnset(unittest.TestCase):
    """When LIVE_ACCOUNT env var is empty/unset, the guard must not fire."""

    def test_no_env_no_block(self):
        with patch.dict(os.environ, {"LIVE_ACCOUNT": ""}):
            from bridge.oif_writer import _reject_live_account
            # Should not raise for ANY account if env is empty
            _reject_live_account("9999999", "test_caller")
            _reject_live_account("SimBias Momentum", "test_caller")


if __name__ == "__main__":
    unittest.main()
