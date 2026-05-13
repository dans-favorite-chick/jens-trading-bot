"""RiskManager hydration on bot startup — regression test.

Bug observed live 2026-05-13: dashboard's Daily Stats panel showed
`$0.00 / 0 trades / 0% WR` after a bot bounce mid-session, even though
the TODAY (CME Globex) summary card correctly showed today's $114.22
from 4 sim_bot wins (read directly from trade_memory files via
/api/today-pnl).

Root cause: `RiskManager.__init__` constructs a fresh `RiskState` with
all-zero counters. The 2026-05-13 audit fix to `PositionManager` hydrated
`trade_history` from disk, but the RiskManager's daily counters were
never derived from that history. Daily Stats panel reads from
`sim.risk` (the RiskManager's `to_dict()` output) — so it saw zeros.

Fix: `RiskManager.hydrate_from_trades(trades, since_ts)` replays today's
trades in chronological order to set daily_pnl / trades_today /
wins_today / losses_today / consecutive_losses / last_trade_time.

This test:
- Builds a synthetic trade history with mixed pre/post-midnight trades
- Calls hydrate_from_trades with midnight-today as the cutoff
- Asserts counters match the expected sums (today only)
- Asserts consecutive_losses counts correctly (replay order matters)
- Asserts recovery_mode is set if the hydrated daily_pnl is bad enough
"""
from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.risk_manager import RiskManager


def _midnight_today_ts() -> float:
    return datetime.combine(datetime.now().date(), dt_time.min).timestamp()


