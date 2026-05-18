"""
exit_methodology_v3.py
Compare different exit strategies on the winning regime-robust config.

Holds entry signal constant (LONG, boost ≥ 25, corr > 0.85) and varies exits:
  - Stop / Target combos (different R:R, different absolute sizes)
  - Time-based exits (close after N minutes regardless)
  - Trailing stops (breakeven after 1R, chandelier after 1R)
  - Hybrid exits (time + stop/target)

Reports: total P&L, win rate, PF, max drawdown, regime consistency for each exit.
"""
import os
import pandas as pd
import numpy as np
import time

DATA_DIR = r"C:\Trading Project\phoenix_bot\data\historical"
OUTPUT_DIR = r"C:\Trading Project\phoenix_bot\backtest_results"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Winning config (frozen)
DIRECTION = "LONG"
BOOST_THRESHOLD = 25.0
CORR_THRESHOLD = 0.85
TICK_SIZE = 0.25
TICK_VALUE = 0.50
Z_WINDOW = 50

# Exit methodology definitions — each is a function that returns exit result
# Stop/target in ticks. Time in minutes.


def find_entries():
    """Compute signals using same logic as v3_sweep, return entry list."""
    print("[1/4] Loading data and computing entry signals...")
    mnq_5m = pd.read_csv(os.path.join(DATA_DIR, "mnq_5min_databento.csv"))
    mes_5m = pd.read_csv(os.path.join(DATA_DIR, "mes_5min_databento.csv"))
    mnq_1m = pd.read_csv(os.path.join(DATA_DIR, "mnq_1min_databento.csv"))
    for d in (mnq_5m, mes_5m, mnq_1m):
        d['ts_utc'] = pd.to_datetime(d['ts_utc'], utc=True)

    mnq = mnq_5m[['ts_utc', 'close']].rename(columns={'close': 'mnq_close'})
    mes = mes_5m[['ts_utc', 'close']].rename(columns={'close': 'mes_close'})
    df = pd.merge(mnq, mes, on='ts_utc', how='inner').sort_values('ts_utc').reset_index(drop=True)
    df['mnq_ret'] = df['mnq_close'].pct_change()
    df['mes_ret'] = df['mes_close'].pct_change()
    df['spread'] = df['mnq_ret'] - df['mes_ret']
    df['boost'] = df['spread'] * 10000
    df['corr'] = df['mnq_ret'].rolling(Z_WINDOW).corr(df['mes_ret'])
    df = df.dropna(subset=['boost', 'corr']).reset_index(drop=True)

    sig_mask = (df['boost'] >= BOOST_THRESHOLD) & (df['corr'] > CORR_THRESHOLD)
    entries = []
    for idx in df.index[sig_mask]:
        next_idx = idx + 1
        if next_idx >= len(df):
            continue
        entries.append({
            'signal_ts': df.iloc[idx]['ts_utc'],
            'entry_ts': df.iloc[next_idx]['ts_utc'],
            'entry_price': df.iloc[next_idx]['mnq_close'],
        })
    print(f"      {len(entries)} entries identified")
    return entries, mnq_1m.set_index('ts_utc').sort_index()


def simulate_with_exits(entry_ts, entry_price, m1_idx, stop_ticks, target_ticks,
                       time_limit_min=240, trail_after_r=None, breakeven_after_r=None):
    """
    Simulate one trade with configurable exits.
    - stop_ticks/target_ticks: fixed S/T in ticks
    - time_limit_min: max hold
    - trail_after_r: if set, move stop to (current_high - 1R) once 1R achieved (only LONG)
    - breakeven_after_r: if set, move stop to entry once Nx R achieved
    """
    stop = entry_price - stop_ticks * TICK_SIZE
    target = entry_price + target_ticks * TICK_SIZE
    r_value = stop_ticks * TICK_SIZE  # 1R = initial risk in points

    max_ts = entry_ts + pd.Timedelta(minutes=time_limit_min)
    walk = m1_idx.loc[entry_ts:max_ts]
    if len(walk) <= 1:
        return None
    walk = walk.iloc[1:]

    highs = walk['high'].to_numpy()
    lows = walk['low'].to_numpy()
    closes = walk['close'].to_numpy()
    n = len(highs)

    current_stop = stop
    high_water = entry_price

    for i in range(n):
        high = highs[i]
        low = lows[i]

        # === Check exits using stops/targets as of START of this bar ===
        # (Trail from prior bars is already baked into current_stop)
        if low <= current_stop:
            exit_price = current_stop
            pnl_ticks = (exit_price - entry_price) / TICK_SIZE
            return {'pnl_ticks': pnl_ticks, 'exit_reason': 'stop', 'hold_min': float(i + 1)}
        if high >= target:
            exit_price = target
            pnl_ticks = (exit_price - entry_price) / TICK_SIZE
            return {'pnl_ticks': pnl_ticks, 'exit_reason': 'target', 'hold_min': float(i + 1)}

        # === End-of-bar: update high water mark and trail/BE ===
        # (These will affect the NEXT bar's stop check — realistic for 1-min bars)
        if high > high_water:
            high_water = high

        if breakeven_after_r is not None:
            if high_water - entry_price >= breakeven_after_r * r_value:
                current_stop = max(current_stop, entry_price)
        if trail_after_r is not None:
            if high_water - entry_price >= trail_after_r * r_value:
                new_trail = high_water - r_value
                # CAP at just below target — trail can't exit above target in real trading
                new_trail = min(new_trail, target - TICK_SIZE)
                current_stop = max(current_stop, new_trail)

    # Timeout — exit at last close
    exit_price = closes[-1]
    pnl_ticks = (exit_price - entry_price) / TICK_SIZE
    return {'pnl_ticks': pnl_ticks, 'exit_reason': 'timeout', 'hold_min': float(n)}


