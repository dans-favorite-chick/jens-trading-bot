"""
backtest_v3.py
Regime-stratified ES/NQ confluence backtest using Databento data.

Strategy (winning config from backtest_v2):
  - LONG only
  - Signal: NQ outperforming ES with high correlation
  - Entry: next 5-min bar close after signal
  - Stop: 24 ticks (entry - 6 points on MNQ)
  - Target: 48 ticks (entry + 12 points on MNQ) [2:1 R:R]
  - Correlation filter: > 0.9
  - All-day (no session filter)

Fill precision:
  - Uses 1-min bars to walk forward and detect exact stop/target hit timing
  - Conservative same-1min-bar resolution: stop wins (matches live behavior)

Output:
  - Aggregate stats (overall + per-year)
  - Trade-level CSV for further analysis
"""
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime

DATA_DIR = r"C:\Trading Project\phoenix_bot\data\historical"
OUTPUT_DIR = r"C:\Trading Project\phoenix_bot\backtest_results"

# ===== STRATEGY PARAMETERS =====
DIRECTION = "LONG"
BOOST_THRESHOLD = 7.0       # NQ-ES spread, scaled (configurable)
CORR_THRESHOLD = 0.90       # rolling NQ/ES correlation
STOP_TICKS = 24             # 6 points on MNQ
TARGET_TICKS = 48           # 12 points (2:1 R:R)
TICK_SIZE = 0.25            # MNQ tick = 0.25 index points
TICK_VALUE = 0.50           # $0.50 per tick per MNQ contract
Z_WINDOW = 50               # rolling lookback for confluence stats
MAX_HOLD_MINUTES = 240      # max 4-hour hold


def load_data():
    print("[1/5] Loading data...")
    mnq_5m = pd.read_csv(os.path.join(DATA_DIR, "mnq_5min_databento.csv"))
    mes_5m = pd.read_csv(os.path.join(DATA_DIR, "mes_5min_databento.csv"))
    mnq_1m = pd.read_csv(os.path.join(DATA_DIR, "mnq_1min_databento.csv"))

    for df in (mnq_5m, mes_5m, mnq_1m):
        df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)

    print(f"      MNQ 5-min: {len(mnq_5m):,} bars")
    print(f"      MES 5-min: {len(mes_5m):,} bars")
    print(f"      MNQ 1-min: {len(mnq_1m):,} bars")
    return mnq_5m, mes_5m, mnq_1m


def compute_signals(mnq_5m, mes_5m):
    print("\n[2/5] Computing ES/NQ confluence signals...")
    mnq = mnq_5m[['ts_utc', 'close']].rename(columns={'close': 'mnq_close'})
    mes = mes_5m[['ts_utc', 'close']].rename(columns={'close': 'mes_close'})

    df = pd.merge(mnq, mes, on='ts_utc', how='inner').sort_values('ts_utc').reset_index(drop=True)
    df['mnq_ret'] = df['mnq_close'].pct_change()
    df['mes_ret'] = df['mes_close'].pct_change()
    df['spread'] = df['mnq_ret'] - df['mes_ret']

    # Rolling correlation
    df['corr'] = df['mnq_ret'].rolling(Z_WINDOW).corr(df['mes_ret'])

    # Boost = NQ outperformance vs ES, scaled to "basis points × 100"
    # Example: NQ +0.5%, ES +0.2% → spread = 0.003 → boost = 30
    df['boost'] = df['spread'] * 10000

    df = df.dropna(subset=['boost', 'corr']).reset_index(drop=True)
    print(f"      Signal bars after warmup: {len(df):,}")
    print(f"      Boost distribution:")
    print(f"        Median: {df['boost'].median():.2f}")
    print(f"        95th pct: {df['boost'].quantile(0.95):.2f}")
    print(f"        99th pct: {df['boost'].quantile(0.99):.2f}")
    print(f"        99.9th pct: {df['boost'].quantile(0.999):.2f}")
    print(f"      Correlation distribution:")
    print(f"        Median: {df['corr'].median():.2f}")
    print(f"        90th pct: {df['corr'].quantile(0.90):.2f}")
    return df


def find_trade_signals(signals):
    print("\n[3/5] Finding trade signals...")
    if DIRECTION == 'LONG':
        sig_mask = (signals['boost'] >= BOOST_THRESHOLD) & (signals['corr'] > CORR_THRESHOLD)
    else:
        sig_mask = (signals['boost'] <= -BOOST_THRESHOLD) & (signals['corr'] > CORR_THRESHOLD)

    signal_bars = signals[sig_mask].copy()
    print(f"      Trade signals found: {len(signal_bars):,}")
    if len(signal_bars) == 0:
        print(f"      ⚠️  No signals — try lowering BOOST_THRESHOLD (current: {BOOST_THRESHOLD})")
    return signal_bars


