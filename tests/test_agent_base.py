"""Tests for agents.base_agent (S4 infra).

Uses FakeAIClient-style monkey-patching against the real AIClient so we
never hit a real API. Covers:
  - timeout path returns default
  - retry-on-transient-error eventually succeeds
  - success path + JSON parsing
  - call logger writes one JSONL line per call
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

# Ensure project root on sys.path (pytest usually handles this, but be safe)
import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import base_agent, config as agent_config
from agents.base_agent import AIClient, BaseAgent, estimate_tokens


# ─── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_log_dir(tmp_path, monkeypatch):
    """Redirect agent call-log output to a tmp dir."""
    log_dir = tmp_path / "agent_logs"
    monkeypatch.setattr(agent_config, "LOG_DIR", log_dir)

    def _daily(date_str: str | None = None) -> Path:
        from datetime import datetime
        if date_str is None:
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
        return log_dir / f"{date_str}_agent_calls.jsonl"

    monkeypatch.setattr(agent_config, "daily_log_path", _daily)
    return log_dir


@pytest.fixture
def have_keys(monkeypatch):
    """Pretend we have Gemini + Claude keys so DEGRADED gate is open."""
    monkeypatch.setattr(agent_config, "GOOGLE_API_KEY", "fake-google")
    monkeypatch.setattr(agent_config, "ANTHROPIC_API_KEY", "fake-anthropic")
    monkeypatch.setattr(agent_config, "DEGRADED", False)
    return True


# ─── Tests ───────────────────────────────────────────────────────────────

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("abcd") >= 1
    assert estimate_tokens("x" * 400) >= 100


def test_parse_json_variants():
    assert AIClient.parse_json('{"a": 1}') == {"a": 1}
    fenced = "here it is:\n```json\n{\"b\": 2}\n```\n"
    assert AIClient.parse_json(fenced) == {"b": 2}
    embedded = "prefix {\"c\": 3} suffix"
    assert AIClient.parse_json(embedded) == {"c": 3}
    assert AIClient.parse_json(None, default={"x": 0}) == {"x": 0}
    assert AIClient.parse_json("not json", default=None) is None


def test_timeout_returns_default(tmp_log_dir, have_keys, monkeypatch):
    """If the underlying call times out every attempt, default is returned."""
    client = AIClient(timeout_s=0.01, max_attempts=2, backoff_initial_s=0.0)

    async def _always_timeout(*a, **kw):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(client, "_gemini_once", _always_timeout)

    result = asyncio.run(client.ask_gemini("hi", default="FALLBACK"))
    assert result == "FALLBACK"

    log_file = agent_config.daily_log_path()
    assert log_file.exists()
    lines = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["outcome"] == "timeout"
    assert lines[0]["attempts"] == 2


def test_retry_then_success(tmp_log_dir, have_keys, monkeypatch):
    """Two transient errors, then success — final outcome is success."""
    client = AIClient(timeout_s=1.0, max_attempts=3, backoff_initial_s=0.0)

    calls = {"n": 0}

    async def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient boom")
        return '{"ok": true}'

    monkeypatch.setattr(client, "_gemini_once", _flaky)

    result = asyncio.run(client.ask_gemini("hi", default=None))
    assert result == '{"ok": true}'
    assert calls["n"] == 3

    # Exactly one log line (final outcome)
    lines = agent_config.daily_log_path().read_text().splitlines()
    lines = [ln for ln in lines if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["outcome"] == "success"
    assert entry["attempts"] == 3


def test_success_path_parses_json(tmp_log_dir, have_keys, monkeypatch):
    client = AIClient(timeout_s=1.0, max_attempts=1, backoff_initial_s=0.0)

    async def _ok(*a, **kw):
        return "```json\n{\"verdict\": \"pass\", \"score\": 0.8}\n```"

    monkeypatch.setattr(client, "_claude_once", _ok)

    text = asyncio.run(client.ask_claude("hi", default=None))
    parsed = AIClient.parse_json(text)
    assert parsed == {"verdict": "pass", "score": 0.8}

    entries = [
        json.loads(l) for l in agent_config.daily_log_path().read_text().splitlines()
        if l.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "success"
    assert entries[0]["input_tokens_est"] >= 1
    assert entries[0]["output_tokens_est"] >= 1


def test_degraded_mode_returns_default_without_call(tmp_log_dir, monkeypatch):
    """With no API key, ask_* returns default and logs 'degraded' without
    invoking the underlying call."""
    monkeypatch.setattr(agent_config, "GOOGLE_API_KEY", None)
    client = AIClient(timeout_s=1.0, max_attempts=1, backoff_initial_s=0.0)

    called = {"n": 0}

    async def _should_not_run(*a, **kw):
        called["n"] += 1
        return "nope"

    monkeypatch.setattr(client, "_gemini_once", _should_not_run)

    result = asyncio.run(client.ask_gemini("hi", default="DEF"))
    assert result == "DEF"
    assert called["n"] == 0

    entries = [
        json.loads(l) for l in agent_config.daily_log_path().read_text().splitlines()
        if l.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "degraded"


def test_base_agent_safe_call(tmp_log_dir, have_keys):
    class MyAgent(BaseAgent):
        name = "unit-test-agent"

        async def run(self, ctx):
            return await self.safe_call(
                lambda: self._boom(), default={"ok": False}, what="boom"
            )

        async def _boom(self):
            raise ValueError("intentional")

    agent = MyAgent()
    out = asyncio.run(agent.run({}))
    assert out == {"ok": False}


def test_call_log_writes_one_line_per_call(tmp_log_dir, have_keys, monkeypatch):
    client = AIClient(timeout_s=1.0, max_attempts=1, backoff_initial_s=0.0)

    async def _ok(*a, **kw):
        return "hello"

    monkeypatch.setattr(client, "_gemini_once", _ok)

    async def _multi():
        await client.ask_gemini("one")
        await client.ask_gemini("two")
        await client.ask_gemini("three")

    asyncio.run(_multi())

    lines = [
        l for l in agent_config.daily_log_path().read_text().splitlines() if l.strip()
    ]
    assert len(lines) == 3
    for l in lines:
        entry = json.loads(l)
        assert entry["outcome"] == "success"
        assert "latency_ms" in entry
        assert "model" in entry
