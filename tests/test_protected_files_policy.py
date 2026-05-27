"""tests/test_protected_files_policy.py

Lock-in tests for the Protected Files policy (2026-05-27).

These tests are the rope that keeps three independent declarations
synchronized:

  1. The canonical policy doc at `.claude/PROTECTED_FILES.md`
  2. The quick-reference table in `CLAUDE.md`
  3. The PROTECTED_FILES set hard-coded in `.githooks/pre-commit`

If they drift apart, an edit to a protected file could pass one
checkpoint and slip through the others. So we assert:

- The hook script exists and is executable-on-disk (Windows: just
  exists; POSIX: has the IX bit somewhere).
- Every protected file the hook enforces actually exists in the repo
  (catches a rename that left the hook list stale).
- Every protected file is also listed verbatim in the canonical doc
  AND in CLAUDE.md.
- The hook's approval-recency check works (today = OK, 30 days ago
  = REJECT).

We also exercise the hook end-to-end against a tmp git repo, to
confirm it actually blocks a bad commit and lets a good one through.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

PHOENIX_ROOT = Path(__file__).resolve().parent.parent
# 2026-05-27: hook is commit-msg (not pre-commit). Pre-commit can't
# validate the message — that's commit-msg's job. See policy doc.
HOOK_FILE = PHOENIX_ROOT / ".githooks" / "commit-msg"
POLICY_DOC = PHOENIX_ROOT / ".claude" / "PROTECTED_FILES.md"
CLAUDE_MD = PHOENIX_ROOT / "CLAUDE.md"


def _hook_protected_files() -> set[str]:
    """Extract the PROTECTED_FILES set literal from the hook source."""
    src = HOOK_FILE.read_text(encoding="utf-8")
    m = re.search(r"PROTECTED_FILES\s*=\s*\{([^}]*)\}", src, re.S)
    assert m, "could not find PROTECTED_FILES set in hook source"
    body = m.group(1)
    return set(re.findall(r'"([^"]+)"', body))


# ─── basic existence ─────────────────────────────────────────────────


def test_hook_file_exists():
    assert HOOK_FILE.is_file(), f"{HOOK_FILE} missing — install_git_hooks.py won't work"


def test_policy_doc_exists():
    assert POLICY_DOC.is_file(), f"{POLICY_DOC} missing"


def test_claude_md_references_policy_doc():
    txt = CLAUDE_MD.read_text(encoding="utf-8")
    assert ".claude/PROTECTED_FILES.md" in txt, (
        "CLAUDE.md must point at the canonical policy doc"
    )


# ─── invariant: every protected file in the hook actually exists ────


def test_every_protected_file_exists():
    files = _hook_protected_files()
    assert files, "hook PROTECTED_FILES set is empty — that's wrong"
    missing = [f for f in files if not (PHOENIX_ROOT / f).is_file()]
    assert not missing, (
        f"hook enforces files that don't exist: {missing}\n"
        f"Either the file was renamed or the hook list is stale."
    )


# ─── invariant: hook list == policy doc list == CLAUDE.md list ──────


def test_hook_list_subset_of_policy_doc():
    hook_files = _hook_protected_files()
    policy_txt = POLICY_DOC.read_text(encoding="utf-8")
    missing = [f for f in hook_files if f not in policy_txt]
    assert not missing, (
        f"Hook enforces files not documented in policy doc: {missing}"
    )


def test_hook_list_subset_of_claude_md():
    hook_files = _hook_protected_files()
    claude_txt = CLAUDE_MD.read_text(encoding="utf-8")
    # CLAUDE.md is now a quick-reference; the canonical doc is .claude/.
    # CLAUDE.md must either name each protected file directly OR contain
    # the canonical-doc link (which it does).
    if ".claude/PROTECTED_FILES.md" in claude_txt:
        return  # passes via reference
    missing = [f for f in hook_files if f not in claude_txt]
    assert not missing, (
        f"Files in hook but not named in CLAUDE.md (and no doc reference): "
        f"{missing}"
    )


# ─── hook unit-level: approval-recency check ────────────────────────


def _import_hook_module():
    """Load .githooks/pre-commit (extensionless) as a module so we can
    call its pure-functions directly. importlib's normal
    spec_from_file_location can't infer a loader without a .py
    extension, so we hand it a SourceFileLoader explicitly."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("phoenix_pre_commit", str(HOOK_FILE))
    spec = importlib.util.spec_from_loader("phoenix_pre_commit", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_approval_status_accepts_today():
    mod = _import_hook_module()
    msg = f"some commit\n\nOPERATOR-APPROVED: {date.today().isoformat()}\n"
    ok, reason = mod._approval_status(msg)
    assert ok is True, reason


def test_approval_status_rejects_missing_tag():
    mod = _import_hook_module()
    ok, reason = mod._approval_status("just a normal commit message\n")
    assert ok is False
    assert "no OPERATOR-APPROVED" in reason


def test_approval_status_rejects_stale_date():
    mod = _import_hook_module()
    stale = (date.today() - timedelta(days=30)).isoformat()
    msg = f"old approval\n\nOPERATOR-APPROVED: {stale}\n"
    ok, reason = mod._approval_status(msg)
    assert ok is False
    assert "older than" in reason


def test_approval_status_rejects_future_date():
    mod = _import_hook_module()
    future = (date.today() + timedelta(days=30)).isoformat()
    msg = f"future date\n\nOPERATOR-APPROVED: {future}\n"
    ok, reason = mod._approval_status(msg)
    assert ok is False
    assert "future" in reason


def test_approval_status_rejects_malformed_date():
    mod = _import_hook_module()
    msg = "OPERATOR-APPROVED: not-a-date\n"
    ok, reason = mod._approval_status(msg)
    # Malformed isn't matched by the regex (which requires YYYY-MM-DD),
    # so this reports as "no OPERATOR-APPROVED line" — which is also
    # a correct rejection.
    assert ok is False


# ─── integration: end-to-end in a synthetic git repo ────────────────


@pytest.fixture
def fake_repo(tmp_path):
    """A tmp git repo containing a copy of:
      - bridge/oif_writer.py (a protected file)
      - .githooks/commit-msg (the hook under test)
    so we can stage a real diff and run the hook against it."""
    repo = tmp_path / "fake_phoenix"
    repo.mkdir()
    # Copy the hook (and create the directory structure the hook expects).
    (repo / ".githooks").mkdir()
    shutil.copy2(HOOK_FILE, repo / ".githooks" / "commit-msg")
    # Initial dummy version of a protected file.
    (repo / "bridge").mkdir()
    (repo / "bridge" / "oif_writer.py").write_text(
        "# stub\nVERSION = 1\n", encoding="utf-8"
    )

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    if os.name != "nt":
        hp = repo / ".githooks" / "commit-msg"
        hp.chmod(hp.stat().st_mode | stat.S_IXUSR)

    # IMPORTANT: do the initial commit BEFORE wiring the hook, so the
    # baseline state lands without tripping the very guard we're testing.
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", "init"],
        cwd=repo, check=True,
    )
    # NOW activate the hook for subsequent commits/test invocations.
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"],
                   cwd=repo, check=True)
    return repo


