"""
_wfa_merge.py — combine WFA shard window CSVs into the canonical
OUT_DIR/wfa_windows.csv + OUT_DIR/wfa_summary.csv.

Run AFTER all _wfa_shard.py processes finish:
    python tools/portfolio_backtest/_wfa_merge.py
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.portfolio_backtest import wfa, paths  # noqa: E402


def main() -> int:
    # Two glob patterns: original 14-strategy shards + Phase 13 lab shards.
    # _wfa_merge picks up both so the merged outputs cover all 18 strategies.
    patterns = [
        str(paths.OUT_DIR / "wfa_windows_shard*.csv"),
        str(paths.OUT_DIR / "wfa_windows_p13_*.csv"),
    ]
    files = []
    for p in patterns:
        files.extend(sorted(glob.glob(p)))
    if not files:
        print(f"[merge] no shard files matching {patterns}")
        return 1

    frames = []
    for f in files:
        try:
            d = pd.read_csv(f)
            print(f"[merge] {Path(f).name}: {len(d)} rows, "
                  f"strategies={sorted(d['strategy'].unique())}")
            frames.append(d)
        except Exception as exc:
            print(f"[merge] WARN could not read {f}: {exc!r}")

    if not frames:
        print("[merge] no readable shard files")
        return 1

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["strategy", "window_idx"]).reset_index(drop=True)
    out = paths.OUT_DIR / "wfa_windows.csv"
    df.to_csv(out, index=False)

    summary = wfa.summarize_wfa(df)   # writes OUT_DIR/wfa_summary.csv
    print(f"\n[merge] {len(files)} shards -> {len(df)} window-rows -> {out}")
    print("=" * 88)
    print("WALK-FORWARD ANALYSIS SUMMARY (merged)")
    print("=" * 88)
    print(summary.to_string(index=False) if not summary.empty else "(empty)")
    n_deg = int((df["degraded"] == True).sum()) if "degraded" in df.columns else 0  # noqa: E712
    print(f"\nTotal window-rows: {len(df)} | degraded (>20% OOS PF drop): {n_deg}")

    # Warehouse sidecars: one per merged output, derived lookback from windows.
    from tools.portfolio_backtest.sidecar import emit_sidecar
    lookback_start = str(df["is_start"].min()) if not df.empty else None
    lookback_end = str(df["oos_end"].max()) if not df.empty else None
    shard_names = [Path(f).name for f in files]
    common_params = {
        "shards_merged": shard_names,
        "n_strategies": int(df["strategy"].nunique()),
        "strategies": sorted(df["strategy"].unique().tolist()),
        "n_window_rows": int(len(df)),
    }
    emit_sidecar(
        out,
        strategy=None,
        params=common_params,
        lookback_start=lookback_start, lookback_end=lookback_end,
        friction_per_rt_usd=4.82,
        logical_group="portfolio_wfa",
        notes="merged from shard CSVs; preserves per-window IS/OOS detail",
    )
    summary_path = paths.OUT_DIR / "wfa_summary.csv"
    emit_sidecar(
        summary_path,
        strategy=None,
        params=common_params,
        lookback_start=lookback_start, lookback_end=lookback_end,
        friction_per_rt_usd=4.82,
        logical_group="portfolio_wfa",
        notes="per-strategy robustness summary derived from wfa_windows.csv",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
