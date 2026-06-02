"""
Phoenix Bot — NT8 Log Capture Helper

Reads NinjaTrader 8's trace/ folder (preferred) or outgoing/*.log files,
filters lines by a date + time-of-day window, and writes the filtered
slice to logs/oracle/research/<DATE>_nt8_log_capture.txt. Read-only:
this tool never deletes or modifies NT8 files.

Built 2026-06-02 to support the post-incident forensics workflow
(see logs/oracle/research/2026-06-02_chart_orders_root_cause.md). When
NT8 ATI goes silent during a session, the only ground truth for why
lives in NT8's own trace logs — Phoenix-side logs only show the
downstream PROTECT-FAILED cascade.

What this tool does:
  * Walks the NT8 trace/ directory (default
    C:\\Users\\Trading PC\\Documents\\NinjaTrader 8\\trace\\) for
    trace.YYYYMMDD.NNNNN.txt files whose date matches --date
  * Also scans NT8 outgoing/ for *.log files as a fallback
  * Line filter: each line whose timestamp prefix
    (YYYY-MM-DD HH:MM:SS:mmm) falls inside the [--start-time, --end-time]
    window on --date is kept. Continuation lines (no timestamp prefix)
    inherit the inclusion state of the prior parsed line.
  * Concatenates the result, prepended with a manifest header, into
    logs/oracle/research/<DATE>_nt8_log_capture.txt

Usage:
  python tools/nt8_log_capture.py
      Default: today, 08:00-12:00 CT, NT8 default Windows path.

  python tools/nt8_log_capture.py --date 2026-06-02 \\
      --start-time 09:00 --end-time 11:00
      Custom date + window.

  python tools/nt8_log_capture.py --nt8-root /custom/path --out other.txt
      Override the NT8 root and output filename.

  python tools/nt8_log_capture.py --help
      Print this help and exit.

Exit codes:
  0 — capture complete (output written; may be empty if no matches).
  1 — NT8 root or trace/ folder not found.
  2 — output target unwritable.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date as _date_t
from datetime import datetime, time
from pathlib import Path


DEFAULT_NT8_ROOT = Path(r"C:\Users\Trading PC\Documents\NinjaTrader 8")
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "logs" / "oracle" / "research"
DEFAULT_START = time(8, 0)
DEFAULT_END = time(12, 0)


# NT8 trace lines start with `YYYY-MM-DD HH:MM:SS:mmm`.
_TS_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})\s+"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?::\d{1,3})?\b"
)
# Filename date patterns we recognise.
_FN_DATE_RE = re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})")


@dataclass(frozen=True)
class CaptureSpec:
    target_date: _date_t
    start: time
    end: time
    nt8_root: Path
    out_path: Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Defaults: today (local), 08:00-12:00 CT, "
            f"NT8 root {DEFAULT_NT8_ROOT!s}."
        ),
    )
    parser.add_argument(
        "--date",
        default=None,
        help="YYYY-MM-DD date filter (default: today local).",
    )
    parser.add_argument(
        "--start-time",
        default=DEFAULT_START.strftime("%H:%M"),
        help="HH:MM lower bound, inclusive (default 08:00).",
    )
    parser.add_argument(
        "--end-time",
        default=DEFAULT_END.strftime("%H:%M"),
        help="HH:MM upper bound, inclusive (default 12:00).",
    )
    parser.add_argument(
        "--nt8-root",
        default=str(DEFAULT_NT8_ROOT),
        help=f"NT8 data root (default {DEFAULT_NT8_ROOT!s}).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "Output file path. Default: "
            "logs/oracle/research/<DATE>_nt8_log_capture.txt"
        ),
    )
    return parser.parse_args(argv)


def _spec_from_args(args: argparse.Namespace) -> CaptureSpec:
    target = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else datetime.now().date()
    )
    start = datetime.strptime(args.start_time, "%H:%M").time()
    end = datetime.strptime(args.end_time, "%H:%M").time()
    if end < start:
        raise SystemExit(
            f"--end-time {args.end_time} is before --start-time {args.start_time}"
        )
    root = Path(args.nt8_root)
    out = (
        Path(args.out)
        if args.out
        else DEFAULT_OUT_DIR / f"{target.isoformat()}_nt8_log_capture.txt"
    )
    return CaptureSpec(target, start, end, root, out)


def _matches_date(path: Path, target: _date_t) -> bool:
    """True if a YYYYMMDD substring in the filename matches target."""
    for m in _FN_DATE_RE.finditer(path.name):
        try:
            d = _date_t(int(m["y"]), int(m["mo"]), int(m["d"]))
        except ValueError:
            continue
        if d == target:
            return True
    return False


def _collect_sources(spec: CaptureSpec) -> list[Path]:
    """Return ordered list of NT8 source files that match the target date.

    Preference order: trace/, then outgoing/*.log (the brief explicitly
    asks for outgoing/ *.log files as a fallback).
    """
    sources: list[Path] = []
    trace_dir = spec.nt8_root / "trace"
    if trace_dir.is_dir():
        sources.extend(
            sorted(
                p
                for p in trace_dir.iterdir()
                if p.is_file() and _matches_date(p, spec.target_date)
            )
        )
    outgoing_dir = spec.nt8_root / "outgoing"
    if outgoing_dir.is_dir():
        sources.extend(
            sorted(
                p
                for p in outgoing_dir.iterdir()
                if p.is_file()
                and p.suffix.lower() == ".log"
                and _matches_date(p, spec.target_date)
            )
        )
    return sources


def _line_state(
    line: str, target: _date_t, start: time, end: time
) -> bool | None:
    """Return True/False if the line carries an in/out-of-window
    timestamp, or None when the line has no recognisable timestamp
    (a continuation of the previous line)."""
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        ts_date = _date_t(int(m["y"]), int(m["mo"]), int(m["d"]))
        ts_time = time(int(m["h"]), int(m["mi"]), int(m["s"]))
    except ValueError:
        return None
    if ts_date != target:
        return False
    return start <= ts_time <= end


def filter_file(path: Path, spec: CaptureSpec) -> list[str]:
    """Read one NT8 log file and return only the in-window lines.
    Continuation lines (no own timestamp) follow the prior line's state.
    """
    out: list[str] = []
    keep = False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [f"# (could not read {path.name}: {e})\n"]
    for line in text.splitlines(keepends=True):
        state = _line_state(line, spec.target_date, spec.start, spec.end)
        if state is True:
            keep = True
            out.append(line)
        elif state is False:
            keep = False
        else:
            if keep:
                out.append(line)
    return out


def run_capture(spec: CaptureSpec) -> int:
    """Execute the capture. Returns process exit code.

    Side effects: creates spec.out_path's parent if needed, writes the
    output file. Never modifies NT8 source files (open for reading only).
    """
    if not spec.nt8_root.exists():
        print(
            f"[nt8_log_capture] ERROR — NT8 root not found at {spec.nt8_root}",
            file=sys.stderr,
        )
        return 1
    trace_dir = spec.nt8_root / "trace"
    outgoing_dir = spec.nt8_root / "outgoing"
    if not trace_dir.is_dir() and not outgoing_dir.is_dir():
        print(
            f"[nt8_log_capture] ERROR — neither {trace_dir} nor "
            f"{outgoing_dir} exists",
            file=sys.stderr,
        )
        return 1
    sources = _collect_sources(spec)
    try:
        spec.out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(
            f"[nt8_log_capture] ERROR — cannot create output parent: {e}",
            file=sys.stderr,
        )
        return 2
    try:
        with spec.out_path.open("w", encoding="utf-8") as fh:
            fh.write(
                f"# NT8 Log Capture — {spec.target_date.isoformat()} "
                f"{spec.start.strftime('%H:%M')}-{spec.end.strftime('%H:%M')}\n"
                f"# Generated: {datetime.now().isoformat(timespec='seconds')}\n"
                f"# NT8 root: {spec.nt8_root}\n"
                f"# Sources scanned: {len(sources)}\n"
            )
            for src in sources:
                fh.write(f"\n# ── {src.name} ──\n")
                for line in filter_file(src, spec):
                    fh.write(line)
            if not sources:
                fh.write(
                    "\n# (no NT8 source files matched the date filter)\n"
                )
    except OSError as e:
        print(
            f"[nt8_log_capture] ERROR — output write failed: {e}",
            file=sys.stderr,
        )
        return 2
    print(
        f"[nt8_log_capture] wrote {spec.out_path} "
        f"(scanned {len(sources)} source file(s))"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    spec = _spec_from_args(args)
    return run_capture(spec)


if __name__ == "__main__":
    sys.exit(main())
