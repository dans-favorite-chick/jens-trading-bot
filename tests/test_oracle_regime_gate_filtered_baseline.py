"""Tests for the R2-diagnostic filtered-baseline variant of regime_gate.

Phoenix Strategy Oracle — 2026-06-05 R2 Diagnostic sprint.

The 2026-06-04 Oracle run halted on `z=+3.69` and the R2 Bug Hunter
red-team flagged the verdict as SUSPECT (two CRITICAL methodology
concerns about baseline composition).  The diagnostic R2 proposed is a
filtered-baseline variant that drops months with abnormally low
trade-count and requires a minimum baseline_n after filtering.

This file pins the contract of the new `check_regime_stability_with_filter`
function before it's implemented.  Default `check_regime_stability`
behavior is left untouched (regression-guarded by
`test_default_function_byte_identical_with_filter_none` below).

Test-helper pattern mirrors `tests/test_regime_gate.py` — real in-memory
DuckDB via `tools.warehouse.db.apply_schema` with synthetic trades so
the prepared_queries SQL is exercised end-to-end.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from tools.warehouse.db import apply_schema

from analytics import regime_gate as rg
from analytics import prepared_queries as pq


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers (mirror the existing test_regime_gate.py shape but expose a
# per-month trade_count knob — the filter operates on that column.)
# ---------------------------------------------------------------------------


def _ins_run(con, run_id, friction=True, strategy="gamma"):
    con.execute(
        """
        INSERT INTO runs (run_id, source_filename, csv_kind, strategy, friction_applied)
        VALUES (?, ?, ?, ?, ?)
        """,
        [run_id, f"{run_id}.csv", "trades", strategy, friction],
    )


def _ins_trade(con, run_id, strategy, entry_ts, pnl_dollars):
    con.execute(
        """
        INSERT INTO trades (
            run_id, strategy, direction, entry_ts, entry_price,
            exit_ts, exit_price, pnl_dollars, pnl_ticks, hold_minutes, year
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id, strategy, "LONG", entry_ts, 21000.0,
            entry_ts + timedelta(minutes=15), 21010.0,
            float(pnl_dollars), float(pnl_dollars) / 25.0, 15.0,
            entry_ts.year,
        ],
    )


def _trade_pnls_for_sharpe(
    n_trades: int, target_mean: float, target_std: float
) -> list[float]:
    """Build `n_trades` PnL values whose AVG = `target_mean` and
    STDDEV_SAMP (ddof=1) = `target_std`.

    Strategy: emit `target_mean - target_std`, then `n_trades - 2` copies of
    `target_mean`, then `target_mean + target_std`.  Sample variance with
    ddof=1 over [m-d, m, m, ..., m, m+d] is 2*d^2 / (n-1), so std is
    d * sqrt(2/(n-1)).  We rescale d to land on target_std exactly.
    """
    if n_trades < 2:
        raise ValueError("need at least 2 trades to define a std")
    # Pick d so that d * sqrt(2/(n-1)) == target_std
    d = target_std * math.sqrt((n_trades - 1) / 2.0)
    pnls = [target_mean - d] + [target_mean] * (n_trades - 2) + [target_mean + d]
    return pnls


def _insert_month_n_trades(con, run_id, strategy, month_start_utc, n_trades, sharpe):
    """Insert `n_trades` trades in the given calendar month so the month's
    monthly_sharpe_proxy is `sharpe` (avg / std with target_std=10).

    Special cases:
    - sharpe = NaN: emit exactly 1 trade so STDDEV_SAMP returns NULL ->
      sharpe_proxy NULL. n_trades is ignored in this branch (always 1).
    """
    if isinstance(sharpe, float) and math.isnan(sharpe):
        # Single trade -> stddev NULL -> sharpe_proxy NULL.
        ts = month_start_utc + timedelta(days=14, hours=15)
        _ins_trade(con, run_id, strategy, ts, 10.0)
        return
    if n_trades < 2:
        # Caller wants <2 trades but a finite sharpe: emit n_trades copies
        # of the mean (std will be NULL -> sharpe NaN; caller likely just
        # wanted low count for the sparse filter to bite).
        for i in range(n_trades):
            ts = month_start_utc + timedelta(days=14, hours=15, minutes=i)
            _ins_trade(con, run_id, strategy, ts, sharpe * 10.0)
        return
    # Use std=10 as a fixed scale; mean = sharpe * 10.
    pnls = _trade_pnls_for_sharpe(
        n_trades=n_trades, target_mean=sharpe * 10.0, target_std=10.0
    )
    for i, p in enumerate(pnls):
        # Spread across the month so all trades stay within the right month
        # bucket but don't clash on minute resolution.
        ts = month_start_utc + timedelta(days=14, hours=15, minutes=i)
        _ins_trade(con, run_id, strategy, ts, p)


