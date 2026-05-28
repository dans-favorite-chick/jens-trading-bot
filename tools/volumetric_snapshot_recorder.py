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
    """Return the inner 'ts' of the last recorded snapshot today, or None.

    2026-05-27 fix: the old 2048-byte tail-read was smaller than a single
    JSONL line (each volumetric_bar is ~5000 bytes due to the imbalances
    array), so json.loads() of the truncated tail always raised and the
    bare except returned None. Result: dedup never fired and 144 duplicate
    stale-snapshot writes per day. Now we walk backwards from EOF in 4 KB
    chunks to find the previous newline, then take everything after it as
    the complete last line — robust to any line length.
    """
    if not today_file.exists():
        return None
    try:
        with open(today_file, "rb") as f:
            f.seek(0, 2)  # end
            size = f.tell()
            if size == 0:
                return None
            # Skip a single trailing newline if present.
            pos = size - 1
            f.seek(pos)
            if f.read(1) == b"\n":
                pos -= 1
            # Walk backwards in chunks until we find the newline that
            # begins the last line (or run off the front of the file).
            chunk_size = 4096
            buf = b""
            last_line = b""
            while pos >= 0:
                start = max(0, pos - chunk_size + 1)
                f.seek(start)
                buf = f.read(pos - start + 1) + buf
                nl = buf.rfind(b"\n")
                if nl >= 0:
                    last_line = buf[nl + 1:]
                    break
                pos = start - 1
            else:
                # Loop exhausted without a newline: whole file is one line.
                last_line = buf
        if not last_line.strip():
            return None
        last = json.loads(last_line.decode("utf-8", errors="strict"))
        return last.get("ts")
    except Exception as e:
        # Log instead of silently swallowing — a future regression here
        # must be visible, not invisible like the 2048-byte bug was.
        logging.getLogger("vol_recorder").warning(
            f"_last_recorded_ts: parse failed ({e!r}); dedup disabled this cycle"
        )
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


# 2026-05-27: staleness guard. The 8-day silent freeze (frozen TickStreamer
# feed, 2026-05-19 -> 2026-05-27) went undetected because the recorder logged
# "captured"/"dedup skip" happily while the underlying bar never changed.
# Now we compare the snapshot's OWN ts against wall-clock and warn LOUDLY if
# the feed looks frozen, so the next freeze is caught in minutes, not days.
STALENESS_WARN_HOURS = 2.0


def _snapshot_age_hours(inner_ts: str) -> Optional[float]:
    """Age (hours) of the snapshot's own ts vs wall clock. TickStreamer ts is
    local naive time, e.g. '2026-05-27T22:56:14.8720000' (7-digit fractional
    seconds). Returns None if unparseable."""
    try:
        s = str(inner_ts).replace("Z", "")
        if "." in s:
            head, frac = s.split(".", 1)
            s = f"{head}.{frac[:6]}"  # Python parses <=6 fractional digits
        bar_dt = datetime.fromisoformat(s)
        return (datetime.now() - bar_dt).total_seconds() / 3600.0
    except Exception:
        return None


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

    # Staleness guard — flag a frozen feed without changing capture behavior.
    age_h = _snapshot_age_hours(inner_ts)
    if age_h is not None and age_h >= STALENESS_WARN_HOURS:
        logger.warning(
            f"STALE FEED: volumetric snapshot ts={inner_ts} is {age_h:.1f}h old "
            f"(threshold {STALENESS_WARN_HOURS}h) — TickStreamer feed may be frozen. "
            f"Check the NT8 chart's data connection."
        )

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
