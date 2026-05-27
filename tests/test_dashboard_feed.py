"""P3-2 — Dashboard /feed page + supporting API endpoints.

This suite covers the routes added to `dashboard/server.py` for the
operator-facing live-feed view:

    /feed                    -> renders templates/feed.html
    /api/feed-signals        -> last N strategy evaluations
    /api/feed-trades         -> last 10 closed trades (via load_all_trades)
    /api/feed-positions      -> bot positions dict

The endpoints use the canonical trade reader
`core.trade_memory.load_all_trades(logs_dir=...)` — never a raw open of
`logs/trade_memory.json`. Tests redirect `dash.PROJECT_ROOT` to a
`tmp_path` so the test never touches the real trade memory file.

If a particular feed endpoint hasn't been wired into server.py yet,
that test is `xfail`'d with a clear message rather than crashing the
whole suite — so this file can land before the server.py patches and
still serve as the integration contract.

Run: pytest tests/test_dashboard_feed.py -v
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from zoneinfo import ZoneInfo
    _CT = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover — Windows < py3.9 fallback
    from datetime import timezone
    _CT = timezone(timedelta(hours=-5))

from dashboard import server as dash


# ─── helpers ───────────────────────────────────────────────────────
def _has_rule(rule: str) -> bool:
    """True iff the Flask app has a route registered for `rule`."""
    return any(r.rule == rule for r in dash.app.url_map.iter_rules())


def _ts(dt: datetime) -> float:
    return dt.timestamp()


# ─── fixtures ──────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with PROJECT_ROOT redirected to tmp_path.

    Also seeds a small trade_memory.json with mixed bot_id rows so the
    /api/feed-trades endpoint has something to merge. Calendar-day pin
    is fixed so trades fall inside the "today" window.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(dash, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        dash, "_calendar_day_start_ct_epoch",
        lambda: datetime(2026, 4, 21, 0, 0, tzinfo=_CT).timestamp(),
    )
    rows = []
    for i in range(15):  # >10 so we can verify the cap
        rows.append({
            "trade_id": f"t{i}",
            "bot_id": "sim" if i % 2 == 0 else "prod",
            "strategy": "orb_breakout" if i < 5 else "vwap_reclaim",
            "direction": "LONG" if i % 3 == 0 else "SHORT",
            "contracts": 1,
            "pnl_dollars": (i - 7) * 5.0,
            "pnl_dollars_net": (i - 7) * 5.0,
            "entry_time": _ts(datetime(2026, 4, 22, 9, i, tzinfo=_CT)),
            "exit_time":  _ts(datetime(2026, 4, 22, 10, i, tzinfo=_CT)),
            "exit_reason": "target_hit" if i % 2 == 0 else "stop_hit",
        })
    (logs_dir / "trade_memory.json").write_text(json.dumps(rows))
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


@pytest.fixture
def fake_bot_state(monkeypatch):
    """Inject a fake bot state into dash._state so the signals + positions
    endpoints have something to render. The state mirrors the shape that
    BaseBot.to_dict() / bot_to_dict() pushes to /api/bot-state.
    """
    fake = {
        "bot_name": "sim",
        "status": "running",
        "last_signal": {
            "ts": "2026-04-22T10:00:00",
            "strategy": "orb_breakout",
            "direction": "LONG",
            "score": 3.7,
            "reason": "ORB high broken with VWAP support",
        },
        "last_eval": {
            "ts": "2026-04-22T10:00:30",
            "regime": "TREND_UP",
            "risk_blocked": None,
            "strategies": [
                {"name": "orb_breakout", "direction": "LONG",
                 "score": 3.7, "decision": "SIGNAL",
                 "reason": "breakout confirmed"},
                {"name": "vwap_reclaim", "direction": "SHORT",
                 "score": 1.2, "decision": "NO_SIGNAL",
                 "reason": "score < threshold"},
                {"name": "compression_squeeze", "direction": "FLAT",
                 "score": 0.0, "decision": "SKIP",
                 "reason": "warmup_incomplete"},
            ],
            "best_signal": None,
        },
        "position": {
            "status": "IN_TRADE",
            "strategy": "orb_breakout",
            "direction": "LONG",
            "entry_price": 18500.25,
            "stop_price":  18495.00,
            "target_price":18512.00,
            "contracts": 2,
            "unrealized_pnl": 12.50,
            "all_positions": [
                {"trade_id": "abc", "strategy": "orb_breakout",
                 "direction": "LONG", "entry_price": 18500.25,
                 "stop_price": 18495.00, "target_price": 18512.00,
                 "contracts": 2, "unrealized_pnl": 12.50},
            ],
        },
    }
    with dash._state_lock:
        dash._state["sim"] = fake
        dash._state["prod"] = {"bot_name": "prod", "status": "stopped"}
    yield fake
    # cleanup
    with dash._state_lock:
        dash._state["sim"] = {}
        dash._state["prod"] = {}


# ═══════════════════════════════════════════════════════════════════
# /feed page
# ═══════════════════════════════════════════════════════════════════
class TestFeedPage:
    def test_feed_route_returns_200_and_html(self, client):
        if not _has_rule("/feed"):
            pytest.xfail("/feed route not yet wired into server.py (P3-2 patch pending)")
        r = client.get("/feed")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Signals" in body
        assert "Trades" in body
        assert "Positions" in body

    def test_feed_template_links_to_dashboard(self, client):
        """feed.html should have a nav link back to /."""
        if not _has_rule("/feed"):
            pytest.xfail("/feed route not yet wired into server.py")
        r = client.get("/feed")
        body = r.get_data(as_text=True)
        assert 'href="/"' in body

    def test_feed_template_polls_three_apis(self, client):
        """feed.html should reference each of the three feed APIs."""
        if not _has_rule("/feed"):
            pytest.xfail("/feed route not yet wired into server.py")
        body = client.get("/feed").get_data(as_text=True)
        assert "/api/feed-signals" in body
        assert "/api/feed-trades" in body
        assert "/api/feed-positions" in body


# ═══════════════════════════════════════════════════════════════════
# /api/feed-signals
# ═══════════════════════════════════════════════════════════════════
class TestFeedSignalsEndpoint:
    def test_returns_200_and_json_list(self, client, fake_bot_state):
        if not _has_rule("/api/feed-signals"):
            pytest.xfail("/api/feed-signals not yet wired into server.py")
        r = client.get("/api/feed-signals")
        assert r.status_code == 200
        data = r.get_json()
        # Endpoint may return a raw list or {"signals": [...]} — accept either.
        rows = data if isinstance(data, list) else data.get("signals", [])
        assert isinstance(rows, list)

    def test_returns_strategy_evaluations_from_last_eval(
        self, client, fake_bot_state
    ):
        if not _has_rule("/api/feed-signals"):
            pytest.xfail("/api/feed-signals not yet wired into server.py")
        r = client.get("/api/feed-signals?bot=sim")
        data = r.get_json()
        rows = data if isinstance(data, list) else data.get("signals", [])
        # Should surface the 3 strategy entries from last_eval.strategies
        # (or last_signal). At minimum, must be non-empty when bot state
        # is present.
        assert len(rows) >= 1
        for row in rows:
            assert isinstance(row, dict)

    def test_respects_limit_param(self, client, fake_bot_state):
        if not _has_rule("/api/feed-signals"):
            pytest.xfail("/api/feed-signals not yet wired into server.py")
        r = client.get("/api/feed-signals?bot=sim&limit=2")
        data = r.get_json()
        rows = data if isinstance(data, list) else data.get("signals", [])
        assert len(rows) <= 2

    def test_empty_bot_state_does_not_500(self, client):
        """No bot state pushed yet — endpoint should return [] not crash."""
        if not _has_rule("/api/feed-signals"):
            pytest.xfail("/api/feed-signals not yet wired into server.py")
        with dash._state_lock:
            dash._state["sim"] = {}
            dash._state["prod"] = {}
        r = client.get("/api/feed-signals?bot=sim")
        assert r.status_code == 200
        data = r.get_json()
        rows = data if isinstance(data, list) else data.get("signals", [])
        assert rows == []


# ═══════════════════════════════════════════════════════════════════
# /api/feed-trades
# ═══════════════════════════════════════════════════════════════════
class TestFeedTradesEndpoint:
    def test_returns_200_and_json_list(self, client):
        if not _has_rule("/api/feed-trades"):
            pytest.xfail("/api/feed-trades not yet wired into server.py")
        r = client.get("/api/feed-trades")
        assert r.status_code == 200
        data = r.get_json()
        rows = data if isinstance(data, list) else data.get("trades", [])
        assert isinstance(rows, list)

    def test_caps_at_10_rows(self, client):
        """We seeded 15 trades; endpoint must return at most 10."""
        if not _has_rule("/api/feed-trades"):
            pytest.xfail("/api/feed-trades not yet wired into server.py")
        r = client.get("/api/feed-trades")
        data = r.get_json()
        rows = data if isinstance(data, list) else data.get("trades", [])
        assert len(rows) <= 10

    def test_sorted_newest_first(self, client):
        """Most-recent exit_time should come first."""
        if not _has_rule("/api/feed-trades"):
            pytest.xfail("/api/feed-trades not yet wired into server.py")
        r = client.get("/api/feed-trades")
        data = r.get_json()
        rows = data if isinstance(data, list) else data.get("trades", [])
        if len(rows) < 2:
            pytest.skip("need at least 2 trades to verify ordering")
        # exit_time is epoch float in our seed data
        times = [float(t.get("exit_time", 0)) for t in rows]
        assert times == sorted(times, reverse=True), (
            f"trades not sorted newest-first: {times}"
        )

    def test_uses_canonical_loader_not_raw_open(self, client, monkeypatch):
        """Endpoint must go through core.trade_memory.load_all_trades —
        the canonical reader. Verified by patching it and confirming the
        endpoint calls our patch (NOT a raw json.load on a hard-coded path).
        """
        if not _has_rule("/api/feed-trades"):
            pytest.xfail("/api/feed-trades not yet wired into server.py")

        called = {"n": 0, "logs_dir": None}

        def _spy(logs_dir="logs"):
            called["n"] += 1
            called["logs_dir"] = logs_dir
            return [
                {"trade_id": "spy1", "bot_id": "sim", "strategy": "x",
                 "direction": "LONG", "contracts": 1, "pnl_dollars": 1.0,
                 "exit_time": _ts(datetime(2026, 4, 22, 10, 0, tzinfo=_CT)),
                 "exit_reason": "tgt"},
            ]
        # Patch at the canonical-import location AND at the dashboard's
        # late-import site if it cached one.
        import core.trade_memory as tm_mod
        monkeypatch.setattr(tm_mod, "load_all_trades", _spy)

        r = client.get("/api/feed-trades")
        assert r.status_code == 200
        assert called["n"] >= 1, (
            "endpoint did not call core.trade_memory.load_all_trades — "
            "it may be raw-opening trade_memory.json (forbidden)"
        )


# ═══════════════════════════════════════════════════════════════════
# /api/feed-positions
# ═══════════════════════════════════════════════════════════════════
class TestFeedPositionsEndpoint:
    def test_returns_200_and_dict(self, client, fake_bot_state):
        if not _has_rule("/api/feed-positions"):
            pytest.xfail("/api/feed-positions not yet wired into server.py")
        r = client.get("/api/feed-positions?bot=sim")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)

    def test_surfaces_bot_positions(self, client, fake_bot_state):
        """fake_bot_state seeded sim with an IN_TRADE position. The
        endpoint must surface either `positions.status=IN_TRADE` or
        a non-empty `all_positions` list."""
        if not _has_rule("/api/feed-positions"):
            pytest.xfail("/api/feed-positions not yet wired into server.py")
        r = client.get("/api/feed-positions?bot=sim")
        data = r.get_json()
        # Accept either {positions: {...}} envelope or root-level dict
        pos = data.get("positions", data)
        all_pos = pos.get("all_positions") or []
        assert pos.get("status") == "IN_TRADE" or len(all_pos) >= 1, (
            f"expected an IN_TRADE position; got {pos!r}"
        )

    def test_flat_when_no_bot_state(self, client):
        """Empty bot state -> endpoint returns FLAT / empty, doesn't 500."""
        if not _has_rule("/api/feed-positions"):
            pytest.xfail("/api/feed-positions not yet wired into server.py")
        with dash._state_lock:
            dash._state["sim"] = {}
            dash._state["prod"] = {}
        r = client.get("/api/feed-positions?bot=sim")
        assert r.status_code == 200
        data = r.get_json()
        pos = data.get("positions", data)
        # Either status FLAT, or no positions at all
        status = pos.get("status") if isinstance(pos, dict) else None
        all_pos = pos.get("all_positions", []) if isinstance(pos, dict) else []
        assert status in (None, "FLAT") and not all_pos


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
