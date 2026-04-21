# Phase C Architecture: ML-Powered Trade Filtering

**Status:** Architecture notes -- NOT for implementation until 200+ trades logged  
**Prerequisite:** Phase B complete (HMM regimes, ChromaDB RAG, Claude Batch debrief, JSONL history all producing data)  
**Dependencies:** `xgboost`, `optuna`, `shap`, `scikit-learn` (add to `requirements.txt` when building)

---

## 1. XGBoost Win Probability Classifier

### Purpose

A calibrated binary classifier that outputs P(win) for each candidate trade. Runs AFTER the council vote and pre-trade filter, BEFORE the OIF file is written. This is a statistical second opinion on trades that have already passed all rule-based and AI-advisory gates.

### Input Features (from existing logged data)

Every feature below is already captured by `HistoryLogger.log_entry()` in `core/history_logger.py` or derivable from `core/strategy_tracker.py` and `core/expectancy_engine.py`:

| # | Feature | Source | Type |
|---|---------|--------|------|
| 1 | `council_agreement` | `agents/council_gate.py` CouncilResult.bullish_votes (or bearish) / 7 | float 0-1 |
| 2 | `signal_confidence` | `Signal.confidence` from `strategies/base_strategy.py` | float 0-100 |
| 3 | `entry_score` | `Signal.entry_score` (0-60 precision score) | float 0-60 |
| 4 | `atr_5m` | `market["atr_5m"]` from `core/tick_aggregator.py` | float |
| 5 | `cvd_slope` | Derived: delta of last 3 `market["cvd"]` values from bar log | float |
| 6 | `dom_imbalance` | `market["dom_imbalance"]` from `core/dom_analyzer.py` | float -1 to 1 |
| 7 | `tf_alignment` | `market["tf_votes_bullish"] - market["tf_votes_bearish"]` (signed, direction-relative) | int |
| 8 | `hmm_regime` | `HMMRegimeDetector.current_regime` from `core/hmm_regime.py` -- one-hot encoded (TRENDING/RANGING/VOLATILE) | categorical |
| 9 | `minutes_since_open` | `datetime.now() - session_open` (8:30 CST from `core/session_manager.py`) | float |
| 10 | `price_vs_vwap` | `(price - vwap) / atr_5m` normalized distance | float |
| 11 | `capture_ratio_trailing` | Rolling mean of last 10 trades' `mfe / (mfe + mae)` from `ExpectancyEngine` | float 0-1 |
| 12 | `strategy_win_rate_20` | `StrategyTracker.strategies[name]["last_10_results"]` extended to last 20 | float 0-1 |
| 13 | `strategy_regime_wr` | `StrategyTracker.regime_stats[strategy][regime]["wins"] / total` | float 0-1 |
| 14 | `rag_similarity_score` | ChromaDB top-match distance (when built) -- 0 if unavailable | float 0-1 |
| 15 | `consecutive_losses` | `RiskManager` current consecutive loss count | int |
| 16 | `session_regime` | Time-based regime from `SessionManager` -- one-hot | categorical |
| 17 | `fingerprint_risk` | `NoTradeFingerprint.check_conditions()` risk score 0-100 | float |
| 18 | `bar_delta` | `market["bar_delta"]` -- net volume delta of last completed bar | float |

Max features: 18 raw, ~22 after one-hot encoding. With 200 trades, the N/10 heuristic allows 20 features, so this is at the boundary. Feature selection (below) will trim to the best 15-18.

### Training Protocol

Walk-forward cross-validation on a rolling window. Never train on future data.

