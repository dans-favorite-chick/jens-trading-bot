"""Tests for analytics/compute_engine.py.

Phoenix Strategy Oracle - Task 2.

Golden-number tests verify each statistical primitive against either a
hand-computed reference value (derived from the formulas in Bailey-LdP /
Harvey-Liu-Zhu papers) or a published example. Integration tests assemble
synthetic trades and check the panel structure end-to-end.

All synthetic data uses a fixed RNG seed so failures are reproducible.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from analytics import compute_engine as ce


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hand_psr(returns: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """Independent hand-implementation of PSR for cross-checking.

    Uses scipy.stats.skew/kurtosis (excess kurtosis) and the closed-form
    PSR from Bailey & Lopez de Prado 2012 Eq. (5).
    """
    r = np.asarray(returns, dtype=float)
    n = r.size
    if n < 2:
        return 0.0
    mu = r.mean()
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    sr = mu / sd
    sk = float(stats.skew(r, bias=False))
    kt = float(stats.kurtosis(r, fisher=True, bias=False))
    denom_sq = (1.0 - sk * sr + (kt / 4.0) * sr * sr) / (n - 1)
    if denom_sq <= 0:
        return float("nan")
    z = (sr - sr_benchmark) / math.sqrt(denom_sq)
    return float(stats.norm.cdf(z))


def _synth_returns_psr_062(seed: int = 7) -> np.ndarray:
    """Returns with mean ~0.0002, std ~0.01, n=252 (annual daily series).

    With sr_hat ~= 0.02 and skew/kurt ~ 0, PSR vs 0 should be ~0.62.
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(loc=0.0002, scale=0.01, size=252)
    # Force exact target moments so the test is repeatable.
    base = (base - base.mean()) / base.std(ddof=1)
    base = base * 0.01 + 0.0002
    return base


# ---------------------------------------------------------------------------
# PSR tests
# ---------------------------------------------------------------------------

class TestPSR:
    def test_psr_golden_normal_returns(self):
        """Mean 0.0002, std 0.01, n=252 -> PSR ~ 0.62.

        Bailey & Lopez de Prado 2012 Eq. (5). With near-symmetric returns
        and SR_hat = 0.02 (per-step), denom ~= sqrt(1/251) ~= 0.0631,
        z ~= 0.317, Phi(0.317) ~= 0.624.
        """
        r = _synth_returns_psr_062()
        psr = ce.compute_psr(r, sr_benchmark=0.0)
        # Hand calc + scipy gives 0.624...
        assert abs(psr - 0.624) < 0.01

    def test_psr_matches_hand_implementation(self):
        rng = np.random.default_rng(42)
        for _ in range(5):
            r = rng.normal(0.001, 0.02, size=200)
            assert abs(ce.compute_psr(r) - _hand_psr(r)) < 1e-9

    def test_psr_empty_returns_zero(self):
        assert ce.compute_psr(np.array([])) == 0.0

    def test_psr_zero_std_returns_zero(self):
        assert ce.compute_psr(np.full(50, 5.0)) == 0.0

    def test_psr_in_unit_interval(self):
        rng = np.random.default_rng(0)
        for _ in range(10):
            r = rng.normal(0, 1, size=100)
            v = ce.compute_psr(r)
            assert 0.0 <= v <= 1.0

    def test_psr_against_benchmark_lower(self):
        """Beating a *lower* benchmark must give a higher PSR than beating zero."""
        r = _synth_returns_psr_062()
        psr_0 = ce.compute_psr(r, sr_benchmark=0.0)
        psr_neg = ce.compute_psr(r, sr_benchmark=-0.01)
        assert psr_neg > psr_0


# ---------------------------------------------------------------------------
# DSR tests
# ---------------------------------------------------------------------------

