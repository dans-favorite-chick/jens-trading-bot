"""
Phoenix Bot — NT8 Outgoing Diagnostic

Walks NT8's outgoing/ folder and inventories every position / order file
that NT8 has written. Added 2026-04-24 to help Jennifer find the rogue
TickStreamer chart that was mixing two price streams (~27,415 real MNQ
and ~7,192 phantom contract) into the bridge. The theory: a second
chart in NT8 is attached to a different instrument (possibly an old
MNQ rollover contract) with its own TickStreamer writing to the same
OIF folder.

What this diagnostic does:
  * Lists every file in the outgoing/ folder
  * Groups by instrument prefix (MNQM6 Globex_*, MNQH6 Globex_*, etc.)
  * Flags any file whose instrument prefix is NOT the expected one
  * For position files, parses the current direction/qty/price and
    flags anything that's not FLAT;0;0 (stale positions)
  * Summarises counts, unexpected instruments, suspicious prices

Expected state for the Phoenix 16+1 account routing:
  * One `MNQM6 Globex_Sim101_position.txt` (prod bot)
  * Sixteen `MNQM6 Globex_Sim{Strategy}_position.txt` files
  * Zero files with any other instrument prefix
  * If any file with prefix other than `MNQM6 Globex_`, that's the bug.

Usage:
  python tools/check_nt8_outgoing.py
  python tools/check_nt8_outgoing.py --json    # machine-readable output
  python tools/check_nt8_outgoing.py --watch   # poll every 5s, print diffs

Exit codes:
  0 — only expected MNQM6 files found
  1 — unexpected instrument prefix detected (likely rogue chart)
  2 — positional inconsistencies (e.g. LONG stuck for >5min)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# NT8 data path — matches core/settings.py NT8_DATA_ROOT.
NT8_DATA_ROOT = Path(r"C:\Users\Trading PC\Documents\NinjaTrader 8")
OUTGOING = NT8_DATA_ROOT / "outgoing"
INCOMING = NT8_DATA_ROOT / "incoming"

EXPECTED_INSTRUMENT = "MNQM6"           # Front-month MNQ as of Apr 2026
EXPECTED_EXCHANGE = "Globex"
EXPECTED_PREFIX = f"{EXPECTED_INSTRUMENT} {EXPECTED_EXCHANGE}_"

# Expected accounts per config/account_routing.py (as of 2026-04-24).
EXPECTED_ACCOUNTS = {
    "Sim101",                       # prod bot + fallback
    "SimBias Momentum",
    "SimDom Pullback",
    "SimIb Breakout",
    "SimOrb",
    "SimNoise Area",
    "SimCompressionBreakout_15m",
    "SimCompressionBreakout_30m",
    "SimOpeningSession_breakout",
    "SimOpeningSession_fade",
    "SimOpeningSession_pullback",
    "SimOpeningSession_reversal",
    "SimOpeningSession_trend_continuation",
    "SimOpeningSession_opening_range",
    "SimVwap Band Pullback",
    "SimVWapp Pullback",            # note the typo — byte-exact from NT8
    "SimSpring Setup",
}

# Any price outside this band on MNQ is suspicious as of Apr 2026.
SANE_PRICE_LOW = 20000.0
SANE_PRICE_HIGH = 35000.0


# ═══════════════════════════════════════════════════════════════════════
# File classifiers
# ═══════════════════════════════════════════════════════════════════════

_POSITION_RE = re.compile(
    r"^(?P<direction>FLAT|LONG|SHORT);(?P<qty>-?\d+);(?P<price>-?[\d.]+)\s*$"
)

def _classify_filename(name: str) -> dict:
    """Classify NT8's outgoing/ files. Two major patterns:

      1. Position files:  `<INST> <EXCH>_<account>_position.txt`
         e.g. "MNQM6 Globex_Sim101_position.txt"
      2. Order-status files: `<account>_<order_id_hex>.txt`
         e.g. "Sim101_07abb8b7a7704a7ab4b007a00b92ce72.txt"
         (NT8 writes one per open order; live-account order_ids are
         numeric, sim order_ids are uuid hex)
      3. Connection / metadata files (Live.txt, Kinetick ... (Free).txt):
         ignored for diagnosis.
    """
    stem = name.rsplit(".", 1)[0]
    # Pattern 1: position file
    m1 = re.match(r"^([A-Z0-9]+)\s+([A-Za-z]+)_(.+?)_position$", stem)
    if m1:
        return {"kind": "position", "instrument": m1.group(1), "exchange": m1.group(2),
                "account": m1.group(3), "suffix": "position", "raw": name}
    # Pattern 2: order-status file (account_hexid)
    m2 = re.match(r"^([A-Za-z0-9][A-Za-z0-9 _]*?)_([0-9a-f]{6,})$", stem)
    if m2:
        return {"kind": "order", "instrument": None, "exchange": None,
                "account": m2.group(1), "suffix": f"order_{m2.group(2)[:8]}", "raw": name}
    # Pattern 3: metadata / connection files
    return {"kind": "meta", "instrument": None, "exchange": None,
            "account": None, "suffix": None, "raw": name}


def _parse_position_content(content: str) -> dict | None:
    content = (content or "").strip()
    m = _POSITION_RE.match(content)
    if not m:
        return None
    return {
        "direction": m.group("direction"),
        "qty": int(m.group("qty")),
        "price": float(m.group("price")),
    }


# ═══════════════════════════════════════════════════════════════════════
# Inventory
# ═══════════════════════════════════════════════════════════════════════

def inventory(outgoing: Path) -> dict:
    """Walk outgoing/ once and produce a structured snapshot."""
    snapshot: dict = {
        "path": str(outgoing),
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "exists": outgoing.exists(),
        "files": [],
        "by_instrument": defaultdict(int),
        "by_account": defaultdict(list),
        "unexpected_instruments": [],
        "unknown_accounts": [],
        "stale_positions": [],
        "price_outliers": [],
        "suspicious_prices": [],
    }

    if not outgoing.exists():
        return dict(snapshot)  # convert defaultdicts to plain dicts at return

    now_epoch = time.time()

    for p in sorted(outgoing.iterdir()):
        if not p.is_file():
            continue
        info = _classify_filename(p.name)
        try:
            st = p.stat()
            age_s = now_epoch - st.st_mtime
            size_b = st.st_size
            content = p.read_text(encoding="utf-8", errors="ignore")[:500]
        except OSError as e:
            content = f"(read error: {e})"
            age_s = None
            size_b = None

        pos = _parse_position_content(content) if info.get("kind") == "position" else None

        entry = {
            "name": p.name,
            "instrument": info["instrument"],
            "exchange": info["exchange"],
            "account": info["account"],
            "suffix": info["suffix"],
            "size_bytes": size_b,
            "age_s": round(age_s, 1) if age_s is not None else None,
            "content_head": content.strip()[:120],
            "parsed_position": pos,
        }
        snapshot["files"].append(entry)

        inst_key = info["instrument"] or "(unparsable)"
        snapshot["by_instrument"][inst_key] += 1

        if info["account"]:
            snapshot["by_account"][info["account"]].append(p.name)

        # Unexpected instrument
        if info["instrument"] and info["instrument"] != EXPECTED_INSTRUMENT:
            snapshot["unexpected_instruments"].append({
                "name": p.name,
                "found_instrument": info["instrument"],
                "expected": EXPECTED_INSTRUMENT,
                "age_s": entry["age_s"],
            })

        # Unknown account
        if info["account"] and info["account"] not in EXPECTED_ACCOUNTS:
            snapshot["unknown_accounts"].append({
                "name": p.name,
                "account": info["account"],
            })

        # Stale non-FLAT positions (possible phantom left over from a crash)
        if pos and pos["direction"] != "FLAT" and age_s and age_s > 5 * 60:
            snapshot["stale_positions"].append({
                "name": p.name,
                "account": info["account"],
                "direction": pos["direction"],
                "qty": pos["qty"],
                "price": pos["price"],
                "age_s": round(age_s, 1),
            })

        # Suspicious price outside the sane band (this is the phantom-price detector!)
        if pos and pos["direction"] != "FLAT":
            if pos["price"] < SANE_PRICE_LOW or pos["price"] > SANE_PRICE_HIGH:
                snapshot["price_outliers"].append({
                    "name": p.name,
                    "account": info["account"],
                    "direction": pos["direction"],
                    "price": pos["price"],
                    "expected_band": [SANE_PRICE_LOW, SANE_PRICE_HIGH],
                })

    # Check for accounts we EXPECT but didn't see
    seen_accounts = {e["account"] for e in snapshot["files"] if e["account"]}
    snapshot["missing_expected_accounts"] = sorted(EXPECTED_ACCOUNTS - seen_accounts)

    # convert defaultdicts to plain dicts for JSON-friendliness
    snapshot["by_instrument"] = dict(snapshot["by_instrument"])
    snapshot["by_account"] = dict(snapshot["by_account"])
    return snapshot


# ═══════════════════════════════════════════════════════════════════════
# Pretty printing
# ═══════════════════════════════════════════════════════════════════════

def _print_report(snap: dict) -> int:
    """Human-readable report. Returns process exit code."""
    print("=" * 72)
    print(" Phoenix NT8 Outgoing Diagnostic")
    print(f" Path     : {snap['path']}")
    print(f" Scanned  : {snap['scanned_at']}")
    print(f" File count: {len(snap['files'])}")
    print("=" * 72)

    if not snap["exists"]:
        print("\n⚠  OUTGOING path does not exist. NT8 is either not running or")
        print(f"    NT8_DATA_ROOT is wrong. Expected: {snap['path']}")
        return 1

    # 1. Instrument breakdown — the key check for "wrong instrument" bug
    print("\n── Instruments written to outgoing/ ──")
    for inst, n in sorted(snap["by_instrument"].items(), key=lambda kv: -kv[1]):
        marker = "✓" if inst == EXPECTED_INSTRUMENT else "⚠"
        print(f"  {marker} {inst:<20} {n:>4} file(s)")

    # 2. Unexpected instruments (ROOT CAUSE INDICATOR)
    if snap["unexpected_instruments"]:
        print(f"\n🚨 UNEXPECTED INSTRUMENT(S) — this is the smoking gun!")
        print(f"   Expected only {EXPECTED_INSTRUMENT!r}, but found:")
        for u in snap["unexpected_instruments"][:20]:
            print(f"     - {u['name']}  (age {u['age_s']}s)")
        print(f"\n   Action: find the NT8 chart using {u['found_instrument']!r} and")
        print(f"   remove TickStreamer from that chart (or switch the chart to MNQM6).")
    else:
        print(f"\n✓ Only {EXPECTED_INSTRUMENT!r} present — NT8 charts are clean on instrument.")

    # 3. Price outliers (the 7k-vs-27k bug)
    if snap["price_outliers"]:
        print(f"\n🚨 POSITION PRICE OUTLIERS (outside {SANE_PRICE_LOW:.0f}-{SANE_PRICE_HIGH:.0f}):")
        for o in snap["price_outliers"]:
            print(f"     - {o['name']}: {o['direction']} @ {o['price']} (account {o['account']})")
    else:
        print("✓ No position-price outliers (all filled prices within sane MNQ band).")

    # 4. Stale non-FLAT positions
    if snap["stale_positions"]:
        print("\n⚠  STALE NON-FLAT POSITIONS (>5 min old — possible phantom from a crash):")
        for s in snap["stale_positions"]:
            print(f"     - {s['account']}: {s['direction']} qty={s['qty']} @ {s['price']} (age {s['age_s']}s)")

    # 5. Accounts seen vs expected
    print(f"\n── Account coverage ({len(snap['by_account'])} seen / {len(EXPECTED_ACCOUNTS)} expected) ──")
    for acct in sorted(snap["by_account"].keys()):
        n = len(snap["by_account"][acct])
        mark = "✓" if acct in EXPECTED_ACCOUNTS else "⚠"
        print(f"  {mark} {acct:<40} ({n} file(s))")

    if snap["unknown_accounts"]:
        print("\n⚠  UNKNOWN ACCOUNTS (not in the expected set):")
        for u in snap["unknown_accounts"][:10]:
            print(f"     - {u['account']}  from file {u['name']}")

    if snap["missing_expected_accounts"]:
        print("\nℹ  Expected accounts with NO files yet (never traded):")
        for a in snap["missing_expected_accounts"]:
            print(f"     - {a}")

    # 6. Current position states
    print("\n── Current positions ──")
    pos_files = [f for f in snap["files"] if f.get("parsed_position")]
    if not pos_files:
        print("  (no parseable position files)")
    else:
        for f in pos_files:
            p = f["parsed_position"]
            age = f["age_s"]
            status = "FLAT" if p["direction"] == "FLAT" else f"{p['direction']} {p['qty']}@{p['price']}"
            print(f"  {f['account']:<40} {status:<30} (age {age}s)")

    # Exit code
    exit_code = 0
    if snap["unexpected_instruments"]:
        exit_code = 1
    if snap["price_outliers"] or snap["stale_positions"]:
        exit_code = max(exit_code, 2)
    return exit_code


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def _list_bridge_clients() -> dict:
    """Query bridge /health and surface the NT8 client connections.

    Added 2026-04-25 for the multi-stream re-diagnostic. The bridge
    publishes a `connection_events` list — every "NT8 client connected
    from (host, port)" tells us a TickStreamer instance opened a socket.
    Yesterday we saw 3 such events (ports 55117, 55116, 55779) all
    claiming MNQM6 — confirming the multi-stream hypothesis.
    """
    import urllib.request as _ur
    out = {"bridge_url": "http://127.0.0.1:8767/health", "ok": False, "clients": [],
           "current_bots": [], "nt8_status": None, "instrument": None,
           "tick_rate_10s": None, "raw_events_tail": []}
    try:
        with _ur.urlopen(out["bridge_url"], timeout=3) as r:
            data = json.loads(r.read())
        out["ok"] = True
        out["current_bots"] = data.get("bots_connected", [])
        out["nt8_status"] = data.get("nt8_status")
        out["instrument"] = data.get("nt8_instrument")
        out["tick_rate_10s"] = data.get("tick_rate_10s")
        # Walk connection_events to identify currently-connected NT8 clients.
        # Pairs of "NT8 client connected from ..." (no later "NT8 disconnected
        # from ..." for that port) are still active.
        events = data.get("connection_events", [])
        out["raw_events_tail"] = events[-10:]
        active = {}     # port -> {ts, instrument}
        for e in events:
            msg = e.get("message", "")
            ts = e.get("ts", "")
            m = re.search(r"NT8 client connected from \('[^']+',\s*(\d+)\)", msg)
            if m:
                port = int(m.group(1))
                active[port] = {"ts_connected": ts, "instrument": None}
                continue
            m = re.search(r"NT8 disconnected from \('[^']+',\s*(\d+)\)", msg)
            if m:
                port = int(m.group(1))
                active.pop(port, None)
                continue
            m = re.search(r"NT8 instrument:\s*(\S+)", msg)
            if m and active:
                last_port = max(active.keys())
                active[last_port]["instrument"] = m.group(1)
        out["clients"] = [
            {"port": p, **info} for p, info in sorted(active.items())
        ]
    except Exception as e:
        out["error"] = repr(e)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON and exit")
    parser.add_argument("--watch", action="store_true",
                        help="Poll every 5s forever; print diffs as they appear")
    parser.add_argument("--duration", type=int, default=None,
                        help="With --watch, stop after N seconds instead of running forever")
    parser.add_argument("--list-clients", action="store_true",
                        help="Query bridge /health and list active NT8 client sockets")
    parser.add_argument("--path", type=str, default=str(OUTGOING),
                        help=f"Override NT8 outgoing path (default: {OUTGOING})")
    args = parser.parse_args()

    path = Path(args.path)

    if args.list_clients:
        result = _list_bridge_clients()
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Bridge: {result['bridge_url']}  ok={result['ok']}")
            print(f"NT8 status: {result['nt8_status']}  instrument: {result['instrument']}  "
                  f"tick_rate_10s: {result['tick_rate_10s']}")
            print(f"Bots connected: {result['current_bots']}")
            n = len(result["clients"])
            print(f"\nActive NT8 client connections: {n}")
            if n >= 2:
                print(f"  ⚠  MULTI-STREAM ({n} TickStreamer instances feeding the bridge)")
            for c in result["clients"]:
                print(f"  port={c['port']:<6}  ts={c['ts_connected']}  instrument={c.get('instrument','?')}")
            if "error" in result:
                print(f"\nERROR: {result['error']}")
        return 0 if (result["ok"] and len(result["clients"]) <= 1) else 1

    if args.watch:
        last_sig = None
        deadline = time.time() + args.duration if args.duration else None
        print(f"Watching {path} (Ctrl-C to stop"
              + (f", or auto-stop in {args.duration}s" if args.duration else "")
              + ")...")
        try:
            while True:
                if deadline and time.time() >= deadline:
                    print(f"\nDuration {args.duration}s elapsed — stopping.")
                    break
                snap = inventory(path)
                sig = tuple(
                    (f["name"], f["size_bytes"], f["age_s"] or 0) for f in snap["files"]
                )
                if sig != last_sig:
                    print(f"\n── {datetime.now().strftime('%H:%M:%S')} — change detected ──")
                    _print_report(snap)
                    last_sig = sig
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        return 0

    snap = inventory(path)
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
        return 0
    return _print_report(snap)


if __name__ == "__main__":
    sys.exit(main())
