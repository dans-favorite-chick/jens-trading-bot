"""Cross-strategy correlation audit (#25, 2026-05-13).

The audit tool buckets every trade entry by time window and computes
co-firing stats per strategy pair (Jaccard index + conditionals).
These tests cover the binning + pair-stat math; the real-data
smoke runs in the CLI and isn't reproducible in CI without trade logs.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.strategy_correlation_audit import (
    _bin_timestamp,
    compute_correlation,
    render_markdown,
)


# ── _bin_timestamp ─────────────────────────────────────────────────────

def test_bin_epoch_seconds():
    """Two trades 200s apart in a 300s window starting at a bucket
    boundary → same bucket. 600s later → different bucket."""
    base = 1700000000 - (1700000000 % 300)  # round to bucket start
    t1 = {"entry_time": base}
    t2 = {"entry_time": base + 200}
    t3 = {"entry_time": base + 600}
    b1 = _bin_timestamp(t1, 300)
    b2 = _bin_timestamp(t2, 300)
    b3 = _bin_timestamp(t3, 300)
    assert b1 == b2
    assert b1 != b3


def test_bin_iso_string_with_tz():
    t = {"ts": "2026-05-13T09:30:00-05:00"}
    b = _bin_timestamp(t, 300)
    assert b is not None


def test_bin_falls_back_through_field_names():
    """If 'entry_time' is missing, try 'ts', 'exit_ts_ct', 'recorded_at'."""
    t = {"recorded_at": 1700000000}
    assert _bin_timestamp(t, 300) is not None


def test_bin_returns_none_when_no_timestamp():
    assert _bin_timestamp({}, 300) is None


# ── compute_correlation ────────────────────────────────────────────────

def test_perfectly_correlated_pair_jaccard_1():
    """Two strategies firing in exactly the same windows → Jaccard = 1."""
    base = 1700000000
    trades = [
        {"strategy": "a", "entry_time": base},
        {"strategy": "b", "entry_time": base + 10},  # same 300s bucket
        {"strategy": "a", "entry_time": base + 600},
        {"strategy": "b", "entry_time": base + 610},  # same bucket
    ]
    stats = compute_correlation(trades, window_seconds=300)
    assert len(stats) == 1
    s = stats[0]
    assert s["jaccard"] == 1.0
    assert s["confirmed_ab"] == 1.0
    assert s["confirmed_ba"] == 1.0


def test_independent_pair_jaccard_0():
    """Strategies firing in disjoint windows → Jaccard = 0."""
    trades = [
        {"strategy": "a", "entry_time": 1700000000},
        {"strategy": "b", "entry_time": 1700001000},  # different bucket
    ]
    stats = compute_correlation(trades, window_seconds=300)
    assert len(stats) == 1
    s = stats[0]
    assert s["jaccard"] == 0.0
    assert s["co_fires"] == 0


def test_partial_overlap_math():
    """A fires in 3 windows, B fires in 4 windows, 2 are shared.
    Jaccard = 2 / (3 + 4 - 2) = 2/5 = 0.4
    conf(a→b) = 2/3 = 0.667
    conf(b→a) = 2/4 = 0.5"""
    base = 1700000000
    trades = [
        {"strategy": "a", "entry_time": base},           # bucket 0
        {"strategy": "a", "entry_time": base + 600},     # bucket 2 — shared
        {"strategy": "a", "entry_time": base + 1200},    # bucket 4 — shared
        {"strategy": "b", "entry_time": base + 600},     # bucket 2 — shared
        {"strategy": "b", "entry_time": base + 1200},    # bucket 4 — shared
        {"strategy": "b", "entry_time": base + 1800},    # bucket 6
        {"strategy": "b", "entry_time": base + 2400},    # bucket 8
    ]
    stats = compute_correlation(trades, window_seconds=300)
    s = stats[0]
    assert s["jaccard"] == 0.4
    assert s["confirmed_ab"] == pytest.approx(0.667, abs=0.01)
    assert s["confirmed_ba"] == 0.5


def test_pair_ordering_alphabetical():
    """Each pair appears once, with a < b. No (b, a) duplicate."""
    base = 1700000000
    trades = [
        {"strategy": "zeta", "entry_time": base},
        {"strategy": "alpha", "entry_time": base},
    ]
    stats = compute_correlation(trades, window_seconds=300)
    assert len(stats) == 1
    assert stats[0]["a"] == "alpha"
    assert stats[0]["b"] == "zeta"


def test_results_sorted_by_jaccard_desc():
    base = 1700000000
    # Pair (a, b): jaccard 1.0; pair (c, d): jaccard 0
    trades = [
        {"strategy": "a", "entry_time": base},
        {"strategy": "b", "entry_time": base + 10},  # co-fire
        {"strategy": "c", "entry_time": base + 1000},
        {"strategy": "d", "entry_time": base + 5000},  # no co-fire
    ]
    stats = compute_correlation(trades, window_seconds=300)
    assert stats[0]["jaccard"] >= stats[-1]["jaccard"]


# ── render_markdown ────────────────────────────────────────────────────

def test_render_flags_high_jaccard():
    """Pairs with Jaccard >= 0.3 get a ⚠️ in the rendered table."""
    stats = [{
        "a": "x", "b": "y", "n_a": 10, "n_b": 10, "co_fires": 5,
        "jaccard": 0.5, "confirmed_ab": 0.5, "confirmed_ba": 0.5,
    }]
    md = render_markdown(stats, window_seconds=300)
    assert "⚠️" in md


def test_render_no_flag_below_threshold():
    stats = [{
        "a": "x", "b": "y", "n_a": 10, "n_b": 10, "co_fires": 1,
        "jaccard": 0.05, "confirmed_ab": 0.1, "confirmed_ba": 0.1,
    }]
    md = render_markdown(stats, window_seconds=300)
    assert "⚠️" not in md


def test_render_empty_stats_emits_friendly_message():
    md = render_markdown([], window_seconds=300)
    assert "No co-fires found" in md
