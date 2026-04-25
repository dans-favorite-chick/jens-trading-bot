"""Phase B+ section 3.2 -- tools/oif_killswitch.py tests.

Covers:
  - All accounts FLAT  -> CANCELALLORDERS only, zero CLOSEPOSITION
  - One account LONG, others FLAT -> 1 CLOSEPOSITION + N CANCELALLORDERS
  - --dry-run writes nothing, but plan is printed
  - --cancel-only skips CLOSEPOSITION even for non-FLAT accounts
  - OIF file content matches NT8's expected 13-field semicolon syntax
  - B59 LIVE_ACCOUNT guard skips matching account, others still flatten

The conftest.py autouse fixture redirects bridge.oif_writer.OIF_INCOMING
to a tmp dir, so writes never reach the real NT8 folder. We point the
killswitch's outgoing/ inspector at our tmp dir too via monkeypatch.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge.oif_writer as _oif  # noqa: E402
import tools.oif_killswitch as ks  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_position_files(outgoing: Path, instrument: str,
                         positions: dict[str, str]) -> None:
    """positions: {account: "FLAT;0;0" | "LONG;1;26800.00" | "SHORT;2;...}"""
    outgoing.mkdir(parents=True, exist_ok=True)
    for acct, content in positions.items():
        f = outgoing / f"{instrument} Globex_{acct}_position.txt"
        f.write_text(content + "\n", encoding="utf-8")


def _list_oifs(incoming_dir: str) -> list[str]:
    return sorted(p.name for p in Path(incoming_dir).iterdir()
                  if p.suffix == ".txt")


def _read_oif(incoming_dir: str, name: str) -> str:
    return (Path(incoming_dir) / name).read_text(encoding="ascii").strip()


@pytest.fixture
def oif_layout(monkeypatch, tmp_path):
    """Lay out a fake NT8 data dir with incoming/ + outgoing/ side by
    side -- exactly the structure the killswitch's _outgoing_dir() walk
    expects. Returns a dict for the test to use."""
    incoming = tmp_path / "incoming"
    outgoing = tmp_path / "outgoing"
    incoming.mkdir()
    outgoing.mkdir()
    # Override OIF_INCOMING in the writer module the killswitch will read.
    monkeypatch.setattr(_oif, "OIF_INCOMING", str(incoming))
    # Tell the killswitch to use this incoming/outgoing pair too.
    monkeypatch.setattr(ks._oif, "OIF_INCOMING", str(incoming))
    return {"incoming": str(incoming), "outgoing": outgoing,
            "instrument": "MNQM6"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_all_accounts_flat_writes_only_cancel(oif_layout):
    """Every targeted account is FLAT -> CANCELALLORDERS for each, zero
    CLOSEPOSITION files."""
    accts = ["Sim101", "SimBias Momentum", "SimORB"]
    _seed_position_files(
        oif_layout["outgoing"], oif_layout["instrument"],
        {a: "FLAT;0;0" for a in accts},
    )

    rc = ks.main(["--account", "Sim101",
                  "--account", "SimBias Momentum",
                  "--account", "SimORB",
                  "--instrument", oif_layout["instrument"]])
    assert rc == 0

    files = _list_oifs(oif_layout["incoming"])
    # 3 accounts x 1 cancel = 3 files. Zero CLOSEPOSITION.
    assert len(files) == 3, f"expected 3 cancel files, got {files}"
    for fname in files:
        assert "_killswitch_cancel_" in fname, fname
        body = _read_oif(oif_layout["incoming"], fname)
        assert body.startswith("CANCELALLORDERS;"), body
    # No close files written.
    assert not any("_killswitch_close_" in f for f in files)


def test_one_long_others_flat(oif_layout):
    """Sim101 is LONG 1@26800, others FLAT -> 1 CLOSEPOSITION + 3
    CANCELALLORDERS."""
    accts = ["Sim101", "SimBias Momentum", "SimORB"]
    _seed_position_files(
        oif_layout["outgoing"], oif_layout["instrument"],
        {
            "Sim101": "LONG;1;26800.00",
            "SimBias Momentum": "FLAT;0;0",
            "SimORB": "FLAT;0;0",
        },
    )

    rc = ks.main(["--account", "Sim101",
                  "--account", "SimBias Momentum",
                  "--account", "SimORB",
                  "--instrument", oif_layout["instrument"]])
    assert rc == 0

    files = _list_oifs(oif_layout["incoming"])
    cancels = [f for f in files if "_killswitch_cancel_" in f]
    closes = [f for f in files if "_killswitch_close_" in f]

    assert len(cancels) == 3, f"expected 3 cancels, got {cancels}"
    assert len(closes) == 1, f"expected 1 close, got {closes}"

    close_body = _read_oif(oif_layout["incoming"], closes[0])
    # NT8 13-field shape: CLOSEPOSITION;Sim101;MNQM6;GTC;;;;;;;;;
    assert close_body == f"CLOSEPOSITION;Sim101;{oif_layout['instrument']};GTC;;;;;;;;;"
    # Only the LONG account got the close.
    assert "_Sim101.txt" in closes[0]


def test_dry_run_writes_nothing(oif_layout, capsys):
    accts = ["Sim101", "SimBias Momentum"]
    _seed_position_files(
        oif_layout["outgoing"], oif_layout["instrument"],
        {
            "Sim101": "LONG;1;26800.00",
            "SimBias Momentum": "FLAT;0;0",
        },
    )

    rc = ks.main(["--account", "Sim101",
                  "--account", "SimBias Momentum",
                  "--dry-run",
                  "--instrument", oif_layout["instrument"]])
    assert rc == 0

    # Zero files on disk.
    assert _list_oifs(oif_layout["incoming"]) == []

    out = capsys.readouterr().out
    # Plan is printed (non-empty) and mentions DRY-RUN.
    assert "DRY-RUN" in out
    # Both planned commands appear in the plan output.
    assert "CANCELALLORDERS;Sim101;" in out
    assert f"CLOSEPOSITION;Sim101;{oif_layout['instrument']};GTC;" in out
    # FLAT account shouldn't get a planned close.
    assert "would write: CLOSEPOSITION;SimBias Momentum" not in out


def test_cancel_only_skips_close_even_when_long(oif_layout):
    """--cancel-only: CLOSEPOSITION is suppressed even though Sim101 is
    LONG. Only CANCELALLORDERS is written."""
    accts = ["Sim101"]
    _seed_position_files(
        oif_layout["outgoing"], oif_layout["instrument"],
        {"Sim101": "LONG;2;26815.25"},
    )

    rc = ks.main(["--account", "Sim101",
                  "--cancel-only",
                  "--instrument", oif_layout["instrument"]])
    assert rc == 0

    files = _list_oifs(oif_layout["incoming"])
    assert len(files) == 1
    assert "_killswitch_cancel_" in files[0]
    body = _read_oif(oif_layout["incoming"], files[0])
    # NT8 13-field CANCELALLORDERS shape (B44 fix): 12 trailing semicolons.
    assert body == "CANCELALLORDERS;Sim101;;;;;;;;;;;"
    # Verify no close file slipped through.
    assert not any("_killswitch_close_" in f for f in files)


def test_oif_format_matches_nt8_spec(oif_layout):
    """Verify both lines have exactly NT8's expected field count:
       CANCELALLORDERS  -> 13 fields (12 semicolons total)
       CLOSEPOSITION    -> 13 fields (12 semicolons total)"""
    _seed_position_files(
        oif_layout["outgoing"], oif_layout["instrument"],
        {"Sim101": "LONG;1;26800.00"},
    )
    rc = ks.main(["--account", "Sim101",
                  "--instrument", oif_layout["instrument"]])
    assert rc == 0

    files = _list_oifs(oif_layout["incoming"])
    assert len(files) == 2
    for fname in files:
        body = _read_oif(oif_layout["incoming"], fname)
        # 12 semicolons => 13 fields. NT8 ATI's B44 verified format.
        assert body.count(";") == 12, (
            f"{fname}: expected 12 semicolons (13 fields), got "
            f"{body.count(';')} -- {body!r}"
        )


def test_live_account_guard_skips_matching(oif_layout):
    """B59: any target whose name matches LIVE_ACCOUNT env var must NOT
    receive an OIF; other accounts still proceed. Exit code is 1
    (partial -- the live skip is reported)."""
    _seed_position_files(
        oif_layout["outgoing"], oif_layout["instrument"],
        {
            "Sim101": "FLAT;0;0",
            "9999999": "LONG;1;26800.00",
        },
    )
    with patch.dict(os.environ, {"LIVE_ACCOUNT": "9999999"}):
        rc = ks.main(["--account", "Sim101",
                      "--account", "9999999",
                      "--instrument", oif_layout["instrument"]])
    # 1 = partial (live account skipped, but Sim101 still flattened)
    assert rc == 1

    files = _list_oifs(oif_layout["incoming"])
    # Only Sim101 should have written -> exactly one cancel file.
    assert len(files) == 1
    assert "_Sim101.txt" in files[0]
    assert "_9999999" not in files[0] and "9999999" not in files[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
