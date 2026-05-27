#!/usr/bin/env python3
"""
Phoenix Bot — Memory Writeback Tool

Single source of truth for updating memory/ from Claude sessions.

Design principles (from LLM-agent-memory research, 2026):
- Atomic writes (tmp file + fsync + os.replace) — no corruption on crash mid-write
- File locking — no race conditions between concurrent sessions
- Read-back validation — confirm write actually persisted
- Append-only audit log — immutable history; derived files regenerable from log
- Git commit — memory/ changes are tracked and recoverable

Called by:
- SessionEnd hook (automatic on Claude session close)
- Nightly integrity check (23:00 CDT weeknights)
- Manual: python tools/memory_writeback.py --summary "..." --changed-files f1 f2

Flags:
  --auto-detect       Scan git for changes, infer summary
  --summary "..."     Explicit session summary
  --changed-files ... List of files changed this session
  --decisions ...     Key decisions made
  --check-pending     Read-only: report if writeback needed, exit 0=clean 1=pending
  --commit            Also git-commit memory/ after write
  --verify            Read-back verify after write
  --sync-procedural   Regenerate procedural/strategy_params.yaml from config/strategies.py
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows-compatible file locking
try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False
    import fcntl

PHOENIX_ROOT = Path(__file__).parent.parent
MEMORY_DIR = PHOENIX_ROOT / "memory"
CONTEXT_DIR = MEMORY_DIR / "context"
AUDIT_LOG = MEMORY_DIR / "audit_log.jsonl"
LOCK_FILE = MEMORY_DIR / ".lock"
CURRENT_STATE = CONTEXT_DIR / "CURRENT_STATE.md"
RECENT_CHANGES = CONTEXT_DIR / "RECENT_CHANGES.md"


class FileLock:
    """Cross-platform file lock context manager."""
    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        if HAS_MSVCRT:
            # Try a few times — msvcrt raises if immediately locked
            for attempt in range(10):
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    return self
                except OSError:
                    time.sleep(0.2)
            raise RuntimeError(f"Could not acquire lock on {self.path} after 2s")
        else:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
            return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fh:
            try:
                if HAS_MSVCRT:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self.fh.close()


def atomic_write(path: Path, content: str, verify: bool = True) -> bool:
    """Write content to path atomically. Optionally read back to verify."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    # Write to tmp, fsync, rename
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

    # Read-back verification
    if verify:
        with open(path, "r", encoding="utf-8") as f:
            written = f.read()
        if written != content:
            raise RuntimeError(f"Read-back mismatch on {path}")
    return True


def _content_hash(payload: dict) -> str:
    """Stable hash of a writeback payload, EXCLUDING any timestamp keys.

    Used to dedupe identical SessionEnd events that fire back-to-back
    (the 2026-05-25 incident: 3 identical writebacks within 4 seconds,
    each producing a duplicate "Session changes: 83 files modified"
    block in RECENT_CHANGES.md).
    """
    # Defensive: strip any ts-style key so the hash is content-only.
    clean = {k: v for k, v in payload.items() if k not in ("ts", "timestamp")}
    blob = json.dumps(clean, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _last_audit_content_hash() -> str | None:
    """Read the last JSONL line in audit_log.jsonl and return its
    content hash, or None if file missing/empty/malformed."""
    if not AUDIT_LOG.exists():
        return None
    try:
        # File can be large; tail by reading the last ~16 KB.
        size = AUDIT_LOG.stat().st_size
        with open(AUDIT_LOG, "rb") as f:
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", errors="replace")
        last_line = ""
        for line in tail.splitlines():
            if line.strip():
                last_line = line
        if not last_line:
            return None
        rec = json.loads(last_line)
        return _content_hash({
            "event": rec.get("event"),
            "actor": rec.get("actor"),
            "details": rec.get("details", {}),
        })
    except (OSError, json.JSONDecodeError):
        return None


def audit_append(event: str, actor: str, details: dict) -> bool:
    """Append one event to audit_log.jsonl. Append-only, never overwrites.

    Returns True if the entry was written, False if it was deduped
    against an immediately-prior identical entry (post-2026-05-25 fix).
    """
    new_hash = _content_hash({"event": event, "actor": actor, "details": details})
    last_hash = _last_audit_content_hash()
    if last_hash is not None and last_hash == new_hash:
        # Identical to the previous entry — most likely a SessionEnd
        # hook re-fire (context compaction, etc.). Skip the duplicate.
        return False

    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        "actor": actor,
        "details": details,
    }
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return True


