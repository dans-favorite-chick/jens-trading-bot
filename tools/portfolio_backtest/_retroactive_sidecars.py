"""
_retroactive_sidecars.py - One-shot retrofitter that emits warehouse sidecar
JSON for the 10 portfolio_framework CSVs produced before the writer patches
went in on 2026-05-31.

Each sidecar carries:
  notes = "retroactively emitted 2026-05-31; this CSV predates the writer patch"

Knowledge of what's in each CSV (strategies, lookback, friction, grid, etc.)
is reconstructed from the run logs and the framework's known invocations.

Idempotent: re-running just overwrites. Warehouse content-hashes both
versions and keeps the latest as the current run_id.

Usage:
    python tools/portfolio_backtest/_retroactive_sidecars.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.portfolio_backtest import paths  # noqa: E402
from tools.portfolio_backtest.sidecar import emit_sidecar  # noqa: E402


# Strategy lists that drove the original shards (preserved here for the
# warehouse's provenance record). Source: this session's transcript + the
# strategy partitioning baked into the shard launch commands.
_SHARD_A_STRATEGIES = [
    "bias_momentum", "ib_breakout", "vwap_band_reversion",
    "compression_breakout_micro", "vwap_pullback_v2", "big_move_signal",
    "orb_v2", "orb_fade", "spring_setup",
]
_SHARD_B_STRATEGIES = [
    "opening_session", "vwap_band_pullback", "compression_breakout_v2",
    "noise_area", "es_nq_confluence",
]
_RETRO_NOTE = "retroactively emitted 2026-05-31; this CSV predates the writer-patch sidecar contract"


def _wfa_common_params(strategies: list[str], grid: str = "full") -> dict:
    return {
        "strategies": strategies, "start": "2021-05-17", "end": "2026-05-15",
        "is_months": 12, "oos_months": 3, "step_months": 3,
        "grid": grid, "warmup": 300, "apply_friction": True,
    }


def main() -> int:
    out = paths.OUT_DIR
    emitted: list[Path] = []

    # ---- 1) macro_trades.csv ------------------------------------------------
    f = out / "macro_trades.csv"
    if f.exists():
        emitted.append(emit_sidecar(
            f, strategy=None,
            params={"strategies_requested": "all (14 harness + 4 phase13 lab)",
                    "harness_strategies": (
                        "bias_momentum, opening_session, ib_breakout, "
                        "compression_breakout_v2, compression_breakout_micro, "
                        "orb_v2, orb_fade, vwap_pullback_v2, vwap_band_pullback, "
                        "vwap_band_reversion, noise_area, spring_setup, "
                        "big_move_signal, es_nq_confluence"),
                    "phase13_strategies": (
                        "raschke_baseline, g_inside_bar_breakout, "
                        "e_multi_day_breakout, a_asian_continuation"),
                    "start": "2021-05-17", "end": "2026-05-15",
                    "friction_on": True,
                    "merge_source": "macro run + _run_phase13_4 merge"},
            lookback_start="2021-05-17", lookback_end="2026-05-15",
            friction_per_rt_usd=4.82,
            logical_group="portfolio_macro",
            notes=_RETRO_NOTE,
        ))

    # ---- 2) phase13_trades.csv ----------------------------------------------
    f = out / "phase13_trades.csv"
    if f.exists():
        emitted.append(emit_sidecar(
            f, strategy=None,
            params={"strategies": ["raschke_baseline", "g_inside_bar_breakout",
                                    "e_multi_day_breakout", "a_asian_continuation"],
                    "start": "2021-05-17", "end": "2026-05-15",
                    "warmup": 300, "friction_on": True,
                    "driver": "_run_phase13_4.py"},
            lookback_start="2021-05-17", lookback_end="2026-05-15",
            friction_per_rt_usd=4.82,
            logical_group="phase13_trades",
            notes=_RETRO_NOTE,
        ))

    # ---- 3-4) wfa_windows_shardA.csv / shardB.csv --------------------------
    f = out / "wfa_windows_shardA.csv"
    if f.exists():
        emitted.append(emit_sidecar(
            f, strategy=None,
            params=_wfa_common_params(_SHARD_A_STRATEGIES),
            lookback_start="2021-05-17", lookback_end="2026-05-15",
            friction_per_rt_usd=4.82,
            logical_group="portfolio_wfa",
            notes=_RETRO_NOTE,
        ))
    f = out / "wfa_windows_shardB.csv"
    if f.exists():
        emitted.append(emit_sidecar(
            f, strategy=None,
            params=_wfa_common_params(_SHARD_B_STRATEGIES),
            lookback_start="2021-05-17", lookback_end="2026-05-15",
            friction_per_rt_usd=4.82,
            logical_group="portfolio_wfa",
            notes=_RETRO_NOTE,
        ))

    # ---- 5) wfa_windows.csv (merged from shardA + shardB; 14 strategies) ---
    f = out / "wfa_windows.csv"
    if f.exists():
        merged_strats = sorted(_SHARD_A_STRATEGIES + _SHARD_B_STRATEGIES)
        emitted.append(emit_sidecar(
            f, strategy=None,
            params={"shards_merged": ["wfa_windows_shardA.csv",
                                       "wfa_windows_shardB.csv"],
                     "n_strategies": len(merged_strats),
                     "strategies": merged_strats,
                     "start": "2021-05-17", "end": "2026-05-15",
                     "grid": "full"},
            lookback_start="2021-05-17", lookback_end="2026-05-15",
            friction_per_rt_usd=4.82,
            logical_group="portfolio_wfa",
            notes=_RETRO_NOTE + "; PRE-multi_day; Phase 13 strategies not yet included",
        ))

    # ---- 6) wfa_summary.csv (derived from wfa_windows.csv) -----------------
    f = out / "wfa_summary.csv"
    if f.exists():
        merged_strats = sorted(_SHARD_A_STRATEGIES + _SHARD_B_STRATEGIES)
        emitted.append(emit_sidecar(
            f, strategy=None,
            params={"source": "wfa_windows.csv",
                     "n_strategies": len(merged_strats),
                     "strategies": merged_strats,
                     "robust_gate": "pct_degraded <= 0.34 AND mean_oos_pf >= 1.30"},
            lookback_start="2021-05-17", lookback_end="2026-05-15",
            friction_per_rt_usd=4.82,
            logical_group="portfolio_wfa",
            notes=_RETRO_NOTE + "; PRE-multi_day",
        ))

    # ---- 7-9) wfa_windows_p13_*.csv (one strategy each, 9-combo full grid) -
    for name, strat in [
        ("wfa_windows_p13_raschke.csv", "raschke_baseline"),
        ("wfa_windows_p13_inside_bar.csv", "g_inside_bar_breakout"),
        ("wfa_windows_p13_asian.csv", "a_asian_continuation"),
        # multi_day intentionally omitted -- shard still running at retrofit time
    ]:
        f = out / name
        if f.exists():
            emitted.append(emit_sidecar(
                f, strategy=strat,
                params=_wfa_common_params([strat]),
                lookback_start="2021-05-17", lookback_end="2026-05-15",
                friction_per_rt_usd=4.82,
                logical_group="phase13_wfa",
                notes=_RETRO_NOTE,
            ))

    print(f"[retro] emitted {len(emitted)} sidecar JSON files:")
    for p in emitted:
        print(f"  {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
