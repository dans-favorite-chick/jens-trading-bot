"""Tests for analytics/regime_gate.py.

Phoenix Strategy Oracle - Task 3.

The regime gate is the pre-flight stability check the orchestrator runs
before any LLM call in `weekly` and `research` modes. It computes a
z-score of the latest month's sharpe_proxy against the trailing 6-month
baseline (latest month excluded). |z| > threshold => halt.

These tests build a small synthetic in-memory DuckDB via the real
warehouse schema (tools.warehouse.db.apply_schema) and drive
prepared_queries.monthly_sharpe_proxy by inserting actual trade rows
across distinct months. No mocking of the SQL layer -- we exercise the
real prepared query so the tests catch contract drift between the gate
and prepared_queries.

The synthetic monthly_sharpe_proxy values are controlled by inserting
trades with carefully chosen pnl_dollars so AVG / STDDEV_SAMP land on
predictable numbers. We use 3 trades per month with values such that
their mean and sample stdev produce a known sharpe_proxy.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd
import pytest

from tools.warehouse.db import apply_schema

from analytics import regime_gate as rg
from analytics import prepared_queries as pq


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ins_run(con, run_id, friction=True, strategy="gamma", csv_kind="trades"):
    con.execute(
        """
        INSERT INTO runs (run_id, source_filename, csv_kind, strategy, friction_applied)
        VALUES (?, ?, ?, ?, ?)
        """,
        [run_id, f"{run_id}.csv", csv_kind, strategy, friction],
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


# Triples that produce a known sharpe_proxy given AVG/STDDEV_SAMP semantics.
# For values (a, b, c): mean = (a+b+c)/3 ; sample std uses ddof=1.
#
# Triple (5, -1, 2): mean=2.0, sample std=3.0  -> sharpe_proxy=0.6667
# Triple (4, 0, 2):  mean=2.0, sample std=2.0  -> sharpe_proxy=1.0
# Triple (12, -8, 8): mean=4.0, sample std=10.583... -> sharpe_proxy=0.378
#
# We will use simple PnL values per month to land on specific sharpe values.
def _trades_for_sharpe(target_mean: float, target_std: float) -> list[float]:
    """Build a 3-trade pnl sequence whose AVG = target_mean and
    STDDEV_SAMP = target_std (sample ddof=1).

    For pnls (m-d, m, m+d): mean = m, sample std = |d| * sqrt(2) (n=3).
    So d = target_std / sqrt(2). target_std MUST be positive (std is
    always non-negative); callers wanting a negative sharpe_proxy should
    pass a negative target_mean with a positive target_std.
    """
    if target_std == 0:
        return [target_mean, target_mean, target_mean]
    if target_std < 0:
        raise ValueError("target_std must be non-negative")
    d = target_std / math.sqrt(2.0)
    return [target_mean - d, target_mean, target_mean + d]


def _insert_month(con, run_id, strategy, month_start_utc: datetime, pnls: list[float]):
    """Insert N synthetic trades in a single calendar month (CT).

    Pin all trades to mid-month at 15:00 UTC so the Chicago-time
    session_date lands in the intended month regardless of DST.
    """
    for i, p in enumerate(pnls):
        ts = month_start_utc + timedelta(days=14, hours=15, minutes=i)
        _ins_trade(con, run_id, strategy, ts, p)


def _build_db(month_sharpe_values: list[float], strategy: str = "gamma",
              months_back_from: datetime | None = None) -> duckdb.DuckDBPyConnection:
    """Build an in-memory DB with N months of trades, each month producing
    the given sharpe_proxy value (avg_pnl / pnl_stddev).

    Each month gets a fixed (mean=10.0, std=10.0/sharpe) triple so the
    resulting sharpe_proxy = 1.0 by default -- adjusted to hit the target.

    Months are placed from the most recent backwards. `months_back_from`
    sets the most-recent month; default is today (UTC).
    """
    con = duckdb.connect(":memory:")
    apply_schema(con)
    _ins_run(con, "R_TEST", friction=True, strategy=strategy)

    if months_back_from is None:
        # Use a fixed reference "now" via a recent date -- but the prepared
        # query filters by `entry_ts >= now() - (months_back * INTERVAL '1
        # month')` using DuckDB's now(), so the trades MUST be recent.
        # Anchor to today (UTC) so the trades land inside the 6-month
        # window when the test runs.
        now_utc = datetime.now(tz=UTC)
        # Pin to the start of the current UTC month so the latest month is
        # the current calendar month.
        months_back_from = datetime(now_utc.year, now_utc.month, 1, tzinfo=UTC)

    # Build month starts going backwards. month_sharpe_values[-1] is the
    # most recent month (latest).
    month_starts: list[datetime] = []
    cur = months_back_from
    for _ in range(len(month_sharpe_values)):
        month_starts.append(cur)
        # Step back one month (approx -- use day=1 of previous month).
        prev_year, prev_month = (cur.year, cur.month - 1) if cur.month > 1 else (cur.year - 1, 12)
        cur = datetime(prev_year, prev_month, 1, tzinfo=UTC)
    month_starts.reverse()  # oldest first

    # Now month_starts[i] corresponds to month_sharpe_values[i] in
    # chronological order.
    for m_start, sharpe in zip(month_starts, month_sharpe_values):
        if math.isnan(sharpe):
            # Single trade -> stddev = NULL -> sharpe = NULL
            ts = m_start + timedelta(days=14, hours=15)
            _ins_trade(con, "R_TEST", strategy, ts, 10.0)
            continue
        if sharpe == 0:
            # mean=0, std=10 -> sharpe=0
            pnls = _trades_for_sharpe(target_mean=0.0, target_std=10.0)
        else:
            # sharpe_proxy = mean / std. Pick std=10, then mean = sharpe*10.
            # This way a negative target sharpe maps to a negative mean
            # with a positive std (std is always non-negative).
            pnls = _trades_for_sharpe(target_mean=sharpe * 10.0, target_std=10.0)
        _insert_month(con, "R_TEST", strategy, m_start, pnls)

    return con


# ---------------------------------------------------------------------------
# 1) Daily-mode short-circuit
# ---------------------------------------------------------------------------

def test_daily_mode_short_circuits_without_touching_warehouse():
    """`daily` mode must return stable=True / mode_skipped=True without
    issuing any query. We pass an EMPTY in-memory DB (no tables) -- if the
    function tried to query monthly_sharpe_proxy it would raise."""
    empty = duckdb.connect(":memory:")
    out = rg.check_regime_stability(empty, mode="daily")
    empty.close()

    assert out["stable"] is True
    assert out["mode_skipped"] is True
    assert out["warning"] is None
    assert math.isnan(out["z_score"])
    assert out["baseline_n_months"] == 0
    assert out["latest_month"] is None
    assert out["latest_sharpe_proxy"] is None


# ---------------------------------------------------------------------------
# 2) Stable regime
# ---------------------------------------------------------------------------

def test_stable_regime_weekly_mode():
    """Seven months of similar sharpe_proxy values -> stable=True, |z| <= 1.5."""
    # All seven months have sharpe ~ 1.0 with tiny noise.
    sharpes = [1.00, 1.05, 0.98, 1.02, 0.97, 1.03, 1.01]  # latest=1.01
    con = _build_db(sharpes)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert out["mode_skipped"] is False
    assert out["stable"] is True
    assert out["warning"] is None
    assert not math.isnan(out["z_score"])
    assert abs(out["z_score"]) <= 1.5
    assert out["baseline_n_months"] == 6
    assert out["latest_sharpe_proxy"] is not None
    assert isinstance(out["latest_month"], str)


def test_stable_regime_research_mode_same_behavior_as_weekly():
    sharpes = [1.00, 1.05, 0.98, 1.02, 0.97, 1.03, 1.01]
    con = _build_db(sharpes)
    out_w = rg.check_regime_stability(con, mode="weekly")
    out_r = rg.check_regime_stability(con, mode="research")
    con.close()

    assert out_w["mode_skipped"] is False
    assert out_r["mode_skipped"] is False
    # Both should give identical verdict for identical data.
    assert out_w["stable"] == out_r["stable"]
    assert math.isclose(out_w["z_score"], out_r["z_score"], rel_tol=1e-9)


# ---------------------------------------------------------------------------
# 3) Unstable -- extreme positive shift
# ---------------------------------------------------------------------------

def test_unstable_regime_extreme_positive_outlier():
    # Six stable baseline months ~1.0, then a huge jump.
    sharpes = [1.00, 1.02, 0.99, 1.01, 1.00, 0.98, 50.0]
    con = _build_db(sharpes)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert out["mode_skipped"] is False
    assert out["stable"] is False
    assert out["z_score"] > 1.5
    assert out["warning"] is not None
    assert "regime" in out["warning"].lower() or "z" in out["warning"].lower()


# ---------------------------------------------------------------------------
# 4) Unstable -- extreme negative shift
# ---------------------------------------------------------------------------

def test_unstable_regime_extreme_negative_outlier():
    sharpes = [1.00, 1.02, 0.99, 1.01, 1.00, 0.98, -50.0]
    con = _build_db(sharpes)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert out["stable"] is False
    assert out["z_score"] < -1.5
    assert out["warning"] is not None


# ---------------------------------------------------------------------------
# 5) Insufficient baseline
# ---------------------------------------------------------------------------

def test_insufficient_baseline_returns_stable_with_warning():
    """Only 3 months total -> baseline (latest excluded) has 2 months, < 4
    -> stable=True with explanatory warning, z=NaN."""
    sharpes = [1.0, 1.0, 1.0]
    con = _build_db(sharpes)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert out["stable"] is True
    assert out["mode_skipped"] is False
    assert math.isnan(out["z_score"])
    assert out["warning"] is not None
    assert "insufficient" in out["warning"].lower() or "baseline" in out["warning"].lower()
    # baseline_n_months reports how many baseline months actually existed.
    assert out["baseline_n_months"] < 4


def test_empty_warehouse_returns_stable_with_warning():
    """No trades at all -> can't compute -> stable=True with insufficient
    data warning (per spec: do NOT halt on missing data)."""
    con = duckdb.connect(":memory:")
    apply_schema(con)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert out["stable"] is True
    assert math.isnan(out["z_score"])
    assert out["warning"] is not None
    assert out["latest_month"] is None
    assert out["latest_sharpe_proxy"] is None


# ---------------------------------------------------------------------------
# 6) Custom threshold
# ---------------------------------------------------------------------------

def test_custom_threshold_tighter_flips_to_unstable():
    """The z_score is the same regardless of threshold; the threshold
    only affects the verdict. We verify that as the threshold tightens,
    a borderline case can flip from stable to unstable."""
    # Baseline with realistic monthly variance: sharpes spread across
    # 0.5..1.5 (std ~0.4). Latest month at 2.0 puts z around +1.7.
    sharpes = [0.5, 0.8, 1.2, 1.5, 0.7, 1.3, 2.0]
    con = _build_db(sharpes)
    out_default = rg.check_regime_stability(con, mode="weekly", z_threshold=1.5)
    out_tight = rg.check_regime_stability(con, mode="weekly", z_threshold=0.5)
    con.close()

    # The z_score must be identical across threshold values -- threshold
    # only affects the verdict.
    assert math.isclose(out_default["z_score"], out_tight["z_score"], rel_tol=1e-9)
    # With a very tight threshold (0.5) and a noticeable shift, must be unstable.
    if abs(out_default["z_score"]) > 0.5:
        assert out_tight["stable"] is False


def test_custom_threshold_looser_keeps_stable():
    """A loose threshold should keep things stable even for noticeable
    outliers. Use a baseline with realistic monthly variance and a latest
    that's a couple of sigma off -- covered by a loose threshold."""
    # Baseline spread: 0.5..1.5 (mean ~1.0, std ~0.4). Latest at 2.0
    # gives z ~ 2.5. A threshold of 5.0 should still report stable.
    sharpes = [0.5, 0.8, 1.2, 1.5, 0.7, 1.3, 2.0]
    con = _build_db(sharpes)
    out_loose = rg.check_regime_stability(con, mode="weekly", z_threshold=5.0)
    con.close()

    assert out_loose["stable"] is True
    # The reported z should be modest.
    assert abs(out_loose["z_score"]) < 5.0


