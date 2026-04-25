"""
Phoenix Bot - TradingView Webhook Runner (Phase B+ Section 3.1)

argparse-driven launcher for bridge/tradingview_webhook.py. Sets up a
rotating log handler, registers SIGINT / SIGTERM for graceful shutdown,
and starts Flask's built-in WSGI server on the configured host:port.

DEFAULT BIND IS 127.0.0.1 ON PURPOSE. The TradingView source IPs are
expected to reach this machine via Tailscale or a port-forward at the
operator's perimeter; the receiver itself is loopback-only by default.
Do NOT change the default to 0.0.0.0.

Usage:
    python tools/tradingview_webhook_runner.py --port 5050
    python tools/tradingview_webhook_runner.py --port 5050 --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading

# Make the project root importable when launched directly from tools/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bridge.tradingview_webhook import create_app, _allowed_strategies  # noqa: E402


_shutdown_event = threading.Event()


def _install_signal_handlers() -> None:
    """Register SIGINT / SIGTERM so Ctrl-C and `taskkill /T` shut down
    cleanly. Werkzeug's dev server doesn't expose a programmatic stop,
    so we set the event and rely on os._exit on a second signal."""
    def _handler(signum, frame):
        if _shutdown_event.is_set():
            # Second signal — operator wants out NOW.
            sys.stderr.write("\n[TV_WEBHOOK] forced exit on second signal\n")
            os._exit(2)
        sys.stderr.write(f"\n[TV_WEBHOOK] caught signal {signum}, shutting down...\n")
        _shutdown_event.set()
        # Re-raise so Flask's blocking serve_forever returns. On Windows
        # SIGTERM may not be deliverable; SIGINT is the practical signal.
        raise KeyboardInterrupt()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Some signals are unavailable on Windows — ignore.
            pass


def _setup_logging(log_path: str) -> None:
    """Rotating file log: 5MB per file, 5 backups."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid double-attaching on import-driven re-runs.
    if not any(isinstance(h, logging.handlers.RotatingFileHandler)
               and getattr(h, "baseFilename", "") == os.path.abspath(log_path)
               for h in root.handlers):
        root.addHandler(handler)

    # Mirror to stderr so an operator running in a terminal sees output.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
               for h in root.handlers):
        root.addHandler(stderr_handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tradingview_webhook_runner",
        description=(
            "Run the Phoenix TradingView webhook receiver. Default bind "
            "is 127.0.0.1 - upstream Tailscale / port-forward is the "
            "operator's responsibility."
        ),
    )
    p.add_argument("--port", type=int, default=5050,
                   help="TCP port to bind (default 5050; dashboard uses 5000).")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default 127.0.0.1; do NOT use 0.0.0.0).")
    p.add_argument("--log-file", default=os.path.join(_ROOT, "logs", "tradingview_webhook.log"),
                   help="Rotating log file path (5MB x 5).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _setup_logging(args.log_file)
    _install_signal_handlers()

    app = create_app()
    strategies = _allowed_strategies()
    secret = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
    secret_status = ("CONFIGURED" if secret and not secret.startswith("<placeholder")
                     else "MISSING (fail-closed: every request 503)")

    banner = [
        "==================================================",
        " Phoenix TradingView Webhook Receiver",
        "==================================================",
        f" Bind:        {args.host}:{args.port}",
        f" Endpoint:    POST http://{args.host}:{args.port}/webhook/tradingview",
        f" Health:      GET  http://{args.host}:{args.port}/webhook/tradingview/health",
        f" Secret:      {secret_status}",
        f" Strategies:  {list(strategies) if strategies else '<none> (fail-closed)'}",
        f" Log:         {args.log_file}",
        "==================================================",
    ]
    for line in banner:
        sys.stderr.write(line + "\n")

    try:
        # use_reloader=False because we install signal handlers ourselves
        # and the auto-reloader spawns a child process that confuses them.
        app.run(host=args.host, port=args.port, debug=False,
                use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        sys.stderr.write("[TV_WEBHOOK] shutdown complete\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
