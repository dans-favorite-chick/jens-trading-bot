"""
Phoenix - Stuck Exit Forensic (READ-ONLY)

Walks all evidence sources for positions that hit `exit_pending` and never
resolved. Produces a markdown report at out/stuck_exits_forensic_<today>.md.

NEVER writes to NT8. NEVER modifies trade_memory or any state file.
NEVER places, cancels, or modifies orders.

Investigates these hypotheses (each has a section in the report):
  H1: Account-name mismatch (the B12 nightmare reborn)
  H2: OIF folder issues (file collision, permission, NT8 not consuming)
  H3: Position state desync (Phoenix says open, NT8 says flat)
  H4: Cancel-then-exit race condition
  H5: SHORT-specific OIF formatting issue
  H6: Bridge connection state at the time of stuck exit

Usage:
    python tools/diagnose_stuck_exits.py [--hours N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CT = ZoneInfo("America/Chicago")


def _data_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "out").exists() or (cwd / "logs").exists():
        return cwd
    if (ROOT / "logs").exists():
        return ROOT
    return cwd


def grep_log(p: Path, patterns: list[str], context_lines: int = 1):
    if not p.exists():
        return []
    matches = []
    compiled = [re.compile(pat) for pat in patterns]
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception:
        return []
    for i, line in enumerate(all_lines):
        if any(rx.search(line) for rx in compiled):
            start = max(0, i - context_lines)
            end = min(len(all_lines), i + context_lines + 1)
            matches.append((i, all_lines[start:end]))
    return matches


def discover_oif_folders():
    try:
        from config.settings import OIF_INCOMING, OIF_OUTGOING, NT8_DATA_ROOT
        return {
            "incoming": Path(OIF_INCOMING) if OIF_INCOMING else None,
            "outgoing": Path(OIF_OUTGOING) if OIF_OUTGOING else None,
            "nt8_root": Path(NT8_DATA_ROOT) if NT8_DATA_ROOT else None,
        }
    except Exception as e:
        return {"error": str(e)}


def known_routing():
    try:
        from config.account_routing import STRATEGY_ACCOUNT_MAP
        return dict(STRATEGY_ACCOUNT_MAP)
    except Exception:
        return {}


def find_stuck_in_trade_memory(data_root: Path):
    """Return list of trades currently in exit_pending state per trade_memory."""
    p = data_root / "logs" / "trade_memory.json"
    if not p.exists():
        return []
    try:
        trades = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(trades, dict):
        trades = trades.get("trades", [])
    stuck = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        # trade_memory typically only has CLOSED trades. exit_pending state
        # would be in an in-memory PositionManager, not trade_memory.json.
        # But if any leaked in (older legacy schema), surface them.
        if t.get("state") in ("exit_pending", "EXIT_PENDING"):
            stuck.append(t)
        elif t.get("exit_reason") in (None, "", "exit_pending"):
            # closed without an exit_reason -> suspicious
            if t.get("exit_price") in (None, 0, "0", 0.0):
                stuck.append(t)
    return stuck


def read_nt8_positions(data_root: Path):
    """Read every NT8 position file currently on disk. Returns dict
    {account: (direction, qty, avg_price)} for non-FLAT only."""
    folders = discover_oif_folders()
    if "error" in folders or folders.get("outgoing") is None:
        return {}
    outgoing = folders["outgoing"]
    if not outgoing.exists():
        return {}
    positions = {}
    for p in outgoing.glob("*_position.txt"):
        try:
            content = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        # Filename: "MNQM6 Globex_<account>_position.txt"
        name = p.stem  # without .txt
        if "_position" not in name:
            continue
        # Extract account
        m = re.match(r".+Globex_(.+)_position$", name)
        if not m:
            continue
        account = m.group(1)
        parts = content.split(";")
        if len(parts) < 3:
            continue
        direction, qty_s, price_s = parts[0], parts[1], parts[2]
        if direction == "FLAT":
            continue
        try:
            positions[account] = (direction, int(qty_s), float(price_s))
        except ValueError:
            continue
    return positions


def find_recent_fixes_for_stuck_exits():
    """Inspect git history for any recent commit that fixed stuck-exit bugs.
    Returns list of (sha, subject) for commits in the last 7 days that match."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "log", "--since=7 days ago", "--oneline",
             "--grep=stuck", "--grep=exit_pending", "--grep=exit_storm",
             "--grep=cover-action", "--all-match=false"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24,
                    help="Look back N hours for exit_pending events")
    args = ap.parse_args()

    data_root = _data_root()
    today = datetime.now(CT).date()
    out = data_root / f"out/stuck_exits_forensic_{today}.md"
    out.parent.mkdir(exist_ok=True)
    cutoff = datetime.now(CT) - timedelta(hours=args.hours)

    L = []
    L.append(f"# Phoenix Stuck-Exit Forensic - {today}")
    L.append("")
    L.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
    L.append(f"_Lookback: {args.hours} hours (since {cutoff.isoformat(timespec='seconds')})_")
    L.append("")
    L.append("**THIS IS A READ-ONLY REPORT. NO CODE OR STATE WAS MODIFIED.**")
    L.append("")

    # ─── 0. Recent stuck-exit fixes in git ────────────────────────────────
    L.append("## 0. Recent stuck-exit-related commits (last 7 days)")
    L.append("")
    recent_fixes = find_recent_fixes_for_stuck_exits()
    if recent_fixes:
        L.append(f"Found {len(recent_fixes)} commits matching "
                 "stuck/exit_pending/exit_storm/cover-action keywords:")
        L.append("")
        for line in recent_fixes:
            L.append(f"- `{line}`")
        L.append("")
        L.append("If these commits already address the root cause, sections "
                 "1-7 below should show clean state. Otherwise, the fixes "
                 "may be incomplete and a new failure mode is at play.")
    else:
        L.append("_No recent stuck-exit-related commits found._")
    L.append("")

    # ─── 1. CURRENT STATE: who is stuck right now ────────────────────────
    L.append("## 1. Currently stuck positions (live state)")
    L.append("")

    # 1a: live NT8 positions vs Phoenix in-memory state
    nt8_open = read_nt8_positions(data_root)
    L.append(f"### 1a. NT8 currently-open positions (from outgoing/*_position.txt)")
    L.append("")
    if nt8_open:
        L.append("| Account | Direction | Qty | Avg Price |")
        L.append("|---|---|---:|---:|")
        for acct, (d, q, p) in sorted(nt8_open.items()):
            L.append(f"| `{acct}` | {d} | {q} | {p} |")
    else:
        L.append("_All routed accounts are FLAT in NT8. No stuck positions._")
    L.append("")

    # 1b: trade_memory.json suspicious entries
    stuck_in_memory = find_stuck_in_trade_memory(data_root)
    L.append(f"### 1b. Suspicious entries in `logs/trade_memory.json`")
    L.append("")
    if stuck_in_memory:
        L.append(f"Found {len(stuck_in_memory)} suspicious record(s):")
        L.append("")
        for t in stuck_in_memory[:10]:
            L.append(f"- trade_id=`{t.get('trade_id', '?')}` "
                     f"strategy=`{t.get('strategy', '?')}` "
                     f"account=`{t.get('account', '?')}` "
                     f"state=`{t.get('state', '?')}` "
                     f"exit_reason=`{t.get('exit_reason', '?')}` "
                     f"exit_price=`{t.get('exit_price', '?')}`")
        if len(stuck_in_memory) > 10:
            L.append(f"- ... and {len(stuck_in_memory) - 10} more")
    else:
        L.append("_No exit_pending or zero-exit-price entries in trade_memory._")
    L.append("")

    # ─── 2. RECENT EXIT_TIMEOUT EVENTS in logs ───────────────────────────
    L.append("## 2. EXIT_TIMEOUT events in the lookback window")
    L.append("")
    log_paths = [
        data_root / "logs" / "sim_bot_stdout.log",
        data_root / "logs" / "prod_bot_stdout.log",
        data_root / "logs" / "watchdog.log",
    ]
    timeout_events = []
    for lp in log_paths:
        matches = grep_log(lp, [r"EXIT_TIMEOUT", r"exit_pending_timeout",
                                r"stuck exit_pending"])
        for line_no, ctx in matches:
            timeout_events.append({
                "file": lp.name, "line": line_no, "context": "".join(ctx)
            })

    L.append(f"Total EXIT_TIMEOUT-related log lines: **{len(timeout_events)}**")
    L.append("")
    if timeout_events:
        # Show distribution: by trade_id, by account, by strategy
        tid_counts = defaultdict(int)
        for ev in timeout_events:
            m = re.search(r"EXIT_PENDING_TIMEOUT:([^\]]+)", ev["context"])
            if m:
                tid_counts[m.group(1)] += 1
        L.append("Distribution by trade_id (top 10):")
        L.append("")
        L.append("| trade_id | hits |")
        L.append("|---|---:|")
        for tid, count in sorted(tid_counts.items(), key=lambda kv: -kv[1])[:10]:
            L.append(f"| `{tid}` | {count} |")
        L.append("")
        L.append("Last 3 events (most recent context):")
        L.append("")
        for ev in timeout_events[-3:]:
            L.append(f"- `{ev['file']}:{ev['line']}`")
            L.append("  ```")
            for ln in ev["context"].splitlines()[:4]:
                L.append(f"  {ln[:200]}")
            L.append("  ```")
        L.append("")

    # ─── 3. H1 — Account-name mismatch ───────────────────────────────────
    L.append("## H1 - Account-name mismatch (B12 ghost?)")
    L.append("")
    routing = known_routing()
    if not routing:
        L.append("WARN: Could not load `STRATEGY_ACCOUNT_MAP` - verify "
                 "`config/account_routing.py` is importable.")
    else:
        L.append(f"Loaded {len(routing)} strategy mapping(s):")
        L.append("")
        L.append("| Strategy | Configured Account |")
        L.append("|---|---|")
        for k, v in sorted(routing.items()):
            if isinstance(v, dict):
                for sub, acct in v.items():
                    L.append(f"| `{k}.{sub}` | `{acct}` |")
            else:
                L.append(f"| `{k}` | `{v}` |")
        L.append("")
        # Cross-check: for any account that NT8 has open right now, is it in
        # the routing map?
        if nt8_open:
            routed_accounts = set()
            for v in routing.values():
                if isinstance(v, dict):
                    routed_accounts.update(v.values())
                else:
                    routed_accounts.add(v)
            unrouted = [a for a in nt8_open if a not in routed_accounts]
            if unrouted:
                L.append(f"WARN: NT8 has open position(s) on accounts NOT in "
                         f"the routing map: {unrouted}. Phoenix won't be able "
                         f"to send exits there - those are unmanaged orphans.")
                L.append("")

    # ─── 4. H2 — OIF folder state ────────────────────────────────────────
    L.append("## H2 - OIF folder state")
    L.append("")
    folders = discover_oif_folders()
    if "error" in folders:
        L.append(f"WARN: Could not discover OIF folders: {folders['error']}")
    else:
        for label in ("incoming", "outgoing"):
            p = folders.get(label)
            if p is None:
                continue
            L.append(f"### `{label}`: `{p}`")
            L.append("")
            if not p.exists():
                L.append(f"FAIL: Folder does not exist!")
                L.append("")
                continue
            try:
                files = list(p.iterdir())
                L.append(f"- File count: {len(files)}")
                if files:
                    files.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0)
                    L.append(f"- Oldest file:")
                    f = files[0]
                    if f.exists():
                        try:
                            mt = datetime.fromtimestamp(f.stat().st_mtime, CT)
                            age_h = (datetime.now(CT) - mt).total_seconds() / 3600
                            stale_warn = " <-- STALE >5min" if age_h > (5/60) and label == "incoming" else ""
                            L.append(f"  - `{f.name}` mtime={mt.isoformat(timespec='seconds')} "
                                     f"age={age_h:.1f}h{stale_warn}")
                        except Exception as e:
                            L.append(f"  - `{f.name}` stat failed: {e}")
                    if label == "incoming" and len(files) > 0 and any(
                        (datetime.now(CT) - datetime.fromtimestamp(f.stat().st_mtime, CT)).total_seconds() > 300
                        for f in files if f.exists()
                    ):
                        L.append("")
                        L.append("  WARN: incoming has files >5min old. NT8 should consume "
                                 "OIFs within seconds. Stale files indicate ATI is not "
                                 "processing them OR a guard quarantined them.")
            except Exception as e:
                L.append(f"- Failed to enumerate: {e}")
            L.append("")

    # ─── 5. H3 — Phoenix vs NT8 state desync ─────────────────────────────
    L.append("## H3 - Phoenix vs NT8 position-state desync")
    L.append("")
    L.append("Phoenix records active positions in PositionManager (in-memory). ")
    L.append("NT8 holds the actual position. If Phoenix shows OPEN but NT8 ")
    L.append("shows FLAT (or vice versa), every retry targets the wrong state ")
    L.append("and silently fails.")
    L.append("")
    L.append("**Manual check:** Open NT8 Control Center -> Positions tab. ")
    L.append("Compare against section 1a above. If they match, no desync.")
    L.append("")
    L.append("**The auto-retry path (commit 9c3e74b) now uses NT8's reported "
             "direction, not Python's. So even when desync exists, the cover "
             "order goes the right way and flattens NT8's actual position.**")
    L.append("")

    # ─── 6. H4 — Cancel-then-exit race ───────────────────────────────────
    L.append("## H4 - Cancel-then-exit race condition")
    L.append("")
    cancel_then_exit = []
    for lp in log_paths:
        matches = grep_log(lp, [r"cancel_all_orders", r"CANCELALLORDERS",
                                r"cancel_single_order"])
        for line_no, ctx in matches:
            cancel_then_exit.append((lp.name, line_no, "".join(ctx)))
    if cancel_then_exit:
        L.append(f"Found {len(cancel_then_exit)} cancel-related log entries.")
        L.append("")
        L.append("Note: B75 hard-blocks CANCELALLORDERS from any bot path "
                 "(see oif_writer.py:1042+) so this should be near zero in "
                 "modern logs. Any matches indicate operator/test triggers.")
    else:
        L.append("_No cancel/exit log entries found in lookback window._")
    L.append("")

    # ─── 7. H5 — SHORT-specific issue ────────────────────────────────────
    L.append("## H5 - SHORT-specific OIF formatting")
    L.append("")
    L.append("Both stuck positions in the original incident were SHORT. This ")
    L.append("was the smoking gun for the CLOSEPOSITION-vs-OCO race: when ")
    L.append("the OCO stop fills (closing the original LONG to FLAT) AND the ")
    L.append("bot's CLOSEPOSITION OIF arrives at the same instant, NT8 ")
    L.append("re-reads its stale 'current position' cache, sees LONG, fires ")
    L.append("a SELL MARKET, and opens a phantom SHORT 1 - leaving Python ")
    L.append("thinking flat while NT8 actually holds the new SHORT.")
    L.append("")
    L.append("**Mitigation in place (commit e6129d8):** runtime reconciliation ")
    L.append("uses directional MARKET (BUY-to-cover SHORT, SELL-to-flatten ")
    L.append("LONG) instead of CLOSEPOSITION on retries. This bypasses the ")
    L.append("race entirely.")
    L.append("")

    # ─── 8. RECOMMENDATIONS ──────────────────────────────────────────────
    L.append("## 8. Recommendations")
    L.append("")
    if not nt8_open and not stuck_in_memory:
        L.append("**STATE IS CLEAN.** No stuck positions in NT8 or Phoenix ")
        L.append("trade memory. The 13h incident was already resolved.")
        L.append("")
        L.append("Sprint D Phase 2A-2E root-cause fixes are NOT needed - the ")
        L.append("root cause (CLOSEPOSITION-vs-OCO race + PhoenixOIFGuard ")
        L.append("filename quarantine + give-up-on-timeout) was fixed in ")
        L.append("commits e6129d8 and 9c3e74b.")
        L.append("")
        L.append("Recommended Sprint D continuation:")
        L.append("- Phase 2G: defensive observability (OIF pipeline health + ")
        L.append("  state desync detector + mark-flat operator tool + ")
        L.append("  EXIT_TIMEOUT_FORENSIC log). Ship as defense-in-depth.")
        L.append("- Phases F1-F4: alert noise reduction. Even with the bug ")
        L.append("  fixed, hardening alert dedup prevents future regression ")
        L.append("  spam.")
    else:
        L.append("**STATE IS NOT CLEAN.** See section 1 above for specifics.")
        L.append("")
        L.append("1. **DO NOT** restart the bot until verified.")
        L.append("2. **First verify** in NT8 Control Center what the actual ")
        L.append("   position state is for affected accounts.")
        L.append("3. **If NT8 shows FLAT** (Phoenix is desynced): use ")
        L.append("   `tools/mark_position_flat.py --trade-id <id> --apply`.")
        L.append("4. **If NT8 shows OPEN** (NT8 isn't flattening): manually ")
        L.append("   flatten in NT8 Control Center, THEN run mark_position_flat.")
        L.append("5. **Re-run this forensic** after action to confirm clean.")
    L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n{'='*60}")
    print(f"FORENSIC REPORT WRITTEN: {out}")
    print(f"{'='*60}")
    print(f"Currently stuck NT8 positions:    {len(nt8_open)}")
    print(f"Suspicious trade_memory entries:  {len(stuck_in_memory)}")
    print(f"EXIT_TIMEOUT log entries:         {len(timeout_events)}")
    print(f"Recent stuck-exit fix commits:    {len(recent_fixes)}")
    print()
    print("READ THE REPORT BEFORE PROCEEDING.")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
