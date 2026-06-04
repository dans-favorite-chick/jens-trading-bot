"""REDTEAM-2-FIX-SCRIPT (remediation 2026-06-04 round 2) +
FINDING-2026-06-04-HIST-MIGRATE: backfill provenance flags on every
historical orphan-adoption row across the canonical trade_memory view.

Why this exists (round 1 + round 2)
-----------------------------------
Until commit 257df2f, the B77 startup-reconciliation path adopted
orphan NT8 positions and labeled them with whatever
`_infer_strategy_from_account()` returned. For multi-strategy
accounts (Sim101 hosts big_move_signal, es_nq_confluence, and the
_default fallback) the inferred name was always whichever entry hit
first in dict iteration, so operator manual fills were credited to
the wrong strategy — most visibly inflating big_move_signal's win-
rate accounting.

Round 1 of this script (dafaeec, 2026-06-04) intended to backfill 16
rows. Two problems came out of red-team round 1:

  REDTEAM-2: scope. The script queried the SQLite shadow only, so
  it never enumerated the ~104 unique orphan rows that exist only in
  the JSON files (`logs/trade_memory.json` plus `logs/trade_memory_<bot>.json`
  per-bot files). The DB shadow started 2026-05-25 (P4-4 dual-write)
  and is incomplete for historical data.

  Migration race: even for the 16 rows it did enumerate, prod_bot's
  TradeMemory in-memory cache wiped the JSON updates on the next
  bot save(). DB shadow stuck, JSON didn't — and the dashboard reads
  JSON via `load_all_trades()`.

Round 2 (this version) fixes both:

  * iterates the canonical deduplicated view via
    `core.trade_memory.load_all_trades(logs_dir=...)`, picking up
    every JSON-only row regardless of DB presence;
  * matches trade_ids against a configurable regex pattern (default
    `^RECONCILED_`, --patterns extends for future formats);
  * applies the TOPOLOGY-AWARE label produced by Phase 3a (84ebf28):
      single-strategy account → real strategy name
      multi-strategy account  → `_reconciled_<account>`
      unrouted (defensive)    → `_reconciled_<account>`
  * still writes through `TradeMemory.update_trade()` — the canonical
    writer that atomically rewrites the per-bot JSON file AND syncs
    the SQLite shadow. NEVER raw-opens JSON.
  * idempotent in the strict sense — a row that already has
    `source='manual_reconciled'` AND a strategy field consistent with
    the topology rule is skipped. A row with source set but stale
    strategy label (e.g. an early-round-1-migrated SimBias Momentum
    row that got the `_reconciled_<account>` label before topology
    awareness landed) will be re-migrated to the canonical label
    while preserving its `strategy_original_attribution`.
  * MUST be run with the bots quiesced (operator-actioned). The
    JSON-clobber race made round 1 a partial no-op for prod; round 2
    relies on the operator stopping prod_bot + sim_bot before
    `--apply`, then restarting them after the commit lands. The
    coordinated-restart steps live in the master prompt's
    `PHASE 3-RESTART` block, not here.

Behavior
--------
* Default mode is DRY-RUN — no writes. Writes a per-row plan to
  `out/hist_migrate_dryrun_<YYYY-MM-DD>.md`.
* `--apply` performs the migration via the canonical
  `core.trade_memory.TradeMemory.update_trade(...)` API.
* `--patterns RE[,RE...]` extends the trade_id match regex set.
  Default: `^RECONCILED_`.
* `--logs-dir <path>` overrides the trade_memory directory (tests
  pass a tmp dir; production defaults to `<repo>/logs`).

Usage
-----
    # Dry run (default)
    python tools/backfill_reconciled_source.py

    # Apply
    python tools/backfill_reconciled_source.py --apply

    # Custom pattern set (e.g. legacy RECONCILED + ORPHAN_ prefixes)
    python tools/backfill_reconciled_source.py --patterns "^RECONCILED_,^ORPHAN_"

Output reports
--------------
    out/hist_migrate_dryrun_<YYYY-MM-DD>.md     (dry-run)
    out/hist_migrate_applied_<YYYY-MM-DD>.md    (post --apply)

Refs: FINDING-2026-06-04-HIST-MIGRATE
Refs: REDTEAM-2-FIX-SCRIPT
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

# Repo paths.
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DB_PATH = REPO / "data" / "trade_memory.db"
DEFAULT_LOGS_DIR = REPO / "logs"
OUT_DIR = REPO / "out"

# Ensure `core.trade_memory` import resolves when run from anywhere.
# Mirrors the pattern used in tools/daily_session_summary.py and the
# canonical writer is the only sanctioned trade_memory mutation path
# (operator standing instruction).
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_PATTERNS: tuple[str, ...] = (r"^RECONCILED_",)


def _today_tag() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


# ── Topology-aware target label ────────────────────────────────────────
#
# Mirrors core/startup_reconciliation.py post-84ebf28. Imported lazily
# so this script can run even if the config import path is broken in
# some unusual repo state.

def _target_strategy_label(account: str) -> tuple[str, list[str]]:
    """Return ``(strategy_label, routed_strategies)`` for ``account``
    per the topology rule. Empty account string is treated as unrouted.
    """
    if not account:
        return f"_reconciled_", []
    try:
        from config.account_routing import strategies_for_account
        routed = strategies_for_account(account)
    except Exception:
        routed = []
    if len(routed) == 1:
        return routed[0], routed
    return f"_reconciled_{account}", routed


def _matches_pattern(trade_id: str, patterns: tuple[re.Pattern, ...]) -> bool:
    return any(p.search(trade_id) for p in patterns)


def _read_canonical_rows(
    logs_dir: Path, patterns: tuple[re.Pattern, ...]
) -> list[dict]:
    """Iterate the canonical deduplicated trade_memory view via
    `core.trade_memory.load_all_trades()` and return every row whose
    ``trade_id`` matches at least one pattern.
    """
    from core.trade_memory import load_all_trades

    rows = load_all_trades(logs_dir=str(logs_dir))
    matched: list[dict] = []
    for t in rows:
        tid = str(t.get("trade_id") or "")
        if tid and _matches_pattern(tid, patterns):
            matched.append(t)
    return matched


def _plan_update(row: dict) -> Optional[dict]:
    """Compute the canonical update for ``row`` per the topology rule,
    or return ``None`` if the row is already in its final canonical
    state (idempotency skip).

    A row is in its canonical state when:
      * ``source == 'manual_reconciled'``, AND
      * ``reconciled_from_orphan == True``, AND
      * ``strategy == _target_strategy_label(account)[0]``.

    Otherwise the returned dict contains the fields to merge:
      * ``source='manual_reconciled'``
      * ``reconciled_from_orphan=True``
      * ``strategy_original_attribution`` — preserves any existing value;
        on a fresh row falls back to the current ``strategy`` field
        (which is the pre-fix mislabel we want preserved for audit);
        unrouted/empty-account rows get None.
      * ``strategy`` — topology-derived label.

    Empty ``account`` returns a ``_skip_reason`` sentinel so the dry-run
    can surface the row for operator inspection rather than silently
    fabricating a label.
    """
    account = row.get("account") or ""
    if not account:
        return {
            "_skip_reason": "account is NULL/empty — refusing to fabricate label",
        }

    target_strategy, routed = _target_strategy_label(account)
    current_source = row.get("source")
    current_strategy = row.get("strategy")
    current_orphan_flag = row.get("reconciled_from_orphan")

    fully_canonical = (
        current_source == "manual_reconciled"
        and current_orphan_flag is True
        and current_strategy == target_strategy
    )
    if fully_canonical:
        return None

    # Preserve strategy_original_attribution if it's already set; this
    # keeps the audit trail across multiple migration rounds. Only fall
    # back to the current strategy when no original was ever recorded.
    existing_original = row.get("strategy_original_attribution")
    if existing_original is not None:
        target_original = existing_original
    elif len(routed) == 0:
        target_original = None
    else:
        target_original = current_strategy

    return {
        "source": "manual_reconciled",
        "reconciled_from_orphan": True,
        "strategy_original_attribution": target_original,
        "strategy": target_strategy,
    }


class _Cwd:
    """Context manager to temporarily chdir. The canonical
    `core.trade_memory` module resolves both ``LEGACY_FILE`` and
    ``_per_bot_path`` relative to CWD; this scopes any chdir to the
    apply call so tests with a synthetic logs_dir work without
    permanently moving the process CWD.
    """

    def __init__(self, target: Path):
        self.target = target
        self._old: Optional[str] = None

    def __enter__(self):
        self._old = os.getcwd()
        os.chdir(str(self.target))

    def __exit__(self, exc_type, exc_val, tb):
        if self._old is not None:
            os.chdir(self._old)


def _apply_one(row: dict, update: dict, logs_dir: Path) -> tuple[bool, str]:
    """Call canonical writer for one row. Returns (ok, message).

    The writer (``core.trade_memory.TradeMemory.update_trade``) atomically
    updates the per-bot JSON file via os.replace AND syncs the SQLite
    shadow (P4-4 dual-write). NEVER raw-opens trade_memory.json.

    ``logs_dir`` is honored by chdir-ing the process into
    ``logs_dir.parent`` for the duration of the call so the canonical
    writer's CWD-relative paths
    (``logs/trade_memory.json`` legacy + ``logs/trade_memory_<bot>.json``
    per-bot) resolve into the chosen tree. The CWD is restored on exit.
    """
    from core.trade_memory import TradeMemory

    bot_id = row.get("bot_id") or "unknown"
    cwd_target = logs_dir.parent
    with _Cwd(cwd_target):
        tm = TradeMemory(bot_id=bot_id)
        ok = tm.update_trade(row["trade_id"], update)
    if not ok:
        return False, (
            f"update_trade returned False — trade_id not present in "
            f"trade_memory_{bot_id}.json or legacy. Row stays unmodified."
        )
    return True, "updated"


def _write_dryrun_report(
    rows: list[dict], plans: list[Optional[dict]], out_path: Path
) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    lines: list[str] = []
    lines.append(f"# HIST-MIGRATE Dry-Run Report — {_today_tag()}\n")
    lines.append(
        f"_Source:_ canonical view via `core.trade_memory.load_all_trades()`\n"
    )
    lines.append(
        f"_Rows matched by patterns:_ **{len(rows)}**\n"
    )
    accounts = Counter(r.get("account") for r in rows)
    bots = Counter(r.get("bot_id") for r in rows)
    lines.append(f"_Accounts seen:_ {dict(accounts)}\n")
    lines.append(f"_Bot attribution:_ {dict(bots)}\n")
    lines.append("\n## Per-row plan\n")
    lines.append(
        "| # | trade_id | bot_id | strategy (current) | strategy (after) | "
        "account | source (current) | status |\n"
    )
    lines.append("|---|---|---|---|---|---|---|---|\n")
    n_to_migrate = n_skipped_done = n_skipped_other = 0
    for i, (row, plan) in enumerate(zip(rows, plans), start=1):
        if plan is None:
            status = "ALREADY CANONICAL — skip (idempotent)"
            new_strat = row.get("strategy")
            n_skipped_done += 1
        elif "_skip_reason" in plan:
            status = f"SKIP: {plan['_skip_reason']}"
            new_strat = row.get("strategy")
            n_skipped_other += 1
        else:
            status = "MIGRATE"
            new_strat = plan["strategy"]
            n_to_migrate += 1
        lines.append(
            f"| {i} | `{row.get('trade_id')}` | {row.get('bot_id')} | "
            f"`{row.get('strategy')}` | `{new_strat}` | "
            f"{row.get('account')} | {row.get('source','<absent>')} | "
            f"{status} |\n"
        )
    lines.append("\n## Summary\n")
    lines.append(f"- Rows to migrate (`--apply` would change): **{n_to_migrate}**\n")
    lines.append(f"- Rows already canonical (skip): **{n_skipped_done}**\n")
    lines.append(f"- Rows skipped for other reason: **{n_skipped_other}**\n")
    lines.append("\n## Next step\n")
    if n_to_migrate == 0:
        lines.append("Nothing to do. All matching rows already canonical.\n")
    else:
        lines.append(
            "Re-run with `--apply` to perform the migration. Updates land\n"
            "via `core.trade_memory.TradeMemory.update_trade`, which writes\n"
            "atomically to the per-bot JSON file AND syncs the SQLite\n"
            "shadow. Bots MUST be quiesced first — otherwise the in-memory\n"
            "TradeMemory cache in a running bot will clobber the JSON on\n"
            "its next save() call (REDTEAM-2 round-1 root cause).\n"
        )
    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path


def _write_applied_report(
    rows: list[dict],
    results: list[tuple[bool, str]],
    out_path: Path,
    logs_dir: Path,
) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    lines: list[str] = []
    lines.append(f"# HIST-MIGRATE Applied Report — {_today_tag()}\n")
    n_ok = sum(1 for ok, _ in results if ok)
    n_fail = sum(1 for ok, _ in results if not ok)
    lines.append(
        f"_Migrated successfully:_ **{n_ok}**, _failed/skipped:_ **{n_fail}**\n"
    )
    lines.append("\n## Per-row outcome\n")
    lines.append("| # | trade_id | bot_id | account | ok? | message |\n")
    lines.append("|---|---|---|---|---|---|\n")
    for i, (row, (ok, msg)) in enumerate(zip(rows, results), start=1):
        flag = "OK" if ok else "FAIL"
        lines.append(
            f"| {i} | `{row.get('trade_id')}` | {row.get('bot_id')} | "
            f"{row.get('account')} | {flag} | {msg} |\n"
        )

    # Post-condition: re-read the canonical view and verify each migrated
    # row now carries the target fields. Catches the round-1 silent
    # clobber: a successful apply that disagrees with what's on disk.
    lines.append("\n## Post-apply canonical-view verification\n")
    try:
        from core.trade_memory import load_all_trades
        all_rows = load_all_trades(logs_dir=str(logs_dir))
        by_id = {r.get("trade_id"): r for r in all_rows if r.get("trade_id")}
    except Exception as e:
        lines.append(f"\n_load_all_trades failed: {e!r}_\n")
        by_id = {}

    verified: list[tuple[str, bool, str]] = []
    for row, (ok, _) in zip(rows, results):
        if not ok:
            continue
        tid = row.get("trade_id")
        canonical_row = by_id.get(tid)
        if canonical_row is None:
            verified.append((tid, False, "trade_id missing from canonical view"))
            continue
        target_strategy, _ = _target_strategy_label(canonical_row.get("account") or "")
        ok_v = (
            canonical_row.get("source") == "manual_reconciled"
            and canonical_row.get("reconciled_from_orphan") is True
            and canonical_row.get("strategy") == target_strategy
        )
        verified.append(
            (
                tid,
                ok_v,
                f"strategy={canonical_row.get('strategy')!r} "
                f"source={canonical_row.get('source')!r} "
                f"reconciled_from_orphan={canonical_row.get('reconciled_from_orphan')!r} "
                f"strategy_original_attribution={canonical_row.get('strategy_original_attribution')!r}",
            )
        )
    lines.append("| trade_id | post-write verified? | canonical state |\n")
    lines.append("|---|---|---|\n")
    for tid, ok_v, msg in verified:
        lines.append(f"| `{tid}` | {'YES' if ok_v else 'NO'} | {msg} |\n")
    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill source flag on RECONCILED_* rows across the canonical "
            "trade_memory view. Operates on JSON via TradeMemory.update_trade — "
            "never raw-opens trade_memory.json. Run with bots quiesced "
            "(REDTEAM-2 round-1 race)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the migration. Default is dry-run.",
    )
    parser.add_argument(
        "--patterns",
        type=str,
        default=",".join(DEFAULT_PATTERNS),
        help=(
            "Comma-separated regexes to match trade_ids against. "
            f"Default: {','.join(DEFAULT_PATTERNS)!r}."
        ),
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default=str(DEFAULT_LOGS_DIR),
        help=f"Override the trade_memory directory. Default: {DEFAULT_LOGS_DIR}.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help=(
            "Override the dry-run / applied-report output path. Default: "
            "out/hist_migrate_{dryrun,applied}_<date>.md"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    patterns = tuple(re.compile(p) for p in args.patterns.split(",") if p.strip())
    if not patterns:
        print(
            "FATAL: --patterns produced an empty pattern set; nothing to match.",
            file=sys.stderr,
        )
        return 2
    logs_dir = Path(args.logs_dir)

    rows = _read_canonical_rows(logs_dir, patterns)
    plans = [_plan_update(r) for r in rows]
    n_to_migrate = sum(1 for p in plans if p and "_skip_reason" not in p)

    if not args.apply:
        out_path = Path(args.out) if args.out else (
            OUT_DIR / f"hist_migrate_dryrun_{_today_tag()}.md"
        )
        _write_dryrun_report(rows, plans, out_path)
        print(f"DRY-RUN — {n_to_migrate} rows would be migrated.")
        print(f"Report: {out_path}")
        print("Re-run with --apply to perform the migration.")
        return 0

    # --apply mode
    results: list[tuple[bool, str]] = []
    for row, plan in zip(rows, plans):
        if plan is None:
            results.append((True, "skip (already canonical)"))
            continue
        if "_skip_reason" in plan:
            results.append((False, plan["_skip_reason"]))
            continue
        ok, msg = _apply_one(row, plan, logs_dir)
        results.append((ok, msg))

    out_path = Path(args.out) if args.out else (
        OUT_DIR / f"hist_migrate_applied_{_today_tag()}.md"
    )
    _write_applied_report(rows, results, out_path, logs_dir)
    n_ok = sum(1 for ok, _ in results if ok)
    n_fail = sum(1 for ok, _ in results if not ok)
    print(f"APPLIED — ok={n_ok}, failed/skipped={n_fail}.")
    print(f"Report: {out_path}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
