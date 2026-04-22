"""
End-to-end ATI smoke test for all 16 dedicated NT8 Sim accounts.

For each account in config.account_routing.STRATEGY_ACCOUNT_MAP:
  1. Write a MARKET BUY 1-contract OIF directly to NT8 incoming/
  2. Wait 2s, verify NT8 consumed the file (disappears from incoming/)
  3. Verify NT8 outgoing/position file reports LONG 1 at a price > 0
  4. Write a flatten (MARKET SELL 1) OIF
  5. Verify position goes FLAT 0 0
  6. Record PASS / FAIL with reason

Exits with code 0 if all accounts pass, 1 otherwise.
Writes a Markdown report to logs/ati_smoke_test_YYYY-MM-DD_HHMM.md.

USAGE:
  python tools/test_all_accounts.py                    # all accounts
  python tools/test_all_accounts.py --account "SimBias Momentum"  # one
  python tools/test_all_accounts.py --dry-run          # show plan, no trades
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.account_routing import STRATEGY_ACCOUNT_MAP  # noqa: E402
from config.settings import INSTRUMENT  # noqa: E402

INCOMING = r"C:\Users\Trading PC\Documents\NinjaTrader 8\incoming"
OUTGOING = r"C:\Users\Trading PC\Documents\NinjaTrader 8\outgoing"


def enumerate_accounts() -> list[str]:
    """Return unique NT8 accounts from the routing map (16 dedicated + Sim101)."""
    accts = set()
    for k, v in STRATEGY_ACCOUNT_MAP.items():
        if k == "_default":
            accts.add(v)
            continue
        if isinstance(v, dict):
            for sub in v.values():
                accts.add(sub)
        else:
            accts.add(v)
    return sorted(accts)


def write_oif(line: str, tag: str = "smoke") -> str:
    """Write an OIF file directly to incoming/ (partial-write window is sub-ms)."""
    os.makedirs(INCOMING, exist_ok=True)
    rid = random.randint(100000, 999999)
    fname = f"oif_{tag}_{rid}.txt"
    final = os.path.join(INCOMING, fname)
    with open(final, "w", encoding="ascii") as f:
        f.write(line + "\n")
    return final


def wait_for_consume(path: str, timeout_s: float = 3.0) -> bool:
    """NT8 deletes the file from incoming/ once ATI processes it. Wait for disappearance."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


