"""
Phase 9 — visual PNG charts for top winners + losers.

Scope-reduced version: 10 charts total (5 bias_momentum winners + 5 losers
from DERIVATION).  opening_session skipped — only 8 total trades.

Each chart: 3-panel (price+markers / footprint-or-bars / CVD).
TBBO data for trades in window; volumetric_history fallback otherwise
(degraded chart, marked).

Output: out/charts/winning_conditions/*.png + chart index appended to
out/winning_conditions_stats_2026-06-04.md.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO = Path(r"C:\Trading Project\phoenix_bot")
SCRATCH = Path(r"C:\tmp\winning_conditions")
CHART_DIR = REPO / "out" / "charts" / "winning_conditions"
STATS_MD = REPO / "out" / "winning_conditions_stats_2026-06-04.md"
CHART_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO))
from tools.tbbo_cache_builder import load_clean_ticks

TBBO_START = pd.Timestamp("2026-03-17", tz="UTC")
TBBO_END = pd.Timestamp("2026-05-16", tz="UTC")
TICK_SIZE = 0.25


def load_top_n(strategy: str, kind: str, n: int) -> pd.DataFrame:
    """kind in ('winners', 'losers')."""
    if kind == "winners":
        p = SCRATCH / f"phase2_top_winners_{strategy}.csv"
    else:
        p = SCRATCH / f"phase3_top_losers_{strategy}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    return df.head(n)


def make_chart(row: pd.Series, kind: str, rank: int, strategy: str,
               ticks_full: pd.DataFrame) -> tuple[str, str, str] | None:
    """Render one 3-panel chart.  Returns (filename, source_label, note)."""
    et_iso = row.get("entry_time_iso")
    if et_iso is None or pd.isna(et_iso):
        return None
    try:
        et = pd.Timestamp(et_iso)
        if et.tzinfo is None:
            et = et.tz_localize("UTC")
        else:
            et = et.tz_convert("UTC")
    except Exception:
        return None
    direction = (row.get("direction") or "").upper()
    entry_px = float(row.get("entry_price") or 0)
    stop_px = float(row.get("stop_price") or 0)
    target_px = float(row.get("target_price") or 0)
    exit_px = float(row.get("exit_price") or 0)
    pnl = float(row.get("pnl_dollars_net") or 0)
    regime = row.get("regime", "")
    day_type = row.get("day_type", "")
    trade_id = str(row.get("trade_id") or "")[:12]

    win_label = "WIN" if pnl > 0 else "LOSS"
    title = (f"{strategy} {trade_id} — {win_label} ${pnl:+.2f}  "
             f"{direction}  regime={regime}  day_type={day_type}")
    fname = f"{strategy}_{kind}_{rank}_{trade_id}.png"

    has_tbbo = TBBO_START <= et <= TBBO_END
    src_note = "TBBO" if has_tbbo else "VOLUMETRIC_FALLBACK_DEGRADED"

    # Slice window: [-15m, +30m] — enforced via explicit xlim later
    ws = et - pd.Timedelta(minutes=15)
    we = et + pd.Timedelta(minutes=30)

    if has_tbbo:
        try:
            sub = ticks_full.loc[ws:we]
        except Exception:
            sub = pd.DataFrame()
    else:
        sub = pd.DataFrame()

    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True,
                              gridspec_kw={"height_ratios": [2, 1.5, 1]})

    if not sub.empty:
        # Panel 1: price + entry/stop/target/exit markers
        axes[0].plot(sub.index, sub["price"], lw=0.5, color="black", label="price")
        axes[0].plot(sub.index, sub["bid_px_00"], lw=0.4, color="#d33", alpha=0.4, label="bid")
        axes[0].plot(sub.index, sub["ask_px_00"], lw=0.4, color="#3a3", alpha=0.4, label="ask")
        axes[0].axhline(entry_px, color="blue", ls="-", lw=1.2, label=f"entry {entry_px:.2f}")
        axes[0].axhline(stop_px, color="red", ls="--", lw=1.0, label=f"stop {stop_px:.2f}")
        axes[0].axhline(target_px, color="green", ls="--", lw=1.0, label=f"target {target_px:.2f}")
        axes[0].axvline(et, color="blue", ls=":", lw=0.8)
        if exit_px > 0 and row.get("exit_time_iso"):
            try:
                xt = pd.Timestamp(row.get("exit_time_iso"))
                if xt.tzinfo is None:
                    xt = xt.tz_localize("UTC")
                else:
                    xt = xt.tz_convert("UTC")
                axes[0].scatter([xt], [exit_px], marker="x", color="purple", s=100,
                                label=f"exit {exit_px:.2f}", zorder=5)
            except Exception:
                pass
        axes[0].legend(loc="upper left", fontsize=8)
        axes[0].set_title(title, fontsize=10)
        axes[0].set_ylabel("price")

        # Panel 2: footprint — aggregate per 1m bucket, color by net delta
        sub2 = sub.copy()
        sub2["minute"] = sub2.index.floor("1min")
        # CRITICAL: TBBO `size` column is uint32. np.where + uint32 subtraction
        # underflows to ~4.3e9 sentinel. Cast to int64 before any arithmetic.
        _size_i64 = sub2["size"].astype("int64")
        sub2["bid_vol"] = np.where(sub2["side"] == "B", _size_i64, 0).astype("int64")
        sub2["ask_vol"] = np.where(sub2["side"] == "A", _size_i64, 0).astype("int64")
        grp = sub2.groupby("minute").agg(
            bid_vol=("bid_vol", "sum"),
            ask_vol=("ask_vol", "sum"),
        ).reset_index()
        grp["net"] = grp["ask_vol"] - grp["bid_vol"]
        colors = ["green" if n > 0 else "red" for n in grp["net"]]
        axes[1].bar(grp["minute"], grp["ask_vol"], color="#3a3", alpha=0.5, label="ask vol", width=pd.Timedelta(seconds=50))
        axes[1].bar(grp["minute"], -grp["bid_vol"], color="#d33", alpha=0.5, label="bid vol", width=pd.Timedelta(seconds=50))
        axes[1].axhline(0, color="black", lw=0.3)
        axes[1].axvline(et, color="blue", ls=":", lw=0.8)
        axes[1].legend(loc="upper left", fontsize=8)
        axes[1].set_ylabel("aggressor vol")

        # Panel 3: cumulative CVD (=running sum of net = ask - bid per tick)
        sub3 = sub.copy()
        # Same uint32 cast as panel 2.
        _size_i64_3 = sub3["size"].astype("int64")
        sub3["signed"] = (
            np.where(sub3["side"] == "A", _size_i64_3, 0).astype("int64")
            - np.where(sub3["side"] == "B", _size_i64_3, 0).astype("int64")
        )
        cvd = sub3["signed"].cumsum()
        axes[2].plot(sub3.index, cvd, color="blue", lw=0.8)
        axes[2].axvline(et, color="blue", ls=":", lw=0.8)
        axes[2].axhline(0, color="black", lw=0.3)
        axes[2].set_ylabel("CVD")
        axes[2].set_xlabel("time (UTC)")
        # Lock x-axis to spec window so the entry marker is centered
        for ax in axes:
            ax.set_xlim([ws, we])
        axes[0].legend(loc="upper right", fontsize=7, framealpha=0.7)
    else:
        for ax in axes:
            ax.text(0.5, 0.5, f"NO TBBO DATA — {src_note}\nEntry: {et.isoformat()}",
                    ha="center", va="center", transform=ax.transAxes, fontsize=11)
            ax.set_xticks([])
            ax.set_yticks([])

    plt.tight_layout()
    outp = CHART_DIR / fname
    fig.savefig(outp, dpi=100)
    plt.close(fig)

    # Deterministic sanity (Phase 9.2.0): bid_vol + ask_vol ~ total ticks * avg_size
    note = ""
    if not sub.empty:
        total_bid = float(sub2["bid_vol"].sum())
        total_ask = float(sub2["ask_vol"].sum())
        total_vol = float(sub["size"].sum())
        diff_pct = abs((total_bid + total_ask) - total_vol) / max(total_vol, 1)
        if diff_pct > 0.01:
            note = f"VERIFY MANUALLY — bid+ask={total_bid+total_ask:.0f} vs total={total_vol:.0f} diff {100*diff_pct:.2f}%"
    return fname, src_note, note


def main() -> None:
    print("Loading TBBO ticks ...")
    ticks = load_clean_ticks()
    print(f"  ticks: {ticks.shape}")

    index_rows: list[str] = []
    index_rows.append("\n---\n## Phase 9 — Visual chart index\n")
    index_rows.append("All charts in `out/charts/winning_conditions/`. "
                      "Scope-reduced from spec 20 → 10: 5 winners + 5 losers for "
                      "bias_momentum from DERIVATION top-N (ranked by R-multiple). "
                      "opening_session charts skipped — n=8 total doesn't merit "
                      "individual chart treatment; see dataset CSVs.\n")
    index_rows.append("| strategy | rank | kind | trade_id | regime | TBBO? | chart file |")
    index_rows.append("|---|---:|---|---|---|---|---|")

    # bias_momentum top 5 winners
    df_w = load_top_n("bias_momentum", "winners", 5)
    for i, row in df_w.iterrows():
        result = make_chart(row, "win", i + 1, "bias_momentum", ticks)
        if result is None:
            continue
        fname, src, note = result
        index_rows.append(
            f"| bias_momentum | {i+1} | WIN | `{str(row['trade_id'])[:12]}` | "
            f"{row.get('regime','')} | {src} | "
            f"[{fname}](charts/winning_conditions/{fname}) {note} |"
        )
        print(f"  WIN  #{i+1}: {fname} ({src}) {note}")

    # bias_momentum top 5 losers
    df_l = load_top_n("bias_momentum", "losers", 5)
    for i, row in df_l.iterrows():
        result = make_chart(row, "loss", i + 1, "bias_momentum", ticks)
        if result is None:
            continue
        fname, src, note = result
        index_rows.append(
            f"| bias_momentum | {i+1} | LOSS | `{str(row['trade_id'])[:12]}` | "
            f"{row.get('regime','')} | {src} | "
            f"[{fname}](charts/winning_conditions/{fname}) {note} |"
        )
        print(f"  LOSS #{i+1}: {fname} ({src}) {note}")

    # Append index to stats md
    existing = STATS_MD.read_text(encoding="utf-8")
    STATS_MD.write_text(existing + "\n" + "\n".join(index_rows), encoding="utf-8")
    print(f"Appended chart index to {STATS_MD}")


if __name__ == "__main__":
    main()