class TestHydrateFromTrades(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager()
        self.midnight = _midnight_today_ts()

    def test_empty_trade_list_no_change(self):
        """Empty input is a no-op — state stays at defaults."""
        self.rm.hydrate_from_trades([], since_ts=self.midnight)
        d = self.rm.to_dict()
        self.assertEqual(d["daily_pnl"], 0.0)
        self.assertEqual(d["trades_today"], 0)
        self.assertEqual(d["wins_today"], 0)
        self.assertEqual(d["losses_today"], 0)

    def test_yesterday_trades_excluded(self):
        """Trades with exit_time < since_ts must not count."""
        yesterday = self.midnight - 3600  # 1h before midnight
        trades = [
            {"trade_id": "y1", "exit_time": yesterday, "pnl_dollars": 50.0},
            {"trade_id": "y2", "exit_time": yesterday + 60, "pnl_dollars": -20.0},
        ]
        self.rm.hydrate_from_trades(trades, since_ts=self.midnight)
        self.assertEqual(self.rm.state.daily_pnl, 0.0)
        self.assertEqual(self.rm.state.trades_today, 0)

    def test_today_trades_counted_with_net_pnl(self):
        """Trades >= midnight count; net_pnl preferred over pnl_dollars."""
        trades = [
            # net_pnl present — takes precedence
            {"trade_id": "t1", "exit_time": self.midnight + 100,
             "pnl_dollars": 30.0, "net_pnl": 25.68},
            # only pnl_dollars
            {"trade_id": "t2", "exit_time": self.midnight + 200,
             "pnl_dollars": 8.68},
        ]
        self.rm.hydrate_from_trades(trades, since_ts=self.midnight)
        self.assertAlmostEqual(self.rm.state.daily_pnl, 25.68 + 8.68, places=2)
        self.assertEqual(self.rm.state.trades_today, 2)
        self.assertEqual(self.rm.state.wins_today, 2)
        self.assertEqual(self.rm.state.losses_today, 0)
        # last_trade_time = most recent
        self.assertAlmostEqual(
            self.rm.state.last_trade_time, self.midnight + 200, places=2
        )

    def test_consecutive_losses_replays_in_order(self):
        """consecutive_losses must reflect the END-OF-DAY streak.
        Replay order matters: W, L, L, W, L, L → consecutive=2."""
        base = self.midnight + 100
        trades = [
            {"trade_id": "a", "exit_time": base + 1, "pnl_dollars":  10.0},  # W
            {"trade_id": "b", "exit_time": base + 2, "pnl_dollars": -5.0},   # L
            {"trade_id": "c", "exit_time": base + 3, "pnl_dollars": -7.0},   # L
            {"trade_id": "d", "exit_time": base + 4, "pnl_dollars":  20.0},  # W (reset)
            {"trade_id": "e", "exit_time": base + 5, "pnl_dollars": -3.0},   # L
            {"trade_id": "f", "exit_time": base + 6, "pnl_dollars": -4.0},   # L
        ]
        self.rm.hydrate_from_trades(trades, since_ts=self.midnight)
        self.assertEqual(self.rm.state.trades_today, 6)
        self.assertEqual(self.rm.state.wins_today, 2)
        self.assertEqual(self.rm.state.losses_today, 4)
        self.assertEqual(
            self.rm.state.consecutive_losses, 2,
            f"expected end-of-day streak=2 (e,f), got {self.rm.state.consecutive_losses}"
        )

    def test_chronological_replay_handles_input_order(self):
        """Input order shouldn't matter — sort by exit_time internally."""
        base = self.midnight + 100
        # Same scenario as above, but shuffled input order
        trades = [
            {"trade_id": "d", "exit_time": base + 4, "pnl_dollars":  20.0},
            {"trade_id": "f", "exit_time": base + 6, "pnl_dollars": -4.0},
            {"trade_id": "a", "exit_time": base + 1, "pnl_dollars":  10.0},
            {"trade_id": "b", "exit_time": base + 2, "pnl_dollars": -5.0},
            {"trade_id": "e", "exit_time": base + 5, "pnl_dollars": -3.0},
            {"trade_id": "c", "exit_time": base + 3, "pnl_dollars": -7.0},
        ]
        self.rm.hydrate_from_trades(trades, since_ts=self.midnight)
        self.assertEqual(
            self.rm.state.consecutive_losses, 2,
            "replay order must be chronological regardless of input order"
        )

    def test_recovery_mode_triggers_on_hydrated_loss(self):
        """If hydrated daily_pnl <= -RECOVERY_MODE_TRIGGER, the flag flips."""
        from config.settings import RECOVERY_MODE_TRIGGER
        # Build trades that sum to a loss deep enough to trip recovery
        loss_per_trade = -(RECOVERY_MODE_TRIGGER + 5.0) / 2
        trades = [
            {"trade_id": "a", "exit_time": self.midnight + 1,
             "pnl_dollars": loss_per_trade},
            {"trade_id": "b", "exit_time": self.midnight + 2,
             "pnl_dollars": loss_per_trade},
        ]
        self.rm.hydrate_from_trades(trades, since_ts=self.midnight)
        self.assertTrue(
            self.rm.state.recovery_mode,
            f"recovery_mode should be True when daily_pnl="
            f"{self.rm.state.daily_pnl:.2f} <= -{RECOVERY_MODE_TRIGGER}"
        )

    def test_to_dict_reflects_hydrated_state(self):
        """The dashboard reads /api/status -> risk.to_dict() — confirm
        the hydrated counters reach that surface."""
        trades = [
            {"trade_id": "x", "exit_time": self.midnight + 1,
             "pnl_dollars": 25.68},
            {"trade_id": "y", "exit_time": self.midnight + 2,
             "pnl_dollars": 8.68},
        ]
        self.rm.hydrate_from_trades(trades, since_ts=self.midnight)
        d = self.rm.to_dict()
        self.assertAlmostEqual(d["daily_pnl"], 34.36, places=2)
        self.assertEqual(d["trades_today"], 2)
        self.assertEqual(d["wins_today"], 2)
        self.assertEqual(d["losses_today"], 0)
        self.assertEqual(d["win_rate"], 100.0)

    def test_handles_missing_pnl_fields_gracefully(self):
        """Trades with no pnl field count as 0 — no crash."""
        trades = [
            {"trade_id": "noop", "exit_time": self.midnight + 1},
            {"trade_id": "good", "exit_time": self.midnight + 2,
             "pnl_dollars": 10.0},
        ]
        self.rm.hydrate_from_trades(trades, since_ts=self.midnight)
        self.assertEqual(self.rm.state.trades_today, 2)
        self.assertEqual(self.rm.state.daily_pnl, 10.0)
        # 0-pnl trade is neither win nor loss
        self.assertEqual(self.rm.state.wins_today, 1)
        self.assertEqual(self.rm.state.losses_today, 0)


class TestBaseBotInitCallsHydrate(unittest.TestCase):
    """Static check: BaseBot.__init__ must call risk.hydrate_from_trades
    after self.positions = PositionManager(load_history=True)."""

    def test_basebot_init_calls_hydrate(self):
        src = (Path(__file__).parent.parent / "bots" / "base_bot.py").read_text(
            encoding="utf-8"
        )
        # Find __init__ body
        import re
        m = re.search(
            r"def __init__\(self\).*?(?=\n    (?:async )?def )",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "couldn't locate BaseBot.__init__")
        body = m.group(0)
        self.assertIn(
            "self.risk.hydrate_from_trades", body,
            "BaseBot.__init__ doesn't call risk.hydrate_from_trades — "
            "Daily Stats panel will silently reset on every bot restart"
        )
        # Must be AFTER position_manager hydrates trade_history
        idx_pm = body.find("PositionManager(load_history=True)")
        idx_hyd = body.find("hydrate_from_trades")
        self.assertGreater(idx_pm, 0)
        self.assertGreater(idx_hyd, idx_pm,
                           "hydrate_from_trades must come AFTER "
                           "PositionManager(load_history=True)")

    def test_basebot_filters_by_bot_id_before_hydrate(self):
        """CRITICAL: hydration input must be filtered to THIS bot's
        trades only (bot_id == bot_name).

        position_manager.trade_history contains EVERY bot's trades
        (legacy + every per-bot file, via load_all_trades). Hydrating
        risk_manager from the unfiltered list would attribute sim's
        trades to prod and vice versa. Observed live 2026-05-13:
        first-cut fix had both bots showing identical $114.22 because
        they both hydrated from sim's 4 wins."""
        src = (Path(__file__).parent.parent / "bots" / "base_bot.py").read_text(
            encoding="utf-8"
        )
        import re
        m = re.search(
            r"def __init__\(self\).*?(?=\n    (?:async )?def )",
            src, re.DOTALL,
        )
        body = m.group(0)
        # Extract the lines between "PositionManager(load_history=True)" and
        # "hydrate_from_trades" — the bot_id filter must live in this gap.
        idx_pm = body.find("PositionManager(load_history=True)")
        idx_hyd = body.find("hydrate_from_trades")
        gap = body[idx_pm:idx_hyd]
        self.assertIn(
            "bot_id", gap,
            "between PositionManager and hydrate_from_trades, the code "
            "must filter trade_history by bot_id. Without it, every bot "
            "hydrates from EVERY bot's trades — cross-attribution bug."
        )
        self.assertIn(
            "self.bot_name", gap,
            "filter must compare bot_id against self.bot_name"
        )


if __name__ == "__main__":
    unittest.main()
