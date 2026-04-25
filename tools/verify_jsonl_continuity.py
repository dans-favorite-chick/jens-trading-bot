"""
verify_jsonl_continuity.py — verify that a destination JSONL file is a
faithful continuation of (or identical copy of) a source JSONL file.

Use case: after robocopy'ing trade history from the dev PC to the Chicago
VPS during the Phoenix migration, run this against
  --source      <dev_pc_path>
  --destination <vps_path>
to prove the destination file is byte-equivalent on the tail and has the
same record count and last timestamp.

The script handles both strict JSONL (one JSON object per line) and
gracefully degrades for files that lack a `ts` field on every row — it
walks records bottom-up to find the most recent extractable timestamp,
and reports None if nothing usable is found.

Output: JSON document on stdout (or --out-json PATH) plus a
human-readable green/red summary on stderr. Pass --json for a stdout-only
machine-readable report (suppresses the human summary).

Exit codes:
  0  -- match (rows, first/last ts, last-N MD5, schema all agree)
  1  -- mismatch (one or more checks disagree)
  2  -- file or parse error (file missing, IO error, totally unparseable)

Usage:
  python tools/verify_jsonl_continuity.py \
      --source logs/trade_memory.json \
      --dest   \\\\vps\\Phoenix\\logs\\trade_memory.json \
      --rows   1000 \
      --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Number of trailing rows to MD5. 1000 is a sweet spot: enough to catch
# any byte-level corruption on the tail of the file, small enough to read
# in O(1) memory even for large logs.
MD5_TAIL_ROWS = 1000

# Fields, in order of preference, from which we extract a "last timestamp".
# trade_memory.json uses entry_time/exit_time (epoch seconds). Future
# JSONL streams may use a unified `ts` field. We try them all.
TS_FIELD_CANDIDATES = ("ts", "timestamp", "exit_time", "entry_time", "time")


class FileParseError(Exception):
    """Raised when a file cannot be opened or contains no parseable rows."""


def _iter_rows(path: Path):
    """
    Yield (line_number, parsed_dict) for each line in a JSONL file.

    Skips blank lines. Skips lines that fail to parse but logs a warning
    on stderr. If the entire file appears to be a single JSON array
    (Phoenix's current trade_memory.json format), falls back to loading
    the whole array and yielding its elements.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(1)
            if not head:
                return  # empty file -> no rows
            fh.seek(0)
            # Heuristic: if the first non-whitespace char is '[', treat as
            # a JSON array (legacy trade_memory.json shape).
            first_real = head
            # Read enough to decide
            peek = fh.read(64)
            fh.seek(0)
            if first_real == "[" or peek.lstrip().startswith("["):
                try:
                    data = json.load(fh)
                except json.JSONDecodeError as exc:
                    raise FileParseError(
                        f"{path}: not valid JSON array: {exc}"
                    ) from exc
                if not isinstance(data, list):
                    raise FileParseError(
                        f"{path}: top-level JSON is {type(data).__name__}, expected list"
                    )
                for i, row in enumerate(data, 1):
                    if isinstance(row, dict):
                        yield i, row
                return
            # Strict JSONL path
            for i, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    print(
                        f"[verify_jsonl_continuity] WARN: {path}:{i} skipped, "
                        f"unparseable JSON: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if isinstance(obj, dict):
                    yield i, obj
    except FileNotFoundError as exc:
        raise FileParseError(f"{path}: file not found") from exc
    except OSError as exc:
        raise FileParseError(f"{path}: OS error: {exc}") from exc


def _extract_ts(row: dict[str, Any]) -> float | None:
    """Pull a numeric timestamp from the row, trying known field names."""
    for key in TS_FIELD_CANDIDATES:
        val = row.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                # ISO-format date string; we don't bother parsing here —
                # the verifier just needs equality between source and dest.
                # Hash it into a stable float-ish surrogate? No — return
                # the string via a sentinel: we'll handle equality at the
                # caller level by also exposing the raw value.
                return None
    return None


def _row_ts(row: dict[str, Any]) -> tuple[float | None, str | None]:
    """Walk known fields; return (epoch_seconds, field_name) or (None, None)."""
    for field in TS_FIELD_CANDIDATES:
        val = row.get(field)
        if isinstance(val, (int, float)):
            return float(val), field
        if isinstance(val, str):
            try:
                return float(val), field
            except ValueError:
                continue
    return None, None


def _summarize(path: Path, tail_rows: int = MD5_TAIL_ROWS) -> dict[str, Any]:
    """
    Walk the file once, returning:
      {
        "row_count":           int,
        "first_ts":            float | None,
        "first_ts_field":      str | None,
        "first_row_keys":      list[str] | None,   # schema spot-check sample
        "last_ts":             float | None,
        "last_ts_field":       str | None,
        "md5_last_n":          str,                 # MD5 hex of canonicalized last N rows
        "md5_last_1000":       str,                 # back-compat alias
        "tail_rows_used":      int,
        "tail_rows_requested": int,
      }
    """
    rows: list[dict[str, Any]] = []
    count = 0
    first_ts: float | None = None
    first_ts_field: str | None = None
    first_row_keys: list[str] | None = None
    cap = max(1, int(tail_rows))

    for _, row in _iter_rows(path):
        if count == 0:
            first_row_keys = sorted(row.keys())
            first_ts, first_ts_field = _row_ts(row)
        rows.append(row)
        count += 1
        # Cap the in-memory tail to keep memory bounded.
        if len(rows) > cap:
            rows.pop(0)

    if count == 0:
        # Empty (but readable) file: distinct from "parse error".
        return {
            "row_count": 0,
            "first_ts": None,
            "first_ts_field": None,
            "first_row_keys": None,
            "last_ts": None,
            "last_ts_field": None,
            "md5_last_n": hashlib.md5(b"").hexdigest(),
            "md5_last_1000": hashlib.md5(b"").hexdigest(),
            "tail_rows_used": 0,
            "tail_rows_requested": cap,
        }

    # Find the most recent extractable ts walking the tail in reverse.
    last_ts: float | None = None
    last_ts_field: str | None = None
    for row in reversed(rows):
        ts, field = _row_ts(row)
        if ts is not None:
            last_ts, last_ts_field = ts, field
            break

    # Canonical-JSON the tail rows with sorted keys for byte-stable MD5.
    h = hashlib.md5()
    for row in rows:
        h.update(
            json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode(
                "ascii"
            )
        )
        h.update(b"\n")
    md5_hex = h.hexdigest()

    return {
        "row_count": count,
        "first_ts": first_ts,
        "first_ts_field": first_ts_field,
        "first_row_keys": first_row_keys,
        "last_ts": last_ts,
        "last_ts_field": last_ts_field,
        "md5_last_n": md5_hex,
        "md5_last_1000": md5_hex,
        "tail_rows_used": len(rows),
        "tail_rows_requested": cap,
    }


def _ts_equal(a: float | None, b: float | None) -> bool:
    """Both None == match; otherwise equal within 1 microsecond."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < 1e-6


def compare(
    source: Path,
    destination: Path,
    tail_rows: int = MD5_TAIL_ROWS,
) -> dict[str, Any]:
    """
    Build a structured comparison report. Does not raise on mismatch —
    callers inspect `report["match"]` to decide.
    Raises FileParseError if either file cannot be read.
    """
    src = _summarize(source, tail_rows=tail_rows)
    dst = _summarize(destination, tail_rows=tail_rows)

    rows_match = src["row_count"] == dst["row_count"]
    md5_match = src["md5_last_n"] == dst["md5_last_n"]
    first_ts_match = _ts_equal(src["first_ts"], dst["first_ts"])
    last_ts_match = _ts_equal(src["last_ts"], dst["last_ts"])
    # Schema spot-check on the first row: sorted key sets must match.
    schema_match = src["first_row_keys"] == dst["first_row_keys"]

    overall = (
        rows_match
        and md5_match
        and first_ts_match
        and last_ts_match
        and schema_match
    )

    return {
        "source": {"path": str(source), **src},
        "destination": {"path": str(destination), **dst},
        "checks": {
            "row_count_match": rows_match,
            "first_ts_match": first_ts_match,
            "last_ts_match": last_ts_match,
            "md5_last_n_match": md5_match,
            "md5_last_1000_match": md5_match,  # back-compat alias
            "schema_match": schema_match,
        },
        "tail_rows_requested": int(tail_rows),
        "match": overall,
    }


def _print_summary(report: dict[str, Any], use_color: bool) -> None:
    """Human-readable green/red summary on stderr."""
    if use_color:
        green = "\033[32m"
        red = "\033[31m"
        bold = "\033[1m"
        reset = "\033[0m"
    else:
        green = red = bold = reset = ""

    src = report["source"]
    dst = report["destination"]
    checks = report["checks"]

    def mark(ok: bool) -> str:
        return f"{green}OK{reset}" if ok else f"{red}FAIL{reset}"

    print(f"{bold}verify_jsonl_continuity{reset}", file=sys.stderr)
    print(f"  source      : {src['path']}", file=sys.stderr)
    print(f"  destination : {dst['path']}", file=sys.stderr)
    print(
        f"  rows        : src={src['row_count']:,} dst={dst['row_count']:,} "
        f"[{mark(checks['row_count_match'])}]",
        file=sys.stderr,
    )
    print(
        f"  schema_keys : src={src.get('first_row_keys')!r} dst={dst.get('first_row_keys')!r} "
        f"[{mark(checks.get('schema_match', True))}]",
        file=sys.stderr,
    )
    print(
        f"  first_ts    : src={src.get('first_ts')} dst={dst.get('first_ts')} "
        f"(field={src.get('first_ts_field')!r}/{dst.get('first_ts_field')!r}) "
        f"[{mark(checks.get('first_ts_match', True))}]",
        file=sys.stderr,
    )
    print(
        f"  last_ts     : src={src['last_ts']} dst={dst['last_ts']} "
        f"(field={src['last_ts_field']!r}/{dst['last_ts_field']!r}) "
        f"[{mark(checks['last_ts_match'])}]",
        file=sys.stderr,
    )
    n = report.get("tail_rows_requested", 1000)
    md5_src = src.get("md5_last_n", src.get("md5_last_1000", ""))
    md5_dst = dst.get("md5_last_n", dst.get("md5_last_1000", ""))
    print(
        f"  md5_last_{n:<4}: src={md5_src[:12]}... dst={md5_dst[:12]}... "
        f"[{mark(checks.get('md5_last_n_match', checks.get('md5_last_1000_match', False)))}]",
        file=sys.stderr,
    )
    overall = report["match"]
    color = green if overall else red
    print(
        f"  {bold}overall    : {color}{'MATCH' if overall else 'MISMATCH'}{reset}",
        file=sys.stderr,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="verify_jsonl_continuity",
        description="Compare two JSONL (or JSON-array) files for migration continuity.",
    )
    p.add_argument("--source", required=True, type=Path, help="Source JSONL path")
    # Accept both --dest and --destination so existing callers keep working.
    p.add_argument(
        "--dest",
        "--destination",
        dest="destination",
        required=True,
        type=Path,
        help="Destination JSONL path (alias: --destination)",
    )
    p.add_argument(
        "--rows",
        type=int,
        default=MD5_TAIL_ROWS,
        help=f"Number of trailing rows to MD5 (default {MD5_TAIL_ROWS}).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_only",
        help="Print machine-readable JSON to stdout and suppress the human summary.",
    )
    p.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="If set, write the JSON report here in addition to stdout.",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color in the stderr summary.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = compare(args.source, args.destination, tail_rows=args.rows)
    except FileParseError as exc:
        # Render an error JSON so automation can still parse it.
        err = {
            "error": str(exc),
            "match": False,
            "source": {"path": str(args.source)},
            "destination": {"path": str(args.destination)},
        }
        out = json.dumps(err, indent=2, sort_keys=True)
        if args.out_json:
            args.out_json.parent.mkdir(parents=True, exist_ok=True)
            args.out_json.write_text(out, encoding="utf-8")
        if args.json_only or not args.out_json:
            print(out)
        print(f"[verify_jsonl_continuity] ERROR: {exc}", file=sys.stderr)
        return 2

    out = json.dumps(report, indent=2, sort_keys=True)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(out, encoding="utf-8")
    if args.json_only or not args.out_json:
        print(out)

    if not args.json_only:
        use_color = (not args.no_color) and sys.stderr.isatty()
        _print_summary(report, use_color=use_color)

    return 0 if report["match"] else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
