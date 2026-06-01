"""Deterministic risk-metric layer for the Phoenix Strategy Oracle.

Task 2 of the Phoenix Strategy Oracle build.

DESIGN PHILOSOPHY
-----------------
The LLM never computes a risk metric. Every Sharpe-related claim in the
agent's final debrief is traced back to a number that came out of this
module. The orchestrator (Task 5) calls into here to build a per-strategy
facts panel; the verifier (Task 4) uses that panel as ground truth.

CITATIONS
---------
- Probabilistic Sharpe Ratio (PSR):
  Bailey & Lopez de Prado, "The Sharpe Ratio Efficient Frontier",
  Journal of Risk, 2012.
- Deflated Sharpe Ratio (DSR):
  Bailey & Lopez de Prado, "The Deflated Sharpe Ratio: Correcting for
  Selection Bias, Backtest Overfitting and Non-Normality", Journal of
  Portfolio Management, 2014.
- Minimum Track Record Length (MinTRL):
  Bailey & Lopez de Prado 2012 (same paper as PSR), Eq. (8).
- Newey-West t-stat / HLZ:
  Harvey, Liu & Zhu, "...and the Cross-Section of Expected Returns",
  Review of Financial Studies, 2016.
- BHY (Benjamini-Hochberg-Yekutieli) FDR adjustment:
  Benjamini & Yekutieli 2001; applied in HLZ 2016 for finance.
- Optimal Number of Clusters for trial deduplication:
  Bailey & Lopez de Prado, "An Open-Source Implementation of the
  Optimal Number of Clusters Algorithm", 2017.

ALLOWED IMPORTS
---------------
- numpy, pandas, scipy.stats, scipy.special, scipy.cluster.hierarchy
- analytics.prepared_queries
- Standard library

FORBIDDEN IMPORTS (CI invariant)
--------------------------------
- bots/, core/, bridge/, data_feeds/  (this is the pure-math layer)
- anthropic / claude SDKs              (no LLM here)
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd
from scipy import stats
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

logger = logging.getLogger(__name__)

__all__ = [
    "compute_psr",
    "compute_dsr",
    "compute_min_trl",
    "compute_hlz_tstat",
    "compute_effective_n",
    "compute_strategy_metrics",
    "compute_proximity",
    "compute_delta_vs_prior",
    "classify_confidence_tier",
    "compute_bhy_p_adjusted",
]

# Materially-changed thresholds per spec sec 13.
_MATERIAL_DSR_DELTA = 0.05
_MATERIAL_PSR_DELTA = 0.05
_MATERIAL_WR_DELTA = 0.03
_MATERIAL_PF_DELTA = 0.20
_MATERIAL_N_DELTA = 30

# Gate thresholds per spec sec 7b.
_DSR_LUCK_FLOOR = 0.90
_DSR_PROPOSE = 0.95
_PSR_LUCK_FLOOR = 0.90
_HLZ_T_THRESHOLD = 3.0
_WFE_THRESHOLD = 0.6
_BHY_P_THRESHOLD = 0.05

# Sample-size gates per spec sec 7b. Centralized here as named constants
# so they can be emitted into facts.json under `gate_thresholds` (see
# `compute_strategy_metrics`) and traced by the verifier. The verifier's
# finding-classifier marks a rationale TRANSCRIPTION only when every
# number traces to a leaf in facts; if a rationale cites "0.95 gate"
# without that value being reachable, the finding is misclassified as
# INTERPRETATION. Emitting the thresholds keeps gate references
# legitimately quotable.
_N_FLOOR = 30
_N_MEDIUM = 100
_N_HIGH = 200

_MIN_TRL_INFEASIBLE = 10**9
_EULER_GAMMA = 0.5772156649015329


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _as_1d_array(returns: Any) -> np.ndarray:
    arr = np.asarray(returns, dtype=float).ravel()
    if arr.size == 0:
        return arr
    mask = np.isfinite(arr)
    n_dropped = int((~mask).sum())
    if n_dropped > 0:
        logger.debug("_as_1d_array: dropped %d non-finite values", n_dropped)
    return arr[mask]


def _sanitize_for_json(d: Any) -> Any:
    """Replace non-finite floats with None for JSON-RFC-8259 compliance.

    Python's json module emits literal 'Infinity' / 'NaN' tokens which
    downstream consumers (orjson, ujson, JavaScript JSON.parse) reject.
    This helper recursively walks dicts and lists and replaces any
    non-finite float (inf, -inf, NaN) with None. Other types pass through
    unchanged.
    """
    if isinstance(d, float):
        if math.isnan(d) or math.isinf(d):
            return None
        return d
    if isinstance(d, dict):
        return {k: _sanitize_for_json(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_sanitize_for_json(v) for v in d]
    return d


def _sample_sr_and_moments(returns: np.ndarray) -> tuple[float, float, float, int] | None:
    """Return (sr_hat, skew, excess_kurtosis, n) or None if the sample is
    too small / has zero std."""
    n = returns.size
    if n < 2:
        return None
    mu = float(returns.mean())
    sd = float(returns.std(ddof=1))
    if sd == 0.0 or not math.isfinite(sd):
        return None
    sr_hat = mu / sd
    sk = float(stats.skew(returns, bias=False))
    kt = float(stats.kurtosis(returns, fisher=True, bias=False))
    return sr_hat, sk, kt, n


def _psr_z(sr_hat: float, sk: float, kt: float, n: int,
           sr_benchmark: float) -> float | None:
    """Compute the PSR z-statistic. Returns None if degenerate."""
    denom_sq = (1.0 - sk * sr_hat + (kt / 4.0) * sr_hat * sr_hat) / (n - 1)
    if denom_sq <= 0 or not math.isfinite(denom_sq):
        logger.warning(
            "_psr_z: non-positive denominator (denom_sq=%r) for "
            "sr_hat=%r, skew=%r, kurt=%r, n=%d; returning None",
            denom_sq, sr_hat, sk, kt, n,
        )
        return None
    return (sr_hat - sr_benchmark) / math.sqrt(denom_sq)


# ---------------------------------------------------------------------------
# PSR
# ---------------------------------------------------------------------------

def compute_psr(returns: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio (Bailey & Lopez de Prado 2012, Eq. 5).

    Returns the probability that the true Sharpe ratio exceeds
    `sr_benchmark`, accounting for sample size, skew and excess kurtosis.

    PSR = Phi( (SR_hat - sr_benchmark) /
               sqrt( (1 - skew * SR_hat + (kurt/4) * SR_hat**2) / (n-1) ) )

    Returns 0.0 if the input is empty, contains < 2 finite values, or has
    zero sample standard deviation.
    """
    r = _as_1d_array(returns)
    moments = _sample_sr_and_moments(r)
    if moments is None:
        return 0.0
    sr_hat, sk, kt, n = moments
    z = _psr_z(sr_hat, sk, kt, n, sr_benchmark)
    if z is None:
        return 0.0
    return float(stats.norm.cdf(z))