```python
# pseudocode -- lives in tools/ml_trainer.py (new file)
from sklearn.model_selection import TimeSeriesSplit
import xgboost as xgb
import shap

class WinProbTrainer:
    """Walk-forward XGBoost trainer for trade win probability."""

    MIN_TRADES = 200
    WINDOW_SIZE = 200       # Rolling training window
    MAX_FEATURES = 20       # N/10 rule cap
    N_SPLITS = 5            # TimeSeriesSplit folds

    def __init__(self, trade_history: list[dict]):
        self.trades = trade_history
        self.model = None
        self.feature_names = []
        self.shap_explainer = None

    def build_features(self, trades: list[dict]) -> tuple:
        """Extract feature matrix X and label vector y from trade dicts.

        Each trade dict comes from HistoryLogger 'entry' + 'exit' events
        joined on trade_id plus StrategyTracker and ExpectancyEngine stats.
        """
        # ... extract features listed in table above ...
        # y = 1 if trade pnl_dollars > 0 else 0
        return X, y

    def train(self) -> dict:
        """Walk-forward train with TimeSeriesSplit. Returns metrics."""
        if len(self.trades) < self.MIN_TRADES:
            raise ValueError(f"Need {self.MIN_TRADES} trades, have {len(self.trades)}")

        X, y = self.build_features(self.trades[-self.WINDOW_SIZE:])

        # Feature selection: mutual information, keep top MAX_FEATURES
        from sklearn.feature_selection import mutual_info_classif
        mi = mutual_info_classif(X, y)
        top_k = mi.argsort()[-self.MAX_FEATURES:]
        X = X[:, top_k]
        self.feature_names = [self.feature_names[i] for i in top_k]

        # Walk-forward splits
        tscv = TimeSeriesSplit(n_splits=self.N_SPLITS)
        oof_preds = np.zeros(len(y))

        for train_idx, val_idx in tscv.split(X):
            model = xgb.XGBClassifier(
                n_estimators=100, max_depth=3, learning_rate=0.1,
                subsample=0.8, colsample_bytree=0.8,
                use_label_encoder=False, eval_metric="logloss",
            )
            model.fit(X[train_idx], y[train_idx],
                      eval_set=[(X[val_idx], y[val_idx])],
                      verbose=False)
            oof_preds[val_idx] = model.predict_proba(X[val_idx])[:, 1]

        # Final model on full window
        self.model = xgb.XGBClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            subsample=0.8, colsample_bytree=0.8,
        )
        self.model.fit(X, y)
        self.shap_explainer = shap.TreeExplainer(self.model)

        return self._validate(y, oof_preds)
```

### Validation Gate

The model is rejected (not deployed) if it fails any of these checks:

1. **Bootstrap CI test:** Resample OOF predictions 1000 times, compute AUC each time. If the 95% CI lower bound includes 0.5, the model has no proven edge -- reject it.
2. **Brier score:** Must be below 0.25 (better than always predicting base rate).
3. **Calibration:** Predicted probabilities must match observed frequencies within 10% across 5 bins (reliability diagram check).

```python
def _validate(self, y_true, y_pred_proba) -> dict:
    """Bootstrap validation. Returns metrics + pass/fail."""
    from sklearn.metrics import roc_auc_score, brier_score_loss
    aucs = []
    for _ in range(1000):
        idx = np.random.choice(len(y_true), len(y_true), replace=True)
        if len(np.unique(y_true[idx])) < 2:
            continue
        aucs.append(roc_auc_score(y_true[idx], y_pred_proba[idx]))

    ci_lower = np.percentile(aucs, 2.5)
    ci_upper = np.percentile(aucs, 97.5)
    brier = brier_score_loss(y_true, y_pred_proba)

    return {
        "auc_mean": np.mean(aucs),
        "auc_ci_lower": ci_lower,
        "auc_ci_upper": ci_upper,
        "brier_score": brier,
        "passed": ci_lower > 0.5 and brier < 0.25,
        "n_trades": len(y_true),
    }
```

### SHAP Integration

Every trade that goes through the classifier gets per-feature SHAP values logged alongside the decision. This feeds into the session debriefer for coaching ("this trade was flagged because DOM imbalance was against you and the strategy's recent win rate in VOLATILE regime was 30%").

### Integration Point in base_bot.py

The classifier slots in between the pre-trade filter and OIF execution:

```python
# In base_bot.py, after pretrade_filter.check() returns CLEAR/CAUTION:

if self.ml_classifier and self.ml_classifier.is_ready():
    win_prob, shap_values = self.ml_classifier.predict(signal, market)
    self.history_logger.log_ml_prediction(signal.trade_id, win_prob, shap_values)

    if win_prob < 0.45:
        logger.info(f"[ML] P(win)={win_prob:.2f} < 0.45 -- skipping trade")
        continue  # Do not trade
    elif win_prob < 0.55:
        risk_dollars *= 0.5  # Reduce size for marginal trades
        logger.info(f"[ML] P(win)={win_prob:.2f} -- half size")
    # else: full size, proceed
```