def simulate_trade(entry_ts, entry_price, direction, mnq_1m_indexed):
    """Walk 1-min bars to find exit. Returns dict or None."""
    if direction == 'LONG':
        stop = entry_price - STOP_TICKS * TICK_SIZE
        target = entry_price + TARGET_TICKS * TICK_SIZE
    else:
        stop = entry_price + STOP_TICKS * TICK_SIZE
        target = entry_price - TARGET_TICKS * TICK_SIZE

    max_exit_ts = entry_ts + pd.Timedelta(minutes=MAX_HOLD_MINUTES)
    walk = mnq_1m_indexed.loc[entry_ts:max_exit_ts]
    if len(walk) <= 1:
        return None
    walk = walk.iloc[1:]  # skip the entry bar itself

    mfe_ticks = 0.0
    mae_ticks = 0.0

    for ts, bar in walk.iterrows():
        high = bar['high']
        low = bar['low']

        if direction == 'LONG':
            # Conservative: stop checked first if both stop and target hit same bar
            if low <= stop:
                return {
                    'exit_ts': ts, 'exit_price': stop, 'exit_reason': 'stop',
                    'pnl_ticks': (stop - entry_price) / TICK_SIZE,
                    'hold_minutes': (ts - entry_ts).total_seconds() / 60,
                    'mfe_ticks': mfe_ticks, 'mae_ticks': mae_ticks,
                }
            if high >= target:
                return {
                    'exit_ts': ts, 'exit_price': target, 'exit_reason': 'target',
                    'pnl_ticks': (target - entry_price) / TICK_SIZE,
                    'hold_minutes': (ts - entry_ts).total_seconds() / 60,
                    'mfe_ticks': mfe_ticks, 'mae_ticks': mae_ticks,
                }
            mfe_ticks = max(mfe_ticks, (high - entry_price) / TICK_SIZE)
            mae_ticks = min(mae_ticks, (low - entry_price) / TICK_SIZE)
        else:
            if high >= stop:
                return {
                    'exit_ts': ts, 'exit_price': stop, 'exit_reason': 'stop',
                    'pnl_ticks': (entry_price - stop) / TICK_SIZE,
                    'hold_minutes': (ts - entry_ts).total_seconds() / 60,
                    'mfe_ticks': mfe_ticks, 'mae_ticks': mae_ticks,
                }
            if low <= target:
                return {
                    'exit_ts': ts, 'exit_price': target, 'exit_reason': 'target',
                    'pnl_ticks': (entry_price - target) / TICK_SIZE,
                    'hold_minutes': (ts - entry_ts).total_seconds() / 60,
                    'mfe_ticks': mfe_ticks, 'mae_ticks': mae_ticks,
                }
            mfe_ticks = max(mfe_ticks, (entry_price - low) / TICK_SIZE)
            mae_ticks = min(mae_ticks, (entry_price - high) / TICK_SIZE)

    # Timeout exit
    last_ts, last_bar = walk.index[-1], walk.iloc[-1]
    exit_price = last_bar['close']
    pnl_ticks = ((exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)) / TICK_SIZE
    return {
        'exit_ts': last_ts, 'exit_price': exit_price, 'exit_reason': 'timeout',
        'pnl_ticks': pnl_ticks,
        'hold_minutes': MAX_HOLD_MINUTES,
        'mfe_ticks': mfe_ticks, 'mae_ticks': mae_ticks,
    }


def run_backtest(signals, signal_bars, mnq_1m):
    print(f"\n[4/5] Simulating {len(signal_bars):,} trades with 1-min fill precision...")
    print(f"      (this is the slow part — may take 1-3 minutes)")

    mnq_1m_indexed = mnq_1m.set_index('ts_utc').sort_index()

    trades = []
    signal_bars_reset = signal_bars.reset_index(drop=False).rename(columns={'index': 'orig_idx'})

    for i, sig in signal_bars_reset.iterrows():
        if i % 100 == 0 and i > 0:
            print(f"      Processed {i}/{len(signal_bars_reset)} signals...")

        orig_idx = sig['orig_idx']
        next_idx = orig_idx + 1
        if next_idx >= len(signals):
            continue

        entry_ts = signals.iloc[next_idx]['ts_utc']
        entry_price = signals.iloc[next_idx]['mnq_close']

        fill = simulate_trade(entry_ts, entry_price, DIRECTION, mnq_1m_indexed)
        if fill is None:
            continue

        trades.append({
            'signal_ts': sig['ts_utc'],
            'entry_ts': entry_ts,
            'entry_price': entry_price,
            'direction': DIRECTION,
            'boost': sig['boost'],
            'corr': sig['corr'],
            'pnl_dollars': fill['pnl_ticks'] * TICK_VALUE,
            **fill,
        })

    return pd.DataFrame(trades)


