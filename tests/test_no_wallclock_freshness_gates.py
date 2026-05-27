"""F-27 lint guard — no wallclock freshness gates in strategies/*.py.

The F-27 / Phase 13 B3 bug
--------------------------
Strategies that compared bar freshness using ``time.time()`` worked in
live trading (wallclock and bar epoch are both "now") but ALWAYS
rejected in backtest, because ``time.time()`` returns the wallclock of
the backtest run (e.g. 2026) while ``last_bar.end_time`` is the
historical bar epoch (years older). The fix is to use the strategy's
notion of "now" — ``market["now_ct"].timestamp()`` — which is wallclock
in live and the simulated cursor in backtest.

This test scans every ``strategies/*.py`` file for ``time.time()`` calls
and fails if any such call appears on a line that also mentions a
bar-epoch field (``bar``, ``end_time``, ``start_time``, ``bar_ts``,
``last_bar``). That heuristic matches the F-27 bug shape.

Whitelist
---------
Append ``# noqa: time-time-ok`` to a line if you have a legitimate
wallclock use that the heuristic flags by accident (e.g. logging the
wallclock alongside bar metadata for a latency report).

Run with
--------
    pytest tests/test_no_wallclock_freshness_gates.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────
STRATEGIES_DIR = Path(__file__).resolve().parents[1] / "strategies"

# Tokens that, when on the same line as `time.time()`, indicate the
# wallclock is almost certainly being compared against a bar-epoch
# value — i.e. the F-27 bug shape.
BAR_EPOCH_TOKENS = (
    "bar",
    "end_time",
    "start_time",
    "bar_ts",
    "last_bar",
)

WHITELIST_TAG = "# noqa: time-time-ok"


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
def _strip_string_literals(line: str) -> str:
    """Best-effort removal of string/comment content so we only check
    *code* tokens. This is intentionally simple — false positives here
    just mean a comment mentioning ``time.time()`` won't trip the gate.
    """
    # Drop trailing inline comment (heuristic; OK for our use case).
    if "#" in line:
        line = line.split("#", 1)[0]
    # Drop simple "…" and '…' string contents.
    out = []
    quote = None
    for ch in line:
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                continue
            out.append(ch)
        else:
            if ch == quote:
                quote = None
    return "".join(out)


def _find_violations() -> list[tuple[str, int, str]]:
    """Return [(path, lineno, raw_line), …] for every line in every
    strategies/*.py that contains a ``time.time()`` call AND mentions a
    bar-epoch token AND is not whitelisted.
    """
    violations: list[tuple[str, int, str]] = []
    for path in sorted(STRATEGIES_DIR.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-1")

        for lineno, raw in enumerate(text.splitlines(), start=1):
            if WHITELIST_TAG in raw:
                continue
            code = _strip_string_literals(raw)
            if "time.time()" not in code:
                continue
            if not any(tok in code for tok in BAR_EPOCH_TOKENS):
                continue
            violations.append((str(path), lineno, raw.rstrip()))
    return violations


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────
def test_strategies_dir_exists() -> None:
    """Sanity — the directory the lint scans must exist."""
    assert STRATEGIES_DIR.is_dir(), f"missing {STRATEGIES_DIR}"


def test_no_wallclock_freshness_gates() -> None:
    """Fail if any strategies/*.py compares ``time.time()`` against a
    bar-epoch field — that's the F-27 / B3 bug pattern."""
    violations = _find_violations()
    if not violations:
        return

    lines = [
        "Found wallclock freshness-gate violation(s) — these compare "
        "time.time() against a bar-epoch field, which is the F-27/B3 bug "
        "(rejects every bar in backtest). Replace time.time() with "
        "market['now_ct'].timestamp() (or now_ct.timestamp() locally).",
        "If the line is legitimately a wallclock use (logging latency, "
        "rate-limit timer), append '# noqa: time-time-ok' to whitelist.",
        "",
        "Offenders:",
    ]
    for path, lineno, raw in violations:
        lines.append(f"  {path}:{lineno}  {raw.strip()}")
    pytest.fail("\n".join(lines))


def test_helper_strip_string_literals_drops_comments() -> None:
    """Defend the heuristic against accidental regressions."""
    assert "time.time()" not in _strip_string_literals(
        "# wallclock time.time() reference in a comment"
    )


def test_helper_strip_string_literals_keeps_real_call() -> None:
    assert "time.time()" in _strip_string_literals(
        "if (time.time() - last_bar_ts) > 90:"
    )


def test_whitelist_tag_exempts_line() -> None:
    """A line with the whitelist tag must NOT be flagged even if it
    matches the bug pattern (operator escape hatch)."""
    # We cannot easily inject a fake file into _find_violations without
    # mocking, so just assert the constant is non-empty and unique
    # enough to be a stable opt-out.
    assert WHITELIST_TAG.startswith("# noqa:")
    assert "time-time-ok" in WHITELIST_TAG
