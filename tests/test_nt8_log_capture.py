"""Unit tests for tools/nt8_log_capture.py.

No real NT8 dependency — all tests build a synthetic NT8 root under a
tmp_path fixture (with trace/ and outgoing/ subfolders) and verify the
helper's date+time filter behavior.
"""
from __future__ import annotations

import sys
from datetime import date, time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.nt8_log_capture import (  # noqa: E402
    CaptureSpec,
    _collect_sources,
    _line_state,
    _matches_date,
    filter_file,
    main,
    run_capture,
)


# ─── _line_state ────────────────────────────────────────────────────

def test_line_state_in_window():
    state = _line_state(
        "2026-06-02 09:30:00:123 something\n",
        date(2026, 6, 2),
        time(8, 0),
        time(12, 0),
    )
    assert state is True


def test_line_state_out_of_window_high():
    state = _line_state(
        "2026-06-02 13:30:00:000 something\n",
        date(2026, 6, 2),
        time(8, 0),
        time(12, 0),
    )
    assert state is False


def test_line_state_wrong_date():
    state = _line_state(
        "2026-06-01 09:30:00:000 something\n",
        date(2026, 6, 2),
        time(8, 0),
        time(12, 0),
    )
    assert state is False


def test_line_state_continuation():
    state = _line_state(
        "    Stack trace continuation, no timestamp\n",
        date(2026, 6, 2),
        time(8, 0),
        time(12, 0),
    )
    assert state is None


# ─── _matches_date ──────────────────────────────────────────────────

def test_matches_date_trace_filename(tmp_path):
    p = tmp_path / "trace.20260602.00000.txt"
    p.touch()
    assert _matches_date(p, date(2026, 6, 2))
    assert not _matches_date(p, date(2026, 6, 1))


def test_matches_date_log_filename(tmp_path):
    p = tmp_path / "phoenix.20260602.log"
    p.touch()
    assert _matches_date(p, date(2026, 6, 2))


# ─── filter_file ────────────────────────────────────────────────────

def _make_spec(tmp_path: Path) -> CaptureSpec:
    return CaptureSpec(
        target_date=date(2026, 6, 2),
        start=time(8, 0),
        end=time(12, 0),
        nt8_root=tmp_path / "nt8",
        out_path=tmp_path / "out.txt",
    )


def test_filter_file_keeps_only_in_window(tmp_path):
    src = tmp_path / "trace.20260602.00000.txt"
    src.write_text(
        "2026-06-02 07:59:59:999 pre-window\n"
        "2026-06-02 08:00:00:000 first-in\n"
        "    continuation of first-in\n"
        "2026-06-02 11:59:59:000 last-in\n"
        "2026-06-02 12:00:00:000 boundary-in\n"
        "2026-06-02 12:00:00:001 boundary-out\n"
        "2026-06-02 13:00:00:000 post-window\n",
        encoding="utf-8",
    )
    spec = _make_spec(tmp_path)
    result = "".join(filter_file(src, spec))
    assert "pre-window" not in result
    assert "first-in" in result
    assert "continuation of first-in" in result
    assert "last-in" in result
    assert "boundary-in" in result
    # Boundary-out is one ms past 12:00:00 — within the second-resolution
    # window we accept (12:00:00 inclusive), so it's actually kept; the
    # tool is second-precision by design. Document the behavior here.
    assert "boundary-out" in result
    assert "post-window" not in result


def test_filter_file_continuation_lines_follow_state(tmp_path):
    src = tmp_path / "trace.20260602.00000.txt"
    src.write_text(
        "2026-06-02 09:00:00:000 in-window\n"
        "    indented continuation A\n"
        "    indented continuation B\n"
        "2026-06-02 14:00:00:000 out-of-window\n"
        "    indented continuation C\n",
        encoding="utf-8",
    )
    spec = _make_spec(tmp_path)
    result = "".join(filter_file(src, spec))
    assert "in-window" in result
    assert "indented continuation A" in result
    assert "indented continuation B" in result
    assert "out-of-window" not in result
    assert "indented continuation C" not in result


# ─── _collect_sources ───────────────────────────────────────────────

