"""
backtest_v3_sweep.py
Multi-config parameter sweep for ES/NQ confluence strategy.

Tests combinations of:
  - direction:       LONG / SHORT
  - signal_sign:     POS / NEG (boost positive means NQ outperforming ES)
  - boost_formula:   raw_spread (bp×100) / z_score (rolling)
  - boost_threshold: range of thresholds per formula
  - corr_threshold:  0.85 / 0.90 / 0.95

For each config, simulates trades using 5-min signal + 1-min fill precision,
then ranks by regime robustness (must be positive in 2022 bear + most other years).

Output: top 15 configs by composite score, plus full CSV of results.
"""
import os
import sys
import time
import pandas as pd
import numpy as np
from itertools import product

DATA_DIR = r"C:\Trading Project\phoenix_bot\data\historical"
OUTPUT_DIR = r"C:\Trading Project\phoenix_bot\backtest_results"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===== FIXED PARAMETERS =====
STOP_TICKS = 24
TARGET_TICKS = 48
TICK_SIZE = 0.25
TICK_VALUE = 0.50
Z_WINDOW = 50
MAX_HOLD_MIN = 240

# ===== SWEEP GRID =====
DIRECTIONS = ['LONG', 'SHORT']
SIGNAL_SIGNS = ['POS', 'NEG']        # POS = enter when boost > +threshold; NEG = when boost < -threshold
BOOST_FORMULAS = ['raw', 'zscore']
RAW_THRESHOLDS = [10, 15, 20, 25, 30]    # bp × 100 (for raw_spread formula)
ZSCORE_THRESHOLDS = [1.5, 2.0, 2.5, 3.0]  # standard deviations (for z-score formula)
CORR_THRESHOLDS = [0.85, 0.90, 0.95]


def load_and_prep():
    print("[Loading data]")
    t0 = time.time()
    mnq_5m = pd.read_csv(os.path.join(DATA_DIR, "mnq_5min_databento.csv"))
    mes_5m = pd.read_csv(os.path.join(DATA_DIR, "mes_5min_databento.csv"))
    mnq_1m = pd.read_csv(os.path.join(DATA_DIR, "mnq_1min_databento.csv"))
    for df in (mnq_5m, mes_5m, mnq_1m):
        df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)
    print(f"  MNQ 5m: {len(mnq_5m):,}  MES 5m: {len(mes_5m):,}  MNQ 1m: {len(mnq_1m):,}")
    print(f"  Loaded in {time.time()-t0:.1f}s")

    # Merge MNQ + MES 5m
    mnq = mnq_5m[['ts_utc', 'close']].rename(columns={'close': 'mnq_close'})
    mes = mes_5m[['ts_utc', 'close']].rename(columns={'close': 'mes_close'})
    df = pd.merge(mnq, mes, on='ts_utc', how='inner').sort_values('ts_utc').reset_index(drop=True)
    df['mnq_ret'] = df['mnq_close'].pct_change()
    df['mes_ret'] = df['mes_close'].pct_change()
    df['spread'] = df['mnq_ret'] - df['mes_ret']
    df['corr'] = df['mnq_ret'].rolling(Z_WINDOW).corr(df['mes_ret'])

    # Boost formulas
    df['boost_raw'] = df['spread'] * 10000
    rolling_mean = df['spread'].rolling(Z_WINDOW).mean()
    rolling_std = df['spread'].rolling(Z_WINDOW).std()
    df['boost_zscore'] = (df['spread'] - rolling_mean) / rolling_std

    df = df.dropna(subset=['boost_raw', 'boost_zscore', 'corr']).reset_index(drop=True)
    return df, mnq_1m


def build_1min_indexed(mnq_1m):
    """Index 1-min data by tz-aware timestamp for pandas slicing."""
    return mnq_1m.set_index('ts_utc').sort_index()


