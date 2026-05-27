# Building an AI-powered post-session trade analysis system for MNQ/NQ futures

**A custom AI system combining XGBoost classification, Hidden Markov regime detection, RAG-based pattern matching, and Claude API batch analysis can transform a NinjaTrader 8 trading bot into a continuously self-improving system — for under $10/month in API costs.** The most effective architecture is a hybrid: ML models handle quantitative prediction (win probability, optimal stops, regime classification) while an LLM provides qualitative pattern narratives, behavioral analysis, and natural-language strategy recommendations. This approach draws from what actually works at professional quant firms — nightly adaptive backtesting (Trade Ideas Holly), meta-labeling (Marcos Lopez de Prado), and regime-conditional parameter sets — while remaining practical for a solo Python developer. The key insight across all research: **feature quality matters far more than model complexity**, and MAE/MFE capture ratios, order flow imbalance, and council agreement scores are likely the highest-signal features available.

---

## How Lux Algo AI and the best commercial tools actually work

Lux Algo AI is not what most traders assume. It operates as a **massive combinatorial search engine** that iterates through 6+ million pre-backtested strategies across 93+ tickers, ranking candidates by net profit, win rate, drawdown, and profit factor. There are no neural networks generating predictions — it exhaustively searches hard-coded parameter combinations within its Pine Script indicator library. The AI distinction is speed and scale, not intelligence. Its "Quant" coding agent is a fine-tuned LLM for Pine Script generation, and its strategy alerts convert backtested strategies into live webhook signals. The critical limitation: exhaustive backtesting carries significant **overfitting risk**.

Trade Ideas Holly AI is more technically sophisticated. It runs genuine machine learning in a nightly cycle: ingesting complete market data at 4:00 PM ET, testing 70+ strategies against 8,000+ stocks, and filtering survivors through hard gates (**>60% win rate AND ≥2:1 reward-to-risk**). Strategies that stop working get benched; ones that work get promoted. This nightly adaptive approach is the gold standard worth replicating. TrendSpider goes further, letting users train actual Random Forest and KNN models through a no-code interface with walk-forward validation and model "crossbreeding."

For MNQ/NQ specifically, **Edgeful** is the most directly relevant commercial tool. Built by an ex-Goldman Sachs quant, it processes 7+ years of CME exchange data to compute statistical probabilities for specific futures setups — gap fill rates by size and direction, Opening Range Breakout statistics, Initial Balance breakout probabilities, and engulfing candle continuation rates. It connects to NinjaTrader via webhooks and includes Monte Carlo simulation for robustness testing. The lesson: pre-computing historical probabilities for NQ-specific setups provides an orthogonal edge that ML models alone miss.

Bookmap provides the order flow visualization layer — rendering every order book change at 40 FPS, detecting iceberg orders, and tracking cumulative volume delta. While not AI-powered itself, the data it surfaces (liquidity behavior, absorption patterns, institutional activity) represents **excellent input features** for a custom ML system.

---

## The optimal data collection schema for maximum AI learning

The database architecture should use a **PostgreSQL + TimescaleDB + ChromaDB** stack — PostgreSQL for relational trade data, TimescaleDB (a PostgreSQL extension) for tick-level time-series with 10-15x compression, and ChromaDB for vector similarity search in the RAG pipeline. Start with SQLite for prototyping, then migrate when scaling.

Every trade should capture data across five tiers. The essential tier includes entry/exit prices, timestamps, P&L, direction, stop/target levels, setup type, and the three most critical computed metrics: **MAE (Max Adverse Excursion)**, **MFE (Max Favorable Excursion)**, and **capture ratio** (realized profit divided by MFE). Capture ratio reveals whether exits are destroying value — if average MFE on winners is 5.5R but realized profit is only 1.8R, the problem is exits, not entries. The 80th percentile MAE rule provides the stop calibration baseline: place stops beyond where 80% of historical winners experienced their maximum drawdown.

The market context tier records session type (RTH/ETH), time-of-day bucket, minutes since open, market regime label, ATR-14, VIX level, trend strength (ADX), and distance to key levels (VWAP, Point of Control, daily high/low). The order flow tier captures CVD value and slope, DOM bid/ask imbalance ratio, volume delta across 1-minute and 5-minute windows, spread, large order flow direction, and trade rate per second. Technical indicator states should be stored as JSONB for flexibility as indicators change over time. The meta tier includes council confluence score, number of agreeing agents, dominant agent identity, signal strength, tags (setup quality grade, emotional state, checklist compliance), chart snapshots at multiple timeframes, and free-form notes.

