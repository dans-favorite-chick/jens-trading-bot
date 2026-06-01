"""
microstructure.py - Phase 2 sub-second order-flow overlays for the portfolio
backtest framework.

WHAT THIS MODULE DOES
---------------------
Given the framework's standard *trades* DataFrame (schema below), it overlays
tick-level (and one bar-level) order-flow filters and reports whether each
filter lifts baseline win-rate / profit-factor on the trades that fall inside
the MNQ TBBO tick-coverage window (2026-03-17 .. 2026-05-15). Trades outside
coverage get pd.NA / "no_tick_data" and are excluded from the lift table.

  2.1  apply_intermarket_filter    -- SMT divergence / convergence (BAR level;
                                      MES has no ticks, so 1-minute bars only).
  2.2  apply_absorption_filter     -- CVD passive-absorption confirmation
                                      (tick level)  [PRIMARY VALUE].
  2.3  dom_stop_hunt_analysis      -- NOT COMPUTABLE (no Level-2 depth data).
  2.4  apply_delta_cluster_trail   -- internal signed-delta clusters by price
                                      bucket + scratch guardrail (tick level).

  microstructure_lift_table        -- baseline vs filtered summarize() stats.

TRADES SCHEMA (extra columns ignored; the schema emitted by the backtester):
    strategy, direction ('LONG'|'SHORT'), entry_ts (UTC ts), entry_price,
    stop_price, target_price, exit_ts (UTC ts), exit_price, exit_reason,
    pnl_dollars, pnl_ticks, hold_min

SIGNED-DELTA CONVENTION (from the TBBO clean cache):
    side == 'A'  -> aggressor BUYER lifted the ask  -> signed delta = +size
    side == 'B'  -> aggressor SELLER hit the bid    -> signed delta = -size
    side == 'N'  -> none                            -> signed delta = 0
    Cumulative sum of signed delta = CVD (cumulative volume delta).

DATA REALITY
------------
* TBBO clean ticks (MNQ only): paths.CLEAN_TICKS_PARQUET. 43.8M ticks,
  2026-03-17 -> 2026-05-15, symbol MNQM6. Index ts_event (datetime64[ns, UTC],
  sorted ascending). bid_px_00 / ask_px_00 are TOP OF BOOK ONLY -- there is NO
  Level-2 / depth-of-book data anywhere in the repo (this is why 2.3 is
  not-computable).
* 5y OHLCV: paths.MNQ_1M_CSV, paths.MES_1M_CSV (ts_utc, open, high, low, close,
  volume). MES has NO tick data -- only 1-minute bars, so 2.1 is bar level.

REUSE NOTES (logic borrowed from the two existing standalone tools; neither is
modified):
* phoenix_tick_entry_quality.py:
    - np.searchsorted windowing into a sorted int64-ns timestamp array for fast
      per-trade tick slicing (compute_fills). We reuse that exact pattern.
    - The MNQ price band guard (18000..35000) to reject off-instrument prints.
    - TICK_SIZE = 0.25, TICK_VALUE = 0.50 (MNQ micro: $0.50 / tick / contract).
* phoenix_tick_trail_verification.py:
    - The TickIndex idea (precompute ts_ns int64 + price arrays once, slice per
      trade with searchsorted) -- here folded into per-day slicing.
    - The "walk every tick within [entry, entry + horizon]" replay structure and
      the scratch/initial-stop exit-reason vocabulary used by the guardrail.
    - MAX_HOLD horizon concept for bounding the post-entry window.

CONSOLE / ENCODING: cp1252 console -- every PRINTED or RETURNED string literal
is ASCII-only (no unicode arrows / em-dashes). Docstrings/comments may use
unicode.
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Make the package importable whether run as a module or as a script.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import paths      # noqa: E402  canonical data locations
import analytics  # noqa: E402  summarize(), profit_factor(), compute_mae_mfe()


# ════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════

TICK_SIZE = 0.25       # MNQ
TICK_VALUE = 0.50      # $ per tick per contract (MNQ micro)

# Minimum number of ticks that must fall inside a judging window before a
# tick-level filter is willing to render a verdict. Below this the tape is too
# thin to be meaningful (a couple of prints), so the filter abstains rather than
# act on noise. Absorption requires a 2-tick price-change reference; the cluster
# guardrail bins a footprint, so it wants a touch more (3).
_MIN_TICKS_TO_JUDGE = 2          # absorption lookback window
_MIN_TICKS_TO_JUDGE_CLUSTER = 3  # cluster footprint candle

# MNQ outright price band (2026-Q1/Q2 ~ 22000-32000). Reused from
# phoenix_tick_entry_quality._normalize_ticks_df to reject spread/calendar
# prints that would corrupt CVD and price math.
_PRICE_LO, _PRICE_HI = 18000.0, 35000.0

# Tick coverage window (clean TBBO cache extent). Trades whose entry falls
# outside this window get filter value pd.NA / "no_tick_data".
TICK_COVERAGE = (
    pd.Timestamp("2026-03-17 00:00:00", tz="UTC"),
    pd.Timestamp("2026-05-15 21:00:00", tz="UTC"),  # stream ends ~21:00 UTC on 05-15
)

# Round-trip friction applied to a scratch exit (commission + ~half-tick
# slippage). MNQ commission ~ $0.50-1.00 RT; we charge a conservative one-tick
# of friction so a "scratch" is realistically slightly negative, not free.
SCRATCH_FRICTION_DOLLARS = TICK_VALUE * 1.0  # = $0.50

# ── Strategy classification for the intermarket (SMT) filter (2.1) ────
# Reversal/mean-reversion strategies want SMT DIVERGENCE; trend/breakout
# strategies want CONVERGENCE. Membership is intentionally explicit; unknown
# strategies -> pd.NA.
REVERSAL_STRATEGIES = frozenset({
    "noise_area",
    "vwap_band_pullback",
    "vwap_band_reversion",
    "spring_setup",
    "orb_fade",
    "footprint_cvd_reversal",
    "nq_lsr",
})

TREND_STRATEGIES = frozenset({
    "bias_momentum",
    "opening_session",
    "ib_breakout",
    "compression_breakout_v2",
    "compression_breakout_micro",
    "multi_day_breakout",
    "inside_bar_breakout",
    "es_nq_confluence",
    "big_move_signal",
})


# ════════════════════════════════════════════════════════════════════
# Tick loading
# ════════════════════════════════════════════════════════════════════

def load_ticks(start: Optional[pd.Timestamp] = None,
               end: Optional[pd.Timestamp] = None,
               symbol: str = "MNQM6") -> pd.DataFrame:
    """Load MNQ TBBO ticks from paths.CLEAN_TICKS_PARQUET, optionally sliced to
    [start, end] (UTC) via parquet predicate pushdown so we never materialize
    all 43.8M rows for a one-day analysis.

    Returns a DataFrame indexed by ts_event (datetime64[ns, UTC], ascending)
    with columns: symbol, price, size, side, bid_px_00, ask_px_00, and the two
    derived columns:
        signed_delta (int64)  -> +size for 'A', -size for 'B', 0 for 'N'
        cvd          (int64)  -> cumulative sum of signed_delta over the slice

    The off-instrument price-band guard (18000..35000) from
    phoenix_tick_entry_quality is applied so spread/calendar prints cannot
    contaminate CVD or price levels.
    """
    filters = []
    if start is not None:
        start = pd.Timestamp(start)
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        filters.append(("ts_event", ">=", start))
    if end is not None:
        end = pd.Timestamp(end)
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        filters.append(("ts_event", "<=", end))

    df = pd.read_parquet(
        paths.CLEAN_TICKS_PARQUET,
        filters=filters if filters else None,
    )

    # Filter to the traded outright contract.
    if symbol is not None and "symbol" in df.columns:
        df = df[df["symbol"] == symbol]

    # Off-instrument guard (reused from phoenix_tick_entry_quality).
    df = df[(df["price"] >= _PRICE_LO) & (df["price"] <= _PRICE_HI)]

    # Ensure sorted ascending by the time index (cache is already sorted, but
    # be defensive after filtering).
    if not df.index.is_monotonic_increasing:
        df = df.sort_index(kind="mergesort")

    # Derived order-flow columns.
    side = df["side"].astype("string").to_numpy()
    size = df["size"].fillna(0).to_numpy().astype("int64")
    signed = np.where(side == "A", size, np.where(side == "B", -size, 0)).astype("int64")
    df = df.copy()
    df["signed_delta"] = signed
    df["cvd"] = signed.cumsum()
    return df


def _ticks_to_arrays(ticks: pd.DataFrame):
    """Precompute the numpy arrays used by the per-trade searchsorted walks.
    Mirrors the TickIndex pattern in phoenix_tick_trail_verification."""
    ts_ns = ticks.index.values.astype("datetime64[ns]").view("int64")
    price = ticks["price"].to_numpy(dtype="float64")
    signed = ticks["signed_delta"].to_numpy(dtype="int64")
    size = ticks["size"].to_numpy(dtype="int64")
    return ts_ns, price, signed, size


def _in_coverage(entry_ts) -> bool:
    t = pd.Timestamp(entry_ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return TICK_COVERAGE[0] <= t <= TICK_COVERAGE[1]


def _coverage_mask(ets: pd.Series) -> np.ndarray:
    """Vectorized equivalent of [_in_coverage(t) for t in ets], returned as a
    positional numpy bool array.

    ``ets`` MUST already be a tz-aware (UTC) datetime Series (the callers build
    it via ``pd.to_datetime(..., utc=True)``). Replaces the per-row .apply /
    per-iteration ``_in_coverage`` calls (which re-wrapped each ts in a fresh
    pd.Timestamp) with a single masked comparison. NA semantics are unchanged:
    out-of-coverage positions stay False here, and the filters leave those rows
    as pd.NA (never scored True/False, never counted in the lift table)."""
    arr = ets.to_numpy()  # datetime64[ns, UTC]
    return (arr >= TICK_COVERAGE[0]) & (arr <= TICK_COVERAGE[1])


def _day_groups(ets: pd.Series):
    """Yield (positional_index_array, day_start, day_end) for each UTC calendar
    day that contains at least one IN-COVERAGE trade.

    ``ets`` is a tz-aware (UTC) datetime Series aligned 1:1 with the trades
    frame's rows. Out-of-coverage trades are simply omitted (they keep pd.NA /
    original P&L upstream). Grouping is by UTC calendar date, which is what the
    tick filters need: each group's ticks are loaded independently so peak RAM
    is bounded to ~one trading day (~800k rows) no matter how many days the
    trade set spans."""
    cov = _coverage_mask(ets)
    if not cov.any():
        return
    pos = np.flatnonzero(cov)
    # Work entirely in int64 ns-since-epoch (UTC) so grouping never touches a
    # tz-aware datetime64 conversion (which emits a numpy UserWarning). Floor
    # each entry to UTC midnight via integer division by one day's ns.
    ns_per_day = 86_400 * 1_000_000_000
    # ns-since-epoch (UTC) int64. ``to_numpy()`` yields UTC-wall datetime64[ns];
    # the .view("int64") is a pure reinterpret. numpy emits a cosmetic
    # "no explicit representation of timezones" warning on the tz drop even
    # though the UTC instant is exactly what we want -- suppress just that one.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        ns = ets.to_numpy().astype("datetime64[ns]").view("int64")[pos]
    day_key = (ns // ns_per_day) * ns_per_day
    for key in np.unique(day_key):
        idx = pos[day_key == key]
        day_start = pd.Timestamp(int(key), tz="UTC")
        day_end = day_start + pd.Timedelta(days=1)
        yield idx, day_start, day_end


def _load_day_window_ticks(trades_df: pd.DataFrame, symbol: str = "MNQM6",
                           horizon_min: int = 0) -> Optional[pd.DataFrame]:
    """Load only the tick span needed to cover all in-coverage trades, padded by
    a lookback head (60s) and a forward horizon (horizon_min). Returns None if
    no trades are in coverage.

    NOTE (memory): this loads ONE contiguous [min(entry)-60s, max(entry)+horizon]
    slice. For a trade set spanning many days that slice is huge, so the tick
    filters NO LONGER call this for multi-day sets -- they day-batch via
    ``_day_groups`` (loading one calendar day at a time) instead. This helper is
    retained for the single-window / small-span verification path and for
    backward compatibility, but is not on the hot multi-day path."""
    if trades_df.empty:
        return None
    ets = pd.to_datetime(trades_df["entry_ts"], utc=True)
    in_cov = _coverage_mask(ets)
    if not in_cov.any():
        return None
    cov_ets = ets[in_cov]
    lo = cov_ets.min() - pd.Timedelta(seconds=60)
    hi = cov_ets.max() + pd.Timedelta(minutes=max(horizon_min, 1))
    return load_ticks(start=lo, end=hi, symbol=symbol)


# ════════════════════════════════════════════════════════════════════
# 2.2  CVD order-flow ABSORPTION filter (tick level)  [PRIMARY VALUE]
# ════════════════════════════════════════════════════════════════════

# Threshold rationale: a window shows ONE-SIDED aggression when the net signed
# delta over the lookback is a large fraction of total traded volume. We require
# |sum(signed_delta)| / total_volume >= ABSORPTION_IMBALANCE_RATIO. A balanced
# tape sits near 0; 0.30 means ~65/35 buy/sell pressure (since
# |buy-sell|/(buy+sell)=0.30 implies the heavy side is ~65% of volume). This is
# strong, one-directional aggression -- the precondition for spotting ABSORPTION
# (that aggression NOT moving price its way). We also require a minimum traded
# volume in the window so a thin tape with two prints cannot trip the filter.
ABSORPTION_IMBALANCE_RATIO = 0.30
ABSORPTION_MIN_WINDOW_VOLUME = 50  # contracts (MNQ); thin-tape guard


def apply_absorption_filter(trades_df: pd.DataFrame,
                            lookback_seconds: int = 30,
                            ticks: Optional[pd.DataFrame] = None,
                            symbol: str = "MNQM6") -> pd.DataFrame:
    """Add column ``absorption_confirms`` (bool or pd.NA).

    PASSIVE ABSORPTION = strong one-sided aggression that FAILS to move price the
    aggressors' way, i.e. the resting (passive) side soaked it up:

      LONG entry  -> we want aggressive SELLING absorbed: cumulative signed delta
                     over the lookback window strongly NEGATIVE
                     (delta/volume <= -RATIO) BUT price holds or ticks up
                     (last_price >= first_price). Sellers hit bids, bids held ->
                     bullish confirmation.
      SHORT entry -> mirror: delta strongly POSITIVE (>= +RATIO) but price holds
                     or falls (last_price <= first_price). Buyers lifted offers,
                     offers held -> bearish confirmation.

    The window is [entry_ts - lookback_seconds, entry_ts]. Trades outside tick
    coverage get pd.NA.

    Threshold: |delta|/window_volume >= ABSORPTION_IMBALANCE_RATIO (0.30) with a
    minimum window volume of ABSORPTION_MIN_WINDOW_VOLUME (50) contracts.
    """
    out = trades_df.copy()
    n = len(out)
    if n == 0:
        out["absorption_confirms"] = pd.array([], dtype="boolean")
        return out

    result = pd.array([pd.NA] * n, dtype="boolean")
    ets = pd.to_datetime(out["entry_ts"], utc=True)
    dirs = out["direction"].astype("string").to_numpy()
    lookback_ns = int(lookback_seconds * 1e9)

    def _score(ticks_df: pd.DataFrame, positions: np.ndarray) -> None:
        """Score the trades at ``positions`` (positional indices into ``out``)
        against ``ticks_df`` and write into ``result`` in place. Filter math is
        byte-for-byte identical to the original per-trade loop; only WHICH ticks
        are in memory differs (day-batched vs one big slice)."""
        if ticks_df is None or ticks_df.empty:
            return
        ts_ns, price, signed, size = _ticks_to_arrays(ticks_df)
        for i in positions:
            i = int(i)
            entry_ns = int(ets.iloc[i].value)
            lo = np.searchsorted(ts_ns, entry_ns - lookback_ns, side="left")
            hi = np.searchsorted(ts_ns, entry_ns, side="right")
            if hi - lo < _MIN_TICKS_TO_JUDGE:
                result[i] = pd.NA  # not enough ticks to judge
                continue
            win_signed = signed[lo:hi]
            win_size = size[lo:hi]
            win_price = price[lo:hi]
            total_vol = int(win_size.sum())
            if total_vol < ABSORPTION_MIN_WINDOW_VOLUME:
                result[i] = False  # thin tape: no confirmation
                continue
            net_delta = int(win_signed.sum())
            imbalance = net_delta / total_vol  # in [-1, 1]
            price_change = float(win_price[-1] - win_price[0])

            is_long = (dirs[i] == "LONG")
            if is_long:
                # Aggressive selling (imbalance very negative) absorbed -> price held/up.
                confirms = (imbalance <= -ABSORPTION_IMBALANCE_RATIO) and (price_change >= 0.0)
            else:
                # Aggressive buying (imbalance very positive) absorbed -> price held/down.
                confirms = (imbalance >= ABSORPTION_IMBALANCE_RATIO) and (price_change <= 0.0)
            result[i] = bool(confirms)

    if ticks is not None:
        # Caller injected ticks: score every in-coverage trade against them
        # (preserves the single-window injection contract used by the selftest).
        cov_pos = np.flatnonzero(_coverage_mask(ets))
        _score(ticks, cov_pos)
    else:
        # Day-batch: load ONE calendar day of ticks at a time so peak RAM is
        # bounded to ~one trading day regardless of how many days `out` spans.
        for idx, day_start, day_end in _day_groups(ets):
            # Pad the head so a trade early in the UTC day still sees its full
            # lookback window (which can reach back into the prior minute(s)).
            day_ticks = load_ticks(start=day_start - pd.Timedelta(seconds=lookback_seconds + 1),
                                   end=day_end, symbol=symbol)
            _score(day_ticks, idx)
            del day_ticks  # release before loading the next day

    out["absorption_confirms"] = result
    return out


# ════════════════════════════════════════════════════════════════════
# 2.4  Internal delta CLUSTERS + trailing guardrail (tick level)
# ════════════════════════════════════════════════════════════════════

# Cluster-size threshold rationale: within the post-entry execution window we
# bin signed delta into 0.25-tick price buckets (a "footprint" column). The
# heaviest-imbalance bucket is a "cluster" worth guarding only if it is large
# both in absolute terms AND relative to the window. We require:
#   abs(bucket_delta) >= CLUSTER_MIN_ABS_DELTA   (absolute floor; thin tape guard)
#   abs(bucket_delta) >= CLUSTER_FRAC * total_abs_delta_in_window
# 0.25 means a single price level absorbed/aggressed at least a quarter of all
# the window's directional flow -- a genuine concentration, not noise spread
# across many levels. The guardrail only fires when that cluster sits at/near
# the wick extreme (within CLUSTER_WICK_TOL_TICKS) AND price then trades back
# THROUGH it (a failed push / exhaustion), which is the classic absorption-at-
# the-high reversal that a fixed stop would sit through.
CLUSTER_FRAC = 0.25
CLUSTER_MIN_ABS_DELTA = 40        # contracts at one 0.25 bucket
CLUSTER_WICK_TOL_TICKS = 2.0      # cluster must be within 2 ticks of the wick
CLUSTER_FAIL_TOL_TICKS = 1.0      # price must trade >= 1 tick back through cluster
CLUSTER_HORIZON_MIN = 5           # execution window after entry (minutes)


# NOTE on ``mnq_1m_df``: this parameter is RESERVED / currently UNUSED. The
# "candles" this guardrail bins are derived directly from the tick stream (1m
# buckets walked via searchsorted below), not from the OHLCV bars, so no bar
# frame is needed. The parameter is kept in the signature because the
# orchestrator (run_portfolio_backtest.run_micro) calls this positionally as
# ``apply_delta_cluster_trail(sub, mnq_1m)`` -- removing it would break that call.
def apply_delta_cluster_trail(trades_df: pd.DataFrame,
                              mnq_1m_df: pd.DataFrame,
                              horizon_min: int = CLUSTER_HORIZON_MIN,
                              ticks: Optional[pd.DataFrame] = None,
                              symbol: str = "MNQM6") -> pd.DataFrame:
    """Add columns ``trail_scratch_exit`` (bool) and ``trail_adj_pnl_dollars``
    (float).

    Within the trade's execution candle(s) after entry (the first
    ``horizon_min`` minutes), bin signed delta by 0.25-tick price bucket and find
    the heaviest-imbalance bucket (the delta "cluster").

    Guardrail rule (LONG):
      If a breakout LONG shows a massive POSITIVE delta cluster at/near the
      candle wick HIGH (within CLUSTER_WICK_TOL_TICKS) that then FAILS to hold --
      price trades back below that cluster level by CLUSTER_FAIL_TOL_TICKS within
      the window -- exit at SCRATCH (~entry_price, 0 P&L minus friction) instead
      of waiting for the full stop.
    Mirror for SHORT (massive NEGATIVE cluster at the wick LOW that price trades
    back above).

    ``trail_adj_pnl_dollars`` is the P&L the trade WOULD have had under this
    scratch overlay:
        scratch trades  -> -SCRATCH_FRICTION_DOLLARS (~0, minus one-tick friction)
        untouched trades-> original pnl_dollars
    Trades outside tick coverage are never scratched (trail_scratch_exit=False)
    and keep their original pnl_dollars.
    """
    out = trades_df.copy()
    n = len(out)
    if n == 0:
        out["trail_scratch_exit"] = pd.array([], dtype="bool")
        out["trail_adj_pnl_dollars"] = pd.Series([], dtype="float64")
        return out

    scratch = np.zeros(n, dtype=bool)
    adj_pnl = out["pnl_dollars"].to_numpy(dtype="float64").copy()

    ets = pd.to_datetime(out["entry_ts"], utc=True)
    dirs = out["direction"].astype("string").to_numpy()
    bar_ns = int(60 * 1e9)

    def _score(ticks_df: pd.DataFrame, positions: np.ndarray) -> None:
        """Score the trades at ``positions`` (positional indices into ``out``)
        against ``ticks_df``, writing ``scratch`` / ``adj_pnl`` in place. The
        cluster/footprint/failure math is byte-for-byte identical to the
        original loop; only WHICH ticks are resident in RAM differs."""
        if ticks_df is None or ticks_df.empty:
            return
        ts_ns, price, signed, size = _ticks_to_arrays(ticks_df)
        for i in positions:
            i = int(i)
            entry_ns = int(ets.iloc[i].value)
            is_long = (dirs[i] == "LONG")
            # Walk the execution candles one minute at a time. The "footprint"
            # cluster is meaningful WITHIN a candle (delta concentrated at one
            # price level), so we bin per 1m candle and test the guardrail on
            # each candle's own wick. Price-failure may extend into the rest of
            # the horizon (subsequent candles), mirroring a stop sitting through
            # a failed push at the high/low.
            horizon_end_ns = entry_ns + int(horizon_min) * bar_ns
            for c in range(int(horizon_min)):
                c_start = entry_ns + c * bar_ns
                c_end = c_start + bar_ns
                lo = np.searchsorted(ts_ns, c_start, side="left")
                hi = np.searchsorted(ts_ns, c_end, side="right")
                if hi - lo < _MIN_TICKS_TO_JUDGE_CLUSTER:
                    continue
                cand_price = price[lo:hi]
                cand_signed = signed[lo:hi]

                # Footprint: signed delta per 0.25-tick bucket within this candle.
                buckets = np.round(cand_price / TICK_SIZE).astype("int64")
                uniq, inv = np.unique(buckets, return_inverse=True)
                bucket_delta = np.zeros(len(uniq), dtype="float64")
                np.add.at(bucket_delta, inv, cand_signed.astype("float64"))
                total_abs = float(np.abs(cand_signed).sum())
                if total_abs <= 0:
                    continue

                # Ticks from the cluster onward, through the END of the horizon
                # (not just this candle), so a failure that develops over the
                # next minute still counts.
                fail_hi = np.searchsorted(ts_ns, horizon_end_ns, side="right")

                if is_long:
                    k = int(np.argmax(bucket_delta))      # heaviest BUY cluster
                    cluster_delta = bucket_delta[k]
                    cluster_px = uniq[k] * TICK_SIZE
                    wick = float(cand_price.max())        # candle wick HIGH
                    near_wick = (wick - cluster_px) <= CLUSTER_WICK_TOL_TICKS * TICK_SIZE
                    big = (cluster_delta >= CLUSTER_MIN_ABS_DELTA) and \
                          (cluster_delta >= CLUSTER_FRAC * total_abs)
                    ci = lo + int(np.argmax(buckets == uniq[k]))  # first tick at cluster
                    failed = bool(
                        (price[ci:fail_hi] <= cluster_px - CLUSTER_FAIL_TOL_TICKS * TICK_SIZE).any()
                    ) if ci < fail_hi else False
                else:
                    k = int(np.argmin(bucket_delta))      # heaviest SELL cluster
                    cluster_delta = bucket_delta[k]
                    cluster_px = uniq[k] * TICK_SIZE
                    wick = float(cand_price.min())        # candle wick LOW
                    near_wick = (cluster_px - wick) <= CLUSTER_WICK_TOL_TICKS * TICK_SIZE
                    big = (-cluster_delta >= CLUSTER_MIN_ABS_DELTA) and \
                          (-cluster_delta >= CLUSTER_FRAC * total_abs)
                    ci = lo + int(np.argmax(buckets == uniq[k]))
                    failed = bool(
                        (price[ci:fail_hi] >= cluster_px + CLUSTER_FAIL_TOL_TICKS * TICK_SIZE).any()
                    ) if ci < fail_hi else False

                if big and near_wick and failed:
                    scratch[i] = True
                    adj_pnl[i] = -SCRATCH_FRICTION_DOLLARS
                    break  # one scratch-worthy candle is enough

    if ticks is not None:
        # Caller injected ticks: score every in-coverage trade against them
        # (preserves the single-window injection contract used by the selftest).
        cov_pos = np.flatnonzero(_coverage_mask(ets))
        _score(ticks, cov_pos)
    else:
        # Day-batch: load ONE calendar day at a time. The execution horizon runs
        # FORWARD from entry, so pad the tail by horizon_min (+1) so a trade late
        # in the UTC day still sees its full post-entry window.
        for idx, day_start, day_end in _day_groups(ets):
            day_ticks = load_ticks(start=day_start,
                                   end=day_end + pd.Timedelta(minutes=int(horizon_min) + 1),
                                   symbol=symbol)
            _score(day_ticks, idx)
            del day_ticks  # release before loading the next day

    out["trail_scratch_exit"] = scratch
    out["trail_adj_pnl_dollars"] = adj_pnl
    return out


# ════════════════════════════════════════════════════════════════════
# 2.1  Intermarket SMT divergence / convergence (BAR level)
# ════════════════════════════════════════════════════════════════════

def _load_bars(csv_path: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Load a 1m OHLCV CSV (ts_utc, open, high, low, close, volume) sliced to
    [start, end], indexed by UTC timestamp."""
    df = pd.read_csv(csv_path)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts").sort_index()
    df = df.loc[(df.index >= start) & (df.index <= end)]
    return df[["open", "high", "low", "close", "volume"]]