The classifier lives in `core/ml_classifier.py` (new file). It loads a saved XGBoost model from `logs/models/xgb_win_prob_v{N}.json` and exposes a simple `predict(signal, market) -> (float, dict)` interface. The model file is versioned -- see section 4 for rollback.

---

## 2. Meta-Labeling (Lopez de Prado)

### Concept

The existing pipeline (strategies + council + pre-trade filter) is the **primary model**. It has high recall -- it finds tradeable setups. But not every setup it flags is worth the same size. Meta-labeling adds a **secondary model** that decides bet sizing, including "no bet."

This is a strict separation: the primary model decides DIRECTION, the meta-model decides SIZE.

### Triple Barrier Labeling

Every historical trade gets relabeled using triple barriers computed from ATR at entry time. This replaces the simple win/loss label with a more nuanced outcome:

```python
# pseudocode -- lives in tools/meta_labeler.py (new file)

class TripleBarrierLabeler:
    """Apply triple-barrier method to historical trades."""

    def label_trade(self, trade: dict, subsequent_bars: list[dict]) -> dict:
        """Walk forward through bars after entry until a barrier is hit.

        Args:
            trade: entry event from HistoryLogger JSONL
            subsequent_bars: 1-min bars following entry

        Returns:
            dict with barrier_hit, label, holding_period, touch_price
        """
        entry = trade["price"]
        atr = trade["market"]["atr_5m"]
        direction = 1 if trade["direction"] == "LONG" else -1

        # Dynamic barriers from ATR
        upper = entry + direction * atr * 1.5   # Take-profit
        lower = entry - direction * atr * 1.0   # Stop-loss
        max_bars = int(atr / 2)                 # Time barrier scales with vol

        for i, bar in enumerate(subsequent_bars[:max_bars]):
            price = bar["close"]
            if direction * (price - entry) >= direction * (upper - entry):
                return {"label": 1, "barrier": "upper", "bars_held": i + 1}
            if direction * (price - entry) <= direction * (lower - entry):
                return {"label": 0, "barrier": "lower", "bars_held": i + 1}

        # Time expiration -- label based on final P&L
        final_price = subsequent_bars[min(max_bars, len(subsequent_bars)) - 1]["close"]
        return {
            "label": 1 if direction * (final_price - entry) > 0 else 0,
            "barrier": "vertical",
            "bars_held": max_bars,
        }
```

### Secondary Model Features

The meta-model receives everything the primary model produced, plus market context:

| Feature | Description |
|---------|-------------|
| `primary_direction` | LONG=1, SHORT=-1 from strategy pipeline |
| `primary_confidence` | Signal.confidence |
| `primary_entry_score` | Signal.entry_score |
| `council_agreement` | Agreement ratio from CouncilResult |
| `pretrade_verdict` | CLEAR=1, CAUTION=0.5 (SIT_OUT already filtered) |
| `fingerprint_risk` | NoTradeFingerprint risk score |
| `hmm_regime` | One-hot from HMMRegimeDetector |
| `atr_5m` | Current volatility |
| `dom_imbalance` | DOM pressure |
| `cvd_slope` | Volume trend |
| `price_vs_vwap` | Normalized distance |
| `minutes_since_open` | Time context |
| `strategy_regime_wr` | Strategy performance in current regime |
| `xgb_win_prob` | Output from section 1's classifier |

### Output Mapping

The meta-model outputs a continuous score 0-1 mapped to discrete sizing:

| Meta Score | Action | Size |
|------------|--------|------|
| < 0.30 | No bet | 0 contracts |
| 0.30 - 0.50 | Quarter size | `risk_dollars * 0.25` |
| 0.50 - 0.70 | Half size | `risk_dollars * 0.50` |
| > 0.70 | Full size | `risk_dollars * 1.00` |

For single-contract MNQ trading (current mode), quarter/half size translates to tighter stops rather than fractional contracts: the meta-model reduces risk exposure by narrowing the stop distance, which lowers dollar risk per trade while keeping the contract count at 1.

### Integration Point

