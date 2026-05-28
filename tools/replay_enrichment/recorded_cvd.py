"""Reconstruct the REAL `cvd_health` market field from recorded order-flow.

RESEARCH / BACKTEST TOOLING ONLY — this never touches a live trade path.

Background
----------
The live bot derives CVD from tick-level aggressor side (real buy vs sell
volume) and feeds a CUMULATIVE session CVD into `core.cvd_trend_health.
CVDTrendHealth` once per completed 1-minute bar:

    self.cvd_health = CVDTrendHealth(lookback_bars=6, veto_threshold=-0.3)
    self.cvd_health.update_bar(bar.close, market["cvd"])   # per 1m bar
    health = self.cvd_health.assess("LONG")                # at entry

The backtester (`tools/phoenix_real_backtest.py`) cannot see tick-level
aggressor data, so it APPROXIMATES per-bar delta as
`volume * sign(close - open)` and warns this overstates magnitude on inside
bars (see its module docstring + `_approx_bar_delta`). That approximation
poisons every `cvd_health` veto decision in research replays.

This module reconstructs the genuine `cvd_health` field from the recorded
volumetric order-flow stream (`logs/volumetric_history.jsonl`), whose `delta`
field IS the real tick-level aggressor imbalance (buy_volume - sell_volume).
A backtester can then query `RecordedCVDProvider.health_at(ts, direction)`
to get the exact dict the live `CVDTrendHealth.assess()` would have produced.

It IMPORTS and uses `CVDTrendHealth` directly — the CVD slope/agreement/veto
math is NOT duplicated here.

Source data shape
-----------------
Each line of `logs/volumetric_history.jsonl` is one TICK-volume bar (e.g.
1500-tick) — NOT a time bar. There can be several per minute, or one bar
spanning multiple minutes. Relevant fields:

    {"type":"volumetric_bar","ts":"2026-05-04T21:24:44.0330000",
     "instrument":"MNQM6","bar_size_ticks":1500,
     "open":...,"high":...,"low":...,"close":27818.25,
     "delta":176,"buy_volume":943,"sell_volume":767, ...}

`ts` is a NAIVE timestamp in the recording machine's local wall-clock time
(TickStreamer stamps it at capture; `tools/volumetric_snapshot_recorder.py`
compares it directly against `datetime.now()`). It carries 7 fractional
digits, so it is parsed defensively (truncate to <=6 digits, strip a
trailing 'Z') exactly like the recorder does.

Aggregation + session-reset assumptions
----------------------------------------
* Minute alignment: each tick-bar's `delta` is bucketed into the 1-MINUTE
  bucket obtained by flooring `ts` to the minute (second=microsecond=0).
  Deltas within a minute are SUMMED; the minute "close" is the `close` of
  the LAST tick-bar whose ts falls in that minute. This mirrors how the
  live bot feeds one (close, cumulative_cvd) pair per completed 1m bar.
* Cumulative CVD: a running session sum of the per-minute summed deltas.
* Session reset: `tools/phoenix_real_backtest.py` resets `cvd_session` on a
  CALENDAR-DATE CHANGE of the bar datetime expressed in CT
  (`bar_dt_ct.strftime("%Y-%m-%d")`; see `_update_state_with_1m_bar`). Its
  input bars are tz-aware UTC and converted via `tz_convert(_CT)`.
  The recorded volumetric `ts`, however, is NAIVE machine-local time with
  no offset, so it cannot be cleanly converted to CT. We therefore MATCH
  the backtester's *behavior* — reset the cumulative CVD on a calendar-date
  change — but evaluate that date change in the timestamp's OWN (naive,
  machine-local) tz. ASSUMPTION: the recording machine runs on CT (this is
  the documented trading-PC convention), so the date boundary coincides
  with the backtester's CT boundary; if the machine tz differs the reset
  boundary shifts accordingly. This is documented rather than guessed.

Public API
----------
    RecordedCVDProvider(volumetric_path="logs/volumetric_history.jsonl",
                        lookback_bars=6, veto_threshold=-0.3, instrument=None)
    .health_at(ts, direction="LONG") -> dict | None
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional, Union

# Ensure the repo root is importable so `core.cvd_trend_health` resolves even
# when this module is imported/executed standalone (not just under pytest,
# which already inserts the repo root). The repo root is two levels up:
#   <repo>/tools/replay_enrichment/recorded_cvd.py
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.cvd_trend_health import CVDTrendHealth  # noqa: E402  (path setup first)

logger = logging.getLogger("RecordedCVDProvider")

TsLike = Union[str, datetime]


def _parse_ts(raw: object) -> Optional[datetime]:
    """Parse a volumetric `ts` into a naive datetime, or None if unparseable.

    Mirrors `tools/volumetric_snapshot_recorder.py._snapshot_age_hours`:
    strip a trailing 'Z' and truncate fractional seconds to <=6 digits so
    older `datetime.fromisoformat` implementations accept it (the recorded
    stamps carry 7 fractional digits)."""
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    s = raw.strip().replace("Z", "")
    head, dot, frac = s.partition(".")
    if dot and len(frac) > 6:
        s = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _floor_minute(dt: datetime) -> datetime:
    """Floor a datetime to the start of its minute."""
    return dt.replace(second=0, microsecond=0)


class RecordedCVDProvider:
    """Replay recorded order-flow deltas into real `cvd_health` assessments.

    Builds an in-memory, per-minute cache of `CVDTrendHealth.assess()` dicts
    for both LONG and SHORT directions, queryable by timestamp.
    """

    def __init__(
        self,
        volumetric_path: str = "logs/volumetric_history.jsonl",
        lookback_bars: int = 6,
        veto_threshold: float = -0.3,
        instrument: Optional[str] = None,
    ):
        self.volumetric_path = volumetric_path
        self.lookback_bars = lookback_bars
        self.veto_threshold = veto_threshold
        self.instrument = instrument

        # records: list of (parsed_dt, close, delta) sorted by ts.
        self._records: list[tuple[datetime, float, float]] = []
        # Sorted list of minute-keys actually replayed (for prior-minute lookup).
        self._minute_keys: list[datetime] = []
        # minute_key -> {"LONG": assess_dict, "SHORT": assess_dict}
        self._cache: dict[datetime, dict[str, dict]] = {}

        self._load(volumetric_path, instrument)
        self._replay(lookback_bars, veto_threshold)

    # ── loading ────────────────────────────────────────────────────────
    def _load(self, path: str, instrument: Optional[str]) -> None:
        """Parse the jsonl, optionally filter by instrument, sort by ts.

        Bad/empty lines are skipped gracefully. A missing file yields an
        empty provider (every query returns None)."""
        if isinstance(path, (list, tuple)):
            # Allow callers/tests to pass an in-memory list of records.
            lines = [json.dumps(r) if not isinstance(r, str) else r for r in path]
        else:
            if not os.path.exists(path):
                logger.warning("volumetric file not found: %s", path)
                return
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError as exc:
                logger.warning("could not read %s: %r", path, exc)
                return

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            if instrument is not None and rec.get("instrument") != instrument:
                continue
            dt = _parse_ts(rec.get("ts"))
            if dt is None:
                continue
            close = rec.get("close")
            delta = rec.get("delta")
            if close is None or delta is None:
                continue
            try:
                close_f = float(close)
                delta_f = float(delta)
            except (TypeError, ValueError):
                continue
            self._records.append((dt, close_f, delta_f))

        self._records.sort(key=lambda r: r[0])

    # ── replay ─────────────────────────────────────────────────────────
    def _aggregate_minutes(self) -> list[tuple[datetime, float, float]]:
        """Collapse tick-bars into per-minute (minute_key, minute_close,
        summed_delta), preserving chronological order. Within a minute the
        deltas are summed and the LAST tick-bar's close is the minute close.
        """
        minutes: list[tuple[datetime, float, float]] = []
        cur_key: Optional[datetime] = None
        cur_close = 0.0
        cur_delta = 0.0
        for dt, close, delta in self._records:
            key = _floor_minute(dt)
            if cur_key is None:
                cur_key, cur_close, cur_delta = key, close, delta
            elif key == cur_key:
                cur_close = close  # last close in the minute wins
                cur_delta += delta
            else:
                minutes.append((cur_key, cur_close, cur_delta))
                cur_key, cur_close, cur_delta = key, close, delta
        if cur_key is not None:
            minutes.append((cur_key, cur_close, cur_delta))
        return minutes

    def _replay(self, lookback_bars: int, veto_threshold: float) -> None:
        """Walk minute buckets chronologically, maintaining a cumulative
        session CVD (reset on calendar-date change) and a single
        CVDTrendHealth instance; cache LONG/SHORT assessments per minute.

        NOTE: the CVDTrendHealth instance is intentionally NOT reset across
        the session boundary — only the cumulative CVD value is. This matches
        the live bot, which constructs `cvd_health` ONCE and never re-creates
        it at the daily roll; its rolling deque naturally ages out stale bars.
        """
        cvd = CVDTrendHealth(lookback_bars=lookback_bars,
                             veto_threshold=veto_threshold)
        cumulative_cvd = 0.0
        last_session_date: Optional[str] = None

        for minute_key, minute_close, minute_delta in self._aggregate_minutes():
            date_str = minute_key.strftime("%Y-%m-%d")
            if last_session_date is None:
                last_session_date = date_str
            if date_str != last_session_date:
                # Calendar-date change -> reset cumulative session CVD
                # (matches phoenix_real_backtest._update_state_with_1m_bar).
                cumulative_cvd = 0.0
                last_session_date = date_str

            cumulative_cvd += minute_delta
            cvd.update_bar(minute_close, cumulative_cvd)

            self._cache[minute_key] = {
                "LONG": cvd.assess("LONG"),
                "SHORT": cvd.assess("SHORT"),
            }
            self._minute_keys.append(minute_key)

    # ── query ──────────────────────────────────────────────────────────
    def health_at(self, ts: TsLike, direction: str = "LONG") -> Optional[dict]:
        """Return the cached `assess()` dict for the minute containing `ts`.

        Floors `ts` to the minute. If that exact minute was not recorded,
        returns the most recent PRIOR minute within the SAME calendar date
        (session). Returns None if `ts` is before coverage, after the last
        recorded minute of a different session with no same-day prior, or if
        no data exists.

        Args:
            ts: ISO string or datetime (naive, machine-local).
            direction: "LONG" or "SHORT" (case-insensitive).
        """
        dt = _parse_ts(ts)
        if dt is None or not self._minute_keys:
            return None

        key = _floor_minute(dt)
        dir_key = "SHORT" if str(direction).upper() == "SHORT" else "LONG"

        # Exact minute hit.
        entry = self._cache.get(key)
        if entry is not None:
            return entry[dir_key]

        # Most-recent prior minute within the same calendar date.
        import bisect

        idx = bisect.bisect_right(self._minute_keys, key) - 1
        if idx < 0:
            return None
        prior_key = self._minute_keys[idx]
        if prior_key.strftime("%Y-%m-%d") != key.strftime("%Y-%m-%d"):
            return None
        return self._cache[prior_key][dir_key]
