# PROMPT: STRATEGY KNOWLEDGE INJECTION SYSTEM — CLAUDE CODE IMPLEMENTATION

## Overview
You are implementing a **Strategy Knowledge Injection System** for the Phoenix Trading Bot — a production MNQ/NQ futures trading system running on NinjaTrader 8 via WebSocket. The goal is to scale from 4 manually-built strategies to **500+ validated patterns and strategies** by integrating open-source pattern recognition libraries, SMC/ICT detection, enriched data feeds, and an AI-powered strategy generation pipeline — all feeding into the existing ChromaDB RAG and XGBoost classification system.

**This is a real trading system with real money at risk. Every new module must be non-blocking, fault-tolerant, and never interfere with the live trading pipeline.**

---

## CRITICAL CONTEXT — READ FIRST

### Phoenix Bot Architecture
```
NinjaTrader 8 (TickStreamer.cs — TCP CLIENT)
    ↓  raw TCP ticks (bid/ask/price/vol per tick)
bridge_server.py  ←  TCP server :8765
                  →  WS server  :8766  (fans out to bots)
    ↓
    ├── prod_bot.py   (live/sim trading, session hours)
    └── lab_bot.py    (experimental, paper trading 24/7)
    ↓
OIF files → NT8 execution
```

### Key Active Directories
- **Project root:** `C:\Trading Project\phoenix_bot\`
- **Bots:** `phoenix_bot/bots/base_bot.py` — core bot logic
- **Core math:** `phoenix_bot/core/tick_aggregator.py` — bars, ATR, VWAP, EMA, CVD, DOM
- **HMM Regime:** `phoenix_bot/core/hmm_regime.py` — 3-state HMM (TRENDING/RANGING/VOLATILE)
- **Trade RAG:** `phoenix_bot/core/trade_rag.py` — ChromaDB similarity search
- **Agents:** `phoenix_bot/agents/` — AI agents (batch_analyzer.py, council_gate.py, etc.)
- **Config:** `phoenix_bot/config/strategies.py` — strategy parameters
- **Config:** `phoenix_bot/config/settings.py` — ports, paths, risk limits
- **Logs:** `phoenix_bot/logs/history/YYYY-MM-DD_bot.jsonl` — every bar + eval + entry + exit
- **Trade Memory:** `phoenix_bot/logs/trade_memory.json` — all trades with P&L and context

### Already Installed
- Python 3.11+, numpy, websockets, aiofiles, flask, python-dotenv
- chromadb, anthropic, pydantic (Phase A/B)
- hmmlearn needs MSVC Build Tools (fallback HMM exists without it)

### Strategy Pattern (from strategies_v3.py)
```python
class BaseStrategy:
    name: str = "BASE"
    description: str = ""
    stop_ticks: int = 9
    target_rr: float = 2.0
    min_confluence: float = 3.0
    min_tf_votes: int = 3
    enabled: bool = True
    max_hold_minutes: int = 20
    trend_mode: bool = False

    def should_enter(self, market_data: dict) -> bool:
        raise NotImplementedError

    def entry_direction(self, market_data: dict) -> str:
        bias = market_data.get('bias', 'NEUTRAL').upper()
        return 'LONG' if bias in ('LONG', 'BULLISH', 'UP') else 'SHORT'

    def entry_reason(self, market_data: dict) -> str:
        return f"{self.name}: no reason provided"
