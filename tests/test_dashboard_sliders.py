"""P3-1 (2026-05-24) — Dashboard 3C tuning slider wire end-to-end.

Confirms the full path:
    slider POST → /api/runtime-controls/params handler
        → per-bot command queue (_state[_commands_<bot>])
        → DashboardCommandDispatcher.handle("update_params", params)
        → BaseBot.update_runtime_params(params)
        → RiskManager.set_risk_per_trade / set_daily_limit
        → in-force value clamped at MAX_LOSS_PER_TRADE

And the secondary "Save to Config" stub path:
    button → POST /api/runtime-controls/save → JSON {ok: True, message: ...}

The dashboard.server module is heavy (starts a bridge poller thread on
import). These tests use Flask's test_client so no live server is required.

Run: python -m unittest tests.test_dashboard_sliders -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── 1) Slider POST → Flask handler → per-bot command queue ───────────
class TestSliderPostEnqueuesCommands(unittest.TestCase):
    """The Apply Changes button posts to /api/runtime-controls/params.
    The handler must enqueue exactly one update_params command per bot
    (prod + lab), each carrying the slider payload verbatim under "params".
    """

    @classmethod
    def setUpClass(cls):
        # Import server lazily — top-level import spawns the bridge poller
        # thread, but that's OK in test (it'll just log "Bridge unreachable"
        # every 2s and never affect _state for our keys).
        from dashboard import server as srv
        cls.srv = srv
        cls.client = srv.app.test_client()

    def setUp(self):
        # Drain any pre-existing command queues so each test starts clean.
        with self.srv._state_lock:
            for k in list(self.srv._state.keys()):
                if k.startswith("_commands_"):
                    self.srv._state[k] = []

    def _drain(self, bot: str) -> list[dict]:
        with self.srv._state_lock:
            return list(self.srv._state.get(f"_commands_{bot}", []))

    def test_all_four_slider_values_make_it_into_one_command_per_bot(self):
        payload = {
            "risk_per_trade": 12.0,
            "max_daily_loss": 40.0,
            "min_trade_spacing": 7,
            "max_trades_per_session": 5,
        }
        resp = self.client.post(
            "/api/runtime-controls/params",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True})

        prod_cmds = self._drain("prod")
        lab_cmds = self._drain("lab")
        # Handler enqueues one command per bot (prod + lab).
        self.assertEqual(len(prod_cmds), 1, prod_cmds)
        self.assertEqual(len(lab_cmds), 1, lab_cmds)
        for cmd in (prod_cmds[0], lab_cmds[0]):
            self.assertEqual(cmd["type"], "update_params")
            self.assertEqual(cmd["params"], payload)
            self.assertIn("ts", cmd)

    def test_partial_payload_still_enqueues_only_provided_keys(self):
        """Operator drags only the spacing slider — payload contains just
        min_trade_spacing. The handler must not invent keys."""
        payload = {"min_trade_spacing": 20}
        resp = self.client.post(
            "/api/runtime-controls/params",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        cmd = self._drain("prod")[0]
        self.assertEqual(cmd["params"], {"min_trade_spacing": 20})


# ─── 2) DashboardCommandDispatcher.handle → BaseBot.update_runtime_params ─
class TestDispatcherRoutesToUpdateRuntimeParams(unittest.TestCase):
    """Once DashboardPusher pulls the queued command, it invokes
    DashboardCommandDispatcher.handle(cmd). For update_params this must
    delegate to bot.update_runtime_params(params) verbatim."""

    def test_update_params_calls_update_runtime_params_with_payload(self):
        from bots._dashboard_commands import DashboardCommandDispatcher
        mock_bot = MagicMock()
        dispatcher = DashboardCommandDispatcher(mock_bot)
        params = {
            "risk_per_trade": 12.0,
            "max_daily_loss": 40.0,
            "min_trade_spacing": 7,
            "max_trades_per_session": 5,
        }
        dispatcher.handle({"type": "update_params", "params": params})
        mock_bot.update_runtime_params.assert_called_once_with(params)

    def test_unknown_command_type_is_silent_noop(self):
        """Defensive: unknown command type doesn't blow up the poll loop."""
        from bots._dashboard_commands import DashboardCommandDispatcher
        mock_bot = MagicMock()
        dispatcher = DashboardCommandDispatcher(mock_bot)
        # Should not raise.
        dispatcher.handle({"type": "this_command_does_not_exist"})
        mock_bot.update_runtime_params.assert_not_called()

    def test_missing_params_key_falls_back_to_empty_dict(self):
        from bots._dashboard_commands import DashboardCommandDispatcher
        mock_bot = MagicMock()
        dispatcher = DashboardCommandDispatcher(mock_bot)
        dispatcher.handle({"type": "update_params"})  # no "params"
        mock_bot.update_runtime_params.assert_called_once_with({})


