"""Read-only diagnostic -- why does a high-WR strategy lose money?

Initial use case: vwap_pullback shows ~62% WR but is net NEGATIVE
(~-$284 across 48 trades, per validation_tracker --post-b13-only).
The math: 0.62*W - 0.38*L = -$5.92/trade -> realized R:R ~= 0.135 (vs
configured target_rr=1.8). Winners are getting cut to ~$2-3 while
losers hit full stops. This script identifies WHERE the bleed occurs.

Sections:
  A. Headline numbers (WR, avg winner/loser, realized R:R, breakeven WR)
  B. Breakdown by exit_reason -- THE key section, with pattern flags
  C. P&L distribution histogram ($5 buckets)
  D. Top 5 winners / Top 5 losers (full row context)
  E. Score distribution among losers (entry_score buckets)

Data source: core.trade_memory.load_all_trades() -- the canonical merger
(legacy logs/trade_memory.json + every per-bot logs/trade_memory_<bot>.json).
Established as the only correct read path post-2026-05-12 audit. If
that import fails, aborts with a clear message -- there is no separate
"fallback" needed because every reader in the codebase routes through
this same function.

Read-only. No bots/ imports, no writes, stdout only.

Usage:
    python tools/diagnose_vwap_pullback.py
    python tools/diagnose_vwap_pullback.py --strategy noise_area
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# -----------------------------------------------------------------------
# Data loading
# -----------------------------------------------------------------------

def _data_root() -> Path:
    """Detect phoenix_bot root via existence of any trade_memory file
    (legacy `trade_memory.json` OR per-bot `trade_memory_<bot>.json`)."""
    def _has_tm(p: Path) -> bool:
        logs = p / "logs"
        if not logs.is_dir():
            return False
        if (logs / "trade_memory.json").exists():
            return True
        try:
            for f in logs.iterdir():
                if (f.name.startswith("trade_memory_")
                        and f.name.endswith(".json")):
                    return True
        except OSError:
            pass
        return False
    cwd = Path.cwd()
    if _has_tm(cwd):
        return cwd
    if _has_tm(ROOT):
        return ROOT
    return cwd


def load_trades(strategy: str) -> list[dict]:
    """Return strategy-filtered, post-B13-only trades.

    Mirrors validation_tracker.py's filter exactly:
      - strategy match
      - is_post_b13(t) = "cost_total_dollars" in t
    """
    root = _data_root()
    logs = root / "logs"
    if not logs.is_dir():
        raise SystemExit(
            f"ERROR: no logs/ directory at {root}.\n"
            f"  Looked in cwd={Path.cwd()} and ROOT={ROOT}.\n"
            f"  Run this script from inside phoenix_bot/ root."
        )
    try:
        from core.trade_memory import load_all_trades
    except ImportError as e:
        raise SystemExit(
            f"ERROR: cannot import core.trade_memory: {e}\n"
            f"  Run from phoenix_bot/ root so 'core' is importable."
        )

    rows = load_all_trades(logs_dir=str(logs))
    if not isinstance(rows, list) or not rows:
        raise SystemExit(
            f"ERROR: no trade rows loaded from {logs}/trade_memory*.json.\n"
            f"  Check the files exist and contain a JSON list."
        )

    out: list[dict] = []
    for t in rows:
        if not isinstance(t, dict):
            continue
        if t.get("strategy") != strategy:
            continue
        if "cost_total_dollars" not in t:   # post-B13 gate
            continue
        out.append(t)
    return out


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _safe_pnl(t: dict) -> float:
    """Mirrors validation_tracker.safe_pnl_net."""
    try:
        return float(t.get("pnl_dollars_net", t.get("pnl_dollars", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hold_minutes(t: dict):
    et = t.get("entry_time")
    xt = t.get("exit_time")
    if (isinstance(et, (int, float)) and isinstance(xt, (int, float))
            and xt > et):
        return (xt - et) / 60.0
    return None


def _entry_score(t: dict):
    """Return entry_score if present, else None.
    Checked at top-level and inside `metadata`."""
    for k in ("entry_score", "score"):
        v = t.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    md = t.get("metadata") or {}
    if isinstance(md, dict):
        for k in ("entry_score", "score"):
            v = md.get(k)
            if isinstance(v, (int, float)):
                return float(v)
    return None


# -----------------------------------------------------------------------
# Sections
# -----------------------------------------------------------------------

def section_a(trades: list[dict]) -> None:
    print("=" * 76)
    print("SECTION A -- HEADLINE NUMBERS")
    print("=" * 76)
    pnls = [_safe_pnl(t) for t in trades]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakevens = [p for p in pnls if p == 0]
    total = sum(pnls)

    wr = (len(wins) / n * 100) if n else 0
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0  # negative
    rr = (avg_w / abs(avg_l)) if losses and avg_l != 0 else float("inf")

    if wins and losses:
        be_wr = abs(avg_l) / (avg_w + abs(avg_l)) * 100
    else:
        be_wr = float("nan")

    print(f"  Total trades:           {n}")
    print(f"  Wins:                   {len(wins)} ({wr:.1f}%)")
    print(f"  Losses:                 {len(losses)}")
    if breakevens:
        print(f"  Breakeven (pnl=0):      {len(breakevens)}")
    print(f"  Net P&L:                ${total:+.2f}")
    if n:
        print(f"  Avg $/trade:            ${total/n:+.2f}")
    print(f"  Avg winner:             ${avg_w:+.2f}")
    print(f"  Avg loser:              ${avg_l:+.2f}")
    print(f"  Realized R:R:           {rr:.3f}")
    if not (be_wr != be_wr):  # NaN check
        print(f"  Break-even WR needed:   {be_wr:.1f}%  "
              f"(at current realized R:R of {rr:.3f})")
        if wr < be_wr:
            print(f"  -> Current WR {wr:.1f}% is BELOW break-even {be_wr:.1f}% "
                  f"-- losing structurally, not bad luck.")
    print()


def section_b(trades: list[dict]) -> None:
    print("=" * 76)
    print("SECTION B -- BREAKDOWN BY exit_reason  (THE KEY SECTION)")
    print("=" * 76)
    by_reason: dict[str, dict] = {}
    for t in trades:
        reason = t.get("exit_reason") or t.get("exit_type") or "(unknown)"
        b = by_reason.setdefault(
            reason, {"pnls": [], "holds": [], "wins": 0}
        )
        p = _safe_pnl(t)
        b["pnls"].append(p)
        if p > 0:
            b["wins"] += 1
        h = _hold_minutes(t)
        if h is not None:
            b["holds"].append(h)

    rows = []
    for reason, b in by_reason.items():
        n = len(b["pnls"])
        wr = (b["wins"] / n * 100) if n else 0
        avg = sum(b["pnls"]) / n if n else 0
        total = sum(b["pnls"])
        avg_hold = (sum(b["holds"]) / len(b["holds"])) if b["holds"] else 0
        rows.append((reason, n, wr, avg, total, avg_hold))
    rows.sort(key=lambda x: -x[1])  # count desc

    print(f"  {'exit_reason':<24} {'count':>5} {'wr%':>6} "
          f"{'avg$':>10} {'total$':>11} {'avg_hold_min':>14}")
    print(f"  {'-'*24} {'-'*5} {'-'*6} "
          f"{'-'*10} {'-'*11} {'-'*14}")
    for reason, n, wr, avg, total, ah in rows:
        print(f"  {str(reason):<24} {n:>5} {wr:>5.1f}% "
              f"${avg:>+8.2f} ${total:>+10.2f} {ah:>14.1f}")
    print()

    # Pattern flags
    print("  PATTERN FLAGS:")
    flagged = False
    for reason, n, wr, avg, total, ah in rows:
        rl = str(reason).lower()
        # Scale-out + BE trap
        if any(k in rl for k in ("partial", "scale", "breakeven", "be_", "_be")):
            if 0 <= avg <= 10 and n >= 3:
                print(f"    !! SCALE-OUT / BE-TRAP suspect -- '{reason}': "
                      f"avg=${avg:+.2f} over {n} trades")
                flagged = True
        # Tight trail kicking in early
        if "trail" in rl and avg < 10 and n >= 3:
            print(f"    !! TIGHT-TRAIL EARLY suspect -- '{reason}': "
                  f"avg=${avg:+.2f} over {n} trades (hold {ah:.1f}m)")
            flagged = True
        # Time stop premature
        if "time" in rl and abs(avg) < 5 and n >= 3:
            print(f"    !! TIME-STOP PREMATURE suspect -- '{reason}': "
                  f"avg=${avg:+.2f} over {n} trades (hold {ah:.1f}m)")
            flagged = True
        # EoD flatten chopping
        if any(k in rl for k in ("eod", "session_end", "flatten",
                                 "daily_flat")) and 0 <= avg <= 10 and n >= 2:
            print(f"    !! EoD-FLATTEN CHOPPING suspect -- '{reason}': "
                  f"avg=${avg:+.2f} over {n} trades")
            flagged = True
        # Target hit working fine
        if any(k in rl for k in ("target", "tp_hit")) and avg >= 15:
            print(f"    OK target_hit normal -- '{reason}': "
                  f"avg=${avg:+.2f} over {n} trades "
                  f"(if this dominates volume, bug is elsewhere)")
            flagged = True
    if not flagged:
        print("    (no obvious patterns flagged)")
    print()


def section_c(trades: list[dict]) -> None:
    print("=" * 76)
    print("SECTION C -- P&L DISTRIBUTION ($5 BUCKETS, -$100..+$100)")
    print("=" * 76)
    buckets: dict[int, int] = {}
    under = 0
    over = 0
    for t in trades:
        p = _safe_pnl(t)
        if p < -100:
            under += 1
            continue
        if p > 100:
            over += 1
            continue
        # Floor to nearest $5 multiple, handling negative correctly
        # so e.g. -3.5 lands in [-5, 0) and +3.5 lands in [0, +5)
        if p >= 0:
            low = int(p // 5) * 5
        else:
            low = -((int(-p) // 5 + (1 if (-p) % 5 else 0)) * 5)
        buckets[low] = buckets.get(low, 0) + 1

    counts_all = list(buckets.values()) + [under, over]
    max_count = max(counts_all) if counts_all else 1
    scale = 40.0 / max_count if max_count else 1.0

    print(f"  {'bucket':>14}  {'count':>5}  {'histogram':<42}")
    print(f"  {'-'*14}  {'-'*5}  {'-'*42}")
    if under:
        bar = "#" * max(1, int(under * scale))
        print(f"  {'< -100':>14}  {under:>5}  {bar}")
    for low in range(-100, 100, 5):
        cnt = buckets.get(low, 0)
        if cnt == 0:
            continue
        label = f"{low:+d}..{low+5:+d}"
        bar = "#" * max(1, int(cnt * scale))
        print(f"  {label:>14}  {cnt:>5}  {bar}")
    if over:
        bar = "#" * max(1, int(over * scale))
        print(f"  {'> +100':>14}  {over:>5}  {bar}")
    print()


def section_d(trades: list[dict]) -> None:
    print("=" * 76)
    print("SECTION D -- TOP 5 WINNERS / TOP 5 LOSERS")
    print("=" * 76)
    enriched = [(t, _safe_pnl(t)) for t in trades]
    winners = sorted(enriched, key=lambda x: -x[1])[:5]
    losers = sorted(enriched, key=lambda x: x[1])[:5]

    def _print_header():
        print(f"  {'timestamp':<17} {'dir':<5} "
              f"{'entry':>8} -> {'exit':>8}  "
              f"{'stop_tk':<7} {'reason':<22} "
              f"{'pnl':>9} {'hold':>7} {'confs':>5}")
        print(f"  {'-'*17} {'-'*5} "
              f"{'-'*8}    {'-'*8}  "
              f"{'-'*7} {'-'*22} "
              f"{'-'*9} {'-'*7} {'-'*5}")

    def _print_row(t: dict, pnl: float) -> None:
        et = t.get("entry_time") or 0
        if isinstance(et, (int, float)) and et:
            ts = datetime.fromtimestamp(et).strftime("%Y-%m-%d %H:%M")
        else:
            ts = "n/a"
        d = str(t.get("direction") or "?")
        ep = float(t.get("entry_price") or 0)
        xp = float(t.get("exit_price") or 0)
        stop_ticks = t.get("stop_ticks")
        st = str(stop_ticks) if stop_ticks is not None else "?"
        reason = str(t.get("exit_reason") or t.get("exit_type") or "?")[:22]
        hold = _hold_minutes(t)
        hold_s = f"{hold:.1f}m" if hold is not None else "n/a"
        confs = t.get("confluences") or []
        nc = len(confs) if isinstance(confs, list) else 0
        print(f"  {ts:<17} {d:<5} "
              f"{ep:>8.2f} -> {xp:>8.2f}  "
              f"{st:<7} {reason:<22} "
              f"${pnl:>+7.2f} {hold_s:>7} {nc:>5}")

    print("  TOP 5 WINNERS:")
    _print_header()
    for t, p in winners:
        _print_row(t, p)
    print()
    print("  TOP 5 LOSERS:")
    _print_header()
    for t, p in losers:
        _print_row(t, p)
    print()


def section_e(trades: list[dict]) -> None:
    print("=" * 76)
    print("SECTION E -- SCORE DISTRIBUTION (LOSERS ONLY)")
    print("=" * 76)
    losers = [t for t in trades if _safe_pnl(t) < 0]
    if not losers:
        print("  (no losing trades)")
        print()
        return

    bucket_order = ["30-39", "40-49", "50-59", "60+", "(no score)"]
    buckets: dict[str, list[dict]] = {k: [] for k in bucket_order}
    for t in losers:
        s = _entry_score(t)
        if s is None:
            buckets["(no score)"].append(t)
        elif s < 40:
            buckets["30-39"].append(t)
        elif s < 50:
            buckets["40-49"].append(t)
        elif s < 60:
            buckets["50-59"].append(t)
        else:
            buckets["60+"].append(t)

    print(f"  {'score bucket':<15} {'count':>5} "
          f"{'avg_loss':>11} {'total_loss':>13}")
    print(f"  {'-'*15} {'-'*5} {'-'*11} {'-'*13}")
    any_printed = False
    for label in bucket_order:
        ts = buckets[label]
        if not ts:
            continue
        any_printed = True
        pnls = [_safe_pnl(t) for t in ts]
        avg = sum(pnls) / len(pnls)
        total = sum(pnls)
        print(f"  {label:<15} {len(ts):>5} "
              f"${avg:>+10.2f} ${total:>+12.2f}")
    if not any_printed:
        print("  (all losers have no entry_score field -- "
              "strategy doesn't persist score)")
    print()


# -----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only diagnostic -- why does a positive-WR strategy "
                    "lose money?"
    )
    ap.add_argument(
        "--strategy", default="vwap_pullback",
        help="Strategy name to diagnose (default: vwap_pullback)",
    )
    args = ap.parse_args()

    print()
    print(f"=== DIAGNOSTIC: strategy='{args.strategy}', post-B13 only ===")
    print()

    trades = load_trades(args.strategy)
    if not trades:
        print(f"No post-B13 trades found for strategy='{args.strategy}'.")
        print(f"  Check the strategy name and that trades carry a "
              f"'cost_total_dollars' field (the post-B13 marker).")
        return 1

    section_a(trades)
    section_b(trades)
    section_c(trades)
    section_d(trades)
    section_e(trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
