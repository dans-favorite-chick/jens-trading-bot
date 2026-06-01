"""
_wfa_shard.py — run WFA for a SUBSET of strategies, writing only its own
windows CSV (no shared summary file), so multiple shards run in parallel
without clobbering each other.

Each shard calls wfa.run_wfa() directly (NOT wfa.main(), which would also write
the shared OUT_DIR/wfa_summary.csv and race other shards). The summary is built
once afterward by _wfa_merge.py over all shard window CSVs.

    python tools/portfolio_backtest/_wfa_shard.py \
        --strategies bias_momentum,spring_setup --start 2021-05-17 \
        --end 2026-05-15 --grid full --out <OUT_DIR>/wfa_windows_shardA.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.portfolio_backtest import wfa  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", required=True, help="comma-separated subset")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--grid", choices=["lean", "full"], default="full")
    ap.add_argument("--is-months", type=int, default=12)
    ap.add_argument("--oos-months", type=int, default=3)
    ap.add_argument("--step-months", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    names = [s.strip() for s in a.strategies.split(",") if s.strip()]
    t0 = time.time()
    print(f"[shard] strategies={names} grid={a.grid} out={a.out}")
    df = wfa.run_wfa(
        strategies=names, start=a.start, end=a.end,
        is_months=a.is_months, oos_months=a.oos_months,
        step_months=a.step_months, grid=a.grid,
        apply_friction=True, warmup_min=a.warmup, out_csv=a.out,
    )
    print(f"[shard] done in {time.time()-t0:.0f}s; {len(df)} window-rows -> {a.out}")
    # Warehouse sidecar (per duckdb_warehouse_layout memory; contract schema_version=1)
    from tools.portfolio_backtest.sidecar import emit_sidecar
    emit_sidecar(
        a.out,
        strategy=(names[0] if len(names) == 1 else None),
        params={"strategies": names, "start": a.start, "end": a.end,
                "is_months": a.is_months, "oos_months": a.oos_months,
                "step_months": a.step_months, "grid": a.grid,
                "warmup": a.warmup, "apply_friction": True},
        lookback_start=a.start, lookback_end=a.end,
        friction_per_rt_usd=4.82,
        logical_group="portfolio_wfa",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
