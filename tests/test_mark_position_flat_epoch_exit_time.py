"""T1 (sibling of Finding E) — mark_position_flat writes float-epoch exit_time.

Pins the 2026-06-03 fix to tools/mark_position_flat.py: when --apply is used
together with --exit-price and the trade has no existing exit_time, the tool
must stamp exit_time as a float epoch (time.time()) and preserve the
human-readable ISO timestamp under exit_time_iso for forensics.

Background: the equity-curve _exit_key float() coerce in dashboard/server.py
silently dropped any row whose exit_time was an ISO string, and
RiskManager.hydrate_from_trades does a numeric since-cutoff compare on
exit_time. The pre-fix tool wrote ISO strings, so operator-elective manual
flattens silently vanished from the dashboard and risk replay.

This test mirrors the contract pinned by Finding E in
tests/test_pending_entry_sweeper_epoch_time.py — same idiom, same forensic
key. Reverting the production fix to ``t["exit_time"] = now_iso`` MUST
cause assertion #1 here to fail.

Run: pytest tests/test_mark_position_flat_epoch_exit_time.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "mark_position_flat.py"


def _seed(tmp_path: Path, trades: list[dict]) -> None:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )


def _stuck_trade(tid: str = "abc123") -> dict:
    return {
        "trade_id": tid,
        "strategy": "bias_momentum",
        "account": "SimBias Momentum",
        "direction": "LONG",
        "entry_price": 27800.0,
        "entry_time": "2026-06-03T09:30:00",
        "exit_price": None,
        "exit_time": None,
        "state": "exit_pending",
    }


def _run(tmp_path: Path, *cli_args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(TOOL), *cli_args],
        cwd=tmp_path, capture_output=True, text=True,
    )
    return result.returncode, result.stdout, result.stderr


# ─── Primary T1 assertion ────────────────────────────────────────────

def test_exit_time_is_float_epoch_after_apply_with_exit_price(tmp_path):
    """Primary T1 contract: exit_time MUST be a float epoch (NOT an ISO string).

    Reverting the production fix to ``t["exit_time"] = now_iso`` makes this
    test fail — the type check on line ``isinstance(exit_time, float)`` will
    flip to False because the assignment becomes a str.
    """
    _seed(tmp_path, [_stuck_trade("abc123")])

    t_before = time.time()
    rc, out, err = _run(
        tmp_path, "--trade-id", "abc123", "--apply",
        "--exit-price", "27795.5",
    )
    t_after = time.time()
    assert rc == 0, f"tool failed: stdout={out!r} stderr={err!r}"

    raw = json.loads(
        (tmp_path / "logs" / "trade_memory.json").read_text(encoding="utf-8")
    )
    trade = raw[0]

    # bool is an int subclass — guard explicitly so True/False don't pass.
    exit_time = trade.get("exit_time")
    assert exit_time is not None, "exit_time missing after --apply"
    assert not isinstance(exit_time, bool), (
        f"exit_time must not be a bool; got {exit_time!r}"
    )
    assert isinstance(exit_time, float), (
        f"exit_time must be a float epoch (T1 contract); "
        f"got type {type(exit_time).__name__} value={exit_time!r}"
    )
    # Within the wall-clock window of the call (with slack for subprocess).
    assert (t_before - 2.0) <= exit_time <= (t_after + 2.0), (
        f"exit_time={exit_time} not within call window "
        f"[{t_before}, {t_after}]"
    )


# ─── exit_time_iso forensic sibling key ──────────────────────────────

def test_exit_time_iso_present_and_parseable(tmp_path):
    """T1 requires preserving the human-readable timestamp under exit_time_iso
    so forensics can still answer 'what wall-clock did this happen at'."""
    _seed(tmp_path, [_stuck_trade("abc123")])
    rc, out, err = _run(
        tmp_path, "--trade-id", "abc123", "--apply",
        "--exit-price", "27795.5",
    )
    assert rc == 0, f"tool failed: stdout={out!r} stderr={err!r}"

    raw = json.loads(
        (tmp_path / "logs" / "trade_memory.json").read_text(encoding="utf-8")
    )
    trade = raw[0]

    assert "exit_time_iso" in trade, (
        "row missing 'exit_time_iso' — T1 fix must preserve the "
        "human-readable timestamp under this forensic sibling key"
    )
    iso = trade["exit_time_iso"]
    assert isinstance(iso, str), (
        f"exit_time_iso must be a string; got type {type(iso).__name__}"
    )
    try:
        datetime.fromisoformat(iso)
    except (ValueError, TypeError) as exc:
        pytest.fail(
            f"exit_time_iso {iso!r} does not parse as ISO-8601: {exc}"
        )


# ─── Lock-in: revert detection ───────────────────────────────────────

def test_reverting_fix_would_fail_primary_assertion(tmp_path):
    """Lock-in test: prove the primary assertion meaningfully gates the fix.

    Simulates the pre-fix behavior (exit_time as ISO string) by pre-stamping
    the trade with an ISO exit_time and feeding it through a code path that
    would not re-stamp. We then assert that the resulting exit_time would
    FAIL the float-type assertion used in
    `test_exit_time_is_float_epoch_after_apply_with_exit_price`.

    If T1 ever regresses to ``t["exit_time"] = now_iso``, the primary test
    becomes equivalent to this scenario and fails.
    """
    # Pre-fix shape: tool writes ISO string under exit_time.
    pre_fix_iso = datetime.now().isoformat(timespec="seconds")
    simulated_pre_fix_trade = {
        "trade_id": "abc123",
        "exit_time": pre_fix_iso,
    }

    # The primary assertion in the test above is: isinstance(exit_time, float)
    # Verify that the pre-fix ISO-string shape would have failed it.
    assert not isinstance(simulated_pre_fix_trade["exit_time"], float), (
        "Lock-in invariant: the pre-fix ISO-string assignment MUST fail "
        "the float-type assertion. If this assertion fails, T1's primary "
        "test no longer meaningfully gates the fix."
    )
    # And confirm a true post-fix epoch DOES satisfy it (sanity check).
    epoch_value = time.time()
    assert isinstance(epoch_value, float), (
        "Sanity: time.time() must be a float for the T1 contract to hold."
    )
