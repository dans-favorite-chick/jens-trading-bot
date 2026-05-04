"""OIF filename must always match PhoenixOIFGuard's regex.

Forensic context: 2026-05-04 incident — two SHORT positions
(SimDom Pull Back, SimVWapp Pullback) were stuck in NT8 for hours.
Root cause: when a CLOSEPOSITION-style call to write_oif() supplied
no trade_id and no suffix, _stage_oif() produced filenames like:

    oif45845_phoenix_87104.txt

PhoenixOIFGuard's regex `(^phoenix_\\d+_)|(_phoenix_\\d+_)` REQUIRES a
trailing underscore after the PID digits. The file ends with `.txt`
right after the PID, so it failed the regex and the guard quarantined
the file as ROGUE — preventing NT8 ATI from ever processing it.

This left the bot's own retry-flatten path silently broken: every
auto-retry CLOSEPOSITION OIF was quarantined before NT8 could act on
it.

These tests assert that EVERY filename produced by `_stage_oif`
contains `_phoenix_<pid>_<something>` — the regex-safe form — even
when trade_id and suffix are both empty.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from bridge.oif_writer import _stage_oif, _PHOENIX_PID  # type: ignore


# Same regex as ninjatrader/PhoenixOIFGuard.cs:72-73.
GUARD_RE = re.compile(r"(^phoenix_\d+_)|(_phoenix_\d+_)")


def _basename_only(path: str) -> str:
    return os.path.basename(path)


def test_stage_oif_with_trade_id_matches_guard(tmp_path, monkeypatch):
    """Standard case: a real trade_id is supplied."""
    monkeypatch.setenv("PHOENIX_NT8_INCOMING", str(tmp_path))
    # _stage_oif uses module-level OIF_INCOMING; redirect via monkeypatch
    import bridge.oif_writer as mod
    monkeypatch.setattr(mod, "OIF_INCOMING", str(tmp_path))
    final, _ = _stage_oif("PLACE;test;MNQM6;BUY;1;MARKET;0;0;GTC;;;;",
                          "abc12345")
    fname = _basename_only(final)
    assert GUARD_RE.search(fname), f"filename {fname!r} fails OIF Guard regex"
    assert f"_phoenix_{_PHOENIX_PID}_" in fname


def test_stage_oif_with_empty_trade_id_still_matches_guard(tmp_path, monkeypatch):
    """Regression: trade_id="" must still produce a guard-matching filename.

    This is the exact case that broke the runtime-reconciliation flatten
    retries in production on 2026-05-04. CLOSEPOSITION calls without a
    trade_id produced `oifN_phoenix_PID.txt` which failed the guard regex
    `_phoenix_\\d+_` (no trailing underscore after the digits)."""
    import bridge.oif_writer as mod
    monkeypatch.setattr(mod, "OIF_INCOMING", str(tmp_path))
    final, _ = _stage_oif("CLOSEPOSITION;SimX;MNQM6;GTC;;;;;;;;;", "")
    fname = _basename_only(final)
    assert GUARD_RE.search(fname), (
        f"filename {fname!r} fails OIF Guard regex — would be quarantined "
        f"as ROGUE in production"
    )
    assert f"_phoenix_{_PHOENIX_PID}_" in fname


def test_stage_oif_with_empty_trade_id_and_suffix(tmp_path, monkeypatch):
    """Both trade_id and suffix empty — the worst case."""
    import bridge.oif_writer as mod
    monkeypatch.setattr(mod, "OIF_INCOMING", str(tmp_path))
    final, _ = _stage_oif("CLOSEPOSITION;SimX;MNQM6;GTC;;;;;;;;;",
                          trade_id="", suffix="")
    fname = _basename_only(final)
    assert GUARD_RE.search(fname), (
        f"filename {fname!r} fails OIF Guard regex"
    )


def test_stage_oif_with_trade_id_containing_spaces(tmp_path, monkeypatch):
    """Reconciled trade_ids contain spaces (account name) — must still pass.

    Real example: trade_id='RECONCILED_SimDom Pull Back_6e7e9d2b'.
    The guard regex matches `_phoenix_PID_` at the start of the tag
    portion regardless of what comes after — but verify it explicitly."""
    import bridge.oif_writer as mod
    monkeypatch.setattr(mod, "OIF_INCOMING", str(tmp_path))
    tid = "RECONCILED_SimDom Pull Back_6e7e9d2b"
    final, _ = _stage_oif("CLOSEPOSITION;SimDom Pull Back;MNQM6;GTC;;;;;;;;;",
                          tid)
    fname = _basename_only(final)
    assert GUARD_RE.search(fname), (
        f"filename {fname!r} fails OIF Guard regex"
    )


def test_stage_oif_filename_starts_with_oif(tmp_path, monkeypatch):
    """NT8 ATI requires filenames to START with 'oif' to be classified
    correctly. A non-`oif` prefix is silently dropped as 'Unknown OIF
    file type' (B45 incident, 2026-04-22)."""
    import bridge.oif_writer as mod
    monkeypatch.setattr(mod, "OIF_INCOMING", str(tmp_path))
    final, _ = _stage_oif("PLACE;test;MNQM6;BUY;1;MARKET;0;0;GTC;;;;", "")
    fname = _basename_only(final)
    assert fname.startswith("oif"), f"filename {fname!r} doesn't start with 'oif'"
