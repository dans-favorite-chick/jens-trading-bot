"""
Phoenix Bot — OIF (Order Instruction File) Writer

Writes trade files to NT8's incoming folder. NT8 monitors this folder
and executes orders immediately. This is the proven reliable trade path.

OIF format: PLACE;Account;Instrument;Action;Qty;OrderType;LimitPrice;StopPrice;TIF;;;;
"""

import os
import glob
import logging
import time

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import OIF_INCOMING, OIF_OUTGOING, ACCOUNT, INSTRUMENT

logger = logging.getLogger("OIF")

import uuid as _uuid
_oif_counter = int(time.time() * 1000) % 1000000  # Start from timestamp to avoid restart collisions


def write_oif(action: str, qty: int = 1, stop_price: float = None,
              target_price: float = None, trade_id: str = "",
              order_type: str = "MARKET", limit_price: float = 0.0) -> list[str]:
    """
    Write OIF file(s) to NT8 incoming folder.

    Args:
        action: ENTER_LONG, ENTER_SHORT, EXIT, CANCEL_ALL
        qty: Number of contracts (default 1)
        stop_price: Optional stop loss price (Phase 2: OCO brackets)
        target_price: Optional profit target price (Phase 2: OCO brackets)
        trade_id: Unique trade ID for correlation
        order_type: "MARKET" (default) or "LIMIT" — LIMIT fills at limit_price or better
        limit_price: Required when order_type="LIMIT". Price to limit entry at.

    Returns:
        List of file paths written
    """
    global _oif_counter
    action = action.upper().strip()
    qty = int(qty)
    # Don't silently coerce 0 → 1. Caller must validate qty >= 1 for entries.
    # For CANCEL_ALL and EXIT, qty doesn't matter.
    if qty < 1 and action not in ("CANCEL_ALL", "CANCELALLORDERS", "EXIT",
                                   "EXIT_ALL", "CLOSE", "CLOSEPOSITION"):
        logger.error(f"[OIF:{trade_id or 'N/A'}] Refusing to write entry with qty={qty}")
        return []
    written = []

    cmds = []

    if action in ("ENTER_LONG", "BUY"):
        # OIF format: PLACE;Acct;Inst;Action;Qty;Type;LimitPrice;StopPrice;TIF
        if order_type == "LIMIT" and limit_price > 0:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};LIMIT;{limit_price:.2f};0;DAY;;;;\n")
            logger.info(f"[OIF:{trade_id or 'N/A'}] LIMIT ENTRY LONG @ {limit_price:.2f}")
        else:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};MARKET;0;0;DAY;;;;\n")
        # OCO bracket orders: stop + target protect position in NT8
        if stop_price and target_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};STOP;0;{stop_price:.2f};DAY;;;;\n")
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};LIMIT;{target_price:.2f};0;DAY;;;;\n")

    elif action in ("ENTER_SHORT", "SELL"):
        if order_type == "LIMIT" and limit_price > 0:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};LIMIT;{limit_price:.2f};0;DAY;;;;\n")
            logger.info(f"[OIF:{trade_id or 'N/A'}] LIMIT ENTRY SHORT @ {limit_price:.2f}")
        else:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};MARKET;0;0;DAY;;;;\n")
        if stop_price and target_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};STOP;0;{stop_price:.2f};DAY;;;;\n")
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};LIMIT;{target_price:.2f};0;DAY;;;;\n")

    elif action in ("EXIT", "EXIT_ALL", "CLOSE", "CLOSEPOSITION"):
        cmds.append(f"CLOSEPOSITION;{ACCOUNT};{INSTRUMENT};DAY;;;;;;;;;\n")

    elif action == "PARTIAL_EXIT_LONG":
        # Sell N contracts at market (partial close of a LONG position)
        cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};MARKET;0;0;DAY;;;;\n")

    elif action == "PARTIAL_EXIT_SHORT":
        # Buy N contracts at market (partial close of a SHORT position)
        cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};MARKET;0;0;DAY;;;;\n")

    elif action == "PLACE_STOP_SELL":
        # Standalone stop-loss for a LONG position (sell stop below market)
        if stop_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};STOP;0;{stop_price:.2f};GTC;;;;\n")

    elif action == "PLACE_STOP_BUY":
        # Standalone stop-loss for a SHORT position (buy stop above market)
        if stop_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};STOP;0;{stop_price:.2f};GTC;;;;\n")

    elif action == "CANCEL_ALL":
        cmds.append(f"CANCELALLORDERS;{ACCOUNT};{INSTRUMENT};;;;;;;;;;;;\n")

    else:
        logger.warning(f"Unknown OIF action: {action}")
        return written

    os.makedirs(OIF_INCOMING, exist_ok=True)

    for cmd in cmds:
        _oif_counter += 1
        # Include trade_id in filename for fill correlation
        tag = f"_{trade_id}" if trade_id else ""
        filepath = os.path.join(OIF_INCOMING, f"oif{_oif_counter}{tag}.txt")
        try:
            with open(filepath, "w") as f:
                f.write(cmd)
            written.append(filepath)
            logger.info(f"[OIF:{trade_id or 'N/A'}] {filepath} -> {cmd.strip()}")
        except Exception as e:
            logger.error(f"[OIF FAILED] {filepath}: {e}")

    return written


