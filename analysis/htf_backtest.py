"""
HTF Pattern Scanner Retrofit Backtest
=====================================

Replays JSONL bar events from history logs through the HTFPatternScanner,
then at each trade entry evaluates whether HTF confluence was present.

Compares:
  - All trades (baseline)
  - Trades WITH HTF confluence >= threshold  (scanner says "yes")
  - Trades WITHOUT HTF confluence             (scanner says nothing)

Usage:
    cd C:/Trading Project/phoenix_bot
    python analysis/htf_backtest.py --date 2026-04-13 --bot lab
    python analysis/htf_backtest.py --date 2026-04-13 2026-04-14 --bot lab
"""

import sys
import os
import json
import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.htf_pattern_scanner import HTFPatternScanner
from core.tick_aggregator import Bar

HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "history")


# ── Bar reconstruction from log event ─────────────────────────────────────────
def bar_from_event(event: dict) -> Bar:
    b = Bar()
    b.open  = event.get("open",  0)
    b.high  = event.get("high",  0)
    b.low   = event.get("low",   0)
    b.close = event.get("close", 0)
    b.volume = event.get("volume", 0)
    b.tick_count = event.get("tick_count", 0)
    try:
        b.end_time = datetime.fromisoformat(event["ts"]).timestamp()
    except Exception:
        b.end_time = 0.0
    return b


# ── Load JSONL file ────────────────────────────────────────────────────────────
def load_jsonl(path: str) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


