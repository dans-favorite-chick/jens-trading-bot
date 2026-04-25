"""
Phoenix Bot - Finnhub News Runner (Section 3.5)

Standalone async launcher for the FinnhubWebSocketClient. Persists every
received NewsEvent to ``logs/finnhub_news.jsonl`` (one JSON object per
line). Logs go to ``logs/finnhub_news.log``.

This runner is what gets registered as the Windows scheduled task
``PhoenixFinnhubNews`` when the operator activates the Section 3.5
news feed. It does NOT auto-register itself.

Usage
-----
  python tools/finnhub_news_runner.py --help
  python tools/finnhub_news_runner.py
  python tools/finnhub_news_runner.py --symbols AAPL,MSFT,SPY
  python tools/finnhub_news_runner.py --rest-only --poll 60
  python tools/finnhub_news_runner.py --ws-only

Behavior
--------
* Auto-detects mode (WS preferred, REST fallback) unless ``--rest-only``
  or ``--ws-only`` is set.
* SIGINT (Ctrl-C) and SIGTERM trigger graceful shutdown.
* Missing ``FINNHUB_API_KEY`` -> prints a clear error and exits non-zero
  WITHOUT crashing or partially connecting.
* Never logs the API key beyond a 4-char prefix.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure project root on sys.path when launched as a script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 2026-04-25: load .env before reading FINNHUB_API_KEY. When this script
# is launched by Task Scheduler (PhoenixFinnhubNews), the spawning context
# does NOT inherit shell env exports — it only sees user/system Windows
# environment vars. .env keys live ONLY in the dotfile, so without this
# load the runner would always exit with "FINNHUB_API_KEY is not set"
# even though the key is sitting in .env. Same pattern used by
# fred_poll.py and watcher_agent.py.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=True)
except Exception:
    pass  # python-dotenv missing → fall through; runner will report missing key

# Defer imports until after sys.path is fixed.
try:
    from core.news.finnhub_ws import (  # noqa: E402
        FINNHUB_API_KEY_ENV,
        FinnhubNewsItem,
        FinnhubWebSocketClient,
    )
except Exception as _e:  # pragma: no cover - import-time hard failure
    print(f"[finnhub_news_runner] import failed: {_e}", file=sys.stderr)
    sys.exit(2)


_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_FILE = _LOG_DIR / "finnhub_news.log"
_JSONL_FILE = _LOG_DIR / "finnhub_news.jsonl"


def _setup_logging(verbose: bool) -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if reloaded.
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)
    return logging.getLogger("FinnhubNewsRunner")


def _redact_key(api_key: Optional[str]) -> str:
    if not api_key:
        return "<unset>"
    if len(api_key) <= 4:
        return api_key[:1] + "***"
    return api_key[:4] + "***"


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="finnhub_news_runner",
        description=(
            "Phoenix Finnhub news runner - WS primary with REST fallback, "
            "writes NewsEvents to logs/finnhub_news.jsonl"
        ),
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="Comma-separated symbols to subscribe to (default: empty = general news)",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=60,
        help="REST poll interval in seconds (default: 60)",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--rest-only",
        action="store_true",
        help="Force REST polling mode (skip WebSocket)",
    )
    mode_group.add_argument(
        "--ws-only",
        action="store_true",
        help="Force WebSocket mode; exit if WS unavailable",
    )
    parser.add_argument(
        "--category",
        type=str,
        default="general",
        help="REST category filter (default: general)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose (DEBUG) logging",
    )
    return parser.parse_args(argv)


def _print_banner(args: argparse.Namespace, mode: str, key_redacted: str) -> None:
    print("=" * 72)
    print(" Phoenix Finnhub News Runner ".center(72, "="))
    print("=" * 72)
    print(f"  mode      : {mode}")
    print(f"  symbols   : {args.symbols or '(general news)'}")
    print(f"  poll_s    : {args.poll}")
    print(f"  category  : {args.category}")
    print(f"  api_key   : {key_redacted}")
    print(f"  jsonl     : {_JSONL_FILE}")
    print(f"  log       : {_LOG_FILE}")
    print("=" * 72)
    sys.stdout.flush()


def _persist_event(item: FinnhubNewsItem) -> None:
    """Append one NewsEvent as a single-line JSON record."""
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "id": item.id,
        "headline": item.headline,
        "summary": item.summary,
        "source": item.source,
        "url": item.url,
        "category": item.category,
        "datetime_iso": item.datetime_iso,
        "symbols_related": list(item.symbols_related or []),
        "timestamp": item.timestamp,
    }
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _JSONL_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        logging.getLogger("FinnhubNewsRunner").warning(
            "jsonl write failed: %s", e,
        )


async def _run(args: argparse.Namespace) -> int:
    log = _setup_logging(args.verbose)

    api_key = os.environ.get(FINNHUB_API_KEY_ENV, "").strip()
    if not api_key:
        log.error(
            "%s is not set; refusing to start. Set the env var and retry.",
            FINNHUB_API_KEY_ENV,
        )
        print(
            f"ERROR: {FINNHUB_API_KEY_ENV} is not set. "
            "Configure your Finnhub API key and retry.",
            file=sys.stderr,
        )
        return 3

    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]

    # Build client.
    client = FinnhubWebSocketClient(
        api_key=api_key,
        on_news=_persist_event,
        fallback_rest=not args.ws_only,
        rest_poll_interval_s=args.poll,
        symbols=symbols,
        category=args.category,
    )

    if args.rest_only:
        force_mode: Optional[str] = "rest"
        banner_mode = "REST (forced)"
    elif args.ws_only:
        force_mode = "ws"
        banner_mode = "WebSocket (forced)"
    else:
        force_mode = None
        banner_mode = "auto (WS -> REST fallback)"

    _print_banner(args, banner_mode, _redact_key(api_key))

    # Wire signal handlers so Ctrl-C / SIGTERM trigger graceful shutdown.
    loop = asyncio.get_running_loop()
    stop_signal = asyncio.Event()

    def _handle_stop(signame: str) -> None:
        log.info("signal received: %s - shutting down", signame)
        stop_signal.set()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handle_stop, sig_name)
        except (NotImplementedError, RuntimeError):
            # Windows ProactorEventLoop does not support add_signal_handler
            # for all signals - fall back to signal.signal().
            try:
                signal.signal(sig, lambda *_a, name=sig_name: _handle_stop(name))
            except Exception:
                pass

    runner_task = asyncio.create_task(client.start(force_mode=force_mode))
    stopper_task = asyncio.create_task(stop_signal.wait())

    done, pending = await asyncio.wait(
        {runner_task, stopper_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if stopper_task in done:
        log.info("stopping client...")
        await client.stop()

    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    log.info("runner exit")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
