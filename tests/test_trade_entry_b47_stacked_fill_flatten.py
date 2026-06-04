"""Regression for REDTEAM-R2-1-FIX (remediation 2026-06-04 round 2).

After Phase 3b (commit 69187ec) narrowed the wholesale Sim101 carve-out
on the B47 post-fill verify, the verify NOW runs when a reconciled
orphan is the only Sim101 position open. The verify correctly catches
the stacked-fill mismatch (NT8 reports orphan_qty + new_qty), but the
abort branch previously did a bare `return` — leaving OUR new
`contracts` qty NAKED on NT8 with no OCO and no Phoenix tracking. The
P4 red-team's stress-scenario walk-through called this "one timing
race away from a live double-fill".

The fix mirrors the existing OCO-fail-flatten machinery (lines
~1094-1156 in bots/_trade_entry.py): on wrong_qty or wrong_direction,
trip nt8_sink_health, fire _sink_submit_exit for qty=contracts to
close out OUR portion, telegram-alert. The orphan + its safety-net OCO
(attached at reconcile time) are left untouched.

These tests pin the fix at the source level — a behavioral test would
require ~10+ mocks (await_fill_confirmation, OIF writer,
pending_entry_tracker, telegram, sink_submit_exit, get_sink_health,
history, position_manager). The structural-pin pattern mirrors the
existing tests/test_trade_entry_phantom_guard.py and
tests/test_trade_entry_inverse_phantom_guard.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_ENTRY = REPO_ROOT / "bots" / "_trade_entry.py"


def _read_trade_entry() -> str:
    return TRADE_ENTRY.read_text(encoding="utf-8")


def test_b47_abort_branch_fires_emergency_flatten_on_wrong_qty():
    """The B47 abort branch MUST call _sink_submit_exit with
    reason="STACKED_FILL_FLATTEN" when pos_check.status is
    wrong_qty or wrong_direction.

    A future refactor that removes the flatten call (e.g. someone
    "simplifying" the abort path back to a bare return) would re-open
    the naked-fill window the P4 red-team identified as CRITICAL.
    """
    src = _read_trade_entry()
    # The NT8_VERIFY block must still exist (this is what we're guarding).
    assert "[NT8_VERIFY:{tid}]" in src, (
        "[NT8_VERIFY:{tid}] log marker missing — B47 block may have been "
        "refactored. Re-check this regression test."
    )
    # The fix marker must be present.
    assert "REDTEAM-R2-1-FIX" in src, (
        "REDTEAM-R2-1-FIX comment marker missing from B47 abort path — "
        "the emergency-flatten wiring was likely reverted."
    )
    # The conditional wrong_qty / wrong_direction discriminator MUST be
    # present in the source — without it the flatten would fire on
    # 'flat' / 'missing' too, which is wrong (nothing to flatten there).
    assert re.search(
        r'pos_check\["status"\]\s+in\s+\(\s*"wrong_qty"\s*,\s*"wrong_direction"\s*\)',
        src,
    ) is not None, (
        "The wrong_qty / wrong_direction discriminator that gates the "
        "emergency-flatten is missing or has changed shape — the flatten "
        "might now fire on every non-confirmed status (including flat "
        "and missing), which would be incorrect (no fill = nothing to "
        "flatten)."
    )
    # The flatten itself MUST be _sink_submit_exit with the canonical
    # reason string the operator can grep for in incident review.
    assert "STACKED_FILL_FLATTEN" in src, (
        "STACKED_FILL_FLATTEN reason string missing — the flatten call "
        "may have been removed or renamed, breaking incident-log grep."
    )
    # And the trade_id for the flatten OIF must be tied to the trade
    # being aborted via the `_redteam_r2_1_flatten` suffix, so post-
    # mortem can correlate.
    assert "_redteam_r2_1_flatten" in src, (
        "The flatten OIF's trade_id suffix is missing — the audit trail "
        "between the aborted entry and its emergency flatten is broken."
    )


def test_b47_abort_branch_only_flattens_on_stacked_fill_statuses():
    """Defensive: 'flat' and 'missing' statuses (the no-fill cases) must
    NOT trigger emergency flatten. The structural pin asserts the
    conditional gates the flatten body.

    If a refactor expanded the flatten to fire on every non-confirmed
    status, it would emit spurious CLOSEPOSITION orders against accounts
    where the bot's order never actually landed — making the orphan
    state worse, not better.
    """
    src = _read_trade_entry()
    # Locate the REDTEAM-R2-1-FIX block and verify the flatten is INSIDE
    # the `if pos_check["status"] in ("wrong_qty", "wrong_direction"):` arm.
    # Heuristic: the `_sink_submit_exit` call with STACKED_FILL_FLATTEN
    # must appear AFTER the wrong_qty conditional and BEFORE the next
    # `return`.
    fix_idx = src.find("REDTEAM-R2-1-FIX")
    assert fix_idx >= 0
    discriminator_idx = src.find(
        '("wrong_qty", "wrong_direction")', fix_idx
    )
    flatten_idx = src.find("STACKED_FILL_FLATTEN", discriminator_idx)
    assert discriminator_idx > 0 and flatten_idx > discriminator_idx, (
        "The STACKED_FILL_FLATTEN call must appear AFTER the "
        "wrong_qty/wrong_direction conditional — otherwise it would "
        "fire unconditionally on every status mismatch including the "
        "no-fill statuses."
    )


def test_b47_flatten_uses_only_contracts_not_observed_qty():
    """The flatten MUST size the exit to OUR contribution (`contracts`),
    NOT to the observed qty NT8 is reporting. NT8's observed qty includes
    any existing orphan position — flattening that would close the
    orphan too, which is wrong: the orphan has its own safety-net OCO
    attached at reconcile time and Phoenix tracking via the reconciled
    Position record.

    A flatten with qty=observed_qty would close BOTH our contribution
    AND the orphan, leaving the operator confused about why their manual
    fill disappeared.
    """
    src = _read_trade_entry()
    # Grab the line that calls _sink_submit_exit with STACKED_FILL_FLATTEN.
    # It must pass `qty=contracts`, not `qty=pos_check.get(...)` or similar.
    m = re.search(
        r"_sink_submit_exit\(\s*\n?\s*qty=([^,\)]+),"
        r"[\s\S]{0,400}?"
        r'reason="STACKED_FILL_FLATTEN"',
        src,
    )
    assert m is not None, (
        "Could not locate the _sink_submit_exit call associated with "
        "STACKED_FILL_FLATTEN. Source structure may have changed; "
        "update this regression test."
    )
    qty_expr = m.group(1).strip()
    assert qty_expr == "contracts", (
        f"The STACKED_FILL_FLATTEN call MUST size the exit to OUR "
        f"`contracts` only (NOT observed_qty), so the orphan position "
        f"(with its own safety-net OCO) survives. Found qty={qty_expr!r}."
    )
