"""Tests for the R2 Finding 3 detrended-baseline variant of regime_gate.

Phoenix Strategy Oracle — 2026-06-05 R2 Finding 3 sprint.

R2 Finding 3 (gradual baseline drift): if the baseline contains a
gradual regime-improvement trend, the latest month's z against the
baseline mean+std is inflated by the trend itself, not by a genuine
regime shift. The detrended variant fits a linear trend to the
baseline sharpe-proxies, computes residuals, and runs the z-score on
the residual of the latest month against the residual std of the
baseline.

Pre-decision rule (per sprint spec):
- |z_detrended| > 3.0      -> REGIME SHIFT IS REAL (HALT stands, drift NOT cause)
- |z_detrended| < 2.0 AND r_squared > 0.5  -> HALT WAS DRIFT ARTIFACT
- |z_detrended| < 2.0 AND r_squared < 0.5  -> AMBIGUOUS (noisy baseline)
- 2.0 <= |z_detrended| <= 3.0  -> MARGINAL
- baseline_n < 4           -> INSUFFICIENT_BASELINE

These tests pin the function's externally-visible contract. Default
`check_regime_stability` and `check_regime_stability_with_filter` MUST
remain byte-identical (regression-guarded by their own test files).

Helper pattern mirrors `tests/test_regime_gate.py`.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from tools.warehouse.db import apply_schema

from analytics import regime_gate as rg


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
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
    """Symmetric triple for n=3 producing AVG=target_mean and
    STDDEV_SAMP=target_std."""
    if target_std == 0:
        return [target_mean, target_mean, target_mean]
    d = target_std
    return [target_mean - d, target_mean, target_mean + d]


def _insert_month(con, run_id, strategy, month_start_utc, pnls):
    for i, p in enumerate(pnls):
        ts = month_start_utc + timedelta(days=14, hours=15, minutes=i)
        _ins_trade(con, run_id, strategy, ts, p)


def _build_db_from_sharpes(
    monthly_sharpes: list[float], strategy: str = "gamma"
) -> duckdb.DuckDBPyConnection:
    """Build N months of data where month[i] has sharpe_proxy ==
    monthly_sharpes[i]. monthly_sharpes is ordered oldest-first; the
    last entry becomes the latest month."""
    con = duckdb.connect(":memory:")
    apply_schema(con)
    _ins_run(con, "R_TEST", friction=True, strategy=strategy)

    anchor = datetime.now(tz=UTC) - timedelta(days=20)
    months_back_from = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)
    cur = months_back_from
    month_starts: list[datetime] = []
    for _ in range(len(monthly_sharpes)):
        month_starts.append(cur)
        prev_year, prev_month = (
            (cur.year, cur.month - 1) if cur.month > 1 else (cur.year - 1, 12)
        )
        cur = datetime(prev_year, prev_month, 1, tzinfo=UTC)
    month_starts.reverse()

    for m_start, sharpe in zip(month_starts, monthly_sharpes):
        if isinstance(sharpe, float) and math.isnan(sharpe):
            # single trade -> NULL stddev -> NULL sharpe
            ts = m_start + timedelta(days=14, hours=15)
            _ins_trade(con, "R_TEST", strategy, ts, 10.0)
            continue
        # Use std=10 fixed scale; mean = sharpe * 10.
        pnls = _trades_for_sharpe(target_mean=sharpe * 10.0, target_std=10.0)
        _insert_month(con, "R_TEST", strategy, m_start, pnls)

    return con


# ---------------------------------------------------------------------------
# Pre-decision rule fixtures: build sharpe series with controlled trend.
# ---------------------------------------------------------------------------


def _trend_series(
    n_baseline: int, slope: float, intercept: float, noise_amplitude: float = 0.01
) -> list[float]:
    """Build a baseline sharpe series with a linear trend `intercept +
    slope*x` plus small alternating noise. Returns oldest-first."""
    import numpy as np
    rng = np.random.default_rng(42)  # deterministic for test repro
    noise = (rng.random(n_baseline) - 0.5) * 2 * noise_amplitude
    return [float(intercept + slope * i + noise[i]) for i in range(n_baseline)]


# ---------------------------------------------------------------------------
# 1) DRIFT ARTIFACT — linear baseline trend + latest matching projection.
#    z_detrended should be small; r_squared high.
# ---------------------------------------------------------------------------


def test_detrended_latest_matches_projection_yields_low_z():
    """A baseline with a clear linear trend AND a latest month that
    matches the trend's projection produces small z_detrended. This is
    the "HALT was drift artifact" verdict the sprint exists to surface.

    Build 12 baseline months on a clear trend (intercept=0.02, slope=0.02)
    with small noise (amplitude 0.005). Latest is on-projection at
    intercept + slope * baseline_n + tiny noise.
    """
    baseline_sharpes = _trend_series(
        n_baseline=12, slope=0.02, intercept=0.02, noise_amplitude=0.005
    )
    # Latest = intercept + slope * 12 = 0.02 + 0.24 = 0.26, within noise.
    latest = 0.26 + 0.003
    con = _build_db_from_sharpes(baseline_sharpes + [latest])
    out = rg.check_regime_stability_detrended(con, mode="weekly")
    con.close()

    assert out["mode_skipped"] is False
    # NB: the detrended path is independent of the filter path; it does NOT
    # expose `filter_applied` in its output dict (those keys belong to
    # `check_regime_stability_with_filter`).
    assert "filter_applied" not in out
    assert out["detrended_applied"] is True
    # On-projection latest with a clear trend AND low noise -> SMALL |z|
    # (well under 1 sigma). R3 Test Quality MEDIUM #1 fix (2026-06-05):
    # previous tolerance |z| < 3.0 would mistakenly PASS a buggy fit
    # producing z=2.9; tightened to |z| < 1.5 (still tolerant of finite
    # noise but catches order-of-magnitude bugs).
    z = out["z_score_detrended"]
    assert not math.isnan(z)
    assert abs(z) < 1.5, (
        f"on-projection latest with low-noise data should have |z| << 1, "
        f"got {z}. slope={out.get('baseline_slope')}, "
        f"r²={out.get('baseline_r_squared')}"
    )
    # Trend should be STRONG: low-noise synthetic data must fit well.
    # R3 Test Quality MEDIUM #1 fix: tightened r² threshold from 0.5 to 0.9.
    assert out["baseline_r_squared"] is not None
    assert out["baseline_r_squared"] > 0.9, (
        f"trend fit should be very strong with low-noise synthetic data, "
        f"got r²={out['baseline_r_squared']}"
    )
    # The gate should mark this stable.
    assert out["stable"] is True


# ---------------------------------------------------------------------------
# 2) GENUINE REGIME SHIFT — linear baseline trend + latest FAR above
#    projection. z_detrended should exceed threshold.
# ---------------------------------------------------------------------------


def test_detrended_latest_far_above_projection_yields_halt():
    """Same baseline as test 1 but latest is FAR above projection.
    z_detrended should exceed threshold -> HALT.
    """
    baseline_sharpes = _trend_series(
        n_baseline=12, slope=0.02, intercept=0.02, noise_amplitude=0.005
    )
    # Latest = projection + 30x the noise amplitude — well beyond the
    # baseline residual std.
    latest = 0.26 + 0.15
    con = _build_db_from_sharpes(baseline_sharpes + [latest])
    out = rg.check_regime_stability_detrended(con, mode="weekly")
    con.close()

    z = out["z_score_detrended"]
    assert not math.isnan(z)
    assert abs(z) > 3.0, (
        f"far-above-projection latest should HALT (|z|>3), got {z}. "
        f"latest_residual={out.get('latest_residual')}, "
        f"r²={out.get('baseline_r_squared')}"
    )
    assert out["stable"] is False


# ---------------------------------------------------------------------------
# 3) BACKWARD-COMPAT — zero trend + outlier latest should produce
#    z_detrended approximately equal to stock z (within tolerance).
# ---------------------------------------------------------------------------


def test_detrended_with_zero_trend_matches_stock_z_approximately():
    """When the baseline has no trend (slope ~ 0), detrending should
    reduce to "z of latest residual against residual std", which is
    essentially the same as "z of latest against baseline std" because
    residual std ~ baseline std and slope*x ~ 0.

    Build 12 flat baseline months (sharpe = 0.05 +/- noise) + outlier
    latest at sharpe = 0.30.
    """
    baseline_sharpes = _trend_series(
        n_baseline=12, slope=0.0, intercept=0.05, noise_amplitude=0.015
    )
    latest = 0.30
    con1 = _build_db_from_sharpes(baseline_sharpes + [latest])
    stock = rg.check_regime_stability(con1, mode="weekly")
    con1.close()

    con2 = _build_db_from_sharpes(baseline_sharpes + [latest])
    detrended = rg.check_regime_stability_detrended(con2, mode="weekly")
    con2.close()

    z_stock = stock["z_score"]
    z_det = detrended["z_score_detrended"]
    assert not math.isnan(z_stock)
    assert not math.isnan(z_det)
    # With flat baseline (slope ~ 0) the two z's should match within a
    # small relative tolerance. R3 Test Quality MEDIUM #2 fix
    # (2026-06-05): tightened from absolute < 1.0 (trivially true) to
    # relative < 5% — the ddof=2 vs ddof=1 difference on n=12 produces a
    # ~3% gap in residual_std which propagates proportionally into the
    # z difference. A sign error or wrong-ddof bug would produce >> 5%
    # relative gap.
    denom = max(abs(z_stock), 1.0)  # guard against tiny denominators
    relative_gap = abs(z_stock - z_det) / denom
    assert relative_gap < 0.05, (
        f"flat-baseline detrended z should match stock z within 5% "
        f"relative, got stock={z_stock} vs detrended={z_det} "
        f"(relative gap = {relative_gap:.3f})"
    )


# ---------------------------------------------------------------------------
# 4) INSUFFICIENT — baseline_n < _MIN_BASELINE_MONTHS (4).
# ---------------------------------------------------------------------------


def test_detrended_returns_insufficient_baseline_below_min():
    """With only 3 months of data (2 baseline + 1 latest), the detrended
    variant returns the stable-with-warning shape (gate never halts on
    missing data, per spec)."""
    con = _build_db_from_sharpes([0.05, 0.10, 0.15])
    out = rg.check_regime_stability_detrended(con, mode="weekly")
    con.close()

    assert out["stable"] is True
    assert math.isnan(out["z_score_detrended"])
    assert out["baseline_n_months"] < 4
    assert "Insufficient" in (out["warning"] or "")
    assert out["detrended_applied"] is True


# ---------------------------------------------------------------------------
# 5) DICT SHAPE — required diagnostic keys present on the applied path.
# ---------------------------------------------------------------------------


def test_detrended_output_dict_exposes_diagnostic_keys():
    """The detrended variant must expose baseline_slope, intercept,
    r_squared, residuals[], latest_residual, z_score_detrended,
    detrended_applied on every applied-path return."""
    baseline_sharpes = _trend_series(n_baseline=12, slope=0.01, intercept=0.05)
    con = _build_db_from_sharpes(baseline_sharpes + [0.17])
    out = rg.check_regime_stability_detrended(con, mode="weekly")
    con.close()

    expected_keys = {
        "stable", "z_score", "warning", "mode_skipped", "baseline_n_months",
        "latest_month", "latest_sharpe_proxy",
        # Detrended-specific:
        "detrended_applied", "baseline_slope", "baseline_intercept",
        "baseline_r_squared", "residuals", "latest_residual",
        "z_score_detrended",
    }
    missing = expected_keys - set(out.keys())
    assert not missing, f"missing diagnostic keys: {sorted(missing)}"

    assert isinstance(out["residuals"], list)
    assert len(out["residuals"]) == out["baseline_n_months"]
    assert isinstance(out["baseline_slope"], float)
    assert isinstance(out["baseline_intercept"], float)
    assert 0.0 <= out["baseline_r_squared"] <= 1.0


# ---------------------------------------------------------------------------
# 6) DAILY MODE — short-circuit, same shape as stock daily.
# ---------------------------------------------------------------------------


def test_detrended_daily_mode_short_circuits():
    """Daily mode must skip the gate entirely (matches stock behavior)."""
    empty = duckdb.connect(":memory:")
    out = rg.check_regime_stability_detrended(empty, mode="daily")
    empty.close()

    assert out["stable"] is True
    assert out["mode_skipped"] is True
    assert math.isnan(out["z_score_detrended"])
    assert out["detrended_applied"] is False


# ---------------------------------------------------------------------------
# 7) DEGENERATE — all baseline sharpes identical -> zero slope, zero
#    residual std -> graceful NaN z, not a crash.
# ---------------------------------------------------------------------------


def test_detrended_degenerate_zero_residual_std_returns_nan_z():
    """When the baseline is perfectly flat (all sharpe identical), the
    residual std is zero and z is undefined. Must return stable=True
    with a warning explaining the degeneracy (matches stock zero-std
    fallback)."""
    baseline_sharpes = [0.1] * 12  # perfectly flat
    con = _build_db_from_sharpes(baseline_sharpes + [0.2])
    out = rg.check_regime_stability_detrended(con, mode="weekly")
    con.close()

    assert out["stable"] is True
    assert math.isnan(out["z_score_detrended"])
    assert "zero" in (out["warning"] or "").lower() or "degener" in (out["warning"] or "").lower()


# ---------------------------------------------------------------------------
# 8) MUTUAL EXCLUSION — Oracle's _check_regime_gate must raise when
#    both ORACLE_REGIME_GATE_FILTER and _DETREND are set.
# ---------------------------------------------------------------------------


def test_oracle_mutual_exclusion_filter_and_detrend(monkeypatch):
    """Setting both ORACLE_REGIME_GATE_FILTER and ORACLE_REGIME_GATE_
    DETREND must raise ValueError. The two variants test different
    methodology questions and combining them produces meaningless
    cross-method results."""
    from agents import strategy_oracle

    monkeypatch.setenv("ORACLE_REGIME_GATE_FILTER", "1")
    monkeypatch.setenv("ORACLE_REGIME_GATE_DETREND", "1")
    empty = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="mutually exclusive"):
        strategy_oracle._check_regime_gate(empty, mode="weekly")
    empty.close()


# ---------------------------------------------------------------------------
# 9) Oracle wiring — env var routes through detrended variant.
# ---------------------------------------------------------------------------


def test_detrended_drift_artifact_scenario_collapses_z():
    """R3 Test Quality missing-test (2026-06-05) — the SPRINT'S WHOLE
    REASON FOR EXISTING. The "HALT was drift artifact" scenario:

    - Baseline has a strong linear trend.
    - Latest month falls ON the trend's projection (no anomaly vs trend).
    - Stock gate sees `latest >> baseline_mean` and reports inflated z
      (the trend pollutes the baseline mean+std).
    - Detrended gate computes `latest_residual ≈ 0` -> z collapses to
      near zero.

    This is the exact scenario the sprint exists to surface. The
    detrended z must be MUCH SMALLER than the stock z.

    Note: the relationship `detrended_z < stock_z` does NOT hold in
    general — when the latest is FAR from the trend's projection AND r²
    is high, the residual_std collapses so detrended z can EXCEED stock
    z (more sensitive, not less). This test is specifically the drift-
    artifact case.
    """
    baseline_sharpes = _trend_series(
        n_baseline=12, slope=0.025, intercept=0.0, noise_amplitude=0.005
    )
    # Latest ON projection (intercept + slope * 12 = 0.30) with tiny noise.
    latest = 0.30 + 0.003
    con1 = _build_db_from_sharpes(baseline_sharpes + [latest])
    stock = rg.check_regime_stability(con1, mode="weekly")
    con1.close()

    con2 = _build_db_from_sharpes(baseline_sharpes + [latest])
    detrended = rg.check_regime_stability_detrended(con2, mode="weekly")
    con2.close()

    z_stock = stock["z_score"]
    z_det = detrended["z_score_detrended"]
    assert not math.isnan(z_stock)
    assert not math.isnan(z_det)
    # Stock z is INFLATED by trend pollution (latest=0.30 vs baseline_mean
    # ~0.139 + std ~0.092 -> z ~ 1.7+). On a strong trend with on-
    # projection latest, the stock gate looks suspicious.
    assert abs(z_stock) > 1.0, (
        f"stock z should be inflated by trend pollution, got {z_stock}"
    )
    # Detrended z collapses to near-zero because latest matches projection.
    assert abs(z_det) < 1.5, (
        f"detrended z should collapse when latest matches projection, "
        f"got {z_det}. r²={detrended['baseline_r_squared']}"
    )
    # And the detrended z is MUCH smaller than the stock z — that's the
    # whole sprint's point on the drift-artifact branch.
    assert abs(z_det) < abs(z_stock), (
        f"detrended z should be << stock z when latest matches projection "
        f"on a strong-trend baseline (DRIFT_ARTIFACT scenario), got "
        f"stock={z_stock} vs detrended={z_det}"
    )
    # Trend should be visible.
    assert detrended["baseline_r_squared"] > 0.9, (
        f"strong-trend synthetic data should fit well, got "
        f"r²={detrended['baseline_r_squared']}"
    )


def test_detrended_z_sign_matches_residual_sign():
    """R3 Test Quality missing-test (2026-06-05): pin that a latest
    BELOW the projection yields NEGATIVE z (= regime got worse), ABOVE
    yields POSITIVE z (= regime got better). Catches sign errors in the
    residual formula."""
    baseline_sharpes = _trend_series(
        n_baseline=12, slope=0.01, intercept=0.05, noise_amplitude=0.005
    )
    # Projection at x=12: 0.05 + 0.12 = 0.17.
    # Latest below projection -> negative z.
    below = 0.17 - 0.08
    above = 0.17 + 0.08

    con_b = _build_db_from_sharpes(baseline_sharpes + [below])
    z_below = rg.check_regime_stability_detrended(con_b, mode="weekly")["z_score_detrended"]
    con_b.close()

    con_a = _build_db_from_sharpes(baseline_sharpes + [above])
    z_above = rg.check_regime_stability_detrended(con_a, mode="weekly")["z_score_detrended"]
    con_a.close()

    assert z_below < 0, f"below-projection should yield negative z, got {z_below}"
    assert z_above > 0, f"above-projection should yield positive z, got {z_above}"


def test_oracle_routes_to_detrended_when_env_var_set(monkeypatch):
    """With ORACLE_REGIME_GATE_DETREND=1, the orchestrator's
    _check_regime_gate wrapper must call the detrended variant — verifiable
    by checking the output dict has the detrended-specific keys."""
    from agents import strategy_oracle

    monkeypatch.setenv("ORACLE_REGIME_GATE_DETREND", "1")
    monkeypatch.delenv("ORACLE_REGIME_GATE_FILTER", raising=False)

    baseline_sharpes = _trend_series(n_baseline=12, slope=0.01, intercept=0.05)
    con = _build_db_from_sharpes(baseline_sharpes + [0.17])
    out = strategy_oracle._check_regime_gate(con, mode="weekly")
    con.close()

    # Must be the detrended dict shape.
    assert "z_score_detrended" in out
    assert out.get("detrended_applied") is True
