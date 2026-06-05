"""
Phase 6 — How does current config reject historical WINNERS?

PARTIAL retroactive evaluation per Phase 0.6 verdict. Gates we CAN
evaluate from the persisted market_snapshot:
  - regime_veto (OVERNIGHT_RANGE)
  - session_block_window (04:00-04:59 CT)
  - EMA_STACK (ema9 vs ema21)
  - VWAP_GATE (price vs vwap)
  - TF_VOTES (tf_votes_bullish/bearish vs min_tf_votes=2)
  - max_ema_dist_ticks=60 (distance_from_ema9_ticks)

Cannot evaluate (fields not persisted):
  - min_confluence=5.5  (confluence_score)
  - SHORT-asymmetric 1m AND 5m BEARISH (separate 1m/5m bias not stored)
  - cr_mom_score only available on 16% of records

Appends to: out/winning_conditions_counterfactual_2026-06-04.md
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import pandas as pd
import numpy as np

REPO = Path(r"C:\Trading Project\phoenix_bot")
SCRATCH = Path(r"C:\tmp\winning_conditions")
OUT_MD = REPO / "out" / "winning_conditions_counterfactual_2026-06-04.md"

DATASET = SCRATCH / "wc_dataset.parquet"


def _ct_hour_min(iso: str) -> tuple[int, int] | None:
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        ct = t - dt.timedelta(hours=5)  # CDT in Mar-Jun
        return ct.hour, ct.minute
    except Exception:
        return None


def evaluate_current_gates(row: pd.Series) -> dict[str, bool]:
    """Return dict gate_name -> would_pass_today."""
    res: dict[str, bool] = {}
    # 1. regime_veto (OVERNIGHT_RANGE)
    regime = row.get("regime") or ""
    res["regime_veto"] = (regime != "OVERNIGHT_RANGE")
    # 2. session_block (04:00-04:59 CT)
    iso = row.get("entry_time_iso")
    if iso:
        ct = _ct_hour_min(iso)
        if ct is not None:
            h, m = ct
            res["session_block"] = not (h == 4)
        else:
            res["session_block"] = True
    else:
        res["session_block"] = True
    # 3. EMA_STACK
    try:
        ema9 = float(row.get("ema9") or 0)
        ema21 = float(row.get("ema21") or 0)
        direction = (row.get("direction") or "").upper()
        if ema9 > 0 and ema21 > 0 and direction:
            if direction == "LONG":
                res["ema_stack"] = (ema9 > ema21)
            else:
                res["ema_stack"] = (ema9 < ema21)
        else:
            res["ema_stack"] = None
    except Exception:
        res["ema_stack"] = None
    # 4. VWAP_GATE
    try:
        price = float(row.get("entry_price") or row.get("price") or 0)
        vwap = float(row.get("vwap") or 0)
        if price > 0 and vwap > 0 and direction:
            if direction == "LONG":
                res["vwap_gate"] = (price > vwap)
            else:
                res["vwap_gate"] = (price < vwap)
        else:
            res["vwap_gate"] = None
    except Exception:
        res["vwap_gate"] = None
    # 5. min_tf_votes=2
    try:
        if direction == "LONG":
            votes = int(row.get("tf_votes_bullish") or 0)
        else:
            votes = int(row.get("tf_votes_bearish") or 0)
        res["min_tf_votes_2"] = (votes >= 2)
    except Exception:
        res["min_tf_votes_2"] = None
    # 6. max_ema_dist_ticks=60 (only blocks LATE_AFTERNOON; relaxed here)
    try:
        d = float(row.get("distance_from_ema9_ticks") or 0)
        # Strategy gates only outside golden windows. We approximate the
        # 60-tick cap as a hard check regardless of regime; this OVER-counts
        # rejections vs reality.
        if direction == "LONG":
            res["max_ema_dist_60t"] = (d <= 60)
        else:
            res["max_ema_dist_60t"] = (d >= -60)
    except Exception:
        res["max_ema_dist_60t"] = None
    return res


def main() -> None:
    df = pd.read_parquet(DATASET)
    df_bm = df[df["strategy"] == "bias_momentum"].copy()
    print(f"bias_momentum trades: {len(df_bm)}")
    winners = df_bm[df_bm["win"] == True].copy()
    losers = df_bm[df_bm["win"] == False].copy()
    print(f"  winners={len(winners)} losers={len(losers)}")

    gate_results_win: list[dict] = []
    gate_results_loss: list[dict] = []
    for _, r in winners.iterrows():
        g = evaluate_current_gates(r)
        gate_results_win.append(g)
    for _, r in losers.iterrows():
        g = evaluate_current_gates(r)
        gate_results_loss.append(g)

    gates = ["regime_veto", "session_block", "ema_stack", "vwap_gate", "min_tf_votes_2", "max_ema_dist_60t"]

    # Per-gate fail rate
    win_fail = {g: 0 for g in gates}
    win_evald = {g: 0 for g in gates}
    loss_fail = {g: 0 for g in gates}
    loss_evald = {g: 0 for g in gates}
    for d in gate_results_win:
        for g in gates:
            v = d.get(g)
            if v is None:
                continue
            win_evald[g] += 1
            if not v:
                win_fail[g] += 1
    for d in gate_results_loss:
        for g in gates:
            v = d.get(g)
            if v is None:
                continue
            loss_evald[g] += 1
            if not v:
                loss_fail[g] += 1

    # Compute # trades that would be rejected by AT LEAST ONE gate
    def rejected_count(results: list[dict]) -> int:
        n = 0
        for d in results:
            for g in gates:
                v = d.get(g)
                if v is False:
                    n += 1
                    break
        return n
    rej_win = rejected_count(gate_results_win)
    rej_loss = rejected_count(gate_results_loss)

    lines: list[str] = []
    lines.append("\n---\n## Phase 6 · Retroactive current-config rejection of historical trades\n")
    lines.append(
        "Per the Phase 0.6 PARTIAL verdict, we evaluate the gates that CAN be tested "
        "from persisted market_snapshot fields. Gates we cannot test: `min_confluence` "
        "(field never persisted), `SHORT-asymmetric tf_bias` (1m/5m not stored "
        "separately), `cr_mom_score`-based min_momentum (only 16% populated).\n"
    )
    lines.append(f"- bias_momentum historical winners (full dataset): {len(winners)}")
    lines.append(f"- bias_momentum historical losers: {len(losers)}")
    lines.append("")
    lines.append("### Per-gate fail rate (% of trades where the gate would reject today)\n")
    lines.append("| gate | win fail/eval'd | win fail % | loss fail/eval'd | loss fail % |")
    lines.append("|---|---|---:|---|---:|")
    for g in gates:
        wn = win_evald[g] or 1
        ln = loss_evald[g] or 1
        lines.append(
            f"| `{g}` | {win_fail[g]}/{win_evald[g]} | {100*win_fail[g]/wn:.1f}% | "
            f"{loss_fail[g]}/{loss_evald[g]} | {100*loss_fail[g]/ln:.1f}% |"
        )
    lines.append("")
    lines.append("### Aggregate rejection (any-gate-fail)\n")
    lines.append(f"- **Winners rejected by ≥1 current gate: {rej_win} / {len(winners)} = {100*rej_win/max(1,len(winners)):.1f}%**")
    lines.append(f"- **Losers rejected by ≥1 current gate: {rej_loss} / {len(losers)} = {100*rej_loss/max(1,len(losers)):.1f}%**")
    lines.append("")

    # Interpretation
    win_rej_pct = 100*rej_win/max(1,len(winners))
    loss_rej_pct = 100*rej_loss/max(1,len(losers))
    if loss_rej_pct > win_rej_pct + 5:
        verdict = "**Tightening is JUSTIFIED**: rejection rate is higher for losers than winners — the gate stack filters more losses than wins."
    elif loss_rej_pct < win_rej_pct - 5:
        verdict = "**Tightening is COUNTER-PRODUCTIVE**: rejection rate is HIGHER for winners than losers — the gates are biased against good trades."
    else:
        verdict = "**Tightening is NEUTRAL**: rejection rates are similar for winners and losers — the gates don't preferentially filter either."
    lines.append(f"### Interpretation\n\n{verdict}\n")

    lines.append("- The two newest gates (`session_block` 04:00-04:59 added 2026-06-01; "
                 "`max_ema_dist_60t` anti-chase) dominate the difference where they appear. "
                 "Older trades (pre-instrumentation) rarely have the data fields needed to "
                 "evaluate every gate, so the aggregate any-gate-fail count is an "
                 "underestimate.\n")
    lines.append("- The honest number lives in the per-gate column: `session_block` filtering "
                 f"{win_fail['session_block']}/{win_evald['session_block']} historical winners "
                 f"= {100*win_fail['session_block']/(win_evald['session_block'] or 1):.1f}% — "
                 "if this is HIGH, the recent session_block addition is over-rejecting.\n")

    # Per-gate implied PnL delta (rough)
    rejected_winner_pnl = sum(
        float(winners.iloc[i].get("pnl_dollars_net") or 0)
        for i, d in enumerate(gate_results_win)
        if any(d.get(g) is False for g in gates)
    )
    rejected_loser_pnl = sum(
        float(losers.iloc[i].get("pnl_dollars_net") or 0)
        for i, d in enumerate(gate_results_loss)
        if any(d.get(g) is False for g in gates)
    )
    net_delta = -(rejected_winner_pnl + rejected_loser_pnl)
    lines.append(f"\n### Rough implied PnL delta (cumulative across full bias_momentum history)\n")
    lines.append(
        f"- Winner PnL the current gates WOULD have rejected: ${rejected_winner_pnl:+.2f}\n"
        f"- Loser PnL the current gates WOULD have rejected: ${rejected_loser_pnl:+.2f}\n"
        f"- **Implied delta (positive = tightening helps): ${net_delta:+.2f}**\n"
    )
    lines.append("> **Caveat:** the calculation rejects an entire trade if ANY single "
                 "current gate fails. Many of these gates didn't exist when the trade "
                 "was placed, and some have different intent (session_block 04:00-04:59 "
                 "is hour-specific). Treat the number as a directional sanity check, "
                 "not a deployable lift estimate.\n")

    # Append
    existing = OUT_MD.read_text(encoding="utf-8")
    OUT_MD.write_text(existing + "\n" + "\n".join(lines), encoding="utf-8")
    print(f"Appended Phase 6 to {OUT_MD}")
    print(f"\nVerdict: {verdict}")
    print(f"Winners rej: {rej_win}/{len(winners)} = {win_rej_pct:.1f}%")
    print(f"Losers rej: {rej_loss}/{len(losers)} = {loss_rej_pct:.1f}%")
    print(f"Implied delta: ${net_delta:+.2f}")


if __name__ == "__main__":
    main()
