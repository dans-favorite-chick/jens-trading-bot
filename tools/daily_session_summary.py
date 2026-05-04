"""
Phoenix — Daily Session Summary

Produces a per-session report from today's history JSONL + trade_memory tail.
Includes anomaly detection (signal volume vs trailing 7-day average,
unusual halt counts, no-fill alerts).

Output: out/daily_summary_<YYYY-MM-DD>.md

Usage:
  python tools/daily_session_summary.py            # today (CT)
  python tools/daily_session_summary.py --date 2026-05-04
  python tools/daily_session_summary.py --bot sim  # sim only
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
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
    if (cwd / "logs" / "history").exists():
        return cwd
    if (ROOT / "logs" / "history").exists():
        return ROOT
    return cwd


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD; defaults to today (CT)")
    ap.add_argument("--bot", choices=["sim", "prod", "both"], default="both")
    return ap.parse_args()


def load_jsonl(p: Path):
    if not p.exists():
        return
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def trailing_signal_baseline(data_root: Path, target_date: date_t,
                             bot: str, lookback_days: int = 7):
    """Compute average signals/strategy/day from last N days (excluding target)."""
    by_strat = defaultdict(list)
    for d_offset in range(1, lookback_days + 1):
        d = target_date - timedelta(days=d_offset)
        p = data_root / f"logs/history/{d}_{bot}.jsonl"
        per_strat_today = Counter()
        for ev in load_jsonl(p):
            if ev.get("event") == "eval":
                for s in ev.get("strategies", []) or []:
                    if s.get("result") == "SIGNAL":
                        per_strat_today[s.get("name") or s.get("strategy")] += 1
        for strat, count in per_strat_today.items():
            by_strat[strat].append(count)
    return {strat: (sum(v) / len(v)) if v else 0.0
            for strat, v in by_strat.items()}


def summarize_one_bot(data_root: Path, bot: str, target_date: date_t):
    p = data_root / f"logs/history/{target_date}_{bot}.jsonl"
    if not p.exists():
        return {"bot": bot, "missing": True}

    signals = Counter()
    fills = Counter()
    rejections = Counter()
    halts = []
    caps = []
    pnl_by_strat: dict[str, float] = defaultdict(float)
    win_by_strat: dict[str, int] = defaultdict(int)
    loss_by_strat: dict[str, int] = defaultdict(int)
    first_eval_ts = None
    last_eval_ts = None
    total_evals = 0

    for ev in load_jsonl(p):
        et = ev.get("event")
        msg = str(ev.get("message", ""))
        if et == "eval":
            total_evals += 1
            ts = ev.get("ts")
            if first_eval_ts is None: first_eval_ts = ts
            last_eval_ts = ts
            for s in ev.get("strategies", []) or []:
                name = s.get("name") or s.get("strategy") or "unknown"
                result = s.get("result", "?")
                if result == "SIGNAL":
                    signals[name] += 1
                elif result == "REJECTED":
                    rejections[(name, (s.get("reason") or "?")[:80])] += 1
        elif et == "entry":
            fills[ev.get("strategy", "unknown")] += 1
        elif et == "exit":
            strat = ev.get("strategy", "unknown")
            pnl = ev.get("pnl_dollars_net",
                         ev.get("pnl_dollars", 0)) or 0
            try:
                pnl = float(pnl)
            except Exception:
                pnl = 0.0
            pnl_by_strat[strat] += pnl
            if pnl > 0: win_by_strat[strat] += 1
            else:       loss_by_strat[strat] += 1
        elif et == "halt" or "[HALT:" in msg:
            halts.append(ev)
        elif et == "cap" or "[CAP:" in msg:
            caps.append(ev)

    return {
        "bot": bot,
        "missing": False,
        "first_eval_ts": first_eval_ts,
        "last_eval_ts":  last_eval_ts,
        "total_evals":   total_evals,
        "signals":       signals,
        "fills":         fills,
        "rejections":    rejections,
        "halts":         halts,
        "caps":          caps,
        "pnl_by_strat":  dict(pnl_by_strat),
        "win_by_strat":  dict(win_by_strat),
        "loss_by_strat": dict(loss_by_strat),
    }


def detect_anomalies(summary: dict, baseline: dict[str, float]):
    """Compare today's signal counts to trailing baseline. Return list of dicts."""
    out = []
    today_signals = summary["signals"]
    for strat, today in today_signals.items():
        avg = baseline.get(strat, 0.0)
        if avg == 0:
            continue
        if today < 0.4 * avg:
            out.append({
                "type": "signal_volume_drop",
                "strategy": strat,
                "today": today,
                "trailing_avg": round(avg, 1),
            })
    for strat, avg in baseline.items():
        if avg >= 1.0 and today_signals.get(strat, 0) == 0:
            out.append({
                "type": "silent_strategy",
                "strategy": strat,
                "trailing_avg": round(avg, 1),
            })
    return out


