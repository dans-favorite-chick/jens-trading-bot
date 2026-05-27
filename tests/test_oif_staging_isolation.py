"""
2026-05-25 — `.tmp` files must never be visible inside NT8's `incoming/`.

Forensic context: NT8's ATI watches `incoming/` for ALL file extensions,
not just `.txt`. When Phoenix's atomic write pattern staged
`incoming/<file>.txt.tmp` then renamed to `incoming/<file>.txt`, NT8's
file watcher raced Phoenix's rename and saw the `.tmp` ~50% of the time —
logging an orange `Unknown OIF file type ...txt.tmp` line and deleting
the file. The rename usually outpaced this race so the .txt still got
processed, but the Log tab noise was unmissable and a theoretical race
window meant a slow disk could lose an order outright.

Evidence (2026-05-25 09:07:03-09:07:12 Sim101 bracket):
  5 paired Log entries — orange `Unknown OIF file type ...txt.tmp`
  followed by white `Order Working ...`. Order at 09:22:07 (SimIB
  Breakout filled) proves the rename usually wins, but it shouldn't
  be a race at all.

Fix: stage the `.tmp` in `OIF_STAGING` (sibling folder of `incoming/`,
NOT watched by NT8) and `os.replace()` into `incoming/` for the atomic
cross-folder same-volume rename.

These tests pin the contract:
  - no `.tmp` file is EVER visible in `OIF_INCOMING`, even transiently
  - the staging folder is auto-created on first use
  - `_commit_staged` uses `os.replace` (not `os.rename`) so an existing
    destination is overwritten on Windows
"""
from __future__ import annotations

import os
import sys
import shutil
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bridge.oif_writer as oif  # noqa: E402


@pytest.fixture
def staged_layout(monkeypatch, tmp_path):
    """Lay out sibling incoming/ + incoming.staging/ folders under tmp_path.

    Both must live on the same volume — by being siblings of one another
    under the OS temp directory they automatically share the same volume.
    """
    incoming = tmp_path / "incoming"
    staging = tmp_path / "incoming.staging"
    incoming.mkdir()
    # Deliberately do NOT create staging — _stage_oif should mkdir it.
    monkeypatch.setattr(oif, "OIF_INCOMING", str(incoming))
    monkeypatch.setattr(oif, "OIF_STAGING", str(staging))
    return {"incoming": incoming, "staging": staging}


# ═══════════════════════════════════════════════════════════════════════
# Contract 1: NO `.tmp` file is EVER visible inside incoming/
# ═══════════════════════════════════════════════════════════════════════

def test_tmp_file_never_lands_in_incoming(staged_layout):
    """While a full bracket write is in flight via the public API, watch
    `incoming/` continuously and assert NO `.tmp` file is ever visible
    there — even transiently. This is the core fix: NT8's ATI watches
    incoming/ for ALL extensions, so a transient .tmp in incoming/ is
    what produced the Log-tab `Unknown OIF file type` noise.
    """
    incoming = staged_layout["incoming"]

    # Background poller: snapshot incoming/ as fast as possible while the
    # write happens. Append any `.tmp` filenames we ever see.
    observed_tmp_in_incoming: list[str] = []
    stop_flag = threading.Event()

    def poll():
        while not stop_flag.is_set():
            try:
                for p in incoming.iterdir():
                    if p.suffix == ".tmp" or p.name.endswith(".txt.tmp"):
                        observed_tmp_in_incoming.append(p.name)
            except OSError:
                pass
            # Tight loop — we want to catch sub-ms transient files.
            # time.sleep(0) yields without sleeping; minimum cost.
            time.sleep(0)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    try:
        # Drive the public API the way base_bot does.
        paths = oif.write_bracket_order(
            direction="LONG", qty=1, entry_type="LIMIT",
            entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
            trade_id="staging_check", account="Sim101",
        )
        assert len(paths) == 3, "bracket = entry + stop + target"
    finally:
        # Let the poller run a few more loops to catch any late writes.
        time.sleep(0.05)
        stop_flag.set()
        poller.join(timeout=1.0)

    assert observed_tmp_in_incoming == [], (
        f"`.tmp` files appeared inside incoming/ during the write — "
        f"this is exactly the bug 2026-05-25 fixed. NT8 would log "
        f"`Unknown OIF file type` for each: {observed_tmp_in_incoming}"
    )

    # Final committed state: .txt files, no .tmp anywhere in incoming/.
    final_names = [p.name for p in incoming.iterdir()]
    assert all(n.endswith(".txt") for n in final_names), final_names
    assert not any(n.endswith(".tmp") for n in final_names), final_names


