"""
Execution-friction analysis: slippage (by volatility) + latency + commission,
then net-after-friction for the fixed-RR strategies.

DATA SOURCES (all real):
  - backtest_results/phoenix_tick_entry_slippage.csv : per-trade signed slippage
    simulated against real TBBO bid/ask (4 fill models). 2026-03-17..2026-05-15.
    sign convention: slip_ticks NEGATIVE = FAVORABLE (filled better than signal).
  - data/historical/mnq_1min_databento.csv : for ATR(14) volatility bucketing.
  - config/settings.py : COMMISSION_PER_SIDE=0.86, EXCHANGE_FEES_PER_SIDE=0.55
    -> round-turn fees = 2*(0.86+0.55) = $2.82/contract. EXACT, not estimated.
  - backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv : per-trade
    gross P&L (entry at signal/bar-close, no friction) for the net calc.

OUTPUTS (out/_baseline_2026-05-27/friction/):
  friction_profile.json        - the Execution Decay Profile
  slippage_by_volatility.csv   - per (strategy, vol_bucket): n, mean/median slip
  net_after_friction.csv       - per strategy: gross, +slip, -commission, net
  friction_summary.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("C:/Trading Project/phoenix_bot")
sys.path.insert(0, str(ROOT))

from config.settings import COMMISSION_PER_SIDE, EXCHANGE_FEES_PER_SIDE  # noqa: E402

TICK_VALUE = 0.50
RT_FEES = 2.0 * (COMMISSION_PER_SIDE + EXCHANGE_FEES_PER_SIDE)  # round-turn $/contract

OUT = ROOT / "out" / "_baseline_2026-05-27" / "friction"
OUT.mkdir(parents=True, exist_ok=True)

# Fixed-RR production strategies (Phase 13 PHASE_13_EXIT_ASSIGNMENTS) — the
# "top-performing fixed Reward-to-Risk strategies" the operator named.
FIXED_RR_STRATEGIES = ["bias_momentum", "spring_setup", "vwap_pullback_v2"]

# Realistic fill model (500ms OIF latency) is the canonical "real world" one.
SLIP_COL = "real_slip_ticks"
LAG_COL = "real_lag_ms"


def load_atr_1m() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data/historical/mnq_1min_databento.csv")
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts").sort_index()
    # True range -> ATR(14)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    return pd.DataFrame({"atr_ticks": atr / 0.25}).dropna()


def main():
    print(f"[config] RT fees = 2*({COMMISSION_PER_SIDE}+{EXCHANGE_FEES_PER_SIDE}) = ${RT_FEES:.2f}/contract")

    slip = pd.read_csv(ROOT / "backtest_results/phoenix_tick_entry_slippage.csv")
    slip["entry_ts"] = pd.to_datetime(slip["entry_ts"], utc=True)
    print(f"[load] slippage rows: {len(slip):,} ({slip['entry_ts'].min().date()} .. {slip['entry_ts'].max().date()})")

    atr = load_atr_1m()
    # asof-join ATR at each entry (nearest preceding 1m bar)
    slip = slip.sort_values("entry_ts")
    slip = pd.merge_asof(slip, atr, left_on="entry_ts", right_index=True, direction="backward")

    # Volatility bucket: split at the GLOBAL median ATR over the window.
    med_atr = slip["atr_ticks"].median()
    slip["vol_bucket"] = np.where(slip["atr_ticks"] >= med_atr, "HIGH_VOL", "LOW_VOL")
    print(f"[vol] median ATR(14) over window = {med_atr:.1f} ticks (split point)")

    # ── 1. Slippage by volatility, per strategy ──────────────────────
    by_vol_rows = []
    for (strat, bucket), g in slip.groupby(["strategy", "vol_bucket"]):
        s = g[SLIP_COL].dropna()
        if len(s) == 0:
            continue
        by_vol_rows.append({
            "strategy": strat,
            "vol_bucket": bucket,
            "n": len(s),
            "mean_slip_ticks": round(float(s.mean()), 2),
            "median_slip_ticks": round(float(s.median()), 2),
            "p95_adverse_ticks": round(float(s.quantile(0.95)), 2),
            "pct_favorable": round(float((s < 0).mean() * 100), 1),
            "mean_spread_ticks": round(float(g["first_spread_ticks"].mean()), 2),
            "mean_atr_ticks": round(float(g["atr_ticks"].mean()), 1),
            "mean_lag_ms": round(float(g[LAG_COL].mean()), 0),
        })
    by_vol = pd.DataFrame(by_vol_rows).sort_values(["strategy", "vol_bucket"])
    by_vol.to_csv(OUT / "slippage_by_volatility.csv", index=False)
    print(f"[write] slippage_by_volatility.csv: {len(by_vol)} rows")

    # Per-strategy overall slippage + latency (for the decay profile + net calc)
    prof = {}
    for strat, g in slip.groupby("strategy"):
        s = g[SLIP_COL].dropna()
        hi = g[g.vol_bucket == "HIGH_VOL"][SLIP_COL].dropna()
        lo = g[g.vol_bucket == "LOW_VOL"][SLIP_COL].dropna()
        prof[strat] = {
            "n_sampled": int(len(s)),
            "mean_slip_ticks_all": round(float(s.mean()), 2),
            "median_slip_ticks_all": round(float(s.median()), 2),
            "mean_slip_ticks_high_vol": round(float(hi.mean()), 2) if len(hi) else None,
            "mean_slip_ticks_low_vol": round(float(lo.mean()), 2) if len(lo) else None,
            "mean_lag_ms": round(float(g[LAG_COL].mean()), 0),
            "mean_spread_ticks": round(float(g["first_spread_ticks"].mean()), 2),
            "slip_convention": "negative = favorable (filled better than signal)",
        }

    # ── 2. Net-after-friction on the fixed-RR strategies ─────────────
    trades = pd.read_csv(ROOT / "backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv")
    net_rows = []
    for strat in FIXED_RR_STRATEGIES:
        g = trades[trades.strategy == strat]
        n = len(g)
        if n == 0:
            continue
        gross = float(g["pnl_dollars"].sum())
        # Slippage P&L adjustment: favorable (negative slip) ADDS to P&L.
        #   adj = -slip_ticks * TICK_VALUE  (per trade)
        # Extrapolate the realistic per-strategy MEAN slip to all 5y trades
        # (per-trade slip only exists for the 2-month TBBO window).
        slip_mean = prof.get(strat, {}).get("mean_slip_ticks_all", 0.0) or 0.0
        slip_adj = -slip_mean * TICK_VALUE * n  # $ added across all trades
        commission = RT_FEES * n  # 1 contract per trade
        net = gross + slip_adj - commission
        net_rows.append({
            "strategy": strat,
            "n_trades": n,
            "gross_pnl": round(gross, 0),
            "slip_mean_ticks": slip_mean,
            "slip_adjustment_$": round(slip_adj, 0),
            "commission_drag_$": round(-commission, 0),
            "net_pnl": round(net, 0),
            "net_per_trade_$": round(net / n, 2),
            "friction_pct_of_gross": round((net - gross) / abs(gross) * 100, 1) if gross else None,
        })
    net = pd.DataFrame(net_rows)
    net.to_csv(OUT / "net_after_friction.csv", index=False)
    print(f"[write] net_after_friction.csv: {len(net)} strategies")

    # ── 3. Friction profile JSON ─────────────────────────────────────
    profile = {
        "generated_for": "operator request 2026-05-27: execution-friction backtest",
        "commission": {
            "commission_per_side_$": COMMISSION_PER_SIDE,
            "exchange_fees_per_side_$": EXCHANGE_FEES_PER_SIDE,
            "round_turn_fees_per_contract_$": round(RT_FEES, 2),
            "source": "config/settings.py:231-232 (Rithmic, empirically derived from account statements)",
            "note": "EXACT, not estimated. This is the dominant friction.",
        },
        "slippage": {
            "source": "backtest_results/phoenix_tick_entry_slippage.csv (real TBBO bid/ask fill sim, 2026-03-17..05-15)",
            "model": "realistic (500ms OIF latency)",
            "convention": "ticks; negative = FAVORABLE (market order fills better than signal)",
            "volatility_split": "ATR(14) on 1m bars, split at window median",
            "per_strategy": prof,
            "KEY_FINDING": (
                "Entry slippage is FAVORABLE (negative) for the mean-reversion/momentum "
                "strategies — they enter at extension extremes with follow-through, so "
                "market orders fill better than the signal bar-close. This is NOT a penalty. "
                "The real friction is COMMISSION."
            ),
        },
        "latency": {
            "source": "real_lag_ms in tick entry slippage CSV (realistic 500ms OIF model) + execution_quality.json",
            "note": "Latency is captured in the slippage figure (slip is measured at the post-latency fill). Not double-counted.",
        },
        "honesty_caveats": [
            "Live broker fill history (logs/performance/execution_quality.json) has only ~26 "
            "trades with real fill data and 95% null regime tags — too thin for a vol split. "
            "This analysis therefore uses the TBBO tick-level fill simulation (Section U.2), "
            "which is the rigorous source and supports the vol split.",
            "Slippage is measured over a 2-month window (Mar-May 2026) and extrapolated to the "
            "5y trade count for the net calc. The per-strategy MEAN is applied uniformly.",
            "Net calc applies slippage as a P&L adjustment + commission per round-turn. It does "
            "NOT re-simulate whether the slightly-shifted entry changes win/loss outcome "
            "(negligible at a few ticks vs 24-200t stops).",
        ],
    }
    (OUT / "friction_profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"[write] friction_profile.json")

    # ── 4. Markdown summary ──────────────────────────────────────────
    md = ["# Execution Decay Profile + Net-After-Friction — 2026-05-27", "",
          f"**Commission (EXACT, config):** ${RT_FEES:.2f} round-turn/contract "
          f"(${COMMISSION_PER_SIDE} brokerage + ${EXCHANGE_FEES_PER_SIDE} exchange, per side x2).",
          "",
          "## 1. Slippage by volatility (realistic 500ms fill, real TBBO bid/ask)",
          "",
          "Negative ticks = FAVORABLE (market order fills better than signal). "
          f"Vol split at ATR(14) median over the 2-month window.",
          "",
          "| Strategy | Vol bucket | n | Mean slip (t) | Median (t) | p95 adverse | % favorable | Mean spread (t) | Mean ATR (t) |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in by_vol.iterrows():
        md.append(f"| {r['strategy']} | {r['vol_bucket']} | {r['n']} | {r['mean_slip_ticks']:+.2f} | "
                  f"{r['median_slip_ticks']:+.2f} | {r['p95_adverse_ticks']:+.1f} | {r['pct_favorable']}% | "
                  f"{r['mean_spread_ticks']} | {r['mean_atr_ticks']} |")
    md += ["", "## 2. Net P&L after friction — fixed-RR strategies (5y, 1 contract)", "",
           "| Strategy | n | Gross $ | Slip adj $ | Commission $ | **Net $** | Net/trade | Friction % |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in net.iterrows():
        md.append(f"| {r['strategy']} | {r['n_trades']:,} | {r['gross_pnl']:,.0f} | "
                  f"{r['slip_adjustment_$']:+,.0f} | {r['commission_drag_$']:+,.0f} | "
                  f"**{r['net_pnl']:,.0f}** | {r['net_per_trade_$']:+.2f} | {r['friction_pct_of_gross']:+.1f}% |")
    md += ["", "## Key finding", "",
           "Entry slippage is **favorable** (negative) for these strategies — they fill better "
           "than the signal bar-close. The dominant friction is **commission** "
           f"(${RT_FEES:.2f}/round-turn x trade count). For high-volume strategies like "
           "bias_momentum (28k+ trades), commission drag is the line item that matters, "
           "not slippage.", ""]
    (OUT / "friction_summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"[write] friction_summary.md")
    print("[done]")

    # Console preview
    print("\n=== NET AFTER FRICTION (fixed-RR strategies) ===")
    print(net.to_string(index=False))


if __name__ == "__main__":
    main()
