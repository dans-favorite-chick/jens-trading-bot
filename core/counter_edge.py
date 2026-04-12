"""
Phoenix Bot -- Counter-Edge Engine

Mines losing trade clusters for opposite-direction signals.
When a setup fails repeatedly under certain conditions,
the OPPOSITE trade may have edge.

Example: Spring longs that fail near VWAP in AFTERNOON_CHOP
-> maybe short continuation is the real play there.

OBSERVATION + ADVISORY -- returns counter-signal data, does NOT block or force trades.
"""

import json
import logging
import os
import time
from collections import defaultdict

logger = logging.getLogger("CounterEdge")

MIN_LOSSES_FOR_COUNTER = 3  # Need 3+ losses in same pattern to flag


class CounterEdgeEngine:
    """
    When a setup fails repeatedly under certain conditions,
    the OPPOSITE trade may have edge.
    """

    def __init__(self):
        self._loss_patterns: dict[str, list] = {}  # {pattern_key: [loss_records]}
        self._counter_signals: list[dict] = []      # Generated counter-edge opportunities
        self._file = "logs/performance/counter_edge.json"
        self._load()

    def _load(self):
        """Load persisted loss patterns from disk."""
        try:
            if os.path.exists(self._file):
                with open(self._file, "r") as f:
                    data = json.load(f)
                self._loss_patterns = data.get("loss_patterns", {})
                self._counter_signals = data.get("counter_signals", [])
                logger.info(f"[COUNTER] Loaded {len(self._loss_patterns)} loss patterns, "
                           f"{len(self._counter_signals)} counter signals")
        except Exception as e:
            logger.debug(f"[COUNTER] Load error (starting fresh): {e}")
            self._loss_patterns = {}
            self._counter_signals = []

    def _save(self):
        """Persist to disk."""
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({
                    "loss_patterns": self._loss_patterns,
                    "counter_signals": self._counter_signals[-50:],  # Keep last 50
                    "updated": time.time(),
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"[COUNTER] Save error (non-blocking): {e}")

    @staticmethod
    def _make_pattern_key(trade: dict) -> str:
        """
        Build pattern key from trade conditions.
        Pattern key = "{strategy}_{regime}_{direction}_{condition}"
        where condition captures additional context like VWAP relation.
        """
        strategy = trade.get("strategy", "unknown")
        snapshot = trade.get("market_snapshot", {})
        regime = snapshot.get("regime", "UNKNOWN")
        direction = trade.get("direction", "?")

        # Add VWAP relation as condition
        entry_price = trade.get("entry_price", 0)
        vwap = snapshot.get("vwap", 0)
        if vwap > 0 and entry_price > 0:
            if abs(entry_price - vwap) / 0.25 <= 5:
                condition = "near_vwap"
            elif entry_price > vwap:
                condition = "above_vwap"
            else:
                condition = "below_vwap"
        else:
            condition = "unknown_vwap"

        return f"{strategy}_{regime}_{direction}_{condition}"

    def learn_from_loss(self, trade: dict):
        """
        Extract loss pattern and check if it qualifies as a counter-signal.

        Args:
            trade: Trade dict from position_manager.close_position()
                   Must have result == "LOSS"
        """
        if trade.get("result") != "LOSS":
            return

        pattern_key = self._make_pattern_key(trade)

        # Build compact loss record
        loss_record = {
            "trade_id": trade.get("trade_id", ""),
            "direction": trade.get("direction", ""),
            "strategy": trade.get("strategy", ""),
            "entry_price": trade.get("entry_price", 0),
            "exit_price": trade.get("exit_price", 0),
            "pnl_ticks": trade.get("pnl_ticks", 0),
            "pnl_dollars": trade.get("pnl_dollars", 0),
            "timestamp": time.time(),
        }

        if pattern_key not in self._loss_patterns:
            self._loss_patterns[pattern_key] = []
        self._loss_patterns[pattern_key].append(loss_record)

        # Cap at 20 losses per pattern (sliding window)
        if len(self._loss_patterns[pattern_key]) > 20:
            self._loss_patterns[pattern_key] = self._loss_patterns[pattern_key][-20:]

        # Check if this pattern now qualifies as a counter-signal
        losses = self._loss_patterns[pattern_key]
        if len(losses) >= MIN_LOSSES_FOR_COUNTER:
            avg_pnl = sum(l["pnl_dollars"] for l in losses) / len(losses)
            original_direction = trade.get("direction", "LONG")
            counter_direction = "SHORT" if original_direction == "LONG" else "LONG"

            counter = {
                "pattern_key": pattern_key,
                "counter_direction": counter_direction,
                "loss_count": len(losses),
                "avg_loss_pnl": round(avg_pnl, 2),
                "description": (
                    f"{len(losses)}x {original_direction} losses in "
                    f"{trade.get('strategy', '?')} during "
                    f"{trade.get('market_snapshot', {}).get('regime', '?')} "
                    f"-- consider {counter_direction}"
                ),
                "created": time.time(),
            }

            # Update or add to counter_signals
            existing = next(
                (i for i, cs in enumerate(self._counter_signals)
                 if cs["pattern_key"] == pattern_key),
                None,
            )
            if existing is not None:
                self._counter_signals[existing] = counter
            else:
                self._counter_signals.append(counter)

            logger.info(
                f"[COUNTER] New counter-edge: {pattern_key} -> "
                f"{counter_direction} ({len(losses)} losses, avg ${avg_pnl:.2f})"
            )

        self._save()

    def check_counter_signal(self, strategy: str, direction: str,
                              regime: str, market: dict) -> dict | None:
        """
        Before entering a trade, check if this setup has a known
        counter-edge (3+ historical losses in same conditions).

        Args:
            strategy: Strategy name
            direction: Proposed trade direction ("LONG" or "SHORT")
            regime: Current market regime
            market: tick_aggregator.snapshot() dict

        Returns: {
            counter_direction: "LONG" | "SHORT",
            loss_count: int,
            avg_loss_pnl: float,
            description: str,
        } or None if no counter-edge found.
        """
        # Build the pattern key for this proposed trade
        entry_price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        if vwap > 0 and entry_price > 0:
            if abs(entry_price - vwap) / 0.25 <= 5:
                condition = "near_vwap"
            elif entry_price > vwap:
                condition = "above_vwap"
            else:
                condition = "below_vwap"
        else:
            condition = "unknown_vwap"

        pattern_key = f"{strategy}_{regime}_{direction}_{condition}"

        # Check if we have a counter-signal for this exact pattern
        for cs in self._counter_signals:
            if cs["pattern_key"] == pattern_key:
                # Only return if losses are recent (within last 7 days)
                age_days = (time.time() - cs.get("created", 0)) / 86400
                if age_days <= 7:
                    logger.info(
                        f"[COUNTER] Found counter-edge for {pattern_key}: "
                        f"{cs['counter_direction']} ({cs['loss_count']} losses)"
                    )
                    return {
                        "counter_direction": cs["counter_direction"],
                        "loss_count": cs["loss_count"],
                        "avg_loss_pnl": cs["avg_loss_pnl"],
                        "description": cs["description"],
                    }

        return None

    def to_dict(self) -> dict:
        """For dashboard and AI debrief."""
        # Summarize active counter-signals
        active = []
        now = time.time()
        for cs in self._counter_signals:
            age_days = (now - cs.get("created", 0)) / 86400
            if age_days <= 7:
                active.append({
                    "pattern": cs["pattern_key"],
                    "counter_direction": cs["counter_direction"],
                    "loss_count": cs["loss_count"],
                    "avg_loss_pnl": cs["avg_loss_pnl"],
                    "age_days": round(age_days, 1),
                })

        return {
            "total_patterns": len(self._loss_patterns),
            "active_counter_signals": len(active),
            "signals": active[:10],  # Top 10 for dashboard
        }
