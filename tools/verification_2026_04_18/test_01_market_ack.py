"""
Verification test 01 â€” does NT8 write outgoing/<orderId>.txt for a MARKET order?

Sends BUY 1 MNQ MARKET via OIF with ORDER ID at field index 10 (per NT8 docs):
  PLACE; 1:ACCT; 2:INST; 3:ACTION; 4:QTY; 5:TYPE; 6:LIMIT; 7:STOP; 8:TIF; 9:OCO; 10:ORDER_ID; 11:STRAT; 12:STRAT_ID

Watches OIF_OUTGOING for 30 seconds. Reports every new / modified file, size,
and content. Cleans up by cancelling + closing any resulting position.

Market is CLOSED Sunday; user enabled Sim101 simulated feed so MARKET fills
against paper liquidity.
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

ORDER_ID = f"MANTEST_MKT_{int(time.time() * 1000)}"

# NT8 PLACE template â€” ORDER ID at field 10
oif_line = f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;1;MARKET;0;0;GTC;;{ORDER_ID};;"

print("=" * 60)
print("Test 01 â€” MARKET order ACK verification")
print("=" * 60)
print(f"Account:    {ACCOUNT}")
print(f"Instrument: {INSTRUMENT}")
print(f"ORDER ID:   {ORDER_ID}")
print(f"OIF ({oif_line.count(';')} semicolons):")
print(f"  {oif_line}")
print()

# Snapshot pre-existing outgoing files
pre_files = {}
for fname in os.listdir(OIF_OUTGOING):
    full = os.path.join(OIF_OUTGOING, fname)
    try:
        pre_files[fname] = os.path.getmtime(full)
    except FileNotFoundError:
        pass
print(f"Pre-test OIF_OUTGOING: {len(pre_files)} files: {sorted(pre_files.keys())}")
print()

# Write the OIF
oif_name = f"oif_verify_mkt_{ORDER_ID}.txt"
oif_path = os.path.join(OIF_INCOMING, oif_name)
with open(oif_path, "w") as f:
    f.write(oif_line + "\n")
print(f"Wrote OIF to incoming: {oif_name}")
print()

# Poll outgoing for 30s
print("Polling OIF_OUTGOING (30s) â€” new/modified files only:")
start = time.time()
seen = {}  # fname -> list of (timestamp, size, content)

while time.time() - start < 30:
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
        # Consider new if: not in pre-snapshot, OR mtime is newer than pre-snapshot
        is_new_or_modified = pre_mtime is None or mtime > pre_mtime + 0.001
        if not is_new_or_modified:
            continue
        # Has content changed since last observation?
        last_obs = seen.get(fname, [])
        if last_obs and abs(last_obs[-1][0] - mtime) < 0.001 and last_obs[-1][1] == size:
            continue
        # Read content
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip()
        except Exception as e:
            content = f"<read error: {e}>"
        seen.setdefault(fname, []).append((mtime, size, content))
        elapsed = time.time() - start
        marker = " *** MATCHES ORDER ID" if ORDER_ID in fname else ""
        print(f"  [{elapsed:6.2f}s]  {fname}  size={size}  content={content!r}{marker}")
    time.sleep(0.1)

print()
print("=" * 60)
print("Summary of new/modified files")
print("=" * 60)
for fname, obs in sorted(seen.items(), key=lambda kv: kv[1][0][0]):
    marker = " *** MATCHES ORDER ID" if ORDER_ID in fname else ""
    print(f"{fname}{marker}")
    for mtime, size, content in obs:
        print(f"  @ {mtime:.3f}  size={size}  {content!r}")
if not seen:
    print("(no new files observed in 30s)")
print()

# Cleanup: cancel by ID, then CLOSEPOSITION to flatten
print("Cleanup: cancel + close position")
cancel_line = f"CANCEL;;;;;;;;;;{ORDER_ID};;"
with open(os.path.join(OIF_INCOMING, f"oif_verify_mkt_cancel_{ORDER_ID}.txt"), "w") as f:
    f.write(cancel_line + "\n")
time.sleep(1)
close_line = f"CLOSEPOSITION;{ACCOUNT};{INSTRUMENT};GTC;;;;;;;;;"
with open(os.path.join(OIF_INCOMING, f"oif_verify_mkt_close_{int(time.time() * 1000)}.txt"), "w") as f:
    f.write(close_line + "\n")
time.sleep(2)
print("Cleanup OIFs sent.")
