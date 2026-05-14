"""ORB state persistence across bot restarts (#13, 2026-05-13).

Before: a bot restart at 09:00 (15min after market open) lost the OR
high/low observed during 8:30-8:45 — meaning the strategy would either
silently fail (no OR set) or, worse, re-observe a NEW (post-restart)
"OR" window and treat that as the day's range.

After: OR state (high, low, set flag, date, traded flag) is persisted
to `logs/orb_state_<bot>.json` on every mutation, loaded on __init__,
and discarded silently if the saved date doesn't match today (ET).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")


def _orb_state_path(tmp_path: Path, bot_name: str = "test") -> Path:
    """Compute where ORB's __init__ would land its state file when the
    PROJECT_ROOT is overridden to tmp_path."""
    return tmp_path / "logs" / f"orb_state_{bot_name}.json"


@pytest.fixture
def isolated_orb(tmp_path, monkeypatch):
    """Patch the ORB state path to tmp_path so tests don't pollute logs/."""
    # Stub PROJECT_ROOT so the strategy lands its state file under tmp.
    import config.settings as _s
    monkeypatch.setattr(_s, "PROJECT_ROOT", str(tmp_path), raising=False)
    yield tmp_path


def _make_orb(bot_name: str = "test"):
    from strategies.orb import OpeningRangeBreakout
    return OpeningRangeBreakout({
        "bot_name": bot_name,
        "is_prod_bot": False,
        "or_duration_minutes": 15,
    })


# ── Save / load round-trip ─────────────────────────────────────────────

def test_save_then_load_restores_or_range(isolated_orb):
    orb = _make_orb()
    today_et = datetime.now(_ET).strftime("%Y-%m-%d")
    orb._or_high = 20050.0
    orb._or_low = 20000.0
    orb._or_set = True
    orb._or_date = today_et
    orb._traded_today = False
    orb._save_state()
    # Fresh instance should load the state
    orb2 = _make_orb()
    assert orb2._or_high == 20050.0
    assert orb2._or_low == 20000.0
    assert orb2._or_set is True
    assert orb2._traded_today is False


def test_traded_flag_survives_restart(isolated_orb):
    """The whole point: a restart must not let ORB trade twice in a day."""
    orb = _make_orb()
    today_et = datetime.now(_ET).strftime("%Y-%m-%d")
    orb._or_high = 20050.0
    orb._or_low = 20000.0
    orb._or_set = True
    orb._or_date = today_et
    orb._traded_today = True
    orb._save_state()
    orb2 = _make_orb()
    assert orb2._traded_today is True


def test_stale_state_silently_discarded(isolated_orb):
    """Saved date != today (ET): must not restore. Otherwise a Friday-
    night restart on Monday morning would "remember" Friday's OR."""
    orb = _make_orb()
    orb._or_high = 20050.0
    orb._or_low = 20000.0
    orb._or_set = True
    orb._or_date = "2020-01-01"  # Definitely not today
    orb._traded_today = True
    orb._save_state()
    orb2 = _make_orb()
    assert orb2._or_high is None
    assert orb2._or_low is None
    assert orb2._or_set is False
    assert orb2._traded_today is False


def test_missing_state_file_is_silent(isolated_orb):
    """No state file (first session on a clean install): __init__ must
    not crash, and state must remain in fresh defaults."""
    orb = _make_orb()
    assert orb._or_high is None
    assert orb._or_low is None
    assert orb._or_set is False


def test_corrupted_state_file_is_silent(isolated_orb):
    """A malformed JSON file (e.g. interrupted write) must not crash
    the strategy on next startup. State stays in defaults."""
    path = _orb_state_path(isolated_orb, "test")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    orb = _make_orb()
    assert orb._or_high is None
    assert orb._or_set is False


# ── Save trigger points ────────────────────────────────────────────────

def test_reset_daily_persists_clean_state(isolated_orb):
    """After _reset_daily, the saved state should reflect cleared OR."""
    orb = _make_orb()
    orb._reset_daily("2026-05-13")
    path = _orb_state_path(isolated_orb, "test")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["or_high"] is None
    assert data["or_low"] is None
    assert data["or_set"] is False
    assert data["traded_today"] is False
    assert data["or_date"] == "2026-05-13"


# ── #13 regression: post-restart entry-window cutoff ──────────────────

def test_session_start_ts_survives_restart(isolated_orb):
    """Regression: before the fix, _or_bars_1m didn't survive restart
    (bar objects don't JSON-serialize), so Step 3 in evaluate() did
    `_or_bars_1m[0].start_time` → IndexError → silently passed the
    max_entry_delay_min cutoff. The bot could trade past the cutoff
    after any mid-session restart.

    Fix: persist the first OR bar's start_time as a separate scalar
    field (_or_session_start_ts) so Step 3 can still gate correctly
    even when _or_bars_1m is empty."""
    orb = _make_orb()
    today_et = datetime.now(_ET).strftime("%Y-%m-%d")
    orb._or_high = 20050.0
    orb._or_low = 20000.0
    orb._or_set = True
    orb._or_date = today_et
    orb._traded_today = False
    orb._or_session_start_ts = 1700000000.0  # arbitrary epoch
    orb._save_state()
    # Verify it's actually in the JSON on disk
    path = _orb_state_path(isolated_orb, "test")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["or_session_start_ts"] == 1700000000.0
    # And the new instance restores it
    orb2 = _make_orb()
    assert orb2._or_session_start_ts == 1700000000.0


def test_session_start_ts_missing_in_legacy_state_doesnt_crash(isolated_orb):
    """A state file written BEFORE this field existed should still load
    cleanly; _or_session_start_ts just stays None (fallback to
    _or_bars_1m[0] in evaluate, which on restart is empty — so the
    cutoff is bypassed, matching pre-fix behavior). No crash."""
    path = _orb_state_path(isolated_orb, "test")
    path.parent.mkdir(parents=True, exist_ok=True)
    today_et = datetime.now(_ET).strftime("%Y-%m-%d")
    # Simulate the OLD schema with no or_session_start_ts key
    path.write_text(json.dumps({
        "or_high": 20050.0, "or_low": 20000.0, "or_set": True,
        "or_date": today_et, "traded_today": False,
    }), encoding="utf-8")
    orb = _make_orb()
    assert orb._or_high == 20050.0
    assert orb._or_session_start_ts is None  # legacy file → None