def write_partial_exit(direction: str, n_contracts: int = 1,
                       trade_id: str = "") -> list[str]:
    """
    Exit N contracts at market (partial close). Leaves remaining contracts open.

    Args:
        direction: "LONG" (sell N) or "SHORT" (buy N)
        n_contracts: Contracts to exit
        trade_id: For logging correlation

    Returns list of file paths written.
    """
    action = "PARTIAL_EXIT_LONG" if direction.upper() == "LONG" else "PARTIAL_EXIT_SHORT"
    paths = write_oif(action, qty=n_contracts, trade_id=trade_id)
    logger.info(f"[OIF:PARTIAL_EXIT:{trade_id}] {direction} close {n_contracts}x -> {paths}")
    return paths


def write_be_stop(direction: str, stop_price: float, n_contracts: int = 1,
                  trade_id: str = "") -> list[str]:
    """
    Place a standalone stop order at break-even price for remaining contracts.

    Args:
        direction: "LONG" (places sell stop) or "SHORT" (places buy stop)
        stop_price: Price for the stop order
        n_contracts: Remaining contracts to protect
        trade_id: For logging

    Returns list of file paths written.
    """
    action = "PLACE_STOP_SELL" if direction.upper() == "LONG" else "PLACE_STOP_BUY"
    paths = write_oif(action, qty=n_contracts, stop_price=stop_price, trade_id=trade_id)
    logger.info(f"[OIF:BE_STOP:{trade_id}] {direction} stop @ {stop_price:.2f} -> {paths}")
    return paths


def check_fills(since_time: float = 0) -> list[dict]:
    """
    Read fill confirmations from NT8's outgoing folder.

    Args:
        since_time: Only return fills newer than this timestamp

    Returns list of dicts with file, content, mtime
    """
    fills = []
    try:
        files = glob.glob(os.path.join(OIF_OUTGOING, "*.txt"))
        for f in sorted(files, key=os.path.getmtime, reverse=True)[:10]:
            try:
                mtime = os.path.getmtime(f)
                if since_time and mtime < since_time:
                    continue
                content = open(f).read().strip()
                fills.append({
                    "file": os.path.basename(f),
                    "content": content,
                    "mtime": mtime,
                })
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Could not read outgoing folder: {e}")
    return fills


def check_latest_fill(since_time: float = 0) -> str | None:
    """Return the content of the most recent fill file, or None."""
    fills = check_fills(since_time)
    return fills[0]["content"] if fills else None


async def wait_for_fill(trade_id: str, timeout_s: float = 5.0,
                        poll_interval: float = 0.3) -> dict:
    """
    Wait for fill confirmation with retry. Non-blocking async.

    Returns:
        {"status": "FILLED", "content": "...", "latency_ms": N}
        {"status": "TIMEOUT", "content": None}
        {"status": "REJECTED", "content": "..."}
    """
    import asyncio
    start = time.time()
    check_start = start  # Only look at fills after we sent the order

    while (time.time() - start) < timeout_s:
        fills = check_fills(since_time=check_start - 1)  # 1s buffer
        for fill in fills:
            content = fill["content"].upper()
            filename = fill.get("file", "")

            # Correlate: only match fills for THIS trade_id if possible
            if trade_id and trade_id in filename:
                # Exact match on trade_id in filename
                pass  # proceed to check content
            elif trade_id and trade_id not in filename:
                # Check if this is a NEW file (within our time window) as fallback
                if fill.get("mtime", 0) < check_start:
                    continue  # Skip old fills from previous trades

            if "REJECT" in content or "ERROR" in content:
                logger.error(f"[OIF:{trade_id}] ORDER REJECTED: {fill['content']}")
                return {"status": "REJECTED", "content": fill["content"],
                        "latency_ms": (time.time() - start) * 1000}
            if "FILLED" in content:
                latency = (time.time() - start) * 1000
                logger.info(f"[OIF:{trade_id}] Fill confirmed in {latency:.0f}ms: {fill['content']}")
                return {"status": "FILLED", "content": fill["content"],
                        "latency_ms": latency}
        await asyncio.sleep(poll_interval)

    logger.warning(f"[OIF:{trade_id}] No fill confirmation after {timeout_s}s")
    return {"status": "TIMEOUT", "content": None,
            "latency_ms": (time.time() - start) * 1000}


# ── CLI test mode ───────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="OIF Writer — test mode")
    parser.add_argument("--test", action="store_true", help="Write a test CLOSEPOSITION file")
    parser.add_argument("--action", default="EXIT", help="Action: ENTER_LONG, ENTER_SHORT, EXIT, CANCEL_ALL")
    parser.add_argument("--qty", type=int, default=1)
    args = parser.parse_args()

    if args.test:
        print(f"Writing test OIF to: {OIF_INCOMING}")
        paths = write_oif("CLOSEPOSITION", 1)
        print(f"Wrote: {paths}")
        time.sleep(1.5)
        fill = check_latest_fill()
        print(f"Latest fill: {fill}")
    else:
        paths = write_oif(args.action, args.qty)
        print(f"Wrote: {paths}")
