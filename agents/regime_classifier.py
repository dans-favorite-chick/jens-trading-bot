#!/usr/bin/env python3
"""
agents/regime_classifier.py - Phase 4 ML model: market-regime classifier.

Trains an XGBoost multi-class classifier on the warehouse trades table to
predict the per-trade ``regime`` label (``LOW_VOL_TREND`` / ``MEAN_REVERT_CHOP``
/ ``HIGH_VOLATILITY``) from features available at signal time.

DESIGN
------
The spec mandates training only on trades with ``session_date``,
``market_open_minutes``, ``mae_ticks``, and ``mfe_ticks`` present. MAE/MFE are
post-trade values, so they cannot serve as inference-time features. We
interpret the rule as a **completeness filter** for the training set
(guarantees the rows have meaningful regime labels and aren't legacy
placeholders) and use only inference-time-knowable inputs as features:

  - market_open_minutes  (numeric, can be negative for Globex)
  - day_of_week          (int 0-6, Monday=0, derived from session_date)
  - strategy             (categorical, label-encoded)
  - direction            (categorical, label-encoded: LONG/SHORT)

USAGE
-----
    # Train (rebuilds the .pkl from current warehouse data)
    python agents/regime_classifier.py train

    # Predict from CLI (smoke check)
    python agents/regime_classifier.py predict \\
        --market-open-minutes 30 --day-of-week 1 \\
        --strategy bias_momentum --direction LONG

    # Programmatic
    from agents.regime_classifier import predict
    regime = predict(market_open_minutes=30, day_of_week=1,
                     strategy="bias_momentum", direction="LONG")

SAFE-DEFAULTS CONTRACT
----------------------
``predict(...)`` returns ``"UNKNOWN"`` on any failure (model missing,
deserialization error, schema mismatch, exception in inference) so that
consumers can call it inline without try/except and never block a trade.

GUARDRAIL: ``train()`` refuses to save the .pkl when test accuracy fails to
exceed the majority-class baseline by at least MIN_LIFT_OVER_BASELINE
(default 5 percentage points). The .pkl is deleted if a previous accepted
model is on disk. predict() then returns UNKNOWN until a future training run
produces a model with real signal.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("agents.regime_classifier")

WAREHOUSE_DB = ROOT / "data" / "warehouse" / "phoenix.duckdb"
MODEL_PATH   = ROOT / "models" / "regime_classifier.pkl"

UNKNOWN_REGIME = "UNKNOWN"
FEATURES       = ("market_open_minutes", "day_of_week", "strategy", "direction")

MIN_LIFT_OVER_BASELINE = 0.05   # 5 percentage points
assert MIN_LIFT_OVER_BASELINE > 0, (
    "MIN_LIFT_OVER_BASELINE must be positive; a zero or negative value "
    "would silently accept any model that does not beat the majority-class baseline."
)


@dataclass
class TrainResult:
    n_rows: int
    n_train: int
    n_test: int
    accuracy: float
    classes: list[str]
    per_class_support: dict[str, int]
    model_path: Path
    # New (added 2026-05-31): guardrail outcome
    majority_baseline: float = 0.0
    lift_over_baseline: float = 0.0
    rejected: bool = False
    rejection_reason: str = ""


def _load_training_frame(db_path: Path):
    """Pull the training frame from the warehouse. Returns pandas.DataFrame."""
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        sql = """
            SELECT
                market_open_minutes,
                EXTRACT(DOW FROM session_date)::INTEGER AS day_of_week,
                strategy,
                direction,
                regime
            FROM trades_ct
            WHERE session_date        IS NOT NULL
              AND market_open_minutes IS NOT NULL
              AND mae_ticks           IS NOT NULL
              AND mfe_ticks           IS NOT NULL
              AND regime              IS NOT NULL
              AND regime              <> 'UNKNOWN'
        """
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def train(*, db_path: Optional[Path] = None,
          model_path: Optional[Path] = None,
          random_state: int = 42) -> TrainResult:
    """Fit XGBoost on the warehouse, persist a joblib bundle. Returns report."""
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import LabelEncoder
    from sklearn.metrics import accuracy_score, classification_report
    from xgboost import XGBClassifier

    db_path    = db_path    or WAREHOUSE_DB
    model_path = model_path or MODEL_PATH
    model_path.parent.mkdir(parents=True, exist_ok=True)

    df = _load_training_frame(db_path)
    if df.empty:
        raise RuntimeError(
            f"no trainable rows in warehouse at {db_path}. The trades table "
            f"either is empty or has no rows passing the completeness filter "
            f"(session_date + market_open_minutes + mae_ticks + mfe_ticks + regime all NOT NULL)."
        )

    # Encode categoricals with fitted LabelEncoders; persist them in the bundle
    # so inference can encode unseen-at-fit-time values to a sentinel slot
    # rather than raising.
    le_strat = LabelEncoder().fit(df["strategy"].astype(str))
    le_dir   = LabelEncoder().fit(df["direction"].astype(str))
    le_regime = LabelEncoder().fit(df["regime"].astype(str))

    X = pd.DataFrame({
        "market_open_minutes": df["market_open_minutes"].astype(float),
        "day_of_week":         df["day_of_week"].astype(int),
        "strategy":            le_strat.transform(df["strategy"].astype(str)),
        "direction":           le_dir.transform(df["direction"].astype(str)),
    })
    y = le_regime.transform(df["regime"].astype(str))

    classes = [str(c) for c in le_regime.classes_]
    support = {str(c): int((df["regime"] == c).sum()) for c in classes}

    try:
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=0.2, random_state=random_state, stratify=y,
        )
    except ValueError as exc:
        model_path.unlink(missing_ok=True)
        rejection_reason = f"insufficient samples for stratified split: {exc}"
        print(
            f"[regime_classifier] REJECTED: {rejection_reason}; no .pkl saved.\n"
            f"                              predict() will return UNKNOWN (safe default)."
        )
        return TrainResult(
            n_rows=len(df),
            n_train=0,
            n_test=0,
            accuracy=0.0,
            classes=classes,
            per_class_support=support,
            model_path=model_path,
            majority_baseline=0.0,
            lift_over_baseline=0.0,
            rejected=True,
            rejection_reason=rejection_reason,
        )

    clf = XGBClassifier(
        n_estimators=200,
        max_depth=6,
        learning_rate=0.1,
        objective="multi:softprob",
        num_class=len(le_regime.classes_),
        random_state=random_state,
        n_jobs=-1,
        eval_metric="mlogloss",
    )
    clf.fit(X_tr, y_tr)

    y_pred = clf.predict(X_te)
    acc = float(accuracy_score(y_te, y_pred))

    # ── Guardrail: compare against majority-class baseline ─────────────────
    counts = np.bincount(y_te)
    majority_baseline = float(counts.max()) / float(len(y_te))
    lift = acc - majority_baseline
    model_has_signal = lift >= MIN_LIFT_OVER_BASELINE

    # Always print the classification report so the operator can see what happened.
    print(classification_report(
        y_te, y_pred, target_names=classes, digits=4, zero_division=0,
    ))

    if not model_has_signal:
        # Delete any stale .pkl so production never silently uses an old model.
        model_path.unlink(missing_ok=True)
        rejection_reason = (
            f"test accuracy {acc:.4f} did not beat majority baseline "
            f"{majority_baseline:.4f} by {MIN_LIFT_OVER_BASELINE * 100:.0f}pp "
            f"(lift={lift:+.4f})"
        )
        print(
            f"[regime_classifier] REJECTED: test accuracy {acc:.4f} did not beat majority baseline\n"
            f"                              {majority_baseline:.4f} by {MIN_LIFT_OVER_BASELINE * 100:.0f}pp\n"
            f"                              (lift={lift:+.4f}); no .pkl saved.\n"
            f"                              predict() will return UNKNOWN (safe default)."
        )
        return TrainResult(
            n_rows=len(df),
            n_train=len(X_tr),
            n_test=len(X_te),
            accuracy=acc,
            classes=classes,
            per_class_support=support,
            model_path=model_path,
            majority_baseline=majority_baseline,
            lift_over_baseline=lift,
            rejected=True,
            rejection_reason=rejection_reason,
        )

    # Model has real signal — save it.
    bundle = {
        "version":      1,
        "model":        clf,
        "le_strategy":  le_strat,
        "le_direction": le_dir,
        "le_regime":    le_regime,
        "feature_order": ["market_open_minutes", "day_of_week", "strategy", "direction"],
    }
    joblib.dump(bundle, model_path)

    print(
        f"[regime_classifier] trained on {len(df):,} rows | test accuracy = {acc:.4f}\n"
        f"                   (baseline {majority_baseline:.4f}, lift {lift:+.4f}) — accepted"
    )
    print(f"[regime_classifier] saved bundle -> {model_path}")
    return TrainResult(
        n_rows=len(df),
        n_train=len(X_tr),
        n_test=len(X_te),
        accuracy=acc,
        classes=classes,
        per_class_support=support,
        model_path=model_path,
        majority_baseline=majority_baseline,
        lift_over_baseline=lift,
        rejected=False,
        rejection_reason="",
    )


_MODEL_CACHE = {"path": None, "bundle": None}


def _load_bundle(model_path: Path):
    """Cache the loaded bundle per-process so inference is hot after first call."""
    if _MODEL_CACHE["path"] == model_path and _MODEL_CACHE["bundle"] is not None:
        return _MODEL_CACHE["bundle"]
    try:
        import joblib
        bundle = joblib.load(model_path)
    except Exception as exc:
        logger.warning("failed to load regime_classifier bundle at %s: %s",
                       model_path, exc)
        return None
    _MODEL_CACHE["path"]   = model_path
    _MODEL_CACHE["bundle"] = bundle
    return bundle


def _safe_label_encode(encoder, value: str) -> int:
    """Return the encoded label, or -1 if `value` is unknown to the encoder."""
    classes = list(encoder.classes_)
    try:
        return classes.index(str(value))
    except ValueError:
        return -1


def predict(*, market_open_minutes: float, day_of_week: int,
            strategy: str, direction: str,
            model_path: Optional[Path] = None) -> str:
    """Predict regime label. Returns ``UNKNOWN`` on any failure (safe default)."""
    model_path = model_path or MODEL_PATH
    if not model_path.exists():
        return UNKNOWN_REGIME
    bundle = _load_bundle(model_path)
    if bundle is None:
        return UNKNOWN_REGIME
    try:
        import pandas as pd
        strat_enc = _safe_label_encode(bundle["le_strategy"],  strategy)
        dir_enc   = _safe_label_encode(bundle["le_direction"], direction)
        if strat_enc < 0 or dir_enc < 0:
            # Unknown categorical value; the model wasn't trained on this.
            return UNKNOWN_REGIME
        X = pd.DataFrame([{
            "market_open_minutes": float(market_open_minutes),
            "day_of_week":         int(day_of_week),
            "strategy":            strat_enc,
            "direction":           dir_enc,
        }])[list(bundle["feature_order"])]
        pred_idx = int(bundle["model"].predict(X)[0])
        return str(bundle["le_regime"].classes_[pred_idx])
    except Exception as exc:
        logger.warning("regime_classifier.predict failed: %s", exc)
        return UNKNOWN_REGIME


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agents.regime_classifier",
        description="Phase 4 ML model: market-regime classifier.",
    )
    sub = ap.add_subparsers(dest="cmd")

    p_train = sub.add_parser("train", help="Train and persist the model")
    p_train.add_argument("--db", default=None, help="Override warehouse DB path")
    p_train.add_argument("--out", default=None, help="Override model output path")

    p_pred = sub.add_parser("predict", help="Predict from CLI (smoke test)")
    p_pred.add_argument("--market-open-minutes", type=float, required=True)
    p_pred.add_argument("--day-of-week",         type=int,   required=True)
    p_pred.add_argument("--strategy",            required=True)
    p_pred.add_argument("--direction",           required=True, choices=["LONG", "SHORT"])
    p_pred.add_argument("--model", default=None, help="Override model path")

    args = ap.parse_args(argv)
    if args.cmd == "train":
        train(
            db_path=Path(args.db) if args.db else None,
            model_path=Path(args.out) if args.out else None,
        )
        return 0
    if args.cmd == "predict":
        result = predict(
            market_open_minutes=args.market_open_minutes,
            day_of_week=args.day_of_week,
            strategy=args.strategy,
            direction=args.direction,
            model_path=Path(args.model) if args.model else None,
        )
        print(result)
        return 0
    ap.print_help()
    return 0


__all__ = [
    "predict",
    "train",
    "TrainResult",
    "UNKNOWN_REGIME",
    "MODEL_PATH",
    "FEATURES",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
