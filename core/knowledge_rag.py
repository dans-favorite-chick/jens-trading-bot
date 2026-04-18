"""
Phoenix Bot — Trading Knowledge RAG

A second ChromaDB collection (alongside trade_rag.py) that stores
trading knowledge — SMC concepts, market profile rules, order flow
principles, macro playbooks, regime-specific rules, and a comprehensive
library of intraday strategies for futures and equities.

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

    # ── Intermarket Relationships ──────────────────────────────────
    ("VIX and NQ Inverse Correlation",
     "NQ and VIX have ~-0.85 correlation. VIX above 25 = elevated fear, expect wider ranges and "
     "more mean-reversion. VIX below 15 = complacency, expect trending moves. VIX spikes above 30 "
     "are typically short-lived (2-5 days). The VIX term structure (VIX vs VIX3M) signals regime: "
     "contango (VIX < VIX3M) = normal, backwardation (VIX > VIX3M) = panic mode, reduce all exposure.",
     "intermarket"),

    ("Dollar Index (DXY) Impact on NQ",
     "Strong dollar hurts NQ because: 1) Mega-cap tech earns 40-60% revenue overseas — strong USD "
     "reduces translated earnings. 2) Dollar strength signals tightening financial conditions. "
     "3) EM/international money flows away from US tech into local markets when DXY weakens. "
     "Key level: DXY above 105 = significant NQ headwind. Below 100 = NQ tailwind.",
     "intermarket"),

    ("Yield Curve and Growth Stocks",
     "Rising 10Y yields hurt NQ more than ES because growth stocks derive more value from future "
     "earnings, which get discounted harder when rates rise. A 10bps move in the 10Y yield can "
     "move NQ 50-100 points. Inverted yield curve (2Y > 10Y) historically precedes recession by "
     "12-18 months. When the curve UN-inverts (steepens), the recession is imminent, not over.",
     "intermarket"),

    ("Credit Spreads as Risk Gauge",
     "High-yield credit spreads (HYG vs TLT, or CDX HY index) are the best real-time gauge of "
     "stress. When credit spreads widen 20%+ in a week, equity selloffs typically follow or "
     "accelerate. When credit is calm (spreads near lows), equities can absorb bad news. "
     "Credit leads equities by 1-3 days in stress events.",
     "intermarket"),

    ("Semiconductor Lead for NQ",
     "Semiconductors (SMH, SOXX) are the highest-beta NQ sector and often lead the index by "
     "15-30 minutes intraday. When semis break VWAP before NQ does, it's a leading signal. "
     "NVDA alone can move NQ 30-50 points due to its index weight. Track NVDA, AMD, AVGO, "
     "MRVL as the 'canary in the NQ coal mine'.",
     "intermarket"),

    # ── NQ-Specific Rules ──────────────────────────────────────────
    ("NQ Concentration Risk",
     "Top 7 names (AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA) are ~55% of QQQ/NQ. When these "
     "names agree (5+ green or red), NQ trends hard. When they diverge, NQ chops. A single "
     "name (NVDA, AAPL) can swing NQ 50+ points on earnings. Always know what the big names "
     "are doing before taking an NQ position.",
     "nq_specific"),

    ("NQ vs ES Character Differences",
     "NQ has ~1.5x the daily range of ES, faster moves, and sharper reversals. NQ leads in "
     "risk-on moves, ES leads in risk-off. NQ gaps are larger and less likely to fill same-day "
     "than ES gaps. NQ is more sensitive to: interest rates, tech earnings, AI narrative, "
     "and growth/value rotation. ES is more influenced by: macro data, Fed policy, breadth.",
     "nq_specific"),

    ("NQ Overnight Session Behavior",
     "NQ overnight (6 PM - 8:30 AM EST) is driven by: 1) Asia/Europe macro data, 2) Currency "
     "moves (DXY, USDJPY), 3) Overseas tech earnings. Overnight ranges are typically 30-50% "
     "of the RTH range. The first 15 minutes of RTH (8:30-8:45) often reverses the overnight "
     "move. Don't trust overnight direction as an indicator for RTH direction.",
     "nq_specific"),

    ("Power Hour NQ Behavior (3-4 PM EST)",
     "The last hour of trading sees massive volume as institutions execute MOC (market-on-close) "
     "orders. NQ often trends strongly in the last 30 minutes. The 3:30-3:50 PM window is "
     "the highest-volume period of the day. If the day was a trend day, power hour often "
     "extends the trend. If range-bound, power hour often picks a direction.",
     "nq_specific"),

    # ── Options Flow Rules ─────────────────────────────────────────
    ("Gamma Exposure (GEX) Explained",
     "Gamma Exposure measures how much dealer hedging influences price. Positive GEX (dealers "
     "long gamma): they sell rallies and buy dips = mean-reversion, low volatility. Negative "
     "GEX (dealers short gamma): they buy rallies and sell dips = momentum amplification, high "
     "volatility. The gamma flip level is where GEX changes sign. Above the flip: fade moves. "
     "Below the flip: follow momentum.",
     "options_flow"),

    ("0DTE Impact on Market Structure",
     "0DTE (zero days to expiration) options now account for 40%+ of SPX/SPY option volume. "
     "The gamma from 0DTE options creates strong intraday effects: rapid gamma decay amplifies "
     "moves in the last 2 hours, and dealer hedging can create pinning effects at high-OI "
     "strikes. MNQ/NQ feels the spillover from SPX 0DTE dynamics because dealers hedge their "
     "index exposure across correlated products.",
     "options_flow"),

    ("Max Pain and Options Expiration Pinning",
     "Max pain is the strike price where option holders (both calls and puts) lose the most "
     "money. Market makers profit when price pins at max pain. On expiration day, price tends "
     "to drift toward max pain, especially in the last 2-3 hours. This effect is strongest on "
     "monthly OPEX (3rd Friday) and weaker on weekly expirations. The pin often fails when "
     "there's a strong catalyst or extreme gamma imbalance.",
     "options_flow"),

    ("Put Wall and Call Wall as Support/Resistance",
     "The put wall (strike with highest put OI) acts as support — dealers who sold puts must "
     "buy futures to hedge as price approaches. The call wall (highest call OI) acts as "
     "resistance — dealers sell futures to hedge. These levels shift daily as new OI accumulates. "
     "The strongest levels have 3x+ normal OI concentration.",
     "options_flow"),

    # ── Market Microstructure Rules ────────────────────────────────
    ("Spoofing Detection",
     "Spoofing is placing large orders with intent to cancel before execution. Signs: large "
     "order appears at bid/ask, price moves toward it, then the order vanishes. If you see "
     "large resting orders that repeatedly cancel when price gets close, it's likely spoofing. "
     "Do NOT trade based on spoofed levels — they're designed to fool you. Wait for actual "
     "fills (time & sales) to confirm the level is real.",
     "microstructure"),

    ("Liquidity Vacuum Rules",
     "A liquidity vacuum occurs when the order book thins out suddenly (total depth drops 50%+). "
     "This often happens before major moves — either news is imminent, or a large player is about "
     "to execute. In a vacuum, prices move further on less volume. Widen stops or exit positions "
     "when you detect a vacuum. Never add to positions in a thin book.",
     "microstructure"),

    ("Time and Sales Tape Reading",
     "The tape (time and sales) shows actual executed trades. Key patterns: 1) Cluster of same-size "
     "prints at the bid = institutional selling. 2) Large prints at the ask = institutional buying. "
     "3) Prints above the ask = extreme urgency to buy. 4) Prints below the bid = extreme urgency "
     "to sell. 5) Round lot sizes (100, 500, 1000) are often institutional.",
     "microstructure"),

    # ── Advanced Regime Detection ──────────────────────────────────
    ("Low Volatility Trap",
     "Extended low-volatility periods (VIX < 14 for 2+ weeks) often end with violent regime "
     "changes. The longer the compression, the more violent the expansion. During low-vol: "
     "1) Take mean-reversion trades. 2) Buy cheap options for the inevitable expansion. "
     "3) Use TIGHTER stops (low vol = less room needed). 4) When vol finally expands, "
     "immediately switch to momentum/trend strategies.",
     "regime"),

    ("Chop Detection Rules",
     "Chop (low-conviction, range-bound, whipsaw) days have these signatures: 1) TF votes are "
     "split 2-2 or 1-1-2. 2) ATR is low but bars are overlapping (not tight consolidation). "
     "3) CVD is flat — no clear buying or selling conviction. 4) VWAP is flat. 5) Multiple "
     "false breakouts of the IB. On chop days: reduce size 50%, only take A+ setups, "
     "tighten time stops to 5-10 minutes, fade extremes.",
     "regime"),

    ("Overnight Range as Daily Anchor",
     "The overnight high/low from 6 PM to 8:30 AM EST creates important levels for RTH trading. "
     "A break above the overnight high in RTH is bullish — all overnight shorts are trapped. "
     "A break below the overnight low is bearish. If price stays within the overnight range "
     "for the first 30 minutes of RTH, it's a range day candidate. Track the overnight midpoint "
     "as an additional VWAP-like anchor.",
     "regime"),

    # ── Seasonal / Calendar Rules ──────────────────────────────────
    ("Monday Market Behavior",
     "Mondays tend to be continuation days — they continue Friday's direction 58% of the time. "
     "Gap-up Mondays following positive Fridays have good follow-through. Gap-down Mondays "
     "following negative Fridays also follow through. The exception: three-day weekends often "
     "produce gap reversals as traders cover weekend hedges.",
     "seasonal"),

    ("End of Quarter Window Dressing",
     "The last 5 trading days of each quarter, fund managers 'window dress' — buying winners "
     "and selling losers to make their quarterly reports look good. This creates momentum in "
     "leading stocks (already up for the quarter) and pressure on laggards. Q4 (December) "
     "has the strongest effect due to tax-loss selling followed by January buying.",
     "seasonal"),

    ("Summer Doldrums (July-August)",
     "July and August historically see lower volume and narrower ranges as institutional "
     "traders go on vacation. Strategy adjustments: reduce position size, expect more range "
     "days, widen time stops (moves take longer to develop). September is historically the "
     "worst month for stocks — be prepared for a regime change after Labor Day.",
     "seasonal"),

    ("FOMC Week Pattern",
     "The week of an FOMC meeting follows a pattern: Monday-Tuesday are typically range-bound "
     "as traders wait. Wednesday before the 2 PM announcement is extremely choppy. The initial "
     "reaction to the decision is often WRONG — fade the first 15-minute move 60% of the time. "
     "Thursday-Friday after FOMC tend to be trending days as the market digests the statement.",
     "seasonal"),

    # ── Advanced Risk Rules ────────────────────────────────────────
    ("Correlation Trap in Drawdowns",
     "During market stress, all correlations go to 1 — everything drops together. Your 'diversified' "
     "NQ long + AAPL long + GOOGL long is actually a 3x levered position in the same thing. "
     "In drawdowns: 1) Cut ALL correlated positions, not just the loser. 2) The first loss is the "
     "cheapest. 3) Don't average down during correlated selloffs. 4) Hold cash as the best hedge.",
     "risk"),

    ("Gap Risk Management",
     "Overnight gaps in NQ average 30-60 points but can be 200+ on catalysts. Rules for managing "
     "gap risk: 1) Never hold max position size overnight. 2) Reduce to 50% before earnings of any "
     "Mag7 name. 3) Before FOMC, CPI, or NFP: flat or 25% size max. 4) Use overnight stop-loss "
     "orders at 2x daily ATR. 5) Accept that gaps are uncontrollable — manage exposure, not price.",
     "risk"),

    ("Drawdown Recovery Math",
     "A 10% drawdown needs 11% to recover. A 20% drawdown needs 25%. A 50% drawdown needs 100%. "
     "This asymmetry means preventing drawdowns matters more than maximizing gains. Rules: "
     "1) Daily stop-loss at 2% of account. 2) Weekly stop-loss at 4%. 3) Monthly stop-loss at 8%. "
     "4) After hitting any stop-loss, reduce size by 50% for the next period. 5) Never increase "
     "size while in a drawdown.",
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
                ids=[f"rule_{i}"],
                documents=[f"{title}: {content}"],
                metadatas=[{"title": title, "category": category, "type": "rule"}],
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
                        "type": meta.get("type", "rule"),
                        "relevance": round(1 - dist, 3),
                    })
            return entries
        except Exception as e:
            logger.warning(f"[KNOWLEDGE RAG] Query failed: {e}")
            return []

    def query_strategies(self, question: str, n_results: int = 5) -> list[dict]:
        """Query specifically for strategy entries.

        Uses semantic search — regime/ATR/timeframe terms in the question
        will match against the structured strategy descriptions.
        """
        if not self._initialized or not self._collection:
            return []

        try:
            count = self._collection.count()
            if count == 0:
                return []

            results = self._collection.query(
                query_texts=[question],
                n_results=min(n_results * 2, count),  # Fetch extra, filter to strategies
                where={"type": "strategy"},
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
                        "regimes": meta.get("regimes", ""),
                        "atr_preference": meta.get("atr_preference", ""),
                        "time_windows": meta.get("time_windows", ""),
                        "asset_class": meta.get("asset_class", ""),
                        "relevance": round(1 - dist, 3),
                    })
            return entries[:n_results]
        except Exception as e:
            logger.warning(f"[KNOWLEDGE RAG] Strategy query failed: {e}")
            return []

    def add_knowledge(self, title: str, content: str, category: str = "custom",
                      **extra_meta):
        """Add a new piece of trading knowledge with optional metadata."""
        if not self._initialized or not self._collection:
            return

        idx = self._collection.count()
        meta = {"title": title, "category": category, "type": "rule"}
        meta.update(extra_meta)
        self._collection.add(
            ids=[f"knowledge_{idx}"],
            documents=[f"{title}: {content}"],
            metadatas=[meta],
        )
        logger.info(f"[KNOWLEDGE RAG] Added: {title}")

    def add_strategy(self, strategy_id: str, title: str, document: str,
                     metadata: dict):
        """Add a strategy entry with structured metadata."""
        if not self._initialized or not self._collection:
            return False

        # Check if already loaded
        try:
            existing = self._collection.get(ids=[strategy_id])
            if existing and existing["ids"]:
                return False  # Already exists
        except Exception:
            pass

        metadata["title"] = title
        metadata["type"] = "strategy"
        self._collection.add(
            ids=[strategy_id],
            documents=[document],
            metadatas=[metadata],
        )
        return True

    def get_strategy_count(self) -> int:
        """Count loaded strategies."""
        if not self._initialized or not self._collection:
            return 0
        try:
            results = self._collection.get(where={"type": "strategy"})
            return len(results["ids"]) if results else 0
        except Exception:
            return 0

    def to_dict(self) -> dict:
        return {
            "initialized": self._initialized,
            "entry_count": self._collection.count() if self._collection else 0,
            "categories": list(set(
                m["category"] for m in (self._collection.get()["metadatas"] or [])
            )) if self._collection else [],
        }
