"""Per-strategy breakdown for the 4 Phase 13 lab strategies:
- when they fire (time-of-day buckets, volatility regime)
- configured stop / take-profit rules (from config/strategies.py)
- MAE/MFE-empirical optimal stop & target derived from realized trades
- win rate, profit factor, drawdown headline
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd

from tools.portfolio_backtest import paths, analytics
from config.strategies import STRATEGIES


PHASE13 = [
    "raschke_baseline",
    "g_inside_bar_breakout",
    "e_multi_day_breakout",
    "a_asian_continuation",
]


def cfg_summary(strat: str) -> str:
    c = STRATEGIES.get(strat, {})
    if strat == "a_asian_continuation":
        return (f"Window: {c.get('window_start_ct','?')}-{c.get('window_end_ct','?')} CT (overnight session)\n"
                f"Trigger:  5-min close beyond the overnight 17:00-08:30 CT range, by {c.get('range_break_atr_mult',0.5)} x ATR\n"
                f"Stop:     min {c.get('min_stop_ticks',6)} / max {c.get('max_stop_ticks',14)} ticks (= ${c.get('min_stop_ticks',6)*0.5:.0f}-${c.get('max_stop_ticks',14)*0.5:.0f} risk per contract)\n"
                f"Target:   {c.get('target_rr',2.0)}x the stop distance\n"
                f"Time exit: cuts the trade at {c.get('max_hold_min',30)} min if neither stop nor target hit")
    if strat == "e_multi_day_breakout":
        return (f"Window: {c.get('window_start_ct','?')}-{c.get('window_end_ct','?')} CT (RTH morning/midday)\n"
                f"Trigger:  5-min close beyond the prior {c.get('lookback_days',3)} RTH sessions' high/low + {c.get('break_buffer_ticks',1)}-tick buffer\n"
                f"Stop:     min {c.get('min_stop_ticks',6)} / max {c.get('max_stop_ticks',30)} ticks, {c.get('stop_buffer_ticks',2)}-tick buffer past the broken range\n"
                f"Target:   {c.get('target_rr',2.0)}x the stop distance\n"
                f"Trail:    chandelier(50-bar high/low, 3x ATR, activates after 1R favorable)")
    if strat == "g_inside_bar_breakout":
        return (f"Window: {c.get('window_start_ct','?')}-{c.get('window_end_ct','?')} CT (RTH morning to mid-afternoon)\n"
                f"Trigger:  5-min inside bar (min {c.get('min_inside_range_ticks',4)} ticks range, max {int(c.get('max_inside_range_ratio',0.85)*100)}% of prior bar) broken by next 5-min close\n"
                f"Stop:     min {c.get('min_stop_ticks',6)} / max {c.get('max_stop_ticks',30)} ticks, {c.get('stop_buffer_ticks',1)}-tick buffer\n"
                f"Target:   {c.get('target_rr',2.0)}x the stop distance\n"
                f"Trail:    chandelier(50-bar high/low, 3x ATR, activates after 1R favorable)")
    if strat == "raschke_baseline":
        return (f"Window: {c.get('window_start_ct','?')}-{c.get('window_end_ct','?')} CT (full RTH)\n"
                f"Trigger:  Linda Raschke 20-EMA pullback. Trend filter: EMA21-EMA50 spread > {c.get('trend_spread_atr',0.3)} x ATR_5m.\n"
                f"          Enter on pullback to EMA21 within {c.get('pullback_lookback',3)} bars.\n"
                f"Stop:     min {c.get('min_stop_ticks',6)} / max {c.get('max_stop_ticks',40)} ticks, {c.get('stop_buffer_ticks',1)}-tick buffer\n"
                f"Target:   {c.get('target_rr',2.0)}x the stop distance\n"
                f"Time exit: cuts the trade at {c.get('max_hold_min',30)} min if neither stop nor target hit")
    return "(unknown)"


def main() -> int:
    df = pd.read_csv(paths.OUT_DIR / "macro_trades.csv",
                     parse_dates=["entry_ts", "exit_ts"])
    print()
    print("PER-STRATEGY BREAKDOWN - 4 PHASE 13 LAB STRATEGIES (5y, friction net)")
    print("=" * 90)

    for strat in PHASE13:
        s = df[df["strategy"] == strat]
        if s.empty:
            print(f"\n{strat}: NO TRADES")
            continue
        print()
        print("=" * 90)
        print(f"  {strat}")
        print("=" * 90)
        print()
        print("  RULES (from config/strategies.py):")
        for line in cfg_summary(strat).splitlines():
            print("    " + line)

        sm = analytics.summarize(s)
        print()
        print("  HEADLINE (5y, friction net):")
        print(f"    Trades:       {sm['n']}")
        print(f"    Net profit:   ${sm['net_pnl']:>+10,.0f}")
        print(f"    Win rate:     {sm['win_rate']*100:.1f}%")
        print(f"    Profit factor:{sm['profit_factor']:>5.2f}")
        print(f"    Expectancy:   ${sm['expectancy']:>+7.2f} per trade")
        print(f"    Max drawdown: ${sm['max_dd']:>9,.0f}")
        print(f"    Sharpe:       {sm['sharpe']:.2f}  Sortino: {sm['sortino']:.2f}")

        print()
        print("  WHEN IT EARNS (by time-of-day, US Eastern):")
        tod = analytics.bucket_table(s, "tod_bucket")[
            ["tod_bucket", "n", "net_pnl", "win_rate", "profit_factor"]
        ]
        tod.columns = ["bucket", "trades", "net_dollars", "WR", "PF"]
        tod["WR"] = (tod["WR"] * 100).round(1).astype(str) + "%"
        tod["net_dollars"] = tod["net_dollars"].round(0).astype(int)
        print(tod.to_string(index=False).replace("\n", "\n    "))

        print()
        print("  WHEN IT EARNS (by volatility regime, ATR-percentile based):")
        rg = analytics.bucket_table(s, "regime")[
            ["regime", "n", "net_pnl", "win_rate", "profit_factor"]
        ]
        rg.columns = ["regime", "trades", "net_dollars", "WR", "PF"]
        rg["WR"] = (rg["WR"] * 100).round(1).astype(str) + "%"
        rg["net_dollars"] = rg["net_dollars"].round(0).astype(int)
        print(rg.to_string(index=False).replace("\n", "\n    "))

        if "mae_ticks" in s.columns and "mfe_ticks" in s.columns:
            st = analytics.optimal_stop_target(s)
            print()
            print("  EMPIRICAL STOP / TARGET (what the trades themselves suggest):")
            print(f"    Suggested stop:   {st.suggested_stop_ticks:>5.1f} ticks  (= ${st.suggested_stop_ticks * 0.5:.2f} risk per contract)")
            print(f"    Suggested target: {st.suggested_target_ticks:>5.1f} ticks  (= ${st.suggested_target_ticks * 0.5:.2f} per contract on hits)")
            print(f"    Winners' worst adverse excursion: median {st.winner_mae_p50} t, 90th-pctile {st.winner_mae_p90} t")
            print(f"    All trades' favorable excursion:  median {st.mfe_p50} t, 75th-pctile {st.mfe_p75} t")

    print()
    print("=" * 90)
    return 0


if __name__ == "__main__":
    sys.exit(main())
