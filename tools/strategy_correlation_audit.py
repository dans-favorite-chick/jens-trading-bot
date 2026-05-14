"""Cross-strategy correlation audit (#25, 2026-05-13).

Two strategies that fire on the same setup are doubling risk
exposure without diversifying. This tool scans the trade history and
computes per-pair co-firing frequency to surface those overlaps so
the operator can decide whether to:
  - Retire one of the two (they're effectively the same)
  - Add a "skip if other strategy just fired" gate
  - Accept the overlap as intentional (e.g. confirmation)

Method:
  1. Bin every trade entry timestamp into a configurable window
     (default 5 minutes). Two trades in the same window count as a
     co-fire.
  2. For each strategy pair (A, B), compute:
       co_fires      : # of windows where BOTH fired
       n_A_only      : # of windows where A fired but not B
       n_B_only      : # of windows where B fired but not A
       both_present  : co_fires / (co_fires + n_A_only + n_B_only)
                       (Jaccard index — symmetric, 1.0 = always together)
       confirmed_AB  : co_fires / (co_fires + n_A_only)
                       (conditional prob: given A fired, did B?)

  3. Output a markdown table sorted by Jaccard, top pairs only.

Usage:
    python tools/strategy_correlation_audit.py
    python tools/strategy_correlation_audit.py --window-min 10
    python tools/strategy_correlation_audit.py --out out/correlation.md
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CT = ZoneInfo("America/Chicago")


def _bin_timestamp(t: dict, window_seconds: int) -> int | None:
    """Convert a trade's entry timestamp into a bucket id. Returns None
    if no parseable timestamp."""
    for k in ("entry_time", "ts", "exit_ts_ct", "recorded_at"):
        v = t.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                ts = float(v)
            else:
                s = str(v).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CT)
                ts = dt.timestamp()
            return int(ts // window_seconds)
        except Exception:
            continue
    return None


def compute_correlation(
    trades: list[dict],
    window_seconds: int = 300,
) -> list[dict]:
    """For each pair of strategies, compute co-fire stats.

    Returns sorted list of pair-stat dicts (sorted desc by jaccard).
    Pairs are returned in alphabetical order (a, b) where a < b
    lexically so each pair appears once.
    """
    # strategy -> set of bucket ids it fired in
    fires: dict[str, set[int]] = defaultdict(set)
    for t in trades:
        strat = t.get("strategy")
        if not strat:
            continue
        bucket = _bin_timestamp(t, window_seconds)
        if bucket is None:
            continue
        fires[strat].add(bucket)

    strategies = sorted(fires.keys())
    out: list[dict] = []
    for i, a in enumerate(strategies):
        for b in strategies[i + 1:]:
            sa, sb = fires[a], fires[b]
            inter = sa & sb
            union = sa | sb
            if not union:
                continue
            jaccard = len(inter) / len(union)
            confirmed_ab = len(inter) / len(sa) if sa else 0.0
            confirmed_ba = len(inter) / len(sb) if sb else 0.0
            out.append({
                "a": a, "b": b,
                "co_fires": len(inter),
                "n_a_only": len(sa - sb),
                "n_b_only": len(sb - sa),
                "n_a": len(sa),
                "n_b": len(sb),
                "jaccard": round(jaccard, 3),
                "confirmed_ab": round(confirmed_ab, 3),
                "confirmed_ba": round(confirmed_ba, 3),
            })
    out.sort(key=lambda d: -d["jaccard"])
    return out


def render_markdown(stats: list[dict], window_seconds: int) -> str:
    L: list[str] = []
    L.append("# Phoenix Cross-Strategy Correlation Audit")
    L.append("")
    L.append(f"_Window: {window_seconds}s ({window_seconds // 60} min). "
             f"Pairs sorted by Jaccard index._")
    L.append("")
    L.append("Two strategies firing on the SAME setup are doubling risk "
             "without diversifying. A high Jaccard (e.g. >0.3) means")
    L.append("you may have effectively a single strategy in two implementations.")
    L.append("")
    L.append("- **jaccard**: |A∩B| / |A∪B| — symmetric, 0..1")
    L.append("- **confirmed(A→B)**: given A fired, fraction of times B also did")
    L.append("- **confirmed(B→A)**: given B fired, fraction of times A also did")
    L.append("")
    L.append("| A | B | A fires | B fires | co-fires | jaccard | "
             "conf(A→B) | conf(B→A) |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for s in stats:
        if s["co_fires"] == 0:
            continue
        flag = " ⚠️" if s["jaccard"] >= 0.3 else ""
        L.append(
            f"| `{s['a']}` | `{s['b']}` | {s['n_a']} | {s['n_b']} | "
            f"{s['co_fires']} | {s['jaccard']:.3f}{flag} | "
            f"{s['confirmed_ab']:.3f} | {s['confirmed_ba']:.3f} |"
        )
    if not any(s["co_fires"] for s in stats):
        L.append("| — | — | — | — | — | — | — | — |")
        L.append("")
        L.append("_No co-fires found in the window. Either the strategies "
                 "are well-diversified or the window is too narrow._")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=float, default=5.0,
                    help="Bucket window in minutes (default 5).")
    ap.add_argument("--out", default="-",
                    help="Output path (default: stdout).")
    args = ap.parse_args()
    window_s = int(args.window_min * 60)

    from tools.validation_tracker import load_all_trades, _data_root
    trades = load_all_trades(_data_root())
    stats = compute_correlation(trades, window_seconds=window_s)
    md = render_markdown(stats, window_seconds=window_s)

    if args.out == "-":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        try:
            print(md)
        except UnicodeEncodeError:
            print(md.encode("ascii", errors="replace").decode("ascii"))
    else:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
