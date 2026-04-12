"""
Phoenix Bot -- Live Expectancy Engine

Tracks MAE, MFE, slippage, and realized expectancy for every trade.
Transforms binary win/loss into rich learning events.

Feeds into:
  - Session Debriefer (coaching analysis)
  - No-Trade Fingerprint (loss pattern learning)
  - Dashboard (live excursion display)
  - Future: Adaptive stop/target tuning
"""

import json
import os
import time
import logging

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import TICK_SIZE

logger = logging.getLogger("ExpectancyEngine")

PERF_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "performance")


class ExpectancyEngine:
    """
    Tracks MAE, MFE, slippage, and realized expectancy for every trade.
    This transforms binary win/loss into rich learning events.
    """

    def __init__(self):
        self._active_trade = None   # Currently tracking
        self._trade_history = []    # Completed analyses
        self._file = os.path.join(PERF_DIR, "expectancy.json")
        self._load()

    # ─── Trade Lifecycle ───────────────────────────────────────────

    def start_tracking(self, trade_id: str, direction: str, entry_price: float,
                       signal_price: float, stop_price: float, target_price: float,
                       strategy: str, regime: str):
        """Call when a trade entry is confirmed. Begin tracking price excursions."""
        # Compute entry slippage (positive = unfavorable)
        if direction == "LONG":
            slippage = (entry_price - signal_price) / TICK_SIZE
        else:
            slippage = (signal_price - entry_price) / TICK_SIZE

        self._active_trade = {
            "trade_id": trade_id,
            "direction": direction,
            "entry_price": entry_price,
            "signal_price": signal_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "strategy": strategy,
            "regime": regime,
            "entry_slippage_ticks": round(slippage, 1),
            "mae": 0.0,           # Max Adverse Excursion (worst drawdown in ticks)
            "mfe": 0.0,           # Max Favorable Excursion (best unrealized in ticks)
            "mae_time_s": 0,      # Time to reach MAE
            "mfe_time_s": 0,      # Time to reach MFE
            "entry_time": time.time(),
            "tick_path": [],      # Recent price path for analysis (last 50)
        }
        logger.info(f"[EXPECTANCY:{trade_id}] Tracking started: {direction} @ {entry_price:.2f} "
                     f"slippage={slippage:+.1f}t")

    def update_tick(self, current_price: float):
        """Call on every tick while in a trade. Updates MAE/MFE."""
        if not self._active_trade:
            return

        t = self._active_trade
        elapsed = time.time() - t["entry_time"]

        if t["direction"] == "LONG":
            excursion = (current_price - t["entry_price"]) / TICK_SIZE
        else:
            excursion = (t["entry_price"] - current_price) / TICK_SIZE

        # Update MAE (most negative excursion -- how bad did it get?)
        if excursion < t["mae"]:
            t["mae"] = round(excursion, 1)
            t["mae_time_s"] = round(elapsed, 1)

        # Update MFE (most positive excursion -- how good did it get?)
        if excursion > t["mfe"]:
            t["mfe"] = round(excursion, 1)
            t["mfe_time_s"] = round(elapsed, 1)

        # Track recent tick path (keep last 50 for pattern analysis)
        t["tick_path"].append(round(excursion, 1))
        t["tick_path"] = t["tick_path"][-50:]

    def close_trade(self, exit_price: float, pnl_ticks: float, result: str) -> dict:
        """Call when trade exits. Compute final expectancy metrics."""
        if not self._active_trade:
            return {}

        t = self._active_trade
        hold_time = time.time() - t["entry_time"]

        # Edge captured: what % of MFE did we actually realize?
        if t["mfe"] > 0:
            edge_captured_pct = round(pnl_ticks / t["mfe"] * 100, 1)
        else:
            edge_captured_pct = 0.0

        # Money left on the table
        mfe_minus_pnl = round(t["mfe"] - pnl_ticks, 1)

        analysis = {
            "trade_id": t["trade_id"],
            "direction": t["direction"],
            "entry_price": t["entry_price"],
            "signal_price": t["signal_price"],
            "stop_price": t["stop_price"],
            "target_price": t["target_price"],
            "strategy": t["strategy"],
            "regime": t["regime"],
            "exit_price": exit_price,
            "pnl_ticks": pnl_ticks,
            "result": result,
            "hold_time_s": round(hold_time, 1),
            # Key metrics
            "entry_slippage_ticks": t["entry_slippage_ticks"],
            "mae_ticks": t["mae"],
            "mfe_ticks": t["mfe"],
            "mae_time_s": t["mae_time_s"],
            "mfe_time_s": t["mfe_time_s"],
            "edge_captured_pct": edge_captured_pct,
            "mfe_minus_pnl": mfe_minus_pnl,
            # Behavioral insights
            "went_red_first": t["mae"] < -1,
            "recovered_from_mae": result == "WIN" and t["mae"] < -2,
            "timestamp": time.time(),
        }

        self._trade_history.append(analysis)
        self._active_trade = None
        self._save()

        logger.info(f"[EXPECTANCY:{analysis['trade_id']}] {result} "
                     f"MAE={analysis['mae_ticks']:+.1f}t MFE={analysis['mfe_ticks']:+.1f}t "
                     f"edge_captured={edge_captured_pct:.0f}% "
                     f"slippage={analysis['entry_slippage_ticks']:+.1f}t "
                     f"left_on_table={mfe_minus_pnl:.1f}t")

        return analysis

    @property
    def is_tracking(self) -> bool:
        return self._active_trade is not None

    # ─── Aggregate Statistics ──────────────────────────────────────

    def get_strategy_expectancy(self, strategy: str = None, regime: str = None) -> dict:
        """Compute realized expectancy stats filtered by strategy/regime."""
        trades = self._trade_history
        if strategy:
            trades = [t for t in trades if t.get("strategy") == strategy]
        if regime:
            trades = [t for t in trades if t.get("regime") == regime]
        if not trades:
            return {"trades": 0}

        wins = [t for t in trades if t["result"] == "WIN"]
        losses = [t for t in trades if t["result"] == "LOSS"]
        n = len(trades)

        return {
            "trades": n,
            "win_rate": round(len(wins) / n * 100, 1),
            "avg_pnl_ticks": round(sum(t["pnl_ticks"] for t in trades) / n, 1),
            "avg_mae": round(sum(t["mae_ticks"] for t in trades) / n, 1),
            "avg_mfe": round(sum(t["mfe_ticks"] for t in trades) / n, 1),
            "avg_slippage": round(sum(t["entry_slippage_ticks"] for t in trades) / n, 1),
            "avg_edge_captured_pct": round(sum(t["edge_captured_pct"] for t in trades) / n, 1),
            "avg_mfe_minus_pnl": round(sum(t["mfe_minus_pnl"] for t in trades) / n, 1),
            "pct_went_red_first": round(sum(1 for t in trades if t["went_red_first"]) / n * 100, 1),
            "pct_recovered_from_mae": round(
                sum(1 for t in trades if t["recovered_from_mae"]) / n * 100, 1
            ) if wins else 0,
            "avg_time_to_mfe_s": round(sum(t["mfe_time_s"] for t in trades) / n, 1),
            "avg_time_to_mae_s": round(sum(t["mae_time_s"] for t in trades) / n, 1),
            # Stop analysis: how many losses would have won with more room?
            "would_win_with_wider_stop": sum(1 for t in losses if t["mfe_ticks"] > 5),
        }

    def get_recent_analyses(self, n: int = 10) -> list[dict]:
        """Return last N trade analyses for dashboard detail view."""
        return self._trade_history[-n:]

    # ─── Dashboard ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """For dashboard display."""
        overall = self.get_strategy_expectancy()
        active = None
        if self._active_trade:
            t = self._active_trade
            active = {
                "trade_id": t["trade_id"],
                "direction": t["direction"],
                "mae": t["mae"],
                "mfe": t["mfe"],
                "slippage": t["entry_slippage_ticks"],
                "elapsed_s": round(time.time() - t["entry_time"], 0),
            }

        return {
            "active_tracking": active,
            "overall": overall,
            "total_analyzed": len(self._trade_history),
            "recent": self.get_recent_analyses(5),
        }

    # ─── Persistence ───────────────────────────────────────────────

    def _load(self):
        """Load trade history from disk."""
        try:
            os.makedirs(PERF_DIR, exist_ok=True)
            if os.path.exists(self._file):
                with open(self._file, "r") as f:
                    data = json.load(f)
                self._trade_history = data.get("trades", [])
                logger.info(f"Loaded {len(self._trade_history)} expectancy records")
        except Exception as e:
            logger.warning(f"Could not load expectancy data: {e}")
            self._trade_history = []

    def _save(self):
        """Persist trade history to disk."""
        try:
            os.makedirs(PERF_DIR, exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({
                    "trades": self._trade_history,
                    "last_updated": time.time(),
                    "total": len(self._trade_history),
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save expectancy data: {e}")