def simulate_trade(entry_ts, entry_price, direction, m1_idx):
    """Pandas slicing approach — robust to tz quirks."""
    if direction == 'LONG':
        stop = entry_price - STOP_TICKS * TICK_SIZE
        target = entry_price + TARGET_TICKS * TICK_SIZE
    else:
        stop = entry_price + STOP_TICKS * TICK_SIZE
        target = entry_price - TARGET_TICKS * TICK_SIZE

    max_ts = entry_ts + pd.Timedelta(minutes=MAX_HOLD_MIN)
    walk = m1_idx.loc[entry_ts:max_ts]
    if len(walk) <= 1:
        return None
    walk = walk.iloc[1:]  # skip the entry bar itself

    highs = walk['high'].to_numpy()
    lows = walk['low'].to_numpy()
    closes = walk['close'].to_numpy()

    if direction == 'LONG':
        stop_hits = lows <= stop
        target_hits = highs >= target
    else:
        stop_hits = highs >= stop
        target_hits = lows <= target

    has_stop = stop_hits.any()
    has_target = target_hits.any()
    stop_idx = stop_hits.argmax() if has_stop else len(stop_hits)
    target_idx = target_hits.argmax() if has_target else len(target_hits)

    if not has_stop and not has_target:
        # Timeout — exit at last close
        exit_price = closes[-1]
        pnl_ticks = ((exit_price - entry_price) if direction == 'LONG'
                     else (entry_price - exit_price)) / TICK_SIZE
        return {'pnl_ticks': pnl_ticks, 'exit_reason': 'timeout',
                'hold_min': MAX_HOLD_MIN}

    # Whichever hit first (stop wins on tie — conservative)
    if stop_idx <= target_idx:
        pnl_ticks = ((stop - entry_price) if direction == 'LONG'
                     else (entry_price - stop)) / TICK_SIZE
        return {'pnl_ticks': pnl_ticks, 'exit_reason': 'stop',
                'hold_min': float(stop_idx + 1)}
    else:
        pnl_ticks = ((target - entry_price) if direction == 'LONG'
                     else (entry_price - target)) / TICK_SIZE
        return {'pnl_ticks': pnl_ticks, 'exit_reason': 'target',
                'hold_min': float(target_idx + 1)}


def run_config(signals, m1, direction, signal_sign, boost_col, threshold, corr_thresh):
    """Run one config; return summary dict."""
    if signal_sign == 'POS':
        mask = (signals[boost_col] >= threshold) & (signals['corr'] > corr_thresh)
    else:
        mask = (signals[boost_col] <= -threshold) & (signals['corr'] > corr_thresh)

    sig_indices = signals.index[mask].tolist()
    if not sig_indices:
        return None

    trades = []
    for idx in sig_indices:
        next_idx = idx + 1
        if next_idx >= len(signals):
            continue
        entry_ts = signals.iloc[next_idx]['ts_utc']
        entry_price = signals.iloc[next_idx]['mnq_close']
        fill = simulate_trade(entry_ts, entry_price, direction, m1)
        if fill is None:
            continue
        trades.append({'entry_ts': entry_ts, 'year': pd.Timestamp(entry_ts).year, **fill})

    if not trades:
        return None
    tdf = pd.DataFrame(trades)
    tdf['pnl_dollars'] = tdf['pnl_ticks'] * TICK_VALUE

    # Aggregate stats
    total = len(tdf)
    wins = (tdf['pnl_ticks'] > 0).sum()
    wr = wins / total * 100
    total_pnl = tdf['pnl_dollars'].sum()
    avg = tdf['pnl_dollars'].mean()
    gw = tdf[tdf['pnl_dollars'] > 0]['pnl_dollars'].sum()
    gl = abs(tdf[tdf['pnl_dollars'] < 0]['pnl_dollars'].sum())
    pf = gw / gl if gl > 0 else float('inf')

    # By year
    by_year = tdf.groupby('year')['pnl_dollars'].sum().to_dict()
    years_pos = sum(1 for v in by_year.values() if v > 0)
    pnl_2022 = by_year.get(2022, 0)

    return {
        'direction': direction,
        'signal_sign': signal_sign,
        'boost_formula': boost_col.replace('boost_', ''),
        'boost_threshold': threshold,
        'corr_threshold': corr_thresh,
        'total_trades': total,
        'win_rate': wr,
        'total_pnl': total_pnl,
        'avg_per_trade': avg,
        'profit_factor': pf,
        'avg_hold_min': tdf['hold_min'].mean(),
        'years_positive': years_pos,
        'pnl_2021': by_year.get(2021, 0),
        'pnl_2022': by_year.get(2022, 0),
        'pnl_2023': by_year.get(2023, 0),
        'pnl_2024': by_year.get(2024, 0),
        'pnl_2025': by_year.get(2025, 0),
        'pnl_2026': by_year.get(2026, 0),
        'composite_score': calc_score(years_pos, pf, pnl_2022, total_pnl, total),
    }


def calc_score(years_pos, pf, pnl_2022, total_pnl, total_trades):
    """Composite score favoring regime robustness."""
    if pf == float('inf'):
        pf = 10
    bear_bonus = 2 if pnl_2022 > 0 else 0
    trade_count_factor = min(np.log10(max(total_trades, 1)), 3.5) / 3.5  # cap to avoid favoring extremes
    return (years_pos + bear_bonus) * min(pf, 5) * (1 + trade_count_factor) * (1 if total_pnl > 0 else 0.1)


