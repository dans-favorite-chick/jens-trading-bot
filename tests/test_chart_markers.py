"""Tests for core.nt8_chart_markers.ChartMarkerWriter.

Pins:
  - Each record_* method writes ONE valid JSONL line.
  - Concurrent writes from multiple threads never interleave.
  - I/O failure (read-only path) does NOT raise — caller is protected.
  - Strategy color/symbol auto-resolved from config/strategy_visuals.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core.nt8_chart_markers import ChartMarkerWriter


@pytest.fixture(autouse=True)
def _reset_warn_cache():
    ChartMarkerWriter._reset_warn_cache()
    yield
    ChartMarkerWriter._reset_warn_cache()


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_record_entry_writes_one_complete_line(tmp_path: Path) -> None:
    p = tmp_path / "incoming" / "phoenix_markers.jsonl"
    w = ChartMarkerWriter(path=str(p))
    w.record_entry(
        trade_id="t1", strategy="bias_momentum", direction="LONG",
        entry_price=20100.25, stop=20080.0, target=20140.0, ts=1700000000.5,
    )
    rows = _read_lines(p)
    assert len(rows) == 1
    r = rows[0]
    assert r["event"] == "entry"
    assert r["trade_id"] == "t1"
    assert r["strategy"] == "bias_momentum"
    assert r["direction"] == "LONG"
    assert r["entry_price"] == 20100.25
    assert r["stop"] == 20080.0
    assert r["target"] == 20140.0
    assert r["color"] == "#3fb950"   # bias_momentum visual
    assert r["symbol"] == "triangleUp"
    assert r["ts"] == 1700000000.5


def test_record_exit_writes_complete_line(tmp_path: Path) -> None:
    p = tmp_path / "incoming" / "phoenix_markers.jsonl"
    w = ChartMarkerWriter(path=str(p))
    w.record_exit(
        trade_id="t1", exit_price=20140.5, exit_reason="target_hit",
        pnl=80.5, ts=1700000300.0,
    )
    rows = _read_lines(p)
    assert len(rows) == 1
    assert rows[0] == {
        "event": "exit", "trade_id": "t1", "exit_price": 20140.5,
        "exit_reason": "target_hit", "pnl": 80.5, "ts": 1700000300.0,
    }


def test_stop_and_target_updates(tmp_path: Path) -> None:
    p = tmp_path / "incoming" / "phoenix_markers.jsonl"
    w = ChartMarkerWriter(path=str(p))
    w.record_stop_update("t1", 20085.0, ts=1700000100.0)
    w.record_target_update("t1", 20150.0, ts=1700000150.0)
    rows = _read_lines(p)
    assert len(rows) == 2
    assert rows[0]["event"] == "stop_update"
    assert rows[0]["stop"] == 20085.0
    assert rows[1]["event"] == "target_update"
    assert rows[1]["target"] == 20150.0


def test_unknown_strategy_falls_back_to_gray(tmp_path: Path) -> None:
    p = tmp_path / "incoming" / "phoenix_markers.jsonl"
    w = ChartMarkerWriter(path=str(p))
    w.record_entry(
        trade_id="x", strategy="not_a_real_strategy", direction="SHORT",
        entry_price=100.0, stop=110.0, target=80.0,
    )
    rows = _read_lines(p)
    assert rows[0]["color"] == "#8b949e"
    assert rows[0]["symbol"] == "circle"


def test_concurrent_writes_do_not_interleave(tmp_path: Path) -> None:
    """Multi-threaded record_entry calls must produce well-formed JSONL —
    each line must parse independently. If the lock fails, we get
    partial/interleaved bytes and json.loads raises."""
    p = tmp_path / "incoming" / "phoenix_markers.jsonl"
    w = ChartMarkerWriter(path=str(p))
    N_THREADS = 12
    PER_THREAD = 25

    def _writer(thread_idx: int) -> None:
        for i in range(PER_THREAD):
            w.record_entry(
                trade_id=f"t{thread_idx}_{i}",
                strategy="bias_momentum",
                direction=("LONG" if i % 2 == 0 else "SHORT"),
                entry_price=20000.0 + thread_idx,
                stop=19990.0 + thread_idx,
                target=20020.0 + thread_idx,
            )

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    rows = _read_lines(p)
    assert len(rows) == N_THREADS * PER_THREAD, \
        "every record_entry call must produce exactly one parsable line"
    ids = {r["trade_id"] for r in rows}
    assert len(ids) == N_THREADS * PER_THREAD


def test_io_failure_does_not_raise(tmp_path: Path, caplog) -> None:
    """If the path is unwritable, methods must NOT raise — the trading
    pipeline is the priority. Test by pointing at a path whose dir we
    have made read-only-ish by NOT having permission to create."""
    # Pick a path under a nonexistent + non-creatable root. On Windows
    # the surest way is to use an empty drive letter.
    bad_path = "Z:\\nonexistent\\incoming\\phoenix_markers.jsonl"
    w = ChartMarkerWriter(path=bad_path)
    import logging
    with caplog.at_level(logging.WARNING, logger="ChartMarkers"):
        w.record_entry(
            trade_id="t1", strategy="bias_momentum", direction="LONG",
            entry_price=100.0, stop=99.0, target=102.0,
        )
        w.record_entry(
            trade_id="t2", strategy="bias_momentum", direction="LONG",
            entry_price=100.0, stop=99.0, target=102.0,
        )
    # Survives without raising; WARN fires ONCE (not twice).
    warns = [m for m in caplog.messages if "write failed" in m]
    assert len(warns) == 1, f"expected 1 dedup'd WARN, got {len(warns)}"


def test_lazy_dir_creation(tmp_path: Path) -> None:
    """incoming/ dir should be created on construction; if deleted between
    events the next write should re-create it."""
    p = tmp_path / "incoming" / "phoenix_markers.jsonl"
    w = ChartMarkerWriter(path=str(p))
    assert p.parent.exists()
    # Remove + re-write
    if p.exists():
        p.unlink()
    p.parent.rmdir()
    assert not p.parent.exists()
    w.record_stop_update("t1", 100.0)
    assert p.parent.exists()
    assert p.exists()


def test_get_chart_markers_singleton() -> None:
    from core.nt8_chart_markers import get_chart_markers
    a = get_chart_markers()
    b = get_chart_markers()
    assert a is b
