"""
Verification test 04 â€” which OIF field does NT8 treat as ORDER ID?

Sends three LIMIT orders with distinctive IDs placed at different field
positions. Whichever ID appears as a filename in outgoing/ reveals the
true ORDER ID field position.

PLACE template: PLACE;1:ACCT;2:INST;3:ACTION;4:QTY;5:TYPE;6:LIMIT;7:STOP;8:TIF;9:OCO;10:ID;11:STRAT;12:STRAT_ID

Tests IDs placed at positions 9, 10, 11 (user spec said 10/11/12; we also
test 9 to rule out OCO-ID field as the match). LIMIT 20000 so orders sit
Working without filling.
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

ts = int(time.time() * 1000)
# Unique distinctive IDs per position
ID_AT_9  = f"FIELD9_ID_{ts}"   # OCO ID slot per docs
ID_AT_10 = f"FIELD10_ID_{ts}"  # ORDER ID slot per docs
ID_AT_11 = f"FIELD11_ID_{ts}"  # STRATEGY slot per docs

# Three OIF lines â€” same order otherwise, different ID placement
oif_field_9  = f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;1;LIMIT;20000;0;GTC;{ID_AT_9};;;"
oif_field_10 = f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;1;LIMIT;20000;0;GTC;;{ID_AT_10};;"
oif_field_11 = f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;1;LIMIT;20000;0;GTC;;;{ID_AT_11};"

print("=" * 60)
print("Test 04 â€” ORDER ID field position verification")
print("=" * 60)
print(f"ID at field 9  (OCO ID slot):  {ID_AT_9}")
print(f"ID at field 10 (ORDER ID slot): {ID_AT_10}")
print(f"ID at field 11 (STRATEGY slot): {ID_AT_11}")
print()
print("OIFs:")
print(f"  F9:  {oif_field_9}")
print(f"  F10: {oif_field_10}")
print(f"  F11: {oif_field_11}")
print()

pre_files = {}
for fname in os.listdir(OIF_OUTGOING):
    try:
        pre_files[fname] = os.path.getmtime(os.path.join(OIF_OUTGOING, fname))
    except FileNotFoundError:
        pass

# Write all three OIFs
for i, line in enumerate([oif_field_9, oif_field_10, oif_field_11], start=1):
    path = os.path.join(OIF_INCOMING, f"oif_verify_field_{i}_{ts}.txt")
    with open(path, "w") as f:
        f.write(line + "\n")
    print(f"Wrote: {os.path.basename(path)}")
    time.sleep(0.3)  # Stagger so NT8 processes in order

print()
print("Polling 10s for outgoing activity...")
start = time.time()
seen = {}
while time.time() - start < 10:
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
        print(f"  [{elapsed:6.2f}s]  {fname}  {content!r}")
    time.sleep(0.1)

print()
print("=" * 60)
print("VERDICT â€” which ID appeared as a filename?")
print("=" * 60)
found = {"field_9": False, "field_10": False, "field_11": False}
for fname in seen:
    if ID_AT_9 in fname:
        found["field_9"] = True
    if ID_AT_10 in fname:
        found["field_10"] = True
    if ID_AT_11 in fname:
        found["field_11"] = True

for pos, yes in found.items():
    mark = "YES" if yes else "no"
    print(f"  {pos}: {mark}")

# Cleanup â€” cancel all three
print()
print("Cleanup: CANCELALLORDERS")
cancel_line = f"CANCELALLORDERS;{ACCOUNT};{INSTRUMENT};;;;;;;;;;;;"
with open(os.path.join(OIF_INCOMING, f"oif_verify_cancel_all_{ts}.txt"), "w") as f:
    f.write(cancel_line + "\n")
time.sleep(3)
print("Cancel sent; done.")
