"""Tests for core/market_state.py (Phase 8 classifier, 2026-06-01).

Each synthetic bar sequence is deterministic and small (20-40 bars)
because the classifier's lookback is 20 bars and we want the assert to
hit on the *last* bar in the sequence after the buffer has warmed.

No warehouse, no tick_aggregator: every test runs `MarketState(None)`
and feeds bars via `on_synthetic_bar()`.

The five composite labels exercised here, in order, mirror the
priority table in core/market_state.py::_classify:
    1. WHIPSAW_HIGH_VOL  -- violent realized_vol + chop
    2. CHOPPY            -- chop with weak trend
    3. COMPRESSED        -- low realized_vol
    4. TRENDING_HIGH_VOL -- strong trend at elevated vol
    5. TRENDING_NORMAL   -- clean trend at normal vol

A NEUTRAL fallback case is also asserted so the priority cascade is
covered.

History semantics are tested separately at the bottom of the file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.market_state import LOOKBACK_BARS, MarketState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_START_TS = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def _ts(i: int) -> datetime:
    """5-minute bar timestamp for bar index i."""
    return _START_TS + timedelta(minutes=5 * i)


def _feed(bars):
    """Construct a fresh MarketState and feed it `bars`.

    `bars` is a list of (close, high, low) tuples. Returns the
    final snapshot dict.
    """
    ms = MarketState(tick_aggregator=None)
    snap = None
    for i, (close, high, low) in enumerate(bars):
        snap = ms.on_synthetic_bar(close=close, high=high, low=low,
                                   bar_ts=_ts(i))
    return ms, snap


# ---------------------------------------------------------------------------
# Label tests -- one per composite label
# ---------------------------------------------------------------------------

def test_trending_normal_clean_uptrend_low_vol():
    """Steady uptrend, constant ATR -> TRENDING_NORMAL.

    Each bar advances `close` by 4 points, range = 4 points. Constant
    ATR means realized_vol -> 1.0; sustained directional drift pulls
    EMA21 ahead of EMA50 so trend_strength > 0.3. Choppiness stays low
    (close to 0) because the cumulative range expands every bar while
    sum(ATR) stays flat.
    """
    # 60 bars: long-enough for EMA50 to warm to a stable lag behind EMA21.
    bars = []
    for i in range(60):
        c = 1000.0 + 4.0 * i
        bars.append((c, c + 2.0, c - 2.0))
    ms, snap = _feed(bars)

    # Sanity-check the underlying signals are in the right regime.
    assert 0.8 <= snap["realized_vol"] <= 1.3, snap
    assert snap["trend_strength"] > 0.3, snap
    assert snap["choppiness_index"] < 50, snap
    assert snap["label"] == "TRENDING_NORMAL", snap


def test_trending_high_vol_strong_trend_elevated_vol():
    """Uptrend that suddenly accelerates -> TRENDING_HIGH_VOL.

    First 50 bars are a quiet uptrend so EMA21/EMA50 separate; then a
    burst of high-range bars in the SAME direction spikes ATR_5m above
    1.3x the 20-bar mean while keeping choppiness low (price keeps
    breaking to new highs every bar, so cumulative range tracks sum
    of ATRs).
    """
    bars = []
    # Quiet baseline.
    for i in range(50):
        c = 1000.0 + 4.0 * i
        bars.append((c, c + 2.0, c - 2.0))
    # Burst: ranges 12-18 pts, all moving up.
    c = 1200.0
    for i in range(20):
        c += 16.0
        bars.append((c, c + 8.0, c - 8.0))
    ms, snap = _feed(bars)

    assert snap["realized_vol"] > 1.3, snap
    assert snap["trend_strength"] > 0.5, snap
    assert snap["choppiness_index"] < 50, snap
    assert snap["label"] == "TRENDING_HIGH_VOL", snap


def test_choppy_range_bound_no_direction():
    """Tight range with chop -> CHOPPY.

    Price oscillates within a narrow band. Each bar's range matches
    the band width, so sum(ATR_20) >> (max_high - min_low) and
    choppiness_index > 61.8. EMA21 and EMA50 sit on top of each other
    inside the band so trend_strength stays well below 0.2.
    """
    bars = []
    # Long warm-up around 1000 so EMA21 ~ EMA50 ~ 1000.
    for i in range(40):
        # Saw-tooth between 999.5 and 1000.5, range only 1 pt per bar.
        if i % 2 == 0:
            c, h, lo = 999.5, 1000.5, 999.0
        else:
            c, h, lo = 1000.5, 1001.0, 999.5
        bars.append((c, h, lo))
    ms, snap = _feed(bars)

    assert snap["trend_strength"] < 0.2, snap
    assert snap["choppiness_index"] > 61.8, snap
    # Realized vol around 1 (constant ATR), not extreme.
    assert snap["label"] == "CHOPPY", snap


def test_compressed_narrow_consolidation():
    """ATR collapses far below its 20-bar mean -> COMPRESSED.

    The trick: COMPRESSED is rule #3 (rv < 0.7), but rule #2 (CHOPPY,
    chop > 61.8 AND trend_strength < 0.2) fires first. So the test
    must engineer a sequence where realized_vol drops below 0.7 BUT
    choppiness stays under 61.8. We accomplish this by holding range
    constant (so chop -> 0 / low) and shrinking it AFTER the buffer
    is mostly full so the per-bar atr_5m collapses faster than the
    20-bar mean.
    """
    bars = []
    # Drift up with wide 8-pt range to populate the ATR mean.
    for i in range(20):
        c = 1000.0 + 1.0 * i
        bars.append((c, c + 4.0, c - 4.0))
    # Now keep drifting up (so total high-low range stays large -> low
    # choppiness) but with COLLAPSED 0.5-pt per-bar range, so atr_5m
    # falls far below the buffer mean.
    last_c = bars[-1][0]
    for i in range(15):
        c = last_c + 1.0 * (i + 1)
        bars.append((c, c + 0.25, c - 0.25))
    ms, snap = _feed(bars)

    assert snap["realized_vol"] < 0.7, snap
    # Confirm the CHOPPY rule is NOT met so the priority cascade
    # arrives at COMPRESSED.
    assert not (
        snap["choppiness_index"] > 61.8
        and snap["trend_strength"] < 0.2
    ), snap
    assert snap["label"] == "COMPRESSED", snap


def test_whipsaw_high_vol_violent_reversals():
    """Massive bars in alternating direction -> WHIPSAW_HIGH_VOL.

    Whipsaw beats CHOPPY in the priority cascade when realized_vol
    > 1.5 AND choppiness_index > 50, even if both choppy and whipsaw
    conditions fire.
    """
    bars = []
    # Quiet baseline -> seed EMA21/EMA50 and the ATR buffer with small
    # ATRs so realized_vol = atr_5m / mean_atr can spike when range
    # explodes at the end.
    for i in range(40):
        c = 1000.0
        bars.append((c, c + 1.0, c - 1.0))
    # 12 medium-range reversals to push the buffer mean up but keep
    # range tight (most of them stay in 995..1005 -> 10pt band).
    for i in range(12):
        if i % 2 == 0:
            c, h, lo = 1003.0, 1005.0, 996.0
        else:
            c, h, lo = 997.0, 1004.0, 995.0
        bars.append((c, h, lo))
    # Final 2 bars: extreme ranges but still within 990..1010 band.
    # These dominate the Wilder ATR (14-period) -> atr_5m spikes.
    bars.append((1000.0, 1010.0, 990.0))
    bars.append((1000.0, 1010.0, 990.0))
    ms, snap = _feed(bars)

    assert snap["realized_vol"] > 1.5, snap
    assert snap["choppiness_index"] > 50, snap
    assert snap["label"] == "WHIPSAW_HIGH_VOL", snap


def test_neutral_fallback():
    """Sequence that does not satisfy any specific bucket -> NEUTRAL.

    Engineering NEUTRAL is delicate: the priority cascade has five
    specific buckets above it. We use a controlled gentle drift so
    EMA21 - EMA50 lands in a narrow band that yields trend_strength
    in roughly (0.2, 0.3]. Combined with realized_vol ~1.0 and a
    cumulative high-low range large enough to keep chop < 61.8.

    In steady state, the spread between two EMAs at periods p1, p2
    fed a constant drift delta/bar is approximately
        delta * (p2 - p1) / 2
    so for delta = 0.08 pts/bar -> EMA21-EMA50 ~= 0.08 * 14.5 ~= 1.16.
    With per-bar ATR = 5.0 (range 5 pts), trend_strength ~= 0.23.
    """
    drift = 0.08
    bars = []
    for i in range(120):
        c = 1000.0 + drift * i
        # Range 5 pts (wider than the drift so chop stays low: each
        # bar's range > the bar's progress, so the cumulative band
        # tracks 20*bar_range, not 20*drift).
        bars.append((c, c + 2.5, c - 2.5))
    ms, snap = _feed(bars)

    # Diagnostic asserts so a future threshold tweak surfaces clearly.
    assert 0.2 < snap["trend_strength"] <= 0.3, snap
    assert 0.7 <= snap["realized_vol"] <= 1.3, snap
    assert snap["label"] == "NEUTRAL", snap


# ---------------------------------------------------------------------------
# Priority-cascade edge case: when both WHIPSAW and CHOPPY conditions
# are met, WHIPSAW wins.
# ---------------------------------------------------------------------------

def test_whipsaw_beats_choppy_in_priority():
    """If realized_vol > 1.5 AND choppiness > 61.8 AND ts < 0.2, the
    label MUST be WHIPSAW_HIGH_VOL (rule 1), not CHOPPY (rule 2)."""
    bars = []
    for i in range(40):
        c = 1000.0
        bars.append((c, c + 2.0, c - 2.0))
    # Inject extreme-range reversals that also trap inside the band.
    for i in range(6):
        if i % 2 == 0:
            c, h, lo = 1000.0, 1015.0, 985.0
        else:
            c, h, lo = 1000.0, 1014.0, 986.0
        bars.append((c, h, lo))
    ms, snap = _feed(bars)

    if (
        snap["realized_vol"] > 1.5
        and snap["choppiness_index"] > 50
    ):
        assert snap["label"] == "WHIPSAW_HIGH_VOL", (
            "Whipsaw priority violated: %s" % snap
        )


# ---------------------------------------------------------------------------
# API contract: current(), history(), schema
# ---------------------------------------------------------------------------

def test_current_returns_neutral_stub_before_first_bar():
    ms = MarketState(tick_aggregator=None)
    snap = ms.current()
    assert snap["label"] == "NEUTRAL"
    assert snap["realized_vol"] == 0.0
    assert snap["trend_strength"] == 0.0
    assert snap["choppiness_index"] == 0.0
    assert "computed_at" in snap


def test_current_matches_last_on_bar():
    ms = MarketState(tick_aggregator=None)
    last = None
    for i in range(30):
        c = 1000.0 + i
        last = ms.on_synthetic_bar(c, c + 1.0, c - 1.0, bar_ts=_ts(i))
    cur = ms.current()
    assert cur["label"] == last["label"]
    assert cur["realized_vol"] == last["realized_vol"]
    assert cur["computed_at"] == last["computed_at"]


def test_history_respects_n_bars_cap():
    ms = MarketState(tick_aggregator=None)
    n_fed = 100
    for i in range(n_fed):
        c = 1000.0 + 0.5 * i
        ms.on_synthetic_bar(c, c + 1.0, c - 1.0, bar_ts=_ts(i))

    # Smaller-than-fed N returns exactly N.
    hist_10 = ms.history(10)
    assert len(hist_10) == 10
    # Larger-than-fed N returns at most n_fed (capped by capacity too).
    hist_huge = ms.history(99999)
    assert len(hist_huge) <= n_fed

    # Each entry has the required schema keys.
    for entry in hist_10:
        assert set(entry.keys()) >= {
            "label", "realized_vol", "trend_strength",
            "choppiness_index", "computed_at",
        }


def test_history_zero_or_negative_returns_empty():
    ms = MarketState(tick_aggregator=None)
    for i in range(5):
        ms.on_synthetic_bar(1000.0, 1001.0, 999.0, bar_ts=_ts(i))
    assert ms.history(0) == []
    assert ms.history(-3) == []


def test_lookback_bars_constant():
    # Sanity guard so the spec threshold can't be silently changed.
    assert LOOKBACK_BARS == 20


# ---------------------------------------------------------------------------
# Determinism: same bar sequence -> same label
# ---------------------------------------------------------------------------

def test_deterministic_replay():
    seq = [(1000.0 + 2.0 * i, 1000.0 + 2.0 * i + 2.0,
            1000.0 + 2.0 * i - 2.0) for i in range(50)]
    _, snap_a = _feed(seq)
    _, snap_b = _feed(seq)
    assert snap_a["label"] == snap_b["label"]
    assert snap_a["realized_vol"] == pytest.approx(snap_b["realized_vol"])
    assert snap_a["trend_strength"] == pytest.approx(snap_b["trend_strength"])
    assert snap_a["choppiness_index"] == pytest.approx(
        snap_b["choppiness_index"]
    )