def _first_existing_block_hash(content: str) -> str | None:
    """Extract the FIRST (most recent) dated entry from RECENT_CHANGES.md
    and return its content-hash (summary + files + decisions, no ts).

    Used to dedupe consecutive identical writebacks. The file is
    newest-first, so the most recent block is the one just below the
    `---\n\n` header marker."""
    # Block format: "### <ts> — <summary>\n\n[**Files changed:** ...]\n[**Decisions:** ...]\n---\n"
    marker = "---\n\n"
    if marker not in content:
        return None
    body = content.split(marker, 1)[1]
    # Take everything up to the next "---" (end of first entry)
    end = body.find("\n---")
    if end == -1:
        return None
    first_block = body[:end]
    # Parse: first line is "### <ts> — <summary>".
    # The timestamp portion has variable token count (e.g.
    # "2026-05-25 08:30 Central Daylight Time" is 4 tokens, while
    # "2026-05-25 08:30 CDT" is 3), so split on the em-dash separator
    # instead of trying to count tokens.
    lines = first_block.splitlines()
    if not lines:
        return None
    head = lines[0]
    summary: str
    if "—" in head:
        summary = head.split("—", 1)[1].strip()
    elif " - " in head:  # ASCII-dash fallback
        summary = head.split(" - ", 1)[1].strip()
    else:
        # Last-resort: strip the leading "### " and take everything else.
        summary = head.lstrip("# ").strip()
    files: list[str] = []
    decisions: list[str] = []
    section = None
    for ln in lines[1:]:
        s = ln.strip()
        if s == "**Files changed:**":
            section = "files"
            continue
        if s == "**Decisions:**":
            section = "decisions"
            continue
        if not s:
            section = None
            continue
        if section == "files" and s.startswith("- "):
            files.append(s[2:].strip("`"))
        elif section == "decisions" and s.startswith("- "):
            decisions.append(s[2:])
    return _content_hash({
        "summary": summary,
        "changed_files": files,
        "decisions": decisions,
    })


def append_to_recent_changes(summary: str, changed_files: list, decisions: list) -> bool:
    """Prepend a new dated entry to RECENT_CHANGES.md (newest first).

    Returns True if a new block was written, False if it was deduped
    against the most-recent existing block (post-2026-05-25 fix: the
    SessionEnd hook can fire several times in a row with identical
    payload — context compaction, session resume, etc. — and we don't
    want each event to leave its own duplicate block)."""
    if not RECENT_CHANGES.exists():
        # Bootstrap the file
        initial = "# Phoenix Bot — Recent Changes\n\n_Auto-appended by tools/memory_writeback.py via SessionEnd hook._\n\n---\n\n"
        atomic_write(RECENT_CHANGES, initial)

    current = RECENT_CHANGES.read_text(encoding="utf-8")

    # Dedupe against the most-recent existing block.
    incoming_hash = _content_hash({
        "summary": summary,
        "changed_files": list(changed_files),
        "decisions": list(decisions),
    })
    existing_hash = _first_existing_block_hash(current)
    if existing_hash is not None and existing_hash == incoming_hash:
        return False  # Identical to last block — skip the duplicate.

    now = datetime.now().astimezone()
    entry_lines = [
        f"### {now.strftime('%Y-%m-%d %H:%M %Z')} — {summary}",
        "",
    ]
    if changed_files:
        entry_lines.append("**Files changed:**")
        for f in changed_files:
            entry_lines.append(f"- `{f}`")
        entry_lines.append("")
    if decisions:
        entry_lines.append("**Decisions:**")
        for d in decisions:
            entry_lines.append(f"- {d}")
        entry_lines.append("")
    entry_lines.append("---")
    entry_lines.append("")
    new_entry = "\n".join(entry_lines)

    # Insert after the header block (before "---\n\n")
    marker = "---\n\n"
    if marker in current:
        head, rest = current.split(marker, 1)
        new_content = head + marker + new_entry + rest
    else:
        new_content = current + "\n" + new_entry

    atomic_write(RECENT_CHANGES, new_content)
    return True


