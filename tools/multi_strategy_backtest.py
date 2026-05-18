"""
multi_strategy_backtest.py
Backtest canonical versions of Phoenix's active strategies on 5-year MNQ data.

Strategies tested (all using 24t stop / 48t target, 2:1 R:R for fair comparison):
  1. ORB-30          — Opening Range Breakout (first 30 min of RTH)
  2. IB Breakout     — Initial Balance (first 60 min) breakout
  3. VWAP Pullback   — Pullback to session VWAP, then bounce
  4. Bias Momentum   — Trend continuation on N-bar momentum
  5. Compression BO  — Low-ATR period followed by directional break
  6. ES/NQ Confluence — Our v3 winner (LONG, boost ≥ 25, corr > 0.85)

All strategies tested with same fill simulation (1-min precision).
Output: side-by-side comparison + per-year regime breakdown.
"""
import os
import pandas as pd
import numpy as np
import time

DATA_DIR = r"C:\Trading Project\phoenix_bot\data\historical"
OUTPUT_DIR = r"C:\Trading Project\phoenix_bot\backtest_results"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Common config — same for ALL strategies for fair comparison
STOP_TICKS = 24
TARGET_TICKS = 48
TICK_SIZE = 0.25
TICK_VALUE = 0.50
MAX_HOLD_MIN = 240

# RTH window in Chicago time (CME index futures)
RTH_START_HOUR = 8
RTH_START_MIN = 30
RTH_END_HOUR = 15
RTH_END_MIN = 15


def load_data():
    print("Loading data...")
    mnq_5m = pd.read_csv(os.path.join(DATA_DIR, "mnq_5min_databento.csv"))
    mes_5m = pd.read_csv(os.path.join(DATA_DIR, "mes_5min_databento.csv"))
    mnq_1m = pd.read_csv(os.path.join(DATA_DIR, "mnq_1min_databento.csv"))
    for d in (mnq_5m, mes_5m, mnq_1m):
        d['ts_utc'] = pd.to_datetime(d['ts_utc'], utc=True)
        d['ts_ct'] = d['ts_utc'].dt.tz_convert('America/Chicago')
        d['date'] = d['ts_ct'].dt.date
        d['minute_of_day'] = d['ts_ct'].dt.hour * 60 + d['ts_ct'].dt.minute
    print(f"  MNQ 5m: {len(mnq_5m):,}  MES 5m: {len(mes_5m):,}  MNQ 1m: {len(mnq_1m):,}")
    return mnq_5m, mes_5m, mnq_1m


def in_rth(minute_of_day):
    return (minute_of_day >= RTH_START_HOUR * 60 + RTH_START_MIN) & \
           (minute_of_day < RTH_END_HOUR * 60 + RTH_END_MIN)


# ===== Strategy implementations =====
# Each returns DataFrame with: signal_ts, direction, entry_price (taken on NEXT bar close)

def strategy_orb30(mnq_5m, **kw):
    """Opening Range Breakout: first 30 min of RTH defines OR. Trade break of OR until 11:00 CT."""
    df = mnq_5m.copy()
    df = df[in_rth(df['minute_of_day'])].copy()

    # OR window: 8:30 - 9:00 CT (minute_of_day 510 to 540)
    OR_START, OR_END = 510, 540
    TRADE_END = 11 * 60  # 11:00 CT cutoff

    # Get OR high/low per date
    or_window = df[(df['minute_of_day'] >= OR_START) & (df['minute_of_day'] < OR_END)]
    or_levels = or_window.groupby('date').agg(or_high=('high', 'max'), or_low=('low', 'min')).reset_index()

    df = df.merge(or_levels, on='date', how='left')
    # Look for breakouts AFTER OR window, before TRADE_END
    breakout_window = df[(df['minute_of_day'] >= OR_END) & (df['minute_of_day'] < TRADE_END)].copy()

    # First break of high = LONG, first break of low = SHORT
    signals = []
    for date, day_df in breakout_window.groupby('date'):
        day_df = day_df.sort_values('ts_utc')
        long_fired = False
        short_fired = False
        or_h = day_df['or_high'].iloc[0]
        or_l = day_df['or_low'].iloc[0]
        if pd.isna(or_h) or pd.isna(or_l):
            continue
        for _, row in day_df.iterrows():
            if not long_fired and row['high'] > or_h:
                signals.append({'signal_ts': row['ts_utc'], 'direction': 'LONG'})
                long_fired = True
            if not short_fired and row['low'] < or_l:
                signals.append({'signal_ts': row['ts_utc'], 'direction': 'SHORT'})
                short_fired = True
            if long_fired and short_fired:
                break
    return pd.DataFrame(signals)


