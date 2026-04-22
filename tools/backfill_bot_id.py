"""
Backfill missing bot_id in logs/trade_memory.json.

Heuristic:
  - account == "Sim101" AND recorded_at >= 2026-04-21 (post-Phase-C) -> "prod"
  - account starts with "Sim" (dedicated SimXxx name)              -> "sim"
  - recorded_at < 2026-04-21                                        -> "legacy"
  - otherwise                                                       -> "unknown"

Only fills rows where bot_id is currently missing or None. Creates a
timestamped backup before writing. Supports --dry-run.
"""

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

MEMORY_FILE = Path("logs/trade_memory.json")
PHASE_C_CUTOFF = "2026-04-21"  # ISO date; lexicographic compare works on ISO timestamps


def classify(row: dict) -> str:
    account = row.get("account")
    ts = row.get("recorded_at") or row.get("entry_time_iso") or ""
    # entry_time may be epoch float; fall back via recorded_at primarily
    ts_str = ts if isinstance(ts, str) else ""

    post_phase_c = ts_str >= PHASE_C_CUTOFF if ts_str else False

    if account == "Sim101" and post_phase_c:
        return "prod"
    if isinstance(account, str) and account.startswith("Sim"):
        return "sim"
    if ts_str and ts_str < PHASE_C_CUTOFF:
        return "legacy"
    return "unknown"


def main():
    ap = argparse.ArgumentParser(description="Backfill bot_id in trade_memory.json")
    ap.add_argument("--dry-run", action="store_true", help="Report only; do not write")
    ap.add_argument("--file", default=str(MEMORY_FILE), help="Path to trade_memory.json")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}")
        return 1

    with path.open("r") as f:
        trades = json.load(f)

    counts = {"prod": 0, "sim": 0, "legacy": 0, "unknown": 0}
    backfilled = 0
    for row in trades:
        if row.get("bot_id") in (None, ""):
            label = classify(row)
            row["bot_id"] = label
            counts[label] += 1
            backfilled += 1

    summary = (
        f"Backfilled {backfilled} rows: "
        f"prod={counts['prod']} sim={counts['sim']} "
        f"legacy={counts['legacy']} unknown={counts['unknown']}"
    )

    if args.dry_run:
        print(f"[DRY-RUN] {summary}")
        return 0

    # Backup
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, backup)
    print(f"Backup saved: {backup}")

    with path.open("w") as f:
        json.dump(trades, f, indent=2, default=str)

    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
