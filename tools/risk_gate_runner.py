r"""
Phoenix Bot - RiskGate runner

Standalone process. Owns:
  - one RiskGate (state, check chain)
  - one PipeServer (Windows named pipe \\.\pipe\phoenix_risk_gate)
  - one HeartbeatWriter (touches heartbeat/risk_gate.hb every 1 s)

Started manually OR via Windows Task Scheduler. Runs in the foreground
so Task Scheduler can monitor exit codes. Logs to logs/risk_gate.log.

Default: NOT auto-started. Operator runs `python tools/risk_gate_runner.py`
when ready, then sets PHOENIX_RISK_GATE=1 in base_bot env to opt in.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PHOENIX_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PHOENIX_ROOT / "logs"
HEARTBEAT_DIR = PHOENIX_ROOT / "heartbeat"


def _setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(LOGS_DIR / "risk_gate.log",
                             maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


def _heartbeat_loop(stop_event: threading.Event, period_s: float = 1.0):
    """Touch heartbeat/risk_gate.hb every `period_s` seconds.
    The watchdog (tools/watchdog_runner.py) reads age and trips a
    killswitch if it goes stale > 2 s."""
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    hb = HEARTBEAT_DIR / "risk_gate.hb"
    while not stop_event.is_set():
        try:
            hb.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass
        stop_event.wait(period_s)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="risk_gate_runner",
        description=("Phoenix RiskGate runner. Hosts the named-pipe server "
                     "(\\\\.\\pipe\\phoenix_risk_gate), the heartbeat writer, "
                     "and the in-process RiskGate. Default: opt-in via "
                     "PHOENIX_RISK_GATE=1; otherwise base_bot keeps the "
                     "legacy direct-write path."))
    p.add_argument("--config", type=str, default=None,
                   help="Optional path to a RiskConfig YAML/JSON override "
                        "(reserved; current build reads env only)")
    p.add_argument("--pipe-path", type=str, default=None,
                   help="Override pipe path (default \\\\.\\pipe\\phoenix_risk_gate)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging()
    logger = logging.getLogger("RiskGateRunner")
    logger.info("=" * 60)
    logger.info(" Phoenix RiskGate runner starting")
    if args.config:
        logger.info(f" --config={args.config} (note: reserved; env-driven today)")
    logger.info("=" * 60)

    from core.risk.risk_config import RiskConfig
    from core.risk.risk_gate import RiskGate
    from core.risk.pipe_server import PipeServer

    cfg = RiskConfig.from_env()
    gate = RiskGate(cfg)
    server = PipeServer(gate, pipe_path=args.pipe_path)

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info(f"signal {signum}; shutting down")
        stop_event.set()
        server.stop()
    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass

    hb_thread = threading.Thread(target=_heartbeat_loop, args=(stop_event,),
                                 daemon=True, name="heartbeat")
    hb_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_event.set()
    logger.info("RiskGate runner stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
