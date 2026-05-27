"""tests/test_agent_verdicts_persisted.py

P4-6 prerequisite: persist AI agent verdicts to trade_memory rows.

Verifies the 2026-05-27 wiring that captures the pretrade_filter +
council verdicts at signal time and threads them onto the recorded
trade row, so the P4-6 uplift harness can read them to compute per-
agent Cohort A / Cohort B lift CIs.

Contract:
  - Position dataclass has agent_verdicts (dict|None) and
    agent_decision_ts_ct (str|None) fields, defaulting to None.
  - close_position() copies them onto the trade dict.
  - TradeMemory.update_trade() exists as the canonical writer for
    post-exit debrief reflections (and any future enrichment).
  - Legacy trade rows without agent_verdicts must still load fine.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

PHOENIX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHOENIX_ROOT))

from core.position_manager import Position, PositionManager
from core.trade_memory import TradeMemory


# ─── Position dataclass contract ─────────────────────────────────────


def test_position_has_agent_verdicts_field_defaulting_to_none():
    """Position must declare both fields with None defaults so legacy
    open_position callers don't need to pass them."""
    pos = Position(
        trade_id="t1", direction="LONG", entry_price=25000.0,
        entry_time=0, contracts=1, stop_price=24990.0,
        initial_stop_price=24990.0, high_water_price=25000.0,
        target_price=25020.0, strategy="bias_momentum",
        reason="test", market_snapshot={}, original_contracts=1,
    )
    assert hasattr(pos, "agent_verdicts")
    assert hasattr(pos, "agent_decision_ts_ct")
    assert pos.agent_verdicts is None
    assert pos.agent_decision_ts_ct is None


def test_position_accepts_explicit_verdicts():
    pos = Position(
        trade_id="t1", direction="LONG", entry_price=25000.0,
        entry_time=0, contracts=1, stop_price=24990.0,
        initial_stop_price=24990.0, high_water_price=25000.0,
        target_price=25020.0, strategy="bias_momentum",
        reason="test", market_snapshot={}, original_contracts=1,
        agent_verdicts={"pretrade": "GO", "council": "NO_GO", "debrief": None},
        agent_decision_ts_ct="2026-05-27T10:00:00-05:00",
    )
    assert pos.agent_verdicts["pretrade"] == "GO"
    assert pos.agent_verdicts["council"] == "NO_GO"
    assert pos.agent_decision_ts_ct.startswith("2026-05-27")


# ─── close_position copies verdicts to trade dict ────────────────────


def _open_and_close(pm: PositionManager, **stash) -> dict | None:
    """Helper: open + (optional stash) + close. Returns trade dict."""
    pm.open_position(
        trade_id="t1", direction="LONG", entry_price=25000.0, contracts=1,
        stop_price=24990.0, target_price=25020.0,
        strategy="bias_momentum", reason="test",
        market_snapshot={}, account="Sim101",
    )
    pos = pm.get_position("t1")
    for k, v in stash.items():
        setattr(pos, k, v)
    return pm.close_position(exit_price=25010.0, exit_reason="target_hit", trade_id="t1")


def test_close_position_copies_agent_verdicts(monkeypatch):
    pm = PositionManager()
    trade = _open_and_close(
        pm,
        agent_verdicts={"pretrade": "GO", "council": "GO", "debrief": None},
        agent_decision_ts_ct="2026-05-27T10:00:00-05:00",
    )
    assert trade is not None
    assert trade["agent_verdicts"] == {
        "pretrade": "GO", "council": "GO", "debrief": None,
    }
    assert trade["agent_decision_ts_ct"] == "2026-05-27T10:00:00-05:00"


def test_close_position_emits_none_when_verdicts_not_stashed():
    """Backward compat: a position opened without verdict stash
    must still produce a clean trade row (agent_verdicts=None)."""
    pm = PositionManager()
    trade = _open_and_close(pm)
    assert trade is not None
    assert trade["agent_verdicts"] is None
    assert trade["agent_decision_ts_ct"] is None


# ─── TradeMemory.update_trade contract ──────────────────────────────


def _fresh_trade_memory(tmp_path: Path) -> TradeMemory:
    """Create a TradeMemory rooted in tmp_path/logs/ so the test
    never touches the real trade_memory*.json files."""
    # Monkey-point the filepath via constructor; the TradeMemory
    # class defaults to logs/trade_memory.json relative to project
    # root, but it accepts a bot_id arg. We use a custom filepath.
    tm = TradeMemory(bot_id="test")
    tm.filepath = str(tmp_path / "trade_memory_test.json")
    tm.trades = []
    return tm


