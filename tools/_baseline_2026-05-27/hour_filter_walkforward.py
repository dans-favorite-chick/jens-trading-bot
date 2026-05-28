"""
Walk-forward validation of the time-of-day filter hypothesis + final window export.

Question: does selecting friction-positive 30-min CT windows on HISTORY and
trading them FORWARD produce robust out-of-sample net profit (after the exact
$2.82/RT commission), or is it data-snooping that fails OOS?

Method (expanding-window walk-forward, Lopez de Prado style):
  For each TEST year in {2023, 2024, 2025, 2026}:
    - TRAIN = all trades strictly before that year
    - Keep buckets where TRAIN avg gross/trade > $2.82 AND train n >= 20
    - Apply that fixed keep-set to the TEST year; record net (all vs filtered)
  A rescue is ROBUST only if filtered beats all-hours in a MAJORITY of folds
  AND the summed OOS filtered net is positive.

Then, for strategies that pass, export the final recommended windows (buckets
selected on the FULL 5y, merged into contiguous CT ranges) as a config proposal
(NOT applied — FREEZE_ACTIVE).

Outputs (out/_baseline_2026-05-27/hour_filter/):
  walkforward_folds.csv      - per (strategy, test_year): all vs filtered net
  recommended_windows.md     - session_windows_ct proposal per passing strategy
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("C:/Trading Project/phoenix_bot")
OUT = ROOT / "out" / "_baseline_2026-05-27" / "hour_filter"
OUT.mkdir(parents=True, exist_ok=True)

COMM = 2.82          # exact round-turn commission, $/contract
MIN_TRAIN_N = 20     # min train samples in a bucket to trust its selection
TEST_YEARS = [2023, 2024, 2025, 2026]

CANDIDATES = ["vwap_band_reversion", "spring_setup", "vwap_pullback_v2",
              "orb_fade", "ib_breakout", "compression_breakout_v2"]


def _bucket_to_min(b: str) -> int:
    h, m = b.split(":"); return int(h) * 60 + int(m)


def _min_to_bucket(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _merge(buckets: list[str]) -> list[tuple[str, str]]:
    if not buckets:
        return []
    mins = sorted(_bucket_to_min(b) for b in buckets)
    out, s, e = [], mins[0], mins[0] + 30
    for m in mins[1:]:
        if m == e:
            e = m + 30
        else:
            out.append((s, e)); s, e = m, m + 30
    out.append((s, e))
    return [(_min_to_bucket(a), _min_to_bucket(min(b, 1439))) for a, b in out]


def load() -> pd.DataFrame:
    frames = [pd.read_csv(ROOT / p) for p in [
        "backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv",
        "backtest_results/phoenix_new_strategy_lab.csv",
        "backtest_results/phoenix_trend_pullback_lab.csv",
    ]]
    df = pd.concat(frames, ignore_index=True)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["entry_ts"])
    ct = df["entry_ts"].dt.tz_convert("America/Chicago")
    df["bucket"] = ct.dt.hour.astype(str).str.zfill(2) + ":" + np.where(ct.dt.minute < 30, "00", "30")
    df["year"] = ct.dt.year
    return df


def keep_buckets(train: pd.DataFrame) -> set:
    g = train.groupby("bucket").agg(n=("pnl_dollars", "size"), gross=("pnl_dollars", "sum"))
    g["avg"] = g.gross / g.n
    return set(g[(g.avg > COMM) & (g.n >= MIN_TRAIN_N)].index)


def main():
    df = load()
    fold_rows = []
    summary = {}

    for s in CANDIDATES:
        sdf = df[df.strategy == s]
        folds_filt_beats_all = 0
        folds_filt_positive = 0
        n_folds = 0
        sum_all = sum_filt = 0.0
        for ty in TEST_YEARS:
            train = sdf[sdf.year < ty]
            test = sdf[sdf.year == ty]
            if len(train) < 50 or len(test) < 20:
                continue
            keep = keep_buckets(train)
            all_net = test.pnl_dollars.sum() - len(test) * COMM
            tk = test[test.bucket.isin(keep)]
            filt_net = tk.pnl_dollars.sum() - len(tk) * COMM
            n_folds += 1
            sum_all += all_net
            sum_filt += filt_net
            if filt_net > all_net:
                folds_filt_beats_all += 1
            if filt_net > 0:
                folds_filt_positive += 1
            fold_rows.append({
                "strategy": s, "test_year": ty,
                "test_n_all": len(test), "test_n_filtered": len(tk),
                "all_net": round(all_net, 0), "filtered_net": round(filt_net, 0),
                "n_keep_buckets": len(keep),
            })
        if n_folds == 0:
            summary[s] = {"verdict": "INSUFFICIENT_DATA"}
            continue
        robust = (folds_filt_beats_all >= (n_folds + 1) // 2 + (1 if n_folds % 2 == 0 else 0)) and sum_filt > 0
        # Majority = strictly more than half
        robust = (folds_filt_beats_all > n_folds / 2) and (sum_filt > 0)
        summary[s] = {
            "n_folds": n_folds,
            "folds_filt_beats_all": folds_filt_beats_all,
            "folds_filt_positive": folds_filt_positive,
            "sum_all_net": round(sum_all, 0),
            "sum_filtered_net": round(sum_filt, 0),
            "verdict": "ROBUST" if robust else "NOT ROBUST",
        }

    folds = pd.DataFrame(fold_rows)
    folds.to_csv(OUT / "walkforward_folds.csv", index=False)

    # ── Console report ──
    print(f"WALK-FORWARD (expanding window) | commission floor ${COMM}/trade\n")
    print(f"{'strategy':<24}{'folds':>6}{'filt>all':>9}{'filt>0':>8}{'sumAll':>9}{'sumFilt':>9}  verdict")
    for s in CANDIDATES:
        v = summary[s]
        if v.get("verdict") == "INSUFFICIENT_DATA":
            print(f"{s:<24}  INSUFFICIENT_DATA"); continue
        print(f"{s:<24}{v['n_folds']:>6}{v['folds_filt_beats_all']:>9}{v['folds_filt_positive']:>8}"
              f"{v['sum_all_net']:>9.0f}{v['sum_filtered_net']:>9.0f}  {v['verdict']}")
    print("\nPer-fold detail:")
    print(folds.to_string(index=False))

    # ── Final recommended windows (full-5y selection) for ROBUST strategies ──
    md = ["# Time-of-Day Filter — Walk-Forward Result + Recommended Windows", "",
          f"Commission floor ${COMM}/trade (exact). Expanding-window walk-forward over "
          f"test years {TEST_YEARS}. A strategy is ROBUST only if the hour-filter beat "
          "all-hours in a majority of OOS folds AND summed OOS net is positive.", "",
          "## Walk-forward verdicts", "",
          "| Strategy | OOS folds | filt>all | filt>0 | sum all-hrs | sum filtered | Verdict |",
          "|---|---:|---:|---:|---:|---:|---|"]
    for s in CANDIDATES:
        v = summary[s]
        if v.get("verdict") == "INSUFFICIENT_DATA":
            md.append(f"| {s} | — | — | — | — | — | INSUFFICIENT_DATA |"); continue
        md.append(f"| {s} | {v['n_folds']} | {v['folds_filt_beats_all']} | {v['folds_filt_positive']} | "
                  f"${v['sum_all_net']:,.0f} | ${v['sum_filtered_net']:,.0f} | **{v['verdict']}** |")
    md += ["", "## Recommended windows (proposal only — NOT applied, FREEZE_ACTIVE)", ""]
    for s in CANDIDATES:
        if summary[s].get("verdict") != "ROBUST":
            continue
        sdf = df[df.strategy == s]
        keep = keep_buckets(sdf)  # full-5y selection for the final window
        windows = _merge(sorted(keep))
        # full-5y net under this window selection
        kept = sdf[sdf.bucket.isin(keep)]
        net_all = sdf.pnl_dollars.sum() - len(sdf) * COMM
        net_win = kept.pnl_dollars.sum() - len(kept) * COMM
        md.append(f"### {s}")
        md.append(f"- Proposed `session_windows_ct`: `{windows}`")
        md.append(f"- 5y net: all-hours ${net_all:,.0f} -> windowed ${net_win:,.0f} "
                  f"({len(kept)}/{len(sdf)} trades kept)")
        md.append("")
    (OUT / "recommended_windows.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n[write] {OUT/'walkforward_folds.csv'}\n[write] {OUT/'recommended_windows.md'}")


if __name__ == "__main__":
    main()