```python
# In base_bot.py, after the XGBoost win-prob check:

if self.meta_model and self.meta_model.is_ready():
    meta_features = {
        "primary_direction": 1 if signal.direction == "LONG" else -1,
        "primary_confidence": signal.confidence,
        "primary_entry_score": signal.entry_score,
        "council_agreement": council_result.bullish_votes / 7,
        "pretrade_verdict": 1.0 if verdict.action == "CLEAR" else 0.5,
        "xgb_win_prob": win_prob,
        **self._extract_market_features(market),
    }
    meta_score = self.meta_model.predict(meta_features)
    self.history_logger.log_meta_prediction(signal.trade_id, meta_score)

    if meta_score < 0.30:
        logger.info(f"[META] score={meta_score:.2f} -- no bet")
        continue
    elif meta_score < 0.50:
        risk_dollars *= 0.25
    elif meta_score < 0.70:
        risk_dollars *= 0.50
    # else: full size
```

### Training

The meta-model trains on triple-barrier-labeled trades. It uses the same walk-forward `TimeSeriesSplit` protocol as the win-prob classifier. Key difference: the meta-model's labels come from the triple barrier method, not raw win/loss. This captures trades that technically "won" but had terrible risk-adjusted paths (hit MAE hard before recovering) and labels them as losses.

The meta-model lives in `core/meta_model.py` (new file) and saves to `logs/models/meta_label_v{N}.json`.

---

## 3. Bayesian Parameter Optimization (Optuna)

### Purpose

Replace the brute-force grid sweep in `tools/optimizer.py` with Bayesian optimization via Optuna. The current grid search tests fixed combinations (`PARAM_GRID` in `optimizer.py`). Optuna uses Tree-Parzen Estimators to explore the space far more efficiently, finding better parameter sets in fewer iterations.

### Parameters to Optimize

These are the parameters that directly affect trade outcomes, sourced from `config/strategies.py` STRATEGIES dict and `config/settings.py`:

| Parameter | Current Default | Search Range | Step |
|-----------|----------------|-------------|------|
| `stop_ticks` | 12 | 6 - 24 | 1 |
| `target_rr` | 1.5 | 1.0 - 3.0 | 0.1 |
| `min_confluence` | 3.0 | 1.5 - 5.0 | 0.25 |
| `min_momentum_confidence` | 50 | 30 - 80 | 5 |
| `risk_per_trade` | 15.0 | 8.0 - 20.0 | 1.0 |

Each parameter set is optimized **per HMM regime** from `core/hmm_regime.py`. This means three separate parameter configs for TRENDING, RANGING, and VOLATILE, stored in `_REGIME_PARAMS` within `hmm_regime.py`.

### Objective Function

Optuna maximizes out-of-sample Sharpe ratio, not raw P&L. This penalizes volatility and rewards consistency:

```python
# pseudocode -- lives in tools/optuna_optimizer.py (new file)
import optuna

class BayesianOptimizer:
    """Optuna-based parameter optimizer using mini-backtests."""

    RETRAIN_EVERY_N_TRADES = 20
    REPLAY_WINDOW = 200      # Trades to replay
    MAX_PARAM_CHANGE = 0.20  # Max 20% shift per optimization cycle

    def __init__(self, trade_history: list[dict], bar_history: list[dict]):
        self.trades = trade_history
        self.bars = bar_history
        self.current_params = self._load_current_params()

    def _load_current_params(self) -> dict:
        """Read current params from config/strategies.py."""
        from config.strategies import STRATEGY_DEFAULTS
        return dict(STRATEGY_DEFAULTS)

    def _objective(self, trial: optuna.Trial, regime: str) -> float:
        """Single Optuna trial: suggest params, mini-backtest, return Sharpe."""
        params = {
            "stop_ticks": trial.suggest_int("stop_ticks", 6, 24),
            "target_rr": trial.suggest_float("target_rr", 1.0, 3.0, step=0.1),
            "min_confluence": trial.suggest_float("min_confluence", 1.5, 5.0, step=0.25),
            "min_momentum_confidence": trial.suggest_int("min_momentum", 30, 80, step=5),
            "risk_per_trade": trial.suggest_float("risk_per_trade", 8.0, 20.0, step=1.0),
        }

        # Enforce max change rate from current params
        for key, val in params.items():
            current = self.current_params.get(key, val)
            max_delta = abs(current * self.MAX_PARAM_CHANGE)
            if abs(val - current) > max_delta:
                return float("-inf")  # Prune this trial

        # Mini-backtest: replay recent trades with candidate params
        # Uses tools/backtester.py Backtester class with param overrides
        regime_trades = [t for t in self.trades[-self.REPLAY_WINDOW:]
                         if t.get("regime") == regime]
        if len(regime_trades) < 20:
            return float("-inf")  # Not enough regime-specific data

        results = self._replay(regime_trades, params)
        return results["sharpe_ratio"]

    def optimize_regime(self, regime: str, n_trials: int = 50) -> dict:
        """Run Optuna study for a single regime."""
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
            pruner=optuna.pruners.MedianPruner(),
        )
        study.optimize(
            lambda trial: self._objective(trial, regime),
            n_trials=n_trials,
            timeout=120,  # 2 minutes max
        )
        return {
            "regime": regime,
            "best_params": study.best_params,
            "best_sharpe": study.best_value,
            "n_trials_completed": len(study.trials),
        }

    def optimize_all_regimes(self) -> dict:
        """Optimize each HMM regime independently."""
        results = {}
        for regime in ["TRENDING", "RANGING", "VOLATILE"]:
            results[regime] = self.optimize_regime(regime)
        return results
```

