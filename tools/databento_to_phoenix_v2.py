"""
Databento → Phoenix converter (v2).

Input:  glbx-mdp3-*.ohlcv-1m.csv (multi-contract Databento dump)
Output: For each instrument family (MNQ, MES):
          - {family}_1min_databento.csv  (raw 1-min, front-month filtered)
          - {family}_5min_databento.csv  (aggregated 5-min, for signal logic)

Logic:
  1. Filter to single-contract symbols (MNQ/MES + month code + year digit). Drop spreads.
  2. Per day per family, identify front-month by daily volume.
  3. Auto-detect rollover dates and report them.
  4. Save 1-min as-is (front-month filtered).
  5. Aggregate to 5-min and save separately.

Backtest usage:
  - Use 5-min for ES/NQ confluence signals (matches strategy design).
  - Use 1-min for fill simulation when entry+exit could happen same 5-min bar
    (reveals actual stop-vs-target sequence).
"""
import os
import re
import sys
import pandas as pd

INPUT = r"C:\Trading Project\phoenix_bot\data\historical\glbx-mdp3-20210517-20260517.ohlcv-1m.csv"
OUTPUT_DIR = r"C:\Trading Project\phoenix_bot\data\historical"

SYMBOL_PATTERN = re.compile(r'^(MNQ|MES)[HMUZ]\d{1,2}$')


def main():
    print("=" * 70)
    print("DATABENTO → PHOENIX CONVERTER (saves BOTH 1-min and 5-min)")
    print("=" * 70)

    if not os.path.exists(INPUT):
        print(f"❌ Input file not found: {INPUT}")
        sys.exit(1)

    print(f"\n📂 Input: {INPUT}")
    print(f"📂 Output dir: {OUTPUT_DIR}")
    file_size_mb = os.path.getsize(INPUT) / 1024 / 1024
    print(f"📏 Input size: {file_size_mb:.1f} MB\n")

    # Step 1: Load
    print("[1/6] Loading CSV (this takes 30-60 sec)...")
    df = pd.read_csv(
        INPUT,
        usecols=['ts_event', 'symbol', 'open', 'high', 'low', 'close', 'volume'],
        dtype={'symbol': str},
    )
    print(f"      Loaded {len(df):,} rows")

    # Step 2: Parse + filter
    print("\n[2/6] Parsing timestamps and filtering symbols...")
    df['ts_event'] = pd.to_datetime(df['ts_event'], utc=True)

    initial_count = len(df)
    df = df[df['symbol'].apply(lambda s: bool(SYMBOL_PATTERN.match(s)))].copy()
    print(f"      Filtered to single-contract symbols: {len(df):,} rows "
          f"({initial_count - len(df):,} non-standard symbols removed)")

    df['family'] = df['symbol'].str[:3]
    df['date'] = df['ts_event'].dt.tz_convert('America/Chicago').dt.date

    print("\n      Contracts found per family:")
    for family in ['MNQ', 'MES']:
        contracts = df[df['family'] == family]['symbol'].value_counts().sort_index()
        print(f"        {family}: {len(contracts)} unique contracts")
        for sym, count in contracts.items():
            print(f"          {sym}: {count:,} 1-min bars")

    # Step 3: Front-month detection
    print("\n[3/6] Identifying front-month per day (highest daily volume)...")
    daily_vol = df.groupby(['date', 'family', 'symbol'])['volume'].sum().reset_index()
    idx = daily_vol.groupby(['date', 'family'])['volume'].idxmax()
    front_month = daily_vol.loc[idx, ['date', 'family', 'symbol']].rename(
        columns={'symbol': 'front_symbol'}
    )

    df = df.merge(front_month, on=['date', 'family'])
    df = df[df['symbol'] == df['front_symbol']].copy()
    print(f"      After front-month filter: {len(df):,} rows")

    # Step 4: Rollover schedule
    print("\n[4/6] Detected rollover schedule:")
    for family in ['MNQ', 'MES']:
        fam_fm = front_month[front_month['family'] == family].sort_values('date')
        fam_fm['prev_symbol'] = fam_fm['front_symbol'].shift(1)
        rolls = fam_fm[fam_fm['front_symbol'] != fam_fm['prev_symbol']]
        print(f"      {family} rollovers:")
        for _, row in rolls.iterrows():
            print(f"        {row['date']}: {row['front_symbol']}")

    # Step 5: Save 1-min (raw, front-month filtered)
    print("\n[5/6] Saving 1-min CSVs...")

    def format_1min(d):
        d = d.copy()
        d['ts_ct'] = d['ts_event'].dt.tz_convert('America/Chicago')
        d = d.rename(columns={'ts_event': 'ts_utc'})
        return d[['ts_utc', 'ts_ct', 'symbol', 'open', 'high', 'low', 'close', 'volume']].sort_values('ts_utc')

    mnq_1m = format_1min(df[df['family'] == 'MNQ'])
    mes_1m = format_1min(df[df['family'] == 'MES'])

    mnq_1m_path = os.path.join(OUTPUT_DIR, "mnq_1min_databento.csv")
    mes_1m_path = os.path.join(OUTPUT_DIR, "mes_1min_databento.csv")
    mnq_1m.to_csv(mnq_1m_path, index=False)
    mes_1m.to_csv(mes_1m_path, index=False)
    print(f"      MNQ 1-min: {len(mnq_1m):,} bars → {mnq_1m_path}")
    print(f"      MES 1-min: {len(mes_1m):,} bars → {mes_1m_path}")

    # Step 6: Aggregate to 5-min
    print("\n[6/6] Aggregating to 5-min and saving...")

    def to_5min(d):
        d = d.set_index('ts_event')
        agg = d.resample('5min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
            'symbol': 'first',
        }).dropna(subset=['open'])
        agg['ts_ct'] = agg.index.tz_convert('America/Chicago')
        agg = agg.reset_index().rename(columns={'ts_event': 'ts_utc'})
        return agg[['ts_utc', 'ts_ct', 'symbol', 'open', 'high', 'low', 'close', 'volume']]

    mnq_5m = to_5min(df[df['family'] == 'MNQ'])
    mes_5m = to_5min(df[df['family'] == 'MES'])

    mnq_5m_path = os.path.join(OUTPUT_DIR, "mnq_5min_databento.csv")
    mes_5m_path = os.path.join(OUTPUT_DIR, "mes_5min_databento.csv")
    mnq_5m.to_csv(mnq_5m_path, index=False)
    mes_5m.to_csv(mes_5m_path, index=False)
    print(f"      MNQ 5-min: {len(mnq_5m):,} bars → {mnq_5m_path}")
    print(f"      MES 5-min: {len(mes_5m):,} bars → {mes_5m_path}")

    print(f"\n{'='*70}")
    print(f"✅ DONE!")
    print(f"{'='*70}")
    print(f"\n📊 4 files ready for backtest_v3.py:")
    print(f"   {mnq_1m_path}")
    print(f"   {mes_1m_path}")
    print(f"   {mnq_5m_path}")
    print(f"   {mes_5m_path}")
    print(f"\n📅 Date range: {mnq_5m['ts_ct'].min()} to {mnq_5m['ts_ct'].max()}")


if __name__ == '__main__':
    main()