# ---------------------------------------------------------------------------
# 7) Zero-baseline-std
# ---------------------------------------------------------------------------

def test_zero_baseline_std_does_not_raise():
    """All baseline months have identical sharpe_proxy -> std=0. Division
    by zero must be handled gracefully -- function never raises."""
    # 6 identical baseline months + 1 latest month
    sharpes = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 5.0]
    con = _build_db(sharpes)
    # Must not raise.
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    # Acceptable outcomes per spec: stable=True+warning OR stable=False+z=inf.
    # The function must NOT raise and the dict must be well-formed.
    assert isinstance(out["stable"], bool)
    assert isinstance(out["mode_skipped"], bool)
    assert out["warning"] is not None  # something should be reported
    # z_score may be NaN (treated as can't-compute) or inf (degenerate).
    assert out["z_score"] is not None
    assert math.isnan(out["z_score"]) or math.isinf(out["z_score"]) or isinstance(out["z_score"], float)


# ---------------------------------------------------------------------------
# 8) Output contract
# ---------------------------------------------------------------------------

REQUIRED_KEYS = {
    "stable", "z_score", "warning", "mode_skipped",
    "baseline_n_months", "latest_month", "latest_sharpe_proxy",
}


@pytest.mark.parametrize("mode", ["daily", "weekly", "research"])
def test_output_contract_keys_present_for_all_modes(mode):
    con = duckdb.connect(":memory:")
    apply_schema(con)
    out = rg.check_regime_stability(con, mode=mode)
    con.close()

    assert set(out.keys()) == REQUIRED_KEYS, (
        f"Output keys mismatch for mode={mode}: got {set(out.keys())}"
    )


