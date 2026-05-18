"""
Phoenix Confluence Filter Experiments — Phase 13b
==================================================

Takes the existing trade entries from phoenix_real_backtest output and
tests whether adding confluence filters (EMA trend alignment, volume,
time-of-day, strategy co-firing) improves WR + total P&L.

Specifically answers:
  1. Does an EMA-trend-alignment filter (only take signals matching 5m
     EMA9 vs EMA21 direction) improve WR?
  2. Does a volume filter (only take signals when entry-bar volume is
     >= 1.5x recent avg) improve P&L?
  3. Does a time-of-day filter (skip 10-15 CT) help every strategy or
     just some?
  4. Does strategy co-firing (2+ strategies fire same hour) flag the
     highest-conviction signals?
  5. COMPRESSION AUTOPSY: what distinguishes the 11 compression_breakout
     winners from the 37 losers?

Each filter is applied independently and in combination. Output shows
per-strategy × per-filter: trades kept, WR, total $, vs unfiltered.

USAGE
-----
    python tools/phoenix_confluence_filters.py \\
        --trades backtest_results/phoenix_real_2025.csv \\
        --mnq data/historical/mnq_1min_databento.csv \\
        --out backtest_results/phoenix_confluence_filters.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("confluence_filters")


# ════════════════════════════════════════════════════════════════════
# Section 1: Per-entry feature computation
# ════════════════════════════════════════════════════════════════════

def annotate_trades_with_features(trades: pd.DataFrame,
                                    mnq_indexed: pd.DataFrame) -> pd.DataFrame:
    """For each trade, compute features at entry_ts:
       - ema9_5m, ema21_5m (computed from 1m bars; close enough)
       - vol_ratio (entry-bar volume / avg of last 20 bars)
       - prior_5min_high, prior_5min_low (for breakout pattern check)
       - hour_ct, weekday_ct
       - bar_close_position (where in the bar's range did close occur)
       - prior_5_bar_trend (% change over 5 bars before entry)
    """
    features = []
    total = len(trades)
    for i, row in enumerate(trades.itertuples(index=False), 1):
        if i % 1000 == 0:
            logger.info(f"[annotate] {i:,}/{total:,}")
        ts = row.entry_ts
        # Get last 50 1m bars at or before entry (inclusive)
        history = mnq_indexed[mnq_indexed.index <= ts].tail(50)
        if len(history) < 22:
            features.append({})
            continue
        closes = history['close'].values
        volumes = history['volume'].values
        highs = history['high'].values
        lows = history['low'].values
        # EMAs from 1m bars
        ema9 = pd.Series(closes).ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = pd.Series(closes).ewm(span=21, adjust=False).mean().iloc[-1]
        ema_spread = ema9 - ema21  # >0 bullish, <0 bearish
        # Volume ratio: entry bar / avg of last 20
        cur_vol = volumes[-1]
        avg_vol = volumes[-21:-1].mean() if len(volumes) >= 21 else volumes[:-1].mean()
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
        # Bar close position in range (0=low, 1=high)
        bar = history.iloc[-1]
        rng = bar['high'] - bar['low']
        if rng > 0:
            close_pos = (bar['close'] - bar['low']) / rng
        else:
            close_pos = 0.5
        # Prior 5-bar trend
        if len(closes) >= 5:
            prior_5_trend_pct = (closes[-1] - closes[-5]) / closes[-5] * 100
        else:
            prior_5_trend_pct = 0.0
        # Hour CT
        hour_ct = ts.tz_convert('America/Chicago').hour
        weekday = ts.tz_convert('America/Chicago').strftime('%a')
        features.append({
            'ema9_1m': ema9,
            'ema21_1m': ema21,
            'ema_spread': ema_spread,
            'vol_ratio': vol_ratio,
            'bar_close_pos': close_pos,
            'prior_5_trend_pct': prior_5_trend_pct,
            'hour_ct': hour_ct,
            'weekday': weekday,
        })
    feat_df = pd.DataFrame(features, index=trades.index)
    return pd.concat([trades, feat_df], axis=1)


# ════════════════════════════════════════════════════════════════════
# Section 2: Filter definitions
# ════════════════════════════════════════════════════════════════════

def apply_filter(annotated: pd.DataFrame, filter_name: str) -> pd.DataFrame:
    """Return subset of trades that pass the named filter."""
    df = annotated.copy()
    if filter_name == 'all':
        return df
    elif filter_name == 'ema_aligned':
        # LONG signals: ema_spread > 0 (bullish trend)
        # SHORT signals: ema_spread < 0 (bearish trend)
        long_ok = (df.direction == 'LONG') & (df.ema_spread > 0)
        short_ok = (df.direction == 'SHORT') & (df.ema_spread < 0)
        return df[long_ok | short_ok]
    elif filter_name == 'ema_counter':
        # Mean reversion: take signals AGAINST the EMA trend
        long_ok = (df.direction == 'LONG') & (df.ema_spread < 0)
        short_ok = (df.direction == 'SHORT') & (df.ema_spread > 0)
        return df[long_ok | short_ok]
    elif filter_name == 'vol_above_1_5x':
        return df[df.vol_ratio >= 1.5]
    elif filter_name == 'vol_above_2x':
        return df[df.vol_ratio >= 2.0]
    elif filter_name == 'time_skip_10_15ct':
        # Drop 10:00 <= hour < 15:00 CT (the dead zone)
        return df[~((df.hour_ct >= 10) & (df.hour_ct < 15))]
    elif filter_name == 'time_only_rth_open':
        # Only take RTH first hour (8 + 9 CT)
        return df[(df.hour_ct >= 8) & (df.hour_ct < 10)]
    elif filter_name == 'time_only_overnight':
        # Only 0-7 CT (overnight + pre-open)
        return df[(df.hour_ct >= 0) & (df.hour_ct < 8)]
    elif filter_name == 'skip_tue_fri':
        return df[~df.weekday.isin(['Tue', 'Fri'])]
    elif filter_name == 'bar_close_strong':
        # LONG: close in top 30% of bar range
        # SHORT: close in bottom 30%
        long_ok = (df.direction == 'LONG') & (df.bar_close_pos >= 0.7)
        short_ok = (df.direction == 'SHORT') & (df.bar_close_pos <= 0.3)
        return df[long_ok | short_ok]
    elif filter_name == 'trend_continuation':
        # LONG with positive prior-5 trend; SHORT with negative
        long_ok = (df.direction == 'LONG') & (df.prior_5_trend_pct > 0.05)
        short_ok = (df.direction == 'SHORT') & (df.prior_5_trend_pct < -0.05)
        return df[long_ok | short_ok]
    elif filter_name == 'combo_ema_vol':
        # EMA-aligned AND volume >= 1.5x
        long_ok = (df.direction == 'LONG') & (df.ema_spread > 0)
        short_ok = (df.direction == 'SHORT') & (df.ema_spread < 0)
        return df[(long_ok | short_ok) & (df.vol_ratio >= 1.5)]
    elif filter_name == 'combo_ema_time':
        # EMA-aligned AND outside dead zone
        long_ok = (df.direction == 'LONG') & (df.ema_spread > 0)
        short_ok = (df.direction == 'SHORT') & (df.ema_spread < 0)
        time_ok = ~((df.hour_ct >= 10) & (df.hour_ct < 15))
        return df[(long_ok | short_ok) & time_ok]
    elif filter_name == 'combo_all':
        long_ok = (df.direction == 'LONG') & (df.ema_spread > 0)
        short_ok = (df.direction == 'SHORT') & (df.ema_spread < 0)
        time_ok = ~((df.hour_ct >= 10) & (df.hour_ct < 15))
        return df[(long_ok | short_ok) & (df.vol_ratio >= 1.5) & time_ok]
    else:
        raise ValueError(f"Unknown filter: {filter_name}")


FILTER_NAMES = [
    'all',  # baseline
    'ema_aligned',
    'ema_counter',
    'vol_above_1_5x',
    'vol_above_2x',
    'time_skip_10_15ct',
    'time_only_rth_open',
    'time_only_overnight',
    'skip_tue_fri',
    'bar_close_strong',
    'trend_continuation',
    'combo_ema_vol',
    'combo_ema_time',
    'combo_all',
]


# ════════════════════════════════════════════════════════════════════
# Section 3: Per-strategy filter summary
# ════════════════════════════════════════════════════════════════════

def summarize_filters(annotated: pd.DataFrame) -> pd.DataFrame:
    """For each (strategy, filter): trades kept, WR, total $, avg $."""
    rows = []
    strategies = sorted(annotated.strategy.unique())
    for strat in strategies:
        sdf = annotated[annotated.strategy == strat]
        baseline_n = len(sdf)
        baseline_pnl = sdf.pnl_dollars.sum()
        for filt in FILTER_NAMES:
            sub = apply_filter(sdf, filt)
            n = len(sub)
            if n == 0:
                wr = 0.0
                total = 0.0
                avg = 0.0
            else:
                wr = (sub.pnl_dollars > 0).mean() * 100
                total = sub.pnl_dollars.sum()
                avg = sub.pnl_dollars.mean()
            kept_pct = n / baseline_n * 100 if baseline_n > 0 else 0
            pnl_lift = total - baseline_pnl
            rows.append({
                'strategy': strat,
                'filter': filt,
                'n_kept': n,
                'kept_pct': round(kept_pct, 1),
                'wr_pct': round(wr, 1),
                'total': round(total, 0),
                'avg': round(avg, 2),
                'pnl_lift_vs_baseline': round(pnl_lift, 0),
            })
    return pd.DataFrame(rows)


# ════════════════════════════════════════════════════════════════════
# Section 4: Compression-breakout autopsy
# ════════════════════════════════════════════════════════════════════

def compression_autopsy(annotated: pd.DataFrame) -> None:
    """Specifically analyze compression_breakout_v2 + micro entries to
    find what distinguishes winners from losers."""
    cb = annotated[annotated.strategy.isin(
        ['compression_breakout_v2', 'compression_breakout_micro']
    )].copy()
    if cb.empty:
        print("No compression trades in dataset")
        return
    cb['win'] = (cb.pnl_dollars > 0)
    cb['mfe_mae_ratio'] = None  # would need MFE/MAE data

    print()
    print("=" * 90)
    print("COMPRESSION BREAKOUT AUTOPSY")
    print("=" * 90)
    print(f"  Total compression trades: {len(cb)}")
    print(f"  Wins: {cb.win.sum()} ({cb.win.mean()*100:.1f}%)")
    print(f"  Total P&L: ${cb.pnl_dollars.sum():+.0f}")
    print()
    print("=== Winners vs Losers — feature comparison ===")
    win_stats = cb[cb.win].agg({
        'ema_spread': 'mean',
        'vol_ratio': 'mean',
        'bar_close_pos': 'mean',
        'prior_5_trend_pct': 'mean',
        'hour_ct': 'mean',
    }).round(2)
    loss_stats = cb[~cb.win].agg({
        'ema_spread': 'mean',
        'vol_ratio': 'mean',
        'bar_close_pos': 'mean',
        'prior_5_trend_pct': 'mean',
        'hour_ct': 'mean',
    }).round(2)
    compare = pd.DataFrame({
        'winners_mean': win_stats,
        'losers_mean': loss_stats,
        'delta': (win_stats - loss_stats).round(2),
    })
    print(compare.to_string())
    print()
    print("=== Compression x EMA-alignment ===")
    cb['ema_aligned'] = ((cb.direction == 'LONG') & (cb.ema_spread > 0)) | \
                         ((cb.direction == 'SHORT') & (cb.ema_spread < 0))
    align_stats = cb.groupby(['strategy', 'ema_aligned']).agg(
        n=('pnl_dollars', 'count'),
        wins=('win', 'sum'),
        total=('pnl_dollars', 'sum'),
    )
    align_stats['wr%'] = (align_stats.wins / align_stats.n * 100).round(1)
    print(align_stats.to_string())
    print()
    print("=== Compression x volume ratio buckets ===")
    cb['vol_bucket'] = pd.cut(cb.vol_ratio, bins=[0, 0.5, 1, 1.5, 2, 5, 100],
                                labels=['<0.5x','0.5-1x','1-1.5x','1.5-2x','2-5x','>5x'])
    vol_stats = cb.groupby(['strategy', 'vol_bucket']).agg(
        n=('pnl_dollars', 'count'),
        wins=('win', 'sum'),
        total=('pnl_dollars', 'sum'),
    )
    vol_stats['wr%'] = (vol_stats.wins / vol_stats.n * 100).round(1)
    print(vol_stats.to_string())
    print()
    print("=== Compression x time-of-day ===")
    time_stats = cb.groupby(['strategy', 'hour_ct']).agg(
        n=('pnl_dollars', 'count'),
        wins=('win', 'sum'),
        total=('pnl_dollars', 'sum'),
    )
    time_stats['wr%'] = (time_stats.wins / time_stats.n * 100).round(1)
    print(time_stats.to_string())


# ════════════════════════════════════════════════════════════════════
# Section 5: CLI
# ════════════════════════════════════════════════════════════════════

def print_filter_summary(summary: pd.DataFrame) -> None:
    print()
    print("=" * 110)
    print("CONFLUENCE FILTER COMPARISON — per strategy × per filter")
    print("=" * 110)
    print()
    print("Reading guide:")
    print("  kept_pct = % of trades that pass the filter (lower = more selective)")
    print("  total    = total P&L for the filtered subset")
    print("  pnl_lift = total - baseline; positive = filter HELPS")
    print()
    for strat in sorted(summary.strategy.unique()):
        s = summary[summary.strategy == strat].sort_values('total', ascending=False)
        print(f"--- {strat} ({s[s.filter=='all'].iloc[0].n_kept} baseline trades) ---")
        print(s[['filter','n_kept','kept_pct','wr_pct','total','avg','pnl_lift_vs_baseline']].to_string(index=False))
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True)
    ap.add_argument("--mnq", default="data/historical/mnq_1min_databento.csv")
    ap.add_argument("--out", default="backtest_results/phoenix_confluence_filters.csv")
    args = ap.parse_args()

    logger.info(f"[main] loading {args.trades}")
    trades = pd.read_csv(args.trades, parse_dates=['entry_ts', 'exit_ts'])
    # Drop noise_area (degenerate target-equals-entry trades)
    trades = trades[trades.strategy != 'noise_area'].copy()
    logger.info(f"[main] {len(trades):,} non-noise trades")

    logger.info(f"[main] loading MNQ 1m")
    mnq = pd.read_csv(args.mnq, parse_dates=['ts_utc'])
    mnq = mnq.rename(columns={'ts_utc': 'ts'}).set_index('ts').sort_index()
    logger.info(f"[main] {len(mnq):,} 1m bars")

    logger.info(f"[main] annotating trades with features...")
    annotated = annotate_trades_with_features(trades, mnq)
    logger.info(f"[main] annotation done")

    summary = summarize_filters(annotated)
    out_path = ROOT / args.out
    out_path.parent.mkdir(exist_ok=True)
    summary.to_csv(out_path, index=False)
    logger.info(f"[main] wrote summary to {out_path}")

    print_filter_summary(summary)
    compression_autopsy(annotated)


if __name__ == "__main__":
    main()