### Reoptimization Trigger

The optimizer runs automatically every 20 completed trades (checked in `base_bot.py` after each trade exit). It runs as a background task so it never blocks live trading:

```python
# In base_bot.py, after recording a completed trade:

self._trades_since_optimize += 1
if self._trades_since_optimize >= 20 and self.bayesian_optimizer:
    self._trades_since_optimize = 0
    asyncio.create_task(self._run_optimization())

async def _run_optimization(self):
    """Background parameter optimization. Never blocks trading."""
    try:
        results = self.bayesian_optimizer.optimize_all_regimes()
        # Log results but do NOT auto-apply -- human reviews first
        self.history_logger.log_optimization(results)
        await tg.send(f"Optuna optimization complete. "
                      f"TRENDING Sharpe: {results['TRENDING']['best_sharpe']:.2f}")
    except Exception as e:
        logger.warning(f"Optimization failed: {e}")
```

### Safety Constraint: 20% Change Rate

No single optimization cycle can shift any parameter by more than 20% from its current value. This prevents the optimizer from making drastic changes based on a small window of trades. Over 5 cycles (100 trades), parameters can drift significantly, but each step is conservative.

---

## 4. Data Requirements & Migration Path

### Minimum Data Thresholds

| Milestone | Trade Count | Unlocks |
|-----------|-------------|---------|
| Phase B complete | 0-50 | JSONL logging, RAG indexing, batch debrief |
| Feature quality verified | 50-100 | All 18 features logging correctly, no nulls |
| XGBoost training viable | 200 | Win-prob classifier, SHAP analysis |
| Meta-labeling viable | 250 | Triple barrier labels need extra history for walk-forward |
| Optuna viable | 200 + 20 per regime | Need 20+ trades per regime for regime-conditional optimization |

### Feature Quality Checklist

Before any model trains, verify that the JSONL history contains non-null values for every input feature. Run this check against `logs/history/*.jsonl`:

```python
REQUIRED_FIELDS_ENTRY = [
    "confidence", "entry_score", "market.atr_5m", "market.cvd",
    "market.dom_imbalance", "market.tf_votes_bullish",
    "market.tf_votes_bearish", "market.vwap",
]
REQUIRED_FIELDS_EXIT = [
    "pnl_dollars", "mae_ticks", "mfe_ticks", "hold_time_s",
]

def audit_feature_quality(jsonl_dir: str) -> dict:
    """Scan all JSONL files and report null/missing rates per field."""
    # Returns {field: {"present": N, "null": N, "missing": N}}
    # All fields must have < 5% null+missing rate before training
```

### SQLite Migration

The JSONL format in `core/history_logger.py` is excellent for append-only logging and human readability. Switch to SQLite when:

1. **Query patterns become complex** -- e.g., "all LONG trades in VOLATILE regime with entry_score > 45 and dom_imbalance < -0.3" is painful in JSONL but trivial in SQL.
2. **Trade count exceeds 1000** -- scanning JSONL files becomes slow for feature extraction.
3. **ChromaDB RAG needs joins** -- linking RAG similarity results back to trade outcomes.

