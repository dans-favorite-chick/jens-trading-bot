"""P0-5 freeze interlock — production-decision freeze.

Per docs/audits/SYNTHESIS_2026-05-24.md P0-5: the FREEZE_ACTIVE flag in
config/strategies.py prevents new validated=True flips and Phase 13
verdict shipping until the reconciliation harness (P1-1) ships.

This test guards the constant's existence and the freeze-banner code path.
Lifting the freeze requires editing config/strategies.py and updating
this test if the freeze stays off; tampering with the constant without
the reconciliation report is the failure mode we're guarding against.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def test_freeze_constant_exists_and_is_bool() -> None:
    from config import strategies
    assert hasattr(strategies, "FREEZE_ACTIVE"), (
        "config/strategies.py must define FREEZE_ACTIVE per P0-5 "
        "(see docs/audits/SYNTHESIS_2026-05-24.md)"
    )
    assert isinstance(strategies.FREEZE_ACTIVE, bool), (
        "FREEZE_ACTIVE must be a bool — True (freeze on) or False "
        "(freeze lifted with a referenced reconciliation report)"
    )


def test_freeze_is_currently_on() -> None:
    """The freeze is ON as of 2026-05-24.

    This will FAIL on the day someone lifts the freeze — and that's the
    point. When lifted, the lifter must also update this test with a
    comment naming the reconciliation report that authorized the lift.
    """
    from config import strategies
    assert strategies.FREEZE_ACTIVE is True, (
        "Freeze appears lifted. Confirm: was the P1-1 reconciliation "
        "harness shipped? Is out/reconciliation_<date>_<strategy>.md "
        "committed? Did operator sign off on the divergence numbers? "
        "If yes, update this test with the authorizing report path."
    )


def test_validation_tracker_prints_freeze_banner_when_active() -> None:
    """tools/validation_tracker.py --check-promotion must print the
    FREEZE banner when FREEZE_ACTIVE is True."""
    # We invoke the script as a subprocess so we get its actual stdout,
    # including the freeze banner. The --check-promotion flag is the
    # documented entry point for promotion checks.
    result = subprocess.run(
        [sys.executable, "tools/validation_tracker.py", "--check-promotion"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert "FREEZE" in output.upper(), (
        f"Expected freeze banner in --check-promotion output. Got:\n{output}"
    )