def apply_intermarket_filter(trades_df: pd.DataFrame,
                             mnq_1m_df: Optional[pd.DataFrame] = None,
                             mes_1m_df: Optional[pd.DataFrame] = None,
                             swing_lookback_bars: int = 5,
                             vol_expansion: float = 1.2) -> pd.DataFrame:
    """Add column ``intermarket_confirms`` (bool or pd.NA).

    NOTE: MES has NO tick data, so this filter operates on 1-MINUTE BARS, not on
    millisecond order flow. It is therefore coarser than the tick-level filters.

    Strategy-type routing (module-level sets):
      REVERSAL_STRATEGIES -> look for SMT DIVERGENCE:
          near a swing extreme at entry, MNQ makes a higher high (LONG-fade /
          SHORT-reversal context) but MES FAILS to confirm (lower-or-equal high)
          -> bearish reversal confirm; mirror at swing lows for the bullish case.
      TREND_STRATEGIES -> look for CONVERGENCE:
          both MNQ and MES break the prior swing_lookback_bars high (LONG) or low
          (SHORT) on the SAME bar, with MNQ volume expanding vs its prior-N mean
          (>= vol_expansion x) -> breakout confirm.
      Unknown strategy -> pd.NA.

    Bars used are the entry bar and the swing_lookback_bars bars before it. The
    entry bar is the 1m bar containing entry_ts. Trades outside tick coverage are
    still scored here (this filter is bar-level and bars exist for the full 5y),
    BUT to keep the lift table apples-to-apples with the tick filters we only
    populate values for trades whose entry resolves to a real bar; trades whose
    entry has no surrounding bars get pd.NA.
    """
    out = trades_df.copy()
    n = len(out)
    if n == 0:
        out["intermarket_confirms"] = pd.array([], dtype="boolean")
        return out

    ets = pd.to_datetime(out["entry_ts"], utc=True)
    span_lo = ets.min() - pd.Timedelta(minutes=swing_lookback_bars + 2)
    span_hi = ets.max() + pd.Timedelta(minutes=2)

    if mnq_1m_df is None:
        mnq_1m_df = _load_bars(paths.MNQ_1M_CSV, span_lo, span_hi)
    if mes_1m_df is None:
        mes_1m_df = _load_bars(paths.MES_1M_CSV, span_lo, span_hi)

    # Normalize both bar frames to a UTC DatetimeIndex regardless of how the
    # caller built them (column 'ts'/'ts_utc' or already-indexed).
    def _norm(bars: pd.DataFrame) -> pd.DataFrame:
        b = bars.copy()
        if not isinstance(b.index, pd.DatetimeIndex):
            tcol = "ts" if "ts" in b.columns else ("ts_utc" if "ts_utc" in b.columns else None)
            if tcol is None:
                raise KeyError("intermarket bars need a 'ts'/'ts_utc' column or DatetimeIndex")
            b = b.set_index(pd.to_datetime(b[tcol], utc=True))
        b = b.sort_index()
        return b

    mnq = _norm(mnq_1m_df)
    mes = _norm(mes_1m_df)
    mnq_ts = mnq.index.values.astype("datetime64[ns]").view("int64")
    mes_ts = mes.index.values.astype("datetime64[ns]").view("int64")
    mnq_high = mnq["high"].to_numpy(dtype="float64")
    mnq_low = mnq["low"].to_numpy(dtype="float64")
    mnq_vol = mnq["volume"].to_numpy(dtype="float64")
    mes_high = mes["high"].to_numpy(dtype="float64")
    mes_low = mes["low"].to_numpy(dtype="float64")

    dirs = out["direction"].astype("string").to_numpy()
    strats = out["strategy"].astype("string").to_numpy()
    result = pd.array([pd.NA] * n, dtype="boolean")

    for i in range(n):
        strat = strats[i]
        is_rev = strat in REVERSAL_STRATEGIES
        is_trend = strat in TREND_STRATEGIES
        if not (is_rev or is_trend):
            continue  # unknown -> pd.NA

        entry_ns = int(ets.iloc[i].value)
        # Entry bar = the bar whose timestamp is the last <= entry_ts.
        e_idx = int(np.searchsorted(mnq_ts, entry_ns, side="right")) - 1
        if e_idx < swing_lookback_bars:
            continue  # not enough history -> pd.NA
        m_idx = int(np.searchsorted(mes_ts, mnq_ts[e_idx], side="right")) - 1
        if m_idx < swing_lookback_bars:
            continue

        is_long = (dirs[i] == "LONG")
        prior_lo = e_idx - swing_lookback_bars
        # MNQ prior swing extremes (exclude the entry bar itself).
        mnq_prior_high = float(mnq_high[prior_lo:e_idx].max())
        mnq_prior_low = float(mnq_low[prior_lo:e_idx].min())
        mes_prior_high = float(mes_high[m_idx - swing_lookback_bars:m_idx].max())
        mes_prior_low = float(mes_low[m_idx - swing_lookback_bars:m_idx].min())

        mnq_eh, mnq_el = float(mnq_high[e_idx]), float(mnq_low[e_idx])
        mes_eh, mes_el = float(mes_high[m_idx]), float(mes_low[m_idx])

        if is_rev:
            # SMT divergence: leader makes a new extreme, MES fails to confirm.
            if is_long:
                # Bullish reversal: MNQ makes a LOWER low but MES holds (higher-or-equal low).
                mnq_new_low = mnq_el < mnq_prior_low
                mes_fails = mes_el >= mes_prior_low
                result[i] = bool(mnq_new_low and mes_fails)
            else:
                # Bearish reversal: MNQ makes a HIGHER high but MES fails (lower-or-equal high).
                mnq_new_high = mnq_eh > mnq_prior_high
                mes_fails = mes_eh <= mes_prior_high
                result[i] = bool(mnq_new_high and mes_fails)
        else:  # trend / breakout -> convergence
            # Both break the same-direction prior extreme on this bar + MNQ vol expansion.
            mean_vol = float(mnq_vol[prior_lo:e_idx].mean()) if e_idx > prior_lo else 0.0
            vol_ok = (mean_vol > 0) and (mnq_vol[e_idx] >= vol_expansion * mean_vol)
            if is_long:
                both_break = (mnq_eh > mnq_prior_high) and (mes_eh > mes_prior_high)
                result[i] = bool(both_break and vol_ok)
            else:
                both_break = (mnq_el < mnq_prior_low) and (mes_el < mes_prior_low)
                result[i] = bool(both_break and vol_ok)

    out["intermarket_confirms"] = result
    return out