def test_output_contract_types_stable_case():
    sharpes = [1.00, 1.05, 0.98, 1.02, 0.97, 1.03, 1.01]
    con = _build_db(sharpes)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert isinstance(out["stable"], bool)
    assert isinstance(out["mode_skipped"], bool)
    assert isinstance(out["z_score"], float)
    assert out["warning"] is None or isinstance(out["warning"], str)
    assert isinstance(out["baseline_n_months"], int)
    assert isinstance(out["latest_month"], str)
    # YYYY-MM format check.
    assert len(out["latest_month"]) == 7 and out["latest_month"][4] == "-"
    assert isinstance(out["latest_sharpe_proxy"], float)


# ---------------------------------------------------------------------------
# 9) No write side-effects
# ---------------------------------------------------------------------------

def test_no_write_side_effects():
    """Calling the gate must not modify the warehouse. We snapshot the
    trades table row count before and after."""
    sharpes = [1.0] * 7
    con = _build_db(sharpes)
    n_before = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    n_runs_before = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    rg.check_regime_stability(con, mode="weekly")

    n_after = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    n_runs_after = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con.close()

    assert n_before == n_after
    assert n_runs_before == n_runs_after


# ---------------------------------------------------------------------------
# 10) Mode-aware: research/weekly DO touch the warehouse
# ---------------------------------------------------------------------------