class TestDSR:
    def test_dsr_below_psr_with_multiple_trials(self):
        """Deflation must lower the score versus a single trial."""
        r = _synth_returns_psr_062()
        psr = ce.compute_psr(r)
        dsr = ce.compute_dsr(r, n_trials_effective=10)
        assert dsr < psr

    def test_dsr_more_trials_means_lower_score(self):
        r = _synth_returns_psr_062()
        dsr_10 = ce.compute_dsr(r, n_trials_effective=10)
        dsr_100 = ce.compute_dsr(r, n_trials_effective=100)
        assert dsr_100 < dsr_10

    def test_dsr_in_unit_interval(self):
        rng = np.random.default_rng(11)
        r = rng.normal(0.001, 0.02, size=250)
        v = ce.compute_dsr(r, n_trials_effective=25)
        assert 0.0 <= v <= 1.0

    def test_dsr_empty_returns_zero(self):
        assert ce.compute_dsr(np.array([]), n_trials_effective=5) == 0.0

    def test_dsr_n_trials_one_close_to_psr(self):
        """With N=1, the expected-maximum SR collapses to 0, so DSR ~= PSR."""
        r = _synth_returns_psr_062()
        psr = ce.compute_psr(r)
        dsr = ce.compute_dsr(r, n_trials_effective=1)
        # With N=1 the deflation benchmark formula goes to 0 (or undefined).
        # The implementation clamps N=1 -> sr_benchmark_max = 0, so DSR == PSR.
        assert abs(dsr - psr) < 1e-9


# ---------------------------------------------------------------------------
# MinTRL tests
# ---------------------------------------------------------------------------

class TestMinTRL:
    def test_min_trl_infeasible_when_sr_below_target(self):
        rng = np.random.default_rng(3)
        r = rng.normal(-0.001, 0.02, size=100)  # negative mean
        v = ce.compute_min_trl(r, target_sr=1.0, alpha=0.05)
        assert v >= 10**9

    def test_min_trl_finite_when_sr_above_target(self):
        # Construct returns so that per-step SR clearly exceeds target.
        rng = np.random.default_rng(7)
        r = rng.normal(0.05, 0.02, size=200)  # mean/std = ~2.5
        v = ce.compute_min_trl(r, target_sr=1.0, alpha=0.05)
        assert 1 < v < 10**9

    def test_min_trl_returns_int(self):
        r = np.random.default_rng(8).normal(0.05, 0.02, size=200)
        v = ce.compute_min_trl(r, target_sr=1.0, alpha=0.05)
        assert isinstance(v, int)

    def test_min_trl_smaller_alpha_requires_more_samples(self):
        rng = np.random.default_rng(9)
        r = rng.normal(0.05, 0.02, size=300)
        v_loose = ce.compute_min_trl(r, target_sr=1.0, alpha=0.10)
        v_tight = ce.compute_min_trl(r, target_sr=1.0, alpha=0.01)
        assert v_tight > v_loose


# ---------------------------------------------------------------------------
# HLZ t-stat tests
# ---------------------------------------------------------------------------

class TestHLZTStat:
    def test_hlz_iid_normal_matches_plain_t(self):
        """For iid normal series Newey-West variance ~= sample variance,
        so the HLZ t-stat should approximate the plain t-stat to within ~5%."""
        rng = np.random.default_rng(12)
        r = rng.normal(0.05, 1.0, size=400)
        plain_t = r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))
        hlz_t = ce.compute_hlz_tstat(r)
        assert abs(hlz_t - plain_t) / abs(plain_t) < 0.10

    def test_hlz_empty_returns_zero(self):
        assert ce.compute_hlz_tstat(np.array([])) == 0.0

    def test_hlz_zero_mean_close_to_zero(self):
        rng = np.random.default_rng(13)
        r = rng.normal(0.0, 1.0, size=500)
        hlz_t = ce.compute_hlz_tstat(r)
        assert abs(hlz_t) < 3.0

    def test_hlz_lag_zero_equals_plain_t(self):
        rng = np.random.default_rng(14)
        r = rng.normal(0.05, 1.0, size=200)
        # With lag=0 NW variance = gamma_0 = population variance ddof=0.
        # Plain t uses ddof=1 -> close but not identical. Assert within 5%.
        plain_t = r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))
        hlz_t = ce.compute_hlz_tstat(r, lag=0)
        assert abs(hlz_t - plain_t) / abs(plain_t) < 0.05


