"""
Phoenix Bot - SentimentFlowAgent unit tests (Section 4)

Pure-mock tests. Verifies:

  - vote() returns (Vote, info) with weight=0 effect
    (bias=NEUTRAL, confidence=0).
  - Persists info to JSONL fallback when ChromaDB / RAG unavailable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.council_gate import Vote  # noqa: E402
from agents.sentiment_flow_agent import (  # noqa: E402
    DEFAULT_WEIGHT,
    NAME,
    SentimentFlowAgent,
)


def _fake_sentiment(
    label: str = "positive",
    p_neg: float = 0.1,
    p_neu: float = 0.2,
    p_pos: float = 0.7,
):
    fake = mock.MagicMock()
    fake.score.return_value = (label, p_neg, p_neu, p_pos)
    return fake


# ---------------------------------------------------------------------
# 1. vote() returns weight-0 (Vote, info) pair
# ---------------------------------------------------------------------
def test_vote_returns_neutral_weight_zero(tmp_path):
    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment(),
        rag=None,
        fallback_log_path=tmp_path / "obs.jsonl",
    )
    market = {"price": 22000.0}
    intel = {"news": {"summary": "Fed signals dovish pivot at next FOMC meeting"}}

    vote, info = agent.vote(market, intel)

    assert isinstance(vote, Vote)
    assert vote.bias == "NEUTRAL"
    assert vote.confidence == 0.0
    assert vote.voter == NAME
    assert "weight=0" in vote.reasoning

    assert info["weight"] == 0.0
    assert info["label"] == "positive"
    assert info["p_pos"] == pytest.approx(0.7)
    assert info["n_headlines"] == 1
    assert info["price"] == 22000.0


def test_default_weight_is_zero():
    assert DEFAULT_WEIGHT == 0.0


def test_name_constant():
    assert NAME == "Sentiment Flow"
    assert SentimentFlowAgent.NAME == NAME
    assert SentimentFlowAgent.DEFAULT_WEIGHT == 0.0


def test_vote_handles_missing_news_text(tmp_path):
    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment(),
        rag=None,
        fallback_log_path=tmp_path / "obs.jsonl",
    )
    vote, info = agent.vote({"price": 22000.0}, intel={})
    assert vote.bias == "NEUTRAL"
    assert info["text"] == ""
    assert info["label"] == "neutral"
    assert info["n_headlines"] == 0


# ---------------------------------------------------------------------
# 2. JSONL persistence when RAG unavailable
# ---------------------------------------------------------------------
def test_persists_to_jsonl_when_rag_unavailable(tmp_path):
    log_path = tmp_path / "sentiment_observations.jsonl"
    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment(label="negative", p_neg=0.8, p_neu=0.15, p_pos=0.05),
        rag=None,
        fallback_log_path=log_path,
    )

    intel = {"news": {"summary": "Massive earnings miss, NDX limit-down"}}
    _, info = agent.vote({"price": 21500.0}, intel)

    assert info["persisted"] is True
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["label"] == "negative"
    assert payload["p_neg"] == pytest.approx(0.8)
    assert payload["weight"] == 0.0


def test_persists_via_rag_when_available(tmp_path):
    fake_rag = mock.MagicMock()
    fake_rag.add_observation = mock.MagicMock()
    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment(),
        rag=fake_rag,
        fallback_log_path=tmp_path / "obs.jsonl",
    )

    _, info = agent.vote({"price": 22000.0}, intel={"news": {"summary": "x"}})

    fake_rag.add_observation.assert_called_once()
    assert info["persisted"] is True
    # Fallback file should NOT be touched.
    assert not (tmp_path / "obs.jsonl").exists()


def test_rag_failure_falls_back_to_jsonl(tmp_path):
    fake_rag = mock.MagicMock()
    fake_rag.add_observation = mock.MagicMock(side_effect=RuntimeError("chroma down"))
    log_path = tmp_path / "obs.jsonl"
    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment(),
        rag=fake_rag,
        fallback_log_path=log_path,
    )

    _, info = agent.vote({"price": 22000.0}, intel={"news": {"summary": "x"}})

    assert info["persisted"] is True
    assert log_path.exists()


def test_nonzero_weight_warns_but_still_zero(caplog, tmp_path):
    import logging
    with caplog.at_level(logging.WARNING, logger="SentimentFlowAgent"):
        agent = SentimentFlowAgent(
            sentiment=_fake_sentiment(),
            rag=None,
            fallback_log_path=tmp_path / "obs.jsonl",
            weight=0.5,
        )
        vote, info = agent.vote({}, {})
    assert vote.confidence == 0.0
    assert info["weight"] == 0.0  # always zero in observation mode
    assert info["configured_weight"] == 0.5
    assert any("non-zero weight" in rec.message for rec in caplog.records)