def strategy_ib_breakout(mnq_5m, **kw):
    """Initial Balance: first 60 min defines IB. Trade break of IB until 13:00 CT."""
    df = mnq_5m.copy()
    df = df[in_rth(df['minute_of_day'])].copy()

    IB_START, IB_END = 510, 570  # 8:30 - 9:30 CT
    TRADE_END = 13 * 60  # 13:00 CT

    ib_window = df[(df['minute_of_day'] >= IB_START) & (df['minute_of_day'] < IB_END)]
    ib_levels = ib_window.groupby('date').agg(ib_high=('high', 'max'), ib_low=('low', 'min')).reset_index()
    df = df.merge(ib_levels, on='date', how='left')

    breakout_window = df[(df['minute_of_day'] >= IB_END) & (df['minute_of_day'] < TRADE_END)].copy()

    signals = []
    for date, day_df in breakout_window.groupby('date'):
        day_df = day_df.sort_values('ts_utc')
        long_fired = short_fired = False
        ib_h, ib_l = day_df['ib_high'].iloc[0], day_df['ib_low'].iloc[0]
        if pd.isna(ib_h) or pd.isna(ib_l):
            continue
        for _, row in day_df.iterrows():
            if not long_fired and row['high'] > ib_h:
                signals.append({'signal_ts': row['ts_utc'], 'direction': 'LONG'})
                long_fired = True
            if not short_fired and row['low'] < ib_l:
                signals.append({'signal_ts': row['ts_utc'], 'direction': 'SHORT'})
                short_fired = True
            if long_fired and short_fired:
                break
    return pd.DataFrame(signals)


def strategy_vwap_pullback(mnq_5m, **kw):
    """LONG when price pulls back to VWAP from above and bounces back up. RTH only."""
    df = mnq_5m.copy()
    df = df[in_rth(df['minute_of_day'])].copy()
    df = df.sort_values('ts_utc').reset_index(drop=True)

    # Compute session VWAP per date
    df['typical'] = (df['high'] + df['low'] + df['close']) / 3
    df['tp_vol'] = df['typical'] * df['volume']
    df['cum_tp_vol'] = df.groupby('date')['tp_vol'].cumsum()
    df['cum_vol'] = df.groupby('date')['volume'].cumsum()
    df['vwap'] = df['cum_tp_vol'] / df['cum_vol']

    df['prev_low'] = df['low'].shift(1)
    df['prev_close'] = df['close'].shift(1)

    # LONG signal: previous bar low touched/crossed VWAP from above,
    # current bar closes above VWAP (bounce confirmation),
    # AND price is in uptrend (current close > 20-bar SMA)
    df['sma20'] = df['close'].rolling(20).mean()
    long_sig = (
        (df['prev_low'] <= df['vwap']) &
        (df['prev_close'] > df['vwap']) &
        (df['close'] > df['vwap']) &
        (df['close'] > df['sma20'])
    )

    # SHORT: mirror (pullback up to VWAP, reject down, downtrend)
    df['prev_high'] = df['high'].shift(1)
    short_sig = (
        (df['prev_high'] >= df['vwap']) &
        (df['prev_close'] < df['vwap']) &
        (df['close'] < df['vwap']) &
        (df['close'] < df['sma20'])
    )

    signals = []
    for ts in df.loc[long_sig, 'ts_utc']:
        signals.append({'signal_ts': ts, 'direction': 'LONG'})
    for ts in df.loc[short_sig, 'ts_utc']:
        signals.append({'signal_ts': ts, 'direction': 'SHORT'})
    return pd.DataFrame(signals)


