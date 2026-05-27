"""
Tests for tools/check_docs_links.py — P3-4 docs cross-link audit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make tools/ importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_docs_links as cdl  # noqa: E402


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_finds_broken_link(tmp_path: Path) -> None:
    """A link to a nonexistent file is flagged BROKEN."""
    docs = tmp_path / "docs"
    _write(docs / "a.md", "See [the plan](missing.md) for details.\n")

    refs = cdl.scan_repo(tmp_path)
    broken = [r for r in refs if r.status == "BROKEN"]
    assert len(broken) == 1
    assert broken[0].target == "missing.md"
    assert broken[0].source.name == "a.md"


def test_external_urls_skipped(tmp_path: Path) -> None:
    """https:// / http:// / mailto: targets never touch the filesystem."""
    docs = tmp_path / "docs"
    body = (
        "[Anthropic](https://anthropic.com)\n"
        "[Mail](mailto:nobody@example.com)\n"
        "[Insecure](http://example.com/x)\n"
    )
    _write(docs / "ext.md", body)

    refs = cdl.scan_repo(tmp_path)
    assert len(refs) == 3
    assert all(r.status == "EXTERNAL" for r in refs)
    # None of these should be flagged broken even though the targets
    # obviously don't exist on disk.
    assert not any(r.status == "BROKEN" for r in refs)


def test_anchor_links_skipped(tmp_path: Path) -> None:
    """Same-page anchor links (#section) are not filesystem-resolved."""
    docs = tmp_path / "docs"
    _write(docs / "anch.md", "Jump to [the bottom](#summary).\n")

    refs = cdl.scan_repo(tmp_path)
    assert len(refs) == 1
    assert refs[0].status == "ANCHOR"


def test_archive_files_skipped_as_sources(tmp_path: Path) -> None:
    """
    Files inside docs/archive/ are NOT scanned for outgoing links.
    Archives are allowed to be stale — that's the point.
    """
    docs = tmp_path / "docs"
    _write(docs / "archive" / "old.md", "Broken [link](nowhere.md) inside an archive.\n")
    _write(docs / "live.md", "Valid [link](archive/old.md) — should be IN_ARCHIVE.\n")

    refs = cdl.scan_repo(tmp_path)
    # The archived file's outgoing broken link must not appear at all.
    archive_sources = [r for r in refs if "archive" in r.source.parts]
    assert archive_sources == []
    # The live file's link should resolve into the archive.
    assert any(r.status == "IN_ARCHIVE" for r in refs)


def test_apply_actually_fixes(tmp_path: Path) -> None:
    """Dry-run leaves files alone; --apply rewrites broken links."""
    docs = tmp_path / "docs"
    src = docs / "fixme.md"
    _write(src, "Before [click here](missing.md) after.\n")
    original = src.read_text(encoding="utf-8")

    # Dry-run: file unchanged.
    refs_dry = cdl.scan_repo(tmp_path)
    assert src.read_text(encoding="utf-8") == original
    assert any(r.status == "BROKEN" for r in refs_dry)

    # Apply: file should be modified, broken link replaced with plain
    # text + HTML comment containing the original target.
    edits = cdl.apply_fixes(tmp_path, refs_dry, today="2026-05-25")
    assert edits == 1
    after = src.read_text(encoding="utf-8")
    assert after != original
    assert "[click here](missing.md)" not in after
    assert "click here" in after
    assert "<!-- LINK BROKEN 2026-05-25: was missing.md -->" in after


def test_relative_path_resolution(tmp_path: Path) -> None:
    """Links with ../ resolve relative to the source file's directory."""
    docs = tmp_path / "docs"
    config = tmp_path / "config"
    _write(config / "settings.py", "# stub\n")
    _write(docs / "a.md", "See [settings](../config/settings.py).\n")

    refs = cdl.scan_repo(tmp_path)
    assert len(refs) == 1
    assert refs[0].status == "OK"


def test_summary_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The Summary line reports total / broken / archived correctly."""
    docs = tmp_path / "docs"
    _write(docs / "archive" / "old.md", "# old\n")
    _write(
        docs / "a.md",
        "[good](archive/old.md)\n"
        "[bad](nope.md)\n"
        "[ext](https://x.example)\n",
    )

    refs = cdl.scan_repo(tmp_path)
    n_total, n_broken, n_archive = cdl.print_report(tmp_path, refs, verbose=False)
    assert n_total == 3
    assert n_broken == 1
    assert n_archive == 1
    out = capsys.readouterr().out
    assert "Summary:" in out


def test_line_number_suffix_stripped(tmp_path: Path) -> None:
    """`file.py:123` and `file.py:123-130` resolve to `file.py`."""
    cfg = tmp_path / "config"
    docs = tmp_path / "docs"
    _write(cfg / "settings.py", "# stub\n")
    _write(docs / "ref.md", "See [setting](../config/settings.py:184-188).\n")

    refs = cdl.scan_repo(tmp_path)
    assert len(refs) == 1
    assert refs[0].status == "OK"


def test_file_url_scheme_skipped(tmp_path: Path) -> None:
    """`file:///c:/path/file.py` is treated as external, not BROKEN."""
    docs = tmp_path / "docs"
    _write(docs / "audit.md", "[x](file:///c:/Trading%20Project/phoenix_bot/foo.py)\n")

    refs = cdl.scan_repo(tmp_path)
    assert len(refs) == 1
    assert refs[0].status == "EXTERNAL"


def test_repo_root_fallback_for_bare_paths(tmp_path: Path) -> None:
    """
    Bare paths (no `./` or `../`) fall back to repo-root if source-relative
    fails. This matches how Phoenix audit docs link to e.g.
    `config/strategies.py` from inside `docs/PHOENIX_PROJECT_PROMPT.md`.
    """
    docs = tmp_path / "docs"
    cfg = tmp_path / "config"
    _write(cfg / "strategies.py", "# stub\n")
    # Bare path — would fail as `docs/config/strategies.py`, succeed as
    # `<repo_root>/config/strategies.py`.
    _write(docs / "prompt.md", "[edit](config/strategies.py:184)\n")

    refs = cdl.scan_repo(tmp_path)
    assert len(refs) == 1
    assert refs[0].status == "OK"


def test_angle_bracketed_absolute_path_skipped(tmp_path: Path) -> None:
    """`<C:/path/file.py:1>` (Codex-style) is treated as external."""
    docs = tmp_path / "docs"
    _write(docs / "audit.md", "[x](<C:/Trading Project/phoenix_bot/foo.py:1>)\n")

    refs = cdl.scan_repo(tmp_path)
    assert len(refs) == 1
    assert refs[0].status == "EXTERNAL"


def test_image_links_not_caught(tmp_path: Path) -> None:
    """`![alt](img.png)` (image syntax) is not treated as a link to check."""
    docs = tmp_path / "docs"
    _write(docs / "img.md", "Header\n\n![diagram](does_not_exist.png)\n")

    refs = cdl.scan_repo(tmp_path)
    # We intentionally do not check images — the !-prefixed link should
    # be skipped by the regex.
    assert refs == []
