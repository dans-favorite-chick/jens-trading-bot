"""Resilience tests for core.trade_memory.load_all_trades.

The 2026-06-02 incident: trade_memory_prod.json got truncated mid-write
(no closing `]`), and the unhardened loader silently returned [] for
prod — operator's dashboard reported "0 prod trades today" while the
bot was actively trading. These tests pin the new behavior:

  - valid file → all records returned
  - truncated mid-record → recovered via trailing-bracket repair
  - trailing-comma only → recovered
  - totally garbage → empty list, NO exception, ERROR logged
  - one bad file does NOT zero out the other bots' data
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from core import trade_memory
from core.trade_memory import load_all_trades, _read_trades_resilient


@pytest.fixture(autouse=True)
def _reset_warn_cache():
    """Recovery WARNs are deduped per-path at module level; reset between tests."""
    trade_memory._RECOVERY_WARN_EMITTED.clear()
    yield
    trade_memory._RECOVERY_WARN_EMITTED.clear()


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_valid_file_loads_all_records(tmp_path: Path) -> None:
    rows = [
        {"trade_id": "t1", "bot_id": "prod", "pnl_dollars": 12.5},
        {"trade_id": "t2", "bot_id": "prod", "pnl_dollars": -8.0},
    ]
    _write(tmp_path / "trade_memory_prod.json", json.dumps(rows))

    out = load_all_trades(logs_dir=str(tmp_path))
    assert len(out) == 2
    assert {t["trade_id"] for t in out} == {"t1", "t2"}


def test_truncated_mid_record_is_recovered(tmp_path: Path, caplog) -> None:
    rows = [
        {"trade_id": "t1", "bot_id": "prod", "pnl_dollars": 12.5},
        {"trade_id": "t2", "bot_id": "prod", "pnl_dollars": -8.0},
        {"trade_id": "t3", "bot_id": "prod", "pnl_dollars": 4.0},
    ]
    full = json.dumps(rows, indent=2)
    truncated = full[:-2]  # chops the final " ]" closing bracket
    _write(tmp_path / "trade_memory_prod.json", truncated)

    with caplog.at_level(logging.WARNING, logger="TradeMemory"):
        out = load_all_trades(logs_dir=str(tmp_path))

    assert len(out) == 3, "structural repair should recover all 3 records"
    assert {t["trade_id"] for t in out} == {"t1", "t2", "t3"}
    assert any("RECOVERED" in m for m in caplog.messages), \
        "operator MUST see a loud WARN line when a file is recovered"


def test_trailing_comma_only_is_recovered(tmp_path: Path, caplog) -> None:
    rows = [{"trade_id": "t1", "pnl_dollars": 1.0}]
    raw = json.dumps(rows)[:-1] + ","  # "[{...},"
    _write(tmp_path / "trade_memory_sim.json", raw)

    with caplog.at_level(logging.WARNING, logger="TradeMemory"):
        out = load_all_trades(logs_dir=str(tmp_path))

    assert len(out) == 1
    assert out[0]["trade_id"] == "t1"
    assert any("RECOVERED" in m for m in caplog.messages)


def test_totally_garbage_returns_empty_without_raising(tmp_path: Path, caplog) -> None:
    _write(tmp_path / "trade_memory_prod.json", "{{{not json at all}}}")

    with caplog.at_level(logging.ERROR, logger="TradeMemory"):
        out = load_all_trades(logs_dir=str(tmp_path))

    assert out == [], "garbage file must yield empty list, not raise"
    assert any("UNRECOVERABLE" in m for m in caplog.messages), \
        "operator MUST see an ERROR line when recovery fails"


def test_one_bad_file_does_not_zero_out_other_bots(tmp_path: Path) -> None:
    """The bug we are insuring against — a corrupt prod file used to make
    sim's data invisible too because of where the exception bubbled."""
    sim_rows = [
        {"trade_id": "s1", "bot_id": "sim", "pnl_dollars": 5.0},
        {"trade_id": "s2", "bot_id": "sim", "pnl_dollars": -3.0},
    ]
    _write(tmp_path / "trade_memory_sim.json", json.dumps(sim_rows))
    _write(tmp_path / "trade_memory_prod.json", "{{not json}}")

    out = load_all_trades(logs_dir=str(tmp_path))
    assert len(out) == 2
    assert {t["trade_id"] for t in out} == {"s1", "s2"}


def test_legacy_file_also_resilient(tmp_path: Path) -> None:
    """The legacy shared logs/trade_memory.json (frozen but still read) must
    get the same protection — older history files have the same risk."""
    legacy_rows = [{"trade_id": "L1", "pnl_dollars": 1.0}]
    full = json.dumps(legacy_rows)
    truncated = full[:-1]  # drop closing "]"
    _write(tmp_path / "trade_memory.json", truncated)

    out = load_all_trades(logs_dir=str(tmp_path))
    assert len(out) == 1
    assert out[0]["trade_id"] == "L1"


def test_per_bot_file_wins_over_legacy_on_collision(tmp_path: Path) -> None:
    """Pre-existing semantics: per-bot file overrides legacy by trade_id."""
    _write(tmp_path / "trade_memory.json", json.dumps([
        {"trade_id": "x", "pnl_dollars": 1.0, "src": "legacy"},
    ]))
    _write(tmp_path / "trade_memory_prod.json", json.dumps([
        {"trade_id": "x", "pnl_dollars": 99.0, "src": "per_bot"},
    ]))

    out = load_all_trades(logs_dir=str(tmp_path))
    assert len(out) == 1
    assert out[0]["src"] == "per_bot"


def test_warn_dedup_per_path(tmp_path: Path, caplog) -> None:
    """WARN should fire ONCE per path even across many load_all_trades calls;
    otherwise the dashboard's 2s poll would spam the log every cycle."""
    rows = [{"trade_id": "t1"}]
    truncated = json.dumps(rows)[:-1]
    p = tmp_path / "trade_memory_prod.json"
    _write(p, truncated)

    with caplog.at_level(logging.WARNING, logger="TradeMemory"):
        load_all_trades(logs_dir=str(tmp_path))
        load_all_trades(logs_dir=str(tmp_path))
        load_all_trades(logs_dir=str(tmp_path))

    recovered = [m for m in caplog.messages if "RECOVERED" in m]
    assert len(recovered) == 1, f"expected 1 RECOVERED line, got {len(recovered)}"


def test_read_trades_resilient_returns_list_for_non_list_top_level(tmp_path: Path) -> None:
    """A dict at the top (instead of list) shouldn't crash callers."""
    p = tmp_path / "trade_memory_prod.json"
    _write(p, json.dumps({"this_is": "a_dict_not_a_list"}))
    assert _read_trades_resilient(str(p)) == []
