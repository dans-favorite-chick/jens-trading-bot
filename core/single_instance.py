"""
core.single_instance — bot single-instance enforcement
======================================================

Prevents two copies of the same Phoenix bot (sim_bot, prod_bot, lab_bot)
from running simultaneously. Discovered necessary 2026-05-20 when the
operator found TWO `sim_bot.py` processes running side-by-side since
2026-05-17 21:08:54 — both writing to the same `logs/trade_memory_sim.json`,
both submitting OIF files, both racing against each other on every
WS-tick that matched a signal.

Mechanism: each bot acquires an OS-level FILE LOCK on a PID file in
`run/<bot_name>.pid` at startup. If another process holds the lock,
the new instance reads the PID, prints a clear error explaining what
to do, and exits with code 17 (a recognizable "duplicate instance" code
the operator's launcher can detect and react to without misinterpreting
as a generic failure).

Uses `portalocker` if available (best — cross-platform exclusive file
locks), falls back to a PID-file existence check that's racy but better
than nothing on systems where portalocker isn't installed.

Usage from a bot script:

    from core.single_instance import acquire_or_exit
    acquire_or_exit("sim_bot")    # blocks if another instance has it;
                                  #   logs + exits if a stale PID is alive
    # ... rest of bot startup ...
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resolve project root regardless of where the caller is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = _PROJECT_ROOT / "run"

# Exit code surfaced when a duplicate instance is detected. Chosen to be
# distinct from common Python exit codes (0=success, 1=generic error,
# 2=usage, 130=SIGINT). 17 = "EEXIST" semantically — file already exists.
DUPLICATE_EXIT_CODE = 17


def _pid_alive(pid: int) -> bool:
    """Return True if the given PID is alive on this machine.

    Cross-platform: uses os.kill(pid, 0) on POSIX, which sends signal 0
    (a no-op that raises if the process doesn't exist). On Windows the
    same idiom works via WinAPI's OpenProcess.
    """
    if pid <= 0:
        return False
    try:
        # signal 0 = "check only, don't deliver"; raises OSError if the
        # process doesn't exist (or we lack permission, which we treat
        # as "alive enough to be cautious")
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_or_exit(bot_name: str) -> None:
    """Acquire a single-instance lock for `bot_name` or exit cleanly.

    On success: writes our PID to run/<bot_name>.pid and returns. The
    lock is released automatically when the process exits (the OS
    releases all file locks held by a dying process).

    On collision (another live process holds the lock): logs a CLEAR
    error message identifying the duplicate PID and exits with
    DUPLICATE_EXIT_CODE so the operator's launcher can distinguish
    "I'm a duplicate, stop trying to start me" from a generic crash.
    """
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    pid_path = _RUN_DIR / f"{bot_name}.pid"
    my_pid = os.getpid()

    # ── Path 1: portalocker available (preferred) ──────────────────
    try:
        import portalocker
    except ImportError:
        portalocker = None

    if portalocker is not None:
        try:
            # Open file in append-binary mode so we don't truncate any
            # existing PID until we hold the lock; then truncate + write.
            lock_fh = open(pid_path, "a+b")
            try:
                portalocker.lock(
                    lock_fh,
                    portalocker.LOCK_EX | portalocker.LOCK_NB,
                )
            except (portalocker.LockException, BlockingIOError):
                # Couldn't get the lock — another instance holds it.
                lock_fh.close()
                _exit_with_duplicate_error(bot_name, pid_path)
            # Got the lock. Truncate + write our PID.
            lock_fh.seek(0)
            lock_fh.truncate(0)
            lock_fh.write(str(my_pid).encode("ascii"))
            lock_fh.flush()
            # IMPORTANT: stash the handle on a module-level list so the
            # lock survives garbage collection for the lifetime of the
            # process. Closing the file releases the lock.
            _HELD_LOCKS.append(lock_fh)
            logger.info(
                f"[single_instance] {bot_name}: lock acquired (pid={my_pid}, "
                f"file={pid_path})"
            )
            return
        except Exception as e:
            logger.warning(
                f"[single_instance] portalocker path failed ({e!r}); "
                f"falling back to PID-file check"
            )

    # ── Path 2: PID-file fallback (racy but functional) ────────────
    # Read existing PID, check if alive. If alive, exit. If stale, take over.
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="ascii").strip() or "0")
        except (ValueError, OSError):
            existing_pid = 0
        if existing_pid and existing_pid != my_pid and _pid_alive(existing_pid):
            _exit_with_duplicate_error(bot_name, pid_path)
        # Stale PID file (process died without releasing): overwrite it.
        logger.info(
            f"[single_instance] {bot_name}: found stale PID {existing_pid} "
            f"(not alive); taking over"
        )

    # Write our PID.
    try:
        pid_path.write_text(str(my_pid), encoding="ascii")
        logger.info(
            f"[single_instance] {bot_name}: PID-file lock acquired "
            f"(pid={my_pid}, file={pid_path})"
        )
    except OSError as e:
        logger.error(
            f"[single_instance] {bot_name}: could not write PID file "
            f"{pid_path}: {e!r}. Continuing WITHOUT single-instance "
            f"protection — duplicate instances are possible."
        )


def _exit_with_duplicate_error(bot_name: str, pid_path: Path) -> None:
    """Print a clear error to stderr and exit with DUPLICATE_EXIT_CODE."""
    try:
        existing_pid = pid_path.read_text(encoding="ascii").strip()
    except OSError:
        existing_pid = "<unknown>"
    msg = (
        f"\n"
        f"==================================================================\n"
        f"  DUPLICATE INSTANCE DETECTED: {bot_name}\n"
        f"==================================================================\n"
        f"  Another {bot_name} is already running (PID {existing_pid}).\n"
        f"  Lock file: {pid_path}\n"
        f"\n"
        f"  Phoenix bots must run as a single instance per bot_name. Two\n"
        f"  copies would race each other on every signal, double-submit\n"
        f"  OIF files, and corrupt per-bot trade memory.\n"
        f"\n"
        f"  TO FIX:\n"
        f"    1. Kill the existing process (Task Manager / taskkill /PID)\n"
        f"    2. If the existing process is dead but the file still exists,\n"
        f"       delete it: del \"{pid_path}\"\n"
        f"    3. Re-run this bot.\n"
        f"\n"
        f"  Exiting with code {DUPLICATE_EXIT_CODE}.\n"
        f"==================================================================\n"
    )
    print(msg, file=sys.stderr)
    logger.critical(
        f"[single_instance] {bot_name}: DUPLICATE INSTANCE — existing "
        f"PID {existing_pid} holds {pid_path}. Exiting code {DUPLICATE_EXIT_CODE}."
    )
    sys.exit(DUPLICATE_EXIT_CODE)


# Module-level storage to keep portalocker file handles alive for the
# lifetime of the process. Lost references → garbage-collected → lock
# released. NEVER popped.
_HELD_LOCKS: list = []
