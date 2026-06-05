"""Regression sentinel for FINDING-2026-06-05-STALE-ORIGINAL-CONTRACTS-SENTINEL.

The PHANTOM-NT8 Round 2 red-team flagged a hypothetical hazard: if
`pos.original_contracts` is read as a quantity argument by any OIF
emit after `scale_out_partial()` reduces the current contract count,
the emit goes out with the STALE (pre-scale-out) qty — quantity-
mismatched OIF on NT8.

Audit verdict (out/slot_interlock_bypass_audit_2026-06-05.md): the
hazard does NOT exist in current production code. `original_contracts`
is set once at `open_position()` and used only as an eligibility gate
(`>= 2`) for scale-out, never as a quantity for an OIF emit. The
production reads are:

  bots/_ws_dispatcher.py:474 — `pos.original_contracts >= 2` (gate)
  core/position_manager.py:1151 — `to_dict()` serialization
  bots/_ws_dispatcher.py + tests — gate checks

This file ships TWO sentinel tests:

  T1 — Behavioral: open a 3-contract position, call
       `scale_out_partial(price, n_contracts=1, reason="...")`, assert
       `pos.original_contracts == 3` (the designed behavior — stable
       trade property) and `pos.contracts == 2` (the current count).

  T2 — Source-grep: scan production python for any line that passes
       `original_contracts` to an OIF emit's `qty=` or `n_contracts=`
       kwarg. The match list MUST be empty. A future PR that violates
       this assertion will trip the sentinel and force a tracker
       discussion before the regression ships.

If `pos.original_contracts` is ever genuinely needed as an emit qty,
the proper fix is to use `pos.contracts` (the current count) instead,
or to update `original_contracts` on scale-out (would require a
PROTECTED edit to `core/position_manager.py` with operator OA).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_t1_original_contracts_stable_after_partial_exit():
    """Open 3-contract LONG; scale-out 1; verify original_contracts
    stays at 3 (designed stable property) and contracts decreases."""
    from core.position_manager import PositionManager

    pm = PositionManager()
    ok = pm.open_position(
        trade_id="t1_orig_contracts",
        direction="LONG",
        entry_price=30000.0,
        contracts=3,
        stop_price=29980.0,
        target_price=30040.0,
        strategy="test_strat_orig",
        reason="sentinel_test",
        account="Sim101",
    )
    assert ok is True, "PositionManager.open_position should return True"

    pos = pm.get_position_by_strategy("test_strat_orig")
    assert pos is not None, "newly-opened position should be retrievable"
    assert pos.contracts == 3
    assert pos.original_contracts == 3, (
        "original_contracts must be set to the open-time contract count"
    )

    # Scale out 1 contract at a profitable price
    partial = pm.scale_out_partial(
        exit_price=30020.0, n_contracts=1, exit_reason="scale_out_target",
    )
    assert partial is not None, (
        "scale_out_partial should return a dict on success"
    )
    assert partial["contracts"] == 1, (
        f"scale-out partial should be 1 contract; got {partial}"
    )

    # The designed invariant: original_contracts is stable, contracts shrinks
    assert pos.original_contracts == 3, (
        f"original_contracts changed after scale-out — it must remain at "
        f"the open-time count for eligibility-gate semantics. Got "
        f"{pos.original_contracts}. If this is a deliberate design "
        f"change, update out/slot_interlock_bypass_audit_2026-06-05.md "
        f"and re-run the source-grep sentinel below."
    )
    assert pos.contracts == 2, (
        f"contracts should decrease by n_exit=1; got {pos.contracts}"
    )
    assert pos.scaled_out is True


def test_t2_no_production_oif_emit_reads_original_contracts_as_qty():
    """Source-grep sentinel: NO production python file may pass
    `original_contracts` to an OIF emit's `qty=` or `n_contracts=`
    kwarg.

    If a future PR introduces such a call, this test trips and the
    audit needs revisiting — either the call is a bug (use
    pos.contracts instead) or the audit needs updating to acknowledge
    a new authorized use site.
    """
    # Patterns we forbid in production code:
    #   - `qty=pos.original_contracts`
    #   - `qty=<anything>.original_contracts`
    #   - `n_contracts=pos.original_contracts`
    #   - `n_contracts=<anything>.original_contracts`
    forbidden = [
        re.compile(r"qty\s*=\s*[\w.]+\.original_contracts\b"),
        re.compile(r"n_contracts\s*=\s*[\w.]+\.original_contracts\b"),
    ]

    # Production python = bots/, bridge/, core/, dashboard/, agents/,
    # config/, orchestrator/. Exclude tests/, tools/ (analysis), out/,
    # logs/, Python/ (vendored interpreter), data/, backtest_results/.
    PROD_DIRS = ["bots", "bridge", "core", "dashboard", "agents",
                  "config", "orchestrator"]

    hits = []
    for d in PROD_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8")
            except Exception:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for pat in forbidden:
                    if pat.search(line):
                        hits.append((str(py.relative_to(REPO_ROOT)), n, line.strip()))

    assert hits == [], (
        "FINDING-2026-06-05-STALE-ORIGINAL-CONTRACTS-SENTINEL tripped: "
        "production code now passes `original_contracts` as an OIF "
        "emit quantity. This re-opens the stale-qty regression class. "
        "Use `pos.contracts` (current count) instead, or DEFER and "
        "file OA-required fix sprint. Hits:\n  " +
        "\n  ".join(f"{p}:{ln}: {src}" for p, ln, src in hits)
    )


def test_t3_audit_marker_present():
    """Sanity: the audit doc this sentinel cross-references must exist
    in the repo. If a future cleanup deletes it, the sentinel loses
    its narrative anchor."""
    audit = REPO_ROOT / "out" / "slot_interlock_bypass_audit_2026-06-05.md"
    assert audit.exists(), (
        "audit doc out/slot_interlock_bypass_audit_2026-06-05.md is "
        "missing — restore it or update this sentinel's docstring."
    )
