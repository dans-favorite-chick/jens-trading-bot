"""T2 (sibling of Finding A) — backfill_bot_id atomic save.

Pins the 2026-06-03 fix to tools/backfill_bot_id.py: the writer no longer
opens the destination file directly with mode "w" (which truncates on
open and corrupts on mid-write process kill). It now writes to a
pid/tid-suffixed tmp sibling and atomically renames via os.replace.

Background — same shape as Finding A on core/trade_memory.py:save():
a raw ``open("w")`` truncates the file the moment it is opened; if the
process is killed (Windows scheduled-task restart, Ctrl+C, OOM, etc.)
between open and json.dump completing, the destination is left empty
or with a partial JSON document and the canonical trade memory is
lost. Atomic rename eliminates that window.

The pid/tid suffix also makes the tmp path unique across concurrent
writers — Windows os.replace cannot atomically clobber a destination
held open by another writer, so a shared ``.tmp`` suffix collides on
the second writer.

Run: pytest tests/test_backfill_bot_id_atomic.py -v
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.backfill_bot_id as bbi


def _seed(tmp_path: Path, trades: list[dict]) -> Path:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "logs" / "trade_memory.json"
    p.write_text(json.dumps(trades), encoding="utf-8")
    return p


def _stuck_trade(tid: str = "T-0001", account: str = "Sim101",
                 recorded_at: str = "2026-05-01T10:00:00") -> dict:
    return {
        "trade_id": tid,
        "strategy": "bias_momentum",
        "account": account,
        "recorded_at": recorded_at,
        "entry_price": 28000.0,
    }


# ─── Assertion 1: success path leaves no .tmp.* siblings ─────────────

def test_successful_write_leaves_no_tmp_siblings(tmp_path, monkeypatch):
    """After a successful run, the destination contains the backfilled
    rows and NO ``*.tmp.*`` sibling files remain in the logs/ dir."""
    path = _seed(tmp_path, [
        _stuck_trade("T-1"),
        _stuck_trade("T-2", account="SimBias Momentum"),
    ])
    monkeypatch.chdir(tmp_path)

    # Drive main() with explicit argv so it doesn't inherit pytest's
    # argv. The writer reads --file and walks the destination path.
    with patch("sys.argv", ["backfill_bot_id.py",
                            "--file", str(path)]):
        rc = bbi.main()
    assert rc == 0

    # Destination has new content (bot_id field stamped on each row).
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert all("bot_id" in row for row in raw), (
        f"every row should have bot_id stamped; got {raw}"
    )

    # NO .tmp.* siblings remain — the atomic rename must have cleaned up.
    siblings = list(path.parent.glob(f"{path.name}.tmp.*"))
    assert siblings == [], (
        f"atomic rename should leave zero .tmp.* siblings; found {siblings}"
    )


# ─── Assertion 2: mid-rename failure leaves dest UNCHANGED ───────────

def test_mid_rename_failure_leaves_destination_unchanged(
    tmp_path, monkeypatch
):
    """If os.replace raises mid-rename (e.g. disk full, AV lock), the
    destination MUST be unchanged — not truncated, not partially written.

    This is the core safety property the atomic-rename pattern buys
    over a raw open("w") + json.dump.
    """
    original_rows = [_stuck_trade("T-1")]
    path = _seed(tmp_path, original_rows)
    before = path.read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # Make os.replace raise on the next call inside the writer.
    def _boom(*args, **kwargs):
        raise OSError("simulated mid-rename failure")

    with patch.object(bbi.os, "replace", side_effect=_boom):
        with patch("sys.argv", ["backfill_bot_id.py",
                                "--file", str(path)]):
            with pytest.raises(OSError, match="simulated mid-rename failure"):
                bbi.main()

    # Destination still has original content — atomic semantics held.
    after = path.read_text(encoding="utf-8")
    assert after == before, (
        "destination was modified despite os.replace failing — the "
        "atomic-rename safety property is broken"
    )
    # The original_rows are still there byte-for-byte.
    raw_after = json.loads(after)
    assert raw_after == original_rows, (
        f"original rows not preserved; got {raw_after}"
    )


# ─── Assertion 3: distinct (pid, tid) → distinct tmp paths ───────────

def test_distinct_pid_tid_produce_distinct_tmp_paths(tmp_path, monkeypatch):
    """The tmp suffix MUST include both pid and tid so two concurrent
    writers (different threads OR different processes) produce different
    tmp paths. Otherwise the second writer collides on the first's tmp
    file and Windows os.replace fails."""
    path = _seed(tmp_path, [_stuck_trade("T-1")])

    # Simulate two writers with (pid, tid) pairs and confirm the tmp
    # paths the writer would construct differ.
    def _tmp_for(pid: int, tid: int) -> Path:
        return path.parent / f"{path.name}.tmp.{pid}.{tid}"

    p1 = _tmp_for(pid=1000, tid=10)
    p2 = _tmp_for(pid=1000, tid=11)   # same pid, different thread
    p3 = _tmp_for(pid=2000, tid=10)   # different process

    assert p1 != p2, (
        "same-pid different-tid should produce DIFFERENT tmp paths; "
        f"got {p1} and {p2}"
    )
    assert p1 != p3, (
        "different-pid same-tid should produce DIFFERENT tmp paths; "
        f"got {p1} and {p3}"
    )
    assert p2 != p3, (
        f"different (pid,tid) should produce different tmp paths; "
        f"got {p2} and {p3}"
    )

    # Also: the literal pattern used by the writer must embed both
    # os.getpid() and threading.get_ident() — guard against a future
    # refactor dropping one of them.
    import inspect
    src = inspect.getsource(bbi.main)
    assert "os.getpid()" in src, (
        "backfill_bot_id.main() no longer calls os.getpid() — tmp path "
        "uniqueness across processes is gone"
    )
    assert "threading.get_ident()" in src, (
        "backfill_bot_id.main() no longer calls threading.get_ident() — "
        "tmp path uniqueness across threads is gone"
    )