def _build_db_with_counts(
    month_specs: list[tuple[int, float]],
    strategy: str = "gamma",
    months_back_from: datetime | None = None,
) -> duckdb.DuckDBPyConnection:
    """Build an in-memory DB.

    `month_specs[i] = (n_trades_in_month, sharpe_proxy_target)`, ordered
    oldest-first.  The most-recent spec becomes the "latest" month for the
    regime gate.

    Uses the same anchor strategy as the existing test_regime_gate helper.
    """
    con = duckdb.connect(":memory:")
    apply_schema(con)
    _ins_run(con, "R_TEST", friction=True, strategy=strategy)

    if months_back_from is None:
        anchor = datetime.now(tz=UTC) - timedelta(days=20)
        months_back_from = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)

    month_starts: list[datetime] = []
    cur = months_back_from
    for _ in range(len(month_specs)):
        month_starts.append(cur)
        prev_year, prev_month = (
            (cur.year, cur.month - 1) if cur.month > 1 else (cur.year - 1, 12)
        )
        cur = datetime(prev_year, prev_month, 1, tzinfo=UTC)
    month_starts.reverse()

    for m_start, (n_trades, sharpe) in zip(month_starts, month_specs):
        _insert_month_n_trades(con, "R_TEST", strategy, m_start, n_trades, sharpe)

    return con


# ---------------------------------------------------------------------------
# 1) Sparse-month filter drops a single low-trade-count baseline month.
# ---------------------------------------------------------------------------


def test_filter_drops_one_sparse_baseline_month():
    """6 normal baseline months (10 trades each) + 1 sparse month (3 trades)
    + 1 latest.  Median trade_count across baseline = 10; threshold =
    0.5*10 = 5; the 3-trade month is below 5 and gets dropped.

    Result: filter_applied=True, pre_filter_baseline_n=7,
    baseline_n_months=6, dropped_months has one entry.
    """
    # 6 normal months @ sharpe=1.0, then 1 sparse month with 3 trades but
    # similar sharpe, then 1 latest at sharpe=1.0.  Note baseline_n in the
    # output excludes the latest month.
    specs = [
        (10, 1.00),  # oldest baseline
        (10, 1.05),
        (10, 0.98),
        (10, 1.02),
        (10, 0.97),
        (10, 1.03),
        (3,  0.50),   # sparse — filter drops this
        (10, 1.01),   # latest
    ]
    con = _build_db_with_counts(specs)

    # NB: lower the floor to 5 for this test so the SQL rolling window's
    # 6-vs-7 month edge case doesn't flip the verdict. The point of the
    # test is to verify the sparse month was dropped, not the floor.
    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=5,
    )
    con.close()

    assert out["mode_skipped"] is False
    assert out["filter_applied"] is True
    # Pre-filter baseline_n is 6 or 7 depending on SQL window edge. The
    # invariant that matters: filter dropped exactly one month from baseline.
    assert out["pre_filter_baseline_n"] in (6, 7)
    assert out["pre_filter_baseline_n"] - out["baseline_n_months"] == 1
    assert isinstance(out["dropped_months"], list)
    assert len(out["dropped_months"]) == 1
    # Each entry should be a YYYY-MM string.
    assert all(
        isinstance(m, str) and len(m) == 7 and m[4] == "-"
        for m in out["dropped_months"]
    )
    # Floor not triggered.
    assert out["insufficient_baseline_after_filter"] is False


# ---------------------------------------------------------------------------
# 2) Filter keeps all months when all are normal.
# ---------------------------------------------------------------------------


def test_filter_keeps_all_when_no_sparse_months():
    """8 normal baseline months + 1 latest.  Filter drops none."""
    specs = [(10, 1.0 + 0.05 * i) for i in range(-4, 4)] + [(10, 1.0)]
    con = _build_db_with_counts(specs)

    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=6,
    )
    con.close()

    assert out["filter_applied"] is True
    assert out["pre_filter_baseline_n"] >= 6
    assert out["baseline_n_months"] == out["pre_filter_baseline_n"]
    assert out["dropped_months"] == []