```

### Market Data Dictionary Keys (available from WebSocket)
```python
market_data = {
    'price': float,              # Current price
    'bid': float,                # Current bid
    'ask': float,                # Current ask
    'volume': float,             # Current bar volume
    'atr': float,                # 14-period ATR
    'vwap': float,               # Session VWAP
    'ema_9': float,              # 9-period EMA
    'ema_21': float,             # 21-period EMA
    'rsi': float,                # 14-period RSI
    'cvd': float,                # Cumulative volume delta
    'bar_delta': float,          # Current bar buy-sell volume delta
    'dom_bid_stack': float,      # DOM bid depth
    'dom_ask_stack': float,      # DOM ask depth
    'dom_imbalance': float,      # DOM imbalance ratio
    'bias': str,                 # LONG/SHORT/NEUTRAL
    'momentum_confidence': int,  # 0-100
    'confluence_score': float,   # Council confluence score
    'tf_vote_count': int,        # Timeframe alignment count (0-5)
    'open': float,               # Bar open
    'high': float,               # Bar high
    'low': float,                # Bar low
    'close': float,              # Bar close
    'bar_count': int,            # Number of completed bars
}
```

### What NOT To Do
- ❌ Never modify `bridge_server.py`, `prod_bot.py`, or `base_bot.py` core logic
- ❌ Never make new modules blocking — all must have timeouts and safe defaults
- ❌ Never reference legacy `Jen_Trading_Botv1/` or `trading_bot_project/` directories
- ❌ Never install packages that require compilation without checking first (e.g., TA-Lib needs C binaries)
- ❌ Never let AI-generated strategy recommendations auto-modify live parameters
- ❌ Never use random cross-validation on time-series data — always `TimeSeriesSplit`

---

## PHASE 1: PATTERN RECOGNITION LIBRARY INTEGRATION (Hours 1-2)

### 1A. Install and Wire `pandas-ta` for 200+ Indicators

**Goal:** Add 62 Japanese candlestick patterns + 150+ technical indicators to every 5-minute bar snapshot.

```bash
pip install pandas-ta-classic --break-system-packages
```

**Create file: `phoenix_bot/core/pattern_detector.py`**

This module must:
1. Maintain a rolling DataFrame of the last 100 OHLCV bars (from `tick_aggregator.py` bar closes)
2. On each new bar close, run `df.ta.cdl_pattern(name="all")` to detect all 62 candlestick patterns
3. Return a dict of detected patterns with their signal values (+100 bullish, -100 bearish, 0 none)
4. Also compute: RSI divergence, MACD histogram direction, Bollinger Band position, Stochastic crossover
5. Expose a `get_active_patterns(bar_data) -> dict` method that returns only non-zero pattern detections
6. Expose a `get_pattern_features(bar_data) -> list[float]` method for XGBoost feature vector
7. Must be non-blocking and catch all exceptions (return empty dict on failure)
8. Log pattern detections at DEBUG level, never at INFO (too noisy)

**Critical: pandas-ta uses pandas DataFrames internally. The tick_aggregator works with deques of dicts. You must bridge between them.**

**Pattern categories to detect:**
- All 62 TA-Lib candlestick patterns via pandas-ta (doji, engulfing, hammer, harami, etc.)
- Custom multi-bar patterns: inside bar, outside bar, pin bar (wick > 2x body)
- Volume-price divergences: price up + volume down = bearish divergence, etc.

### 1B. Install and Wire `smartmoneyconcepts` for ICT/SMC Detection

**Goal:** Detect Fair Value Gaps, Break of Structure, Change of Character, Order Blocks, and Liquidity Sweeps in real-time.

```bash
pip install smartmoneyconcepts --break-system-packages
```

**Create file: `phoenix_bot/core/smc_detector.py`**

This module must:
1. Accept OHLCV data as a pandas DataFrame with lowercase columns: `open, high, low, close, volume`
2. Call `smc.fvg(ohlc, join_consecutive=True)` for Fair Value Gap detection
3. Call `smc.ob(ohlc)` for Order Block detection with OBVolume strength scores
4. Call `smc.bos_choch(ohlc)` for Break of Structure / Change of Character
5. Call `smc.liquidity(ohlc)` for liquidity sweep detection
6. Call `smc.swing_highs_lows(ohlc)` for swing structure
7. Return a structured `SMCState` dataclass with:
   - `active_fvgs: list[dict]` — unmitigated FVGs with top/bottom/direction
   - `active_order_blocks: list[dict]` — unmitigated OBs with strength score
   - `latest_bos: dict` — most recent BOS with level and direction
   - `latest_choch: dict` — most recent CHoCH with level and direction
   - `liquidity_levels: list[dict]` — recent liquidity sweep levels
   - `market_structure: str` — "BULLISH" / "BEARISH" / "NEUTRAL" based on BOS chain
8. Expose `get_smc_features() -> list[float]` for XGBoost feature vector:
   - Distance to nearest FVG (normalized by ATR)
   - Distance to nearest Order Block (normalized by ATR)
   - OB strength score (0-1)
   - BOS direction as int (-1, 0, 1)
   - CHoCH recency (bars since last CHoCH, capped at 50)
   - Number of unmitigated FVGs above/below price
   - Liquidity sweep recency (bars since last sweep)
9. Must handle edge cases: insufficient data (< 20 bars), NaN values, empty results
10. Must be re-entrant and thread-safe (called from async bot loop)

### 1C. Install PatternPy for Chart-Level Pattern Detection

```bash
pip install patternpy --break-system-packages
```

**Create file: `phoenix_bot/core/chart_patterns.py`**

This module must:
1. Detect: Head & Shoulders (top/bottom), Double Top/Bottom, Triangles (ascending/descending/symmetric), Wedges, Support/Resistance levels
2. Run on the last 100 bars of data
3. Return detected patterns with:
   - Pattern name
   - Direction implication (bullish/bearish)
   - Confidence score (0-1 based on pattern quality)
   - Target price (based on pattern measurement rules)
   - Invalidation level (where pattern breaks)
4. Expose `get_chart_pattern_features() -> list[float]` for XGBoost
5. Cache results — only recompute when new bars arrive

### 1D. Wire Everything Into the Signal Pipeline

**Modify: `phoenix_bot/core/tick_aggregator.py`** (carefully, minimal changes)

On each 5-minute bar close event:
1. Call `pattern_detector.update(new_bar)` with the new OHLCV bar
2. Call `smc_detector.update(ohlcv_dataframe)` with the rolling DataFrame
3. Call `chart_patterns.update(ohlcv_dataframe)` with the rolling DataFrame
4. Append all pattern/SMC/chart features to the market_data dict under new keys:
   - `market_data['candlestick_patterns']` — dict of active patterns
   - `market_data['smc_state']` — SMCState dataclass
   - `market_data['chart_patterns']` — list of detected chart patterns
   - `market_data['pattern_features']` — combined feature vector for XGBoost

**Create file: `phoenix_bot/core/feature_assembler.py`**

This module combines all feature sources into a single normalized vector for XGBoost and ChromaDB:
1. Takes market_data dict as input
2. Extracts and normalizes 40-60 features from:
   - Existing: ATR, RSI, CVD slope, DOM imbalance, volume delta, VWAP distance, EMA positions, confluence score
   - New: Top 10 candlestick pattern signals, SMC features (7 values), chart pattern features (5 values)
   - HMM regime state (one-hot encoded: 3 values)
3. Returns `np.array` of shape `(n_features,)` — this feeds both XGBoost predict and ChromaDB embedding
4. Maintains a `feature_names: list[str]` for SHAP explainability later

---

## PHASE 2: STRATEGY KNOWLEDGE INGESTION (Hours 3-6)

### 2A. Clone and Parse Academic Strategy Library

**Goal:** Ingest 140+ pre-coded strategies from Papers With Backtest into ChromaDB as searchable knowledge.

```bash
cd C:\Trading Project\phoenix_bot\data
git clone https://github.com/paperswithbacktest/pwb-toolbox.git
```

**Create file: `phoenix_bot/tools/strategy_ingestor.py`**

This is a CLI tool (not a live trading module) that:
1. Walks `pwb-toolbox/` and finds all strategy Python files
2. For each strategy file:
   a. Extracts: strategy name, description, entry logic, exit logic, asset class, timeframe
   b. Extracts backtest results if present: Sharpe ratio, max drawdown, win rate, profit factor
   c. Creates a structured document:
      ```python
      {
          "strategy_name": "Momentum Factor in Commodity Futures",
          "source": "papers_with_backtest",
          "description": "Long top 20% momentum, short bottom 20%...",
          "entry_logic_summary": "...",
          "exit_logic_summary": "...",
          "asset_class": "futures",
          "timeframe": "monthly",
          "sharpe_ratio": 0.85,
          "max_drawdown": -0.23,
          "win_rate": 0.54,
          "applicable_to_mnq": true/false,  # heuristic based on asset class
          "raw_code": "..."  # first 2000 chars of the Python file
      }
      ```
   d. Embeds into ChromaDB collection `strategy_knowledge` using text description + metadata
3. Also parse strategies from these repos (clone them too):
   - `https://github.com/je-suis-tm/quant-trading` (17 strategies, 9400 stars)
   - `https://github.com/freqtrade/freqtrade-strategies` (100+ community strategies)