def run_methodology(entries, m1_idx, name, **kwargs):
    """Run one exit methodology across all entries."""
    trades = []
    for e in entries:
        fill = simulate_with_exits(e['entry_ts'], e['entry_price'], m1_idx, **kwargs)
        if fill is None:
            continue
        trades.append({
            'entry_ts': e['entry_ts'],
            'year': pd.Timestamp(e['entry_ts']).year,
            'pnl_dollars': fill['pnl_ticks'] * TICK_VALUE,
            **fill,
        })
    if not trades:
        return None
    tdf = pd.DataFrame(trades)
    total = len(tdf)
    wins = (tdf['pnl_dollars'] > 0).sum()
    wr = wins / total * 100
    total_pnl = tdf['pnl_dollars'].sum()
    avg = tdf['pnl_dollars'].mean()
    gw = tdf[tdf['pnl_dollars'] > 0]['pnl_dollars'].sum()
    gl = abs(tdf[tdf['pnl_dollars'] < 0]['pnl_dollars'].sum())
    pf = gw / gl if gl > 0 else float('inf')

    # Drawdown calculation
    cum = tdf.sort_values('entry_ts')['pnl_dollars'].cumsum()
    running_max = cum.expanding().max()
    drawdown = (cum - running_max).min()

    # Regime year breakdown
    by_year = tdf.groupby('year')['pnl_dollars'].sum().to_dict()
    yrs_pos = sum(1 for v in by_year.values() if v > 0)

    return {
        'methodology': name,
        'total_trades': total,
        'win_rate': wr,
        'total_pnl': total_pnl,
        'avg_per_trade': avg,
        'profit_factor': min(pf, 99),
        'max_drawdown': drawdown,
        'avg_hold_min': tdf['hold_min'].mean(),
        'years_positive': yrs_pos,
        'pnl_2022': by_year.get(2022, 0),
        'pnl_2023': by_year.get(2023, 0),
        'pnl_2024': by_year.get(2024, 0),
        'pnl_2025': by_year.get(2025, 0),
        'pnl_2026': by_year.get(2026, 0),
    }


def build_methodology_grid():
    """Define ~30 exit methodologies to test."""
    methods = []

    # 1. Symmetric R:R variations (2:1)
    for s in [12, 18, 24, 30, 36, 48]:
        methods.append((f"Fixed {s}t/{2*s}t (2:1)", dict(stop_ticks=s, target_ticks=2*s)))

    # 2. Same stop, different R:R
    for r in [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]:
        t = int(24 * r)
        methods.append((f"Fixed 24t/{t}t ({r}:1)", dict(stop_ticks=24, target_ticks=t)))

    # 3. Same target, different stops
    for s in [12, 16, 20, 24, 32, 40]:
        methods.append((f"Fixed {s}t/48t (varied stop)", dict(stop_ticks=s, target_ticks=48)))

    # 4. Time exits + fixed S/T
    for t_min in [5, 10, 15, 30, 60]:
        methods.append((f"24t/48t with {t_min}-min time exit",
                        dict(stop_ticks=24, target_ticks=48, time_limit_min=t_min)))

    # 5. Breakeven trailing
    for be_r in [0.5, 1.0, 1.5]:
        methods.append((f"24t/48t + BE@{be_r}R",
                        dict(stop_ticks=24, target_ticks=48, breakeven_after_r=be_r)))

    # 6. Trailing after 1R
    for trail_r in [0.5, 1.0]:
        methods.append((f"24t/48t + trail@{trail_r}R",
                        dict(stop_ticks=24, target_ticks=48, trail_after_r=trail_r)))

    # 7. Aggressive tight scalp
    methods.append(("Tight 8t/16t (2:1)", dict(stop_ticks=8, target_ticks=16)))
    methods.append(("Ultra-tight 6t/12t (2:1)", dict(stop_ticks=6, target_ticks=12)))

    # Dedupe by methodology name (some combinations overlap)
    seen = set()
    unique = []
    for name, kw in methods:
        if name not in seen:
            seen.add(name)
            unique.append((name, kw))
    return unique


