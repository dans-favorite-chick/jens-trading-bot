"""
wfa.py -- Walk-Forward Analysis (Phase 1.1 of the portfolio_backtest spec).

WHAT THIS DOES
--------------
Rolling 12-month in-sample (IS) parameter-optimization windows followed by
3-month out-of-sample (OOS) forward tests. For each window we:

  1. Build ONE CSVEnrichmentPipeline over the IS window.
  2. Instantiate EVERY grid combo of EVERY requested strategy as a separate
     dict entry (keyed "name#param=val|param=val"), and run the harness's
     run_backtest ONCE over that shared pass (all combos see the same single
     no-look-ahead enrichment pass -- this is the whole efficiency win).
  3. Score each combo by NET profit_factor (with execution friction on) and
     pick the best combo per base strategy.
  4. Re-run ONLY the winning combo per strategy on a fresh OOS pipeline.
  5. Flag degradation when OOS profit factor falls below 80% of IS PF
     (i.e. > 20% PF degradation). WFE = oos_pf / is_pf.

DESIGN NOTES
------------
* Parameter injection: phoenix_real_backtest.instantiate_strategies() only
  knows the canonical config and rejects unknown dict keys, so we build our
  OWN instantiator. We get each strategy's CLASS once via a single
  instantiate_strategies([name]) call (type(inst)) and cache it, then build
  fresh instances with an overridden copy of STRATEGIES[name]'s cfg dict.
  This mirrors the harness's own instantiation (cfg = dict(STRATEGIES[name]);
  cfg['is_prod_bot'] = False; class(cfg)) but applies our param overrides on
  top -- so a combo is just the canonical config with a few keys replaced.

* Realistic scoring: we set phoenix_real_backtest.APPLY_EXECUTION_DECAY = True
  (unless --no-friction) so PF reflects NET-of-friction performance.

* TradeResult.strategy carries the dict KEY we passed in (run_backtest uses
  the dict key as signal_strategy), so analyze_results' 'strategy' column IS
  the combo key -- grouping by it separates combos cleanly.

ASCII-ONLY printed output: this machine's console is cp1252. All printed /
returned string literals are plain ASCII (no unicode arrows, em-dashes, etc.).

INTERPRETER
-----------
    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe

VERIFY (one real window, keep it short -- live bots share this CPU):
    python tools/portfolio_backtest/wfa.py --strategies bias_momentum \\
        --start 2024-09-01 --end 2025-12-31 \\
        --is-months 12 --oos-months 3 --step-months 3 --grid lean
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ---- worktree root on sys.path so we can import the harness + config -------
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.portfolio_backtest import paths  # noqa: E402
import tools.phoenix_real_backtest as prb  # noqa: E402
from tools.phoenix_real_backtest import (  # noqa: E402
    CSVEnrichmentPipeline,
    instantiate_strategies,
    run_backtest,
    analyze_results,
    TESTABLE_STRATEGIES,
)
from tools.portfolio_backtest.analytics import profit_factor  # noqa: E402


# ====================================================================
# PARAM_GRID -- per-strategy candidate values.
# ====================================================================
# Every key below was VERIFIED against config/strategies.py (the live
# STRATEGIES dict). We only sweep keys the strategy's config block actually
# defines, so an override changes a real gate/parameter rather than being
# silently ignored. Two grid sizes:
#   "lean" -- 1-2 params, 2-3 values each (fast; for verification / smoke).
#   "full" -- up to 3 params, kept <= ~27 combos/strategy.
# Strategies without an entry fall back to a single no-override combo so they
# still get walked (they just are not optimized).

# --- LEAN grid (small, for quick walks / verification) --------------
PARAM_GRID_LEAN: dict[str, dict[str, list]] = {
    # bias_momentum: RR + the EMA-stack confluence gate are its two biggest
    # earners-vs-frequency knobs (base_rr_ratio is the documented default key;
    # the block also carries target_rr -- we sweep both names so whichever the
    # class reads is covered).
    "bias_momentum": {
        "target_rr": [2.0, 2.5, 3.0],
        "min_confluence": [4.5, 5.5],
    },
    # opening_session: trades/day cap is the dominant frequency knob; the
    # open-drive displacement gate is the dominant selectivity knob.
    "opening_session": {
        "max_trades_per_day": [2, 4],
        "open_drive_min_displacement_pts": [6, 8],
    },
    # es_nq_confluence: the divergence-trigger threshold is THE entry gate.
    "es_nq_confluence": {
        "boost_threshold": [20.0, 25.0, 30.0],
    },
    # ib_breakout: target extension multiple + IB-width tolerance.
    "ib_breakout": {
        "target_extension": [1.5, 2.0],
        "max_ib_width_atr_mult": [3.0, 4.0],
    },
    # compression_breakout_v2: BB std width (squeeze tightness) + RR.
    "compression_breakout_v2": {
        "bb_std": [1.4, 1.5],
        "target_rr": [1.5, 2.0],
    },
    # vwap_band_pullback: RR + the multi-TF alignment vote count.
    "vwap_band_pullback": {
        "target_rr": [1.8, 2.0],
        "min_tf_votes": [2, 3],
    },
    # noise_area: cone width multiple + re-trade cadence.
    "noise_area": {
        "band_mult": [0.7, 1.0],
        "trade_freq_minutes": [30, 60],
    },
}

# --- FULL grid (wider; <= ~27 combos/strategy) ----------------------
PARAM_GRID_FULL: dict[str, dict[str, list]] = {
    "bias_momentum": {
        # 3 x 3 x 3 = 27 combos.
        "target_rr": [2.0, 2.5, 3.0],
        "min_confluence": [4.5, 5.5, 6.5],
        "stop_atr_mult": [1.5, 2.0, 2.5],
    },
    "opening_session": {
        # 3 x 2 x 2 = 12 combos.
        "open_drive_min_displacement_pts": [6, 8, 10],
        "max_trades_per_day": [2, 4],
        "orb_target_pct_of_or": [0.50, 0.75],
    },
    "es_nq_confluence": {
        # 3 x 3 = 9 combos.
        "boost_threshold": [20.0, 25.0, 30.0],
        "corr_threshold": [0.80, 0.85, 0.90],
    },
    "ib_breakout": {
        # 3 x 2 x 2 = 12 combos.
        "target_extension": [1.5, 2.0, 2.5],
        "max_ib_width_atr_mult": [3.0, 4.0],
        "ib_minutes": [10, 15],
    },
    "compression_breakout_v2": {
        # 3 x 3 = 9 combos.
        "bb_std": [1.4, 1.5, 1.6],
        "target_rr": [1.5, 2.0, 2.5],
    },
    "vwap_band_pullback": {
        # 3 x 2 x 2 = 12 combos.
        "target_rr": [1.5, 1.8, 2.0],
        "min_tf_votes": [2, 3],
        "min_volume_ratio": [0.8, 1.0],
    },
    "noise_area": {
        # 3 x 3 = 9 combos.
        "band_mult": [0.7, 1.0, 1.3],
        "trade_freq_minutes": [30, 45, 60],
    },
    # Phase 13 lab strategies (added 2026-05-31 after promotion to harness).
    # Grids exercise their dominant edge/frequency knobs from config/strategies.py.
    "a_asian_continuation": {
        # 3 x 3 = 9 combos.
        "target_rr": [1.5, 2.0, 2.5],
        "range_break_atr_mult": [0.3, 0.5, 0.7],
    },
    "e_multi_day_breakout": {
        # 3 x 3 = 9 combos.
        "target_rr": [1.5, 2.0, 2.5],
        "lookback_days": [2, 3, 4],
    },
    "g_inside_bar_breakout": {
        # 3 x 3 = 9 combos.
        "target_rr": [1.5, 2.0, 2.5],
        "min_inside_range_ticks": [3, 4, 5],
    },
    "raschke_baseline": {
        # 3 x 3 = 9 combos.
        "target_rr": [1.5, 2.0, 2.5],
        "trend_spread_atr": [0.2, 0.3, 0.4],
    },
}

PARAM_GRID: dict[str, dict[str, list]] = PARAM_GRID_FULL  # module-level default


# ====================================================================
# Combo-key encoding (round-trippable, ASCII-only)
# ====================================================================

_KEY_SEP = "#"
_PAIR_SEP = "|"
_KV_SEP = "="


def _combo_key(strategy: str, overrides: dict) -> str:
    """Encode a (strategy, overrides) combo as a flat ASCII dict key.

    "bias_momentum#min_confluence=4.5|target_rr=2.0"  (overrides sorted).
    A no-override combo is just the bare strategy name.
    """
    if not overrides:
        return strategy
    # Guard: combo-key separators must not appear in any param value, else the
    # key (used only for REPORTING best_params; OOS carries the real override
    # dict forward) would decode incorrectly. Grids today are int/float-only so
    # this never fires -- it just fails LOUD if a future grid ever sweeps a
    # string value containing a separator.
    for _k, _v in overrides.items():
        assert not any(s in str(_v) for s in (_KEY_SEP, _KV_SEP, _PAIR_SEP)), (
            f"param {_k}={_v!r} contains a combo-key separator "
            f"({_KEY_SEP!r}/{_KV_SEP!r}/{_PAIR_SEP!r}); not encodable")
    parts = [f"{k}{_KV_SEP}{overrides[k]}" for k in sorted(overrides)]
    return f"{strategy}{_KEY_SEP}{_PAIR_SEP.join(parts)}"


def _base_strategy(combo_key: str) -> str:
    """Recover the base strategy name from a combo key."""
    return combo_key.split(_KEY_SEP, 1)[0]


def _combo_grid(strategy: str, grid: dict[str, list]) -> list[dict]:
    """Expand a {param: [values]} grid into a list of override dicts.

    Empty grid -> a single empty override (the canonical config, no change).
    """
    sub = grid.get(strategy)
    if not sub:
        return [{}]
    keys = list(sub.keys())
    out: list[dict] = []
    for combo in itertools.product(*(sub[k] for k in keys)):
        out.append({k: v for k, v in zip(keys, combo)})
    return out


# ====================================================================
# Custom instantiator with parameter overrides
# ====================================================================

_CLASS_CACHE: dict[str, type] = {}


def _strategy_class(name: str):
    """Resolve a strategy's class via one harness instantiate_strategies call,
    caching the result. Returns None if the harness can't build it (no class /
    no config), in which case the caller skips that strategy."""
    if name in _CLASS_CACHE:
        return _CLASS_CACHE[name]
    built = instantiate_strategies([name])
    inst = built.get(name)
    if inst is None:
        _CLASS_CACHE[name] = None
        return None
    cls = type(inst)
    _CLASS_CACHE[name] = cls
    return cls


def _instantiate_combos(strategy_names: list[str],
                        grid: dict[str, list]) -> dict:
    """Build a {combo_key: strategy_instance} dict spanning ALL grid combos of
    ALL requested strategies. Mirrors the harness's own instantiation
    (cfg = dict(STRATEGIES[name]); cfg['is_prod_bot'] = False) but applies our
    overrides on top. Combos that fail to instantiate are skipped with a note.
    """
    from config.strategies import STRATEGIES

    out: dict = {}
    for name in strategy_names:
        cls = _strategy_class(name)
        if cls is None:
            print(f"[wfa] WARN no class/config for '{name}'; skipping")
            continue
        if name not in STRATEGIES:
            print(f"[wfa] WARN '{name}' not in STRATEGIES; skipping")
            continue
        base_cfg = dict(STRATEGIES[name])
        for overrides in _combo_grid(name, grid):
            cfg = dict(base_cfg)
            cfg["is_prod_bot"] = False
            cfg.update(overrides)
            key = _combo_key(name, overrides)
            try:
                out[key] = cls(cfg)
            except Exception as exc:  # noqa: BLE001
                print(f"[wfa] WARN failed to instantiate '{key}': {exc!r}")
    return out


# ====================================================================
# Window math
# ====================================================================

def _build_windows(start: str, end: str, is_months: int, oos_months: int,
                   step_months: int) -> list[dict]:
    """Rolling calendar-anchored windows. Window k:
        IS  = [t0,            t0 + is_months)
        OOS = [t0 + is_months, t0 + is_months + oos_months)
    stepping t0 by step_months until the OOS end exceeds the overall end.

    Dates are returned as inclusive 'YYYY-MM-DD' strings for the harness's
    inclusive-ish UTC filter (we pass the day BEFORE the exclusive boundary as
    the inclusive end).
    """
    t_start = pd.Timestamp(start)
    t_end = pd.Timestamp(end)
    windows: list[dict] = []
    idx = 0
    t0 = t_start
    while True:
        is_lo = t0
        is_hi_excl = t0 + pd.DateOffset(months=is_months)
        oos_lo = is_hi_excl
        oos_hi_excl = oos_lo + pd.DateOffset(months=oos_months)
        # Stop once the OOS window would extend past the overall end.
        if oos_hi_excl > t_end + pd.Timedelta(days=1):
            break
        windows.append({
            "window_idx": idx,
            "is_start": is_lo.strftime("%Y-%m-%d"),
            "is_end": (is_hi_excl - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            "oos_start": oos_lo.strftime("%Y-%m-%d"),
            "oos_end": (oos_hi_excl - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        })
        idx += 1
        t0 = t0 + pd.DateOffset(months=step_months)
    return windows


# ====================================================================
# Pipeline + scoring helpers
# ====================================================================

def _make_pipeline(start: str, end: str) -> CSVEnrichmentPipeline:
    """Build a fresh enrichment pipeline for [start, end] using paths.py
    (always resolves to the main checkout's data, never the worktree).

    NOTE (perf): reloads all four OHLCV CSVs (~1.77M rows each) per call, i.e.
    once per IS and once per OOS window (~2x n_windows full reads). For a
    multi-year quarterly walk that is ~5-20 min of pure CSV I/O on top of the
    backtest compute -- acceptable for an overnight sweep. Future optimization:
    load the four DataFrames once in run_wfa and slice per window (the harness
    supports __new__-constructed pipelines); deferred to avoid refactoring the
    data path right before an unattended multi-hour run.
    """
    return CSVEnrichmentPipeline(
        mnq_1m_csv=str(paths.MNQ_1M_CSV),
        mnq_5m_csv=str(paths.MNQ_5M_CSV),
        mes_1m_csv=str(paths.MES_1M_CSV),
        mes_5m_csv=str(paths.MES_5M_CSV),
        start=start, end=end,
    )


def _pf(pnl: np.ndarray) -> float:
    """Profit factor with a finite-or-zero guard (avoids inf flowing into the
    degradation / WFE math). Returns 0.0 for an all-loss or empty set, and a
    large finite sentinel for an all-win set."""
    pf = profit_factor(pnl)
    if not np.isfinite(pf):
        # All-win (no losses) -> treat as a large-but-finite PF so ratios work.
        return 999.0 if pf == float("inf") else 0.0
    return float(pf)


def _score_combos(trades_df: pd.DataFrame) -> dict[str, dict]:
    """Group analyze_results output by combo key, return per-combo
    {pf, n, net}. Empty df -> empty dict."""
    out: dict[str, dict] = {}
    if trades_df is None or trades_df.empty:
        return out
    for combo_key, sub in trades_df.groupby("strategy"):
        pnl = sub["pnl_dollars"].to_numpy(dtype="float64")
        out[combo_key] = {
            "pf": _pf(pnl),
            "n": int(len(sub)),
            "net": round(float(pnl.sum()), 2),
        }
    return out


def _best_per_strategy(combo_scores: dict[str, dict],
                       strategy_names: list[str],
                       grid: dict[str, list]) -> dict[str, dict]:
    """Pick the best combo (max PF, tiebreak max net) per base strategy.

    A strategy with ZERO combos that produced trades still gets an entry: its
    canonical no-override combo with pf=0, n=0 so the OOS step has something to
    re-run (and the windows row records the 0-trade fact rather than dropping
    the strategy silently).
    """
    best: dict[str, dict] = {}
    for combo_key, sc in combo_scores.items():
        base = _base_strategy(combo_key)
        # Recover the override dict from the combo key for reporting.
        overrides = _decode_overrides(combo_key)
        cand = {"combo_key": combo_key, "overrides": overrides, **sc}
        cur = best.get(base)
        if cur is None or (sc["pf"], sc["net"]) > (cur["pf"], cur["net"]):
            best[base] = cand
    # Ensure every requested strategy has an entry.
    for name in strategy_names:
        if name not in best:
            # Default to the first grid combo (canonical if no grid).
            first = _combo_grid(name, grid)[0]
            best[name] = {
                "combo_key": _combo_key(name, first),
                "overrides": first,
                "pf": 0.0, "n": 0, "net": 0.0,
            }
    return best


def _decode_overrides(combo_key: str) -> dict:
    """Inverse of _combo_key for reporting. Values are decoded to int/float
    where possible, else left as strings."""
    if _KEY_SEP not in combo_key:
        return {}
    _, payload = combo_key.split(_KEY_SEP, 1)
    out: dict = {}
    for pair in payload.split(_PAIR_SEP):
        if _KV_SEP not in pair:
            continue
        k, v = pair.split(_KV_SEP, 1)
        out[k] = _coerce(v)
    return out


def _coerce(v: str):
    """Best-effort string -> int/float, else original string."""
    try:
        iv = int(v)
        return iv
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


# ====================================================================
# PUBLIC CONTRACT
# ====================================================================

_WINDOW_COLUMNS = [
    "strategy", "window_idx", "is_start", "is_end", "oos_start", "oos_end",
    "best_params", "is_pf", "is_trades", "oos_pf", "oos_trades", "oos_net",
    "wfe", "degraded",
]


def run_wfa(strategies: list[str], start: str, end: str,
            is_months: int = 12, oos_months: int = 3, step_months: int = 3,
            grid: str = "full", apply_friction: bool = True,
            warmup_min: int = 300, out_csv: Optional[str] = None,
            max_windows: Optional[int] = None) -> pd.DataFrame:
    """Walk-Forward Analysis. One row per (strategy, window).

    Returns a DataFrame with columns:
        strategy, window_idx, is_start, is_end, oos_start, oos_end,
        best_params (json str), is_pf, is_trades, oos_pf, oos_trades, oos_net,
        wfe, degraded

    Also writes that DataFrame to out_csv (or OUT_DIR/'wfa_windows.csv').

    degraded = oos_pf < 0.80 * is_pf  (i.e. > 20% PF degradation)
    wfe      = oos_pf / is_pf         (0.0 when is_pf == 0)
    """
    paths.verify(require_ticks=False)

    grid_map = PARAM_GRID_LEAN if grid == "lean" else PARAM_GRID_FULL

    # Realistic NET scoring: net round-turn friction out of every trade's P&L.
    prb.APPLY_EXECUTION_DECAY = bool(apply_friction)

    windows = _build_windows(start, end, is_months, oos_months, step_months)
    if not windows:
        raise ValueError(
            f"No walk-forward windows fit in [{start}, {end}] with "
            f"is={is_months}m oos={oos_months}m step={step_months}m. "
            f"Need at least is_months + oos_months of span."
        )
    if max_windows is not None:
        windows = windows[:max_windows]

    print(f"[wfa] {len(windows)} window(s); strategies={strategies}; "
          f"grid={grid}; friction={'on' if apply_friction else 'off'}")

    rows: list[dict] = []
    for w in windows:
        wi = w["window_idx"]
        print(f"\n[wfa] ===== window {wi}: IS {w['is_start']}..{w['is_end']} "
              f"-> OOS {w['oos_start']}..{w['oos_end']} =====")

        # ---- IS: build one pipeline, run ALL combos in a single pass ----
        t0 = time.time()
        is_pipe = _make_pipeline(w["is_start"], w["is_end"])
        is_combos = _instantiate_combos(strategies, grid_map)
        n_combos = len(is_combos)
        print(f"[wfa] IS: {n_combos} combo(s) across {len(strategies)} "
              f"strategy(ies); one shared backtest pass...")
        is_trades = run_backtest(is_pipe, is_combos, warmup_min=warmup_min)
        is_df = analyze_results(is_trades)
        is_scores = _score_combos(is_df)
        best = _best_per_strategy(is_scores, strategies, grid_map)
        print(f"[wfa] IS done in {time.time()-t0:.0f}s; "
              f"best combos: " + ", ".join(
                  f"{s}->PF={d['pf']:.2f}(n={d['n']})" for s, d in best.items()))

        # ---- OOS: re-run ONLY the winning combo per strategy ----
        t1 = time.time()
        oos_pipe = _make_pipeline(w["oos_start"], w["oos_end"])
        # Build the winners dict, re-keyed to the SAME combo key so we can map
        # results back. Two strategies could in principle collide only if their
        # base names differ -- combo keys are unique by construction.
        oos_specs = {d["combo_key"]: (s, d["overrides"])
                     for s, d in best.items()}
        oos_combos = _instantiate_combos_explicit(oos_specs)
        oos_trades = run_backtest(oos_pipe, oos_combos, warmup_min=warmup_min)
        oos_df = analyze_results(oos_trades)
        oos_scores = _score_combos(oos_df)
        print(f"[wfa] OOS done in {time.time()-t1:.0f}s")

        # ---- assemble one row per strategy ----
        for strat in strategies:
            b = best[strat]
            is_pf = b["pf"]
            is_n = b["n"]
            o = oos_scores.get(b["combo_key"], {"pf": 0.0, "n": 0, "net": 0.0})
            oos_pf = o["pf"]
            oos_n = o["n"]
            oos_net = o["net"]
            wfe = round(oos_pf / is_pf, 4) if is_pf > 0 else 0.0
            degraded = bool(oos_pf < 0.80 * is_pf)
            rows.append({
                "strategy": strat,
                "window_idx": wi,
                "is_start": w["is_start"],
                "is_end": w["is_end"],
                "oos_start": w["oos_start"],
                "oos_end": w["oos_end"],
                "best_params": json.dumps(b["overrides"], sort_keys=True),
                "is_pf": round(is_pf, 4),
                "is_trades": is_n,
                "oos_pf": round(oos_pf, 4),
                "oos_trades": oos_n,
                "oos_net": oos_net,
                "wfe": wfe,
                "degraded": degraded,
            })

    df = pd.DataFrame(rows, columns=_WINDOW_COLUMNS)

    # ---- write windows CSV ----
    out_path = Path(out_csv) if out_csv else (paths.OUT_DIR / "wfa_windows.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\n[wfa] wrote {len(df)} window-rows to {out_path}")
    return df


def _instantiate_combos_explicit(specs: dict[str, tuple]) -> dict:
    """Build {combo_key: instance} from an explicit
    {combo_key: (strategy_name, overrides)} spec. Used for the OOS pass, where
    we want EXACTLY the winning combos (not the whole grid)."""
    from config.strategies import STRATEGIES

    out: dict = {}
    for combo_key, (name, overrides) in specs.items():
        cls = _strategy_class(name)
        if cls is None or name not in STRATEGIES:
            print(f"[wfa] WARN cannot build OOS combo '{combo_key}'; skipping")
            continue
        cfg = dict(STRATEGIES[name])
        cfg["is_prod_bot"] = False
        cfg.update(overrides)
        try:
            out[combo_key] = cls(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[wfa] WARN failed OOS instantiate '{combo_key}': {exc!r}")
    return out


_SUMMARY_COLUMNS = [
    "strategy", "n_windows", "mean_is_pf", "mean_oos_pf", "median_oos_pf",
    "pct_windows_degraded", "robust",
]


def summarize_wfa(df: pd.DataFrame) -> pd.DataFrame:
    """One row per strategy aggregating the per-window WFA results.

    Columns:
        strategy, n_windows, mean_is_pf, mean_oos_pf, median_oos_pf,
        pct_windows_degraded, robust
    where robust = (pct_windows_degraded <= 0.34) and (mean_oos_pf >= 1.3).

    Writes to OUT_DIR/'wfa_summary.csv'.
    """
    rows: list[dict] = []
    if df is not None and not df.empty:
        for strat, sub in df.groupby("strategy"):
            n = int(len(sub))
            mean_is = float(sub["is_pf"].mean())
            mean_oos = float(sub["oos_pf"].mean())
            median_oos = float(sub["oos_pf"].median())
            pct_deg = float(sub["degraded"].mean())  # fraction in [0,1]
            robust = bool((pct_deg <= 0.34) and (mean_oos >= 1.3))
            rows.append({
                "strategy": strat,
                "n_windows": n,
                "mean_is_pf": round(mean_is, 4),
                "mean_oos_pf": round(mean_oos, 4),
                "median_oos_pf": round(median_oos, 4),
                "pct_windows_degraded": round(pct_deg, 4),
                "robust": robust,
            })
    out = pd.DataFrame(rows, columns=_SUMMARY_COLUMNS)
    out_path = paths.OUT_DIR / "wfa_summary.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    return out


# ====================================================================
# CLI
# ====================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Walk-Forward Analysis (12m IS / 3m OOS, "
                    ">20% OOS-PF-degradation flag).")
    ap.add_argument("--strategies", default="bias_momentum",
                    help="Comma-separated strategy names, or 'all' "
                         "(= TESTABLE_STRATEGIES).")
    ap.add_argument("--start", required=True, help="Overall start YYYY-MM-DD.")
    ap.add_argument("--end", required=True, help="Overall end YYYY-MM-DD.")
    ap.add_argument("--is-months", type=int, default=12,
                    help="In-sample window length in months (default 12).")
    ap.add_argument("--oos-months", type=int, default=3,
                    help="Out-of-sample window length in months (default 3).")
    ap.add_argument("--step-months", type=int, default=3,
                    help="Roll step in months (default 3).")
    ap.add_argument("--grid", choices=["lean", "full"], default="full",
                    help="Parameter grid size (default full).")
    ap.add_argument("--no-friction", action="store_true",
                    help="Disable round-turn execution friction (default ON).")
    ap.add_argument("--warmup", type=int, default=300,
                    help="Warmup cycles before evaluating (default 300).")
    ap.add_argument("--out", default=None,
                    help="Windows CSV path (default OUT_DIR/wfa_windows.csv).")
    ap.add_argument("--quick", action="store_true",
                    help="Quick single-window smoke: forces grid=lean and the "
                         "first window only (1 strategy recommended).")
    args = ap.parse_args()

    if args.strategies == "all":
        names = list(TESTABLE_STRATEGIES)
    else:
        names = [s.strip() for s in args.strategies.split(",") if s.strip()]

    grid = "lean" if args.quick else args.grid

    df = run_wfa(
        strategies=names,
        start=args.start,
        end=args.end,
        is_months=args.is_months,
        oos_months=args.oos_months,
        step_months=args.step_months,
        grid=grid,
        apply_friction=(not args.no_friction),
        warmup_min=args.warmup,
        out_csv=args.out,
        max_windows=(1 if args.quick else None),
    )

    summary = summarize_wfa(df)

    print("\n" + "=" * 88)
    print("WALK-FORWARD ANALYSIS SUMMARY")
    print("=" * 88)
    if summary.empty:
        print("No results (no windows produced any rows).")
    else:
        print(summary.to_string(index=False))
        print()
        deg = df[df["degraded"]]
        print(f"Total window-rows: {len(df)}  |  degraded rows: {len(deg)} "
              f"(OOS PF < 0.80 * IS PF)")
        robust_strats = summary[summary["robust"]]["strategy"].tolist()
        print(f"Robust strategies: "
              f"{robust_strats if robust_strats else '(none)'}")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
