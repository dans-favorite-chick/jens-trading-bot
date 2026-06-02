"""Phase 6 (2026-06-02 overnight) tests for market_state on every
HistoryLogger event.

Covers:
  (a) every event type (bar, eval, entry, exit, near_miss) carries a
      ``market_state`` field
  (b) caller-stamped market["market_state"] wins over the source
  (c) source callable is consulted when market dict lacks the field
  (d) None default when MarketState is unavailable
  (e) source-raising-exception path doesn't break the log write
  (f) old JSONL without the field still loads (backward-compat)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_logger(tmp_path, monkeypatch):
    """Return a HistoryLogger writing into tmp_path/logs/history."""
    monkeypatch.chdir(tmp_path)
    from core import history_logger as hl
    hl.HISTORY_DIR = str(tmp_path / "logs" / "history")
    hl.clear_market_state_source()
    log = hl.HistoryLogger(bot_name="test")
    yield log
    log.close()
    hl.clear_market_state_source()


def _read_events(tmp_path):
    """Read all JSONL events from today's file."""
    today = datetime.now().date()
    files = list((tmp_path / "logs" / "history").glob(f"{today}_*.jsonl"))
    if not files:
        return []
    rows = []
    with open(files[0]) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                rows.append(json.loads(ln))
    return rows


def _fake_bar():
    return SimpleNamespace(open=100, high=101, low=99, close=100.5,
                            volume=42, tick_count=10)


def _fake_signal(strategy="bias_momentum", direction="LONG"):
    return SimpleNamespace(
        direction=direction, strategy=strategy, reason="test",
        confluences=[], confidence=80.0, entry_score=70,
        stop_ticks=12, target_rr=2.5,
    )


# ─── (a) every event type carries market_state ────────────────────

def test_log_bar_has_market_state(tmp_path, tmp_logger):
    tmp_logger.log_bar("1m", _fake_bar(), {"market_state": "TRENDING_NORMAL"}, "OPEN")
    events = _read_events(tmp_path)
    assert events[0]["event"] == "bar"
    assert events[0]["market_state"] == "TRENDING_NORMAL"


def test_log_eval_has_market_state(tmp_path, tmp_logger):
    tmp_logger.log_eval(
        {"regime": "OPEN", "strategies": []},
        {"market_state": "CHOPPY"},
    )
    events = _read_events(tmp_path)
    assert events[0]["event"] == "eval"
    assert events[0]["market_state"] == "CHOPPY"


def test_log_entry_has_market_state(tmp_path, tmp_logger):
    tmp_logger.log_entry(
        _fake_signal(), price=100.5, contracts=1,
        stop_price=99.0, target_price=102.0,
        risk_dollars=15.0, tier="A",
        market={"market_state": "TRENDING_HIGH_VOL"},
    )
    events = _read_events(tmp_path)
    assert events[0]["event"] == "entry"
    assert events[0]["market_state"] == "TRENDING_HIGH_VOL"


def test_log_exit_has_market_state(tmp_path, tmp_logger):
    trade = {"direction": "LONG", "strategy": "bias_momentum",
             "entry_price": 100, "exit_price": 102, "contracts": 1,
             "pnl_dollars": 4.0, "pnl_ticks": 8, "exit_reason": "target"}
    tmp_logger.log_exit(trade, {"market_state": "TRENDING_NORMAL"})
    events = _read_events(tmp_path)
    assert events[0]["event"] == "exit"
    assert events[0]["market_state"] == "TRENDING_NORMAL"


def test_log_near_miss_has_market_state(tmp_path, tmp_logger):
    tmp_logger.log_near_miss(
        {"direction": "LONG", "strategy": "x"},
        {"market_state": "COMPRESSED"},
        reason="below_confluence_floor",
    )
    events = _read_events(tmp_path)
    assert events[0]["event"] == "near_miss"
    assert events[0]["market_state"] == "COMPRESSED"


# ─── (b) caller-stamped market wins over source ───────────────────

def test_caller_stamped_wins_over_source(tmp_path, tmp_logger):
    from core.history_logger import set_market_state_source
    set_market_state_source(lambda: "WHIPSAW_HIGH_VOL")
    tmp_logger.log_bar(
        "5m", _fake_bar(), {"market_state": "CHOPPY"}, "MID",
    )
    events = _read_events(tmp_path)
    assert events[0]["market_state"] == "CHOPPY"


# ─── (c) source consulted when market lacks the field ─────────────

def test_source_used_when_market_missing_field(tmp_path, tmp_logger):
    from core.history_logger import set_market_state_source
    set_market_state_source(lambda: "TRENDING_NORMAL")
    tmp_logger.log_eval({"regime": "OPEN"}, {"price": 100})
    events = _read_events(tmp_path)
    assert events[0]["market_state"] == "TRENDING_NORMAL"


# ─── (d) None default when no source registered ───────────────────

def test_none_default_no_source(tmp_path, tmp_logger):
    tmp_logger.log_bar("1m", _fake_bar(), {}, "OPEN")
    events = _read_events(tmp_path)
    assert events[0]["market_state"] is None


# ─── (e) source-exception path doesn't break logging ──────────────

def test_source_exception_logs_none(tmp_path, tmp_logger):
    from core.history_logger import set_market_state_source

    def boom():
        raise RuntimeError("classifier unavailable")

    set_market_state_source(boom)
    # Must not raise.
    tmp_logger.log_eval({"regime": "OPEN"}, {})
    events = _read_events(tmp_path)
    assert events[0]["event"] == "eval"
    assert events[0]["market_state"] is None


# ─── (f) backward-compat: old JSONL without the field still loads ──

def test_legacy_events_load_without_field(tmp_path):
    """Existing JSONL lines without market_state are valid; readers use
    .get("market_state") and treat missing as None."""
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    p = legacy_dir / "2026-05-01_sim.jsonl"
    p.write_text(json.dumps({
        "event": "eval", "ts": "2026-05-01T12:00:00",
        "bot": "sim", "regime": "OPEN", "strategies": [],
    }) + "\n", encoding="utf-8")
    with open(p) as f:
        ev = json.loads(f.readline())
    assert ev["event"] == "eval"
    assert ev.get("market_state") is None  # field absent => None


# ─── (g) dashboard-style aggregation across event types ───────────

def test_dashboard_style_aggregation(tmp_path, tmp_logger):
    from collections import Counter
    from core.history_logger import set_market_state_source
    # Mix: 3 evals + 2 entries + 1 bar; each carries a state via
    # the registered source.
    set_market_state_source(lambda: "TRENDING_NORMAL")
    for _ in range(3):
        tmp_logger.log_eval({"regime": "OPEN"}, {"price": 100})
    for _ in range(2):
        tmp_logger.log_entry(
            _fake_signal(), price=100, contracts=1,
            stop_price=99, target_price=101,
            risk_dollars=15, tier="A", market={"price": 100},
        )
    tmp_logger.log_bar("1m", _fake_bar(), {}, "OPEN")
    events = _read_events(tmp_path)
    by_state = Counter(e["market_state"] for e in events)
    assert by_state["TRENDING_NORMAL"] == 6
    by_event = Counter(e["event"] for e in events)
    assert by_event["eval"] == 3 and by_event["entry"] == 2
    assert by_event["bar"] == 1