# ── Core replay + analysis ─────────────────────────────────────────────────────
def run_backtest(events: list[dict], htf_threshold: float = 20.0) -> dict:
    """
    Replay events through HTFPatternScanner.
    At each entry event, capture HTF confluence score for that direction.
    Match entries to exits by trade_id or sequential pairing.
    """
    scanner = HTFPatternScanner()

    # Separate events by type
    bar_events   = [e for e in events if e.get("event") == "bar"]
    entry_events = [e for e in events if e.get("event") == "entry"]
    exit_events  = [e for e in events if e.get("event") == "exit"]

    # Build lookup: exit_reason + approximate timestamp mapping
    # Match entries to exits by order of occurrence (sequential)
    print(f"  Bars: {len(bar_events)}, Entries: {len(entry_events)}, Exits: {len(exit_events)}")

    # Sort everything by timestamp
    def get_ts(e):
        try:
            return datetime.fromisoformat(e["ts"]).timestamp()
        except Exception:
            return 0.0

    bar_events.sort(key=get_ts)
    entry_events.sort(key=get_ts)
    exit_events.sort(key=get_ts)

    # --- Replay bars, record HTF state at each entry timestamp ---
    # Process bars in chronological order, storing scanner state snapshots
    # at each entry event (nearest bar before entry)

    # Build timeline: all events sorted together
    all_events = [(get_ts(e), e) for e in bar_events + entry_events]
    all_events.sort(key=lambda x: x[0])

    # At each entry, replay all prior bars up to that timestamp
    entry_htf = {}  # entry_ts -> htf_confluence_result

    processed_bars = set()
    bar_idx = 0
    bar_ts_list = [(get_ts(e), e) for e in bar_events]
    bar_ts_list.sort(key=lambda x: x[0])

    for entry in entry_events:
        entry_ts = get_ts(entry)
        direction = entry.get("direction", "LONG")

        # Feed all bars up to (and including) this timestamp into scanner
        while bar_idx < len(bar_ts_list) and bar_ts_list[bar_idx][0] <= entry_ts:
            _, bar_ev = bar_ts_list[bar_idx]
            tf = bar_ev.get("timeframe", "")
            if tf in ("5m", "15m", "1h", "60m"):
                bar_obj = bar_from_event(bar_ev)
                scanner.on_bar(tf, bar_obj)
            bar_idx += 1

        # Capture HTF confluence at entry time
        htf_conf = scanner.get_confluence_score(direction)
        htf_state = scanner.get_state()
        entry_htf[entry_ts] = {
            "htf_score": htf_conf.get("score", 0),
            "htf_aligned": htf_conf.get("aligned_count", 0),
            "htf_opposing": htf_conf.get("opposing_count", 0),
            "htf_strongest": htf_conf.get("strongest", ""),
            "htf_strongest_tf": htf_conf.get("strongest_tf", ""),
            "htf_active_signals": htf_state.get("active_signals", 0),
        }

    # --- Match entries to exits (sequential pairing) ---
    # Exit events contain pnl_dollars; entries happen before exits
    trades = []
    exit_queue = list(exit_events)  # consume exits in order

    for entry in entry_events:
        entry_ts = get_ts(entry)
        # Find the first exit that is after this entry
        matched_exit = None
        for i, ex in enumerate(exit_queue):
            if get_ts(ex) > entry_ts:
                matched_exit = exit_queue.pop(i)
                break

        htf_data = entry_htf.get(entry_ts, {})
        trade = {
            "ts":        entry.get("ts"),
            "strategy":  entry.get("strategy"),
            "direction": entry.get("direction"),
            "confidence": entry.get("confidence", 0),
            "entry_score": entry.get("entry_score", 0),
            "pnl":       matched_exit.get("pnl_dollars", 0) if matched_exit else None,
            "exit_reason": matched_exit.get("exit_reason", "?") if matched_exit else "unmatched",
            "duration_s": matched_exit.get("duration_s", 0) if matched_exit else 0,
            **htf_data,
        }
        trades.append(trade)

    # --- Compute statistics ---
    def stats(trade_subset: list[dict]) -> dict:
        if not trade_subset:
            return {"n": 0, "wr": 0, "avg_pnl": 0, "total_pnl": 0, "pf": 0}
        matched = [t for t in trade_subset if t["pnl"] is not None]
        if not matched:
            return {"n": len(trade_subset), "wr": 0, "avg_pnl": 0, "total_pnl": 0, "pf": 0}
        wins  = [t["pnl"] for t in matched if t["pnl"] > 0]
        losses= [t["pnl"] for t in matched if t["pnl"] <= 0]
        wr    = len(wins) / len(matched) * 100 if matched else 0
        avg   = sum(t["pnl"] for t in matched) / len(matched)
        total = sum(t["pnl"] for t in matched)
        gross_win  = sum(wins)  if wins   else 0
        gross_loss = abs(sum(losses)) if losses else 1e-9
        pf = gross_win / gross_loss if gross_loss > 0 else 0
        return {
            "n": len(matched),
            "wr": round(wr, 1),
            "avg_pnl": round(avg, 2),
            "total_pnl": round(total, 2),
            "pf": round(pf, 2),
            "wins": len(wins),
            "losses": len(losses),
        }

    confirmed   = [t for t in trades if t.get("htf_score", 0) >= htf_threshold]
    unconfirmed = [t for t in trades if t.get("htf_score", 0) < htf_threshold]

    # Per-strategy breakdown
    by_strategy = defaultdict(list)
    by_strategy_htf = defaultdict(list)
    for t in trades:
        by_strategy[t["strategy"]].append(t)
        if t.get("htf_score", 0) >= htf_threshold:
            by_strategy_htf[t["strategy"]].append(t)

    return {
        "all_trades":       stats(trades),
        "htf_confirmed":    stats(confirmed),
        "htf_unconfirmed":  stats(unconfirmed),
        "by_strategy":      {s: stats(v) for s, v in by_strategy.items()},
        "by_strategy_htf":  {s: stats(v) for s, v in by_strategy_htf.items()},
        "htf_score_dist":   [round(t.get("htf_score", 0), 1) for t in trades],
        "n_trades_total":   len(trades),
        "n_bars_replayed":  len(bar_events),
        "htf_threshold":    htf_threshold,
    }