Migration path: keep JSONL as the write format (never change `HistoryLogger`). Add a nightly ETL script (`tools/jsonl_to_sqlite.py`) that reads all JSONL files and inserts into `logs/phoenix_trades.db` with proper indexes. Models read from SQLite; live logging stays JSONL.

Schema:

```sql
CREATE TABLE trades (
    trade_id TEXT PRIMARY KEY,
    ts_entry TEXT, ts_exit TEXT,
    direction TEXT, strategy TEXT, regime TEXT, hmm_regime TEXT,
    entry_price REAL, exit_price REAL, pnl_dollars REAL,
    confidence REAL, entry_score REAL, stop_ticks INTEGER,
    target_rr REAL, atr_5m REAL, cvd REAL, dom_imbalance REAL,
    tf_votes_bullish INTEGER, tf_votes_bearish INTEGER,
    vwap REAL, price_vs_vwap REAL,
    mae_ticks REAL, mfe_ticks REAL, capture_ratio REAL,
    council_agreement REAL, pretrade_verdict TEXT,
    fingerprint_risk REAL, rag_similarity REAL,
    hold_time_s REAL, result TEXT  -- WIN / LOSS
);
CREATE INDEX idx_regime ON trades(hmm_regime);
CREATE INDEX idx_strategy ON trades(strategy);
CREATE INDEX idx_ts ON trades(ts_entry);
```

### Walk-Forward Validation Protocol

All models use the same protocol -- no exceptions:

1. **Never train on future data.** TimeSeriesSplit only.
2. **Rolling window:** Train on trades [N-200, N-20], validate on [N-20, N]. The 20-trade gap prevents label leakage from correlated consecutive trades.
3. **Purge & embargo:** Drop 3 trades on each side of the train/val boundary to prevent information leakage from overlapping trade durations (following Lopez de Prado's combinatorial purged cross-validation principle, simplified for our small sample).
4. **Retrain schedule:** Every 20 trades, retrain from scratch on the latest 200-trade window. No incremental learning -- full retrain ensures the model adapts to regime shifts.

### Model Versioning and Rollback

Models are saved as versioned files in `logs/models/`:

```
logs/models/
  xgb_win_prob_v001.json       # XGBoost model (xgb.save_model format)
  xgb_win_prob_v001_meta.json  # Training metadata: date, n_trades, metrics, features
  meta_label_v001.json
  meta_label_v001_meta.json
  optuna_params_v001.json      # Best params per regime
  active_models.json           # Points to currently active version of each model
```

`active_models.json` example:

```json
{
    "xgb_win_prob": {"version": 3, "activated": "2026-05-15T10:00:00", "auc": 0.62},
    "meta_label": {"version": 2, "activated": "2026-05-14T10:00:00", "auc": 0.59},
    "optuna_params": {"version": 5, "activated": "2026-05-15T10:00:00"}
}
```

Rollback procedure: update `active_models.json` to point to a previous version. The `MLClassifier` and `MetaModel` classes in `core/` read the active version on startup and whenever `active_models.json` changes (file watcher or explicit reload via Telegram command `/ml_rollback v2`).

---

## 5. Safety & Guardrails

### Cardinal Rule: Models Are Advisory Only

No ML model directly writes OIF files or executes trades. The decision chain is:

```
Strategy signal
  -> Council vote (advisory)
  -> Pre-trade filter (advisory, can SIT_OUT)
  -> XGBoost win-prob (advisory, can skip or reduce size)
  -> Meta-model sizing (advisory, can reduce to zero)
  -> RiskManager hard limits (enforced gate)
  -> OIF file written
```

Every step except `RiskManager` is advisory. `RiskManager` in `core/risk_manager.py` enforces hard dollar limits (`MAX_LOSS_PER_TRADE`, `DAILY_LOSS_LIMIT`) regardless of what models say. A model can reduce risk but never increase it beyond the configured caps.

### Automatic Model Benching

Each model tracks its own live accuracy in a rolling 50-trade window. If accuracy drops below 55% (barely better than coin flip for a system that should have edge), the model is automatically benched -- it stops influencing trades but continues logging predictions for monitoring:

```python
# In core/ml_classifier.py

class MLClassifier:
    BENCH_THRESHOLD = 0.55
    EVAL_WINDOW = 50

    def __init__(self):
        self.predictions: list[dict] = []  # {trade_id, predicted, actual}
        self.is_benched = False

    def record_outcome(self, trade_id: str, actual_win: bool):
        """Called after each trade exit. Tracks live accuracy."""
        for p in reversed(self.predictions):
            if p["trade_id"] == trade_id:
                p["actual"] = actual_win
                break

        # Check rolling accuracy
        recent = [p for p in self.predictions[-self.EVAL_WINDOW:]
                  if "actual" in p]
        if len(recent) >= 30:  # Need minimum sample
            accuracy = sum(1 for p in recent
                           if (p["predicted"] > 0.5) == p["actual"]) / len(recent)
            if accuracy < self.BENCH_THRESHOLD:
                self.is_benched = True
                logger.warning(f"[ML] Model benched: accuracy {accuracy:.1%} "
                               f"< {self.BENCH_THRESHOLD:.0%} over last {len(recent)} trades")
                # Notify via Telegram
                asyncio.create_task(tg.send(
                    f"ML model benched. Accuracy: {accuracy:.1%}. "
                    f"Trading continues without ML filter."
                ))

    def predict(self, signal, market) -> tuple[float, dict]:
        """Returns (win_probability, shap_values). Returns (0.5, {}) if benched."""
        if self.is_benched:
            return 0.5, {}  # Neutral -- does not affect trade
        # ... normal prediction ...
```

### A/B Testing: Lab Bot vs Prod Bot

Phoenix Bot already has the `lab_bot.py` / `prod_bot.py` split. The A/B protocol for ML rollout:

1. **Phase C-alpha:** `lab_bot.py` loads ML models, `prod_bot.py` does not. Both receive identical market data from `bridge_server.py` on `:8766`. Both log to separate JSONL files (bot name prefix in `HistoryLogger`).
2. **Comparison script:** `tools/ab_tester.py` (already exists) is extended to compare lab vs prod results over the same sessions. Key metrics: Sharpe ratio, win rate, average P&L, max drawdown.
3. **Promotion criteria:** Lab bot must outperform prod bot by > 10% Sharpe over 50+ trades with p < 0.05 (paired t-test on per-trade P&L) before ML gets promoted to prod.

### Gradual Rollout Stages

| Stage | Duration | ML Behavior | Risk |
|-------|----------|-------------|------|
| **Observation** | First 50 trades after 200 threshold | Models predict and log, but predictions do NOT affect trade sizing or filtering. Dashboard shows "ML says X" alongside actual decisions. | Zero -- pure logging |
| **Size adjustment** | Next 50 trades | Models can reduce size (half/quarter) but cannot skip trades entirely. `meta_score < 0.30` becomes `meta_score < 0.30 -> half size` instead of skip. | Low -- worst case is smaller winners |
| **Full filter** | Ongoing | Models can skip trades (meta_score < 0.30 = no trade). Full integration as designed in sections 1-2. | Medium -- could miss valid setups |
| **Regime-adaptive params** | After Optuna proves out | Optuna-optimized params auto-applied per regime (still within 20% change rate). | Medium -- parameter changes affect all trades in that regime |

Each stage requires explicit user approval via Telegram command (`/ml_stage observation`, `/ml_stage sizing`, `/ml_stage full`). No automatic promotion between stages. The `config/settings.py` file gets a new constant:

```python
# ─── ML Phase C Configuration ─────────────────────────────────────
ML_STAGE = "disabled"          # disabled | observation | sizing | full | regime_adaptive
ML_MIN_TRADES_REQUIRED = 200   # Won't activate until this many trades logged
ML_BENCH_ACCURACY = 0.55       # Auto-bench below this rolling accuracy
ML_WIN_PROB_SKIP = 0.45        # Skip trade if P(win) below this
ML_WIN_PROB_REDUCE = 0.55      # Half size if P(win) below this
ML_META_SKIP = 0.30            # Meta-model "no bet" threshold
```

### Monitoring Dashboard

The existing Flask dashboard at `dashboard/server.py` on `:5000` gets a new ML panel showing:

- Current ML stage (observation / sizing / full)
- Model versions and activation dates
- Rolling 50-trade accuracy for each model
- SHAP summary plot (top 10 features) for the current session
- Benched/active status with reason
- Last optimization results and suggested parameter changes

This is purely display -- no dashboard button can promote ML stages or apply parameter changes. That must go through Telegram commands or manual config edits. The user always decides.
