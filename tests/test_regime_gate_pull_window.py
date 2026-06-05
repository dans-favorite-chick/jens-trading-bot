"""Tests for the `_PULL_MONTHS=13` structural bump in regime_gate.

Phoenix Strategy Oracle — 2026-06-05 R2 Finding 3 sprint.

The prior R2 Diagnostic sprint identified that `_PULL_MONTHS=7` against
the filter's default floor `min_baseline_n_after_filter=6` structurally
neutered the filtered-baseline diagnostic: any sparse-month drop forced
INSUFFICIENT_BASELINE rather than a drop-and-recompute path. Bumping
`_PULL_MONTHS` to 13 (full-year baseline) gives the filter statistical
headroom AND lets the detrended variant fit a meaningful linear trend.

These tests lock in the new window contract. They are deliberately
behavioral — they query the prepared_queries SQL end-to-end and assert
on the regime_gate's externally-visible output (baseline_n_months,
warning text), not on the constant value itself (which is implementation
detail).

Helper pattern mirrors `tests/test_regime_gate.py`.
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
# Helpers (mirror existing test_regime_gate.py exactly)
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


def _trades_for_sharpe(target_mean: float, target_std: float) -> list[float]:
    if target_std == 0:
        return [target_mean, target_mean, target_mean]
    d = target_std
    return [target_mean - d, target_mean, target_mean + d]


def _insert_month(con, run_id, strategy, month_start_utc, pnls):
    for i, p in enumerate(pnls):
        ts = month_start_utc + timedelta(days=14, hours=15, minutes=i)
        _ins_trade(con, run_id, strategy, ts, p)


def _build_db_n_months(n_months: int, strategy: str = "gamma") -> duckdb.DuckDBPyConnection:
    """Build N months of synthetic data, all with sharpe_proxy ~ 1.0 with
    small jitter so the gate stays stable."""
    con = duckdb.connect(":memory:")
    apply_schema(con)
    _ins_run(con, "R_TEST", friction=True, strategy=strategy)

    anchor = datetime.now(tz=UTC) - timedelta(days=20)
    months_back_from = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)

    month_starts: list[datetime] = []
    cur = months_back_from
    for _ in range(n_months):
        month_starts.append(cur)
        prev_year, prev_month = (
            (cur.year, cur.month - 1) if cur.month > 1 else (cur.year - 1, 12)
        )
        cur = datetime(prev_year, prev_month, 1, tzinfo=UTC)
    month_starts.reverse()

    for i, m_start in enumerate(month_starts):
        # Slight per-month sharpe jitter centered on 1.0.
        sharpe = 1.0 + 0.02 * ((i % 5) - 2)
        pnls = _trades_for_sharpe(target_mean=sharpe * 10.0, target_std=10.0)
        _insert_month(con, "R_TEST", strategy, m_start, pnls)

    return con


# ---------------------------------------------------------------------------
# 1) With _PULL_MONTHS=13 and 14 months of synthetic data, the gate must
#    see ~12 baseline months + 1 latest (not capped at 6).
# ---------------------------------------------------------------------------


def test_pull_window_returns_at_least_12_baseline_months_with_14m_of_data():
    """Bumping `_PULL_MONTHS` from 7 to 13 gives the SQL window room to
    pull a full year of baseline. With 14 months of synthetic data the
    gate should report `baseline_n_months >= 12` (SQL rolling-window
    edge can land on 12 or 13).
    """
    con = _build_db_n_months(14)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert out["mode_skipped"] is False
    # Was capped at ~6 under _PULL_MONTHS=7. Must now exceed 11.
    assert out["baseline_n_months"] >= 12, (
        f"baseline_n_months should be >= 12 after _PULL_MONTHS bump, "
        f"got {out['baseline_n_months']} — is _PULL_MONTHS still 7?"
    )


# ---------------------------------------------------------------------------
# 2) The bumped window gives the filtered variant statistical headroom:
#    with default `min_baseline_n_after_filter=6` and 8 normal months +
#    1 sparse + 1 latest, the filter can drop the sparse month AND keep
#    8 baseline months >> 6 floor. No INSUFFICIENT_BASELINE.
# ---------------------------------------------------------------------------


def test_filtered_variant_has_headroom_to_drop_sparse_month_at_default_floor():
    """The whole reason for the bump: at `_PULL_MONTHS=7`, dropping any
    sparse month under default floor=6 forced INSUFFICIENT_BASELINE
    (red-team CRITICAL, 2026-06-05). Post-bump, the filter must have
    real headroom to drop AND recompute z.
    """
    con = duckdb.connect(":memory:")
    apply_schema(con)
    _ins_run(con, "R_TEST", friction=True, strategy="gamma")

    anchor = datetime.now(tz=UTC) - timedelta(days=20)
    months_back_from = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)
    cur = months_back_from
    month_starts: list[datetime] = []
    # 9 normal months + 1 sparse + 1 latest = 11 specs (room for SQL window).
    for _ in range(11):
        month_starts.append(cur)
        prev_year, prev_month = (
            (cur.year, cur.month - 1) if cur.month > 1 else (cur.year - 1, 12)
        )
        cur = datetime(prev_year, prev_month, 1, tzinfo=UTC)
    month_starts.reverse()

    # First 9 baseline months: 10 trades, sharpe ~ 1.0
    for i, m_start in enumerate(month_starts[:9]):
        pnls = _trades_for_sharpe(target_mean=10.0, target_std=10.0)
        # Insert 10 trades by repeating the symmetric triple + extras.
        extra_pnls = pnls + [10.0] * 7
        _insert_month(con, "R_TEST", "gamma", m_start, extra_pnls)

    # 10th baseline month: 3 trades (sparse — below 0.5 * median)
    sparse_start = month_starts[9]
    sparse_pnls = _trades_for_sharpe(target_mean=5.0, target_std=10.0)
    _insert_month(con, "R_TEST", "gamma", sparse_start, sparse_pnls)

    # Latest month: 10 trades, sharpe = 1.0
    latest_start = month_starts[10]
    latest_pnls = _trades_for_sharpe(target_mean=10.0, target_std=10.0) + [10.0] * 7
    _insert_month(con, "R_TEST", "gamma", latest_start, latest_pnls)

    out = rg.check_regime_stability_with_filter(
        con, mode="weekly",
        sparse_factor=0.5, min_baseline_n_after_filter=6,
    )
    con.close()

    assert out["filter_applied"] is True
    # The whole point: filter must have headroom to drop the sparse
    # month AND still pass the floor.
    assert out["insufficient_baseline_after_filter"] is False, (
        f"filter still triggers INSUFFICIENT after _PULL_MONTHS bump — "
        f"baseline_n_months={out['baseline_n_months']}, "
        f"pre_filter={out['pre_filter_baseline_n']}, "
        f"dropped={out['dropped_months']}"
    )
    # And the drop should have actually happened.
    assert len(out["dropped_months"]) >= 1, (
        f"sparse month should have been dropped, got "
        f"dropped_months={out['dropped_months']}"
    )
    # Baseline should comfortably exceed the floor.
    assert out["baseline_n_months"] >= 6


# ---------------------------------------------------------------------------
# 3) Insufficient data still triggers the legacy `_MIN_BASELINE_MONTHS=4`
#    floor (defensive: bumping `_PULL_MONTHS` must NOT loosen the floor).
# ---------------------------------------------------------------------------


def test_insufficient_baseline_below_min_baseline_months_still_warns():
    """With only 3 months of data, the gate must still return
    insufficient (legacy `_MIN_BASELINE_MONTHS=4` floor stays in effect).
    The `_PULL_MONTHS` bump doesn't change the minimum-data requirement.
    """
    con = _build_db_n_months(3)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    # 3 months total -> 2 baseline (latest removed) -> < 4 floor.
    assert out["mode_skipped"] is False
    assert out["stable"] is True  # gate never halts on missing data
    assert math.isnan(out["z_score"])
    assert out["baseline_n_months"] < 4
    assert "Insufficient data" in (out["warning"] or "")


# ---------------------------------------------------------------------------
# 4) Documentation invariant: the constant lives at module scope so
#    callers/tests can introspect.
# ---------------------------------------------------------------------------


def test_pull_months_constant_is_introspectable_at_module_scope():
    """`_PULL_MONTHS` is a module-level constant. The post-bump value
    should be 13 (full-year baseline + 1 latest). This is a STRUCTURAL
    test — it pins implementation choice — but it's intentional, because
    operator-facing documentation and downstream sprints rely on the
    constant's value.
    """
    assert hasattr(rg, "_PULL_MONTHS")
    assert rg._PULL_MONTHS == 13
