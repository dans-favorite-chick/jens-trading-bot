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

# We pull 7 months from monthly_sharpe_proxy. Typical result: 6 baseline + 1 latest.
# Early in a calendar month the rolling 7-month SQL window can produce up to 8 distinct
# months (now() - 7 mo cutoff straddles an extra month boundary), so baseline_n_months
# may occasionally be 7 instead of 6. This is statistically fine -- more baseline data
# only improves the z-score's reliability.
_PULL_MONTHS = 7

Mode = Literal["research", "weekly", "daily"]

__all__ = ["check_regime_stability", "Z_THRESHOLD_DEFAULT"]


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

    1. Pull last ~6 months of portfolio-wide monthly sharpe-proxy via
       ``prepared_queries.monthly_sharpe_proxy(conn, months_back=6)``.
    2. Drop the most recent month's row to form the baseline (the
       "trailing 6 months excluding latest" baseline).
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
                "in the trailing 6 months. Analysis not halted on missing data."
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