def analyze(trades, output_path):
    if len(trades) == 0:
        print("\n❌ NO TRADES — nothing to analyze")
        return

    print(f"\n[5/5] Analyzing {len(trades)} trades...")

    # Overall
    print(f"\n{'='*72}")
    print(f"📊 OVERALL RESULTS")
    print(f"{'='*72}")
    total = len(trades)
    wins = (trades['pnl_ticks'] > 0).sum()
    losses = (trades['pnl_ticks'] < 0).sum()
    wr = wins / total * 100
    total_pnl = trades['pnl_dollars'].sum()
    avg = trades['pnl_dollars'].mean()
    gross_win = trades[trades['pnl_dollars'] > 0]['pnl_dollars'].sum()
    gross_loss = abs(trades[trades['pnl_dollars'] < 0]['pnl_dollars'].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')

    print(f"  Total trades:    {total}")
    print(f"  Wins / Losses:   {wins} / {losses}")
    print(f"  Win rate:        {wr:.1f}%")
    print(f"  Total P&L:       ${total_pnl:+,.2f}")
    print(f"  Avg per trade:   ${avg:+,.2f}")
    print(f"  Profit factor:   {pf:.2f}")
    print(f"  Avg hold:        {trades['hold_minutes'].mean():.1f} min")
    print(f"  Avg MFE:         {trades['mfe_ticks'].mean():.1f} ticks")
    print(f"  Avg MAE:         {trades['mae_ticks'].mean():.1f} ticks")
    print(f"  Exit reasons:")
    for reason, count in trades['exit_reason'].value_counts().items():
        print(f"    {reason}: {count} ({count/total*100:.1f}%)")

    # By year
    print(f"\n{'='*72}")
    print(f"📊 BY YEAR (REGIME)")
    print(f"{'='*72}")
    trades['year'] = pd.to_datetime(trades['entry_ts'], utc=True).dt.year
    regime_labels = {
        2021: "🟢 Late bull peak",
        2022: "🐻 BEAR MARKET (-33%)",
        2023: "🟡 Recovery + SVB",
        2024: "🟢 AI rally + election",
        2025: "🟠 Tariff vol",
        2026: "🟢 Current YTD",
    }
    print(f"  {'Year':<6}{'N':>5}  {'WR':>6}  {'Total $':>11}  {'Avg $':>9}  {'PF':>6}  Regime")
    print(f"  {'-'*6}{'-'*5}  {'-'*6}  {'-'*11}  {'-'*9}  {'-'*6}  {'-'*30}")
    for year, sub in trades.groupby('year'):
        n = len(sub)
        w = (sub['pnl_ticks'] > 0).sum()
        wr_y = w / n * 100
        pnl_y = sub['pnl_dollars'].sum()
        avg_y = sub['pnl_dollars'].mean()
        gw = sub[sub['pnl_dollars'] > 0]['pnl_dollars'].sum()
        gl = abs(sub[sub['pnl_dollars'] < 0]['pnl_dollars'].sum())
        pf_y = gw / gl if gl > 0 else float('inf')
        regime = regime_labels.get(year, "")
        print(f"  {year:<6}{n:>5}  {wr_y:>5.1f}%  ${pnl_y:>+9,.0f}  ${avg_y:>+7,.2f}  {pf_y:>6.2f}  {regime}")

    # Save trades
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    trades.to_csv(output_path, index=False)
    print(f"\n📁 Trade-level data saved to: {output_path}")


def main():
    print("=" * 72)
    print(f"BACKTEST V3 — ES/NQ Confluence (5-min signal, 1-min fills)")
    print(f"Params: {DIRECTION}, boost≥{BOOST_THRESHOLD}, corr>{CORR_THRESHOLD}, "
          f"stop={STOP_TICKS}t, target={TARGET_TICKS}t")
    print("=" * 72)

    mnq_5m, mes_5m, mnq_1m = load_data()
    signals = compute_signals(mnq_5m, mes_5m)
    signal_bars = find_trade_signals(signals)

    if len(signal_bars) == 0:
        print("\n⚠️  No signals at current threshold. Try lowering BOOST_THRESHOLD.")
        print(f"    Re-run with BOOST_THRESHOLD = {signals['boost'].quantile(0.99):.1f} "
              f"(99th percentile)")
        return

    trades = run_backtest(signals, signal_bars, mnq_1m)
    output_path = os.path.join(OUTPUT_DIR, f"backtest_v3_trades_{DIRECTION}_b{BOOST_THRESHOLD}.csv")
    analyze(trades, output_path)


if __name__ == '__main__':
    main()
