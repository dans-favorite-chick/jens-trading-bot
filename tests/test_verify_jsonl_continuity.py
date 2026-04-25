"""
Tests for tools/verify_jsonl_continuity.py.

We exercise:
  - Two matching strict-JSONL files → continuity passes.
  - Destination row mutated → continuity fails on MD5 (and possibly ts).
  - Empty files match.
  - Missing destination file → FileParseError surface (exit code 2 path).
  - JSON-array fallback (Phoenix's current trade_memory.json shape).
  - Missing `ts` field → last_ts_field is the next preferred candidate
    (exit_time / entry_time) and reports correctly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make tools/ importable when pytest runs from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import verify_jsonl_continuity as vjc  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _sample_rows(n: int = 5, base_ts: float = 1_700_000_000.0) -> list[dict]:
    return [
        {"ts": base_ts + i, "trade_id": f"t{i:04d}", "pnl_dollars": float(i) - 2.0}
        for i in range(n)
    ]


def test_matching_jsonl_passes(tmp_path: Path) -> None:
    rows = _sample_rows(5)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows)

    report = vjc.compare(src, dst)

    assert report["match"] is True
    assert report["checks"]["row_count_match"] is True
    assert report["checks"]["last_ts_match"] is True
    assert report["checks"]["md5_last_1000_match"] is True
    assert report["source"]["row_count"] == 5
    assert report["destination"]["row_count"] == 5
    assert report["source"]["last_ts"] == rows[-1]["ts"]
    assert report["source"]["last_ts_field"] == "ts"


def test_modified_destination_fails(tmp_path: Path) -> None:
    rows = _sample_rows(5)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    # Destination has the same number of rows but one row's pnl is corrupted.
    rows_dst = [dict(r) for r in rows]
    rows_dst[2]["pnl_dollars"] = 999.99
    _write_jsonl(dst, rows_dst)

    report = vjc.compare(src, dst)

    assert report["match"] is False
    # Row count still matches (we only mutated, didn't add/remove)...
    assert report["checks"]["row_count_match"] is True
    # ...and last_ts is unchanged...
    assert report["checks"]["last_ts_match"] is True
    # ...but the MD5 of the tail diverges.
    assert report["checks"]["md5_last_1000_match"] is False


def test_main_exit_codes_on_match_and_mismatch(tmp_path: Path) -> None:
    rows = _sample_rows(3)
    src = tmp_path / "src.jsonl"
    dst_match = tmp_path / "dst_match.jsonl"
    dst_diff = tmp_path / "dst_diff.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst_match, rows)
    rows_diff = [dict(r) for r in rows]
    rows_diff[-1]["trade_id"] = "MUTATED"
    _write_jsonl(dst_diff, rows_diff)

    report_path_match = tmp_path / "match.json"
    report_path_diff = tmp_path / "diff.json"

    rc_match = vjc.main(
        [
            "--source",
            str(src),
            "--destination",
            str(dst_match),
            "--out-json",
            str(report_path_match),
            "--no-color",
        ]
    )
    rc_diff = vjc.main(
        [
            "--source",
            str(src),
            "--destination",
            str(dst_diff),
            "--out-json",
            str(report_path_diff),
            "--no-color",
        ]
    )

    assert rc_match == 0
    assert rc_diff == 1
    # Reports are valid JSON and reflect their respective verdicts.
    assert json.loads(report_path_match.read_text(encoding="utf-8"))["match"] is True
    assert json.loads(report_path_diff.read_text(encoding="utf-8"))["match"] is False


def test_missing_file_returns_parse_error_exit(tmp_path: Path) -> None:
    src = tmp_path / "src.jsonl"
    _write_jsonl(src, _sample_rows(1))
    missing = tmp_path / "does_not_exist.jsonl"

    rc = vjc.main(
        [
            "--source",
            str(src),
            "--destination",
            str(missing),
            "--no-color",
        ]
    )
    assert rc == 2


def test_json_array_fallback_matches_jsonl(tmp_path: Path) -> None:
    """Phoenix's trade_memory.json is a JSON array; verifier must handle it."""
    rows = _sample_rows(4)
    array_path = tmp_path / "trade_memory.json"
    array_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    jsonl_path = tmp_path / "trade_memory.jsonl"
    _write_jsonl(jsonl_path, rows)

    report = vjc.compare(array_path, jsonl_path)

    # Same rows, same content, same MD5 — array vs JSONL parsing should
    # yield identical canonicalized hashes.
    assert report["checks"]["row_count_match"] is True
    assert report["checks"]["last_ts_match"] is True
    assert report["checks"]["md5_last_1000_match"] is True
    assert report["match"] is True


def test_missing_ts_falls_back_to_exit_time(tmp_path: Path) -> None:
    """If no `ts`, verifier should pick up `exit_time` (Phoenix's existing field)."""
    rows = [
        {"trade_id": "a", "exit_time": 1_770_000_000.0},
        {"trade_id": "b", "exit_time": 1_770_000_060.0},
    ]
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows)

    report = vjc.compare(src, dst)

    assert report["match"] is True
    assert report["source"]["last_ts"] == 1_770_000_060.0
    assert report["source"]["last_ts_field"] == "exit_time"


def test_no_extractable_ts_anywhere(tmp_path: Path) -> None:
    """Rows with no recognized timestamp field — last_ts is None on both sides
    and the equality check still passes (None == None)."""
    rows = [{"trade_id": "x"}, {"trade_id": "y"}]
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows)

    report = vjc.compare(src, dst)
    assert report["source"]["last_ts"] is None
    assert report["destination"]["last_ts"] is None
    assert report["checks"]["last_ts_match"] is True
    assert report["match"] is True


