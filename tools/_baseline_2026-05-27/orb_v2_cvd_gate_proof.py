"""
PROOF: orb_v2's 1-trade-in-5y is caused by its require_cvd_aligned gate,
NOT by a data limitation. Run two orb_v2 instances over the same 5y OHLCV:
  - gated:   require_cvd_aligned=True  (production default -> ~1 trade)
  - ungated: require_cvd_aligned=False (CVD gate off -> should fire normally)

In-memory config override only; config/strategies.py is NOT modified.
Read-only research. Output: console + out/_baseline_2026-05-27/orb_v2_cvd_gate_proof.txt
"""
from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.strategies import STRATEGIES
from strategies.orb_v2 import ORBv2
from tools.phoenix_real_backtest import CSVEnrichmentPipeline, simulate_trade

COMM = 2.82


def run_variant_signals():
    data_dir = ROOT / "data" / "historical"
    pipeline = CSVEnrichmentPipeline(
        mnq_1m_csv=str(data_dir / "mnq_1min_databento.csv"),
        mnq_5m_csv=str(data_dir / "mnq_5min_databento.csv"),
        mes_1m_csv=str(data_dir / "mes_1min_databento.csv"),
        mes_5m_csv=str(data_dir / "mes_5min_databento.csv"),
        start="2021-05-17", end="2026-05-17",
    )
    mnq_1m_df = pipeline.mnq_1m_df.copy()

    cfg_gated = copy.deepcopy(STRATEGIES["orb_v2"]); cfg_gated["require_cvd_aligned"] = True
    cfg_ungated = copy.deepcopy(STRATEGIES["orb_v2"]); cfg_ungated["require_cvd_aligned"] = False
    variants = {
        "gated(require_cvd=True)": {"strat": ORBv2(cfg_gated), "active": None, "trades": []},
        "ungated(require_cvd=False)": {"strat": ORBv2(cfg_ungated), "active": None, "trades": []},
    }

    cycle = 0
    t0 = time.time()
    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle += 1
        if cycle < 300:
            continue
        for vname, v in variants.items():
            if v["active"] is not None:
                if v["active"].get("exit_ts") is not None and eval_ts >= v["active"]["exit_ts"]:
                    v["active"] = None
                else:
                    continue
            try:
                sig = v["strat"].evaluate(market, bars_5m, bars_1m, session_info)
            except Exception:
                continue
            if sig is None:
                continue
            entry_price = sig.entry_price if sig.entry_price else market["price"]
            if sig.stop_price is not None and sig.target_price is not None:
                stop_price, target_price = sig.stop_price, sig.target_price
            else:
                sd = sig.stop_ticks * 0.25
                if sig.direction == "LONG":
                    stop_price, target_price = entry_price - sd, entry_price + sd * sig.target_rr
                else:
                    stop_price, target_price = entry_price + sd, entry_price - sd * sig.target_rr
            tr = simulate_trade("orb_v2", sig.direction, eval_ts, entry_price,
                                stop_price, target_price, mnq_1m_df)
            v["active"] = {"exit_ts": tr.exit_ts}
            v["trades"].append({"entry_ts": eval_ts, "pnl_dollars": tr.pnl_dollars,
                                "hour_ct": eval_ts.tz_convert("America/Chicago").hour})
        if cycle % 400000 == 0:
            print(f"  ...{cycle:,} cycles ({time.time()-t0:.0f}s)", flush=True)

    print(f"[done] {cycle:,} cycles in {time.time()-t0:.0f}s\n")
    return variants


def main():
    variants = run_variant_signals()
    lines = ["ORB_V2 CVD-GATE PROOF — same 5y OHLCV, two configs", "=" * 60, ""]
    for vname, v in variants.items():
        df = pd.DataFrame(v["trades"])
        n = len(df)
        if n == 0:
            lines.append(f"{vname}: 0 trades"); continue
        gross = df.pnl_dollars.sum()
        wins = int((df.pnl_dollars > 0).sum())
        net = gross - n * COMM
        lines.append(f"{vname}:")
        lines.append(f"  trades={n}  WR={100*wins/n:.1f}%  gross=${gross:,.0f}  "
                     f"avg=${gross/n:.2f}/tr  net_after_comm=${net:,.0f}")
    out = "\n".join(lines)
    print(out)
    (ROOT / "out" / "_baseline_2026-05-27" / "orb_v2_cvd_gate_proof.txt").write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
