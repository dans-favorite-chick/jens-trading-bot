"""Reconstruct the ``es_nq_rs`` market field from historical bars.

This is research/backtest tooling -- NOT a live trade path. It replays the
NQ-vs-ES 30-minute relative-strength figure that the live system computes in
``core/market_intel.py::get_nq_es_relative_strength()`` so that recorded /
backtest runs can carry the same ``es_nq_rs`` value that a live decision would
have seen.

Live formula (5-minute bars), replicated EXACTLY here:

    nq_now      = latest 5m close at/just before the eval time   (Close.iloc[-1])
    nq_30m_ago  = the close 7 bars earlier (~30 min back)        (Close.iloc[-7])
    nq_change_30m = ((nq_now - nq_30m_ago) / nq_30m_ago) * 100   (0 if denom 0)
    es_change_30m = same, from ES bars
    relative_strength = nq_change_30m - es_change_30m            (positive = NQ leading)

Result is rounded to 4 decimal places.

Historically we use MNQ bars as the NQ proxy and MES bars as the ES proxy.

Input DataFrames follow the schema returned by
``tools/phoenix_real_backtest.py::_load_bars_from_csv``:
    columns: ts (UTC tz-aware Timestamp), open, high, low, close, volume
             (optionally: symbol)
"""

from __future__ import annotations

import pandas as pd

# Number of bars between "now" (.iloc[-1]) and "30m ago" (.iloc[-7]).
# 5-minute bars * 6 intervals == 30 minutes; iloc[-7] is 6 bars before iloc[-1].
_MIN_BARS = 7


def _pct_change(now: float, ago: float) -> float | None:
    """Percent change from ``ago`` to ``now``, scaled to percent.

    Returns ``None`` when the denominator is zero (mirrors the live guard
    that yields a non-comparable result rather than dividing by zero).
    """
    if ago == 0:
        return None
    return ((now - ago) / ago) * 100.0


def _change_30m(df: pd.DataFrame, eval_ts: pd.Timestamp) -> float | None:
    """30-minute percent change for one instrument at/just before ``eval_ts``.

    Sorts by ``ts``, keeps rows at or before ``eval_ts``, and requires at least
    7 such rows. Returns ``None`` if too few rows or the denominator is zero.
    """
    ts = df["ts"]
    # Localize tz-naive timestamps to UTC; convert tz-aware to UTC.
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    else:
        ts = ts.dt.tz_convert("UTC")

    work = df.assign(ts=ts).sort_values("ts")
    work = work[work["ts"] <= eval_ts]

    if len(work) < _MIN_BARS:
        return None

    close = work["close"]
    now = float(close.iloc[-1])
    ago = float(close.iloc[-_MIN_BARS])
    return _pct_change(now, ago)


def es_nq_rs_at(
    eval_ts,
    mnq_5m_df: pd.DataFrame,
    mes_5m_df: pd.DataFrame,
) -> float | None:
    """Reconstruct ``es_nq_rs`` (NQ-vs-ES 30m relative strength) at ``eval_ts``.

    Parameters
    ----------
    eval_ts
        The evaluation time. Any value ``pandas.Timestamp`` accepts; coerced to
        a UTC tz-aware Timestamp.
    mnq_5m_df
        MNQ (NQ proxy) 5-minute bars in the ``_load_bars_from_csv`` schema
        (``ts`` UTC tz-aware Timestamp, plus open/high/low/close/volume).
    mes_5m_df
        MES (ES proxy) 5-minute bars in the same schema.

    Returns
    -------
    float | None
        ``relative_strength = nq_change_30m - es_change_30m`` rounded to 4 dp
        (positive => NQ leading). ``None`` if either instrument has fewer than
        7 bars at/before ``eval_ts`` or either 30m-ago close is zero.
    """
    eval_ts = pd.Timestamp(eval_ts)
    if eval_ts.tzinfo is None:
        eval_ts = eval_ts.tz_localize("UTC")
    else:
        eval_ts = eval_ts.tz_convert("UTC")

    nq_change_30m = _change_30m(mnq_5m_df, eval_ts)
    es_change_30m = _change_30m(mes_5m_df, eval_ts)

    if nq_change_30m is None or es_change_30m is None:
        return None

    relative_strength = nq_change_30m - es_change_30m
    return round(relative_strength, 4)
