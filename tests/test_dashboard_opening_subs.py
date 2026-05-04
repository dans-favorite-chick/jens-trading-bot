"""Sprint I (2026-05-03): dashboard expands opening_session into 6 sub-rows.

Operator request: the dashboard's strategy panel collapsed the 6 opening
sub-strategies into one umbrella row — invisible which sub was active
when, no per-sub trade count. This change expands the umbrella into the
6 sub-strategies (premarket_breakout, open_drive, open_test_drive, orb,
open_auction_in/out) with per-sub time-window status + today's trade
count.

Frontend-only change: dashboard/templates/dashboard.html. Backend (the
bots' strategies state) is unchanged — sub_strategy already flows
through in trades via signal metadata.

Tests verify the HTML structurally (no live browser needed), matching
the pattern in tests/test_dashboard_render.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = ROOT / "dashboard" / "templates" / "dashboard.html"


# Six sub-strategies from strategies/opening_session.py:evaluate(). The
# dashboard JS const OPENING_SUBS must list every one so each gets a
# row.
EXPECTED_SUBS = (
    "premarket_breakout",
    "open_drive",
    "open_test_drive",
    "orb",
    "open_auction_out",
    "open_auction_in",
)


# ─── const declaration ──────────────────────────────────────────────

def test_opening_subs_const_declared():
    """The OPENING_SUBS const must be declared in the dashboard JS."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    assert "const OPENING_SUBS" in text, (
        "dashboard.html missing OPENING_SUBS const — Sprint I "
        "expansion not wired"
    )


def test_all_six_subs_listed():
    """Every opening_session sub-strategy must appear in OPENING_SUBS."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    # Crude but robust: each sub name should appear as a 'name' key in
    # the const block.
    block = re.search(
        r"const OPENING_SUBS = \[(.*?)\];", text, re.DOTALL
    )
    assert block, "couldn't extract OPENING_SUBS literal"
    body = block.group(1)
    for sub in EXPECTED_SUBS:
        assert f"name: '{sub}'" in body, (
            f"OPENING_SUBS missing sub '{sub}' — dashboard won't "
            f"render a row for it"
        )


# ─── window strings match opening_session.py dispatcher ─────────────

def test_subs_have_time_windows():
    """Every sub entry must declare start + end times (HH:MM)."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    block = re.search(
        r"const OPENING_SUBS = \[(.*?)\];", text, re.DOTALL
    )
    body = block.group(1)
    # Six entries × (start + end) = at least 6 of each.
    starts = re.findall(r"start: '(\d{2}:\d{2})'", body)
    ends = re.findall(r"end: '(\d{2}:\d{2})'", body)
    assert len(starts) == 6, f"expected 6 start times, got {len(starts)}"
    assert len(ends) == 6, f"expected 6 end times, got {len(ends)}"


def test_orb_window_is_08_45_to_14_30():
    """ORB's window in the dashboard must match the dispatcher's
    is_in_window(now_ct, '08:45', '14:30') in opening_session.py."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    # Find the orb entry — name: 'orb' line, then nearby start/end.
    m = re.search(
        r"name: 'orb',\s*start: '(\d{2}:\d{2})',\s*end: '(\d{2}:\d{2})'",
        text,
    )
    assert m, "couldn't find orb entry in OPENING_SUBS"
    assert m.group(1) == "08:45"
    assert m.group(2) == "14:30"


# ─── renderStrategies signature accepts trades ──────────────────────

def test_render_strategies_takes_trades_arg():
    """renderStrategies must accept the trades list so per-sub trade
    counts can be computed."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"function renderStrategies\((.*?)\)", text)
    assert m, "couldn't find renderStrategies signature"
    args = m.group(1)
    assert "trades" in args, (
        f"renderStrategies signature is `({args})` — must include "
        f"`trades` so per-sub counts can be tallied"
    )


def test_poll_call_site_passes_trades():
    """The poll-loop wiring must pass bot.trades to renderStrategies."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    # Match the [strategies, ...] entry in the renders array.
    m = re.search(
        r"\['strategies',\s*\(\)\s*=>\s*renderStrategies\((.*?)\)\]",
        text,
    )
    assert m, "couldn't find renderStrategies call in poll renders[]"
    args = m.group(1)
    assert "bot.strategies" in args
    assert "bot.trades" in args, (
        f"renderStrategies call passes `{args}` but is missing "
        f"bot.trades — per-sub counts will always be 0"
    )


# ─── helper functions present ───────────────────────────────────────

def test_now_ct_minutes_helper_present():
    """The helper that computes current CT minutes-of-day must exist —
    in/out-of-window badges depend on it."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    assert "_nowCtMinutes" in text, (
        "_nowCtMinutes helper missing — sub-row in/out-of-window "
        "badges can't compute"
    )
    # Must use America/Chicago (host TZ may not be CT).
    assert "America/Chicago" in text, (
        "_nowCtMinutes must use America/Chicago — tz-naive logic "
        "is fragile if dashboard host runs in a non-CT zone"
    )


def test_count_trades_for_sub_filters_by_sub_strategy():
    """The trade-count helper must filter on both strategy and
    sub_strategy fields — anything looser would over-count."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    m = re.search(
        r"function _countTradesForSub\([^)]*\)\s*\{(.*?)\}",
        text, re.DOTALL,
    )
    assert m, "couldn't find _countTradesForSub helper"
    body = m.group(1)
    assert "t.strategy" in body
    assert "opening_session" in body
    assert "t.sub_strategy" in body, (
        "_countTradesForSub doesn't filter by sub_strategy — would "
        "show same count for every sub-row"
    )


# ─── CSS for sub-rows present ───────────────────────────────────────

def test_sub_row_css_classes_present():
    """The sub-row visual styles (indent + status badges) must be in
    the stylesheet."""
    text = HTML.read_text(encoding="utf-8", errors="replace")
    for cls in (".sub-row", ".sub-badge.in-window", ".sub-badge.closed",
                ".sub-name", ".sub-window", ".sub-count"):
        assert cls in text, f"CSS class {cls} missing from stylesheet"