def test_tmp_file_lives_in_staging_only(staged_layout):
    """Direct unit-level check: _stage_oif returns (staging_tmp, final).
    The first element MUST live in OIF_STAGING; the second in OIF_INCOMING.
    """
    staging_tmp, final = oif._stage_oif(
        "PLACE;Sim101;MNQM6;BUY;1;MARKET;0;0;GTC;;;;",
        trade_id="stg_only",
    )
    # Staging path is in the staging folder, ends in `.txt.tmp`.
    assert str(staging_tmp).startswith(str(staged_layout["staging"])), (
        f"_stage_oif returned tmp path {staging_tmp!r} not under "
        f"OIF_STAGING {staged_layout['staging']!r}"
    )
    assert staging_tmp.endswith(".txt.tmp"), staging_tmp
    # Final path is in incoming/, ends in `.txt` (no `.tmp`).
    assert str(final).startswith(str(staged_layout["incoming"])), (
        f"_stage_oif returned final path {final!r} not under "
        f"OIF_INCOMING {staged_layout['incoming']!r}"
    )
    assert final.endswith(".txt") and not final.endswith(".tmp"), final
    # The staging tmp file MUST exist at this point (write happened).
    assert os.path.exists(staging_tmp)
    # The final file must NOT yet exist — only after _commit_staged.
    assert not os.path.exists(final)


# ═══════════════════════════════════════════════════════════════════════
# Contract 2: staging folder is auto-created on first use
# ═══════════════════════════════════════════════════════════════════════

def test_staging_folder_auto_created(staged_layout):
    """Delete the staging folder (simulating fresh install / operator
    cleanup), run a write, assert the folder gets recreated transparently.
    """
    staging = staged_layout["staging"]
    # Pre-condition: staged_layout fixture did NOT create staging — it's
    # the writer's job. Force the state explicitly anyway.
    if staging.exists():
        shutil.rmtree(staging)
    assert not staging.exists()

    paths = oif.write_bracket_order(
        direction="LONG", qty=1, entry_type="LIMIT",
        entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
        trade_id="auto_create", account="Sim101",
    )
    assert len(paths) == 3
    # Staging dir now exists.
    assert staging.is_dir(), (
        "OIF_STAGING was not auto-created by _stage_oif — fresh install "
        "would fail to emit OIFs"
    )


# ═══════════════════════════════════════════════════════════════════════
# Contract 3: _commit_staged uses os.replace (Windows-safe atomic move
# that overwrites an existing destination)
# ═══════════════════════════════════════════════════════════════════════

