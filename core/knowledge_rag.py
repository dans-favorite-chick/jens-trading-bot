"""
Phoenix Bot — Trading Knowledge RAG

A second ChromaDB collection (alongside trade_rag.py) that stores
trading knowledge — SMC concepts, market profile rules, order flow
principles, macro playbooks, regime-specific rules.

When a strategy generates a signal or the AI analyzes a trade,
it can query: "What does the knowledge base say about taking longs
when VIX is above 20 and we're 30 minutes before FOMC?"
"""

import logging
import os

logger = logging.getLogger("KnowledgeRAG")

# ── Pre-loaded Trading Knowledge ────────────────────────────────────
# These are core rules that get loaded into the vector DB on first run.
# Each entry: (title, content, category)

TRADING_KNOWLEDGE = [
    # ── ICT/SMC Concepts ────────────────────────────────────────────
    ("ICT Kill Zones",
     "The three ICT kill zones are: London Open (2-5 AM EST), NY Open (7-10 AM EST), "
     "and NY PM Session (1:30-4 PM EST). These are when institutional order flow is "
     "highest and the best setups form. Avoid trading outside kill zones unless there's "
     "a clear catalyst.",
     "smc"),

    ("Optimal Trade Entry (OTE)",
     "ICT OTE is the 62-79% Fibonacci retracement zone of a displacement leg. "
     "After a market structure break, wait for price to retrace into the OTE zone "
     "before entering. This gives the best risk:reward ratio.",
     "smc"),

    ("Liquidity Sweep Rules",
     "Liquidity rests above swing highs (buy stops) and below swing lows (sell stops). "
     "Smart money sweeps these levels to fill large orders before reversing. "
     "A sweep followed by a strong close back inside the range is a high-probability reversal. "
     "The deeper the sweep and faster the reclaim, the stronger the signal.",
     "smc"),

    ("Fair Value Gap Trading",
     "FVGs are 3-candle imbalances where the high of candle 1 doesn't overlap the low of candle 3. "
     "Price tends to return to fill these gaps. Bullish FVGs act as support, bearish as resistance. "
     "FVGs in premium (above equilibrium) favor shorts, discount FVGs favor longs.",
     "smc"),

    ("Order Block Entry",
     "An order block is the last opposing candle before a strong displacement move. "
     "Bullish OB: last bearish candle before a bullish move. Bearish OB: last bullish candle "
     "before a bearish move. Enter on return to the OB zone with a stop below/above the OB.",
     "smc"),

    # ── Market Profile / Auction Theory ─────────────────────────────
    ("Initial Balance Breakout",
     "The IB is the high-low range of the first 30-60 minutes. NQ breaks the IB 96.2% of days. "
     "Narrow IB (< 0.5x ATR) has 98.7% break probability with larger extensions. "
     "Wide IB (> 1.5x ATR) has smaller extensions. Trade the first breakout direction.",
     "market_profile"),

    ("Value Area Rotation",
     "When price opens inside the previous day's value area (70% of volume), expect rotation. "
     "When price opens outside value area, expect trending moves back toward value or "
     "acceptance at the new level. This determines whether to trade mean-reversion or momentum.",
     "market_profile"),

    # ── Order Flow Principles ───────────────────────────────────────
    ("Delta Divergence",
     "When price makes new highs but delta (buy volume - sell volume) doesn't confirm, "
     "the move is likely exhausting. Aggressive sellers are absorbing buying. "
     "This is a high-probability reversal signal, especially at key levels.",
     "order_flow"),

    ("Absorption Setup",
     "Large resting orders on the DOM that absorb aggressive flow without price moving. "
     "If large bid-side orders eat selling without price dropping, institutions are accumulating. "
     "Entry on absorption completion with stop below the absorbed level.",
     "order_flow"),

    ("Iceberg Order Detection",
     "Iceberg orders show small visible size but large total fill. If you see 2 contracts "
     "showing but 50+ filled at the same price, an institution is hiding size. "
     "This is extremely bullish/bearish depending on side.",
     "order_flow"),

    # ── Macro / Event Playbooks ─────────────────────────────────────
    ("FOMC Day Playbook",
     "On FOMC days: 1) Expect choppy, low-conviction moves before 2 PM EST announcement. "
     "2) Initial reaction in first 15 minutes is often wrong — fade the knee-jerk. "
     "3) Real move starts 30-60 minutes after release as institutions digest. "
     "4) Reduce size 50% before announcement, no new entries within 5 minutes of release.",
     "macro"),

    ("CPI Release Rules",
     "CPI releases at 8:30 AM EST. Market reacts violently in first 30 seconds. "
     "Wait for the 5-minute candle to close before taking a position. "
     "Hot CPI (above consensus) = bearish NQ, cool CPI = bullish NQ. "
     "The 2nd 5-minute candle often reverses — watch for the reversal before committing.",
     "macro"),

    ("NFP Day Rules",
     "Non-Farm Payrolls release first Friday of each month at 8:30 AM EST. "
     "Similar to CPI: wait for 5-min candle close. Strong jobs = potentially bearish "
     "(higher rates) but can be bullish if economy is in 'soft landing' narrative. "
     "Context matters more than the number itself.",
     "macro"),

    # ── Regime-Specific Rules ───────────────────────────────────────
    ("Trending Day Rules",
     "On trending days: 1) Don't fade the trend — only trade with it. "
     "2) Buy pullbacks to VWAP or rising EMA9. 3) Trail stops, don't take quick profits. "
     "4) If 3/4 timeframes agree, the trend is real. "
     "5) A trending day typically moves 1.5-2x ATR from open.",
     "regime"),

    ("Range Day Rules",
     "On range days: 1) Fade moves to extremes (IB high/low). "
     "2) VWAP is the magnet — price returns to it. "
     "3) Take quick profits (1:1 RR is fine). 4) Avoid breakout strategies. "
     "5) Range days show balanced TF votes (2-2 split).",
     "regime"),

    ("Volatile Day Rules",
     "On volatile days (VIX spike, news event): 1) Cut position size 50%. "
     "2) Widen stops 1.5-2x normal. 3) Only take A+ setups with multiple confluences. "
     "4) Expect 2-3x normal ATR moves. 5) Time stops are critical — exit after 8-10 minutes "
     "if trade isn't working.",
     "regime"),

    # ── Risk Management Wisdom ──────────────────────────────────────
    ("Position Sizing Rules",
     "Never risk more than 1-2% of account per trade. After 2 consecutive losses, "
     "reduce size by 50%. After 3 losses, stop trading for 30 minutes (cooloff). "
     "In recovery mode (down 2% daily), only take A++ setups at half size.",
     "risk"),

    ("Revenge Trading Prevention",
     "After a loss, the urge to 'make it back' leads to oversized, low-quality trades. "
     "Rules: 1) After a loss, wait at least 1 bar (5 min) before new entry. "
     "2) Next trade must have HIGHER confidence than the losing trade. "
     "3) If you hit 3 losses in a row, walk away for 30 minutes.",
     "risk"),
]