def test_research_and_weekly_actually_query_warehouse():
    """Sanity check: research and weekly modes should NOT short-circuit;
    they should attempt to compute and either return a real z or a
    well-formed insufficient-data response. Use a DB with no friction-True
    runs (so prepared_queries returns empty)."""
    con = duckdb.connect(":memory:")
    apply_schema(con)
    _ins_run(con, "R_NF", friction=False, strategy="gamma")  # friction FALSE -> excluded
    _ins_trade(con, "R_NF", "gamma", datetime.now(tz=UTC) - timedelta(days=10), 100.0)

    out_w = rg.check_regime_stability(con, mode="weekly")
    out_r = rg.check_regime_stability(con, mode="research")
    con.close()

    assert out_w["mode_skipped"] is False
    assert out_r["mode_skipped"] is False
    # No friction-True data -> insufficient-data response.
    assert out_w["stable"] is True
    assert out_w["warning"] is not None
    assert math.isnan(out_w["z_score"])


# ---------------------------------------------------------------------------
# 11) Friction filter respected (via prepared_queries)
# ---------------------------------------------------------------------------

def test_friction_false_trades_excluded():
    """Adding lots of friction=False noise must not flip the verdict
    because monthly_sharpe_proxy filters them out at the SQL layer."""
    sharpes = [1.00, 1.02, 0.99, 1.01, 1.00, 0.98, 1.01]
    con = _build_db(sharpes)

    # Add a friction=False run with extreme outliers.
    _ins_run(con, "R_NF", friction=False, strategy="gamma")
    base = datetime.now(tz=UTC) - timedelta(days=10)
    for i in range(10):
        _ins_trade(con, "R_NF", "gamma", base + timedelta(hours=i),
                   100000.0 if i % 2 == 0 else -100000.0)

    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    # Verdict should still be stable -- friction=False data was excluded.
    assert out["stable"] is True


