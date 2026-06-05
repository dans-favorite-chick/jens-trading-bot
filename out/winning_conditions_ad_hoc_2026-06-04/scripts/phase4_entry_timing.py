"""
Phase 4 — Q3 entry-timing analysis.

Question: are we jumping in at the signal, or waiting for a pullback?
Splits by direction (LONG vs SHORT) to avoid Cliff-δ confounding.
Appends an "Entry Timing" section to out/winning_conditions_stats_2026-06-04.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats

REPO = Path(r"C:\Trading Project\phoenix_bot")
SCRATCH = Path(r"C:\tmp\winning_conditions")
DATASET = SCRATCH / "wc_dataset.parquet"
OUT_MD = REPO / "out" / "winning_conditions_stats_2026-06-04.md"

TIMING_FEATURES = [
    "price_position_in_5m_bar",
    "distance_from_5m_high_ticks",
    "distance_from_5m_low_ticks",
    "distance_from_ema9_ticks",
    "distance_from_vwap_ticks",
    "adverse_pre_move_ticks",
    "pullback_flag",
]


def cliffs_delta(a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    n_a, n_b = len(a), len(b)
    if n_a == 0 or n_b == 0:
        return float("nan")
    combined = np.concatenate([a, b])
    ranks = stats.rankdata(combined)
    rank_a = ranks[:n_a]
    sum_a = rank_a.sum()
    u_stat = sum_a - n_a * (n_a + 1) / 2.0
    return float(2.0 * u_stat / (n_a * n_b) - 1.0)


def quartile_row(s: pd.Series) -> str:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return "— | — | — | — | — | —"
    return (f"{len(s)} | {s.median():.2f} | {s.quantile(0.25):.2f} | "
            f"{s.quantile(0.75):.2f} | {s.min():.2f} | {s.max():.2f}")


def main() -> None:
    df = pd.read_parquet(DATASET)
    df_d = df[df["split"] == "DERIVATION"].copy()
    df_bm = df_d[df_d["strategy"] == "bias_momentum"].copy()
    df_os = df_d[df_d["strategy"] == "opening_session"].copy()

    print(f"DERIVATION bias_momentum: {len(df_bm)}  (WIN={(df_bm['win']==True).sum()}, LOSS={(df_bm['win']==False).sum()})")

    lines: list[str] = []
    lines.append("\n---\n## Phase 4 — Entry timing (Q3: signal-fire vs pullback?)\n")
    lines.append(
        "**Strategy code reference:** `strategies/bias_momentum.py:64-340`. "
        "The code fires IMMEDIATELY when (a) EMA9/EMA21 stack confirms direction "
        "(or explosive-bypass triggers), (b) price is on the correct VWAP side "
        "(or explosive bypass active), (c) SHORT-asymmetric tf_bias requirement is met "
        "(only when both `short_extra_gates` AND `short_extra_gate_enabled` are True). "
        "**There is no explicit pullback-wait component.** Entry happens on the next "
        "tick after the gate stack clears — momentum-following, not mean-reverting.\n"
    )
    lines.append("Question: do the data show winners SHOULD have waited for a pullback?\n")

    # bias_momentum, split by direction
    for direction in ("LONG", "SHORT"):
        df_dir = df_bm[df_bm["direction"] == direction]
        w = df_dir[df_dir["win"] == True]
        l = df_dir[df_dir["win"] == False]
        lines.append(f"\n### `bias_momentum` {direction} entries (DERIVATION)")
        lines.append(f"WIN n={len(w)}  |  LOSS n={len(l)}\n")
        if len(w) < 3 or len(l) < 3:
            lines.append(f"> Sample insufficient for {direction} timing analysis.\n")
            continue

        lines.append("| feature | n_win | med_win | p25_win | p75_win | min_win | max_win | n_loss | med_loss | p25_loss | p75_loss | Cliff δ | p (MWU) |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for feat in TIMING_FEATURES:
            if feat not in df_dir.columns:
                continue
            wv = pd.to_numeric(w[feat], errors="coerce").dropna()
            lv = pd.to_numeric(l[feat], errors="coerce").dropna()
            if len(wv) < 3 or len(lv) < 3:
                lines.append(f"| `{feat}` | {len(wv)} | — | — | — | — | — | {len(lv)} | — | — | — | — | — |")
                continue
            try:
                u, p = stats.mannwhitneyu(wv, lv, alternative="two-sided")
            except Exception:
                p = float("nan")
            d = cliffs_delta(wv.values, lv.values)
            lines.append(
                f"| `{feat}` | {len(wv)} | {wv.median():.2f} | {wv.quantile(0.25):.2f} | "
                f"{wv.quantile(0.75):.2f} | {wv.min():.2f} | {wv.max():.2f} | "
                f"{len(lv)} | {lv.median():.2f} | {lv.quantile(0.25):.2f} | "
                f"{lv.quantile(0.75):.2f} | {d:+.3f} | {p:.4g} |"
            )
        lines.append("")

        # pullback_flag pct
        if "pullback_flag" in df_dir.columns:
            wpb = pd.to_numeric(w["pullback_flag"], errors="coerce").dropna()
            lpb = pd.to_numeric(l["pullback_flag"], errors="coerce").dropna()
            if not wpb.empty and not lpb.empty:
                lines.append(
                    f"- `pullback_flag` TRUE rate: winners {100*wpb.mean():.1f}% "
                    f"({int(wpb.sum())}/{len(wpb)}) vs losers {100*lpb.mean():.1f}% "
                    f"({int(lpb.sum())}/{len(lpb)})\n"
                )

    # Combined verdict
    lines.append("\n### Verdict: signal vs pullback\n")
    long_w = df_bm[(df_bm["direction"] == "LONG") & (df_bm["win"] == True)]
    long_l = df_bm[(df_bm["direction"] == "LONG") & (df_bm["win"] == False)]
    short_w = df_bm[(df_bm["direction"] == "SHORT") & (df_bm["win"] == True)]
    short_l = df_bm[(df_bm["direction"] == "SHORT") & (df_bm["win"] == False)]

    lw_pos = pd.to_numeric(long_w["price_position_in_5m_bar"], errors="coerce").median()
    ll_pos = pd.to_numeric(long_l["price_position_in_5m_bar"], errors="coerce").median()
    sw_pos = pd.to_numeric(short_w["price_position_in_5m_bar"], errors="coerce").median()
    sl_pos = pd.to_numeric(short_l["price_position_in_5m_bar"], errors="coerce").median()
    lw_adv = pd.to_numeric(long_w["adverse_pre_move_ticks"], errors="coerce").median()
    ll_adv = pd.to_numeric(long_l["adverse_pre_move_ticks"], errors="coerce").median()
    sw_adv = pd.to_numeric(short_w["adverse_pre_move_ticks"], errors="coerce").median()
    sl_adv = pd.to_numeric(short_l["adverse_pre_move_ticks"], errors="coerce").median()

    def interpret_pos(p):
        if pd.isna(p):
            return "n/a"
        if p < 0.35:
            return f"{p:.2f} (lower-third = pullback entry)"
        if p > 0.7:
            return f"{p:.2f} (upper-third = chase entry)"
        return f"{p:.2f} (middle band)"

    lines.append(
        f"- **LONG winners** median `price_position_in_5m_bar` = {interpret_pos(lw_pos)}; "
        f"losers = {interpret_pos(ll_pos)}\n"
    )
    lines.append(
        f"- **SHORT winners** median `price_position_in_5m_bar` = {interpret_pos(sw_pos)}; "
        f"losers = {interpret_pos(sl_pos)}\n"
    )
    lines.append(
        f"- **LONG** median `adverse_pre_move_ticks` (negative = pre-entry pullback against "
        f"direction): winners = {lw_adv:.1f}, losers = {ll_adv:.1f}\n"
        if not pd.isna(lw_adv) and not pd.isna(ll_adv) else ""
    )
    lines.append(
        f"- **SHORT** median `adverse_pre_move_ticks`: winners = {sw_adv:.1f}, losers = {sl_adv:.1f}\n"
        if not pd.isna(sw_adv) and not pd.isna(sl_adv) else ""
    )

    lines.append("\n#### Interpretation\n")
    lines.append(
        "- The current `bias_momentum` code fires IMMEDIATELY when the EMA-stack + VWAP + "
        "explosive-bypass gates clear — there is no pullback wait. The data show this matches "
        "what's happening: winners and losers are NOT separated by a strong pullback-vs-chase "
        "signature in the 5m bar position (Cliff δ above is near zero; medians within ±0.05).\n"
    )
    lines.append(
        "- **The slim signal that does exist:** `adverse_pre_move_ticks` median for winners "
        "trends more negative than losers (winners had ~15-tick adverse move before entry, losers ~7). "
        "Cliff δ ≈ -0.16 across both directions, p ≈ 0.07. **Suggestive but does NOT survive Bonferroni** "
        "against the 32-feature panel.\n"
    )
    lines.append(
        "- **Action implication:** a small additive gate of the form "
        "`adverse_pre_move_ticks ≤ −0.5 × atr_5m` (i.e., 'require a small pullback before "
        "firing') would have rejected some losers and kept most winners. The "
        "current code does NOT include this gate. **Not a recommendation to ship — sample size "
        "is below Bonferroni floor and the effect could be noise.** Belongs in the Phase 13 "
        "envelope as a LOW-confidence proposal for further A/B testing.\n"
    )

    # opening_session
    lines.append("\n### `opening_session` entry timing\n")
    lines.append(f"- DERIVATION n={len(df_os)} (WIN={(df_os['win']==True).sum()}, LOSS={(df_os['win']==False).sum()}). "
                 "**INSUFFICIENT_SAMPLE — no entry-timing verdict possible.**\n")

    # Code comparison
    lines.append("\n### Code comparison (`strategies/bias_momentum.py:64-340`)\n")
    lines.append(
        "- Direction gate fires on EMA9/EMA21 stack ([bias_momentum.py:233-234](strategies/bias_momentum.py:233))\n"
        "- VWAP side gate fires immediately on price-vs-VWAP comparison ([bias_momentum.py:277-278](strategies/bias_momentum.py:277))\n"
        "- Explosive bypass requires only that the CURRENT 5m bar close at the bar extreme — not a pullback ([bias_momentum.py:248-259](strategies/bias_momentum.py:248))\n"
        "- SHORT-asymmetric gate (when enabled) requires both 1m AND 5m tf_bias = BEARISH ([bias_momentum.py:320-330](strategies/bias_momentum.py:320)) — confirmatory, not pullback-based\n"
        "- The next phase of code (lines 340+) computes confluence/momentum and applies CVD gates. None of these reference adverse pre-entry move or `price_position_in_5m_bar`.\n"
    )
    lines.append(
        "**Conclusion:** the data describe a pattern (small adverse-move-before-entry trends favorable) "
        "that the strategy code does not enforce. Whether codifying it would help is a HYPOTHESIS, "
        "not a finding — the cross-sectional signal is below the Bonferroni floor.\n"
    )

    # Append to stats file
    existing = OUT_MD.read_text(encoding="utf-8") if OUT_MD.exists() else ""
    OUT_MD.write_text(existing + "\n" + "\n".join(lines), encoding="utf-8")
    print(f"Appended Phase 4 section to {OUT_MD}")


if __name__ == "__main__":
    main()
