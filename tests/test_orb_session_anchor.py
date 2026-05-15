"""ORB session-anchor fix (2026-05-15).

Before: the daily reset fired at ET-midnight (or bot startup), so the
"Opening Range" was built from arbitrary overnight bars — today's OR
was 393pt wide vs the 80pt cap, producing 3,923 `gate:or_too_wide`
rejections on a single sim day. Strategy fired zero signals.

After: the daily reset anchors to the configured `session_open_et`
(default 09:30 ET = US cash open, matches Zarattini's published spec).
The OR is built from the first 15 1m bars AFTER session_open. Bars
before session_open are excluded from the OR.

These tests pin the new anchor + the "ignore pre-session bars" rule.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")


def _make_orb(bot_name: str | None = "test_anchor", session_open_et: str = "09:30"):
    """Build an ORB with persistence opted in to test_anchor file."""
    from strategies.orb import OpeningRangeBreakout
    return OpeningRangeBreakout({
        "bot_name": bot_name,
        "is_prod_bot": False,
        "or_duration_minutes": 15,
        "session_open_et": session_open_et,
        "min_or_size_points": 5,
        "max_or_size_points": 80,
        "max_or_size_atr_mult": 4.0,
        "max_or_size_hard_cap_points": 150,
        "max_stop_points": 25,
        "stop_buffer_ticks": 2,
        "target_rr": 2.0,
        "max_entry_delay_minutes": 60,
    })


def _bar(end_dt_et: datetime, high: float, low: float, close: float):
    """Minimal Bar-like object that ORB iterates."""
    return SimpleNamespace(
        end_time=end_dt_et.timestamp(),
        start_time=(end_dt_et - timedelta(minutes=1)).timestamp(),
        high=high, low=low, close=close, open=close,
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test ORB state isolation."""
    import config.settings as _s
    monkeypatch.setattr(_s, "PROJECT_ROOT", str(tmp_path), raising=False)
    yield


# ── _session_open_today_et ─────────────────────────────────────────────

def test_session_open_uses_default_0930_et():
    orb = _make_orb()
    ref = datetime(2026, 5, 15, 14, 0, tzinfo=_ET)  # 14:00 ET
    open_dt = orb._session_open_today_et(ref)
    assert open_dt == datetime(2026, 5, 15, 9, 30, tzinfo=_ET)


def test_session_open_before_open_returns_yesterday():
    """At 07:00 ET (pre-open), the 'current session' is yesterday's
    open carried through overnight."""
    orb = _make_orb()
    ref = datetime(2026, 5, 15, 7, 0, tzinfo=_ET)
    open_dt = orb._session_open_today_et(ref)
    assert open_dt == datetime(2026, 5, 14, 9, 30, tzinfo=_ET)


def test_session_open_at_exactly_open_returns_today():
    """09:30 ET on the dot = today's session has started."""
    orb = _make_orb()
    ref = datetime(2026, 5, 15, 9, 30, tzinfo=_ET)
    assert orb._session_open_today_et(ref) == ref


def test_session_open_respects_custom_config():
    """Per-strategy override (e.g. 08:30 ET for futures cash open)."""
    orb = _make_orb(session_open_et="08:30")
    ref = datetime(2026, 5, 15, 14, 0, tzinfo=_ET)
    assert orb._session_open_today_et(ref).hour == 8
    assert orb._session_open_today_et(ref).minute == 30


def test_session_open_handles_malformed_config():
    """Bad config string falls back to 09:30, not a crash."""
    orb = _make_orb(session_open_et="not-a-time")
    ref = datetime(2026, 5, 15, 14, 0, tzinfo=_ET)
    open_dt = orb._session_open_today_et(ref)
    assert open_dt.hour == 9
    assert open_dt.minute == 30


# ── evaluate(): pre-open bars don't pollute the OR ─────────────────────

def test_pre_session_bars_excluded_from_or():
    """Regression: pre-fix, bars from 03:00-09:29 ET formed the OR,
    producing 100-400pt-wide ranges. After fix, only bars at-or-after
    09:30 ET count."""
    orb = _make_orb()
    open_dt = datetime(2026, 5, 15, 9, 30, tzinfo=_ET)
    # Overnight chop: huge range from 03:00 to 09:29 ET
    overnight = [
        _bar(open_dt - timedelta(hours=6, minutes=i), high=29900, low=29200, close=29500)
        for i in range(60)
    ]
    # Session bars: tight range 9:30-9:44 ET, 15 bars
    session = [
        _bar(open_dt + timedelta(minutes=i), high=29550, low=29530, close=29540)
        for i in range(15)
    ]
    bars_1m = overnight + session
    # Drive evaluate() — should ignore overnight, build OR from session
    market = {"price": 29540.0, "atr_5m": 5.0}
    orb.evaluate(market, bars_5m=[], bars_1m=bars_1m, session_info={})
    # OR high/low should come from SESSION bars only, not overnight
    assert orb._or_set is True, "expected OR set after 15 session bars"
    assert orb._or_high == 29550.0, f"or_high contaminated: {orb._or_high}"
    assert orb._or_low == 29530.0, f"or_low contaminated: {orb._or_low}"
    or_size = orb._or_high - orb._or_low
    assert or_size == 20.0, f"OR size {or_size}pt; pre-fix would have been 700pt"


