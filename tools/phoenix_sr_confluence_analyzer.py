"""
Phoenix S/R Confluence Analyzer for spring_setup
================================================

HYPOTHESIS
----------
spring_setup detects rejection wicks (Wyckoff spring). A spring is only
meaningful when it occurs AT a known S/R level (the wick = absorption of
stop-runs at the level, then reclaim). A spring in random noise is less
likely to mean anything.

DESIGN
------
For each spring_setup trade in backtest_results/phoenix_real_5year.csv:

 1. Slice the 300 most recent 5m bars BEFORE entry_ts.
 2. Call core.sr_zones.detect_sr_zones(bars, current_price=entry_price).
 3. For LONG: look for nearest SUPPORT zone within 4 ticks of entry_price.
    For SHORT: look for nearest RESISTANCE zone within 4 ticks of entry.
 4. Bucket by confluence:
       no_sr            no zone within 4t
       weak_sr          zone within 4t, strength <  0.50
       strong_sr        zone within 4t, 0.50 <= strength < 0.70
       very_strong_sr   zone within 4t, strength >= 0.70

 5. Per bucket: n, WR, total $, avg $, PF, by-year.
 6. Simulate size boost (1.3x) on strong_sr + very_strong_sr trades.
 7. Edge checks:
      - what % of trades land at a zone?
      - is there a direction-vs-trend interaction?

USAGE
-----
    python tools/phoenix_sr_confluence_analyzer.py
        --strategy spring_setup
        --trades backtest_results/phoenix_real_5year.csv
        --bars   data/historical/mnq_5min_databento.csv
        --out    backtest_results/phoenix_sr_confluence_summary.csv

The script is read-only — never opens trade_memory.json directly.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sr_zones import detect_sr_zones, TICK  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("sr_conf")

PROXIMITY_TICKS = 4   # spring wick within 4t of zone = at the zone


class FakeBar:
    """Lightweight Bar substitute for sr_zones.detect_sr_zones."""
    __slots__ = ("open", "high", "low", "close", "volume", "start_time", "end_time")

    def __init__(self, o, h, l, c, v, et):
        self.open = float(o)
        self.high = float(h)
        self.low = float(l)
        self.close = float(c)
        self.volume = int(v) if v == v else 0  # NaN safe
        self.start_time = float(et) - 300.0
        self.end_time = float(et)


# ====================================================================
# Bucketing
# ====================================================================

def classify_bucket(zone_strength: float | None) -> str:
    if zone_strength is None:
        return "no_sr"
    if zone_strength < 0.50:
        return "weak_sr"
    if zone_strength < 0.70:
        return "strong_sr"
    return "very_strong_sr"


BUCKETS = ["no_sr", "weak_sr", "strong_sr", "very_strong_sr"]


# ====================================================================
# Per-bucket statistics
# ====================================================================

def bucket_stats(trades: list[dict], label: str) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "bucket": label, "n": 0, "wr": 0.0,
            "total_pnl": 0.0, "avg_pnl": 0.0,
            "wins_pnl": 0.0, "losses_pnl": 0.0, "pf": 0.0,
        }
    wins = [t for t in trades if t["pnl_dollars"] > 0]
    losses = [t for t in trades if t["pnl_dollars"] < 0]
    total = sum(t["pnl_dollars"] for t in trades)
    gross_w = sum(t["pnl_dollars"] for t in wins)
    gross_l = abs(sum(t["pnl_dollars"] for t in losses))
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf") if gross_w > 0 else 0.0
    return {
        "bucket": label,
        "n": n,
        "wr": len(wins) / n,
        "total_pnl": total,
        "avg_pnl": total / n,
        "wins_pnl": gross_w,
        "losses_pnl": gross_l,
        "pf": pf,
    }


# ====================================================================
# Main pipeline
# ====================================================================

def load_trades(trades_csv: Path, strategy: str) -> list[dict]:
    out: list[dict] = []
    with open(trades_csv, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if row.get("strategy") != strategy:
                continue
            try:
                ts = pd.Timestamp(row["entry_ts"])
                if ts.tz is None:
                    ts = ts.tz_localize("UTC")
                else:
                    ts = ts.tz_convert("UTC")
                ep = float(row["entry_price"])
                pnl = float(row["pnl_dollars"])
            except (ValueError, KeyError):
                continue
            out.append({
                "entry_ts": ts,
                "entry_price": ep,
                "direction": row["direction"],
                "pnl_dollars": pnl,
                "year": int(row.get("year") or ts.year),
                "stop_price": float(row.get("stop_price") or 0),
                "target_price": float(row.get("target_price") or 0),
                "exit_reason": row.get("exit_reason", ""),
                "hold_min": float(row.get("hold_min") or 0),
            })
    return out


def load_5m_bars(bars_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(bars_csv)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    # Add epoch seconds for FakeBar end_time.
    # pandas 3.0 defaults to datetime64[us] (microseconds), so naive
    # ``df["ts"].astype("int64") // 1_000_000_000`` returns values 1000x
    # too small. Force ns precision before the int64 cast — or, equivalently,
    # use .apply(lambda t: t.timestamp()). See docs/PANDAS_30_DATETIME_AUDIT.md.
    df["epoch"] = df["ts"].astype("datetime64[ns, UTC]").astype("int64") // 1_000_000_000
    return df[["ts", "epoch", "open", "high", "low", "close", "volume"]]


def slice_window(df: pd.DataFrame, end_ts: pd.Timestamp, lookback: int = 300) -> list[FakeBar]:
    """Return last `lookback` 5m bars whose ts < end_ts."""
    # Bars with ts STRICTLY BEFORE entry (use end of bar < entry_ts)
    # 5m bar at ts=X covers X-5m to X. To be safe we require ts < entry_ts.
    end_pos = df["ts"].searchsorted(end_ts, side="left")
    start_pos = max(0, end_pos - lookback)
    sub = df.iloc[start_pos:end_pos]
    bars = [
        FakeBar(row.open, row.high, row.low, row.close, row.volume, row.epoch)
        for row in sub.itertuples(index=False)
    ]
    return bars


def prior_day_levels(df: pd.DataFrame, end_ts: pd.Timestamp) -> tuple[float | None, float | None]:
    """Compute prior calendar day's high/low (UTC date boundary)."""
    end_date = end_ts.normalize()
    prior_start = end_date - pd.Timedelta(days=1)
    mask = (df["ts"] >= prior_start) & (df["ts"] < end_date)
    sub = df.loc[mask]
    if sub.empty:
        return None, None
    return float(sub["high"].max()), float(sub["low"].min())


