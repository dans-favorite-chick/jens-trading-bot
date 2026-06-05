"""Regime stability pre-flight gate for the Phoenix Strategy Oracle.

Task 3 of the Phoenix Strategy Oracle build.

WHAT THIS IS
------------
Before the orchestrator spends tokens on a `weekly` or `research` analysis,
this gate checks whether the trading regime is stable enough to draw
conclusions from. A regime-shift week is the WORST possible time to tune
parameters -- the new data is mid-transition and any fit will overfit to a
non-stationary distribution. Better to halt and wait for the dust to settle.

The check is small: a z-score of the latest month's portfolio-wide sharpe
proxy against the trailing 6-month baseline (latest month excluded). If
`|z| > 1.5`, we declare the regime unstable.

MODE-AWARE
----------
- `daily` mode SHORT-CIRCUITS with `stable=True, mode_skipped=True`. One
  day of returns is too noisy for the z-test and would generate false-
  halt alarms. This is explicit in the spec (sec 1.1).
- `weekly` and `research` run the full check.

INSUFFICIENT DATA
-----------------
If fewer than 4 baseline months are available, the gate returns
`stable=True` with an explanatory warning. The spec is explicit that the
regime gate should NOT halt analysis on missing data -- that's a separate
warehouse-coverage concern handled in pre-flight. The point of the gate
is to halt on DETECTED regime shifts, not on the absence of evidence.

ALLOWED IMPORTS
---------------
- numpy, pandas (transitively via prepared_queries)
- analytics.prepared_queries
- Standard library

FORBIDDEN IMPORTS (CI invariant)
--------------------------------
- bots/, core/, bridge/, data_feeds/  (pure-math layer)
- anthropic                            (no LLM here)
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from typing import Literal

import duckdb
import numpy as np
import pandas as pd

from analytics import prepared_queries

logger = logging.getLogger(__name__)

# |z| above this threshold => regime declared unstable; analysis halts.
# 1.5 sigma corresponds to ~13% two-tailed rejection -- strict enough to
# catch genuine shifts, loose enough to ride out normal monthly variance.
Z_THRESHOLD_DEFAULT = 1.5

# Minimum number of usable baseline months required to compute z. Below
# this we report insufficient-data rather than running a degenerate test.
_MIN_BASELINE_MONTHS = 4

# We pull 13 months from monthly_sharpe_proxy. Typical result: 12 baseline +
# 1 latest. Early in a calendar month the rolling 13-month SQL window can
# produce up to 14 distinct months (now() - 13 mo cutoff straddles an extra
# month boundary), so baseline_n_months may occasionally be 13 instead of 12.
# This is statistically fine -- more baseline data only improves z reliability.
#
# 2026-06-05 (R2 Finding 3 sprint): bumped from 7 to 13 in response to the
# Phase 4.7 red-team CRITICAL finding on the filtered-baseline diagnostic:
# at _PULL_MONTHS=7 against the filter's default floor=6, any sparse-month
# drop forced INSUFFICIENT_BASELINE rather than drop-and-recompute. The
# bump gives the filtered AND the detrended variant statistical headroom.
# See FINDING-2026-06-05-ORACLE-FILTERED-GATE-FLOOR-METHODOLOGY.
_PULL_MONTHS = 13

Mode = Literal["research", "weekly", "daily"]

# Defaults for the R2-diagnostic filtered variant (2026-06-05 sprint).
# `sparse_factor` defines the trade-count cutoff as a fraction of the
# baseline-median trade_count: a month is "sparse" iff its trade_count is
# strictly less than `sparse_factor * median(baseline_trade_counts)`.
# `min_baseline_n_after_filter` is the floor below which the gate must
# refuse to compute z (and return stable=True with an explanatory warning
# so analysis is NOT halted -- the gate still never halts on missing data).
_SPARSE_FACTOR_DEFAULT = 0.5
_MIN_BASELINE_N_AFTER_FILTER_DEFAULT = 6

__all__ = [
    "check_regime_stability",
    "check_regime_stability_with_filter",
    "check_regime_stability_detrended",
    "check_regime_stability_detrended_weighted",
    "Z_THRESHOLD_DEFAULT",
]


def _latest_month_str(
    ts: "pd.Timestamp | _dt.date | _dt.datetime | None",
) -> str | None:
    """Format a month-bucket timestamp/date as 'YYYY-MM'. Tolerant of
    pandas Timestamp, datetime.date, datetime.datetime, or NaT."""
    if ts is None:
        return None
    try:
        return pd.Timestamp(ts).strftime("%Y-%m")
    except (ValueError, TypeError):
        return None


def check_regime_stability(
    conn: duckdb.DuckDBPyConnection,
    mode: Mode,
    z_threshold: float = Z_THRESHOLD_DEFAULT,
) -> dict:
    """Pre-flight regime stability check for the Phoenix Strategy Oracle.

    Daily mode short-circuits with ``{stable: True, mode_skipped: True}``
    without touching the warehouse -- one day of returns is too noisy for
    the z-test and would produce false-halt alarms.

    For research and weekly modes:

    1. Pull last ~12 months of portfolio-wide monthly sharpe-proxy via
       ``prepared_queries.monthly_sharpe_proxy(conn, months_back=13)``.
    2. Drop the most recent month's row to form the baseline (the
       "trailing 12 months excluding latest" baseline). Note: the
       window was 6 prior to the 2026-06-05 R2 Finding 3 sprint; see
       _PULL_MONTHS at module top for the rationale.
    3. Compute baseline_mean and baseline_std of the sharpe_proxy column.
    4. Compute ``z = (latest_sharpe_proxy - baseline_mean) / baseline_std``.
    5. If ``|z| > z_threshold`` -> stable=False with a structured warning.

    Returns:
        dict with keys::

            {
                "stable": bool,
                "z_score": float,          # NaN when baseline too thin to compute
                "warning": str | None,     # explanation when not stable
                "mode_skipped": bool,      # True only for daily
                "baseline_n_months": int,
                "latest_month": str | None,  # YYYY-MM of the row tested
                "latest_sharpe_proxy": float | None,
            }

    Insufficient-data fallback (research/weekly): if fewer than 4 baseline
    months are available, returns stable=True with warning text explaining
    the gate could not run. Per spec, the regime gate does NOT halt
    analysis on missing data -- that's a separate pre-flight concern.

    Zero-baseline-std fallback: if the baseline is degenerate (all months
    have identical sharpe_proxy), the z calculation would divide by zero.
    We treat this as insufficient information and return stable=True with
    a warning rather than reporting an infinite z.

    Never raises on data shape problems -- only on programming errors
    (e.g. mode is not a string).
    """
    # Mode-aware short-circuit. Daily MUST NOT touch the warehouse -- the
    # spec explicitly skips the gate on daily runs because a one-day
    # window has no statistical power.
    if mode == "daily":
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": None,
            "mode_skipped": True,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
        }

    # Pull portfolio-wide monthly sharpe-proxy from the warehouse.
    try:
        df = prepared_queries.monthly_sharpe_proxy(conn, months_back=_PULL_MONTHS)
    except Exception as e:  # pragma: no cover -- surfaced for operator visibility
        logger.warning(
            "regime_gate: monthly_sharpe_proxy query failed (%s); "
            "treating as insufficient data and returning stable=True.",
            e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate could not run: warehouse query failed "
                f"({type(e).__name__}). Analysis not halted on missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
        }

    # Drop rows where sharpe_proxy is NaN/NULL -- they're useless for the
    # z-test (months with zero stddev or only one trade).
    if df is None or df.empty:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate could not run: no friction-applied trades found "
                "in the trailing 12 months. Analysis not halted on missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
        }

    # Schema-drift guard: monthly_sharpe_proxy must expose the column
    # names this gate consumes. If T1 renames `sharpe_proxy` to `sharpe`
    # (or drops `month`), we must NOT raise an unhandled KeyError that
    # crashes the orchestrator -- the contract is "gate never raises on
    # data-shape problems". Return the standard stable=True dict with a
    # warning explaining the schema drift so the operator can fix it.
    required_columns = {"month", "sharpe_proxy"}
    missing = required_columns - set(df.columns)
    if missing:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate could not run: prepared_queries.monthly_sharpe_proxy "
                f"is missing required columns {sorted(missing)}. Schema may have drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
        }

    # Belt-and-suspenders: even with the schema check above, any unexpected
    # shape problem during post-query processing (dtype surprises, index
    # weirdness, etc.) must NOT crash the orchestrator. Return the same
    # "gate could not run" shape so callers can keep going.
    try:
        # The prepared query returns rows ordered by month ASC. We need the
        # most recent row as "latest" and the rest as baseline.
        df = df.sort_values("month").reset_index(drop=True)

        # Filter out months where sharpe_proxy is NULL/NaN for baseline use,
        # but keep them in `df` so we can still identify the actual most-
        # recent calendar month for reporting.
        latest_row = df.iloc[-1]
        latest_month = _latest_month_str(latest_row["month"])
        latest_sharpe = latest_row["sharpe_proxy"]
        if pd.notna(latest_sharpe):
            latest_sharpe = float(latest_sharpe)
        else:
            latest_sharpe = None

        # Baseline = everything except the most recent row, with NaN sharpe
        # values dropped.
        baseline_df = df.iloc[:-1].copy()
        baseline_df = baseline_df[baseline_df["sharpe_proxy"].notna()]
        baseline_n = int(len(baseline_df))
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        logger.warning(
            "regime_gate: unexpected DataFrame shape during processing (%s: %s); "
            "treating as schema drift and returning stable=True.",
            type(e).__name__, e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate could not run: unexpected DataFrame shape from "
                f"prepared_queries.monthly_sharpe_proxy ({type(e).__name__}). "
                "Schema may have drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
        }

    # Insufficient baseline: don't halt, just warn.
    if baseline_n < _MIN_BASELINE_MONTHS:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                f"Regime gate could not run: only {baseline_n} baseline "
                f"month(s) of usable sharpe-proxy data available "
                f"(need >= {_MIN_BASELINE_MONTHS}). Insufficient data; "
                "analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
        }

    # If the latest month itself has no sharpe_proxy (single trade or
    # zero-std), we can't compute z -- same fallback. The prior block
    # already converts NaN to None via pd.notna() filtering, so a `None`
    # check is sufficient -- math.isnan(None) would TypeError.
    if latest_sharpe is None:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate could not run: latest month has no usable "
                "sharpe-proxy value (single trade or zero variance). "
                "Insufficient data; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": None,
        }

    baseline_mean = float(baseline_df["sharpe_proxy"].mean())
    # Sample standard deviation (ddof=1) to match the convention used by
    # the warehouse's STDDEV_SAMP -- though here we're computing the std
    # of monthly sharpe values, not of trade pnls.
    baseline_std = float(baseline_df["sharpe_proxy"].std(ddof=1))

    # Zero-baseline-std degenerate case: every baseline month has the
    # same sharpe_proxy. Z would be +/-inf or NaN. Per spec we handle
    # gracefully: report insufficient information rather than halt or
    # raise. Return stable=True with a warning.
    #
    # Use an epsilon comparison instead of exact-zero: floating-point std
    # of "identical" values coming back from SQL can land on ~1e-16 rather
    # than 0.0 due to accumulation order in the variance calculation.
    if baseline_std < 1e-10 or math.isnan(baseline_std):
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate could not run: baseline standard deviation is "
                "zero (all baseline months have identical sharpe-proxy). "
                "Z-score undefined; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
        }

    z = (latest_sharpe - baseline_mean) / baseline_std

    if abs(z) > z_threshold:
        warning = (
            f"Regime instability detected: latest-month sharpe-proxy z-score "
            f"= {z:+.2f} (threshold +/- {z_threshold:.2f}). Baseline mean "
            f"{baseline_mean:.3f} over {baseline_n} months; latest "
            f"{latest_sharpe:.3f} ({latest_month}). Analysis halted."
        )
        logger.warning("regime_gate: %s", warning)
        return {
            "stable": False,
            "z_score": float(z),
            "warning": warning,
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
        }

    # Stable verdict.
    return {
        "stable": True,
        "z_score": float(z),
        "warning": None,
        "mode_skipped": False,
        "baseline_n_months": baseline_n,
        "latest_month": latest_month,
        "latest_sharpe_proxy": latest_sharpe,
    }


# ---------------------------------------------------------------------------
# R2-diagnostic filtered variant (2026-06-05 sprint)
# ---------------------------------------------------------------------------


def check_regime_stability_with_filter(
    conn: duckdb.DuckDBPyConnection,
    mode: Mode,
    *,
    z_threshold: float = Z_THRESHOLD_DEFAULT,
    sparse_factor: float = _SPARSE_FACTOR_DEFAULT,
    min_baseline_n_after_filter: int = _MIN_BASELINE_N_AFTER_FILTER_DEFAULT,
) -> dict:
    """R2-diagnostic variant of :func:`check_regime_stability`.

    Mirrors the default gate's contract but applies a two-stage filter to
    the baseline BEFORE computing the z-score:

    1. **Sparse-month drop.** Any baseline month whose ``trade_count`` is
       strictly less than ``sparse_factor * median(baseline_trade_counts)``
       is removed from the baseline. The median is taken over the baseline
       only (latest month excluded). With ``sparse_factor=0.0`` the filter
       is a no-op (no month can have trade_count < 0).

    2. **Minimum baseline floor.** After the sparse drop, if fewer than
       ``min_baseline_n_after_filter`` baseline months remain, the gate
       returns ``stable=True`` with ``insufficient_baseline_after_filter=
       True``. Per the spec the gate NEVER halts analysis on missing data;
       the operator must read the warning and decide.

    Origin (2026-06-04 R2 Bug Hunter, finding 2026-06-04 ORACLE-REGIME-GATE
    -METHODOLOGY): the 2026-06-04 default-gate HALT (z=+3.69) may itself
    be a measurement artifact of (a) late-arriving trades distorting
    "historical" sharpe-proxies and (b) baseline contamination from
    gradual regime drift. The diagnostic verdict rule the operator
    pre-committed to:

    - ``|z| > 3.0`` after filtering -> regime shift is real -> HALT stands.
    - ``|z| < 2.0`` after filtering -> HALT was artifact -> freeze-lift can
      advance (PHANTOM-NT8 + reconciliation still gate; this gate alone
      does not authorize a parameter flip).
    - ``2.0 <= |z| <= 3.0`` -> ambiguous; recommend waiting for next
      monthly Oracle re-run.
    - ``insufficient_baseline_after_filter=True`` -> INSUFFICIENT_BASELINE;
      flag for operator review.

    Returns a dict with the same shape as :func:`check_regime_stability`,
    plus these diagnostic keys:

    - ``filter_applied`` (bool): True iff the filter ran (False on daily
      mode or when the warehouse query path early-returns before the
      filter would execute).
    - ``pre_filter_baseline_n`` (int): baseline_n BEFORE the sparse-month
      drop. Equals ``baseline_n_months`` when no months were dropped.
    - ``dropped_months`` (list[str]): YYYY-MM strings of every baseline
      month removed by the sparse filter, oldest-first.
    - ``baseline_median_trade_count`` (float | None): the median used to
      compute the cutoff. None when filter did not run.
    - ``insufficient_baseline_after_filter`` (bool): True iff
      baseline_n_months < min_baseline_n_after_filter after filtering.
    - ``baseline_filter_config`` (dict): echo of the config that produced
      this verdict, for the audit log.
    - ``nan_sharpe_drops_after_filter`` (int): months removed by the NaN-
      sharpe filter AFTER the sparse-month filter. Together with
      ``len(dropped_months)`` this fully accounts for
      ``pre_filter_baseline_n - baseline_n_months``.

    Raises ``ValueError`` on negative ``sparse_factor`` or
    ``min_baseline_n_after_filter`` — those are clearly-wrong inputs the
    operator should fix at call site rather than have silently produce a
    no-op filter (Bug Hunter R2 CRITICAL #1, 2026-06-05).
    """
    # --- input validation (2026-06-05 Bug Hunter R2 CRITICAL #1) ---
    if sparse_factor < 0:
        raise ValueError(
            f"sparse_factor must be >= 0 (got {sparse_factor!r}); "
            "negative values would silently produce a no-op filter "
            "that audit-logs as if it ran."
        )
    if min_baseline_n_after_filter < 0:
        raise ValueError(
            f"min_baseline_n_after_filter must be >= 0 "
            f"(got {min_baseline_n_after_filter!r}); negative values "
            "silently disable the floor check."
        )

    # --- daily mode: skip entirely, mirror the default function's stub ---
    if mode == "daily":
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": None,
            "mode_skipped": True,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            "filter_applied": False,
            "pre_filter_baseline_n": 0,
            "dropped_months": [],
            "baseline_median_trade_count": None,
            "insufficient_baseline_after_filter": False,
            "baseline_filter_config": {
                "sparse_factor": sparse_factor,
                "min_baseline_n_after_filter": min_baseline_n_after_filter,
            },
            "nan_sharpe_drops_after_filter": 0,
        }

    config_echo = {
        "sparse_factor": float(sparse_factor),
        "min_baseline_n_after_filter": int(min_baseline_n_after_filter),
    }

    def _filter_diagnostics_stub(extra: dict | None = None) -> dict:
        """Shape the early-return shapes need when the filter never ran."""
        out = {
            "filter_applied": False,
            "pre_filter_baseline_n": 0,
            "dropped_months": [],
            "baseline_median_trade_count": None,
            "insufficient_baseline_after_filter": False,
            "baseline_filter_config": config_echo,
            "nan_sharpe_drops_after_filter": 0,
        }
        if extra:
            out.update(extra)
        return out

    # --- query (same as default function) ---
    try:
        df = prepared_queries.monthly_sharpe_proxy(conn, months_back=_PULL_MONTHS)
    except Exception as e:  # pragma: no cover -- surfaced for operator visibility
        logger.warning(
            "regime_gate(filter): monthly_sharpe_proxy query failed (%s); "
            "treating as insufficient data and returning stable=True.",
            e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (filtered) could not run: warehouse query "
                f"failed ({type(e).__name__}). Analysis not halted on "
                "missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_filter_diagnostics_stub(),
        }

    if df is None or df.empty:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (filtered) could not run: no friction-applied "
                "trades found in the trailing 6 months. Analysis not halted "
                "on missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_filter_diagnostics_stub(),
        }

    # Schema-drift guard. The filtered variant needs trade_count too.
    # (R3 Test Quality "missing schema-drift test for trade_count" — locked
    # in by test_filter_returns_schema_drift_when_trade_count_missing.)
    required_columns = {"month", "sharpe_proxy", "trade_count"}
    missing = required_columns - set(df.columns)
    if missing:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (filtered) could not run: prepared_queries"
                ".monthly_sharpe_proxy is missing required columns "
                f"{sorted(missing)}. Schema may have drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_filter_diagnostics_stub(),
        }

    try:
        df = df.sort_values("month").reset_index(drop=True)
        latest_row = df.iloc[-1]
        latest_month = _latest_month_str(latest_row["month"])
        latest_sharpe = latest_row["sharpe_proxy"]
        if pd.notna(latest_sharpe):
            latest_sharpe = float(latest_sharpe)
        else:
            latest_sharpe = None

        baseline_df = df.iloc[:-1].copy()
        # IMPORTANT: apply the trade-count filter FIRST, before the NaN-
        # sharpe drop. R2's diagnostic is specifically about removing
        # low-trade-count months from the baseline -- some of those months
        # already had a usable sharpe (e.g. 3 trades with non-zero std).
        # Filtering AFTER the NaN drop would silently miss them.
        pre_filter_baseline_n = int(len(baseline_df))
        dropped_months: list[str] = []
        baseline_median_trade_count: float | None = None
        if not baseline_df.empty:
            counts = baseline_df["trade_count"].astype(float)
            # Median over the baseline trade-count distribution. With an
            # empty baseline_df we'd never reach here.
            median_count = float(counts.median())
            baseline_median_trade_count = median_count
            cutoff = sparse_factor * median_count
            keep_mask = counts >= cutoff
            dropped = baseline_df.loc[~keep_mask]
            for _, row in dropped.iterrows():
                m_str = _latest_month_str(row["month"])
                if m_str:
                    dropped_months.append(m_str)
            baseline_df = baseline_df.loc[keep_mask].copy()

        # Then drop NaN-sharpe months (same as default function).
        # Track the count separately so the operator-facing warning
        # arithmetic reconciles
        # (Bug Hunter R2 CRITICAL #2 / HIGH #4, 2026-06-05):
        #     pre_filter_baseline_n
        #       - len(dropped_months)            # sparse drops
        #       - nan_sharpe_drops_after_filter  # NaN-sharpe drops
        #       == baseline_n
        n_after_sparse_filter = int(len(baseline_df))
        baseline_df = baseline_df[baseline_df["sharpe_proxy"].notna()]
        baseline_n = int(len(baseline_df))
        nan_sharpe_drops_after_filter = n_after_sparse_filter - baseline_n
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        logger.warning(
            "regime_gate(filter): unexpected DataFrame shape during "
            "processing (%s: %s); treating as schema drift and returning "
            "stable=True.",
            type(e).__name__, e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (filtered) could not run: unexpected DataFrame "
                f"shape from prepared_queries.monthly_sharpe_proxy "
                f"({type(e).__name__}). Schema may have drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_filter_diagnostics_stub(),
        }

    filter_diagnostics = {
        "filter_applied": True,
        "pre_filter_baseline_n": pre_filter_baseline_n,
        "dropped_months": dropped_months,
        "baseline_median_trade_count": baseline_median_trade_count,
        "insufficient_baseline_after_filter": False,
        "baseline_filter_config": config_echo,
        "nan_sharpe_drops_after_filter": nan_sharpe_drops_after_filter,
    }

    # Floor check -- the R2 diagnostic's signature requirement. Distinct
    # from the existing _MIN_BASELINE_MONTHS=4 floor; this floor is
    # configurable per call and defaults to 6.
    if baseline_n < min_baseline_n_after_filter:
        filter_diagnostics["insufficient_baseline_after_filter"] = True
        # Render the drop breakdown accurately. Bug Hunter R2 HIGH #4:
        # the previous text always said "after the sparse-month filter"
        # which misled the operator when the filter dropped nothing but
        # the SQL window simply returned too few baseline months.
        n_sparse = len(dropped_months)
        n_nan = nan_sharpe_drops_after_filter
        if n_sparse and n_nan:
            breakdown = f" (dropped {n_sparse} sparse + {n_nan} NaN-sharpe)"
        elif n_sparse:
            breakdown = f" (dropped {n_sparse} sparse)"
        elif n_nan:
            breakdown = f" (dropped {n_nan} NaN-sharpe; sparse filter dropped 0)"
        else:
            breakdown = " (sparse filter dropped 0; SQL window returned too few baseline months)"
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                f"Regime gate (filtered) could not run: only {baseline_n} "
                f"baseline month(s) remain (need >= "
                f"{min_baseline_n_after_filter}){breakdown}. Insufficient "
                "baseline; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            **filter_diagnostics,
        }

    # Also keep the original safety net: even below the configurable floor,
    # the legacy 4-month minimum still applies (defense-in-depth).
    if baseline_n < _MIN_BASELINE_MONTHS:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                f"Regime gate (filtered) could not run: only {baseline_n} "
                f"baseline month(s) of usable sharpe-proxy data available "
                f"(need >= {_MIN_BASELINE_MONTHS}). Insufficient data; "
                "analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            **filter_diagnostics,
        }

    if latest_sharpe is None:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (filtered) could not run: latest month has no "
                "usable sharpe-proxy value (single trade or zero variance). "
                "Insufficient data; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": None,
            **filter_diagnostics,
        }

    baseline_mean = float(baseline_df["sharpe_proxy"].mean())
    baseline_std = float(baseline_df["sharpe_proxy"].std(ddof=1))

    if baseline_std < 1e-10 or math.isnan(baseline_std):
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (filtered) could not run: baseline standard "
                "deviation is zero (all filtered baseline months have "
                "identical sharpe-proxy). Z-score undefined; analysis not "
                "halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            **filter_diagnostics,
        }

    z = (latest_sharpe - baseline_mean) / baseline_std

    if abs(z) > z_threshold:
        # Reconcile the drop breakdown so operator audit log balances:
        # pre = sparse_drops + nan_drops + baseline_n
        # (Bug Hunter R2 CRITICAL #2 fix, 2026-06-05.)
        n_sparse = len(dropped_months)
        n_nan = nan_sharpe_drops_after_filter
        if n_sparse and n_nan:
            drop_str = f"dropped {n_sparse} sparse + {n_nan} NaN-sharpe"
        elif n_sparse:
            drop_str = f"dropped {n_sparse} sparse"
        elif n_nan:
            drop_str = f"dropped {n_nan} NaN-sharpe (0 sparse)"
        else:
            drop_str = "dropped 0"
        warning = (
            f"Regime instability detected (filtered baseline): latest-month "
            f"sharpe-proxy z-score = {z:+.2f} (threshold +/- "
            f"{z_threshold:.2f}). Filtered baseline mean {baseline_mean:.3f} "
            f"over {baseline_n} months ({drop_str} from "
            f"{pre_filter_baseline_n}); latest {latest_sharpe:.3f} "
            f"({latest_month}). Analysis halted."
        )
        logger.warning("regime_gate(filter): %s", warning)
        return {
            "stable": False,
            "z_score": float(z),
            "warning": warning,
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            **filter_diagnostics,
        }

    return {
        "stable": True,
        "z_score": float(z),
        "warning": None,
        "mode_skipped": False,
        "baseline_n_months": baseline_n,
        "latest_month": latest_month,
        "latest_sharpe_proxy": latest_sharpe,
        **filter_diagnostics,
    }


# ---------------------------------------------------------------------------
# Detrended-baseline variant — R2 Finding 3 sprint, 2026-06-05
# ---------------------------------------------------------------------------


def check_regime_stability_detrended(
    conn: duckdb.DuckDBPyConnection,
    mode: Mode,
    *,
    z_threshold: float = Z_THRESHOLD_DEFAULT,
) -> dict:
    """R2-Finding-3 variant of :func:`check_regime_stability`.

    Tests whether the elevated z under the stock gate is driven by a
    GRADUAL DRIFT in the baseline rather than a regime shift at the
    latest month. The diagnostic fits a linear trend to the baseline
    monthly sharpe-proxies, computes residuals, and reports z on the
    latest month's residual against the baseline residual std.

    Mathematical formulation:
        Let baseline months be x = [0, 1, ..., n-1] (chronological index).
        Fit y = slope * x + intercept by ordinary least squares.
        Residuals r_i = y_i - (slope * x_i + intercept).
        Predicted latest = slope * n + intercept.
        Latest residual = latest_sharpe - predicted_latest.
        Residual std = std(r, ddof=2)  -- two fit parameters.
        z_detrended = latest_residual / residual_std.

    Returns the same key set as :func:`check_regime_stability` plus:

    - ``detrended_applied`` (bool): True iff the regression ran.
    - ``baseline_slope`` (float | None): slope of the linear fit.
    - ``baseline_intercept`` (float | None): intercept of the linear fit.
    - ``baseline_r_squared`` (float | None): R^2 of the fit, in [0, 1].
    - ``residuals`` (list[float]): per-month residuals in chronological order.
    - ``latest_residual`` (float | None): residual of the latest month.
    - ``z_score_detrended`` (float): z of the latest residual; NaN when
      not computable.

    Pre-decision rule (per 2026-06-05 sprint spec — operator applies):

    - ``|z_detrended| > 3.0`` -> REGIME SHIFT IS REAL (HALT stands;
      drift is NOT the cause).
    - ``|z_detrended| < 2.0`` AND ``baseline_r_squared > 0.5`` ->
      HALT WAS DRIFT ARTIFACT (freeze-lift can advance pending PHANTOM-NT8).
    - ``|z_detrended| < 2.0`` AND ``baseline_r_squared < 0.5`` ->
      AMBIGUOUS (noisy baseline, no clear trend; wait).
    - ``2.0 <= |z_detrended| <= 3.0`` -> MARGINAL (wait + monitor).
    - ``baseline_n_months < 4`` -> INSUFFICIENT_BASELINE.

    Same per-shape safety contract as the stock gate: NEVER halts on
    missing data; only on a detected regime shift.
    """
    # --- daily mode short-circuit ---
    if mode == "daily":
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": None,
            "mode_skipped": True,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            "detrended_applied": False,
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
            "z_score_detrended": float("nan"),
        }

    def _detrended_diag_stub() -> dict:
        return {
            "detrended_applied": False,
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
            "z_score_detrended": float("nan"),
        }

    # --- query (mirror stock function) ---
    try:
        df = prepared_queries.monthly_sharpe_proxy(conn, months_back=_PULL_MONTHS)
    except Exception as e:  # pragma: no cover -- operator visibility
        logger.warning(
            "regime_gate(detrended): monthly_sharpe_proxy query failed "
            "(%s); treating as insufficient data, returning stable=True.",
            e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended) could not run: warehouse query "
                f"failed ({type(e).__name__}). Analysis not halted on "
                "missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_detrended_diag_stub(),
        }

    if df is None or df.empty:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended) could not run: no friction-applied "
                "trades found in the trailing window. Analysis not halted "
                "on missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_detrended_diag_stub(),
        }

    required_columns = {"month", "sharpe_proxy"}
    missing = required_columns - set(df.columns)
    if missing:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended) could not run: prepared_queries"
                ".monthly_sharpe_proxy is missing required columns "
                f"{sorted(missing)}. Schema may have drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_detrended_diag_stub(),
        }

    try:
        df = df.sort_values("month").reset_index(drop=True)
        latest_row = df.iloc[-1]
        latest_month = _latest_month_str(latest_row["month"])
        latest_sharpe = latest_row["sharpe_proxy"]
        if pd.notna(latest_sharpe):
            latest_sharpe = float(latest_sharpe)
        else:
            latest_sharpe = None

        baseline_df = df.iloc[:-1].copy()
        baseline_df = baseline_df[baseline_df["sharpe_proxy"].notna()]
        baseline_n = int(len(baseline_df))
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        logger.warning(
            "regime_gate(detrended): unexpected DataFrame shape (%s: %s); "
            "treating as schema drift and returning stable=True.",
            type(e).__name__, e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended) could not run: unexpected "
                f"DataFrame shape ({type(e).__name__}). Schema may have "
                "drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_detrended_diag_stub(),
        }

    detrended_meta = {
        "detrended_applied": True,
    }

    # --- legacy minimum baseline (defensive; same as stock function) ---
    if baseline_n < _MIN_BASELINE_MONTHS:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                f"Regime gate (detrended) could not run: only {baseline_n} "
                f"baseline month(s) of usable sharpe-proxy data available "
                f"(need >= {_MIN_BASELINE_MONTHS}). Insufficient data; "
                "analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
            "z_score_detrended": float("nan"),
            **detrended_meta,
        }

    if latest_sharpe is None:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended) could not run: latest month has "
                "no usable sharpe-proxy value (single trade or zero "
                "variance). Insufficient data; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": None,
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
            "z_score_detrended": float("nan"),
            **detrended_meta,
        }

    # --- linear regression on baseline ---
    try:
        y = baseline_df["sharpe_proxy"].astype(float).to_numpy()
        x = np.arange(len(y), dtype=float)
        # Use lstsq via polyfit; fall back to NaN on degenerate matrix.
        coefs = np.polyfit(x, y, deg=1)
        slope = float(coefs[0])
        intercept = float(coefs[1])
    except (np.linalg.LinAlgError, ValueError, TypeError) as e:
        logger.warning(
            "regime_gate(detrended): polyfit failed (%s: %s); returning "
            "stable=True with NaN z.",
            type(e).__name__, e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended) could not run: linear-fit "
                f"failed ({type(e).__name__}). Degenerate baseline; "
                "analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
            "z_score_detrended": float("nan"),
            **detrended_meta,
        }

    predicted = slope * x + intercept
    residuals = (y - predicted).tolist()
    predicted_latest = slope * float(len(y)) + intercept
    latest_residual = float(latest_sharpe - predicted_latest)

    # Residual std with ddof=2 (intercept + slope consume 2 df).
    if len(residuals) > 2:
        residual_std = float(np.std(np.asarray(residuals), ddof=2))
    else:
        residual_std = float("nan")

    # R-squared.
    y_mean = float(np.mean(y))
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    if ss_tot > 1e-12:
        r_squared = 1.0 - (ss_res / ss_tot)
        r_squared = max(0.0, min(1.0, r_squared))  # clamp
    else:
        # All baseline sharpes identical -> trend is undefined (any line
        # works); report r_squared=0 by convention (no explained variance).
        r_squared = 0.0

    diagnostics = {
        "detrended_applied": True,
        "baseline_slope": slope,
        "baseline_intercept": intercept,
        "baseline_r_squared": r_squared,
        "residuals": [float(r) for r in residuals],
        "latest_residual": latest_residual,
    }

    # Degenerate residual-std: e.g. perfect collinearity in baseline.
    if (
        not math.isfinite(residual_std)
        or residual_std < 1e-10
    ):
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended) could not run: residual standard "
                "deviation is zero (baseline is a perfect linear fit or "
                "degenerate). Z-score undefined; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "z_score_detrended": float("nan"),
            **diagnostics,
        }

    z_detrended = float(latest_residual / residual_std)

    if abs(z_detrended) > z_threshold:
        warning = (
            f"Regime instability detected (detrended): latest-month "
            f"residual z-score = {z_detrended:+.2f} (threshold +/- "
            f"{z_threshold:.2f}). Baseline trend slope={slope:+.5f} "
            f"per month, intercept={intercept:+.5f}, r^2={r_squared:.3f}. "
            f"Latest sharpe {latest_sharpe:.3f} ({latest_month}) vs "
            f"projection {predicted_latest:.3f} -> residual "
            f"{latest_residual:+.3f}. Analysis halted."
        )
        logger.warning("regime_gate(detrended): %s", warning)
        return {
            "stable": False,
            # Legacy key: report the detrended z so existing callers that
            # read z_score still get the operative number.
            "z_score": z_detrended,
            "warning": warning,
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "z_score_detrended": z_detrended,
            **diagnostics,
        }

    return {
        "stable": True,
        "z_score": z_detrended,
        "warning": None,
        "mode_skipped": False,
        "baseline_n_months": baseline_n,
        "latest_month": latest_month,
        "latest_sharpe_proxy": latest_sharpe,
        "z_score_detrended": z_detrended,
        **diagnostics,
    }


# ---------------------------------------------------------------------------
# Sample-size-weighted detrended variant — 2026-06-05 sprint discharges
# FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING.
# ---------------------------------------------------------------------------

_MIN_LATEST_TRADE_COUNT_FRACTION_DEFAULT = 0.7


def check_regime_stability_detrended_weighted(
    conn: duckdb.DuckDBPyConnection,
    mode: Mode,
    *,
    z_threshold: float = Z_THRESHOLD_DEFAULT,
    min_latest_trade_count_fraction: float = _MIN_LATEST_TRADE_COUNT_FRACTION_DEFAULT,
) -> dict:
    """Sample-size-weighted version of :func:`check_regime_stability_detrended`.

    Discharges ``FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING``
    surfaced by the Phase 5.7 red-team CRITICAL on the unweighted variant:
    the unweighted detrended gate treats every month as equal-variance, but
    a latest month with FEWER trades than the baseline median has *higher
    sampling variance* in its sharpe-proxy. Inflating the latest standard
    error by a Welch-style factor flips the May 2026 verdict from
    REGIME_REAL (z=3.35 unweighted) into a MARGINAL band -- the operator
    should NOT advance the freeze-lift conversation on an equal-variance
    artifact.

    Two protections vs the unweighted variant:

    1. **Refuse-to-compute floor.** When ``latest_trade_count <
       min_latest_trade_count_fraction * baseline_median_trade_count``
       (default 0.7), the gate returns ``INSUFFICIENT_SAMPLE`` without
       computing z. The 0.7 default matches the spirit of the filtered
       variant's sparse-month rule applied to the LATEST month: below
       70 % of typical baseline volume, the corrected std is large enough
       that any z is hard to interpret -- better to refuse and let the
       operator wait for the latest month to accumulate more trades.

    2. **Welch-style sampling correction.** If the floor passes, compute
       ``corrected_std = residual_std * sqrt(baseline_median /
       latest_trade_count)``. Latest standard error inflates when latest
       has fewer trades, deflates when latest has more. ``z_weighted =
       latest_residual / corrected_std``.

    Returns the same key set as :func:`check_regime_stability_detrended`
    plus:

    - ``weighted_applied`` (bool): True iff the weighted path ran.
    - ``insufficient_sample`` (bool): True iff the refuse-to-compute
      floor triggered.
    - ``category`` (str): one of INSUFFICIENT_BASELINE,
      INSUFFICIENT_SAMPLE, REGIME_REAL, MARGINAL, DRIFT_ARTIFACT,
      AMBIGUOUS. Maps the operator's pre-decision rule mechanically.
    - ``latest_trade_count`` (int | None).
    - ``baseline_median_trade_count`` (float | None).
    - ``weight_factor`` (float | None): the sqrt-ratio applied to
      residual std; 1.0 when balanced.
    - ``corrected_residual_std`` (float | None).
    - ``z_score_detrended_weighted`` (float): the operative z; NaN when
      not computable.

    Pre-decision rule (per operator spec, mapped to ``category``):

    - ``insufficient_sample`` -> INSUFFICIENT_SAMPLE.
    - ``baseline_n_months < 4`` -> INSUFFICIENT_BASELINE.
    - ``|z_weighted| > 3.0`` -> REGIME_REAL.
    - ``|z_weighted| < 2.0`` AND ``r_squared > 0.5`` -> DRIFT_ARTIFACT.
    - ``|z_weighted| < 2.0`` AND ``r_squared < 0.5`` -> AMBIGUOUS.
    - ``2.0 <= |z_weighted| <= 3.0`` -> MARGINAL.

    Raises ``ValueError`` on negative ``min_latest_trade_count_fraction``
    (matches the input-validation pattern of
    :func:`check_regime_stability_with_filter`).

    Same per-shape safety contract as the other gates: NEVER halts on
    missing data; only on a detected regime shift.
    """
    # --- input validation ---
    if min_latest_trade_count_fraction < 0:
        raise ValueError(
            f"min_latest_trade_count_fraction must be >= 0 "
            f"(got {min_latest_trade_count_fraction!r}); negative values "
            "silently disable the refuse-to-compute floor."
        )
    # Upper-bound guard per 2026-06-05 red-team MEDIUM: an over-large
    # fraction (e.g. operator typo `7` instead of `0.7`) would silently
    # refuse every month forever, masquerading as a "regime instability"
    # signal. 1.5 means latest needs 150% of baseline median to clear --
    # absurd; reject loud.
    if min_latest_trade_count_fraction > 1.5:
        raise ValueError(
            f"min_latest_trade_count_fraction must be <= 1.5 "
            f"(got {min_latest_trade_count_fraction!r}); values above "
            "1.5 silently refuse every Oracle run."
        )

    def _weighted_diag_stub() -> dict:
        return {
            "weighted_applied": False,
            "insufficient_sample": False,
            "category": "INSUFFICIENT_BASELINE",
            "latest_trade_count": None,
            "baseline_median_trade_count": None,
            "weight_factor": None,
            "corrected_residual_std": None,
            "z_score_detrended_weighted": float("nan"),
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
        }

    # --- daily mode short-circuit (mirror existing variants) ---
    if mode == "daily":
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": None,
            "mode_skipped": True,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_weighted_diag_stub(),
        }

    # --- query ---
    try:
        df = prepared_queries.monthly_sharpe_proxy(conn, months_back=_PULL_MONTHS)
    except Exception as e:  # pragma: no cover -- operator visibility
        logger.warning(
            "regime_gate(detrended_weighted): monthly_sharpe_proxy query "
            "failed (%s); treating as insufficient data, returning "
            "stable=True.",
            e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended_weighted) could not run: warehouse "
                f"query failed ({type(e).__name__}). Analysis not halted on "
                "missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_weighted_diag_stub(),
        }

    if df is None or df.empty:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended_weighted) could not run: no "
                "friction-applied trades found in the trailing window. "
                "Analysis not halted on missing data."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_weighted_diag_stub(),
        }

    # The weighted variant requires trade_count in addition to the
    # standard columns.
    required_columns = {"month", "sharpe_proxy", "trade_count"}
    missing = required_columns - set(df.columns)
    if missing:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended_weighted) could not run: "
                "prepared_queries.monthly_sharpe_proxy is missing required "
                f"columns {sorted(missing)}. Schema may have drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_weighted_diag_stub(),
        }

    try:
        df = df.sort_values("month").reset_index(drop=True)
        latest_row = df.iloc[-1]
        latest_month = _latest_month_str(latest_row["month"])
        latest_sharpe = latest_row["sharpe_proxy"]
        if pd.notna(latest_sharpe):
            latest_sharpe = float(latest_sharpe)
        else:
            latest_sharpe = None
        try:
            latest_trade_count = int(latest_row["trade_count"])
        except (TypeError, ValueError):
            latest_trade_count = None

        baseline_df = df.iloc[:-1].copy()
        baseline_df = baseline_df[baseline_df["sharpe_proxy"].notna()]
        baseline_n = int(len(baseline_df))
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        logger.warning(
            "regime_gate(detrended_weighted): unexpected DataFrame shape "
            "(%s: %s); treating as schema drift and returning stable=True.",
            type(e).__name__, e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended_weighted) could not run: unexpected "
                f"DataFrame shape ({type(e).__name__}). Schema may have "
                "drifted."
            ),
            "mode_skipped": False,
            "baseline_n_months": 0,
            "latest_month": None,
            "latest_sharpe_proxy": None,
            **_weighted_diag_stub(),
        }

    # Baseline median trade_count -- the reference for the floor and
    # the Welch correction.
    baseline_median_trade_count: float | None = None
    if not baseline_df.empty:
        try:
            baseline_median_trade_count = float(
                baseline_df["trade_count"].astype(float).median()
            )
        except (TypeError, ValueError):
            baseline_median_trade_count = None

    # --- refuse-to-compute floor (Bug-Hunter safety: catch zero-latest
    # BEFORE the Welch division below) ---
    floor_triggered = False
    if (
        baseline_median_trade_count is not None
        and baseline_median_trade_count > 0
        and latest_trade_count is not None
    ):
        cutoff = min_latest_trade_count_fraction * baseline_median_trade_count
        if latest_trade_count < cutoff:
            floor_triggered = True

    if floor_triggered:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                f"Regime gate (detrended_weighted) refused to compute: "
                f"latest_trade_count={latest_trade_count} is below the "
                f"floor "
                f"(min_latest_trade_count_fraction="
                f"{min_latest_trade_count_fraction} * "
                f"baseline_median_trade_count="
                f"{baseline_median_trade_count:.1f} = "
                f"{cutoff:.1f}). Insufficient sample: latest month is "
                f"under-traded relative to baseline; equal-variance "
                f"assumption violated. Analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "weighted_applied": True,
            "insufficient_sample": True,
            "category": "INSUFFICIENT_SAMPLE",
            "latest_trade_count": latest_trade_count,
            "baseline_median_trade_count": baseline_median_trade_count,
            "weight_factor": None,
            "corrected_residual_std": None,
            "z_score_detrended_weighted": float("nan"),
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
        }

    # --- legacy minimum baseline (matches detrended) ---
    if baseline_n < _MIN_BASELINE_MONTHS:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                f"Regime gate (detrended_weighted) could not run: only "
                f"{baseline_n} baseline month(s) of usable sharpe-proxy "
                f"data available (need >= {_MIN_BASELINE_MONTHS}). "
                "Insufficient data; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "weighted_applied": True,
            "insufficient_sample": False,
            "category": "INSUFFICIENT_BASELINE",
            "latest_trade_count": latest_trade_count,
            "baseline_median_trade_count": baseline_median_trade_count,
            "weight_factor": None,
            "corrected_residual_std": None,
            "z_score_detrended_weighted": float("nan"),
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
        }

    if latest_sharpe is None:
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended_weighted) could not run: latest "
                "month has no usable sharpe-proxy value (single trade or "
                "zero variance). Insufficient data; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": None,
            "weighted_applied": True,
            "insufficient_sample": False,
            "category": "INSUFFICIENT_BASELINE",
            "latest_trade_count": latest_trade_count,
            "baseline_median_trade_count": baseline_median_trade_count,
            "weight_factor": None,
            "corrected_residual_std": None,
            "z_score_detrended_weighted": float("nan"),
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
        }

    # --- linear regression (same math as the unweighted variant) ---
    try:
        y = baseline_df["sharpe_proxy"].astype(float).to_numpy()
        x = np.arange(len(y), dtype=float)
        coefs = np.polyfit(x, y, deg=1)
        slope = float(coefs[0])
        intercept = float(coefs[1])
    except (np.linalg.LinAlgError, ValueError, TypeError) as e:
        logger.warning(
            "regime_gate(detrended_weighted): polyfit failed (%s: %s); "
            "returning stable=True with NaN z.",
            type(e).__name__, e,
        )
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended_weighted) could not run: linear-fit "
                f"failed ({type(e).__name__}). Degenerate baseline; analysis "
                "not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "weighted_applied": True,
            "insufficient_sample": False,
            "category": "INSUFFICIENT_BASELINE",
            "latest_trade_count": latest_trade_count,
            "baseline_median_trade_count": baseline_median_trade_count,
            "weight_factor": None,
            "corrected_residual_std": None,
            "z_score_detrended_weighted": float("nan"),
            "baseline_slope": None,
            "baseline_intercept": None,
            "baseline_r_squared": None,
            "residuals": [],
            "latest_residual": None,
        }

    predicted = slope * x + intercept
    residuals = (y - predicted).tolist()
    predicted_latest = slope * float(len(y)) + intercept
    latest_residual = float(latest_sharpe - predicted_latest)

    if len(residuals) > 2:
        residual_std = float(np.std(np.asarray(residuals), ddof=2))
    else:
        residual_std = float("nan")

    y_mean = float(np.mean(y))
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    if ss_tot > 1e-12:
        r_squared = 1.0 - (ss_res / ss_tot)
        r_squared = max(0.0, min(1.0, r_squared))
    else:
        r_squared = 0.0

    # --- Welch-style sample-size correction ---
    # Floor already caught latest_trade_count < cutoff, so latest_n > 0 here.
    # baseline_median_trade_count > 0 also enforced (the floor branch only
    # triggers when both are positive).
    if (
        baseline_median_trade_count is not None
        and baseline_median_trade_count > 0
        and latest_trade_count is not None
        and latest_trade_count > 0
    ):
        weight_factor = math.sqrt(
            baseline_median_trade_count / float(latest_trade_count)
        )
    else:
        # Degenerate: missing trade_count metadata. Fall back to weight=1.0
        # (equivalent to the unweighted detrended variant) and flag in the
        # warning. The earlier floor check would have caught the more common
        # under-traded case; this branch is for missing-data only.
        weight_factor = 1.0

    if (
        math.isfinite(residual_std)
        and residual_std > 1e-10
    ):
        corrected_residual_std = residual_std * weight_factor
    else:
        corrected_residual_std = float("nan")

    diagnostics_common = {
        "baseline_slope": slope,
        "baseline_intercept": intercept,
        "baseline_r_squared": r_squared,
        "residuals": [float(r) for r in residuals],
        "latest_residual": latest_residual,
    }

    if (
        not math.isfinite(corrected_residual_std)
        or corrected_residual_std < 1e-10
    ):
        return {
            "stable": True,
            "z_score": float("nan"),
            "warning": (
                "Regime gate (detrended_weighted) could not run: residual "
                "standard deviation is zero (baseline is a perfect linear "
                "fit or degenerate). Z-score undefined; analysis not halted."
            ),
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            "weighted_applied": True,
            "insufficient_sample": False,
            "category": "INSUFFICIENT_BASELINE",
            "latest_trade_count": latest_trade_count,
            "baseline_median_trade_count": baseline_median_trade_count,
            "weight_factor": weight_factor,
            "corrected_residual_std": (
                float("nan")
                if not math.isfinite(corrected_residual_std)
                else corrected_residual_std
            ),
            "z_score_detrended_weighted": float("nan"),
            **diagnostics_common,
        }

    z_weighted = float(latest_residual / corrected_residual_std)

    # --- category mapping per operator pre-decision rule ---
    # Operator's pre-decision rule is HARD-PINNED on |z|=3.0 and |z|=2.0 --
    # NOT on the configurable z_threshold. The two thresholds serve
    # different audiences:
    #   - z_threshold controls the gate's HALT signal (stable=False), which
    #     the Oracle orchestrator uses to halt_on_unstable_regime. Weekly
    #     mode uses 1.5, research uses 3.0.
    #   - category serves the OPERATOR's freeze-lift decision and is
    #     pinned to the literal rule (REGIME_REAL > 3.0, MARGINAL [2.0, 3.0]).
    # Conflating them (the 2026-06-05 red-team HIGH bug pre-fix) made
    # the MARGINAL band unreachable in weekly mode.
    _CATEGORY_HIGH_THRESHOLD = 3.0
    _CATEGORY_LOW_THRESHOLD = 2.0
    abs_z = abs(z_weighted)
    if abs_z > _CATEGORY_HIGH_THRESHOLD:
        category = "REGIME_REAL"
    elif abs_z < _CATEGORY_LOW_THRESHOLD:
        if r_squared > 0.5:
            category = "DRIFT_ARTIFACT"
        else:
            category = "AMBIGUOUS"
    else:  # 2.0 <= abs_z <= 3.0
        category = "MARGINAL"

    diagnostics_full = {
        "weighted_applied": True,
        "insufficient_sample": False,
        "category": category,
        "latest_trade_count": latest_trade_count,
        "baseline_median_trade_count": baseline_median_trade_count,
        "weight_factor": weight_factor,
        "corrected_residual_std": corrected_residual_std,
        "z_score_detrended_weighted": z_weighted,
        **diagnostics_common,
    }

    if category == "REGIME_REAL":
        warning = (
            f"Regime instability detected (detrended_weighted): latest-month "
            f"sample-size-corrected residual z-score = {z_weighted:+.2f} "
            f"(threshold +/- {z_threshold:.2f}). Welch factor "
            f"{weight_factor:.3f} (latest_n={latest_trade_count} vs "
            f"baseline_median_n={baseline_median_trade_count:.0f}). "
            f"Baseline trend slope={slope:+.5f} per month, "
            f"intercept={intercept:+.5f}, r^2={r_squared:.3f}. Latest "
            f"sharpe {latest_sharpe:.3f} ({latest_month}) -> residual "
            f"{latest_residual:+.4f}; corrected_std="
            f"{corrected_residual_std:.4f}. Analysis halted."
        )
        logger.warning("regime_gate(detrended_weighted): %s", warning)
        return {
            "stable": False,
            "z_score": z_weighted,
            "warning": warning,
            "mode_skipped": False,
            "baseline_n_months": baseline_n,
            "latest_month": latest_month,
            "latest_sharpe_proxy": latest_sharpe,
            **diagnostics_full,
        }

    return {
        "stable": True,
        "z_score": z_weighted,
        "warning": None,
        "mode_skipped": False,
        "baseline_n_months": baseline_n,
        "latest_month": latest_month,
        "latest_sharpe_proxy": latest_sharpe,
        **diagnostics_full,
    }
