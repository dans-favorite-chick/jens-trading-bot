"""Fidelity check — de-stubbed backtester vs LIVE prod eval-log ground truth.

Produces a defensible per-strategy divergence number WITHOUT waiting for new
live trades to accumulate. For one calendar day it:

  - replays the de-stubbed CSVEnrichmentPipeline (real cvd_health from recorded
    delta, es_nq_rs from MES bars, day_type/cr_verdict from the live core
    modules) and runs the REAL bias_momentum.evaluate() each 1m cycle;
  - aligns each cycle (by CT minute) to the live prod eval record the bot
    actually wrote that day (logs/history/<date>_prod.jsonl);
  - compares the reconstructed cr_verdict to the live-recorded cr_verdict, and
    the backtester's bias_momentum DECISION (signal / no-signal / direction) to
    what live bias_momentum recorded.

This measures STRATEGY-LOGIC + ENRICHMENT fidelity — whether the backtester
SEES and DECIDES like the live bot on the same wall-clock minute. It does NOT
measure execution fidelity (fills, latency, slippage); that still requires real
live trades. Read-only; writes nothing except its stdout report.

Usage:
    python tools/replay_enrichment/fidelity_vs_eval_logs.py --date 2026-05-13
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
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
    """UTC pandas Timestamp -> naive-CT datetime floored to the minute."""
    return (ts_utc.tz_convert(_CT).tz_localize(None)
            .to_pydatetime().replace(second=0, microsecond=0))


def load_live_evals(date_str: str) -> dict[datetime, dict]:
    """Return {ct_minute: live_eval_record} from the prod history log."""
    path = ROOT / "logs" / "history" / f"{date_str}_prod.jsonl"
    out: dict[datetime, dict] = {}
    if not path.exists():
        return out
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
            bm = None
            for s in rec.get("strategies", []) or []:
                if s.get("name") == "bias_momentum":
                    bm = s
                    break
            out[dt] = {
                "cr_verdict": rec.get("cr_verdict"),
                "bm_result": (bm or {}).get("result"),
                "bm_direction": (bm or {}).get("direction"),
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (CT day to check)")
    args = ap.parse_args()
    date_str = args.date

    live = load_live_evals(date_str)
    print(f"live prod eval records for {date_str}: {len(live)}")
    if not live:
        print("No prod eval log for that date — pick a date with logs/history/"
              "<date>_prod.jsonl AND databento CSV coverage (<= ~2026-05-15).")
        return

    # Pipeline window: load the CT day +/- buffer, in UTC.
    start = pd.Timestamp(f"{date_str}T00:00:00Z") - pd.Timedelta(hours=8)
    end = pd.Timestamp(f"{date_str}T00:00:00Z") + pd.Timedelta(hours=32)

    def slc(df):
        return df[(df.ts >= start) & (df.ts <= end)].reset_index(drop=True)

    print("loading CSVs...")
    mnq1 = slc(_load_bars_from_csv(str(DATA / "mnq_1min_databento.csv")))
    mnq5 = slc(_load_bars_from_csv(str(DATA / "mnq_5min_databento.csv")))
    mes1 = slc(_load_bars_from_csv(str(DATA / "mes_1min_databento.csv")))
    mes5 = slc(_load_bars_from_csv(str(DATA / "mes_5min_databento.csv")))
    print(f"MNQ1m={len(mnq1)} MNQ5m={len(mnq5)} MES1m={len(mes1)} MES5m={len(mes5)}")
    if len(mnq1) == 0:
        print("No MNQ CSV bars in window — date is outside databento coverage.")
        return

    pipe = CSVEnrichmentPipeline.__new__(CSVEnrichmentPipeline)
    pipe.mnq_1m_df, pipe.mnq_5m_df = mnq1, mnq5
    pipe.mes_1m_df, pipe.mes_5m_df = mes1, mes5
    pipe.mnq, pipe.mes = EnrichmentState(), EnrichmentState()
    prov = RecordedCVDProvider(volumetric_path=str(ROOT / "logs" / "volumetric_history.jsonl"))
    pipe.enable_real_enrichment(prov)

    strat = instantiate_strategies(["bias_momentum"])["bias_momentum"]

    matched = 0
    cr_compared = 0
    cr_agree = 0
    cr_live_dist: Counter = Counter()
    cr_bt_dist: Counter = Counter()
    decision = Counter()  # both/live_only/bt_only/neither
    dir_match = 0
    dir_total = 0
    bt_eval_errors = 0

    for eval_ts, market, b1, b5, sess in pipe.iter_eval_cycles():
        key = _minute_key_ct(eval_ts)
        lv = live.get(key)
        if lv is None:
            continue
        matched += 1

        # cr_verdict fidelity
        live_cr = lv["cr_verdict"]
        bt_cr = market.get("cr_verdict")
        if live_cr is not None:
            cr_compared += 1
            cr_live_dist[live_cr] += 1
            cr_bt_dist[bt_cr] += 1
            if live_cr == bt_cr:
                cr_agree += 1

        # bias_momentum DECISION fidelity
        try:
            sig = strat.evaluate(market, list(b5), list(b1), sess)
        except Exception:
            bt_eval_errors += 1
            sig = None
        live_sig = (lv["bm_result"] == "SIGNAL")
        bt_sig = sig is not None
        if live_sig and bt_sig:
            decision["both_signal"] += 1
            dir_total += 1
            if (lv.get("bm_direction") or "").upper() == (getattr(sig, "direction", "") or "").upper():
                dir_match += 1
        elif live_sig and not bt_sig:
            decision["live_only"] += 1
        elif bt_sig and not live_sig:
            decision["bt_only"] += 1
        else:
            decision["neither_signal"] += 1

    print("\n================ FIDELITY REPORT ================")
    print(f"date: {date_str}  |  matched CT-minutes (live eval AND backtest cycle): {matched}")
    print("\n-- cr_verdict (reconstructed vs live-recorded) --")
    if cr_compared:
        print(f"  agreement: {cr_agree}/{cr_compared} = {100*cr_agree/cr_compared:.1f}%")
        print(f"  live dist: {dict(cr_live_dist)}")
        print(f"  backtest dist: {dict(cr_bt_dist)}")
    else:
        print("  no comparable cr_verdict (live recorded none)")
    print("\n-- bias_momentum decision (backtest vs live) --")
    tot = sum(decision.values())
    for k in ("both_signal", "neither_signal", "live_only", "bt_only"):
        print(f"  {k}: {decision[k]}/{tot}")
    if dir_total:
        print(f"  direction match | both signalled: {dir_match}/{dir_total} = "
              f"{100*dir_match/dir_total:.1f}%")
    agree_decisions = decision["both_signal"] + decision["neither_signal"]
    if tot:
        print(f"  decision agreement (signal-or-not): {agree_decisions}/{tot} = "
              f"{100*agree_decisions/tot:.1f}%")
    if bt_eval_errors:
        print(f"  (backtest evaluate() errors: {bt_eval_errors})")
    print("=================================================")


if __name__ == "__main__":
    main()