The near-miss table is equally important. When the council's confluence score falls within a configurable margin of the execution threshold (e.g., threshold is 7.0, log when score exceeds 5.0), the system should record the full market context, hypothetical entry price, and then continue monitoring to compute hypothetical MAE, MFE, and P&L. Categorize reasons: `score_below_threshold`, `risk_limit_reached`, `cooldown_active`, `volatility_filter`, `missing_confirmation`. This data directly answers whether thresholds should be lowered — query by score bucket to see win rates at each near-miss level.

```python
@dataclass
class TradeRecord:
    entry_time: datetime
    entry_price: float
    direction: int  # 1=long, -1=short
    mae_ticks: float
    mfe_ticks: float
    capture_ratio: float  # pnl / mfe
    cvd_slope: float
    dom_imbalance: float
    atr_14: float
    volatility_regime: int  # From HMM
    minutes_since_open: int
    council_agreement: float
    signal_strength: float
    pnl_ticks: float
    duration_seconds: float
    exit_reason: str  # 'target', 'stop', 'timeout'
    win: bool
```

---

## ML models that actually work for trade outcome prediction

**XGBoost and LightGBM are the clear starting point** — they consistently outperform neural networks on structured tabular trading data. For a trade-level classification problem (win/loss prediction), use `XGBClassifier` with `n_estimators=100-300`, `max_depth=3-6`, `reg_lambda=1-5` for L2 regularization, `learning_rate=0.05-0.2`, and `min_child_weight=5+` to prevent fitting rare patterns. Use SHAP values for per-trade feature importance — this is critical for understanding which council agents drive wins versus losses.

The central challenge is avoiding overfitting on small trade datasets (50-500 historical trades). Five rules govern this:

- **Walk-forward validation only** — use `TimeSeriesSplit`, never random cross-validation, which causes look-ahead bias on time-ordered trade data
- **Feature parsimony** — no more than N/10 features where N is trade count (for 200 trades, use ≤20 features)
- **Bayesian hyperparameter search** with Optuna, optimizing on out-of-sample Sharpe ratio rather than accuracy
- **Bootstrapped confidence intervals** — if the 95% CI includes 50%, the model lacks statistical significance
- **Rolling window retraining** on the most recent 100-200 trades, discarding stale data

Neural networks are only justified with 1,000+ trades. If used, TabNet (from `pytorch-tabnet`) is designed for tabular data with built-in attention and has shown **0.601 accuracy on silver futures direction prediction** in published research — modest but meaningful for a trading signal filter.

Marcos Lopez de Prado's **meta-labeling** approach is directly applicable to a council-based system. The primary model (your signal council) determines trade direction with high recall, catching most opportunities. A secondary binary classifier then determines bet size, including "no bet," correcting for low precision. This dramatically improves F1-score and reduces overfitting. Implementation: concatenate council agent features plus the council's prediction as inputs, train a secondary XGBoost classifier on trade outcomes, and only trade when both the council and meta-model agree. His **triple barrier method** for labeling trades — take-profit (upper), stop-loss (lower), and time expiration (vertical) barriers set dynamically based on per-observation volatility — replaces fixed-horizon labeling and addresses heteroskedasticity.

---

## Market regime detection changes everything

Hidden Markov Models are the gold standard for regime detection. A 2-state or 3-state `GaussianHMM` from `hmmlearn`, trained on log returns and rolling volatility, identifies hidden market states (trending/ranging/volatile) that generate observable features. Even a simple 2-state HMM regime filter can dramatically improve a trend-following strategy by avoiding high-volatility chop.

```python
from hmmlearn.hmm import GaussianHMM
import numpy as np

features = np.column_stack([log_returns.values, rolling_volatility.values])
model = GaussianHMM(n_components=3, covariance_type="full", n_iter=500)
model.fit(features)
hidden_states = model.predict(features)
```

