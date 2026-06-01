# tests/warehouse/test_lock.py
"""Tests for the PID lock.

Note: The implementation raises RuntimeError (not a custom LockHeldError).
The plan imported LockHeldError, but since that alias isn't in the implementation,
we use RuntimeError directly and match on the error message.
"""
from __future__ import annotations
from pathlib import Path
import json
import os
import pytest

from tools.warehouse.lock import acquire, release, ingest_lock


def test_acquire_creates_file(tmp_path):
    lock = tmp_path / "test.lock"
    acquire(lock)
    assert lock.exists()
    data = json.loads(lock.read_text())
    assert data["pid"] == os.getpid()
    assert "host" in data and "started_at" in data
    release(lock)


def test_release_is_safe_on_missing(tmp_path):
    lock = tmp_path / "test.lock"
    release(lock)  # no error


def test_acquire_raises_on_live_lock(tmp_path):
    lock = tmp_path / "test.lock"
    acquire(lock)
    try:
        with pytest.raises(RuntimeError, match="another ingest is running"):
            acquire(lock)
    finally:
        release(lock)


def test_acquire_recovers_stale_dead_pid(tmp_path):
    lock = tmp_path / "test.lock"
    # Write a lock claiming an impossible PID on this host.
    import socket
    lock.write_text(json.dumps({
        "pid": 999_999_999,
        "host": socket.gethostname(),
        "started_at": "2026-01-01T00:00:00Z",
    }))
    acquire(lock)  # should succeed silently, overwriting
    assert json.loads(lock.read_text())["pid"] == os.getpid()
    release(lock)


def test_acquire_recovers_stale_different_host(tmp_path):
    lock = tmp_path / "test.lock"
    lock.write_text(json.dumps({
        "pid": 1,
        "host": "some-other-host-that-is-not-us",
        "started_at": "2026-01-01T00:00:00Z",
    }))
    acquire(lock)
    release(lock)


def test_ingest_lock_releases_in_finally(tmp_path):
    lock = tmp_path / "test.lock"

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with ingest_lock(lock):
            assert lock.exists()
            raise Boom()
    assert not lock.exists(), "lock must be released even on exception"