4. Print summary: "Ingested X strategies into ChromaDB. Y applicable to futures."
5. Tag each with searchable metadata for RAG retrieval

### 2B. Create Strategy RAG Query Interface

**Create file: `phoenix_bot/core/strategy_rag.py`**

This module extends the existing `trade_rag.py` (which stores trade embeddings) with a strategy knowledge layer:
1. Separate ChromaDB collection: `strategy_knowledge` (distinct from `trade_history`)
2. `query_similar_strategies(market_state: dict, regime: str, k: int = 10) -> list[dict]`
   - Embeds current market conditions as a text query
   - Returns top-K most relevant strategies with similarity scores
   - Filters by regime compatibility
3. `query_by_pattern(pattern_name: str, k: int = 5) -> list[dict]`
   - Finds strategies that use a specific pattern (e.g., "engulfing", "FVG", "breakout")
4. `get_strategy_stats(strategy_name: str) -> dict`
   - Returns backtest metrics for a named strategy
5. Used by the council gate and pre-trade filter to augment decision-making

---

## PHASE 3: FREE DATA ENRICHMENT FEEDS (Hours 7-10)

### 3A. COT Data Integration

```bash
pip install pycot-reports --break-system-packages
```

**Create file: `phoenix_bot/data_feeds/cot_feed.py`**