def strategy_bias_momentum(mnq_5m, **kw):
    """Trend continuation: enter in direction of strong 5-bar momentum when price breaks recent range."""
    df = mnq_5m.copy()
    df = df[in_rth(df['minute_of_day'])].copy()
    df = df.sort_values('ts_utc').reset_index(drop=True)

    df['ret_5bar'] = df['close'].pct_change(5)
    df['high_10'] = df['high'].rolling(10).max()
    df['low_10'] = df['low'].rolling(10).min()
    df['prev_high_10'] = df['high_10'].shift(1)
    df['prev_low_10'] = df['low_10'].shift(1)

    long_sig = (df['ret_5bar'] > 0.002) & (df['close'] > df['prev_high_10'])
    short_sig = (df['ret_5bar'] < -0.002) & (df['close'] < df['prev_low_10'])

    signals = []
    for ts in df.loc[long_sig.fillna(False), 'ts_utc']:
        signals.append({'signal_ts': ts, 'direction': 'LONG'})
    for ts in df.loc[short_sig.fillna(False), 'ts_utc']:
        signals.append({'signal_ts': ts, 'direction': 'SHORT'})
    return pd.DataFrame(signals)


def strategy_compression_breakout(mnq_5m, **kw):
    """Identify low-ATR periods (compression), enter on first break of compression range."""
    df = mnq_5m.copy()
    df = df[in_rth(df['minute_of_day'])].copy()
    df = df.sort_values('ts_utc').reset_index(drop=True)

    df['tr'] = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift(1)).abs(),
        (df['low'] - df['close'].shift(1)).abs()
    ], axis=1).max(axis=1)
    df['atr20'] = df['tr'].rolling(20).mean()
    df['atr_50bar_med'] = df['atr20'].rolling(50).median()

    df['compressed'] = df['atr20'] < df['atr_50bar_med'] * 0.7
    df['comp_high'] = df.where(df['compressed'])['high'].rolling(10).max()
    df['comp_low'] = df.where(df['compressed'])['low'].rolling(10).min()

    long_sig = (
        df['compressed'].shift(1) &
        (df['close'] > df['comp_high'].shift(1))
    )
    short_sig = (
        df['compressed'].shift(1) &
        (df['close'] < df['comp_low'].shift(1))
    )

    signals = []
    for ts in df.loc[long_sig.fillna(False), 'ts_utc']:
        signals.append({'signal_ts': ts, 'direction': 'LONG'})
    for ts in df.loc[short_sig.fillna(False), 'ts_utc']:
        signals.append({'signal_ts': ts, 'direction': 'SHORT'})
    return pd.DataFrame(signals)


def strategy_es_nq_confluence(mnq_5m, mes_5m=None, **kw):
    """The v3 winner: LONG when boost ≥ 25, corr > 0.85."""
    mnq = mnq_5m[['ts_utc', 'close']].rename(columns={'close': 'mnq_close'})
    mes = mes_5m[['ts_utc', 'close']].rename(columns={'close': 'mes_close'})
    df = pd.merge(mnq, mes, on='ts_utc', how='inner').sort_values('ts_utc').reset_index(drop=True)
    df['mnq_ret'] = df['mnq_close'].pct_change()
    df['mes_ret'] = df['mes_close'].pct_change()
    df['spread'] = df['mnq_ret'] - df['mes_ret']
    df['corr'] = df['mnq_ret'].rolling(50).corr(df['mes_ret'])
    df['boost'] = df['spread'] * 10000
    df = df.dropna(subset=['boost', 'corr'])
    sig_mask = (df['boost'] >= 25) & (df['corr'] > 0.85)
    signals = pd.DataFrame({'signal_ts': df.loc[sig_mask, 'ts_utc'], 'direction': 'LONG'})
    return signals.reset_index(drop=True)


