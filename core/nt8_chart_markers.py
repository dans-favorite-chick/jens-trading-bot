"""Phoenix → NT8 chart-marker bridge.

Writes one JSONL line per trade lifecycle event to
``<NT8_DATA_ROOT>/incoming/phoenix_markers.jsonl``. The NT8
PhoenixTradeMarkers indicator (separate from TickStreamer.cs!) tails
the file and draws:
  - entry  → strategy-colored triangle at the bar of ``ts``
  - exit   → small ``×`` at the exit bar
  - stop / target updates → dashed lines (red / green) that move as
    they are revised, removed when the trade closes

Schema (every line is a complete JSON object):
  {"event": "entry"|"exit"|"stop_update"|"target_update",
   "trade_id": "<id>",
   "strategy": "<name>",          # entry only
   "direction": "LONG"|"SHORT",   # entry only
   "entry_price": float,          # entry only
   "stop": float,                 # entry, stop_update
   "target": float,               # entry, target_update
   "exit_price": float,           # exit only
   "exit_reason": str,            # exit only
   "pnl": float,                  # exit only
   "color": "#rrggbb",            # entry only — matches dashboard legend
   "symbol": str,                 # entry only — triangleUp / circle / etc.
   "ts": float}                    # unix epoch seconds (UTC)

Design constraints:
  - Atomic append: each write opens, writes one line + newline, closes.
    Multiple bots writing concurrently never interleave.
  - Fail-soft: any I/O error logs WARN once per error-key and returns;
    the trade-execution path NEVER crashes because chart-overlay write
    failed.
  - NT8_DATA_ROOT path resolved lazily so a config edit + Python restart
    is enough — no NT8 restart needed for the path itself.
  - This module is independent of the existing core.signal_visualizer
    (PhoenixTradeOverlay indicator). The two write to different files
    and can coexist.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("ChartMarkers")

# Per-process singleton so concurrent writers share one lock.
_INSTANCE: Optional["ChartMarkerWriter"] = None
_INSTANCE_LOCK = threading.Lock()

# Dedupe warning spam: once a target file is unwritable, warn once and
# stop until the path changes.
_WARN_EMITTED: set[str] = set()


def get_chart_markers() -> "ChartMarkerWriter":
    """Get the process-wide ChartMarkerWriter."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = ChartMarkerWriter()
    return _INSTANCE


def _resolve_default_path() -> str:
    """Resolve the canonical phoenix_markers.jsonl path lazily."""
    try:
        from config.settings import NT8_DATA_ROOT
        return os.path.join(NT8_DATA_ROOT, "incoming", "phoenix_markers.jsonl")
    except Exception:
        return os.path.join(
            r"C:\Users\Trading PC\Documents\NinjaTrader 8",
            "incoming", "phoenix_markers.jsonl",
        )


def _strategy_visual(strategy: str) -> dict:
    try:
        from config.strategy_visuals import get_visual
        return get_visual(strategy)
    except Exception:
        return {"color": "#8b949e", "symbol": "circle"}


class ChartMarkerWriter:
    """Thread-safe atomic JSONL writer for chart markers.

    Each ``record_*`` method appends ONE line. The methods are no-throw
    contracts: I/O exceptions are caught, logged once per (path, event),
    and swallowed. Callers (trade entry / exit paths) are guaranteed to
    proceed regardless of NT8 disk state.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _resolve_default_path()
        self._lock = threading.Lock()
        # Ensure dir exists once at construction; if NT8_DATA_ROOT is
        # missing we log + continue — record_*() will retry on each call.
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError as e:
            logger.warning(
                f"[markers] mkdir failed for {self.path}: {e!r} — will retry on write"
            )

    # ── public API ─────────────────────────────────────────────────
    def record_entry(self, trade_id: str, strategy: str, direction: str,
                     entry_price: float, stop: float, target: Optional[float],
                     ts: Optional[float] = None) -> None:
        vis = _strategy_visual(strategy)
        self._append({
            "event": "entry",
            "trade_id": str(trade_id),
            "strategy": str(strategy),
            "direction": str(direction).upper(),
            "entry_price": float(entry_price),
            "stop": float(stop) if stop is not None else None,
            "target": float(target) if target is not None else None,
            "color": vis.get("color", "#8b949e"),
            "symbol": vis.get("symbol", "circle"),
            "ts": float(ts) if ts is not None else time.time(),
        })

    def record_exit(self, trade_id: str, exit_price: float,
                    exit_reason: str, pnl: float,
                    ts: Optional[float] = None) -> None:
        self._append({
            "event": "exit",
            "trade_id": str(trade_id),
            "exit_price": float(exit_price) if exit_price is not None else None,
            "exit_reason": str(exit_reason),
            "pnl": float(pnl) if pnl is not None else 0.0,
            "ts": float(ts) if ts is not None else time.time(),
        })

    def record_stop_update(self, trade_id: str, stop: float,
                           ts: Optional[float] = None) -> None:
        self._append({
            "event": "stop_update",
            "trade_id": str(trade_id),
            "stop": float(stop),
            "ts": float(ts) if ts is not None else time.time(),
        })

    def record_target_update(self, trade_id: str, target: float,
                             ts: Optional[float] = None) -> None:
        self._append({
            "event": "target_update",
            "trade_id": str(trade_id),
            "target": float(target),
            "ts": float(ts) if ts is not None else time.time(),
        })

    # ── internal ───────────────────────────────────────────────────
    def _append(self, payload: dict) -> None:
        line = json.dumps(payload, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            try:
                # Re-ensure dir each call — cheap, and survives a stale
                # delete of incoming/ between events.
                d = os.path.dirname(self.path)
                if d and not os.path.isdir(d):
                    os.makedirs(d, exist_ok=True)
                # Append-mode write. ASCII-safe; NT8 reads UTF-8 fine.
                with open(self.path, "a", encoding="utf-8", buffering=1) as f:
                    f.write(line)
            except Exception as e:  # noqa: BLE001
                key = f"{self.path}:{type(e).__name__}"
                if key not in _WARN_EMITTED:
                    _WARN_EMITTED.add(key)
                    logger.warning(
                        f"[markers] write failed (suppressed until path changes): "
                        f"path={self.path!r} err={e!r}"
                    )

    # ── test hook: reset warn dedupe between tests ─────────────────
    @staticmethod
    def _reset_warn_cache() -> None:
        _WARN_EMITTED.clear()


__all__ = ["ChartMarkerWriter", "get_chart_markers"]
