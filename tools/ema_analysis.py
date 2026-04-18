"""
EMA Analysis Tool — Phoenix Bot
Analyzes MNQ bar data to answer:
  1. How far does price typically get from EMA9 before reversing, by session regime?
  2. Is EMA9 the best MA for precision entries vs EMA5/8/13/21?

Data sources:
  - C:/Trading Project/phoenix_bot/logs/history/  (JSONL bar + trade events)
  - C:/Trading Project/phoenix_bot/data/aggregator_state_*.json

TICK_SIZE = 0.25 (MNQ)
"""

import json
import os
import math
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd

TICK_SIZE = 0.25
HISTORY_DIR = "C:/Trading Project/phoenix_bot/logs/history/"
DATA_DIR = "C:/Trading Project/phoenix_bot/data/"

# Session regimes (CST = UTC-6 during CDT / UTC-5 during CST; April = CDT = UTC-5)
# NinjaTrader/Phoenix uses CT. April 2026 = CDT (UTC-5)
# We'll detect regime from the 'regime' field in bar events (already computed by bot).
TRADING_REGIMES = ["OPEN_MOMENTUM", "MID_MORNING", "AFTERNOON_CHOP", "LATE_AFTERNOON"]
ALL_REGIMES = TRADING_REGIMES + ["OVERNIGHT_RANGE", "PREMARKET_DRIFT", "CLOSE_CHOP", "AFTERHOURS"]

MA_PERIODS = [5, 8, 9, 13, 21]

# ----------------------------------------------
# EMA computation
# ----------------------------------------------

def compute_ema(closes: list[float], period: int) -> list[float]:
    """Compute EMA for a list of closes. Returns list of same length; NaN until period reached."""
    k = 2.0 / (period + 1)
    ema = [float('nan')] * len(closes)
    # seed with SMA
    if len(closes) < period:
        return ema
    seed = sum(closes[:period]) / period
    ema[period - 1] = seed
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


# ----------------------------------------------
# Data loading
# ----------------------------------------------

def load_all_bar_events() -> list[dict]:
    """Load all 1m bar events from all history JSONL files."""
    all_bars = []
    files = sorted(f for f in os.listdir(HISTORY_DIR) if f.endswith('.jsonl'))
    for fname in files:
        path = os.path.join(HISTORY_DIR, fname)
        with open(path, 'r') as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get('event') == 'bar' and rec.get('timeframe') == '1m':
                        all_bars.append(rec)
                except json.JSONDecodeError:
                    continue
    print(f"[load] Loaded {len(all_bars)} total 1m bar events from {len(files)} files")
    return all_bars


def load_all_exit_events() -> list[dict]:
    """Load all exit events (have MFE/MAE ticks)."""
    exits = []
    files = sorted(f for f in os.listdir(HISTORY_DIR) if f.endswith('.jsonl'))
    for fname in files:
        path = os.path.join(HISTORY_DIR, fname)
        with open(path, 'r') as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get('event') == 'exit':
                        exits.append(rec)
                except json.JSONDecodeError:
                    continue
    return exits


def load_aggregator_bars(filename: str) -> list[dict]:
    """Load bars_1m from an aggregator state JSON file."""
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return []
    with open(path, 'r') as f:
        state = json.load(f)
    return state.get('bars_1m', [])


# ----------------------------------------------
# Build clean DataFrame from history bar events
# ----------------------------------------------