# ---------------------------------------------------------------------------
# 3) Insufficient baseline after filter — must NOT halt analysis (per spec
#    the gate never halts on missing data), but MUST signal the operator.
# ---------------------------------------------------------------------------


def test_filter_returns_insufficient_baseline_when_under_minimum():
    """4 normal baseline months @ 10 trades + 2 sparse @ 3 trades + 1 latest.
    Filter drops 2 sparse → baseline_n = 4.  Required = 6 → INSUFFICIENT.

    Per spec the gate must NOT halt analysis on missing data; instead it
    returns stable=True with `insufficient_baseline_after_filter=True` so
    the orchestrator can render an operator-visible warning.
    """
    specs = [
        (10, 1.00),
        (10, 1.05),
        (10, 0.98),
        (10, 1.02),
        (3,  0.50),   # sparse
        (3,  0.55),   # sparse
        (10, 1.01),   # latest
    ]
    con = _build_db_with_counts(specs)

    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=6,
    )
    con.close()

    assert out["filter_applied"] is True
    assert out["pre_filter_baseline_n"] == 6
    assert out["baseline_n_months"] == 4
    assert out["insufficient_baseline_after_filter"] is True
    # Per spec: must NOT halt analysis when data is thin.
    assert out["stable"] is True
    assert "insufficient" in (out["warning"] or "").lower()
    # z_score must be NaN since baseline too thin to compute.
    assert math.isnan(out["z_score"])


# ---------------------------------------------------------------------------
# 4) Default function unchanged — passing no filter through the new function
#    must produce identical result to calling the original function.
# ---------------------------------------------------------------------------


def test_default_function_byte_identical_with_filter_none():
    """Calling the original `check_regime_stability` must yield the same
    result as it did pre-sprint.  We assert key parity against a parallel
    run of the new function with no filter config (the new function should
    fall through to the same math path when no filter is configured)."""
    specs = [(10, s) for s in (1.00, 1.05, 0.98, 1.02, 0.97, 1.03, 1.01)]
    con1 = _build_db_with_counts(specs)
    base = rg.check_regime_stability(con1, mode="weekly")
    con1.close()

    # The new function with sparse_factor=0 effectively disables the filter
    # (no month's trade_count can be < 0).  And min_baseline_n_after_filter=0
    # disables the floor.  The resulting baseline_n and z_score must match.
    con2 = _build_db_with_counts(specs)
    new = rg.check_regime_stability_with_filter(
        con2, mode="weekly",
        sparse_factor=0.0, min_baseline_n_after_filter=0,
    )
    con2.close()

    for key in ("stable", "mode_skipped", "latest_month", "baseline_n_months"):
        assert base[key] == new[key], f"mismatch on {key}: {base[key]} vs {new[key]}"
    # z_score uses floats — compare with a tight tolerance.
    if math.isnan(base["z_score"]):
        assert math.isnan(new["z_score"])
    else:
        assert abs(base["z_score"] - new["z_score"]) < 1e-9


# ---------------------------------------------------------------------------
# 5) Filter must short-circuit in daily mode (matching existing behavior).
# ---------------------------------------------------------------------------


def test_filter_short_circuits_in_daily_mode():
    """`daily` mode must skip the gate entirely — even with the filter
    configured.  Returns the same stub as the unfiltered daily path."""
    empty = duckdb.connect(":memory:")
    out = rg.check_regime_stability_with_filter(
        empty, mode="daily",
        sparse_factor=0.5, min_baseline_n_after_filter=6,
    )
    empty.close()

    assert out["stable"] is True
    assert out["mode_skipped"] is True
    assert math.isnan(out["z_score"])
    # Filter diagnostics should still be exposed (filter_applied=False because
    # daily mode skipped before any filtering).
    assert out.get("filter_applied") is False


# ---------------------------------------------------------------------------
# 6) z-score must change when the filter drops a sparse month with extreme
#    sharpe-proxy that distorts the baseline std.
# ---------------------------------------------------------------------------


