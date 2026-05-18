"""
core.signal_visualizer — write JSONL events for the NT8 PhoenixTradeOverlay
============================================================================

Append-only writer of trade lifecycle events to the chart-visualizer file.
NT8's PhoenixTradeOverlay.cs polls this file every bar close and renders
color-coded entry markers + live SL/TP lines.

Event types written:
  - "signal"     : bot decided to enter (entry/stop/target known)
  - "fill"       : NT8 confirmed fill at fill_price
  - "stop_moved" : strategy moved stop (e.g., scale_out_1r BE adjustment)
  - "exit"       : trade closed; pnl + exit_reason

File location:
  C:\\Users\\Trading PC\\Documents\\NinjaTrader 8\\phoenix_signals.jsonl

The file is append-only; NT8 tracks its read offset and processes only new
lines. Truncate (manually or via cron) weekly to keep file under ~10MB.

Thread-safety: single-process Phoenix bot, single file lock. Multiple bots
(prod_bot + sim_bot + lab_bot) writing to the SAME file would conflict —
recommend separate files per bot or namespaced IDs.

Integration: bots/base_bot.py calls these functions at the appropriate
lifecycle hooks (signal emission, fill confirmation, stop adjustment, exit).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default file path — operator-configurable via env var
DEFAULT_PATH = r"C:\Users\Trading PC\Documents\NinjaTrader 8\phoenix_signals.jsonl"
SIGNAL_FILE_PATH = os.environ.get("PHOENIX_SIGNAL_VIZ_PATH", DEFAULT_PATH)

# Single global lock — single-process bot writes only
_write_lock = threading.Lock()


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_event(event: dict) -> None:
    """Append a single event line to the JSONL file. Safe to call repeatedly."""
    try:
        line = json.dumps(event, separators=(",", ":"))
        with _write_lock:
            # Ensure parent dir exists
            Path(SIGNAL_FILE_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(SIGNAL_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        # NEVER let visualization failures break the bot
        logger.warning(f"[signal_viz] write failed: {e!r}")


# ════════════════════════════════════════════════════════════════════
# Public API — called from base_bot at lifecycle events
# ════════════════════════════════════════════════════════════════════

def emit_signal(strategy: str, direction: str,
                 entry: float, stop: float, target: float,
                 trade_id: Optional[str] = None) -> str:
    """Bot decided to enter a trade. Returns the trade_id for tracking.

    Args:
        strategy: name of the strategy emitting the signal
        direction: "LONG" or "SHORT"
        entry: planned entry price (before any slippage)
        stop: stop loss price
        target: take-profit target price
        trade_id: optional explicit ID; auto-generated UUID if None

    Returns:
        trade_id string used for subsequent lifecycle events (fill/exit/etc).
    """
    tid = trade_id or uuid.uuid4().hex[:12]
    _write_event({
        "ts": _now_iso(),
        "event": "signal",
        "id": tid,
        "strategy": strategy,
        "direction": direction,
        "entry": round(float(entry), 2),
        "stop": round(float(stop), 2),
        "target": round(float(target), 2),
    })
    return tid


def emit_fill(trade_id: str, fill_price: float) -> None:
    """NT8 confirmed fill at fill_price."""
    _write_event({
        "ts": _now_iso(),
        "event": "fill",
        "id": trade_id,
        "fill_price": round(float(fill_price), 2),
    })


def emit_stop_moved(trade_id: str, new_stop: float,
                     reason: str = "manual") -> None:
    """Strategy moved the stop (scale_out_1r BE adjustment, trail, etc).

    Common reasons:
      - "scale_out_1r_BE" (moved to break-even after first scale-out)
      - "trail_atr_1x"    (trailing ATR adjustment)
      - "manual"          (operator override)
    """
    _write_event({
        "ts": _now_iso(),
        "event": "stop_moved",
        "id": trade_id,
        "new_stop": round(float(new_stop), 2),
        "reason": reason,
    })


def emit_exit(trade_id: str, exit_price: float, exit_reason: str,
               pnl: float) -> None:
    """Trade closed. exit_reason values:
      - "target_hit"
      - "stop_hit"
      - "be_stop"
      - "time_exit"
      - "manual"
      - "eod_flat"
    """
    _write_event({
        "ts": _now_iso(),
        "event": "exit",
        "id": trade_id,
        "exit_price": round(float(exit_price), 2),
        "exit_reason": exit_reason,
        "pnl": round(float(pnl), 2),
    })


# ════════════════════════════════════════════════════════════════════
# Optional housekeeping
# ════════════════════════════════════════════════════════════════════

def truncate_if_oversized(max_bytes: int = 10 * 1024 * 1024) -> None:
    """Truncate the JSONL file if it grows past max_bytes. Call from a
    weekly cron / startup hook to keep NT8 indicator load time bounded."""
    try:
        p = Path(SIGNAL_FILE_PATH)
        if p.exists() and p.stat().st_size > max_bytes:
            # Keep last 1MB worth (recent events still relevant for chart)
            keep_bytes = 1 * 1024 * 1024
            with open(p, "rb") as f:
                f.seek(-keep_bytes, os.SEEK_END)
                # Move to next newline to avoid partial line
                f.readline()
                tail = f.read()
            with open(p, "wb") as f:
                f.write(tail)
            logger.info(f"[signal_viz] truncated {SIGNAL_FILE_PATH} to {len(tail)} bytes")
    except Exception as e:
        logger.warning(f"[signal_viz] truncate failed: {e!r}")