def main():
    print("=" * 80)
    print("BACKTEST V3 — PARAMETER SWEEP (regime-robust config search)")
    print("=" * 80)

    signals, mnq_1m = load_and_prep()
    m1 = build_1min_indexed(mnq_1m)
    print(f"\n[Signals] {len(signals):,} valid 5-min bars")
    print(f"  raw boost percentiles: 95%={signals['boost_raw'].quantile(.95):.2f}, "
          f"99%={signals['boost_raw'].quantile(.99):.2f}, "
          f"99.9%={signals['boost_raw'].quantile(.999):.2f}")
    print(f"  zscore percentiles:    95%={signals['boost_zscore'].quantile(.95):.2f}, "
          f"99%={signals['boost_zscore'].quantile(.99):.2f}, "
          f"99.9%={signals['boost_zscore'].quantile(.999):.2f}")

    # Build config grid
    configs = []
    for direction, sign, corr in product(DIRECTIONS, SIGNAL_SIGNS, CORR_THRESHOLDS):
        for thr in RAW_THRESHOLDS:
            configs.append((direction, sign, 'boost_raw', thr, corr))
        for thr in ZSCORE_THRESHOLDS:
            configs.append((direction, sign, 'boost_zscore', thr, corr))

    print(f"\n[Sweep] Testing {len(configs)} configs...")
    print(f"  (direction × signal_sign × formula × threshold × corr)")
    t0 = time.time()
    results = []
    for i, (d, s, bcol, thr, c) in enumerate(configs):
        if i % 10 == 0:
            elapsed = time.time() - t0
            eta = elapsed / max(i, 1) * (len(configs) - i)
            print(f"  [{i:>3}/{len(configs)}]  elapsed={elapsed:.0f}s  eta={eta:.0f}s")
        r = run_config(signals, m1, d, s, bcol, thr, c)
        if r is not None:
            results.append(r)

    print(f"\n[Sweep complete] {len(results)} configs produced trades  ({time.time()-t0:.0f}s total)")

    if not results:
        print("❌ No configs produced trades")
        return

    results_df = pd.DataFrame(results).sort_values('composite_score', ascending=False).reset_index(drop=True)

    # Show top 15
    print(f"\n{'='*100}")
    print(f"🏆 TOP 15 CONFIGS BY COMPOSITE SCORE")
    print(f"{'='*100}")
    cols_show = ['direction', 'signal_sign', 'boost_formula', 'boost_threshold', 'corr_threshold',
                 'total_trades', 'win_rate', 'total_pnl', 'avg_per_trade', 'profit_factor',
                 'years_positive', 'pnl_2022', 'composite_score']
    top = results_df.head(15)[cols_show].copy()
    top['win_rate'] = top['win_rate'].round(1)
    top['total_pnl'] = top['total_pnl'].round(0).astype(int)
    top['avg_per_trade'] = top['avg_per_trade'].round(2)
    top['profit_factor'] = top['profit_factor'].round(2)
    top['pnl_2022'] = top['pnl_2022'].round(0).astype(int)
    top['composite_score'] = top['composite_score'].round(2)

    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 200)
    print(top.to_string(index=False))

    # Detailed view of #1
    print(f"\n{'='*100}")
    print(f"🥇 #1 CONFIG — DETAILED REGIME BREAKDOWN")
    print(f"{'='*100}")
    best = results_df.iloc[0]
    print(f"  Direction:        {best['direction']} (entered when boost {best['signal_sign']})")
    print(f"  Boost formula:    {best['boost_formula']}")
    print(f"  Boost threshold:  {best['boost_threshold']}")
    print(f"  Corr threshold:   {best['corr_threshold']}")
    print(f"  Stop / Target:    {STOP_TICKS}t / {TARGET_TICKS}t  ({TARGET_TICKS/STOP_TICKS:.1f}:1 R:R)")
    print(f"\n  Total trades:     {int(best['total_trades']):,}")
    print(f"  Win rate:         {best['win_rate']:.1f}%")
    print(f"  Total P&L:        ${best['total_pnl']:+,.2f}")
    print(f"  Avg per trade:    ${best['avg_per_trade']:+,.2f}")
    print(f"  Profit factor:    {best['profit_factor']:.2f}")
    print(f"  Avg hold:         {best['avg_hold_min']:.1f} min")
    print(f"\n  By regime year:")
    regime_labels = {
        2021: "🟢 Late bull peak",
        2022: "🐻 BEAR (-33%)",
        2023: "🟡 Recovery + SVB",
        2024: "🟢 AI rally",
        2025: "🟠 Tariff vol",
        2026: "🟢 Current YTD",
    }
    for yr in sorted(regime_labels.keys()):
        pnl = best[f'pnl_{yr}']
        marker = "✅" if pnl > 0 else "❌" if pnl < 0 else "  "
        print(f"    {yr}: ${pnl:>+9,.0f}  {marker}  {regime_labels[yr]}")
    print(f"\n  Regime score: {int(best['years_positive'])}/6 years positive")

    # Save full results
    csv_path = os.path.join(OUTPUT_DIR, "backtest_v3_sweep_results.csv")
    results_df.to_csv(csv_path, index=False)
    print(f"\n📁 Full sweep results ({len(results_df)} configs) saved to:")
    print(f"   {csv_path}")


if __name__ == '__main__':
    main()
