"""TapeReader — Sprint M Tier 2.3 (2026-05-12).

Pure-observation feature for institutional large-print detection on
the executed tick tape. These tests verify:

  * Threshold gating — small ticks NOT recorded, large ticks ARE
  * Aggressor-side classification via the quote rule (Lee-Ready 1991)
  * Rolling window cap (DEFAULT_HISTORY_SIZE)
  * Session aggregate stats (avg, max, total)
  * Malformed tick safety (no crash, no state mutation)
  * get_state() shape contract — JSON-serializable end-to-end so the
    field flows through history.jsonl

Per the deliberate scope decision: NO IQS bonus integration yet — those
tests will appear after ~30 trades of footprint_cvd_reversal data show
whether direction-aligned large prints correlate with subsequent moves.

Run: python -m unittest tests.test_tape_reader -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tape_reader import (
    TapeReader,
    DEFAULT_LARGE_PRINT_THRESHOLD,
    DEFAULT_HISTORY_SIZE,
)


def _tick(price: float = 29200.0, bid: float = 29199.75, ask: float = 29200.25,
          vol: int = 1, ts: str = "2026-05-12T22:00:00") -> dict:
    return {"type": "tick", "price": price, "bid": bid, "ask": ask,
            "vol": vol, "ts": ts}


# ═══════════════════════════════════════════════════════════════════
# Threshold gating
# ═══════════════════════════════════════════════════════════════════
class TestThreshold(unittest.TestCase):

    def test_default_threshold_is_25(self):
        self.assertEqual(DEFAULT_LARGE_PRINT_THRESHOLD, 25,
                         "default threshold changed — verify intended")

    def test_tick_below_threshold_not_recorded(self):
        t = TapeReader()
        result = t.record_tick(_tick(vol=10))
        self.assertIsNone(result)
        self.assertEqual(len(t.get_state()["large_prints"]), 0)

    def test_tick_at_threshold_recorded(self):
        t = TapeReader(threshold_contracts=25)
        result = t.record_tick(_tick(vol=25))
        self.assertIsNotNone(result)
        self.assertEqual(result["size"], 25)
        self.assertEqual(len(t.get_state()["large_prints"]), 1)

    def test_tick_above_threshold_recorded(self):
        t = TapeReader(threshold_contracts=25)
        result = t.record_tick(_tick(vol=100))
        self.assertIsNotNone(result)
        self.assertEqual(result["size"], 100)

    def test_custom_threshold_respected(self):
        t = TapeReader(threshold_contracts=5)
        # 4 not recorded
        self.assertIsNone(t.record_tick(_tick(vol=4)))
        # 5 recorded
        self.assertIsNotNone(t.record_tick(_tick(vol=5)))


# ═══════════════════════════════════════════════════════════════════
# Aggressor-side classification (quote rule)
# ═══════════════════════════════════════════════════════════════════
class TestSideClassification(unittest.TestCase):

    def test_price_at_ask_is_buy(self):
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29200.25, bid=29199.75, ask=29200.25))
        self.assertEqual(r["side"], "buy")

    def test_price_above_ask_is_buy(self):
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29200.50, bid=29199.75, ask=29200.25))
        self.assertEqual(r["side"], "buy")

    def test_price_at_bid_is_sell(self):
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29199.75, bid=29199.75, ask=29200.25))
        self.assertEqual(r["side"], "sell")

    def test_price_below_bid_is_sell(self):
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29199.50, bid=29199.75, ask=29200.25))
        self.assertEqual(r["side"], "sell")

    def test_inside_spread_above_midpoint_is_buy(self):
        # bid=29199.75 ask=29200.25 mid=29200.00; price 29200.10 > mid
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29200.10, bid=29199.75, ask=29200.25))
        self.assertEqual(r["side"], "buy")

    def test_inside_spread_below_midpoint_is_sell(self):
        # price 29199.90 < mid 29200.00
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29199.90, bid=29199.75, ask=29200.25))
        self.assertEqual(r["side"], "sell")

    def test_inside_spread_at_midpoint_is_unknown(self):
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29200.00, bid=29199.75, ask=29200.25))
        self.assertEqual(r["side"], "unknown")

    def test_missing_quotes_is_unknown(self):
        t = TapeReader(threshold_contracts=1)
        r = t.record_tick(_tick(price=29200.0, bid=0, ask=0))
        self.assertEqual(r["side"], "unknown")


# ═══════════════════════════════════════════════════════════════════
# Rolling window cap
# ═══════════════════════════════════════════════════════════════════
class TestRollingWindow(unittest.TestCase):

    def test_default_history_size_is_50(self):
        self.assertEqual(DEFAULT_HISTORY_SIZE, 50,
                         "default history size changed — verify intended")

    def test_oldest_records_evicted_at_cap(self):
        t = TapeReader(threshold_contracts=1, history_size=3)
        for i in range(5):
            t.record_tick(_tick(vol=10, ts=f"t{i}"))
        prints = t.get_state()["large_prints"]
        self.assertEqual(len(prints), 3)
        # Most recent three retained.
        self.assertEqual([p["ts"] for p in prints], ["t2", "t3", "t4"])


# ═══════════════════════════════════════════════════════════════════
# Session aggregate stats
# ═══════════════════════════════════════════════════════════════════
class TestSessionStats(unittest.TestCase):

    def test_session_aggregates_include_small_ticks(self):
        # Small ticks contribute to session_avg even though they're not
        # recorded as "large prints".
        t = TapeReader(threshold_contracts=100)
        for sz in [1, 2, 3, 4, 5]:
            t.record_tick(_tick(vol=sz))
        state = t.get_state()
        self.assertEqual(state["session_total_volume"], 15)
        self.assertEqual(state["session_avg_size"], 3.0)
        self.assertEqual(state["session_largest_size"], 5)
        # But no large prints captured.
        self.assertEqual(len(state["large_prints"]), 0)

    def test_empty_session_avg_is_zero(self):
        state = TapeReader().get_state()
        self.assertEqual(state["session_avg_size"], 0.0)
        self.assertEqual(state["session_total_volume"], 0)


# ═══════════════════════════════════════════════════════════════════
# Defensive parsing
# ═══════════════════════════════════════════════════════════════════
class TestMalformedInput(unittest.TestCase):

    def test_missing_vol_no_crash(self):
        t = TapeReader()
        result = t.record_tick({"price": 29200.0, "bid": 29199.75, "ask": 29200.25})
        self.assertIsNone(result)
        self.assertEqual(t.get_state()["session_total_volume"], 0)

    def test_negative_vol_ignored(self):
        t = TapeReader(threshold_contracts=1)
        result = t.record_tick(_tick(vol=-5))
        self.assertIsNone(result)

    def test_string_vol_no_crash(self):
        t = TapeReader(threshold_contracts=1)
        result = t.record_tick({"vol": "not a number", "price": 1, "bid": 1, "ask": 1})
        self.assertIsNone(result)

    def test_string_price_returns_none(self):
        # vol valid but price unparseable — should not crash
        t = TapeReader(threshold_contracts=1)
        result = t.record_tick({"vol": 100, "price": "x", "bid": 1, "ask": 1, "ts": "x"})
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# get_state() shape — must be JSON-serializable
# ═══════════════════════════════════════════════════════════════════
class TestStateShape(unittest.TestCase):

    def test_state_is_json_serializable(self):
        t = TapeReader(threshold_contracts=1)
        t.record_tick(_tick(price=29200.25, vol=50))
        state = t.get_state()
        # Round-trip through JSON — fails if any non-serializable types
        # (e.g. datetime, deque) leak in.
        json_str = json.dumps(state)
        roundtrip = json.loads(json_str)
        self.assertEqual(roundtrip["history_size"], 1)
        self.assertEqual(roundtrip["large_prints"][0]["size"], 50)

    def test_state_keys_stable(self):
        state = TapeReader().get_state()
        expected = {"threshold_contracts", "history_size", "large_prints",
                    "session_avg_size", "session_largest_size",
                    "session_total_volume"}
        self.assertEqual(set(state.keys()), expected,
                         "tape_state schema changed — downstream consumers may break")


# ═══════════════════════════════════════════════════════════════════
# recent_aligned — future IQS bonus consumer
# ═══════════════════════════════════════════════════════════════════
class TestRecentAligned(unittest.TestCase):

    def test_long_aligned_with_buy_prints(self):
        t = TapeReader(threshold_contracts=1)
        # Mix of buy/sell prints
        t.record_tick(_tick(price=29200.25, bid=29199.75, ask=29200.25, vol=50))  # buy
        t.record_tick(_tick(price=29200.50, bid=29199.75, ask=29200.25, vol=50))  # buy
        t.record_tick(_tick(price=29199.75, bid=29199.75, ask=29200.25, vol=50))  # sell
        self.assertEqual(t.recent_aligned("long", lookback=10), 2)
        self.assertEqual(t.recent_aligned("short", lookback=10), 1)

    def test_lookback_limits_window(self):
        t = TapeReader(threshold_contracts=1)
        for _ in range(5):
            t.record_tick(_tick(price=29200.25, vol=50))  # all buys
        self.assertEqual(t.recent_aligned("long", lookback=3), 3)
        self.assertEqual(t.recent_aligned("long", lookback=10), 5)


# ═══════════════════════════════════════════════════════════════════
# Integration: base_bot wires it correctly (static checks)
# ═══════════════════════════════════════════════════════════════════
class TestBaseBotWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).parent.parent / "bots" / "base_bot.py").read_text(
            encoding="utf-8"
        )

    def test_tape_reader_imported(self):
        self.assertIn("from core.tape_reader import TapeReader", self.src)

    def test_tape_reader_instantiated_in_init(self):
        self.assertIn("self.tape_reader = TapeReader()", self.src)

    def test_tape_reader_fed_on_each_tick(self):
        from tests._bot_src_search import bot_source_matches; assert bot_source_matches("self.tape_reader.record_tick(tick)", "self.bot.tape_reader.record_tick(tick)", "bot.tape_reader.record_tick(tick)"), "tape_reader.record_tick wiring missing — should be in bots/_ws_dispatcher.py"

    def test_tape_state_exposed_in_market_snapshot(self):
        # 2026-05-24 P4-1 Stage 3: market enrichment moved to
        # bots/_strategy_dispatch.py — tape_state is stashed via
        # self.bot.tape_reader.get_state() in the extracted module.
        from pathlib import Path
        dispatch_src = (
            Path(__file__).parent.parent / "bots" / "_strategy_dispatch.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'market["tape_state"] = self.bot.tape_reader.get_state()',
            dispatch_src,
        )


if __name__ == "__main__":
    unittest.main()
