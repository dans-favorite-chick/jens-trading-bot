"""Tests for tools/replay_enrichment/recorded_cvd.RecordedCVDProvider.

Research/backtest tooling — uses a SMALL synthetic fixture (a temp jsonl)
rather than the large real logs/volumetric_history.jsonl so the test is fast
and hermetic.

Asserts:
  * the provider builds from a tiny recorded order-flow stream;
  * `health_at` returns a dict whose keys EXACTLY match the live
    `CVDTrendHealth.assess()` output keys;
  * results are deterministic across two calls;
  * minute aggregation + per-minute prior-lookup + session reset behave.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.cvd_trend_health import CVDTrendHealth  # noqa: E402
from tools.replay_enrichment.recorded_cvd import (  # noqa: E402
    RecordedCVDProvider,
    _floor_minute,
    _parse_ts,
)


# The exact keys the live bot reads from CVDTrendHealth.assess().
EXPECTED_ASSESS_KEYS = {
    "agreement",
    "veto",
    "price_slope",
    "cvd_slope",
    "n_bars",
    "reason",
}


def _rec(ts, close, delta, instrument="MNQM6"):
    """Build one synthetic volumetric_bar record (matches real shape)."""
    return {
        "type": "volumetric_bar",
        "ts": ts,
        "instrument": instrument,
        "bar_size_ticks": 1500,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "delta": delta,
        "buy_volume": max(delta, 0),
        "sell_volume": max(-delta, 0),
    }


# A tiny stream: several tick-bars across a handful of minutes, two of which
# share a minute (to exercise summing + last-close-wins), with steadily
# rising price + rising CVD (a healthy LONG trend).
SYNTHETIC = [
    _rec("2026-05-04T21:24:44.0330000", 27818.25, 176),
    _rec("2026-05-04T21:24:58.1000000", 27820.00, 90),   # same minute as above
    _rec("2026-05-04T21:25:54.6210000", 27830.00, 629),
    _rec("2026-05-04T21:26:54.9020000", 27835.25, 313),
    _rec("2026-05-04T21:27:10.0000000", 27840.00, 200),
    _rec("2026-05-04T21:28:01.0000000", 27845.50, 150),
    _rec("2026-05-04T21:29:30.0000000", 27851.00, 175),
]


@pytest.fixture
def jsonl_path(tmp_path):
    """Write SYNTHETIC out as a temp jsonl file (one record per line),
    including a blank line and a malformed line to exercise graceful skip."""
    p = tmp_path / "volumetric_history.jsonl"
    lines = [json.dumps(r) for r in SYNTHETIC]
    lines.insert(2, "")                 # blank line — must be skipped
    lines.insert(4, "{not valid json")  # garbage — must be skipped
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_provider_builds(jsonl_path):
    prov = RecordedCVDProvider(volumetric_path=jsonl_path, instrument="MNQM6")
    assert isinstance(prov, RecordedCVDProvider)
    # 7 records span 6 distinct minutes (21:24..21:29; the two 21:24 bars
    # collapse into one minute bucket).
    assert len(prov._minute_keys) == 6


def test_health_at_keys_match_live_assess(jsonl_path):
    prov = RecordedCVDProvider(volumetric_path=jsonl_path, instrument="MNQM6")

    # Independently compute the live key set from a fresh CVDTrendHealth so
    # the assertion can't drift if the source dict changes.
    live = CVDTrendHealth(lookback_bars=6, veto_threshold=-0.3)
    live.update_bar(100.0, 10.0)
    live.update_bar(101.0, 20.0)
    live_keys = set(live.assess("LONG").keys())
    assert live_keys == EXPECTED_ASSESS_KEYS  # guards against silent schema drift

    health = prov.health_at("2026-05-04T21:29:30", "LONG")
    assert health is not None
    assert isinstance(health, dict)
    assert set(health.keys()) == EXPECTED_ASSESS_KEYS == live_keys

    short = prov.health_at("2026-05-04T21:29:30", "SHORT")
    assert short is not None
    assert set(short.keys()) == EXPECTED_ASSESS_KEYS


def test_deterministic(jsonl_path):
    p1 = RecordedCVDProvider(volumetric_path=jsonl_path, instrument="MNQM6")
    p2 = RecordedCVDProvider(volumetric_path=jsonl_path, instrument="MNQM6")
    ts = "2026-05-04T21:28:45"
    a = p1.health_at(ts, "LONG")
    b = p2.health_at(ts, "LONG")
    assert a == b
    # Same provider, repeated call -> identical dict.
    assert p1.health_at(ts, "LONG") == p1.health_at(ts, "LONG")


def test_minute_alignment_and_summing(jsonl_path):
    prov = RecordedCVDProvider(volumetric_path=jsonl_path, instrument="MNQM6")
    minutes = prov._aggregate_minutes()
    # First minute bucket = 21:24, delta summed (176 + 90 = 266),
    # close = last tick-bar in the minute (27820.00).
    first_key, first_close, first_delta = minutes[0]
    assert first_key == _parse_ts("2026-05-04T21:24:00")
    assert first_delta == 266
    assert first_close == 27820.00


def test_prior_minute_lookup(jsonl_path):
    prov = RecordedCVDProvider(volumetric_path=jsonl_path, instrument="MNQM6")
    # 21:26:30 is not an exact recorded minute key boundary issue — 21:26 IS
    # recorded; query a second within an existing minute returns that minute.
    exact = prov.health_at("2026-05-04T21:26:15", "LONG")
    assert exact is not None
    # A ts after coverage but same day -> most recent prior minute.
    after = prov.health_at("2026-05-04T21:45:00", "LONG")
    assert after is not None
    assert after == prov.health_at("2026-05-04T21:29:00", "LONG")


def test_before_coverage_returns_none(jsonl_path):
    prov = RecordedCVDProvider(volumetric_path=jsonl_path, instrument="MNQM6")
    assert prov.health_at("2026-05-04T20:00:00", "LONG") is None


def test_missing_file_is_empty_provider(tmp_path):
    prov = RecordedCVDProvider(volumetric_path=str(tmp_path / "nope.jsonl"))
    assert prov.health_at("2026-05-04T21:29:30", "LONG") is None


def test_instrument_filter(jsonl_path, tmp_path):
    # Add a foreign-instrument record; filtering to MNQM6 must drop it.
    p = tmp_path / "mixed.jsonl"
    recs = list(SYNTHETIC) + [_rec("2026-05-04T21:30:00.0", 99999, 9999, "ESZ9")]
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    prov = RecordedCVDProvider(volumetric_path=str(p), instrument="MNQM6")
    # The foreign 21:30 minute must NOT be present.
    assert _floor_minute(_parse_ts("2026-05-04T21:30:00")) not in prov._cache


def test_session_reset_on_date_change(tmp_path):
    # Two records on different calendar dates: cumulative CVD must reset, so
    # the second day's first bucket has insufficient history (n_bars resets
    # behavior is on the deque, but cumulative CVD resets to that day's delta).
    recs = [
        _rec("2026-05-04T23:59:30.0", 100.0, 50),
        _rec("2026-05-05T00:00:30.0", 101.0, 60),
        _rec("2026-05-05T00:01:30.0", 102.0, 70),
    ]
    p = tmp_path / "twoday.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    prov = RecordedCVDProvider(volumetric_path=str(p), instrument="MNQM6")
    mins = prov._aggregate_minutes()
    assert len(mins) == 3
    # Sanity: a health dict exists for the second-day minute.
    h = prov.health_at("2026-05-05T00:01:30", "LONG")
    assert h is not None
    assert set(h.keys()) == EXPECTED_ASSESS_KEYS
