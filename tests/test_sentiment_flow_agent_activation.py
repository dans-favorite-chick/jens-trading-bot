"""
Phoenix Bot - SentimentFlowAgent activation toggle tests (post-2026-04-25 §2.2)

Verifies:
  - SENTIMENT_FLOW_ACTIVE=false  -> vote() returns weight=0 even on strong
                                    sentiment input (legacy observation mode).
  - SENTIMENT_FLOW_ACTIVE=true   -> vote() returns weight=SENTIMENT_FLOW_WEIGHT
                                    with non-zero confidence on strong input.
  - Observation log persists in BOTH modes (data is never lost).
  - Council orchestrator dispatches a deterministic voter without crashing
    on a missing LLM response.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.council_gate import Vote  # noqa: E402
from agents.sentiment_flow_agent import (  # noqa: E402
    SentimentFlowAgent,
    _env_active,
    _env_weight,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _fake_sentiment_strong_pos():
    fake = mock.MagicMock()
    # p_pos > 0.6 and p_pos > p_neg -> BULLISH branch
    fake.score.return_value = ("positive", 0.05, 0.10, 0.85)
    return fake


def _fake_sentiment_strong_neg():
    fake = mock.MagicMock()
    fake.score.return_value = ("negative", 0.85, 0.10, 0.05)
    return fake


# ---------------------------------------------------------------------
# 1. Inactive default: weight=0 even with strong input
# ---------------------------------------------------------------------
def test_inactive_default_returns_weight_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "false")
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "0.10")

    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment_strong_pos(),
        rag=None,
        fallback_log_path=tmp_path / "obs.jsonl",
    )
    assert agent.active is False

    market = {"price": 22000.0}
    intel = {"news": {"summary": "Market rips on dovish Fed pivot"}}
    vote, info = agent.vote(market, intel)

    assert isinstance(vote, Vote)
    assert vote.bias == "NEUTRAL"
    assert vote.confidence == 0.0
    assert info["weight"] == 0.0
    assert info["active"] is False
    # Underlying FinBERT score is still computed and exposed.
    assert info["label"] == "positive"
    assert info["p_pos"] == pytest.approx(0.85)


def test_inactive_misc_truthy_strings_not_truthy(monkeypatch, tmp_path):
    """Spec: only true/1/yes (case-insensitive) count as truthy."""
    for raw in ("False", "0", "no", "", "FALSE", "off"):
        monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", raw)
        agent = SentimentFlowAgent(
            sentiment=_fake_sentiment_strong_pos(),
            rag=None,
            fallback_log_path=tmp_path / f"obs_{raw or 'empty'}.jsonl",
        )
        assert agent.active is False, f"raw={raw!r} should be inactive"


# ---------------------------------------------------------------------
# 2. Active: weight = SENTIMENT_FLOW_WEIGHT, real confidence
# ---------------------------------------------------------------------
def test_active_returns_configured_weight_and_bullish(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "true")
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "0.10")

    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment_strong_pos(),
        rag=None,
        fallback_log_path=tmp_path / "obs.jsonl",
    )
    assert agent.active is True
    assert agent.weight == pytest.approx(0.10)

    intel = {"news": {"summary": "Megacap earnings blowout"}}
    vote, info = agent.vote({"price": 22000.0}, intel)

    assert vote.bias == "BULLISH"
    assert vote.confidence > 0
    assert vote.confidence == pytest.approx(85.0)  # round(0.85 * 100)
    assert info["weight"] == pytest.approx(0.10)
    assert info["active"] is True
    assert info["bias"] == "BULLISH"


def test_active_strong_negative_yields_bearish(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "1")  # also truthy
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "0.25")

    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment_strong_neg(),
        rag=None,
        fallback_log_path=tmp_path / "obs.jsonl",
    )
    assert agent.active is True
    assert agent.weight == pytest.approx(0.25)

    intel = {"news": {"summary": "CPI smashes higher, hawks circling"}}
    vote, info = agent.vote({"price": 21500.0}, intel)

    assert vote.bias == "BEARISH"
    assert vote.confidence == pytest.approx(85.0)
    assert info["weight"] == pytest.approx(0.25)


def test_active_weak_signal_stays_neutral(monkeypatch, tmp_path):
    """If neither p_pos nor p_neg crosses 0.6, vote is NEUTRAL even active."""
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "true")
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "0.10")

    fake = mock.MagicMock()
    fake.score.return_value = ("neutral", 0.30, 0.50, 0.20)

    agent = SentimentFlowAgent(
        sentiment=fake,
        rag=None,
        fallback_log_path=tmp_path / "obs.jsonl",
    )
    vote, info = agent.vote({"price": 22000.0}, {"news": {"summary": "mixed bag"}})

    assert vote.bias == "NEUTRAL"
    # confidence == p_neu * 100 == 50
    assert vote.confidence == pytest.approx(50.0)
    assert info["weight"] == pytest.approx(0.10)


# ---------------------------------------------------------------------
# 3. Observation log persists in BOTH modes
# ---------------------------------------------------------------------
def test_observation_log_written_when_inactive(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "false")
    log_path = tmp_path / "obs.jsonl"
    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment_strong_pos(),
        rag=None,
        fallback_log_path=log_path,
    )
    _, info = agent.vote({"price": 22000.0}, {"news": {"summary": "x"}})

    assert info["persisted"] is True
    assert log_path.exists()
    payload = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert payload["active"] is False
    assert payload["weight"] == 0.0
    assert payload["label"] == "positive"


def test_observation_log_written_when_active(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "true")
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "0.10")
    log_path = tmp_path / "obs.jsonl"
    agent = SentimentFlowAgent(
        sentiment=_fake_sentiment_strong_pos(),
        rag=None,
        fallback_log_path=log_path,
    )
    _, info = agent.vote({"price": 22000.0}, {"news": {"summary": "x"}})

    assert info["persisted"] is True
    assert log_path.exists()
    payload = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert payload["active"] is True
    assert payload["weight"] == pytest.approx(0.10)


# ---------------------------------------------------------------------
# 4. Council dispatch: deterministic voter does not crash on missing LLM
# ---------------------------------------------------------------------
def test_council_handles_deterministic_voter_without_llm(monkeypatch, tmp_path):
    """The council orchestrator must dispatch the deterministic Sentiment
    Flow voter via SentimentFlowAgent.vote() rather than the LLM voter
    path. Calling _run_deterministic_voter must succeed even if no LLM
    client is reachable.
    """
    # Make the agent active so the council actually runs it.
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "true")
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "0.10")

    # Redirect the default observation log into tmp_path so we don't
    # touch the real logs/ dir.
    import agents.sentiment_flow_agent as sfa
    monkeypatch.setattr(
        sfa, "_DEFAULT_FALLBACK_LOG", tmp_path / "obs.jsonl",
        raising=False,
    )

    # Force FinBERTSentiment construction inside the deterministic voter
    # to return a fake scorer (avoids the heavy ML init).
    fake = _fake_sentiment_strong_pos()

    class _FakeFinBERT:
        def __init__(self, *a, **kw):
            self.score = fake.score

    monkeypatch.setattr(sfa, "FinBERTSentiment", _FakeFinBERT, raising=False)
    monkeypatch.setattr(sfa, "_HAS_FINBERT", True, raising=False)

    from agents.council_gate import _run_deterministic_voter

    cfg = {
        "name": "Sentiment Flow",
        "system": "FinBERT sentiment voter (deterministic, env-gated).",
        "is_deterministic": True,
    }
    market = {"price": 22000.0, "intel": {"news": {"summary": "Bull rip"}}}

    # No LLM client is supplied / configured anywhere; the voter must
    # still produce a Vote and not raise.
    vote = asyncio.run(_run_deterministic_voter(cfg, market))

    assert isinstance(vote, Vote)
    assert vote.voter == "Sentiment Flow"
    # With active=true and strong-positive fake sentiment we expect BULLISH.
    assert vote.bias in ("BULLISH", "BEARISH", "NEUTRAL")
    # Crash-safety: even an unknown deterministic voter must not raise.
    bad_cfg = {"name": "Mystery", "is_deterministic": True}
    bad_vote = asyncio.run(_run_deterministic_voter(bad_cfg, market))
    assert bad_vote.bias == "ABSTAIN"


def test_inactive_default_excludes_voter_from_council(monkeypatch):
    """In inactive (default) mode the Sentiment Flow voter is filtered
    out of the council so total_voters and tally math remain identical
    to the legacy 8-voter setup.
    """
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "false")
    from agents.council_gate import _select_voter_configs, VOTER_CONFIGS
    selected = _select_voter_configs()
    names = [c["name"] for c in selected]
    assert "Sentiment Flow" not in names
    # The other voters are unchanged.
    assert len(selected) == len(VOTER_CONFIGS) - 1


def test_active_includes_voter_in_council(monkeypatch):
    monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", "true")
    from agents.council_gate import _select_voter_configs
    selected = _select_voter_configs()
    names = [c["name"] for c in selected]
    assert "Sentiment Flow" in names


# ---------------------------------------------------------------------
# 5. Env parser sanity (small but useful)
# ---------------------------------------------------------------------
def test_env_active_truthy_variants(monkeypatch):
    for raw in ("true", "TRUE", "True", "1", "yes", "Yes", "YES", "y", "on"):
        monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", raw)
        assert _env_active() is True, f"raw={raw!r} should be truthy"
    for raw in ("false", "0", "no", "off", "", "anything-else"):
        monkeypatch.setenv("SENTIMENT_FLOW_ACTIVE", raw)
        assert _env_active() is False, f"raw={raw!r} should be falsy"


def test_env_weight_default_and_parse(monkeypatch):
    monkeypatch.delenv("SENTIMENT_FLOW_WEIGHT", raising=False)
    assert _env_weight() == pytest.approx(0.10)
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "0.25")
    assert _env_weight() == pytest.approx(0.25)
    monkeypatch.setenv("SENTIMENT_FLOW_WEIGHT", "not-a-float")
    assert _env_weight() == pytest.approx(0.10)