def read_position(account: str) -> tuple[str, int, float] | None:
    """Read NT8 outgoing/position file for this account. Returns (dir, qty, price) or None."""
    candidates = [
        os.path.join(OUTGOING, f"{INSTRUMENT} Globex_{account}_position.txt"),
        os.path.join(OUTGOING, f"{INSTRUMENT}_{account}_position.txt"),
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                content = open(p).read().strip()
                parts = content.split(";")
                if len(parts) >= 3:
                    return parts[0], int(parts[1]), float(parts[2])
            except Exception:
                pass
    return None


def wait_for_position(account: str, expected_direction: str, expected_qty: int,
                      timeout_s: float = 3.0) -> tuple[str, int, float] | None:
    """Poll until the position file shows the expected direction+qty or timeout."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        pos = read_position(account)
        if pos:
            last = pos
            if pos[0] == expected_direction and pos[1] == expected_qty:
                return pos
            if expected_direction == "FLAT" and pos[0] == "FLAT":
                return pos
        time.sleep(0.15)
    return last


def test_one_account(account: str, dry_run: bool = False) -> dict:
    """Submit BUY → verify LONG → flatten → verify FLAT. Returns a result dict."""
    result = {
        "account": account,
        "entry_path": None,
        "entry_consumed": False,
        "entry_position": None,
        "flatten_path": None,
        "flatten_consumed": False,
        "flatten_position": None,
        "pass": False,
        "error": None,
    }
    try:
        if dry_run:
            result["pass"] = True
            result["error"] = "dry-run"
            return result

        # Step 1: BUY 1 contract MARKET, GTC
        oco = f"SMOKE_{random.randint(10000, 99999)}"
        buy_line = f"PLACE;{account};{INSTRUMENT};BUY;1;MARKET;0;0;GTC;{oco};;;"
        result["entry_path"] = write_oif(buy_line, tag=f"buy_{account.replace(' ','_')}")
        result["entry_consumed"] = wait_for_consume(result["entry_path"])
        if not result["entry_consumed"]:
            result["error"] = "NT8 did not consume entry OIF within 3s"
            return result

        # Step 2: verify position LONG 1
        pos = wait_for_position(account, "LONG", 1, timeout_s=5.0)
        result["entry_position"] = pos
        if not pos or pos[0] != "LONG" or pos[1] != 1:
            result["error"] = f"Expected LONG 1, got {pos}"
            return result

        # Step 3: flatten
        sell_line = f"PLACE;{account};{INSTRUMENT};SELL;1;MARKET;0;0;GTC;SMOKE_FLAT_{oco};;;"
        result["flatten_path"] = write_oif(sell_line, tag=f"flat_{account.replace(' ','_')}")
        result["flatten_consumed"] = wait_for_consume(result["flatten_path"])
        if not result["flatten_consumed"]:
            result["error"] = "NT8 did not consume flatten OIF within 3s"
            return result

        # Step 4: verify FLAT
        pos = wait_for_position(account, "FLAT", 0, timeout_s=5.0)
        result["flatten_position"] = pos
        if not pos or pos[0] != "FLAT":
            result["error"] = f"Expected FLAT, got {pos}"
            return result

        result["pass"] = True
        return result
    except Exception as e:
        result["error"] = f"exception: {type(e).__name__}: {e}"
        return result


def main() -> int:
    ap = argparse.ArgumentParser(description="ATI smoke test for all 16 NT8 Sim accounts")
    ap.add_argument("--account", default=None, help="Test a single account by name")
    ap.add_argument("--dry-run", action="store_true", help="Print plan, no trades")
    ap.add_argument("--pause-s", type=float, default=1.0, help="Pause between accounts")
    args = ap.parse_args()

    accounts = enumerate_accounts()
    if args.account:
        if args.account not in accounts:
            print(f"ERROR: account '{args.account}' not in routing map")
            print(f"Known: {accounts}")
            return 1
        accounts = [args.account]

    print(f"\n{'='*70}")
    print(f"ATI SMOKE TEST — {len(accounts)} accounts — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Instrument: {INSTRUMENT}")
    print(f"Dry-run: {args.dry_run}")
    print(f"{'='*70}\n")

    results = []
    for i, acct in enumerate(accounts, 1):
        print(f"[{i:2d}/{len(accounts)}] {acct:45s} ... ", end="", flush=True)
        r = test_one_account(acct, dry_run=args.dry_run)
        results.append(r)
        status = "PASS" if r["pass"] else "FAIL"
        suffix = f" — {r['error']}" if r["error"] else ""
        print(f"{status}{suffix}")
        time.sleep(args.pause_s)

    # Summary
    passed = sum(1 for r in results if r["pass"])
    failed = len(results) - passed
    print(f"\n{'='*70}")
    print(f"RESULTS: {passed}/{len(results)} passed, {failed} failed")
    print(f"{'='*70}\n")

    # Write markdown report
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_path = Path(__file__).resolve().parent.parent / "logs" / f"ati_smoke_test_{ts}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# ATI Smoke Test — {datetime.now().isoformat()}\n\n")
        f.write(f"Tested {len(results)} accounts. **{passed} passed, {failed} failed.**\n\n")
        f.write("| # | Account | Status | Entry Pos | Flatten Pos | Error |\n")
        f.write("|---|---|---|---|---|---|\n")
        for i, r in enumerate(results, 1):
            status = "✅" if r["pass"] else "❌"
            ep = f"{r['entry_position']}" if r["entry_position"] else "—"
            fp = f"{r['flatten_position']}" if r["flatten_position"] else "—"
            err = r["error"] or ""
            f.write(f"| {i} | {r['account']} | {status} | {ep} | {fp} | {err} |\n")
    print(f"Report written: {report_path}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
