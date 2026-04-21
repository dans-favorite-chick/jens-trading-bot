"""
Phoenix Bot — Trade RAG
ChromaDB-based similarity search over historical trade setups.
Queries: "What happened when we saw similar conditions before?"
Local only (no cloud). Falls back gracefully if chromadb not installed.
"""

import json
import logging
import os
from collections import Counter
from datetime import datetime

import numpy as np

logger = logging.getLogger("TradeRAG")

try:
    import chromadb
    HAS_CHROMADB = True
except ImportError:
    HAS_CHROMADB = False
    logger.warning("[TradeRAG] chromadb not installed — RAG disabled")

# --- Feature extraction constants ---
REGIME_MAP = {"trending_up": 0.0, "trending_down": 0.2, "range_bound": 0.4,
              "volatile": 0.6, "quiet": 0.8, "unknown": 0.5}
EMPTY_RESULT = {
    "n_similar": 0, "win_rate": 0.0, "avg_pnl_ticks": 0.0, "avg_mae_ticks": 0.0,
    "avg_mfe_ticks": 0.0, "avg_capture_ratio": 0.0, "best_strategy": "none",
    "regime_breakdown": {}, "similarity_scores": [], "recommendation": "NO_EDGE",
    "details": [],
}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _market_to_vector(market: dict, direction: str) -> list[float]:
    """Convert market snapshot + direction into a normalized 0-1 feature vector."""
    price = market.get("price", 0)
    vwap = market.get("vwap", price)
    atr = max(market.get("atr_5m", 1.0), 0.01)

    vec = [
        _clamp((price - vwap) / atr * 0.5 + 0.5),          # price_vs_vwap
        _clamp(atr / 50.0),                                  # atr_5m (50pt = max)
        _clamp(market.get("cvd_slope", 0) * 0.5 + 0.5),     # cvd_slope
        _clamp(market.get("dom_imbalance", 0.5)),            # dom_imbalance
        _clamp(market.get("tf_votes_bullish", 0) / 4.0),    # tf_votes_bullish
        _clamp(market.get("tf_votes_bearish", 0) / 4.0),    # tf_votes_bearish
        _clamp(market.get("ema9_vs_ema21", 0) * 0.5 + 0.5), # ema9_vs_ema21
        _clamp(market.get("bar_delta", 0) * 0.5 + 0.5),     # bar_delta
        _clamp(market.get("minutes_since_open", 90) / 390),  # minutes_since_open
        REGIME_MAP.get(str(market.get("regime", "unknown")).lower(), 0.5),
        1.0 if direction.upper() == "LONG" else 0.0,         # direction
    ]
    return vec


def _classify_edge(win_rate: float, avg_pnl: float, n: int) -> str:
    if n < 3:
        return "NO_EDGE"
    if win_rate >= 65 and avg_pnl > 0:
        return "STRONG_EDGE"
    if win_rate >= 50 and avg_pnl > 0:
        return "MODERATE_EDGE"
    if avg_pnl < 0:
        return "NEGATIVE_EDGE"
    return "NO_EDGE"