This module must:
1. Pull weekly COT data for NASDAQ MINI (CME) using `pycot-reports`
2. Extract leveraged fund net positioning (long - short contracts)
3. Compute: percentile rank of current positioning vs. last 52 weeks
4. Expose: `get_cot_signal() -> dict` returning:
   - `leveraged_fund_net`: int (net contracts)
   - `percentile_rank`: float (0-1, where 0 = max short, 1 = max long)
   - `extreme_signal`: str ("BULLISH_EXTREME" / "BEARISH_EXTREME" / "NEUTRAL")
   - `last_updated`: datetime
5. Cache locally — only fetch once per day (data updates weekly)
6. Graceful failure: return neutral signal if API unavailable

### 3B. FRED Macro Data Integration

```bash
pip install fredapi --break-system-packages
```

**Create file: `phoenix_bot/data_feeds/macro_feed.py`**

Requires: `FRED_API_KEY` in `.env` file (free from https://fred.stlouisfed.org/docs/api/api_key.html)

This module must:
1. Pull and cache these series (update daily, cache locally):
   - `VIXCLS` — VIX close (you already have this from NT8, but FRED is backup)
   - `T10Y2Y` — 10Y minus 2Y yield spread (recession indicator)
   - `DFF` — Fed Funds effective rate
   - `DTWEXBGS` — Trade-weighted US dollar index
2. Compute derived signals:
   - Yield curve inversion flag (T10Y2Y < 0)
   - VIX regime (< 15 = complacent, 15-25 = normal, 25-35 = elevated, > 35 = fear)
   - Dollar trend (20-day change direction)
3. Expose: `get_macro_context() -> dict` for dashboard and AI agents
4. Cache results in `data/macro_cache.json` — never fetch more than once per trading session

### 3C. Finnhub Economic Calendar + News Sentiment

```bash
pip install finnhub-python --break-system-packages
```

**Create file: `phoenix_bot/data_feeds/event_feed.py`**

Requires: `FINNHUB_API_KEY` in `.env` (free tier: 60 calls/minute)

This module must:
1. At session start, fetch today's economic calendar events
2. Flag high-impact events within the next 60 minutes (FOMC, CPI, NFP, GDP, etc.)
3. Expose: `get_upcoming_events() -> list[dict]` with:
   - `event_name`, `time`, `impact` (high/medium/low), `expected`, `previous`
4. Expose: `is_event_window() -> bool` — True if a high-impact event is within 15 minutes
5. When `is_event_window() == True`, the bot should reduce position size or pause (advisory signal)
6. Also fetch market news sentiment for "NASDAQ" keyword:
   - Run lightweight sentiment scoring (positive/negative/neutral word counts)
   - Expose: `get_news_sentiment() -> float` (-1 to +1)
7. Rate limit all calls — never exceed 30 calls/minute (leave headroom)

### 3D. FlashAlpha Gamma Exposure (Optional — Free Tier)

```bash
pip install flashalpha --break-system-packages
```

**Create file: `phoenix_bot/data_feeds/gamma_feed.py`**

This module must:
1. Pull GEX data for QQQ (best NQ proxy available on free tier)
2. Extract: gamma flip level, call wall, put wall, net GEX value
3. Expose: `get_gamma_levels() -> dict` with:
   - `gamma_flip`: float (price level where market maker hedging flips)
   - `call_wall`: float (heavy call OI = resistance)
   - `put_wall`: float (heavy put OI = support)
   - `regime`: str ("POSITIVE_GAMMA" / "NEGATIVE_GAMMA")
4. Free tier = 5 requests/day — fetch once at session start, cache
5. Convert QQQ levels to approximate NQ levels (QQQ × ~43 ratio, verify current multiplier)

---

## PHASE 4: CLAUDE STRATEGY GENERATION PIPELINE (Hours 11-16)

### 4A. Trading Book Knowledge Base Builder

**Create file: `phoenix_bot/tools/book_ingestor.py`**

CLI tool that:
1. Accepts a directory of PDF trading books
2. Extracts text using PyMuPDF (`pip install pymupdf --break-system-packages`)
3. Chunks text at 800 tokens with 150-token overlap
4. Embeds and stores in ChromaDB collection `trading_books`
5. Tags each chunk with: book title, chapter, page range
6. Handles: "Trading in the Zone", "Market Wizards", ICT/SMC methodology PDFs, etc.

Usage:
```bash
python -m phoenix_bot.tools.book_ingestor --dir "C:\Trading Books\" --collection trading_books
```

### 4B. Claude Strategy Factory

**Create file: `phoenix_bot/tools/strategy_factory.py`**

This is the core strategy generation pipeline. It uses Claude Batch API to generate and validate strategies at scale.

The pipeline has 5 stages:

**Stage 1: Hypothesis Generation**
- Query ChromaDB `trading_books` for relevant passages about a given market condition
- Prompt Claude to generate a strategy hypothesis:
  ```
  You are a quantitative trading strategist specializing in NQ/MNQ futures.
  Given the following trading knowledge context:
  {rag_context}
  
  Generate a specific, testable trading strategy hypothesis for MNQ 5-minute bars.
  You must specify:
  1. Strategy name (descriptive, unique)
  2. Market regime where it works (TRENDING / RANGING / VOLATILE / ALL)
  3. Entry conditions (specific, measurable — reference indicators by name)
  4. Entry direction logic (when to go LONG vs SHORT)
  5. Stop loss calculation (in ticks or ATR multiples)
  6. Take profit calculation (risk:reward ratio or target method)
  7. Time-based exit (max hold time in minutes)
  8. Required confluence score (what other signals must agree)
  9. Why this should work (market microstructure reasoning)
  10. What would invalidate this strategy (failure modes)
  
  Format as JSON.
  ```

**Stage 2: Code Generation**
- Take the hypothesis JSON and prompt Claude to write executable Python:
  ```
  Convert this strategy hypothesis into a Python class that inherits from BaseStrategy.
  
  The class must implement:
  - should_enter(self, market_data: dict) -> bool
  - entry_direction(self, market_data: dict) -> str
  - entry_reason(self, market_data: dict) -> str
  
  Available market_data keys: {list all keys from the market_data dict above}
  
  CRITICAL RULES:
  - Use only data available in market_data — no future data, no look-ahead
  - All comparisons must use current bar data only
  - Include safety checks: if any required key is missing, return False
  - Add type hints and docstrings
  - The class must be self-contained (no imports beyond standard library + numpy)
  ```

**Stage 3: Static Validation**
- Parse the generated code with Python's `ast` module
- Check for:
  - No imports of disallowed modules
  - No file I/O operations
  - No network calls
  - No infinite loops
  - Correct method signatures
  - All referenced market_data keys exist
- If validation fails, retry generation with error feedback (max 2 retries)

**Stage 4: Backtest Simulation**
- Run the strategy against historical JSONL logs (`logs/history/*.jsonl`)
- Compute: win rate, profit factor, max drawdown, Sharpe ratio, trade count
- Reject if: win rate < 40%, profit factor < 1.0, trade count < 10, Sharpe < 0.3

**Stage 5: Storage**
- Validated strategies get stored in:
  a. `phoenix_bot/generated_strategies/` as individual `.py` files
  b. ChromaDB `strategy_knowledge` collection with performance metadata
  c. A master registry: `generated_strategies/registry.json`

**CLI interface:**
```bash
# Generate 50 strategy hypotheses from book knowledge
python -m phoenix_bot.tools.strategy_factory --generate 50 --source trading_books

# Generate strategies focused on a specific pattern
python -m phoenix_bot.tools.strategy_factory --generate 20 --focus "fair_value_gap"

# Validate all unvalidated strategies against historical data
python -m phoenix_bot.tools.strategy_factory --validate-all

# Show strategy leaderboard
python -m phoenix_bot.tools.strategy_factory --leaderboard
```

### 4C. Multi-Reviewer Validation

**Create file: `phoenix_bot/tools/strategy_reviewer.py`**

Before a generated strategy is promoted to production testing, run it through 5 Claude reviewer personas:

1. **Quant Analyst:** "Does this strategy have statistical edge? Are the entry conditions specific enough? Is there a risk of curve-fitting?"
2. **Risk Manager:** "What's the worst-case scenario? Is the stop loss adequate? Can this blow up the account?"
3. **Execution Engineer:** "Can this be executed in real-time on 5-minute bars? Are there latency concerns? Does it depend on data that might be stale?"
4. **Data Scientist:** "Is there look-ahead bias? Are the features properly lagged? Would this survive walk-forward testing?"
5. **Market Microstructure Expert:** "Does this align with how NQ actually trades? Are there time-of-day effects not accounted for? Does this make sense given MNQ tick size ($0.50/tick)?"

Each reviewer returns: PASS / FAIL / CAUTION with reasoning.
Strategy must get 4/5 PASS or 3 PASS + 2 CAUTION to proceed.

Use Claude Batch API to run all 5 reviews in parallel (cost: ~$0.01 per strategy review).

---

## PHASE 5: XGBOOST FEATURE EXPANSION (Hours 17-20)

### 5A. Expanded Feature Set for XGBoost Classifier

**Modify: `phoenix_bot/core/trade_rag.py`** (extend, don't replace)

The existing trade embedding uses ~20 features. Expand to 50-60 features:

**Existing features (keep):**
- trend_strength, atr, rsi, cvd_slope, dom_imbalance, volume_delta
- distance_to_vwap, confluence_score, minutes_since_open

**New features to add from Phase 1:**
- Top 5 candlestick pattern signals (normalized -1 to +1)
- SMC features: fvg_distance, ob_distance, ob_strength, bos_direction, choch_recency (5 values)
- Chart pattern features: pattern_active, pattern_direction, pattern_confidence (3 values)

**New features to add from Phase 3:**
- COT percentile rank (1 value)
- Macro regime flags: yield_curve_inverted, vix_regime_code, dollar_trend (3 values)
- Event proximity: minutes_to_next_high_impact_event (1 value, capped at 120)
- News sentiment score (1 value)
- Gamma regime: positive_gamma flag, distance_to_gamma_flip (2 values)

**New features from HMM:**
- Regime one-hot: is_trending, is_ranging, is_volatile (3 values)
- Regime transition probability (1 value — probability of regime change)

**Implementation rules:**
- All features must be float, normalized to [-1, 1] or [0, 1] range
- Missing data → use 0.0 (neutral)
- Feature names must be tracked in a list for SHAP analysis later
- N/10 rule: don't use more features than trades/10 (if 200 trades, max 20 active features — use feature selection)

### 5B. XGBoost Retraining Pipeline

**Modify: `phoenix_bot/agents/batch_analyzer.py`** (extend)

Add a `retrain_classifier()` method that:
1. Loads all trades from `trade_memory.json`
2. Assembles feature vectors using `feature_assembler.py`
3. Labels trades as WIN (1) or LOSS (0) using the triple barrier method:
   - Upper barrier: take profit hit
   - Lower barrier: stop loss hit
   - Vertical barrier: time expiry (max hold time reached)
4. Trains XGBClassifier with TimeSeriesSplit (5 folds)
5. Computes out-of-sample accuracy and logs it
6. Only updates the model if new accuracy > old accuracy - 0.02 (don't regress)
7. Saves model to `models/xgb_classifier.pkl`
8. Runs SHAP analysis and saves top 20 feature importances to `models/feature_importance.json`

---

## PHASE 6: INTEGRATION WIRING (Hours 21-24)

### 6A. Unified Knowledge Manager

**Create file: `phoenix_bot/core/knowledge_manager.py`**

This is the central orchestrator that ties everything together:

```python
class KnowledgeManager:
    """
    Central hub for all pattern detection, data enrichment, and strategy knowledge.
    Called by base_bot.py on each bar close to enrich market_data.
    """
    
    def __init__(self):
        self.pattern_detector = PatternDetector()
        self.smc_detector = SMCDetector()
        self.chart_patterns = ChartPatternDetector()
        self.feature_assembler = FeatureAssembler()
        self.strategy_rag = StrategyRAG()
        self.cot_feed = COTFeed()        # updates daily
        self.macro_feed = MacroFeed()     # updates daily
        self.event_feed = EventFeed()     # updates at session start
        self.gamma_feed = GammaFeed()     # updates at session start
    
    async def initialize(self):
        """Called once at bot startup. Loads caches, fetches daily data."""
        await self.cot_feed.refresh()
        await self.macro_feed.refresh()
        await self.event_feed.refresh()
        await self.gamma_feed.refresh()
    
    def enrich_market_data(self, market_data: dict) -> dict:
        """
        Called on every 5-minute bar close.
        Adds pattern detections, SMC state, and enrichment data to market_data.
        Returns the enriched dict — never modifies the original.
        MUST complete in < 100ms. MUST never throw exceptions.
        """
        enriched = market_data.copy()
        try:
            enriched['candlestick_patterns'] = self.pattern_detector.get_active_patterns(market_data)
            enriched['smc_state'] = self.smc_detector.get_state()
            enriched['chart_patterns'] = self.chart_patterns.get_patterns()
            enriched['feature_vector'] = self.feature_assembler.assemble(enriched)
            enriched['cot_signal'] = self.cot_feed.get_cot_signal()
            enriched['macro_context'] = self.macro_feed.get_macro_context()
            enriched['event_window'] = self.event_feed.is_event_window()
            enriched['upcoming_events'] = self.event_feed.get_upcoming_events()
            enriched['news_sentiment'] = self.event_feed.get_news_sentiment()
            enriched['gamma_levels'] = self.gamma_feed.get_gamma_levels()
            
            # RAG: find similar historical trades for this market state
            similar_trades = self.strategy_rag.query_similar(enriched['feature_vector'])
            enriched['similar_trade_win_rate'] = self._calc_similar_win_rate(similar_trades)
            enriched['similar_trade_count'] = len(similar_trades)
            
        except Exception as e:
            logger.warning(f"[KnowledgeManager] Enrichment error (non-fatal): {e}")
        
        return enriched
```

### 6B. Dashboard Integration

**Modify: `phoenix_bot/dashboard/` (or create if needed)**

Add a new dashboard panel or tab showing:
1. **Active Patterns:** List of currently detected candlestick patterns and chart patterns
2. **SMC State:** Current FVGs, Order Blocks, BOS/CHoCH status
3. **Data Feeds Status:** COT signal, macro context, event warnings, gamma levels
4. **Strategy Knowledge:** Top 5 most similar historical strategies for current market state
5. **Feature Vector Heatmap:** Visual showing which features are active/extreme
6. **Model Confidence:** XGBoost prediction probability for current setup

---

## TESTING REQUIREMENTS

Before ANY module goes live:

1. **Unit tests for each detector:**
   - `test_pattern_detector.py` — feed known OHLCV sequences, verify pattern detection
   - `test_smc_detector.py` — feed known BOS/FVG sequences, verify detection
   - `test_feature_assembler.py` — verify output shape and normalization ranges

2. **Integration test:**
   - `test_knowledge_manager.py` — feed 100 historical bars through the full pipeline
   - Verify: enriched market_data has all expected keys
   - Verify: no exceptions thrown on edge cases (empty data, NaN values, missing keys)
   - Verify: execution time < 100ms per bar

3. **Backtest validation:**
   - Run strategy_factory generated strategies against at least 3 months of historical data
   - Verify no look-ahead bias (all features use only past data)
   - Verify walk-forward performance matches in-sample within reasonable bounds

---

## FILE STRUCTURE (what you're building)

```
phoenix_bot/
├── core/
│   ├── tick_aggregator.py      # EXISTING — add bar-close hooks
│   ├── hmm_regime.py           # EXISTING — no changes
│   ├── trade_rag.py            # EXISTING — extend with new features
│   ├── pattern_detector.py     # NEW — pandas-ta candlestick patterns
│   ├── smc_detector.py         # NEW — smartmoneyconcepts ICT/SMC
│   ├── chart_patterns.py       # NEW — PatternPy chart patterns
│   ├── feature_assembler.py    # NEW — unified feature vector builder
│   ├── strategy_rag.py         # NEW — strategy knowledge RAG queries
│   └── knowledge_manager.py    # NEW — central orchestrator
├── data_feeds/
│   ├── __init__.py             # NEW
│   ├── cot_feed.py             # NEW — CFTC COT data
│   ├── macro_feed.py           # NEW — FRED economic data
│   ├── event_feed.py           # NEW — Finnhub calendar + news
│   └── gamma_feed.py           # NEW — FlashAlpha GEX data
├── tools/
│   ├── strategy_ingestor.py    # NEW — bulk strategy → ChromaDB
│   ├── book_ingestor.py        # NEW — PDF books → ChromaDB
│   ├── strategy_factory.py     # NEW — Claude-powered strategy generation
│   └── strategy_reviewer.py    # NEW — multi-persona strategy validation
├── generated_strategies/
│   ├── __init__.py             # NEW
│   └── registry.json           # NEW — master list of generated strategies
├── models/
│   ├── xgb_classifier.pkl      # Updated by retrain pipeline
│   └── feature_importance.json # SHAP feature rankings
├── data/
│   ├── macro_cache.json        # Cached FRED data
│   ├── cot_cache.json          # Cached COT data
│   └── pwb-toolbox/            # Cloned strategy repo
└── tests/
    ├── test_pattern_detector.py
    ├── test_smc_detector.py
    ├── test_feature_assembler.py
    └── test_knowledge_manager.py
```

---

## DEPENDENCIES TO ADD

Add to `requirements.txt`:
```
# Phase 1: Pattern Recognition
pandas-ta-classic>=0.2.0
smartmoneyconcepts>=0.0.9
patternpy>=0.1.0

# Phase 3: Data Feeds
pycot-reports>=0.1.0
fredapi>=0.5.0
finnhub-python>=2.4.0
flashalpha>=0.1.0

# Phase 4: Strategy Generation (already installed)
# anthropic — already installed
# chromadb — already installed
# pydantic — already installed

# Phase 5: ML Pipeline
xgboost>=2.0.0
shap>=0.43.0
scikit-learn>=1.3.0
pymupdf>=1.23.0

# Core (may need adding)
pandas>=2.0.0
```

---

## IMPLEMENTATION ORDER (for Claude Code)

Execute phases in this order. Each phase should be complete and tested before moving to the next.

1. **Phase 1A** — PatternDetector with pandas-ta (highest ROI per hour)
2. **Phase 1B** — SMCDetector with smartmoneyconcepts (fills your biggest gap vs Lux AI)
3. **Phase 1D** — FeatureAssembler (connects Phase 1 to your XGBoost pipeline)
4. **Phase 3A** — COT feed (highest-value free data source)
5. **Phase 3B** — FRED macro feed (recession/VIX context)
6. **Phase 3C** — Finnhub events (avoid getting wrecked by CPI/FOMC)
7. **Phase 6A** — KnowledgeManager (wires everything together)
8. **Phase 2A** — Strategy ingestor (bulk ChromaDB loading)
9. **Phase 4B** — Strategy factory (Claude-powered generation)
10. **Phase 1C** — Chart patterns (lower priority, PatternPy is newer)
11. **Phase 3D** — Gamma feed (optional, free tier is limited)
12. **Phase 4C** — Strategy reviewer (polish step)
13. **Phase 5** — XGBoost retraining (needs 200+ trades first)

---

## ENVIRONMENT VARIABLES NEEDED

Add to `.env`:
```
# Existing
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...

# New — Phase 3
FRED_API_KEY=...          # Free from https://fred.stlouisfed.org/docs/api/api_key.html
FINNHUB_API_KEY=...       # Free from https://finnhub.io/register
FLASHALPHA_API_KEY=...    # Free from https://flashalpha.com (optional)
```

---

## SUCCESS CRITERIA

When complete, the system should:
- ✅ Detect 62+ candlestick patterns on every 5-minute bar in < 50ms
- ✅ Detect FVGs, Order Blocks, BOS/CHoCH, and liquidity sweeps in real-time
- ✅ Detect chart-level patterns (H&S, double tops, triangles) on rolling 100-bar window
- ✅ Assemble a 50+ feature vector for XGBoost on every bar
- ✅ Have 140+ academic strategies indexed in ChromaDB for RAG retrieval
- ✅ Pull weekly COT positioning and flag extremes
- ✅ Pull daily macro data (VIX, yield curve, dollar) from FRED
- ✅ Flag upcoming high-impact economic events
- ✅ Generate new strategy hypotheses via Claude API on demand
- ✅ Validate generated strategies with multi-persona review
- ✅ Never block or slow down the live trading pipeline
- ✅ Gracefully degrade if any feed or detector fails
- ✅ All new features flow through to the dashboard for monitoring

---

**System:** Phoenix Trading Bot — Strategy Knowledge Injection
**Status:** Ready for implementation
**Priority:** Phases 1-3 are critical (pattern detection + data feeds). Phases 4-5 are high-value but can iterate. Phase 6 ties everything together.
**Estimated time:** 20-24 hours of Claude Code implementation across a weekend
