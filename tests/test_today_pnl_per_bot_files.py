"""/api/today-pnl reads per-bot trade_memory files — regression test.

Background
----------
Commit 02b0efd (2026-05-12) split trade_memory into per-bot files
(`trade_memory_<bot>.json`) to fix a prod/sim file-write race. The
shared `trade_memory.json` is now frozen as a historical snapshot.

But `/api/today-pnl` was reading `trade_memory.json` directly via
`open(tm_path)` — meaning AFTER the split, current-session trades
written to the per-bot files were invisible to the dashboard's
TODAY card. Operator saw "$0 / 0 trades" in the TODAY card while
the Daily Stats panel (which reads the live in-memory `trades`
array via /api/status) correctly showed today's wins.

Observed live 2026-05-13: Daily Stats showed 2 wins / $34.36 from
sim_bot (vwap_pullback), but TODAY card showed $0 / 0 trades.

Fix: route through `core.trade_memory.load_all_trades()` — the
same merging loader that backs `_load_session_trades_by_bot` —
which reads legacy + every per-bot file.

This test creates an isolated logs directory with ONLY a per-bot
file (no legacy file at all), points the dashboard at it, and
verifies a recent trade in the per-bot file IS counted.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestTodayPnlPerBotFiles(unittest.TestCase):
    """The bug: TODAY card returned 0 trades after the per-bot split."""

    def setUp(self):
        from dashboard import server as dash
        self.dash = dash
        # Build an isolated logs dir for this test
        self.tmpdir = Path(__file__).parent / "_tmp_today_pnl_logs"
        self.tmpdir.mkdir(exist_ok=True)
        # Recent trade — well inside the current globex session
        # (session starts at most-recent 17:00 CT, so any trade in
        # the last hour is unambiguously in-session)
        self.now = time.time()
        self.recent_trade = {
            "trade_id": "test_8b51c073",
            "bot_id": "sim",
            "strategy": "vwap_pullback",
            "direction": "LONG",
            "entry_time": self.now - 600,
            "exit_time": self.now - 60,
            "entry_price": 29375.75,
            "exit_price": 29391.0,
            "contracts": 1,
            "pnl_ticks": 61.0,
            "pnl_dollars": 25.68,
            "net_pnl": 25.68,
            "pnl_dollars_gross": 26.18,
            "cost_total_dollars": 0.50,
        }
        # Write to per-bot file only — no legacy trade_memory.json
        per_bot_path = self.tmpdir / "trade_memory_sim.json"
        per_bot_path.write_text(json.dumps([self.recent_trade]),
                                encoding="utf-8")

    def tearDown(self):
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_today_pnl_finds_trades_in_per_bot_file(self):
        """Before the fix: returned trade_count=0 because it read
        trade_memory.json (absent). After: counts the per-bot trade."""
        # Point the dashboard at our isolated logs dir by patching PROJECT_ROOT
        with patch.object(self.dash, "PROJECT_ROOT",
                          str(self.tmpdir.parent.parent)):
            # Actually easier: monkeypatch os.path.join(PROJECT_ROOT, "logs", ...)
            # to return our tmpdir. But since the new code uses
            # load_all_trades(logs_dir=os.path.join(PROJECT_ROOT, "logs")),
            # we can patch via patching PROJECT_ROOT alone — the new logs dir
            # will be PROJECT_ROOT/"logs". So set PROJECT_ROOT to tmpdir.parent
            # so its "logs" subdir is tmpdir.
            pass

        # Cleaner: patch PROJECT_ROOT so that os.path.join(PROJECT_ROOT, "logs")
        # resolves to self.tmpdir.
        with patch.object(self.dash, "PROJECT_ROOT", str(self.tmpdir.parent)):
            # tmpdir is named "_tmp_today_pnl_logs"; rename via symlink
            # would be overkill. Simpler: also patch the function via the
            # logs_dir kwarg by intercepting load_all_trades.
            from core import trade_memory as tm
            orig = tm.load_all_trades
            captured = {}

            def spy(logs_dir, *a, **kw):
                captured["logs_dir"] = logs_dir
                # Call original but force it to use OUR tmpdir
                return orig(logs_dir=str(self.tmpdir), *a, **kw)

            with patch.object(tm, "load_all_trades", spy):
                # Also re-import the dashboard's reference so it picks up
                # the patched module function. The dashboard does
                # `from core.trade_memory import load_all_trades` INSIDE
                # the handler so each call re-imports — the patch wins.
                client = self.dash.app.test_client()
                resp = client.get("/api/today-pnl")
                self.assertEqual(resp.status_code, 200)
                payload = resp.get_json()

        # Bug regression: trade_count must NOT be 0
        self.assertEqual(
            payload.get("trade_count"), 1,
            f"expected trade_count=1, got {payload.get('trade_count')}. "
            f"per_bot={payload.get('per_bot')}, "
            f"error={payload.get('error')}"
        )
        # sim bucket should have the trade
        sim = payload.get("per_bot", {}).get("sim")
        self.assertIsNotNone(sim, f"missing sim bucket in {payload.get('per_bot')}")
        self.assertEqual(sim["trades"], 1)
        self.assertEqual(sim["wins"], 1)
        self.assertEqual(sim["losses"], 0)
        # P&L should reflect the trade's net_pnl
        self.assertAlmostEqual(sim["pnl"], 25.68, places=2)

    def test_today_pnl_uses_load_all_trades_not_raw_open(self):
        """Static: handler must call load_all_trades, not open() the legacy file."""
        src = (Path(__file__).parent.parent / "dashboard" / "server.py").read_text(
            encoding="utf-8"
        )
        # Find the /api/today-pnl handler body
        start = src.find('@app.route("/api/today-pnl")')
        self.assertGreater(start, 0)
        end_route = src.find("@app.route", start + 1)
        body = src[start:end_route] if end_route > 0 else src[start:]
        # Must use load_all_trades
        self.assertIn(
            "load_all_trades", body,
            "/api/today-pnl handler doesn't call load_all_trades — "
            "will silently miss per-bot trade_memory files"
        )
        # Must NOT raw-open the legacy file path
        self.assertNotIn(
            'open(tm_path', body,
            "/api/today-pnl handler still raw-opens trade_memory.json — "
            "regression of the 2026-05-13 fix"
        )


if __name__ == "__main__":
    unittest.main()
