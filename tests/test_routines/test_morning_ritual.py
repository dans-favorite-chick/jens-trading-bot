"""Tests for tools/routines/morning_ritual.py — deterministic checks.

The morning_ritual verdict MUST be deterministic. AI commentary lives
in the appendix and is NOT part of the verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.routines.morning_ritual import (
    check_processes, check_ports, check_nt8_single_stream,
    check_mq_staleness, check_watcher_heartbeat, check_markers,
    run as run_morning_ritual,
)


class TestCheckProcesses:
    def test_all_alive_green(self):
        snap = {"processes": {f"p{i}.py": True for i in range(5)}}
        status, detail, _ = check_processes(snap)
        assert status == "GREEN"
        assert "5/5" in detail

    def test_one_missing_yellow(self):
        snap = {"processes": {"a": True, "b": True, "c": False}}
        status, _, _ = check_processes(snap)
        assert status == "YELLOW"

    def test_two_or_more_missing_red(self):
        snap = {"processes": {"a": False, "b": False, "c": True}}
        status, _, _ = check_processes(snap)
        assert status == "RED"


class TestCheckPorts:
    def test_all_listening_green(self):
        snap = {"ports": {8765: True, 8766: True, 8767: True}}
        status, _, _ = check_ports(snap)
        assert status == "GREEN"

    def test_two_missing_red(self):
        snap = {"ports": {8765: True, 8766: False, 8767: False}}
        status, _, _ = check_ports(snap)
        assert status == "RED"


class TestCheckNT8SingleStream:
    def test_one_client_green(self):
        snap = {
            "bridge_health": {
                "ok": True,
                "data": {
                    "nt8_instrument": "MNQM6",
                    "connection_events": [
                        {"message": "NT8 client connected from ('127.0.0.1', 52228)"},
                    ],
                },
            },
        }
        status, detail, _ = check_nt8_single_stream(snap)
        assert status == "GREEN"
        assert "52228" in detail

    def test_zero_clients_red(self):
        snap = {"bridge_health": {"ok": True, "data": {
            "nt8_instrument": "MNQM6", "connection_events": []
        }}}
        status, _, _ = check_nt8_single_stream(snap)
        assert status == "RED"

    def test_three_clients_yellow(self):
        events = [
            {"message": f"NT8 client connected from ('127.0.0.1', {p})"}
            for p in (52228, 52229, 52230)
        ]
        snap = {"bridge_health": {"ok": True, "data": {
            "nt8_instrument": "MNQM6", "connection_events": events
        }}}
        status, detail, _ = check_nt8_single_stream(snap)
        assert status == "YELLOW"
        assert "3" in detail

    def test_disconnects_subtract(self):
        events = [
            {"message": "NT8 client connected from ('127.0.0.1', 52228)"},
            {"message": "NT8 client connected from ('127.0.0.1', 52229)"},
            {"message": "NT8 disconnected from ('127.0.0.1', 52229)"},
        ]
        snap = {"bridge_health": {"ok": True, "data": {
            "nt8_instrument": "MNQM6", "connection_events": events
        }}}
        status, _, _ = check_nt8_single_stream(snap)
        assert status == "GREEN"

    def test_bridge_unreachable_red(self):
        snap = {"bridge_health": {"ok": False, "error": "boom"}}
        status, _, _ = check_nt8_single_stream(snap)
        assert status == "RED"


class TestCheckMQStaleness:
    """2026-05-06 Sprint J: check_mq_staleness now always returns GREEN
    (no-op stub — MenthorQ subscription retired). The original
    fresh/missing/stale tests no longer apply; this single test
    confirms the stub returns GREEN regardless of file state."""

    def test_always_green_after_retirement(self, tmp_path: Path):
        from tools.routines import morning_ritual as mr
        status, detail, _ = mr.check_mq_staleness()
        assert status == "GREEN"
        assert "retired" in detail.lower() or "skipped" in detail.lower()


class TestCheckMarkers:
    def test_no_markers_green(self):
        status, _, _ = check_markers({"halt_marker": False, "killswitch_marker": False})
        assert status == "GREEN"

    def test_killswitch_red(self):
        status, _, _ = check_markers({"halt_marker": False, "killswitch_marker": True})
        assert status == "RED"

    def test_halt_red(self):
        status, _, _ = check_markers({"halt_marker": True, "killswitch_marker": False})
        assert status == "RED"


class TestRunSmokeTest:
    def test_run_skip_ai_does_not_raise(self, tmp_path: Path, monkeypatch):
        """End-to-end smoke test — `run(skip_ai=True)` must complete and
        return a RoutineReport with at least one verdict_check, regardless
        of the actual stack state."""
        from tools.routines import _shared as shared
        monkeypatch.setattr(shared, "OUT_DIR", tmp_path)
        monkeypatch.setattr(shared, "DIGEST_QUEUE_PATH", tmp_path / "digest_queue.jsonl")
        report = run_morning_ritual(session_date="2026-04-25", skip_ai=True)
        assert report.name == "morning_ritual"
        assert report.session_date == "2026-04-25"
        assert len(report.verdict_checks) >= 5
        assert report.verdict in ("GREEN", "YELLOW", "RED")
