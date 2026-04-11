"""
Phoenix Bot — Strategy Performance Tracker

Tracks per-strategy win/loss, P&L, regime performance, and signal quality
metrics. Persists to JSON for AI learning and parameter tuning.

This data feeds into:
  - Session Debriefer (coaching analysis)
  - Council Gate (trade performance context)
  - Future: Adaptive parameter tuning (Phase 5)
"""

import json
import os
import logging
from datetime import datetime, date
from collections import defaultdict
from typing import Optional

logger = logging.getLogger("StrategyTracker")

TRACKER_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "performance")


class StrategyTracker:
    """
    Tracks cumulative + daily per-strategy performance.
    Writes daily snapshots and maintains rolling stats.
    """

    def __init__(self):
        os.makedirs(TRACKER_DIR, exist_ok=True)
        self._cumulative_file = os.path.join(TRACKER_DIR, "cumulative.json")
        self._daily_file = os.path.join(
            TRACKER_DIR, f"daily_{date.today().isoformat()}.json"
        )

        # Per-strategy cumulative stats
        self.strategies: dict[str, dict] = {}
        # Per-strategy + per-regime stats
        self.regime_stats: dict[str, dict[str, dict]] = {}
        # Signal tracking (generated vs. taken vs. filtered)
        self.signal_log: list[dict] = []

        self._load_cumulative()

    def _default_strategy_stats(self) -> dict:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "max_win": 0.0,
            "max_loss": 0.0,
            "avg_hold_time_s": 0.0,
            "avg_win_pnl": 0.0,
            "avg_loss_pnl": 0.0,
            "consecutive_wins_max": 0,
            "consecutive_losses_max": 0,
            "_consecutive_wins": 0,
            "_consecutive_losses": 0,
            "last_10_results": [],  # ["WIN", "LOSS", ...]
        }

    def _load_cumulative(self):
        try:
            if os.path.exists(self._cumulative_file):
                with open(self._cumulative_file, "r") as f:
                    data = json.load(f)
                self.strategies = data.get("strategies", {})
                self.regime_stats = data.get("regime_stats", {})
                logger.info(f"Loaded cumulative stats for {len(self.strategies)} strategies")
        except Exception as e:
            logger.warning(f"Could not load cumulative stats: {e}")

    def _save_cumulative(self):
        try:
            with open(self._cumulative_file, "w") as f:
                json.dump({
                    "strategies": self.strategies,
                    "regime_stats": self.regime_stats,
                    "last_updated": datetime.now().isoformat(),
                }, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Could not save cumulative stats: {e}")

    def record_trade(self, trade: dict):
        """
        Record a completed trade and update all performance metrics.

        Args:
            trade: Full trade dict from PositionManager.close_position()
        """
        strategy = trade.get("strategy", "unknown")
        pnl = trade.get("pnl_dollars", 0)
        result = trade.get("result", "LOSS")
        hold_time = trade.get("hold_time_s", 0)
        regime = trade.get("market_snapshot", {}).get("regime", "UNKNOWN")
        trade_id = trade.get("trade_id", "")

        # Init strategy stats if needed
        if strategy not in self.strategies:
            self.strategies[strategy] = self._default_strategy_stats()
        s = self.strategies[strategy]

        # Update core stats
        s["total_trades"] += 1
        s["total_pnl"] = round(s["total_pnl"] + pnl, 2)

        if result == "WIN":
            s["wins"] += 1
            s["max_win"] = max(s["max_win"], pnl)
            s["_consecutive_wins"] += 1
            s["_consecutive_losses"] = 0
            s["consecutive_wins_max"] = max(s["consecutive_wins_max"], s["_consecutive_wins"])
        else:
            s["losses"] += 1
            s["max_loss"] = min(s["max_loss"], pnl)
            s["_consecutive_losses"] += 1
            s["_consecutive_wins"] = 0
            s["consecutive_losses_max"] = max(s["consecutive_losses_max"], s["_consecutive_losses"])

        # Rolling averages
        if s["wins"] > 0:
            total_win_pnl = s.get("_total_win_pnl", 0) + (pnl if result == "WIN" else 0)
            s["_total_win_pnl"] = total_win_pnl
            s["avg_win_pnl"] = round(total_win_pnl / s["wins"], 2)
        if s["losses"] > 0:
            total_loss_pnl = s.get("_total_loss_pnl", 0) + (pnl if result == "LOSS" else 0)
            s["_total_loss_pnl"] = total_loss_pnl
            s["avg_loss_pnl"] = round(total_loss_pnl / s["losses"], 2)

        total_hold = s.get("_total_hold", 0) + hold_time
        s["_total_hold"] = total_hold
        s["avg_hold_time_s"] = round(total_hold / s["total_trades"], 1)

        # Last 10 results (for pattern detection)
        s["last_10_results"].append(result)
        s["last_10_results"] = s["last_10_results"][-10:]

        # Per-regime stats
        if strategy not in self.regime_stats:
            self.regime_stats[strategy] = {}
        if regime not in self.regime_stats[strategy]:
            self.regime_stats[strategy][regime] = {
                "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0
            }
        rs = self.regime_stats[strategy][regime]
        rs["trades"] += 1
        rs["pnl"] = round(rs["pnl"] + pnl, 2)
        if result == "WIN":
            rs["wins"] += 1
        else:
            rs["losses"] += 1

        # Log
        logger.info(f"[TRACKER:{trade_id}] {strategy} in {regime}: {result} ${pnl:.2f} "
                     f"(total: {s['total_trades']} trades, {s['wins']}W/{s['losses']}L, "
                     f"${s['total_pnl']:.2f})")

        self._save_cumulative()
        self._save_daily_snapshot()

    def record_signal(self, strategy: str, direction: str, confidence: float,
                      taken: bool, filter_action: str = "CLEAR",
                      regime: str = "UNKNOWN", trade_id: str = ""):
        """Record every signal (taken or filtered) for signal quality analysis."""
        self.signal_log.append({
            "ts": datetime.now().isoformat(),
            "trade_id": trade_id,
            "strategy": strategy,
            "direction": direction,
            "confidence": confidence,
            "taken": taken,
            "filter_action": filter_action,
            "regime": regime,
        })
        # Keep last 200 signals in memory
        self.signal_log = self.signal_log[-200:]

    def _save_daily_snapshot(self):
        """Write today's performance snapshot for AI debrief analysis."""
        try:
            self._daily_file = os.path.join(
                TRACKER_DIR, f"daily_{date.today().isoformat()}.json"
            )
            with open(self._daily_file, "w") as f:
                json.dump({
                    "date": date.today().isoformat(),
                    "strategies": self.strategies,
                    "regime_stats": self.regime_stats,
                    "recent_signals": self.signal_log[-50:],
                    "snapshot_time": datetime.now().isoformat(),
                }, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Could not save daily snapshot: {e}")

    def get_strategy_summary(self, strategy: str) -> dict:
        """Get summary for a specific strategy (for AI agents)."""
        s = self.strategies.get(strategy, self._default_strategy_stats())
        win_rate = round(s["wins"] / max(1, s["total_trades"]) * 100, 1)
        return {
            "strategy": strategy,
            "total_trades": s["total_trades"],
            "win_rate": win_rate,
            "total_pnl": s["total_pnl"],
            "avg_win": s["avg_win_pnl"],
            "avg_loss": s["avg_loss_pnl"],
            "max_win": s["max_win"],
            "max_loss": s["max_loss"],
            "last_10": s["last_10_results"],
            "regime_breakdown": self.regime_stats.get(strategy, {}),
        }

    def get_all_summaries(self) -> dict:
        """Get all strategy summaries (for dashboard and AI)."""
        return {
            name: self.get_strategy_summary(name)
            for name in self.strategies
        }

    def to_dict(self) -> dict:
        """Serialize for dashboard display."""
        summaries = self.get_all_summaries()
        return {
            "strategies": summaries,
            "total_signals_tracked": len(self.signal_log),
        }