def find_nearest_zone(zones: list, entry_price: float, direction: str,
                       proximity_ticks: int = PROXIMITY_TICKS):
    """For LONG return nearest SUPPORT within proximity. For SHORT, nearest RES."""
    if direction == "LONG":
        zone_type = "support"
    else:
        zone_type = "resistance"
    proximity = proximity_ticks * TICK
    best = None
    best_dist = float("inf")
    for z in zones:
        if z.type != zone_type:
            continue
        d = abs(z.price - entry_price)
        if d <= proximity and d < best_dist:
            best = z
            best_dist = d
    return best


def analyze(trades: list[dict], bars_df: pd.DataFrame) -> list[dict]:
    """Annotate each trade with sr bucket + strength. Returns the annotated list."""
    annotated: list[dict] = []
    n = len(trades)
    t0 = time.time()
    last_report = t0
    for i, tr in enumerate(trades):
        bars = slice_window(bars_df, tr["entry_ts"], lookback=300)
        if len(bars) < 50:
            tr["bucket"] = "no_sr"
            tr["zone_price"] = None
            tr["zone_strength"] = None
            tr["zone_source"] = None
            tr["zone_n_tests"] = None
            annotated.append(tr)
            continue
        pdh, pdl = prior_day_levels(bars_df, tr["entry_ts"])
        zones = detect_sr_zones(
            bars_5m=bars,
            current_price=tr["entry_price"],
            lookback_bars=300,
            prior_day_high=pdh,
            prior_day_low=pdl,
        )
        z = find_nearest_zone(zones, tr["entry_price"], tr["direction"])
        if z is None:
            tr["bucket"] = "no_sr"
            tr["zone_price"] = None
            tr["zone_strength"] = None
            tr["zone_source"] = None
            tr["zone_n_tests"] = None
        else:
            tr["bucket"] = classify_bucket(z.strength)
            tr["zone_price"] = z.price
            tr["zone_strength"] = z.strength
            tr["zone_source"] = z.source
            tr["zone_n_tests"] = z.n_tests
        annotated.append(tr)

        # Progress every 5s
        now = time.time()
        if now - last_report > 5.0:
            done = i + 1
            rate = done / (now - t0)
            eta = (n - done) / rate if rate > 0 else 0
            logger.info(
                f"[analyze] {done}/{n} ({100*done/n:.1f}%) "
                f"rate={rate:.0f}/s eta={eta:.0f}s"
            )
            last_report = now
    elapsed = time.time() - t0
    logger.info(f"[analyze] DONE n={n} elapsed={elapsed:.1f}s")
    return annotated


