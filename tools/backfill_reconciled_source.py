"""FINDING-2026-06-04-HIST-MIGRATE: backfill provenance flags on
historical RECONCILED_* rows in trade_memory.

Context
-------
Until commit 257df2f, the B77 startup-reconciliation path adopted
orphan NT8 positions and labeled them with whatever strategy
`_infer_strategy_from_account()` returned. For accounts with multiple
strategies routed to them (Sim101 → big_move_signal, es_nq_confluence,
etc.) this returned the FIRST dict-order match, which was always
`big_move_signal`. Twelve operator manual fills were therefore credited
to prod_bot's big_move_signal strategy (8W/4L, +$504 totalPnL).

Post-fix (257df2f), NEW reconciled trades are tagged at close time with:
    source='manual_reconciled'
    reconciled_from_orphan=True
    strategy='_reconciled_<account>'                  (was: inferred name)
    strategy_original_attribution=<inferred name>     (audit)

The dashboard /api/today-pnl aggregator already filters on
source=='manual_reconciled'. But the 12 historical rows on disk still
carry source=None and strategy='big_move_signal', so the dashboard
keeps treating them as real bot trades. This script backfills the
provenance fields so the dashboard filter applies retroactively.

Behavior
--------
* Default mode is DRY-RUN — no writes. Outputs the proposed changes
  to `out/hist_migrate_dryrun_<YYYY-MM-DD>.md` and prints a summary.
* `--apply` performs the migration via the canonical
  `core.trade_memory.TradeMemory.update_trade(...)` API. That writer
  atomically updates the per-bot JSON file AND syncs the SQLite
  shadow (P4-4 dual-write). NEVER raw-opens trade_memory.json.
* Idempotent — re-running on already-migrated rows is a no-op.
* Validates row scope: any row from an unexpected account or any
  count > 12 (the operator-confirmed target) HALTS before writing.
  Caller must pass `--allow-count N` to override.

Usage
-----
    # Dry run (default)
    python tools/backfill_reconciled_source.py

    # Apply
    python tools/backfill_reconciled_source.py --apply

    # Override the 12-row safety cap (do not use without operator OK)
    python tools/backfill_reconciled_source.py --apply --allow-count 25

Output reports
--------------
    out/hist_migrate_dryrun_<YYYY-MM-DD>.md     (dry-run)
    out/hist_migrate_applied_<YYYY-MM-DD>.md    (post --apply)

Refs: FINDING-2026-06-04-HIST-MIGRATE
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

# Repo paths.
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DB_PATH = REPO / "data" / "trade_memory.db"
OUT_DIR = REPO / "out"

# Ensure `core.trade_memory` import resolves when run from anywhere.
# Mirrors the pattern used in tools/daily_session_summary.py and the
# canonical writer is the only sanctioned trade_memory mutation path
# (operator standing instruction).
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Operator-confirmed safety cap. Phase 1 diagnostic verified exactly
# 12 RECONCILED prod-attributed rows in the DB at the time of writing.
# Any new row that appeared since (e.g. another manual fill the bot
# reconciled after Phase 1) will land naturally via the post-257df2f
# fix and won't need migrating — but a count mismatch should HALT and
# surface the new row for explicit operator approval.
DEFAULT_ALLOW_COUNT = 12

# Sim101 is the only account expected per Phase 1. If a row from any
# other account appears here, HALT — could be a legitimate per-account
# routing that this migration should NOT touch.
EXPECTED_ACCOUNTS = {"Sim101"}


def _today_tag() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def _read_db_reconciled_rows() -> list[dict]:
    """Pull every RECONCILED_* row from the SQLite shadow with the
    fields we need to plan + verify the migration.

    Returns dicts with keys: trade_id, bot_id, strategy, account,
    direction, entry_time, exit_time, pnl_dollars, result, raw_json.
    """
    if not DB_PATH.exists():
        raise SystemExit(
            f"FATAL: {DB_PATH} not found. Run from repo root or fix DB_PATH."
        )
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT trade_id, bot_id, strategy, account, direction,
               entry_time, exit_time, pnl_dollars, result, raw_json
        FROM trades
        WHERE trade_id LIKE 'RECONCILED_%'
        ORDER BY entry_time
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _already_migrated(row: dict) -> bool:
    """A row is migrated when raw_json carries source='manual_reconciled'.
    The new column-mapped strategy alone is not sufficient — could be a
    fresh post-fix row that was BORN with _reconciled_<account>.
    """
    try:
        rj = json.loads(row.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return rj.get("source") == "manual_reconciled"


def _plan_update(row: dict) -> Optional[dict]:
    """Return the dict of fields to merge onto the trade row, or None
    if the row is already migrated.

    The merge dict is the same shape `TradeMemory.update_trade(...)`
    expects.
    """
    if _already_migrated(row):
        return None
    account = row.get("account") or ""
    if not account:
        # Cannot derive _reconciled_<account>; refuse silently. The
        # dry-run report surfaces this row so operator can decide.
        return {
            "_skip_reason": "account is NULL/empty — refusing to fabricate label",
        }
    original = row.get("strategy") or None
    return {
        "source": "manual_reconciled",
        "reconciled_from_orphan": True,
        "strategy_original_attribution": original,
        # Replace the misleading inferred label (e.g. 'big_move_signal')
        # with the new account-scoped pseudo-strategy.
        "strategy": f"_reconciled_{account}",
    }


def _apply_one(row: dict, update: dict) -> tuple[bool, str]:
    """Call canonical writer for one row. Returns (ok, message).
    The writer atomically updates the per-bot JSON file AND syncs the
    SQLite shadow. Never raw-opens trade_memory.json.
    """
    from core.trade_memory import TradeMemory

    bot_id = row.get("bot_id") or "unknown"
    tm = TradeMemory(bot_id=bot_id)
    ok = tm.update_trade(row["trade_id"], update)
    if not ok:
        return False, (
            f"update_trade returned False — trade_id not present in "
            f"trade_memory_{bot_id}.json or legacy. Row stays unmodified."
        )
    return True, "updated"


def _write_dryrun_report(rows: list[dict], plans: list[Optional[dict]]) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"hist_migrate_dryrun_{_today_tag()}.md"
    lines: list[str] = []
    lines.append(f"# HIST-MIGRATE Dry-Run Report — {_today_tag()}\n")
    lines.append(f"_Source DB:_ `{DB_PATH}`\n")
    lines.append(
        f"_Rows found matching `RECONCILED_%`:_ **{len(rows)}** "
        f"(operator-confirmed cap: {DEFAULT_ALLOW_COUNT})\n"
    )
    accounts = Counter(r.get("account") for r in rows)
    lines.append(
        f"_Accounts seen:_ {dict(accounts)} "
        f"(expected: {sorted(EXPECTED_ACCOUNTS)})\n"
    )
    lines.append("\n## Per-row plan\n")
    lines.append(
        "| # | trade_id | bot_id | strategy (current) | strategy (after) | "
        "account | pnl | already_migrated? | _skip_reason |\n"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|\n")
    n_to_migrate = n_skipped_done = n_skipped_other = 0
    for i, (row, plan) in enumerate(zip(rows, plans), start=1):
        if plan is None:
            status = "YES — skip"
            new_strat = row.get("strategy")
            skip = "row already has source='manual_reconciled'"
            n_skipped_done += 1
        elif "_skip_reason" in plan:
            status = "NO — but skip"
            new_strat = row.get("strategy")
            skip = plan["_skip_reason"]
            n_skipped_other += 1
        else:
            status = "NO"
            new_strat = plan["strategy"]
            skip = ""
            n_to_migrate += 1
        lines.append(
            f"| {i} | `{row['trade_id']}` | {row.get('bot_id')} | "
            f"`{row.get('strategy')}` | `{new_strat}` | "
            f"{row.get('account')} | ${row.get('pnl_dollars')} | "
            f"{status} | {skip} |\n"
        )
    lines.append("\n## Summary\n")
    lines.append(f"- Rows to migrate (`--apply` would change): **{n_to_migrate}**\n")
    lines.append(f"- Rows already migrated (skip, idempotent): **{n_skipped_done}**\n")
    lines.append(f"- Rows skipped for other reason: **{n_skipped_other}**\n")
    lines.append("\n## Next step\n")
    if n_to_migrate == 0:
        lines.append("Nothing to do. All rows already migrated.\n")
    else:
        lines.append(
            "If this plan looks right, re-run with `--apply`. Each row\n"
            "is updated via `core.trade_memory.TradeMemory.update_trade`,\n"
            "which atomically rewrites the per-bot JSON file and syncs\n"
            "the SQLite shadow. NEVER raw-opens trade_memory.json.\n"
        )
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _write_applied_report(
    rows: list[dict], results: list[tuple[bool, str]]
) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"hist_migrate_applied_{_today_tag()}.md"
    lines: list[str] = []
    lines.append(f"# HIST-MIGRATE Applied Report — {_today_tag()}\n")
    lines.append(f"_Source DB:_ `{DB_PATH}`\n")
    n_ok = sum(1 for ok, _ in results if ok)
    n_fail = sum(1 for ok, _ in results if not ok)
    lines.append(f"_Migrated successfully:_ **{n_ok}**, _failed/skipped:_ **{n_fail}**\n")
    lines.append("\n## Per-row outcome\n")
    lines.append("| # | trade_id | bot_id | account | ok? | message |\n")
    lines.append("|---|---|---|---|---|---|\n")
    for i, (row, (ok, msg)) in enumerate(zip(rows, results), start=1):
        flag = "OK" if ok else "FAIL"
        lines.append(
            f"| {i} | `{row['trade_id']}` | {row.get('bot_id')} | "
            f"{row.get('account')} | {flag} | {msg} |\n"
        )

    # Post-condition: re-read the DB shadow and confirm each migrated
    # row now carries source='manual_reconciled' in raw_json.
    lines.append("\n## Post-apply DB shadow verification\n")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    verified = []
    for row, (ok, _) in zip(rows, results):
        if not ok:
            continue
        cur = conn.execute(
            "SELECT strategy, raw_json FROM trades WHERE trade_id = ?",
            (row["trade_id"],),
        )
        r = cur.fetchone()
        if r is None:
            verified.append((row["trade_id"], False, "row missing post-write"))
            continue
        try:
            rj = json.loads(r["raw_json"] or "{}")
        except Exception:
            verified.append((row["trade_id"], False, "raw_json unparseable"))
            continue
        ok_v = (
            rj.get("source") == "manual_reconciled"
            and rj.get("reconciled_from_orphan") is True
            and r["strategy"].startswith("_reconciled_")
        )
        verified.append(
            (row["trade_id"], ok_v,
             f"strategy={r['strategy']!r} source={rj.get('source')!r} "
             f"reconciled_from_orphan={rj.get('reconciled_from_orphan')!r} "
             f"strategy_original_attribution="
             f"{rj.get('strategy_original_attribution')!r}")
        )
    conn.close()
    lines.append("| trade_id | post-write verified? | shadow state |\n")
    lines.append("|---|---|---|\n")
    for tid, ok_v, msg in verified:
        lines.append(f"| `{tid}` | {'YES' if ok_v else 'NO'} | {msg} |\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill source flag on RECONCILED_* rows in trade memory."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the migration. Default is dry-run.",
    )
    parser.add_argument(
        "--allow-count",
        type=int,
        default=DEFAULT_ALLOW_COUNT,
        help=(
            f"Cap on rows-to-migrate. HALTs if the DB has more than this "
            f"many RECONCILED_* rows not yet migrated. Default {DEFAULT_ALLOW_COUNT}."
        ),
    )
    parser.add_argument(
        "--allow-account",
        action="append",
        default=None,
        help=(
            "Add an account to the expected-account allowlist. Repeat to "
            "add multiple. Default allowlist: " + ",".join(sorted(EXPECTED_ACCOUNTS))
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    expected_accounts = set(EXPECTED_ACCOUNTS)
    if args.allow_account:
        expected_accounts.update(args.allow_account)

    rows = _read_db_reconciled_rows()
    plans = [_plan_update(r) for r in rows]
    n_to_migrate = sum(1 for p in plans if p and "_skip_reason" not in p)

    # Safety: HALT if we'd migrate more rows than operator approved.
    if n_to_migrate > args.allow_count:
        print(
            f"HALT: {n_to_migrate} rows would be migrated, but --allow-count is "
            f"{args.allow_count}. Re-run with --allow-count {n_to_migrate} after "
            f"explicit operator approval.",
            file=sys.stderr,
        )
        report = _write_dryrun_report(rows, plans)
        print(f"Dry-run report written to: {report}")
        return 2

    # Safety: HALT if any row is from an unexpected account.
    unexpected = [
        r["trade_id"] for r in rows if (r.get("account") or "") not in expected_accounts
    ]
    if unexpected:
        print(
            "HALT: rows from accounts NOT in the expected allowlist:\n  "
            + "\n  ".join(unexpected)
            + f"\nExpected: {sorted(expected_accounts)}\n"
              "Re-run with --allow-account <name> for each new account after "
              "explicit operator approval.",
            file=sys.stderr,
        )
        report = _write_dryrun_report(rows, plans)
        print(f"Dry-run report written to: {report}")
        return 2

    if not args.apply:
        report = _write_dryrun_report(rows, plans)
        print(f"DRY-RUN — {n_to_migrate} rows would be migrated.")
        print(f"Report: {report}")
        print("Re-run with --apply to perform the migration.")
        return 0

    # --apply mode
    results: list[tuple[bool, str]] = []
    for row, plan in zip(rows, plans):
        if plan is None:
            results.append((True, "skip (already migrated)"))
            continue
        if "_skip_reason" in plan:
            results.append((False, plan["_skip_reason"]))
            continue
        ok, msg = _apply_one(row, plan)
        results.append((ok, msg))

    report = _write_applied_report(rows, results)
    n_ok = sum(1 for ok, _ in results if ok)
    n_fail = sum(1 for ok, _ in results if not ok)
    print(f"APPLIED — ok={n_ok}, failed/skipped={n_fail}.")
    print(f"Report: {report}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
