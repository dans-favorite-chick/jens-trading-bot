"""
Smoke-test the NT8 OIF paths are in working order.

    python tools/smoke_test_oif_paths.py

Checks:
  1. OIF_INCOMING exists and is writable — writes a throwaway file,
     verifies it landed, deletes it.
  2. OIF_OUTGOING exists and is readable — lists the directory.

Exits 0 on full pass, 1 on any failure. Prints a one-line per-check
report plus a summary line.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Emoji output relies on UTF-8 stdout. Windows defaults to cp1252 which
# cannot encode ✅/❌ and would crash the script mid-report. Reconfigure
# if possible; silently fall through on older Python or exotic stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config.settings import OIF_INCOMING, OIF_OUTGOING

PASS = "\u2705"   # ✅
FAIL = "\u274C"   # ❌


def check_incoming_writable():
    if not os.path.isdir(OIF_INCOMING):
        return False, f"{FAIL}  OIF_INCOMING missing: {OIF_INCOMING}"

    name = f"smoke_test_{int(time.time() * 1000)}.tmp"
    path = os.path.join(OIF_INCOMING, name)
    try:
        with open(path, "w") as f:
            f.write("smoke-test\n")
        if not os.path.isfile(path):
            return False, f"{FAIL}  OIF_INCOMING write appeared to succeed but file not found: {path}"
        os.remove(path)
        return True, f"{PASS}  OIF_INCOMING writable: {OIF_INCOMING}"
    except Exception as e:
        # Best-effort cleanup
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass
        return False, f"{FAIL}  OIF_INCOMING write failed ({e}): {OIF_INCOMING}"


def check_outgoing_readable():
    if not os.path.isdir(OIF_OUTGOING):
        return False, f"{FAIL}  OIF_OUTGOING missing: {OIF_OUTGOING}"
    try:
        entries = os.listdir(OIF_OUTGOING)
        return True, f"{PASS}  OIF_OUTGOING readable: {OIF_OUTGOING} ({len(entries)} entries)"
    except Exception as e:
        return False, f"{FAIL}  OIF_OUTGOING read failed ({e}): {OIF_OUTGOING}"


def main():
    print("NT8 OIF path smoke test")
    print("=" * 48)
    checks = [check_incoming_writable(), check_outgoing_readable()]
    for _ok, line in checks:
        print(line)
    print("-" * 48)
    passed = sum(1 for ok, _ in checks if ok)
    total = len(checks)
    status = PASS if passed == total else FAIL
    print(f"{status}  {passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
