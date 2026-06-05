"""
Phase 2 + Phase 3 — WIN / LOSS condition analysis + discriminators.

Operates on the DERIVATION SET only (Phase 0.7 lock).

Output: appends sections to out/winning_conditions_stats_2026-06-04.md.
Also writes:
  /c/tmp/winning_conditions/phase2_3_winners.csv
  /c/tmp/winning_conditions/phase2_3_losers.csv
  /c/tmp/winning_conditions/phase2_3_discriminators_bias_momentum.csv
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

REPO = Path(r"C:\Trading Project\phoenix_bot")
SCRATCH = Path(r"C:\tmp\winning_conditions")
DATASET = SCRATCH / "wc_dataset.parquet"
OUT_MD = REPO / "out" / "winning_conditions_stats_2026-06-04.md"

# Features to analyze for distribution + discriminator analysis.
NUMERIC_FEATURES = [
    # Snapshot — environmental
    "atr_1m", "atr_5m", "atr_15m", "atr_60m",
    "cvd", "bar_delta", "bar_buy_vol", "bar_sell_vol",
    "vol_climax_ratio",
    "tf_votes_bullish", "tf_votes_bearish",
    "cr_mom_score", "cr_confidence",  # momentum_score / precision proxies
    "es_nq_rs",
    "dom_imbalance",
    "vwap_std",
    # Distance / position features (computed from snapshot)
    "distance_from_ema9_ticks", "distance_from_vwap_ticks",
    # TBBO tick-window features
    "tick_count_5m_before", "tick_count_5m_after",
    "bid_volume_5m_before", "ask_volume_5m_before",
    "delta_aligned_ratio_5m",
    "spread_avg_5m", "spread_max_5m",
    "price_range_5m_ticks",
    "price_position_in_5m_bar",
    "distance_from_5m_high_ticks", "distance_from_5m_low_ticks",
    "cvd_slope_5m_per_min", "cvd_slope_1m", "cvd_acceleration",
    "adverse_pre_move_ticks",
    # Trade-level
    "hold_time_s", "contracts",
]

CATEGORICAL_FEATURES = [
    "regime", "day_type", "cr_verdict", "cr_direction",
    "mq_direction_bias", "vsa_signal_5m",
    "direction",
    "session_phase_ct",
    "split",
    "pullback_flag",
    "cvd_health_veto",
    "dom_bid_heavy", "dom_ask_heavy",
    "cr_at_resistance", "cr_at_support",
]


def cliffs_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Cliff's delta effect size. Range [-1, 1]. Positive => a > b on average."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    n_a, n_b = len(a), len(b)
    if n_a == 0 or n_b == 0:
        return float("nan")
    # Use the rank-based equivalent for speed
    combined = np.concatenate([a, b])
    ranks = stats.rankdata(combined)
    rank_a = ranks[:n_a]
    # mean rank of a → relate to delta
    sum_a = rank_a.sum()
    u_stat = sum_a - n_a * (n_a + 1) / 2.0
    # Cliff's delta = 2 * U / (n_a * n_b) - 1
    return float(2.0 * u_stat / (n_a * n_b) - 1.0)


def describe(s: pd.Series) -> dict[str, float]:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {"n": 0, "mean": float("nan"), "median": float("nan"),
                "p25": float("nan"), "p75": float("nan"),
                "min": float("nan"), "max": float("nan"),
                "skew": float("nan"), "std": float("nan")}
    return {
        "n": int(len(s)),
        "mean": float(s.mean()),
        "median": float(s.median()),
        "p25": float(s.quantile(0.25)),
        "p75": float(s.quantile(0.75)),
        "min": float(s.min()),
        "max": float(s.max()),
        "skew": float(s.skew()) if len(s) >= 3 else float("nan"),
        "std": float(s.std()) if len(s) >= 2 else float("nan"),
    }


def cat_distribution(df_subset: pd.DataFrame, col: str) -> pd.Series:
    if col not in df_subset.columns:
        return pd.Series([], dtype="int64")
    vc = df_subset[col].fillna("<NULL>").astype(str).value_counts()
    return vc


def fmt_pct(n: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{100*n/total:.1f}%"


def render_dist_table(stats_dict: dict[str, dict[str, float]]) -> str:
    lines = ["| feature | n | mean | median | p25 | p75 | min | max | skew |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for feat, s in stats_dict.items():
        if s["n"] == 0:
            lines.append(f"| `{feat}` | 0 | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| `{feat}` | {s['n']} | {s['mean']:.3f} | {s['median']:.3f} | "
            f"{s['p25']:.3f} | {s['p75']:.3f} | {s['min']:.3f} | {s['max']:.3f} | "
            f"{s['skew']:.2f} |"
        )
    return "\n".join(lines)


def render_cat_table(name: str, vc: pd.Series, total: int) -> str:
    lines = [f"**{name}** (n={total}):", "", "| value | count | % |",
             "|---|---:|---:|"]
    for val, n in vc.items():
        lines.append(f"| {val} | {n} | {fmt_pct(int(n), total)} |")
    return "\n".join(lines)


def main() -> None:
    df = pd.read_parquet(DATASET)
    print(f"Loaded dataset: {df.shape}")

    # Filter to DERIVATION
    df_d = df[df["split"] == "DERIVATION"].copy()
    print(f"DERIVATION subset: {df_d.shape}")
    print(df_d.groupby("strategy").size().to_string())

    sections: list[str] = []
    sections.append("# Winning Conditions Sprint — Stats Report")
    sections.append(f"*Phases 2 / 3 / 4 deliverable | 2026-06-04*\n")
    sections.append("Analysis is restricted to the **DERIVATION SET**")
    sections.append("(2026-03-06 → 2026-05-05, oldest 60 days of the 90-day rich-data window).")
    sections.append("The HOLDOUT SET (2026-05-05 → 2026-06-04) is reserved untouched for the Phase 13.6 in-window WFA.\n")
    sections.append("Sample sizes per strategy in DERIVATION:\n")
    grp = df_d.groupby("strategy").size()
    for s, n in grp.items():
        sections.append(f"- `{s}`: {n}\n")

    # =====================================================================
    # Phase 2 — WIN conditions per strategy
    # =====================================================================
    sections.append("\n---\n## Phase 2 — Win conditions\n")

    for strat in ("bias_momentum", "opening_session"):
        df_s = df_d[df_d["strategy"] == strat].copy()
        df_w = df_s[df_s["win"] == True].copy()
        df_l = df_s[df_s["win"] == False].copy()
        sections.append(f"### `{strat}` — DERIVATION winners")
        sections.append(f"- Total DERIVATION trades: {len(df_s)} (WIN={len(df_w)}, LOSS={len(df_l)})\n")

        if len(df_w) < 5:
            sections.append("> **INSUFFICIENT_SAMPLE** — fewer than 5 winners in DERIVATION; "
                            "distribution stats are descriptive only and statistical tests are declined.\n")

        # 2.2 Numeric distributions across winners
        win_stats = {feat: describe(df_w[feat]) if feat in df_w.columns else describe(pd.Series([]))
                     for feat in NUMERIC_FEATURES}
        sections.append("#### Winner numeric distributions\n")
        sections.append(render_dist_table(win_stats))
        sections.append("")

        # 2.3 Regime / day_type / session_phase tabulation
        sections.append("\n#### Top regimes / day_types / session_phases for winners\n")
        for col in ("regime", "day_type", "session_phase_ct", "cr_verdict", "mq_direction_bias"):
            vc = cat_distribution(df_w, col).head(10)
            if not vc.empty:
                sections.append(render_cat_table(col, vc, len(df_w)))
                sections.append("")

        # 2.4 Ideal-winner profile (paragraph)
        if not df_w.empty:
            median_atr = df_w["atr_5m"].median(skipna=True) if "atr_5m" in df_w.columns else float("nan")
            median_dist_vwap = df_w["distance_from_vwap_ticks"].median(skipna=True) if "distance_from_vwap_ticks" in df_w.columns else float("nan")
            median_dist_ema = df_w["distance_from_ema9_ticks"].median(skipna=True) if "distance_from_ema9_ticks" in df_w.columns else float("nan")
            median_aligned = df_w["delta_aligned_ratio_5m"].median(skipna=True) if "delta_aligned_ratio_5m" in df_w.columns else float("nan")
            median_pos = df_w["price_position_in_5m_bar"].median(skipna=True) if "price_position_in_5m_bar" in df_w.columns else float("nan")
            median_mom = df_w["cr_mom_score"].median(skipna=True) if "cr_mom_score" in df_w.columns else float("nan")
            top_regime = cat_distribution(df_w, "regime")
            top_regime_str = top_regime.index[0] if len(top_regime) else "n/a"
            top_regime_pct = fmt_pct(int(top_regime.iloc[0]), len(df_w)) if len(top_regime) else "n/a"
            sections.append(
                f"#### Ideal `{strat}` winner profile\n\n"
                f"The median DERIVATION winner of `{strat}` entered at ATR(5m) = {median_atr:.2f} "
                f"points, at {median_dist_vwap:+.1f} ticks vs VWAP and {median_dist_ema:+.1f} ticks vs EMA9. "
                f"5-min aggressor-aligned ratio was {median_aligned:.3f} "
                f"(values above 0.5 = aggressor pressure aligned with trade direction). "
                f"Entry sat at position {median_pos:.2f} of the prior 5-min bar (0 = bar low, 1 = bar high). "
                f"cr_mom_score median = {median_mom if not np.isnan(median_mom) else float('nan'):.1f} "
                f"(proxy for the deprecated momentum_score). "
                f"The most common regime for winners was `{top_regime_str}` ({top_regime_pct}).\n"
            )

        # 2.5 Top 5 winners by computed R-multiple
        if len(df_w) >= 1:
            ranked = df_w.copy()
            ranked["rank_key_r"] = pd.to_numeric(ranked["r_multiple_computed"], errors="coerce")
            ranked["rank_key_pnl"] = pd.to_numeric(ranked["pnl_dollars_net"], errors="coerce")
            ranked = ranked.sort_values(
                by=["rank_key_r", "rank_key_pnl"], ascending=[False, False]
            )
            top_n = min(5, len(ranked))
            top = ranked.head(top_n)
            sections.append(f"\n#### Top {top_n} `{strat}` winners (DERIVATION, ranked by R-multiple)\n")
            sections.append("| rank | trade_id | entry_iso | dir | entry | exit | stop | pnl_$net | R | regime | day_type |")
            sections.append("|---:|---|---|---|---:|---:|---:|---:|---:|---|---|")
            for i, (_, r) in enumerate(top.iterrows(), 1):
                rmult = r.get("r_multiple_computed")
                rmult_s = f"{rmult:.2f}" if pd.notna(rmult) else "—"
                sections.append(
                    f"| {i} | `{str(r['trade_id'])[:12]}` | {str(r['entry_time_iso'])[:19]} | "
                    f"{r.get('direction','—')} | {r.get('entry_price','—')} | {r.get('exit_price','—')} | "
                    f"{r.get('stop_price','—')} | "
                    f"${float(r.get('pnl_dollars_net') or 0):+.2f} | {rmult_s} | "
                    f"{r.get('regime','—')} | {r.get('day_type','—')} |"
                )
            sections.append("")
            # save for later use by Phase 9
            top.to_csv(SCRATCH / f"phase2_top_winners_{strat}.csv", index=False)

    # =====================================================================
    # Phase 3 — LOSS conditions + Mann-Whitney U discriminators
    # =====================================================================
    sections.append("\n---\n## Phase 3 — Loss conditions + discriminators\n")

    discriminator_rows: list[dict[str, Any]] = []

    for strat in ("bias_momentum", "opening_session"):
        df_s = df_d[df_d["strategy"] == strat].copy()
        df_w = df_s[df_s["win"] == True]
        df_l = df_s[df_s["win"] == False]
        sections.append(f"### `{strat}` — DERIVATION losers")
        sections.append(f"- Total DERIVATION trades: {len(df_s)} (WIN={len(df_w)}, LOSS={len(df_l)})\n")

        if len(df_l) < 5 or len(df_w) < 5:
            sections.append("> **INSUFFICIENT_SAMPLE** — Mann-Whitney comparison declined; "
                            "min(WIN, LOSS) < 5. Descriptive stats below only.\n")

        loss_stats = {feat: describe(df_l[feat]) if feat in df_l.columns else describe(pd.Series([]))
                      for feat in NUMERIC_FEATURES}
        sections.append("#### Loser numeric distributions\n")
        sections.append(render_dist_table(loss_stats))
        sections.append("")

        sections.append("\n#### Top regimes / day_types / session_phases for losers\n")
        for col in ("regime", "day_type", "session_phase_ct", "cr_verdict"):
            vc = cat_distribution(df_l, col).head(10)
            if not vc.empty:
                sections.append(render_cat_table(col, vc, len(df_l)))
                sections.append("")

        # 3.5 Top 5 losers by abs(R-multiple)
        if len(df_l) >= 1:
            ranked = df_l.copy()
            ranked["abs_rmult"] = pd.to_numeric(ranked["r_multiple_computed"], errors="coerce").abs()
            ranked["abs_pnl"] = pd.to_numeric(ranked["pnl_dollars_net"], errors="coerce").abs()
            ranked = ranked.sort_values(by=["abs_rmult", "abs_pnl"], ascending=[False, False])
            top_n = min(5, len(ranked))
            top = ranked.head(top_n)
            sections.append(f"\n#### Top {top_n} `{strat}` losers (DERIVATION, ranked by |R-multiple|)\n")
            sections.append("| rank | trade_id | entry_iso | dir | entry | exit | stop | pnl_$net | R | regime | day_type |")
            sections.append("|---:|---|---|---|---:|---:|---:|---:|---:|---|---|")
            for i, (_, r) in enumerate(top.iterrows(), 1):
                rmult = r.get("r_multiple_computed")
                rmult_s = f"{rmult:.2f}" if pd.notna(rmult) else "—"
                sections.append(
                    f"| {i} | `{str(r['trade_id'])[:12]}` | {str(r['entry_time_iso'])[:19]} | "
                    f"{r.get('direction','—')} | {r.get('entry_price','—')} | {r.get('exit_price','—')} | "
                    f"{r.get('stop_price','—')} | "
                    f"${float(r.get('pnl_dollars_net') or 0):+.2f} | {rmult_s} | "
                    f"{r.get('regime','—')} | {r.get('day_type','—')} |"
                )
            sections.append("")
            top.to_csv(SCRATCH / f"phase3_top_losers_{strat}.csv", index=False)

        # 3.2-3.4 Mann-Whitney U + Cliff's delta + Bonferroni
        if len(df_w) >= 5 and len(df_l) >= 5:
            sections.append(f"\n#### Discriminating features (Mann-Whitney U, winners vs losers)\n")

            test_results: list[dict[str, Any]] = []
            for feat in NUMERIC_FEATURES:
                if feat not in df_s.columns:
                    continue
                w_vals = pd.to_numeric(df_w[feat], errors="coerce").dropna().values
                l_vals = pd.to_numeric(df_l[feat], errors="coerce").dropna().values
                if len(w_vals) < 5 or len(l_vals) < 5:
                    continue
                try:
                    u_stat, p = stats.mannwhitneyu(
                        w_vals, l_vals, alternative="two-sided"
                    )
                except Exception:
                    continue
                d = cliffs_delta(w_vals, l_vals)
                test_results.append({
                    "feature": feat,
                    "n_win": len(w_vals),
                    "n_loss": len(l_vals),
                    "u": float(u_stat),
                    "p": float(p),
                    "cliffs_delta": d,
                    "abs_delta": abs(d) if not np.isnan(d) else 0.0,
                    "median_win": float(np.median(w_vals)),
                    "median_loss": float(np.median(l_vals)),
                })

            k = len(test_results)
            if k > 0:
                alpha = 0.05
                bonf_alpha = alpha / k
                # Sort by |Cliff's delta|, then p
                test_results.sort(
                    key=lambda r: (-r["abs_delta"], r["p"])
                )

                sections.append(f"K = {k} features tested.  Bonferroni-corrected α = {alpha}/{k} = "
                                f"{bonf_alpha:.5f}\n")
                sections.append("| feature | n_win | n_loss | median_win | median_loss | Cliff δ | p | survives α/K? |")
                sections.append("|---|---:|---:|---:|---:|---:|---:|:-:|")
                for r in test_results:
                    survives = "**YES**" if r["p"] < bonf_alpha else "no"
                    sections.append(
                        f"| `{r['feature']}` | {r['n_win']} | {r['n_loss']} | "
                        f"{r['median_win']:.3f} | {r['median_loss']:.3f} | "
                        f"{r['cliffs_delta']:+.3f} | {r['p']:.4g} | {survives} |"
                    )
                sections.append("")

                # Save for later phases
                dfd = pd.DataFrame(test_results)
                dfd["bonf_alpha"] = bonf_alpha
                dfd["strategy"] = strat
                dfd.to_csv(SCRATCH / f"phase3_discriminators_{strat}.csv", index=False)

                # 3.4 Footprint feasibility cross-reference (delta_aligned_ratio_5m)
                dar = next(
                    (r for r in test_results if r["feature"] == "delta_aligned_ratio_5m"),
                    None,
                )
                if dar is not None:
                    sections.append("##### Footprint feasibility cross-check on `delta_aligned_ratio_5m`\n")
                    sections.append(
                        f"- Winners (n={dar['n_win']}) median = {dar['median_win']:.3f}; "
                        f"losers (n={dar['n_loss']}) median = {dar['median_loss']:.3f}\n"
                    )
                    sign = "contrarian" if dar["cliffs_delta"] < 0 else "aligned"
                    sections.append(
                        f"- Cliff's δ = {dar['cliffs_delta']:+.3f}, p = {dar['p']:.4g}. "
                        f"Effect direction at this strategy level: **{sign}** "
                        f"(contrarian = aggressor pressure AGAINST direction predicts a win)."
                    )
                    sections.append(
                        "  (Per the prior footprint feasibility finding p ≈ 0.0004 at the full-portfolio level "
                        "the signal was contrarian. The per-strategy verdict here either confirms or refutes it.)\n"
                    )

                discriminator_rows.append({
                    "strategy": strat,
                    "k_tested": k,
                    "bonf_alpha": bonf_alpha,
                    "n_survivors": sum(1 for r in test_results if r["p"] < bonf_alpha),
                })
        else:
            sections.append(f"\n#### Discriminating features\n\n"
                            f"**Declined** — min(WIN, LOSS) = {min(len(df_w), len(df_l))} < 5.\n")

    # =====================================================================
    # Save sections
    # =====================================================================
    text = "\n".join(sections)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(text, encoding="utf-8")
    print(f"\nWrote {OUT_MD} ({len(text)} bytes)")

    # Summary
    print("\n=== Discriminator summary ===")
    for d in discriminator_rows:
        print(d)


if __name__ == "__main__":
    main()
