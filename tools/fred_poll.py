"""Phoenix Phase B+ §3.3 — Standalone FRED macro poller.

Fetches FRED macro snapshot, compares against last RegimeHistory snapshot,
records the result, and (best-effort) fires a Telegram alert on any
detected regime shift.

Usage:
    python tools/fred_poll.py --once
    python tools/fred_poll.py --interval-min 60

Logs to logs/fred_macros.log. Operator registers the scheduled task
separately — this script does not self-schedule.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

# Make the project root importable when run as `python tools/fred_poll.py`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(override=False)
except Exception:
    pass

from core.macros.fred_feed import FredMacroFeed, MacroSnapshot, RegimeShiftEvent
from core.macros.regime_history import RegimeHistory

logger = logging.getLogger("fred_poll")


def _setup_logging() -> None:
    log_dir = _PROJECT_ROOT / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    log_path = log_dir / "fred_macros.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] [%(levelname)s] %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Avoid duplicate handlers on re-runs in the same process.
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _fire_alert_best_effort(shifts: list[RegimeShiftEvent]) -> None:
    """Best-effort Telegram alert. Never crashes the poll loop."""
    if not shifts:
        return
    try:
        from core.telegram_notifier import notify_alert  # type: ignore
    except Exception as e:
        logger.warning("telegram_notifier unavailable: %s", e)
        return

    lines = ["FRED regime shift detected:"]
    for s in shifts:
        lines.append(
            f"- {s.series}: {s.direction} "
            f"({s.prev_value} -> {s.curr_value}, |delta|={s.magnitude:.3f})"
        )
    msg = "\n".join(lines)

    try:
        asyncio.run(notify_alert("FRED_REGIME_SHIFT", msg))
    except RuntimeError:
        # Already inside a running loop (unlikely from CLI). Fall back: schedule.
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(notify_alert("FRED_REGIME_SHIFT", msg))
        except Exception as e:
            logger.warning("telegram alert dispatch failed: %s", e)
    except Exception as e:
        logger.warning("telegram alert failed: %s", e)


def _poll_once(feed: FredMacroFeed, history: RegimeHistory) -> tuple[MacroSnapshot, list[RegimeShiftEvent]]:
    snap = feed.get_snapshot()

    prev = history.get_last_snapshot()
    shifts: list[RegimeShiftEvent] = []
    if prev is not None:
        shifts = feed.detect_regime_shift(prev, snap)

    history.record(snap, shifts)

    logger.info(
        "snapshot: FFR=%s CPI_YoY=%s UNRATE=%s 10Y2Y=%s shifts=%d",
        snap.ffr, snap.cpi_yoy, snap.unemployment, snap.yield_curve_2y10y, len(shifts),
    )
    for s in shifts:
        logger.info(
            "  shift: %s %s prev=%s curr=%s |delta|=%.3f",
            s.series, s.direction, s.prev_value, s.curr_value, s.magnitude,
        )

    if shifts:
        _fire_alert_best_effort(shifts)

    return snap, shifts


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phoenix FRED macro poller")
    p.add_argument(
        "--interval-min",
        type=int,
        default=60,
        help="Polling interval in minutes (default 60). Ignored if --once.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Single fetch, then exit.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = _parse_args(argv)

    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        logger.warning(
            "FRED_API_KEY not set — running in degraded mode (cache-only / no fresh fetch)"
        )

    feed = FredMacroFeed(api_key=api_key or None)
    history = RegimeHistory()

    if args.once:
        try:
            _poll_once(feed, history)
        except Exception as e:
            logger.exception("poll failed: %s", e)
            return 2
        return 0

    interval_s = max(60, int(args.interval_min) * 60)
    logger.info("starting poll loop, interval=%ds", interval_s)
    try:
        while True:
            try:
                _poll_once(feed, history)
            except Exception as e:
                logger.exception("poll iteration failed: %s", e)
            time.sleep(interval_s)
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
