"""
NT8 historical data import for Phoenix Bot.

Reads NT8 minute bar export format and writes Phoenix Bot's internal
historical bar format, which feeds into sigma_open_table warmup.

Usage:
    python tools/nt8_import.py \\
        --input "data/historical/MNQ 06-26.Last.txt" \\
        --output "data/historical/mnq_1m_bars.jsonl" \\
        --source-tz "America/Chicago" \\
        --target-tz "America/New_York"
"""

import argparse
import json
import pytz
from datetime import datetime
from pathlib import Path


def parse_nt8_minute_line(line: str):
    """
    Parse one line of NT8 minute export.
    
    Format: YYYYMMDD HHMMSS;OPEN;HIGH;LOW;CLOSE;VOLUME
    Example: 20260320 083000;17234.50;17238.25;17232.75;17236.00;142
    
    Returns dict with keys: timestamp, open, high, low, close, volume
    Returns None if line is malformed.
    """
    try:
        # Split timestamp half from price half
        ts_part, prices_part = line.strip().split(';', 1)
        
        # Parse timestamp (naive — we'll tz-localize later)
        ts = datetime.strptime(ts_part, "%Y%m%d %H%M%S")
        
        # Parse OHLCV
        fields = prices_part.split(';')
        if len(fields) != 5:
            return None
        
        return {
            "timestamp": ts,
            "open": float(fields[0]),
            "high": float(fields[1]),
            "low": float(fields[2]),
            "close": float(fields[3]),
            "volume": int(fields[4])
        }
    except (ValueError, IndexError) as e:
        return None


def process_file(input_path, output_path, source_tz_name, target_tz_name):
    """Convert NT8 export to Phoenix Bot JSONL format, with tz conversion."""
    
    source_tz = pytz.timezone(source_tz_name)
    target_tz = pytz.timezone(target_tz_name)
    
    bars_written = 0
    lines_skipped = 0
    
    with open(input_path, 'r', encoding='utf-8') as infile, \
         open(output_path, 'w', encoding='utf-8') as outfile:
        
        for line_num, line in enumerate(infile, 1):
            bar = parse_nt8_minute_line(line)
            if bar is None:
                lines_skipped += 1
                continue
            
            # Localize naive timestamp to source timezone, convert to target
            ts_local = source_tz.localize(bar['timestamp'])
            ts_et = ts_local.astimezone(target_tz)
            
            # Build Phoenix Bot bar record
            record = {
                "timestamp_et": ts_et.strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_epoch_ms": int(ts_et.timestamp() * 1000),
                "symbol": "MNQ",
                "timeframe": "1m",
                "open": bar['open'],
                "high": bar['high'],
                "low": bar['low'],
                "close": bar['close'],
                "volume": bar['volume'],
                "source": "nt8_historical_export"
            }
            
            outfile.write(json.dumps(record) + "\n")
            bars_written += 1
    
    return bars_written, lines_skipped


def validate_coverage(output_path):
    """Quick sanity check — verify we got reasonable session coverage."""
    from collections import Counter
    
    dates_seen = Counter()
    session_open_count = 0
    session_close_count = 0
    
    with open(output_path, 'r') as f:
        for line in f:
            record = json.loads(line)
            dt = datetime.strptime(record['timestamp_et'], "%Y-%m-%d %H:%M:%S")
            dates_seen[dt.date()] += 1
            
            if dt.hour == 9 and dt.minute == 30:
                session_open_count += 1
            if dt.hour == 16 and dt.minute == 0:
                session_close_count += 1
    
    unique_days = len(dates_seen)
    trading_days = sum(1 for d, count in dates_seen.items() if count > 100)
    
    print(f"\n📊 Coverage validation:")
    print(f"   Unique calendar dates: {unique_days}")
    print(f"   Trading days (>100 bars each): {trading_days}")
    print(f"   Days with 9:30 ET bar: {session_open_count}")
    print(f"   Days with 16:00 ET bar: {session_close_count}")
    print(f"   Average bars per trading day: {sum(dates_seen.values()) // max(trading_days, 1)}")
    
    if trading_days < 20:
        print(f"   ⚠️  WARNING: Only {trading_days} trading days found. You need 14+ for Noise Area warmup.")
    else:
        print(f"   ✅ Sufficient data for Noise Area sigma_open_table warmup.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to NT8 .txt export")
    parser.add_argument("--output", required=True, help="Path to write JSONL output")
    parser.add_argument("--source-tz", default="America/Chicago",
                       help="Timezone of NT8 export (default: America/Chicago)")
    parser.add_argument("--target-tz", default="America/New_York",
                       help="Timezone for Phoenix Bot (default: America/New_York)")
    parser.add_argument("--validate", action="store_true", help="Run coverage validation after import")
    
    args = parser.parse_args()
    
    print(f"📥 Reading: {args.input}")
    print(f"📤 Writing: {args.output}")
    print(f"🕐 Timezone: {args.source_tz} → {args.target_tz}")
    
    bars, skipped = process_file(args.input, args.output, args.source_tz, args.target_tz)
    
    print(f"\n✅ Conversion complete:")
    print(f"   Bars written: {bars:,}")
    print(f"   Lines skipped: {skipped}")
    
    if args.validate:
        validate_coverage(args.output)