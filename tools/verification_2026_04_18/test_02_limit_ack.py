"""
Verification test 02 — does NT8 write outgoing/<Account>_<orderId>.txt
for a LIMIT order placed far below market (won't fill)?

Tests the Submitted / Working state ACKs even when no fill occurs.
Market closed Sunday; LIMIT far off should sit Working indefinitely.
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

ORDER_ID = f"MANTEST_LMT_{int(time.time() * 1000)}"
# MNQ recent range 26172-26802 per memory. Use 20000 (well below) so order
# sits Working even if market data arrives. Buy LIMIT 20000 = buy-if-cheap.
LIMIT_PRICE = 20000.00
oif_line = f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;1;LIMIT;{LIMIT_PRICE:.2f};0;DAY;;{ORDER_ID};;"

print("=" * 60)
print("Test 02 — LIMIT order ACK verification")
print("=" * 60)
print(f"ORDER ID:   {ORDER_ID}")
print(f"LIMIT price: {LIMIT_PRICE}  (far below market — should sit Working)")
print(f"OIF ({oif_line.count(';')} semicolons):")
print(f"  {oif_line}")
print()

pre_files = {}
for fname in os.listdir(OIF_OUTGOING):
    try:
        pre_files[fname] = os.path.getmtime(os.path.join(OIF_OUTGOING, fname))
    except FileNotFoundError:
        pass
print(f"Pre-test OIF_OUTGOING: {sorted(pre_files.keys())}")
print()

oif_path = os.path.join(OIF_INCOMING, f"oif_verify_lmt_{ORDER_ID}.txt")
with open(oif_path, "w") as f:
    f.write(oif_line + "\n")
print(f"Wrote: {os.path.basename(oif_path)}")
print()

print("Polling 15s...")
start = time.time()
seen = {}
while time.time() - start < 15:
    try:
        current = os.listdir(OIF_OUTGOING)
    except Exception:
        current = []
    for fname in current:
        full = os.path.join(OIF_OUTGOING, fname)
        try:
            mtime = os.path.getmtime(full)
            size = os.path.getsize(full)
        except FileNotFoundError:
            continue
        pre_mtime = pre_files.get(fname)
        if pre_mtime is not None and mtime <= pre_mtime + 0.001:
            continue
        last = seen.get(fname, [])
        if last and abs(last[-1][0] - mtime) < 0.001 and last[-1][1] == size:
            continue
        try:
            content = open(full, "r", encoding="utf-8", errors="replace").read().strip()
        except Exception as e:
            content = f"<err: {e}>"
        seen.setdefault(fname, []).append((mtime, size, content))
        elapsed = time.time() - start
        marker = " ***" if ORDER_ID in fname else ""
        print(f"  [{elapsed:6.2f}s]  {fname}  size={size}  {content!r}{marker}")
    time.sleep(0.1)

# Now cancel the Working order and see what state transition we get
print()
print("--- Sending CANCEL ---")
cancel_line = f"CANCEL;;;;;;;;;;{ORDER_ID};;"
with open(os.path.join(OIF_INCOMING, f"oif_verify_lmt_cancel_{ORDER_ID}.txt"), "w") as f:
    f.write(cancel_line + "\n")

# Poll another 10s to catch the cancelled state
cancel_start = time.time()
while time.time() - cancel_start < 10:
    try:
        current = os.listdir(OIF_OUTGOING)
    except Exception:
        current = []
    for fname in current:
        full = os.path.join(OIF_OUTGOING, fname)
        try:
            mtime = os.path.getmtime(full)
            size = os.path.getsize(full)
        except FileNotFoundError:
            continue
        last = seen.get(fname, [])
        if last and abs(last[-1][0] - mtime) < 0.001 and last[-1][1] == size:
            continue
        try:
            content = open(full, "r", encoding="utf-8", errors="replace").read().strip()
        except Exception as e:
            content = f"<err: {e}>"
        seen.setdefault(fname, []).append((mtime, size, content))
        elapsed = time.time() - start
        marker = " ***" if ORDER_ID in fname else ""
        print(f"  [{elapsed:6.2f}s]  {fname}  size={size}  {content!r}{marker}")
    time.sleep(0.1)

print()
print("=" * 60)
print("Full state transition log for this order")
print("=" * 60)
for fname, obs in sorted(seen.items()):
    print(f"{fname}:")
    for mtime, size, content in obs:
        print(f"  @ {mtime:.3f}  {content!r}")
