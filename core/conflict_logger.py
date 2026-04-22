"""
Phoenix Bot — Directional Conflict Logger (S6 / B70)

Non-blocking data-gathering layer: detects and logs cross-strategy
directional conflicts (e.g. bias_momentum LONG while spring_setup SHORT
on separate accounts). Conflicts are ALLOWED; this module only records
them for later analysis. Decision on block/arbitrate will be made in
2-4 weeks based on evidence collected here.

Events land in logs/conflicts/YYYY-MM-DD.jsonl, one JSON object per line.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ConflictLogger")

# Log directory — created on demand.
_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "conflicts",
)


def _log_dir() -> str:
    d = os.environ.get("PHOENIX_CONFLICT_LOG_DIR", _DEFAULT_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _today_path() -> str:
    return os.path.join(_log_dir(), f"{datetime.now().strftime('%Y-%m-%d')}.jsonl")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pos_summary(pos) -> dict[str, Any]:
    """Serialize a Position (or dict) into a stable summary dict."""
    if pos is None:
        return {}
    get = (lambda k, d=None: pos.get(k, d)) if isinstance(pos, dict) else (
        lambda k, d=None: getattr(pos, k, d))
    entry_time = get("entry_time")
    opened_at = None
    if entry_time:
        try:
            opened_at = datetime.fromtimestamp(float(entry_time), tz=timezone.utc).isoformat()
        except Exception:
            opened_at = None
    return {
        "trade_id": get("trade_id"),
        "strategy": get("strategy"),
        "sub_strategy": get("sub_strategy"),
        "direction": get("direction"),
        "entry_price": get("entry_price"),
        "account": get("account"),
        "opened_at": opened_at,
    }


def _append(event: dict) -> None:
    try:
        path = _today_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        logger.warning(f"[CONFLICT_LOG] append failed: {e}")


def log_conflict_opened(new_position, existing_conflicts: list[dict],
                        exposure_snapshot: dict) -> None:
    """Append conflict_opened event to today's jsonl.

    existing_conflicts is the list produced by
    StrategyRiskRegistry.detect_directional_conflicts(...) filtered to
    only pairs that include new_position.
    """
    if not existing_conflicts:
        return
    event = {
        "event": "conflict_opened",
        "ts": _iso_now(),
        "new_position": _pos_summary(new_position),
        "conflicts": existing_conflicts,
        "exposure": exposure_snapshot,
    }
    _append(event)
    logger.info(
        f"[CONFLICT_OPENED] {len(existing_conflicts)} pair(s) involving "
        f"{_pos_summary(new_position).get('strategy')} "
        f"net={exposure_snapshot.get('net')} gross={exposure_snapshot.get('gross')}"
    )


def log_conflict_closed(closed_position, remaining_conflicts: list[dict],
                        exposure_snapshot: dict) -> None:
    """Append conflict_closed event when one side of a conflict closes."""
    event = {
        "event": "conflict_closed",
        "ts": _iso_now(),
        "closed_position": _pos_summary(closed_position),
        "remaining_conflicts": remaining_conflicts,
        "exposure": exposure_snapshot,
    }
    _append(event)
    logger.info(
        f"[CONFLICT_CLOSED] strategy={_pos_summary(closed_position).get('strategy')} "
        f"remaining_pairs={len(remaining_conflicts)}"
    )
