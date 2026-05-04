"""
Phoenix - Shared-Account Collision Forensic (READ-ONLY)

Investigates whether sim_bot and prod_bot are colliding on shared accounts.
Both bots use STRATEGY_ACCOUNT_MAP - when both decide to take the same
strategy, OIFs land in the same account, potentially conflicting.

Output: out/collision_forensic_<today>.md

NEVER modifies state. NEVER touches NT8. Pure analysis.

Usage:
    python tools/diagnose_account_collisions.py
    python tools/diagnose_account_collisions.py --hours 24
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
# Order matters: sys.path.insert(0, x) puts x at the FRONT. We want
# cwd-local `config/` to WIN over the project's when the test injects
# a stub. So insert ROOT first (lower priority), then cwd if it has a
# stub config (higher priority — pushes ROOT to index 1). Production
# callers run from cwd == ROOT, so the cwd-prepend is a no-op.
sys.path.insert(0, str(ROOT))
_CWD = Path.cwd()
if (_CWD / "config" / "account_routing.py").exists() and _CWD != ROOT:
    sys.path.insert(0, str(_CWD))
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


def parse_ts_from_line(line: str):
    m = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        s = m.group(1).replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CT)
        return dt.astimezone(CT)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72,
                    help="Look back N hours for collision evidence")
    args = ap.parse_args()

    data_root = _data_root()
    today = datetime.now(CT).date()
    cutoff = datetime.now(CT) - timedelta(hours=args.hours)
    out = data_root / f"out/collision_forensic_{today}.md"
    out.parent.mkdir(exist_ok=True)

    L = []
    L.append(f"# Phoenix Shared-Account Collision Forensic - {today}")
    L.append("")
    L.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
    L.append(f"_Lookback: {args.hours} hours (since {cutoff.isoformat(timespec='seconds')})_")
    L.append("")
    L.append("**THIS IS A READ-ONLY REPORT. NO STATE MODIFIED.**")
    L.append("")

    # ─── 1. Account routing snapshot ────────────────────────────────
    L.append("## 1. Account routing - who's sharing what")
    L.append("")
    shared = {}
    try:
        from config.account_routing import STRATEGY_ACCOUNT_MAP
        # Group strategies by account to find shared accounts.
        # opening_session is a dict mapping sub_strategy -> account; expand it.
        by_account = defaultdict(list)
        for strat, acct in STRATEGY_ACCOUNT_MAP.items():
            if isinstance(acct, dict):
                # opening_session sub-mapping
                for sub, real_acct in acct.items():
                    by_account[real_acct].append(f"{strat}.{sub}")
            else:
                by_account[acct].append(strat)
        shared = {a: s for a, s in by_account.items() if len(s) > 1}
        L.append(f"Total entries in STRATEGY_ACCOUNT_MAP: {len(STRATEGY_ACCOUNT_MAP)}")
        L.append(f"Total unique accounts: {len(by_account)}")
        L.append(f"Accounts with >1 strategy mapped (collision candidates): "
                 f"{len(shared)}")
        L.append("")
        if shared:
            L.append("**Shared-account mappings:**")
            L.append("")
            L.append("| Account | Strategies sharing it |")
            L.append("|---|---|")
            for acct, strats in sorted(shared.items()):
                L.append(f"| `{acct}` | "
                         f"{', '.join(f'`{s}`' for s in strats)} |")
            L.append("")
        else:
            L.append("OK: No multi-strategy accounts in routing.")
            L.append("")
    except Exception as e:
        L.append(f"WARN: Could not load STRATEGY_ACCOUNT_MAP: {e}")
        L.append("")

    # ─── 2. prod_bot vs sim_bot strategy overlap ────────────────────
    L.append("## 2. Bot-level strategy overlap")
    L.append("")
    L.append("Per discovery: prod_bot filters strategies by `validated=True`.")
    L.append("sim_bot runs all strategies. So overlap = strategies prod loaded.")
    L.append("")
    prod_log = data_root / "logs" / "prod_bot_stdout.log"
    sim_log = data_root / "logs" / "sim_bot_stdout.log"
    prod_loaded = set()
    sim_loaded = set()
    for log_path, target in [(prod_log, prod_loaded),
                               (sim_log, sim_loaded)]:
        if not log_path.exists():
            continue
        matches = grep_log(log_path, [r"Loaded strategy:\s*(\S+)"],
                            context_lines=0)
        for line_no, ctx in matches:
            for line in ctx:
                m = re.search(r"Loaded strategy:\s*(\S+)", line)
                if m:
                    target.add(m.group(1))
    overlap = prod_loaded & sim_loaded
    L.append(f"- prod_bot loaded: {sorted(prod_loaded)} "
             f"({len(prod_loaded)})")
    L.append(f"- sim_bot loaded:  {sorted(sim_loaded)} "
             f"({len(sim_loaded)})")
    L.append(f"- **Shared (both bots can fire):** {sorted(overlap)} "
             f"({len(overlap)})")
    L.append("")
    if overlap:
        L.append("**Per overlapping strategy: which account does each bot route to?**")
        L.append("")
        L.append("| Strategy | Bots | Account | Collision risk |")
        L.append("|---|---|---|---|")
        try:
            from config.account_routing import STRATEGY_ACCOUNT_MAP
            for strat in sorted(overlap):
                acct = STRATEGY_ACCOUNT_MAP.get(strat, "-")
                if isinstance(acct, dict):
                    for sub, real_acct in acct.items():
                        risk = "RED: SAME ACCOUNT" if real_acct else "-"
                        L.append(f"| `{strat}.{sub}` | sim+prod | "
                                 f"`{real_acct}` | {risk} |")
                else:
                    risk = "RED: SAME ACCOUNT" if acct != "-" else "-"
                    L.append(f"| `{strat}` | sim+prod | `{acct}` | {risk} |")
        except Exception:
            pass
        L.append("")

    # ─── 3. Collision evidence in logs ──────────────────────────────
    L.append("## 3. Collision evidence in recent logs")
    L.append("")
    collision_patterns = [
        r"position.*already.*held",
        r"already.*open.*position",
        r"reject.*duplicate",
        r"duplicate.*entry",
        r"OIF.*queued",
        r"SKIP.*position.*exists",
        r"another bot.*position",
        r"NT8.*reject.*account",
        r"Exceeds.*account.*maximum.*position",
        r"already in_trade",
        r"is_flat_for.*returned False",
    ]
    collision_evidence = []
    last_hour_evidence = []
    one_hour_ago = datetime.now(CT) - timedelta(hours=1)
    for log_path in [prod_log, sim_log]:
        if not log_path.exists():
            continue
        matches = grep_log(log_path, collision_patterns, context_lines=2)
        for line_no, ctx in matches:
            ts_line = next((line for line in ctx
                            if parse_ts_from_line(line)), None)
            ts = parse_ts_from_line(ts_line) if ts_line else None
            if ts is None or ts < cutoff:
                continue
            ev = {
                "file": log_path.name,
                "line": line_no,
                "ts": ts.isoformat(timespec="seconds"),
                "ts_obj": ts,
                "context": "".join(ctx)[:500],
            }
            collision_evidence.append(ev)
            if ts >= one_hour_ago:
                last_hour_evidence.append(ev)
    L.append(f"Total collision-pattern matches in last {args.hours}h: "
             f"**{len(collision_evidence)}**")
    L.append(f"In last 1h: **{len(last_hour_evidence)}**")
    L.append("")
    if collision_evidence:
        L.append("Last 5 events:")
        L.append("")
        for ev in collision_evidence[-5:]:
            L.append(f"- `{ev['file']}:{ev['line']}` @ `{ev['ts']}`")
            L.append("  ```")
            for line in ev["context"].splitlines()[:4]:
                L.append(f"  {line[:200]}")
            L.append("  ```")
        L.append("")
    else:
        L.append("_No explicit collision-pattern matches found in lookback. "
                 "This either means_:")
        L.append("- Collisions don't happen (one bot consistently wins)")
        L.append("- Collisions happen silently (no log line), need finer "
                 "instrumentation")
        L.append("- Lookback period had no overlapping signals")
        L.append("")

    # ─── 4. Today's prod_bot signal attempts ────────────────────────
    L.append("## 4. Today's prod_bot signal attempts vs sim_bot fills")
    L.append("")
    prod_signals_today = []
    if prod_log.exists():
        prod_signals = grep_log(
            prod_log,
            [r"\[SIGNAL", r"signal.*generated", r"\[FIRE",
             r"action=ENTER_", r"\[ENTRY"],
            context_lines=0,
        )
        for line_no, ctx in prod_signals:
            for line in ctx:
                ts = parse_ts_from_line(line)
                if ts and ts.date() == datetime.now(CT).date():
                    prod_signals_today.append((ts, line.rstrip()))
        L.append(f"prod_bot signal events today: **{len(prod_signals_today)}**")
        if prod_signals_today:
            L.append("")
            L.append("Last 5:")
            L.append("```")
            for ts, line in prod_signals_today[-5:]:
                L.append(f"{ts.isoformat(timespec='seconds')}  {line[:140]}")
            L.append("```")
        L.append("")
    else:
        L.append("_prod_bot_stdout.log not found_")
        L.append("")

    # ─── 5. Historical RECONCILED events ────────────────────────────
    L.append("## 5. Historical RECONCILED_* events")
    L.append("")
    L.append("_From context: `RECONCILED_SimVWapp Pullback_f216c768` was the "
             "13h stuck-SHORT incident. Analyzing for shared-account pattern._")
    L.append("")
    reconciled = []
    for log_path in [prod_log, sim_log]:
        if not log_path.exists():
            continue
        matches = grep_log(log_path, [r"RECONCILED_"], context_lines=0)
        for line_no, ctx in matches:
            for line in ctx:
                m = re.search(r"RECONCILED_([^_]+(?:\s[^_]+)*)_([a-f0-9]{6,})", line)
                if m:
                    reconciled.append({
                        "file": log_path.name,
                        "name": m.group(1).strip(),
                        "id": m.group(2),
                        "line": line_no,
                    })
    if reconciled:
        # Count by base name + unique trade ids
        by_name = Counter(r["name"] for r in reconciled)
        unique_ids = {(r["name"], r["id"]) for r in reconciled}
        L.append(f"Total RECONCILED log lines: **{len(reconciled)}**  "
                 f"(unique trade_ids: **{len(unique_ids)}**)")
        L.append("")
        L.append("| Account/strategy | log lines | unique tids | "
                 "shared with |")
        L.append("|---|---:|---:|---|")
        try:
            from config.account_routing import STRATEGY_ACCOUNT_MAP
            for name, count in by_name.most_common(10):
                tids = {r["id"] for r in reconciled if r["name"] == name}
                # name here is the account name, not strategy. Look up which
                # strategies share it.
                sharing = []
                for s, a in STRATEGY_ACCOUNT_MAP.items():
                    if a == name:
                        sharing.append(s)
                    elif isinstance(a, dict):
                        for sub, real_a in a.items():
                            if real_a == name:
                                sharing.append(f"{s}.{sub}")
                shared_note = (", ".join(sharing) if len(sharing) > 1
                               else sharing[0] if sharing else "-")
                L.append(f"| `{name}` | {count} | {len(tids)} | "
                         f"{shared_note} |")
        except Exception:
            for name, count in by_name.most_common(10):
                tids = {r["id"] for r in reconciled if r["name"] == name}
                L.append(f"| `{name}` | {count} | {len(tids)} | - |")
        L.append("")
    else:
        L.append("_No RECONCILED_* events found._")
        L.append("")

    # ─── 6. Recommendations ─────────────────────────────────────────
    L.append("## 6. Recommendations")
    L.append("")
    L.append("**If shared-account mappings exist (Section 1) AND bot overlap "
             "exists (Section 2):**")
    L.append("- Long-term fix: separate `STRATEGY_ACCOUNT_MAP_SIM` and ")
    L.append("  `STRATEGY_ACCOUNT_MAP_PROD` so each bot has its own routing")
    L.append("- Short-term: ensure prod_bot's loaded strategies route to ")
    L.append("  accounts sim_bot does NOT use, OR add an in-bot lock that ")
    L.append("  prevents duplicate signals on the same account")
    L.append("")
    L.append("**If collision evidence is sparse (Section 3) but bot overlap "
             "exists:**")
    L.append("- Add explicit collision-detection logging at the OIF write path")
    L.append("- Re-run this forensic in 7 days to see if instrumentation "
             "surfaces it")
    L.append("")
    L.append("**If RECONCILED events cluster on shared accounts (Section 5):**")
    L.append("- Strong evidence collision IS the root cause of stuck-position "
             "issues")
    L.append("- Routing separation moves to highest priority")
    L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\nWrote {out}")
    print(f"Shared accounts found: {len(shared)}")
    print(f"Bot overlap: {len(overlap)} strategies")
    print(f"Collision events in last {args.hours}h: {len(collision_evidence)}")
    print(f"  In last 1h:                       {len(last_hour_evidence)}")
    print(f"Historical RECONCILED log lines: {len(reconciled)}")
    return 0 if len(last_hour_evidence) == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