# ════════════════════════════════════════════════════════════════════
# 2.3  DOM liquidity-sweep / stop-hunt  (NOT COMPUTABLE)
# ════════════════════════════════════════════════════════════════════

def dom_stop_hunt_analysis(*args, **kwargs) -> dict:
    """DOM liquidity-sweep / stop-hunt analysis -- NOT COMPUTABLE in this repo.

    A stop-hunt / liquidity-sweep detector needs to know the RESTING (passive)
    size sitting 1-4 ticks beyond the protective stop, so it can tell whether a
    fast spike that tags the stop was a deliberate sweep of that resting
    liquidity (which then reverses) versus genuine directional flow.

    The only tick data in this repo is the TBBO clean cache, which carries the
    BEST bid / best ask price (bid_px_00 / ask_px_00) ONLY -- top of book. There
    is NO Level-2 / market-by-price / depth-of-book feed anywhere in the data
    directory, so the resting size beyond the stop cannot be reconstructed.

    Rather than fabricate depth from top-of-book (which would be a guess, not a
    measurement), this function returns a status sentinel.
    """
    return {
        "status": "NOT_COMPUTABLE",
        "reason": (
            "TBBO is top-of-book (best bid/ask only); no Level-2 depth-of-book "
            "data exists in the repo, so resting liquidity 1-4 ticks beyond the "
            "stop cannot be reconstructed."
        ),
    }


