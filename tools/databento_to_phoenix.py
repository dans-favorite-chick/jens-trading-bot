"""
Databento → Phoenix converter.

Input:  glbx-mdp3-*.ohlcv-1m.csv (multi-contract Databento dump)
Output: mnq_5min_databento.csv + mes_5min_databento.csv (clean front-month, 5-min bars)

Logic:
  1. Filter to single-contract symbols (MNQ/MES + month code + year digit). Drop spreads.
  2. Per day per family, identify front-month by daily volume.
  3. Auto-detect rollover dates and report them.
  4. Aggregate 1-min → 5-min bars.
  5. Save with UTC + CT timestamps for downstream convenience.
"""
import os
import re
import sys
import pandas as pd

INPUT = r"C:\Trading Project\phoenix_bot\data\historical\glbx-mdp3-20210517-20260517.ohlcv-1m.csv"
OUTPUT_DIR = r"C:\Trading Project\phoenix_bot\data\historical"

# Symbol pattern: MNQ or MES + month code (H/M/U/Z) + 1 or 2 digit year
SYMBOL_PATTERN = re.compile(r'^(MNQ|MES)[HMUZ]\d{1,2}$')


def main():
    print("=" * 70)
    print("DATABENTO → PHOENIX 5-MIN CONVERTER")
    print("=" * 70)

    if not os.path.exists(INPUT):
        print(f"❌ Input file not found: {INPUT}")
        sys.exit(1)

    print(f"\n📂 Input: {INPUT}")
    print(f"📂 Output dir: {OUTPUT_DIR}")
    file_size_mb = os.path.getsize(INPUT) / 1024 / 1024
    print(f"📏 Input size: {file_size_mb:.1f} MB\n")

    # Step 1: Load
    print("[1/5] Loading CSV (this takes 30-60 sec)...")
    df = pd.read_csv(
        INPUT,
        usecols=['ts_event', 'symbol', 'open', 'high', 'low', 'close', 'volume'],
        dtype={'symbol': str},
    )
    print(f"      Loaded {len(df):,} rows")

    # Step 2: Parse timestamps + filter symbols
    print("\n[2/5] Parsing timestamps and filtering symbols...")
    df['ts_event'] = pd.to_datetime(df['ts_event'], utc=True)

    initial_count = len(df)
    df = df[df['symbol'].apply(lambda s: bool(SYMBOL_PATTERN.match(s)))].copy()
    print(f"      Filtered to single-contract symbols: {len(df):,} rows "
          f"({initial_count - len(df):,} non-standard symbols removed)")

    df['family'] = df['symbol'].str[:3]
    df['date'] = df['ts_event'].dt.tz_convert('America/Chicago').dt.date

    # Show contracts found
    print("\n      Contracts found per family:")
    for family in ['MNQ', 'MES']:
        contracts = df[df['family'] == family]['symbol'].value_counts().sort_index()
        print(f"        {family}: {len(contracts)} unique contracts")
        for sym, count in contracts.items():
            print(f"          {sym}: {count:,} 1-min bars")

    # Step 3: Identify front-month per day
    print("\n[3/5] Identifying front-month contracts (highest daily volume)...")
    daily_vol = df.groupby(['date', 'family', 'symbol'])['volume'].sum().reset_index()
    idx = daily_vol.groupby(['date', 'family'])['volume'].idxmax()
    front_month = daily_vol.loc[idx, ['date', 'family', 'symbol']].rename(
        columns={'symbol': 'front_symbol'}
    )

    df = df.merge(front_month, on=['date', 'family'])
    df = df[df['symbol'] == df['front_symbol']].copy()
    print(f"      After front-month filter: {len(df):,} rows")

    # Step 4: Show rollover schedule
    print("\n[4/5] Detected rollover schedule:")
    for family in ['MNQ', 'MES']:
        fam_fm = front_month[front_month['family'] == family].sort_values('date')
        # Find rollover points (where front_symbol changes)
        fam_fm['prev_symbol'] = fam_fm['front_symbol'].shift(1)
        rolls = fam_fm[fam_fm['front_symbol'] != fam_fm['prev_symbol']]
        print(f"      {family} rollovers:")
        for _, row in rolls.iterrows():
            print(f"        {row['date']}: {row['front_symbol']}")

    # Step 5: Aggregate to 5-min bars
    print("\n[5/5] Aggregating to 5-min bars and saving...")

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

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    mnq_path = os.path.join(OUTPUT_DIR, "mnq_5min_databento.csv")
    mes_path = os.path.join(OUTPUT_DIR, "mes_5min_databento.csv")
    mnq_5m.to_csv(mnq_path, index=False)
    mes_5m.to_csv(mes_path, index=False)

    print(f"\n{'='*70}")
    print(f"✅ DONE!")
    print(f"{'='*70}")
    print(f"   MNQ: {len(mnq_5m):,} 5-min bars")
    print(f"        → {mnq_path}")
    print(f"        Date range: {mnq_5m['ts_ct'].min()} to {mnq_5m['ts_ct'].max()}")
    print(f"\n   MES: {len(mes_5m):,} 5-min bars")
    print(f"        → {mes_path}")
    print(f"        Date range: {mes_5m['ts_ct'].min()} to {mes_5m['ts_ct'].max()}")
    print(f"\n📊 Ready for backtest_v3.py!")


if __name__ == '__main__':
    main()
