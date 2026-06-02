"""Regression for the 2026-06-02 chart-orders incident.

PHANTOM_GUARD in `bots/_trade_entry.py` previously exempted
`_account == "Sim101"` via the condition
`if (_account and _account != "Sim101"):`. That carve-out was added
when Sim101 was treated as a fully-mocked path; on 2026-06-02 it
caused prod_bot's NT8-rejected OIFs to fall through to "assume filled
(paper mode)", leaving 14 stale BUY LIMITs in NT8's `incoming/` that
became working orders when ATI recovered ~11:00 CT.

Full root cause:
`logs/oracle/research/2026-06-02_chart_orders_root_cause.md`.

The fix lifts the carve-out. PHANTOM_GUARD must now run for any
non-LIVE account when an OIF is stuck — including Sim101.

These tests guard the condition at the source level so a future refactor
can't silently re-introduce the exemption. A behavioural enter_trade
test would require ~10 mocks (await_fill_confirmation, OIF writer,
position manager, pending_entry_tracker, telegram, history, etc.) and
the cost/benefit doesn't pay; the structural test catches the regression
that mattered.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_ENTRY = REPO_ROOT / "bots" / "_trade_entry.py"


def _read_trade_entry() -> str:
    return TRADE_ENTRY.read_text(encoding="utf-8")


def test_phantom_guard_branch_does_not_exempt_sim101():
    """The PHANTOM_GUARD branch must not exempt Sim101.

    The guard condition sits immediately above the
    `[PHANTOM_GUARD:{tid}]` CRITICAL log. It must check only that an
    account is set, not that the account is not Sim101.
    """
    src = _read_trade_entry()
    assert "[PHANTOM_GUARD:{tid}]" in src, (
        "PHANTOM_GUARD CRITICAL log line missing — the branch may have "
        "been refactored. Re-check this regression test."
    )
    # Find the line that opens the PHANTOM_GUARD branch — it is the
    # `if _account` (or similar) immediately preceding the glob lookup
    # for stuck OIFs.
    m = re.search(
        r"^(\s*)if (.*?):\s*\n\s+import glob as _glob",
        src,
        re.MULTILINE,
    )
    assert m, (
        "Could not locate the PHANTOM_GUARD opening condition. The "
        "structure of bots/_trade_entry.py may have changed; update "
        "this regression test accordingly."
    )
    cond = m.group(2)
    assert '_account != "Sim101"' not in cond, (
        f"PHANTOM_GUARD branch still exempts Sim101 — condition is "
        f"`if {cond}:`. Today's 2026-06-02 incident hinged on this "
        f"exemption. See "
        f"logs/oracle/research/2026-06-02_chart_orders_root_cause.md."
    )


def test_phantom_guard_admits_sim101_at_runtime():
    """Simulate the PHANTOM_GUARD condition logic directly.

    The fix replaces `if (_account and _account != "Sim101"):` with
    `if _account:`. This test pins the new semantics: Sim101 must now
    enter the guard branch.
    """
    src = _read_trade_entry()
    # Grab the opening condition again, evaluated against each test
    # account value to confirm the branch is taken.
    m = re.search(
        r"^\s*if (.*?):\s*\n\s+import glob as _glob",
        src,
        re.MULTILINE,
    )
    assert m, "PHANTOM_GUARD opening condition not found"
    cond = m.group(1).strip()

    # Evaluate the condition for the accounts that matter. The condition
    # references only `_account`, so we can eval it in a tiny namespace.
    def _enters_guard(account: str | None) -> bool:
        return bool(eval(cond, {}, {"_account": account}))

    assert _enters_guard("Sim101") is True, (
        "Sim101 must enter PHANTOM_GUARD post-fix"
    )
    assert _enters_guard("SimBias Momentum") is True, (
        "Per-strategy sim sub-accounts must continue to enter PHANTOM_GUARD"
    )
    assert _enters_guard(None) is False, (
        "Empty/None account must skip PHANTOM_GUARD (defensive fall-through)"
    )
    assert _enters_guard("") is False, (
        "Empty string account must skip PHANTOM_GUARD (defensive fall-through)"
    )
