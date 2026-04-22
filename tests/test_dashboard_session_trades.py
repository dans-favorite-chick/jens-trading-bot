"""
B82 — Dashboard session-scoped durable trades.

The dashboard must show ALL current-session trades (sim + prod + lab)
from logs/trade_memory.json, not just whatever happens to be cached in
the bot's last state-push. Session boundary = most-recent 17:00 CT
(CME globex open), so trades from yesterday afternoon remain visible
through today's 16:00-17:00 daily-flatten dead zone and only reset
when the next globex session opens.

Run: pytest tests/test_dashboard_session_trades.py -v
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from zoneinfo import ZoneInfo
    _CT = ZoneInfo("America/Chicago")
except Exception:
    from datetime import timezone
    _CT = timezone(timedelta(hours=-5))

from dashboard import server as dash


# ═══════════════════════════════════════════════════════════════════
# _session_start_ct_epoch — returns the most-recent 17:00 CT
# ═══════════════════════════════════════════════════════════════════
class TestSessionStart:
    def _session_for(self, now_ct: datetime) -> datetime:
        """Call the helper with `now_ct` injected (testability kwarg)
        and return the resulting session-start as a CT datetime."""
        ts = dash._session_start_ct_epoch(now_ct=now_ct)
        return datetime.fromtimestamp(ts, tz=_CT)

    def test_before_1700_returns_yesterday_1700(self):
        """At 09:00 CT Thursday, the current session started 17:00 CT
        Wednesday. Dashboard should still show Wed afternoon + Wed night
        trades."""
        now = datetime(2026, 4, 22, 9, 0, tzinfo=_CT)  # Thursday morning
        session = self._session_for(now)
        assert session == datetime(2026, 4, 21, 17, 0, tzinfo=_CT)

    def test_exactly_at_1700_starts_new_session(self):
        """At 17:00 CT on the dot, we're already in the new session."""
        now = datetime(2026, 4, 22, 17, 0, tzinfo=_CT)
        session = self._session_for(now)
        assert session == datetime(2026, 4, 22, 17, 0, tzinfo=_CT)

    def test_after_1700_returns_today_1700(self):
        """At 19:30 CT Thursday (night session active), session started
        17:00 CT Thursday — yesterday's trades are no longer 'today'."""
        now = datetime(2026, 4, 22, 19, 30, tzinfo=_CT)
        session = self._session_for(now)
        assert session == datetime(2026, 4, 22, 17, 0, tzinfo=_CT)

    def test_daily_flatten_dead_zone_preserves_session(self):
        """16:30 CT is in the 16:00-17:00 'daily flatten' dead zone.
        Session is still yesterday-17:00 — today's morning/afternoon
        trades remain visible."""
        now = datetime(2026, 4, 22, 16, 30, tzinfo=_CT)
        session = self._session_for(now)
        assert session == datetime(2026, 4, 21, 17, 0, tzinfo=_CT)


