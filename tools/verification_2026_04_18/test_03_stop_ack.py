"""
Verification test 03 â€” STOP MARKET order ACK.

Places BUY STOPMARKET at 30000 (far above market). Should sit Working
until cancelled.
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

ORDER_ID = f"MANTEST_STP_{int(time.time() * 1000)}"
STOP_PRICE = 30000.00
oif_line = f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;1;STOPMARKET;0;{STOP_PRICE:.2f};GTC;;{ORDER_ID};;"

print("=" * 60)
print("Test 03 â€” STOPMARKET order ACK verification")
print("=" * 60)
print(f"ORDER ID:   {ORDER_ID}")
print(f"STOP price: {STOP_PRICE}  (far above market â€” should sit Working)")
print(f"OIF ({oif_line.count(';')} semicolons):")
print(f"  {oif_line}")
print()

pre_files = {}
for fname in os.listdir(OIF_OUTGOING):
    try:
        pre_files[fname] = os.path.getmtime(os.path.join(OIF_OUTGOING, fname))
    except FileNotFoundError:
        pass

oif_path = os.path.join(OIF_INCOMING, f"oif_verify_stp_{ORDER_ID}.txt")
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

print()
print("--- Sending CANCEL ---")
cancel_line = f"CANCEL;;;;;;;;;;{ORDER_ID};;"
with open(os.path.join(OIF_INCOMING, f"oif_verify_stp_cancel_{ORDER_ID}.txt"), "w") as f:
    f.write(cancel_line + "\n")

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
print("Full state transition log")
print("=" * 60)
for fname, obs in sorted(seen.items()):
    print(f"{fname}:")
    for mtime, size, content in obs:
        print(f"  @ {mtime:.3f}  {content!r}")