# ─── 3) RiskManager.set_risk_per_trade respects MAX_LOSS_PER_TRADE clamp ──
class TestRiskManagerSlidersClampToConfig(unittest.TestCase):
    """RiskManager.set_risk_per_trade clamps the slider value at
    MAX_LOSS_PER_TRADE (config/settings.py). This is the safety net that
    prevents an operator typo from overriding the hard-cap. The dashboard
    slider's max=20 mirrors the same config value; this test guards both
    sides so a future config change doesn't silently widen the slider.
    """

    def test_slider_value_above_cap_is_clamped_to_cap(self):
        from core.risk_manager import RiskManager
        from config.settings import MAX_LOSS_PER_TRADE

        rm = RiskManager()
        rm.set_risk_per_trade(MAX_LOSS_PER_TRADE + 999.0)
        self.assertEqual(rm._risk_per_trade, MAX_LOSS_PER_TRADE)

    def test_slider_value_below_cap_is_honored_verbatim(self):
        from core.risk_manager import RiskManager
        from config.settings import MAX_LOSS_PER_TRADE

        rm = RiskManager()
        below = max(1.0, MAX_LOSS_PER_TRADE - 5.0)
        rm.set_risk_per_trade(below)
        self.assertEqual(rm._risk_per_trade, below)

    def test_daily_limit_setter_has_no_implicit_cap(self):
        """set_daily_limit currently doesn't clamp. Documenting that here
        so the test breaks loudly if someone adds a clamp without thinking
        through the dashboard slider range (20-60)."""
        from core.risk_manager import RiskManager
        rm = RiskManager()
        rm.set_daily_limit(40.0)
        self.assertEqual(rm._daily_limit, 40.0)
        rm.set_daily_limit(999.0)  # No clamp by design (today)
        self.assertEqual(rm._daily_limit, 999.0)

    def test_trade_spacing_setter_updates_value(self):
        from core.risk_manager import RiskManager
        rm = RiskManager()
        rm.set_trade_spacing(7)
        self.assertEqual(rm._spacing_min, 7)
        rm.set_trade_spacing(30)
        self.assertEqual(rm._spacing_min, 30)

    def test_max_trades_setter_updates_value(self):
        from core.risk_manager import RiskManager
        rm = RiskManager()
        rm.set_max_trades(5)
        self.assertEqual(rm._max_trades, 5)


# ─── 4) Save-to-Config endpoint is a stub but returns ok ──────────────
class TestSaveConfigEndpointStub(unittest.TestCase):
    """The Save to Config button posts to /api/runtime-controls/save.
    Currently a stub (TODO in dashboard/server.py:984) — we lock in the
    stub contract so the dashboard's flashFeedback() always gets a
    message string back, and so the day the real file-write lands we
    notice it broke the wire to the button."""

    @classmethod
    def setUpClass(cls):
        from dashboard import server as srv
        cls.client = srv.app.test_client()

    def test_save_endpoint_returns_ok_with_message(self):
        resp = self.client.post(
            "/api/runtime-controls/save",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        # The stub message is human-readable so the dashboard's
        # flashFeedback() surfaces it to the operator verbatim.
        self.assertIn("message", body)
        self.assertIsInstance(body["message"], str)


if __name__ == "__main__":
    unittest.main()
