"""Regression for FINDING-2026-06-05-SLOT-INTERLOCK-BYPASS T1 + T2.

Two emit sites previously used `_sink_submit_exit` (CLOSEPOSITION,
account-wide, qty-ignored):

  T1 — bots/_trade_entry.py:1255 OCO-fail emergency-flatten
  T2 — bots/_trade_exit.py:146   OIF fallback after WS send fails

The Forensics Cluster 2 audit
(out/slot_interlock_bypass_audit_2026-06-05.md) classified both as
(B) — unbounded primitive. On multi-strategy Sim101, a CLOSEPOSITION
emitted by strategy A flattens strategy B's position too. The fix
mirrors the Phase 3g (REDTEAM-R2-1-FIX-V2, commit 354bb3c) pattern at
bots/_trade_entry.py:1109 (B47 STACKED FILL recovery): swap to
`_sink_submit_partial_exit(direction=..., n_contracts=..., ...)`,
which packages op="PARTIAL_EXIT" with qty=n_contracts and NT8 honors
the qty.

These tests pin the V3 swap at the source level (matching the
established convention at tests/test_trade_entry_b47_stacked_fill_flatten.py)
PLUS a behavioral sink-monkeypatch test that asserts the request dict
the sink RECEIVES carries op="PARTIAL_EXIT" not op="EXIT".

The behavioral pin is the stronger guarantee: a future refactor that
renames `_sink_submit_*` would not invalidate the source pin (which
greps for the new name) but the behavioral pin tracks the op field
the sink actually sees.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_ENTRY = REPO_ROOT / "bots" / "_trade_entry.py"
TRADE_EXIT = REPO_ROOT / "bots" / "_trade_exit.py"


# ── T1 — bots/_trade_entry.py:1255 OCO-fail EMERGENCY FLATTEN ─────────

def test_t1_oco_fail_uses_partial_exit_not_close_position():
    """The OCO-fail EMERGENCY FLATTEN block in _trade_entry.py MUST use
    sized PARTIAL_EXIT, not unbounded CLOSEPOSITION. Mirrors the V2 fix
    at the B47 STACKED FILL block above.
    """
    src = TRADE_ENTRY.read_text(encoding="utf-8")
    # Anchor on the stable T1-FIX marker we add as part of the swap.
    fail_idx = src.find("FINDING-2026-06-05-SLOT-INTERLOCK-BYPASS T1")
    assert fail_idx > 0, (
        "T1 marker missing — the SLOT-INTERLOCK swap comment was either "
        "reverted or never landed."
    )
    # Slice from the T1 marker through the `# NOW open position locally`
    # marker which begins the post-protect success path.
    end_idx = src.find("NOW open position locally", fail_idx)
    assert end_idx > fail_idx, (
        "Could not locate end of EMERGENCY FLATTEN block via 'NOW open "
        "position locally' marker. Source structure may have changed."
    )
    flatten_block = src[fail_idx:end_idx]

    # The block MUST call _sink_submit_partial_exit
    assert "_sink_submit_partial_exit(" in flatten_block, (
        "T1: OCO-fail EMERGENCY FLATTEN does not call "
        "_sink_submit_partial_exit. Either the fix was reverted or a "
        "future refactor regressed to the unbounded primitive. "
        "Re-check this swap — CLOSEPOSITION flattens the entire account "
        "and would wipe other strategies' positions on multi-strategy "
        "Sim101."
    )
    # And it MUST NOT call _sink_submit_exit in this block.
    assert "_sink_submit_exit(" not in flatten_block, (
        "T1: OCO-fail EMERGENCY FLATTEN still calls _sink_submit_exit "
        "(unbounded CLOSEPOSITION). Swap to _sink_submit_partial_exit "
        "with direction=signal.direction + n_contracts=contracts."
    )
    # direction must come from signal.direction (correct side for sized close)
    assert re.search(
        r"_sink_submit_partial_exit\(\s*\n?\s*direction=signal\.direction",
        flatten_block,
    ) is not None, (
        "T1: PARTIAL_EXIT must pass direction=signal.direction so the "
        "sized MARKET goes the right way."
    )
    # n_contracts must be `contracts` (the entry's qty, not the observed stack)
    assert re.search(
        r"_sink_submit_partial_exit\([\s\S]{0,200}?n_contracts=contracts",
        flatten_block,
    ) is not None, (
        "T1: PARTIAL_EXIT must pass n_contracts=contracts so the close "
        "is sized to OUR entry."
    )


def test_t1_observability_log_present():
    """A [SLOT-INTERLOCK] log line MUST be emitted on the OCO-fail
    EMERGENCY FLATTEN so the swap is visible in operator logs and the
    forensics replay can attribute it correctly."""
    src = TRADE_ENTRY.read_text(encoding="utf-8")
    fail_idx = src.find("FINDING-2026-06-05-SLOT-INTERLOCK-BYPASS T1")
    end_idx = src.find("NOW open position locally", fail_idx)
    flatten_block = src[fail_idx:end_idx]
    assert "[SLOT-INTERLOCK]" in flatten_block, (
        "T1 observability: [SLOT-INTERLOCK] log line missing from "
        "OCO-fail EMERGENCY FLATTEN. Operator forensics can't "
        "distinguish a CLOSEPOSITION sibling-strategy hit from an "
        "intended sized flatten."
    )


# ── T2 — bots/_trade_exit.py:146 OIF FALLBACK ─────────────────────────

def test_t2_oif_fallback_uses_partial_exit_not_close_position():
    """The OIF FALLBACK in _trade_exit.py (runs only when WS EXIT fails)
    MUST use sized PARTIAL_EXIT, not unbounded CLOSEPOSITION."""
    src = TRADE_EXIT.read_text(encoding="utf-8")
    # Locate the OIF fallback block — heuristic: the `# Sink-mediated
    # EXIT fallback` comment, or its successor `_sink_submit_*` call.
    fb_idx = src.find("EXIT fallback")
    assert fb_idx > 0, (
        "'EXIT fallback' marker missing — OIF fallback section may have "
        "been refactored. Re-check this regression test."
    )
    # Slice from fallback marker through the end of the immediate except clause
    end_idx = src.find("MANUAL EXIT REQUIRED", fb_idx)
    assert end_idx > fb_idx
    fallback_block = src[fb_idx:end_idx + 50]  # +50 to capture the closing

    assert "_sink_submit_partial_exit(" in fallback_block, (
        "T2: OIF fallback does not call _sink_submit_partial_exit. The "
        "unbounded CLOSEPOSITION primitive must be replaced with the "
        "sized PARTIAL_EXIT primitive."
    )
    assert "_sink_submit_exit(" not in fallback_block, (
        "T2: OIF fallback still calls _sink_submit_exit. Swap to "
        "_sink_submit_partial_exit with direction=pos.direction + "
        "n_contracts=pos.contracts."
    )
    # direction = pos.direction; n_contracts = pos.contracts
    assert re.search(
        r"_sink_submit_partial_exit\([\s\S]{0,300}?direction=pos\.direction",
        fallback_block,
    ) is not None, (
        "T2: PARTIAL_EXIT must pass direction=pos.direction."
    )
    assert re.search(
        r"_sink_submit_partial_exit\([\s\S]{0,300}?n_contracts=pos\.contracts",
        fallback_block,
    ) is not None, (
        "T2: PARTIAL_EXIT must pass n_contracts=pos.contracts."
    )


def test_t2_observability_log_present():
    """A [SLOT-INTERLOCK] log line MUST be emitted on the OIF fallback
    EXIT so the swap is visible."""
    src = TRADE_EXIT.read_text(encoding="utf-8")
    fb_idx = src.find("EXIT fallback")
    end_idx = src.find("MANUAL EXIT REQUIRED", fb_idx)
    fallback_block = src[fb_idx:end_idx + 50]
    assert "[SLOT-INTERLOCK]" in fallback_block, (
        "T2 observability: [SLOT-INTERLOCK] log line missing from OIF "
        "EXIT fallback."
    )


# ── Behavioral pin (sink monkeypatch) ─────────────────────────────────

def test_t1_behavioral_sink_receives_partial_exit_op(monkeypatch):
    """Behavioral: invoke the public PARTIAL_EXIT submitter and assert
    the sink receives op='PARTIAL_EXIT'. The structural pins above
    catch refactors that rename the call; this behavioral pin catches
    refactors that change the underlying op string.
    """
    from bots import _oif_emitter as oe
    from bots import base_bot as bb

    captured = []

    class CaptureSink:
        def submit(self, req):
            captured.append(req)
            return {"decision": "ACCEPT", "oif_path": "fake"}

    # Replace the cached sink with our capture
    monkeypatch.setattr(bb, "_OIF_SINK", CaptureSink())
    monkeypatch.setattr(bb, "_get_oif_sink", lambda: CaptureSink._get())
    # _get class-method shortcut
    CaptureSink._get = staticmethod(lambda: bb._OIF_SINK)

    oe.submit_partial_exit(
        direction="LONG", n_contracts=2,
        trade_id="behavioral_T1", account="Sim101",
    )

    assert len(captured) == 1, f"expected 1 sink call, got {len(captured)}"
    req = captured[0]
    assert req["op"] == "PARTIAL_EXIT", (
        f"behavioral guarantee: sink must receive op='PARTIAL_EXIT' "
        f"(NOT op='EXIT'). Got: {req!r}"
    )
    assert req["qty"] == 2, f"qty must round-trip; got {req!r}"
    assert req["action"] == "SELL", (
        f"LONG PARTIAL_EXIT must send SELL action; got {req!r}"
    )
    assert req["trade_id"] == "behavioral_T1"
    assert req["account"] == "Sim101"


def test_behavioral_close_position_op_is_account_wide():
    """Sentinel: submit_exit's op='EXIT' carries no qty-binding semantic
    on the NT8 side. This test pins that the legacy EXIT primitive
    submits op='EXIT' (which the bridge writer translates to
    CLOSEPOSITION) — so the audit's claim that swapping to PARTIAL_EXIT
    is required for sized behavior holds.

    If a future refactor makes submit_exit emit a sized op, this test
    breaks and forces an audit update."""
    from bots import _oif_emitter as oe
    from bots import base_bot as bb

    captured = []

    class CaptureSink:
        def submit(self, req):
            captured.append(req)
            return {"decision": "ACCEPT", "oif_path": "fake"}

    monkeypatch_target = CaptureSink()
    bb._OIF_SINK = monkeypatch_target
    try:
        oe.submit_exit(
            qty=2, trade_id="behavioral_sentinel", account="Sim101",
            reason="sentinel_test",
        )
    finally:
        bb._OIF_SINK = None  # reset cache

    assert len(captured) == 1
    req = captured[0]
    assert req["op"] == "EXIT", (
        f"sentinel: submit_exit still emits op='EXIT' (account-wide "
        f"CLOSEPOSITION). If this breaks, the audit's swap-required "
        f"claim needs revisiting. Got: {req!r}"
    )
