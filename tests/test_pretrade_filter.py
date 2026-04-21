"""Tests for agents.pretrade_filter (S6 / Phase H-4B).

Strategy: stub out ``AIClient.ask_gemini`` with a fake async that either
returns canned JSON, sleeps past the timeout, raises, or returns None.
No real API calls are made.

Covers the S6 acceptance checklist:
  - 3-second timeout → default CLEAR
  - advisory mode never blocks (even SIT_OUT)
  - blocking mode + SIT_OUT → should_skip_trade True
  - CAUTION logs a warning
  - One JSONL line written to logs/agents/YYYY-MM-DD_agent_calls.jsonl
    on every AI call (via base_agent's call logger).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import config as agent_config
from agents import pretrade_filter
from agents.base_agent import AIClient
from agents.pretrade_filter import (
    DEFAULT_FILTER_MODE,
    DEFAULT_VERDICT,
    FILTER_TIMEOUT_S,
    FilterVerdict,
    PretradeFilter,
    Verdict,
    get_filter_mode,
    should_skip_trade,
)


# ─── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_log_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / "agent_logs"
    monkeypatch.setattr(agent_config, "LOG_DIR", log_dir)

    def _daily(date_str: str | None = None) -> Path:
        if date_str is None:
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
        return log_dir / f"{date_str}_agent_calls.jsonl"

    monkeypatch.setattr(agent_config, "daily_log_path", _daily)
    return log_dir


@pytest.fixture
def have_keys(monkeypatch):
    monkeypatch.setattr(agent_config, "GOOGLE_API_KEY", "fake-google")
    monkeypatch.setattr(agent_config, "ANTHROPIC_API_KEY", "fake-anthropic")
    monkeypatch.setattr(agent_config, "DEGRADED", False)
    return True


@pytest.fixture
def reset_singleton():
    """Ensure module-level shared agent is rebuilt per test."""
    pretrade_filter._DEFAULT_AGENT = None
    yield
    pretrade_filter._DEFAULT_AGENT = None


class FakeAIClient(AIClient):
    """Replaces ask_gemini with a configurable behavior."""

    def __init__(self, *, behavior="clear", **kw):
        super().__init__(timeout_s=FILTER_TIMEOUT_S, max_attempts=1,
                         backoff_initial_s=0.0)
        self.behavior = behavior
        self.calls = 0

    async def ask_gemini(self, prompt, *, system="", model=None, default=None,
                        timeout_s=None, max_tokens=256, temperature=0.1):
        self.calls += 1
        if self.behavior == "clear":
            return json.dumps({"verdict": "CLEAR", "reason": "looks fine", "confidence": 80})
        if self.behavior == "caution":
            return json.dumps({"verdict": "CAUTION", "reason": "ATR elevated", "confidence": 60})
        if self.behavior == "sit_out":
            return json.dumps({"verdict": "SIT_OUT", "reason": "wrong regime", "confidence": 85})
        if self.behavior == "fenced":
            return "```json\n" + json.dumps({"verdict": "CLEAR", "reason": "ok", "confidence": 55}) + "\n```"
        if self.behavior == "junk":
            return "totally not json"
        if self.behavior == "timeout":
            # Sleep well past the filter timeout — outer guard must catch it.
            await asyncio.sleep(FILTER_TIMEOUT_S + 2.0)
            return json.dumps({"verdict": "CLEAR"})
        if self.behavior == "none":
            return default
        if self.behavior == "raise":
            raise RuntimeError("boom")
        raise AssertionError(f"unknown behavior {self.behavior!r}")


# ─── Sample signal/market fixtures ───────────────────────────────────────

SIGNAL = {
    "direction": "LONG",
    "strategy": "bias_momentum",
    "reason": "EMA stack + TF alignment",
    "confluences": ["ema_bullish", "tf_bias_3+"],
    "confidence": 72,
    "entry_score": 45,
    "stop_ticks": 40,
    "target_rr": 5.0,
}
MARKET = {"price": 18527.5, "vwap": 18520.0, "atr_5m": 12.5, "cvd": 340}
RECENT: list[dict] = []


# ─── Tests ───────────────────────────────────────────────────────────────

def test_verdict_enum_values():
    assert Verdict.CLEAR.value == "CLEAR"
    assert Verdict.CAUTION.value == "CAUTION"
    assert Verdict.SIT_OUT.value == "SIT_OUT"


def test_default_mode_is_advisory():
    # Sanity: strategies.py advisory-by-default backfill.
    assert get_filter_mode("bias_momentum") == "advisory"
    assert get_filter_mode("does_not_exist") == DEFAULT_FILTER_MODE


def test_clear_verdict_parsed(tmp_log_dir, have_keys, reset_singleton):
    agent = PretradeFilter(client=FakeAIClient(behavior="clear"))
    verdict = asyncio.run(agent.check(SIGNAL, MARKET, RECENT))
    assert verdict.verdict == "CLEAR"
    assert verdict.action == "CLEAR"  # legacy alias
    assert verdict.source == "ai"
    assert verdict.confidence == 80.0


def test_fenced_json_still_parses(tmp_log_dir, have_keys, reset_singleton):
    agent = PretradeFilter(client=FakeAIClient(behavior="fenced"))
    v = asyncio.run(agent.check(SIGNAL, MARKET, RECENT))
    assert v.verdict == "CLEAR"
    assert v.source == "ai"


def test_junk_response_defaults_clear(tmp_log_dir, have_keys, reset_singleton):
    agent = PretradeFilter(client=FakeAIClient(behavior="junk"))
    v = asyncio.run(agent.check(SIGNAL, MARKET, RECENT))
    assert v.verdict == DEFAULT_VERDICT == "CLEAR"
    assert v.source == "default"


def test_none_response_defaults_clear(tmp_log_dir, have_keys, reset_singleton):
    agent = PretradeFilter(client=FakeAIClient(behavior="none"))
    v = asyncio.run(agent.check(SIGNAL, MARKET, RECENT))
    assert v.verdict == "CLEAR"
    assert v.source == "default"


def test_raise_defaults_clear(tmp_log_dir, have_keys, reset_singleton):
    agent = PretradeFilter(client=FakeAIClient(behavior="raise"))
    v = asyncio.run(agent.check(SIGNAL, MARKET, RECENT))
    assert v.verdict == "CLEAR"
    assert v.source == "default"


def test_timeout_defaults_clear(tmp_log_dir, have_keys, reset_singleton):
    """Slow AI past the 3s budget → outer guard fires → default CLEAR."""
    # Use a tiny timeout so the test doesn't actually wait 3 seconds.
    agent = PretradeFilter(client=FakeAIClient(behavior="timeout"), timeout_s=0.05)
    v = asyncio.run(agent.check(SIGNAL, MARKET, RECENT))
    assert v.verdict == "CLEAR"
    assert v.source == "default"
    assert v.latency_ms < 3000  # guarded well before the 3s hard budget


def test_caution_logs_warning(tmp_log_dir, have_keys, reset_singleton, caplog):
    agent = PretradeFilter(client=FakeAIClient(behavior="caution"))
    import logging
    with caplog.at_level(logging.WARNING, logger="agents.pretrade_filter"):
        v = asyncio.run(agent.check(SIGNAL, MARKET, RECENT))
    assert v.verdict == "CAUTION"
    assert any("CAUTION" in rec.getMessage() for rec in caplog.records)


def test_advisory_mode_never_blocks(monkeypatch):
    from config import strategies as strat_cfg
    monkeypatch.setitem(strat_cfg.STRATEGIES["bias_momentum"],
                         "ai_filter_mode", "advisory")
    v = FilterVerdict(verdict="SIT_OUT", reason="x", confidence=90,
                      latency_ms=10.0, source="ai")
    assert should_skip_trade(v, "bias_momentum") is False


def test_blocking_mode_sit_out_skips(monkeypatch):
    from config import strategies as strat_cfg
    monkeypatch.setitem(strat_cfg.STRATEGIES["bias_momentum"],
                         "ai_filter_mode", "blocking")
    v = FilterVerdict(verdict="SIT_OUT", reason="x", confidence=90,
                      latency_ms=10.0, source="ai")
    assert should_skip_trade(v, "bias_momentum") is True


def test_blocking_mode_clear_does_not_skip(monkeypatch):
    from config import strategies as strat_cfg
    monkeypatch.setitem(strat_cfg.STRATEGIES["bias_momentum"],
                         "ai_filter_mode", "blocking")
    v = FilterVerdict(verdict="CLEAR", reason="ok", confidence=80,
                      latency_ms=10.0, source="ai")
    assert should_skip_trade(v, "bias_momentum") is False


def test_blocking_mode_caution_does_not_skip(monkeypatch):
    from config import strategies as strat_cfg
    monkeypatch.setitem(strat_cfg.STRATEGIES["bias_momentum"],
                         "ai_filter_mode", "blocking")
    v = FilterVerdict(verdict="CAUTION", reason="iffy", confidence=60,
                      latency_ms=10.0, source="ai")
    assert should_skip_trade(v, "bias_momentum") is False


def test_unknown_strategy_defaults_advisory():
    assert get_filter_mode("totally_fake_strategy") == "advisory"


def test_invalid_mode_coerces_to_default(monkeypatch):
    from config import strategies as strat_cfg
    monkeypatch.setitem(strat_cfg.STRATEGIES["bias_momentum"],
                         "ai_filter_mode", "bogus")
    assert get_filter_mode("bias_momentum") == "advisory"


def test_call_writes_jsonl_log(tmp_log_dir, have_keys, reset_singleton):
    """Every AI call writes exactly one JSONL line to the daily log."""
    # Route through the real ask_gemini machinery by stubbing the low-level
    # _gemini_once. The retry/log path then runs and writes to tmp_log_dir.
    async def _fake_once(self, prompt, *, system, model, timeout_s,
                         max_tokens, temperature):
        return json.dumps({"verdict": "CLEAR", "reason": "ok", "confidence": 90})

    import agents.base_agent as base_mod
    base_mod.AIClient._gemini_once = _fake_once  # type: ignore[assignment]

    client = AIClient(timeout_s=FILTER_TIMEOUT_S, max_attempts=1,
                      backoff_initial_s=0.0)
    agent = PretradeFilter(client=client)
    asyncio.run(agent.check(SIGNAL, MARKET, RECENT))

    log_path = agent_config.daily_log_path()
    assert log_path.exists(), f"call log not written to {log_path}"
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["outcome"] == "success"
    assert "latency_ms" in entry
    assert entry["model"]  # non-empty model id


def test_module_level_check_delegates(tmp_log_dir, have_keys, reset_singleton,
                                       monkeypatch):
    """Back-compat: pretrade_filter.check(...) uses the shared singleton."""
    fake = FakeAIClient(behavior="sit_out")
    pretrade_filter._DEFAULT_AGENT = PretradeFilter(client=fake)
    v = asyncio.run(pretrade_filter.check(SIGNAL, MARKET, RECENT, regime="X"))
    assert v.verdict == "SIT_OUT"
    assert v.action == "SIT_OUT"
    assert fake.calls == 1