# ═══════════════════════════════════════════════════════════════════
# _load_session_trades_by_bot — bucketing + session filter
# ═══════════════════════════════════════════════════════════════════
class TestLoadSessionTrades:
    @pytest.fixture
    def tm_file(self, tmp_path, monkeypatch):
        """Redirect PROJECT_ROOT to a temp dir and seed trade_memory.json."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        monkeypatch.setattr(dash, "PROJECT_ROOT", str(tmp_path))
        return logs_dir / "trade_memory.json"

    def _seed(self, path, rows):
        path.write_text(json.dumps(rows))

    def _ts(self, dt: datetime) -> float:
        return dt.timestamp()

    def test_buckets_by_bot_id(self, tm_file, monkeypatch):
        # Freeze "now" so session_start is known.
        monkeypatch.setattr(
            dash, "_session_start_ct_epoch",
            lambda: datetime(2026, 4, 21, 17, 0, tzinfo=_CT).timestamp(),
        )
        rows = [
            {"trade_id": "s1", "bot_id": "sim", "pnl_dollars": 10.0,
             "exit_time": self._ts(datetime(2026, 4, 22, 10, 0, tzinfo=_CT))},
            {"trade_id": "p1", "bot_id": "prod", "pnl_dollars": 20.0,
             "exit_time": self._ts(datetime(2026, 4, 22, 11, 0, tzinfo=_CT))},
            {"trade_id": "s2", "bot_id": "sim", "pnl_dollars": -5.0,
             "exit_time": self._ts(datetime(2026, 4, 22, 12, 0, tzinfo=_CT))},
        ]
        self._seed(tm_file, rows)

        buckets = dash._load_session_trades_by_bot()
        assert set(buckets.keys()) == {"sim", "prod"}
        assert len(buckets["sim"]) == 2
        assert len(buckets["prod"]) == 1
        # Oldest-first within bucket (contract with template .reverse()).
        assert [t["trade_id"] for t in buckets["sim"]] == ["s1", "s2"]

    def test_filters_out_pre_session_trades(self, tm_file, monkeypatch):
        """Trade with exit_time before session_start must NOT appear."""
        monkeypatch.setattr(
            dash, "_session_start_ct_epoch",
            lambda: datetime(2026, 4, 22, 17, 0, tzinfo=_CT).timestamp(),
        )
        # Before session
        old = {"trade_id": "old", "bot_id": "sim", "pnl_dollars": 100.0,
               "exit_time": self._ts(datetime(2026, 4, 21, 15, 0, tzinfo=_CT))}
        # Post-session
        new = {"trade_id": "new", "bot_id": "sim", "pnl_dollars": 5.0,
               "exit_time": self._ts(datetime(2026, 4, 22, 19, 0, tzinfo=_CT))}
        self._seed(tm_file, [old, new])

        buckets = dash._load_session_trades_by_bot()
        ids = [t["trade_id"] for t in buckets.get("sim", [])]
        assert ids == ["new"], (
            f"session filter didn't exclude pre-session trade: {ids}"
        )

    def test_missing_bot_id_lands_in_unknown_bucket(self, tm_file, monkeypatch):
        monkeypatch.setattr(
            dash, "_session_start_ct_epoch",
            lambda: datetime(2026, 4, 21, 17, 0, tzinfo=_CT).timestamp(),
        )
        self._seed(tm_file, [{
            "trade_id": "x", "pnl_dollars": 1.0,
            "exit_time": self._ts(datetime(2026, 4, 22, 10, 0, tzinfo=_CT)),
        }])
        buckets = dash._load_session_trades_by_bot()
        assert "unknown" in buckets
        assert buckets["unknown"][0]["trade_id"] == "x"

    def test_missing_file_returns_empty_dict(self, tm_file):
        # Don't seed — file doesn't exist. Helper must not crash.
        assert not tm_file.exists()
        buckets = dash._load_session_trades_by_bot()
        assert buckets == {}

    def test_corrupt_json_returns_empty_dict(self, tm_file):
        tm_file.write_text("{not valid json at all")
        buckets = dash._load_session_trades_by_bot()
        assert buckets == {}

    def test_non_list_shape_returns_empty_dict(self, tm_file):
        tm_file.write_text(json.dumps({"nope": "dict not list"}))
        buckets = dash._load_session_trades_by_bot()
        assert buckets == {}

    def test_trades_without_exit_time_are_skipped(self, tm_file, monkeypatch):
        monkeypatch.setattr(
            dash, "_session_start_ct_epoch",
            lambda: datetime(2026, 4, 21, 17, 0, tzinfo=_CT).timestamp(),
        )
        self._seed(tm_file, [
            {"trade_id": "no_ts", "bot_id": "sim", "pnl_dollars": 1.0},
            {"trade_id": "ok", "bot_id": "sim", "pnl_dollars": 1.0,
             "exit_time": self._ts(datetime(2026, 4, 22, 10, 0, tzinfo=_CT))},
        ])
        buckets = dash._load_session_trades_by_bot()
        assert [t["trade_id"] for t in buckets.get("sim", [])] == ["ok"]


# ═══════════════════════════════════════════════════════════════════
# /api/trades endpoint — sim bucket inclusion + shape
# ═══════════════════════════════════════════════════════════════════
class TestApiTradesEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        monkeypatch.setattr(dash, "PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(
            dash, "_session_start_ct_epoch",
            lambda: datetime(2026, 4, 21, 17, 0, tzinfo=_CT).timestamp(),
        )
        rows = [
            {"trade_id": "s1", "bot_id": "sim", "pnl_dollars": 10.0,
             "account": "SimSpring Setup",
             "exit_time": datetime(2026, 4, 22, 10, 0, tzinfo=_CT).timestamp()},
            {"trade_id": "p1", "bot_id": "prod", "pnl_dollars": 20.0,
             "account": "Sim101",
             "exit_time": datetime(2026, 4, 22, 11, 0, tzinfo=_CT).timestamp()},
        ]
        (logs_dir / "trade_memory.json").write_text(json.dumps(rows))
        dash.app.config["TESTING"] = True
        return dash.app.test_client()

    def test_response_has_prod_sim_and_lab_buckets(self, client):
        r = client.get("/api/trades")
        assert r.status_code == 200
        data = r.get_json()
        assert "prod" in data
        assert "sim" in data
        assert "lab" in data
        assert "session_start_ts" in data

    def test_sim_bucket_contains_sim_trades(self, client):
        r = client.get("/api/trades")
        data = r.get_json()
        sim_ids = [t["trade_id"] for t in data["sim"]]
        prod_ids = [t["trade_id"] for t in data["prod"]]
        assert sim_ids == ["s1"]
        assert prod_ids == ["p1"]

    def test_sim_trade_preserves_account_field(self, client):
        r = client.get("/api/trades")
        data = r.get_json()
        assert data["sim"][0]["account"] == "SimSpring Setup"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
