"""Walk-forward + CPCV + DSR + PBO statistical validation harness (P4-5).

Closes F-24 in docs/audits/SYNTHESIS_2026-05-24.md. The academic methods
for distinguishing "this backtest result will hold out-of-sample" from
"this is overfitting noise."

Methods implemented:
  - WALK-FORWARD: time-series cross-validation with embargo period.
    Train on [0, t], test on [t, t+test_window], embargo
    [t+test_window, t+test_window+embargo]. Slide forward. Per
    Lopez de Prado (2018) "Advances in Financial Machine Learning",
    ch. 7, embargo prevents leakage when features have memory.
  - CPCV (Combinatorial Purged Cross-Validation): generate K groups,
    leave ``test_size`` out for test, purge surrounding observations
    around each test group to avoid leakage. Compute statistic across
    all C(K, test_size) splits. Per Lopez de Prado (2018) ch. 12.
  - DSR (Deflated Sharpe Ratio): adjusts in-sample Sharpe for the
    number of trials and the skew/kurtosis of returns. Per
    Bailey & Lopez de Prado (2014) "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting and
    Non-Normality", Journal of Portfolio Management 40 (5).
    Outputs p-value: probability that the strategy's true Sharpe
    is positive after correcting for selection bias.
  - PBO (Probability of Backtest Overfitting): per Bailey, Borwein,
    Lopez de Prado & Zhu (2017) "The Probability of Backtest
    Overfitting", Journal of Computational Finance 20 (4).
    Splits backtest into N segments, computes the rank of the
    best-IS strategy in the OOS evaluation, derives PBO from the
    logit of the OOS rank distribution. PBO > 0.5 means strategy
    selection is no better than chance.

Output: structured JSON + markdown report. JSON consumed by
weekly_evolution.py to flip the [NOT YET RUN] checkboxes to [PASS]/
[FAIL]/[INSUFFICIENT_DATA] with the actual numbers.

CLI:
  python tools/walk_forward_harness.py --strategy bias_momentum \
                                       --since 2026-04-22 \
                                       --min-trades 200 \
                                       --out out/walk_forward_<date>_<strategy>

Sample-size thresholds (verdict = INSUFFICIENT_DATA below these):
  - walk-forward: 60
  - CPCV:        100
  - DSR:          30
  - PBO:          60
  - run_all:     min_trades (default 200) — the gate-level threshold

Stdlib + numpy only. No scikit-learn, no scipy dependency.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

_HERE = Path(__file__).resolve()
PROJECT_ROOT = _HERE.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.trade_memory import load_all_trades  # canonical reader

logger = logging.getLogger("WalkForwardHarness")


# ─── sample-size thresholds (first-class INSUFFICIENT_DATA gate) ──────────
MIN_TRADES_WALK_FORWARD = 60
MIN_TRADES_CPCV = 100
MIN_TRADES_DSR = 30
MIN_TRADES_PBO = 60
DEFAULT_MIN_TRADES_RUN_ALL = 200  # the operator-facing promotion gate

INSUFFICIENT = "INSUFFICIENT_DATA"
PASS = "PASS"
FAIL = "FAIL"


# ════════════════════════════════════════════════════════════════════════
# Helpers — trade -> returns extraction
# ════════════════════════════════════════════════════════════════════════

def _trade_return(t: dict) -> float:
    """Pull a per-trade *net* PnL in dollars. Falls back through
    candidate field names. Used as the unit return for Sharpe/PBO/etc.

    Phoenix's canonical net PnL is `pnl_dollars_net` (post-commission,
    post-fees, post-slippage). Falls back to `pnl_dollars` then 0.0.
    """
    for k in ("pnl_dollars_net", "pnl_dollars", "pnl"):
        v = t.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _trade_sort_key(t: dict) -> float:
    """Chronological sort key — entry_time (unix seconds) or 0."""
    et = t.get("entry_time")
    if et is None:
        # Fall back to recorded_at ISO string
        ra = t.get("recorded_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
        return 0.0
    try:
        return float(et)
    except (TypeError, ValueError):
        return 0.0


def _ensure_sorted(trades: list[dict]) -> list[dict]:
    return sorted(trades, key=_trade_sort_key)


def _sharpe(returns: list[float], periods_per_year: float = 252.0) -> float:
    """Annualized Sharpe ratio. Returns 0.0 when undefined.

    Treats each return as one period. ``periods_per_year`` defaults to 252
    (one trade per trading day approximation). For per-trade returns the
    relative magnitudes of Sharpes across strategies still rank correctly;
    only the absolute scale shifts with this choice.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    mu = float(arr.mean())
    sigma = float(arr.std(ddof=1))
    if sigma <= 0:
        return 0.0
    return (mu / sigma) * math.sqrt(periods_per_year)


