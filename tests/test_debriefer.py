"""S7 — 4C Session Debriefer tests.

Covers:
  - Happy path: FakeAIClient returns well-formed markdown with all five
    sections → file written to logs/ai_debrief/YYYY-MM-DD.md, Claude
    called exactly once, all sections present.
  - safe_call fallback path: FakeAIClient raises → deterministic
    fallback markdown written, still contains all five sections.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.base_agent import AIClient
from agents.session_debriefer import SessionDebriefer, _REQUIRED_SECTIONS


# ─── Fixtures ────────────────────────────────────────────────────────────

def _sample_events() -> list[dict]:
    """Minimal synthetic session: 1 winning long, 1 losing short."""
    return [
        {"event": "bar", "timeframe": "1m", "regime": "OPEN_MOMENTUM",
         "ts": "2026-04-21T08:30:00"},
        {"event": "bar", "timeframe": "5m", "regime": "OPEN_MOMENTUM",
         "ts": "2026-04-21T08:35:00"},
        {"event": "eval", "ts": "2026-04-21T08:35:10",
         "regime": "OPEN_MOMENTUM", "risk_blocked": False,
         "strategies": [{"name": "bias_momentum", "result": "SIGNAL",
                         "reason": "ok"}],
         "best_signal": {"strategy": "bias_momentum"}},
        {"event": "entry", "ts": "2026-04-21T08:35:15",
         "direction": "LONG", "strategy": "bias_momentum",
         "confluences": ["vwap_above", "cvd_positive"],
         "price": 20000.0, "stop_price": 19990.0, "target_price": 20020.0,
         "confidence": 0.75, "entry_score": 8, "tier": "A"},
        {"event": "exit", "ts": "2026-04-21T08:50:00",
         "direction": "LONG", "strategy": "bias_momentum",
         "entry_price": 20000.0, "exit_price": 20020.0,
         "pnl_dollars": 40.0, "pnl_ticks": 80, "duration_s": 885,
         "exit_reason": "target", "confluences": ["vwap_above"]},
        {"event": "entry", "ts": "2026-04-21T10:15:00",
         "direction": "SHORT", "strategy": "vwap_pullback",
         "confluences": ["vwap_reject"],
         "price": 20025.0, "stop_price": 20035.0, "target_price": 20005.0,
         "confidence": 0.55, "entry_score": 5, "tier": "B"},
        {"event": "exit", "ts": "2026-04-21T10:25:00",
         "direction": "SHORT", "strategy": "vwap_pullback",
         "entry_price": 20025.0, "exit_price": 20035.0,
         "pnl_dollars": -20.0, "pnl_ticks": -40, "duration_s": 600,
         "exit_reason": "stop", "confluences": ["vwap_reject"]},
        {"event": "session_summary", "trade_count": 2, "pnl_today": 20.0,
         "win_rate": 50, "consecutive_losses": 0, "recovery_mode": False},
    ]


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    """Redirect history + debrief dirs to tmp, seed a synthetic session."""
    hist_dir = tmp_path / "history"
    debrief_dir = tmp_path / "ai_debrief"
    tm_path = tmp_path / "trade_memory.json"
    hist_dir.mkdir()
    target = date(2026, 4, 21)
    hist_file = hist_dir / f"{target}_sim.jsonl"
    hist_file.write_text(
        "\n".join(json.dumps(e) for e in _sample_events()),
        encoding="utf-8",
    )
    tm_path.write_text(json.dumps({"trades": []}), encoding="utf-8")
    # Disable telegram via env
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return {
        "hist_dir": hist_dir,
        "debrief_dir": debrief_dir,
        "trade_memory_path": tm_path,
        "target": target,
    }


class FakeAIClient(AIClient):
    """Stand-in AIClient: records calls, returns scripted output."""

    def __init__(self, *, response: str | None = None,
                 raises: Exception | None = None):
        self.calls: list[dict] = []
        self._response = response
        self._raises = raises

    async def ask_claude(self, prompt, *, system="", model="",
                         default=None, max_tokens=1024, temperature=0.2, **_):
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        if self._raises is not None:
            raise self._raises
        return self._response if self._response is not None else default


# ─── Tests ───────────────────────────────────────────────────────────────

_GOOD_MD = """## Summary
Solid session — 2 trades, net +$20, 50% WR in OPEN_MOMENTUM.

## Wins
- 08:35 bias_momentum LONG +$40 — vwap_above + cvd_positive fired clean.

## Losses
- 10:15 vwap_pullback SHORT -$20 — stop hit, confluence thin.

## Patterns
- bias_momentum earned its keep; vwap_pullback fighting tape.

## Questions for Tomorrow
- Does vwap_pullback need a regime filter on chop days?
"""


def test_debriefer_happy_path_all_sections_written(session_env):
    """FakeAIClient returns well-formed 5-section markdown → file has them all."""
    fake = FakeAIClient(response=_GOOD_MD)
    agent = SessionDebriefer(
        client=fake,
        history_dir=session_env["hist_dir"],
        debrief_dir=session_env["debrief_dir"],
        trade_memory_path=session_env["trade_memory_path"],
    )
    out_path = asyncio.run(agent.run(
        target_date=session_env["target"],
        bot_name="sim",
        dispatch_telegram=False,
    ))

    # Claude called exactly once
    assert len(fake.calls) == 1
    # File is at logs/ai_debrief/YYYY-MM-DD.md
    assert out_path is not None
    p = Path(out_path)
    assert p.exists()
    assert p.name == f"{session_env['target']}.md"

    body = p.read_text(encoding="utf-8")
    for section in _REQUIRED_SECTIONS:
        assert section in body, f"missing section {section!r} in output"

    # Prompt carried the synthetic session data
    call = fake.calls[0]
    assert "bias_momentum" in call["prompt"]
    assert "vwap_pullback" in call["prompt"]


def test_debriefer_safe_call_fallback_on_client_exception(session_env):
    """If Claude call raises, safe_call catches → deterministic fallback md."""
    fake = FakeAIClient(raises=RuntimeError("simulated API outage"))
    agent = SessionDebriefer(
        client=fake,
        history_dir=session_env["hist_dir"],
        debrief_dir=session_env["debrief_dir"],
        trade_memory_path=session_env["trade_memory_path"],
    )
    out_path = asyncio.run(agent.run(
        target_date=session_env["target"],
        bot_name="sim",
        dispatch_telegram=False,
    ))
    assert out_path is not None
    body = Path(out_path).read_text(encoding="utf-8")
    # Fallback still produces all five required sections
    for section in _REQUIRED_SECTIONS:
        assert section in body
    # Tagged as fallback in header
    assert "fallback" in body.splitlines()[0].lower()


def test_debriefer_no_history_returns_none(session_env):
    """Missing history file → returns None, no file written."""
    fake = FakeAIClient(response=_GOOD_MD)
    agent = SessionDebriefer(
        client=fake,
        history_dir=session_env["hist_dir"],
        debrief_dir=session_env["debrief_dir"],
        trade_memory_path=session_env["trade_memory_path"],
    )
    out = asyncio.run(agent.run(
        target_date=date(2020, 1, 1),  # no file for this date
        bot_name="sim",
        dispatch_telegram=False,
    ))
    assert out is None
    assert len(fake.calls) == 0
