"""
Warmup the Noise Area sigma_open_table from historical 1m bars.

Reads mnq_1m_bars.jsonl, computes |close/open - 1| at each minute-of-day
from 9:30 ET for each session, then saves the rolling history table.

Per Zarattini et al. 2024:
    move_open[t] = abs(close[t] / open_of_day - 1)
    sigma_open[minute] = rolling 14-day mean of move_open at that minute
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def build_sigma_open_table(bars_path, output_path, session_open_hour_et=9, session_open_minute_et=30):
    """
    Build sigma_open_table from 1-minute bars.
    
    Returns dict: {minute_of_day: [list of |move_open| values, chronological]}
    where minute_of_day = minutes since 9:30 ET (0, 1, 2, ... 389 for full RTH).
    """
    
    # Group bars by trading date (in ET)
    bars_by_date = defaultdict(list)
    
    with open(bars_path, 'r') as f:
        for line in f:
            bar = json.loads(line)
            dt_et = datetime.strptime(bar['timestamp_et'], "%Y-%m-%d %H:%M:%S")
            
            # Only include RTH session (9:30 ET - 16:00 ET) for sigma_open calc
            if dt_et.hour < session_open_hour_et:
                continue
            if dt_et.hour == session_open_hour_et and dt_et.minute < session_open_minute_et:
                continue
            if dt_et.hour >= 16:  # after 4 PM ET
                continue
            
            bars_by_date[dt_et.date()].append((dt_et, bar))
    
    # For each session, compute move_open at each minute-of-day
    sigma_open_table = defaultdict(list)
    
    sorted_dates = sorted(bars_by_date.keys())
    
    for session_date in sorted_dates:
        session_bars = bars_by_date[session_date]
        if not session_bars:
            continue
        
        # First bar of session = open at 9:30 ET (or whenever first bar is)
        open_price = session_bars[0][1]['open']
        session_open_time = session_bars[0][0]
        
        for dt_et, bar in session_bars:
            # Minute of day relative to 9:30 ET
            minutes_from_open = int((dt_et - session_open_time).total_seconds() / 60)
            
            if minutes_from_open < 0 or minutes_from_open > 390:
                continue  # Outside RTH
            
            # Zarattini formula: |close / open - 1|
            move_open = abs(bar['close'] / open_price - 1)
            sigma_open_table[minutes_from_open].append(move_open)
    
    # Convert defaultdict to regular dict, add metadata
    output = {
        "metadata": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "source_file": str(bars_path),
            "total_sessions": len(sorted_dates),
            "formula": "move_open = abs(close / open_of_day - 1)",
            "reference": "Zarattini, Aziz, Barbon (2024) SSRN 4824172"
        },
        "sigma_open_history": {str(k): v for k, v in sigma_open_table.items()}
    }
    
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    
    # Print sanity check
    print(f"\n📊 sigma_open_table summary:")
    print(f"   Total sessions processed: {len(sorted_dates)}")
    print(f"   First session: {sorted_dates[0]}")
    print(f"   Last session: {sorted_dates[-1]}")
    print(f"   Minutes covered: {len(sigma_open_table)}")
    
    # Show sample values at key times
    print(f"\n   Sample sigma_open values (14-day mean of |move_open|):")
    for minute in [0, 30, 60, 120, 240, 390]:
        if minute in sigma_open_table:
            values = sigma_open_table[minute]
            if len(values) >= 14:
                avg = sum(values[-14:]) / 14
                hour_offset = minute // 60
                min_offset = minute % 60
                et_time = f"{9+hour_offset:02d}:{30+min_offset if min_offset+30 < 60 else min_offset-30:02d}"
                print(f"     minute {minute:3d} (~{et_time} ET): "
                      f"{len(values)} samples, 14-day mean = {avg:.6f} "
                      f"(~{avg*100:.4f}%)")
    
    return output_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", default="data/historical/mnq_1m_bars.jsonl")
    parser.add_argument("--output", default="data/sigma_open_table.json")
    args = parser.parse_args()
    
    print(f"🔬 Building sigma_open_table from {args.bars}...")
    build_sigma_open_table(args.bars, args.output)
    print(f"✅ Saved to {args.output}")