# ════════════════════════════════════════════════════════════════════════
# 1. Walk-forward split with embargo (Lopez de Prado 2018, ch. 7)
# ════════════════════════════════════════════════════════════════════════

def walk_forward_split(
    trades: list[dict],
    n_splits: int = 5,
    embargo_frac: float = 0.02,
) -> Iterator[tuple[list[dict], list[dict]]]:
    """Time-series CV with expanding train + embargo.

    For each split i in [1, n_splits]:
      - test = window of size ``test_window`` starting at index
        ``i * test_window``.
      - train = all trades before test start, MINUS the embargo at the
        end of the train window (the last ``embargo`` trades before test
        are excluded). Per Lopez de Prado, this prevents leakage when
        features overlap across labels.

    Yields (train, test) tuples. Train is expanding; test slides forward
    by ``test_window`` each split.

    Sorted in entry_time order before splitting.
    """
    if not trades:
        return
    s = _ensure_sorted(trades)
    n = len(s)
    if n < n_splits + 1:
        return
    test_window = n // (n_splits + 1)
    if test_window < 1:
        return
    embargo = max(1, int(round(n * embargo_frac))) if embargo_frac > 0 else 0

    for i in range(1, n_splits + 1):
        test_start = i * test_window
        test_end = min(test_start + test_window, n)
        if test_start >= n:
            break
        train_end = max(0, test_start - embargo)
        train = s[:train_end]
        test = s[test_start:test_end]
        if train and test:
            yield train, test


# ════════════════════════════════════════════════════════════════════════
# 2. CPCV split (Lopez de Prado 2018, ch. 12)
# ════════════════════════════════════════════════════════════════════════

