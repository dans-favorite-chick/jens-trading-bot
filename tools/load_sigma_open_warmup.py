"""
Phoenix Bot — Noise Area sigma_open_table warmup loader.

Reads data/sigma_open_table.json (produced by tools/warmup_sigma_open.py)
and returns a dict shaped for NoiseAreaMomentum.seed_history().

JSON stores minute_of_day keys as strings (JSON spec requires); this
loader converts them back to ints.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_sigma_open_warmup(path: str | Path = "data/sigma_open_table.json") -> dict[int, list[float]] | None:
    """
    Load pre-computed sigma_open history from disk.

    Returns:
        dict[int, list[float]] shaped for seed_history(), or None if file
        missing / malformed. Returning None lets the bot gracefully fall
        back to live-accumulation warmup.
    """
    path = Path(path)
    if not path.exists():
        logger.warning(f"No sigma_open warmup found at {path} — "
                       f"Noise Area will need 14 live sessions before firing")
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load sigma_open warmup: {e}")
        return None

    raw_history = payload.get("sigma_open_history", {})
    if not raw_history:
        logger.warning(f"sigma_open warmup at {path} contains no history entries")
        return None

    # Convert str keys → int (JSON serialization requirement)
    try:
        history = {int(k): list(v) for k, v in raw_history.items()}
    except (ValueError, TypeError) as e:
        logger.error(f"Malformed sigma_open warmup keys: {e}")
        return None

    meta = payload.get("metadata", {})
    logger.info(
        f"Loaded sigma_open warmup: {len(history)} minute-buckets, "
        f"{meta.get('total_sessions', '?')} sessions, "
        f"source={meta.get('source_file', '?')}"
    )
    return history