def test_filter_changes_z_when_dropping_extreme_sparse_month():
    """A sparse outlier month with extreme sharpe inflates baseline std and
    pulls baseline mean — filtering it should materially shift z.

    Without filter: 6 baseline months at sharpe=1.0 + 1 sparse month at
    sharpe=-3.0 (3 trades).  Baseline mean drags down, std inflates → z
    of latest=1.0 vs that polluted baseline is mild.

    With filter: sparse month dropped.  Baseline is 6 tight months at ~1.0
    → std collapses → z of latest=1.0 is near zero too (because identical
    to baseline mean).  In any case the two z values must differ noticeably.
    """
    specs = [
        (10, 1.00),
        (10, 1.05),
        (10, 0.98),
        (10, 1.02),
        (10, 0.97),
        (10, 1.03),
        (3,  -3.00),   # sparse outlier
        (10, 1.00),    # latest
    ]
    con1 = _build_db_with_counts(specs)
    unfiltered = rg.check_regime_stability(con1, mode="weekly")
    con1.close()

    # NB: lower the floor to 4 so the filter's drop-and-recompute branch
    # actually runs. With the production default min_baseline_n_after_filter
    # =6 against a 7-month SQL pull, the filter is structurally unable to
    # drop any month without immediately triggering INSUFFICIENT_BASELINE
    # (red-team CRITICAL finding, 2026-06-05).
    con2 = _build_db_with_counts(specs)
    filtered = rg.check_regime_stability_with_filter(
        con2, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=4,
    )
    con2.close()

    # The two z scores must NOT be byte-equal — the filter is supposed to
    # change something here. R3 Test Quality MEDIUM #1 fix (2026-06-05):
    # previous version silently passed when one z was NaN and the other
    # was finite, which would hide a real regression where the filter
    # accidentally collapses baseline to all-NaN. The synthetic inputs
    # here are deliberately chosen so BOTH paths produce finite z (6 normal
    # baseline + 1 sparse outlier baseline + 1 latest; even after the filter
    # drops the sparse outlier we have 6 baseline months for the filtered
    # path's math). Demand both finite AND demand they differ.
    z_u = unfiltered["z_score"]
    z_f = filtered["z_score"]
    assert not math.isnan(z_u), (
        f"unfiltered z is NaN — synthetic setup unexpectedly degenerate"
    )
    assert not math.isnan(z_f), (
        f"filtered z is NaN — filter accidentally collapsed baseline "
        f"(filter_applied={filtered.get('filter_applied')}, "
        f"baseline_n={filtered.get('baseline_n_months')}, "
        f"insufficient={filtered.get('insufficient_baseline_after_filter')})"
    )
    assert abs(z_u - z_f) > 0.01, (
        f"filter didn't shift z meaningfully: unfiltered={z_u}, "
        f"filtered={z_f}"
    )


# ---------------------------------------------------------------------------
# 7) Input validation: negative sparse_factor / min_baseline_n must raise.
# ---------------------------------------------------------------------------


def test_filter_raises_on_negative_sparse_factor():
    """Bug Hunter R2 CRITICAL #1 (2026-06-05): a negative sparse_factor
    silently produced a no-op filter that audit-logged as if it had run.
    Now must raise ValueError at call site so the operator fixes the bug
    instead of trusting a misleading verdict."""
    empty = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="sparse_factor"):
        rg.check_regime_stability_with_filter(
            empty, mode="weekly",
            sparse_factor=-0.5, min_baseline_n_after_filter=6,
        )
    empty.close()


def test_filter_raises_on_negative_min_baseline_n():
    """Bug Hunter R2 CRITICAL #1 fix: negative floor silently disabled
    the insufficient-baseline check."""
    empty = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="min_baseline_n_after_filter"):
        rg.check_regime_stability_with_filter(
            empty, mode="weekly",
            sparse_factor=0.5, min_baseline_n_after_filter=-1,
        )
    empty.close()


# ---------------------------------------------------------------------------
# 8) Diagnostic accounting identity:
#    pre_filter_baseline_n == sparse_drops + nan_sharpe_drops + baseline_n.
# ---------------------------------------------------------------------------


