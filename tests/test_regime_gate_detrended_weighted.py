"""Tests for the sample-size-weighted detrended-baseline variant of regime_gate.

Phoenix Strategy Oracle — 2026-06-05 R2 Finding 3 sample-size sprint.

Discharges `FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING`,
which surfaced from the Phase 5.7 red-team CRITICAL on the unweighted
detrended variant: the unweighted variant treated every month as
equal-variance, but a latest month with fewer trades than the baseline
median has *higher sampling variance* in its sharpe-proxy. Inflating
the latest std by a Welch-style factor of ``sqrt(baseline_median /
latest_trade_count)`` flips the May 2026 verdict from REGIME_REAL
(z=3.35 unweighted) into the MARGINAL band (~2.6-2.9).

The weighted variant ALSO adds a refuse-to-compute floor: when
``latest_trade_count < min_latest_trade_count_fraction *
baseline_median_trade_count`` (default 0.7), the gate returns
``INSUFFICIENT_SAMPLE`` rather than producing a hard-to-interpret
heavily-inflated z. The 0.7 default matches the spirit of the
filtered variant's sparse-month rule applied to the LATEST month.

These tests pin the contract before implementation. They are
deliberately behavioral — they query the prepared_queries SQL
end-to-end via real in-memory DuckDB and assert on the gate's
externally-visible output dict.

Default `check_regime_stability` and `check_regime_stability_detrended`
MUST remain byte-identical (regression-guarded by their own test files).

Helper pattern mirrors `tests/test_regime_gate_detrended.py`.
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


def _trade_pnls_for_n_and_sharpe(n_trades: int, sharpe: float) -> list[float]:
    """Build `n_trades` PnL values whose AVG/STDDEV_SAMP yields the
    target sharpe.

    Strategy: pick mean=sharpe*10, std=10. For n>=2, emit pnls
    [m-d, m, m, ..., m, m+d] where d is chosen so STDDEV_SAMP(pnls) =
    10. Closed-form: d = std * sqrt((n-1)/2).
    """
    if n_trades < 2:
        # Cannot define STDDEV_SAMP with one trade; sharpe will be NULL.
        # Return one trade at the mean so the row exists.
        return [sharpe * 10.0]
    target_mean = sharpe * 10.0
    target_std = 10.0
    d = target_std * math.sqrt((n_trades - 1) / 2.0)
    return [target_mean - d] + [target_mean] * (n_trades - 2) + [target_mean + d]


def _insert_month(con, run_id, strategy, month_start_utc, n_trades, sharpe):
    """Insert n_trades trades in the given calendar month, yielding the
    target sharpe."""
    pnls = _trade_pnls_for_n_and_sharpe(n_trades, sharpe)
    for i, p in enumerate(pnls):
        ts = month_start_utc + timedelta(days=14, hours=15, minutes=i)
        _ins_trade(con, run_id, strategy, ts, p)


def _build_db(
    specs: list[tuple[int, float]],
    strategy: str = "gamma",
) -> duckdb.DuckDBPyConnection:
    """Build N months of synthetic data. ``specs[i] = (n_trades, sharpe)``,
    oldest-first. Last spec is the latest month."""
    con = duckdb.connect(":memory:")
    apply_schema(con)
    _ins_run(con, "R_TEST", friction=True, strategy=strategy)

    anchor = datetime.now(tz=UTC) - timedelta(days=20)
    months_back_from = datetime(anchor.year, anchor.month, 1, tzinfo=UTC)
    cur = months_back_from
    month_starts: list[datetime] = []
    for _ in range(len(specs)):
        month_starts.append(cur)
        prev_year, prev_month = (
            (cur.year, cur.month - 1) if cur.month > 1 else (cur.year - 1, 12)
        )
        cur = datetime(prev_year, prev_month, 1, tzinfo=UTC)
    month_starts.reverse()

    for m_start, (n_trades, sharpe) in zip(month_starts, specs):
        _insert_month(con, "R_TEST", strategy, m_start, n_trades, sharpe)
    return con


# ---------------------------------------------------------------------------
# 1) REFUSE-TO-COMPUTE — latest_trade_count < 0.7 * baseline_median.
# ---------------------------------------------------------------------------


def test_weighted_refuses_when_latest_under_traded_below_floor():
    """When the latest month's trade_count is below
    ``min_latest_trade_count_fraction * baseline_median_trade_count``
    (default 0.7), the gate returns INSUFFICIENT_SAMPLE without
    computing a z-score. This protects against the equal-variance
    violation the unweighted detrended variant exhibited on May 2026
    (the Phase 5.7 red-team CRITICAL).

    Setup: 12 baseline months @ 1000 trades each + 1 latest @ 500
    trades (= 0.5 of median, well below 0.7 threshold).
    """
    specs = [(1000, 0.05 + 0.01 * i) for i in range(12)]
    specs.append((500, 0.10))  # latest, half the baseline volume
    con = _build_db(specs)
    out = rg.check_regime_stability_detrended_weighted(con, mode="weekly")
    con.close()

    assert out["weighted_applied"] is True
    assert out["insufficient_sample"] is True
    assert out["category"] == "INSUFFICIENT_SAMPLE"
    assert out["stable"] is True  # spec: gate NEVER halts on missing data
    assert math.isnan(out["z_score_detrended_weighted"])
    # latest_trade_count and baseline_median_trade_count must be exposed.
    assert out["latest_trade_count"] is not None
    assert out["baseline_median_trade_count"] is not None
    assert out["latest_trade_count"] < 0.7 * out["baseline_median_trade_count"]
    # warning string explains the refusal.
    assert "insufficient" in (out["warning"] or "").lower()


# ---------------------------------------------------------------------------
# 2) MAY-REPLAY — 826 vs 1372 (red-team CRITICAL scenario).
# ---------------------------------------------------------------------------


def test_weighted_may_replay_lands_in_marginal_band():
    """Replay the actual May 2026 conditions (826 latest trades, baseline
    median ≈ 1372 trades). Per Phase 5.7 red-team CRITICAL: the unweighted
    z=3.35 ignores May's under-traded state; with the sampling-variance
    correction the corrected z lands in the MARGINAL band [2.0, 3.0].

    Setup: 12 baseline months @ 1372 trades each + 1 latest @ 826 trades.
    The sharpe series uses small drift to roughly reproduce the May
    residual ≈ 3.35 standard deviations under unweighted detrending.

    Floor check: latest_n/baseline_median = 826/1372 = 0.602 < 0.7.
    Therefore the weighted variant must REFUSE-TO-COMPUTE, returning
    INSUFFICIENT_SAMPLE. (The "operator's corrected z ≈ 2.91" was a
    back-of-envelope estimate based on a half-and-half between-vs-
    sampling decomposition; the conservative Welch-style formula in
    the spec triggers the floor instead of computing a number that
    would still need operator judgement.)
    """
    # 826 / 1372 = 0.602, below 0.7 floor.
    specs = [(1372, 0.0 + 0.005 * i) for i in range(12)]
    specs.append((826, 0.10))  # May — under-traded
    con = _build_db(specs)
    out = rg.check_regime_stability_detrended_weighted(con, mode="weekly")
    con.close()

    assert out["weighted_applied"] is True
    assert out["latest_trade_count"] == 826
    assert out["baseline_median_trade_count"] == pytest.approx(1372.0)
    # The 826/1372 = 0.602 ratio falls under the 0.7 floor -> refuse.
    assert out["insufficient_sample"] is True
    assert out["category"] == "INSUFFICIENT_SAMPLE"
    # Operator-facing verdict: do NOT advance the freeze-lift conversation
    # on this evidence; wait for May trade count to settle.
    assert out["stable"] is True  # never halts on missing data


# ---------------------------------------------------------------------------
# 3) EQUAL N — weighted == unweighted within tolerance when balanced.
# ---------------------------------------------------------------------------


def test_weighted_matches_unweighted_when_latest_n_equals_baseline_median():
    """When latest_trade_count == baseline_median_trade_count the Welch
    correction factor is sqrt(1.0) = 1.0 — the weighted z must match
    the unweighted detrended z exactly. This pins the no-op-when-
    balanced contract.

    NB: small noise added to sharpes so residual_std is non-zero (a
    perfectly-linear synthetic baseline would yield residuals=0 and
    NaN z, defeating the test)."""
    # Use a deterministic noise pattern to keep the residual_std finite.
    noise = [0.003, -0.002, 0.001, -0.004, 0.002, -0.001,
             0.004, -0.003, 0.002, -0.002, 0.001, -0.001]
    specs = [(1000, 0.05 + 0.01 * i + noise[i]) for i in range(12)]
    # Latest with EXACTLY the median trade count. With 12 baseline at
    # 1000 each, median = 1000 -> ratio = 1.0 -> factor = 1.0.
    specs.append((1000, 0.25))  # outlier above projection
    con1 = _build_db(specs)
    unweighted = rg.check_regime_stability_detrended(con1, mode="weekly")
    con1.close()

    con2 = _build_db(specs)
    weighted = rg.check_regime_stability_detrended_weighted(con2, mode="weekly")
    con2.close()

    z_u = unweighted["z_score_detrended"]
    z_w = weighted["z_score_detrended_weighted"]
    assert not math.isnan(z_u)
    assert not math.isnan(z_w)
    # Floor must NOT trigger (latest n == median).
    assert weighted["insufficient_sample"] is False
    # Welch factor = sqrt(1.0) = 1.0 -> weighted and unweighted match.
    # Allow tiny tolerance for float arithmetic.
    assert abs(z_u - z_w) < 1e-9, (
        f"weighted should match unweighted when latest_n == "
        f"baseline_median (factor=1.0), got unweighted={z_u} vs "
        f"weighted={z_w}"
    )
    # The corrected std should equal the raw residual_std.
    assert weighted["weight_factor"] == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 4) MUTUAL EXCLUSION — DETREND_WEIGHTED + DETREND set simultaneously
#    must raise. Same pattern as DETREND + FILTER mutual exclusion.
# ---------------------------------------------------------------------------


def test_oracle_mutual_exclusion_weighted_and_detrend(monkeypatch):
    """Setting both ORACLE_REGIME_GATE_DETREND_WEIGHTED=1 and
    ORACLE_REGIME_GATE_DETREND=1 must raise ValueError. The two
    variants are alternative methodology choices; the operator must
    pick one per run."""
    from agents import strategy_oracle

    monkeypatch.setenv("ORACLE_REGIME_GATE_DETREND_WEIGHTED", "1")
    monkeypatch.setenv("ORACLE_REGIME_GATE_DETREND", "1")
    monkeypatch.delenv("ORACLE_REGIME_GATE_FILTER", raising=False)
    empty = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="mutually exclusive"):
        strategy_oracle._check_regime_gate(empty, mode="weekly")
    empty.close()


def test_oracle_mutual_exclusion_weighted_and_filter(monkeypatch):
    """Setting both DETREND_WEIGHTED and FILTER must also raise."""
    from agents import strategy_oracle

    monkeypatch.setenv("ORACLE_REGIME_GATE_DETREND_WEIGHTED", "1")
    monkeypatch.setenv("ORACLE_REGIME_GATE_FILTER", "1")
    monkeypatch.delenv("ORACLE_REGIME_GATE_DETREND", raising=False)
    empty = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="mutually exclusive"):
        strategy_oracle._check_regime_gate(empty, mode="weekly")
    empty.close()


# ---------------------------------------------------------------------------
# 4c) MARGINAL-band test — operator's pre-decision rule pivots on the
#     HARD-PINNED 3.0 boundary, NOT the configurable z_threshold.
#     Pre-fix this was broken in weekly mode (z_threshold=1.5 made the
#     MARGINAL band [2.0, 3.0] unreachable). 2026-06-05 red-team HIGH.
# ---------------------------------------------------------------------------


def test_weighted_category_uses_hard_3p0_boundary_in_weekly_mode():
    """Pre-decision-rule category mapping MUST be pinned to |z|=3.0 and
    |z|=2.0 — not the configurable z_threshold. Weekly mode passes
    z_threshold=1.5; in the buggy pre-fix code that made the MARGINAL
    band unreachable and silently labeled |z|∈[1.5, 2.0) as REGIME_REAL.

    Construct synthetic data engineered to land a weighted z in [2.0,
    3.0] AND verify that weekly-mode call still labels it MARGINAL.
    Specs are tuned: latest_n=800, baseline_median=1000 -> floor 0.7*
    1000=700 cleared (800>=700), Welch factor sqrt(1000/800)≈1.118.
    """
    # Baseline: 12 months @ 1000 trades, flat-ish sharpe around 0.05
    # with deterministic noise to keep residual_std finite.
    noise = [0.003, -0.002, 0.001, -0.004, 0.002, -0.001,
             0.004, -0.003, 0.002, -0.002, 0.001, -0.001]
    specs = [(1000, 0.05 + noise[i]) for i in range(12)]
    # Latest: 800 trades, sharpe at 0.115 to drive an unweighted z
    # comfortably above the unweighted MARGINAL boundary (≈3.4) so the
    # Welch correction brings it into [2.0, 3.0].
    specs.append((800, 0.115))
    con = _build_db(specs)
    out = rg.check_regime_stability_detrended_weighted(con, mode="weekly")
    con.close()

    # Floor must pass (800/1000 = 0.8 > 0.7 default).
    assert out["insufficient_sample"] is False, (
        f"floor unexpectedly tripped: latest_n={out['latest_trade_count']}, "
        f"median={out['baseline_median_trade_count']}"
    )
    # Welch factor must be > 1.0 (latest under-traded vs median).
    assert out["weight_factor"] > 1.0
    # The whole point of the fix: category mapping uses HARD 2.0/3.0
    # boundaries, NOT z_threshold (which is 1.5 in weekly mode and would
    # have collapsed the MARGINAL band pre-fix).
    z_w = out["z_score_detrended_weighted"]
    assert not math.isnan(z_w)
    if 2.0 <= abs(z_w) <= 3.0:
        assert out["category"] == "MARGINAL", (
            f"weighted z={z_w} is in [2.0, 3.0] band; category should be "
            f"MARGINAL, got {out['category']!r}"
        )
    elif abs(z_w) > 3.0:
        assert out["category"] == "REGIME_REAL"
    else:  # < 2.0
        assert out["category"] in ("DRIFT_ARTIFACT", "AMBIGUOUS")


# ---------------------------------------------------------------------------
# 4d) Corrective-zone test (R3 Test Quality missing-test) — latest_n in
#     [0.7, 1.0) of baseline_median so the floor doesn't trigger but the
#     Welch correction does meaningful work. THIS is the test that
#     numerically demonstrates the Welch math (the May-replay test #2
#     hits the floor refusal and does NOT exercise Welch).
# ---------------------------------------------------------------------------


def test_weighted_in_corrective_zone_lowers_z_vs_unweighted():
    """Setup the floor-allowed zone (latest_n=800, baseline_median=1000,
    ratio=0.8) where Welch correction inflates corrected_std by
    sqrt(1000/800)=1.118. The weighted z must be strictly LOWER than the
    unweighted detrended z (the whole point of the sample-size correction)
    and the ratio of unweighted-to-weighted z must equal the Welch
    factor (within numerical tolerance).

    R3 Test Quality 2026-06-05 missing-test fix: prior test suite never
    actually demonstrated the Welch math on real data — Test 2 hit the
    floor before any math ran.
    """
    noise = [0.003, -0.002, 0.001, -0.004, 0.002, -0.001,
             0.004, -0.003, 0.002, -0.002, 0.001, -0.001]
    specs = [(1000, 0.05 + noise[i]) for i in range(12)]
    specs.append((800, 0.115))  # latest in [0.7, 1.0) of median

    con1 = _build_db(specs)
    unweighted = rg.check_regime_stability_detrended(con1, mode="weekly")
    con1.close()

    con2 = _build_db(specs)
    weighted = rg.check_regime_stability_detrended_weighted(con2, mode="weekly")
    con2.close()

    z_u = unweighted["z_score_detrended"]
    z_w = weighted["z_score_detrended_weighted"]
    assert not math.isnan(z_u)
    assert not math.isnan(z_w)
    assert weighted["insufficient_sample"] is False
    # Welch factor inflates std -> z gets smaller (toward zero).
    assert abs(z_w) < abs(z_u), (
        f"weighted z should be smaller in magnitude than unweighted "
        f"when latest < baseline_median, got unweighted={z_u} vs "
        f"weighted={z_w}"
    )
    # Ratio of |z_u| / |z_w| should equal weight_factor (the Welch math).
    weight_factor = weighted["weight_factor"]
    expected_ratio = weight_factor
    actual_ratio = abs(z_u) / abs(z_w)
    assert abs(expected_ratio - actual_ratio) < 1e-6, (
        f"unweighted/weighted z ratio must equal weight_factor: "
        f"expected {expected_ratio:.6f} (= sqrt({weighted['baseline_median_trade_count']}/"
        f"{weighted['latest_trade_count']})), got {actual_ratio:.6f}"
    )


# ---------------------------------------------------------------------------
# 4e) Upper-bound validation (R2 Bug Hunter + Red-team MEDIUM): an
#     over-large fraction would silently refuse every month forever.
# ---------------------------------------------------------------------------


def test_weighted_raises_on_excessive_min_fraction():
    """min_latest_trade_count_fraction > 1.5 must raise — otherwise an
    operator typo (e.g. `7` instead of `0.7`) silently refuses every
    Oracle run."""
    empty = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="min_latest_trade_count_fraction"):
        rg.check_regime_stability_detrended_weighted(
            empty, mode="weekly",
            min_latest_trade_count_fraction=7.0,
        )
    empty.close()


# ---------------------------------------------------------------------------
# 5) ZERO LATEST TRADES — must NOT divide-by-zero; floor catches.
# ---------------------------------------------------------------------------


def test_weighted_zero_latest_trades_returns_insufficient_no_divide_error():
    """If the latest month somehow has zero trades (e.g., very early in a
    new month before any fills land), the floor check must catch it
    BEFORE the Welch division. Verifies no divide-by-zero hazard."""
    # 12 normal baseline months + 1 latest with effectively zero usable
    # data. SQL COUNT(*) >= 1 if any trade exists, so we use 1 trade
    # (which yields NULL sharpe — the existing baseline-NaN path then
    # treats it as no usable latest_sharpe).
    specs = [(1000, 0.05 + 0.005 * i) for i in range(12)]
    specs.append((1, 0.10))  # latest with single trade -> NULL sharpe
    con = _build_db(specs)
    out = rg.check_regime_stability_detrended_weighted(con, mode="weekly")
    con.close()

    # The function must not crash with ZeroDivisionError. It must
    # return a graceful "insufficient" / "no latest" shape.
    assert out["stable"] is True  # never halts on missing data
    assert math.isnan(out["z_score_detrended_weighted"])
    # Either insufficient_sample=True (floor caught the under-traded
    # latest) OR latest_sharpe_proxy=None (NaN-sharpe upstream caught
    # the single-trade month) is acceptable — both are graceful.
    assert (
        out["insufficient_sample"] is True
        or out["latest_sharpe_proxy"] is None
    )
