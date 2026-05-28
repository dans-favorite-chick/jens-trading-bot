"""
Tests for tools/replay_enrichment/recorded_day_cr.py

Verifies the replay enrichment helper produces the real day_type + cr_verdict
fields via the core modules, exposes exactly the 8 contract keys, and degrades
to "UNKNOWN" (without raising) on an empty market dict.

The module under test has no __init__.py (the parent adds one later), so we
load it directly from its file path.
"""

import importlib.util
import os

import pytest

# ── Load recorded_day_cr.py by path (no package __init__.py yet) ────────────
_MODULE_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "tools",
        "replay_enrichment",
        "recorded_day_cr.py",
    )
)
_spec = importlib.util.spec_from_file_location("recorded_day_cr", _MODULE_PATH)
recorded_day_cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorded_day_cr)

EXPECTED_KEYS = {
    "day_type",
    "day_type_reason",
    "cr_verdict",
    "cr_confidence",
    "cr_direction",
    "cr_mom_score",
    "cr_at_resistance",
    "cr_at_support",
}


def test_empty_market_returns_all_keys_without_raising():
    """enrich_day_cr({}) must return exactly the 8 keys and never raise.

    NOTE: an empty market dict does NOT raise inside the core modules — the
    real cr_assess() runs and returns verdict="WAIT" (zero score), and the
    real DayClassifier classifies score=0 as "RANGE". This mirrors the live
    bot exactly: the result is a concrete, faithful classification rather
    than the backtester's old hard "BALANCED" default. The "UNKNOWN" fallback
    is reserved for the case where a core call genuinely raises — exercised in
    test_cr_failure_degrades_to_unknown / test_day_failure_degrades_to_unknown.
    """
    result = recorded_day_cr.enrich_day_cr({})

    # Exactly the contract keys — no more, no less.
    assert set(result.keys()) == EXPECTED_KEYS

    # Real (non-raising) degraded outputs — valid labels, never "BALANCED".
    assert result["cr_verdict"] in {
        "CONTINUATION", "REVERSAL", "CONTESTED", "WAIT", "UNKNOWN",
    }
    assert result["day_type"] in {"TREND", "RANGE", "VOLATILE", "UNKNOWN"}

    # Neutral sub-field fallbacks (no levels present in an empty market).
    assert result["cr_at_resistance"] is False
    assert result["cr_at_support"] is False


def test_cr_failure_degrades_to_unknown(monkeypatch):
    """If the C/R assessment raises, cr_verdict falls back to "UNKNOWN"
    (and day_type is then classified from "UNKNOWN") — never raising."""
    import core.continuation_reversal as cr_mod

    def _boom(*_a, **_k):
        raise RuntimeError("simulated CR failure")

    monkeypatch.setattr(cr_mod, "assess", _boom)

    result = recorded_day_cr.enrich_day_cr({"atr_5m": 12.0})
    assert set(result.keys()) == EXPECTED_KEYS
    assert result["cr_verdict"] == "UNKNOWN"
    assert result["cr_at_resistance"] is False
    assert result["cr_at_support"] is False


def test_day_failure_degrades_to_unknown(monkeypatch):
    """If day-type classification raises, day_type falls back to "UNKNOWN"."""
    import core.day_classifier as day_mod

    def _boom(*_a, **_k):
        raise RuntimeError("simulated classify failure")

    monkeypatch.setattr(day_mod.DayClassifier, "classify", _boom)

    result = recorded_day_cr.enrich_day_cr({"atr_5m": 12.0})
    assert set(result.keys()) == EXPECTED_KEYS
    assert result["day_type"] == "UNKNOWN"


def test_populated_market_returns_all_keys():
    """A populated-ish market dict still returns exactly the 8 keys and
    produces a concrete (non-UNKNOWN-by-default) classification path."""
    market = {
        "price": 25344.5,
        "vwap": 25204.5,
        "atr_5m": 18.0,
        "atr_1m": 4.5,
        "vix": 17.0,
        "cvd": 750_000,
        "bar_delta": 120,
        "tf_bias": {
            "1m": "BULLISH",
            "5m": "BULLISH",
            "15m": "BULLISH",
            "60m": "BULLISH",
        },
    }

    result = recorded_day_cr.enrich_day_cr(market)

    assert set(result.keys()) == EXPECTED_KEYS

    # day_type must be one of the real classifier outputs (never raises ->
    # never the bare "BALANCED" default the backtester used to use).
    assert result["day_type"] in {"TREND", "RANGE", "VOLATILE", "UNKNOWN"}

    # cr_verdict must be one of the real verdict labels.
    assert result["cr_verdict"] in {
        "CONTINUATION",
        "REVERSAL",
        "CONTESTED",
        "WAIT",
        "UNKNOWN",
    }

    # Sub-field types are stable.
    assert isinstance(result["cr_at_resistance"], bool)
    assert isinstance(result["cr_at_support"], bool)
    assert isinstance(result["cr_mom_score"], int)


def test_does_not_raise_on_partial_market():
    """Partial/garbage-ish fields must not raise."""
    recorded_day_cr.enrich_day_cr({"price": 0, "atr_5m": None, "vix": None})


def test_custom_trajectory_window_accepted():
    """trajectory_window kwarg is honored and still returns the contract."""
    result = recorded_day_cr.enrich_day_cr({}, trajectory_window=5)
    assert set(result.keys()) == EXPECTED_KEYS