def test_empty_files_match(tmp_path: Path) -> None:
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    src.write_text("", encoding="utf-8")
    dst.write_text("", encoding="utf-8")

    report = vjc.compare(src, dst)
    assert report["match"] is True
    assert report["source"]["row_count"] == 0
    assert report["destination"]["row_count"] == 0


def test_extra_row_in_destination_fails(tmp_path: Path) -> None:
    rows = _sample_rows(3)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows + [{"ts": rows[-1]["ts"] + 1, "trade_id": "extra"}])

    report = vjc.compare(src, dst)
    assert report["match"] is False
    assert report["checks"]["row_count_match"] is False
    assert report["checks"]["md5_last_1000_match"] is False


@pytest.mark.parametrize("n_rows", [1, 100, 1500])  # exercise tail-cap behavior
def test_md5_tail_cap_handles_various_sizes(tmp_path: Path, n_rows: int) -> None:
    rows = _sample_rows(n_rows)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows)
    report = vjc.compare(src, dst)
    assert report["match"] is True
    expected_tail = min(n_rows, vjc.MD5_TAIL_ROWS)
    assert report["source"]["tail_rows_used"] == expected_tail
    assert report["destination"]["tail_rows_used"] == expected_tail


# ---------------------------------------------------------------------------
# Spec-aligned tests for Phase B+ Section 5 (Chicago VPS migration).
# These mirror the four scenarios called out in the build spec:
#   1) identical files                        -> exit 0, all checks match
#   2) destination missing last 5 rows        -> exit 1, row count + MD5 differ
#   3) timestamp drift in dest's last row     -> last_ts_match flagged
#   4) file not found                         -> exit 2 + helpful error string
# ---------------------------------------------------------------------------


def test_spec_identical_files_exit_0(tmp_path: Path) -> None:
    rows = _sample_rows(50)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows)

    rc = vjc.main(
        [
            "--source",
            str(src),
            "--dest",  # spec uses --dest (alias of --destination)
            str(dst),
            "--rows",
            "1000",
            "--json",
            "--no-color",
        ]
    )
    assert rc == 0


def test_spec_dest_missing_last_5_rows_exit_1(tmp_path: Path) -> None:
    rows = _sample_rows(20)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows[:-5])  # destination is missing the last 5 rows

    report = vjc.compare(src, dst)
    assert report["match"] is False
    # Row count must differ.
    assert report["checks"]["row_count_match"] is False
    assert report["source"]["row_count"] == 20
    assert report["destination"]["row_count"] == 15
    # MD5 of trailing window must differ.
    assert report["checks"]["md5_last_n_match"] is False
    # Exit code path through main() should also be 1.
    rc = vjc.main(["--source", str(src), "--dest", str(dst), "--no-color", "--json"])
    assert rc == 1


def test_spec_timestamp_drift_in_dest_last_row(tmp_path: Path) -> None:
    rows = _sample_rows(10)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)

    # Same row count, same first row, but the last row's `ts` drifts by 30s.
    rows_dst = [dict(r) for r in rows]
    rows_dst[-1]["ts"] = float(rows[-1]["ts"]) + 30.0
    _write_jsonl(dst, rows_dst)

    report = vjc.compare(src, dst)
    assert report["match"] is False
    assert report["checks"]["row_count_match"] is True  # same N
    assert report["checks"]["last_ts_match"] is False   # drift flagged
    # MD5 also flips because the bytes changed, but the spec specifically
    # asks us to surface the timestamp drift, which we do.
    assert report["source"]["last_ts"] != report["destination"]["last_ts"]


def test_spec_file_not_found_exit_2(tmp_path: Path, capsys) -> None:
    src = tmp_path / "src.jsonl"
    _write_jsonl(src, _sample_rows(2))
    missing = tmp_path / "absent_destination.jsonl"

    rc = vjc.main(
        [
            "--source",
            str(src),
            "--dest",
            str(missing),
            "--no-color",
            "--json",
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    # Helpful error mentions the path and "not found".
    assert "not found" in captured.err.lower()
    assert str(missing) in captured.err or "absent_destination" in captured.err


# ---------------------------------------------------------------------------
# Additional sanity checks for the new --rows flag and schema spot-check.
# ---------------------------------------------------------------------------


def test_rows_flag_changes_tail_window(tmp_path: Path) -> None:
    rows = _sample_rows(100)
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, rows)
    _write_jsonl(dst, rows)
    report = vjc.compare(src, dst, tail_rows=10)
    assert report["match"] is True
    assert report["source"]["tail_rows_used"] == 10
    assert report["destination"]["tail_rows_used"] == 10
    assert report["tail_rows_requested"] == 10


def test_schema_mismatch_first_row(tmp_path: Path) -> None:
    """First row's field names differ -> schema_match flagged."""
    src_rows = [{"ts": 1.0, "trade_id": "a", "pnl_dollars": 1.0}]
    dst_rows = [{"ts": 1.0, "trade_id": "a", "pnl": 1.0}]  # different key
    src = tmp_path / "src.jsonl"
    dst = tmp_path / "dst.jsonl"
    _write_jsonl(src, src_rows)
    _write_jsonl(dst, dst_rows)
    report = vjc.compare(src, dst)
    assert report["match"] is False
    assert report["checks"]["schema_match"] is False
