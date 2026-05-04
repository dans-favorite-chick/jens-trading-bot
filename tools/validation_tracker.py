"""
Phoenix — Validation Tracker

For each strategy, computes:
  - Trades completed (from logs/trade_memory.json)
  - Statistical tier (INSUFFICIENT_SAMPLE / PRELIMINARY / TENTATIVE /
    VALIDATED / HIGH_CONFIDENCE)
  - Win rate with Wilson 95% CI (asymmetric, bounded [0,1])
  - Profit factor + R/R metrics
  - Projected ETA to next tier (based on trailing 7-day rate)
  - Decision recommendation (KILL_CANDIDATE / WATCH / GRADUATE / SCALE)

Statistical tiers (from validation research; DARA.TRADE; backtestbase.com;
Trading Dude on Medium):
  INSUFFICIENT_SAMPLE: < 30  trades  — no stat inference possible
  PRELIMINARY:        30-99  trades  — directional only, ~70% confidence
  TENTATIVE:        100-384  trades  — meaningful, ~90% confidence
  VALIDATED:        385-665  trades  — ~95% confidence
  HIGH_CONFIDENCE:    666+   trades  — ~99% confidence

Phoenix's 50-trade project gate sits inside PRELIMINARY — i.e. enough
to look at, not enough to bet the farm on.

Output: out/validation_status_<YYYY-MM-DD>.md
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date as date_t
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CT = ZoneInfo("America/Chicago")


# ─── data root: cwd if it has logs/, else fall back to project ROOT ──
def _data_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "logs" / "trade_memory.json").exists():
        return cwd
    if (ROOT / "logs" / "trade_memory.json").exists():
        return ROOT
    return cwd


# ─── Statistical helpers ─────────────────────────────────────────────
def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI for binomial proportion. Asymmetric, bounded [0,1]."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    margin = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def statistical_tier(n: int) -> str:
    if n < 30:    return "INSUFFICIENT_SAMPLE"
    if n < 100:   return "PRELIMINARY"
    if n < 385:   return "TENTATIVE"
    if n < 666:   return "VALIDATED"
    return "HIGH_CONFIDENCE"


def tier_next_threshold(n: int):
    if n < 30:  return 30
    if n < 100: return 100
    if n < 385: return 385
    if n < 666: return 666
    return None


def decision_recommendation(n: int, wr: float, pf: float, lo_wr: float) -> str:
    """Tier-aware decision tree."""
    if n < 30:
        return "WATCH (insufficient sample for any decision)"
    # Disasters: at any sample >= 30, PF < 0.7 = kill candidate
    if pf < 0.7:
        return "KILL_CANDIDATE (PF < 0.7)"
    # Strong: PF > 1.5 with lower-CI WR > 45% = scale candidate (need TENTATIVE+)
    if pf > 1.5 and lo_wr > 0.45 and n >= 100:
        return "GRADUATE / SCALE (PF > 1.5 with WR LCI > 45%)"
    # Moderate edge
    if pf >= 1.3 and lo_wr > 0.40:
        return "WATCH (moderate edge — wait for tier escalation)"
    # Marginal
    if 1.0 < pf < 1.3:
        return "WATCH (marginal — could be noise)"
    return "WATCH"


# ─── Data loading ────────────────────────────────────────────────────
def safe_pnl_net(t: dict) -> float:
    return float(t.get("pnl_dollars_net", t.get("pnl_dollars", 0.0)) or 0.0)


def is_post_b13(t: dict) -> bool:
    return "cost_total_dollars" in t


def load_all_trades(data_root: Path):
    trades_file = data_root / "logs/trade_memory.json"
    if not trades_file.exists():
        return []
    raw = trades_file.read_text(encoding="utf-8")
    try:
        trades = json.loads(raw)
    except Exception:
        return []
    if isinstance(trades, dict):
        trades = trades.get("trades", [])
    if not isinstance(trades, list):
        return []
    return [t for t in trades if isinstance(t, dict)]


def trade_ts(t: dict):
    for k in ("ts", "exit_ts_ct", "exit_time", "entry_time", "recorded_at"):
        v = t.get(k)
        if v is None: continue
        try:
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(v, CT)
            s = str(v).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CT)
            return dt.astimezone(CT)
        except Exception:
            continue
    return None


# ─── Main ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD; only count trades on/after this date")
    ap.add_argument("--post-b13-only", action="store_true",
                    help="Restrict to trades with B13 cost fields (cleaner baseline)")
    args = ap.parse_args()

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=CT)

    data_root = _data_root()
    trades = load_all_trades(data_root)

    # Filter
    filtered = []
    for t in trades:
        if args.post_b13_only and not is_post_b13(t):
            continue
        if since:
            ts = trade_ts(t)
            if ts is None or ts < since:
                continue
        filtered.append(t)

    # Per strategy
    per_strat = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0,
        "gross_win_total": 0.0, "gross_loss_total": 0.0,
        "net_pnl": 0.0,
        "first_ts": None, "last_ts": None,
    })

    for t in filtered:
        strat = t.get("strategy", "unknown")
        s = per_strat[strat]
        pnl = safe_pnl_net(t)
        s["n"] += 1
        s["net_pnl"] += pnl
        if pnl > 0:
            s["wins"] += 1
            s["gross_win_total"] += pnl
        else:
            s["losses"] += 1
            s["gross_loss_total"] += pnl  # negative
        ts = trade_ts(t)
        if ts:
            if s["first_ts"] is None or ts < s["first_ts"]: s["first_ts"] = ts
            if s["last_ts"]  is None or ts > s["last_ts"]:  s["last_ts"]  = ts

    today = datetime.now(CT).date()
    out = data_root / f"out/validation_status_{today}.md"
    out.parent.mkdir(exist_ok=True)
    L = []
    L.append(f"# Phoenix Validation Status — {today}")
    L.append("")
    L.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
    if args.since: L.append(f"_Filter: trades on/after {args.since}_")
    if args.post_b13_only: L.append("_Filter: post-B13 trades only_")
    L.append("")

    # Statistical tier reference
    L.append("## Statistical Tier Reference")
    L.append("")
    L.append("| Tier | Trades | Confidence | Decisions Allowed |")
    L.append("|---|---:|---:|---|")
    L.append("| INSUFFICIENT_SAMPLE | < 30 | none | WATCH only |")
    L.append("| PRELIMINARY | 30-99 | ~70% | WATCH or KILL if PF<0.7 |")
    L.append("| TENTATIVE | 100-384 | ~90% | + GRADUATE candidate |")
    L.append("| VALIDATED | 385-665 | ~95% | + SCALE candidate |")
    L.append("| HIGH_CONFIDENCE | 666+ | ~99% | full confidence |")
    L.append("")
    L.append("_Phoenix's 50-trade project gate sits inside PRELIMINARY._")
    L.append("")

    # Per strategy table
    L.append("## Per Strategy")
    L.append("")
    L.append("| strategy | trades | tier | next threshold | WR (95% CI) | PF | net P&L | decision |")
    L.append("|---|---:|---|---|---|---:|---:|---|")

    for strat, s in sorted(per_strat.items(), key=lambda kv: -kv[1]["n"]):
        n = s["n"]
        if n == 0:
            continue
        wr = s["wins"] / n
        lo, hi = wilson_ci(s["wins"], n)
        if s["gross_loss_total"] < 0:
            pf = s["gross_win_total"] / abs(s["gross_loss_total"])
        else:
            pf = float("inf")
        tier = statistical_tier(n)
        next_t = tier_next_threshold(n)
        next_t_str = f"{next_t} ({next_t - n} more)" if next_t else "—"
        decision = decision_recommendation(n, wr, pf, lo)
        wr_str = f"{wr:.0%} ({lo:.0%}-{hi:.0%})"
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        L.append(f"| `{strat}` | {n} | {tier} | {next_t_str} | {wr_str} | "
                 f"{pf_str} | ${s['net_pnl']:+,.2f} | {decision} |")
    L.append("")

    # Trailing 7-day rate → ETA projection
    L.append("## Trailing 7-Day Trade Rate + ETA")
    L.append("")
    cutoff = datetime.now(CT) - timedelta(days=7)
    rates: dict[str, int] = defaultdict(int)
    for t in filtered:
        ts = trade_ts(t)
        if ts and ts >= cutoff:
            rates[t.get("strategy", "unknown")] += 1
    L.append("| strategy | trades/7d | trades/day | ETA to next tier |")
    L.append("|---|---:|---:|---|")
    for strat in sorted(per_strat.keys()):
        r7 = rates.get(strat, 0)
        per_day = r7 / 7
        next_t = tier_next_threshold(per_strat[strat]["n"])
        if next_t and per_day > 0:
            need = next_t - per_strat[strat]["n"]
            days_eta = math.ceil(need / per_day)
            eta_str = f"{days_eta} days ({need} trades needed)"
        elif not next_t:
            eta_str = "max tier reached"
        else:
            eta_str = "no recent trades — ETA undefined"
        L.append(f"| `{strat}` | {r7} | {per_day:.1f} | {eta_str} |")
    L.append("")

    # Notes
    L.append("## Notes")
    L.append("")
    L.append("- Win rates shown with Wilson 95% CI. With <100 trades the CI is wide;")
    L.append("  e.g. a 50%-WR estimate with n=50 has CI roughly 36%–64%.")
    L.append("- 'Decision' column uses a tier-aware tree (KILL only if PF<0.7,")
    L.append("  GRADUATE only at TENTATIVE+ with PF>1.5 and lower-CI WR>45%).")
    L.append("- Re-run with `--post-b13-only` once you have ≥30 post-B13 trades for")
    L.append("  a cleaner baseline that doesn't mix pre/post cost-accounting eras.")
    L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {out}")
    for strat, s in sorted(per_strat.items(), key=lambda kv: -kv[1]["n"]):
        if s["n"] == 0: continue
        lo, hi = wilson_ci(s["wins"], s["n"])
        print(f"  {strat:30s} n={s['n']:5d}  tier={statistical_tier(s['n']):20s}  "
              f"WR={s['wins']/s['n']:.0%} ({lo:.0%}-{hi:.0%})  net=${s['net_pnl']:+,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