# ---------------------------------------------------------------------------
# Effective N (clustering) tests
# ---------------------------------------------------------------------------

class TestEffectiveN:
    def test_eff_n_highly_correlated_collapses(self):
        """5 series that are scaled copies of one -> 1 cluster."""
        rng = np.random.default_rng(20)
        base = rng.normal(size=200)
        trials = [base.copy(), base * 1.0001, base * 0.999, base + 0.001, base - 0.001]
        n_eff = ce.compute_effective_n(trials)
        assert n_eff <= 2

    def test_eff_n_uncorrelated_returns_all(self):
        rng = np.random.default_rng(21)
        trials = [rng.normal(size=200) for _ in range(5)]
        n_eff = ce.compute_effective_n(trials)
        assert n_eff >= 4

    def test_eff_n_single_trial_returns_one(self):
        assert ce.compute_effective_n([np.array([1.0, 2.0, 3.0])]) == 1

    def test_eff_n_empty_returns_one(self):
        assert ce.compute_effective_n([]) == 1

    def test_eff_n_two_anticorrelated_collapses(self):
        """Perfectly anti-correlated series -> |corr|=1, also collapses."""
        rng = np.random.default_rng(22)
        base = rng.normal(size=200)
        trials = [base, -base]
        n_eff = ce.compute_effective_n(trials)
        assert n_eff == 1


# ---------------------------------------------------------------------------
# BHY adjustment tests
# ---------------------------------------------------------------------------

class TestBHY:
    def test_bhy_single_trial_equals_raw_p(self):
        # Harmonic number c(1) = 1, so adjusted = raw.
        adj = ce.compute_bhy_p_adjusted(t_stat=3.0, n_trials=1)
        raw = 2.0 * (1.0 - stats.norm.cdf(3.0))
        assert abs(adj - raw) < 1e-9

    def test_bhy_large_t_low_p(self):
        adj = ce.compute_bhy_p_adjusted(t_stat=5.0, n_trials=10)
        assert adj < 0.05

    def test_bhy_clamped_at_one(self):
        adj = ce.compute_bhy_p_adjusted(t_stat=0.1, n_trials=100)
        assert adj <= 1.0

    def test_bhy_harmonic_scaling(self):
        # raw_p for t=2.0 is ~0.0455; with N=10, c(10) = 2.928968...,
        # adjusted = min(1, 0.0455 * 2.929) = 0.1334.
        adj = ce.compute_bhy_p_adjusted(t_stat=2.0, n_trials=10)
        raw = 2.0 * (1.0 - stats.norm.cdf(2.0))
        c_10 = sum(1.0 / k for k in range(1, 11))
        assert abs(adj - min(1.0, raw * c_10)) < 1e-9


# ---------------------------------------------------------------------------
# Strategy metrics integration
# ---------------------------------------------------------------------------

def _build_trades_df(pnls, session_dates=None):
    n = len(pnls)
    if session_dates is None:
        session_dates = [pd.Timestamp("2026-01-01") + pd.Timedelta(days=i)
                         for i in range(n)]
    return pd.DataFrame({
        "entry_ts": session_dates,
        "exit_ts": session_dates,
        "direction": ["LONG"] * n,
        "pnl_dollars": pnls,
        "pnl_ticks": [p / 0.5 for p in pnls],
        "mae_ticks": [1.0] * n,
        "mfe_ticks": [2.0] * n,
        "regime": ["trend"] * n,
        "tod_bucket": ["am"] * n,
        "session_date": session_dates,
        "market_open_minutes": [60.0] * n,
        "hold_minutes": [15.0] * n,
    })


