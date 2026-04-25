#!/usr/bin/env python3
"""
Phoenix Bot -- OIF Kill Switch (Phase B+ section 3.2)

Flattens NT8 across every configured account by writing OIF files
directly to NT8's incoming/ folder:

  1. CANCELALLORDERS;<account>;;;;;;;;;;;     -> kill working orders
  2. CLOSEPOSITION;<account>;<instrument>;GTC;;;;;;;;;   -> flatten any
     open position (skipped if outgoing/ position file already FLAT)

Distinct from KillSwitch.bat (which kills the Python processes). After
KillSwitch.bat tears down Phoenix, NT8 is still running with whatever
working orders / open positions it had. This tool sweeps NT8 itself.

Usage:
  python tools/oif_killswitch.py
  python tools/oif_killswitch.py --account Sim101
  python tools/oif_killswitch.py --account Sim101 --account "SimBias Momentum"
  python tools/oif_killswitch.py --cancel-only
  python tools/oif_killswitch.py --close-only
  python tools/oif_killswitch.py --dry-run
  python tools/oif_killswitch.py --instrument MNQU6

Exit codes:
  0  every targeted account had its OIFs written successfully
  1  partial -- at least one write failed (or LIVE_ACCOUNT skip warned)
  2  configuration error (no accounts resolvable, paths missing)

B59 LIVE_ACCOUNT guard: if any target account matches the LIVE_ACCOUNT
env var, this tool refuses to write OIFs for THAT account and prints a
loud warning. Other accounts proceed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

# Project root on sys.path so `bridge.*` and `config.*` imports resolve.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from bridge import oif_writer as _oif  # noqa: E402
from bridge.oif_writer import (  # noqa: E402
    cancel_all_orders_line,
    close_position_line,
)
from config import account_routing as _routing  # noqa: E402
from config import settings as _settings  # noqa: E402


# ---------------------------------------------------------------------------
# Position-file inspection (read NT8 outgoing/ to filter already-FLAT accounts)
# ---------------------------------------------------------------------------

_POS_RE = re.compile(
    r"^(?P<direction>FLAT|LONG|SHORT);(?P<qty>-?\d+);(?P<price>-?[\d.]+)\s*$"
)


def _outgoing_dir() -> Path:
    """NT8 outgoing/ path. Mirrors bridge.oif_writer.verify_nt8_position."""
    return Path(os.path.dirname(_oif.OIF_INCOMING)) / "outgoing"


def _read_position(account: str, instrument: str) -> dict:
    """Parse `<inst> Globex_<account>_position.txt` (NT8's standard
    naming) and return {direction, qty, price, raw, source}. If no file
    is found, direction defaults to 'UNKNOWN' which the caller treats as
    'maybe non-flat' -> CLOSEPOSITION will be issued anyway (safer).
    """
    outgoing = _outgoing_dir()
    candidates = [
        outgoing / f"{instrument} Globex_{account}_position.txt",
        outgoing / f"{instrument}_{account}_position.txt",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        m = _POS_RE.match(content)
        if not m:
            return {"direction": "UNPARSED", "qty": 0, "price": 0.0,
                    "raw": content, "source": str(p)}
        return {"direction": m.group("direction"),
                "qty": int(m.group("qty")),
                "price": float(m.group("price")),
                "raw": content,
                "source": str(p)}
    return {"direction": "UNKNOWN", "qty": 0, "price": 0.0,
            "raw": "", "source": ""}


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------

def _resolve_targets(cli_accounts: list[str] | None) -> list[str]:
    """If --account was given (one or more), use exactly that list; else
    pull every unique account from config.account_routing and prepend the
    module-level Sim101 fallback (which prod_bot uses)."""
    if cli_accounts:
        return list(dict.fromkeys(cli_accounts))  # dedupe, preserve order
    accts = list(_routing.validate_account_map())
    fallback = getattr(_settings, "ACCOUNT", "Sim101")
    if fallback and fallback not in accts:
        accts.insert(0, fallback)
    return accts


# ---------------------------------------------------------------------------
# OIF writing (direct, no atomic stage -- single-line CANCEL/CLOSE files
# are tiny and NT8's watcher coalesces final-write events fine; this
# matches bridge/oif_writer._stage_oif's post-B45 direct-write strategy.)
# ---------------------------------------------------------------------------

# Unique counter so successive files don't collide. Mirrors
# bridge/oif_writer._oif_counter shape but kept local so we don't depend
# on its internals.
_KS_COUNTER = int(time.time() * 1000) % 1000000


def _write_oif_file(line: str, account: str, kind: str) -> str:
    """Write a single OIF line to NT8's incoming/ folder using the same
    `oif<n>_phoenix_<pid>_killswitch_<kind>_<account>.txt` shape that
    PhoenixOIFGuard accepts (see bridge.oif_writer._stage_oif).

    Returns the path written.
    """
    global _KS_COUNTER
    _KS_COUNTER += 1
    pid = os.getpid()
    safe_acct = re.sub(r"[^A-Za-z0-9]+", "_", account).strip("_") or "acct"
    fname = (
        f"oif{_KS_COUNTER}_phoenix_{pid}_killswitch_{kind}_{safe_acct}.txt"
    )
    incoming = _oif.OIF_INCOMING
    os.makedirs(incoming, exist_ok=True)
    final_path = os.path.join(incoming, fname)
    with open(final_path, "w", encoding="ascii") as f:
        f.write(line + "\n")
    return final_path


# ---------------------------------------------------------------------------
# Per-account flatten plan + execution
# ---------------------------------------------------------------------------

def _is_live(account: str) -> bool:
    """B59: account matches LIVE_ACCOUNT env var (refuse OIF for it)."""
    live = os.environ.get("LIVE_ACCOUNT", "").strip()
    return bool(live) and str(account).strip() == live


def _plan_for_account(account: str, instrument: str,
                      do_cancel: bool, do_close: bool) -> dict:
    """Build (don't execute) the flatten plan for a single account."""
    plan = {
        "account": account,
        "is_live": _is_live(account),
        "cancel_line": None,
        "close_line": None,
        "position": None,
        "skip_close_reason": None,
        "skip_cancel_reason": None,
    }
    if plan["is_live"]:
        plan["skip_cancel_reason"] = "LIVE_ACCOUNT guard (B59)"
        plan["skip_close_reason"] = "LIVE_ACCOUNT guard (B59)"
        return plan

    if do_cancel:
        plan["cancel_line"] = cancel_all_orders_line(account=account)
    else:
        plan["skip_cancel_reason"] = "--close-only"

    if do_close:
        pos = _read_position(account, instrument)
        plan["position"] = pos
        if pos["direction"] == "FLAT":
            plan["skip_close_reason"] = "already flat"
        else:
            # Non-FLAT, UNPARSED, or UNKNOWN -> issue CLOSEPOSITION.
            # UNKNOWN/UNPARSED still get a close: we'd rather over-close
            # than leave a stranded position.
            plan["close_line"] = close_position_line(
                account=account, instrument=instrument,
            )
    else:
        plan["skip_close_reason"] = "--cancel-only"

    return plan


def _execute_plan(plan: dict, dry_run: bool) -> tuple[list[str], list[str]]:
    """Returns (paths_written, errors)."""
    written: list[str] = []
    errors: list[str] = []
    acct = plan["account"]

    if plan["cancel_line"] and not dry_run:
        try:
            written.append(_write_oif_file(plan["cancel_line"], acct, "cancel"))
        except OSError as e:
            errors.append(f"cancel write failed for {acct}: {e}")

    if plan["close_line"] and not dry_run:
        try:
            written.append(_write_oif_file(plan["close_line"], acct, "close"))
        except OSError as e:
            errors.append(f"close write failed for {acct}: {e}")

    return written, errors


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _summary_line(plan: dict, written: list[str]) -> str:
    acct = plan["account"]
    if plan["is_live"]:
        return (f"  !! {acct}: SKIPPED (LIVE_ACCOUNT guard) -- "
                f"refusing CANCEL/CLOSE on live account")

    cancel_part = (
        "YES" if plan["cancel_line"]
        else f"NO ({plan['skip_cancel_reason'] or 'n/a'})"
    )
    pos = plan["position"]
    if plan["close_line"]:
        close_part = "YES"
    elif pos and pos["direction"] == "FLAT":
        close_part = "NO (already flat)"
    elif plan["skip_close_reason"]:
        close_part = f"NO ({plan['skip_close_reason']})"
    else:
        close_part = "NO"

    pos_str = ""
    if pos:
        if pos["direction"] in ("LONG", "SHORT"):
            pos_str = f" pos={pos['direction']}{pos['qty']}@{pos['price']}"
        elif pos["direction"] in ("UNKNOWN", "UNPARSED"):
            pos_str = f" pos={pos['direction']}"
    return f"  - {acct}: cancel={cancel_part} close={close_part}{pos_str}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Phoenix OIF kill-switch. Flattens working orders "
                     "and open positions across every NT8 account by "
                     "writing CANCELALLORDERS + CLOSEPOSITION OIFs."),
    )
    parser.add_argument(
        "--account", action="append", default=[],
        help=("Target a specific NT8 account. Repeat to target multiple. "
              "If omitted, every account from config.account_routing is "
              "targeted (plus the module-level fallback Sim101)."),
    )
    parser.add_argument(
        "--cancel-only", action="store_true",
        help="Only write CANCELALLORDERS; skip CLOSEPOSITION.",
    )
    parser.add_argument(
        "--close-only", action="store_true",
        help="Only write CLOSEPOSITION; skip CANCELALLORDERS.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan but write nothing.",
    )
    parser.add_argument(
        "--instrument", default=getattr(_settings, "INSTRUMENT", "MNQM6"),
        help="Instrument for CLOSEPOSITION (default: settings.INSTRUMENT).",
    )
    args = parser.parse_args(argv)

    if args.cancel_only and args.close_only:
        print("ERROR: --cancel-only and --close-only are mutually exclusive.",
              file=sys.stderr)
        return 2

    do_cancel = not args.close_only
    do_close = not args.cancel_only

    try:
        targets = _resolve_targets(args.account)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not resolve target accounts: {e}", file=sys.stderr)
        return 2

    if not targets:
        print("ERROR: no target accounts (config.account_routing returned "
              "empty and no --account provided).", file=sys.stderr)
        return 2

    print("=" * 72)
    print(" Phoenix OIF Kill-Switch")
    print(f" Mode       : {'DRY-RUN' if args.dry_run else 'LIVE WRITE'}")
    print(f" Instrument : {args.instrument}")
    print(f" Incoming   : {_oif.OIF_INCOMING}")
    print(f" Targets    : {len(targets)} account(s)")
    print(f" Cancel ord : {'YES' if do_cancel else 'NO'}")
    print(f" Close pos  : {'YES' if do_close else 'NO'}")
    live = os.environ.get("LIVE_ACCOUNT", "").strip()
    if live:
        print(f" LIVE_ACCT  : {live}  (guarded -- will be skipped)")
    print("=" * 72)

    total_written: list[str] = []
    total_errors: list[str] = []
    skipped_live = 0

    for acct in targets:
        try:
            plan = _plan_for_account(
                acct, args.instrument, do_cancel, do_close,
            )
        except Exception as e:  # noqa: BLE001
            total_errors.append(f"plan failed for {acct}: {e}")
            print(f"  ! {acct}: PLAN ERROR -- {e}")
            continue

        if plan["is_live"]:
            skipped_live += 1

        written, errors = _execute_plan(plan, dry_run=args.dry_run)
        total_written.extend(written)
        total_errors.extend(errors)

        print(_summary_line(plan, written))
        if args.dry_run:
            if plan["cancel_line"]:
                print(f"      would write: {plan['cancel_line']}")
            if plan["close_line"]:
                print(f"      would write: {plan['close_line']}")

    print("-" * 72)
    print(f" Files written : {len(total_written)}"
          + ("  (dry-run, none on disk)" if args.dry_run else ""))
    if skipped_live:
        print(f" LIVE skipped  : {skipped_live} account(s) "
              f"(LIVE_ACCOUNT guard)")
    if total_errors:
        print(" Errors:")
        for e in total_errors:
            print(f"   - {e}")
    print("=" * 72)

    if total_errors or skipped_live:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