# ===== Fill simulation =====
def simulate_entries(signals, mnq_5m_idx, mnq_1m_idx):
    """For each signal, enter at NEXT 5-min bar close, walk 1-min for fill."""
    if len(signals) == 0:
        return pd.DataFrame()

    trades = []
    for _, sig in signals.iterrows():
        sig_ts = sig['signal_ts']
        direction = sig['direction']
        # Find next 5-min bar
        try:
            next_bars = mnq_5m_idx.loc[sig_ts + pd.Timedelta(minutes=1):].head(1)
        except Exception:
            continue
        if len(next_bars) == 0:
            continue
        entry_ts = next_bars.index[0]
        entry_price = next_bars.iloc[0]['close']

        fill = walk_1m(entry_ts, entry_price, direction, mnq_1m_idx)
        if fill is None:
            continue
        trades.append({
            'signal_ts': sig_ts,
            'entry_ts': entry_ts,
            'entry_price': entry_price,
            'direction': direction,
            'year': pd.Timestamp(entry_ts).year,
            'pnl_dollars': fill['pnl_ticks'] * TICK_VALUE,
            **fill,
        })
    return pd.DataFrame(trades)


def walk_1m(entry_ts, entry_price, direction, m1_idx):
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
    walk = walk.iloc[1:]
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
    s_i = stop_hits.argmax() if has_stop else len(stop_hits)
    t_i = target_hits.argmax() if has_target else len(target_hits)

    if not has_stop and not has_target:
        exit_price = closes[-1]
        pnl = ((exit_price - entry_price) if direction == 'LONG' else (entry_price - exit_price)) / TICK_SIZE
        return {'pnl_ticks': pnl, 'exit_reason': 'timeout', 'hold_min': float(len(walk))}
    if s_i <= t_i:
        pnl = ((stop - entry_price) if direction == 'LONG' else (entry_price - stop)) / TICK_SIZE
        return {'pnl_ticks': pnl, 'exit_reason': 'stop', 'hold_min': float(s_i + 1)}
    else:
        pnl = ((target - entry_price) if direction == 'LONG' else (entry_price - target)) / TICK_SIZE
        return {'pnl_ticks': pnl, 'exit_reason': 'target', 'hold_min': float(t_i + 1)}


# ===== Analysis =====
def summarize(name, trades):
    if len(trades) == 0:
        return {'strategy': name, 'total_trades': 0, 'win_rate': 0, 'total_pnl': 0,
                'avg_per_trade': 0, 'profit_factor': 0, 'max_drawdown': 0,
                'pnl_2021': 0, 'pnl_2022': 0, 'pnl_2023': 0, 'pnl_2024': 0,
                'pnl_2025': 0, 'pnl_2026': 0, 'years_positive': 0}
    n = len(trades)
    wins = (trades['pnl_dollars'] > 0).sum()
    wr = wins / n * 100
    total = trades['pnl_dollars'].sum()
    avg = trades['pnl_dollars'].mean()
    gw = trades[trades['pnl_dollars'] > 0]['pnl_dollars'].sum()
    gl = abs(trades[trades['pnl_dollars'] < 0]['pnl_dollars'].sum())
    pf = gw / gl if gl > 0 else float('inf')
    cum = trades.sort_values('entry_ts')['pnl_dollars'].cumsum()
    dd = (cum - cum.expanding().max()).min()
    by_year = trades.groupby('year')['pnl_dollars'].sum().to_dict()
    yrs_pos = sum(1 for v in by_year.values() if v > 0)
    return {
        'strategy': name,
        'total_trades': n,
        'win_rate': round(wr, 1),
        'total_pnl': round(total, 0),
        'avg_per_trade': round(avg, 2),
        'profit_factor': round(min(pf, 99), 2),
        'max_drawdown': round(dd, 0),
        'pnl_2021': round(by_year.get(2021, 0), 0),
        'pnl_2022': round(by_year.get(2022, 0), 0),
        'pnl_2023': round(by_year.get(2023, 0), 0),
        'pnl_2024': round(by_year.get(2024, 0), 0),
        'pnl_2025': round(by_year.get(2025, 0), 0),
        'pnl_2026': round(by_year.get(2026, 0), 0),
        'years_positive': yrs_pos,
    }


