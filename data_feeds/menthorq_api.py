"""
Phoenix Bot — MenthorQ Direct API Client

Fetches live GEX levels directly from api.menthorq.io and writes to
C:\\temp\\menthorq_levels.json -- the same file menthorq_feed.py reads.

Replaces MQBridge.cs (NT8 indicator approach, which failed because
MenthorQ uses OnRender() not DrawObjects).

Usage:
    # One-shot fetch:
    python -m data_feeds.menthorq_api

    # Run as a background poller (fetches every 15 min):
    python -m data_feeds.menthorq_api --poll

    # Test / print current levels:
    python -m data_feeds.menthorq_api --test

Config:
    MENTHORQ_API_KEY in .env  (or set MENTHORQ_API_KEY env var)
    QQQ_TO_NQ_RATIO  in .env  (default 40.5 — adjust if levels look off)

API details (reverse-engineered from MenthorQLevelsV6.dll):
    GET https://api.menthorq.io/getDailyLevels
    Header: X-API-Key: <key>
    Params: platform=ninjatrader&ticker=QQQ&level_type=gamma_levels_intraday&user_id=1
    Response: {"ticker":"QQQ","levels":[{"level_type":...,"level_values":[{"name":"HVL","value":607},...]}]}
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import urllib.request
    import urllib.error
except ImportError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
except ImportError:
    pass

logger = logging.getLogger("MenthorQAPI")

# ── Config ────────────────────────────────────────────────────────────────────
API_BASE    = "https://api.menthorq.io"
TICKER      = "QQQ"        # Only QQQ returns data; converted to NQ prices below
PLATFORM    = "ninjatrader"
USER_ID     = "1"          # auth is via X-API-Key header; user_id is legacy param
OUTPUT_FILE = r"C:\temp\menthorq_levels.json"
POLL_INTERVAL_MIN = 10     # Match MenthorQ's intraday update cycle (~10 min)

# QQQ → NQ conversion.
# QQQ tracks the Nasdaq-100. NQ futures ≈ QQQ × ratio.
# Ratio is ~40-41 and is stable over months. Check once a week.
# Override in .env: QQQ_TO_NQ_RATIO=40.5
def _get_live_ratio() -> float | None:
    """
    Compute QQQ→NQ ratio using live Yahoo Finance prices for BOTH tickers.
    Uses NQ=F (NQ futures continuous contract) — no tick file dependency,
    no staleness, works whether NT8 is running or not.
    """
    try:
        nq, qqq = 0.0, 0.0
        for ticker, target in [("NQ=F", "nq"), ("QQQ", "qqq")]:
            req = urllib.request.Request(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                d = json.loads(r.read())
            price = d["chart"]["result"][0]["meta"].get("regularMarketPrice")
            if target == "nq":
                nq = price or 0.0
            else:
                qqq = price or 0.0

        if nq <= 0 or qqq <= 0:
            return None

        ratio = round(nq / qqq, 2)
        logger.info(f"Live ratio: NQ={nq:.2f} / QQQ={qqq:.2f} = {ratio}")
        return ratio
    except Exception as e:
        logger.debug(f"Live ratio fetch failed: {e}")
        return None


def _get_ratio() -> float:
    """
    Get QQQ→NQ conversion ratio.
    Always tries live NQ=F/QQQ from Yahoo first.
    Falls back to QQQ_TO_NQ_RATIO in .env if Yahoo is unreachable.
    """
    live = _get_live_ratio()
    if live and 35.0 < live < 50.0:   # sanity check — ratio should stay in this range
        return live
    val = os.environ.get("QQQ_TO_NQ_RATIO", "41.1")
    logger.warning(f"Using fallback ratio from .env: {val}")
    try:
        return float(val)
    except ValueError:
        return 41.1


def _get_api_key() -> str:
    key = os.environ.get("MENTHORQ_API_KEY", "")
    if not key:
        raise RuntimeError(
            "MENTHORQ_API_KEY not set. Add to .env:\n"
            "  MENTHORQ_API_KEY=your_key_here"
        )
    return key


# ── API fetch ─────────────────────────────────────────────────────────────────
def fetch_levels(level_type: str = "gamma_levels_intraday") -> dict:
    """
    Fetch QQQ gamma levels from api.menthorq.io.
    level_type options: gamma_levels_intraday (live), gamma_levels (EOD),
                        gamma_scalping_intraday, swing_levels
    Returns raw API response dict.
    """
    key = _get_api_key()
    url = (f"{API_BASE}/getDailyLevels"
           f"?platform={PLATFORM}&ticker={TICKER}"
           f"&level_type={level_type}&user_id={USER_ID}")

    req = urllib.request.Request(url, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"MenthorQ API {e.code}: {body[:200]}")
    except Exception as e:
        raise RuntimeError(f"MenthorQ API fetch error: {e}")


# ── Level name → JSON key mapping ─────────────────────────────────────────────
_NAME_MAP = {
    "Call Resistance":      "call_resistance",
    "Put Support":          "put_support",
    "HVL":                  "hvl",
    "1D Min":               "day_min",
    "1D Max":               "day_max",
    "Call Resistance 0DTE": "call_resistance_0dte",
    "Put Support 0DTE":     "put_support_0dte",
    "HVL 0DTE":             "hvl_0dte",
    "Gamma Wall 0DTE":      "gamma_wall_0dte",
    "GEX 1":  "gex_1",  "GEX 2":  "gex_2",  "GEX 3":  "gex_3",
    "GEX 4":  "gex_4",  "GEX 5":  "gex_5",  "GEX 6":  "gex_6",
    "GEX 7":  "gex_7",  "GEX 8":  "gex_8",  "GEX 9":  "gex_9",
    "GEX 10": "gex_10",
}


def _parse_levels(api_data: dict, ratio: float) -> dict:
    """
    Parse API response and convert QQQ prices → NQ prices.
    Returns dict matching the format menthorq_feed.py reads.
    """
    out = {}
    levels_list = api_data.get("levels", [])
    if not levels_list:
        return out

    # Use first entry (there's typically one per level_type)
    entry = levels_list[0]
    for lv in entry.get("level_values", []):
        name = lv.get("name", "")
        qqq_val = lv.get("value", 0.0)
        if not qqq_val:
            continue
        key = _NAME_MAP.get(name)
        if key:
            out[key] = round(qqq_val * ratio, 2)
        # GEX with numeric suffix not in static map
        elif name.startswith("GEX "):
            n = name.split()[-1]
            out[f"gex_{n}"] = round(qqq_val * ratio, 2)

    return out


# ── Build output JSON ─────────────────────────────────────────────────────────
def build_output(levels: dict, api_date: str, ratio: float, level_type: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    sb = ["{"]
    sb.append(f'  "ts": "{ts}",')
    sb.append(f'  "source": "MenthorQAPI_Python",')
    sb.append(f'  "api_date": "{api_date}",')
    sb.append(f'  "level_type": "{level_type}",')
    sb.append(f'  "qqq_to_nq_ratio": {ratio},')

    named_keys = [
        "hvl", "call_resistance", "put_support",
        "call_resistance_0dte", "put_support_0dte", "hvl_0dte", "gamma_wall_0dte",
        "day_min", "day_max",
        "gex_1", "gex_2", "gex_3", "gex_4", "gex_5",
        "gex_6", "gex_7", "gex_8", "gex_9", "gex_10",
    ]
    for k in named_keys:
        val = levels.get(k, 0.0)
        sb.append(f'  "{k}": {val},')

    sb.append('  "_note": "Prices converted from QQQ via qqq_to_nq_ratio"')
    sb.append("}")
    return "\n".join(sb)


# ── Main fetch + write ────────────────────────────────────────────────────────
def fetch_and_write(level_type: str = "gamma_levels_intraday") -> dict:
    """
    Fetch current levels, convert to NQ prices, write to OUTPUT_FILE.
    Returns the levels dict (NQ prices).
    """
    ratio = _get_ratio()

    # Try intraday first; fall back to EOD if intraday is empty
    try:
        data = fetch_levels(level_type)
        if not data.get("levels"):
            logger.warning(f"MenthorQ: {level_type} empty, trying gamma_levels (EOD)")
            data = fetch_levels("gamma_levels")
            level_type = "gamma_levels"
    except Exception as e:
        logger.error(f"MenthorQ fetch failed: {e}")
        raise

    api_date = ""
    if data.get("levels"):
        api_date = data["levels"][0].get("date", "")

    levels_nq = _parse_levels(data, ratio)
    if not levels_nq:
        logger.warning("MenthorQ: API returned data but no parseable levels")

    output_json = build_output(levels_nq, api_date, ratio, level_type)

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(output_json)

    hvl = levels_nq.get("hvl", 0)
    cr  = levels_nq.get("call_resistance", 0)
    ps  = levels_nq.get("put_support", 0)
    logger.info(f"MenthorQ levels written → HVL={hvl:.2f} CR={cr:.2f} PS={ps:.2f} "
                f"(ratio={ratio}, source={level_type}, date={api_date})")

    return levels_nq


# ── Poller ────────────────────────────────────────────────────────────────────
def run_poller():
    """Poll every POLL_INTERVAL_MIN minutes. Run this in a background thread or process."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [MenthorQAPI] %(message)s")
    logger.info(f"MenthorQ API poller started — refreshing every {POLL_INTERVAL_MIN} min")
    while True:
        try:
            fetch_and_write()
        except Exception as e:
            logger.error(f"Fetch error (will retry next cycle): {e}")
        time.sleep(POLL_INTERVAL_MIN * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [MenthorQAPI] %(message)s")

    import argparse
    parser = argparse.ArgumentParser(description="MenthorQ API client for Phoenix Bot")
    parser.add_argument("--poll", action="store_true", help="Run as continuous poller")
    parser.add_argument("--test", action="store_true", help="Fetch and print levels (no file write)")
    parser.add_argument("--eod", action="store_true", help="Use EOD levels instead of intraday")
    args = parser.parse_args()

    level_type = "gamma_levels" if args.eod else "gamma_levels_intraday"

    if args.test:
        ratio = _get_ratio()
        data = fetch_levels(level_type)
        levels = _parse_levels(data, ratio)
        print(f"\nMenthorQ levels (QQQ x{ratio} -> NQ prices):")
        for k, v in sorted(levels.items()):
            print(f"  {k:30s} = {v:.2f}")
        api_date = data["levels"][0]["date"] if data.get("levels") else "?"
        print(f"\nSource: {level_type}, date: {api_date}")
    elif args.poll:
        run_poller()
    else:
        levels = fetch_and_write(level_type)
        print(f"Written to {OUTPUT_FILE}")
        print(f"  HVL              = {levels.get('hvl', 0):.2f}")
        print(f"  Call Resistance  = {levels.get('call_resistance', 0):.2f}")
        print(f"  Put Support      = {levels.get('put_support', 0):.2f}")
        print(f"  HVL 0DTE         = {levels.get('hvl_0dte', 0):.2f}")
