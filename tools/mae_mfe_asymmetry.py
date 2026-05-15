"""MAE/MFE asymmetry analysis — does each strategy have edge or anti-edge?

Surfaces the key insight the manual audit found 2026-05-15:
  - noise_area: 10% WR, avgMAE 165t vs avgMFE 73t → losers go 2.3× further
    adverse than winners go favorable. **Anti-edge.**
  - bias_momentum: 27% WR, avgMAE 64t vs avgMFE 86.5t → winners run further
    than losers go adverse. **Real edge.**

The rule of thumb:
  - If avgMFE > avgMAE: strategy has structural edge regardless of WR
  - If avgMAE > avgMFE: strategy is losing the variance war
  - If avgMAE ≈ avgMFE: WR drives outcome; need WR > 50% to profit

Combined with realized win/loss sizes, this tells you whether your
strategy is profitable BEFORE you look at WR. Key for new strategies
where small samples make WR misleading.

Usage:
    python tools/mae_mfe_asymmetry.py
    python tools/mae_mfe_asymmetry.py --since 2026-04-01
    python tools/mae_mfe_asymmetry.py --strategy bias_momentum
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CT = ZoneInfo("America/Chicago")


def _parse_ts(t: dict) -> float | None:
    for k in ("exit_time", "entry_time", "ts", "recorded_at"):
        v = t.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return float(v)
            s = str(v).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CT)
            return dt.timestamp()
        except Exception:
            continue
    return None


def analyze(trades: list[dict], tick_value: float = 0.50) -> dict[str, dict]:
    """Group trades by strategy, compute MAE/MFE asymmetry."""
    by_strat: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0,
        "win_pnls": [], "loss_pnls": [],
        "mae_ticks_winners": [], "mae_ticks_losers": [],
        "mfe_ticks_winners": [], "mfe_ticks_losers": [],
        "winners_realized_mfe_pct": [],
        "losers_realized_mae_pct": [],
    })
    for t in trades:
        s = t.get("strategy", "?")
        rec = by_strat[s]
        pnl = float(t.get("pnl_dollars_net", t.get("pnl_dollars", 0)) or 0)
        rec["n"] += 1
        mae_t = t.get("mae_ticks")
        mfe_t = t.get("mfe_ticks")
        is_win = pnl > 0
        if is_win:
            rec["wins"] += 1
            rec["win_pnls"].append(pnl)
            if mae_t is not None:
                rec["mae_ticks_winners"].append(float(mae_t))
            if mfe_t is not None:
                rec["mfe_ticks_winners"].append(float(mfe_t))
                # Did the winner realize most of its MFE?
                pnl_ticks = abs(pnl) / tick_value if tick_value > 0 else 0
                if float(mfe_t) > 0:
                    rec["winners_realized_mfe_pct"].append(
                        100.0 * min(1.0, pnl_ticks / float(mfe_t))
                    )
        else:
            rec["losses"] += 1
            rec["loss_pnls"].append(pnl)
            if mae_t is not None:
                rec["mae_ticks_losers"].append(float(mae_t))
                pnl_ticks = abs(pnl) / tick_value if tick_value > 0 else 0
                if float(mae_t) > 0:
                    rec["losers_realized_mae_pct"].append(
                        100.0 * min(1.0, pnl_ticks / float(mae_t))
                    )
            if mfe_t is not None:
                rec["mfe_ticks_losers"].append(float(mfe_t))
    return dict(by_strat)


def _avg(xs: list[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def _verdict(stat: dict, tick_value: float) -> str:
    """Edge verdict based on MAE/MFE asymmetry."""
    n = stat["n"]
    if n < 10:
        return "INSUFFICIENT"
    wr = stat["wins"] / n if n else 0
    # Combine winners + losers MAE/MFE
    all_mae = stat["mae_ticks_winners"] + stat["mae_ticks_losers"]
    all_mfe = stat["mfe_ticks_winners"] + stat["mfe_ticks_losers"]
    avg_mae = _avg(all_mae)
    avg_mfe = _avg(all_mfe)
    if not all_mae or not all_mfe:
        return "NO_MAE_MFE_DATA"
    ratio = avg_mfe / max(avg_mae, 1)
    if ratio >= 1.2 and wr >= 0.20:
        return "EDGE"
    if ratio >= 0.9:
        return "MARGINAL"
    return "ANTI_EDGE"


def render(by_strat: dict[str, dict], tick_value: float) -> str:
    lines = []
    lines.append("Strategy MAE/MFE asymmetry — does each strategy have edge?")
    lines.append("=" * 110)
    lines.append(
        f"{'strategy':24s} {'n':>4s} {'WR':>5s} {'net$':>9s} "
        f"{'avgMAE':>8s} {'avgMFE':>8s} {'MFE/MAE':>8s} {'winC%':>7s} "
        f"{'losC%':>7s} verdict"
    )
    lines.append("-" * 110)
    rows = []
    for s, d in by_strat.items():
        if d["n"] == 0:
            continue
        n = d["n"]
        wr = d["wins"] / n
        net = sum(d["win_pnls"]) + sum(d["loss_pnls"])
        all_mae = d["mae_ticks_winners"] + d["mae_ticks_losers"]
        all_mfe = d["mfe_ticks_winners"] + d["mfe_ticks_losers"]
        avg_mae = _avg(all_mae)
        avg_mfe = _avg(all_mfe)
        ratio = avg_mfe / max(avg_mae, 1)
        winners_cap = _avg(d["winners_realized_mfe_pct"])
        losers_cap = _avg(d["losers_realized_mae_pct"])
        verdict = _verdict(d, tick_value)
        rows.append((
            s, n, wr, net, avg_mae, avg_mfe, ratio,
            winners_cap, losers_cap, verdict,
        ))
    # Sort by verdict (EDGE first), then by n desc
    rank = {"EDGE": 0, "MARGINAL": 1, "INSUFFICIENT": 2,
            "NO_MAE_MFE_DATA": 3, "ANTI_EDGE": 4}
    rows.sort(key=lambda r: (rank.get(r[9], 5), -r[1]))
    for (s, n, wr, net, avg_mae, avg_mfe, ratio,
         wcap, lcap, verdict) in rows:
        lines.append(
            f"{s:24s} {n:>4d} {wr*100:>4.0f}% ${net:>+8.2f} "
            f"{avg_mae:>8.1f} {avg_mfe:>8.1f} {ratio:>7.2f}x "
            f"{wcap:>6.0f}% {lcap:>6.0f}% {verdict}"
        )
    lines.append("")
    lines.append("Columns:")
    lines.append("  avgMAE  — average MAXIMUM ADVERSE EXCURSION in ticks "
                 "(how far against the trade)")
    lines.append("  avgMFE  — average MAXIMUM FAVORABLE EXCURSION in ticks "
                 "(how far in the trade's favor)")
    lines.append("  MFE/MAE — ratio. >1.0 = winners run further than "
                 "losers go adverse. <1.0 = strategy is fighting itself.")
    lines.append("  winC%   — average fraction of MFE realized on winners. "
                 "Low = take-profit too eager.")
    lines.append("  losC%   — average fraction of MAE realized on losers. "
                 "Low = stop too tight (mostly trapped). High = stop too "
                 "wide (riding losers).")
    lines.append("")
    lines.append("Verdicts (combine WR + asymmetry):")
    lines.append("  EDGE         — MFE/MAE ≥ 1.2x AND WR ≥ 20% → real edge")
    lines.append("  MARGINAL     — MFE/MAE 0.9-1.2x → WR-dependent, watch")
    lines.append("  ANTI_EDGE    — MFE/MAE < 0.9x → losing the variance war, "
                 "tune or retire")
    lines.append("  INSUFFICIENT — n < 10")
    lines.append("  NO_MAE_MFE_DATA — pre-2026-05-13 trades (MAE/MFE landed "
                 "in commit c14a3a1)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD; only trades on/after")
    ap.add_argument("--strategy", help="Only this strategy")
    ap.add_argument("--tick-value", type=float, default=0.50,
                    help="$ per tick per contract (MNQ default 0.50)")
    args = ap.parse_args()

    from core.trade_memory import load_all_trades
    trades = load_all_trades(logs_dir=str(ROOT / "logs"))
    if not isinstance(trades, list):
        print("ERROR: load_all_trades returned wrong shape")
        return 1

    # Filters
    if args.since:
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=CT).timestamp()
        trades = [t for t in trades if (_parse_ts(t) or 0) >= cutoff]
    if args.strategy:
        trades = [t for t in trades if t.get("strategy") == args.strategy]

    if not trades:
        print("No trades match filters.")
        return 0

    by_strat = analyze(trades, tick_value=args.tick_value)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(render(by_strat, args.tick_value))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