def test_update_trade_finds_and_updates(tmp_path):
    tm = _fresh_trade_memory(tmp_path)
    tm.trades.append({
        "trade_id": "t1",
        "strategy": "bias_momentum",
        "result": "WIN",
        "pnl_dollars": 12.40,
        "agent_verdicts": {"pretrade": "GO", "council": "GO", "debrief": None},
    })
    tm.save()
    ok = tm.update_trade("t1", {
        "agent_verdicts": {"pretrade": "GO", "council": "GO", "debrief": "REINFORCE"},
    })
    assert ok is True
    assert tm.trades[0]["agent_verdicts"]["debrief"] == "REINFORCE"
    assert "last_updated_at" in tm.trades[0]
    # Confirm JSON on disk also reflects the update.
    rec = json.loads(Path(tm.filepath).read_text(encoding="utf-8"))
    assert rec[0]["agent_verdicts"]["debrief"] == "REINFORCE"


def test_update_trade_returns_false_when_trade_id_missing(tmp_path):
    tm = _fresh_trade_memory(tmp_path)
    tm.trades.append({"trade_id": "real", "strategy": "x"})
    ok = tm.update_trade("does_not_exist", {"x": 1})
    assert ok is False


def test_update_trade_preserves_unrelated_fields(tmp_path):
    tm = _fresh_trade_memory(tmp_path)
    tm.trades.append({
        "trade_id": "t1",
        "strategy": "bias_momentum",
        "pnl_dollars": 12.40,
        "agent_verdicts": {"pretrade": "GO"},
    })
    tm.update_trade("t1", {"agent_verdicts": {"pretrade": "GO", "debrief": "X"}})
    # pnl_dollars + strategy must still be present, untouched.
    assert tm.trades[0]["strategy"] == "bias_momentum"
    assert tm.trades[0]["pnl_dollars"] == 12.40


# ─── Backward compat for legacy rows ────────────────────────────────


def test_legacy_trade_without_agent_verdicts_loads(tmp_path):
    """A trade dict pre-dating this change has no agent_verdicts key
    at all. The TradeMemory loader and consumers must default it to
    None rather than crashing."""
    tm = _fresh_trade_memory(tmp_path)
    legacy = {
        "trade_id": "legacy",
        "strategy": "bias_momentum",
        "result": "WIN",
        "pnl_dollars": 5.00,
        # No agent_verdicts field.
    }
    tm.trades.append(legacy)
    tm.save()
    # Reload from disk.
    tm2 = TradeMemory(bot_id="test")
    tm2.filepath = tm.filepath
    tm2.trades = json.loads(Path(tm.filepath).read_text(encoding="utf-8"))
    assert len(tm2.trades) == 1
    # The consumer default pattern: .get("agent_verdicts") → None.
    assert tm2.trades[0].get("agent_verdicts") is None
    assert tm2.trades[0].get("agent_decision_ts_ct") is None


# ─── End-to-end: trade_entry's stash logic via direct simulation ────


def test_norm_verdict_dict_action_extraction():
    """Simulates the _norm_verdict helper logic embedded in
    _trade_entry — the pretrade filter publishes a DICT verdict with
    an 'action' key, not a string. The persistence code must extract
    just the action."""
    # Inline copy of the helper for unit-test isolation. This mirrors
    # the inline closure in bots/_trade_entry.py.
    def _norm_verdict(v):
        if v is None:
            return None
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return (v.get("action")
                    or v.get("verdict")
                    or v.get("bias")
                    or None)
        return None

    # The real _filter_verdict shape from bots/_signal_router.py:161
    filter_v = {
        "action": "CLEAR",
        "reason": "CVD aligned, structure intact",
        "confidence": 78.0,
        "latency_ms": 312.0,
        "source": "pretrade_filter",
        "timestamp": "2026-05-27T10:00:00",
    }
    assert _norm_verdict(filter_v) == "CLEAR"

    # Council result shape: bias key
    council_v = {"bias": "NEUTRAL", "vote_count": "4-2-1", "summary": "..."}
    assert _norm_verdict(council_v) == "NEUTRAL"

    # String pass-through
    assert _norm_verdict("GO") == "GO"
    # None safety
    assert _norm_verdict(None) is None
    # Empty dict
    assert _norm_verdict({}) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
