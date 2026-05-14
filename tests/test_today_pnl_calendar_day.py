"""/api/today-pnl uses calendar-day boundary, matching bot's RiskManager.

Background
----------
Pre-2026-05-13, `/api/today-pnl` used the CME Globex session boundary —
"today" started at the most-recent 17:00 CT. The bot's RiskManager
`daily_pnl` counter (shown on the Daily Stats panel) uses CALENDAR day
boundary — resets at 00:00 CT.

Result: from 17:00 CT to 00:00 CT every evening, the TODAY (CME Globex)
card and the Daily Stats panel disagreed for 7 hours. TODAY card showed
$0 (new Globex session just started, no trades yet) while Daily Stats
still showed the day's accumulated P&L. Operator confusion was a
recurring nightly experience.

Fix: switched `/api/today-pnl` to call `_calendar_day_start_ct_epoch()`
(new helper) instead of `_session_start_ct_epoch()`. As of 2026-05-14,
the same fix has been extended to `_load_session_trades_by_bot()`,
`/api/status`, and `/api/trades` (see
`tests/test_dashboard_session_trades.py`) — every dashboard surface now
uses calendar day, so panels agree. The Globex helper is preserved but
unused by the dashboard.

This test covers:
1. `_calendar_day_start_ct_epoch()` returns midnight CT today
2. `api_today_pnl` handler uses the calendar helper, not Globex
3. Boundary behavior: a trade exiting at 17:00 CT today counts as today
   (the bug case — pre-fix this would not have counted)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_calendar_day_helper_returns_midnight_ct():
    """The new helper must return the most-recent 00:00 CT (today's midnight)."""
    from dashboard import server as dash
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")

    # Fake "now" = 2026-05-13 17:12 CT
    now_ct = datetime(2026, 5, 13, 17, 12, 30, tzinfo=ct)
    ts = dash._calendar_day_start_ct_epoch(now_ct=now_ct)

    expected = datetime(2026, 5, 13, 0, 0, 0, tzinfo=ct).timestamp()
    assert ts == expected, (
        f"expected midnight CT today ({expected}), got {ts} "
        f"(delta: {ts - expected}s = {(ts - expected)/3600:.2f}h)"
    )


def test_calendar_day_helper_at_2am_returns_today_midnight_not_yesterday():
    """At 02:00 CT, the calendar-day start is TODAY's midnight (2 hours ago),
    not yesterday's. (Globex helper at this hour would return YESTERDAY's
    17:00 — that's the diverging case.)"""
    from dashboard import server as dash
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")

    now_ct = datetime(2026, 5, 13, 2, 0, 0, tzinfo=ct)
    cal_ts = dash._calendar_day_start_ct_epoch(now_ct=now_ct)
    cal_expected = datetime(2026, 5, 13, 0, 0, 0, tzinfo=ct).timestamp()
    assert cal_ts == cal_expected

    # And confirm the Globex helper at the same moment returns the
    # previous day's 17:00 — so we know the two boundaries are different.
    globex_ts = dash._session_start_ct_epoch(now_ct=now_ct)
    globex_expected = datetime(2026, 5, 12, 17, 0, 0, tzinfo=ct).timestamp()
    assert globex_ts == globex_expected
    assert cal_ts != globex_ts, (
        "calendar and Globex helpers must return different boundaries — "
        "they're the same here which means the test isn't actually "
        "exercising the bug case"
    )


def test_api_today_pnl_uses_calendar_helper_not_globex():
    """Source-grep: api_today_pnl handler must call the calendar helper,
    not the Globex one."""
    src = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    # Locate the handler body
    import re
    m = re.search(
        r"@app\.route\(\"/api/today-pnl\"\).*?(?=@app\.route|\Z)",
        src, re.DOTALL,
    )
    assert m, "couldn't locate /api/today-pnl handler"
    body = m.group(0)
    # Strip comment lines so the deliberate doc-block doesn't false-positive
    non_comment = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "_calendar_day_start_ct_epoch" in non_comment, (
        "/api/today-pnl handler must call _calendar_day_start_ct_epoch — "
        "or the dashboard TODAY card will disagree with the Daily Stats "
        "panel for 7 hours every evening (17:00 CT to midnight)."
    )
    # And it MUST NOT have an uncommented _session_start_ct_epoch call,
    # otherwise we're back to the bug. (The helper itself may appear in
    # comments referencing the deprecation.)
    assert "_session_start_ct_epoch" not in non_comment, (
        "/api/today-pnl handler still calls _session_start_ct_epoch — "
        "regression of 2026-05-13 boundary fix"
    )


def test_trade_exiting_at_17_00_counts_as_today():
    """The diverging boundary case: a trade exiting at 17:00 CT today
    must show up in today's P&L. Pre-fix: Globex boundary said this was
    the start of NEW session = 0 trades. Post-fix: it's still today
    (calendar day), so it counts."""
    from dashboard import server as dash
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")

    # Mock "now" to a moment after 17:00 CT
    now_ct = datetime(2026, 5, 13, 17, 30, 0, tzinfo=ct)

    # Build a synthetic trade that exited at 17:00 CT today
    trade_exit_ts = datetime(2026, 5, 13, 17, 0, 0, tzinfo=ct).timestamp()
    synthetic_trade = {
        "trade_id": "boundary-test",
        "bot_id": "sim",
        "strategy": "vwap_pullback",
        "exit_time": trade_exit_ts,
        "pnl_dollars": 50.0,
        "net_pnl": 50.0,
        "pnl_dollars_gross": 50.5,
        "cost_total_dollars": 0.5,
    }

    # Patch the now-source AND the trade loader
    with patch.object(dash, "_calendar_day_start_ct_epoch",
                      return_value=dash._calendar_day_start_ct_epoch(now_ct=now_ct)):
        # Mock load_all_trades via patching the module-level reference
        # in core.trade_memory (dashboard does a function-scope import)
        from core import trade_memory as tm
        with patch.object(tm, "load_all_trades",
                          return_value=[synthetic_trade]):
            client = dash.app.test_client()
            resp = client.get("/api/today-pnl")
            assert resp.status_code == 200
            payload = resp.get_json()

    assert payload["trade_count"] == 1, (
        f"trade exiting at 17:00 CT today should count as today (calendar "
        f"day), got trade_count={payload['trade_count']}. "
        f"This is the original bug — TODAY card would show $0 while the "
        f"trade is clearly from today."
    )
    sim = payload["per_bot"].get("sim", {})
    assert sim.get("pnl") == 50.0, f"expected pnl=50.0, got {sim}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