def main():
    print("=" * 90)
    print("MULTI-STRATEGY BACKTEST — Phoenix canonical strategies vs ES/NQ Confluence")
    print(f"Stop: {STOP_TICKS}t  Target: {TARGET_TICKS}t  R:R = {TARGET_TICKS/STOP_TICKS:.1f}:1")
    print(f"Data: 5 years of MNQ + MES (2021-05 to 2026-05)")
    print("=" * 90)

    mnq_5m, mes_5m, mnq_1m = load_data()
    mnq_5m_idx = mnq_5m.set_index('ts_utc').sort_index()
    mnq_1m_idx = mnq_1m.set_index('ts_utc').sort_index()

    strategies = [
        ('ORB-30',            strategy_orb30),
        ('IB Breakout',       strategy_ib_breakout),
        ('VWAP Pullback',     strategy_vwap_pullback),
        ('Bias Momentum',     strategy_bias_momentum),
        ('Compression BO',    strategy_compression_breakout),
        ('ES/NQ Confluence',  strategy_es_nq_confluence),
    ]

    results = []
    for name, fn in strategies:
        print(f"\n[{name}] Generating signals...")
        t0 = time.time()
        sigs = fn(mnq_5m=mnq_5m, mes_5m=mes_5m)
        print(f"  Signals: {len(sigs)}")
        if len(sigs) > 5000:
            print(f"  ⚠️  Too many signals — sampling 5000")
            sigs = sigs.sample(5000, random_state=42).sort_values('signal_ts').reset_index(drop=True)
        trades = simulate_entries(sigs, mnq_5m_idx, mnq_1m_idx)
        print(f"  Trades simulated: {len(trades)} ({time.time()-t0:.0f}s)")
        results.append(summarize(name, trades))

    # Display comparison
    print(f"\n{'='*120}")
    print(f"🏆 STRATEGY COMPARISON")
    print(f"{'='*120}")
    df_res = pd.DataFrame(results).sort_values('total_pnl', ascending=False).reset_index(drop=True)

    show_cols = ['strategy', 'total_trades', 'win_rate', 'total_pnl', 'avg_per_trade',
                 'profit_factor', 'max_drawdown', 'years_positive', 'pnl_2022']
    pd.set_option('display.width', 200)
    pd.set_option('display.max_colwidth', 30)
    print(df_res[show_cols].to_string(index=False))

    # Regime detail per strategy
    print(f"\n{'='*120}")
    print(f"📊 REGIME BREAKDOWN BY STRATEGY (P&L per year)")
    print(f"{'='*120}")
    year_cols = ['strategy', 'pnl_2021', 'pnl_2022', 'pnl_2023', 'pnl_2024', 'pnl_2025', 'pnl_2026', 'years_positive']
    print(df_res[year_cols].to_string(index=False))

    csv = os.path.join(OUTPUT_DIR, "multi_strategy_comparison.csv")
    df_res.to_csv(csv, index=False)
    print(f"\n📁 Saved: {csv}")

    # Key insights
    print(f"\n{'='*120}")
    print(f"💡 KEY INSIGHTS")
    print(f"{'='*120}")
    pos_2022 = df_res[df_res['pnl_2022'] > 0]
    print(f"  Strategies profitable in 2022 bear: {len(pos_2022)}/{len(df_res)}")
    for _, r in pos_2022.iterrows():
        print(f"    ✅ {r['strategy']:<22}  $+{r['pnl_2022']:>6,.0f}")
    neg_strats = df_res[df_res['total_pnl'] < 0]
    if len(neg_strats):
        print(f"\n  ❌ Net LOSING strategies (5-year):")
        for _, r in neg_strats.iterrows():
            print(f"    ❌ {r['strategy']:<22}  ${r['total_pnl']:>+7,.0f}")


if __name__ == "__main__":
    main()
