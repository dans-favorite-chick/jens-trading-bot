"""Spawn prod_bot.py as a fully detached Windows process.

Survives the parent shell, parent Claude session, or any job-object
the parent might be enrolled in. Output goes to logs/prod_bot_stdout.log.

Why this exists:
  - dashboard/server.py::_start_bot uses subprocess.Popen with
    CREATE_NEW_PROCESS_GROUP. On Windows-with-Claude-bash that combo
    produces a subprocess that dies silently within 2-3 minutes.
    Confirmed on 2026-05-11 — see memory/context/MORNING_2026-05-12.md.
  - PowerShell Start-Process -WindowStyle Hidden has the same failure.
  - Bash background launch works but dies when Claude session ends.

DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB + CREATE_NEW_PROCESS_GROUP
together produce a process that:
  - Has no console attached (asyncio ProactorEventLoop ok)
  - Cannot be terminated by parent job-object exit
  - Has its own process group (Ctrl+C in parent does not propagate)
  - Logs to file via redirected stdout

Usage: python tools/spawn_prod_detached.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = Path(os.environ.get("LOCALAPPDATA", "")) / "Python" / "pythoncore-3.14-64" / "python.exe"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "prod_bot_stdout.log"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB

log_handle = open(LOG_PATH, "a", encoding="utf-8", buffering=1)

proc = subprocess.Popen(
    [str(PYTHON), "-u", str(ROOT / "bots" / "prod_bot.py")],
    cwd=str(ROOT),
    creationflags=flags,
    stdout=log_handle,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    close_fds=True,
)

print(f"Spawned prod_bot detached PID={proc.pid} log={LOG_PATH}")
