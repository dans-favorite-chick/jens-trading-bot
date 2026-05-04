"""Sprint G dashboard fix — integration + structural tests.

Sprint G found that the dashboard defaulted to the prod_bot tab, but
prod_bot only runs 2 validated strategies and has 0 trades on most days.
The operator saw "$0 P&L / 0 trades" and concluded the bot wasn't
trading, when in fact sim_bot was actively trading.

These tests verify:
  - /api/today-pnl returns the data structure the new summary bar reads
  - The HTML has the new combined-bots summary
  - The HTML's default activeBot is 'sim' (not 'prod')
  - The HTML actually fetches /api/today-pnl on each poll
  - Backend route /api/today-pnl exists and returns expected shape

Frontend rendering is verified structurally rather than via a real
browser — element IDs and the fetcher function must be present.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


HTML = ROOT / "dashboard" / "templates" / "dashboard.html"
SERVER_PY = ROOT / "dashboard" / "server.py"


# ─── HTML structural checks (no live server needed) ─────────────────

def test_default_active_bot_is_sim():
    """Sprint G fix: default activeBot must be 'sim', not 'prod'.

    Operator complaint: opened the dashboard, saw $0 P&L, concluded
    bot wasn't trading. Root cause was the prod tab being default —
    prod runs only 2 strategies and often has 0 trades. Sim is where
    validation activity happens."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    # Match the actual assignment, ignoring nearby comment text
    m = re.search(r"^\s*let\s+activeBot\s*=\s*'(\w+)'\s*;",
                  text, re.MULTILINE)
    assert m, "couldn't find `let activeBot = '...'` declaration"
    assert m.group(1) == "sim", (
        f"default activeBot is '{m.group(1)}', expected 'sim' — "
        f"prod tab as default misleads operator (Sprint G fix)"
    )


def test_sim_tab_has_active_class_at_load():
    """The Sim tab must carry class='tab-btn active' on initial render
    so the visual state matches the activeBot variable."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    sim_idx = text.find('id="tab-sim"')
    prod_idx = text.find('id="tab-prod"')
    assert sim_idx > 0 and prod_idx > 0
    # Walk back from each tab-btn to find its class attribute
    sim_line_start = text.rfind("<button", 0, sim_idx)
    prod_line_start = text.rfind("<button", 0, prod_idx)
    sim_btn = text[sim_line_start:sim_idx + 50]
    prod_btn = text[prod_line_start:prod_idx + 50]
    assert "active" in sim_btn, (
        f"Sim Bot tab missing 'active' class:\n{sim_btn}"
    )
    assert "active" not in prod_btn, (
        f"Prod Bot tab still has 'active' class — both can't be active:"
        f"\n{prod_btn}"
    )


def test_combined_summary_bar_present():
    """The new both-bots summary card must be in the HTML so the
    operator always sees both bots' P&L without switching tabs."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    assert 'id="both-bots-summary"' in text, (
        "Sprint G's combined both-bots summary bar is missing"
    )
    # All four expected element IDs must be present (sim/prod × pnl/trades)
    for which in ("sim", "prod"):
        for what in ("pnl", "trades", "winrate"):
            elem = f'id="bb-{which}-{what}"'
            assert elem in text, f"summary bar missing element {elem}"


def test_html_has_today_pnl_fetcher():
    """The dashboard must call /api/today-pnl to populate the summary."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    assert "/api/today-pnl" in text, (
        "dashboard.html doesn't fetch /api/today-pnl — the summary "
        "bar will stay empty"
    )
    assert "refreshBothBotsSummary" in text, (
        "expected refreshBothBotsSummary() function to populate the "
        "summary bar"
    )


def test_summary_help_text_explains_validated_filter():
    """Help text must explain why prod has fewer strategies — a
    one-liner that prevents the next 'why is prod $0' question."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    assert "validated=True" in text or "validated</code>" in text or \
           "validated=" in text, (
        "summary bar should mention validated=True filter so operator "
        "understands why prod has fewer strategies"
    )


def test_summary_bar_renders_above_bot_tabs():
    """The summary bar should appear before the bot-tab section so the
    operator sees combined data first, then drills into a tab."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    summary_idx = text.find('id="both-bots-summary"')
    tab_bar_idx = text.find('class="tab-bar"')
    assert summary_idx > 0 and tab_bar_idx > 0
    assert summary_idx < tab_bar_idx, (
        "summary bar should appear before the tab bar in the DOM"
    )


# ─── Backend route checks (read source, not live server) ────────────

def test_server_has_today_pnl_route():
    """The Flask app must define /api/today-pnl (the summary bar
    depends on it)."""
    text = SERVER_PY.read_text(encoding="utf-8", errors="replace")
    assert '@app.route("/api/today-pnl")' in text, (
        "server.py is missing /api/today-pnl route — summary bar will "
        "404 on every poll"
    )


def test_today_pnl_returns_per_bot_shape():
    """Source-grep: today-pnl handler returns per_bot with sim/prod keys
    that include pnl, trades, win_rate — the shape the summary bar
    consumes. We assert the keys are present in the response builder."""
    text = SERVER_PY.read_text(encoding="utf-8", errors="replace")
    # Find the /api/today-pnl handler body
    start = text.find('@app.route("/api/today-pnl")')
    assert start > 0
    # Body extends to the next route or function def
    end_route = text.find("@app.route", start + 1)
    end_def = text.find("\ndef ", start + 200)
    end = min(x for x in (end_route, end_def, len(text)) if x > start)
    body = text[start:end]
    # The handler must populate per_bot with at least these keys
    for required in ('"per_bot"', '"pnl"', '"trades"', '"win_rate"'):
        assert required in body, (
            f"/api/today-pnl handler missing field {required}"
        )


# ─── Live-server integration (skipped if no server running) ─────────

DASH_PORT = int(os.environ.get("PHOENIX_DASHBOARD_PORT", "5000"))
DASH_BASE = f"http://localhost:{DASH_PORT}"


def _live_get(path: str, timeout: float = 2.0):
    """Best-effort GET; returns None if dashboard isn't running."""
    try:
        with urllib.request.urlopen(
            DASH_BASE + path, timeout=timeout
        ) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def test_live_today_pnl_endpoint_responds():
    """If the dashboard is running, /api/today-pnl returns valid JSON
    matching the contract. Skipped when the server isn't running."""
    r = _live_get("/api/today-pnl")
    if r is None:
        pytest.skip("dashboard not running on localhost:5000")
    status, body = r
    assert status == 200
    data = json.loads(body)
    assert "per_bot" in data, f"missing per_bot key: {list(data.keys())}"
    assert "trade_count" in data
    # If there's data, per-bot entries must have the right shape
    for bot_name, bot_data in (data.get("per_bot") or {}).items():
        assert "pnl" in bot_data, f"{bot_name} missing pnl"
        assert "trades" in bot_data, f"{bot_name} missing trades"
        assert "win_rate" in bot_data, f"{bot_name} missing win_rate"


def test_live_dashboard_html_loads():
    """If running, root URL returns HTML content with the new summary
    bar markup."""
    r = _live_get("/")
    if r is None:
        pytest.skip("dashboard not running on localhost:5000")
    status, body = r
    assert status == 200
    assert 'id="both-bots-summary"' in body, (
        "live dashboard HTML missing the new summary bar — "
        "Flask may be caching old template? Restart dashboard."
    )
    # Default tab assertion at the live layer too
    m = re.search(r"^\s*let\s+activeBot\s*=\s*'(\w+)'\s*;",
                  body, re.MULTILINE)
    if m:
        assert m.group(1) == "sim"
