"""
Phoenix Bot - FinBERT ONNX Sentiment Engine (Section 4)

Wraps the ProsusAI/finbert model exported to ONNX (optionally INT8-quantized)
for fast local inference on financial headlines. Uses onnxruntime directly
(skips the optimum pipeline overhead) and a Hugging Face tokenizer for
preprocessing.

Targets: p50 <= 10ms, p99 <= 50ms on a single short headline (CPU, 4 threads).

Lives behind feature flag DEFAULT_WEIGHT=0.0 in agents/sentiment_flow_agent.py
until the strategy is validated.

------------------------------------------------------------------------
How to download / build the real model (run inside the .venv-ml Python 3.12):

    pip install optimum[onnxruntime] transformers onnxruntime
    optimum-cli export onnx --model ProsusAI/finbert --task text-classification \\
        ./models/finbert_onnx
    optimum-cli onnxruntime quantize --avx512 --onnx_model ./models/finbert_onnx \\
        -o ./models/finbert_onnx_int8

Windows fallback if optimum-cli quantize is unavailable:

    from onnxruntime.quantization import quantize_dynamic, QuantType
    quantize_dynamic(
        model_input="models/finbert_onnx/model.onnx",
        model_output="models/finbert_onnx_int8/model.onnx",
        weight_type=QuantType.QInt8,
    )

Until the model files are on disk, this module operates in DEGRADED MODE:
every score() returns ("neutral", 0.5, 0.5, 0.0). A single WARN is logged
on the first degraded call, then suppressed.
------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import logging
import os
from functools import lru_cache
from typing import Optional

logger = logging.getLogger("FinBERTSentiment")

# Try to import the heavy ML deps. Missing deps trigger DEGRADED MODE,
# they do NOT raise on import. Tests use unittest.mock to swap these.
try:
    import onnxruntime as ort  # type: ignore
    _HAS_ORT = True
except Exception:  # pragma: no cover - exercised in degraded path
    ort = None  # type: ignore
    _HAS_ORT = False

try:
    from transformers import AutoTokenizer  # type: ignore
    _HAS_TOKENIZER = True
except Exception:  # pragma: no cover - exercised in degraded path
    AutoTokenizer = None  # type: ignore
    _HAS_TOKENIZER = False

try:
    import numpy as np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    np = None  # type: ignore
    _HAS_NUMPY = False


# ProsusAI/finbert label order (matches config.json id2label).
# NOTE: index 0 = positive, 1 = negative, 2 = neutral.
FINBERT_LABELS = ["positive", "negative", "neutral"]

# Default tuple returned in degraded mode.
# Order matches the public score() return: (label, p_neg, p_neu, p_pos).
_DEGRADED_RESULT: tuple[str, float, float, float] = ("neutral", 0.5, 0.5, 0.0)


def _sha1(text: str) -> str:
    """Stable SHA1 hex digest of the input text."""
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _softmax_py(logits: list[float]) -> list[float]:
    """Pure-Python softmax for the degraded path (no numpy required)."""
    m = max(logits)
    exps = [pow(2.71828182845904523536, x - m) for x in logits]
    s = sum(exps) or 1.0
    return [e / s for e in exps]


class FinBERTSentiment:
    """ONNX-backed FinBERT inference with a process-local LRU cache.

    If the model files or ML deps are missing the instance enters DEGRADED
    MODE: every score() / score_batch() call returns the neutral default
    ``("neutral", 0.5, 0.5, 0.0)`` without raising. A single WARN is logged
    on first degraded call, then suppressed.

    Parameters
    ----------
    onnx_path : str
        Path to model.onnx (or model_quantized.onnx). Can be the directory
        containing the model file or the file itself. Default is
        ``models/finbert_onnx_int8/model.onnx``.
    tokenizer_path : str
        Path to the tokenizer directory (typically same as onnx export dir).
    max_len : int
        Maximum tokenized sequence length. 64 is plenty for headlines.
    num_threads : int
        intra_op thread count for onnxruntime.
    """

    def __init__(
        self,
        onnx_path: str = "models/finbert_onnx_int8/model.onnx",
        tokenizer_path: str = "models/finbert_onnx_int8",
        max_len: int = 64,
        num_threads: int = 4,
    ) -> None:
        self.onnx_path: str = onnx_path
        self.tokenizer_path: str = tokenizer_path
        self.max_len: int = int(max_len)
        self.num_threads: int = int(num_threads)

        # Lazy-loaded; populated on first call to _ensure_loaded().
        self.session: Optional[object] = None
        self.tokenizer: Optional[object] = None
        self._input_names: set[str] = set()
        self._output_name: Optional[str] = None
        self._loaded: bool = False
        self._degraded: bool = False
        self._degraded_warned: bool = False

        # LRU cache keyed on SHA1(text). The cached function recomputes via
        # self._text_by_hash so the actual cache value is the result tuple.
        self._cached_score = lru_cache(maxsize=2048)(self._score_uncached_by_hash)
        self._text_by_hash: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lazy loader
    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """Load the ONNX session + tokenizer on first use.

        On any failure (missing deps, missing files, broken export) the
        instance flips to degraded mode and a single WARN is logged.
        """
        if self._loaded or self._degraded:
            return
        try:
            if not _HAS_ORT or not _HAS_TOKENIZER or not _HAS_NUMPY:
                raise RuntimeError(
                    "FinBERT deps missing (onnxruntime / transformers / numpy)"
                )

            # Resolve onnx file path (accept either dir or file).
            resolved = self._resolve_onnx_path(self.onnx_path)
            if not os.path.isfile(resolved):
                raise FileNotFoundError(f"FinBERT model not found at {resolved}")

            self.tokenizer = AutoTokenizer.from_pretrained(self.tokenizer_path)

            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = self.num_threads
            sess_options.inter_op_num_threads = 1
            sess_options.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            )
            self.session = ort.InferenceSession(
                resolved,
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )
            self._input_names = {i.name for i in self.session.get_inputs()}
            self._output_name = self.session.get_outputs()[0].name
            self.onnx_path = resolved
            self._loaded = True
            logger.info(
                "[FinBERTSentiment] loaded model=%s tokenizer=%s threads=%d",
                resolved, self.tokenizer_path, self.num_threads,
            )
        except Exception as e:
            self._degraded = True
            if not self._degraded_warned:
                logger.warning(
                    "[FinBERTSentiment] DEGRADED MODE - %s. "
                    "Returning neutral defaults until model is available.",
                    e,
                )
                self._degraded_warned = True

    @staticmethod
    def _resolve_onnx_path(onnx_path: str) -> str:
        """Accept either a directory or a file path to the ONNX model."""
        if os.path.isdir(onnx_path):
            for c in ("model_quantized.onnx", "model.onnx"):
                p = os.path.join(onnx_path, c)
                if os.path.isfile(p):
                    return p
            return os.path.join(onnx_path, "model.onnx")  # nonexistent, will trigger degraded
        return onnx_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def score(self, text: str) -> tuple[str, float, float, float]:
        """Score one text.

        Returns
        -------
        tuple[str, float, float, float]
            ``(label, p_neg, p_neu, p_pos)`` where ``label`` is the argmax
            and the three floats sum to ~1.0. In degraded mode returns
            ``("neutral", 0.5, 0.5, 0.0)``.
        """
        if not isinstance(text, str):
            raise TypeError(f"score expects str, got {type(text).__name__}")
        self._ensure_loaded()
        if self._degraded:
            return _DEGRADED_RESULT
        h = _sha1(text)
        self._text_by_hash[h] = text
        return self._cached_score(h)

    def score_batch(self, texts: list[str]) -> list[tuple[str, float, float, float]]:
        """Batched inference.

        Bypasses the LRU cache - if you want caching for repeated headlines,
        call :meth:`score` in a loop instead.
        """
        if not texts:
            return []
        self._ensure_loaded()
        if self._degraded:
            return [_DEGRADED_RESULT for _ in texts]

        feeds = self._tokenize(list(texts))
        outputs = self.session.run([self._output_name], feeds)[0]  # type: ignore[union-attr]
        arr = np.asarray(outputs, dtype=np.float32)  # type: ignore[union-attr]
        # Stable softmax along last axis.
        shifted = arr - np.max(arr, axis=-1, keepdims=True)  # type: ignore[union-attr]
        exp = np.exp(shifted)  # type: ignore[union-attr]
        probs = exp / np.sum(exp, axis=-1, keepdims=True)  # type: ignore[union-attr]
        results: list[tuple[str, float, float, float]] = []
        for i in range(probs.shape[0]):
            results.append(self._row_to_tuple(probs[i]))
        return results

    def cache_info(self):
        """Expose lru_cache stats for debugging / tests."""
        return self._cached_score.cache_info()

    def cache_clear(self) -> None:
        """Drop all cached scores and the SHA1->text map."""
        self._cached_score.cache_clear()
        self._text_by_hash.clear()

    @property
    def degraded(self) -> bool:
        """True if the instance is operating without a real ONNX model."""
        # Trigger lazy load so callers can check after construction.
        self._ensure_loaded()
        return self._degraded

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _tokenize(self, texts: list[str]) -> dict:
        enc = self.tokenizer(  # type: ignore[misc]
            texts,
            padding=True,
            truncation=True,
            max_length=self.max_len,
            return_tensors="np",
        )
        feeds: dict = {}
        for name in ("input_ids", "attention_mask", "token_type_ids"):
            if name in self._input_names and name in enc:
                arr = enc[name]
                if arr.dtype != np.int64:  # type: ignore[union-attr]
                    arr = arr.astype(np.int64)  # type: ignore[union-attr]
                feeds[name] = arr
        return feeds

    @staticmethod
    def _row_to_tuple(row) -> tuple[str, float, float, float]:
        """Convert a 3-vector of probs (positive, negative, neutral)
        into the public ``(label, p_neg, p_neu, p_pos)`` tuple.
        """
        p_pos = float(row[0])
        p_neg = float(row[1])
        p_neu = float(row[2])
        # argmax across the original ProsusAI label order.
        order = [("positive", p_pos), ("negative", p_neg), ("neutral", p_neu)]
        label = max(order, key=lambda t: t[1])[0]
        return (label, p_neg, p_neu, p_pos)

    def _score_uncached_by_hash(
        self, text_hash: str
    ) -> tuple[str, float, float, float]:
        text = self._text_by_hash.get(text_hash, "")
        feeds = self._tokenize([text])
        outputs = self.session.run([self._output_name], feeds)[0]  # type: ignore[union-attr]
        row = outputs[0]
        # Apply softmax (use numpy if available, else pure Python).
        if _HAS_NUMPY:
            arr = np.asarray(row, dtype=np.float32)  # type: ignore[union-attr]
            arr = arr - float(np.max(arr))  # type: ignore[union-attr]
            exp = np.exp(arr)  # type: ignore[union-attr]
            probs = exp / float(np.sum(exp))  # type: ignore[union-attr]
        else:  # pragma: no cover
            probs = _softmax_py(list(row))
        return self._row_to_tuple(probs)


__all__ = ["FinBERTSentiment", "FINBERT_LABELS"]
