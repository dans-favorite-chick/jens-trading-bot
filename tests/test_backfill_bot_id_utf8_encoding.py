"""L1 — tools/backfill_bot_id.py reads the JSON file as UTF-8.

Subagent B 2026-06-03 (LOW) flagged the implicit cp1252 default-encoding
read paired with the explicit utf-8 write. JSON's \\uXXXX escapes mean
ASCII round-trips fine, but a non-ASCII byte in the source file would
be misread under Windows' default cp1252 encoding. The fix adds
``encoding="utf-8"`` to the read call.

Two layers of verification:
  1. Source-level: the read call in the file literally includes
     ``encoding="utf-8"``.
  2. Behavioural: a non-ASCII bot_id round-trips through the tool.

Run: pytest tests/test_backfill_bot_id_utf8_encoding.py -v
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "backfill_bot_id.py"


# ─── Assertion 1: source-level — read call uses encoding="utf-8" ─────


def test_backfill_read_uses_explicit_utf8_encoding():
    """The ``path.open`` (or any read) call that loads the JSON file
    MUST include ``encoding="utf-8"``. The implicit default on Windows
    is cp1252, which would misread non-ASCII bytes."""
    src = SCRIPT.read_text(encoding="utf-8")

    # The script's read pattern is:
    #     with path.open("r", encoding="utf-8") as f:
    #         trades = json.load(f)
    # Allow some whitespace flexibility but require the encoding kwarg
    # on the read call that feeds json.load.
    read_pattern = re.compile(
        r"path\.open\(\s*[\"']r[\"']\s*,\s*encoding\s*=\s*[\"']utf-8[\"']\s*\)",
        re.IGNORECASE,
    )
    assert read_pattern.search(src), (
        "tools/backfill_bot_id.py read call is missing explicit "
        'encoding="utf-8" — implicit cp1252 on Windows would silently '
        "misread non-ASCII bytes."
    )

    # And there must NOT be a bare path.open("r") (positional, no
    # encoding) in the file — that would be the regression path.
    bare_pattern = re.compile(
        r"path\.open\(\s*[\"']r[\"']\s*\)",
    )
    assert not bare_pattern.search(src), (
        "Found a bare path.open(\"r\") without encoding= — the L1 "
        "regression is back."
    )


# ─── Assertion 2: behavioural — non-ASCII bot_id round-trips ─────────


def test_non_ascii_bot_id_round_trips_through_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A non-ASCII bot_id ("Sim🦅") must survive the read + write cycle
    intact. JSON escapes the character to \\uXXXX in the on-disk
    representation, but the logical value through Python str must
    round-trip unchanged."""
    tm_path = tmp_path / "trade_memory.json"

    # Pre-existing row with the non-ASCII bot_id (already populated, so
    # backfill doesn't overwrite it). A second row is missing bot_id —
    # backfill should fill it, proving the script ran end-to-end.
    initial = [
        {
            "trade_id": "T-NONASCII-1",
            "bot_id": "Sim🦅",
            "account": "Sim101",
            "recorded_at": "2026-04-22T10:00:00",
        },
        {
            "trade_id": "T-FILL-2",
            "bot_id": None,
            "account": "Sim101",
            "recorded_at": "2026-05-01T10:00:00",
        },
    ]
    # ensure_ascii=False so the on-disk bytes contain the actual UTF-8
    # encoding of "Sim🦅". Without explicit encoding= on the read, the
    # tool would misread those bytes under cp1252 on Windows.
    tm_path.write_text(
        json.dumps(initial, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys, "argv",
        ["backfill_bot_id.py", "--file", str(tm_path)],
    )

    from tools.backfill_bot_id import main

    rc = main()
    assert rc == 0, f"backfill main() returned {rc}, expected 0"

    final = json.loads(tm_path.read_text(encoding="utf-8"))
    assert isinstance(final, list) and len(final) == 2

    # The pre-existing non-ASCII bot_id survived intact (logical value).
    assert final[0]["trade_id"] == "T-NONASCII-1"
    assert final[0]["bot_id"] == "Sim🦅", (
        f"non-ASCII bot_id corrupted through round-trip: "
        f"{final[0]['bot_id']!r}"
    )

    # The missing bot_id row got backfilled — proves the script
    # actually ran the write path and didn't bail early.
    assert final[1]["trade_id"] == "T-FILL-2"
    assert final[1]["bot_id"] == "prod", (
        f"expected post-phase-c Sim101 to classify as 'prod', got "
        f"{final[1]['bot_id']!r}"
    )
