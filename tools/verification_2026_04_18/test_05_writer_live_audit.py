"""
Verification test 05 — live NT8 acceptance of OIF command formats not
covered by Phase 1 tests.

Two runtime checks remain after static audit:
  (A) CANCELALLORDERS with CORRECTED 12-semicolon format — does NT8 accept?
      (This is the B2 fix preview. If NT8 accepts, the one-line production
       fix is validated.)
  (B) PLACE STOP with GTC TIF — PLACE_STOP_SELL/PLACE_STOP_BUY are new in
      commit 6e2e325 and have never been NT8-verified. All prior tests used
      DAY; GTC on a STOP order may or may not be accepted.

Each command is sent hand-crafted (not via write_oif) so we test the bytes
NT8 sees, not the writer's call path.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config.settings import OIF_INCOMING, OIF_OUTGOING, ACCOUNT, INSTRUMENT

test_id = int(time.time() * 1000)
script_start = time.time()


def send(label, content, wait=2.0):
    path = os.path.join(OIF_INCOMING, f"oif_t05_{test_id}_{label}.txt")
    with open(path, "w") as f:
        f.write(content + "\n")
    print(f"  SENT [{label}]  ({content.count(';')} semis)  {content}")
    time.sleep(wait)


print("=" * 64)
print("Test 05 — Live NT8 acceptance of unverified OIF formats")
print("=" * 64)
print(f"test_id={test_id}  start={script_start:.3f}")
print(f"Account={ACCOUNT}  Instrument={INSTRUMENT}")
print()

# ─── (A) CANCELALLORDERS corrected format ───────────────────
print("[A] CANCELALLORDERS with corrected 12-semicolon format (B2 fix preview)")
cancel_all_fixed = "CANCELALLORDERS;;;;;;;;;;;;"
send("A_cancelall_fixed", cancel_all_fixed, wait=2.0)
print()

# ─── (B1) PLACE SELL STOP GTC ──────────────────────────────
stop_sell_id = f"T05_STPS_{test_id}"
stop_sell_oif = f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;1;STOP;0;20000;GTC;;{stop_sell_id};;"
print(f"[B1] PLACE SELL STOP GTC (stop 20000, ID={stop_sell_id})")
send("B1_stopsell_gtc", stop_sell_oif, wait=3.0)
print()

# ─── (B2) PLACE BUY STOP GTC ──────────────────────────────
stop_buy_id = f"T05_STPB_{test_id}"
stop_buy_oif = f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;1;STOP;0;40000;GTC;;{stop_buy_id};;"
print(f"[B2] PLACE BUY STOP GTC (stop 40000, ID={stop_buy_id})")
send("B2_stopbuy_gtc", stop_buy_oif, wait=3.0)
print()

# Collect ACK files created by (B1) and (B2)
time.sleep(1.5)
print("=" * 64)
print("Outgoing ACK files from (B1) and (B2)")
print("=" * 64)
found_acks = {}
for fname in sorted(os.listdir(OIF_OUTGOING)):
    if stop_sell_id in fname or stop_buy_id in fname:
        full = os.path.join(OIF_OUTGOING, fname)
        content = open(full, "r", encoding="utf-8", errors="replace").read().strip()
        mtime = os.path.getmtime(full)
        found_acks[fname] = content
        print(f"  {fname}  (mtime={mtime:.3f})  {content!r}")

if not found_acks:
    print("  (no ACK files found for (B1) or (B2) — flag as FAILED or format issue)")
print()

# Cleanup with corrected CANCELALLORDERS
print("=" * 64)
print("Cleanup")
print("=" * 64)
send("C_cancelall_cleanup", cancel_all_fixed, wait=4.0)
print()

time.sleep(2.0)
print("Final state of (B1)/(B2) ACK files:")
for fname in sorted(os.listdir(OIF_OUTGOING)):
    if stop_sell_id in fname or stop_buy_id in fname:
        full = os.path.join(OIF_OUTGOING, fname)
        content = open(full, "r", encoding="utf-8", errors="replace").read().strip()
        print(f"  {fname}  {content!r}")

print()
print(f"Script done. Elapsed: {time.time() - script_start:.1f}s")
print(f"Next: check NT8 log for 'invalid #' / 'Unknown OIF' / 'Rejected' lines since {script_start:.0f}")
