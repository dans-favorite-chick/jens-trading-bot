"""Regression for REDTEAM-R2-1-FIX (Phase 3f) +
REDTEAM-R2-1-FIX-V2 (Phase 3g, focused red-team correction).

After Phase 3b (commit 69187ec) narrowed the wholesale Sim101 carve-out
on the B47 post-fill verify, the verify NOW runs when a reconciled
orphan is the only Sim101 position open. The verify correctly catches
the stacked-fill mismatch (NT8 reports orphan_qty + new_qty), but the
abort branch previously did a bare `return` — leaving OUR new
`contracts` qty NAKED on NT8 with no OCO and no Phoenix tracking.

The initial Phase 3f fix called `_sink_submit_exit(qty=contracts)`,
but the focused red-team caught a CRITICAL: that primitive ultimately
emits `CLOSEPOSITION;{account};{INSTRUMENT};GTC;;;;;;;;;` with the
qty field EMPTY (see bridge/oif_writer.py:354 and 1296). NT8 flattens
the entire account net position regardless — so the orphan would
have been closed too, leaving its safety-net OCO as zombie working
orders on a flat account.

Phase 3g (REDTEAM-R2-1-FIX-V2) swaps to `_sink_submit_partial_exit`,
which packages `op="PARTIAL_EXIT"` with `qty=n_contracts` (see
bots/_oif_emitter.py:135-155) and NT8 honors the qty. Only OUR
portion closes; the orphan + its safety-net OCO survive.

These tests pin the V2 fix at the source level — a behavioral test
would require ~10+ mocks (await_fill_confirmation, OIF writer,
pending_entry_tracker, telegram, sink_submit_partial_exit,
get_sink_health, history, position_manager). The structural-pin
pattern mirrors tests/test_trade_entry_phantom_guard.py and
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


def test_b47_abort_branch_fires_partial_exit_on_wrong_qty():
    """The B47 abort branch MUST call _sink_submit_partial_exit with
    direction=signal.direction and n_contracts=contracts when
    pos_check.status is wrong_qty or wrong_direction.

    A future refactor that reverts to _sink_submit_exit would re-open
    the CLOSEPOSITION-flattens-everything hole the focused red-team
    identified as CRITICAL after Phase 3f.
    """
    src = _read_trade_entry()
    assert "[NT8_VERIFY:{tid}]" in src, (
        "[NT8_VERIFY:{tid}] log marker missing — B47 block may have been "
        "refactored. Re-check this regression test."
    )
    # Both fix markers must be present (V1 and V2 — V1 stays in the
    # comment for historical context; V2 is the live fix).
    assert "REDTEAM-R2-1-FIX-V2" in src, (
        "REDTEAM-R2-1-FIX-V2 comment marker missing from B47 abort path "
        "— the focused-red-team correction was likely reverted."
    )
    # The conditional discriminator must still gate the flatten body.
    assert re.search(
        r'pos_check\["status"\]\s+in\s+\(\s*"wrong_qty"\s*,\s*"wrong_direction"\s*\)',
        src,
    ) is not None, (
        "The wrong_qty / wrong_direction discriminator that gates the "
        "stacked-fill response is missing or has changed shape."
    )
    # And the trade_id audit-trail suffix.
    assert "_redteam_r2_1_flatten" in src, (
        "The flatten OIF's trade_id suffix is missing — the audit trail "
        "between the aborted entry and its sized partial-exit is broken."
    )


def test_b47_abort_branch_only_flattens_on_stacked_fill_statuses():
    """Defensive: 'flat' and 'missing' statuses (the no-fill cases) must
    NOT trigger the partial-exit. The structural pin asserts the
    conditional gates the flatten body.

    If a refactor expanded the flatten to fire on every non-confirmed
    status, it would emit spurious PARTIAL_EXIT orders against accounts
    where the bot's order never actually landed.
    """
    src = _read_trade_entry()
    fix_idx = src.find("REDTEAM-R2-1-FIX-V2")
    assert fix_idx >= 0
    discriminator_idx = src.find('("wrong_qty", "wrong_direction")', fix_idx)
    partial_exit_idx = src.find("_sink_submit_partial_exit(", discriminator_idx)
    assert discriminator_idx > 0 and partial_exit_idx > discriminator_idx, (
        "The _sink_submit_partial_exit call must appear AFTER the "
        "wrong_qty/wrong_direction conditional — otherwise it would "
        "fire on the no-fill statuses too."
    )


def test_b47_uses_partial_exit_not_close_position():
    """The Phase 3g fix MUST use the sized-PARTIAL_EXIT primitive, NOT
    the full-account CLOSEPOSITION primitive.

    `_sink_submit_exit` ultimately emits the CLOSEPOSITION OIF line
    with an EMPTY qty field — NT8 flattens the entire account net
    position regardless of what qty the Python wrapper accepts. That
    would close the orphan too, leaving its safety-net OCO as zombie
    working orders on a flat account. The focused red-team identified
    this as CRITICAL after Phase 3f.

    `_sink_submit_partial_exit` packages op="PARTIAL_EXIT" with
    qty=n_contracts and NT8 honors the qty (per
    bots/_oif_emitter.py:135-155). Only OUR portion closes; the
    orphan + its safety-net OCO survive.
    """
    src = _read_trade_entry()
    # Locate the V2 fix block.
    fix_idx = src.find("REDTEAM-R2-1-FIX-V2")
    assert fix_idx >= 0
    # Slice everything from V2 marker up to the closing `return` of the
    # B47 abort branch (heuristic: next blank-line + `logger.info`
    # marking the confirmed path).
    confirmed_idx = src.find("Position confirmed", fix_idx)
    assert confirmed_idx > fix_idx
    v2_block = src[fix_idx:confirmed_idx]

    # The block MUST call _sink_submit_partial_exit (with the expected
    # kwargs).
    assert "_sink_submit_partial_exit(" in v2_block, (
        "The V2 fix block does not call _sink_submit_partial_exit — "
        "the wiring may have reverted to _sink_submit_exit, which "
        "emits CLOSEPOSITION and flattens the entire account."
    )
    # And it MUST NOT call _sink_submit_exit (the broken primitive).
    assert "_sink_submit_exit(" not in v2_block, (
        "The V2 fix block calls _sink_submit_exit — that primitive "
        "emits CLOSEPOSITION which ignores qty and flattens the entire "
        "account net position, including the orphan we're trying to "
        "preserve. Use _sink_submit_partial_exit instead."
    )
    # Direction kwarg must come from signal.direction so the sized
    # MARKET order goes the right way.
    assert re.search(
        r"_sink_submit_partial_exit\(\s*\n?\s*direction=signal\.direction",
        v2_block,
    ) is not None, (
        "The partial-exit must pass direction=signal.direction so the "
        "MARKET order opposite-side correctly closes OUR portion."
    )
    # And n_contracts must be `contracts` so the exit sizes to OUR
    # intended entry, not the observed-stack qty.
    assert re.search(
        r"_sink_submit_partial_exit\([\s\S]{0,200}?n_contracts=contracts",
        v2_block,
    ) is not None, (
        "The partial-exit must pass n_contracts=contracts so we close "
        "OUR portion only and leave the orphan untouched."
    )


def test_b47_retries_partial_exit_three_times_with_backoff():
    """The Phase 3g fix MUST retry the partial-exit up to 3 times with
    a 1-second backoff, mirroring the OCO-protect-fail retry pattern
    below. A single-shot best-effort flatten leaves a naked NT8
    position on any sink REFUSE / ERROR / exception.
    """
    src = _read_trade_entry()
    fix_idx = src.find("REDTEAM-R2-1-FIX-V2")
    confirmed_idx = src.find("Position confirmed", fix_idx)
    v2_block = src[fix_idx:confirmed_idx]

    # 3-attempt loop.
    assert re.search(
        r"for\s+_attempt\s+in\s+range\(\s*1\s*,\s*4\s*\)\s*:", v2_block,
    ) is not None, (
        "The V2 fix must use a `for _attempt in range(1, 4):` loop "
        "(mirroring the OCO-fail retry at line ~1050). A single-shot "
        "flatten leaves NT8 naked on the first sink REFUSE/ERROR."
    )
    # 1-second sleep.
    assert "await asyncio.sleep(1.0)" in v2_block, (
        "The retry loop must back off for 1 second between attempts."
    )
    # And a sentinel that distinguishes succeeded-on-some-attempt from
    # failed-all-attempts so the Telegram alert can report accurately.
    assert "partial_exit_ok" in v2_block, (
        "The retry loop must track a success sentinel so the operator "
        "alert can distinguish 'flatten worked' from 'flatten failed "
        "all 3 attempts — manual intervention required'."
    )


def test_b47_record_protect_failed_carries_stacked_fill_reason():
    """The Phase 3g fix MUST pass an explicit reason= to
    record_protect_failed so the operator's dashboard / pause Telegram
    text says "STACKED FILL ..." instead of the default "PROTECT FAILED
    ..." (different root cause, different remediation).
    """
    src = _read_trade_entry()
    fix_idx = src.find("REDTEAM-R2-1-FIX-V2")
    confirmed_idx = src.find("Position confirmed", fix_idx)
    v2_block = src[fix_idx:confirmed_idx]

    # Locate the record_protect_failed call AND assert reason= is passed.
    assert re.search(
        r"record_protect_failed\([\s\S]{0,500}?reason=", v2_block,
    ) is not None, (
        "record_protect_failed must be called with an explicit reason= "
        "argument so the pause-message label says 'STACKED FILL' "
        "rather than the default 'PROTECT FAILED'. The two failure "
        "modes need distinct labels in incident review."
    )
    # And the reason string must contain "STACKED FILL" so a grep on
    # the operator's pause-message log finds it.
    assert "STACKED FILL on" in v2_block, (
        "The reason= text must include 'STACKED FILL on' so the "
        "operator's pause-message log is grep-able for the V2 fix."
    )
