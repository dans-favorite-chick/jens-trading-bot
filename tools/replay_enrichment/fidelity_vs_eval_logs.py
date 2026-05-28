"""Fidelity check — de-stubbed backtester vs LIVE prod eval-log ground truth.

Produces a defensible per-strategy divergence number WITHOUT waiting for new
live trades. Over a date range it:

  - replays the de-stubbed CSVEnrichmentPipeline (real cvd_health from recorded
    delta, es_nq_rs from MES bars, day_type/cr_verdict from the live core
    modules) and runs the REAL <strategy>.evaluate() each 1m cycle;
  - aligns each cycle (by CT minute) to the live prod eval record the bot
    actually wrote (logs/history/<date>_prod.jsonl);
  - compares the backtester's DECISION (signal / no-signal / direction) to what
    the live strategy recorded, plus cr_verdict where applicable.

Measures STRATEGY-LOGIC + ENRICHMENT fidelity (does the backtest SEE and DECIDE
like live on the same wall-clock minute) — NOT execution fidelity (fills,
latency, slippage), which still needs real live trades. Read-only.

Usage:
    python tools/replay_enrichment/fidelity_vs_eval_logs.py \
        --strategy noise_area --start 2026-05-06 --end 2026-05-15
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, date as date_cls
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import (  # noqa: E402
    CSVEnrichmentPipeline, EnrichmentState, _load_bars_from_csv,
    instantiate_strategies, _CT,
)
from tools.replay_enrichment.recorded_cvd import RecordedCVDProvider  # noqa: E402

DATA = ROOT / "data" / "historical"


def _minute_key_ct(ts_utc) -> datetime:
    return (ts_utc.tz_convert(_CT).tz_localize(None)
            .to_pydatetime().replace(second=0, microsecond=0))


def _daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    d = d0
    while d <= d1:
        yield d.isoformat()
        d += timedelta(days=1)


def load_live_evals(start: str, end: str, strategy: str) -> dict[datetime, dict]:
    """Merge {ct_minute: live_eval_record} across prod history logs in range."""
    out: dict[datetime, dict] = {}
    for date_str in _daterange(start, end):
        path = ROOT / "logs" / "history" / f"{date_str}_prod.jsonl"
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") != "eval":
                    continue
                try:
                    dt = datetime.fromisoformat(rec["ts"]).replace(second=0, microsecond=0)
                except Exception:
                    continue
                sig = None
                for s in rec.get("strategies", []) or []:
                    if s.get("name") == strategy:
                        sig = s
                        break
                out[dt] = {
                    "cr_verdict": rec.get("cr_verdict"),
                    "result": (sig or {}).get("result"),
                    "direction": (sig or {}).get("direction"),
                }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="bias_momentum")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (CT, inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (CT, inclusive)")
    ap.add_argument("--warmup-days", type=int, default=20,
                    help="Calendar days of bars BEFORE --start to replay so the "
                         "strategy's rolling internal tables (e.g. noise_area's "
                         "sigma_open) warm up. Only [start,end] minutes are compared.")
    args = ap.parse_args()
    strategy = args.strategy

    live = load_live_evals(args.start, args.end, strategy)
    live_sig_min = sum(1 for v in live.values() if v["result"] == "SIGNAL")
    print(f"live prod eval records {args.start}..{args.end}: {len(live)} "
          f"(minutes where {strategy} SIGNAL: {live_sig_min})")
    if not live:
        print("No prod eval logs in range.")
        return

    # Replay extra days BEFORE --start so rolling strategy tables warm up; the
    # comparison still only matches minutes that exist in the live-eval dict
    # ([start,end]), so warmup bars never enter the scored set.
    start = pd.Timestamp(f"{args.start}T00:00:00Z") - pd.Timedelta(days=args.warmup_days)
    end = pd.Timestamp(f"{args.end}T00:00:00Z") + pd.Timedelta(days=1, hours=8)
    print(f"pipeline window (incl. {args.warmup_days}d warmup): {start.date()} -> {end.date()}")

    def slc(df):
        return df[(df.ts >= start) & (df.ts <= end)].reset_index(drop=True)

    print("loading CSVs...")
    mnq1 = slc(_load_bars_from_csv(str(DATA / "mnq_1min_databento.csv")))
    mnq5 = slc(_load_bars_from_csv(str(DATA / "mnq_5min_databento.csv")))
    mes1 = slc(_load_bars_from_csv(str(DATA / "mes_1min_databento.csv")))
    mes5 = slc(_load_bars_from_csv(str(DATA / "mes_5min_databento.csv")))
    print(f"MNQ1m={len(mnq1)} MNQ5m={len(mnq5)} MES1m={len(mes1)} MES5m={len(mes5)}")
    if len(mnq1) == 0:
        print("No MNQ CSV bars in range — outside databento coverage.")
        return

    pipe = CSVEnrichmentPipeline.__new__(CSVEnrichmentPipeline)
    pipe.mnq_1m_df, pipe.mnq_5m_df = mnq1, mnq5
    pipe.mes_1m_df, pipe.mes_5m_df = mes1, mes5
    pipe.mnq, pipe.mes = EnrichmentState(), EnrichmentState()
    prov = RecordedCVDProvider(volumetric_path=str(ROOT / "logs" / "volumetric_history.jsonl"))
    pipe.enable_real_enrichment(prov)

    strat = instantiate_strategies([strategy])[strategy]
    # Replicate the live bot's startup warmup (base_bot.py:1759-1764): seed
    # noise_area's sigma_open_table from data/sigma_open_table.json. No-op for
    # strategies without seed_history. This is the live-parity seed; continuous
    # accrual then happens via evaluate() every cycle below.
    if hasattr(strat, "seed_history"):
        try:
            from tools.load_sigma_open_warmup import load_sigma_open_warmup
            _warm = load_sigma_open_warmup()
            if _warm:
                strat.seed_history(_warm)
                print(f"seeded {len(_warm)} minute-buckets via load_sigma_open_warmup (live-parity)")
            else:
                print("seed_history: loader returned None (will self-accrue during warmup)")
        except Exception as _se:
            print(f"seed_history skipped: {_se!r}")

    matched = 0
    decision = Counter()
    dir_match = 0
    dir_total = 0
    bt_eval_errors = 0
    bt_only_examples = []

    for eval_ts, market, b1, b5, sess in pipe.iter_eval_cycles():
        # Evaluate EVERY cycle so the strategy's rolling internal state (e.g.
        # noise_area.sigma_open_table) warms up continuously, exactly like a
        # live run. Only minutes with a live eval record are scored below.
        try:
            sig = strat.evaluate(market, list(b5), list(b1), sess)
            _errored = False
        except Exception:
            sig = None
            _errored = True
        key = _minute_key_ct(eval_ts)
        lv = live.get(key)
        if lv is None:
            continue
        matched += 1
        if _errored:
            bt_eval_errors += 1
        live_sig = (lv["result"] == "SIGNAL")
        bt_sig = sig is not None
        if live_sig and bt_sig:
            decision["both_signal"] += 1
            dir_total += 1
            if (lv.get("direction") or "").upper() == (getattr(sig, "direction", "") or "").upper():
                dir_match += 1
        elif live_sig and not bt_sig:
            decision["live_only"] += 1
        elif bt_sig and not live_sig:
            decision["bt_only"] += 1
            if len(bt_only_examples) < 8:
                bt_only_examples.append(str(key))
        else:
            decision["neither_signal"] += 1

    print("\n================ FIDELITY REPORT ================")
    print(f"strategy: {strategy}  |  range: {args.start}..{args.end}")
    print(f"matched CT-minutes (live eval AND backtest cycle): {matched}")
    print("\n-- decision (backtest vs live) --")
    tot = sum(decision.values())
    for k in ("both_signal", "neither_signal", "live_only", "bt_only"):
        print(f"  {k}: {decision[k]}/{tot}")
    if dir_total:
        print(f"  direction match | both signalled: {dir_match}/{dir_total}")
    if tot:
        agree = decision["both_signal"] + decision["neither_signal"]
        print(f"  decision agreement (signal-or-not): {agree}/{tot} = {100*agree/tot:.2f}%")
        live_total_sig = decision["both_signal"] + decision["live_only"]
        bt_total_sig = decision["both_signal"] + decision["bt_only"]
        print(f"  live signals in matched set: {live_total_sig} | backtest signals: {bt_total_sig}")
        print(f"  signal recall (bt reproduced live): {decision['both_signal']}/{live_total_sig}"
              if live_total_sig else "  signal recall: n/a (live had 0 signals in matched set)")
        print(f"  over-fire (bt signalled, live did not): {decision['bt_only']}")
    if bt_only_examples:
        print(f"  bt_only example minutes: {bt_only_examples}")
    if bt_eval_errors:
        print(f"  (backtest evaluate() errors: {bt_eval_errors})")
    print("=================================================")


if __name__ == "__main__":
    main()