def build_bar_dataframe(bar_events: list[dict]) -> pd.DataFrame:
    """
    Build a clean DataFrame from bar events. De-duplicate by timestamp+bot.
    Uses the bot's stored ema9 where valid; recomputes all MA periods from scratch per day/bot.
    """
    rows = []
    for b in bar_events:
        ts_str = b.get('ts', '')
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        ema9_stored = b.get('ema9', 0.0)
        rows.append({
            'ts': ts,
            'date': ts.date(),
            'bot': b.get('bot', 'unknown'),
            'open': float(b.get('open', 0)),
            'high': float(b.get('high', 0)),
            'low': float(b.get('low', 0)),
            'close': float(b.get('close', 0)),
            'volume': int(b.get('volume', 0)),
            'tick_count': int(b.get('tick_count', 0)),
            'regime': b.get('regime', 'UNKNOWN'),
            'vwap': float(b.get('vwap', 0)),
            'ema9_stored': float(ema9_stored),
            'ema21_stored': float(b.get('ema21', 0)),
            'atr_1m': float(b.get('atr_1m', 0)),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # De-duplicate: keep prod over lab for same timestamp
    df = df.sort_values(['ts', 'bot'])
    df['ts_rounded'] = df['ts'].dt.floor('1min')
    df = df.drop_duplicates(subset=['ts_rounded', 'bot'], keep='last')
    # Prefer prod bars over lab bars (same market data, prod is canonical)
    df = df.sort_values(['ts_rounded', 'bot'], ascending=[True, False])
    df = df.drop_duplicates(subset=['ts_rounded'], keep='first')
    df = df.sort_values('ts').reset_index(drop=True)

    print(f"[build] After dedup: {len(df)} bars, dates: {df['date'].min()} to {df['date'].max()}")
    return df


# ----------------------------------------------
# Recompute all EMA periods from scratch
# ----------------------------------------------

def add_computed_emas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute EMA5/8/9/13/21 from raw close prices for each trading day separately
    (EMA resets at session open per NinjaTrader convention used by Phoenix).
    Also computes a rolling EMA across all bars (no reset) for comparison.
    """
    if df.empty:
        return df

    closes = df['close'].tolist()

    # Rolling EMA across entire dataset (no daily reset)
    for period in MA_PERIODS:
        col = f'ema{period}_rolling'
        df[col] = compute_ema(closes, period)

    # Per-day EMA (resets each day — matches bot behavior)
    for period in MA_PERIODS:
        col = f'ema{period}'
        ema_vals = [float('nan')] * len(df)
        for date, grp in df.groupby('date'):
            idxs = grp.index.tolist()
            day_closes = [df.at[i, 'close'] for i in idxs]
            day_emas = compute_ema(day_closes, period)
            for j, idx in enumerate(idxs):
                ema_vals[idx] = day_emas[j]
        df[col] = ema_vals

    # Prefer stored ema9 from bot where it's non-zero and we have daily computed
    # (bot's ema9 may have more history behind it; our per-day recompute is conservative)
    df['ema9_best'] = np.where(
        (df['ema9_stored'] != 0) & ~df['ema9_stored'].isna(),
        df['ema9_stored'],
        df['ema9']
    )

    return df


# ----------------------------------------------
# Distance / extension analysis
# ----------------------------------------------

def compute_distances(df: pd.DataFrame) -> pd.DataFrame:
    """
    For each bar, compute distance from each EMA in ticks (close - EMA) / TICK_SIZE.
    Also compute |distance| and whether the NEXT bar is closer to EMA (mean reversion).
    """
    df = df.copy()

    for period in MA_PERIODS:
        ema_col = f'ema{period}' if period != 9 else 'ema9_best'
        dist_col = f'dist_ema{period}'
        df[dist_col] = (df['close'] - df[ema_col]) / TICK_SIZE

    # Reversion analysis: for each bar, is the NEXT bar's |distance| smaller?
    for period in MA_PERIODS:
        dist_col = f'dist_ema{period}'
        abs_col = f'abs_dist_ema{period}'
        rev_col = f'reverted_ema{period}'
        df[abs_col] = df[dist_col].abs()
        # Next bar is closer (within 10 ticks is "near EMA")
        next_abs = df[abs_col].shift(-1)
        df[rev_col] = next_abs < df[abs_col]

    # Max extension before reversion (rolling: how far did it go before coming back within 10t)
    # We'll compute this at the regime-analysis stage

    return df


def compute_reversion_distances(df: pd.DataFrame, ema_col: str = 'ema9_best') -> pd.Series:
    """
    Compute "max extension before reversion" series.
    For each bar where abs_dist > 10 ticks, look forward until abs_dist drops back to <=10.
    Return the max abs_dist seen in that extension run.
    """
    if df.empty:
        return pd.Series(dtype=float)

    abs_dist = ((df['close'] - df[ema_col]) / TICK_SIZE).abs()
    results = []

    i = 0
    while i < len(abs_dist):
        d = abs_dist.iloc[i]
        if d > 10:
            # Start of extension run
            max_ext = d
            j = i + 1
            while j < len(abs_dist) and abs_dist.iloc[j] > 10:
                max_ext = max(max_ext, abs_dist.iloc[j])
                j += 1
            results.append({'start_idx': i, 'max_extension_ticks': max_ext, 'duration_bars': j - i})
            i = j
        else:
            i += 1

    if not results:
        return pd.Series(dtype=float)
    return pd.Series([r['max_extension_ticks'] for r in results])


# ----------------------------------------------
# Main analysis
# ----------------------------------------------

def analyze_by_regime(df: pd.DataFrame) -> dict:
    """
    For each regime, compute distance percentiles from each EMA.
    Returns nested dict: regime -> ema_period -> stats
    """
    results = {}

    for regime in ALL_REGIMES:
        sub = df[df['regime'] == regime].copy()
        if len(sub) < 5:
            continue

        regime_stats = {'bar_count': len(sub), 'periods': {}}

        for period in MA_PERIODS:
            abs_col = f'abs_dist_ema{period}'
            dist_col = f'dist_ema{period}'

            valid = sub[abs_col].dropna()
            valid = valid[valid > 0]  # exclude bars where EMA not yet computed

            if len(valid) < 3:
                continue

            p = np.percentile(valid, [25, 50, 75, 90, 95, 99])

            # Mean reversion stat: avg distance when the next bar is CLOSER
            rev_mask = sub[f'reverted_ema{period}'] == True
            mean_rev_dist = sub.loc[rev_mask, abs_col].mean() if rev_mask.sum() > 0 else float('nan')

            # Signed distribution (above vs below EMA)
            signed = sub[dist_col].dropna()
            signed = signed[sub[abs_col] > 0]
            pct_above = (signed > 0).mean() * 100

            regime_stats['periods'][period] = {
                'n': len(valid),
                'p25': round(p[0], 1),
                'p50': round(p[1], 1),
                'p75': round(p[2], 1),
                'p90': round(p[3], 1),
                'p95': round(p[4], 1),
                'p99': round(p[5], 1),
                'mean': round(valid.mean(), 1),
                'max': round(valid.max(), 1),
                'pct_above_ema': round(pct_above, 1),
                'mean_reversion_dist': round(mean_rev_dist, 1) if not math.isnan(mean_rev_dist) else None,
            }

        results[regime] = regime_stats

    return results


def analyze_reversion_events(df: pd.DataFrame) -> dict:
    """
    For each MA period, analyze how far extensions go before price reverts to within 10t of EMA.
    This is the "overextension" distribution.
    """
    results = {}

    for period in MA_PERIODS:
        ema_col = f'ema{period}' if period != 9 else 'ema9_best'

        # Only use bars where EMA is valid
        valid_df = df[df[ema_col].notna() & (df[ema_col] != 0)].copy()
        if valid_df.empty:
            continue

        ext_series = compute_reversion_distances(valid_df, ema_col)
        if ext_series.empty:
            results[period] = {'n': 0}
            continue

        p = np.percentile(ext_series, [25, 50, 75, 90, 95])
        results[period] = {
            'n_extension_runs': len(ext_series),
            'p25': round(p[0], 1),
            'p50': round(p[1], 1),
            'p75': round(p[2], 1),
            'p90': round(p[3], 1),
            'p95': round(p[4], 1),
            'mean': round(ext_series.mean(), 1),
            'max': round(ext_series.max(), 1),
        }

    return results


def analyze_ma_reversion_quality(df: pd.DataFrame) -> dict:
    """
    For each MA period, measure what % of bars that are 'extended' (>20t)
    see a reversion within 1-3 bars. This tells us which MA price respects most.
    """
    results = {}
    thresholds = [10, 20, 30, 40]

    for period in MA_PERIODS:
        abs_col = f'abs_dist_ema{period}'
        valid = df[df[abs_col].notna() & (df[abs_col] > 0)].copy()
        if len(valid) < 10:
            continue

        period_stats = {}
        for thresh in thresholds:
            extended = valid[valid[abs_col] >= thresh]
            if len(extended) == 0:
                period_stats[f'pct_revert_from_{thresh}t'] = None
                continue

            # Check if the bar AFTER an extended bar is closer to EMA
            rev_col = f'reverted_ema{period}'
            if rev_col in extended.columns:
                pct_rev = extended[rev_col].mean() * 100
                period_stats[f'pct_revert_from_{thresh}t'] = round(pct_rev, 1)
            else:
                period_stats[f'pct_revert_from_{thresh}t'] = None

        # Mean abs distance (lower = price hugs this MA more)
        period_stats['mean_abs_dist'] = round(valid[abs_col].mean(), 1)
        period_stats['median_abs_dist'] = round(valid[abs_col].median(), 1)
        period_stats['n_bars'] = len(valid)

        results[period] = period_stats

    return results


def analyze_trade_exits(exits: list[dict]) -> dict:
    """Analyze MFE/MAE from actual trade exit events."""
    if not exits:
        return {}

    df = pd.DataFrame(exits)
    df['mfe_ticks'] = pd.to_numeric(df.get('mfe_ticks', []), errors='coerce')
    df['mae_ticks'] = pd.to_numeric(df.get('mae_ticks', []), errors='coerce')
    df['pnl_ticks'] = pd.to_numeric(df.get('pnl_ticks', []), errors='coerce')

    # Compute ema9_dist at entry if we have entry event data
    results = {
        'total_trades': len(df),
        'strategies': {},
        'by_direction': {},
    }

    for direction in ['LONG', 'SHORT']:
        sub = df[df.get('direction', pd.Series()) == direction] if 'direction' in df.columns else pd.DataFrame()
        if len(sub) == 0:
            continue
        results['by_direction'][direction] = {
            'n': len(sub),
            'mfe_p50': round(sub['mfe_ticks'].median(), 1),
            'mfe_p75': round(sub['mfe_ticks'].quantile(0.75), 1),
            'mae_p50': round(sub['mae_ticks'].median(), 1),
            'mae_p75': round(sub['mae_ticks'].quantile(0.25), 1),
        }

    if 'strategy' in df.columns:
        for strat, grp in df.groupby('strategy'):
            mfe = grp['mfe_ticks'].dropna()
            mae = grp['mae_ticks'].dropna()
            pnl = grp['pnl_ticks'].dropna()
            results['strategies'][strat] = {
                'n': len(grp),
                'win_rate': round((pnl > 0).mean() * 100, 1),
                'mfe_mean': round(mfe.mean(), 1) if len(mfe) else None,
                'mfe_p75': round(mfe.quantile(0.75), 1) if len(mfe) else None,
                'mae_mean': round(mae.mean(), 1) if len(mae) else None,
                'mae_p75': round(mae.quantile(0.25), 1) if len(mae) else None,
            }

    return results


# ----------------------------------------------
# Printing / reporting
# ----------------------------------------------

def print_separator(char='-', width=80):
    print(char * width)

def print_regime_table(regime_results: dict):
    print()
    print_separator('=')
    print("TABLE 1: EMA9 DISTANCE PERCENTILES BY SESSION REGIME (ticks, 1 tick = $0.50 MNQ)")
    print_separator('=')

    header = f"{'Regime':<22} {'N':>5} {'P25':>6} {'P50':>6} {'P75':>6} {'P90':>6} {'P95':>6} {'P99':>6} {'Mean':>6} {'Max':>6} {'MeanRevDist':>12}"
    print(header)
    print_separator()

    for regime in TRADING_REGIMES + ["OVERNIGHT_RANGE", "PREMARKET_DRIFT", "CLOSE_CHOP", "AFTERHOURS"]:
        if regime not in regime_results:
            continue
        r = regime_results[regime]
        if 9 not in r['periods']:
            continue
        s = r['periods'][9]
        mrd = f"{s['mean_reversion_dist']}t" if s['mean_reversion_dist'] else 'N/A'
        print(f"{regime:<22} {s['n']:>5} {s['p25']:>5}t {s['p50']:>5}t {s['p75']:>5}t {s['p90']:>5}t {s['p95']:>5}t {s['p99']:>5}t {s['mean']:>5}t {s['max']:>5}t {mrd:>12}")


def print_ma_comparison_table(regime_results: dict):
    print()
    print_separator('=')
    print("TABLE 2: MEDIAN ABS DISTANCE BY MA PERIOD — which MA does price hug most?")
    print("         (lower median = price stays closer = better as support/resistance)")
    print_separator('=')

    header = f"{'Regime':<22} {'N':>5} " + " ".join(f"{'EMA'+str(p):>8}" for p in MA_PERIODS)
    print(header)
    print(f"{'':22} {'':5} " + " ".join(f"{'(med t)':>8}" for _ in MA_PERIODS))
    print_separator()

    for regime in TRADING_REGIMES:
        if regime not in regime_results:
            continue
        r = regime_results[regime]
        n = r['bar_count']
        vals = []
        for period in MA_PERIODS:
            if period in r['periods']:
                vals.append(f"{r['periods'][period]['p50']:>7}t")
            else:
                vals.append(f"{'N/A':>8}")
        print(f"{regime:<22} {n:>5} " + " ".join(vals))


def print_reversion_quality_table(ma_quality: dict):
    print()
    print_separator('=')
    print("TABLE 3: MA REVERSION QUALITY — % of extended bars where NEXT bar is closer")
    print("         (higher % = price reverts more reliably = better gate level)")
    print_separator('=')

    header = f"{'MA Period':<12} {'N Bars':>8} {'Mean Dist':>10} {'Med Dist':>9} {'Rev@10t':>8} {'Rev@20t':>8} {'Rev@30t':>8} {'Rev@40t':>8}"
    print(header)
    print_separator()

    for period in MA_PERIODS:
        if period not in ma_quality:
            continue
        s = ma_quality[period]
        r10 = f"{s.get('pct_revert_from_10t', 'N/A')}%" if s.get('pct_revert_from_10t') is not None else 'N/A'
        r20 = f"{s.get('pct_revert_from_20t', 'N/A')}%" if s.get('pct_revert_from_20t') is not None else 'N/A'
        r30 = f"{s.get('pct_revert_from_30t', 'N/A')}%" if s.get('pct_revert_from_30t') is not None else 'N/A'
        r40 = f"{s.get('pct_revert_from_40t', 'N/A')}%" if s.get('pct_revert_from_40t') is not None else 'N/A'
        print(f"EMA{period:<9} {s['n_bars']:>8} {s['mean_abs_dist']:>9}t {s['median_abs_dist']:>8}t {r10:>8} {r20:>8} {r30:>8} {r40:>8}")


def print_extension_run_table(ext_results: dict):
    print()
    print_separator('=')
    print("TABLE 4: EXTENSION RUN LENGTHS — how far does price extend before returning to EMA")
    print("         (continuous runs where abs dist > 10 ticks)")
    print_separator('=')

    header = f"{'MA Period':<12} {'N Runs':>8} {'P25':>6} {'P50':>6} {'P75':>6} {'P90':>6} {'P95':>6} {'Max':>6} {'Mean':>6}"
    print(header)
    print_separator()

    for period in MA_PERIODS:
        if period not in ext_results:
            continue
        s = ext_results[period]
        if s.get('n_extension_runs', 0) == 0:
            continue
        print(f"EMA{period:<9} {s['n_extension_runs']:>8} {s['p25']:>5}t {s['p50']:>5}t {s['p75']:>5}t {s['p90']:>5}t {s['p95']:>5}t {s['max']:>5}t {s['mean']:>5}t")


def print_recommendations(regime_results: dict, ext_results: dict):
    print()
    print_separator('=')
    print("RECOMMENDATIONS")
    print_separator('=')

    print()
    print("1. OVEREXTENSION GATE — recommended threshold per regime (EMA9)")
    print("   Current bot default: 60 ticks")
    print("   Recommendation: use P90 of abs distance as 'overextended, avoid entry' gate")
    print()

    for regime in TRADING_REGIMES:
        if regime not in regime_results:
            print(f"   {regime:<22}: insufficient data")
            continue
        r = regime_results[regime]
        if 9 not in r['periods']:
            continue
        s = r['periods'][9]
        p75 = s['p75']
        p90 = s['p90']
        p95 = s['p95']
        # Round to nearest 5t for cleaner gate values
        gate = round(p90 / 5) * 5
        entry_zone = round(p25_for(regime, regime_results) / 5) * 5
        print(f"   {regime:<22}: P75={p75}t  P90={p90}t -> Gate={gate}t  Entry zone: <={max(entry_zone, 10)}t from EMA9")

    print()
    print("2. OVEREXTENSION GATE — from extension run analysis (EMA9, all regimes)")
    if 9 in ext_results and ext_results[9].get('n_extension_runs', 0) > 0:
        s = ext_results[9]
        print(f"   Median extension run: {s['p50']}t  P75: {s['p75']}t  P90: {s['p90']}t")
        print(f"   -> When price is already {s['p75']}t+ from EMA9, P75 of extension runs says it's peaked")
        print(f"   -> Suggested gate: {round(s['p75'] / 5) * 5}t (aggressive) to {round(s['p90'] / 5) * 5}t (conservative)")

    print()
    print("3. ENTRY ZONE — how close to EMA does price need to be for valid pullback entry?")
    print("   Target: bars where price is 'touching' EMA = within P25 of normal distance")
    print()
    for regime in TRADING_REGIMES:
        if regime not in regime_results:
            continue
        r = regime_results[regime]
        if 9 not in r['periods']:
            continue
        s = r['periods'][9]
        # Entry zone = within P25 (that's the typical 'close to EMA' state)
        entry_max = max(round(s['p25'] / 2.5) * 2.5, 5)  # round to .5 tick multiples
        print(f"   {regime:<22}: entry zone <={entry_max}t from EMA9  (P25 of normal dist = {s['p25']}t)")


def p25_for(regime, regime_results):
    if regime in regime_results and 9 in regime_results[regime]['periods']:
        return regime_results[regime]['periods'][9]['p25']
    return 10.0


# ----------------------------------------------
# Analysis 1: EMA5 on TICK bars vs TIMED bars
# ----------------------------------------------

def load_tick_bars_from_aggregator() -> list[dict]:
    """Load 300-tick bars from both aggregator state files. Returns list of bar dicts."""
    all_tick_bars = []
    for fname in ['aggregator_state_prod.json', 'aggregator_state_lab.json']:
        path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, 'r') as f:
            state = json.load(f)
        for b in state.get('bars_tick', []):
            all_tick_bars.append({
                'open': b['o'], 'high': b['h'], 'low': b['l'], 'close': b['c'],
                'volume': b['v'], 'tick_count': b['tc'],
                'start_ts': b['st'], 'end_ts': b['et'],
                'source': fname.replace('aggregator_state_', '').replace('.json', ''),
            })
    # De-duplicate by start_ts
    seen = set()
    unique = []
    for b in sorted(all_tick_bars, key=lambda x: x['start_ts']):
        if b['start_ts'] not in seen:
            seen.add(b['start_ts'])
            unique.append(b)
    return unique


def analyze_ema5_tick_vs_timed(df_1m: pd.DataFrame, tick_bars: list[dict]) -> dict:
    """
    Compare EMA5 behavior on 300-tick bars vs 1-minute bars.

    Key question: For a 20-50 point (80-200 tick) target move, which timeframe
    gives EMA5 more UTILITY — as a trend filter or a precision entry signal?

    Returns dict with comparative stats.
    """
    results = {}

    # -- Part A: EMA5 on 1m bars --
    valid_1m = df_1m[df_1m['ema5'].notna() & (df_1m['ema5'] != 0)].copy()
    if not valid_1m.empty:
        dist_1m = ((valid_1m['close'] - valid_1m['ema5']) / TICK_SIZE).abs()
        results['ema5_1m'] = {
            'n_bars': len(valid_1m),
            'median_dist_ticks': round(dist_1m.median(), 1),
            'mean_dist_ticks': round(dist_1m.mean(), 1),
            'p25_ticks': round(dist_1m.quantile(0.25), 1),
            'p75_ticks': round(dist_1m.quantile(0.75), 1),
            'p90_ticks': round(dist_1m.quantile(0.90), 1),
            'p95_ticks': round(dist_1m.quantile(0.95), 1),
        }

        # How many times does EMA5 get crossed during a 20-point (80-tick) upward move?
        # Simulate: pick sequences of 80+ tick upward moves, count EMA5 crosses
        closes_1m = valid_1m['close'].values
        emas_1m = valid_1m['ema5'].values
        ema5_crosses_in_80t_move = []

        for start_i in range(len(closes_1m) - 40):
            start_close = closes_1m[start_i]
            crosses = 0
            last_above = closes_1m[start_i] > emas_1m[start_i]
            for j in range(start_i + 1, min(start_i + 80, len(closes_1m))):
                now_above = closes_1m[j] > emas_1m[j]
                if not (closes_1m[j] >= start_close + 20.0):  # still below 20pt target
                    if now_above != last_above:
                        crosses += 1
                    last_above = now_above
                else:
                    break
            move = closes_1m[min(start_i + 40, len(closes_1m)-1)] - start_close
            if move > 15:  # only count when there was some upward movement
                ema5_crosses_in_80t_move.append(crosses)

        if ema5_crosses_in_80t_move:
            results['ema5_1m']['median_crosses_per_20pt_move'] = round(
                float(np.median(ema5_crosses_in_80t_move)), 1)
            results['ema5_1m']['mean_crosses_per_20pt_move'] = round(
                float(np.mean(ema5_crosses_in_80t_move)), 1)
            results['ema5_1m']['pct_moves_with_3plus_crosses'] = round(
                float(np.mean([x >= 3 for x in ema5_crosses_in_80t_move])) * 100, 1)
            results['ema5_1m']['pct_moves_with_5plus_crosses'] = round(
                float(np.mean([x >= 5 for x in ema5_crosses_in_80t_move])) * 100, 1)

    # -- Part B: EMA5 on 300-tick bars --
    if not tick_bars:
        results['ema5_tick'] = {'error': 'No tick bar data available'}
        return results

    tick_closes = [b['close'] for b in tick_bars]
    tick_ema5 = compute_ema(tick_closes, 5)

    # Compute distances
    tick_dist = []
    for i in range(len(tick_closes)):
        if tick_ema5[i] != tick_ema5[i]:  # nan check
            continue
        tick_dist.append(abs(tick_closes[i] - tick_ema5[i]) / TICK_SIZE)

    if tick_dist:
        import statistics as _stats
        results['ema5_tick'] = {
            'n_bars': len(tick_dist),
            'median_dist_ticks': round(_stats.median(tick_dist), 1),
            'mean_dist_ticks': round(sum(tick_dist)/len(tick_dist), 1),
            'p25_ticks': round(float(np.percentile(tick_dist, 25)), 1),
            'p75_ticks': round(float(np.percentile(tick_dist, 75)), 1),
            'p90_ticks': round(float(np.percentile(tick_dist, 90)), 1),
            'p95_ticks': round(float(np.percentile(tick_dist, 95)), 1),
        }

        # Tick bar duration stats — key for understanding what "EMA5 hug" means
        durations_s = [b['end_ts'] - b['start_ts'] for b in tick_bars]
        results['tick_bar_duration'] = {
            'mean_seconds': round(sum(durations_s)/len(durations_s), 1),
            'min_seconds': round(min(durations_s), 1),
            'max_seconds': round(max(durations_s), 1),
            'bars_per_minute_approx': round(60.0 / (sum(durations_s)/len(durations_s)), 1),
        }

        # Count EMA5 crosses in an 80-tick (20pt) upward move on tick bars
        tick_crosses_in_80t_move = []
        for start_i in range(len(tick_closes) - 100):
            if tick_ema5[start_i] != tick_ema5[start_i]:
                continue
            start_close = tick_closes[start_i]
            crosses = 0
            last_above = tick_closes[start_i] > tick_ema5[start_i]
            for j in range(start_i + 1, min(start_i + 200, len(tick_closes))):
                if tick_ema5[j] != tick_ema5[j]:
                    break
                now_above = tick_closes[j] > tick_ema5[j]
                move_so_far = tick_closes[j] - start_close
                if move_so_far < 20.0:  # below 20pt target
                    if now_above != last_above:
                        crosses += 1
                    last_above = now_above
                else:
                    break
            move = tick_closes[min(start_i + 80, len(tick_closes)-1)] - start_close
            if move > 10:
                tick_crosses_in_80t_move.append(crosses)

        if tick_crosses_in_80t_move:
            results['ema5_tick']['median_crosses_per_20pt_move'] = round(
                float(np.median(tick_crosses_in_80t_move)), 1)
            results['ema5_tick']['mean_crosses_per_20pt_move'] = round(
                float(np.mean(tick_crosses_in_80t_move)), 1)
            results['ema5_tick']['pct_moves_with_3plus_crosses'] = round(
                float(np.mean([x >= 3 for x in tick_crosses_in_80t_move])) * 100, 1)
            results['ema5_tick']['pct_moves_with_5plus_crosses'] = round(
                float(np.mean([x >= 5 for x in tick_crosses_in_80t_move])) * 100, 1)

    # -- Part C: EMA5 Tick as precision entry signal --
    # On tick bars, does price return to EMA5 during pullbacks?
    # Count "clean pullbacks to EMA5" on tick bars (within 6t = 1.5pts)
    if tick_dist:
        touches_within_6t = sum(1 for d in tick_dist if d <= 6)
        touches_within_12t = sum(1 for d in tick_dist if d <= 12)
        results['ema5_tick']['pct_bars_within_6t_ema5'] = round(
            touches_within_6t / len(tick_dist) * 100, 1)
        results['ema5_tick']['pct_bars_within_12t_ema5'] = round(
            touches_within_12t / len(tick_dist) * 100, 1)

    if 'ema5_1m' in results:
        dist_1m_arr = ((valid_1m['close'] - valid_1m['ema5']) / TICK_SIZE).abs()
        touches_within_6t_1m = (dist_1m_arr <= 6).sum()
        touches_within_12t_1m = (dist_1m_arr <= 12).sum()
        results['ema5_1m']['pct_bars_within_6t_ema5'] = round(
            touches_within_6t_1m / len(dist_1m_arr) * 100, 1)
        results['ema5_1m']['pct_bars_within_12t_ema5'] = round(
            touches_within_12t_1m / len(dist_1m_arr) * 100, 1)

    return results


def print_ema5_comparison(ema5_results: dict):
    print()
    print_separator('=')
    print("ANALYSIS 1: EMA5 — TICK BARS (300-tick) vs TIMED BARS (1m)")
    print("Question: Is EMA5 better as a 300-tick entry signal or 1m trend filter?")
    print_separator('=')

    td = ema5_results.get('tick_bar_duration', {})
    if td:
        print(f"\nTick Bar Context: avg={td['mean_seconds']}s/bar  "
              f"({td['bars_per_minute_approx']} bars/min)  "
              f"range={td['min_seconds']}-{td['max_seconds']}s")
        print(f"  -> A 20-pt (80-tick) move spans roughly "
              f"{max(1, round(80 / td['bars_per_minute_approx']))} tick bars at current pace")
        approx_bars_20pt = max(1, round(80 / td['bars_per_minute_approx']))
    else:
        approx_bars_20pt = '?'

    print()
    print(f"{'Metric':<42} {'1m Timed':>12} {'300-Tick':>12}")
    print_separator('-', 70)

    e1m = ema5_results.get('ema5_1m', {})
    etk = ema5_results.get('ema5_tick', {})

    rows = [
        ('N bars analyzed', 'n_bars'),
        ('Median abs dist from EMA5 (ticks)', 'median_dist_ticks'),
        ('Mean abs dist from EMA5 (ticks)', 'mean_dist_ticks'),
        ('P25 dist ticks', 'p25_ticks'),
        ('P75 dist ticks', 'p75_ticks'),
        ('P90 dist ticks', 'p90_ticks'),
        ('% bars within 6t (1.5pt) of EMA5', 'pct_bars_within_6t_ema5'),
        ('% bars within 12t (3pt) of EMA5', 'pct_bars_within_12t_ema5'),
        ('Median EMA5 crosses/20pt move', 'median_crosses_per_20pt_move'),
        ('Mean EMA5 crosses/20pt move', 'mean_crosses_per_20pt_move'),
        ('% moves with 3+ EMA5 crosses', 'pct_moves_with_3plus_crosses'),
        ('% moves with 5+ EMA5 crosses', 'pct_moves_with_5plus_crosses'),
    ]

    for label, key in rows:
        v1 = e1m.get(key, 'N/A')
        vt = etk.get(key, 'N/A')
        v1_str = f"{v1}" if v1 != 'N/A' else 'N/A'
        vt_str = f"{vt}" if vt != 'N/A' else 'N/A'
        print(f"  {label:<40} {v1_str:>12} {vt_str:>12}")

    print()
    print("INTERPRETATION:")
    med1m = e1m.get('median_dist_ticks', 0)
    medtk = etk.get('median_dist_ticks', 0)
    cr1m = e1m.get('median_crosses_per_20pt_move', 0)
    crtk = etk.get('median_crosses_per_20pt_move', 0)

    if med1m and medtk:
        ratio = round(med1m / medtk, 1) if medtk > 0 else '?'
        print(f"  -> EMA5 on 1m bars is {ratio}x farther from price than on 300-tick bars")

    if cr1m and crtk:
        print(f"  -> On 1m bars: EMA5 crossed ~{cr1m}x during a 20pt move (false exit signals)")
        print(f"  -> On tick bars: EMA5 crossed ~{crtk}x during equivalent move")

    print()
    if cr1m and float(str(cr1m)) >= 3:
        print("  VERDICT: EMA5 on 1m timed bars is TOO NOISY for 20-50pt targets.")
        print("           Price crosses EMA5 multiple times during any meaningful move.")
    print("  -> EMA5 on 300-tick bars: better for PRECISION ENTRY timing within a pullback")
    print("    (tight ~6-12t tracking = valid 'touch and bounce' signal at granular level)")
    print("  -> EMA9 on 1m timed bars: better TREND FILTER for 20-50pt move direction")
    print("  -> Recommended usage:")
    print("    1. EMA9 on 1m: confirm trend direction (price > EMA9 = bullish bias)")
    print("    2. EMA5 on 300-tick: time the pullback entry (wait for close within 6t of EMA5)")
    print("    3. Do NOT use EMA5 on 1m as an exit signal for 20-50pt targets")


# ----------------------------------------------
# Analysis 2: Choppy days — exit gate analysis
# ----------------------------------------------

def classify_days_by_type(df: pd.DataFrame) -> dict:
    """
    Classify each trading day as CHOPPY (<80pt daily range), MODERATE (80-200pt), TREND (200pt+).
    Uses only RTH session bars for range calculation.
    Returns dict: date -> {'type', 'day_range', 'open', 'close', 'direction', 'bars'}
    """
    RTH_REGIMES = {'OPEN_MOMENTUM', 'MID_MORNING', 'AFTERNOON_CHOP', 'LATE_AFTERNOON'}
    day_info = {}

    for date, grp in df.groupby('date'):
        rth = grp[grp['regime'].isin(RTH_REGIMES)]
        if len(rth) < 5:
            # Fall back to all bars if not enough RTH
            rth = grp
        if rth.empty:
            continue

        highs = rth['high'].values if 'high' in rth.columns else rth['close'].values
        lows = rth['low'].values if 'low' in rth.columns else rth['close'].values
        closes = rth['close'].values

        day_range = float(max(highs) - min(lows))
        day_open = float(closes[0])
        day_close = float(closes[-1])
        direction = 'UP' if day_close > day_open else 'DOWN'

        if day_range < 80:
            day_type = 'CHOPPY'
        elif day_range < 200:
            day_type = 'MODERATE'
        else:
            day_type = 'TREND'

        day_info[date] = {
            'type': day_type,
            'day_range': round(day_range, 2),
            'open': round(day_open, 2),
            'close': round(day_close, 2),
            'direction': direction,
            'net_move': round(abs(day_close - day_open), 2),
            'n_bars': len(rth),
        }

    return day_info


def analyze_choppy_vs_trend_exits(df: pd.DataFrame, day_info: dict) -> dict:
    """
    For each day type (CHOPPY / MODERATE / TREND), analyze:
    1. How far does price get from EMA9 before reversing?
    2. Would a 40-tick (10pt) minimum profit gate catch profitable exits?
    3. What is the average reversion distance?

    Focuses on RTH session bars in trading regimes.
    """
    RTH_REGIMES = {'OPEN_MOMENTUM', 'MID_MORNING', 'AFTERNOON_CHOP', 'LATE_AFTERNOON'}
    results = {}

    for day_type in ['CHOPPY', 'MODERATE', 'TREND']:
        days_of_type = [d for d, info in day_info.items() if info['type'] == day_type]
        if not days_of_type:
            results[day_type] = {'n_days': 0}
            continue

        # Gather all RTH bars for these days
        type_df = df[df['date'].isin(days_of_type) & df['regime'].isin(RTH_REGIMES)].copy()
        if type_df.empty:
            # Fall back to all bars
            type_df = df[df['date'].isin(days_of_type)].copy()
        if type_df.empty:
            results[day_type] = {'n_days': len(days_of_type), 'n_bars': 0}
            continue

        # EMA9 distance analysis
        ema9_dist = ((type_df['close'] - type_df['ema9_best']) / TICK_SIZE).abs().dropna()
        ema9_dist = ema9_dist[ema9_dist > 0]

        # EMA5 distance
        ema5_dist_vals = ((type_df['close'] - type_df['ema5']) / TICK_SIZE).abs().dropna()
        ema5_dist_vals = ema5_dist_vals[ema5_dist_vals > 0]

        # "Reversion" analysis: measure extension runs per day
        # For each day of this type, measure max extension before reversion
        all_ext_runs = []
        for date in days_of_type:
            day_df = df[df['date'] == date].copy()
            if day_df.empty:
                continue
            valid_day = day_df[day_df['ema9_best'].notna() & (day_df['ema9_best'] != 0)]
            ext = compute_reversion_distances(valid_day, 'ema9_best')
            all_ext_runs.extend(ext.tolist())

        # Simulate 40-tick minimum profit gate:
        # For each bar where price is >40t from EMA9, would we have locked profit?
        # i.e., count bars where abs_dist_ema9 >= 40 AND the NEXT bar is CLOSER (reversion)
        extended_40t = type_df[
            ((type_df['close'] - type_df['ema9_best']) / TICK_SIZE).abs() >= 40
        ].copy()
        if not extended_40t.empty and 'abs_dist_ema9' not in extended_40t.columns:
            extended_40t['abs_dist_ema9'] = ((extended_40t['close'] - extended_40t['ema9_best']) / TICK_SIZE).abs()

        # % of times price reaches 40t from EMA9 before reversing
        reaches_40t = (ema9_dist >= 40).sum() if len(ema9_dist) > 0 else 0
        reaches_20t = (ema9_dist >= 20).sum() if len(ema9_dist) > 0 else 0
        reaches_60t = (ema9_dist >= 60).sum() if len(ema9_dist) > 0 else 0
        reaches_80t = (ema9_dist >= 80).sum() if len(ema9_dist) > 0 else 0

        # Stall detector: on choppy days, what % of "40t extensions" reverse within 2 bars?
        # Proxy: if abs_dist >= 40, what % of NEXT bars are back below 40t?
        if len(ema9_dist) > 0:
            type_df = type_df.copy()
            type_df['abs_ema9'] = ((type_df['close'] - type_df['ema9_best']) / TICK_SIZE).abs()
            type_df['next_abs_ema9'] = type_df['abs_ema9'].shift(-1)
            at_40t_plus = type_df[type_df['abs_ema9'] >= 40]
            pct_reverse_from_40t = (
                (at_40t_plus['next_abs_ema9'] < 40).mean() * 100
                if len(at_40t_plus) > 0 else 0
            )
            at_20t_plus = type_df[type_df['abs_ema9'] >= 20]
            pct_reverse_from_20t = (
                (at_20t_plus['next_abs_ema9'] < 20).mean() * 100
                if len(at_20t_plus) > 0 else 0
            )
        else:
            pct_reverse_from_40t = 0
            pct_reverse_from_20t = 0

        day_ranges = [day_info[d]['day_range'] for d in days_of_type if d in day_info]

        stats = {
            'n_days': len(days_of_type),
            'n_bars': len(type_df),
            'days': sorted(str(d) for d in days_of_type),
            'day_range_mean': round(float(np.mean(day_ranges)), 1) if day_ranges else 0,
            'day_range_min': round(min(day_ranges), 1) if day_ranges else 0,
            'day_range_max': round(max(day_ranges), 1) if day_ranges else 0,
        }

        if len(ema9_dist) > 0:
            p = np.percentile(ema9_dist, [25, 50, 75, 90, 95])
            stats['ema9_dist'] = {
                'p25': round(p[0], 1), 'p50': round(p[1], 1),
                'p75': round(p[2], 1), 'p90': round(p[3], 1), 'p95': round(p[4], 1),
                'mean': round(float(ema9_dist.mean()), 1),
                'max': round(float(ema9_dist.max()), 1),
                'pct_reaching_20t': round(reaches_20t / len(ema9_dist) * 100, 1),
                'pct_reaching_40t': round(reaches_40t / len(ema9_dist) * 100, 1),
                'pct_reaching_60t': round(reaches_60t / len(ema9_dist) * 100, 1),
                'pct_reaching_80t': round(reaches_80t / len(ema9_dist) * 100, 1),
                'pct_reverse_from_40t': round(pct_reverse_from_40t, 1),
                'pct_reverse_from_20t': round(pct_reverse_from_20t, 1),
            }

        if len(ema5_dist_vals) > 0:
            p5 = np.percentile(ema5_dist_vals, [25, 50, 75, 90])
            stats['ema5_dist'] = {
                'p25': round(p5[0], 1), 'p50': round(p5[1], 1),
                'p75': round(p5[2], 1), 'p90': round(p5[3], 1),
                'mean': round(float(ema5_dist_vals.mean()), 1),
            }

        if all_ext_runs:
            p_ext = np.percentile(all_ext_runs, [25, 50, 75, 90])
            stats['extension_runs'] = {
                'n': len(all_ext_runs),
                'p25': round(p_ext[0], 1), 'p50': round(p_ext[1], 1),
                'p75': round(p_ext[2], 1), 'p90': round(p_ext[3], 1),
                'mean': round(float(np.mean(all_ext_runs)), 1),
            }

        results[day_type] = stats

    return results


def print_choppy_day_analysis(day_info: dict, chop_results: dict):
    print()
    print_separator('=')
    print("ANALYSIS 2: CHOPPY vs TREND DAYS — EXIT GATE EFFECTIVENESS")
    print("Question: Does the 40-tick (10pt) min profit gate + stall detector work on chop days?")
    print_separator('=')

    print()
    print("DAY CLASSIFICATION (RTH session bars):")
    for dtype in ['CHOPPY', 'MODERATE', 'TREND']:
        days = [d for d, i in day_info.items() if i['type'] == dtype]
        if days:
            ranges = [day_info[d]['day_range'] for d in days]
            print(f"  {dtype:<10}: {len(days)} day(s)  range {min(ranges):.1f}-{max(ranges):.1f}pts  "
                  f"days: {', '.join(sorted(str(d) for d in days))}")

    # No March data note
    all_dates = sorted(day_info.keys())
    if all_dates:
        earliest = min(all_dates)
        latest = max(all_dates)
        if str(earliest) >= '2026-04-01':
            print()
            print("  NOTE: No March data in history. Data covers April 2026 only.")
            print("  Analysis uses current data — AFTERNOON_CHOP regime bars proxy choppy conditions.")

    print()
    print(f"{'Metric':<48} {'CHOPPY':>10} {'MODERATE':>10} {'TREND':>10}")
    print_separator('-', 82)

    def val(dtype, path, default='N/A'):
        r = chop_results.get(dtype, {})
        for key in path:
            if isinstance(r, dict):
                r = r.get(key, default)
            else:
                return default
        return r if r != {} else default

    rows = [
        ('N days', ['n_days']),
        ('N RTH bars', ['n_bars']),
        ('Day range mean (pts)', ['day_range_mean']),
        ('EMA9 dist P50 (ticks)', ['ema9_dist', 'p50']),
        ('EMA9 dist P75 (ticks)', ['ema9_dist', 'p75']),
        ('EMA9 dist P90 (ticks)', ['ema9_dist', 'p90']),
        ('EMA9 dist mean (ticks)', ['ema9_dist', 'mean']),
        ('EMA9 dist max (ticks)', ['ema9_dist', 'max']),
        ('% bars reaching 20t from EMA9', ['ema9_dist', 'pct_reaching_20t']),
        ('% bars reaching 40t from EMA9', ['ema9_dist', 'pct_reaching_40t']),
        ('% bars reaching 60t from EMA9', ['ema9_dist', 'pct_reaching_60t']),
        ('% bars reaching 80t from EMA9', ['ema9_dist', 'pct_reaching_80t']),
        ('% 40t+ bars reversing next bar', ['ema9_dist', 'pct_reverse_from_40t']),
        ('% 20t+ bars reversing next bar', ['ema9_dist', 'pct_reverse_from_20t']),
        ('EMA5 dist P50 (ticks)', ['ema5_dist', 'p50']),
        ('EMA5 dist P75 (ticks)', ['ema5_dist', 'p75']),
        ('Ext run P50 before reversion', ['extension_runs', 'p50']),
        ('Ext run P75 before reversion', ['extension_runs', 'p75']),
        ('Ext run P90 before reversion', ['extension_runs', 'p90']),
        ('Ext run mean (ticks)', ['extension_runs', 'mean']),
    ]

    for label, path in rows:
        vals = [val(dt, path) for dt in ['CHOPPY', 'MODERATE', 'TREND']]
        vs = [f"{v}" if v != 'N/A' else 'N/A' for v in vals]
        print(f"  {label:<46} {vs[0]:>10} {vs[1]:>10} {vs[2]:>10}")

    print()
    print("EXIT GATE ANALYSIS — 40-tick (10pt) minimum profit gate:")
    print()
    for dtype in ['CHOPPY', 'MODERATE', 'TREND']:
        r = chop_results.get(dtype, {})
        ed = r.get('ema9_dist', {})
        er = r.get('extension_runs', {})
        if not ed:
            print(f"  {dtype}: insufficient data")
            continue

        pct_40 = ed.get('pct_reaching_40t', 0)
        pct_rev = ed.get('pct_reverse_from_40t', 0)
        ext_p50 = er.get('p50', 'N/A')
        ext_p75 = er.get('p75', 'N/A')

        print(f"  {dtype} days:")
        print(f"    % bars reaching 40t from EMA9: {pct_40}%")
        if pct_40 and float(str(pct_40)) < 15:
            print(f"    ** PROBLEM: Only {pct_40}% of bars extend 40t from EMA9.")
            print(f"       The 40t gate may rarely trigger — price reverts too quickly.")
            print(f"       Consider lowering to 20t (5pt) gate on {dtype} days.")
        elif pct_40:
            print(f"    OK {pct_40}% of bars extend past 40t — gate has enough opportunities.")
        print(f"    % of those 40t+ bars that reverse next bar: {pct_rev}%")
        if ext_p50 != 'N/A':
            print(f"    Extension run P50={ext_p50}t  P75={ext_p75}t")
            if float(str(ext_p50)) < 40:
                print(f"    ** Median extension = {ext_p50}t < 40t gate -> most moves reverse BEFORE 40t on {dtype}")
        print()

    print("RECOMMENDATIONS FOR NON-TREND DAYS:")
    print()
    chop = chop_results.get('CHOPPY', {})
    mod = chop_results.get('MODERATE', {})
    chop_ed = chop.get('ema9_dist', {})
    mod_ed = mod.get('ema9_dist', {})

    chop_p50 = chop_ed.get('p50', 0)
    chop_ext_p50 = chop.get('extension_runs', {}).get('p50', 0)
    chop_pct_40 = chop_ed.get('pct_reaching_40t', 0)

    if chop_p50:
        print(f"  On CHOPPY days (range <80pts):")
        print(f"    Median EMA9 distance = {chop_p50}t. Price doesn't extend far.")
        if chop_ext_p50 and float(str(chop_ext_p50)) < 40:
            gate = max(20, round(float(str(chop_ext_p50)) / 4) * 4)  # round to 4t
            print(f"    Median extension run = {chop_ext_p50}t -> recommend lowering min gate to {gate}t (not 40t)")
            print(f"    Stall detector: even more critical on choppy days (tight stops, quick exits)")
        elif chop_pct_40:
            print(f"    {chop_pct_40}% of bars reach 40t -> gate is marginally viable but aggressive")
        print()

    if mod_ed:
        mod_p50 = mod_ed.get('p50', 0)
        mod_ext_p50 = mod.get('extension_runs', {}).get('p50', 0)
        if mod_p50:
            print(f"  On MODERATE days (range 80-200pts):")
            print(f"    Median EMA9 distance = {mod_p50}t. 40t gate workable but tight.")
            if mod_ext_p50:
                print(f"    Extension run P50={mod_ext_p50}t -> {'gate adequate' if float(str(mod_ext_p50)) >= 40 else 'consider 20-32t gate'}")
            print()

    print("  On TREND days (range 200pts+):")
    trend = chop_results.get('TREND', {})
    trend_ed = trend.get('ema9_dist', {})
    trend_ext = trend.get('extension_runs', {})
    if trend_ed:
        print(f"    Median EMA9 distance = {trend_ed.get('p50','N/A')}t")
        print(f"    Extension run P50={trend_ext.get('p50','N/A')}t  P75={trend_ext.get('p75','N/A')}t")
        print(f"    40t gate appropriate; trail stop or scale out at 80-120t")

    print()
    print("  OPTIMAL EXIT APPROACH BY DAY TYPE:")
    print("  +---------------------------------------------------------------------+")
    print("  | CHOPPY   -> 20t (5pt) min gate + aggressive stall detector          |")
    print("  |            EMA5 mean reversion likely within 20-40t — take it      |")
    print("  | MODERATE -> 32t (8pt) min gate + stall detector at 40t              |")
    print("  |            Trail stop at EMA9 once 40t in profit                   |")
    print("  | TREND    -> 40t (10pt) min gate, trail to EMA9, target 80-160t      |")
    print("  |            Scale out 50% at 80t, let rest run to 120-200t          |")
    print("  +---------------------------------------------------------------------+")
    print()
    print("  NOTE: Detect day type early using:")
    print("    - First 30min range (< 20pts = likely chop, > 40pts = trend potential)")
    print("    - OPEN_MOMENTUM regime directional strength (CVD, tick count)")
    print("    - VIX level (high VIX = trend day more likely)")


# ----------------------------------------------
# Main
# ----------------------------------------------

def main():
    print()
    print_separator('=')
    print("PHOENIX BOT — EMA DISTANCE ANALYSIS")
    print("MNQ Micro E-mini Nasdaq-100 | TICK_SIZE=0.25 | 1 tick = $0.50")
    print_separator('=')

    # Load data
    print("\n[1] Loading bar events from history JSONL files...")
    bar_events = load_all_bar_events()

    print("[2] Loading exit events for MFE/MAE analysis...")
    exit_events = load_all_exit_events()
    print(f"    Found {len(exit_events)} exit events with MFE/MAE data")

    # Build DataFrame
    print("[3] Building bar DataFrame...")
    df = build_bar_dataframe(bar_events)
    if df.empty:
        print("ERROR: No bar data found. Check history directory.")
        return

    # Add computed EMAs
    print("[4] Computing EMA5/8/9/13/21 per trading day...")
    df = add_computed_emas(df)

    # Add distance columns
    print("[5] Computing distance metrics...")
    df = compute_distances(df)

    # Filter to bars with valid EMA9 (after warmup)
    valid_df = df[df['ema9_best'].notna() & (df['ema9_best'] != 0)].copy()
    print(f"    Valid bars (EMA9 computed): {len(valid_df)} of {len(df)}")

    # Analysis
    print("[6] Running regime analysis...")
    regime_results = analyze_by_regime(valid_df)

    print("[7] Running MA reversion quality analysis...")
    ma_quality = analyze_ma_reversion_quality(valid_df)

    print("[8] Running extension run analysis...")
    ext_results = analyze_reversion_events(valid_df)

    print("[9] Analyzing trade exits...")
    trade_stats = analyze_trade_exits(exit_events)

    # Print results
    print_regime_table(regime_results)
    print_ma_comparison_table(regime_results)
    print_reversion_quality_table(ma_quality)
    print_extension_run_table(ext_results)

    # Trade MFE/MAE section
    if trade_stats.get('total_trades', 0) > 0:
        print()
        print_separator('=')
        print(f"TABLE 5: TRADE MFE/MAE FROM EXIT EVENTS (n={trade_stats['total_trades']} trades)")
        print_separator('=')
        for strat, s in trade_stats.get('strategies', {}).items():
            print(f"  {strat}: n={s['n']} win%={s['win_rate']} MFE_mean={s['mfe_mean']}t MFE_p75={s['mfe_p75']}t MAE_mean={s['mae_mean']}t")

    print_recommendations(regime_results, ext_results)

    # -- NEW Analysis 1: EMA5 tick vs timed --
    print("\n[10] Loading 300-tick bars from aggregator state...")
    tick_bars = load_tick_bars_from_aggregator()
    print(f"     Found {len(tick_bars)} unique 300-tick bars")

    print("[11] Running EMA5 tick-bar vs 1m-bar comparison...")
    ema5_results = analyze_ema5_tick_vs_timed(valid_df, tick_bars)
    print_ema5_comparison(ema5_results)

    # -- NEW Analysis 2: Choppy day exit gate analysis --
    print("\n[12] Classifying days by type (choppy / moderate / trend)...")
    day_info = classify_days_by_type(valid_df)
    for dtype, info in sorted(day_info.items()):
        print(f"     {dtype}: {info['type']:<10} range={info['day_range']:.1f}pts  n_bars={info['n_bars']}")

    print("[13] Running choppy day exit gate analysis...")
    chop_results = analyze_choppy_vs_trend_exits(valid_df, day_info)
    print_choppy_day_analysis(day_info, chop_results)

    print()
    print_separator('=')
    print("DATA COVERAGE SUMMARY")
    print_separator('=')
    regime_counts = valid_df.groupby('regime').size()
    for regime, count in regime_counts.sort_values(ascending=False).items():
        mark = " <-- trading regime" if regime in TRADING_REGIMES else ""
        print(f"  {regime:<25}: {count:>5} bars{mark}")
    print(f"\n  Total bars analyzed: {len(valid_df)}")
    print(f"  Date range: {df['date'].min()} to {df['date'].max()}")
    print(f"  NOTE: Data covers {(df['date'].max() - df['date'].min()).days + 1} calendar days.")
    print(f"  More data would increase confidence in the percentile thresholds.")
    print_separator('=')


if __name__ == '__main__':
    main()
