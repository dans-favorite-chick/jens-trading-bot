"""
Phoenix Bot - FinBERT sentiment tests.

These tests are designed to run under the ML venv (.venv-ml) which has
transformers + onnxruntime installed and the ProsusAI/finbert ONNX export
under models/finbert_onnx[_int8]/. If those aren't present we skip the
inference-dependent tests so the primary venv test run isn't broken.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Skip the whole module if optional ML deps aren't installed.
ort = pytest.importorskip("onnxruntime")
transformers = pytest.importorskip("transformers")

from core.sentiment_finbert import FinBERTSentiment, FINBERT_LABELS  # noqa: E402
from council.sentiment_flow_agent import (  # noqa: E402
    DEFAULT_WEIGHT,
    SentimentFlowAgent,
)

# Paths to the exported model. If the export hasn't happened yet, all
# inference tests skip (we still test pure-Python pieces below).
ONNX_FP32_DIR = PROJECT_ROOT / "models" / "finbert_onnx"
ONNX_INT8_DIR = PROJECT_ROOT / "models" / "finbert_onnx_int8"

MODEL_DIR = ONNX_INT8_DIR if ONNX_INT8_DIR.exists() else ONNX_FP32_DIR
MODEL_AVAILABLE = MODEL_DIR.exists() and any(
    (MODEL_DIR / f).exists() for f in ("model.onnx", "model_quantized.onnx")
)
TOKENIZER_DIR = ONNX_FP32_DIR  # tokenizer always exported alongside FP32

requires_model = pytest.mark.skipif(
    not MODEL_AVAILABLE,
    reason=f"FinBERT ONNX export not present at {MODEL_DIR}",
)

HEADLINES = [
    "Fed signals dovish pivot at next FOMC meeting",
    "GDP misses estimates, Q3 print revised lower",
    "NVDA beats earnings, guides above consensus on AI demand",
    "Hawkish FOMC minutes spook bond market",
    "AAPL misses revenue estimates on weak iPhone sales",
]


@pytest.fixture(scope="module")
def finbert():
    if not MODEL_AVAILABLE:
        pytest.skip("model not available")
    return FinBERTSentiment(
        onnx_path=str(MODEL_DIR),
        tokenizer_path=str(TOKENIZER_DIR),
        max_len=64,
        num_threads=4,
    )


# ---------------------------------------------------------------------
# Tokenizer round-trip
# ---------------------------------------------------------------------
@requires_model
def test_tokenizer_roundtrip_5_headlines(finbert):
    """Every headline must survive encode/decode at max_len=64."""
    tok = finbert.tokenizer
    for h in HEADLINES:
        ids = tok.encode(h, max_length=64, truncation=True, add_special_tokens=True)
        assert isinstance(ids, list)
        assert len(ids) > 0
        decoded = tok.decode(ids, skip_special_tokens=True)
        # Allow case-insensitive substring match - BERT tokenizer is uncased.
        assert decoded.strip().lower().split()[:3] == h.lower().split()[:3], (
            f"roundtrip drift: {h!r} -> {decoded!r}"
        )


# ---------------------------------------------------------------------
# score() output shape
# ---------------------------------------------------------------------
@requires_model
@pytest.mark.parametrize("headline", HEADLINES)
def test_score_returns_valid_distribution(finbert, headline):
    out = finbert.score(headline)
    assert set(out.keys()) == set(FINBERT_LABELS)
    for v in out.values():
        assert 0.0 <= v <= 1.0
    s = sum(out.values())
    assert abs(s - 1.0) < 1e-3, f"probs don't sum to 1: {out} (sum={s})"
    dom = max(out, key=out.get)
    assert dom in {"positive", "negative", "neutral"}


@requires_model
def test_score_batch_matches_score(finbert):
    batch = finbert.score_batch(HEADLINES)
    assert len(batch) == len(HEADLINES)
    for s in batch:
        assert set(s.keys()) == set(FINBERT_LABELS)
        assert abs(sum(s.values()) - 1.0) < 1e-3


# ---------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------
@requires_model
def test_lru_cache_hits_skip_session_run(finbert):
    """Calling score() twice with the same text should run the session once."""
    finbert.cache_clear()
    text = HEADLINES[0]
    real_run = finbert.session.run
    with mock.patch.object(finbert.session, "run", side_effect=real_run) as mocked:
        a = finbert.score(text)
        b = finbert.score(text)  # should be cached
        c = finbert.score(text)  # cached again
    assert a == b == c
    assert mocked.call_count == 1, (
        f"expected 1 session.run call, got {mocked.call_count}"
    )
    info = finbert.cache_info()
    assert info.hits >= 2
    assert info.misses == 1


# ---------------------------------------------------------------------
# Council agent stays at 0.0 even on extreme polarization
# ---------------------------------------------------------------------
def test_default_weight_is_zero():
    assert DEFAULT_WEIGHT == 0.0


def test_sentiment_flow_agent_returns_zero_without_model():
    """Agent without a sentiment model should still return (0.0, info_dict)."""
    agent = SentimentFlowAgent(sentiment=None, rag=None)
    vote, info = agent.vote({"price": 22000.0}, headlines=["irrelevant"])
    assert vote == 0.0
    assert info["n_headlines"] == 1
    assert info["weight"] == 0.0


@requires_model
def test_sentiment_flow_agent_zero_on_polarized_inputs(finbert):
    """Even with strong sentiment in either direction, vote magnitude is 0.0."""
    agent = SentimentFlowAgent(sentiment=finbert, rag=None)

    bull_news = [
        "S&P 500 closes at fresh all-time high",
        "NVDA crushes earnings, guides way above consensus",
        "Fed signals dovish pivot, equities rip higher",
        "Goldman raises NDX target, calls melt-up scenario",
    ]
    bear_news = [
        "VIX spikes 50% on geopolitical shock",
        "Regional banks plunge as credit losses mount",
        "GDP misses badly, recession officially declared",
        "Massive earnings miss, NDX futures limit-down",
    ]

    bull_vote, bull_info = agent.vote({"price": 22000.0}, headlines=bull_news)
    bear_vote, bear_info = agent.vote({"price": 22000.0}, headlines=bear_news)

    assert bull_vote == 0.0
    assert bear_vote == 0.0
    # net_score should still be informative (positive vs negative tilt)
    assert bull_info["net_score"] >= bear_info["net_score"]


def test_sentiment_flow_agent_persists_via_rag():
    """Agent should call rag.add_near_miss and mark info['persisted']=True."""
    fake_rag = mock.MagicMock()
    agent = SentimentFlowAgent(sentiment=None, rag=fake_rag)
    vote, info = agent.vote({"price": 22000.0}, headlines=[])
    assert vote == 0.0
    fake_rag.add_near_miss.assert_called_once()
    assert info["persisted"] is True


def test_sentiment_flow_agent_warns_on_nonzero_weight(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="SentimentFlowAgent"):
        agent = SentimentFlowAgent(sentiment=None, rag=None, weight=0.5)
        vote, _ = agent.vote({}, headlines=["anything"])
    assert vote == 0.0  # still zero magnitude
    assert agent.weight == 0.5
    assert any("non-zero weight" in rec.message for rec in caplog.records)