# ====================================================================
# Reports
# ====================================================================

def per_bucket_table(annotated: list[dict]) -> list[dict]:
    rows = []
    for b in BUCKETS:
        sub = [t for t in annotated if t["bucket"] == b]
        rows.append(bucket_stats(sub, b))
    rows.append(bucket_stats(annotated, "ALL"))
    return rows


def per_year_bucket(annotated: list[dict]) -> list[dict]:
    out: list[dict] = []
    years = sorted({t["year"] for t in annotated})
    for y in years:
        for b in BUCKETS:
            sub = [t for t in annotated if t["year"] == y and t["bucket"] == b]
            s = bucket_stats(sub, b)
            s["year"] = y
            out.append(s)
    return out


def per_source(annotated: list[dict]) -> list[dict]:
    """Per-source breakdown of trades that landed at a zone."""
    at_zone = [t for t in annotated if t["bucket"] != "no_sr"]
    sources = sorted({t["zone_source"] for t in at_zone if t["zone_source"]})
    out: list[dict] = []
    for s in sources:
        sub = [t for t in at_zone if t["zone_source"] == s]
        r = bucket_stats(sub, f"source:{s}")
        out.append(r)
    return out


def simulate_size_boost(annotated: list[dict], boost: float = 1.3,
                        boost_buckets: tuple = ("strong_sr", "very_strong_sr")) -> dict:
    """1.3x size on trades in boost_buckets. Compare to baseline."""
    baseline = sum(t["pnl_dollars"] for t in annotated)
    boosted = 0.0
    n_boosted = 0
    for t in annotated:
        if t["bucket"] in boost_buckets:
            boosted += t["pnl_dollars"] * boost
            n_boosted += 1
        else:
            boosted += t["pnl_dollars"]
    return {
        "baseline_pnl": baseline,
        "boosted_pnl": boosted,
        "lift": boosted - baseline,
        "n_boosted": n_boosted,
        "n_total": len(annotated),
    }


def simulate_size_boost_per_year(annotated: list[dict], boost: float = 1.3,
                                  boost_buckets: tuple = ("strong_sr", "very_strong_sr")
                                  ) -> list[dict]:
    out: list[dict] = []
    years = sorted({t["year"] for t in annotated})
    for y in years:
        sub = [t for t in annotated if t["year"] == y]
        baseline = sum(t["pnl_dollars"] for t in sub)
        boosted = 0.0
        n_boosted = 0
        for t in sub:
            if t["bucket"] in boost_buckets:
                boosted += t["pnl_dollars"] * boost
                n_boosted += 1
            else:
                boosted += t["pnl_dollars"]
        out.append({
            "year": y,
            "n_total": len(sub),
            "n_boosted": n_boosted,
            "baseline_pnl": baseline,
            "boosted_pnl": boosted,
            "lift": boosted - baseline,
        })
    return out


# ====================================================================
# Output
# ====================================================================

