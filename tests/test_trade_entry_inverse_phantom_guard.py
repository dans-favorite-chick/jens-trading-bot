"""Regression for REDTEAM-1-FIX-3B (remediation 2026-06-04 round 2).

The B50 inverse-phantom-guard at bots/_trade_entry.py used to skip
wholesale for ``_account == "Sim101"`` because Sim101 historically
hosted concurrent multi-strategy positions and the
``verify_nt8_position`` check would false-positive on every legit
entry. After commit 257df2f introduced orphan-adoption tagging
(``reconciled=True`` positions), the wholesale-skip became a
defense-in-depth hole on the live canary path — a real signal could
fire on top of an adopted orphan even though NT8 would have
reported the existing fill.

The fix narrows the skip:
    - Sim101 + at least one non-reconciled (real bot) position open
      → skip (preserve legit concurrent-entry semantics)
    - Sim101 + zero or only-reconciled positions open
      → RUN the guard (catch orphan-vs-real double-fill)
    - non-Sim101 → run the guard as before
    - empty/None account → skip defensively (no routing target)

The decision is extracted to module-level
``should_run_inverse_phantom_guard(account, position_manager)`` so it
can be unit-tested directly without rebuilding the full TradeEntry
stack (await_fill_confirmation, OIF writer, pending_entry_tracker,
telegram, history — ~10 mocks).

The companion structural test that catches a future revert to the
wholesale-skip lives in this file too — mirrors the test_phantom_guard
pattern shipped 2026-06-02.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from bots._trade_entry import should_run_inverse_phantom_guard


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_ENTRY = REPO_ROOT / "bots" / "_trade_entry.py"


def _read_trade_entry() -> str:
    return TRADE_ENTRY.read_text(encoding="utf-8")


class _StubPositionManager:
    """Minimal PositionManager double for testing the guard decision
    in isolation. Only the attributes the helper reads need to exist:
    ``active_positions`` returning an iterable of objects with
    ``.account`` and ``.reconciled`` attributes.
    """

    def __init__(self, *positions):
        self._positions = list(positions)

    @property
    def active_positions(self):
        return list(self._positions)


def _pos(account: str, reconciled: bool):
    return SimpleNamespace(account=account, reconciled=reconciled)


# ── Unit tests for the extracted decision helper ──────────────────────


def test_empty_account_returns_false():
    """Defensive: no account string → nothing to verify against."""
    assert should_run_inverse_phantom_guard("", _StubPositionManager()) is False
    assert should_run_inverse_phantom_guard(None, _StubPositionManager()) is False


def test_non_sim101_account_always_runs_guard():
    """Any non-empty, non-Sim101 account routes through the verify path
    regardless of local position state — unchanged behavior from before
    the narrowing."""
    pm = _StubPositionManager()
    assert should_run_inverse_phantom_guard("SimBias Momentum", pm) is True
    # Even with an open position on that account, non-Sim101 always verifies.
    pm2 = _StubPositionManager(_pos("SimBias Momentum", reconciled=False))
    assert should_run_inverse_phantom_guard("SimBias Momentum", pm2) is True


def test_sim101_empty_local_state_runs_guard():
    """No open Sim101 positions locally → run the guard. Any NT8-reported
    position would be an orphan; the guard correctly catches it."""
    pm = _StubPositionManager()
    assert should_run_inverse_phantom_guard("Sim101", pm) is True


def test_sim101_only_reconciled_position_runs_guard():
    """Only reconciled (orphan) positions on Sim101 — the orphan is NOT a
    real bot fill, so the guard MUST run to catch a real-signal fill
    arriving on top of it."""
    pm = _StubPositionManager(_pos("Sim101", reconciled=True))
    assert should_run_inverse_phantom_guard("Sim101", pm) is True


def test_sim101_real_bot_position_skips_guard():
    """At least one non-reconciled (real bot) position open on Sim101 →
    skip the guard. This preserves legit multi-strategy concurrent entry
    on Sim101 — without this branch the inverse-phantom check would
    false-positive on the existing real bot position and reject every
    second concurrent entry."""
    pm = _StubPositionManager(_pos("Sim101", reconciled=False))
    assert should_run_inverse_phantom_guard("Sim101", pm) is False


def test_sim101_mixed_real_and_reconciled_skips_guard():
    """Mixed state — at least one real bot position present → skip. The
    presence of orphans alongside real bot trades is plausible if the
    bot crashed/restarted while a real trade was open; the real trade
    keeps its priority in the decision."""
    pm = _StubPositionManager(
        _pos("Sim101", reconciled=True),
        _pos("Sim101", reconciled=False),
    )
    assert should_run_inverse_phantom_guard("Sim101", pm) is False


def test_sim101_positions_on_other_accounts_dont_unskip():
    """A non-Sim101 open position must NOT affect the Sim101 decision —
    the filter is account-scoped, not bot-wide."""
    pm = _StubPositionManager(
        _pos("SimBias Momentum", reconciled=False),  # ← different account
    )
    # No real Sim101 positions, so the guard must run.
    assert should_run_inverse_phantom_guard("Sim101", pm) is True


# ── Structural lock-in (mirrors test_trade_entry_phantom_guard pattern) ──


def test_inverse_phantom_guard_no_longer_wholesale_skips_sim101():
    """The wholesale `if _account and _account != "Sim101"` pattern that
    used to gate the B50 verify_nt8_position block must NOT be present.

    A future refactor that re-introduces it (e.g. someone reverting this
    commit) would silently re-open the orphan-vs-real double-fill hole
    on Sim101. This structural test catches that revert at test time so
    the operator gets a loud failure instead of a quiet regression.
    """
    src = _read_trade_entry()
    # The verify_nt8_position call site MUST still exist (that's the
    # guard we're narrowing, not eliminating).
    assert "verify_nt8_position" in src
    # And the new helper MUST be invoked at the call site.
    assert "should_run_inverse_phantom_guard(" in src, (
        "The decision helper is no longer wired up at the call site — "
        "the narrowing logic was removed or refactored. Re-check."
    )
    # The old wholesale-skip pattern must NOT be present anywhere in
    # the verify block. (PHANTOM_GUARD's later block is separate and
    # already covered by tests/test_trade_entry_phantom_guard.py.)
    inverse_pattern = re.compile(
        r"if\s*\(\s*_account\s+and\s+_account\s*!=\s*\"Sim101\"\s*\)\s*:"
        r"[\s\S]{0,500}verify_nt8_position"
    )
    assert inverse_pattern.search(src) is None, (
        "Wholesale `if _account and _account != \"Sim101\":` pattern "
        "leading into a verify_nt8_position call is back — revert of "
        "REDTEAM-1-FIX-3B detected."
    )
