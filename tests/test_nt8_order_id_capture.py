"""P1-8 tests for core.nt8_order_id_capture.

Coverage:
    - wait_for_stop_id returns the order_id when NT8 writes a WORKING
      file mid-poll.
    - wait_for_stop_id returns None when no WORKING file appears in
      the window.
    - wait_for_stop_id ignores files that pre-existed the wait (so a
      previous trade's WORKING file is not adopted).
    - wait_for_stop_id ignores files whose contents are not 'WORKING'
      (FILLED/CANCELLED/REJECTED never become the captured stop_id).
    - save_stop_id -> load_stop_id round-trip persists across processes
      (we simulate by reading the JSON file directly).
    - clear_stop_id removes the entry.
    - load_stop_id of a missing key returns None.
    - The state file is written atomically (tmp + rename): a tmpfile
      sibling never lingers after a successful write.
    - Multiple save_stop_id calls accumulate -- existing entries are
      preserved.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from core import nt8_order_id_capture as cap


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def outgoing(tmp_path):
    """Fresh empty outgoing folder per test."""
    d = tmp_path / "outgoing"
    d.mkdir()
    return d


@pytest.fixture
def state_path(tmp_path):
    """Absolute path to a per-test state file. Absolute so the module's
    _resolve_state_path() short-circuits the project-root anchoring."""
    return str(tmp_path / "active_stops.json")


# ---------------------------------------------------------------------------
# wait_for_stop_id
# ---------------------------------------------------------------------------

def test_wait_for_stop_id_returns_id_when_file_appears(outgoing):
    """A WORKING file dropped mid-poll is captured + returned."""
    captured_oid = "oid_abc123"
    account = "SimNoise"
    fname = f"{account}_{captured_oid}.txt"

    def _writer():
        time.sleep(0.4)  # ~2 polls in, well inside the window
        (outgoing / fname).write_text("WORKING;0;26500.25\n")

    t = threading.Thread(target=_writer, daemon=True)
    t.start()
    try:
        result = cap.wait_for_stop_id(
            trade_id="trade_xyz",
            outgoing_dir=str(outgoing),
            timeout_s=2.0,
            poll_interval_s=0.1,
        )
    finally:
        t.join(timeout=2.0)
    assert result == captured_oid


def test_wait_for_stop_id_returns_none_on_timeout(outgoing):
    """No WORKING file -> None after the timeout elapses."""
    t0 = time.monotonic()
    result = cap.wait_for_stop_id(
        trade_id="trade_no_ack",
        outgoing_dir=str(outgoing),
        timeout_s=0.4,
        poll_interval_s=0.1,
    )
    elapsed = time.monotonic() - t0
    assert result is None
    # Must actually have waited ~timeout, not returned instantly.
    assert elapsed >= 0.3


def test_wait_for_stop_id_ignores_preexisting_files(outgoing):
    """A WORKING file that predates the wait must not be adopted -- it
    belongs to a previous trade."""
    stale = outgoing / "SimNoise_oid_old.txt"
    stale.write_text("WORKING;0;26000.00\n")
    # Push mtime safely into the past so we're not racing the grace
    # window inside wait_for_stop_id.
    past = time.time() - 60.0
    os.utime(stale, (past, past))

    result = cap.wait_for_stop_id(
        trade_id="trade_new",
        outgoing_dir=str(outgoing),
        timeout_s=0.3,
        poll_interval_s=0.1,
    )
    assert result is None


def test_wait_for_stop_id_ignores_non_working_files(outgoing):
    """FILLED/CANCELLED/REJECTED files arriving mid-poll must not be
    captured as the new stop_id -- only WORKING counts."""
    def _writer():
        time.sleep(0.2)
        (outgoing / "SimNoise_oid_filled.txt").write_text("FILLED;1;26500.25\n")
        (outgoing / "SimNoise_oid_rejected.txt").write_text("REJECTED;0;0\n")

    t = threading.Thread(target=_writer, daemon=True)
    t.start()
    try:
        result = cap.wait_for_stop_id(
            trade_id="trade_xyz",
            outgoing_dir=str(outgoing),
            timeout_s=0.7,
            poll_interval_s=0.1,
        )
    finally:
        t.join(timeout=1.0)
    assert result is None


def test_wait_for_stop_id_handles_missing_outgoing_dir(tmp_path):
    """A non-existent outgoing folder must not crash -- just return None
    on timeout."""
    missing = tmp_path / "nope"
    result = cap.wait_for_stop_id(
        trade_id="trade_missing",
        outgoing_dir=str(missing),
        timeout_s=0.2,
        poll_interval_s=0.1,
    )
    assert result is None


# ---------------------------------------------------------------------------
# save_stop_id / load_stop_id / clear_stop_id round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_round_trip(state_path):
    cap.save_stop_id("trade_A", "oid_111", path=state_path)
    assert cap.load_stop_id("trade_A", path=state_path) == "oid_111"


def test_load_missing_trade_returns_none(state_path):
    assert cap.load_stop_id("never_saved", path=state_path) is None


def test_load_with_no_state_file_returns_none(state_path):
    # state file doesn't exist yet
    assert not os.path.exists(state_path)
    assert cap.load_stop_id("trade_A", path=state_path) is None


def test_save_preserves_existing_entries(state_path):
    cap.save_stop_id("trade_A", "oid_111", path=state_path)
    cap.save_stop_id("trade_B", "oid_222", path=state_path)
    assert cap.load_stop_id("trade_A", path=state_path) == "oid_111"
    assert cap.load_stop_id("trade_B", path=state_path) == "oid_222"


def test_save_overwrites_same_trade(state_path):
    cap.save_stop_id("trade_A", "oid_old", path=state_path)
    cap.save_stop_id("trade_A", "oid_new", path=state_path)
    assert cap.load_stop_id("trade_A", path=state_path) == "oid_new"


def test_clear_stop_id_removes_entry(state_path):
    cap.save_stop_id("trade_A", "oid_111", path=state_path)
    cap.save_stop_id("trade_B", "oid_222", path=state_path)
    cap.clear_stop_id("trade_A", path=state_path)
    assert cap.load_stop_id("trade_A", path=state_path) is None
    # Other trades survive.
    assert cap.load_stop_id("trade_B", path=state_path) == "oid_222"


def test_clear_missing_trade_is_noop(state_path):
    cap.save_stop_id("trade_A", "oid_111", path=state_path)
    cap.clear_stop_id("trade_does_not_exist", path=state_path)
    # Original entry still present, no exception raised.
    assert cap.load_stop_id("trade_A", path=state_path) == "oid_111"


def test_clear_with_no_state_file_is_noop(state_path):
    # Must not raise even though the file was never created.
    cap.clear_stop_id("trade_anything", path=state_path)
    assert not os.path.exists(state_path)


# ---------------------------------------------------------------------------
# Atomic write (tmp + rename) -- crash safety
# ---------------------------------------------------------------------------

def test_state_file_write_is_atomic_no_lingering_tmp(state_path, tmp_path):
    """After a successful save, no tmp sibling should remain in the
    directory (atomic write = single rename committed)."""
    cap.save_stop_id("trade_A", "oid_111", path=state_path)
    cap.save_stop_id("trade_B", "oid_222", path=state_path)
    # The state file itself exists.
    assert os.path.exists(state_path)
    # No tmp leftovers in the same directory.
    parent = tmp_path
    tmp_files = [
        p for p in os.listdir(parent)
        if p.startswith("active_stops.json.") and p.endswith(".tmp")
    ]
    assert tmp_files == [], (
        f"Expected zero lingering tmp files, got: {tmp_files}"
    )


def test_state_file_is_valid_json(state_path):
    """Verify we wrote real JSON, not some custom format, so external
    tools (jq, dashboards) can read it."""
    cap.save_stop_id("trade_A", "oid_111", path=state_path)
    cap.save_stop_id("trade_B", "oid_222", path=state_path)
    with open(state_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data == {"trade_A": "oid_111", "trade_B": "oid_222"}


def test_save_creates_parent_directory(tmp_path):
    """Atomic write should create parent dirs that don't exist yet
    (data/ may be wiped on a fresh deploy)."""
    deep = str(tmp_path / "data" / "nested" / "active_stops.json")
    cap.save_stop_id("trade_A", "oid_111", path=deep)
    assert os.path.exists(deep)
    assert cap.load_stop_id("trade_A", path=deep) == "oid_111"


def test_corrupt_state_file_treated_as_empty(state_path):
    """A corrupt/truncated state file must not propagate as an
    exception to callers -- treat as empty and continue."""
    with open(state_path, "w", encoding="utf-8") as fh:
        fh.write("this is not json {{{")
    # load returns None
    assert cap.load_stop_id("trade_A", path=state_path) is None
    # save still works and overwrites with valid JSON
    cap.save_stop_id("trade_A", "oid_111", path=state_path)
    assert cap.load_stop_id("trade_A", path=state_path) == "oid_111"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_save_rejects_empty_trade_id(state_path):
    with pytest.raises(ValueError):
        cap.save_stop_id("", "oid_111", path=state_path)


def test_save_rejects_empty_order_id(state_path):
    with pytest.raises(ValueError):
        cap.save_stop_id("trade_A", "", path=state_path)


# ---------------------------------------------------------------------------
# P1-8 INTEGRATION: end-to-end through bridge.oif_writer.write_oif
# ---------------------------------------------------------------------------

def test_write_oif_stop_persists_to_active_stops_json(
    tmp_path, monkeypatch, state_path,
):
    """End-to-end recovery path: write a stop OIF via write_oif and confirm
    data/active_stops.json contains the trade_id -> stop_order_id mapping.

    Wiring under test (P1-8, 2026-05-24):
        bridge.oif_writer.write_oif(action=PLACE_STOP_SELL, ...)
            -> scan_outgoing_for_order_id (mocked WORKING file)
            -> core.nt8_order_id_capture.save_stop_id
            -> data/active_stops.json
    """
    import bridge.oif_writer as oif

    # Build sibling incoming/outgoing folders. scan_outgoing_for_order_id
    # derives outgoing as os.path.dirname(OIF_INCOMING)/outgoing, so we
    # repoint OIF_INCOMING to a folder whose sibling is our outgoing.
    nt8_root = tmp_path / "nt8"
    incoming = nt8_root / "incoming"
    outgoing = nt8_root / "outgoing"
    incoming.mkdir(parents=True)
    outgoing.mkdir(parents=True)
    monkeypatch.setattr(oif, "OIF_INCOMING", str(incoming), raising=False)

    # Pre-place a WORKING file at the expected stop price so the in-band
    # scan_outgoing_for_order_id() call inside write_oif finds it within
    # the 0.5s window. NT8 publishes ``{account}_{order_id}.txt`` whose
    # first line is ``WORKING;0;<price>``.
    account = "SimE2E"
    stop_price = 26300.25
    expected_oid = "nt8oid_e2e_77"
    (outgoing / f"{account}_{expected_oid}.txt").write_text(
        f"WORKING;0;{stop_price:.2f}\n", encoding="utf-8",
    )

    # Redirect the P1-8 save_stop_id default path to a per-test absolute
    # file so the test never touches the real project data/active_stops.json.
    # Function default values are bound at DEFINITION time, so monkeypatching
    # the module-level _DEFAULT_STATE_PATH is not enough — we must also
    # rewrite save_stop_id.__defaults__ so the call site inside write_oif
    # (which passes no `path` kwarg) lands in the per-test file.
    _orig_save_defaults = cap.save_stop_id.__defaults__
    cap.save_stop_id.__defaults__ = (state_path,)
    monkeypatch.setattr(
        cap, "_DEFAULT_STATE_PATH", state_path, raising=False,
    )

    try:
        trade_id = "trade_e2e_p18"
        written = oif.write_oif(
            action="PLACE_STOP_SELL",
            qty=1,
            stop_price=stop_price,
            trade_id=trade_id,
            account=account,
        )
        assert written, "write_oif PLACE_STOP_SELL produced no files"

        # The mapping must now live in the persistence file.
        assert cap.load_stop_id(trade_id, path=state_path) == expected_oid, (
            f"P1-8 wiring broken: write_oif did not persist trade_id -> "
            f"stop_order_id. State file content: "
            f"{open(state_path).read() if os.path.exists(state_path) else '<missing>'}"
        )
    finally:
        cap.save_stop_id.__defaults__ = _orig_save_defaults