# ════════════════════════════════════════════════════════════════════
# Lift table
# ════════════════════════════════════════════════════════════════════

def _summ_row(filter_name: str, subset_name: str, sub: pd.DataFrame,
              pnl_col: str = "pnl_dollars") -> dict:
    """One lift-table row from analytics.summarize over ``sub`` (using pnl_col as
    the P&L source)."""
    if sub.empty:
        return {"filter": filter_name, "subset": subset_name, "n": 0,
                "net_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0}
    work = sub.copy()
    if pnl_col != "pnl_dollars":
        work["pnl_dollars"] = work[pnl_col].astype("float64")
    s = analytics.summarize(work)
    return {
        "filter": filter_name,
        "subset": subset_name,
        "n": s["n"],
        "net_pnl": s["net_pnl"],
        "win_rate": s["win_rate"],
        "profit_factor": s["profit_factor"],
    }


def microstructure_lift_table(trades_df: pd.DataFrame,
                              write_csv: bool = True) -> pd.DataFrame:
    """For each filter (absorption, delta-trail, intermarket) compute
    baseline-vs-filtered stats over the TICK-COVERED trades, via
    analytics.summarize.

    Columns: filter, subset ('baseline'|'passed'|'failed'|'trail_applied'),
             n, net_pnl, win_rate, profit_factor.

    The caller is expected to have already run the relevant apply_* functions so
    the filter columns exist; any missing column is simply skipped. Writes
    OUT_DIR/'microstructure_lift.csv' when write_csv is True.

    'Baseline' for the tick filters (absorption, delta-trail) is the set of
    trades whose entry is within TICK_COVERAGE (so the comparison is
    apples-to-apples). 'Baseline' for the bar-level intermarket filter is the set
    of trades for which the filter produced a non-NA value (known strategy with
    enough bar history).
    """
    rows: list[dict] = []
    if trades_df is None or trades_df.empty:
        cols = ["filter", "subset", "n", "net_pnl", "win_rate", "profit_factor"]
        empty = pd.DataFrame(columns=cols)
        if write_csv:
            _write_lift_csv(empty)
        return empty

    df = trades_df.copy()
    ets = pd.to_datetime(df["entry_ts"], utc=True)
    in_cov = _coverage_mask(ets)  # vectorized, positional bool mask
    covered = df[in_cov]

    # --- Absorption (tick) ---
    if "absorption_confirms" in df.columns:
        rows.append(_summ_row("absorption", "baseline", covered))
        passed = covered[covered["absorption_confirms"] == True]   # noqa: E712
        failed = covered[covered["absorption_confirms"] == False]  # noqa: E712
        rows.append(_summ_row("absorption", "passed", passed))
        rows.append(_summ_row("absorption", "failed", failed))

    # --- Delta-cluster trail (tick) ---
    if "trail_adj_pnl_dollars" in df.columns:
        rows.append(_summ_row("delta_trail", "baseline", covered))
        # 'trail_applied' = the same covered set but scored under the scratch
        # overlay P&L, so the lift is baseline_pnl vs overlay_pnl.
        rows.append(_summ_row("delta_trail", "trail_applied", covered,
                              pnl_col="trail_adj_pnl_dollars"))
        if "trail_scratch_exit" in df.columns:
            scratched = covered[covered["trail_scratch_exit"] == True]  # noqa: E712
            # Report the scratched subset's ORIGINAL P&L (what we avoided/gave up).
            rows.append(_summ_row("delta_trail", "scratched_original", scratched))

    # --- Intermarket (bar level) ---
    if "intermarket_confirms" in df.columns:
        known = df[df["intermarket_confirms"].notna()]
        rows.append(_summ_row("intermarket", "baseline", known))
        passed = known[known["intermarket_confirms"] == True]   # noqa: E712
        failed = known[known["intermarket_confirms"] == False]  # noqa: E712
        rows.append(_summ_row("intermarket", "passed", passed))
        rows.append(_summ_row("intermarket", "failed", failed))

    table = pd.DataFrame(rows, columns=["filter", "subset", "n", "net_pnl",
                                        "win_rate", "profit_factor"])
    if write_csv:
        _write_lift_csv(table)
    return table


def _write_lift_csv(table: pd.DataFrame) -> Path:
    paths.OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = paths.OUT_DIR / "microstructure_lift.csv"
    table.to_csv(dest, index=False)
    return dest


# ════════════════════════════════════════════════════════════════════
# Self-test (one day of real ticks + synthetic trades) -- run:
#   python microstructure.py
# ════════════════════════════════════════════════════════════════════

def _find_absorption_window(ts_ns, price, signed, size, direction: str,
                            lookback_seconds: int = 30):
    """Self-test helper: scan the day's tape for the first 30s window that would
    CONFIRM absorption for ``direction`` under apply_absorption_filter's own
    thresholds, and return an entry timestamp whose lookback ends there. Returns
    a tz-aware pd.Timestamp or None. (Test-only; not part of the public API.)"""
    look = int(lookback_seconds * 1e9)
    step = int(60 * 1e9)
    t = ts_ns[0] + look
    while t < ts_ns[-1]:
        lo = np.searchsorted(ts_ns, t - look, side="left")
        hi = np.searchsorted(ts_ns, t, side="right")
        if hi - lo >= 20:
            tv = int(size[lo:hi].sum())
            if tv >= ABSORPTION_MIN_WINDOW_VOLUME:
                imb = int(signed[lo:hi].sum()) / tv
                pc = float(price[hi - 1] - price[lo])
                if direction == "LONG" and imb <= -ABSORPTION_IMBALANCE_RATIO and pc >= 0:
                    return pd.Timestamp(int(t), tz="UTC")
                if direction == "SHORT" and imb >= ABSORPTION_IMBALANCE_RATIO and pc <= 0:
                    return pd.Timestamp(int(t), tz="UTC")
        t += step
    return None


def _make_exhaustion_ticks(entry_ts: pd.Timestamp, base_px: float) -> pd.DataFrame:
    """Self-test helper: build a small synthetic tick frame whose first candle is
    a LONG buy-climax-at-the-high-then-fail (the exact footprint the cluster
    guardrail targets). Returns a ticks DataFrame shaped exactly like load_ticks
    output (index ts_event UTC ns; columns symbol/price/size/side/bid/ask/
    signed_delta/cvd). (Test-only; not part of the public API.)"""
    rows = []
    t0 = pd.Timestamp(entry_ts).value
    ms = int(1e6)
    # Run price up to a high tick, dump a huge buy cluster AT the high, then fail.
    seq = []
    px = base_px
    # ramp up 8 ticks with normal-size buys (delta spread across levels)
    for s in range(8):
        px += TICK_SIZE
        seq.append((px, 3, "A"))
    high = px
    # MASSIVE buy cluster right at the high tick (single price level)
    for _ in range(60):
        seq.append((high, 4, "A"))   # 240 contracts of buying at the high bucket
    # then price FAILS: trades back down well below the cluster
    for s in range(12):
        px -= TICK_SIZE
        seq.append((px, 3, "B"))
    for k, (p, sz, side) in enumerate(seq):
        ts = t0 + (k + 1) * 200 * ms  # 200ms apart -> all inside the entry candle
        rows.append({
            "symbol": "MNQM6", "price": float(p), "size": int(sz), "side": side,
            "bid_px_00": float(p) - TICK_SIZE, "ask_px_00": float(p),
        })
    idx = pd.to_datetime(
        [t0 + (k + 1) * 200 * ms for k in range(len(seq))], utc=True
    )
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(idx, name="ts_event")
    side = df["side"].astype("string").to_numpy()
    size = df["size"].fillna(0).to_numpy().astype("int64")
    signed = np.where(side == "A", size, np.where(side == "B", -size, 0)).astype("int64")
    df["signed_delta"] = signed
    df["cvd"] = signed.cumsum()
    return df


def _selftest() -> None:
    print("=" * 64)
    print("microstructure._selftest")
    print("=" * 64)

    # --- 1. Load ONE day of ticks and verify schema + signed-delta/CVD ----
    day = pd.Timestamp("2026-04-15", tz="UTC")
    t0 = time.time()
    ticks = load_ticks(start=day, end=day + pd.Timedelta(days=1), symbol="MNQM6")
    print(f"[ticks] loaded {len(ticks):,} ticks for {day.date()} in "
          f"{time.time()-t0:.1f}s")
    assert ticks.index.name == "ts_event", "index must be ts_event"
    assert str(ticks.index.dtype) == "datetime64[ns, UTC]", "index must be UTC ns"
    assert ticks.index.is_monotonic_increasing, "ticks must be sorted ascending"
    for c in ("symbol", "price", "size", "side", "bid_px_00", "ask_px_00",
              "signed_delta", "cvd"):
        assert c in ticks.columns, f"missing column {c}"
    assert (ticks["symbol"] == "MNQM6").all(), "symbol must be MNQM6"

    # Independent recompute of signed delta + CVD to verify the derived columns.
    side = ticks["side"].astype("string").to_numpy()
    size = ticks["size"].to_numpy().astype("int64")
    expect_signed = np.where(side == "A", size, np.where(side == "B", -size, 0))
    assert (ticks["signed_delta"].to_numpy() == expect_signed).all(), "signed_delta mismatch"
    assert int(ticks["cvd"].iloc[-1]) == int(expect_signed.sum()), "CVD endpoint mismatch"
    print(f"[ticks] signed-delta/CVD verified. day CVD close = "
          f"{int(ticks['cvd'].iloc[-1]):+,} contracts; "
          f"price {ticks['price'].iloc[0]:.2f} -> {ticks['price'].iloc[-1]:.2f}")

    # --- 2. Build a few synthetic trades within that day -------------------
    # Place entries at known liquid times; mix LONG/SHORT and known strategies.
    px0 = float(ticks["price"].iloc[0])
    entry_times = [
        day + pd.Timedelta(hours=13, minutes=35),   # ~08:35 CT
        day + pd.Timedelta(hours=14, minutes=10),
        day + pd.Timedelta(hours=15, minutes=20),
        day + pd.Timedelta(hours=16, minutes=5),
        day + pd.Timedelta(hours=17, minutes=30),
        day + pd.Timedelta(hours=18, minutes=45),
    ]
    strat_cycle = ["bias_momentum", "spring_setup", "ib_breakout",
                   "vwap_band_pullback", "opening_session", "noise_area"]
    recs = []
    ts_ns = ticks.index.values.astype("datetime64[ns]").view("int64")
    px_arr = ticks["price"].to_numpy(dtype="float64")
    for k, et in enumerate(entry_times):
        ens = int(et.value)
        j = int(np.searchsorted(ts_ns, ens, side="left"))
        j = min(j, len(px_arr) - 1)
        ep = float(px_arr[j])
        direction = "LONG" if k % 2 == 0 else "SHORT"
        # Exit ~8 minutes later at whatever price prevailed (synthetic).
        xt = et + pd.Timedelta(minutes=8)
        xj = int(np.searchsorted(ts_ns, int(xt.value), side="left"))
        xj = min(xj, len(px_arr) - 1)
        xp = float(px_arr[xj])
        ticks_pnl = (xp - ep) / TICK_SIZE if direction == "LONG" else (ep - xp) / TICK_SIZE
        stop = ep - 20 * TICK_SIZE if direction == "LONG" else ep + 20 * TICK_SIZE
        tgt = ep + 40 * TICK_SIZE if direction == "LONG" else ep - 40 * TICK_SIZE
        recs.append({
            "strategy": strat_cycle[k % len(strat_cycle)],
            "direction": direction,
            "entry_ts": et, "entry_price": ep,
            "stop_price": stop, "target_price": tgt,
            "exit_ts": xt, "exit_price": xp, "exit_reason": "time_exit",
            "pnl_ticks": round(ticks_pnl), "pnl_dollars": round(ticks_pnl) * TICK_VALUE,
            "hold_min": 8.0,
        })
    # Add two trades whose entries sit on REAL one-sided-absorption windows in
    # this day's tape, so the self-test exercises the absorption True branch
    # (not just the "no confirmation" branch). We locate them by scanning 30s
    # windows for |imbalance| >= ratio with price holding the aggressors' way.
    for want_dir in ("LONG", "SHORT"):
        et_hit = _find_absorption_window(ts_ns, px_arr,
                                         ticks["signed_delta"].to_numpy(dtype="int64"),
                                         ticks["size"].to_numpy(dtype="int64"),
                                         want_dir, lookback_seconds=30)
        if et_hit is not None:
            j = int(np.searchsorted(ts_ns, int(et_hit.value), side="left"))
            j = min(j, len(px_arr) - 1)
            ep = float(px_arr[j])
            recs.append({
                "strategy": "spring_setup", "direction": want_dir,
                "entry_ts": et_hit, "entry_price": ep,
                "stop_price": ep - 20 * TICK_SIZE if want_dir == "LONG" else ep + 20 * TICK_SIZE,
                "target_price": ep + 40 * TICK_SIZE if want_dir == "LONG" else ep - 40 * TICK_SIZE,
                "exit_ts": et_hit + pd.Timedelta(minutes=8),
                "exit_price": ep, "exit_reason": "time_exit",
                "pnl_ticks": 0, "pnl_dollars": 0.0, "hold_min": 8.0,
            })

    # Add one OUT-OF-COVERAGE trade (before the tick window) to confirm pd.NA.
    recs.append({
        "strategy": "bias_momentum", "direction": "LONG",
        "entry_ts": pd.Timestamp("2026-01-02 15:00", tz="UTC"),
        "entry_price": 21000.0, "stop_price": 20995.0, "target_price": 21010.0,
        "exit_ts": pd.Timestamp("2026-01-02 15:08", tz="UTC"),
        "exit_price": 21005.0, "exit_reason": "time_exit",
        "pnl_ticks": 20, "pnl_dollars": 10.0, "hold_min": 8.0,
    })
    trades = pd.DataFrame(recs)
    print(f"[trades] built {len(trades)} synthetic trades "
          f"({(trades['direction']=='LONG').sum()} LONG / "
          f"{(trades['direction']=='SHORT').sum()} SHORT); "
          f"1 deliberately out-of-coverage")

    # Build a 1m bar frame for that day for the cluster-trail + intermarket.
    # Load real MNQ bars for the day; build a synthetic MES frame aligned to it
    # if the real MES slice is empty (it should not be, but be robust).
    mnq_bars = _load_bars(paths.MNQ_1M_CSV, day - pd.Timedelta(minutes=10),
                          day + pd.Timedelta(days=1))
    mes_bars = _load_bars(paths.MES_1M_CSV, day - pd.Timedelta(minutes=10),
                          day + pd.Timedelta(days=1))
    print(f"[bars] MNQ 1m bars for day: {len(mnq_bars):,}; MES 1m bars: {len(mes_bars):,}")

    # --- 3. Run the overlays ----------------------------------------------
    trades = apply_absorption_filter(trades, lookback_seconds=30, ticks=ticks)
    n_abs_true = int((trades["absorption_confirms"] == True).sum())   # noqa: E712
    n_abs_false = int((trades["absorption_confirms"] == False).sum()) # noqa: E712
    n_abs_na = int(trades["absorption_confirms"].isna().sum())
    print(f"[2.2 absorption] confirms: {n_abs_true} True / {n_abs_false} False / "
          f"{n_abs_na} NA")
    # The out-of-coverage trade MUST be NA.
    assert pd.isna(trades["absorption_confirms"].iloc[-1]), \
        "out-of-coverage trade must be NA for absorption"
    # We seeded two trades on real absorption windows -> the True branch must fire.
    assert n_abs_true >= 1, "expected at least one absorption confirmation (True branch)"

    trades = apply_delta_cluster_trail(trades, mnq_bars, horizon_min=5, ticks=ticks)
    n_scratch = int(trades["trail_scratch_exit"].sum())
    assert "trail_adj_pnl_dollars" in trades.columns
    assert trades["trail_scratch_exit"].dtype == bool
    # Out-of-coverage trade must NOT be scratched and must keep original pnl.
    assert not bool(trades["trail_scratch_exit"].iloc[-1]), \
        "out-of-coverage trade must not be scratched"
    assert trades["trail_adj_pnl_dollars"].iloc[-1] == trades["pnl_dollars"].iloc[-1], \
        "out-of-coverage trade must keep original pnl"
    print(f"[2.4 delta-trail] scratch exits (real tape): {n_scratch}/{len(trades)}; "
          f"net pnl baseline={trades['pnl_dollars'].sum():.2f} -> "
          f"overlay={trades['trail_adj_pnl_dollars'].sum():.2f}")
    print("[2.4 delta-trail] note: heavy buy/sell delta concentrates in the "
          "candle BODY, not the wick, so the 'cluster-at-wick-then-fail' "
          "exhaustion trap is genuinely rare on real MNQ tape (selective by design).")

    # Prove the cluster-guardrail True branch with a CRAFTED exhaustion candle:
    # a LONG breakout whose entry minute prints a massive BUY delta cluster right
    # at the high tick, then price trades back through it (failed push). This
    # verifies the scratch logic is reachable + correct without depending on a
    # rare natural occurrence.
    synth_entry = pd.Timestamp("2026-04-15 14:00:00", tz="UTC")
    synth = _make_exhaustion_ticks(synth_entry, base_px=26200.0)
    synth_trade = pd.DataFrame([{
        "strategy": "ib_breakout", "direction": "LONG",
        "entry_ts": synth_entry, "entry_price": 26200.0,
        "stop_price": 26195.0, "target_price": 26210.0,
        "exit_ts": synth_entry + pd.Timedelta(minutes=5),
        "exit_price": 26195.0, "exit_reason": "stop",
        "pnl_ticks": -20, "pnl_dollars": -10.0, "hold_min": 5.0,
    }])
    synth_out = apply_delta_cluster_trail(synth_trade, None, horizon_min=5, ticks=synth)
    assert bool(synth_out["trail_scratch_exit"].iloc[0]), \
        "crafted exhaustion candle must trigger a scratch exit"
    assert abs(synth_out["trail_adj_pnl_dollars"].iloc[0] - (-SCRATCH_FRICTION_DOLLARS)) < 1e-9, \
        "scratched trade overlay pnl must equal -friction"
    print(f"[2.4 delta-trail] crafted-exhaustion check: scratch fired, "
          f"original pnl ${synth_out['pnl_dollars'].iloc[0]:.2f} -> "
          f"overlay ${synth_out['trail_adj_pnl_dollars'].iloc[0]:.2f} (True branch OK)")

    trades = apply_intermarket_filter(trades, mnq_bars, mes_bars)
    n_im_true = int((trades["intermarket_confirms"] == True).sum())   # noqa: E712
    n_im_false = int((trades["intermarket_confirms"] == False).sum()) # noqa: E712
    n_im_na = int(trades["intermarket_confirms"].isna().sum())
    print(f"[2.1 intermarket] confirms: {n_im_true} True / {n_im_false} False / "
          f"{n_im_na} NA (bar-level)")

    # --- 2.3 NOT COMPUTABLE check -----------------------------------------
    dom = dom_stop_hunt_analysis()
    assert dom["status"] == "NOT_COMPUTABLE", "2.3 must report NOT_COMPUTABLE"
    print(f"[2.3 dom] status={dom['status']}")

    # --- 4. MULTI-DAY day-batch memory proof ------------------------------
    # Build trades spread across 3 separate calendar days (all in coverage) plus
    # one out-of-coverage, then run all three apply_* filters with ticks=None so
    # the filters MUST day-batch. We wrap load_ticks to record each slice it
    # loads (start/end/rowcount): proof that only ~one trading day is resident at
    # a time, never the whole 43.8M-row parquet.
    print()
    print("-" * 64)
    print("[multi-day] day-batch memory proof (ticks=None -> per-day loads)")
    md_days = [pd.Timestamp("2026-04-14", tz="UTC"),
               pd.Timestamp("2026-04-15", tz="UTC"),
               pd.Timestamp("2026-04-16", tz="UTC")]
    md_recs = []
    md_strats = ["bias_momentum", "spring_setup", "ib_breakout",
                 "vwap_band_pullback", "opening_session", "noise_area"]
    for k in range(6):
        d = md_days[k % 3]
        et = d + pd.Timedelta(hours=14, minutes=30 + k)  # ~09:30 ET, in RTH
        direction = "LONG" if k % 2 == 0 else "SHORT"
        ep = 26000.0 + k
        md_recs.append({
            "strategy": md_strats[k], "direction": direction,
            "entry_ts": et, "entry_price": ep,
            "stop_price": ep - 20 * TICK_SIZE if direction == "LONG" else ep + 20 * TICK_SIZE,
            "target_price": ep + 40 * TICK_SIZE if direction == "LONG" else ep - 40 * TICK_SIZE,
            "exit_ts": et + pd.Timedelta(minutes=8), "exit_price": ep + 1.0,
            "exit_reason": "time_exit", "pnl_ticks": 4,
            "pnl_dollars": 2.0, "hold_min": 8.0,
        })
    # One deliberately out-of-coverage trade (before the tick window) -> pd.NA.
    md_recs.append({
        "strategy": "bias_momentum", "direction": "LONG",
        "entry_ts": pd.Timestamp("2026-01-05 15:00", tz="UTC"),
        "entry_price": 21000.0, "stop_price": 20995.0, "target_price": 21010.0,
        "exit_ts": pd.Timestamp("2026-01-05 15:08", tz="UTC"), "exit_price": 21005.0,
        "exit_reason": "time_exit", "pnl_ticks": 20, "pnl_dollars": 10.0, "hold_min": 8.0,
    })
    md_trades = pd.DataFrame(md_recs)
    md_unique_days = sorted({pd.Timestamp(t).normalize() for t in
                             pd.to_datetime(md_trades["entry_ts"], utc=True)
                             if _in_coverage(t)})
    print(f"[multi-day] {len(md_trades)} trades over {len(md_unique_days)} "
          f"in-coverage days + 1 out-of-coverage")

    # Instrument load_ticks to log each per-day slice (no production behavior
    # change -- this wrapper exists only for the duration of the self-test).
    _real_load = load_ticks
    _load_log: list = []
    _peak = {"max_rows": 0}

    def _logged_load(start=None, end=None, symbol="MNQM6"):
        df = _real_load(start=start, end=end, symbol=symbol)
        _load_log.append((pd.Timestamp(start), pd.Timestamp(end), len(df)))
        _peak["max_rows"] = max(_peak["max_rows"], len(df))
        print(f"  [load_ticks] {pd.Timestamp(start)} .. {pd.Timestamp(end)} "
              f"-> {len(df):,} rows")
        return df

    globals()["load_ticks"] = _logged_load
    try:
        md_trades = apply_absorption_filter(md_trades, lookback_seconds=30)  # ticks=None
        md_trades = apply_delta_cluster_trail(md_trades, None, horizon_min=5)  # ticks=None
        md_trades = apply_intermarket_filter(md_trades, mnq_bars, mes_bars)
        md_lift = microstructure_lift_table(md_trades, write_csv=False)
    finally:
        globals()["load_ticks"] = _real_load

    # (a) completed; (b) loaded day-by-day -> >= one load per filter per
    # in-coverage day, and each load held at most ~one day of ticks (well under
    # the full 43.8M parquet); (c) out-of-coverage trade -> NA / unscratched.
    assert len(_load_log) >= 2 * len(md_unique_days), \
        "expected per-day loads from the two tick filters (day-batched)"
    assert _peak["max_rows"] < 5_000_000, \
        "a single per-day load must be ~one trading day, not the whole parquet"
    assert pd.isna(md_trades["absorption_confirms"].iloc[-1]), \
        "multi-day out-of-coverage trade must be NA for absorption"
    assert not bool(md_trades["trail_scratch_exit"].iloc[-1]), \
        "multi-day out-of-coverage trade must not be scratched"
    assert md_trades["trail_adj_pnl_dollars"].iloc[-1] == md_trades["pnl_dollars"].iloc[-1], \
        "multi-day out-of-coverage trade must keep original pnl"
    print(f"[multi-day] total per-day load calls: {len(_load_log)}; "
          f"PEAK rows resident in any single load: {_peak['max_rows']:,} "
          f"(<< 43.8M whole-parquet)")
    print(f"[multi-day] absorption NA={int(md_trades['absorption_confirms'].isna().sum())}, "
          f"scratch exits={int(md_trades['trail_scratch_exit'].sum())}, "
          f"out-of-coverage tail is NA/unscratched -> OK")
    print("[multi-day] lift table over multi-day set:")
    print(md_lift.to_string(index=False))
    print("-" * 64)
    print()

    # --- 5. Equivalence check: day-batch == single-window injection -------
    # Run the absorption filter both ways over the SAME single-day trade set:
    # (1) injected single-window ticks (old path), (2) ticks=None day-batch
    # (new path). The per-trade verdicts MUST be identical -- proof that HOW
    # ticks load does not change the filter math.
    eq_inject = apply_absorption_filter(trades, lookback_seconds=30, ticks=ticks)
    eq_batch = apply_absorption_filter(trades, lookback_seconds=30)  # ticks=None
    a = eq_inject["absorption_confirms"]
    b = eq_batch["absorption_confirms"]
    same = ((a == b) | (a.isna() & b.isna())).all()
    assert same, "day-batch absorption verdicts must equal single-window injection"
    print(f"[equivalence] absorption verdicts identical injected-vs-day-batch "
          f"over {len(trades)} trades -> OK")

    # --- Lift table --------------------------------------------------------
    lift = microstructure_lift_table(trades, write_csv=True)
    assert list(lift.columns) == ["filter", "subset", "n", "net_pnl",
                                  "win_rate", "profit_factor"], "lift columns"
    print()
    print("microstructure_lift_table:")
    print(lift.to_string(index=False))
    print()
    print(f"[lift] wrote -> {paths.OUT_DIR / 'microstructure_lift.csv'}")
    print()
    print("OK - all asserts passed")


if __name__ == "__main__":
    _selftest()