# ---------------------------------------------------------------------------
# DSR
# ---------------------------------------------------------------------------

def _expected_max_sr(n_trials: int, v_sr: float) -> float:
    """Expected maximum Sharpe Ratio under N iid trials, per Bailey-LdP 2014.

    E[max SR] = sqrt(V_sr) * (
        (1 - euler_gamma) * Phi_inv(1 - 1/N)
        + euler_gamma * Phi_inv(1 - 1/(N*e))
    )

    With N <= 1 the formula is undefined (Phi_inv(0)); we return 0 so the
    deflated benchmark collapses to the PSR baseline.
    """
    if n_trials <= 1 or v_sr <= 0.0:
        return 0.0
    if not math.isfinite(v_sr):
        return 0.0
    p1 = 1.0 - 1.0 / float(n_trials)
    p2 = 1.0 - 1.0 / (float(n_trials) * math.e)
    # Guard against ppf(1.0) -> +inf for huge N.
    p1 = min(p1, 1.0 - 1e-15)
    p2 = min(p2, 1.0 - 1e-15)
    term = ((1.0 - _EULER_GAMMA) * stats.norm.ppf(p1)
            + _EULER_GAMMA * stats.norm.ppf(p2))
    return float(math.sqrt(v_sr) * term)


def compute_dsr(returns: np.ndarray, n_trials_effective: int,
                variance_of_trials_sr: float | None = None) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    Deflates the PSR benchmark by the expected maximum SR under
    `n_trials_effective` independent trials.

    The variance of the trials' SR (`V_sr`) is supplied if known; otherwise
    we estimate it from the sample using the asymptotic SR variance
    (1 - skew * SR_hat + (kurt/4) * SR_hat^2) / (n - 1), which equals the
    PSR denominator squared.
    """
    r = _as_1d_array(returns)
    moments = _sample_sr_and_moments(r)
    if moments is None:
        return 0.0
    sr_hat, sk, kt, n = moments

    if variance_of_trials_sr is None:
        # Use the asymptotic SR variance as the trials' SR variance proxy.
        v_sr = (1.0 - sk * sr_hat + (kt / 4.0) * sr_hat * sr_hat) / (n - 1)
        if v_sr <= 0 or not math.isfinite(v_sr):
            return 0.0
    else:
        v_sr = float(variance_of_trials_sr)

    sr_benchmark_max = _expected_max_sr(int(n_trials_effective), v_sr)
    return compute_psr(r, sr_benchmark=sr_benchmark_max)


# ---------------------------------------------------------------------------
# Min Track Record Length
# ---------------------------------------------------------------------------

def compute_min_trl(returns: np.ndarray, target_sr: float = 0.0,
                    alpha: float = 0.05) -> int:
    """Minimum Track Record Length (Bailey & Lopez de Prado 2012, Eq. 8).

    The minimum number of observations required so that PSR > 1 - alpha
    against the `target_sr` benchmark.

    Solving Eq. (5) of B-LdP for N:
        N >= 1 + (1 - skew*SR_hat + (kurt/4)*SR_hat**2)
                 * (Phi_inv(1-alpha) / (SR_hat - target_sr))**2

    Returns the ceiling of that bound. If SR_hat <= target_sr, the test is
    infeasible and we return a large sentinel (10**9).

    Convention note (default target_sr changed 2026-06-01):
    --------------------------------------------------------
    `returns` is interpreted in the SAME time unit the caller supplies --
    typically per-trade P&L in this codebase. `target_sr` MUST be expressed
    in matching units.

    The default is 0.0 ("minimum trades to prove SR > 0"), matching the
    convention used by compute_psr (sr_benchmark=0.0). The earlier default
    of 1.0 silently assumed annualized Sharpe ratios; passing per-trade
    returns against an annualized target made the gate always-infeasible,
    so every strategy failed `min_trl_met` regardless of edge.

    Callers wanting an "annualized SR > 1" question must annualize the
    returns first (e.g. multiply trade SR by sqrt(trades_per_year)) or
    pass target_sr explicitly in the same units as the returns. See the
    Hillsdale 2024 "Three Types of Backtests" paper for the practical
    application of MinTRL with SR* = 0 in trading-system evaluation.
    """
    r = _as_1d_array(returns)
    moments = _sample_sr_and_moments(r)
    if moments is None:
        return _MIN_TRL_INFEASIBLE
    sr_hat, sk, kt, _ = moments
    if sr_hat <= target_sr:
        return _MIN_TRL_INFEASIBLE
    factor = 1.0 - sk * sr_hat + (kt / 4.0) * sr_hat * sr_hat
    if factor <= 0 or not math.isfinite(factor):
        return _MIN_TRL_INFEASIBLE
    z = float(stats.norm.ppf(1.0 - alpha))
    delta = sr_hat - target_sr
    bound = 1.0 + factor * (z / delta) ** 2
    if not math.isfinite(bound) or bound <= 0:
        return _MIN_TRL_INFEASIBLE
    return int(math.ceil(bound))


# ---------------------------------------------------------------------------
# HLZ t-statistic with Newey-West variance
# ---------------------------------------------------------------------------

def _newey_west_variance(returns: np.ndarray, lag: int) -> float:
    """Newey-West long-run variance estimator with Bartlett kernel.

    V_NW = gamma_0 + 2 * sum_{l=1}^{L} (1 - l/(L+1)) * gamma_l
    where gamma_l is the lag-l autocovariance using population mean.
    """
    n = returns.size
    if n == 0:
        return 0.0
    x = returns - returns.mean()
    gamma_0 = float(np.dot(x, x) / n)
    v = gamma_0
    if lag <= 0:
        return v
    for l in range(1, lag + 1):
        if l >= n:
            break
        gamma_l = float(np.dot(x[:-l], x[l:]) / n)
        w = 1.0 - l / (lag + 1.0)
        v += 2.0 * w * gamma_l
    return v


def compute_hlz_tstat(returns: np.ndarray, lag: int | None = None) -> float:
    """Newey-West-adjusted t-statistic for the mean of `returns`
    (Harvey, Liu & Zhu 2016).

    Default lag = floor(n**(1/4)) per the standard rule of thumb.
    Returns 0.0 for empty input or non-positive Newey-West variance.
    """
    r = _as_1d_array(returns)
    n = r.size
    if n == 0:
        return 0.0
    if lag is None:
        lag = int(math.floor(n ** 0.25))
        lag = max(lag, 0)
    v_nw = _newey_west_variance(r, lag)
    if v_nw <= 0.0 or not math.isfinite(v_nw):
        return 0.0
    se = math.sqrt(v_nw / n)
    if se == 0:
        return 0.0
    return float(r.mean() / se)


# ---------------------------------------------------------------------------
# Effective N (cluster-corrected trial count)
# ---------------------------------------------------------------------------

def compute_effective_n(trial_returns: Sequence[np.ndarray]) -> int:
    """Cluster-corrected trial count per Bailey-LdP.

    Build the pairwise Pearson correlation matrix between trials, convert
    to distance d_ij = 1 - |corr_ij|, and apply average-linkage hierarchical
    clustering with distance threshold 0.5 (approximate threshold of
    |corr| >= 0.5 under average linkage; exact behavior depends on cluster
    composition). The number of resulting clusters is the effective N.

    Edge cases:
    - len(trial_returns) == 0 -> returns 1 (BHY adjustment uses c(1) = 1).
    - len(trial_returns) == 1 -> returns 1.
    """
    k = len(trial_returns)
    if k <= 1:
        return max(k, 1)

    # Truncate / align series to the shortest common length so correlations
    # are well-defined. Trials of length < 2 are dropped from the corr
    # computation (they contribute nothing) and rejoin as singleton clusters.
    aligned = [np.asarray(t, dtype=float).ravel() for t in trial_returns]
    n_short = min(t.size for t in aligned)
    if n_short < 2:
        # Cannot compute correlations; conservatively assume all distinct.
        return k

    matrix = np.stack([t[:n_short] for t in aligned], axis=0)
    # Pearson correlation between rows.
    corr = np.corrcoef(matrix)
    if not np.isfinite(corr).all():
        # Replace NaNs (zero-variance rows) with 0 -> treated as distinct.
        corr = np.nan_to_num(corr, nan=0.0)
    dist = 1.0 - np.abs(corr)
    np.fill_diagonal(dist, 0.0)
    # Symmetrize against floating-point drift; clamp into [0, 2].
    dist = (dist + dist.T) / 2.0
    dist = np.clip(dist, 0.0, 2.0)
    condensed = squareform(dist, checks=False)
    if condensed.size == 0:
        return k
    z = linkage(condensed, method="average")
    clusters = fcluster(z, t=0.5, criterion="distance")
    return int(len(set(clusters.tolist())))


# ---------------------------------------------------------------------------
# BHY adjustment
# ---------------------------------------------------------------------------

def compute_bhy_p_adjusted(t_stat: float, n_trials: int) -> float:
    """Benjamini-Hochberg-Yekutieli adjusted p-value (HLZ 2016 family).

    For a single test embedded in an effective-N family the multiplicative
    adjustment factor is the harmonic number c(N) = sum_{k=1..N} 1/k.

    raw_p = 2 * (1 - Phi(|t|))
    adjusted_p = min(1.0, raw_p * c(N))
    """
    if n_trials < 1:
        n_trials = 1
    raw_p = 2.0 * (1.0 - float(stats.norm.cdf(abs(float(t_stat)))))
    c_n = float(sum(1.0 / k for k in range(1, int(n_trials) + 1)))
    return float(min(1.0, raw_p * c_n))


# ---------------------------------------------------------------------------
# Confidence tier
# ---------------------------------------------------------------------------

def classify_confidence_tier(n_trades: int, wfa_passes: bool) -> str:
    """Spec sec 7a confidence-tier table.

    n_trades < 30          -> 'INSUFFICIENT'
    30 <= n < 100          -> 'LOW'
    100 <= n < 200         -> 'MEDIUM' if wfa_passes else 'LOW'
    n >= 200               -> 'HIGH'   if wfa_passes else 'MEDIUM'
    """
    n = int(n_trades)
    if n < 30:
        return "INSUFFICIENT"
    if n < 100:
        return "LOW"
    if n < 200:
        return "MEDIUM" if wfa_passes else "LOW"
    return "HIGH" if wfa_passes else "MEDIUM"


# ---------------------------------------------------------------------------
# Profit-factor / Sortino / Calmar / max DD helpers
# ---------------------------------------------------------------------------

def _profit_factor(pnls: np.ndarray) -> float:
    pos = float(pnls[pnls > 0].sum())
    neg = float(-pnls[pnls < 0].sum())
    if neg == 0:
        return float("inf") if pos > 0 else 0.0
    return pos / neg


def _sortino(pnls: np.ndarray) -> float:
    """Sortino ratio using the standard (Sortino & Price 1994) convention.

    Denominator is the RMS of min(returns, 0) computed over the FULL series
    (positive returns are replaced by zero, not dropped):

        dd = sqrt( mean( min(returns, 0) ** 2 ) )   # mean over all n

    This matches FactSet, Bloomberg, and the original 1994 paper. The
    alternative convention (mean over downside observations only) yields a
    systematically smaller denominator and a larger Sortino; switching
    between the two diverges by ~37% at 60% win-rate and ~68% at 90%
    win-rate, so external comparisons require the standard form.

    Returns 0.0 for empty input. Returns float('inf') if all returns are
    non-negative and mean > 0; returns 0.0 if all returns are non-negative
    and mean <= 0.
    """
    if pnls.size == 0:
        return 0.0
    mean_return = float(pnls.mean())
    downside = np.minimum(pnls, 0.0)
    dd = float(math.sqrt(np.mean(downside ** 2)))
    if dd == 0.0:
        return float("inf") if mean_return > 0 else 0.0
    return mean_return / dd


def _max_drawdown(pnls: np.ndarray) -> float:
    if pnls.size == 0:
        return 0.0
    equity = np.cumsum(pnls)
    peaks = np.maximum.accumulate(equity)
    drawdowns = equity - peaks
    return float(drawdowns.min())  # negative or 0


def _calmar(pnls: np.ndarray) -> float:
    if pnls.size == 0:
        return 0.0
    total = float(pnls.sum())
    max_dd = abs(_max_drawdown(pnls))
    if max_dd == 0:
        return float("inf") if total > 0 else 0.0
    return total / max_dd


# ---------------------------------------------------------------------------
# Strategy-level panel
# ---------------------------------------------------------------------------

def compute_strategy_metrics(trades_df: pd.DataFrame,
                             wfa_summary: dict,
                             n_trials_effective: int) -> dict:
    """Build the strategies[name] sub-dict per spec sec 12c.

    Uses `pnl_dollars` as the returns vector. Trade-level returns are used
    directly per the B-LdP convention for trade-based Sharpe analysis (no
    per-day aggregation).

    Conventions used here (important for cross-tool comparison):

    - Sortino ratio: standard Sortino & Price 1994 convention. The downside
      deviation denominator is RMS of min(returns, 0) over the FULL series
      with zeros substituted for positive returns. See ``_sortino``.
    - JSON safety: non-finite floats (inf, -inf, NaN) in the output are
      replaced with None via ``_sanitize_for_json`` so the panel is
      RFC-8259 compliant and survives orjson / JavaScript consumers.

    The output structure exactly matches the spec's facts.json schema and
    is the ground truth used by the verifier (Task 4).
    """
    if "pnl_dollars" in trades_df.columns:
        pnls = trades_df["pnl_dollars"].to_numpy(dtype=float)
    else:
        pnls = np.array([], dtype=float)
    pnls = pnls[np.isfinite(pnls)]
    n = int(pnls.size)

    # Statistical primitives.
    psr = compute_psr(pnls) if n >= 2 else 0.0
    dsr = compute_dsr(pnls, n_trials_effective=max(int(n_trials_effective), 1)) \
        if n >= 2 else 0.0
    min_trl = compute_min_trl(pnls) if n >= 2 else _MIN_TRL_INFEASIBLE
    hlz_t = compute_hlz_tstat(pnls) if n >= 2 else 0.0
    bhy_p = compute_bhy_p_adjusted(hlz_t, max(int(n_trials_effective), 1))

    # Trade-level descriptives.
    pf = _profit_factor(pnls)
    sortino = _sortino(pnls)
    calmar = _calmar(pnls)
    max_dd = _max_drawdown(pnls)
    win_rate = float((pnls > 0).sum() / n) if n > 0 else 0.0

    # WFA pass / WFE ratio.
    is_pf = wfa_summary.get("mean_is_pf") if wfa_summary else None
    oos_pf = wfa_summary.get("mean_oos_pf") if wfa_summary else None
    if is_pf is not None and oos_pf is not None and is_pf not in (0, 0.0):
        wfe_ratio = float(oos_pf) / float(is_pf)
        wfa_pass = wfe_ratio >= _WFE_THRESHOLD
    else:
        wfe_ratio = float("nan")
        wfa_pass = False

    metrics = {
        "n_trades": n,
        "psr": float(psr),
        "dsr": float(dsr),
        "min_trl": int(min_trl),
        "hlz_t_stat": float(hlz_t),
        "bhy_p_adjusted": float(bhy_p),
        "profit_factor": float(pf),
        "sortino": float(sortino),
        "calmar": float(calmar),
        "max_drawdown_dollars": float(max_dd),
        "oos_pf": float(oos_pf) if oos_pf is not None else float("nan"),
        "is_pf": float(is_pf) if is_pf is not None else float("nan"),
        "wfe_ratio": float(wfe_ratio),
        "win_rate": float(win_rate),
    }

    # Gates per spec sec 7. `wfa_pass` is duplicated into BOTH `gates`
    # and `metrics` so external consumers reading either dict find it
    # (the spec example shows it in `metrics`; the implementation
    # additionally needs it in `gates` for the all-pass aggregation).
    metrics["wfa_pass"] = bool(wfa_pass)
    gates = {
        "n_floor": n >= _N_FLOOR,
        "n_medium": n >= _N_MEDIUM,
        "n_high": n >= _N_HIGH,
        "psr_0_90": psr >= _PSR_LUCK_FLOOR,
        "dsr_0_90": dsr >= _DSR_LUCK_FLOOR,
        "dsr_0_95": dsr >= _DSR_PROPOSE,
        "hlz_3_0": hlz_t > _HLZ_T_THRESHOLD,
        "min_trl_met": n >= min_trl,
        "wfa_pass": bool(wfa_pass),
        "bhy_0_05": bhy_p <= _BHY_P_THRESHOLD,
    }

    proposal_gates = [
        "n_floor", "psr_0_90", "dsr_0_95", "hlz_3_0",
        "min_trl_met", "wfa_pass", "bhy_0_05",
    ]
    failed = [g for g in proposal_gates if not gates[g]]
    gates["all_pass_for_proposal"] = (len(failed) == 0)
    gates["failed_gates"] = failed

    # Emit gate thresholds so any rationale citing e.g. "0.95 gate" or
    # "n>=30 floor" has a traceable leaf-value in facts.json. The
    # verifier's number reconciler walks this dict because it sits
    # outside the `findings` skip-tree.
    gate_thresholds = {
        "dsr_high": _DSR_PROPOSE,
        "dsr_luck_floor": _DSR_LUCK_FLOOR,
        "psr": _PSR_LUCK_FLOOR,
        "hlz_t_stat": _HLZ_T_THRESHOLD,
        "n_floor": _N_FLOOR,
        "n_medium": _N_MEDIUM,
        "n_high": _N_HIGH,
        "wfe_ratio_min": _WFE_THRESHOLD,
        "bhy_0_05": _BHY_P_THRESHOLD,
    }

    panel = {
        "metrics": metrics,
        "gates": gates,
        "gate_thresholds": gate_thresholds,
    }
    return _sanitize_for_json(panel)


# ---------------------------------------------------------------------------
# Proximity (parameter-plateau stress test)
# ---------------------------------------------------------------------------

def _sharpe_proxy(pnls: np.ndarray) -> float:
    """Per-step Sharpe ratio used as the default proximity metric."""
    arr = _as_1d_array(pnls)
    if arr.size < 2:
        return 0.0
    sd = float(arr.std(ddof=1))
    if sd == 0:
        return 0.0
    return float(arr.mean() / sd)


def compute_proximity(neighbor_pnls: dict[str, np.ndarray],
                      center_metric: float,
                      tolerance_pct: float = 0.10,
                      metric_fn: Callable[[np.ndarray], float] | None = None
                      ) -> dict:
    """Parameter-proximity plateau test.

    `neighbor_pnls` maps a parameter-variation label to the pnl_dollars
    array for that variation. The same metric is computed on each
    neighbor and compared to `center_metric`. The test passes
    (plateau=True) iff every neighbor's metric is within +/-
    `tolerance_pct` of the center.

    If the center metric is exactly zero, we fall back to an absolute
    tolerance equal to `tolerance_pct`.

    Parameters
    ----------
    metric_fn:
        Callable used to compute each neighbor's metric. **It must be the
        SAME function the caller used to compute `center_metric`.** If you
        compute `center_metric` with PSR (a probability in [0, 1]) and the
        neighbors with Sharpe (an unbounded real), the drift comparison is
        meaningless. Defaults to the per-step Sharpe proxy ``_sharpe_proxy``
        (mean / std); if you computed `center_metric` with anything else
        you must pass that function here.
    """
    if metric_fn is None:
        metric_fn = _sharpe_proxy

    neighbor_drift: dict[str, float] = {}
    max_drift_pct = 0.0
    plateau = True

    denom = abs(float(center_metric)) if center_metric != 0 else 1.0

    for label, pnls in neighbor_pnls.items():
        metric = float(metric_fn(np.asarray(pnls, dtype=float)))
        drift = abs(metric - float(center_metric))
        neighbor_drift[label] = float(drift)
        drift_pct = drift / denom
        if drift_pct > max_drift_pct:
            max_drift_pct = drift_pct
        if drift_pct > float(tolerance_pct):
            plateau = False

    return {
        "plateau": bool(plateau),
        "center": float(center_metric),
        "neighbor_drift": neighbor_drift,
        "max_drift_pct": float(max_drift_pct),
    }


# ---------------------------------------------------------------------------
# Delta vs prior run
# ---------------------------------------------------------------------------

def _strategy_metric(facts: dict, strategy: str, key: str,
                     default: float = float("nan")) -> float:
    try:
        v = facts["strategies"][strategy]["metrics"][key]
    except (KeyError, TypeError):
        return default
    if v is None:
        return default
    return float(v)


def _strategy_int_metric(facts: dict, strategy: str, key: str,
                         default: int = 0) -> int:
    try:
        v = facts["strategies"][strategy]["metrics"][key]
    except (KeyError, TypeError):
        return default
    if v is None:
        return default
    return int(v)


def _strategy_all_pass(facts: dict, strategy: str) -> bool:
    try:
        return bool(facts["strategies"][strategy]["gates"]
                    ["all_pass_for_proposal"])
    except (KeyError, TypeError):
        return False


def _tier_change(current_facts: dict, prior_facts: dict, strat: str) -> str | None:
    """Best-effort tier-flip detector. Returns 'UP', 'DOWN', or None.

    Reads `wfa_pass` from the strategy's `gates` block (single source of
    truth). Falls back to the legacy `metrics['wfa_pass']` location if
    present, for compatibility with prior_facts written before the
    duplication was removed.
    """
    def _wfa_pass(facts: dict) -> bool:
        try:
            strat_block = facts["strategies"][strat]
        except (KeyError, TypeError):
            return False
        gates = strat_block.get("gates") if isinstance(strat_block, dict) else None
        if isinstance(gates, dict) and "wfa_pass" in gates:
            return bool(gates["wfa_pass"])
        metrics = strat_block.get("metrics") if isinstance(strat_block, dict) else None
        if isinstance(metrics, dict) and "wfa_pass" in metrics:
            return bool(metrics["wfa_pass"])
        return False

    try:
        cur_n = _strategy_int_metric(current_facts, strat, "n_trades")
        pri_n = _strategy_int_metric(prior_facts, strat, "n_trades")
        cur_wfa = _wfa_pass(current_facts)
        pri_wfa = _wfa_pass(prior_facts)
    except (KeyError, TypeError):
        return None
    cur_tier = classify_confidence_tier(cur_n, cur_wfa)
    pri_tier = classify_confidence_tier(pri_n, pri_wfa)
    order = ["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"]
    try:
        ci = order.index(cur_tier)
        pi = order.index(pri_tier)
    except ValueError:
        return None
    if ci > pi:
        return "UP"
    if ci < pi:
        return "DOWN"
    return None


def compute_delta_vs_prior(current_facts: dict,
                           prior_facts: dict | None) -> dict:
    """Per spec sec 12c / 13 - diff current_facts vs prior_facts.

    Reports per-strategy deltas (dsr / psr / wr / pf / n) and flags whether
    those deltas crossed the materially-changed thresholds. Also flags
    proposal-eligibility flips.

    If prior_facts is None, the run is a baseline.
    """
    if prior_facts is None:
        return {"is_baseline": True}

    cur_strats = set(current_facts.get("strategies", {}).keys())
    pri_strats = set(prior_facts.get("strategies", {}).keys())
    common = cur_strats & pri_strats

    strategies_out: dict[str, dict] = {}
    n_changed = 0
    n_newly_eligible = 0
    n_newly_failing = 0

    for strat in sorted(common):
        dsr_d = (_strategy_metric(current_facts, strat, "dsr")
                 - _strategy_metric(prior_facts, strat, "dsr"))
        psr_d = (_strategy_metric(current_facts, strat, "psr")
                 - _strategy_metric(prior_facts, strat, "psr"))
        wr_d = (_strategy_metric(current_facts, strat, "win_rate")
                - _strategy_metric(prior_facts, strat, "win_rate"))
        pf_d = (_strategy_metric(current_facts, strat, "profit_factor")
                - _strategy_metric(prior_facts, strat, "profit_factor"))
        n_d = (_strategy_int_metric(current_facts, strat, "n_trades")
               - _strategy_int_metric(prior_facts, strat, "n_trades"))

        materially_changed = (
            (math.isfinite(dsr_d) and abs(dsr_d) >= _MATERIAL_DSR_DELTA)
            or (math.isfinite(psr_d) and abs(psr_d) >= _MATERIAL_PSR_DELTA)
            or (math.isfinite(wr_d) and abs(wr_d) >= _MATERIAL_WR_DELTA)
            or (math.isfinite(pf_d) and abs(pf_d) >= _MATERIAL_PF_DELTA)
            or (abs(n_d) >= _MATERIAL_N_DELTA)
        )

        cur_pass = _strategy_all_pass(current_facts, strat)
        pri_pass = _strategy_all_pass(prior_facts, strat)
        newly_eligible = cur_pass and not pri_pass
        newly_failing = (not cur_pass) and pri_pass

        if materially_changed:
            n_changed += 1
        if newly_eligible:
            n_newly_eligible += 1
        if newly_failing:
            n_newly_failing += 1

        strategies_out[strat] = {
            "dsr_delta": float(dsr_d) if math.isfinite(dsr_d) else 0.0,
            "psr_delta": float(psr_d) if math.isfinite(psr_d) else 0.0,
            "wr_delta": float(wr_d) if math.isfinite(wr_d) else 0.0,
            "pf_delta": float(pf_d) if math.isfinite(pf_d) else 0.0,
            "n_delta": int(n_d),
            "materially_changed": bool(materially_changed),
            "newly_eligible": bool(newly_eligible),
            "newly_failing": bool(newly_failing),
            "tier_change": _tier_change(current_facts, prior_facts, strat),
        }

    return {
        "is_baseline": False,
        "strategies": strategies_out,
        "summary": {
            "n_strategies_changed": n_changed,
            "n_newly_eligible": n_newly_eligible,
            "n_newly_failing": n_newly_failing,
        },
    }