For MNQ/NQ, feed the HMM with daily/hourly log returns, 20-period rolling volatility, volume changes, order flow imbalance, and spread changes. Train on 2+ years of history, serialize the model, and retrain weekly or monthly with walk-forward windows. The `ruptures` library complements this with online change-point detection using the PELT algorithm — alerting when a regime shift is actively occurring rather than just classifying the current regime.

The practical implementation maintains **separate parameter sets per regime**. When the HMM identifies a trending regime, the system uses wider targets and tighter stop multipliers; during choppy regimes, it tightens entry thresholds and may reduce position sizes or pause trading entirely. This is how the Conservative/Aggressive mode toggle works in practice — Conservative mode requires regime alignment plus high council agreement, while Aggressive mode trades in ambiguous regimes with lower confluence thresholds.

---

## RAG-based pattern matching finds your historical edge

The RAG pipeline works by embedding each completed trade as a normalized feature vector, storing it in ChromaDB, and querying for the K most similar historical setups when a new signal appears. ChromaDB is ideal for a Python trading bot — open-source, runs locally with zero cloud dependency, supports metadata filtering, and is **5.9x faster than pgvector** for similarity queries.

Create trade embeddings by normalizing and concatenating 30-50 numerical features directly (trend strength, ATR, RSI, CVD slope, DOM imbalance, volume delta, distance to VWAP, minutes since open, confluence score). When a new setup appears, embed the current market state and retrieve the 10 most similar historical trades. Analyze their win rate, average R-multiple, and average MAE to augment the council's confluence score. If the 10 most similar historical setups show a 75% win rate with 2.3R average, that's a strong green light; if they show 35% with -0.8R average, the system should reduce position size or pass.

Store near-miss embeddings in the same ChromaDB collection tagged with `{"type": "near_miss"}`. When querying similar setups, the system now sees both how similar real trades performed and how similar near-misses would have performed — providing richer data for threshold calibration.

For the LLM integration layer, pass RAG results to Claude as context: "The 10 most similar historical setups had a 72% win rate, average +1.8R, but 3 of the losses occurred during the first 30 minutes after market open. Current setup matches at 0.87 cosine similarity." The LLM can then synthesize this with qualitative factors the ML models miss.

---

## Claude API integration costs under $10/month

The **Claude Message Batches API** is purpose-built for post-session analysis — it's asynchronous with a **50% cost discount** on both input and output tokens. Most batches complete within one hour, and results are available for 29 days. After each session ends, queue the day's trade log as a batch request; results arrive well before the next session.

Using Claude Sonnet 4.6 with the Batch API for a typical session of 20 trades (approximately 3,000 input tokens for the system prompt and trade log, 2,000 output tokens for analysis), the daily cost is roughly **$0.02 per session** — about $0.50/month for daily trading. Prompt caching the system prompt saves an additional 90% on the approximately 2,000-token system prompt that remains constant across sessions. Adding weekly multi-session deep analysis on Opus 4.6 costs $2-5/month, and daily chart vision analysis adds $0.10-0.15/month.

**Structured outputs** are now generally available on Claude, guaranteeing JSON schema compliance via constrained decoding. Define a Pydantic model for `SessionAnalysis` with fields for `patterns_identified`, `parameter_adjustments` (each with `parameter_name`, `current_value`, `recommended_value`, `confidence`, `reasoning`), `behavioral_flags`, `key_insights`, and `regime_assessment`. The API guarantees the response parses correctly — no more fragile regex parsing of free-text responses.

The safety architecture is critical. Never let LLM recommendations directly modify parameters. Implement a `ParameterUpdater` class with absolute bounds (stop distance: 2-20 MNQ points, entry threshold: 0.5-0.95), a maximum change rate of 20% per session, and automatic rejection of low-confidence recommendations. Track all parameter changes over time to create a feedback loop evaluating which LLM recommendations actually improved performance.

The hybrid architecture separates responsibilities cleanly: ML models produce quantitative predictions (optimal stop distance, regime classification, win probability), while the LLM provides pattern narratives, behavioral analysis (detecting revenge trading, premature exits, FOMO entries), regime interpretation, and natural-language journal entries. A `HybridAnalyzer` class runs both pipelines and merges their recommendations with weighted averaging.

---

## The adaptive parameter tuning system