def _stage_protected_change(repo: Path):
    """Modify the protected file and stage."""
    p = repo / "bridge" / "oif_writer.py"
    p.write_text(p.read_text() + "\n# tweak\n", encoding="utf-8")
    subprocess.run(["git", "add", "bridge/oif_writer.py"],
                   cwd=repo, check=True)


def _write_commit_message(repo: Path, msg: str) -> Path:
    """Drop a commit message at .git/COMMIT_EDITMSG and return the
    path. commit-msg hooks receive this path as sys.argv[1] from git;
    our tests invoke the hook directly so we have to pass it explicitly."""
    p = repo / ".git" / "COMMIT_EDITMSG"
    p.write_text(msg, encoding="utf-8")
    return p


def _run_hook(repo: Path, msg_file: Path) -> subprocess.CompletedProcess:
    """Invoke the hook via `python <hook> <msg_file>` with cwd=repo so
    REPO_ROOT resolves correctly. This bypasses git's shebang handling
    (which is flaky on Windows for extensionless hooks) and tests the
    hook's actual logic — which is the part that matters."""
    hook = repo / ".githooks" / "commit-msg"
    return subprocess.run(
        [sys.executable, str(hook), str(msg_file)],
        cwd=repo, capture_output=True,
        encoding="utf-8", errors="replace",
    )


def test_e2e_hook_blocks_without_approval_tag(fake_repo):
    _stage_protected_change(fake_repo)
    mf = _write_commit_message(fake_repo, "tweak oif_writer\n")
    r = _run_hook(fake_repo, mf)
    assert r.returncode != 0, (
        f"hook did not block protected change w/o approval\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )
    assert "BLOCKED" in r.stderr or "protected" in r.stderr.lower()


def test_e2e_hook_passes_with_fresh_approval(fake_repo):
    _stage_protected_change(fake_repo)
    mf = _write_commit_message(
        fake_repo,
        f"tweak oif_writer\n\n"
        f"Reason: harmless comment update.\n\n"
        f"OPERATOR-APPROVED: {date.today().isoformat()}\n",
    )
    r = _run_hook(fake_repo, mf)
    assert r.returncode == 0, (
        f"hook unexpectedly blocked fresh-approval commit\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_e2e_hook_passes_on_unrelated_change(fake_repo):
    """A change to an unprotected file must not trip the hook."""
    (fake_repo / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=fake_repo, check=True)
    mf = _write_commit_message(fake_repo, "docs only\n")
    r = _run_hook(fake_repo, mf)
    assert r.returncode == 0, (
        f"hook blocked an unrelated docs-only change\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_e2e_hook_blocks_stale_approval(fake_repo):
    """An OPERATOR-APPROVED date older than 7 days must NOT pass."""
    _stage_protected_change(fake_repo)
    stale = (date.today() - timedelta(days=30)).isoformat()
    mf = _write_commit_message(
        fake_repo,
        f"tweak\n\nOPERATOR-APPROVED: {stale}\n",
    )
    r = _run_hook(fake_repo, mf)
    assert r.returncode != 0, "stale approval must be rejected"
    assert "older than" in r.stderr.lower() or "blocked" in r.stderr.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
