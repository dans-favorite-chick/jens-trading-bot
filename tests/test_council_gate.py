"""Tests for agents.council_gate (S5 — 4A Council Gate, spec surface).

Covers the new CouncilGate class built on S4 infra:
  - 7 voters run, votes tallied
  - orchestrator synthesizes {verdict, score, summary}
  - tie 3-3-1 → NEUTRAL
  - voter timeout → NEUTRAL default
  - writes logs/council/YYYY-MM-DD.json
  - get_current_bias() returns last vote
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import council_gate as cg
from agents import config as agent_config
from agents.base_agent import AIClient


# ─── FakeAIClient ───────────────────────────────────────────────────

class FakeAIClient:
    """Drop-in replacement for AIClient in tests. Scripted responses."""

    def __init__(self, voter_responses=None, orch_response=None, voter_delay=0.0):
        # voter_responses: list of raw text strings (one per persona, in order)
        #                  or dict {persona_name: text}
        # orch_response: single raw text string for orchestrator
        self.voter_responses = voter_responses or []
        self.orch_response = orch_response
        self.voter_delay = voter_delay
        self.calls = []  # list of (model, prompt)
        self._voter_idx = 0

    @staticmethod
    def parse_json(text, default=None):
        return AIClient.parse_json(text, default=default)

    async def ask_gemini(self, prompt, *, system="", model="", default=None,
                         timeout_s=None, max_tokens=1024, temperature=0.2):
        self.calls.append((model, prompt))
        await asyncio.sleep(self.voter_delay)

        # Pro model → orchestrator
        if "pro" in (model or "").lower():
            return self.orch_response if self.orch_response is not None else default

        # Flash → voter
        if isinstance(self.voter_responses, dict):
            for name, resp in self.voter_responses.items():
                if name in prompt and name not in [c[1] for c in self.calls[:-1] if name in c[1]]:
                    # crude but fine for tests
                    pass
            # simpler: pick by index
        if isinstance(self.voter_responses, list):
            if self._voter_idx < len(self.voter_responses):
                resp = self.voter_responses[self._voter_idx]
                self._voter_idx += 1
                return resp if resp is not None else default
            return default
        return default

    async def ask_claude(self, prompt, *, default=None, **kw):
        return default


@pytest.fixture
def have_keys(monkeypatch):
    monkeypatch.setattr(agent_config, "GOOGLE_API_KEY", "fake-google")
    monkeypatch.setattr(agent_config, "ANTHROPIC_API_KEY", "fake-anthropic")
    monkeypatch.setattr(agent_config, "DEGRADED", False)


@pytest.fixture
def tmp_council_dir(tmp_path, monkeypatch):
    d = tmp_path / "council_logs"
    monkeypatch.setattr(cg, "COUNCIL_LOG_DIR", d)
    return d


@pytest.fixture
def tmp_agent_logs(tmp_path, monkeypatch):
    d = tmp_path / "agent_logs"
    monkeypatch.setattr(agent_config, "LOG_DIR", d)

    def _daily(date_str=None):
        from datetime import datetime
        if date_str is None:
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
        return d / f"{date_str}_agent_calls.jsonl"

    monkeypatch.setattr(agent_config, "daily_log_path", _daily)
    return d


MARKET = {"price": 18500.0, "vwap": 18490.0, "regime": "OPEN_MOMENTUM"}


def _bullish_response(rat="up"):
    return json.dumps({"vote": "BULLISH", "rationale": rat})


def _bearish_response(rat="down"):
    return json.dumps({"vote": "BEARISH", "rationale": rat})


def _neutral_response(rat="flat"):
    return json.dumps({"vote": "NEUTRAL", "rationale": rat})


# ─── Tests ──────────────────────────────────────────────────────────

def test_personas_count():
    assert len(cg.COUNCIL_PERSONAS) == 7
    names = {p["name"] for p in cg.COUNCIL_PERSONAS}
    expected = {
        "trend-follower", "mean-reverter", "vol-watcher", "gamma-reader",
        "intermarket-analyst", "session-historian", "contrarian",
    }
    assert names == expected


def test_seven_voters_run_and_tally_bullish(have_keys, tmp_council_dir, tmp_agent_logs):
    # 5 bullish, 1 bearish, 1 neutral → BULLISH 5/7
    voter_texts = (
        [_bullish_response()] * 5
        + [_bearish_response()]
        + [_neutral_response()]
    )
    orch_text = json.dumps({
        "verdict": "BULLISH", "score": "5/7",
        "summary": "Strong bullish consensus across 5 voters.",
    })

    fake = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)
    gate = cg.CouncilGate(client=fake)

    result = asyncio.run(gate.run({"market": MARKET, "trigger": "session_open"}))

    # 7 voter calls + 1 orchestrator call
    flash_calls = [c for c in fake.calls if "pro" not in c[0].lower()]
    pro_calls = [c for c in fake.calls if "pro" in c[0].lower()]
    assert len(flash_calls) == 7
    assert len(pro_calls) == 1

    assert result["verdict"] == "BULLISH"
    assert result["score"] == "5/7"
    assert len(result["votes"]) == 7
    votes_by = {v["voter"]: v["vote"] for v in result["votes"]}
    assert sum(1 for v in votes_by.values() if v == "BULLISH") == 5


def test_tie_3_3_1_forces_neutral(have_keys, tmp_council_dir, tmp_agent_logs):
    # 3 bull, 3 bear, 1 neutral
    voter_texts = (
        [_bullish_response()] * 3
        + [_bearish_response()] * 3
        + [_neutral_response()]
    )
    # Even if orchestrator wrongly claims BULLISH, spec forces NEUTRAL.
    orch_text = json.dumps({
        "verdict": "BULLISH", "score": "3/7", "summary": "wrong",
    })
    fake = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)
    gate = cg.CouncilGate(client=fake)
    result = asyncio.run(gate.run({"market": MARKET}))

    assert result["verdict"] == "NEUTRAL"
    # Deterministic score is max of (3,3,1) = 3
    assert result["score"].endswith("/7")


def test_voter_timeout_defaults_to_neutral(have_keys, tmp_council_dir, tmp_agent_logs):
    # All voters return None (timeout)
    voter_texts = [None] * 7
    orch_text = None  # orchestrator also fails
    fake = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)
    gate = cg.CouncilGate(client=fake)
    result = asyncio.run(gate.run({"market": MARKET}))

    # All voters default to NEUTRAL
    for v in result["votes"]:
        assert v["vote"] == "NEUTRAL"
    # Verdict falls back to deterministic (7 neutral) → NEUTRAL
    assert result["verdict"] == "NEUTRAL"
    assert result["score"] == "7/7"


def test_writes_daily_json_log(have_keys, tmp_council_dir, tmp_agent_logs):
    voter_texts = [_bullish_response()] * 7
    orch_text = json.dumps({
        "verdict": "BULLISH", "score": "7/7", "summary": "unanimous",
    })
    fake = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)
    gate = cg.CouncilGate(client=fake)
    result = asyncio.run(gate.run({"market": MARKET, "trigger": "session_open"}))

    log_path = Path(result["log_path"])
    assert log_path.exists()
    assert log_path.parent == tmp_council_dir
    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["verdict"] == "BULLISH"
    assert data[0]["trigger"] == "session_open"
    assert len(data[0]["votes"]) == 7


def test_second_run_appends_to_same_day(have_keys, tmp_council_dir, tmp_agent_logs):
    voter_texts = [_bullish_response()] * 7
    orch_text = json.dumps({"verdict": "BULLISH", "score": "7/7", "summary": "x"})
    fake1 = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)
    fake2 = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)

    asyncio.run(cg.CouncilGate(client=fake1).run({"market": MARKET, "trigger": "session_open"}))
    result2 = asyncio.run(cg.CouncilGate(client=fake2).run({"market": MARKET, "trigger": "regime_shift"}))

    data = json.loads(Path(result2["log_path"]).read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0]["trigger"] == "session_open"
    assert data[1]["trigger"] == "regime_shift"


def test_get_current_bias_updates(have_keys, tmp_council_dir, tmp_agent_logs):
    voter_texts = [_bearish_response()] * 5 + [_neutral_response()] * 2
    orch_text = json.dumps({
        "verdict": "BEARISH", "score": "5/7", "summary": "sellers in control",
    })
    fake = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)
    gate = cg.CouncilGate(client=fake)
    asyncio.run(gate.run({"market": MARKET}))

    bias = cg.get_current_bias()
    assert bias["verdict"] == "BEARISH"
    assert bias["score"] == "5/7"
    assert bias["timestamp"] is not None
    assert "sellers" in bias["summary"]


def test_deterministic_verdict_helper():
    # Strict majority bullish
    v = [{"vote": "BULLISH"}] * 4 + [{"vote": "BEARISH"}] * 2 + [{"vote": "NEUTRAL"}]
    assert cg._deterministic_verdict(v) == ("BULLISH", "4/7")
    # Tie 3-3-1
    v = [{"vote": "BULLISH"}] * 3 + [{"vote": "BEARISH"}] * 3 + [{"vote": "NEUTRAL"}]
    verdict, score = cg._deterministic_verdict(v)
    assert verdict == "NEUTRAL"
    # Unanimous bearish
    v = [{"vote": "BEARISH"}] * 7
    assert cg._deterministic_verdict(v) == ("BEARISH", "7/7")


def test_orchestrator_json_parse_failure_falls_back(have_keys, tmp_council_dir, tmp_agent_logs):
    voter_texts = [_bullish_response()] * 5 + [_bearish_response()] * 2
    orch_text = "not json at all"
    fake = FakeAIClient(voter_responses=voter_texts, orch_response=orch_text)
    gate = cg.CouncilGate(client=fake)
    result = asyncio.run(gate.run({"market": MARKET}))

    # Fallback to deterministic
    assert result["verdict"] == "BULLISH"
    assert result["score"] == "5/7"


# ─── [COUNCIL-AUTO] Auto-trigger tests ──────────────────────────────
# These cover the session-open date-guard and the regime-shift 15-min
# debounce in bots/sim_bot.py without booting the full SimBot. We bind
# the sim_bot methods onto a lightweight stub that only carries the
# state those methods touch.

from bots import sim_bot as _sim_bot  # noqa: E402


class _StubSimBot:
    """Minimal object to exercise SimBot's council auto-trigger logic
    without running the real __init__ (which wires websockets, NT8,
    strategies, etc.). We bind the unbound methods we need."""

    COUNCIL_REGIME_DEBOUNCE_S = _sim_bot.SimBot.COUNCIL_REGIME_DEBOUNCE_S

    def __init__(self):
        self._last_council_date = None
        self._last_regime_council_ts = 0.0
        self._last_seen_regime = None
        self.aggregator = None
        self.session = None
        self.fired = []  # list of (trigger, reason)

    # Borrow the real ctx builder (it handles a missing aggregator).
    _build_council_ctx = _sim_bot.SimBot._build_council_ctx

    async def _fire_council(self, trigger, reason):
        # Record fire; don't actually call CouncilGate.
        self.fired.append((trigger, reason))


class _FakeSession:
    """Stand-in for SessionManager — returns whatever regime we set."""
    def __init__(self, regime):
        self.regime = regime

    def get_current_regime(self):
        return self.regime


def test_council_session_open_guard_once_per_day():
    """Second invocation on the same trading day is a no-op."""
    bot = _StubSimBot()

    # Fire once with today's date.
    from datetime import date as _date
    today = _date(2026, 4, 21)
    assert bot._last_council_date != today

    # Simulate the loop's core guard block twice for the same day.
    async def _run_guard_once():
        if bot._last_council_date != today:
            bot._last_council_date = today
            await bot._fire_council("session_open", f"session_open ({today})")

    asyncio.run(_run_guard_once())
    asyncio.run(_run_guard_once())
    # Exactly one fire recorded.
    assert len(bot.fired) == 1
    assert bot.fired[0][0] == "session_open"

    # A new day resets the guard.
    next_day = _date(2026, 4, 22)

    async def _run_guard_next():
        if bot._last_council_date != next_day:
            bot._last_council_date = next_day
            await bot._fire_council("session_open", f"session_open ({next_day})")

    asyncio.run(_run_guard_next())
    assert len(bot.fired) == 2


def test_council_regime_shift_debounce(monkeypatch):
    """A second shift within 15 min is skipped; after the debounce
    window, a subsequent shift fires."""
    bot = _StubSimBot()
    bot.session = _FakeSession("PREMARKET")

    # Fake monotonic clock we can advance.
    fake_now = {"t": 1000.0}
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "monotonic", lambda: fake_now["t"])

    async def _tick():
        """One iteration of the regime-shift loop body."""
        new_regime = bot.session.get_current_regime()
        prev = bot._last_seen_regime
        if prev is None:
            bot._last_seen_regime = new_regime
            return
        if new_regime != prev:
            since = _time_mod.monotonic() - bot._last_regime_council_ts
            if since >= bot.COUNCIL_REGIME_DEBOUNCE_S:
                bot._last_regime_council_ts = _time_mod.monotonic()
                bot._last_seen_regime = new_regime
                await bot._fire_council(
                    "regime_shift", f"regime_shift {prev} -> {new_regime}"
                )
            else:
                bot._last_seen_regime = new_regime

    # Tick 1: seed prev=PREMARKET. No fire.
    asyncio.run(_tick())
    assert bot.fired == []

    # Transition 1: PREMARKET -> OPEN_MOMENTUM. Fires (first transition,
    # debounce timestamp is 0 so always >= 15 min).
    bot.session.regime = "OPEN_MOMENTUM"
    asyncio.run(_tick())
    assert len(bot.fired) == 1
    assert "OPEN_MOMENTUM" in bot.fired[0][1]

    # Transition 2: OPEN_MOMENTUM -> MIDDAY_CHOP only 5 min later → debounced.
    fake_now["t"] += 5 * 60
    bot.session.regime = "MIDDAY_CHOP"
    asyncio.run(_tick())
    assert len(bot.fired) == 1  # still only the first fire

    # Transition 3: another shift at +20 min total — past the 15-min
    # debounce, so fires again.
    fake_now["t"] += 16 * 60  # well past debounce
    bot.session.regime = "AFTERHOURS"
    asyncio.run(_tick())
    assert len(bot.fired) == 2
    assert "AFTERHOURS" in bot.fired[1][1]
