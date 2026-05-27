"""Helper for tests that grep for code patterns that may have moved during
the P4-1 base_bot decomposition (2026-05-24).

Pre-decomposition, every static check was `needle in BASE_BOT_SRC`. After
Stage 1-4 extractions, code lives across base_bot.py and 19+ `bots/_*.py`
modules. The right semantic question for most existing tests is "does this
behavior live SOMEWHERE in the bot's runtime path?" — not "is it physically
in base_bot.py?" — so this helper searches base_bot + every extracted module.

Usage:
    from tests._bot_src_search import bot_source_contains
    assert bot_source_contains("some_substring"), "missing wiring"
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

_ROOT = Path(__file__).resolve().parent.parent
_BOTS_DIR = _ROOT / "bots"


@lru_cache(maxsize=1)
def _all_bot_sources() -> List[tuple[Path, str]]:
    """Cached list of (path, source) for base_bot.py + all bots/_*.py."""
    paths: list[Path] = []
    base = _BOTS_DIR / "base_bot.py"
    if base.exists():
        paths.append(base)
    # Sorted for deterministic iteration; alphabetical by filename.
    paths.extend(sorted(_BOTS_DIR.glob("_*.py")))
    return [(p, p.read_text(encoding="utf-8")) for p in paths]


def bot_source_contains(needle: str) -> bool:
    """True if `needle` appears in base_bot.py or any extracted bots/_*.py."""
    return any(needle in src for _, src in _all_bot_sources())


def bot_source_findall(needle: str) -> List[str]:
    """Return list of file paths (as strings) containing the needle."""
    return [str(p) for p, src in _all_bot_sources() if needle in src]


def bot_combined_source() -> str:
    """Return the concatenation of base_bot.py + all bots/_*.py sources.

    Useful for regex assertions that span the bot codebase. Each file is
    separated by a comment marker for grep-readability.
    """
    parts: list[str] = []
    for p, src in _all_bot_sources():
        parts.append(f"\n# === {p.name} ===\n")
        parts.append(src)
    return "".join(parts)


def bot_source_matches(*needles: str) -> bool:
    """True if ANY of `needles` appears in any bot source file. Accepts
    multiple variant forms (e.g., 'self.X', 'self.bot.X', 'bot.X') and
    passes if any one is present. Lets a single assertion tolerate the
    P4-1 self→self.bot→bot rewrite across extracted modules."""
    return any(bot_source_contains(n) for n in needles)