def main():
    print("=" * 90)
    print("EXIT METHODOLOGY COMPARATOR")
    print(f"Entry: LONG, boost≥{BOOST_THRESHOLD}, corr>{CORR_THRESHOLD} (winning regime-robust config)")
    print("=" * 90)

    entries, m1_idx = find_entries()
    methods = build_methodology_grid()
    print(f"\n[2/4] Testing {len(methods)} exit methodologies on {len(entries)} entries...")

    t0 = time.time()
    results = []
    for i, (name, kwargs) in enumerate(methods):
        if i % 5 == 0 and i > 0:
            elapsed = time.time() - t0
            print(f"  [{i}/{len(methods)}]  elapsed={elapsed:.0f}s")
        r = run_methodology(entries, m1_idx, name, **kwargs)
        if r is not None:
            results.append(r)
    print(f"  All done in {time.time()-t0:.0f}s\n")

    if not results:
        print("❌ No methodologies produced trades")
        return

    df = pd.DataFrame(results).sort_values('total_pnl', ascending=False).reset_index(drop=True)

    # Display table
    print(f"{'='*120}")
    print(f"🏆 TOP 15 EXIT METHODOLOGIES BY TOTAL P&L")
    print(f"{'='*120}")
    show_cols = ['methodology', 'total_trades', 'win_rate', 'total_pnl',
                 'avg_per_trade', 'profit_factor', 'max_drawdown', 'avg_hold_min',
                 'years_positive', 'pnl_2022']
    top = df.head(15)[show_cols].copy()
    top['win_rate'] = top['win_rate'].round(1)
    top['total_pnl'] = top['total_pnl'].round(0).astype(int)
    top['avg_per_trade'] = top['avg_per_trade'].round(2)
    top['profit_factor'] = top['profit_factor'].round(2)
    top['max_drawdown'] = top['max_drawdown'].round(0).astype(int)
    top['avg_hold_min'] = top['avg_hold_min'].round(1)
    top['pnl_2022'] = top['pnl_2022'].round(0).astype(int)

    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_colwidth', 45)
    print(top.to_string(index=False))

    # Best methodology detail
    print(f"\n{'='*120}")
    print(f"🥇 BEST EXIT — DETAILED BREAKDOWN")
    print(f"{'='*120}")
    best = df.iloc[0]
    print(f"  Methodology:      {best['methodology']}")
    print(f"  Total trades:     {int(best['total_trades'])}")
    print(f"  Win rate:         {best['win_rate']:.1f}%")
    print(f"  Total P&L:        ${best['total_pnl']:+,.2f}")
    print(f"  Avg/trade:        ${best['avg_per_trade']:+,.2f}")
    print(f"  Profit factor:    {best['profit_factor']:.2f}")
    print(f"  Max drawdown:     ${best['max_drawdown']:,.2f}")
    print(f"  Avg hold:         {best['avg_hold_min']:.1f} min")
    print(f"\n  Regime breakdown:")
    for yr in [2022, 2023, 2024, 2025, 2026]:
        v = best.get(f'pnl_{yr}', 0)
        mk = "✅" if v > 0 else "❌"
        print(f"    {yr}: ${v:>+8,.0f}  {mk}")
    print(f"  Years positive: {int(best['years_positive'])}/5")

    # Compare to baseline
    baseline = df[df['methodology'].str.startswith("Fixed 24t/48t (2.0:1)")]
    if len(baseline) > 0:
        b = baseline.iloc[0]
        delta_pnl = best['total_pnl'] - b['total_pnl']
        delta_pct = delta_pnl / b['total_pnl'] * 100 if b['total_pnl'] else 0
        print(f"\n  vs baseline (24t/48t 2:1): "
              f"${delta_pnl:+,.0f} ({delta_pct:+.1f}%)")

    # Save
    csv = os.path.join(OUTPUT_DIR, "exit_methodology_v3_results.csv")
    df.to_csv(csv, index=False)
    print(f"\n📁 Full results saved to: {csv}")


if __name__ == "__main__":
    main()