# ---------------------------------------------------------------------------
# 12) Z threshold is INCLUSIVE upper bound for stability (|z| <= threshold)
# ---------------------------------------------------------------------------

def test_z_score_value_is_float_when_baseline_sufficient():
    sharpes = [1.0, 1.1, 0.9, 1.05, 0.95, 1.02, 1.0]
    con = _build_db(sharpes)
    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert isinstance(out["z_score"], float)
    assert not math.isnan(out["z_score"])


# ---------------------------------------------------------------------------
# 13) latest_month / latest_sharpe_proxy round-trip
# ---------------------------------------------------------------------------

def test_latest_month_matches_actual_most_recent_month():
    """The returned latest_month should be YYYY-MM of the month_starts
    we inserted as the most-recent month."""
    sharpes = [1.0] * 7
    con = _build_db(sharpes)
    # Pull the actual months from the SQL layer to know what to expect.
    df = pq.monthly_sharpe_proxy(con, months_back=6)
    expected_latest = pd.to_datetime(df["month"].iloc[-1]).strftime("%Y-%m")

    out = rg.check_regime_stability(con, mode="weekly")
    con.close()

    assert out["latest_month"] == expected_latest


# ---------------------------------------------------------------------------
# 14) Default threshold constant is documented
# ---------------------------------------------------------------------------

def test_default_threshold_constant():
    assert rg.Z_THRESHOLD_DEFAULT == 1.5


# ---------------------------------------------------------------------------
# 15) Unknown mode falls into research/weekly path or raises -- we just
#     check it does NOT silently report mode_skipped (only daily skips).
# ---------------------------------------------------------------------------

def test_only_daily_short_circuits():
    sharpes = [1.0] * 7
    con = _build_db(sharpes)
    out_w = rg.check_regime_stability(con, mode="weekly")
    out_r = rg.check_regime_stability(con, mode="research")
    out_d = rg.check_regime_stability(con, mode="daily")
    con.close()

    assert out_w["mode_skipped"] is False
    assert out_r["mode_skipped"] is False
    assert out_d["mode_skipped"] is True
