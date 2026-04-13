"""
Phoenix Bot — HMM Market Regime Detector

Volatility-based regime detection orthogonal to session_manager's time-based
regimes. Uses a 3-state Gaussian HMM (TRENDING / RANGING / VOLATILE) trained
incrementally on rolling 5-min bar features. Labels assigned by sorting HMM
states on their mean volatility after each fit.
"""
import logging
from collections import deque
from typing import Optional
import numpy as np

logger = logging.getLogger("HMMRegime")

try:
    from hmmlearn.hmm import GaussianHMM
    HAS_HMM = True
except ImportError:
    HAS_HMM = False
    logger.warning("hmmlearn not installed — HMM regime detection disabled")

try:
    import ruptures
    HAS_RUPTURES = True
except ImportError:
    HAS_RUPTURES = False
    logger.warning("ruptures not installed — change-point detection disabled")

REGIME_NAMES = {0: "TRENDING", 1: "RANGING", 2: "VOLATILE"}
_MAX_HISTORY, _RETRAIN_INTERVAL = 500, 50

_REGIME_PARAMS = {
    "TRENDING":  {"target_multiplier": 1.4,  "stop_multiplier": 1.0,
                  "min_confluence_adj": 0.0,  "size_multiplier": 1.0},
    "RANGING":   {"target_multiplier": 1.0,  "stop_multiplier": 0.85,
                  "min_confluence_adj": 0.3,  "size_multiplier": 0.7},
    "VOLATILE":  {"target_multiplier": 1.5,  "stop_multiplier": 1.2,
                  "min_confluence_adj": -0.5, "size_multiplier": 0.5},
}


class HMMRegimeDetector:
    """Incremental HMM-based market regime detector for MNQ 5-min bars."""

    def __init__(self, n_regimes: int = 3, warmup_bars: int = 50):
        self.n_regimes, self.warmup_bars = n_regimes, warmup_bars
        self._closes: deque = deque(maxlen=_MAX_HISTORY)
        self._volumes: deque = deque(maxlen=_MAX_HISTORY)
        self._bars_seen = self._bars_since_train = 0
        self._current_regime, self._regime_id = "RANGING", 1
        self._confidence = 0.0
        self._probabilities = [0.0, 0.0, 0.0]
        self._bars_in_regime = 0
        self._change_point = False
        self._state_map = {0: 0, 1: 1, 2: 2}
        self._model: Optional[object] = None
        self._warmed_up = False

    def _build_features(self) -> Optional[np.ndarray]:
        """Features: [20-bar mean log return, 20-bar rolling vol, volume change rate]."""
        closes = np.array(self._closes, dtype=np.float64)
        volumes = np.array(self._volumes, dtype=np.float64)
        if len(closes) < 21:
            return None
        log_ret = np.diff(np.log(closes))
        feat_ret = np.convolve(log_ret, np.ones(20) / 20, mode="valid")
        feat_vol = np.array([log_ret[i:i+20].std() for i in range(len(log_ret) - 19)])
        vol_slice, vol_prev = volumes[20:], volumes[19:-1]
        safe_prev = np.where(vol_prev == 0, 1.0, vol_prev)
        feat_vchg = np.clip((vol_slice - vol_prev) / safe_prev, -3.0, 3.0)
        min_len = min(len(feat_ret), len(feat_vol), len(feat_vchg))
        if min_len < 10:
            return None
        return np.column_stack([feat_ret[-min_len:], feat_vol[-min_len:], feat_vchg[-min_len:]])

    def _fit(self, X: np.ndarray) -> bool:
        if not HAS_HMM:
            return False
        try:
            model = GaussianHMM(n_components=self.n_regimes, covariance_type="diag",
                                n_iter=30, random_state=42, verbose=False)
            model.fit(X)
            self._model = model
            # Label states: sort by volatility mean (col 1) ascending
            order = np.argsort(model.means_[:, 1])
            self._state_map = {int(order[0]): 1, int(order[1]): 0, int(order[2]): 2}
            self._bars_since_train = 0
            logger.info("HMM retrained on %d samples", len(X))
            return True
        except Exception as e:
            logger.error("HMM fit failed: %s", e)
            return False

    def _detect_change_point(self, X: np.ndarray) -> bool:
        if not HAS_RUPTURES or len(X) < 30:
            return False
        try:
            bkps = ruptures.Pelt(model="rbf", min_size=10, jump=5).fit(X).predict(pen=3.0)
            return any(len(X) - 3 <= bp <= len(X) for bp in bkps[:-1])
        except Exception:
            return False

    def update(self, bar) -> dict:
        """Called on every 5-min bar. Returns regime state dict."""
        self._closes.append(float(bar.close))
        self._volumes.append(float(bar.volume))
        self._bars_seen += 1
        self._bars_since_train += 1

        if self._bars_seen < self.warmup_bars:
            self._bars_in_regime += 1
            return self._result()

        X = self._build_features()
        if X is None:
            self._bars_in_regime += 1
            return self._result()

        if self._model is None or self._bars_since_train >= _RETRAIN_INTERVAL:
            self._fit(X)
        if self._model is None:
            self._bars_in_regime += 1
            return self._result()

        self._warmed_up = True
        try:
            probs = self._model.predict_proba(X)
            last_probs = probs[-1]
            raw_state = int(np.argmax(last_probs))
            label_id = self._state_map.get(raw_state, 1)
            regime = REGIME_NAMES[label_id]
            confidence = float(last_probs[raw_state])
            inv_map = {v: k for k, v in self._state_map.items()}
            ordered = [float(last_probs[inv_map.get(i, 0)]) for i in range(3)]
            cp = self._detect_change_point(X)
            if regime != self._current_regime:
                logger.info("Regime shift: %s -> %s (conf=%.2f)",
                            self._current_regime, regime, confidence)
                self._bars_in_regime = 1
            else:
                self._bars_in_regime += 1
            self._current_regime = regime
            self._regime_id = label_id
            self._confidence = confidence
            self._probabilities = ordered
            self._change_point = cp
        except Exception as e:
            logger.error("HMM predict failed: %s", e)
            self._bars_in_regime += 1
        return self._result()

    def _result(self) -> dict:
        return {
            "regime": self._current_regime,
            "confidence": round(self._confidence, 3),
            "regime_id": self._regime_id,
            "probabilities": [round(p, 3) for p in self._probabilities],
            "change_point": self._change_point,
            "bars_in_regime": self._bars_in_regime,
            "regime_params": _REGIME_PARAMS[self._current_regime].copy(),
        }

    def get_state(self) -> dict:
        """For dashboard display."""
        return {**self._result(), "warmed_up": self._warmed_up,
                "bars_seen": self._bars_seen, "warmup_bars": self.warmup_bars}

    def to_dict(self) -> dict:
        """For bot state push to dashboard."""
        return self.get_state()