class TradeRAG:
    """ChromaDB-backed similarity search for historical trade setups."""

    def __init__(self, db_path: str = "data/trade_vectors"):
        self._available = HAS_CHROMADB
        self._collection = None
        self._db_path = db_path
        if self._available:
            try:
                os.makedirs(db_path, exist_ok=True)
                client = chromadb.PersistentClient(path=db_path)
                self._collection = client.get_or_create_collection(
                    name="phoenix_trades",
                    metadata={"hnsw:space": "cosine"},
                )
                logger.info(f"[TradeRAG] Collection loaded — {self._collection.count()} vectors")
            except Exception as e:
                logger.error(f"[TradeRAG] ChromaDB init failed: {e}")
                self._available = False

    # ─── Write ──────────────────────────────────────────────────────

    def _build_meta(self, typ: str, direction: str, trade: dict,
                    outcome: dict, market: dict) -> dict:
        return {
            "type": typ, "direction": direction,
            "strategy": str(trade.get("strategy", "unknown")),
            "result": str(trade.get("result", "UNKNOWN")),
            "pnl_ticks": float(trade.get("pnl_ticks", 0)),
            "mae_ticks": float(outcome.get("mae_ticks", 0)),
            "mfe_ticks": float(outcome.get("mfe_ticks", 0)),
            "capture_ratio": float(outcome.get("capture_ratio", 0)),
            "hold_seconds": float(outcome.get("hold_seconds", 0)),
            "exit_reason": str(outcome.get("exit_reason", trade.get("skip_reason", ""))),
            "regime": str(market.get("regime", "unknown")),
            "entry_price": float(trade.get("entry_price", 0)),
            "timestamp": datetime.now().isoformat(),
        }

    def add_trade(self, trade: dict, market: dict, outcome: dict):
        """Store a completed trade with its market context and outcome."""
        if not self._available:
            return
        try:
            direction = trade.get("direction", "LONG")
            vec = _market_to_vector(market, direction)
            doc_id = f"trade_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{id(trade) % 10000}"
            meta = self._build_meta("trade", direction, trade, outcome, market)
            self._collection.add(ids=[doc_id], embeddings=[vec], metadatas=[meta])
            logger.debug(f"[TradeRAG] Added trade {doc_id}")
        except Exception as e:
            logger.error(f"[TradeRAG] add_trade error: {e}")

    def add_near_miss(self, signal: dict, market: dict, hypothetical_outcome: dict = None):
        """Store a signal that was generated but not taken."""
        if not self._available:
            return
        try:
            direction = signal.get("direction", "LONG")
            vec = _market_to_vector(market, direction)
            doc_id = f"miss_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{id(signal) % 10000}"
            stub = {"result": "SKIPPED", "pnl_ticks": 0}
            meta = self._build_meta("near_miss", direction, {**signal, **stub}, {}, market)
            if hypothetical_outcome:
                meta["hyp_pnl_ticks"] = float(hypothetical_outcome.get("pnl_ticks", 0))
                meta["hyp_result"] = str(hypothetical_outcome.get("result", "UNKNOWN"))
            self._collection.add(ids=[doc_id], embeddings=[vec], metadatas=[meta])
            logger.debug(f"[TradeRAG] Added near-miss {doc_id}")
        except Exception as e:
            logger.error(f"[TradeRAG] add_near_miss error: {e}")

    # ─── Read ───────────────────────────────────────────────────────

    def query_similar(self, market: dict, direction: str, k: int = 10) -> dict:
        """Find K most similar historical setups and return aggregated stats."""
        if not self._available or self._collection is None:
            return dict(EMPTY_RESULT)
        try:
            vec = _market_to_vector(market, direction)
            results = self._collection.query(
                query_embeddings=[vec],
                n_results=min(k, max(self._collection.count(), 1)),
                where={"type": "trade"},
            )
            metas = results.get("metadatas", [[]])[0]
            dists = results.get("distances", [[]])[0]
            if not metas:
                return dict(EMPTY_RESULT)

            sims = [round(1.0 - d, 4) for d in dists]  # cosine dist -> similarity
            n = len(metas)
            wins = sum(1 for m in metas if m.get("result") == "WIN")
            wr = round(wins / n * 100, 1) if n else 0.0
            _avg = lambda key: round(float(np.mean([m.get(key, 0) for m in metas])), 2)
            win_strats = [m["strategy"] for m in metas if m.get("result") == "WIN"]
            best = Counter(win_strats).most_common(1)[0][0] if win_strats else "none"
            reg: dict[str, list] = {}
            for m in metas:
                reg.setdefault(m.get("regime", "unknown"), []).append(m.get("result") == "WIN")
            return {
                "n_similar": n, "win_rate": wr,
                "avg_pnl_ticks": _avg("pnl_ticks"), "avg_mae_ticks": _avg("mae_ticks"),
                "avg_mfe_ticks": _avg("mfe_ticks"), "avg_capture_ratio": _avg("capture_ratio"),
                "best_strategy": best,
                "regime_breakdown": {r: round(sum(v)/len(v)*100, 1) for r, v in reg.items()},
                "similarity_scores": sims,
                "recommendation": _classify_edge(wr, _avg("pnl_ticks"), n),
                "details": [{**m, "similarity": s} for m, s in zip(metas, sims)],
            }
        except Exception as e:
            logger.error(f"[TradeRAG] query_similar error: {e}")
            return dict(EMPTY_RESULT)

    # ─── Bootstrap ──────────────────────────────────────────────────

    def load_from_history(self, history_dir: str):
        """Bootstrap from existing JSONL history files."""
        if not self._available:
            return
        loaded = 0
        for fname in sorted(os.listdir(history_dir)):
            if not fname.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(history_dir, fname), "r", encoding="utf-8") as f:
                    entries, exits = [], {}
                    for line in f:
                        evt = json.loads(line.strip())
                        if evt.get("event") == "entry":
                            entries.append(evt)
                        elif evt.get("event") == "exit":
                            exits[evt.get("trade_id", id(evt))] = evt
                for entry in entries:
                    ex = exits.get(entry.get("trade_id", id(entry)))
                    if not ex:
                        continue
                    market = entry.get("market", entry.get("snapshot", {}))
                    trade = {k: entry.get(k, d) for k, d in [
                        ("direction", "LONG"), ("strategy", "unknown"),
                        ("entry_price", 0), ("pnl_ticks", ex.get("pnl_ticks", 0)),
                        ("result", ex.get("result", "UNKNOWN"))]}
                    trade["exit_price"] = ex.get("exit_price", 0)
                    trade["pnl_ticks"] = ex.get("pnl_ticks", 0)
                    trade["result"] = ex.get("result", "UNKNOWN")
                    outcome = {k: ex.get(k, 0) for k in [
                        "mae_ticks", "mfe_ticks", "capture_ratio", "hold_seconds"]}
                    outcome["exit_reason"] = ex.get("exit_reason", "")
                    self.add_trade(trade, market, outcome)
                    loaded += 1
            except Exception as e:
                logger.warning(f"[TradeRAG] Error reading {fname}: {e}")
        logger.info(f"[TradeRAG] Loaded {loaded} trades from history")

    # ─── Info ───────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Collection size and basic info."""
        if not self._available or self._collection is None:
            return {"available": False, "count": 0}
        count = self._collection.count()
        return {"available": True, "count": count, "db_path": self._db_path}

    def to_dict(self) -> dict:
        """For dashboard state."""
        stats = self.get_stats()
        return {
            "rag_available": stats.get("available", False),
            "rag_vector_count": stats.get("count", 0),
        }