class TestStrategyMetrics:
    def test_basic_panel_shape(self):
        rng = np.random.default_rng(101)
        pnls = rng.normal(5.0, 50.0, size=120).tolist()
        df = _build_trades_df(pnls)
        wfa = {"mean_is_pf": 2.0, "mean_oos_pf": 1.3}
        panel = ce.compute_strategy_metrics(df, wfa, n_trials_effective=10)
        assert "metrics" in panel
        assert "gates" in panel
        m = panel["metrics"]
        for key in ("n_trades", "psr", "dsr", "min_trl", "hlz_t_stat",
                    "bhy_p_adjusted", "profit_factor", "sortino", "calmar",
                    "max_drawdown_dollars", "oos_pf", "is_pf", "wfe_ratio",
                    "wfa_pass"):
            assert key in m, f"missing metric: {key}"
        g = panel["gates"]
        for key in ("n_floor", "n_medium", "n_high", "psr_0_90", "dsr_0_90",
                    "dsr_0_95", "hlz_3_0", "min_trl_met", "wfa_pass",
                    "all_pass_for_proposal", "failed_gates"):
            assert key in g, f"missing gate: {key}"

    def test_n_trades_matches_input(self):
        pnls = [10.0] * 50 + [-5.0] * 50
        df = _build_trades_df(pnls)
        panel = ce.compute_strategy_metrics(df, {}, n_trials_effective=1)
        assert panel["metrics"]["n_trades"] == 100

    def test_profit_factor_matches_hand_calc(self):
        # Winners total 100, losers total 50 -> PF = 2.0
        pnls = [50.0, 50.0, -25.0, -25.0]
        df = _build_trades_df(pnls)
        panel = ce.compute_strategy_metrics(df, {}, n_trials_effective=1)
        assert abs(panel["metrics"]["profit_factor"] - 2.0) < 1e-9

    def test_max_drawdown_calc(self):
        # Equity: 10, 30, 20, 5, 15. Peak=30, trough=5 -> DD = -25
        pnls = [10.0, 20.0, -10.0, -15.0, 10.0]
        df = _build_trades_df(pnls)
        panel = ce.compute_strategy_metrics(df, {}, n_trials_effective=1)
        assert abs(panel["metrics"]["max_drawdown_dollars"] - (-25.0)) < 1e-9

    def test_wfa_pass_via_ratio(self):
        df = _build_trades_df([1.0] * 30)
        wfa_pass = {"mean_is_pf": 2.0, "mean_oos_pf": 1.4}  # ratio 0.7
        wfa_fail = {"mean_is_pf": 2.0, "mean_oos_pf": 1.0}  # ratio 0.5
        p1 = ce.compute_strategy_metrics(df, wfa_pass, n_trials_effective=1)
        p2 = ce.compute_strategy_metrics(df, wfa_fail, n_trials_effective=1)
        assert p1["metrics"]["wfa_pass"] is True
        assert p2["metrics"]["wfa_pass"] is False

    def test_n_floor_gates_below_30(self):
        pnls = [1.0] * 10
        df = _build_trades_df(pnls)
        panel = ce.compute_strategy_metrics(df, {}, n_trials_effective=1)
        assert panel["gates"]["n_floor"] is False
        assert panel["gates"]["all_pass_for_proposal"] is False
        assert "n_floor" in panel["gates"]["failed_gates"]

    def test_n_high_above_200(self):
        pnls = [1.0] * 250
        df = _build_trades_df(pnls)
        panel = ce.compute_strategy_metrics(df, {}, n_trials_effective=1)
        assert panel["gates"]["n_floor"] is True
        assert panel["gates"]["n_medium"] is True
        assert panel["gates"]["n_high"] is True

    def test_empty_trades_df_safe(self):
        df = _build_trades_df([])
        panel = ce.compute_strategy_metrics(df, {}, n_trials_effective=1)
        assert panel["metrics"]["n_trades"] == 0
        assert panel["gates"]["all_pass_for_proposal"] is False