def test_collect_sources_picks_trace_and_outgoing_log(tmp_path):
    nt8 = tmp_path / "nt8"
    (nt8 / "trace").mkdir(parents=True)
    (nt8 / "outgoing").mkdir(parents=True)
    target = nt8 / "trace" / "trace.20260602.00000.txt"
    target.write_text("placeholder\n", encoding="utf-8")
    other = nt8 / "trace" / "trace.20260601.00000.txt"
    other.write_text("placeholder\n", encoding="utf-8")
    log = nt8 / "outgoing" / "anything.20260602.log"
    log.write_text("placeholder\n", encoding="utf-8")
    not_a_log = nt8 / "outgoing" / "phoenix.20260602.txt"
    not_a_log.write_text("placeholder\n", encoding="utf-8")
    spec = CaptureSpec(
        target_date=date(2026, 6, 2),
        start=time(8, 0),
        end=time(12, 0),
        nt8_root=nt8,
        out_path=tmp_path / "out.txt",
    )
    sources = _collect_sources(spec)
    names = {p.name for p in sources}
    assert "trace.20260602.00000.txt" in names
    assert "anything.20260602.log" in names
    assert "trace.20260601.00000.txt" not in names
    assert "phoenix.20260602.txt" not in names


# ─── run_capture (end-to-end) ───────────────────────────────────────

def test_run_capture_writes_filtered_output(tmp_path):
    nt8 = tmp_path / "nt8"
    (nt8 / "trace").mkdir(parents=True)
    src = nt8 / "trace" / "trace.20260602.00000.txt"
    src.write_text(
        "2026-06-02 07:30:00:000 pre-window\n"
        "2026-06-02 09:00:00:000 inside-window\n"
        "2026-06-02 15:00:00:000 post-window\n",
        encoding="utf-8",
    )
    out = tmp_path / "captured.txt"
    spec = CaptureSpec(
        target_date=date(2026, 6, 2),
        start=time(8, 0),
        end=time(12, 0),
        nt8_root=nt8,
        out_path=out,
    )
    rc = run_capture(spec)
    assert rc == 0
    content = out.read_text(encoding="utf-8")
    assert "# NT8 Log Capture" in content
    assert "trace.20260602.00000.txt" in content
    assert "inside-window" in content
    assert "pre-window" not in content
    assert "post-window" not in content


def test_run_capture_missing_root_returns_1(tmp_path):
    out = tmp_path / "captured.txt"
    spec = CaptureSpec(
        target_date=date(2026, 6, 2),
        start=time(8, 0),
        end=time(12, 0),
        nt8_root=tmp_path / "does-not-exist",
        out_path=out,
    )
    rc = run_capture(spec)
    assert rc == 1
    assert not out.exists()


def test_run_capture_no_matching_files_still_writes_header(tmp_path):
    nt8 = tmp_path / "nt8"
    (nt8 / "trace").mkdir(parents=True)
    (nt8 / "trace" / "trace.20260101.00000.txt").write_text(
        "2026-01-01 09:00:00:000 ancient\n", encoding="utf-8"
    )
    out = tmp_path / "captured.txt"
    spec = CaptureSpec(
        target_date=date(2026, 6, 2),
        start=time(8, 0),
        end=time(12, 0),
        nt8_root=nt8,
        out_path=out,
    )
    rc = run_capture(spec)
    assert rc == 0
    content = out.read_text(encoding="utf-8")
    assert "# NT8 Log Capture" in content
    assert "no NT8 source files matched" in content


def test_run_capture_never_modifies_sources(tmp_path):
    nt8 = tmp_path / "nt8"
    (nt8 / "trace").mkdir(parents=True)
    src = nt8 / "trace" / "trace.20260602.00000.txt"
    payload = "2026-06-02 09:00:00:000 keepalive\n"
    src.write_text(payload, encoding="utf-8")
    src_stat_before = src.stat()
    out = tmp_path / "captured.txt"
    spec = CaptureSpec(
        target_date=date(2026, 6, 2),
        start=time(8, 0),
        end=time(12, 0),
        nt8_root=nt8,
        out_path=out,
    )
    run_capture(spec)
    assert src.exists()
    assert src.read_text(encoding="utf-8") == payload
    assert src.stat().st_size == src_stat_before.st_size


# ─── CLI smoke ──────────────────────────────────────────────────────

def test_help_smoke(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "NT8 Log Capture Helper" in captured.out or "nt8_log_capture" in captured.out