def test_commit_staged_uses_os_replace(staged_layout):
    """Source-level + behavior-level: _commit_staged MUST call os.replace
    (not os.rename). os.rename on Windows raises FileExistsError if the
    destination already exists; os.replace silently overwrites. Same-
    volume cross-folder moves are atomic in either case, but only
    os.replace is overwrite-safe.
    """
    # Source-level check.
    src = Path(oif.__file__).read_text(encoding="utf-8")
    commit_start = src.index("def _commit_staged")
    commit_end = src.index("\ndef ", commit_start + 1)
    commit_region = src[commit_start:commit_end]
    assert "os.replace(tmp, final)" in commit_region, (
        "_commit_staged must use os.replace (Windows-safe overwrite). "
        "os.rename would raise FileExistsError on a (rare) destination "
        "collision."
    )
    assert "os.rename(tmp, final)" not in commit_region, (
        "_commit_staged must NOT use os.rename — switch to os.replace."
    )

    # Behavior-level check: pre-create the destination, then run a
    # write — os.replace must silently overwrite without raising.
    incoming = staged_layout["incoming"]
    # We can't easily predict the auto-counter filename, so we test the
    # primitive directly via _stage_oif + _commit_staged.
    staging_tmp, final = oif._stage_oif("PLACE;Sim101;MNQM6;BUY;1;MARKET;0;0;GTC;;;;",
                                        trade_id="replace_test")
    # Plant a pre-existing file at the destination.
    Path(final).write_text("PRE-EXISTING\n", encoding="utf-8")
    assert os.path.exists(final)
    # Commit should overwrite, not raise.
    written = oif._commit_staged([(staging_tmp, final)], "replace_test")
    assert written == [final]
    body = Path(final).read_text(encoding="utf-8")
    assert "PRE-EXISTING" not in body, (
        "os.replace failed to overwrite the destination — _commit_staged "
        "is using os.rename, not os.replace"
    )
    assert "PLACE;Sim101" in body


# ═══════════════════════════════════════════════════════════════════════
# Contract 4: staging cleanup helper
# ═══════════════════════════════════════════════════════════════════════

def test_cleanup_stale_staging_tmp_files_removes_old(staged_layout):
    """Stale .tmp files (older than max_age_s) get cleaned up; in-flight
    ones (newer) are left alone."""
    staging = staged_layout["staging"]
    staging.mkdir(exist_ok=True)
    # Old .tmp (stale).
    old = staging / "oif1_phoenix_999_old.txt.tmp"
    old.write_text("PLACE;old;;;;;;;;;;;\n")
    # Backdate it.
    past = time.time() - 300.0
    os.utime(old, (past, past))
    # Fresh .tmp (in flight).
    fresh = staging / "oif2_phoenix_999_fresh.txt.tmp"
    fresh.write_text("PLACE;fresh;;;;;;;;;;;\n")

    removed = oif.cleanup_stale_staging_tmp_files(max_age_s=60.0)
    assert removed == 1, f"expected 1 removed, got {removed}"
    assert not old.exists(), "stale .tmp should be removed"
    assert fresh.exists(), "fresh .tmp should be left alone"


def test_cleanup_no_staging_folder_returns_zero(monkeypatch, tmp_path):
    """If OIF_STAGING does not exist (fresh install before any write),
    cleanup should noop and return 0."""
    missing = tmp_path / "does_not_exist_staging"
    monkeypatch.setattr(oif, "OIF_STAGING", str(missing))
    removed = oif.cleanup_stale_staging_tmp_files(max_age_s=60.0)
    assert removed == 0


# ═══════════════════════════════════════════════════════════════════════
# Contract 5: NT8 only sees the final .txt (post-commit incoming state)
# ═══════════════════════════════════════════════════════════════════════

def test_final_incoming_state_is_txt_only(staged_layout):
    """After a full bracket write, every file in incoming/ is a `.txt`
    (no `.tmp` residue). The staging folder is allowed to be empty OR
    have only `.tmp` files (un-committed). NT8 sees only the .txt set."""
    incoming = staged_layout["incoming"]
    oif.write_bracket_order(
        direction="LONG", qty=1, entry_type="LIMIT",
        entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
        trade_id="final_state", account="Sim101",
    )
    for p in incoming.iterdir():
        assert p.suffix == ".txt", (
            f"incoming/ should only contain .txt files (NT8 ATI prefix-"
            f"matches all files in this folder). Found: {p.name}"
        )
        assert not p.name.endswith(".txt.tmp"), p.name
