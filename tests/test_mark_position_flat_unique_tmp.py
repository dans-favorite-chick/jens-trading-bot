"""T3 — mark_position_flat uses pid/tid-suffixed tmp for concurrency safety.

Pins the 2026-06-03 fix to tools/mark_position_flat.py: the atomic-rename
write block no longer uses a fixed ``.json.tmp`` suffix shared across
writers. It now derives a pid/tid-suffixed tmp path, so two concurrent
invocations (different processes or different threads in the same
process) don't collide on the same tmp file.

Background — same idiom as core/trade_memory.py:save() and T2 sibling on
tools/backfill_bot_id.py. On Windows, os.replace cannot atomically
clobber a destination held open by another writer, so a shared tmp
suffix means the second writer fails partway through. Per-writer tmp
paths eliminate that race.

Run: pytest tests/test_mark_position_flat_unique_tmp.py -v
"""
from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "mark_position_flat.py"


# ─── Assertion 1: tmp path embeds both pid AND tid ──────────────────

def test_tmp_path_construction_embeds_pid_and_tid():
    """Two writers with different (pid, tid) MUST produce different tmp
    paths.

    Pure inspection test — reads the tool source and verifies the tmp
    path construction calls both os.getpid() and threading.get_ident()
    so the suffix is unique per (process, thread). Reverting to a
    fixed ``.json.tmp`` suffix removes both calls and this assertion
    fails.
    """
    src = TOOL.read_text(encoding="utf-8")

    # The atomic-rename block must derive tmp from path.parent +
    # f-string with .tmp.{pid}.{tid} — the pre-fix
    # `path.with_suffix(".json.tmp")` pattern MUST be gone.
    assert "path.with_suffix(\".json.tmp\")" not in src, (
        "shared .json.tmp suffix is back — T3 regressed; concurrent "
        "writers will collide on Windows os.replace"
    )

    # Both pid and tid must appear in the writer's tmp-path source.
    assert re.search(r"_os\.getpid\(\)|os\.getpid\(\)", src), (
        "tmp path no longer embeds os.getpid() — uniqueness across "
        "processes is gone"
    )
    assert re.search(
        r"_threading\.get_ident\(\)|threading\.get_ident\(\)", src
    ), (
        "tmp path no longer embeds threading.get_ident() — "
        "uniqueness across threads is gone"
    )

    # And the f-string pattern with .tmp.{pid}.{tid} must be present.
    assert re.search(
        r"\.tmp\.\{_?os\.getpid\(\)\}\.\{_?threading\.get_ident\(\)\}",
        src,
    ), (
        "tmp path f-string no longer matches the pid/tid pattern "
        "expected by T3"
    )

    # And the atomic rename now goes through os.replace (not
    # Path.replace), matching the T2 sibling idiom.
    assert re.search(r"_os\.replace\(|os\.replace\(", src), (
        "atomic rename no longer uses os.replace() — the T3 fix is "
        "incomplete or has regressed"
    )


# ─── Assertion 2: final destination has expected content, no .tmp.* ─

def test_apply_writes_destination_and_leaves_no_tmp_siblings(tmp_path):
    """End-to-end run of the tool with --apply produces a clean
    destination and no leftover .tmp.* sibling files.

    This verifies the rename actually completed (rather than leaving a
    stray tmp file alongside the real one)."""
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    dest = logs / "trade_memory.json"
    dest.write_text(json.dumps([
        {
            "trade_id": "abc123",
            "strategy": "bias_momentum",
            "account": "SimBias Momentum",
            "direction": "LONG",
            "entry_price": 27800.0,
            "entry_time": "2026-06-03T09:30:00",
            "exit_price": None,
            "exit_time": None,
            "state": "exit_pending",
        }
    ]), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(TOOL), "--trade-id", "abc123", "--apply",
         "--exit-price", "27795.5"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"tool failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Destination has the new state.
    raw = json.loads(dest.read_text(encoding="utf-8"))
    assert raw[0]["state"] == "manually_closed"
    assert raw[0]["exit_price"] == 27795.5

    # No .tmp.* siblings remain — atomic rename cleaned up.
    siblings = list(logs.glob("trade_memory.json.tmp.*"))
    assert siblings == [], (
        f"expected zero .tmp.* siblings after atomic rename; "
        f"found {siblings}"
    )

    # And the legacy fixed-suffix tmp file is also gone (T3 is about
    # killing this exact pattern).
    legacy_tmp = logs / "trade_memory.json.tmp"
    assert not legacy_tmp.exists(), (
        f"legacy shared {legacy_tmp.name} reappeared — T3 regressed"
    )