# ---------------------------------------------------------------------------
# Confidence tier tests (spec sec 7a)
# ---------------------------------------------------------------------------

class TestConfidenceTier:
    def test_below_30_insufficient(self):
        assert ce.classify_confidence_tier(29, True) == "INSUFFICIENT"
        assert ce.classify_confidence_tier(0, True) == "INSUFFICIENT"
        assert ce.classify_confidence_tier(29, False) == "INSUFFICIENT"

    def test_30_to_99_low(self):
        assert ce.classify_confidence_tier(30, True) == "LOW"
        assert ce.classify_confidence_tier(99, False) == "LOW"

    def test_100_to_199_medium_if_wfa_pass(self):
        assert ce.classify_confidence_tier(100, True) == "MEDIUM"
        assert ce.classify_confidence_tier(199, True) == "MEDIUM"

    def test_100_to_199_low_if_wfa_fail(self):
        assert ce.classify_confidence_tier(100, False) == "LOW"
        assert ce.classify_confidence_tier(199, False) == "LOW"

    def test_200_plus_high_if_wfa_pass(self):
        assert ce.classify_confidence_tier(200, True) == "HIGH"
        assert ce.classify_confidence_tier(1000, True) == "HIGH"

    def test_200_plus_medium_if_wfa_fail(self):
        assert ce.classify_confidence_tier(200, False) == "MEDIUM"
        assert ce.classify_confidence_tier(1000, False) == "MEDIUM"


# ---------------------------------------------------------------------------
# Proximity tests
# ---------------------------------------------------------------------------

class TestProximity:
    def test_plateau_when_neighbors_within_tolerance(self):
        # Center=1.0, neighbors give Sharpe within 10%
        rng = np.random.default_rng(30)
        # Build neighbor returns so the resulting Sharpe is near center.
        neighbors = {
            "minus_1": rng.normal(1.0, 1.0, size=100),
            "plus_1": rng.normal(1.05, 1.0, size=100),
        }
        # Use a metric_fn=None default (Sharpe). Just confirm result keys.
        result = ce.compute_proximity(neighbors, center_metric=1.0,
                                      tolerance_pct=0.50)
        assert set(result.keys()) == {"plateau", "center",
                                      "neighbor_drift", "max_drift_pct"}
        # With tolerance=0.5 and neighbors close, this should be a plateau.
        assert result["plateau"] is True

    def test_no_plateau_when_neighbor_far(self):
        neighbors = {
            "huge": np.array([10.0] * 100),
            "tiny": np.array([0.01] * 100),
        }
        result = ce.compute_proximity(neighbors, center_metric=1.0,
                                      tolerance_pct=0.10)
        assert result["plateau"] is False

    def test_proximity_neighbor_drift_dict(self):
        rng = np.random.default_rng(31)
        neighbors = {
            "a": rng.normal(1.0, 1.0, size=100),
            "b": rng.normal(1.0, 1.0, size=100),
        }
        result = ce.compute_proximity(neighbors, center_metric=1.0)
        assert set(result["neighbor_drift"].keys()) == {"a", "b"}

    def test_proximity_empty_neighbors_plateau_true(self):
        result = ce.compute_proximity({}, center_metric=1.0)
        # No neighbors to violate the plateau -> trivially flat.
        assert result["plateau"] is True
        assert result["max_drift_pct"] == 0.0


# ---------------------------------------------------------------------------
# Delta vs prior tests
# ---------------------------------------------------------------------------

