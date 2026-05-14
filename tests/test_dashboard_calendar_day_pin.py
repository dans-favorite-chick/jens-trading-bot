"""Dashboard calendar-day boundary regression pin (2026-05-14).

Three dashboard call sites must use `_calendar_day_start_ct_epoch()`
and MUST NOT call `_session_start_ct_epoch()`:

  1. `_load_session_trades_by_bot()` — bucketing helper feeding the
     Daily Stats trade tables on /api/status and /api/trades.
  2. `/api/status` (`api_status` handler) — `session_start_ts` field.
  3. `/api/trades` (`api_trades` handler) — `session_start_ts` field.

Originally (B82) all three used Globex 17:00 CT. The 2026-05-13 fix
`0c24a8e` moved `/api/today-pnl` to calendar day but missed these
three; result was a 7-hour-per-night panel mismatch where the trade
table showed yesterday-evening trades that the TODAY card had
already rolled out. Operator observed "16 trades on dashboard, 8 sim,
1 prod" — the 16 was the sim Globex-window count, the 8 was the
calendar-day count from RiskManager.

This test does a source-pin on the function body so any future
regression that re-introduces the Globex call is caught at test time.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")


def _function_body(fn_name: str) -> str:
    """Extract the body of `def fn_name(...):` up to the next top-level
    `def ` or `@app.route` decorator. Comments stripped so the assertion
    can't false-positive on docstrings or rationale comments."""
    # Match `def <name>(...):` then everything until the next top-level
    # def/decorator at column 0.
    pattern = re.compile(
        rf"^def {re.escape(fn_name)}\(.*?\n(.*?)(?=^def |^@app\.route|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(SRC)
    assert m, f"couldn't locate `def {fn_name}` in dashboard/server.py"
    body = m.group(1)
    # Strip comment lines and docstring blocks so the assertion looks at
    # actual code only.
    non_comment_lines = [
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    ]
    body_no_comments = "\n".join(non_comment_lines)
    # Crude triple-quoted-docstring strip (good enough for our docstrings
    # which all use triple-double-quotes).
    body_no_docstr = re.sub(r'"""[\s\S]*?"""', "", body_no_comments)
    return body_no_docstr


def _route_handler_body(route: str) -> str:
    """Extract the body of an @app.route('<route>') handler."""
    pattern = re.compile(
        rf'@app\.route\("{re.escape(route)}"\)\s*\ndef (\w+)\(',
        re.MULTILINE,
    )
    m = pattern.search(SRC)
    assert m, f"couldn't locate @app.route('{route}') handler"
    return _function_body(m.group(1))


# ── _load_session_trades_by_bot ────────────────────────────────────────

def test_load_session_trades_uses_calendar_day():
    body = _function_body("_load_session_trades_by_bot")
    assert "_calendar_day_start_ct_epoch" in body, (
        "_load_session_trades_by_bot must call _calendar_day_start_ct_epoch "
        "so the Daily Stats trade table agrees with the TODAY P&L card."
    )


def test_load_session_trades_does_not_use_globex():
    body = _function_body("_load_session_trades_by_bot")
    assert "_session_start_ct_epoch" not in body, (
        "_load_session_trades_by_bot must NOT call _session_start_ct_epoch — "
        "that's the 2026-05-14 regression. Globex 17:00 boundary breaks "
        "agreement with /api/today-pnl from 17:00 CT to midnight every night."
    )


# ── /api/status handler ────────────────────────────────────────────────

def test_api_status_uses_calendar_day():
    body = _route_handler_body("/api/status")
    assert "_calendar_day_start_ct_epoch" in body, (
        "/api/status handler must return calendar-day session_start_ts so "
        "frontend timestamps match every other panel."
    )


def test_api_status_does_not_use_globex():
    body = _route_handler_body("/api/status")
    assert "_session_start_ct_epoch" not in body, (
        "/api/status handler must NOT call _session_start_ct_epoch — "
        "regression of 2026-05-14 boundary fix."
    )


# ── /api/trades handler ────────────────────────────────────────────────

def test_api_trades_uses_calendar_day():
    body = _route_handler_body("/api/trades")
    assert "_calendar_day_start_ct_epoch" in body, (
        "/api/trades handler must return calendar-day session_start_ts."
    )


def test_api_trades_does_not_use_globex():
    body = _route_handler_body("/api/trades")
    assert "_session_start_ct_epoch" not in body, (
        "/api/trades handler must NOT call _session_start_ct_epoch — "
        "regression of 2026-05-14 boundary fix."
    )


# ── End-to-end: all four dashboard surfaces agree ─────────────────────

def test_all_dashboard_surfaces_use_calendar_day():
    """Belt-and-suspenders: collect the 4 dashboard handlers/helpers
    and verify ALL of them call the calendar-day helper."""
    surfaces = {
        "_load_session_trades_by_bot": _function_body("_load_session_trades_by_bot"),
        "/api/status":     _route_handler_body("/api/status"),
        "/api/trades":     _route_handler_body("/api/trades"),
        "/api/today-pnl":  _route_handler_body("/api/today-pnl"),
    }
    bad = [
        name for name, body in surfaces.items()
        if "_calendar_day_start_ct_epoch" not in body
    ]
    assert not bad, (
        f"Dashboard surfaces not using calendar-day boundary: {bad}. "
        f"All 4 must agree or operator sees 'today' meaning different "
        f"things on different panels."
    )