# ── Report formatting ──────────────────────────────────────────────────────────
def print_report(result: dict, label: str):
    print(f"\n{'='*60}")
    print(f"  HTF BACKTEST — {label}")
    print(f"{'='*60}")
    a = result["all_trades"]
    c = result["htf_confirmed"]
    u = result["htf_unconfirmed"]
    thr = result["htf_threshold"]

    print(f"\n  {'Category':<25} {'N':>5} {'WR%':>6} {'Avg P&L':>9} {'Total':>9} {'PF':>6}")
    print(f"  {'-'*60}")

    def row(label, s):
        if s["n"] == 0:
            print(f"  {label:<25} {'0':>5} {'—':>6} {'—':>9} {'—':>9} {'—':>6}")
        else:
            print(f"  {label:<25} {s['n']:>5} {s['wr']:>6.1f} {s['avg_pnl']:>9.2f} "
                  f"{s['total_pnl']:>9.2f} {s['pf']:>6.2f}")

    row("ALL TRADES", a)
    row(f"HTF confirmed (>={thr})", c)
    row(f"HTF unconfirmed (<{thr})", u)

    print(f"\n  Strategy breakdown (ALL | WITH HTF >= {thr}):")
    print(f"  {'-'*60}")
    all_strats = set(list(result["by_strategy"].keys()) + list(result["by_strategy_htf"].keys()))
    for strat in sorted(all_strats):
        sa = result["by_strategy"].get(strat, {"n":0,"wr":0,"total_pnl":0})
        sh = result["by_strategy_htf"].get(strat, {"n":0,"wr":0,"total_pnl":0})
        print(f"  {strat:<25}  all: n={sa['n']} wr={sa.get('wr',0):.0f}% pnl=${sa.get('total_pnl',0):.2f}"
              f"  |  htf: n={sh['n']} wr={sh.get('wr',0):.0f}% pnl=${sh.get('total_pnl',0):.2f}")

    # HTF impact summary
    if c["n"] > 0 and u["n"] > 0:
        wr_delta = c["wr"] - u["wr"]
        pnl_delta = c["avg_pnl"] - u["avg_pnl"]
        print(f"\n  HTF EDGE SUMMARY:")
        print(f"    Win rate delta (confirmed - unconfirmed): {wr_delta:+.1f}%")
        print(f"    Avg P&L delta:                          ${pnl_delta:+.2f}/trade")
        if wr_delta > 5:
            print(f"    VERDICT: HTF confirmation adds meaningful edge (+{wr_delta:.0f}% WR)")
        elif wr_delta > 0:
            print(f"    VERDICT: Modest HTF edge ({wr_delta:+.1f}% WR) — needs more data")
        else:
            print(f"    VERDICT: No HTF edge detected (need more data or threshold tuning)")

    scores = result["htf_score_dist"]
    if scores:
        nonzero = [s for s in scores if s > 0]
        print(f"\n  HTF Score distribution:")
        print(f"    Trades with score=0 (no patterns active): {scores.count(0)}/{len(scores)}")
        if nonzero:
            print(f"    Non-zero scores: min={min(nonzero):.0f} avg={sum(nonzero)/len(nonzero):.0f} max={max(nonzero):.0f}")
        print(f"    Trades above threshold ({thr}): {len([s for s in scores if s >= thr])}/{len(scores)}")


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="HTF Pattern Scanner backtest")
    parser.add_argument("--date", nargs="+", default=["2026-04-13", "2026-04-14"],
                        help="Date(s) to backtest (YYYY-MM-DD)")
    parser.add_argument("--bot", default="lab", choices=["lab", "prod", "both"],
                        help="Which bot's log to use")
    parser.add_argument("--threshold", type=float, default=20.0,
                        help="HTF confluence score threshold (default: 20)")
    args = parser.parse_args()

    bots = ["lab", "prod"] if args.bot == "both" else [args.bot]

    for bot in bots:
        all_events = []
        dates_loaded = []
        for date in args.date:
            path = os.path.join(HISTORY_DIR, f"{date}_{bot}.jsonl")
            if os.path.exists(path):
                events = load_jsonl(path)
                all_events.extend(events)
                dates_loaded.append(date)
                print(f"  Loaded {len(events)} events from {os.path.basename(path)}")
            else:
                print(f"  WARNING: {path} not found, skipping")

        if not all_events:
            print(f"  No data found for {bot} bot")
            continue

        label = f"{', '.join(dates_loaded)} | {bot.upper()} bot"
        print(f"\nRunning backtest: {label}")
        result = run_backtest(all_events, htf_threshold=args.threshold)
        print_report(result, label)

    print()


if __name__ == "__main__":
    main()
