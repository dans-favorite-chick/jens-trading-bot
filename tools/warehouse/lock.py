"""
tools.warehouse.lock — PID-file lock for single-writer DuckDB warehouse.

Cross-platform (no fcntl / msvcrt). Stale-PID detection via psutil.

Usage:
    from tools.warehouse.lock import acquire_lock, release_lock

    lock_info = acquire_lock()      # raises RuntimeError if another live process holds it
    try:
        ... do ingest work ...
    finally:
        release_lock()              # always runs
"""

import atexit
import json
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

import psutil

from tools.warehouse import LOCK_PATH

log = logging.getLogger(__name__)

_lock_held = threading.local()   # per-thread sentinel; only one thread ingests at a time


def acquire_lock(lock_path: Path = LOCK_PATH, *, skip_lock: bool = False) -> dict | None:
    """Acquire the warehouse PID lock.

    Parameters
    ----------
    lock_path:  path to `.ingest.lock` (override in tests)
    skip_lock:  if True, return None immediately (caller holds lock already)

    Returns the lock dict written, or None if skip_lock=True.
    Raises RuntimeError if another live process holds the lock.
    """
    if skip_lock:
        return None

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text())
            pid  = existing.get("pid")
            host = existing.get("host")
            started = existing.get("started_at", "?")
            if host == socket.gethostname() and pid and psutil.pid_exists(int(pid)):
                raise RuntimeError(
                    f"another ingest is running (pid={pid}, started={started})"
                )
            log.warning("stale lock from pid %s (host=%s, started=%s), recovering", pid, host, started)
        except (json.JSONDecodeError, ValueError):
            log.warning("unreadable lock file at %s, overwriting", lock_path)

    lock_data = {
        "pid":        os.getpid(),
        "host":       socket.gethostname(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_path.write_text(json.dumps(lock_data))

    # Belt-and-suspenders: release on normal interpreter exit
    atexit.register(_atexit_release, lock_path)

    _lock_held.path = lock_path
    return lock_data


def release_lock(lock_path: Path = LOCK_PATH, *, skip_lock: bool = False) -> None:
    """Remove the PID lock file. Safe to call even if file is already gone."""
    if skip_lock:
        return
    try:
        if lock_path.exists():
            lock_path.unlink()
            log.debug("lock released: %s", lock_path)
    except OSError as exc:
        log.warning("could not remove lock file %s: %s", lock_path, exc)


def _atexit_release(lock_path: Path) -> None:
    """atexit handler — only releases if *this* process owns the lock."""
    try:
        if not lock_path.exists():
            return
        data = json.loads(lock_path.read_text())
        if data.get("pid") == os.getpid():
            lock_path.unlink()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────
# Context manager and aliases
# ──────────────────────────────────────────────────────────────

from contextlib import contextmanager


@contextmanager
def ingest_lock(lock_path: Path = LOCK_PATH):
    """Context manager: acquire on enter, release in finally."""
    acquire_lock(lock_path)
    try:
        yield
    finally:
        release_lock(lock_path)


# Aliases for plan-driven test compatibility.
acquire = acquire_lock
release = release_lock