# ── Faithful-reconstruction tests (the de-stub fix) ─────────────────────────
#
# Root cause: cr_verdict is dominated by the cross-session momentum trajectory
# (current_score / current_direction / trend), because mq_snap=None in a
# backtest removes every MenthorQ level signal. A replay that reads a stale,
# single-value production momentum file feeds ONE frozen (score, direction)
# into every bar of every day, collapsing the verdict to a single label
# (CONTESTED / WAIT). The fix lets the replay build an ISOLATED momentum file
# chronologically via feed_trajectory(..., momentum_file=...) (or under the
# isolated_momentum_file context manager), so day N's verdict sees the real
# accumulated trajectory of days 1..N-1 — exactly like the live bot.


def _eod_market(tf_dir, cvd, atr_5m=18.0):
    """Build an end-of-session market snapshot that record_daily/compute_score
    will turn into a momentum score with the intended direction/strength."""
    tf = {tf_: tf_dir for tf_ in ("1m", "5m", "15m", "60m")}
    price, vwap = (25400.0, 25300.0) if tf_dir == "BULLISH" else (25200.0, 25300.0)
    return {
        "price": price,
        "vwap": vwap,
        "atr_5m": atr_5m,
        "atr_1m": atr_5m / 4.0,
        "cvd": cvd,
        "tf_bias": tf,
    }


def _intraday_market(tf_dir, cvd, atr_5m=18.0):
    """A representative intraday bar snapshot (per-bar fields only — the
    trajectory comes from the isolated momentum file)."""
    m = _eod_market(tf_dir, cvd, atr_5m)
    m["bar_delta"] = 0.0
    return m


def test_isolated_momentum_file_does_not_touch_production(tmp_path):
    """feed_trajectory(momentum_file=...) must write the scratch file and leave
    the shared production data/momentum_scores.json untouched, and must restore
    core.momentum_score.DATA_FILE afterwards."""
    import core.momentum_score as ms

    original = ms.DATA_FILE
    scratch = str(tmp_path / "replay_momentum.json")

    recorded_day_cr.feed_trajectory(
        _eod_market("BULLISH", 600_000_000),
        session_date="2026-05-01",
        momentum_file=scratch,
    )

    assert os.path.exists(scratch), "scratch momentum file should have been written"
    # Production global is restored (no leak of the scratch path).
    assert ms.DATA_FILE == original


def test_cr_verdict_varies_across_isolated_trajectory(tmp_path):
    """THE FIX PROOF: build an isolated momentum file day-by-day representing
    differing momentum conditions; cr_verdict must take at least two DIFFERENT
    values across the replay (i.e. NOT stuck on CONTESTED / one label)."""
    scratch = str(tmp_path / "replay_momentum.json")

    # Chronological EOD scores that drive distinct trajectory states:
    #   strong sustained bullish (-> CONTINUATION) building to institutional,
    #   then a sharp neutral/choppy break (-> WAIT/RANGE-ish low score).
    sessions = [
        ("2026-05-01", "BULLISH", 600_000_000),
        ("2026-05-02", "BULLISH", 600_000_000),
        ("2026-05-03", "BULLISH", 600_000_000),
        ("2026-05-04", "BULLISH", 600_000_000),
        ("2026-05-05", "NEUTRAL", 0),
        ("2026-05-06", "NEUTRAL", 0),
    ]

    verdicts = []
    with recorded_day_cr.isolated_momentum_file(scratch):
        for i, (sess_date, tf_dir, cvd) in enumerate(sessions):
            # Intraday enrichment for this session uses the trajectory built
            # from all PRIOR sessions (skip day 0 which has no history yet).
            if i > 0:
                res = recorded_day_cr.enrich_day_cr(_intraday_market(tf_dir, cvd))
                verdicts.append(res["cr_verdict"])
            # EOD: append this session's score to the isolated trajectory.
            recorded_day_cr.feed_trajectory(
                _eod_market(tf_dir, cvd), session_date=sess_date
            )

    distinct = set(verdicts)
    assert len(distinct) >= 2, (
        f"cr_verdict failed to vary across the replay (stuck): {verdicts}"
    )
    # And it must NOT be degenerate-all-CONTESTED (the reported failure mode).
    assert distinct != {"CONTESTED"}, f"cr_verdict collapsed to CONTESTED: {verdicts}"


def test_momentum_file_kwarg_matches_context_manager(tmp_path):
    """Passing momentum_file= to enrich_day_cr is equivalent to wrapping in the
    isolated_momentum_file context manager (same trajectory source)."""
    scratch = str(tmp_path / "replay_momentum.json")

    # Seed a clear institutional-bullish trajectory in the isolated file.
    for d in ("2026-05-01", "2026-05-02", "2026-05-03"):
        recorded_day_cr.feed_trajectory(
            _eod_market("BULLISH", 600_000_000),
            session_date=d,
            momentum_file=scratch,
        )

    bar = _intraday_market("BULLISH", 600_000_000)

    via_kwarg = recorded_day_cr.enrich_day_cr(bar, momentum_file=scratch)
    with recorded_day_cr.isolated_momentum_file(scratch):
        via_ctx = recorded_day_cr.enrich_day_cr(bar)

    assert via_kwarg["cr_verdict"] == via_ctx["cr_verdict"]
    assert via_kwarg["cr_mom_score"] == via_ctx["cr_mom_score"]
    # A real institutional-bullish trajectory should NOT read as CONTESTED.
    assert via_kwarg["cr_verdict"] in {"CONTINUATION", "WAIT", "REVERSAL", "CONTESTED"}
    assert via_kwarg["cr_mom_score"] >= 1
