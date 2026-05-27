"""Tests for tools/clean_nt8_outgoing.py — the NT8 outgoing janitor.

These tests guard the **safety contract**: position files, feed files,
active_stops.json, and any non-32-hex-tail file must be impossible to
delete, no matter what flags are passed. The dry-run default is also
enforced here.

Real UUIDs in `test_uuid_files_match` are byte-exact from
C:\\Users\\Trading PC\\Documents\\NinjaTrader 8\\outgoing\\ on 2026-05-25
to make sure the regex matches what NT8 actually writes today.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tools.clean_nt8_outgoing import (
    DEFAULT_AGE_DAYS,
    apply_deletions,
    main,
    matches_order_ack_pattern,
    scan,
)


# ════════════════════════════════════════════════════════════════════════
# 1. The strict regex must NEVER match preserved-file patterns
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    "MNQM6 Globex_Sim101_position.txt",
    "ES Globex_Sim101_position.txt",
    "MNQM6 Globex_SimBias Momentum_position.txt",
    "MNQM6 Globex_Sim_VWAP_Pullback_v2_position.txt",
    "MNQM6 Globex_SimORB_position.txt",
    "MNQM6 Globex_SimSpring Setup_position.txt",
])
def test_position_files_never_match(name):
    assert matches_order_ack_pattern(name) is False, (
        f"Position file {name!r} must never match the order-ack regex — "
        f"this is the load-bearing safety property of the janitor."
    )


@pytest.mark.parametrize("name", [
    "Live.txt",
    "Simulated Data Feed.txt",
    "Kinetick – End Of Day (Free).txt",
])
def test_feed_files_never_match(name):
    assert matches_order_ack_pattern(name) is False


def test_active_stops_json_never_match():
    # Belt: not a .txt so regex doesn't match.
    # Braces: also in EXPLICIT_PRESERVE.
    assert matches_order_ack_pattern("active_stops.json") is False


# ════════════════════════════════════════════════════════════════════════
# 2. Real UUID files from the live folder MUST match
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    # Byte-exact samples from outgoing/ on 2026-05-25:
    "Sim_VWAP_Pullback_v2_0b17993a2865419da755de6b05440f1b.txt",
    "Sim_multi_Day_501b3fd7422e414da97942ef6bc78f75.txt",
    "Sim_VWAP_Pullback_v2_12428c3a1dd64c989644bc57b3fab4d6.txt",
    "Sim101_07abb8b7a7704a7ab4b007a00b92ce72.txt",
    "SimBias Momentum_094c8273aa1e48768ad77abc1c2ad8d2.txt",
])
def test_uuid_files_match(name):
    assert matches_order_ack_pattern(name) is True, (
        f"Real NT8 order-ack file {name!r} must be detected as a candidate."
    )


@pytest.mark.parametrize("name", [
    "Sim101_07abb8b7a7704a7ab4b007a00b92ce7.txt",      # 31 hex, too short
    "Sim101_07abb8b7a7704a7ab4b007a00b92ce72X.txt",    # 33 chars, non-hex
    "Sim101_07ABB8B7A7704A7AB4B007A00B92CE72.txt",     # uppercase hex
    "07abb8b7a7704a7ab4b007a00b92ce72.txt",            # no leading "_"
])
def test_almost_matching_files_dont_match(name):
    """Off-by-one fuzz around the regex boundary."""
    assert matches_order_ack_pattern(name) is False


# ════════════════════════════════════════════════════════════════════════
# 3. End-to-end on a tmp_path folder
# ════════════════════════════════════════════════════════════════════════

UUID_OLD = "Sim_VWAP_Pullback_v2_0b17993a2865419da755de6b05440f1b.txt"
UUID_YOUNG = "Sim_VWAP_Pullback_v2_12428c3a1dd64c989644bc57b3fab4d6.txt"
POSITION = "MNQM6 Globex_Sim101_position.txt"


def _make_file(folder: Path, name: str, age_days: float, content: str = "x") -> Path:
    p = folder / name
    p.write_text(content, encoding="utf-8")
    mt = time.time() - age_days * 86400.0
    os.utime(p, (mt, mt))
    return p


def test_files_too_young_kept(tmp_path):
    """A young UUID file (2 days old, cutoff 7) must not be a candidate."""
    _make_file(tmp_path, UUID_YOUNG, age_days=2.0)
    result = scan(tmp_path, age_days=DEFAULT_AGE_DAYS)
    assert result.too_young == 1
    assert len(result.candidates) == 0


def test_files_old_enough_deleted_on_apply(tmp_path):
    """A backdated UUID file (30 days) gets deleted on --apply."""
    old_path = _make_file(tmp_path, UUID_OLD, age_days=30.0)
    pos_path = _make_file(tmp_path, POSITION, age_days=30.0)  # MUST survive
    feed_path = _make_file(tmp_path, "Live.txt", age_days=30.0)  # MUST survive

    rc = main(["--path", str(tmp_path), "--apply", "--days", "7"])
    assert rc == 0
    assert not old_path.exists(), "Old UUID file should be deleted"
    assert pos_path.exists(), "Position file must NEVER be deleted"
    assert feed_path.exists(), "Feed file must NEVER be deleted"


def test_dry_run_default_does_not_delete(tmp_path):
    """Without --apply, no files are removed even if they match."""
    old_path = _make_file(tmp_path, UUID_OLD, age_days=30.0)
    rc = main(["--path", str(tmp_path), "--days", "7"])
    assert rc == 0
    assert old_path.exists(), "Dry-run must leave the file in place"


def test_invalid_path_exits_cleanly(tmp_path):
    """Pointing the tool at a non-existent path exits non-zero, no crash."""
    bad = tmp_path / "does_not_exist"
    rc = main(["--path", str(bad), "--apply"])
    assert rc == 1


# ════════════════════════════════════════════════════════════════════════
# 4. Defence in depth: scan() alone (without CLI) preserves safe files
# ════════════════════════════════════════════════════════════════════════

def test_mixed_folder_only_uuid_candidates(tmp_path):
    """One tmp folder with the full safety zoo — only UUID files become candidates."""
    safe = [
        POSITION,
        "ES Globex_Sim101_position.txt",
        "Live.txt",
        "Simulated Data Feed.txt",
        "Kinetick – End Of Day (Free).txt",
        "active_stops.json",
    ]
    uuids = [
        UUID_OLD,
        "Sim_multi_Day_501b3fd7422e414da97942ef6bc78f75.txt",
        "SimORB_e3bd446ce30c4546975c19a1b2c3d4e5.txt",
    ]
    for n in safe:
        _make_file(tmp_path, n, age_days=99.0)  # ancient — still must survive
    for n in uuids:
        _make_file(tmp_path, n, age_days=99.0)

    result = scan(tmp_path, age_days=7.0)
    candidate_names = sorted(c.path.name for c in result.candidates)
    assert candidate_names == sorted(uuids), (
        f"Only UUID files may be candidates. Got: {candidate_names}"
    )
    # And nothing in the safe zoo should appear:
    for s in safe:
        assert all(c.path.name != s for c in result.candidates)


def test_apply_deletions_counts(tmp_path):
    """apply_deletions returns (deleted, failed) accurately."""
    for i, n in enumerate([
        UUID_OLD,
        "Sim_multi_Day_501b3fd7422e414da97942ef6bc78f75.txt",
    ]):
        _make_file(tmp_path, n, age_days=30.0)
    result = scan(tmp_path, age_days=7.0)
    deleted, failed = apply_deletions(result)
    assert deleted == 2
    assert failed == 0
    for c in result.candidates:
        assert not c.path.exists()
