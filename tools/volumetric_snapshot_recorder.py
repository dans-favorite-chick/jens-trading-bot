"""
Volumetric snapshot recorder — historical footprint data collection
====================================================================

The free path to historical footprint/order-flow data: poll Phoenix's
LIVE `data/volumetric_latest.json` every N minutes and append snapshots
to a daily JSONL file.

After 3-6 months of recording, this builds a meaningful historical
order-flow dataset that can be used to:
  - Backtest `footprint_cvd_reversal` and other order-flow strategies
  - Validate the "footprint as confluence" hypothesis (see Phase 13 Section R)
  - Train a CVD/delta-aware regime classifier

Storage layout:
  data/historical/volumetric/YYYY-MM-DD.jsonl
    └── one JSON object per line, each = a snapshot at capture time
  data/historical/volumetric/_recorder.log
    └── recorder activity (successful captures, dedup skips, errors)

Run mode:
  Single-shot: `python tools/volumetric_snapshot_recorder.py`
    Reads latest snapshot, appends if new (dedup by inner "ts" field),
    exits. Intended to be invoked by Windows Scheduled Task every 10 min.

  Loop mode:   `python tools/volumetric_snapshot_recorder.py --loop 600`
    Runs forever, sleeping 600 seconds between captures. Useful for
    testing or if you don't want to use Task Scheduler.

Setup (Windows Scheduled Task — one-time):

  schtasks /create /tn "PhoenixVolumetricRecorder" /tr ^
    "python C:\\Trading Project\\phoenix_bot\\tools\\volumetric_snapshot_recorder.py" ^
    /sc minute /mo 10 /ru "Trading PC"

  - tn = task name
  - tr = command to run
  - sc = schedule type (minute)
  - mo = modifier (every 10 minutes)
  - ru = user to run as

  To verify: `schtasks /query /tn "PhoenixVolumetricRecorder"`
  To remove: `schtasks /delete /tn "PhoenixVolumetricRecorder" /f`

Dedup logic:
  Each snapshot has its own "ts" field set by TickStreamer at capture.
  If the latest file's ts matches the most-recently-recorded snapshot,
  skip — TickStreamer hasn't updated since last poll.

Failure modes (handled):
  - volumetric_latest.json doesn't exist (TickStreamer not running) → log + skip
  - JSON parse error (file mid-write) → log + skip
  - Disk full / permission error → log + skip
  - Never raises; always exits 0 so Task Scheduler doesn't mark task as failed
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = ROOT / "data" / "volumetric_latest.json"
OUTPUT_DIR = ROOT / "data" / "historical" / "volumetric"
LOG_PATH = OUTPUT_DIR / "_recorder.log"


def _setup_logging() -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vol_recorder")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(sh)
    return logger


def _read_latest_snapshot(logger: logging.Logger) -> Optional[dict]:
    """Read volumetric_latest.json; return parsed dict or None on failure."""
    if not SOURCE_PATH.exists():
        logger.warning(f"source missing: {SOURCE_PATH} — TickStreamer may not be running")
        return None
    try:
        with open(SOURCE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            logger.warning("source is empty (mid-write?)")
            return None
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse failed (mid-write?): {e}")
        return None
    except Exception as e:
        logger.warning(f"read failed: {e!r}")
        return None


def _last_recorded_ts(today_file: Path) -> Optional[str]:
    """Return the inner 'ts' of the last recorded snapshot today, or None."""
    if not today_file.exists():
        return None
    try:
        # Read last line (tail-style; daily files won't be huge)
        with open(today_file, "rb") as f:
            f.seek(0, 2)  # end
            file_size = f.tell()
            if file_size == 0:
                return None
            # Read last ~2KB and split lines
            f.seek(max(0, file_size - 2048))
            tail = f.read().decode("utf-8", errors="ignore")
        lines = [ln for ln in tail.strip().split("\n") if ln.strip()]
        if not lines:
            return None
        last = json.loads(lines[-1])
        return last.get("ts")
    except Exception:
        return None


def _append_snapshot(today_file: Path, snapshot: dict, logger: logging.Logger) -> bool:
    """Append snapshot as one JSONL line. Returns True on success."""
    try:
        today_file.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(snapshot, separators=(",", ":"))
        with open(today_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        logger.error(f"append failed: {e!r}")
        return False


def capture_once(logger: logging.Logger) -> str:
    """Single-shot capture. Returns one of: 'captured', 'duplicate',
    'no_source', 'error'."""
    snapshot = _read_latest_snapshot(logger)
    if snapshot is None:
        return "no_source"

    inner_ts = snapshot.get("ts")
    if not inner_ts:
        logger.warning(f"snapshot missing 'ts' field: {list(snapshot.keys())[:5]}")
        return "error"

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_file = OUTPUT_DIR / f"{today_str}.jsonl"

    last_ts = _last_recorded_ts(today_file)
    if last_ts == inner_ts:
        logger.info(f"dedup skip (TickStreamer ts unchanged): {inner_ts}")
        return "duplicate"

    if _append_snapshot(today_file, snapshot, logger):
        # Log success with a snippet for visibility
        delta = snapshot.get("delta", "?")
        poc = snapshot.get("poc", "?")
        cvd = snapshot.get("cvd_session", "?")
        logger.info(f"captured snapshot ts={inner_ts} delta={delta} poc={poc} cvd_session={cvd}")
        return "captured"
    return "error"


def loop_mode(interval_sec: int, logger: logging.Logger) -> None:
    logger.info(f"loop mode start (interval={interval_sec}s)")
    while True:
        try:
            capture_once(logger)
        except Exception as e:
            logger.error(f"unexpected loop err: {e!r}")
        time.sleep(interval_sec)


def main() -> int:
    parser = argparse.ArgumentParser(description="Volumetric snapshot recorder")
    parser.add_argument("--loop", type=int, default=0,
                          help="Loop interval in seconds (0 = single-shot)")
    args = parser.parse_args()

    logger = _setup_logging()
    if args.loop > 0:
        loop_mode(args.loop, logger)
        return 0
    else:
        result = capture_once(logger)
        # Always exit 0 — Task Scheduler shouldn't mark "missing source" as failure
        return 0


if __name__ == "__main__":
    sys.exit(main())