class TestDeltaVsPrior:
    def _facts(self, dsr, psr=0.9, wr=0.5, pf=1.5, n=100, gates_pass=True):
        gates = {"all_pass_for_proposal": gates_pass}
        return {
            "strategies": {
                "bias_momentum": {
                    "metrics": {"dsr": dsr, "psr": psr, "win_rate": wr,
                                "profit_factor": pf, "n_trades": n},
                    "gates": gates,
                }
            }
        }

    def test_baseline_when_prior_none(self):
        cur = self._facts(0.7)
        out = ce.compute_delta_vs_prior(cur, None)
        assert out == {"is_baseline": True}

    def test_material_change_at_dsr_threshold(self):
        cur = self._facts(0.75)
        prior = self._facts(0.70)  # delta 0.05
        out = ce.compute_delta_vs_prior(cur, prior)
        delta = out["strategies"]["bias_momentum"]
        assert delta["materially_changed"] is True
        assert abs(delta["dsr_delta"] - 0.05) < 1e-9

    def test_below_threshold_not_material(self):
        cur = self._facts(0.74)
        prior = self._facts(0.70)  # delta 0.04, below 0.05
        # With pf/wr also unchanged and n unchanged, must not flag.
        out = ce.compute_delta_vs_prior(cur, prior)
        delta = out["strategies"]["bias_momentum"]
        assert delta["materially_changed"] is False

    def test_newly_eligible_when_gates_flip_true(self):
        cur = self._facts(0.9, gates_pass=True)
        prior = self._facts(0.9, gates_pass=False)
        out = ce.compute_delta_vs_prior(cur, prior)
        delta = out["strategies"]["bias_momentum"]
        assert delta["newly_eligible"] is True
        assert delta["newly_failing"] is False

    def test_newly_failing_when_gates_flip_false(self):
        cur = self._facts(0.9, gates_pass=False)
        prior = self._facts(0.9, gates_pass=True)
        out = ce.compute_delta_vs_prior(cur, prior)
        delta = out["strategies"]["bias_momentum"]
        assert delta["newly_failing"] is True
        assert delta["newly_eligible"] is False

    def test_summary_counts(self):
        cur = self._facts(0.9, gates_pass=True)
        prior = self._facts(0.7, gates_pass=False)
        out = ce.compute_delta_vs_prior(cur, prior)
        s = out["summary"]
        assert s["n_strategies_changed"] >= 1
        assert s["n_newly_eligible"] == 1

    def test_n_delta_threshold_30(self):
        cur = self._facts(0.7, n=130)
        prior = self._facts(0.7, n=100)  # delta 30
        out = ce.compute_delta_vs_prior(cur, prior)
        delta = out["strategies"]["bias_momentum"]
        assert delta["n_delta"] == 30
        assert delta["materially_changed"] is True

    def test_pf_delta_threshold(self):
        cur = self._facts(0.7, pf=1.75)
        prior = self._facts(0.7, pf=1.5)  # delta 0.25 > 0.20
        out = ce.compute_delta_vs_prior(cur, prior)
        delta = out["strategies"]["bias_momentum"]
        assert delta["materially_changed"] is True


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

class TestPublicSurface:
    def test_all_public_functions_exported(self):
        expected = {
            "compute_psr", "compute_dsr", "compute_min_trl",
            "compute_hlz_tstat", "compute_effective_n",
            "compute_strategy_metrics", "compute_proximity",
            "compute_delta_vs_prior", "classify_confidence_tier",
            "compute_bhy_p_adjusted",
        }
        assert expected.issubset(set(ce.__all__))

    def test_no_forbidden_imports(self):
        """The compute engine must not import bots/core/bridge/data_feeds
        or anthropic."""
        import inspect
        src = inspect.getsource(ce)
        for forbidden in ("from bots", "import bots", "from core",
                           "import core", "from bridge", "import bridge",
                           "from data_feeds", "import data_feeds",
                           "import anthropic", "from anthropic"):
            assert forbidden not in src, f"forbidden import found: {forbidden}"
