"""tests/test_memory_writeback.py

Regression tests for the 2026-05-25 SessionEnd duplicate-append bug.

Background:
  The SessionEnd hook can fire multiple times in rapid succession (context
  compaction, session resume, etc.). Before the fix, each fire appended an
  identical "Session changes: N files modified" block to
  memory/context/RECENT_CHANGES.md and an identical event to
  memory/audit_log.jsonl. On 2026-05-25 alone this produced 8 effectively-
  identical blocks within seconds.

Fix:
  tools/memory_writeback.py now content-hashes the incoming entry against the
  most-recent existing block (RECENT_CHANGES) and against the last line of
  audit_log.jsonl. Identical hash -> skip the write.

These tests confirm the dedupe contract and verify that legitimate changes
(different summary, different file list, different decisions) still produce
fresh entries.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PHOENIX_ROOT = Path(__file__).resolve().parent.parent
TOOL = PHOENIX_ROOT / "tools" / "memory_writeback.py"


# ─── helpers ──────────────────────────────────────────────────────────


def _import_memory_writeback_with_root(tmp_root: Path):
    """Re-import tools.memory_writeback with its PHOENIX_ROOT / MEMORY_DIR
    / etc. constants pointed at tmp_root. Returns the freshly-loaded module."""
    sys.path.insert(0, str(PHOENIX_ROOT))
    mod_name = "tools.memory_writeback"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    module = importlib.import_module(mod_name)
    # Repoint module-level paths so the test never touches the real repo.
    module.PHOENIX_ROOT = tmp_root
    module.MEMORY_DIR = tmp_root / "memory"
    module.CONTEXT_DIR = module.MEMORY_DIR / "context"
    module.AUDIT_LOG = module.MEMORY_DIR / "audit_log.jsonl"
    module.LOCK_FILE = module.MEMORY_DIR / ".lock"
    module.CURRENT_STATE = module.CONTEXT_DIR / "CURRENT_STATE.md"
    module.RECENT_CHANGES = module.CONTEXT_DIR / "RECENT_CHANGES.md"
    (module.MEMORY_DIR).mkdir(parents=True, exist_ok=True)
    (module.CONTEXT_DIR).mkdir(parents=True, exist_ok=True)
    return module


# ─── unit-level dedupe tests ─────────────────────────────────────────


def test_append_to_recent_changes_writes_first_block(tmp_path):
    m = _import_memory_writeback_with_root(tmp_path)
    wrote = m.append_to_recent_changes(
        summary="Session changes: 5 files modified",
        changed_files=["a.py", "b.py"],
        decisions=[],
    )
    assert wrote is True
    body = m.RECENT_CHANGES.read_text(encoding="utf-8")
    assert "Session changes: 5 files modified" in body
    assert body.count("Session changes: 5 files modified") == 1


def test_append_to_recent_changes_dedupes_identical_second_call(tmp_path):
    m = _import_memory_writeback_with_root(tmp_path)
    payload = dict(
        summary="Session changes: 5 files modified",
        changed_files=["a.py", "b.py"],
        decisions=[],
    )
    first = m.append_to_recent_changes(**payload)
    second = m.append_to_recent_changes(**payload)
    assert first is True
    assert second is False, "second identical write must be deduped"
    body = m.RECENT_CHANGES.read_text(encoding="utf-8")
    assert body.count("Session changes: 5 files modified") == 1


def test_append_to_recent_changes_allows_different_file_lists(tmp_path):
    """Same summary but different files = legit new event."""
    m = _import_memory_writeback_with_root(tmp_path)
    assert m.append_to_recent_changes(
        "Session changes: 5 files modified", ["a.py"], []
    ) is True
    assert m.append_to_recent_changes(
        "Session changes: 5 files modified", ["b.py"], []
    ) is True
    body = m.RECENT_CHANGES.read_text(encoding="utf-8")
    # Two distinct blocks
    assert body.count("### ") == 2


def test_append_to_recent_changes_allows_different_decisions(tmp_path):
    """Same files but different decisions = legit new event."""
    m = _import_memory_writeback_with_root(tmp_path)
    assert m.append_to_recent_changes("Sum", ["a.py"], ["choice 1"]) is True
    assert m.append_to_recent_changes("Sum", ["a.py"], ["choice 2"]) is True
    body = m.RECENT_CHANGES.read_text(encoding="utf-8")
    assert body.count("### ") == 2


def test_audit_append_dedupes_identical_event(tmp_path):
    m = _import_memory_writeback_with_root(tmp_path)
    payload = dict(
        event="session_writeback",
        actor="claude-session",
        details={"summary": "X", "changed_files": ["a"], "decisions": []},
    )
    assert m.audit_append(**payload) is True
    assert m.audit_append(**payload) is False, "identical audit event must be deduped"
    lines = m.AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_audit_append_allows_changed_details(tmp_path):
    m = _import_memory_writeback_with_root(tmp_path)
    m.audit_append(
        event="session_writeback",
        actor="claude-session",
        details={"summary": "X", "changed_files": ["a"], "decisions": []},
    )
    m.audit_append(
        event="session_writeback",
        actor="claude-session",
        details={"summary": "X", "changed_files": ["a", "b"], "decisions": []},
    )
    lines = m.AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_content_hash_ignores_timestamp_fields(tmp_path):
    m = _import_memory_writeback_with_root(tmp_path)
    a = m._content_hash({"summary": "X", "ts": "2026-05-25T08:00:00"})
    b = m._content_hash({"summary": "X", "ts": "2026-05-26T22:00:00"})
    assert a == b, "ts must not contribute to the hash"


def test_first_existing_block_hash_returns_none_on_empty(tmp_path):
    m = _import_memory_writeback_with_root(tmp_path)
    assert m._first_existing_block_hash("") is None
    assert m._first_existing_block_hash("# Phoenix\n\n") is None


# ─── integration: invoke the CLI twice with no intervening change ───


def test_cli_double_invoke_writes_one_block(tmp_path):
    """Run memory_writeback.py twice in succession with IDENTICAL
    explicit args. The second invocation must be a no-op (dedupe).

    NOTE: we don't use --auto-detect because git diff would naturally
    differ between runs (the first run modifies RECENT_CHANGES.md,
    which then appears in the second run's diff). Explicit args remove
    that ambiguity and isolate the dedupe-logic test.
    """
    repo = tmp_path / "fake_phoenix"
    (repo / "memory" / "context").mkdir(parents=True)
    (repo / "memory" / "context" / "RECENT_CHANGES.md").write_text(
        "# Phoenix\n\n_seed_\n\n---\n\n", encoding="utf-8"
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # Shim re-roots memory_writeback at the fake repo so the test
    # never touches the real one.
    wrapper = repo / "run_writeback.py"
    wrapper.write_text(
        f"""
import sys, importlib
sys.path.insert(0, {repr(str(PHOENIX_ROOT))})
m = importlib.import_module('tools.memory_writeback')
from pathlib import Path
root = Path({repr(str(repo))})
m.PHOENIX_ROOT = root
m.MEMORY_DIR = root / 'memory'
m.CONTEXT_DIR = m.MEMORY_DIR / 'context'
m.AUDIT_LOG = m.MEMORY_DIR / 'audit_log.jsonl'
m.LOCK_FILE = m.MEMORY_DIR / '.lock'
m.CURRENT_STATE = m.CONTEXT_DIR / 'CURRENT_STATE.md'
m.RECENT_CHANGES = m.CONTEXT_DIR / 'RECENT_CHANGES.md'
sys.exit(m.main())
""",
        encoding="utf-8",
    )

    args = [
        "--summary", "Session changes: 5 files modified",
        "--changed-files", "a.py", "b.py", "c.py",
    ]

    # First invocation should write one block.
    r1 = subprocess.run(
        [sys.executable, str(wrapper)] + args,
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert r1.returncode == 0, f"first invoke failed: {r1.stderr}"

    # Second invocation with IDENTICAL args should dedupe.
    r2 = subprocess.run(
        [sys.executable, str(wrapper)] + args,
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert r2.returncode == 0, f"second invoke failed: {r2.stderr}"

    rc = (repo / "memory" / "context" / "RECENT_CHANGES.md").read_text(encoding="utf-8")
    body = rc.split("---\n\n", 1)[1]
    n_blocks = body.count("\n### ") + (1 if body.startswith("### ") else 0)
    assert n_blocks == 1, (
        f"expected exactly 1 block after double-invoke, got {n_blocks}\n"
        f"RECENT_CHANGES body:\n{body}"
    )

    audit = (repo / "memory" / "audit_log.jsonl").read_text(encoding="utf-8")
    audit_lines = [l for l in audit.splitlines() if l.strip()]
    assert len(audit_lines) == 1, f"expected 1 audit entry, got {len(audit_lines)}"
    rec = json.loads(audit_lines[0])
    assert rec["event"] == "session_writeback"

    # The dedupe path should show in stdout on the second run.
    assert "DEDUPE" in r2.stdout or "duplicate" in r2.stdout.lower(), (
        f"second run stdout did not signal dedupe:\n{r2.stdout}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