def update_current_state(summary: str) -> None:
    """Update the _Last updated_ timestamp in CURRENT_STATE.md."""
    if not CURRENT_STATE.exists():
        return  # Nothing to update
    content = CURRENT_STATE.read_text(encoding="utf-8")
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    # Replace the _Last updated: ...* line
    new_content = re.sub(
        r"^_Last updated: [^_]+_$",
        f"_Last updated: {now}_",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if new_content != content:
        atomic_write(CURRENT_STATE, new_content)


def git_commit_memory(message: str) -> bool:
    """Commit memory/ dir changes. Returns True on success, False if nothing to commit."""
    try:
        # Stage memory/ only, never code
        subprocess.run(
            ["git", "-C", str(PHOENIX_ROOT), "add", "memory/"],
            check=True, capture_output=True
        )
        # Check if anything staged
        result = subprocess.run(
            ["git", "-C", str(PHOENIX_ROOT), "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if result.returncode == 0:
            return False  # Nothing to commit
        # Commit
        subprocess.run(
            ["git", "-C", str(PHOENIX_ROOT), "commit", "-m", message],
            check=True, capture_output=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[WARN] git commit failed: {e.stderr.decode() if e.stderr else e}", file=sys.stderr)
        return False


def check_pending() -> int:
    """Check if memory/ has uncommitted changes. Returns 1 if pending, 0 if clean."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PHOENIX_ROOT), "status", "--porcelain", "memory/"],
            capture_output=True, text=True, check=True
        )
        if result.stdout.strip():
            print("[WRITEBACK PENDING] Uncommitted memory/ changes detected:")
            print(result.stdout)
            return 1
        print("[WRITEBACK CLEAN] memory/ up to date")
        return 0
    except subprocess.CalledProcessError:
        return 0  # Git not available or not a repo — don't error


def main():
    parser = argparse.ArgumentParser(description="Phoenix memory writeback")
    parser.add_argument("--auto-detect", action="store_true",
                        help="Infer summary from git diff")
    parser.add_argument("--summary", type=str, default="",
                        help="Session summary (1 line)")
    parser.add_argument("--changed-files", nargs="*", default=[],
                        help="List of files changed")
    parser.add_argument("--decisions", nargs="*", default=[],
                        help="Key decisions made")
    parser.add_argument("--check-pending", action="store_true",
                        help="Read-only: check if writeback needed, exit 0/1")
    parser.add_argument("--commit", action="store_true",
                        help="Git commit memory/ after write")
    parser.add_argument("--verify", action="store_true", default=True,
                        help="Read-back verify writes (default: True)")
    args = parser.parse_args()

    # Check-pending mode: read-only, exit with status
    if args.check_pending:
        sys.exit(check_pending())

    # Auto-detect from git diff
    if args.auto_detect and not args.summary:
        try:
            result = subprocess.run(
                ["git", "-C", str(PHOENIX_ROOT), "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, check=False,
            )
            if result.stdout.strip():
                files = result.stdout.strip().split("\n")
                args.changed_files = files[:20]  # cap at 20
                args.summary = f"Session changes: {len(files)} files modified"
            else:
                # No git changes — probably nothing to write back
                print("[WRITEBACK] No git changes detected, nothing to write")
                return 0
        except Exception:
            pass

    if not args.summary:
        args.summary = "Unnamed session"

    # Acquire lock
    with FileLock(LOCK_FILE):
        # Append to audit log (source of truth; content-hash deduped).
        audit_written = audit_append(
            event="session_writeback",
            actor="claude-session",
            details={
                "summary": args.summary,
                "changed_files": args.changed_files,
                "decisions": args.decisions,
            },
        )

        # Append to RECENT_CHANGES.md (content-hash deduped).
        rc_written = append_to_recent_changes(
            args.summary, args.changed_files, args.decisions
        )

        # Update CURRENT_STATE.md timestamp (idempotent — safe even on dedupe).
        update_current_state(args.summary)

        # Git commit — skip the commit if both append targets deduped,
        # otherwise we'd produce empty commits.
        if args.commit:
            if not (audit_written or rc_written):
                print("[WRITEBACK] Identical to last writeback — no new commit")
            else:
                committed = git_commit_memory(f"memory: {args.summary[:70]}")
                if committed:
                    print(f"[WRITEBACK] Committed memory/ changes")
                else:
                    print(f"[WRITEBACK] Nothing to commit")

        if not (audit_written or rc_written):
            print(f"[WRITEBACK DEDUPE] {args.summary} (skipped — duplicate)")
        else:
            wrote = []
            if audit_written:
                wrote.append("audit")
            if rc_written:
                wrote.append("recent_changes")
            print(f"[WRITEBACK OK] {args.summary} (wrote: {', '.join(wrote)})")
        return 0


if __name__ == "__main__":
    sys.exit(main())
