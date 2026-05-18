"""
Phoenix Bot — Phase 12-0 Backtest: ES/NQ Confluence Validation

Runs the confluence boost logic against historical NQ + ES (or MNQ + MES) bars
to validate whether the design's signals fire at reasonable frequencies and
whether the z-score / SMT / correlation values are in expected ranges.

This is the PRELIMINARY backtest. If signals look healthy here, we proceed to
Phase 12A (live data plumbing). If not, we tune the design before building
live infra.

USAGE (from C:\\Trading Project\\phoenix_bot\\):
    python tools/backtest_es_nq_confluence.py

OUTPUTS:
    - Console summary stats
    - data/historical/backtest_results.csv (per-bar signals)
    - data/historical/backtest_summary.txt (aggregate stats)
"""

from __future__ import annotations

import csv
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────────────────────────
# Config — adjust if your file paths differ
# ──────────────────────────────────────────────────────────────────

# Find the project root regardless of where this is run from
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
HISTORICAL_DIR = PROJECT_ROOT / "data" / "historical"

# NT8 export filenames (adjust if yours differ)
ES_FILE = HISTORICAL_DIR / "MES 06-26.Last.txt"
NQ_FILE = HISTORICAL_DIR / "MNQ 06-26.Last.txt"

# Fallback: try without .txt extension (NT8 sometimes saves without)
if not ES_FILE.exists():
    ES_FILE = HISTORICAL_DIR / "MES 06-26.Last"
if not NQ_FILE.exists():
    NQ_FILE = HISTORICAL_DIR / "MNQ 06-26.Last"

OUTPUT_CSV = HISTORICAL_DIR / "backtest_results.csv"
OUTPUT_SUMMARY = HISTORICAL_DIR / "backtest_summary.txt"

# Confluence parameters (mirror the design doc)
BETA_WINDOW = 60          # 5-min bars used for OLS beta + z-score
SMT_LOOKBACK = 10         # bars to look back for swing high/low
Z_EXTREME = 2.0           # |z| > this = extreme dislocation
Z_VERY_EXTREME = 2.5      # |z| > this triggers skip filter

# ──────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

# ──────────────────────────────────────────────────────────────────
# NT8 .Last file parser
# ──────────────────────────────────────────────────────────────────
# NT8 export format for minute bars:
#   yyyyMMdd HHmmss;open;high;low;close;volume
# Or for tick data (.Last):
#   yyyyMMddHHmmss;price;volume
# We handle both.

