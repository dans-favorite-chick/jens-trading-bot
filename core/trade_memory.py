"""
Phoenix Bot — Trade Memory

Persists trade history to JSON file for adaptive learning.
Phase 2: will feed into trade clustering analysis (MNQ v5 Upgrade #4).

2026-05-12: per-bot file split. Previously prod and sim both wrote to
`logs/trade_memory.json` with whole-file rewrites in their own
processes. The last writer always won — sim's longer uptime meant it
nearly always wrote after prod, so prod's just-recorded trade got
overwritten with sim's (older) in-memory view. Result: prod entries
appeared in history.jsonl but never in trade_memory.json, dashboard
showed prod with 0 trades despite the bot actually trading.

Fix: each bot writes to its own `logs/trade_memory_<bot_id>.json`.
The legacy `logs/trade_memory.json` is preserved read-only — it still
holds 1,250+ historical trades from before the split, and downstream
tools (validation_tracker, indicator_audit, etc.) read it directly.
Per-bot files accumulate from today forward; the merged view (legacy
+ all per-bot files, deduplicated) is provided by `load_all_trades()`
for callers who want everything in one list.
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("TradeMemory")

# Legacy shared file (pre-2026-05-12 split). Still loaded for read,
# never written. New writes go to LEGACY_FILE-replaced per-bot files.
LEGACY_FILE = "logs/trade_memory.json"


def _per_bot_path(bot_id: str) -> str:
    return f"logs/trade_memory_{bot_id}.json"


def load_all_trades(logs_dir: str = "logs") -> list[dict]:
    """Read legacy file + every `trade_memory_<bot>.json` and merge.

    Per-bot files take precedence (newer schema). Trades are deduped
    by `trade_id` when both sources have the same id, with the per-bot
    file winning. Trades without `trade_id` are kept as-is.

    ``logs_dir`` overrides the default ``logs/`` (CWD-relative) — used
    by tests that redirect to a tmp_path, and by the dashboard which
    passes an absolute ``PROJECT_ROOT/logs`` path.

    Used by dashboard `_load_session_trades_by_bot` and any tool that
    wants the unified history across bots.
    """
    out: list[dict] = []
    seen_ids: set = set()

    # Per-bot files first so they win the dedupe.
    try:
        for fname in sorted(os.listdir(logs_dir)):
            if not (fname.startswith("trade_memory_") and fname.endswith(".json")):
                continue
            path = os.path.join(logs_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rows = json.load(f)
                if not isinstance(rows, list):
                    continue
                for t in rows:
                    tid = t.get("trade_id")
                    if tid and tid in seen_ids:
                        continue
                    if tid:
                        seen_ids.add(tid)
                    out.append(t)
            except Exception as e:
                logger.warning(f"load_all_trades skip {fname}: {e}")
    except FileNotFoundError:
        pass

    # Legacy file last; per-bot files override matching trade_ids.
    legacy_path = os.path.join(logs_dir, "trade_memory.json")
    if os.path.exists(legacy_path):
        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, list):
                for t in rows:
                    tid = t.get("trade_id")
                    if tid and tid in seen_ids:
                        continue
                    out.append(t)
        except Exception as e:
            logger.warning(f"load_all_trades legacy read failed: {e}")

    return out


class TradeMemory:
    def __init__(
        self,
        filepath: Optional[str] = None,
        bot_id: Optional[str] = None,
    ):
        """Per-bot trade memory.

        - ``filepath`` explicitly overrides the path (tests, tools).
        - ``bot_id`` (e.g. "prod" / "sim" / "lab") → uses per-bot file
          `logs/trade_memory_<bot_id>.json`.
        - If both are None, falls back to the legacy shared file
          (backwards compat for callers that don't pass either).

        On first load with a per-bot file, this bot's trades from the
        legacy file are seeded in-memory (read-only) so `recent()`
        and `by_strategy()` see the full history. New writes go ONLY
        to the per-bot file — the legacy file is never modified.
        """
        if filepath is not None:
            self.filepath = filepath
            self._bot_id = bot_id
        elif bot_id is not None:
            self.filepath = _per_bot_path(bot_id)
            self._bot_id = bot_id
        else:
            self.filepath = LEGACY_FILE
            self._bot_id = None
        self.trades: list[dict] = []
        self._load()

    def _load(self):
        # Per-bot file first.
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r") as f:
                    self.trades = json.load(f)
                logger.info(
                    f"Loaded {len(self.trades)} trades from {self.filepath}"
                )
        except Exception as e:
            logger.warning(f"Could not load {self.filepath}: {e}")
            self.trades = []

        # If we're a per-bot file, seed in-memory with any legacy trades
        # tagged with our bot_id so historical data is still visible to
        # recent() / by_strategy() / win_rate() in the running bot.
        if self._bot_id and os.path.exists(LEGACY_FILE) and self.filepath != LEGACY_FILE:
            try:
                with open(LEGACY_FILE, "r") as f:
                    legacy = json.load(f)
                if isinstance(legacy, list):
                    seen_ids = {
                        t.get("trade_id") for t in self.trades if t.get("trade_id")
                    }
                    seeded = 0
                    for t in legacy:
                        if t.get("bot_id") != self._bot_id:
                            continue
                        tid = t.get("trade_id")
                        if tid and tid in seen_ids:
                            continue
                        # Prepend (older trades go first by convention).
                        self.trades.insert(0, t)
                        seeded += 1
                    if seeded:
                        logger.info(
                            f"Seeded {seeded} legacy trades for "
                            f"bot_id={self._bot_id} (read-only)"
                        )
            except Exception as e:
                logger.warning(f"legacy seed for {self._bot_id} failed: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, "w") as f:
                json.dump(self.trades, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Could not save trade memory: {e}")

    def record(self, trade: dict, bot_id: str | None = None):
        """
        Persist a closed trade.

        Args:
            trade: Trade dict from position manager.
            bot_id: Originating bot name ("prod" | "lab" | "sim"). Stamped
                    into trade dict so downstream consumers (dashboard,
                    learner) can partition P&L across bots. B16 fix.

        B70 write-time guard: if bot_id is missing/None AND the trade dict
        doesn't already carry a non-null bot_id, default to "unknown" and
        log a WARNING. Prevents future null-bot_id pollution of
        trade_memory.json that breaks the Historical Learner's grouping.
        """
        trade["recorded_at"] = datetime.now().isoformat()
        if bot_id is not None:
            trade["bot_id"] = bot_id
        elif trade.get("bot_id") in (None, ""):
            logger.warning(
                "TradeMemory.record() called with no bot_id; defaulting to "
                "'unknown'. Caller should pass bot_id=... explicitly."
            )
            trade["bot_id"] = "unknown"
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
