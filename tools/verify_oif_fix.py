"""
One-off verification: write a MARKET BUY via the REAL bridge.oif_writer
(post-fix filename shape) and confirm NT8 accepts it.

Procedure:
  1. Record starting position on test account.
  2. write_oif("ENTER_LONG", MARKET, qty=1) via real writer.
  3. Poll outgoing/position file for LONG 1.
  4. write_oif("EXIT") to flatten.
  5. Poll for FLAT.
  6. Print PASS/FAIL with evidence.

Test account is chosen to be idle (no live signals routed to it right now).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import oif_writer
from config.settings import INSTRUMENT

TEST_ACCOUNT = "SimBias Momentum"
OUTGOING = r"C:\Users\Trading PC\Documents\NinjaTrader 8\outgoing"
INCOMING = r"C:\Users\Trading PC\Documents\NinjaTrader 8\incoming"


def read_position(account: str):
    for p in [
        os.path.join(OUTGOING, f"{INSTRUMENT} Globex_{account}_position.txt"),
        os.path.join(OUTGOING, f"{INSTRUMENT}_{account}_position.txt"),
    ]:
        if os.path.exists(p):
            try:
                parts = open(p).read().strip().split(";")
                if len(parts) >= 3:
                    return parts[0], int(parts[1]), float(parts[2])
            except Exception:
                pass
    return None


def wait_for(account: str, direction: str, qty: int, timeout_s: float = 4.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        pos = read_position(account)
        if pos:
            last = pos
            if pos[0] == direction and pos[1] == qty:
                return pos
        time.sleep(0.15)
    return last


def main():
    print(f"[VERIFY] Using real bridge.oif_writer (pid={os.getpid()})")
    print(f"[VERIFY] Test account: {TEST_ACCOUNT}")
    print(f"[VERIFY] Instrument: {INSTRUMENT}")

    start_pos = read_position(TEST_ACCOUNT)
    print(f"[VERIFY] Starting position: {start_pos}")
    if start_pos and start_pos[1] != 0:
        print("[VERIFY] FAIL: account is not flat; refusing to trade.")
        return 2

    # ── ENTER LONG 1 @ MARKET ───────────────────────────────────────
    print("[VERIFY] Step 1: write_oif ENTER_LONG MARKET qty=1")
    t0 = time.time()
    paths = oif_writer.write_oif(
        "ENTER_LONG",
        qty=1,
        order_type="MARKET",
        trade_id=f"verify_{int(t0)}",
        account=TEST_ACCOUNT,
    )
    print(f"[VERIFY]   wrote: {[os.path.basename(p) for p in paths]}")
    # Confirm filename shape: starts with 'oif', contains _phoenix_<pid>_
    for p in paths:
        name = os.path.basename(p)
        tag = f"_phoenix_{os.getpid()}_"
        assert name.startswith("oif"), f"BAD PREFIX: {name}"
        assert tag in name, f"MISSING TAG: {name}"
    print(f"[VERIFY]   filename shape OK (starts with oif, has {tag!r})")

    pos = wait_for(TEST_ACCOUNT, "LONG", 1, timeout_s=5.0)
    print(f"[VERIFY]   post-entry position: {pos}")
    if not pos or pos[0] != "LONG" or pos[1] != 1:
        print("[VERIFY] FAIL: NT8 did not open LONG 1 — order not executed.")
        # Dump anything stuck in incoming/
        try:
            stuck = [f for f in os.listdir(INCOMING) if f.endswith(".txt")]
            print(f"[VERIFY]   incoming/ remaining: {stuck}")
        except Exception:
            pass
        return 1

    # ── FLATTEN ─────────────────────────────────────────────────────
    print("[VERIFY] Step 2: write_oif EXIT (flatten)")
    paths = oif_writer.write_oif(
        "EXIT", qty=1, trade_id=f"verify_flat_{int(time.time())}",
        account=TEST_ACCOUNT,
    )
    print(f"[VERIFY]   wrote: {[os.path.basename(p) for p in paths]}")

    pos = wait_for(TEST_ACCOUNT, "FLAT", 0, timeout_s=5.0)
    print(f"[VERIFY]   post-exit position: {pos}")
    if not pos or pos[0] != "FLAT":
        print("[VERIFY] FAIL: did not go FLAT after EXIT.")
        return 1

    print("[VERIFY] PASS — NT8 accepted OIF, filled LONG 1, flattened clean.")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