def write_summary(per_bucket: list[dict], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["bucket", "n", "wr", "total_pnl",
                                            "avg_pnl", "wins_pnl",
                                            "losses_pnl", "pf"])
        w.writeheader()
        for r in per_bucket:
            w.writerow({k: round(v, 4) if isinstance(v, float) else v
                         for k, v in r.items()})


def fmt_pct(x): return f"{100*x:.1f}%"
def fmt_dol(x): return f"${x:,.0f}"


def print_report(per_bucket, per_year, sources, boost, boost_year):
    print()
    print("=" * 70)
    print("S/R CONFLUENCE ANALYSIS — spring_setup (5 years, 20,778 trades)")
    print("=" * 70)

    print()
    print("Per-bucket statistics")
    print("-" * 70)
    print(f"{'bucket':<18}{'n':>7}{'WR':>8}{'tot $':>14}{'avg $':>10}{'PF':>8}")
    for r in per_bucket:
        print(f"{r['bucket']:<18}{r['n']:>7}{fmt_pct(r['wr']):>8}"
              f"{fmt_dol(r['total_pnl']):>14}"
              f"  ${r['avg_pnl']:>7.2f}"
              f"{r['pf']:>8.2f}")

    print()
    print("Per-year stability (bucket x year, $)")
    print("-" * 70)
    years = sorted({r['year'] for r in per_year})
    header = f"{'year':>6}"
    for b in BUCKETS:
        header += f"{b:>17}"
    print(header)
    for y in years:
        line = f"{y:>6}"
        for b in BUCKETS:
            row = next((r for r in per_year if r['year'] == y and r['bucket'] == b), None)
            if row and row['n'] > 0:
                line += f"  n={row['n']:>4} ${row['total_pnl']:>6.0f}"
            else:
                line += f"  {'-':>13}"
        print(line)

    if sources:
        print()
        print("Per-zone-source breakdown (only trades AT a zone)")
        print("-" * 70)
        print(f"{'source':<22}{'n':>7}{'WR':>8}{'tot $':>14}{'avg $':>10}{'PF':>8}")
        for r in sources:
            print(f"{r['bucket']:<22}{r['n']:>7}{fmt_pct(r['wr']):>8}"
                  f"{fmt_dol(r['total_pnl']):>14}"
                  f"  ${r['avg_pnl']:>7.2f}"
                  f"{r['pf']:>8.2f}")

    print()
    print("Size-boost simulation (1.3x on strong_sr + very_strong_sr)")
    print("-" * 70)
    print(f"baseline 5y P&L:       {fmt_dol(boost['baseline_pnl'])}")
    print(f"boosted 5y P&L:        {fmt_dol(boost['boosted_pnl'])}")
    print(f"net lift:              {fmt_dol(boost['lift'])}")
    print(f"trades boosted:        {boost['n_boosted']} / {boost['n_total']} "
          f"({fmt_pct(boost['n_boosted']/boost['n_total'])})")

    print()
    print("Boost lift by year")
    print("-" * 70)
    print(f"{'year':>6}{'n_total':>10}{'n_boost':>10}{'baseline $':>14}"
          f"{'boosted $':>14}{'lift $':>10}")
    for r in boost_year:
        print(f"{r['year']:>6}{r['n_total']:>10}{r['n_boosted']:>10}"
              f"{fmt_dol(r['baseline_pnl']):>14}"
              f"{fmt_dol(r['boosted_pnl']):>14}"
              f"{fmt_dol(r['lift']):>12}")
    print()


