"""
_wfa_shard_phase13.py - WFA shard driver for the 4 Phase 13 lab strategies.

Identical to _wfa_shard.py except it pre-populates wfa._CLASS_CACHE with the
4 lab strategy classes (raschke_baseline, g_inside_bar_breakout,
e_multi_day_breakout, a_asian_continuation) before invoking run_wfa, so
_strategy_class returns the lab classes immediately instead of trying to
fetch them from phoenix_real_backtest.instantiate_strategies (which doesn't
include them).

Same CSV race-freedom as _wfa_shard.py - writes only its own --out file,
never touches the shared summary.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.portfolio_backtest import wfa  # noqa: E402

# Pre-populate the class cache for the 4 lab strategies. Done at import-time
# so any subsequent wfa._strategy_class call hits the cache immediately.
from strategies.raschke_baseline import RaschkeBaseline  # noqa: E402
from strategies.g_inside_bar_breakout import InsideBarBreakout  # noqa: E402
from strategies.e_multi_day_breakout import MultiDayBreakout  # noqa: E402
from strategies.a_asian_continuation import AsianContinuation  # noqa: E402

wfa._CLASS_CACHE["raschke_baseline"] = RaschkeBaseline
wfa._CLASS_CACHE["g_inside_bar_breakout"] = InsideBarBreakout
wfa._CLASS_CACHE["e_multi_day_breakout"] = MultiDayBreakout
wfa._CLASS_CACHE["a_asian_continuation"] = AsianContinuation


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", required=True,
                    help="comma-separated subset of Phase 13 lab strategy names")
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
    print(f"[shard-p13] strategies={names} grid={a.grid} out={a.out}")
    df = wfa.run_wfa(
        strategies=names, start=a.start, end=a.end,
        is_months=a.is_months, oos_months=a.oos_months,
        step_months=a.step_months, grid=a.grid,
        apply_friction=True, warmup_min=a.warmup, out_csv=a.out,
    )
    print(f"[shard-p13] done in {time.time()-t0:.0f}s; "
          f"{len(df)} window-rows -> {a.out}")
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
        logical_group="phase13_wfa",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