Bayesian optimization with Optuna is more practical than reinforcement learning for parameter optimization with limited computational budget. Define the parameter space (stop loss ticks, take profit ticks, entry threshold, ATR multiplier), run mini-backtests with each parameter set on recent data, and optimize on out-of-sample Sharpe ratio. Fifty evaluations with 10 random starts typically finds a near-optimal parameter set.

The `AdaptiveParameterManager` reoptimizes every 20 completed trades on a rolling window of the most recent 100-200 trades. Critically, it **never jumps to new parameters** — instead, it blends old and new values with exponential smoothing (alpha=0.3), requires statistical significance before updating, and enforces hard guardrails. Professional systems maintain regime-conditional parameter sets and validate new parameters on held-out recent data before deployment.

For the Conservative/Aggressive mode toggle, implement two parameter profiles stored in YAML:

The Conservative profile uses the 90th percentile confidence threshold from the meta-labeling model, requires regime alignment from the HMM, demands ≥80% council agreement, sets stops at the 90th percentile MAE of historical winners, targets only setups where RAG retrieval shows ≥65% historical win rate, and limits to 3-5 trades per session. The Aggressive profile drops to the 70th percentile confidence threshold, trades in ambiguous regimes, accepts ≥60% council agreement, sets stops at the 75th percentile MAE, accepts setups where RAG shows ≥50% win rate, and allows up to 10-15 trades per session.

Reinforcement learning (PPO via Stable-Baselines3) is the advanced path for dynamic parameter selection, but only pursue it after the Bayesian optimization pipeline is proven. RL requires thousands of simulated episodes — created by historical replay of market data — and careful reward engineering using risk-adjusted returns (Sharpe/Sortino), not raw P&L. The FinRL library provides pre-built financial trading environments that integrate with Stable-Baselines3.

---

## The complete Python tech stack

The recommended libraries form a coherent stack that covers every component:

| Component | Library | Purpose |
|-----------|---------|---------|
| Trade prediction | XGBoost, LightGBM, scikit-learn | Win/loss classification, feature importance |
| Regime detection | hmmlearn, ruptures | HMM state classification, change-point detection |
| Parameter optimization | Optuna, scikit-optimize | Bayesian hyperparameter tuning |
| Backtesting | VectorBT | Fast vectorized parameter sweeps, MAE/MFE analysis |
| Performance reports | QuantStats | Tear sheets, Sharpe/Sortino/Calmar ratios |
| ML for finance | mlfinpy | Triple barrier, meta-labeling, CUSUM filter |
| Vector search | ChromaDB | RAG trade similarity matching |
| LLM integration | anthropic SDK, instructor | Claude API structured outputs |
| Model interpretability | SHAP | Per-trade feature attribution |
| Experiment tracking | MLflow | Log parameters, metrics, compare runs |
| Database | PostgreSQL + TimescaleDB | Trade records + tick data |
| NT8 integration | websockets or CrossTrade API | Bidirectional NinjaTrader 8 communication |
| Visualization | Plotly, Dash | Interactive analysis dashboards |

For NinjaTrader 8 integration specifically, the CrossTrade API (crosstrade.io) is the most production-ready option — a REST API + WebSocket streaming bridge that turns NinjaTrader desktop into a remote execution engine accessible from Python. The open-source NinjaSocket project provides a simpler WebSocket client alternative. For post-trade analysis only, CSV export from NT8 into the Python analysis pipeline is the simplest starting path.

---

## Conclusion

The highest-leverage starting point is not the most complex component — it's the data collection layer. Without comprehensive per-trade logging of MAE/MFE, order flow state, regime labels, and council metadata, no amount of ML sophistication will help. Build the `TradeRecord` dataclass and near-miss logger first, accumulate 200+ trades with full context, then layer on XGBoost classification with SHAP interpretability. The regime detection HMM and Bayesian parameter optimizer each represent force multipliers that compound the edge — a trend-following strategy that pauses during detected chop regimes will outperform one that trades blindly through all conditions. The Claude API integration adds a qualitative intelligence layer at negligible cost ($0.02/session) that catches behavioral patterns and regime shifts that pure statistical models miss. The entire system should follow Holly AI's nightly adaptive principle: **hard performance gates first, then optimization within the survivors.** Any parameter set that drops below 55% win rate or 1.5:1 reward-to-risk in walk-forward testing gets benched automatically, regardless of what the ML models or LLM recommend.