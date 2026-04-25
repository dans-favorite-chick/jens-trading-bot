"""Tests for tools/routines/_shared.py — RoutineReport + DigestQueue + helpers.

Per Jennifer 2026-04-25:
  - Verdict determinism: AI commentary must NOT influence the verdict.
  - DigestQueue: file-backed FIFO; drainable atomically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.routines._shared import (
    RoutineReport, DigestQueue, write_artifacts,
)


class TestVerdictDeterminism:
    """The verdict comes from `verdict_checks` ONLY. AI appendix changes
    nothing. This is Jennifer's amendment in code form."""

    def test_empty_checks_default_green(self):
        r = RoutineReport(name="x", session_date="2026-04-25")
        assert r.verdict == "GREEN"

    def test_red_beats_yellow_beats_green(self):
        r = RoutineReport(name="x", session_date="2026-04-25")
        r.set_verdict_check("a", "GREEN", "all good")
        r.set_verdict_check("b", "YELLOW", "warn")
        assert r.verdict == "YELLOW"
        r.set_verdict_check("c", "RED", "broken")
        assert r.verdict == "RED"

    def test_ai_appendix_does_not_affect_verdict(self):
        """KEY INVARIANT — Jennifer's amendment. AI commentary, even if it
        sounds catastrophic, MUST NOT downgrade a GREEN verdict."""
        r = RoutineReport(name="x", session_date="2026-04-25")
        r.set_verdict_check("a", "GREEN", "all good")
        assert r.verdict == "GREEN"
        r.set_ai_appendix("CATASTROPHIC OVERNIGHT MELTDOWN — DO NOT TRADE")
        assert r.verdict == "GREEN", "AI appendix must NOT change verdict"

    def test_ai_appendix_renders_with_advisory_label(self):
        r = RoutineReport(name="x", session_date="2026-04-25")
        r.set_ai_appendix("foo bar")
        md = r.to_markdown()
        assert "advisory only" in md
        assert "does not affect verdict" in md

    def test_set_verdict_check_overwrites_duplicate_name(self):
        r = RoutineReport(name="x", session_date="2026-04-25")
        r.set_verdict_check("a", "GREEN", "first")
        r.set_verdict_check("a", "RED", "second")
        assert len(r.verdict_checks) == 1
        assert r.verdict_checks[0].status == "RED"


class TestDigestQueue:
    """File-backed FIFO. drain() must be atomic — concurrent push during
    drain may land in the queue but won't be lost AND won't be doubly-read."""

    def test_push_and_peek_roundtrip(self, tmp_path: Path):
        q = DigestQueue(path=tmp_path / "queue.jsonl")
        q.push({"a": 1})
        q.push({"a": 2})
        items = q.peek()
        assert len(items) == 2
        assert items[0]["a"] == 1
        assert items[1]["a"] == 2

    def test_drain_clears_queue(self, tmp_path: Path):
        q = DigestQueue(path=tmp_path / "queue.jsonl")
        q.push({"a": 1})
        q.push({"a": 2})
        items = q.drain()
        assert len(items) == 2
        # Subsequent peek returns empty
        assert q.peek() == []

    def test_drain_empty_queue_returns_empty(self, tmp_path: Path):
        q = DigestQueue(path=tmp_path / "queue.jsonl")
        assert q.drain() == []
        assert q.peek() == []

    def test_corrupt_lines_skipped(self, tmp_path: Path):
        path = tmp_path / "queue.jsonl"
        path.write_text('{"good":1}\nNOT JSON\n{"good":2}\n', encoding="utf-8")
        q = DigestQueue(path=path)
        items = q.peek()
        assert len(items) == 2
        assert items[0]["good"] == 1
        assert items[1]["good"] == 2


class TestWriteArtifacts:
    def test_writes_md_html_and_enqueues(self, tmp_path: Path, monkeypatch):
        # Redirect OUT_DIR + DIGEST_QUEUE_PATH at module level
        from tools.routines import _shared as shared
        monkeypatch.setattr(shared, "OUT_DIR", tmp_path)
        monkeypatch.setattr(shared, "DIGEST_QUEUE_PATH", tmp_path / "digest_queue.jsonl")
        r = RoutineReport(name="test_routine", session_date="2026-04-25")
        r.set_verdict_check("c1", "GREEN", "all good")
        paths = write_artifacts(r, also_pdf=False)
        assert paths["markdown"].exists()
        assert paths["html"].exists()
        # Markdown contains verdict + check
        md = paths["markdown"].read_text(encoding="utf-8")
        assert "GREEN" in md
        assert "c1" in md
        # Digest queue got the entry
        q_items = DigestQueue(path=tmp_path / "digest_queue.jsonl").peek()
        assert len(q_items) == 1
        assert q_items[0]["routine"] == "test_routine"
        assert q_items[0]["verdict"] == "GREEN"


class TestStackHealthSnapshot:
    def test_returns_expected_shape(self):
        from tools.routines._shared import stack_health_snapshot
        snap = stack_health_snapshot()
        # Top-level keys
        for k in ("processes", "ports", "bridge_health", "halt_marker",
                  "killswitch_marker", "watcher_heartbeat_age_s", "ts"):
            assert k in snap, f"missing key {k}"
        # processes is dict[str, bool]
        assert isinstance(snap["processes"], dict)
        assert all(isinstance(v, bool) for v in snap["processes"].values())
        # ports is dict[int, bool]
        assert isinstance(snap["ports"], dict)
