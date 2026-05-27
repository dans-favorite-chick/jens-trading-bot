"""Big-Move Detector tests (2026-05-15).

Built from empirical analysis of 48 100pt+ moves on MNQ over 10 sessions.
Today's 09:04-09:23 squeeze (+217pt in 19 min) is the canonical example:

  Pre-move (09:01-09:04):
    - Volume collapsed 3,000M → 94-308M (97% drop on the bounce)
    - CVD held flat at -282M while price made new low (divergence)
    - 09:04 broke prior low and immediately reversed (failed break)
    - dom_imbalance showed buyers absorbing through 08:58-09:00

  Peak (09:21-09:23):
    - CVD went -65M → -421M during the rally (textbook divergence)
    - Volume declined: 1,642M → 1,008M → 1,266M (exhaustion)
    - dom_imbalance flipped to 1.00 at 09:25 (selling absorption)
    - First bear TF vote appeared at 09:25

Detector scores 0-100, 25 pts per flag fired.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.big_move_detector import BigMoveDetector


def _bar(
    open_: float, high: float, low: float, close: float,
    volume: float = 1000.0, delta: float = 0.0, end_time: float = 0.0,
):
    return SimpleNamespace(
        open=open_, high=high, low=low, close=close,
        volume=volume, delta=delta, bar_delta=delta,
        end_time=end_time,
    )


# ── Pre-move: warmup ──────────────────────────────────────────────────

def test_pre_move_returns_zero_with_insufficient_bars():
    bars = [_bar(100, 101, 99, 100) for _ in range(10)]
    result = BigMoveDetector.detect_pre_move(bars, {"cvd": 0})
    assert result.score == 0
    assert "warmup" in result.reason


# ── Pre-move: volume collapse ─────────────────────────────────────────

def test_pre_move_detects_volume_collapse():
    """20 high-volume bars (10K each), then 5 low-volume bars (1K each).
    Should flag vol_collapse and increment the score by 25."""
    # Stable-price bars with volume drop
    bars = []
    for i in range(20):
        bars.append(_bar(100, 101, 99, 100, volume=10000, delta=0))
    for i in range(5):
        bars.append(_bar(100, 101, 99, 100, volume=1000, delta=0))
    result = BigMoveDetector.detect_pre_move(bars, {"cvd": 0})
    assert "vol_collapse" in result.flags
    assert result.score >= 25


# ── Pre-move: CVD divergence at new low (bullish setup) ───────────────

def test_pre_move_detects_bullish_cvd_divergence_at_new_low():
    """Price makes a new low in the recent 5 bars, but bar deltas net
    POSITIVE (sellers exhausted, buyers stepping in). → likely LONG."""
    bars = []
    # 20 trailing bars: stable around 100
    for i in range(20):
        bars.append(_bar(100, 101, 99, 100, volume=5000, delta=0))
    # 5 recent bars: price drifts to NEW LOW with positive deltas
    bars.append(_bar(100, 100, 95, 96, volume=5000, delta=+500))  # new low 95
    bars.append(_bar(96, 97, 94, 96, volume=5000, delta=+800))   # new low 94
    bars.append(_bar(96, 97, 95, 96, volume=5000, delta=+200))
    bars.append(_bar(96, 98, 96, 97, volume=5000, delta=+600))
    bars.append(_bar(97, 100, 96, 99, volume=5000, delta=+1000)) # bounce
    result = BigMoveDetector.detect_pre_move(bars, {"cvd": 100})
    assert "cvd_divergence" in result.flags
    assert result.likely_direction == "LONG"


# ── Pre-move: failed break ────────────────────────────────────────────

def test_pre_move_detects_failed_break_at_new_low():
    """Last bar breaks below prior low but closes near the high (long
    lower wick) → failed break → bullish setup."""
    bars = []
    for i in range(20):
        bars.append(_bar(100, 101, 99, 100, volume=5000, delta=0))
    # 4 bars of light recent action
    for i in range(4):
        bars.append(_bar(99, 100, 98, 99, volume=2000, delta=0))
    # Last bar: spike low, bounce close (long lower wick = >50% wick)
    bars.append(_bar(99, 100, 90, 99, volume=2000, delta=+500))
    result = BigMoveDetector.detect_pre_move(bars, {"cvd": 0})
    assert "failed_break" in result.flags


# ── Pre-move: DOM absorption ──────────────────────────────────────────

def test_pre_move_detects_dom_absorption():
    bars = [_bar(100, 101, 99, 100, volume=5000, delta=0) for _ in range(25)]
    result = BigMoveDetector.detect_pre_move(
        bars, {"dom_imbalance": 0.85, "cvd": 0}
    )
    assert "dom_absorption" in result.flags


# ── Pre-move: combined scoring (today's 09:01-09:04 replay) ──────────

def test_pre_move_today_squeeze_signature_scores_high():
    """Replay today's 09:01-09:04 setup:
    - 20 prior bars at typical volume (3000M-ish)
    - 5 recent bars with low volume + new low + bullish bar deltas
    - DOM showing absorption
    Should fire 3-4 of the 4 conditions → score >= 75.
    """
    bars = []
    # 20 trailing bars at high volume
    for i in range(20):
        bars.append(_bar(100, 101, 99, 100, volume=3000, delta=0))
    # 5 recent bars: vol drops to <30% of trailing, price goes new-low,
    # last bar has long lower wick (failed break)
    bars.append(_bar(100, 100, 98, 99, volume=300, delta=+100))
    bars.append(_bar(99, 99, 97, 98, volume=300, delta=+200))
    bars.append(_bar(98, 99, 96, 98, volume=200, delta=+300))  # new low 96
    bars.append(_bar(98, 99, 95, 98, volume=300, delta=+200))  # new low 95
    bars.append(_bar(98, 100, 94, 99, volume=400, delta=+500))  # spring rejection
    result = BigMoveDetector.detect_pre_move(
        bars, {"dom_imbalance": 0.85, "cvd": 100}
    )
    assert result.score >= 75, f"Expected score>=75 for full setup; got {result.score} flags={result.flags}"
    assert result.likely_direction == "LONG"


# ── Exhaustion: warmup ────────────────────────────────────────────────

def test_exhaustion_warmup():
    bars = [_bar(100, 101, 99, 100) for _ in range(10)]
    result = BigMoveDetector.detect_exhaustion(bars, {"cvd": 0}, "LONG")
    assert result.score == 0


# ── Exhaustion: CVD divergence at new high ────────────────────────────

def test_exhaustion_detects_cvd_divergence_at_new_high_for_long():
    """LONG position. Price makes new high in recent 5 bars, but bar
    deltas net NEGATIVE — sellers absorbing the rally. Textbook top."""
    bars = []
    for i in range(10):
        bars.append(_bar(100, 102, 98, 100, volume=5000, delta=0))
    # Recent 5: new high but negative deltas
    bars.append(_bar(100, 103, 100, 102, volume=5000, delta=-500))
    bars.append(_bar(102, 105, 101, 104, volume=5000, delta=-800))  # new high 105
    bars.append(_bar(104, 106, 103, 105, volume=4000, delta=-700))  # new high 106
    bars.append(_bar(105, 107, 104, 106, volume=3000, delta=-600))  # new high 107
    bars.append(_bar(106, 108, 105, 106, volume=2500, delta=-400))  # new high 108
    result = BigMoveDetector.detect_exhaustion(
        bars, {"cvd": -1000, "dom_imbalance": 0.9}, "LONG"
    )
    assert "cvd_divergence_at_extreme" in result.flags
    assert "volume_exhaustion" in result.flags  # last 3 vols: 4000>3000>2500


# ── Exhaustion: DOM flip ─────────────────────────────────────────────

def test_exhaustion_dom_flip_against_long():
    bars = [_bar(100, 101, 99, 100, volume=5000, delta=0) for _ in range(15)]
    # dom_imb >= 0.85 = heavy ask side = sellers absorbing late longs
    result = BigMoveDetector.detect_exhaustion(
        bars, {"dom_imbalance": 0.90, "cvd": 0}, "LONG"
    )
    assert "dom_flip_against_long" in result.flags


# ── Exhaustion: TF vote flip ─────────────────────────────────────────

def test_exhaustion_tf_vote_flip_against_long():
    bars = [_bar(100, 101, 99, 100, volume=5000, delta=0) for _ in range(15)]
    result = BigMoveDetector.detect_exhaustion(
        bars, {"tf_votes_bullish": 1, "tf_votes_bearish": 2}, "LONG"
    )
    assert "tf_vote_flip_to_bear" in result.flags


# ── Exhaustion: combined (today's 09:21-09:23 replay) ────────────────

def test_exhaustion_today_peak_signature_scores_high():
    """Replay today's 09:21-09:23 peak conditions for an open LONG:
    - Price makes new highs in recent 5 bars
    - Bar deltas net negative (CVD divergence)
    - Volume declining each bar
    - dom_imb at 1.0 (selling absorption)
    - First bear TF vote appears
    Should fire 4/4 → score 100.
    """
    bars = []
    for i in range(10):
        bars.append(_bar(100, 102, 98, 100, volume=5000, delta=+200))
    # Recent 5 bars: new highs, declining volume, negative deltas
    bars.append(_bar(102, 104, 102, 103, volume=4500, delta=-300))
    bars.append(_bar(103, 106, 103, 105, volume=4000, delta=-500))
    bars.append(_bar(105, 108, 104, 107, volume=3000, delta=-700))
    bars.append(_bar(107, 110, 106, 109, volume=2500, delta=-800))
    bars.append(_bar(109, 112, 108, 111, volume=1800, delta=-1000))
    result = BigMoveDetector.detect_exhaustion(
        bars,
        {
            "cvd": -3300,
            "dom_imbalance": 1.00,
            "tf_votes_bullish": 0,
            "tf_votes_bearish": 1,
        },
        "LONG",
    )
    assert result.score >= 75, (
        f"Expected score>=75 for full peak signature; got "
        f"{result.score} flags={result.flags}"
    )


# ── Wiring + config ──────────────────────────────────────────────────

def test_base_bot_instantiates_big_move_detector():
    # P4-1 (2026-05-24): search all extracted bot modules, not base_bot.py alone
    from tests._bot_src_search import bot_combined_source as _bcs
    src = _bcs()
    assert "from core.big_move_detector import BigMoveDetector" in src
    assert "self.big_move = BigMoveDetector()" in src
    # The exhaustion exit must be wired into the position loop
    assert "big_move_exhaustion" in src


def test_exit_priority_includes_big_move_exhaustion():
    from core.exit_decision import EXIT_PRIORITY, FLOW_REVERSAL_REASONS  # noqa: F401
    assert "big_move_exhaustion" in EXIT_PRIORITY
    # Same rank as cvd_flip / cvd_divergence (5)
    assert EXIT_PRIORITY["big_move_exhaustion"] == EXIT_PRIORITY["cvd_flip"]
