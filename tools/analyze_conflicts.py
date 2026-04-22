#!/usr/bin/env python
"""
Phoenix Bot — Historical Directional Conflict Analyzer (S6 / B72)

Reads logs/conflicts/YYYY-MM-DD.jsonl files over a window and prints:
  - total events / conflict minutes
  - commission drag estimate (2 * $0.86 * conflict_count)
  - win rate of LONG side vs SHORT side during conflicts
  - top 5 most-conflicting strategy pairs
  - recommendation flag if a strategy loses >60% of its conflict halves

Usage:
    python tools/analyze_conflicts.py --days 14
    python tools/analyze_conflicts.py --strategy bias_momentum
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CONFLICT_DIR = os.path.join(REPO_ROOT, "logs", "conflicts")
TRADE_MEMORY_PATH = os.path.join(REPO_ROOT, "logs", "trade_memory.json")
COMMISSION_PER_SIDE = 0.86


def _load_window(days: int) -> list[dict]:
    events: list[dict] = []
    if not os.path.isdir(CONFLICT_DIR):
        return events
    cutoff = date.today() - timedelta(days=days)
    for fname in sorted(os.listdir(CONFLICT_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        try:
            d = datetime.strptime(fname[:-6], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        path = os.path.join(CONFLICT_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
    return events


def _load_trade_pnl() -> dict[str, float]:
    if not os.path.exists(TRADE_MEMORY_PATH):
        return {}
    try:
        with open(TRADE_MEMORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    trades = data if isinstance(data, list) else data.get("trades", [])
    out: dict[str, float] = {}
    for t in trades or []:
        tid = t.get("trade_id")
        if tid:
            try:
                out[tid] = float(t.get("pnl_dollars") or 0)
            except Exception:
                pass
    return out


def analyze(days: int = 14, strategy_filter: str | None = None) -> dict:
    events = _load_window(days)
    pnl_by_tid = _load_trade_pnl()

    opened = [e for e in events if e.get("event") == "conflict_opened"]
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    strategy_halves: dict[str, dict[str, int]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "open": 0})
    long_wins = long_losses = 0
    short_wins = short_losses = 0
    total_overlap_s = 0.0
    involved_tids: set[str] = set()

    for ev in opened:
        for c in ev.get("conflicts", []):
            sa, sb = c.get("strategy_a"), c.get("strategy_b")
            if strategy_filter and strategy_filter not in (sa, sb):
                continue
            pair = tuple(sorted([sa or "?", sb or "?"]))
            pair_counts[pair] += 1
            total_overlap_s += float(c.get("overlap_seconds") or 0)
            for tid_key, strat_key, dir_key in (
                ("trade_id_a", "strategy_a", "dir_a"),
                ("trade_id_b", "strategy_b", "dir_b"),
            ):
                tid = c.get(tid_key)
                strat = c.get(strat_key)
                direction = c.get(dir_key)
                if not tid:
                    continue
                involved_tids.add(tid)
                pnl = pnl_by_tid.get(tid)
                if pnl is None:
                    strategy_halves[strat]["open"] += 1
                    continue
                won = pnl > 0
                if won:
                    strategy_halves[strat]["wins"] += 1
                else:
                    strategy_halves[strat]["losses"] += 1
                if direction == "LONG":
                    long_wins += int(won)
                    long_losses += int(not won)
                else:
                    short_wins += int(won)
                    short_losses += int(not won)

    n_conflicts = sum(pair_counts.values())
    commission_drag = 2 * COMMISSION_PER_SIDE * n_conflicts
    long_wr = (long_wins / (long_wins + long_losses) * 100) if (long_wins + long_losses) else 0.0
    short_wr = (short_wins / (short_wins + short_losses) * 100) if (short_wins + short_losses) else 0.0

    flagged: list[str] = []
    for strat, d in strategy_halves.items():
        total = d["wins"] + d["losses"]
        if total >= 5:
            loss_rate = d["losses"] / total
            if loss_rate > 0.6:
                flagged.append(f"{strat} ({d['losses']}/{total} = {loss_rate*100:.0f}% loss)")

    return {
        "days": days,
        "events": len(opened),
        "n_conflicts": n_conflicts,
        "overlap_minutes": total_overlap_s / 60.0,
        "commission_drag": commission_drag,
        "long_wr": long_wr,
        "short_wr": short_wr,
        "long_wl": (long_wins, long_losses),
        "short_wl": (short_wins, short_losses),
        "top_pairs": sorted(pair_counts.items(), key=lambda kv: -kv[1])[:5],
        "strategy_halves": dict(strategy_halves),
        "flagged": flagged,
    }


def _print_report(r: dict, strategy_filter: str | None) -> None:
    filt = f" (strategy={strategy_filter})" if strategy_filter else ""
    print(f"=== Directional Conflict Analysis — last {r['days']} days{filt} ===")
    print(f"Conflict-opened events : {r['events']}")
    print(f"Total conflict pairs   : {r['n_conflicts']}")
    print(f"Cumulative overlap     : {r['overlap_minutes']:.1f} min")
    print(f"Commission drag (est)  : ${r['commission_drag']:.2f}")
    print()
    lw, ll = r["long_wl"]
    sw, sl = r["short_wl"]
    print(f"LONG side  W/L: {lw}/{ll}  WR={r['long_wr']:.1f}%")
    print(f"SHORT side W/L: {sw}/{sl}  WR={r['short_wr']:.1f}%")
    print()
    if r["top_pairs"]:
        print("Top conflicting pairs:")
        for (a, b), n in r["top_pairs"]:
            print(f"  {n:>4}x   {a}  vs  {b}")
    else:
        print("No conflicting pairs in window.")
    print()
    if r["flagged"]:
        print("⚠️  Low-quality conflict halves (>60% loss rate, n>=5):")
        for line in r["flagged"]:
            print(f"  - {line}")
    else:
        print("No strategies flagged as low-quality conflict halves.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze directional conflict logs")
    ap.add_argument("--days", type=int, default=14,
                    help="Lookback window in days (default: 14)")
    ap.add_argument("--strategy", type=str, default=None,
                    help="Filter to conflicts involving this strategy")
    args = ap.parse_args()
    report = analyze(days=args.days, strategy_filter=args.strategy)
    _print_report(report, args.strategy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
