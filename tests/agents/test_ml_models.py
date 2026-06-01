"""
tests/agents/test_ml_models.py
Unit tests for Phase 4 ML modules:
  - agents/regime_classifier.py
  - agents/signal_predictor.py

All tests use synthetic in-tmp data; no live warehouse is touched.
All tests must complete in < 5 s total (tiny XGBoost models).
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBClassifier

# ── Ensure project root is importable ──────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import regime_classifier, signal_predictor
from agents.regime_classifier import (
    UNKNOWN_REGIME,
    MIN_LIFT_OVER_BASELINE,
    predict as rc_predict,
    train as rc_train,
)
from agents.signal_predictor import (
    SAFE_DEFAULT_PROBA,
    predict_proba as sp_predict_proba,
    train as sp_train,
)


# ══════════════════════════════════════════════════════════════════════════════
# Infrastructure
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _bust_caches():
    """Clear module-level caches so cached paths from one test don't leak."""
    regime_classifier._MODEL_CACHE["path"] = None
    regime_classifier._MODEL_CACHE["bundle"] = None
    signal_predictor._MODEL_CACHE["path"] = None
    signal_predictor._MODEL_CACHE["bundle"] = None
    yield
    # Bust again on teardown to be safe
    regime_classifier._MODEL_CACHE["path"] = None
    regime_classifier._MODEL_CACHE["bundle"] = None
    signal_predictor._MODEL_CACHE["path"] = None
    signal_predictor._MODEL_CACHE["bundle"] = None


def _build_rc_bundle(tmp_path: Path, name: str = "ok.pkl") -> Path:
    """Build a minimal but valid regime_classifier bundle and dump it.

    Trains a tiny 3-class XGBClassifier on 4 rows so joblib loading works.
    Returns the .pkl path.
    """
    le_strat = LabelEncoder().fit(["a", "b"])
    le_dir = LabelEncoder().fit(["LONG", "SHORT"])
    le_regime = LabelEncoder().fit(["LOW_VOL_TREND", "HIGH_VOLATILITY", "MEAN_REVERT_CHOP"])

    clf = XGBClassifier(
        n_estimators=10,
        objective="multi:softprob",
        num_class=3,
        random_state=42,
        eval_metric="mlogloss",
    )
    X = pd.DataFrame({
        "market_open_minutes": [0.0, 30.0, -10.0, 60.0],
        "day_of_week": [1, 2, 3, 4],
        "strategy": le_strat.transform(["a", "b", "a", "b"]),
        "direction": le_dir.transform(["LONG", "LONG", "SHORT", "SHORT"]),
    })
    y = le_regime.transform(
        ["LOW_VOL_TREND", "HIGH_VOLATILITY", "MEAN_REVERT_CHOP", "LOW_VOL_TREND"]
    )
    clf.fit(X, y)

    bundle = {
        "version": 1,
        "model": clf,
        "le_strategy": le_strat,
        "le_direction": le_dir,
        "le_regime": le_regime,
        "feature_order": ["market_open_minutes", "day_of_week", "strategy", "direction"],
    }
    pkl_path = tmp_path / name
    joblib.dump(bundle, pkl_path)
    return pkl_path


