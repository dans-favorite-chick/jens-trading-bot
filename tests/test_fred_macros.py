"""Tests for core.macros.fred_feed and core.macros.regime_history.

Phoenix Phase B+ §3.3 — structured FRED macro feed.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.macros.fred_feed import (  # noqa: E402
    FredMacroFeed,
    MacroSnapshot,
    RegimeShiftEvent,
    SERIES_DFF,
    SERIES_UNRATE,
    SERIES_T10Y2Y,
    SERIES_CPIAUCSL,
)
from core.macros.regime_history import RegimeHistory  # noqa: E402


# ----- Helpers -----


class _FakeResponse:
    """Minimal urlopen response stand-in (context-manager + read())."""

    def __init__(self, body: dict, status: int = 200):
        self._buf = BytesIO(json.dumps(body).encode("utf-8"))
        self.status = status

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _series_response(series_id: str, value: str, date: str = "2026-04-25") -> dict:
    return {
        "observations": [{"series_id": series_id, "value": value, "date": date}],
    }


def _cpi_response(latest: float, year_ago: float) -> dict:
    """13 monthly CPI obs, newest first."""
    obs = [{"value": str(latest), "date": "2026-04-01"}]
    # 11 filler months between latest and year_ago
    for i in range(11):
        obs.append({"value": str(latest - (latest - year_ago) * (i + 1) / 12), "date": ""})
    obs.append({"value": str(year_ago), "date": "2025-04-01"})
    return {"observations": obs}


def _make_router(value_map: dict[str, dict], call_log: list | None = None):
    """Return a fake urlopen() callable that routes by ?series_id=... param.

    value_map: {series_id: response_body_dict}
    call_log:  optional list to append (series_id, hit_count) for assertions
    """
    counts: dict[str, int] = {}

    def _fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        # Pull series_id out of the query string
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        sid = (q.get("series_id") or [""])[0]
        counts[sid] = counts.get(sid, 0) + 1
        if call_log is not None:
            call_log.append((sid, counts[sid]))
        if sid not in value_map:
            return _FakeResponse({"observations": []})
        return _FakeResponse(value_map[sid])

    _fake_urlopen.counts = counts  # type: ignore[attr-defined]
    return _fake_urlopen


# ----- Tests -----


def test_single_series_fetch_returns_expected_value(tmp_path):
    """A bare-bones one-shot fetch parses the FRED JSON correctly."""
    feed = FredMacroFeed(api_key="testkey", cache_dir=tmp_path / "cache", cache_ttl_s=3600)
    fake = _make_router({SERIES_DFF: _series_response("DFF", "5.33")})
    with patch("core.macros.fred_feed.urllib.request.urlopen", fake):
        v = feed._fetch_series_value(SERIES_DFF)
    assert v == 5.33


def test_all_series_snapshot_dataclass_populated(tmp_path):
    """get_snapshot() populates all four MacroSnapshot fields."""
    feed = FredMacroFeed(api_key="testkey", cache_dir=tmp_path / "cache", cache_ttl_s=3600)
    fake = _make_router({
        SERIES_DFF: _series_response("DFF", "5.25"),
        SERIES_UNRATE: _series_response("UNRATE", "3.9"),
        SERIES_T10Y2Y: _series_response("T10Y2Y", "0.18"),
        SERIES_CPIAUCSL: _cpi_response(latest=320.0, year_ago=310.0),
    })
    with patch("core.macros.fred_feed.urllib.request.urlopen", fake):
        snap = feed.get_snapshot()
    assert isinstance(snap, MacroSnapshot)
    assert snap.ffr == 5.25
    assert snap.unemployment == 3.9
    assert snap.yield_curve_2y10y == 0.18
    assert snap.cpi_yoy is not None
    # (320 - 310) / 310 * 100 = 3.225...
    assert abs(snap.cpi_yoy - 3.23) < 0.05
    assert snap.fetched_at_iso  # non-empty ISO string


def test_ttl_cache_hit_skips_http(tmp_path):
    """Second call within TTL must NOT re-issue HTTP."""
    feed = FredMacroFeed(api_key="testkey", cache_dir=tmp_path / "cache", cache_ttl_s=3600)
    fake = _make_router({SERIES_DFF: _series_response("DFF", "5.33")})
    with patch("core.macros.fred_feed.urllib.request.urlopen", fake):
        v1 = feed._fetch_series_value(SERIES_DFF)
        v2 = feed._fetch_series_value(SERIES_DFF)
    assert v1 == v2 == 5.33
    # Only one HTTP call for SERIES_DFF
    assert fake.counts.get(SERIES_DFF, 0) == 1


def test_ttl_cache_expiry_triggers_refresh(tmp_path):
    """Second call after TTL expiry MUST re-issue HTTP."""
    # 1-second TTL
    feed = FredMacroFeed(api_key="testkey", cache_dir=tmp_path / "cache", cache_ttl_s=1)
    fake = _make_router({SERIES_DFF: _series_response("DFF", "5.33")})
    with patch("core.macros.fred_feed.urllib.request.urlopen", fake):
        feed._fetch_series_value(SERIES_DFF)
        # Forge an expired cache by rewriting cached_at
        cache_file = (tmp_path / "cache" / "fred_DFF.json")
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        payload["cached_at"] = time.time() - 7200  # 2 hours ago
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
        feed._fetch_series_value(SERIES_DFF)
    assert fake.counts.get(SERIES_DFF, 0) == 2


def test_regime_shift_ffr_change_detects_event(tmp_path):
    """A 25bp FFR change yields exactly one FFR_CUT or FFR_HIKE event."""
    prev = MacroSnapshot(
        ffr=5.50, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=0.20, fetched_at_iso="2026-04-24T00:00:00+00:00",
    )
    curr = MacroSnapshot(
        ffr=5.25, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=0.20, fetched_at_iso="2026-04-25T00:00:00+00:00",
    )
    events = FredMacroFeed.detect_regime_shift(prev, curr)
    ffr_events = [e for e in events if e.series == "DFF"]
    assert len(ffr_events) == 1
    assert ffr_events[0].direction == "FFR_CUT"
    assert abs(ffr_events[0].magnitude - 0.25) < 1e-9

    # Hike case
    curr_hike = MacroSnapshot(
        ffr=5.75, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=0.20, fetched_at_iso="2026-04-25T00:00:00+00:00",
    )
    events_hike = FredMacroFeed.detect_regime_shift(prev, curr_hike)
    ffr_hike = [e for e in events_hike if e.series == "DFF"]
    assert len(ffr_hike) == 1
    assert ffr_hike[0].direction == "FFR_HIKE"


def test_regime_shift_unrate_flat_returns_empty(tmp_path):
    """Identical UNRATE between snapshots produces no UNRATE event."""
    prev = MacroSnapshot(
        ffr=5.25, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=0.20, fetched_at_iso="2026-04-24T00:00:00+00:00",
    )
    curr = MacroSnapshot(
        ffr=5.25, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=0.20, fetched_at_iso="2026-04-25T00:00:00+00:00",
    )
    events = FredMacroFeed.detect_regime_shift(prev, curr)
    assert events == []


def test_regime_shift_curve_inversion_flip(tmp_path):
    """Sign flip on T10Y2Y produces a CURVE_INVERSION event."""
    prev = MacroSnapshot(
        ffr=5.25, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=0.05, fetched_at_iso="2026-04-24T00:00:00+00:00",
    )
    curr = MacroSnapshot(
        ffr=5.25, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=-0.10, fetched_at_iso="2026-04-25T00:00:00+00:00",
    )
    events = FredMacroFeed.detect_regime_shift(prev, curr)
    curve = [e for e in events if e.series == "T10Y2Y"]
    assert len(curve) == 1
    assert curve[0].direction == "CURVE_INVERSION"


def test_network_error_falls_back_to_cache_no_raise(tmp_path, caplog):
    """On HTTP failure, return last cached value (even if expired) and WARN."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    # Pre-seed an expired cache for DFF
    (cache_dir / "fred_DFF.json").write_text(
        json.dumps({
            "series_id": "DFF",
            "value": 4.50,
            "cached_at": time.time() - 999_999,  # very stale
            "obs_date": "2025-01-01",
        }),
        encoding="utf-8",
    )

    feed = FredMacroFeed(api_key="testkey", cache_dir=cache_dir, cache_ttl_s=3600)

    def _boom(req, timeout=None):
        raise OSError("network down")

    caplog.set_level(logging.WARNING, logger="FredMacroFeed")
    with patch("core.macros.fred_feed.urllib.request.urlopen", _boom):
        v = feed._fetch_series_value(SERIES_DFF)

    assert v == 4.50  # served from stale cache
    assert any("FRED fetch" in rec.message for rec in caplog.records)


# ----- RegimeHistory (bonus persistence smoke) -----


def test_regime_history_record_and_recall(tmp_path):
    """RegimeHistory round-trips snapshot + shifts."""
    hist_path = tmp_path / "regime_history.jsonl"
    hist = RegimeHistory(path=hist_path)

    snap = MacroSnapshot(
        ffr=5.25, cpi_yoy=3.0, unemployment=3.9,
        yield_curve_2y10y=-0.10,
        fetched_at_iso="2026-04-25T00:00:00+00:00",
    )
    shifts = [
        RegimeShiftEvent(
            series="T10Y2Y", prev_value=0.05, curr_value=-0.10,
            magnitude=0.15, direction="CURVE_INVERSION",
        )
    ]
    hist.record(snap, shifts)

    last = hist.get_last_snapshot()
    assert last is not None
    assert last.ffr == 5.25
    assert last.yield_curve_2y10y == -0.10

    recent = hist.get_recent_shifts(hours=24)
    assert len(recent) == 1
    assert recent[0].direction == "CURVE_INVERSION"
