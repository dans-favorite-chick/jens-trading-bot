"""Backtester enrichment fidelity audit — field-by-field vs live ground truth.

Both the backtester and the live bot call the IDENTICAL strategy.evaluate();
the only thing that can differ is the `market` dict each feeds it. So this audit
compares the de-stubbed backtester's reconstructed market fields against the
values the live bot actually recorded (logs/history/<date>_prod.jsonl), minute
by minute, and reports per-field agreement. This isolates exactly which inputs
the backtester reconstructs faithfully and which diverge — without needing any
new data and without the broken-cr DECISION contamination (we compare raw
inputs, and flag cr_verdict separately since it was buggy live in this window).

Usage:
    python tools/replay_enrichment/enrichment_audit.py --start 2026-05-06 --end 2026-05-15
"""
from __future__ import annotations
import argparse, json, sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from tools.phoenix_real_backtest import (
    CSVEnrichmentPipeline, EnrichmentState, _load_bars_from_csv, _CT,
)
from tools.replay_enrichment.recorded_cvd import RecordedCVDProvider

DATA = ROOT / "data" / "historical"
NUMERIC = ["price", "vwap", "ema9", "atr_1m", "atr_5m", "dom_imbalance",
           "tf_votes_bullish", "tf_votes_bearish", "cvd"]
CATEGORICAL = [("tf_bias", "1m"), ("tf_bias", "5m"), ("tf_bias", "15m"),
               ("tf_bias", "60m"), ("regime", None), ("cr_verdict", None)]

def mkey(ts):
    return ts.tz_convert(_CT).tz_localize(None).to_pydatetime().replace(second=0, microsecond=0)

def load_live(start, end):
    out = {}
    d = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= d1:
        p = ROOT / "logs" / "history" / f"{d.isoformat()}_prod.jsonl"
        d += timedelta(days=1)
        if not p.exists():
            continue
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("event") != "eval":
                continue
            try:
                dt = datetime.fromisoformat(r["ts"]).replace(second=0, microsecond=0)
            except Exception:
                continue
            out[dt] = r
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--warmup-days", type=int, default=20)
    ap.add_argument("--bar-source", choices=["databento", "recorded"], default="databento",
                    help="recorded = replay the bot's own logs/history bars (MNQ) "
                         "instead of databento CSVs.")
    args = ap.parse_args()

    live = load_live(args.start, args.end)
    print(f"live eval minutes {args.start}..{args.end}: {len(live)}")
    start = pd.Timestamp(f"{args.start}T00:00:00Z") - pd.Timedelta(days=args.warmup_days)
    end = pd.Timestamp(f"{args.end}T00:00:00Z") + pd.Timedelta(days=1, hours=8)
    def slc(df): return df[(df.ts >= start) & (df.ts <= end)].reset_index(drop=True)
    if args.bar_source == "recorded":
        from tools.replay_enrichment.recorded_bars import load_recorded_bars
        mnq1 = load_recorded_bars(args.start, args.end, "1m", warmup_days=args.warmup_days)
        mnq5 = load_recorded_bars(args.start, args.end, "5m", warmup_days=args.warmup_days)
        print(f"recorded MNQ bars: 1m={len(mnq1)} 5m={len(mnq5)} (MES from databento)")
    else:
        mnq1 = slc(_load_bars_from_csv(str(DATA / "mnq_1min_databento.csv")))
        mnq5 = slc(_load_bars_from_csv(str(DATA / "mnq_5min_databento.csv")))
    mes1 = slc(_load_bars_from_csv(str(DATA / "mes_1min_databento.csv")))
    mes5 = slc(_load_bars_from_csv(str(DATA / "mes_5min_databento.csv")))
    pipe = CSVEnrichmentPipeline.__new__(CSVEnrichmentPipeline)
    pipe.mnq_1m_df, pipe.mnq_5m_df = mnq1, mnq5
    pipe.mes_1m_df, pipe.mes_5m_df = mes1, mes5
    pipe.mnq, pipe.mes = EnrichmentState(), EnrichmentState()
    pipe.enable_real_enrichment(RecordedCVDProvider(volumetric_path=str(ROOT / "logs" / "volumetric_history.jsonl")))

    num = {f: [] for f in NUMERIC}      # list of (bt, live)
    cat = {f"{a}.{b}" if b else a: [0, 0] for a, b in CATEGORICAL}  # [agree, total]
    matched = 0
    for ets, market, b1, b5, sess in pipe.iter_eval_cycles():
        lv = live.get(mkey(ets))
        if lv is None:
            continue
        matched += 1
        for f in NUMERIC:
            bv = market.get(f)
            lvv = lv.get(f)
            if isinstance(bv, (int, float)) and isinstance(lvv, (int, float)):
                num[f].append((float(bv), float(lvv)))
        for a, b in CATEGORICAL:
            key = f"{a}.{b}" if b else a
            bv = (market.get(a) or {}).get(b) if b else market.get(a)
            lvv = (lv.get(a) or {}).get(b) if b else lv.get(a)
            if bv is not None and lvv is not None:
                cat[key][1] += 1
                if str(bv) == str(lvv):
                    cat[key][0] += 1

    print(f"matched minutes: {matched}\n")
    print("== NUMERIC fields (BT vs live) ==")
    print(f"{'field':18s} {'n':>6s} {'medAbsErr':>10s} {'p90AbsErr':>10s} {'medRel%':>9s} {'corr':>6s}")
    for f in NUMERIC:
        pairs = num[f]
        if not pairs:
            print(f"{f:18s} {'0':>6s}  (no comparable values)")
            continue
        errs = sorted(abs(b - l) for b, l in pairs)
        rels = sorted(abs(b - l) / abs(l) * 100 for b, l in pairs if abs(l) > 1e-9)
        n = len(errs)
        med = errs[n // 2]
        p90 = errs[min(n - 1, int(0.9 * n))]
        medrel = rels[len(rels) // 2] if rels else float("nan")
        # Pearson corr
        bs = [b for b, _ in pairs]; ls = [l for _, l in pairs]
        mb = sum(bs) / n; ml = sum(ls) / n
        cov = sum((b - mb) * (l - ml) for b, l in pairs)
        vb = sum((b - mb) ** 2 for b in bs) ** 0.5
        vl = sum((l - ml) ** 2 for l in ls) ** 0.5
        corr = cov / (vb * vl) if vb > 0 and vl > 0 else float("nan")
        print(f"{f:18s} {n:>6d} {med:>10.4f} {p90:>10.4f} {medrel:>8.2f}% {corr:>6.3f}")
    print("\n== CATEGORICAL fields (agreement %) ==")
    for key, (agree, total) in cat.items():
        pct = 100 * agree / total if total else 0
        print(f"  {key:14s} {agree:5d}/{total:<5d} = {pct:6.2f}%")

if __name__ == "__main__":
    main()