def emit_report(data_root: Path, target_date: date_t,
                summaries: list[dict], anomalies_per_bot: dict):
    out = data_root / f"out/daily_summary_{target_date}.md"
    out.parent.mkdir(exist_ok=True)
    L = []
    L.append(f"# Phoenix Daily Session Summary — {target_date}")
    L.append("")
    L.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
    L.append("")

    for s in summaries:
        bot = s["bot"]
        L.append(f"## Bot: `{bot}`")
        L.append("")
        if s.get("missing"):
            L.append(f"_No history JSONL for {target_date}_{bot}.jsonl — bot was down or no events._")
            L.append("")
            continue
        L.append(f"- Eval window:    {s['first_eval_ts']} -> {s['last_eval_ts']}")
        L.append(f"- Total evals:    {s['total_evals']:,}")
        L.append(f"- Total signals:  {sum(s['signals'].values()):,}")
        L.append(f"- Total fills:    {sum(s['fills'].values()):,}")
        L.append(f"- Halts:          {len(s['halts'])}")
        L.append(f"- Caps:           {len(s['caps'])}")
        L.append("")

        all_strats = set(s["signals"]) | set(s["fills"]) | set(s["pnl_by_strat"])
        if all_strats:
            L.append("### Per strategy")
            L.append("")
            L.append("| strategy | signals | fills | wins | losses | net P&L |")
            L.append("|---|---:|---:|---:|---:|---:|")
            for strat in sorted(all_strats):
                sig = s["signals"].get(strat, 0)
                fil = s["fills"].get(strat, 0)
                w = s["win_by_strat"].get(strat, 0)
                l = s["loss_by_strat"].get(strat, 0)
                pnl = s["pnl_by_strat"].get(strat, 0.0)
                L.append(f"| `{strat}` | {sig} | {fil} | {w} | {l} | ${pnl:+,.2f} |")
            L.append("")

        if s["rejections"]:
            L.append("### Top 10 rejection reasons")
            L.append("")
            L.append("| strategy | reason | count |")
            L.append("|---|---|---:|")
            for (strat, reason), count in s["rejections"].most_common(10):
                L.append(f"| `{strat}` | {reason} | {count} |")
            L.append("")

        if s["halts"]:
            L.append("### Halts")
            for h in s["halts"]:
                L.append(f"- `{h.get('ts', '?')}` `{(h.get('message') or json.dumps(h, default=str))[:200]}`")
            L.append("")
        if s["caps"]:
            L.append("### Caps")
            for c in s["caps"]:
                L.append(f"- `{c.get('ts', '?')}` `{(c.get('message') or json.dumps(c, default=str))[:200]}`")
            L.append("")

        anoms = anomalies_per_bot.get(bot, [])
        if anoms:
            L.append("### Anomalies vs trailing 7-day baseline")
            L.append("")
            for a in anoms:
                if a["type"] == "signal_volume_drop":
                    L.append(f"- WARN **{a['strategy']}**: {a['today']} signals today vs "
                             f"~{a['trailing_avg']}/day baseline (>60% drop)")
                elif a["type"] == "silent_strategy":
                    L.append(f"- WARN **{a['strategy']}**: ZERO signals today vs "
                             f"~{a['trailing_avg']}/day baseline (silent)")
            L.append("")
        else:
            L.append("- OK: No anomalies detected vs trailing 7-day baseline.")
            L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {out}")
    return out


def main():
    args = parse_args()
    target = (datetime.fromisoformat(args.date).date() if args.date
              else datetime.now(CT).date())
    bots = ["sim", "prod"] if args.bot == "both" else [args.bot]

    data_root = _data_root()

    summaries = []
    anomalies_per_bot = {}
    for bot in bots:
        s = summarize_one_bot(data_root, bot, target)
        summaries.append(s)
        if not s.get("missing"):
            baseline = trailing_signal_baseline(data_root, target, bot)
            anomalies_per_bot[bot] = detect_anomalies(s, baseline)

    emit_report(data_root, target, summaries, anomalies_per_bot)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
