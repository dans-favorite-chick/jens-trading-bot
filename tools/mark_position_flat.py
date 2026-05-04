"""
Phoenix - Manually mark a position as resolved in trade_memory.

Use when an operator has manually verified a position is flat in NT8
but a stale entry exists in logs/trade_memory.json with no exit_price
or with an unresolved state. Default mode is PREVIEW; --apply gates
the actual write. Always writes an audit log entry.

NEVER places, cancels, or modifies orders in NT8.
NEVER mutates live bot in-memory state (the running bot's
PositionManager owns that; this tool only fixes the persisted log).

Examples:
    python tools/mark_position_flat.py --trade-id abc123              # preview
    python tools/mark_position_flat.py --trade-id abc123 --apply
    python tools/mark_position_flat.py --trade-id abc123 --apply \\
        --exit-price 27800 --reason "manual_flatten_after_disconnect"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CT = ZoneInfo("America/Chicago")


def _data_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "logs" / "trade_memory.json").exists():
        return cwd
    if (ROOT / "logs" / "trade_memory.json").exists():
        return ROOT
    return cwd


def _load_trades(path: Path):
    if not path.exists():
        return None, "trade_memory file not found"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"parse error: {e}"
    if isinstance(data, dict):
        return data.get("trades", []), None
    if isinstance(data, list):
        return data, None
    return None, f"unexpected schema: {type(data).__name__}"


def _find_matching(trades: list, trade_id: str):
    out = []
    for i, t in enumerate(trades):
        if not isinstance(t, dict):
            continue
        if t.get("trade_id") == trade_id:
            out.append((i, t))
    return out


def _is_unresolved(t: dict) -> bool:
    """A trade looks unresolved if it has no exit_price/exit_time OR
    its state is still exit_pending."""
    state = t.get("state")
    if state in ("exit_pending", "EXIT_PENDING"):
        return True
    if t.get("exit_price") in (None, 0, 0.0, "0", ""):
        if t.get("exit_time") in (None, "", 0):
            return True
    return False


def _audit_log(audit_path: Path, entry: dict):
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trade-id", required=True,
                    help="trade_id of the position to mark as flat")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the change. Default is preview only.")
    ap.add_argument("--exit-price", type=float, default=None,
                    help="Optional exit price to set (default: leave unset)")
    ap.add_argument("--reason", default="operator_manual_flatten",
                    help="Audit reason for the state change")
    args = ap.parse_args()

    data_root = _data_root()
    tm_path = data_root / "logs" / "trade_memory.json"
    audit_path = data_root / "memory" / "audit_log.jsonl"

    trades, err = _load_trades(tm_path)
    if err:
        print(f"ERROR reading {tm_path}: {err}")
        return 2

    matches = _find_matching(trades, args.trade_id)
    if not matches:
        print(f"No trade with trade_id={args.trade_id!r} found in {tm_path}")
        return 1

    print(f"Found {len(matches)} trade(s) matching trade_id={args.trade_id!r}:")
    for idx, t in matches:
        unresolved = _is_unresolved(t)
        flag = " [UNRESOLVED]" if unresolved else " [already resolved]"
        print(f"\n  [{idx}]{flag}")
        print(f"    strategy:    {t.get('strategy', '?')}")
        print(f"    account:     {t.get('account', '?')}")
        print(f"    direction:   {t.get('direction', '?')}")
        print(f"    entry_price: {t.get('entry_price', '?')}")
        print(f"    exit_price:  {t.get('exit_price', '?')}")
        print(f"    state:       {t.get('state', '?')}")
        print(f"    exit_reason: {t.get('exit_reason', '?')}")

    if not args.apply:
        print("\n[PREVIEW] No changes written. Re-run with --apply to commit.")
        return 0

    # Apply: mutate matching trades in-memory, write atomically.
    now_iso = datetime.now(CT).isoformat(timespec="seconds")
    modified = 0
    for idx, t in matches:
        before = {k: t.get(k) for k in ("state", "exit_price", "exit_reason")}
        t["state"] = "manually_closed"
        t["state_change_reason"] = args.reason
        t["state_change_ts"] = now_iso
        if args.exit_price is not None:
            t["exit_price"] = args.exit_price
            if not t.get("exit_time"):
                t["exit_time"] = now_iso
        if not t.get("exit_reason"):
            t["exit_reason"] = "manual_flatten"
        modified += 1
        _audit_log(audit_path, {
            "ts": now_iso,
            "event": "manual_mark_flat",
            "trade_id": args.trade_id,
            "trade_index": idx,
            "reason": args.reason,
            "before": before,
            "after": {
                "state": t["state"],
                "exit_price": t.get("exit_price"),
                "exit_reason": t.get("exit_reason"),
            },
            "operator": True,
            "tool": "mark_position_flat.py",
        })

    # Write back. Trade file may be stored as either {"trades": [...]} or [...]
    # Preserve original schema.
    raw = json.loads(tm_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw["trades"] = trades
        out_data = raw
    else:
        out_data = trades
    # Atomic write via temp + rename
    tmp = tm_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out_data, indent=2, default=str), encoding="utf-8")
    tmp.replace(tm_path)

    print(f"\n[APPLIED] Modified {modified} trade record(s).")
    print(f"Audit logged to {audit_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