def test_filter_diagnostic_accounting_reconciles_under_combined_drops():
    """Bug Hunter R2 CRITICAL #2 (2026-06-05): the operator-facing warning
    text reported "dropped N sparse from M" without crediting NaN-sharpe
    drops, leaving the arithmetic short when both drop types fired.

    The new key `nan_sharpe_drops_after_filter` plus the existing
    `dropped_months` list must together reconcile the arithmetic:
        pre_filter_baseline_n
          = len(dropped_months)
          + nan_sharpe_drops_after_filter
          + baseline_n_months

    Build a baseline that triggers BOTH drop types: 5 normal months @ 10
    trades each + 1 sparse month @ 3 trades + 1 NaN-sharpe month (single
    trade, std undefined -> sharpe NaN) + 1 latest.
    """
    specs = [
        (10, 1.00),
        (10, 1.05),
        (10, 0.98),
        (10, 1.02),
        (10, 0.97),
        (3,  0.50),     # sparse (will be dropped by trade-count filter)
        (1,  math.nan),  # single-trade -> NaN sharpe (NaN-sharpe drop)
        (10, 1.01),     # latest
    ]
    con = _build_db_with_counts(specs)
    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=4,
    )
    con.close()

    pre = out["pre_filter_baseline_n"]
    n_sparse = len(out["dropped_months"])
    n_nan = out["nan_sharpe_drops_after_filter"]
    post = out["baseline_n_months"]
    assert pre == n_sparse + n_nan + post, (
        f"accounting identity broken: pre={pre} != sparse={n_sparse} + "
        f"nan={n_nan} + baseline_n={post}"
    )
    # Both drop types must have fired given the synthetic setup.
    assert n_sparse >= 1, "expected at least 1 sparse drop on this setup"
    # NB: the NaN-sharpe month had trade_count=1 which is ALSO below the
    # sparse cutoff. It gets credited to dropped_months (sparse filter
    # removes it BEFORE the NaN-sharpe filter sees it). So n_nan might be 0
    # even though we inserted a NaN-sharpe month -- that's not a bug, it's
    # the documented sparse-first ordering. The accounting identity above
    # still holds, which is the property under test.


# ---------------------------------------------------------------------------
# 9) Latest month with low trade_count is NEVER filtered (it's the month
#    being tested, not part of the baseline).
# ---------------------------------------------------------------------------


def test_filter_never_drops_latest_month_even_when_sparse():
    """R3 Test Quality missing-test (2026-06-05): the filter operates on
    the BASELINE only. A latest month with low trade_count must still be
    used as the latest sharpe value -- the gate's job is to test it, not
    to filter it."""
    # 6 normal baseline months @ 10 trades + 1 latest @ 2 trades (very low).
    specs = [(10, s) for s in (1.00, 1.05, 0.98, 1.02, 0.97, 1.03)]
    specs.append((2, 1.50))  # latest, low count, high sharpe
    con = _build_db_with_counts(specs)
    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=5,
    )
    con.close()

    # Latest month is reported even though its trade_count is below the
    # cutoff that would apply to a baseline month.
    assert out["latest_sharpe_proxy"] is not None
    assert out["latest_month"] is not None
    # The filter only touches the baseline.
    assert out["pre_filter_baseline_n"] >= 5


# ---------------------------------------------------------------------------
# 10) baseline_median_trade_count is exposed on every applied-filter path.
# ---------------------------------------------------------------------------


def test_filter_exposes_baseline_median_trade_count():
    """R3 Test Quality missing-test (2026-06-05): the audit log must
    capture the trade-count median that produced the cutoff, so the
    operator can reproduce the verdict."""
    specs = [(10, s) for s in (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)]
    con = _build_db_with_counts(specs)
    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=5,
    )
    con.close()

    assert out["filter_applied"] is True
    assert out["baseline_median_trade_count"] is not None
    # All baseline months have 10 trades -> median is 10.
    assert out["baseline_median_trade_count"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 11) Schema drift: missing `trade_count` column returns the schema-drift
#     shape (the default function doesn't require trade_count; the filtered
#     variant does).
# ---------------------------------------------------------------------------


def test_filter_returns_schema_drift_when_trade_count_missing(monkeypatch):
    """R3 Test Quality missing-test (2026-06-05): the filtered variant
    requires trade_count to compute the cutoff. If a future schema change
    drops the column from monthly_sharpe_proxy, the gate must NOT crash
    -- it must return the stable=True schema-drift shape (per the
    no-halt-on-missing-data spec).
    """
    import pandas as pd

    def _no_trade_count_query(_conn, months_back=6):
        # Mimic a future schema where trade_count was dropped.
        return pd.DataFrame({
            "month": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"]),
            "sharpe_proxy": [0.1, 0.2, 0.15],
        })

    monkeypatch.setattr(
        rg.prepared_queries, "monthly_sharpe_proxy", _no_trade_count_query
    )
    con = duckdb.connect(":memory:")
    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=6,
    )
    con.close()

    assert out["stable"] is True
    assert math.isnan(out["z_score"])
    assert "trade_count" in (out["warning"] or "")
    # Filter never applied because schema check fires first.
    assert out["filter_applied"] is False