def parse_nt8_export(filepath: Path) -> list[Bar]:
    """Parse an NT8 export file. Auto-detects tick vs minute format."""
    bars = []
    if not filepath.exists():
        print(f"  ⚠ File not found: {filepath}")
        return bars

    with filepath.open("r", encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            parts = line.split(";")
            try:
                if len(parts) == 3:
                    # Tick format: yyyyMMddHHmmss;price;volume
                    ts_str = parts[0]
                    if len(ts_str) == 14:
                        ts = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
                    elif " " in ts_str:
                        ts = datetime.strptime(ts_str, "%Y%m%d %H%M%S")
                    else:
                        continue
                    price = float(parts[1])
                    volume = float(parts[2])
                    # Treat each tick as a "bar" with O=H=L=C=price
                    bars.append(Bar(ts, price, price, price, price, volume))
                elif len(parts) == 6:
                    # Minute format: yyyyMMdd HHmmss;O;H;L;C;V
                    ts_str = parts[0]
                    if " " in ts_str:
                        ts = datetime.strptime(ts_str, "%Y%m%d %H%M%S")
                    elif len(ts_str) == 14:
                        ts = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
                    else:
                        continue
                    bars.append(Bar(
                        ts=ts,
                        open=float(parts[1]),
                        high=float(parts[2]),
                        low=float(parts[3]),
                        close=float(parts[4]),
                        volume=float(parts[5]),
                    ))
            except (ValueError, IndexError) as e:
                if line_num <= 5:
                    print(f"  ⚠ Failed to parse line {line_num}: {line[:80]} → {e}")
                continue

    return bars

def aggregate_to_5m(bars: list[Bar]) -> list[Bar]:
    """Aggregate 1-min or tick bars into 5-min bars (clock-aligned)."""
    if not bars:
        return []
    buckets: dict[datetime, list[Bar]] = defaultdict(list)
    for b in bars:
        # Round down to nearest 5-min boundary
        bucket_ts = b.ts.replace(minute=(b.ts.minute // 5) * 5, second=0, microsecond=0)
        buckets[bucket_ts].append(b)

    five_m = []
    for ts in sorted(buckets):
        chunk = buckets[ts]
        five_m.append(Bar(
            ts=ts,
            open=chunk[0].open,
            high=max(b.high for b in chunk),
            low=min(b.low for b in chunk),
            close=chunk[-1].close,
            volume=sum(b.volume for b in chunk),
        ))
    return five_m

# ──────────────────────────────────────────────────────────────────
# Confluence logic (from design doc, condensed)
# ──────────────────────────────────────────────────────────────────

def detect_smt(nq_bars: list[Bar], es_bars: list[Bar], lookback: int = SMT_LOOKBACK) -> dict:
    """Detect structural SMT divergence at the latest bar."""
    if len(nq_bars) < lookback + 1 or len(es_bars) < lookback + 1:
        return {"smt_bullish": False, "smt_bearish": False}

    nq_recent_low = min(b.low for b in nq_bars[-lookback - 1:-1])
    es_recent_low = min(b.low for b in es_bars[-lookback - 1:-1])
    nq_new_low = nq_bars[-1].low < nq_recent_low
    es_new_low = es_bars[-1].low < es_recent_low

    nq_recent_high = max(b.high for b in nq_bars[-lookback - 1:-1])
    es_recent_high = max(b.high for b in es_bars[-lookback - 1:-1])
    nq_new_high = nq_bars[-1].high > nq_recent_high
    es_new_high = es_bars[-1].high > es_recent_high

    return {
        "smt_bullish": nq_new_low and not es_new_low,
        "smt_bearish": nq_new_high and not es_new_high,
    }

def compute_zscore(nq_bars: list[Bar], es_bars: list[Bar], window: int = BETA_WINDOW) -> Optional[dict]:
    """Compute beta-adjusted spread z-score + correlation."""
    if len(nq_bars) < window + 1 or len(es_bars) < window + 1:
        return None

    # Log returns
    nq_closes = [b.close for b in nq_bars[-window - 1:]]
    es_closes = [b.close for b in es_bars[-window - 1:]]
    if any(c <= 0 for c in nq_closes) or any(c <= 0 for c in es_closes):
        return None

    r_nq = [math.log(nq_closes[i + 1] / nq_closes[i]) for i in range(window)]
    r_es = [math.log(es_closes[i + 1] / es_closes[i]) for i in range(window)]

    # OLS beta via covariance/variance
    mean_nq = sum(r_nq) / window
    mean_es = sum(r_es) / window
    cov = sum((a - mean_nq) * (b - mean_es) for a, b in zip(r_nq, r_es)) / window
    var_es = sum((b - mean_es) ** 2 for b in r_es) / window
    if var_es <= 0:
        return None
    beta = cov / var_es

    # Spread series
    spread = [a - beta * b for a, b in zip(r_nq, r_es)]
    mean_s = sum(spread) / len(spread)
    var_s = sum((s - mean_s) ** 2 for s in spread) / len(spread)
    std_s = math.sqrt(var_s) if var_s > 0 else 1e-9
    spread_z = (spread[-1] - mean_s) / std_s

    # Pearson correlation
    var_nq = sum((a - mean_nq) ** 2 for a in r_nq) / window
    if var_nq <= 0:
        correlation = 0.0
    else:
        correlation = cov / (math.sqrt(var_nq) * math.sqrt(var_es))

    return {
        "spread_z": spread_z,
        "beta": beta,
        "correlation": correlation,
    }

def regime_weight(correlation: float) -> float:
    """Multiplier in [0, 1.0] applied to boost values."""
    if correlation > 0.90: return 1.0
    if correlation > 0.80: return 0.8
    if correlation > 0.70: return 0.5
    return 0.0

def z_to_boost(z: float, direction: str) -> int:
    """Convert z-score to IQS boost for a given direction (per design doc table)."""
    if direction == "long":
        if z < -2.0: return 8       # NQ underperforming, long the cheap one
        if z < -1.0: return 4
        if z <= 1.0: return 0
        if z <= 2.0: return 0
        return -3                    # NQ overheated, fade long bias
    else:  # short
        if z > 2.0: return 8         # NQ overheated, short
        if z > 1.0: return 4
        if z >= -1.0: return 0
        if z >= -2.0: return 0
        return -3                    # NQ oversold, fade short bias

def smt_to_boost(smt: dict, direction: str) -> int:
    """SMT structural boost (categorical)."""
    if direction == "long" and smt["smt_bullish"]:
        return 8
    if direction == "short" and smt["smt_bearish"]:
        return 8
    # SMT against direction is mild penalty
    if direction == "long" and smt["smt_bearish"]:
        return -5
    if direction == "short" and smt["smt_bullish"]:
        return -5
    return 0

def should_skip(direction: str, smt: dict, z: float, correlation: float) -> bool:
    """Skip filter for extreme adverse confluence."""
    if correlation < 0.70:
        return False
    if direction == "long" and z > Z_VERY_EXTREME and smt["smt_bearish"]:
        return True
    if direction == "short" and z < -Z_VERY_EXTREME and smt["smt_bullish"]:
        return True
    return False

def compute_confluence(nq_bars: list[Bar], es_bars: list[Bar]) -> dict:
    """Top-level: combine SMT + z-score + regime gate."""
    smt = detect_smt(nq_bars, es_bars)
    z_data = compute_zscore(nq_bars, es_bars)

    if z_data is None:
        return {
            "warmup": True,
            "smt": smt,
            "boost_long": 0,
            "boost_short": 0,
            "skip_long": False,
            "skip_short": False,
        }

    z = z_data["spread_z"]
    corr = z_data["correlation"]
    weight = regime_weight(corr)

    # Combine SMT + Z boosts, multiply by regime weight, cap at ±10
    raw_long = smt_to_boost(smt, "long") + z_to_boost(z, "long")
    raw_short = smt_to_boost(smt, "short") + z_to_boost(z, "short")
    boost_long = max(-10, min(10, int(round(raw_long * weight))))
    boost_short = max(-10, min(10, int(round(raw_short * weight))))

    return {
        "warmup": False,
        "smt": smt,
        "spread_z": z,
        "beta": z_data["beta"],
        "correlation": corr,
        "regime_weight": weight,
        "boost_long": boost_long,
        "boost_short": boost_short,
        "skip_long": should_skip("long", smt, z, corr),
        "skip_short": should_skip("short", smt, z, corr),
    }

# ──────────────────────────────────────────────────────────────────
# Bar alignment
# ──────────────────────────────────────────────────────────────────

def align_bars(nq_bars: list[Bar], es_bars: list[Bar]) -> list[tuple[Bar, Bar]]:
    """Match NQ and ES bars by timestamp. Drop unpaired bars."""
    es_by_ts = {b.ts: b for b in es_bars}
    pairs = []
    for nb in nq_bars:
        eb = es_by_ts.get(nb.ts)
        if eb is not None:
            pairs.append((nb, eb))
    return pairs

# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("Phoenix Phase 12-0 — ES/NQ Confluence Backtest")
    print("=" * 70)
    print(f"Looking for files in: {HISTORICAL_DIR}")
    print()

    # 1. Parse
    print(f"Parsing {ES_FILE.name}...")
    es_raw = parse_nt8_export(ES_FILE)
    print(f"  → {len(es_raw):,} raw bars/ticks")

    print(f"Parsing {NQ_FILE.name}...")
    nq_raw = parse_nt8_export(NQ_FILE)
    print(f"  → {len(nq_raw):,} raw bars/ticks")

    if not es_raw or not nq_raw:
        print("\n❌ ERROR: Could not parse files. Check file paths and format.")
        print(f"   Expected files at: {HISTORICAL_DIR}")
        print(f"   Got: ES_FILE={ES_FILE.exists()}, NQ_FILE={NQ_FILE.exists()}")
        sys.exit(1)

    # 2. Aggregate
    print("\nAggregating to 5-min bars...")
    es_5m = aggregate_to_5m(es_raw)
    nq_5m = aggregate_to_5m(nq_raw)
    print(f"  ES 5m bars: {len(es_5m):,}")
    print(f"  NQ 5m bars: {len(nq_5m):,}")

    # 3. Align
    print("\nAligning bars by timestamp...")
    pairs = align_bars(nq_5m, es_5m)
    print(f"  Paired bars: {len(pairs):,}")
    if pairs:
        print(f"  Date range: {pairs[0][0].ts} to {pairs[-1][0].ts}")

    if len(pairs) < BETA_WINDOW + 10:
        print(f"\n❌ Not enough paired bars (need ≥{BETA_WINDOW + 10}, got {len(pairs)})")
        sys.exit(1)

    # 4. Compute confluence for each bar
    print("\nComputing confluence signals...")
    results = []
    nq_window = []
    es_window = []
    for nq_bar, es_bar in pairs:
        nq_window.append(nq_bar)
        es_window.append(es_bar)
        if len(nq_window) > BETA_WINDOW + 5:
            nq_window.pop(0)
            es_window.pop(0)
        conf = compute_confluence(nq_window, es_window)
        results.append({
            "ts": nq_bar.ts.isoformat(),
            "nq_close": nq_bar.close,
            "es_close": es_bar.close,
            **conf,
            "smt_bullish": conf["smt"]["smt_bullish"],
            "smt_bearish": conf["smt"]["smt_bearish"],
        })

    # 5. Aggregate stats
    non_warmup = [r for r in results if not r.get("warmup")]
    print(f"  Non-warmup bars: {len(non_warmup):,}")

    if not non_warmup:
        print("\n❌ All bars are in warmup. Need more historical data.")
        sys.exit(1)

    smt_bull_count = sum(1 for r in non_warmup if r["smt_bullish"])
    smt_bear_count = sum(1 for r in non_warmup if r["smt_bearish"])
    extreme_long = sum(1 for r in non_warmup if r.get("spread_z", 0) < -Z_EXTREME)
    extreme_short = sum(1 for r in non_warmup if r.get("spread_z", 0) > Z_EXTREME)
    skip_long_count = sum(1 for r in non_warmup if r["skip_long"])
    skip_short_count = sum(1 for r in non_warmup if r["skip_short"])
    boost_long_nonzero = sum(1 for r in non_warmup if r["boost_long"] != 0)
    boost_short_nonzero = sum(1 for r in non_warmup if r["boost_short"] != 0)

    correlations = [r["correlation"] for r in non_warmup]
    mean_corr = sum(correlations) / len(correlations)
    min_corr = min(correlations)
    max_corr = max(correlations)
    # Approx percentile
    sorted_corr = sorted(correlations)
    median_corr = sorted_corr[len(sorted_corr) // 2]
    pct_high_corr = 100 * sum(1 for c in correlations if c > 0.90) / len(correlations)
    pct_decorrelated = 100 * sum(1 for c in correlations if c < 0.70) / len(correlations)

    zscores = [r["spread_z"] for r in non_warmup if "spread_z" in r]
    mean_z = sum(zscores) / len(zscores)
    abs_zscores = sorted(abs(z) for z in zscores)
    median_abs_z = abs_zscores[len(abs_zscores) // 2]
    p95_abs_z = abs_zscores[int(0.95 * len(abs_zscores))]

    # 6. Build summary
    n = len(non_warmup)
    summary = f"""
╔════════════════════════════════════════════════════════════════════╗
║  ES/NQ Confluence Backtest Summary                                ║
╠════════════════════════════════════════════════════════════════════╣
║  Date range:           {pairs[0][0].ts.date()} → {pairs[-1][0].ts.date()}             ║
║  Total paired 5m bars: {len(pairs):,}                                         ║
║  Non-warmup bars:      {n:,}                                          ║
║                                                                    ║
║  CORRELATION REGIME (the gate):                                    ║
║    Mean correlation:    {mean_corr:.3f}                                    ║
║    Median correlation:  {median_corr:.3f}                                    ║
║    Range:               [{min_corr:.3f}, {max_corr:.3f}]                       ║
║    % bars corr > 0.90:  {pct_high_corr:.1f}%  (full boost weight)            ║
║    % bars corr < 0.70:  {pct_decorrelated:.1f}%  (boost gated to zero)        ║
║                                                                    ║
║  Z-SCORE DISTRIBUTION:                                             ║
║    Mean z-score:        {mean_z:+.3f}  (should be near 0)                ║
║    Median |z|:          {median_abs_z:.3f}                                     ║
║    95th pct |z|:        {p95_abs_z:.3f}                                     ║
║    Extreme bars (|z|>{Z_EXTREME}): {extreme_long + extreme_short:,} ({100*(extreme_long+extreme_short)/n:.1f}%)                       ║
║                                                                    ║
║  SMT STRUCTURAL SIGNALS:                                           ║
║    Bullish SMT prints:  {smt_bull_count:,} ({100*smt_bull_count/n:.2f}%)                       ║
║    Bearish SMT prints:  {smt_bear_count:,} ({100*smt_bear_count/n:.2f}%)                       ║
║                                                                    ║
║  BOOSTS APPLIED:                                                   ║
║    LONG boost ≠ 0:      {boost_long_nonzero:,} ({100*boost_long_nonzero/n:.1f}%) of bars              ║
║    SHORT boost ≠ 0:     {boost_short_nonzero:,} ({100*boost_short_nonzero/n:.1f}%) of bars              ║
║                                                                    ║
║  SKIP FILTER ACTIVATIONS:                                          ║
║    LONG skipped:        {skip_long_count:,} ({100*skip_long_count/n:.2f}%)                       ║
║    SHORT skipped:       {skip_short_count:,} ({100*skip_short_count/n:.2f}%)                       ║
╚════════════════════════════════════════════════════════════════════╝

HEALTH CHECK INTERPRETATION:

  ✓ Healthy if:
    - Mean correlation > 0.80 (typically ~0.93 for ES/NQ on 5m RTH)
    - Mean z-score near 0 (the math is unbiased)
    - 95th pct |z| in range [1.6, 2.5]  (normal distribution-ish tail)
    - SMT prints fire 2-8% of bars (rare but present)
    - Boost activations 15-40% of bars (frequent but not constant)
    - Skip filter rare (< 2%)

  ⚠ Concerning if:
    - Mean correlation < 0.70 → ES/NQ not normally synced this period
    - 95th |z| > 4 → spread is too volatile, parameters need tuning
    - SMT prints > 15% → lookback too short
    - Skip filter > 5% → thresholds too aggressive

  → If healthy: proceed to Phase 12A (live infrastructure)
  → If concerning: tune parameters and re-run
"""

    print(summary)

    # 7. Save outputs
    print(f"Writing per-bar results to {OUTPUT_CSV}...")
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ts", "nq_close", "es_close",
            "smt_bullish", "smt_bearish",
            "spread_z", "beta", "correlation",
            "regime_weight", "boost_long", "boost_short",
            "skip_long", "skip_short",
        ])
        for r in non_warmup:
            writer.writerow([
                r["ts"], r["nq_close"], r["es_close"],
                r["smt_bullish"], r["smt_bearish"],
                f"{r.get('spread_z', 0):.4f}",
                f"{r.get('beta', 0):.4f}",
                f"{r.get('correlation', 0):.4f}",
                f"{r.get('regime_weight', 0):.2f}",
                r["boost_long"], r["boost_short"],
                r["skip_long"], r["skip_short"],
            ])

    print(f"Writing summary to {OUTPUT_SUMMARY}...")
    OUTPUT_SUMMARY.write_text(summary, encoding="utf-8")

    print("\n✓ Done. Open the CSV in Excel or feed it to a follow-up analysis.")
    print(f"  CSV: {OUTPUT_CSV}")
    print(f"  Summary: {OUTPUT_SUMMARY}")


if __name__ == "__main__":
    main()
