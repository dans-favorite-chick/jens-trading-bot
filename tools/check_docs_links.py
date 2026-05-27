"""
Phoenix Bot — Docs Cross-Link Audit (P3-4)

Walks every `.md` file under `docs/` and `memory/`, extracts every
Markdown link of the form `[text](path)`, resolves the path relative
to the source file's location, and verifies the target exists.

Reports broken links and links pointing into `docs/archive/`. Skips:
  * External URLs (http://, https://, mailto:)
  * Pure anchor links (`#section`)
  * Files inside `docs/archive/` as SOURCES (archives are allowed to
    have stale links — that's the point of archiving)

Usage:
  python tools/check_docs_links.py           # dry-run, report only
  python tools/check_docs_links.py --apply   # rewrite broken links as
                                             # HTML comments preserving
                                             # the original text

Exit codes:
  0 — no broken links found (archive links don't fail)
  1 — broken links detected (in dry-run only; --apply always 0 on success)
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterable

# Match standard inline Markdown links: [text](target)
# Skip reference-style links and image links (we don't need to be perfect
# — the goal is to catch the common case).
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")

EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "ftp://", "tel:", "file://", "file:")

# Matches a trailing `:<line>` or `:<line>-<line>` reference, common in
# IDE-grep output and audit reports (e.g. `config/strategies.py:184-188`).
# We strip this before filesystem resolution so the link still validates.
LINE_REF_RE = re.compile(r":\d+(?:-\d+)?$")


@dataclasses.dataclass
class LinkRef:
    source: Path        # repo-relative path to the source .md
    line_no: int
    text: str           # link text inside [ ]
    target: str         # raw target string inside ( )
    resolved: Path | None  # filesystem path if relative, None if external/anchor
    status: str         # OK / BROKEN / IN_ARCHIVE / EXTERNAL / ANCHOR

    @property
    def display_target(self) -> str:
        return self.target


def iter_markdown_files(repo_root: Path) -> Iterable[Path]:
    """Yield every .md file under docs/ or memory/, repo-relative."""
    for sub in ("docs", "memory"):
        base = repo_root / sub
        if not base.exists():
            continue
        for p in base.rglob("*.md"):
            yield p


def is_archive_source(repo_root: Path, path: Path) -> bool:
    """True if this file lives inside docs/archive/ (skip as a source)."""
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False
    parts = rel.parts
    return len(parts) >= 2 and parts[0] == "docs" and parts[1] == "archive"


def classify_target(repo_root: Path, source: Path, target: str) -> tuple[Path | None, str]:
    """
    Return (resolved_path_or_None, status) for a single link target.

    status ∈ {OK, BROKEN, IN_ARCHIVE, EXTERNAL, ANCHOR}
    """
    raw = target.strip()

    # Some doc generators wrap absolute paths in angle brackets:
    # `[text](<C:/path/file.py:123>)`. Strip the brackets first.
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()

    # Anchor-only links (e.g. [x](#section))
    if raw.startswith("#"):
        return None, "ANCHOR"

    # External URLs
    if any(raw.lower().startswith(s) for s in EXTERNAL_SCHEMES):
        return None, "EXTERNAL"

    # Windows-absolute paths like `C:/...` or `C:\...` — treat as external
    # references (out-of-repo IDE artifacts). We don't try to validate
    # them since the resolution depends on the user's machine layout, not
    # the repo.
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return None, "EXTERNAL"

    # Strip URL fragments (#anchor) and query strings (?foo=bar) from the
    # filesystem portion before resolution.
    path_part = raw.split("#", 1)[0].split("?", 1)[0]
    if not path_part:
        # Was a pure anchor with extra text, treat as anchor
        return None, "ANCHOR"

    # Strip a trailing `:line` or `:line-line` reference. These show up in
    # audit reports as `[file.py:184-188](file.py:184)` and refer to a
    # real file plus a line range, not a different file.
    path_part = LINE_REF_RE.sub("", path_part)

    # Resolve. Absolute paths starting with "/" are treated repo-relative
    # (a common Markdown convention).
    if path_part.startswith("/"):
        candidate = repo_root / path_part.lstrip("/")
    else:
        candidate = (source.parent / path_part).resolve()

    # Normalize to absolute.
    try:
        candidate = candidate.resolve()
    except OSError:
        return candidate, "BROKEN"

    if not candidate.exists():
        # Fallback: many Phoenix doc files use bare repo-root-relative
        # paths like `config/strategies.py` from inside `docs/...`. If
        # the source-relative resolution failed AND the path is bare
        # (no leading `./`, `../`, or drive letter), try again from
        # the repo root.
        if not path_part.startswith(("./", "../")):
            alt = (repo_root / path_part).resolve()
            if alt.exists():
                candidate = alt
            else:
                return candidate, "BROKEN"
        else:
            return candidate, "BROKEN"

    # OK — exists. Is it inside docs/archive/?
    try:
        rel = candidate.relative_to(repo_root.resolve())
    except ValueError:
        # Outside the repo. We still call it OK (e.g. ../../config/settings.py)
        return candidate, "OK"

    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "docs" and parts[1] == "archive":
        return candidate, "IN_ARCHIVE"

    return candidate, "OK"


def scan_file(repo_root: Path, source: Path) -> list[LinkRef]:
    """Extract every link from `source` and classify it."""
    refs: list[LinkRef] = []
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return refs

    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in LINK_RE.finditer(line):
            link_text, target = match.group(1), match.group(2)
            resolved, status = classify_target(repo_root, source, target)
            refs.append(
                LinkRef(
                    source=source,
                    line_no=line_no,
                    text=link_text,
                    target=target,
                    resolved=resolved,
                    status=status,
                )
            )
    return refs


def scan_repo(repo_root: Path) -> list[LinkRef]:
    """Scan every non-archive .md file in docs/ and memory/."""
    all_refs: list[LinkRef] = []
    for md in iter_markdown_files(repo_root):
        if is_archive_source(repo_root, md):
            continue
        all_refs.extend(scan_file(repo_root, md))
    return all_refs


def print_report(repo_root: Path, refs: list[LinkRef], verbose: bool = False) -> tuple[int, int, int]:
    """
    Print a grouped report. Returns (n_links, n_broken, n_archive).
    """
    by_source: dict[Path, list[LinkRef]] = {}
    for r in refs:
        by_source.setdefault(r.source, []).append(r)

    n_broken = 0
    n_archive = 0
    n_total = len(refs)
    n_files_with_issues = 0

    print("Phoenix docs link audit")
    print("-" * 30)

    for source in sorted(by_source.keys()):
        rs = by_source[source]
        flagged = [r for r in rs if r.status in ("BROKEN", "IN_ARCHIVE")]
        if not flagged and not verbose:
            continue
        n_files_with_issues += 1
        rel = source.relative_to(repo_root) if source.is_absolute() else source
        print(f"\nSource: {rel.as_posix()}")
        for r in rs:
            if not verbose and r.status not in ("BROKEN", "IN_ARCHIVE"):
                continue
            if r.status == "BROKEN":
                note = "BROKEN (no such file)"
                n_broken += 1
            elif r.status == "IN_ARCHIVE":
                note = "IN_ARCHIVE (still resolves; OK)"
                n_archive += 1
            else:
                note = r.status
            print(f"  L{r.line_no:<4} -> {r.target:<40} {note}")

    # Re-tally because verbose path double-counts otherwise
    n_broken = sum(1 for r in refs if r.status == "BROKEN")
    n_archive = sum(1 for r in refs if r.status == "IN_ARCHIVE")

    n_files = len({r.source for r in refs})
    print()
    print(
        f"Summary: {n_files} files scanned, {n_total} links checked, "
        f"{n_broken} broken, {n_archive} archived (intentional or not)"
    )
    return n_total, n_broken, n_archive


def apply_fixes(repo_root: Path, refs: list[LinkRef], today: str | None = None) -> int:
    """
    Rewrite each BROKEN link in-place as plain text plus an HTML comment
    of the form `<!-- LINK BROKEN 2026-05-25: was X -->`. Returns the
    number of edits made.

    Conservative: we don't try to "best-effort retarget" — that's risky
    without human review. The original link text is preserved (as plain
    text) so the prose stays readable.
    """
    if today is None:
        today = date.today().isoformat()

    edits = 0
    by_source: dict[Path, list[LinkRef]] = {}
    for r in refs:
        if r.status != "BROKEN":
            continue
        by_source.setdefault(r.source, []).append(r)

    for source, broken in by_source.items():
        text = source.read_text(encoding="utf-8")
        new_text = text
        # Replace each occurrence of the exact `[text](target)` substring.
        # Use string replace (not regex) and bound to one replacement per
        # LinkRef to avoid double-edits when the same link appears twice.
        for r in broken:
            old = f"[{r.text}]({r.target})"
            replacement = f"{r.text} <!-- LINK BROKEN {today}: was {r.target} -->"
            if old in new_text:
                new_text = new_text.replace(old, replacement, 1)
                edits += 1
        if new_text != text:
            source.write_text(new_text, encoding="utf-8")
    return edits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Markdown cross-links under docs/ and memory/.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite broken links as plain text + HTML comment. Default is dry-run.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root (defaults to the parent of this script).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print every link, not only the flagged ones.",
    )
    args = parser.parse_args(argv)

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        repo_root = Path(__file__).resolve().parent.parent

    refs = scan_repo(repo_root)
    n_total, n_broken, n_archive = print_report(repo_root, refs, verbose=args.verbose)

    if args.apply and n_broken:
        edits = apply_fixes(repo_root, refs)
        print(f"\nApplied {edits} fix(es): broken links replaced with HTML comments.")
        return 0

    return 1 if n_broken else 0


if __name__ == "__main__":
    sys.exit(main())