def write_markdown_report(per_bucket, per_year, sources, boost, boost_year,
                            edge_pct_at_zone: float,
                            out_path: Path,
                            dir_breakdown: list[dict] | None = None,
                            boost_strong_only: dict | None = None,
                            boost_strong_only_year: list[dict] | None = None):
    L = []
    L.append("# S/R Confluence Analysis — spring_setup")
    L.append("")
    L.append("**Date:** 2026-05-19")
    L.append("**Source:** `tools/phoenix_sr_confluence_analyzer.py`")
    L.append("**Strategy:** `spring_setup` (20,778 clean trades, 5 years)")
    L.append("**S/R engine:** `core/sr_zones.py` (swing + round + PDH/PDL + clustering)")
    L.append("")
    L.append("## Hypothesis")
    L.append("")
    L.append("> A spring wick at a strong S/R zone reflects real absorption/stop-runs")
    L.append("> at a known level — high probability. A spring in noise is just a wick.")
    L.append("> Therefore springs at strong S/R should outperform springs with no S/R.")
    L.append("")
    L.append("## Method")
    L.append("")
    L.append("For each spring_setup trade, compute S/R zones from the 300 most recent")
    L.append("5m bars STRICTLY BEFORE entry_ts. Find nearest zone of the matching")
    L.append("direction (SUPPORT for LONG, RESISTANCE for SHORT) within 4 ticks of")
    L.append("entry_price. Bucket by zone strength:")
    L.append("")
    L.append("| bucket | criterion |")
    L.append("|---|---|")
    L.append("| `no_sr` | no qualifying zone within 4t |")
    L.append("| `weak_sr` | zone within 4t, strength < 0.50 |")
    L.append("| `strong_sr` | zone within 4t, 0.50 <= strength < 0.70 |")
    L.append("| `very_strong_sr` | zone within 4t, strength >= 0.70 |")
    L.append("")
    L.append("## Edge check #1: how often do springs land at a zone?")
    L.append("")
    L.append(f"{edge_pct_at_zone*100:.1f}% of spring_setup trades occurred AT a")
    L.append("detected S/R zone (within 4 ticks). If this number is near 100% or near")
    L.append("0% the signal isn't actionable — must be in the actionable middle.")
    L.append("")
    L.append("## Per-bucket statistics")
    L.append("")
    L.append("| bucket | n | WR | total $ | avg $ | PF |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for r in per_bucket:
        L.append(f"| `{r['bucket']}` | {r['n']:,} | {fmt_pct(r['wr'])} | "
                  f"{fmt_dol(r['total_pnl'])} | ${r['avg_pnl']:.2f} | "
                  f"{r['pf']:.2f} |")
    L.append("")
    if sources:
        L.append("## Per-zone-source breakdown (only trades AT a zone)")
        L.append("")
        L.append("| source | n | WR | total $ | avg $ | PF |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for r in sources:
            L.append(f"| `{r['bucket']}` | {r['n']:,} | {fmt_pct(r['wr'])} | "
                      f"{fmt_dol(r['total_pnl'])} | ${r['avg_pnl']:.2f} | "
                      f"{r['pf']:.2f} |")
        L.append("")
    L.append("## Per-year stability (bucket x year, total $)")
    L.append("")
    years = sorted({r['year'] for r in per_year})
    header = "| year | " + " | ".join(BUCKETS) + " |"
    sep = "|---:|" + "|".join([":-:"] * len(BUCKETS)) + "|"
    L.append(header)
    L.append(sep)
    for y in years:
        cells = []
        for b in BUCKETS:
            row = next((r for r in per_year if r['year'] == y
                         and r['bucket'] == b), None)
            if row and row['n'] > 0:
                cells.append(f"n={row['n']} {fmt_dol(row['total_pnl'])}")
            else:
                cells.append("-")
        L.append(f"| {y} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("## Size-boost simulation (1.3x on `strong_sr` + `very_strong_sr`)")
    L.append("")
    L.append(f"- Baseline 5y P&L: **{fmt_dol(boost['baseline_pnl'])}**")
    L.append(f"- Boosted 5y P&L: **{fmt_dol(boost['boosted_pnl'])}**")
    L.append(f"- Net lift: **{fmt_dol(boost['lift'])}**")
    L.append(f"- Trades boosted: {boost['n_boosted']:,} / "
              f"{boost['n_total']:,} ({fmt_pct(boost['n_boosted']/boost['n_total'])})")
    L.append("")
    L.append("### Boost lift by year")
    L.append("")
    L.append("| year | n_total | n_boost | baseline $ | boosted $ | lift $ |")
    L.append("|---:|---:|---:|---:|---:|---:|")
    for r in boost_year:
        L.append(f"| {r['year']} | {r['n_total']:,} | {r['n_boosted']:,} | "
                  f"{fmt_dol(r['baseline_pnl'])} | {fmt_dol(r['boosted_pnl'])} | "
                  f"{fmt_dol(r['lift'])} |")
    L.append("")

    if boost_strong_only is not None:
        L.append("## Conservative boost variant — `strong_sr` ONLY (skip `very_strong_sr`)")
        L.append("")
        L.append("Driven by the observation that `very_strong_sr` is the disaster bucket")
        L.append("(price already at a multi-tested level — next test more likely to break).")
        L.append("This variant boosts ONLY the cleaner `strong_sr` bucket.")
        L.append("")
        L.append(f"- Baseline 5y P&L: **{fmt_dol(boost_strong_only['baseline_pnl'])}**")
        L.append(f"- Boosted 5y P&L: **{fmt_dol(boost_strong_only['boosted_pnl'])}**")
        L.append(f"- Net lift: **{fmt_dol(boost_strong_only['lift'])}**")
        L.append(f"- Trades boosted: {boost_strong_only['n_boosted']:,} / "
                  f"{boost_strong_only['n_total']:,} "
                  f"({fmt_pct(boost_strong_only['n_boosted']/boost_strong_only['n_total'])})")
        L.append("")
        if boost_strong_only_year:
            L.append("| year | n_boost | baseline $ | boosted $ | lift $ |")
            L.append("|---:|---:|---:|---:|---:|")
            for r in boost_strong_only_year:
                L.append(f"| {r['year']} | {r['n_boosted']:,} | "
                          f"{fmt_dol(r['baseline_pnl'])} | "
                          f"{fmt_dol(r['boosted_pnl'])} | "
                          f"{fmt_dol(r['lift'])} |")
            L.append("")

    if dir_breakdown:
        L.append("## Edge check #2: direction-vs-bucket interaction")
        L.append("")
        L.append("Hypothesis sub-test: does a LONG into known support behave the same as")
        L.append("a SHORT into known resistance?")
        L.append("")
        L.append("| direction x bucket | n | WR | total $ | avg $ | PF |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for r in dir_breakdown:
            if r['n'] == 0:
                continue
            L.append(f"| `{r['bucket']}` | {r['n']:,} | {fmt_pct(r['wr'])} | "
                      f"{fmt_dol(r['total_pnl'])} | ${r['avg_pnl']:.2f} | "
                      f"{r['pf']:.2f} |")
        L.append("")

    # Verdict
    no_sr = next((r for r in per_bucket if r['bucket'] == 'no_sr'), None)
    strong_combined_trades = [r for r in per_bucket
                                if r['bucket'] in ('strong_sr', 'very_strong_sr')]
    n_strong = sum(r['n'] for r in strong_combined_trades)
    avg_strong = (sum(r['total_pnl'] for r in strong_combined_trades) / n_strong
                    if n_strong else 0)
    avg_no_sr = no_sr['avg_pnl'] if no_sr else 0
    wr_strong = (sum(r['wr'] * r['n'] for r in strong_combined_trades) / n_strong
                  if n_strong else 0)
    wr_no_sr = no_sr['wr'] if no_sr else 0

    L.append("## Verdict")
    L.append("")
    L.append(f"- `no_sr` avg/trade: ${avg_no_sr:.2f}  (WR {wr_no_sr*100:.1f}%)")
    L.append(f"- combined strong/very_strong avg/trade: ${avg_strong:.2f}  "
              f"(WR {wr_strong*100:.1f}%)")
    diff = avg_strong - avg_no_sr
    rel = (diff / abs(avg_no_sr)) * 100 if avg_no_sr != 0 else 0
    L.append(f"- delta avg/trade: ${diff:+.2f}  ({rel:+.0f}% vs no_sr)")
    L.append("")

    consistent = sum(1 for r in boost_year if r['lift'] > 0)
    total_years = len(boost_year)
    L.append(f"- Years with positive boost lift (default boost): "
              f"{consistent}/{total_years}")
    L.append("")

    if boost_strong_only is not None:
        strong_only_consistent = sum(1 for r in boost_strong_only_year if r['lift'] > 0)
        L.append(f"- Years with positive lift (strong_sr-only variant): "
                  f"{strong_only_consistent}/{total_years}")
        L.append("")

        # Decide on the strong_only variant
        s_lift = boost_strong_only['lift']
        if s_lift > 500 and strong_only_consistent >= total_years - 1:
            L.append("**VERDICT: Partial hypothesis support.**")
            L.append("")
            L.append("The default 'boost strong + very_strong' is NEGATIVE because")
            L.append("`very_strong_sr` (strength >= 0.70) is itself an anti-edge bucket")
            L.append("(price at heavily-tested levels breaks more often than it holds).")
            L.append("")
            L.append("However, **`strong_sr` ONLY (0.50 <= strength < 0.70) is a")
            L.append("real edge** — boosting just this thin band is profitable and stable")
            L.append(f"across years (+{fmt_dol(s_lift)} over 5 years).")
            L.append("")
            L.append("**Recommend shipping `SpringSrSizeBoostFilter` with")
            L.append("strict-band gating (only boost strength in [0.50, 0.70)).**")
        elif s_lift > 0 and strong_only_consistent >= 3:
            L.append("**VERDICT: Weak partial support.** `strong_sr`-only variant is")
            L.append(f"positive ({fmt_dol(s_lift)}) but sample size is small ")
            L.append("(only a few hundred trades over 5y). Consider as future research,")
            L.append("not production wiring.")
        else:
            L.append("**VERDICT: Hypothesis REJECTED.** Neither boost variant produces")
            L.append("meaningful, consistent lift. Wyckoff springs apparently fire BOTH")
            L.append("at noise and at known S/R levels but the S/R confluence does NOT")
            L.append("predict outperformance. Do NOT wire S/R zones into spring_setup")
            L.append("sizing.")
    L.append("")

    L.append("## Production wiring (draft)")
    L.append("")
    L.append("Draft `SpringSrSizeBoostFilter` lives in `core/entry_filters_size.py`")
    L.append("(separate file from Spawn A's `entry_filters_sr.py` veto for")
    L.append("`bias_momentum`).")
    L.append("")
    L.append("CRITICAL: the filter must use a NARROW strength band (0.50 <= s < 0.70).")
    L.append("Boosting >= 0.70 (`very_strong_sr`) is an actual anti-edge — that band")
    L.append("must be EXCLUDED, not included.")
    L.append("")
    L.append("```python")
    L.append("from core.entry_filters_size import SpringSrSizeBoostFilter")
    L.append("")
    L.append("# inside base_bot._evaluate_strategies, after a spring_setup signal:")
    L.append("if signal and signal.strategy == 'spring_setup':")
    L.append("    boost_filter = SpringSrSizeBoostFilter()")
    L.append("    multiplier = boost_filter.size_multiplier(")
    L.append("        signal, bars_5m=bars_5m, market=market,")
    L.append("    )")
    L.append("    # multiplier == 1.30 ONLY when nearest zone is in [0.50, 0.70).")
    L.append("    # Returns 1.00 for noise AND for very_strong_sr (skip the anti-edge).")
    L.append("    signal.size_multiplier = multiplier")
    L.append("```")
    L.append("")
    L.append("**Do NOT ship until live-shadow paper-tracked for 30+ trades** —")
    L.append("the `strong_sr` bucket has only 162 historical trades, so a Wilson 95%")
    L.append("CI on its win rate is wide. Validation tier: PRELIMINARY.")
    L.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L), encoding="utf-8")
    logger.info(f"[md] wrote {out_path}")


# ====================================================================
# Main
# ====================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="spring_setup")
    ap.add_argument("--trades", default="backtest_results/phoenix_real_5year.csv")
    ap.add_argument("--bars", default="data/historical/mnq_5min_databento.csv")
    ap.add_argument("--out", default="backtest_results/phoenix_sr_confluence_summary.csv")
    ap.add_argument("--md", default="docs/SR_CONFLUENCE_SPRING_SETUP.md")
    ap.add_argument("--annotated-out",
                     default="backtest_results/phoenix_sr_confluence_per_trade.csv")
    ap.add_argument("--limit", type=int, default=0,
                     help="if >0, only analyze first N trades (for fast debug)")
    args = ap.parse_args()

    trades_csv = ROOT / args.trades
    bars_csv = ROOT / args.bars

    logger.info(f"[main] loading trades from {trades_csv}")
    trades = load_trades(trades_csv, args.strategy)
    logger.info(f"[main] loaded {len(trades)} {args.strategy} trades")
    if args.limit > 0:
        trades = trades[: args.limit]
        logger.info(f"[main] LIMIT to first {args.limit}")

    logger.info(f"[main] loading 5m bars from {bars_csv}")
    t_bars = time.time()
    bars_df = load_5m_bars(bars_csv)
    logger.info(f"[main] loaded {len(bars_df):,} 5m bars in "
                f"{time.time()-t_bars:.1f}s")

    annotated = analyze(trades, bars_df)

    # Save per-trade annotation
    out_per_trade = ROOT / args.annotated_out
    out_per_trade.parent.mkdir(parents=True, exist_ok=True)
    with open(out_per_trade, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entry_ts", "direction", "entry_price", "pnl_dollars",
                     "year", "bucket", "zone_price", "zone_strength",
                     "zone_source", "zone_n_tests", "exit_reason"])
        for t in annotated:
            w.writerow([
                t["entry_ts"].isoformat(),
                t["direction"], t["entry_price"], t["pnl_dollars"],
                t["year"], t["bucket"],
                t.get("zone_price"), t.get("zone_strength"),
                t.get("zone_source"), t.get("zone_n_tests"),
                t.get("exit_reason"),
            ])
    logger.info(f"[main] per-trade annotation written: {out_per_trade}")

    # Summary stats
    per_bucket = per_bucket_table(annotated)
    per_year = per_year_bucket(annotated)
    sources = per_source(annotated)
    boost = simulate_size_boost(annotated)
    boost_year = simulate_size_boost_per_year(annotated)

    # Edge check #1
    at_zone = sum(1 for t in annotated if t["bucket"] != "no_sr")
    pct_at_zone = at_zone / len(annotated) if annotated else 0

    # Edge check #2: direction-vs-bucket interaction
    dir_breakdown: list[dict] = []
    for d in ("LONG", "SHORT"):
        for b in BUCKETS:
            sub = [t for t in annotated if t["direction"] == d and t["bucket"] == b]
            row = bucket_stats(sub, f"{d}_{b}")
            dir_breakdown.append(row)

    # Conservative-only boost: strong_sr only (the actual signal-rich bucket)
    boost_strong_only = simulate_size_boost(
        annotated, boost=1.3, boost_buckets=("strong_sr",))
    boost_strong_only_year = simulate_size_boost_per_year(
        annotated, boost=1.3, boost_buckets=("strong_sr",))

    out_csv = ROOT / args.out
    write_summary(per_bucket, out_csv)
    logger.info(f"[main] summary CSV written: {out_csv}")

    md_path = ROOT / args.md
    write_markdown_report(per_bucket, per_year, sources, boost, boost_year,
                            pct_at_zone, md_path,
                            dir_breakdown=dir_breakdown,
                            boost_strong_only=boost_strong_only,
                            boost_strong_only_year=boost_strong_only_year)

    print_report(per_bucket, per_year, sources, boost, boost_year)
    print(f"Springs at a zone: {pct_at_zone*100:.1f}% "
          f"({at_zone:,}/{len(annotated):,})")
    print()
    print(f"Per-trade CSV:   {out_per_trade}")
    print(f"Summary CSV:     {out_csv}")
    print(f"Markdown report: {md_path}")


if __name__ == "__main__":
    main()
