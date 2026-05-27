#!/usr/bin/env python3
"""tools/install_git_hooks.py — point git at .githooks/ for this clone.

Per-clone, idempotent. Operator runs ONCE per fresh clone. CI can also
run it as part of bootstrap.

What it does:
  1. git config core.hooksPath .githooks
  2. Ensure .githooks/pre-commit is executable on POSIX (Windows
     doesn't care about the bit, but git on WSL/MSYS does)
  3. Verifies by running the hook with a synthetic input

Why we don't use the legacy .git/hooks/ path: those don't get committed
to the repo. By using core.hooksPath instead, the hook script is part
of the source tree, version-controlled, code-reviewed, and survives
re-clones without anyone having to remember to copy a file.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = REPO_ROOT / ".githooks"
# 2026-05-27: hook is commit-msg (not pre-commit). Pre-commit can't see
# the message; commit-msg gets it as argv[1]. See .claude/PROTECTED_FILES.md.
HOOK_FILE = HOOK_DIR / "commit-msg"


def _run(args: list[str]) -> tuple[int, str, str]:
    r = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True,
        encoding="utf-8", errors="replace",
    )
    return r.returncode, r.stdout, r.stderr


def main() -> int:
    if not HOOK_DIR.is_dir():
        print(f"ERROR: {HOOK_DIR} missing — repo clone is incomplete?",
              file=sys.stderr)
        return 1
    if not HOOK_FILE.is_file():
        print(f"ERROR: {HOOK_FILE} missing", file=sys.stderr)
        return 1

    # 1. Point git at the in-repo hooks directory.
    code, out, err = _run(["git", "config", "core.hooksPath", ".githooks"])
    if code != 0:
        print(f"ERROR: git config failed: {err}", file=sys.stderr)
        return 1

    # 2. Make the hook executable on POSIX (Windows ignores).
    if os.name != "nt":
        try:
            st = HOOK_FILE.stat()
            HOOK_FILE.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as e:
            print(f"WARN: chmod failed (non-blocking): {e}", file=sys.stderr)

    # 3. Verify by reading back the config.
    code, out, err = _run(["git", "config", "--get", "core.hooksPath"])
    if code != 0 or out.strip() != ".githooks":
        print(f"ERROR: post-install verification failed (got '{out.strip()}')",
              file=sys.stderr)
        return 1

    print("Phoenix git hooks installed.")
    print(f"  hooks_path:    .githooks/")
    print(f"  commit-msg:    {HOOK_FILE.relative_to(REPO_ROOT)}")
    print(f"  policy doc:    .claude/PROTECTED_FILES.md")
    print("")
    print("Next protected-file commit will require an OPERATOR-APPROVED line.")
    print("Bypass (emergency only): git commit --no-verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
