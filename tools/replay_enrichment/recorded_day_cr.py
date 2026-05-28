#!/usr/bin/env python3
"""
Phoenix Bot -- Replay Enrichment: day_type + cr_verdict producer

Standalone research/backtest helper. NOT a live trade path.

Produces the real ``day_type`` and ``cr_verdict`` market fields (and their
sub-fields) the same way the LIVE bot does, so a chronological backtest /
replay can stop defaulting ``day_type`` to "BALANCED".

It mirrors the live dispatch logic in ``bots/_strategy_dispatch.py``
(the Continuation/Reversal block ~lines 361-376 and the Day-Type block
~lines 378-404), which does, per bar:

    from core.continuation_reversal import assess as cr_assess
    from core.momentum_score import get_trajectory
    _cr = cr_assess(market, None, get_trajectory(10))
    market["cr_verdict"] = _cr.verdict
    ... (sub-fields) ...
    _day = DayClassifier().classify(cr_verdict, cr_mom_score, atr_5m, vix)
    market["day_type"] = _day.day_type
    market["day_type_reason"] = _day.reason

This module imports and USES the real core modules — it does not
reimplement any of their scoring.

-----------------------------------------------------------------------------
IMPORTANT — momentum trajectory feeding (the get_trajectory requirement)
-----------------------------------------------------------------------------
``core.momentum_score.get_trajectory(n)`` is NOT fed per-bar from in-memory
module-global state. It reads ``data/momentum_scores.json`` from disk
(``_load_file`` -> ``get_history`` -> ``get_trajectory``). That file holds
ONE end-of-session momentum score PER DAY, written by
``core.momentum_score.record_daily(market, None, session_date=...)`` — which
the live bot calls exactly once per session at EOD (see
``bots/base_bot.py`` ~line 2279), NOT on every bar.

Consequences for a replay:
  * There is NO per-bar feeding requirement. You may call ``enrich_day_cr``
    on every bar without any prior setup; ``get_trajectory`` degrades safely
    (returns ``current_score=0``, ``trend="UNKNOWN"`` when the file is
    missing/empty), so ``cr_assess`` still runs and ``day_type`` still
    classifies (it will lean RANGE/UNKNOWN without trajectory, which is the
    correct conservative behaviour rather than a hard default to BALANCED).
  * To reproduce the live cross-session momentum trajectory in a
    chronological replay, call ``feed_trajectory(market, session_date=...)``
    ONCE at each session's end (EOD), in chronological order, BEFORE
    enriching the next session's bars. This appends that day's score to
    ``data/momentum_scores.json`` exactly as the live bot does, so the next
    day's ``get_trajectory(10)`` sees the accumulated history.

    Required call order for a multi-day replay (ISOLATED — does NOT touch the
    shared production data/momentum_scores.json):

        from tools.replay_enrichment.recorded_day_cr import (
            enrich_day_cr, feed_trajectory, isolated_momentum_file)

        scratch = os.path.join(tempfile.mkdtemp(), "replay_momentum.json")
        with isolated_momentum_file(scratch):
            for session in sessions (oldest -> newest):
                for bar in session.bars:                 # intraday
                    enrich_day_cr(bar_market)            # uses trajectory so far
                feed_trajectory(session_eod_market,      # once, at session close
                                session_date=session.date)

    Equivalently, pass ``momentum_file=scratch`` to each ``enrich_day_cr`` /
    ``feed_trajectory`` call instead of wrapping in the context manager.

ROOT CAUSE OF "cr_verdict CONTESTED for 100% of bars" (de-stub investigation):
    The C/R verdict is DOMINATED by the cross-session momentum trajectory, not
    by per-bar market fields. In a backtest ``cr_assess`` is called with
    ``mq_snap=None``, so EVERY MenthorQ level signal (day_max/min, call_res,
    put_sup, hvl) is 0 and contributes nothing — ``level_state`` returns
    NEUTRAL for all of them. That leaves momentum + CVD as the only inputs, and
    of those ``trajectory["current_score"]`` and ``["current_direction"]`` /
    ``["trend"]`` gate the result:
      * empty/stale file -> current_score 0  -> total==0 -> verdict WAIT (100%)
      * a single frozen production day (e.g. score 4 BULLISH, trend FALLING)
        applied to every bar of every replay day -> the verdict collapses to
        ONE label (CONTESTED) regardless of intraday action.
    Because the replay was reading the SHARED production file (one stale value),
    not its own chronologically-built history, the trajectory never varied and
    so the verdict never varied. The fix gives the replay an ISOLATED momentum
    file it builds day-by-day with ``feed_trajectory`` — so day N's verdict sees
    days 1..N-1's real accumulated trajectory, exactly like the live bot.

    LIMITATION: even with a faithful isolated trajectory, the backtest verdict
    cannot reproduce the live MenthorQ-level reversal signals (REJECTED_AT /
    BROKEN_BELOW etc.) — those required the now-retired MenthorQ subscription
    and are absent live too since Sprint J (2026-05-06). So backtest and live
    are on EQUAL footing there; the trajectory is the only remaining driver and
    isolating it makes the reconstruction faithful to current live behaviour.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys

# ── Make the phoenix_bot package root importable ────────────────────────────
# This module lives at tools/replay_enrichment/recorded_day_cr.py, so the repo
# root (containing core/, bots/, config/) is three levels up.
_PHOENIX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _PHOENIX_ROOT not in sys.path:
    sys.path.insert(0, _PHOENIX_ROOT)

logger = logging.getLogger("ReplayDayCR")

# Output keys, in a fixed order, that enrich_day_cr() always returns.
RESULT_KEYS = (
    "day_type",
    "day_type_reason",
    "cr_verdict",
    "cr_confidence",
    "cr_direction",
    "cr_mom_score",
    "cr_at_resistance",
    "cr_at_support",
)


@contextlib.contextmanager
def isolated_momentum_file(path: str):
    """
    Redirect ``core.momentum_score.DATA_FILE`` to an isolated scratch ``path``
    for the duration of the ``with`` block, restoring the original on exit.

    This is the clean isolation primitive for a backtest replay. The momentum
    module reads/writes its history file via the *module-global* ``DATA_FILE``
    (``_load_file`` / ``_save_file`` look it up at call time), so swapping the
    global redirects BOTH ``get_trajectory`` (read by ``enrich_day_cr`` via
    ``cr_assess``) and ``record_daily`` (write by ``feed_trajectory``) at once —
    WITHOUT touching the shared production ``data/momentum_scores.json``.

    Why this matters (see root-cause notes in the module docstring): the C/R
    verdict is dominated by the cross-session momentum trajectory
    (``current_score`` / ``current_direction`` / ``trend``), NOT by per-bar
    market fields. With ``mq_snap=None`` in a backtest, ALL MenthorQ level
    signals are absent, so the ONLY thing that moves the verdict off a single
    frozen value is the trajectory. If a replay reads the (stale, single-value)
    production file, every bar of every day sees the SAME (score, direction)
    and the verdict collapses to one label (e.g. CONTESTED or WAIT). Pointing
    at an isolated file that the replay builds chronologically with
    ``feed_trajectory`` restores faithful per-day variation.

    Usage (multi-day replay)::

        scratch = os.path.join(tempfile.mkdtemp(), "replay_momentum.json")
        with isolated_momentum_file(scratch):
            for session in sessions:                 # oldest -> newest
                for bar in session.bars:
                    enrich_day_cr(bar_market)        # uses trajectory so far
                feed_trajectory(session_eod_market,  # once, at session close
                                session_date=session.date)
        # production data/momentum_scores.json is untouched

    The scratch file need not exist beforehand — ``get_trajectory`` degrades
    safely (score 0 -> WAIT) until the first ``feed_trajectory`` writes a day.
    """
    import core.momentum_score as _ms

    original = _ms.DATA_FILE
    _ms.DATA_FILE = path
    try:
        yield path
    finally:
        _ms.DATA_FILE = original


def enrich_day_cr(
    market: dict,
    *,
    trajectory_window: int = 10,
    momentum_file: str | None = None,
) -> dict:
    """
    Compute the real day_type + cr_verdict fields from a market snapshot,
    exactly as the live bot's dispatch does.

    Args:
        market:            A market snapshot dict (price, atr_5m, vix, plus
                           whatever levels/structure cr_assess reads:
                           vwap, atr_1m, cvd, tf_bias, bar_delta, ...). May be
                           empty/partial — missing fields degrade gracefully.
        trajectory_window: Days of momentum history to consider (live uses 10).
        momentum_file:     OPTIONAL path to an ISOLATED momentum history file.
                           When given, the cross-session momentum trajectory is
                           read from this file instead of the shared production
                           ``data/momentum_scores.json`` — for the duration of
                           THIS call only (restored on return). This is the
                           single most important knob for faithful replay: the
                           C/R verdict is dominated by the trajectory's
                           current_score / current_direction / trend (see the
                           root-cause notes below and on ``isolated_momentum_file``).
                           Pass the SAME path you feed via ``feed_trajectory``
                           (or, for many bars, wrap the whole replay in
                           ``with isolated_momentum_file(path):`` and omit this
                           kwarg — both compose correctly). If None, behaviour
                           is unchanged (reads production / whatever DATA_FILE
                           currently points at).

    Returns:
        dict with EXACTLY these keys (see RESULT_KEYS):
            day_type, day_type_reason, cr_verdict, cr_confidence,
            cr_direction, cr_mom_score, cr_at_resistance, cr_at_support

    Failure semantics mirror the live bot:
        * If the C/R assessment raises, cr_verdict is set to "UNKNOWN"
          (and the dependent cr_* sub-fields fall back to neutral defaults).
        * If day-type classification raises, day_type is set to "UNKNOWN".
        * The function never raises.
    """
    # Defaults match the live "failed" fallbacks so the contract holds even
    # when both stages error out.
    cr_verdict = "UNKNOWN"
    cr_confidence = "LOW"
    cr_direction = "NEUTRAL"
    cr_mom_score = 0
    cr_at_resistance = False
    cr_at_support = False

    day_type = "UNKNOWN"
    day_type_reason = ""

    # ── Continuation / Reversal assessment ──────────────────────────────────
    # Live: cr_assess(market, None, get_trajectory(10)).
    #
    # The trajectory is the DOMINANT driver of the verdict (per-bar market
    # fields only nudge it once a non-trivial trajectory exists, because
    # mq_snap=None removes every MenthorQ level signal in a backtest). When an
    # isolated momentum_file is requested, redirect the momentum module's
    # DATA_FILE for just this call so get_trajectory reads the replay's OWN
    # accumulated history instead of stale production state.
    try:
        from core.continuation_reversal import assess as cr_assess
        from core.momentum_score import get_trajectory

        _isolation = (
            isolated_momentum_file(momentum_file)
            if momentum_file is not None
            else contextlib.nullcontext()
        )
        with _isolation:
            _cr_traj = get_trajectory(trajectory_window)
        _cr = cr_assess(market, None, _cr_traj)

        cr_verdict = _cr.verdict
        cr_confidence = _cr.confidence
        cr_direction = _cr.direction_bias
        cr_mom_score = _cr.momentum_score
        cr_at_resistance = bool(_cr.at_call_resistance or _cr.at_day_max)
        cr_at_support = bool(_cr.at_put_support or _cr.at_day_min)
    except Exception as cr_err:  # noqa: BLE001 — match live broad catch
        logger.warning(f"[CR] assess failed (non-blocking): {cr_err!r}")
        cr_verdict = "UNKNOWN"
        # Leave the cr_* sub-fields at their neutral defaults above.

    # ── Day-type classification ─────────────────────────────────────────────
    # Live: DayClassifier().classify(cr_verdict, cr_mom_score, atr_5m, vix).
    # Per-call construction matches the reference usage; the classifier's only
    # cross-call state is logging/stickiness, which is irrelevant per-bar here.
    try:
        from core.day_classifier import DayClassifier

        _atr = float(market.get("atr_5m", 0) or 0)
        _vix = float(market.get("vix", 0) or 0)
        _day = DayClassifier().classify(cr_verdict, cr_mom_score, _atr, _vix)
        day_type = _day.day_type
        day_type_reason = _day.reason
    except Exception as day_err:  # noqa: BLE001 — match live broad catch
        logger.debug(f"[DAY TYPE] Non-blocking classification error: {day_err}")
        day_type = "UNKNOWN"

    return {
        "day_type": day_type,
        "day_type_reason": day_type_reason,
        "cr_verdict": cr_verdict,
        "cr_confidence": cr_confidence,
        "cr_direction": cr_direction,
        "cr_mom_score": cr_mom_score,
        "cr_at_resistance": cr_at_resistance,
        "cr_at_support": cr_at_support,
    }


def feed_trajectory(
    market: dict,
    *,
    session_date: str | None = None,
    momentum_file: str | None = None,
) -> dict:
    """
    Persist ONE end-of-session momentum score, exactly as the live bot does at
    EOD (core.momentum_score.record_daily). Call this ONCE per session, at the
    session close, in chronological order, so that the NEXT session's
    enrich_day_cr() sees the accumulated cross-day momentum trajectory.

    This is the ONLY "feeding" step the momentum trajectory needs — there is
    no per-bar feed. See the module docstring for the full required call order.

    Args:
        market:        The session's EOD market snapshot.
        session_date:  ISO date string "YYYY-MM-DD" for this session. If None,
                       record_daily defaults to today's date.
        momentum_file: OPTIONAL path to an ISOLATED momentum history file. When
                       given, the score is written THERE for the duration of
                       this call only (production data/momentum_scores.json is
                       untouched). Pass the SAME path to enrich_day_cr's
                       momentum_file kwarg so the next day's verdict reads it
                       back. Equivalent to wrapping the call in
                       ``with isolated_momentum_file(path):``. If None,
                       behaviour is unchanged (WRITES to production).

    Returns:
        The record dict that record_daily persisted.

    WARNING: with momentum_file=None this WRITES to data/momentum_scores.json
    (the same file the live bot uses). For an isolated backtest, pass
    momentum_file=<scratch path> (or wrap in isolated_momentum_file), or skip
    this call entirely and accept the safe-degraded (no-trajectory) path.
    """
    from core.momentum_score import record_daily

    _isolation = (
        isolated_momentum_file(momentum_file)
        if momentum_file is not None
        else contextlib.nullcontext()
    )
    with _isolation:
        return record_daily(market, None, session_date=session_date)


# ── CLI smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    demo_market = {
        "price": 25344.5,
        "vwap": 25204.5,
        "atr_5m": 18.0,
        "atr_1m": 4.5,
        "vix": 17.0,
        "cvd": 750_000,
        "tf_bias": {"1m": "BULLISH", "5m": "BULLISH", "15m": "BULLISH", "60m": "BULLISH"},
    }
    print(json.dumps(enrich_day_cr(demo_market), indent=2))