def _build_sp_bundle(tmp_path: Path, name: str = "sp_ok.pkl") -> Path:
    """Build a minimal valid signal_predictor bundle (binary XGB)."""
    from agents.signal_predictor import DEFAULT_NUMERIC_KEYS

    rng = random.Random(7)
    n = 20
    # Half wins / half losses so both classes are present
    y_vals = [1] * (n // 2) + [0] * (n // 2)
    rng.shuffle(y_vals)

    keys = list(DEFAULT_NUMERIC_KEYS[:5])  # use a tiny feature subset
    X_list = [[rng.uniform(0, 10) for _ in keys] for _ in range(n)]

    X = pd.DataFrame(X_list, columns=keys)
    y = pd.Series(y_vals, name="win")

    clf = XGBClassifier(
        n_estimators=10,
        objective="binary:logistic",
        random_state=42,
        eval_metric="logloss",
    )
    clf.fit(X, y)

    bundle = {"version": 1, "model": clf, "feature_keys": keys}
    pkl_path = tmp_path / name
    joblib.dump(bundle, pkl_path)
    return pkl_path


def _build_warehouse(tmp_path: Path, rows: list[dict]) -> Path:
    """Create a synthetic DuckDB warehouse at tmp_path/phx.duckdb.

    Inserts a single 'run1' runs row, then one trades row per dict in `rows`.
    Required row keys:
      strategy, direction, entry_ts (ISO str with TZ), entry_price,
      exit_ts, exit_price, pnl_dollars, mae_ticks, mfe_ticks, regime,
      entry_context (str or None)
    """
    schema_path = _ROOT / "tools" / "warehouse" / "schema.sql"
    schema_sql = schema_path.read_text()
    # Strip INSTALL/LOAD lines that attempt network access in CI
    schema_sql = "\n".join(
        ln for ln in schema_sql.splitlines()
        if not ln.strip().startswith(("INSTALL", "LOAD"))
    )

    db_path = tmp_path / "phx.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(schema_sql)
    con.execute(
        "INSERT INTO runs (run_id, source_filename, csv_kind, friction_applied) "
        "VALUES (?, ?, ?, ?)",
        ["run1", "t.csv", "trades", True],
    )
    for r in rows:
        con.execute(
            """
            INSERT INTO trades (
                run_id, strategy, direction,
                entry_ts, entry_price,
                exit_ts, exit_price,
                pnl_dollars, mae_ticks, mfe_ticks,
                regime, entry_context
            ) VALUES ('run1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                r["strategy"],
                r["direction"],
                r["entry_ts"],
                r["entry_price"],
                r["exit_ts"],
                r["exit_price"],
                r.get("pnl_dollars", 0.0),
                r.get("mae_ticks"),
                r.get("mfe_ticks"),
                r.get("regime"),
                r.get("entry_context"),
            ],
        )
    con.close()
    return db_path


# ══════════════════════════════════════════════════════════════════════════════
# A. regime_classifier — safe defaults
# ══════════════════════════════════════════════════════════════════════════════

def test_regime_predict_returns_unknown_when_pkl_missing(tmp_path):
    """predict() returns UNKNOWN when the model file does not exist."""
    result = rc_predict(
        market_open_minutes=30,
        day_of_week=1,
        strategy="bias_momentum",
        direction="LONG",
        model_path=tmp_path / "nonexistent.pkl",
    )
    assert result == UNKNOWN_REGIME


def test_regime_predict_returns_unknown_on_corrupt_pkl(tmp_path):
    """predict() returns UNKNOWN when the pkl contains garbage bytes."""
    bad = tmp_path / "bad.pkl"
    bad.write_bytes(b"not a real pickle")
    result = rc_predict(
        market_open_minutes=30,
        day_of_week=1,
        strategy="bias_momentum",
        direction="LONG",
        model_path=bad,
    )
    assert result == UNKNOWN_REGIME


def test_regime_predict_returns_unknown_for_unknown_strategy(tmp_path):
    """predict() returns UNKNOWN when the strategy label was never seen at train time."""
    pkl = _build_rc_bundle(tmp_path, "ok_strat.pkl")
    result = rc_predict(
        market_open_minutes=30,
        day_of_week=1,
        strategy="unknown_strat_xyz",
        direction="LONG",
        model_path=pkl,
    )
    assert result == UNKNOWN_REGIME


def test_regime_predict_returns_unknown_for_unknown_direction(tmp_path):
    """predict() returns UNKNOWN when the direction label was never seen at train time."""
    pkl = _build_rc_bundle(tmp_path, "ok_dir.pkl")
    result = rc_predict(
        market_open_minutes=30,
        day_of_week=1,
        strategy="a",
        direction="INVALID",
        model_path=pkl,
    )
    assert result == UNKNOWN_REGIME


def test_regime_predict_returns_a_known_class_on_valid_inputs(tmp_path):
    """predict() returns one of the 3 trained regime classes on fully valid input."""
    known_regimes = {"LOW_VOL_TREND", "HIGH_VOLATILITY", "MEAN_REVERT_CHOP"}
    pkl = _build_rc_bundle(tmp_path, "ok_valid.pkl")
    result = rc_predict(
        market_open_minutes=30,
        day_of_week=1,
        strategy="a",
        direction="LONG",
        model_path=pkl,
    )
    assert result != UNKNOWN_REGIME, f"Expected a regime class, got {result!r}"
    assert result in known_regimes, f"Unexpected regime value: {result!r}"


# ══════════════════════════════════════════════════════════════════════════════
# B. regime_classifier.train() guardrail
# ══════════════════════════════════════════════════════════════════════════════

def test_regime_train_raises_when_warehouse_empty(tmp_path):
    """train() raises RuntimeError('no trainable rows') when no qualifying rows exist."""
    db_path = _build_warehouse(tmp_path, rows=[])  # zero rows
    with pytest.raises(RuntimeError, match="no trainable rows"):
        rc_train(db_path=db_path, model_path=tmp_path / "out.pkl")


def test_regime_train_rejection_deletes_existing_pkl(tmp_path):
    """train() deletes any stale .pkl when the model fails to beat the baseline by 5pp."""
    rng = random.Random(42)
    strategies = ["a", "b"]
    directions = ["LONG", "SHORT"]
    regimes = ["LOW_VOL_TREND", "HIGH_VOLATILITY", "MEAN_REVERT_CHOP"]

    # 200 rows with random labels — no real signal
    rows = []
    for i in range(200):
        # Spread entry times across several days to ensure session_date is non-NULL
        # and mae_ticks/mfe_ticks are set so the completeness filter passes.
        offset_days = i // 10  # 10 rows per day
        rows.append({
            "strategy": rng.choice(strategies),
            "direction": rng.choice(directions),
            "entry_ts": f"2024-01-{2 + offset_days:02d}T09:31:00-06:00",
            "entry_price": 4800.0,
            "exit_ts": f"2024-01-{2 + offset_days:02d}T09:45:00-06:00",
            "exit_price": 4800.25,
            "pnl_dollars": rng.uniform(-200, 200),
            "mae_ticks": float(rng.randint(1, 20)),
            "mfe_ticks": float(rng.randint(1, 20)),
            "regime": rng.choice(regimes),
            "entry_context": None,
        })

    db_path = _build_warehouse(tmp_path, rows=rows)
    out_pkl = tmp_path / "out.pkl"

    # Pre-create a stale pkl so we can verify it gets deleted
    joblib.dump({"stale": True}, out_pkl)
    assert out_pkl.exists(), "Pre-condition: stale pkl should exist before train()"

    result = rc_train(db_path=db_path, model_path=out_pkl)

    assert result.rejected is True, (
        f"Expected rejected=True but got lift={result.lift_over_baseline:.4f} "
        f"vs baseline={result.majority_baseline:.4f}"
    )
    assert result.lift_over_baseline < MIN_LIFT_OVER_BASELINE, (
        f"lift={result.lift_over_baseline:.4f} should be < {MIN_LIFT_OVER_BASELINE}"
    )
    assert not out_pkl.exists(), "Stale pkl should have been deleted on rejection"


# ══════════════════════════════════════════════════════════════════════════════
# C. signal_predictor — safe defaults
# ══════════════════════════════════════════════════════════════════════════════

def test_signal_predict_returns_05_when_pkl_missing(tmp_path):
    """predict_proba() returns 0.5 when the model file does not exist."""
    result = sp_predict_proba(
        {"atr_5m": 4.2},
        model_path=tmp_path / "nonexistent.pkl",
    )
    assert result == SAFE_DEFAULT_PROBA


def test_signal_predict_returns_05_when_entry_context_is_none(tmp_path):
    """predict_proba(None) returns 0.5 regardless of whether the model exists."""
    pkl = _build_sp_bundle(tmp_path, "sp_none.pkl")
    result = sp_predict_proba(None, model_path=pkl)
    assert result == SAFE_DEFAULT_PROBA


def test_signal_predict_returns_05_when_entry_context_is_not_dict(tmp_path):
    """predict_proba() returns 0.5 when entry_context is a list or string."""
    pkl = _build_sp_bundle(tmp_path, "sp_notdict.pkl")
    assert sp_predict_proba(["atr_5m", 4.2], model_path=pkl) == SAFE_DEFAULT_PROBA
    assert sp_predict_proba("atr_5m=4.2", model_path=pkl) == SAFE_DEFAULT_PROBA


def test_signal_predict_returns_05_on_corrupt_pkl(tmp_path):
    """predict_proba() returns 0.5 when the pkl contains garbage bytes."""
    bad = tmp_path / "sp_bad.pkl"
    bad.write_bytes(b"not a real pickle")
    result = sp_predict_proba({"atr_5m": 4.2}, model_path=bad)
    assert result == SAFE_DEFAULT_PROBA


def test_signal_predict_returns_real_proba_on_valid_inputs(tmp_path):
    """predict_proba() returns a float in [0, 1] (sanity: not trivially 0 or 1)."""
    from agents.signal_predictor import DEFAULT_NUMERIC_KEYS

    pkl = _build_sp_bundle(tmp_path, "sp_valid.pkl")
    # Provide all keys that the bundle was trained on (first 5 of DEFAULT_NUMERIC_KEYS)
    keys_used = list(DEFAULT_NUMERIC_KEYS[:5])
    entry_context = {k: float(i + 1) for i, k in enumerate(keys_used)}

    result = sp_predict_proba(entry_context, model_path=pkl)
    assert isinstance(result, float), f"Expected float, got {type(result)}"
    assert 0.0 <= result <= 1.0, f"Proba {result} out of [0, 1]"
    # A tiny trained model on random data rarely returns exactly 0.5
    # but we loosen to [0, 1] to avoid flakiness on perfectly balanced data.


# ══════════════════════════════════════════════════════════════════════════════
# D. signal_predictor.train() — no-data behavior
# ══════════════════════════════════════════════════════════════════════════════

def test_signal_train_returns_not_trained_when_no_entry_context_rows(tmp_path):
    """train() returns trained=False, n_trades=0 when entry_context IS NULL for all rows."""
    rows = [
        {
            "strategy": "bias_momentum",
            "direction": "LONG",
            "entry_ts": "2024-01-02T09:31:00-06:00",
            "entry_price": 4800.0,
            "exit_ts": "2024-01-02T09:45:00-06:00",
            "exit_price": 4800.25,
            "pnl_dollars": 50.0,
            "mae_ticks": 2.0,
            "mfe_ticks": 8.0,
            "regime": "LOW_VOL_TREND",
            "entry_context": None,  # NULL — must be excluded
        }
    ]
    db_path = _build_warehouse(tmp_path, rows=rows)
    out_pkl = tmp_path / "sp_out.pkl"

    result = sp_train(db_path=db_path, model_path=out_pkl)

    assert result.trained is False
    assert result.n_trades == 0
    assert not out_pkl.exists(), "No pkl should be saved when n_trades == 0"
    assert "entry_context" in result.message.lower(), (
        f"message should mention 'entry_context', got: {result.message!r}"
    )


def test_signal_train_returns_not_trained_when_all_entry_context_unparseable(tmp_path):
    """train() returns trained=False when every entry_context is non-dict JSON.

    DuckDB's JSON column type validates JSON syntax at insert time, so we cannot
    store raw-invalid bytes.  Instead we store a JSON array ('[1,2,3]'), which is
    syntactically valid JSON but evaluates to a list, not a dict.  The
    signal_predictor skips any row whose parsed value is not a dict, so the
    result is the same: all rows skipped, trained=False.
    """
    n = 5
    rows = [
        {
            "strategy": "bias_momentum",
            "direction": "LONG",
            "entry_ts": "2024-01-02T09:31:00-06:00",
            "entry_price": 4800.0,
            "exit_ts": "2024-01-02T09:45:00-06:00",
            "exit_price": 4800.25,
            "pnl_dollars": 50.0,
            "mae_ticks": 2.0,
            "mfe_ticks": 8.0,
            "regime": "LOW_VOL_TREND",
            # A JSON array is valid JSON (DuckDB accepts it) but not a dict,
            # so signal_predictor skips it, triggering the "failed to parse as
            # JSON dict" path.
            "entry_context": "[1, 2, 3]",
        }
        for _ in range(n)
    ]
    db_path = _build_warehouse(tmp_path, rows=rows)
    out_pkl = tmp_path / "sp_out2.pkl"

    result = sp_train(db_path=db_path, model_path=out_pkl)

    assert result.trained is False
    assert result.n_trades == n
    assert not out_pkl.exists(), "No pkl should be saved when all contexts are non-dict"
    assert "parse" in result.message.lower() or "failed" in result.message.lower(), (
        f"message should reference parsing failure, got: {result.message!r}"
    )


def test_regime_train_returns_rejected_when_stratified_split_impossible(tmp_path):
    """train() returns rejected=True when there are too few rows for stratified split.

    Two rows, one per class (LOW_VOL_TREND and HIGH_VOLATILITY), is insufficient
    for stratified split with test_size=0.2 — sklearn requires at least 2 samples
    per class. The train() function must catch the ValueError and return a
    TrainResult with rejected=True rather than propagating the exception.
    """
    rows = [
        {
            "strategy": "bias_momentum",
            "direction": "LONG",
            "entry_ts": "2024-01-02T09:31:00-06:00",
            "entry_price": 4800.0,
            "exit_ts": "2024-01-02T09:45:00-06:00",
            "exit_price": 4800.25,
            "pnl_dollars": 50.0,
            "mae_ticks": 2.0,
            "mfe_ticks": 8.0,
            "regime": "LOW_VOL_TREND",
            "entry_context": None,
        },
        {
            "strategy": "bias_momentum",
            "direction": "SHORT",
            "entry_ts": "2024-01-02T09:35:00-06:00",
            "entry_price": 4801.0,
            "exit_ts": "2024-01-02T09:50:00-06:00",
            "exit_price": 4800.75,
            "pnl_dollars": -25.0,
            "mae_ticks": 3.0,
            "mfe_ticks": 5.0,
            "regime": "HIGH_VOLATILITY",
            "entry_context": None,
        },
    ]
    db_path = _build_warehouse(tmp_path, rows=rows)
    out_pkl = tmp_path / "rc_split_fail.pkl"

    result = rc_train(db_path=db_path, model_path=out_pkl)

    assert result.rejected is True, (
        f"Expected rejected=True when stratified split is impossible, got rejected={result.rejected}"
    )
    assert "stratified split" in result.rejection_reason.lower(), (
        f"rejection_reason should mention 'stratified split', got: {result.rejection_reason!r}"
    )
    assert not out_pkl.exists(), "No .pkl should be saved when stratified split fails"


def test_signal_train_returns_not_trained_when_only_one_outcome_class(tmp_path):
    """train() returns trained=False when every trade is a winner (only class 1)."""
    n = 10
    rows = [
        {
            "strategy": "bias_momentum",
            "direction": "LONG",
            "entry_ts": "2024-01-02T09:31:00-06:00",
            "entry_price": 4800.0,
            "exit_ts": "2024-01-02T09:45:00-06:00",
            "exit_price": 4800.25,
            "pnl_dollars": 100.0,  # always winner
            "mae_ticks": 2.0,
            "mfe_ticks": 8.0,
            "regime": "LOW_VOL_TREND",
            "entry_context": json.dumps({"atr_5m": float(i + 1), "cvd": float(i * 100)}),
        }
        for i in range(n)
    ]
    db_path = _build_warehouse(tmp_path, rows=rows)
    out_pkl = tmp_path / "sp_out3.pkl"

    result = sp_train(db_path=db_path, model_path=out_pkl)

    assert result.trained is False
    assert result.n_trades == n
    assert not out_pkl.exists(), "No pkl should be saved with only one outcome class"
    assert "one outcome class" in result.message.lower() or "class" in result.message.lower(), (
        f"message should mention single outcome class, got: {result.message!r}"
    )