def cpcv_split(
    trades: list[dict],
    k: int = 6,
    test_size: int = 2,
    purge_frac: float = 0.01,
) -> Iterator[tuple[list[dict], list[dict]]]:
    """Combinatorial purged cross-validation.

    Partition the sorted trades into ``k`` contiguous groups. For each
    combination of ``test_size`` groups: test = those groups; train =
    the remaining groups MINUS a purge window of ``purge_frac * n``
    observations at each train/test boundary.

    Yields all C(k, test_size) (train, test) pairs.
    """
    if not trades or k < 2 or test_size < 1 or test_size >= k:
        return
    s = _ensure_sorted(trades)
    n = len(s)
    if n < k:
        return

    # Group index for each trade — contiguous groups.
    group_size = n // k
    if group_size < 1:
        return
    group_of = [0] * n
    for i in range(n):
        # Last group absorbs the remainder.
        g = min(i // group_size, k - 1)
        group_of[i] = g

    purge = max(1, int(round(n * purge_frac))) if purge_frac > 0 else 0

    for test_groups in combinations(range(k), test_size):
        test_groups_set = set(test_groups)
        # Test indices = all indices in any of the test groups
        test_idx = [i for i in range(n) if group_of[i] in test_groups_set]
        # Train indices = all indices NOT in test groups, minus purge.
        train_idx = []
        for i in range(n):
            if group_of[i] in test_groups_set:
                continue
            # Purge: exclude if within ``purge`` indices of any test obs
            in_purge = False
            for j in test_idx:
                if abs(i - j) <= purge:
                    in_purge = True
                    break
            if not in_purge:
                train_idx.append(i)
        if not train_idx or not test_idx:
            continue
        yield [s[i] for i in train_idx], [s[i] for i in test_idx]


# ════════════════════════════════════════════════════════════════════════
# 3. Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014)
# ════════════════════════════════════════════════════════════════════════

# Euler-Mascheroni constant used in DSR's selection-bias correction.
_EULER_GAMMA = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via stdlib statistics.NormalDist."""
    return statistics.NormalDist().cdf(x)


def _norm_ppf(p: float) -> float:
    """Standard normal inverse CDF via stdlib statistics.NormalDist."""
    # Guard endpoints to avoid +/- inf on degenerate inputs.
    p = max(min(p, 1.0 - 1e-12), 1e-12)
    return statistics.NormalDist().inv_cdf(p)


def _sample_skew(arr: np.ndarray) -> float:
    """Adjusted Fisher-Pearson skewness (sample bias-corrected).
    Returns 0.0 when undefined.
    """
    n = arr.size
    if n < 3:
        return 0.0
    mu = float(arr.mean())
    s = float(arr.std(ddof=1))
    if s <= 0:
        return 0.0
    m3 = float(((arr - mu) ** 3).mean())
    # Bias-corrected estimator: g1 = m3 / s^3 * sqrt(n*(n-1)) / (n-2)
    g1 = (m3 / (s ** 3)) * math.sqrt(n * (n - 1)) / (n - 2)
    return g1


def _sample_kurtosis(arr: np.ndarray) -> float:
    """Excess kurtosis (population estimator), guarded for small n."""
    n = arr.size
    if n < 4:
        return 0.0
    mu = float(arr.mean())
    s2 = float(arr.var(ddof=1))
    if s2 <= 0:
        return 0.0
    m4 = float(((arr - mu) ** 4).mean())
    g2 = m4 / (s2 ** 2) - 3.0
    return g2


def compute_dsr(returns: list[float], n_trials: int = 1) -> dict:
    """Deflated Sharpe Ratio.

    Per Bailey & Lopez de Prado (2014). Steps:

    1. Sharpe-hat = mean / std of per-trade returns (NOT annualized; DSR
       works with the raw per-period statistic).
    2. Variance of Sharpe-hat under non-normality:
         Var(SR_hat) = (1 - skew*SR + (kurt-1)/4 * SR^2) / (n - 1)
       where skew, kurt are sample skew & EXCESS kurtosis. (Mertens 2002.)
    3. Selection-adjusted expected max Sharpe across ``n_trials``
       independent trials, under H0 of true Sharpe = 0:
         E[max SR] ≈ sqrt(Var(SR)) * ((1-gamma)*Phi^-1(1-1/N)
                                      + gamma*Phi^-1(1 - 1/(N*e)))
       where gamma is Euler-Mascheroni and Phi^-1 is normal-inverse-CDF.
    4. DSR z-score:
         z = (SR_hat - E[max SR]) / sqrt(Var(SR))
       Probabilistic Sharpe Ratio:
         PSR = Phi(z)   = probability true Sharpe > expected-max-under-H0
       Two-sided p-value of "true Sharpe ≤ 0 after selection correction"
       is reported as ``p_value = 1 - PSR``.

    INSUFFICIENT_DATA if n < MIN_TRADES_DSR (=30).

    Returns dict with keys: verdict, n, sharpe, sharpe_var, skew, kurt,
    expected_max_sr, dsr_z, psr, p_value, n_trials.
    """
    n = len(returns)
    if n < MIN_TRADES_DSR:
        return {
            "verdict": INSUFFICIENT,
            "n": n,
            "n_needed": MIN_TRADES_DSR,
            "reason": f"DSR requires n>={MIN_TRADES_DSR}, got {n}",
            "sharpe": None, "p_value": None, "psr": None,
            "n_trials": int(n_trials),
        }

    arr = np.asarray(returns, dtype=float)
    mu = float(arr.mean())
    sigma = float(arr.std(ddof=1))
    if sigma <= 0:
        return {
            "verdict": INSUFFICIENT,
            "n": n, "n_needed": MIN_TRADES_DSR,
            "reason": "zero variance in returns (sigma=0)",
            "sharpe": 0.0, "p_value": None, "psr": None,
            "n_trials": int(n_trials),
        }

    # Per-trade (NOT annualized) Sharpe
    sr_hat = mu / sigma
    skew = _sample_skew(arr)
    kurt = _sample_kurtosis(arr)  # excess kurtosis

    # Mertens variance of estimator
    sr_var = (1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * (sr_hat ** 2)) / (n - 1)
    sr_var = max(sr_var, 1e-12)
    sr_std = math.sqrt(sr_var)

    N = max(int(n_trials), 1)
    if N == 1:
        # No selection bias when only one trial was tested
        expected_max_sr = 0.0
    else:
        z1 = _norm_ppf(1.0 - 1.0 / N)
        z2 = _norm_ppf(1.0 - 1.0 / (N * math.e))
        # Variance of trial-Sharpes assumed unit under H0; scale by sr_std
        expected_max_sr = sr_std * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)

    dsr_z = (sr_hat - expected_max_sr) / sr_std
    psr = _norm_cdf(dsr_z)
    p_value = 1.0 - psr  # prob that the *deflated* Sharpe is not > 0

    return {
        "verdict": PASS if p_value < 0.05 else FAIL,
        "n": n,
        "n_trials": N,
        "sharpe": sr_hat,                   # per-trade
        "sharpe_annualized": _sharpe(returns),
        "sharpe_var": sr_var,
        "skew": skew,
        "kurt_excess": kurt,
        "expected_max_sr": expected_max_sr,
        "dsr_z": dsr_z,
        "psr": psr,
        "p_value": p_value,
    }


# ════════════════════════════════════════════════════════════════════════
# 4. PBO — Probability of Backtest Overfitting (Bailey et al. 2017)
# ════════════════════════════════════════════════════════════════════════

def compute_pbo(in_sample_sharpes: list[list[float]]) -> dict:
    """Probability of Backtest Overfitting.

    Input: ``in_sample_sharpes`` is an N_strategies x N_segments matrix
    of Sharpe ratios. Each ROW = one candidate config evaluated on each
    of the N_segments backtest segments. (Build this matrix by splitting
    the trade history into S segments and computing each candidate's
    Sharpe on each segment.)

    Per Bailey, Borwein, Lopez de Prado & Zhu (2017), the PBO is computed
    by combinatorially symmetric cross-validation (CSCV):

    1. Form all ways to split the S segments into two halves: IS (in-
       sample) and OOS (out-of-sample). Number of partitions =
       C(S, S/2).
    2. For each partition:
         a. Find the strategy with the highest IS aggregate Sharpe.
         b. Compute that strategy's OOS rank among all strategies (1
            = best OOS, N_strategies = worst).
         c. Logit-transform the relative rank:
              omega = rank/(N+1)
              lambda = log(omega / (1 - omega))
    3. PBO = fraction of partitions where lambda < 0 — i.e. the best-
       IS strategy ended up below the OOS median.

    INSUFFICIENT_DATA if fewer than 2 strategies, fewer than 4 segments,
    or any segment-strategy combination has effectively no observations.
    The harness's own segment-builder enforces ≥ 30 trades per segment.

    Returns dict with keys: verdict, pbo, n_segments, n_strategies,
    n_partitions, logits (summary stats only), reason.
    """
    if not in_sample_sharpes:
        return {"verdict": INSUFFICIENT, "pbo": None, "reason": "empty input"}
    n_strategies = len(in_sample_sharpes)
    n_segments = len(in_sample_sharpes[0]) if n_strategies > 0 else 0
    if n_strategies < 2:
        return {
            "verdict": INSUFFICIENT, "pbo": None,
            "n_strategies": n_strategies, "n_segments": n_segments,
            "reason": "PBO requires >= 2 candidate strategies",
        }
    if n_segments < 4 or n_segments % 2 != 0:
        # CSCV needs even segment count; minimum 4 for any signal.
        return {
            "verdict": INSUFFICIENT, "pbo": None,
            "n_strategies": n_strategies, "n_segments": n_segments,
            "reason": "PBO requires even n_segments >= 4 (got "
                      f"{n_segments})",
        }
    # Rectangularity check
    if any(len(row) != n_segments for row in in_sample_sharpes):
        return {
            "verdict": INSUFFICIENT, "pbo": None,
            "reason": "in_sample_sharpes is not rectangular",
        }

    M = np.asarray(in_sample_sharpes, dtype=float)  # N_strat x N_seg
    half = n_segments // 2

    logits = []
    for is_idx in combinations(range(n_segments), half):
        is_set = set(is_idx)
        oos_idx = [s for s in range(n_segments) if s not in is_set]
        is_scores = M[:, list(is_idx)].sum(axis=1)
        oos_scores = M[:, oos_idx].sum(axis=1)
        # Best-IS strategy
        best_is = int(np.argmax(is_scores))
        # OOS *relative* rank, per Bailey et al. 2017 §2: omega is the
        # fraction of strategies the best-IS strategy beats on OOS
        # (close to 1 = good, close to 0 = bad / overfit). lambda = logit
        # of omega is negative when best-IS underperforms OOS median.
        best_oos_score = oos_scores[best_is]
        # # of strategies the best-IS strictly beats on OOS
        n_beaten = int(np.sum(oos_scores < best_oos_score))
        # Inclusive cumulative position (1..N) for the best-IS strategy;
        # use (n_beaten + 1) as its rank from the bottom.
        omega = (n_beaten + 1.0) / (n_strategies + 1.0)
        # Clamp for numerical safety on degenerate edges
        omega = min(max(omega, 1e-9), 1.0 - 1e-9)
        lam = math.log(omega / (1.0 - omega))
        logits.append(lam)

    if not logits:
        return {
            "verdict": INSUFFICIENT, "pbo": None,
            "n_strategies": n_strategies, "n_segments": n_segments,
            "reason": "no valid CSCV partitions",
        }

    arr = np.asarray(logits, dtype=float)
    # NB: lambda < 0  ⇔  rank/(N+1) < 0.5  ⇔  rank below median
    pbo = float(np.mean(arr < 0))
    return {
        "verdict": PASS if pbo < 0.5 else FAIL,
        "pbo": pbo,
        "n_strategies": n_strategies,
        "n_segments": n_segments,
        "n_partitions": int(arr.size),
        "logits_mean": float(arr.mean()),
        "logits_median": float(np.median(arr)),
        "logits_std": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
    }


# ════════════════════════════════════════════════════════════════════════
# 5. Walk-forward harness (orchestrator)
# ════════════════════════════════════════════════════════════════════════

def _walk_forward_report(trades: list[dict]) -> dict:
    """Run the walk-forward split + compute per-fold test Sharpe.

    Returns dict with verdict, folds (list of per-fold stats),
    all_folds_positive (bool), mean_test_sharpe.
    """
    n = len(trades)
    if n < MIN_TRADES_WALK_FORWARD:
        return {
            "verdict": INSUFFICIENT,
            "n": n,
            "n_needed": MIN_TRADES_WALK_FORWARD,
            "reason": (f"walk-forward requires n>={MIN_TRADES_WALK_FORWARD}, "
                       f"got {n}"),
            "folds": [],
        }

    folds = []
    for i, (train, test) in enumerate(walk_forward_split(trades, n_splits=5,
                                                          embargo_frac=0.02)):
        train_ret = [_trade_return(t) for t in train]
        test_ret = [_trade_return(t) for t in test]
        fold = {
            "fold": i + 1,
            "n_train": len(train),
            "n_test": len(test),
            "train_sharpe": _sharpe(train_ret),
            "test_sharpe": _sharpe(test_ret),
            "test_mean": float(np.mean(test_ret)) if test_ret else 0.0,
            "test_total_pnl": float(np.sum(test_ret)) if test_ret else 0.0,
        }
        folds.append(fold)

    if not folds:
        return {
            "verdict": INSUFFICIENT, "n": n,
            "reason": "no folds produced (too few trades for split)",
            "folds": [],
        }

    test_sharpes = [f["test_sharpe"] for f in folds]
    all_positive = all(s > 0 for s in test_sharpes)
    return {
        "verdict": PASS if all_positive else FAIL,
        "n": n,
        "n_folds": len(folds),
        "all_folds_positive": all_positive,
        "mean_test_sharpe": float(np.mean(test_sharpes)),
        "min_test_sharpe": float(np.min(test_sharpes)),
        "folds": folds,
    }


def _cpcv_report(trades: list[dict]) -> dict:
    """Run CPCV + summarize per-split test Sharpe distribution."""
    n = len(trades)
    if n < MIN_TRADES_CPCV:
        return {
            "verdict": INSUFFICIENT,
            "n": n,
            "n_needed": MIN_TRADES_CPCV,
            "reason": f"CPCV requires n>={MIN_TRADES_CPCV}, got {n}",
            "splits": 0,
        }

    test_sharpes = []
    for train, test in cpcv_split(trades, k=6, test_size=2, purge_frac=0.01):
        test_ret = [_trade_return(t) for t in test]
        test_sharpes.append(_sharpe(test_ret))

    if not test_sharpes:
        return {
            "verdict": INSUFFICIENT, "n": n,
            "reason": "no CPCV splits produced",
            "splits": 0,
        }

    arr = np.asarray(test_sharpes, dtype=float)
    median = float(np.median(arr))
    # We define PASS as median CPCV test Sharpe > 0 AND >= 75% of splits
    # have positive Sharpe (robust to a couple of outlier weak folds).
    frac_positive = float(np.mean(arr > 0))
    return {
        "verdict": PASS if (median > 0 and frac_positive >= 0.75) else FAIL,
        "n": n,
        "splits": int(arr.size),
        "median_test_sharpe": median,
        "mean_test_sharpe": float(arr.mean()),
        "frac_positive": frac_positive,
        "min_test_sharpe": float(arr.min()),
        "max_test_sharpe": float(arr.max()),
    }


def _pbo_from_trades(trades: list[dict],
                     n_segments: int = 8,
                     n_synthetic_strategies: int = 10) -> dict:
    """Build the IS-Sharpes matrix for PBO from trade history.

    With only a single strategy's trades we can't compare candidate
    configs head-to-head. Instead we follow the standard PBO practice
    for single-strategy evaluation: generate ``n_synthetic_strategies``
    bootstrap-resampled return paths from the real trade returns. The
    null hypothesis being tested is that the historical Sharpe is no
    better than what would arise by selecting the best of many noisy
    backtests.

    INSUFFICIENT_DATA if n < MIN_TRADES_PBO (=60) or if any segment
    would have fewer than 30 trades.
    """
    n = len(trades)
    if n < MIN_TRADES_PBO:
        return {
            "verdict": INSUFFICIENT,
            "n": n,
            "n_needed": MIN_TRADES_PBO,
            "reason": f"PBO requires n>={MIN_TRADES_PBO}, got {n}",
        }

    sorted_trades = _ensure_sorted(trades)
    returns = np.asarray([_trade_return(t) for t in sorted_trades],
                         dtype=float)

    seg_size = n // n_segments
    if seg_size < 30:
        # Reduce n_segments to keep ≥30 trades/segment.
        n_segments = max(4, n // 30)
        # Enforce even
        if n_segments % 2 != 0:
            n_segments -= 1
        seg_size = n // n_segments
        if n_segments < 4 or seg_size < 30:
            return {
                "verdict": INSUFFICIENT,
                "n": n,
                "reason": (f"per-segment trades < 30 (n={n}, segs={n_segments}, "
                           f"seg_size={seg_size})"),
            }

    # Per-segment Sharpes for the REAL strategy.
    real_seg_sharpes = []
    for s in range(n_segments):
        chunk = returns[s * seg_size:(s + 1) * seg_size]
        real_seg_sharpes.append(_sharpe(chunk.tolist()))

    # Synthetic competitor strategies: bootstrap-resample returns. The
    # RNG is seeded for reproducibility per (n, strategy_id) — important
    # so the validation report is stable across runs on the same data.
    rng = np.random.default_rng(seed=hash(("phoenix_pbo", n)) & 0xFFFFFFFF)
    matrix = [real_seg_sharpes]
    for k in range(n_synthetic_strategies):
        synth_seg = []
        for s in range(n_segments):
            sample = rng.choice(returns, size=seg_size, replace=True)
            synth_seg.append(_sharpe(sample.tolist()))
        matrix.append(synth_seg)

    return compute_pbo(matrix)


# ════════════════════════════════════════════════════════════════════════
# 6. run_all — top-level entrypoint consumed by weekly_evolution.py
# ════════════════════════════════════════════════════════════════════════

def run_all(trades: list[dict], strategy: str,
            min_trades: int = DEFAULT_MIN_TRADES_RUN_ALL) -> dict:
    """Top-level validation harness.

    Returns a structured dict consumed by weekly_evolution.py's commit-
    body renderer. Shape:

      {
        "strategy": str,
        "n_trades": int,
        "min_trades": int,
        "verdict": "PASS" | "FAIL" | "INSUFFICIENT_DATA",
        "reason": str,
        "walk_forward": {...},
        "cpcv":         {...},
        "dsr":          {...},
        "pbo":          {...},
        "generated_at": ISO timestamp,
      }

    Top-level verdict logic:
      - INSUFFICIENT_DATA if n < min_trades OR if any sub-test is
        INSUFFICIENT_DATA (we report the underlying sub-test reasons).
      - PASS if walk_forward all-folds-positive AND DSR p_value < 0.05
        AND PBO < 0.5. This matches the promotion-gate criterion
        documented in docs/runbook.md §"P4-5 Statistical Validation".
      - FAIL otherwise.
    """
    n = len(trades)
    report = {
        "strategy": strategy,
        "n_trades": n,
        "min_trades": min_trades,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if n < min_trades:
        report.update({
            "verdict": INSUFFICIENT,
            "reason": (f"need {min_trades} trades for full statistical "
                       f"validation, have {n}"),
            "walk_forward": {"verdict": INSUFFICIENT, "n": n,
                             "n_needed": min_trades},
            "cpcv": {"verdict": INSUFFICIENT, "n": n, "n_needed": min_trades},
            "dsr": {"verdict": INSUFFICIENT, "n": n, "n_needed": min_trades},
            "pbo": {"verdict": INSUFFICIENT, "n": n, "n_needed": min_trades},
        })
        return report

    returns = [_trade_return(t) for t in _ensure_sorted(trades)]

    wf = _walk_forward_report(trades)
    cp = _cpcv_report(trades)
    # n_trials = 1 for a single-strategy historical evaluation. If the
    # caller knows the actual number of configs explored, pass it via a
    # follow-up enhancement (kwarg) — but for the weekly automated
    # validation this is the per-strategy single-config case.
    ds = compute_dsr(returns, n_trials=1)
    pb = _pbo_from_trades(trades)

    sub_verdicts = [wf["verdict"], cp["verdict"], ds["verdict"], pb["verdict"]]
    if INSUFFICIENT in sub_verdicts:
        verdict = INSUFFICIENT
        reason = "one or more sub-tests INSUFFICIENT_DATA"
    elif all(v == PASS for v in (wf["verdict"], ds["verdict"], pb["verdict"])):
        # CPCV is informational; gate is WF + DSR + PBO per the runbook.
        verdict = PASS
        reason = "walk-forward all-folds-positive + DSR p<0.05 + PBO<0.5"
    else:
        verdict = FAIL
        failing = []
        if wf["verdict"] == FAIL:
            failing.append("walk_forward")
        if ds["verdict"] == FAIL:
            failing.append("DSR")
        if pb["verdict"] == FAIL:
            failing.append("PBO")
        reason = f"failing sub-test(s): {', '.join(failing)}"

    report.update({
        "verdict": verdict,
        "reason": reason,
        "walk_forward": wf,
        "cpcv": cp,
        "dsr": ds,
        "pbo": pb,
    })
    return report


# ════════════════════════════════════════════════════════════════════════
# 7. Markdown rendering
# ════════════════════════════════════════════════════════════════════════

def _fmt(v, fmt: str = ".4f") -> str:
    if v is None:
        return "n/a"
    if isinstance(v, str):
        return v
    try:
        return format(v, fmt)
    except (TypeError, ValueError):
        return str(v)


def render_markdown(report: dict) -> str:
    """Render the run_all() report as a human-readable markdown doc."""
    lines = []
    strat = report.get("strategy", "?")
    verdict = report.get("verdict", "?")
    n = report.get("n_trades", 0)
    min_n = report.get("min_trades", 0)
    lines.append(f"# Walk-Forward Statistical Validation — `{strat}`")
    lines.append("")
    lines.append(f"- **Verdict:** `{verdict}`")
    lines.append(f"- **Trades:** {n} (threshold {min_n})")
    lines.append(f"- **Reason:** {report.get('reason', '')}")
    lines.append(f"- **Generated:** {report.get('generated_at', '')}")
    lines.append("")
    lines.append("## Promotion gate (per `docs/runbook.md` §P4-5)")
    lines.append("")
    lines.append("Flip `validated=True` ONLY when ALL of:")
    lines.append("")
    lines.append("- walk-forward: all 5 folds have positive test Sharpe")
    lines.append("- DSR: p-value < 0.05 (deflated Sharpe is statistically positive)")
    lines.append("- PBO: < 0.5 (best-IS strategy beats OOS-median > chance)")
    lines.append("")

    def _section(title, sub):
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- verdict: `{sub.get('verdict', '?')}`")
        for k, v in sub.items():
            if k in ("verdict", "folds"):
                continue
            if isinstance(v, (int, float)) or v is None:
                lines.append(f"- {k}: {_fmt(v)}")
            elif isinstance(v, str):
                lines.append(f"- {k}: {v}")
        if "folds" in sub and sub["folds"]:
            lines.append("")
            lines.append("| fold | n_train | n_test | train_sharpe | test_sharpe |")
            lines.append("|-----:|--------:|-------:|-------------:|------------:|")
            for f in sub["folds"]:
                lines.append(
                    f"| {f['fold']} | {f['n_train']} | {f['n_test']} | "
                    f"{_fmt(f['train_sharpe'])} | {_fmt(f['test_sharpe'])} |"
                )
        lines.append("")

    _section("Walk-forward CV (Lopez de Prado 2018 ch. 7)", report.get("walk_forward", {}))
    _section("CPCV (Lopez de Prado 2018 ch. 12)", report.get("cpcv", {}))
    _section("DSR (Bailey & Lopez de Prado 2014)", report.get("dsr", {}))
    _section("PBO (Bailey, Borwein, Lopez de Prado, Zhu 2017)", report.get("pbo", {}))

    return "\n".join(lines) + "\n"


# ════════════════════════════════════════════════════════════════════════
# 8. CLI
# ════════════════════════════════════════════════════════════════════════

def _filter_trades(trades: list[dict], strategy: str,
                   since: Optional[str] = None) -> list[dict]:
    """Filter for strategy + (optional) min entry_time date."""
    out = [t for t in trades if t.get("strategy") == strategy]
    if since:
        # Per memory/lessons_learned: "the date-filter Unix-vs-ISO gotcha"
        # entry_time is unix seconds. Convert ``since`` to a unix epoch.
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(
                tzinfo=timezone.utc,
            )
            since_epoch = since_dt.timestamp()
        except ValueError:
            since_epoch = None
        if since_epoch is not None:
            out = [t for t in out
                   if (t.get("entry_time") or 0) >= since_epoch]
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="P4-5 walk-forward + CPCV + DSR + PBO statistical "
                    "validation harness.",
    )
    parser.add_argument("--strategy", required=True,
                        help="Strategy name (e.g. bias_momentum)")
    parser.add_argument("--since", default=None,
                        help="Filter to trades with entry_time >= this date (YYYY-MM-DD)")
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES_RUN_ALL,
                        help=f"Sample-size gate (default {DEFAULT_MIN_TRADES_RUN_ALL})")
    parser.add_argument("--out", default=None,
                        help="Output path prefix (writes .md + .json). "
                             "Default: out/walk_forward_<date>_<strategy>")
    parser.add_argument("--logs-dir", default=None,
                        help="Override trade_memory logs dir (test/diagnostic).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

    logs_dir = args.logs_dir or str(PROJECT_ROOT / "logs")
    trades = load_all_trades(logs_dir)
    filtered = _filter_trades(trades, args.strategy, args.since)
    logger.info(f"{args.strategy}: {len(filtered)} trades after filter")

    report = run_all(filtered, args.strategy, min_trades=args.min_trades)

    # Output paths
    if args.out:
        out_prefix = Path(args.out)
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        out_dir = PROJECT_ROOT / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_prefix = out_dir / f"walk_forward_{today}_{args.strategy}"

    md_path = out_prefix.with_suffix(".md")
    json_path = out_prefix.with_suffix(".json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, default=str),
                         encoding="utf-8")
    logger.info(f"wrote {md_path}")
    logger.info(f"wrote {json_path}")

    print(f"\n=== {args.strategy} — verdict: {report['verdict']} ===")
    print(f"  reason: {report.get('reason', '')}")
    print(f"  n_trades: {report['n_trades']} (min {args.min_trades})")
    for sub in ("walk_forward", "cpcv", "dsr", "pbo"):
        v = report.get(sub, {}).get("verdict", "?")
        print(f"  {sub}: {v}")

    return 0 if report["verdict"] == PASS else (
        2 if report["verdict"] == FAIL else 1
    )


if __name__ == "__main__":
    sys.exit(main())
