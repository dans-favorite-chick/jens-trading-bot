#!/usr/bin/env python3
"""
Phoenix Bot — NT8 Outgoing Janitor

Purpose
-------
Remove stale per-order acknowledgment files that NinjaTrader 8 writes into
its `outgoing/` folder every time an order is placed. After the order has
filled/cancelled, nothing reads these files — they just accumulate and
bloat folder scans (check_nt8_outgoing.py, OIF guard, bridge inventory),
making diagnostics noisier and slower over time.

Naming pattern targeted (NT8's own convention):

    <account_or_strategy>_<32-hex-guid>.txt

Examples seen on Trading PC (2026-05-25):
    SimBias Momentum_094c8273aa1e48768ad77abc1c2ad8d2.txt
    Sim101_07abb8b7a7704a7ab4b007a00b92ce72.txt
    Sim_VWAP_Pullback_v2_0b17993a2865419da755de6b05440f1b.txt
    SimORB_e3bd446ce30c4546975c19...txt

Safety contract — files that MUST be preserved (never deleted)
-------------------------------------------------------------
1. `active_stops.json`              — Phoenix reads this for live stops.
2. `<INSTRUMENT> <EXCHANGE>_<account>_position.txt`
                                    — NT8 position state. Phoenix &
                                      check_nt8_outgoing.py rely on it.
                                      e.g. "MNQM6 Globex_Sim101_position.txt"
3. Feed status files:
       "Kinetick – End Of Day (Free).txt"
       "Live.txt"
       "Simulated Data Feed.txt"
4. ANY file whose stem does NOT end with `_<exactly-32-lowercase-hex>`.
   The match regex is intentionally tight:
       ^.+_[0-9a-f]{32}\\.txt$
   so a position file like `MNQM6 Globex_Sim101_position.txt` cannot
   possibly match (no 32-hex tail), and feed files cannot match (no
   trailing `_<hex>` block).

Default behaviour is **dry-run**. The operator must pass `--apply` for
any file to be deleted. The defaults are intentionally conservative
(7-day age cutoff) so a same-day order ack isn't whisked out from
under NT8 mid-fill.

Invocation
----------
    python tools/clean_nt8_outgoing.py                    # dry-run, age >= 7 days
    python tools/clean_nt8_outgoing.py --days 14          # only files >= 14 days
    python tools/clean_nt8_outgoing.py --apply            # actually delete
    python tools/clean_nt8_outgoing.py --path D:\test     # override outgoing folder
    python tools/clean_nt8_outgoing.py --debug            # log every preserved file

Exit codes
----------
    0 — success (including "nothing to delete")
    1 — filesystem error (path missing, permission denied, etc.)

Scheduling
----------
To run daily as a Windows scheduled task (operator approval required):

    schtasks /create /tn PhoenixOutgoingJanitor \\
        /tr "python C:\\Trading Project\\phoenix_bot\\tools\\clean_nt8_outgoing.py --apply" \\
        /sc daily /st 23:30

This tool does NOT register the task itself — operator wants to approve
first.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

# Make config importable when run from anywhere
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from config.settings import OIF_OUTGOING as _CFG_OUTGOING  # type: ignore
except Exception:  # pragma: no cover - exercised only if settings import fails
    _CFG_OUTGOING = r"C:\Users\Trading PC\Documents\NinjaTrader 8\outgoing"


# Tight regex: stem must end with underscore + EXACTLY 32 lowercase hex chars.
# A position file ("..._position.txt") has no 32-hex suffix, so it can't match.
# A feed file ("Live.txt", "Simulated Data Feed.txt") has no `_<hex>` at all.
ORDER_ACK_RE = re.compile(r"^.+_[0-9a-f]{32}\.txt$")

# Sentinel filenames that must NEVER be considered candidates, even if some
# future NT8 release changes naming — belt-and-braces alongside the regex.
EXPLICIT_PRESERVE = frozenset({
    "active_stops.json",
    "Live.txt",
    "Simulated Data Feed.txt",
    "Kinetick – End Of Day (Free).txt",
})

DEFAULT_AGE_DAYS = 7

log = logging.getLogger("clean_nt8_outgoing")


# ════════════════════════════════════════════════════════════════════════
# Data structures
# ════════════════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    """A file matched by the order-ack regex and old enough to delete."""
    path: Path
    age_seconds: float
    size_bytes: int

    @property
    def age_days(self) -> float:
        return self.age_seconds / 86400.0


@dataclass
class ScanResult:
    """Outcome of one folder scan. Pure data — printing is separate."""
    path: Path
    scanned_at: datetime
    total_scanned: int = 0
    preserved: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    too_young: int = 0
    cutoff_days: float = DEFAULT_AGE_DAYS

    @property
    def total_bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)

    @property
    def ages_seconds(self) -> list[float]:
        return [c.age_seconds for c in self.candidates]


# ════════════════════════════════════════════════════════════════════════
# Core logic
# ════════════════════════════════════════════════════════════════════════

def matches_order_ack_pattern(name: str) -> bool:
    """Return True iff `name` matches the strict order-ack pattern.

    Position / feed / active_stops.json files cannot match.
    """
    if name in EXPLICIT_PRESERVE:
        return False
    return bool(ORDER_ACK_RE.match(name))


def _iter_files(path: Path) -> Iterable[Path]:
    for p in sorted(path.iterdir()):
        if p.is_file():
            yield p


def scan(path: Path, age_days: float = DEFAULT_AGE_DAYS,
         now: float | None = None) -> ScanResult:
    """Scan `path` and classify each file. Does NOT delete anything.

    Raises:
        FileNotFoundError: if `path` doesn't exist.
        NotADirectoryError: if `path` exists but isn't a directory.
    """
    if not path.exists():
        raise FileNotFoundError(f"Outgoing folder not found: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    now_epoch = time.time() if now is None else now
    cutoff_seconds = age_days * 86400.0
    result = ScanResult(path=path, scanned_at=datetime.now(),
                        cutoff_days=age_days)

    for p in _iter_files(path):
        result.total_scanned += 1
        name = p.name
        if not matches_order_ack_pattern(name):
            result.preserved += 1
            log.debug("PRESERVE  %s  (does not match order-ack pattern)", name)
            continue
        try:
            st = p.stat()
        except OSError as e:
            log.warning("Could not stat %s: %s — skipping", name, e)
            result.preserved += 1
            continue
        age_s = now_epoch - st.st_mtime
        if age_s < cutoff_seconds:
            result.too_young += 1
            log.debug("PRESERVE  %s  (age %.1fd < cutoff %.1fd)",
                      name, age_s / 86400.0, age_days)
            continue
        result.candidates.append(Candidate(
            path=p,
            age_seconds=age_s,
            size_bytes=st.st_size,
        ))
    return result


def apply_deletions(result: ScanResult) -> tuple[int, int]:
    """Delete every candidate. Returns (deleted, failed)."""
    deleted = failed = 0
    for c in result.candidates:
        try:
            c.path.unlink()
            log.info("DELETED   %s  (age %.1fd, %d bytes)",
                     c.path.name, c.age_days, c.size_bytes)
            deleted += 1
        except OSError as e:
            log.error("FAILED    %s: %s", c.path.name, e)
            failed += 1
    return deleted, failed


# ════════════════════════════════════════════════════════════════════════
# Reporting
# ════════════════════════════════════════════════════════════════════════

def _format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024.0:
            return f"{f:,.1f} {u}"
        f /= 1024.0
    return f"{f:,.1f} TB"


def print_summary(result: ScanResult, apply: bool,
                  deleted: int = 0, failed: int = 0) -> None:
    print("=" * 72)
    print(" Phoenix NT8 Outgoing Janitor")
    print(f" Path        : {result.path}")
    print(f" Scanned at  : {result.scanned_at.isoformat(timespec='seconds')}")
    print(f" Cutoff age  : {result.cutoff_days} day(s)")
    print(f" Mode        : {'APPLY (deletions enabled)' if apply else 'DRY-RUN (no changes)'}")
    print("=" * 72)
    print(f"  Total files scanned     : {result.total_scanned:>6}")
    print(f"  Preserved (not matched) : {result.preserved:>6}")
    print(f"  Too young (kept)        : {result.too_young:>6}")
    print(f"  Candidates              : {len(result.candidates):>6}")
    print(f"  Bytes that would free   : {_format_bytes(result.total_bytes):>10}")

    if result.candidates:
        ages_days = sorted(c.age_days for c in result.candidates)
        oldest = ages_days[-1]
        newest = ages_days[0]
        median = statistics.median(ages_days)
        print()
        print(f"  Age distribution (days):")
        print(f"    oldest = {oldest:.2f}   newest = {newest:.2f}   "
              f"median = {median:.2f}")

        top5 = sorted(result.candidates, key=lambda c: -c.age_seconds)[:5]
        print(f"  Top 5 oldest:")
        for c in top5:
            print(f"    - {c.path.name}  ({c.age_days:.1f}d, "
                  f"{_format_bytes(c.size_bytes)})")

    if apply:
        print()
        print(f"  Deleted   : {deleted}")
        if failed:
            print(f"  Failed    : {failed}   <-- see ERROR log lines above")

    print()
    if not apply and result.candidates:
        print("  Re-run with --apply to actually delete these files.")
    elif not result.candidates:
        print("  Nothing to delete. Folder is clean for the given cutoff.")

    # Scheduling hint — operator wants approval before wiring this up.
    exe = sys.executable or "python"
    print()
    print("  To run daily, add a Windows scheduled task:")
    print(f"    schtasks /create /tn PhoenixOutgoingJanitor /tr "
          f"'{exe} {Path(__file__).resolve()} --apply' /sc daily /st 23:30")


# ════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clean_nt8_outgoing",
        description=("Remove stale NT8 per-order acknowledgment files from "
                     "the outgoing/ folder. Dry-run by default; pass --apply "
                     "to actually delete."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--apply", action="store_true",
                   help="Actually delete matching files. Without this flag, "
                        "the tool only reports what WOULD be deleted.")
    p.add_argument("--days", type=float, default=DEFAULT_AGE_DAYS,
                   help=f"Only delete files older than N days (default: "
                        f"{DEFAULT_AGE_DAYS}).")
    p.add_argument("--path", type=str, default=str(_CFG_OUTGOING),
                   help=f"Override outgoing folder (default: "
                        f"config.settings.OIF_OUTGOING = {_CFG_OUTGOING}).")
    p.add_argument("--debug", action="store_true",
                   help="Verbose: log every preserved file at DEBUG level.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    path = Path(args.path)
    try:
        result = scan(path, age_days=args.days)
    except (FileNotFoundError, NotADirectoryError) as e:
        log.error(str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except PermissionError as e:
        log.error("Permission denied reading %s: %s", path, e)
        print(f"ERROR: permission denied: {e}", file=sys.stderr)
        return 1

    deleted = failed = 0
    if args.apply and result.candidates:
        deleted, failed = apply_deletions(result)

    print_summary(result, apply=args.apply, deleted=deleted, failed=failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
