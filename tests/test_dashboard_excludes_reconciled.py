"""Regression test for FINDING-2026-06-04-DASH-ATTR (dashboard side).

`/api/today-pnl` aggregates per-bot and per-strategy stats over the
current session. Before this fix, every reconciled orphan-fill row
counted toward the prod_bot's `big_move_signal` win rate / PnL,
because reconciled trades carried no provenance flag the aggregator
could filter on.

Post-fix, rows tagged `source='manual_reconciled'` are excluded from
both per_bot and per_strategy aggregation. The aggregator surfaces
the excluded count via a new `reconciled_trade_count` field so the UI
can show "N manual-reconciled trades not counted in bot stats".

We pin the behavior by monkeypatching `core.trade_memory.load_all_trades`
to return a synthetic mix of one reconciled row and one real bot row,
then hit the Flask endpoint and assert only the real row is counted.
"""
from __future__ import annotations

import time

import pytest


def _calendar_day_recent_ts() -> float:
    """A timestamp that falls inside today's calendar-day window so
    the aggregator's `session_start` filter accepts it."""
    # 10 seconds ago — definitely past midnight CT.
    return time.time() - 10.0


@pytest.fixture
def client(monkeypatch):
    """A Flask test client with `load_all_trades` patched to a known
    synthetic shape — no live trade_memory file involvement."""
    from dashboard import server as srv

    app = srv.app
    app.config["TESTING"] = True
    return app.test_client()


def _mk_trade(*, trade_id, bot_id, strategy, source, pnl=10.0, exit_offset=0):
    """Build a session-window trade dict carrying the minimal fields
    /api/today-pnl reads (B13-era schema)."""
    return {
        "trade_id": trade_id,
        "bot_id": bot_id,
        "strategy": strategy,
        "source": source,
        "direction": "SHORT",
        "entry_time": _calendar_day_recent_ts() - 120.0,
        "exit_time": _calendar_day_recent_ts() + exit_offset,
        "entry_price": 30000.0,
        "exit_price": 29990.0,
        "contracts": 1,
        "pnl_dollars": pnl,
        "pnl_dollars_net": pnl,
        "pnl_dollars_gross": pnl + 1.0,
        "cost_total_dollars": 1.0,
        "result": "WIN" if pnl > 0 else "LOSS",
        "exit_reason": "target_hit",
        "account": "Sim101",
        "reconciled_from_orphan": (source == "manual_reconciled"),
    }


def test_today_pnl_excludes_reconciled_from_per_bot_and_per_strategy(
    client, monkeypatch
):
    """Synthetic two-row scenario:
    - One reconciled WIN labeled `_reconciled_Sim101` (NOT a real strategy).
    - One real bot WIN labeled `bias_momentum`.
    The aggregator must count only bias_momentum in per_bot/per_strategy.
    """
    reconciled = _mk_trade(
        trade_id="RECONCILED_Sim101_aaaaaaaa",
        bot_id="prod",
        strategy="_reconciled_Sim101",
        source="manual_reconciled",
        pnl=70.18,
    )
    real_bot = _mk_trade(
        trade_id="trade_realbot_bbbbbbbb",
        bot_id="prod",
        strategy="bias_momentum",
        source="bot",
        pnl=22.5,
        exit_offset=5,
    )

    from core import trade_memory as tm
    monkeypatch.setattr(tm, "load_all_trades", lambda **kw: [reconciled, real_bot])

    resp = client.get("/api/today-pnl")
    assert resp.status_code == 200, resp.data
    payload = resp.get_json()

    # The reconciled row must NOT appear in per_strategy or per_bot maths.
    assert "_reconciled_Sim101" not in payload["per_strategy"], (
        f"reconciled label leaked into per_strategy: "
        f"{list(payload['per_strategy'])}"
    )
    # The real bot trade IS counted.
    assert "bias_momentum" in payload["per_strategy"]
    bm = payload["per_strategy"]["bias_momentum"]
    assert bm["trades"] == 1
    assert bm["wins"] == 1
    assert bm["losses"] == 0
    assert bm["pnl"] == pytest.approx(22.5)
    # And per_bot prod counts only the real trade — not 2.
    prod = payload["per_bot"]["prod"]
    assert prod["trades"] == 1, (
        f"per_bot.prod must count only real bot trade (1), got {prod['trades']}"
    )
    assert prod["wins"] == 1
    assert prod["pnl"] == pytest.approx(22.5)
    # And the new reconciled_trade_count surfaces the excluded row.
    assert payload.get("reconciled_trade_count") == 1
    # And trade_count (the headline count) reflects real trades only.
    assert payload["trade_count"] == 1


def test_today_pnl_handles_only_reconciled_rows_gracefully(
    client, monkeypatch
):
    """If today has ONLY reconciled rows (e.g. operator manual-only day),
    aggregator must show 0 real trades, the reconciled count surfaced,
    and no per_bot / per_strategy entries (since we never aggregated).
    """
    reconciled = _mk_trade(
        trade_id="RECONCILED_Sim101_cccccccc",
        bot_id="prod",
        strategy="_reconciled_Sim101",
        source="manual_reconciled",
        pnl=-10.0,
    )

    from core import trade_memory as tm
    monkeypatch.setattr(tm, "load_all_trades", lambda **kw: [reconciled])

    resp = client.get("/api/today-pnl")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["trade_count"] == 0
    assert payload.get("reconciled_trade_count") == 1
    assert payload["per_bot"] == {}
    assert payload["per_strategy"] == {}


def test_today_pnl_unflagged_legacy_rows_still_aggregate(client, monkeypatch):
    """Backwards-compat: pre-fix trade memory rows have NO `source` field.
    The aggregator must treat absent-source as 'bot' (i.e. count them);
    only an EXPLICIT 'manual_reconciled' tag triggers exclusion.

    Critical because the 12 historical RECONCILED rows in the DB don't
    yet carry the new flag — the HIST-MIGRATE script (Phase 3.4) backfills
    them. Until that script runs, those rows must keep aggregating as
    they always have. Otherwise this fix would silently change today's
    reported PnL for past sessions.
    """
    legacy = _mk_trade(
        trade_id="legacy_no_source_field",
        bot_id="prod",
        strategy="bias_momentum",
        source=None,  # absent in legacy schema
        pnl=42.0,
    )
    legacy.pop("source")

    from core import trade_memory as tm
    monkeypatch.setattr(tm, "load_all_trades", lambda **kw: [legacy])

    resp = client.get("/api/today-pnl")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["trade_count"] == 1
    assert payload["per_strategy"]["bias_momentum"]["trades"] == 1
    assert payload.get("reconciled_trade_count") == 0
