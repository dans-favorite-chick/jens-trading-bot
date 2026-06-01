#!/usr/bin/env python3
"""
agents/signal_predictor.py - Phase 4 ML model: signal-quality predictor.

XGBoost binary classifier that estimates the probability of a profitable trade
(``pnl_dollars > 0``) from the strategy's ``entry_context`` JSON.

SPEC RULES
----------
- Train only on warehouse rows with ``entry_context IS NOT NULL``.
- Return safe default ``0.5`` on any inference failure — never block a signal.
- ``agents/pretrade_filter.py`` must be able to ``import`` this module
  without modification.

DATA STATE (2026-05-31)
-----------------------
As of this writing, the warehouse has **zero** trades with ``entry_context``
populated (neither the portfolio_framework runs nor the Phase 2 backtest
engine emits it). ``train()`` recognizes this case, prints a clear message,
and returns ``{"trained": False, "n_trades": 0}`` without raising. When a
future writer starts emitting ``entry_context``, re-run ``train`` to pick up
the rows automatically.

USAGE
-----
    # Train (no-op until entry_context rows exist)
    python agents/signal_predictor.py train

    # Predict from CLI (smoke check)
    python agents/signal_predictor.py predict \\
        --entry-context-json '{"atr_5m": 4.2, "cvd": 1500, "vwap_dist": 8}'

    # Programmatic
    from agents.signal_predictor import predict_proba
    p_win = predict_proba({"atr_5m": 4.2, "cvd": 1500, ...})

SAFE-DEFAULTS CONTRACT
----------------------
``predict_proba(entry_context)`` returns ``0.5`` whenever:
  - The .pkl bundle is missing or unloadable,
  - ``entry_context`` is None / not a dict,
  - Inference raises for any reason.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("agents.signal_predictor")

WAREHOUSE_DB = ROOT / "data" / "warehouse" / "phoenix.duckdb"
MODEL_PATH   = ROOT / "models" / "signal_predictor.pkl"

SAFE_DEFAULT_PROBA = 0.5
# Feature keys read from each entry_context dict at training time. Numeric
# values only; anything missing / non-numeric is encoded as NaN (XGBoost
# tolerates NaN natively).
#
# Engine-authoritative — kept in sync with agents/backtest_engine.py:ENTRY_CONTEXT_KEYS
# plus identifying scalar fields (entry_score, stop_ticks, target_rr,
# market_open_minutes). If you add a key to one, mirror it in the other (and
# re-train the model).
DEFAULT_NUMERIC_KEYS = (
    # Volatility
    "atr_1m", "atr_5m", "atr_15m",
    # Order flow
    "cvd", "cvd_session", "bar_delta",
    # VWAP family
    "vwap", "vwap_std", "vwap_upper1", "vwap_lower1",
    # Moving averages
    "ema5", "ema9", "ema21", "ema9_15m", "ema21_15m",
    # Volume
    "vol_climax_ratio", "avg_vol_5m",
    # Multi-TF vote counts
    "tf_votes_bullish", "tf_votes_bearish",
    # DOM
    "dom_imbalance", "dom_bid_stack", "dom_ask_stack",
    # Momentum
    "rsi", "macd_histogram", "macd_line", "macd_signal",
    # Signal-time context
    "market_open_minutes", "entry_score", "stop_ticks", "target_rr",
)


@dataclass
class TrainResult:
    trained: bool
    n_trades: int
    n_train: int = 0
    n_test:  int = 0
    accuracy: float = 0.0
    log_loss: float = 0.0
    feature_keys: list[str] = field(default_factory=list)
    model_path: Optional[Path] = None
    message: str = ""


def _extract_features(ctx: dict, keys) -> list[float]:
    """Pull `keys` from `ctx`, coercing to float. Missing / non-numeric -> NaN."""
    import math
    out: list[float] = []
    for k in keys:
        v = ctx.get(k) if isinstance(ctx, dict) else None
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                f = float("nan")
        except (TypeError, ValueError):
            f = float("nan")
        out.append(f)
    return out


def _load_training_frame(db_path: Path):
    """Pull (entry_context json text, label) from the warehouse."""
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("""
            SELECT CAST(entry_context AS VARCHAR) AS ctx_text,
                   (pnl_dollars > 0)::INTEGER     AS y
            FROM trades_ct
            WHERE entry_context IS NOT NULL
              AND pnl_dollars   IS NOT NULL
        """).fetchall()
    finally:
        con.close()
    return rows


def train(*, db_path: Optional[Path] = None,
          model_path: Optional[Path] = None,
          feature_keys=DEFAULT_NUMERIC_KEYS,
          random_state: int = 42) -> TrainResult:
    """Fit XGBoost binary classifier on entry_context features.

    Returns a TrainResult. If the warehouse has zero qualifying rows, the
    result has ``trained=False`` and ``n_trades=0`` (no exception raised).
    """
    db_path    = db_path    or WAREHOUSE_DB
    model_path = model_path or MODEL_PATH

    rows = _load_training_frame(db_path)
    if not rows:
        msg = (
            "no warehouse trades have entry_context populated; "
            "training is a no-op until a writer (backtest engine or live bot) "
            "starts emitting entry_context JSON. "
            "predict_proba() will continue to return the safe default 0.5."
        )
        print(f"[signal_predictor] {msg}")
        return TrainResult(trained=False, n_trades=0, message=msg)

    # Materialize features
    import numpy as np
    import pandas as pd
    keys = list(feature_keys)
    X_list: list[list[float]] = []
    y_list: list[int] = []
    skipped = 0
    for ctx_text, y in rows:
        try:
            ctx = json.loads(ctx_text) if ctx_text else {}
        except Exception:
            skipped += 1
            continue
        if not isinstance(ctx, dict):
            skipped += 1
            continue
        X_list.append(_extract_features(ctx, keys))
        y_list.append(int(y))
    if not X_list:
        msg = (
            f"all {len(rows)} entry_context cells failed to parse as JSON dict; "
            f"training skipped (predict_proba returns 0.5)."
        )
        print(f"[signal_predictor] {msg}")
        return TrainResult(trained=False, n_trades=len(rows), message=msg)

    X = pd.DataFrame(X_list, columns=keys)
    y = pd.Series(y_list, name="win")

    if y.nunique() < 2:
        msg = (
            f"only one outcome class present in {len(y)} rows "
            f"(value={y.iloc[0]}); cannot fit a binary classifier."
        )
        print(f"[signal_predictor] {msg}")
        return TrainResult(trained=False, n_trades=len(rows), message=msg)

    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, log_loss, classification_report
    from xgboost import XGBClassifier

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.2, random_state=random_state, stratify=y,
    )

    clf = XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        objective="binary:logistic",
        random_state=random_state,
        n_jobs=-1,
        eval_metric="logloss",
    )
    clf.fit(X_tr, y_tr)

    y_pred = clf.predict(X_te)
    y_proba = clf.predict_proba(X_te)[:, 1]
    acc = float(accuracy_score(y_te, y_pred))
    ll  = float(log_loss(y_te, y_proba))

    import joblib
    model_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "version":      1,
        "model":        clf,
        "feature_keys": keys,
    }
    joblib.dump(bundle, model_path)

    print(f"[signal_predictor] trained on {len(y):,} rows "
          f"(skipped {skipped} unparseable) | test accuracy={acc:.4f} "
          f"log_loss={ll:.4f}")
    print(classification_report(y_te, y_pred, target_names=["loss", "win"],
                                digits=4, zero_division=0))
    print(f"[signal_predictor] saved bundle -> {model_path}")

    return TrainResult(
        trained=True,
        n_trades=len(y),
        n_train=len(X_tr),
        n_test=len(X_te),
        accuracy=acc,
        log_loss=ll,
        feature_keys=keys,
        model_path=model_path,
        message="ok",
    )


_MODEL_CACHE: dict[str, Any] = {"path": None, "bundle": None}


def _load_bundle(model_path: Path):
    if _MODEL_CACHE["path"] == model_path and _MODEL_CACHE["bundle"] is not None:
        return _MODEL_CACHE["bundle"]
    try:
        import joblib
        bundle = joblib.load(model_path)
    except Exception as exc:
        logger.warning("failed to load signal_predictor bundle at %s: %s",
                       model_path, exc)
        return None
    _MODEL_CACHE["path"]   = model_path
    _MODEL_CACHE["bundle"] = bundle
    return bundle


def predict_proba(entry_context: dict,
                  *, model_path: Optional[Path] = None) -> float:
    """Return P(win) in [0, 1]. ``0.5`` (the safe default) on any failure.

    ``entry_context`` is the same JSON-able dict the strategy attaches to
    its ``Signal``. Missing keys are tolerated (treated as NaN by XGBoost).
    """
    model_path = model_path or MODEL_PATH
    if not model_path.exists():
        return SAFE_DEFAULT_PROBA
    if not isinstance(entry_context, dict):
        return SAFE_DEFAULT_PROBA
    bundle = _load_bundle(model_path)
    if bundle is None:
        return SAFE_DEFAULT_PROBA
    try:
        import pandas as pd
        keys = list(bundle["feature_keys"])
        X = pd.DataFrame([_extract_features(entry_context, keys)], columns=keys)
        proba = float(bundle["model"].predict_proba(X)[0, 1])
        # Guard against NaN / out-of-range due to corruption
        if not (0.0 <= proba <= 1.0):
            return SAFE_DEFAULT_PROBA
        return proba
    except Exception as exc:
        logger.warning("signal_predictor.predict_proba failed: %s", exc)
        return SAFE_DEFAULT_PROBA


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agents.signal_predictor",
        description="Phase 4 ML model: signal-quality predictor.",
    )
    sub = ap.add_subparsers(dest="cmd")

    p_train = sub.add_parser("train", help="Train and persist the model")
    p_train.add_argument("--db", default=None)
    p_train.add_argument("--out", default=None)

    p_pred = sub.add_parser("predict", help="Predict from CLI (smoke test)")
    p_pred.add_argument("--entry-context-json", required=True,
                        help='JSON dict, e.g. \'{"atr_5m": 4.2, "cvd": 1500}\'')
    p_pred.add_argument("--model", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "train":
        train(
            db_path=Path(args.db) if args.db else None,
            model_path=Path(args.out) if args.out else None,
        )
        return 0
    if args.cmd == "predict":
        try:
            ctx = json.loads(args.entry_context_json)
        except Exception as exc:
            print(f"ERROR: invalid JSON for --entry-context-json: {exc}",
                  file=sys.stderr)
            return 2
        p = predict_proba(ctx, model_path=Path(args.model) if args.model else None)
        print(f"{p:.4f}")
        return 0
    ap.print_help()
    return 0


__all__ = [
    "predict_proba",
    "train",
    "TrainResult",
    "SAFE_DEFAULT_PROBA",
    "MODEL_PATH",
    "DEFAULT_NUMERIC_KEYS",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
