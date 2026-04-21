"""Tests for agents.historical_learner (S8).

Uses a FakeAIClient that returns a canned JSON payload so we never hit the
real Claude API. Synthetic trade_memory + history JSONL written into
tmp_path. Asserts:
  - Aggregates computed correctly (WR per regime, PF per hour, confluence).
  - Markdown + JSON both written.
  - Recommendation schema is valid (all required fields).
  - `ai_learner/pending_recommendations.json` is the structured S9 input.
  - Graceful degradation when Claude is unavailable (empty recs, no crash).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import config as agent_config
from agents import historical_learner as hl
from agents.historical_learner import (
    HistoricalLearnerAgent,
    REQUIRED_FIELDS,
    compute_aggregates,
    load_history_events,
    load_trade_memory,
    summarize_history_events,
    _validate_recommendations,
    _trade_hour_ct,
)


# ─── Fakes ──────────────────────────────────────────────────────────────

class FakeAIClient:
    """Stand-in for AIClient. Returns canned text for ask_claude."""

    def __init__(self, response_text: str = "", *, raise_exc: bool = False) -> None:
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.calls = 0
        self.last_prompt = None
        self.last_system = None

    async def ask_claude(self, prompt, *, system="", default=None,
                         max_tokens=1024, temperature=0.2, **_):
        self.calls += 1
        self.last_prompt = prompt
        self.last_system = system
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.response_text or default

    async def ask_gemini(self, *a, **kw):  # unused, kept for interface parity
        return kw.get("default")


CANNED = {
    "recommendations": [
        {
            "strategy": "bias_momentum",
            "param": "min_momentum",
            "current": 70,
            "proposed": 80,
            "rationale": "bias_momentum WR at 70 is 33%; sample n=6.",
            "expected_impact": "WR climbs to ~60% at cost of ~40% trades.",
        },
        {
            "strategy": "vwap_pullback",
            "param": "allowed_regimes",
            "current": ["OPEN_MOMENTUM", "LUNCH_CHOP"],
            "proposed": ["OPEN_MOMENTUM"],
            "rationale": "LUNCH_CHOP PF = 0.4 over 3 trades.",
            "expected_impact": "Eliminates the worst-PF bucket.",
        },
        {
            "strategy": "global",
            "param": "min_confluences",
            "current": 2,
            "proposed": 3,
            "rationale": "Trades with 3+ confluences show 70% WR vs 40% at 2.",
            "expected_impact": "Fewer trades, higher quality.",
        },
    ]
}


# ─── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def have_keys(monkeypatch):
    monkeypatch.setattr(agent_config, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(agent_config, "GOOGLE_API_KEY", "fake")
    monkeypatch.setattr(agent_config, "DEGRADED", False)


@pytest.fixture
def synthetic(tmp_path: Path):
    """Write synthetic trade_memory + history into tmp_path, return paths."""
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    out_dir = tmp_path / "ai_learner"
    tmem = tmp_path / "trade_memory.json"

    today = date(2026, 4, 21)

    # Fabricate 10 trades across 3 strategies + 2 regimes + different hours/confluences
    def _t(i, strategy, regime, pnl, hour_ct, confluences):
        # Build epoch so CT hour bucket matches target.
        # CT hour h  =>  UTC hour (h + 6) % 24
        utc_h = (hour_ct + 6) % 24
        dt = datetime(today.year, today.month, today.day - (i % 3),
                      utc_h, 0, 0, tzinfo=timezone.utc)
        return {
            "trade_id": f"t{i}",
            "strategy": strategy,
            "regime": regime,
            "pnl_dollars": pnl,
            "entry_time": dt.timestamp(),
            "confluences": confluences,
        }

    trades = [
        _t(0, "bias_momentum",  "OPEN_MOMENTUM",  15.0, 9,  ["vwap", "ema"]),
        _t(1, "bias_momentum",  "OPEN_MOMENTUM",  12.0, 9,  ["vwap", "ema", "dom"]),
        _t(2, "bias_momentum",  "LUNCH_CHOP",    -10.0, 12, ["vwap"]),
        _t(3, "bias_momentum",  "LUNCH_CHOP",    -12.0, 12, []),
        _t(4, "vwap_pullback",  "OPEN_MOMENTUM",  20.0, 9,  ["vwap", "ema"]),
        _t(5, "vwap_pullback",  "OPEN_MOMENTUM",  -8.0, 10, ["vwap"]),
        _t(6, "vwap_pullback",  "LUNCH_CHOP",    -15.0, 12, []),
        _t(7, "spring_setup",   "OPEN_MOMENTUM",  25.0, 9,  ["vwap", "ema", "dom"]),
        _t(8, "spring_setup",   "OPEN_MOMENTUM",  18.0, 10, ["vwap", "ema"]),
        _t(9, "spring_setup",   "LUNCH_CHOP",    -20.0, 13, ["vwap"]),
    ]
    tmem.write_text(json.dumps(trades), encoding="utf-8")

    # history jsonl — a couple of bars + eval events for recent days
    for i in range(3):
        d = today - timedelta(days=i)
        path = history_dir / f"{d}_prod.jsonl"
        lines = [
            json.dumps({"event": "bar", "ts": f"{d}T09:00:00",
                        "regime": "OPEN_MOMENTUM"}),
            json.dumps({"event": "bar", "ts": f"{d}T12:00:00",
                        "regime": "LUNCH_CHOP"}),
            json.dumps({"event": "eval", "strategies": [
                {"name": "bias_momentum", "result": "SIGNAL"},
                {"name": "vwap_pullback", "result": "SKIP"},
                {"name": "spring_setup",  "result": "BLOCKED"},
            ]}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "today": today,
        "history_dir": history_dir,
        "trade_memory": tmem,
        "out_dir": out_dir,
        "trades": trades,
    }


# ─── Unit tests — pure functions ────────────────────────────────────────

def test_trade_hour_ct_from_epoch():
    # 2026-04-21 15:00 UTC → 09 CT
    dt = datetime(2026, 4, 21, 15, 0, 0, tzinfo=timezone.utc)
    assert _trade_hour_ct({"entry_time": dt.timestamp()}) == 9


def test_trade_hour_ct_from_iso():
    assert _trade_hour_ct({"ts": "2026-04-21T15:00:00+00:00"}) == 9


def test_trade_hour_ct_fallback_neg1():
    assert _trade_hour_ct({}) == -1


def test_load_trade_memory_missing(tmp_path):
    assert load_trade_memory(tmp_path / "nope.json") == []


def test_compute_aggregates_empty():
    out = compute_aggregates([])
    assert out["total_trades"] == 0
    assert out["strategies"] == {}


def test_compute_aggregates_per_strategy(synthetic):
    trades = synthetic["trades"]
    agg = compute_aggregates(trades)

    assert agg["total_trades"] == 10
    strats = agg["strategies"]
    assert set(strats.keys()) == {"bias_momentum", "vwap_pullback", "spring_setup"}

    # bias_momentum: 2 wins / 4 trades = 50% WR; PF = 27 / 22
    bm = strats["bias_momentum"]
    assert bm["n_trades"] == 4
    assert bm["wr_overall"] == 50.0
    assert abs(bm["pf_overall"] - round(27 / 22, 3)) < 1e-6

    # Regime split: OPEN_MOMENTUM is 100% WR for bias_momentum
    assert bm["by_regime"]["OPEN_MOMENTUM"]["wr"] == 100.0
    assert bm["by_regime"]["LUNCH_CHOP"]["wr"] == 0.0

    # Hour CT buckets produced as string keys
    assert "9" in bm["by_hour_ct"]
    assert "12" in bm["by_hour_ct"]

    # Confluence effectiveness indexed by count
    assert "0" in bm["by_confluence_count"]  # the empty-confluence loss
    assert bm["by_confluence_count"]["0"]["wr"] == 0.0


def test_summarize_history_events(synthetic):
    events = load_history_events(synthetic["history_dir"], days=14,
                                 today=synthetic["today"])
    summ = summarize_history_events(events)
    assert summ["total_events"] >= 9  # 3 days * 3 lines
    assert summ["signals_generated"] >= 3
    assert summ["signals_blocked"] >= 3
    assert summ["regime_bar_counts"].get("OPEN_MOMENTUM", 0) >= 3


# ─── Recommendation validation ──────────────────────────────────────────

def test_validate_recommendations_happy_path():
    recs = _validate_recommendations(CANNED)
    assert len(recs) == 3
    for r in recs:
        assert set(REQUIRED_FIELDS).issubset(r.keys())


def test_validate_recommendations_drops_bad_entries():
    bad = {"recommendations": [
        {"strategy": "x"},  # missing fields → dropped
        CANNED["recommendations"][0],
    ]}
    out = _validate_recommendations(bad)
    assert len(out) == 1


def test_validate_recommendations_non_dict():
    assert _validate_recommendations("garbage") == []
    assert _validate_recommendations(None) == []


# ─── Integration — full agent run with FakeAIClient ─────────────────────

def test_agent_writes_md_and_json(synthetic, have_keys):
    fake = FakeAIClient(response_text=json.dumps(CANNED))
    agent = HistoricalLearnerAgent(
        client=fake,
        days=14,
        history_dir=synthetic["history_dir"],
        trade_memory_path=synthetic["trade_memory"],
        out_dir=synthetic["out_dir"],
    )
    result = asyncio.run(agent.run({"today": synthetic["today"]}))

    assert result.md_path.exists()
    assert result.json_path.exists()
    assert result.json_path.name == "pending_recommendations.json"
    assert result.md_path.name == f"weekly_{synthetic['today'].isoformat()}.md"

    # JSON content matches S9 contract
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert "recommendations" in payload
    assert payload["days"] == 14
    assert payload["n_trades"] == 10
    assert len(payload["recommendations"]) == 3
    for r in payload["recommendations"]:
        assert set(REQUIRED_FIELDS).issubset(r.keys())

    # Markdown includes the summary + recommendations
    md = result.md_path.read_text(encoding="utf-8")
    assert "Phoenix Weekly Learner" in md
    assert "bias_momentum" in md
    assert "min_momentum" in md

    assert fake.calls == 1


def test_agent_degrades_when_ai_returns_none(synthetic, have_keys):
    fake = FakeAIClient(response_text="")  # empty → parse_json returns None
    agent = HistoricalLearnerAgent(
        client=fake,
        days=14,
        history_dir=synthetic["history_dir"],
        trade_memory_path=synthetic["trade_memory"],
        out_dir=synthetic["out_dir"],
    )
    result = asyncio.run(agent.run({"today": synthetic["today"]}))

    assert result.recommendations == []
    # JSON still written with empty recs list
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["recommendations"] == []
    # Markdown still produced
    md = result.md_path.read_text(encoding="utf-8")
    assert "No recommendations" in md


def test_agent_handles_ai_exception(synthetic, have_keys):
    fake = FakeAIClient(raise_exc=True)
    agent = HistoricalLearnerAgent(
        client=fake,
        days=14,
        history_dir=synthetic["history_dir"],
        trade_memory_path=synthetic["trade_memory"],
        out_dir=synthetic["out_dir"],
    )
    result = asyncio.run(agent.run({"today": synthetic["today"]}))
    assert result.recommendations == []
    assert result.md_path.exists()
    assert result.json_path.exists()


def test_agent_skips_ai_when_no_claude_key(monkeypatch, synthetic):
    # Simulate missing key — agent must not call ask_claude and still write files.
    monkeypatch.setattr(agent_config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(agent_config, "DEGRADED", True)

    fake = FakeAIClient(response_text=json.dumps(CANNED))
    agent = HistoricalLearnerAgent(
        client=fake,
        days=14,
        history_dir=synthetic["history_dir"],
        trade_memory_path=synthetic["trade_memory"],
        out_dir=synthetic["out_dir"],
    )
    result = asyncio.run(agent.run({"today": synthetic["today"]}))

    assert fake.calls == 0
    assert result.recommendations == []
    assert result.md_path.exists()
    assert result.json_path.exists()


def test_prompt_includes_aggregates_and_window(synthetic, have_keys):
    fake = FakeAIClient(response_text=json.dumps(CANNED))
    agent = HistoricalLearnerAgent(
        client=fake, days=7,
        history_dir=synthetic["history_dir"],
        trade_memory_path=synthetic["trade_memory"],
        out_dir=synthetic["out_dir"],
    )
    asyncio.run(agent.run({"today": synthetic["today"]}))

    assert "bias_momentum" in fake.last_prompt
    assert "2026-04-21" in fake.last_prompt  # window_end
    assert "aggregates" in fake.last_prompt
    assert fake.last_system  # template loaded
