"""TradeMemory per-bot file split — fixes 2026-05-12 prod/sim write race.

Previously prod and sim both wrote whole-file rewrites to
`logs/trade_memory.json`. The last-writer-wins race dropped one bot's
closed trades when the other rewrote with its older in-memory view.
Symptom: prod traded twice on 2026-05-12 morning, both closes logged
correctly in the bot's history.jsonl, but trade_memory.json had only
sim's trades because sim wrote after prod each time.

Fix: per-bot files `trade_memory_<bot>.json`. Legacy file is preserved
for read-only historical access. `load_all_trades()` merges everything
for callers that want unified history (dashboard, validation tools).

Run: python -m unittest tests.test_trade_memory_per_bot -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.trade_memory import TradeMemory, load_all_trades, LEGACY_FILE


class TestPerBotFilesIsolation(unittest.TestCase):
    """The whole point of the split: writes from one bot must not
    clobber writes from another bot."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        os.makedirs("logs", exist_ok=True)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmpdir.cleanup()

    def test_prod_and_sim_write_independent_files(self):
        prod = TradeMemory(bot_id="prod")
        sim = TradeMemory(bot_id="sim")

        prod.record({"trade_id": "P1", "strategy": "opening_session"})
        sim.record({"trade_id": "S1", "strategy": "dom_pullback"})

        # Each bot's file has only its own trade — no cross-contamination.
        self.assertTrue(os.path.exists("logs/trade_memory_prod.json"))
        self.assertTrue(os.path.exists("logs/trade_memory_sim.json"))

        with open("logs/trade_memory_prod.json") as f:
            prod_data = json.load(f)
        with open("logs/trade_memory_sim.json") as f:
            sim_data = json.load(f)

        self.assertEqual([t["trade_id"] for t in prod_data], ["P1"])
        self.assertEqual([t["trade_id"] for t in sim_data], ["S1"])

    def test_simulated_race_does_not_lose_trades(self):
        """The bug we're fixing: interleaved record() calls from two
        bots both produce durable trades."""
        prod = TradeMemory(bot_id="prod")
        sim = TradeMemory(bot_id="sim")

        # Interleave like real production: sim writes, then prod writes,
        # then sim again. Pre-fix, prod's write would have been clobbered.
        sim.record({"trade_id": "S1", "pnl_dollars": -10})
        prod.record({"trade_id": "P1", "pnl_dollars": -20})
        sim.record({"trade_id": "S2", "pnl_dollars": -30})
        prod.record({"trade_id": "P2", "pnl_dollars": -40})

        merged = load_all_trades()
        ids = {t["trade_id"] for t in merged}
        self.assertEqual(ids, {"S1", "P1", "S2", "P2"},
                         "load_all_trades lost a trade — race regressed")


class TestLegacyFileBackwardsCompat(unittest.TestCase):
    """Existing data in the legacy `logs/trade_memory.json` must remain
    visible to historical-analysis tools and to each bot's in-memory
    view (for win_rate / recent calls)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        os.makedirs("logs", exist_ok=True)
        # Seed legacy file with historical trades, mixed bot_ids
        legacy = [
            {"trade_id": "OLD_P1", "bot_id": "prod", "pnl_dollars": -5},
            {"trade_id": "OLD_S1", "bot_id": "sim", "pnl_dollars": +10},
            {"trade_id": "OLD_NULL", "bot_id": None, "pnl_dollars": +3},
        ]
        with open("logs/trade_memory.json", "w") as f:
            json.dump(legacy, f)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmpdir.cleanup()

    def test_per_bot_constructor_seeds_legacy_filtered_by_bot_id(self):
        prod = TradeMemory(bot_id="prod")
        # In-memory should see legacy prod trade.
        ids = {t["trade_id"] for t in prod.trades}
        self.assertIn("OLD_P1", ids)
        self.assertNotIn("OLD_S1", ids,
                         "prod TradeMemory should not see sim's legacy trades")

    def test_load_all_trades_returns_legacy_and_per_bot(self):
        # Seed per-bot file too
        prod = TradeMemory(bot_id="prod")
        prod.record({"trade_id": "NEW_P", "pnl_dollars": -1})

        merged = load_all_trades()
        ids = {t["trade_id"] for t in merged}
        self.assertEqual(
            ids, {"OLD_P1", "OLD_S1", "OLD_NULL", "NEW_P"},
            "merged view should include legacy + per-bot trades",
        )

    def test_per_bot_save_does_not_touch_legacy(self):
        legacy_path = "logs/trade_memory.json"
        before = os.path.getmtime(legacy_path)

        prod = TradeMemory(bot_id="prod")
        prod.record({"trade_id": "NEW_P", "pnl_dollars": -1})

        after = os.path.getmtime(legacy_path)
        self.assertEqual(before, after,
                         "legacy trade_memory.json should never be written")


class TestExplicitFilepathStillWorks(unittest.TestCase):
    """Tools and tests sometimes need explicit filepath control. The
    constructor's filepath= kwarg must still work."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_explicit_filepath_overrides_bot_id(self):
        path = os.path.join(self._tmpdir.name, "custom.json")
        tm = TradeMemory(filepath=path)
        tm.record({"trade_id": "X"})
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            data = json.load(f)
        self.assertEqual([t["trade_id"] for t in data], ["X"])


if __name__ == "__main__":
    unittest.main()