def test_or_filter_has_upper_bound_or_duration():
    """Critical: the OR must come from bars in [open, open+or_duration),
    NOT just `>= open`. Pre-fix, after the daily reset at session open,
    the deque held 200 overnight bars all >= session_open_ts; the
    `[:15]` slice grabbed the OLDEST 15 of those (= 3-hour-old overnight
    chop) and called it the OR. The upper-bound fix excludes them."""
    orb = _make_orb()
    open_dt = datetime(2026, 5, 15, 9, 30, tzinfo=_ET)
    # Mix: 200 overnight bars covering 06:10-09:29 ET (all >= yesterday
    # 09:30 ET in epoch terms BUT outside today's OR window), then 15
    # session bars 09:30-09:44 ET (tight 20pt range).
    overnight = [
        _bar(open_dt - timedelta(minutes=200 - i),  # 06:10 ET onward
             high=29900, low=29200, close=29500)
        for i in range(200)
    ]
    session = [
        _bar(open_dt + timedelta(minutes=i),
             high=29550, low=29530, close=29540)
        for i in range(15)
    ]
    market = {"price": 29540.0, "atr_5m": 5.0}
    orb.evaluate(market, bars_5m=[], bars_1m=overnight + session,
                 session_info={})
    assert orb._or_set is True
    # The OR must be 20pt (from session bars), NOT 700pt (overnight)
    assert orb._or_high == 29550.0, (
        f"upper-bound filter failed: or_high={orb._or_high} "
        f"(should be 29550 from session bars, not 29900 from overnight)"
    )
    assert orb._or_low == 29530.0
    assert (orb._or_high - orb._or_low) == 20.0


def test_carryover_with_no_window_bars_skips_cleanly():
    """When the bot starts up at 03:00 ET (mid-overnight), the deque
    has bars from this morning — all OUTSIDE yesterday's OR window
    (09:30-09:45 ET yesterday). The strategy must not fabricate an
    OR from random overnight bars; it should SKIP cleanly."""
    orb = _make_orb()
    # Now is 03:00 ET 2026-05-15. Session-day = yesterday's 09:30 ET.
    overnight_now = datetime(2026, 5, 15, 3, 0, tzinfo=_ET)
    # 30 bars of this morning's overnight chop (00:30-03:00 ET)
    bars_1m = [
        _bar(overnight_now - timedelta(minutes=30 - i),
             high=29900, low=29200, close=29500)
        for i in range(30)
    ]
    market = {"price": 29500.0, "atr_5m": 5.0}
    result = orb.evaluate(market, bars_5m=[], bars_1m=bars_1m,
                          session_info={})
    assert result is None, "must not signal during overnight"
    assert orb._or_set is False, (
        "must not fabricate an OR — yesterday's window already passed, "
        "today's window hasn't opened. Wait for the real 09:30 ET bar."
    )


def test_overnight_evaluation_does_not_emit_signal():
    """Pre-fix: bars at 03:00 ET could form an OR and fire signals
    during dead-of-night Asian session. With the session-anchor in
    place, evaluate() at 03:00 ET should not emit a SIGNAL — either
    because the session-day carries yesterday's OR (entry window
    expired) or because there aren't enough bars after this session's
    open yet."""
    orb = _make_orb()
    # 03:00 ET 2026-05-15 — yesterday's session was 09:30 ET 2026-05-14.
    # 17+ hours after open, well past max_entry_delay_minutes=60.
    overnight = datetime(2026, 5, 15, 3, 0, tzinfo=_ET)
    bars_1m = [_bar(overnight + timedelta(minutes=i), 29500, 29490, 29495)
               for i in range(30)]
    market = {"price": 29495.0, "atr_5m": 5.0}
    result = orb.evaluate(market, bars_5m=[], bars_1m=bars_1m, session_info={})
    assert result is None, (
        "ORB must not fire SIGNAL during overnight — pre-fix it would have "
        "built a phantom OR from these bars and shipped a breakout."
    )


def test_session_start_ts_anchored_to_open_not_first_bar():
    """The persisted session_start_ts must be the configured 09:30 ET,
    not the first-bar's start_time (which can drift by a few seconds)."""
    orb = _make_orb()
    open_dt = datetime(2026, 5, 15, 9, 30, tzinfo=_ET)
    # First bar starts 7 seconds AFTER 09:30 (real-world tick clustering)
    session = [
        _bar(open_dt + timedelta(seconds=7) + timedelta(minutes=i),
             high=29550, low=29530, close=29540)
        for i in range(15)
    ]
    market = {"price": 29540.0, "atr_5m": 5.0}
    orb.evaluate(market, bars_5m=[], bars_1m=session, session_info={})
    assert orb._or_set is True
    # session_start_ts should be EXACTLY 09:30 ET, not 09:30:07
    assert orb._or_session_start_ts == open_dt.timestamp(), (
        f"expected exact session_open anchor; got drift "
        f"{orb._or_session_start_ts - open_dt.timestamp()}s"
    )


# ── Config / pin tests ────────────────────────────────────────────────

def test_config_carries_session_open_et():
    """The strategy config must ship `session_open_et` so the anchor
    is configurable + the fix is durable across reloads."""
    from config.strategies import STRATEGIES
    assert STRATEGIES["orb"].get("session_open_et") == "09:30", (
        "orb.session_open_et must default to 09:30 ET (Zarattini spec)."
    )


def test_source_no_longer_anchors_on_et_midnight():
    """Regression pin: the old `today = bar_dt.strftime('%Y-%m-%d')`
    pattern (which produced ET-midnight resets) must NOT be the source
    of `today` anymore. The fix routes through `_session_open_today_et`."""
    src = (ROOT / "strategies" / "orb.py").read_text(encoding="utf-8")
    # Must call the new helper
    assert "_session_open_today_et(bar_dt)" in src, (
        "ORB evaluate() must compute today off _session_open_today_et."
    )
    # Must derive `today` from the session-open datetime
    assert "today = session_open_et.strftime" in src, (
        "today must be derived from session_open_et, not bar_dt directly."
    )
