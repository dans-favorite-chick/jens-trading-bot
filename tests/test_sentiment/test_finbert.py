"""
Phoenix Bot - FinBERT sentiment unit tests (Section 4)

Pure-mock tests that do NOT require onnxruntime, transformers, numpy, or
a real ONNX model on disk. Covers:

  - Tokenizer round-trip on 5 sample headlines (mock the tokenizer)
  - score() returns a label in the valid set and 3 probs sum ~1.0
  - LRU cache: same input -> only ONE underlying forward pass
  - Degraded mode: missing model files -> ("neutral", 0.5, 0.5, 0.0)
    and does NOT raise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import sentiment_finbert as sf  # noqa: E402
from core.sentiment_finbert import (  # noqa: E402
    FINBERT_LABELS,
    FinBERTSentiment,
)


HEADLINES = [
    "Fed signals dovish pivot at next FOMC meeting",
    "GDP misses estimates, Q3 print revised lower",
    "NVDA beats earnings, guides above consensus on AI demand",
    "Hawkish FOMC minutes spook bond market",
    "AAPL misses revenue estimates on weak iPhone sales",
]


# ---------------------------------------------------------------------
# Helpers - build a fully mocked FinBERTSentiment instance.
# ---------------------------------------------------------------------
def _mock_session_run(probs_pos_neg_neu: tuple[float, float, float]):
    """Return a fake session.run that emits raw logits matching the order
    (positive, negative, neutral) -- the real ProsusAI/finbert label order."""
    pos, neg, neu = probs_pos_neg_neu
    # Use logits whose softmax approximates these probs.
    import math
    logits = [math.log(max(p, 1e-9)) for p in (pos, neg, neu)]

    def run(_outputs, _feeds):
        return [[logits]]  # shape (1, 3)
    return run


def _make_loaded_finbert(monkeypatch, probs=(0.7, 0.1, 0.2)) -> FinBERTSentiment:
    """Build a FinBERTSentiment whose _ensure_loaded() succeeds with a
    fully-mocked tokenizer + session, regardless of disk state."""
    fb = FinBERTSentiment(
        onnx_path="/nonexistent/model.onnx",
        tokenizer_path="/nonexistent/tokenizer",
    )
    # Force "loaded" state with mocks.
    fb._loaded = True
    fb._degraded = False
    fb._input_names = {"input_ids", "attention_mask"}
    fb._output_name = "logits"
    fb.tokenizer = mock.MagicMock()
    fb.session = mock.MagicMock()
    fb.session.run = mock.MagicMock(side_effect=_mock_session_run(probs))

    # _tokenize requires numpy if available; if numpy isn't installed,
    # patch _tokenize to a no-op so the path stays compatible.
    if not sf._HAS_NUMPY:
        fb._tokenize = lambda texts: {}  # type: ignore[assignment]
    else:
        # Provide a shape-correct fake encoding.
        import numpy as np
        fake_enc = {
            "input_ids": np.array([[101, 2003, 1037, 102]], dtype=np.int64),
            "attention_mask": np.array([[1, 1, 1, 1]], dtype=np.int64),
        }
        fb.tokenizer.return_value = fake_enc
    return fb


def _patch_softmax_path(monkeypatch):
    """Make _score_uncached_by_hash work without numpy by patching it."""
    if sf._HAS_NUMPY:
        return
    # Replace the inner method with a pure-python equivalent that uses
    # the mocked session.run output directly.
    def _alt(self, text_hash):
        text = self._text_by_hash.get(text_hash, "")
        feeds = self._tokenize([text])
        outputs = self.session.run([self._output_name], feeds)[0]
        row = outputs[0]
        probs = sf._softmax_py(list(row))
        return self._row_to_tuple(probs)
    monkeypatch.setattr(FinBERTSentiment, "_score_uncached_by_hash", _alt)


# ---------------------------------------------------------------------
# 1. Tokenizer round-trip on 5 sample headlines (mock the tokenizer)
# ---------------------------------------------------------------------
def test_tokenizer_roundtrip_5_headlines(monkeypatch):
    fb = _make_loaded_finbert(monkeypatch)
    # Mock encode/decode to behave like an identity round trip.
    fb.tokenizer.encode = lambda text, **kwargs: list(range(1, len(text.split()) + 1))
    fb.tokenizer.decode = lambda ids, **kwargs: " ".join(["word"] * len(ids))

    for h in HEADLINES:
        ids = fb.tokenizer.encode(h, max_length=64, truncation=True)
        assert isinstance(ids, list)
        assert len(ids) > 0
        decoded = fb.tokenizer.decode(ids, skip_special_tokens=True)
        assert isinstance(decoded, str)
        assert len(decoded) > 0


# ---------------------------------------------------------------------
# 2. score() returns valid label + probs sum ~ 1.0
# ---------------------------------------------------------------------
def test_score_returns_valid_distribution(monkeypatch):
    _patch_softmax_path(monkeypatch)
    fb = _make_loaded_finbert(monkeypatch, probs=(0.7, 0.2, 0.1))

    label, p_neg, p_neu, p_pos = fb.score("NVDA crushes earnings")
    assert label in {"positive", "negative", "neutral"}
    assert label in set(FINBERT_LABELS)
    total = p_neg + p_neu + p_pos
    assert abs(total - 1.0) < 1e-3, f"probs don't sum to 1: {total}"
    # Argmax should be positive given probs=(0.7, 0.2, 0.1).
    assert label == "positive"


# ---------------------------------------------------------------------
# 3. LRU cache: same input -> only ONE forward pass
# ---------------------------------------------------------------------
def test_lru_cache_skips_second_forward_pass(monkeypatch):
    _patch_softmax_path(monkeypatch)
    fb = _make_loaded_finbert(monkeypatch, probs=(0.5, 0.3, 0.2))

    text = "Fed signals dovish pivot"
    a = fb.score(text)
    b = fb.score(text)
    c = fb.score(text)

    assert a == b == c
    assert fb.session.run.call_count == 1, (
        f"expected 1 forward pass, got {fb.session.run.call_count}"
    )
    info = fb.cache_info()
    assert info.hits >= 2
    assert info.misses == 1


# ---------------------------------------------------------------------
# 4. Degraded mode: missing model -> neutral default, no raise
# ---------------------------------------------------------------------
def test_degraded_mode_returns_neutral_default():
    fb = FinBERTSentiment(
        onnx_path="/totally/nonexistent/path/model.onnx",
        tokenizer_path="/totally/nonexistent/path",
    )
    # Should not raise.
    out = fb.score("anything at all")
    assert out == ("neutral", 0.5, 0.5, 0.0)
    # Batch path also degraded.
    batch = fb.score_batch(["a", "b", "c"])
    assert batch == [("neutral", 0.5, 0.5, 0.0)] * 3
    # The flag is exposed.
    assert fb.degraded is True


def test_degraded_mode_does_not_raise_on_repeated_calls():
    fb = FinBERTSentiment(
        onnx_path="/nope/model.onnx",
        tokenizer_path="/nope",
    )
    for _ in range(20):
        assert fb.score("Headline") == ("neutral", 0.5, 0.5, 0.0)


# ---------------------------------------------------------------------
# Bonus: score_batch returns the right shape from mocked session
# ---------------------------------------------------------------------
@pytest.mark.skipif(not sf._HAS_NUMPY, reason="batch path uses numpy")
def test_score_batch_shape(monkeypatch):
    fb = _make_loaded_finbert(monkeypatch, probs=(0.4, 0.4, 0.2))

    import numpy as np
    # Mock tokenizer to handle batch input.
    fb.tokenizer.return_value = {
        "input_ids": np.array([[101, 1, 102], [101, 2, 102]], dtype=np.int64),
        "attention_mask": np.array([[1, 1, 1], [1, 1, 1]], dtype=np.int64),
    }
    # session.run returns a (2, 3) logits array.
    import math
    logits = np.array(
        [[math.log(0.4), math.log(0.4), math.log(0.2)]] * 2,
        dtype=np.float32,
    )
    fb.session.run = mock.MagicMock(return_value=[logits])

    batch = fb.score_batch(["a", "b"])
    assert len(batch) == 2
    for label, p_neg, p_neu, p_pos in batch:
        assert label in {"positive", "negative", "neutral"}
        assert abs((p_neg + p_neu + p_pos) - 1.0) < 1e-3
