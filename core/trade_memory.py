"""
Phoenix Bot — Trade Memory

Persists trade history to JSON file for adaptive learning.
Phase 2: will feed into trade clustering analysis (MNQ v5 Upgrade #4).
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger("TradeMemory")

MEMORY_FILE = "logs/trade_memory.json"


class TradeMemory:
    def __init__(self, filepath: str = MEMORY_FILE):
        self.filepath = filepath
        self.trades: list[dict] = []
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r") as f:
                    self.trades = json.load(f)
                logger.info(f"Loaded {len(self.trades)} trades from memory")
        except Exception as e:
            logger.warning(f"Could not load trade memory: {e}")
            self.trades = []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, "w") as f:
                json.dump(self.trades, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Could not save trade memory: {e}")

    def record(self, trade: dict):
        trade["recorded_at"] = datetime.now().isoformat()
        self.trades.append(trade)
        self.save()

    def recent(self, n: int = 30) -> list[dict]:
        return self.trades[-n:]

    def win_rate(self, last_n: int = 0) -> float:
        trades = self.trades[-last_n:] if last_n else self.trades
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("result") == "WIN")
        return wins / len(trades) * 100

    def by_strategy(self, strategy: str) -> list[dict]:
        return [t for t in self.trades if t.get("strategy") == strategy]

    def by_regime(self, regime: str) -> list[dict]:
        return [t for t in self.trades if t.get("market_snapshot", {}).get("regime") == regime]
