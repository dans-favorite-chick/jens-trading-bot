"""M2 — mark_position_flat detects concurrent writes via mtime-recheck.

Subagent B 2026-06-03 flagged the read-then-rename window in
``tools/mark_position_flat.py`` as a TOCTOU race: a concurrent writer
touching the destination file between the in-process read and the
``os.replace`` would silently lose its writes. The fix captures the
destination's ``st_mtime_ns`` BEFORE the read and re-stats BEFORE the
rename — if the value differs, it cleans up the tmp file and raises a
clear lost-update RuntimeError.

These tests exercise the script via ``main()`` so the real argparse +
filesystem path runs.

Run: pytest tests/test_mark_position_flat_no_toctou.py -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


def _seed_fake_phoenix_root(tmp_root: Path, trade_id: str) -> tuple[Path, str]:
    """Create a self-contained fake phoenix_bot project rooted at
    ``tmp_root`` containing ``logs/trade_memory.json`` with one
    unresolved trade matching ``trade_id``. Returns (tm_path, initial_json).
    """
    logs_dir = tmp_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (tmp_root / "memory").mkdir(parents=True, exist_ok=True)

    initial_trades = [
        {
            "trade_id": trade_id,
            "strategy": "bias_momentum",
            "account": "Sim101",
            "state": "exit_pending",
            "direction": "LONG",
            "entry_price": 28000.0,
            "exit_price": None,
        }
    ]
    initial_json = json.dumps(initial_trades, indent=2)
    tm_path = logs_dir / "trade_memory.json"
    tm_path.write_text(initial_json, encoding="utf-8")
    return tm_path, initial_json


# ─── Assertion 1: normal flow (no concurrent write) succeeds ─────────


def test_normal_flow_writes_mutation_and_leaves_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    proj_root = tmp_path / "phoenix_proj"
    tm_path, _initial_json = _seed_fake_phoenix_root(proj_root, "T-OK-001")

    monkeypatch.chdir(proj_root)
    monkeypatch.setattr(
        sys, "argv",
        ["mark_position_flat.py", "--trade-id", "T-OK-001", "--apply"],
    )

    from tools.mark_position_flat import main

    rc = main()
    assert rc == 0, f"main() should return 0 in normal flow, got {rc}"

    # Mutation landed.
    final = json.loads(tm_path.read_text(encoding="utf-8"))
    assert isinstance(final, list)
    assert final[0]["trade_id"] == "T-OK-001"
    assert final[0]["state"] == "manually_closed", (
        f"state was not flipped to manually_closed: {final[0]!r}"
    )

    # No leftover .tmp file.
    leftover = list(tm_path.parent.glob("trade_memory.json.tmp.*"))
    assert leftover == [], f"unexpected leftover tmp file(s): {leftover}"


# ─── Assertion 2: concurrent write triggers RuntimeError + cleanup ───


def test_concurrent_write_raises_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    proj_root = tmp_path / "phoenix_proj"
    tm_path, initial_json = _seed_fake_phoenix_root(proj_root, "T-RACE-002")

    monkeypatch.chdir(proj_root)
    monkeypatch.setattr(
        sys, "argv",
        ["mark_position_flat.py", "--trade-id", "T-RACE-002", "--apply"],
    )

    # Simulate a concurrent writer by hooking json.loads — the second
    # invocation is the one inside the apply loop (after mtime_before
    # was captured). At that point, bump the destination's mtime to
    # simulate another process touching the file.
    import json as _json_mod
    real_loads = _json_mod.loads
    loads_state = {"calls": 0}

    def loads_with_race(s, *args, **kwargs):
        result = real_loads(s, *args, **kwargs)
        loads_state["calls"] += 1
        # The apply-loop's json.loads is the second call (the first
        # happened during _enumerate_trade_memory_files). At that
        # point, simulate a concurrent writer bumping the file mtime.
        if loads_state["calls"] == 2:
            st = tm_path.stat()
            os.utime(
                str(tm_path),
                ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000),
            )
        return result

    monkeypatch.setattr("tools.mark_position_flat.json.loads", loads_with_race)

    from tools.mark_position_flat import main

    with pytest.raises(RuntimeError, match="Concurrent write.*mtime changed"):
        main()

    # Destination content must be UNCHANGED — the lost-update guard
    # aborted before the atomic rename.
    final = tm_path.read_text(encoding="utf-8")
    assert final == initial_json, (
        "destination content was mutated despite TOCTOU abort — "
        f"\nexpected: {initial_json!r}\ngot: {final!r}"
    )

    # tmp file cleaned up (the guard calls tmp.unlink(missing_ok=True)).
    leftover = list(tm_path.parent.glob("trade_memory.json.tmp.*"))
    assert leftover == [], f"tmp file not cleaned up after abort: {leftover}"