class KnowledgeRAG:
    """Trading knowledge vector database for AI agent queries."""

    def __init__(self, db_path: str = None):
        self._db_path = db_path or os.path.join(
            os.path.dirname(__file__), "..", "data", "knowledge_vectors"
        )
        self._collection = None
        self._initialized = False
        self._init_db()

    def _init_db(self):
        """Initialize ChromaDB with pre-loaded trading knowledge."""
        try:
            import chromadb
            os.makedirs(self._db_path, exist_ok=True)
            client = chromadb.PersistentClient(path=self._db_path)
            self._collection = client.get_or_create_collection(
                name="trading_knowledge",
                metadata={"hnsw:space": "cosine"},
            )
            self._initialized = True

            # Load knowledge if collection is empty
            if self._collection.count() == 0:
                self._seed_knowledge()

            logger.info(f"[KNOWLEDGE RAG] Initialized with {self._collection.count()} entries")
        except ImportError:
            logger.warning("[KNOWLEDGE RAG] chromadb not installed — running without knowledge RAG")
        except Exception as e:
            logger.warning(f"[KNOWLEDGE RAG] Init failed: {e}")

    def _seed_knowledge(self):
        """Load pre-defined trading knowledge into the vector DB."""
        if not self._collection:
            return

        for i, (title, content, category) in enumerate(TRADING_KNOWLEDGE):
            self._collection.add(
                ids=[f"knowledge_{i}"],
                documents=[f"{title}: {content}"],
                metadatas=[{"title": title, "category": category}],
            )
        logger.info(f"[KNOWLEDGE RAG] Seeded {len(TRADING_KNOWLEDGE)} knowledge entries")

    def query(self, question: str, n_results: int = 3) -> list[dict]:
        """Query the knowledge base with a natural language question."""
        if not self._initialized or not self._collection:
            return []

        try:
            results = self._collection.query(
                query_texts=[question],
                n_results=min(n_results, self._collection.count()),
            )

            entries = []
            if results and results["documents"]:
                for doc, meta, dist in zip(
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0],
                ):
                    entries.append({
                        "content": doc,
                        "title": meta.get("title", ""),
                        "category": meta.get("category", ""),
                        "relevance": round(1 - dist, 3),  # Convert distance to similarity
                    })
            return entries
        except Exception as e:
            logger.warning(f"[KNOWLEDGE RAG] Query failed: {e}")
            return []

    def add_knowledge(self, title: str, content: str, category: str = "custom"):
        """Add a new piece of trading knowledge."""
        if not self._initialized or not self._collection:
            return

        idx = self._collection.count()
        self._collection.add(
            ids=[f"knowledge_{idx}"],
            documents=[f"{title}: {content}"],
            metadatas=[{"title": title, "category": category}],
        )
        logger.info(f"[KNOWLEDGE RAG] Added: {title}")

    def to_dict(self) -> dict:
        return {
            "initialized": self._initialized,
            "entry_count": self._collection.count() if self._collection else 0,
            "categories": list(set(
                m["category"] for m in (self._collection.get()["metadatas"] or [])
            )) if self._collection else [],
        }
