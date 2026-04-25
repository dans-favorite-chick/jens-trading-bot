"""
Multi-account close-isolation tests (2026-04-25).

Jennifer's question: if prod_bot, sim_bot, and a hypothetical live bot all
have open positions on different NT8 accounts, will closing ONE trade
emit an OIF only for that ONE account? Or could the close cascade and
flatten positions on other accounts too?

These tests prove the per-account isolation invariant by:
1. Setting up 3 positions on 3 different NT8 accounts
2. Closing ONE of them
3. Asserting the resulting OIF carries the targeted account only
4. Asserting no OIF is written for the other accounts

Defenses validated:
- bridge.oif_writer.write_oif("EXIT", ...) emits CLOSEPOSITION scoped
  to the explicit `account` argument, NOT a global "close all"
- bridge.oif_writer.close_position_line(account=...) refuses empty account
- bridge.oif_writer.write_oif("CANCEL_ALL", ...) is hard-blocked (B75)
- The pid-tag in OIF filenames means rogue (non-Phoenix) writers can't
  inject orders that masquerade as Phoenix closes
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import oif_writer  # type: ignore
from bridge.oif_writer import (
    write_oif,
    close_position_line,
    cancel_single_order_line,
)


@pytest.fixture
def tmp_oif_dirs(tmp_path, monkeypatch):
    """Redirect OIF writes into a tmp dir so we can inspect them."""
    incoming = tmp_path / "incoming"
    outgoing = tmp_path / "outgoing"
    incoming.mkdir()
    outgoing.mkdir()
    monkeypatch.setattr(oif_writer, "OIF_INCOMING", str(incoming))
    monkeypatch.setattr(oif_writer, "OIF_OUTGOING", str(outgoing))
    return incoming, outgoing


def _read_all_oif_contents(incoming: Path) -> list[str]:
    """Return contents of every .txt OIF file in the incoming dir."""
    return [p.read_text(encoding="utf-8").strip()
            for p in sorted(incoming.glob("*.txt"))]


def test_close_one_account_does_not_emit_for_others(tmp_oif_dirs):
    """
    Three concurrent positions on three different NT8 accounts. Closing
    just `SimBias Momentum` must produce exactly ONE CLOSEPOSITION OIF,
    targeted to that account. Sim101 and SimORB must remain untouched.
    """
    incoming, _ = tmp_oif_dirs

    # Three positions on three accounts (simulating prod_bot Sim101 +
    # sim_bot SimBias Momentum + sim_bot SimORB all holding trades)
    accounts = ["Sim101", "SimBias Momentum", "SimORB"]

    # Close ONLY "SimBias Momentum" (the middle one)
    target = "SimBias Momentum"
    paths = write_oif(
        action="EXIT",
        qty=1,
        trade_id="trade_for_bias_momentum_only",
        account=target,
    )

    # Exactly one OIF should have been emitted
    contents = _read_all_oif_contents(incoming)
    assert len(contents) == 1, \
        f"Expected exactly 1 OIF for closing 1 trade, got {len(contents)}: {contents}"

    # That OIF must be a CLOSEPOSITION targeted at the right account
    oif = contents[0]
    assert oif.startswith(f"CLOSEPOSITION;{target};"), \
        f"OIF must close exactly {target!r}, got: {oif}"

    # The OTHER accounts must NOT appear in any OIF
    for other in accounts:
        if other == target:
            continue
        assert other not in oif, \
            f"Closing {target} must NOT mention {other}, but OIF was: {oif}"


def test_close_each_of_three_accounts_emits_isolated_oifs(tmp_oif_dirs):
    """
    Closing each of 3 accounts in sequence emits 3 distinct, account-
    scoped CLOSEPOSITIONs — one per account, none cross-contaminated.
    """
    incoming, _ = tmp_oif_dirs

    accounts = ["Sim101", "SimBias Momentum", "SimORB"]
    for i, acct in enumerate(accounts):
        write_oif(
            action="EXIT",
            qty=1,
            trade_id=f"trade_{i}_{acct.replace(' ', '_')}",
            account=acct,
        )

    contents = _read_all_oif_contents(incoming)
    assert len(contents) == 3, \
        f"Expected 3 OIFs for 3 closes, got {len(contents)}"

    # Each OIF must target its own account and only its own account
    for oif, expected_account in zip(contents, accounts):
        assert oif.startswith(f"CLOSEPOSITION;{expected_account};"), \
            f"OIF mismatch: expected CLOSEPOSITION;{expected_account};... got {oif}"
        # Ensure the other accounts are not embedded anywhere
        for other in accounts:
            if other == expected_account:
                continue
            assert other not in oif, \
                f"OIF for {expected_account} leaked {other}: {oif}"


def test_close_position_line_refuses_empty_account(tmp_oif_dirs):
    """B58: empty account must raise — no silent default to Sim101."""
    with pytest.raises(ValueError):
        close_position_line(account="")
    with pytest.raises(ValueError):
        close_position_line(account=None)  # type: ignore[arg-type]


def test_close_position_line_refuses_live_account(tmp_oif_dirs, monkeypatch):
    """B59: LIVE_ACCOUNT env hard-guards every close path."""
    monkeypatch.setenv("LIVE_ACCOUNT", "MyRealLiveAccount")
    with pytest.raises(RuntimeError, match="LIVE_GUARD"):
        close_position_line(account="MyRealLiveAccount")


def test_cancel_all_action_hard_blocked(tmp_oif_dirs):
    """
    B75: write_oif('CANCEL_ALL', ...) refuses to emit because NT8 ATI
    cross-cancels CANCELALLORDERS across every connected account
    regardless of the account field. The hard block prevents Phoenix
    from ever emitting this destructive verb. Per-order CANCEL by
    NT8 order_id is the safe alternative.
    """
    incoming, _ = tmp_oif_dirs

    paths = write_oif(
        action="CANCEL_ALL",
        qty=0,
        trade_id="should_not_be_emitted",
        account="Sim101",
    )
    assert paths == [], "CANCEL_ALL must return empty path list (hard blocked)"

    # And no file landed
    files = list(incoming.glob("*.txt"))
    assert len(files) == 0, \
        f"CANCEL_ALL must not write any OIF, but found: {[f.name for f in files]}"


def test_cancel_single_order_targets_only_that_order(tmp_oif_dirs):
    """
    Per-order CANCEL is the safe verb. It targets a specific NT8
    order_id; no account scoping is involved (the order_id alone is
    unique across accounts in NT8's order space).
    """
    line = cancel_single_order_line("oif_abc123")
    assert line == "CANCEL;;;;;;;;;;oif_abc123;;"
    # Only the named order_id appears — no account or instrument fields
    # populated, which is correct: NT8 looks up by order_id alone.


def test_close_position_oif_filename_carries_pid_tag(tmp_oif_dirs):
    """
    Defense-in-depth: every OIF Phoenix writes is filename-tagged with
    the writing process's PID (P0.2 / D8). PhoenixOIFGuard.cs (NT8 AddOn)
    quarantines anything in incoming/ whose name does NOT match the
    `_phoenix_<pid>_` pattern — protecting against rogue (non-Phoenix)
    OIF writers injecting orders that look like Phoenix closes.
    """
    incoming, _ = tmp_oif_dirs

    write_oif(
        action="EXIT",
        qty=1,
        trade_id="pid_tag_check",
        account="SimBias Momentum",
    )

    files = list(incoming.glob("*.txt"))
    assert len(files) == 1, f"Expected 1 OIF, got {len(files)}"
    fname = files[0].name
    pid = os.getpid()
    assert fname.startswith("oif"), \
        f"Phoenix OIF filenames must start with 'oif' (NT8 ATI requirement): {fname}"
    assert f"_phoenix_{pid}_" in fname, \
        f"Phoenix OIF filename must contain _phoenix_{pid}_ tag: {fname}"
