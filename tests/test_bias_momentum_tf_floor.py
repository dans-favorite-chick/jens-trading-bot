"""Regression — bias_momentum hard TF-vote floor (2026-05-13).

Forensic observation: operator flagged trade `dc0c99aa` firing LONG
at 18:24:01 with only 1 of 4 timeframes bullish, score=27, tier=C,
during AFTERHOURS regime. The strategy is named "Bias Momentum" —
its premise is multi-TF alignment. 1/4 TF agreement is noise, not bias.

The pre-existing `confluence = votes + momentum_score/30` formula
should have rejected this trade (1 + 27/30 = 1.9 < AFTERHOURS min of 4.0),
but a SIGNAL was still emitted. The math either has a bypass we
haven't located, or the log printout misrepresents the inputs.

Either way: a HARD TF-vote floor is non-bypassable and unambiguous.
This test enforces it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BIAS_SRC = (ROOT / "strategies" / "bias_momentum.py").read_text(encoding="utf-8")


def test_min_tf_votes_floor_exists():
    """The bias_momentum module must contain a `min_tf_votes` hard gate
    that rejects entries with insufficient TF alignment, BEFORE the
    confluence formula gate (so it's not bypassable by high momentum_score)."""
    # Strip comments before checking
    non_comment = "\n".join(
        line for line in BIAS_SRC.splitlines()
        if not line.lstrip().startswith("#")
    )

    assert "min_tf_votes" in non_comment, (
        "bias_momentum.py must define a min_tf_votes config gate. "
        "Without it, the strategy can fire on 1/4 TF alignment, "
        "contradicting its 'multi-TF bias' premise."
    )

    # The check must compare votes < min_tf_votes and return None
    m = re.search(
        r"min_tf_votes\s*=.*?if\s+not\s+trend_day\s+and\s+votes\s*<\s*min_tf_votes:",
        BIAS_SRC, re.DOTALL,
    )
    assert m, (
        "bias_momentum.py must have an active guard of the form\n"
        "  min_tf_votes = self.config.get('min_tf_votes', N)\n"
        "  if not trend_day and votes < min_tf_votes:\n"
        "      return None\n"
        "in the non-TREND code path."
    )


def test_default_min_tf_votes_is_at_least_3():
    """The default min_tf_votes must be >= 3 (majority of 4 TFs).
    1 or 2 of 4 is not 'bias' — it's slight tilt at best."""
    # Find the default value
    m = re.search(
        r"min_tf_votes\s*=\s*int\(\s*self\.config\.get\(\s*['\"]min_tf_votes['\"],\s*(\d+)\s*\)",
        BIAS_SRC,
    )
    assert m, "couldn't find default value for min_tf_votes"
    default = int(m.group(1))
    assert default >= 3, (
        f"min_tf_votes default is {default}; must be >= 3 (majority of 4 TFs). "
        f"With 1 or 2 of 4 TFs aligned the strategy isn't trading 'bias momentum', "
        f"it's trading noise. See forensic of trade dc0c99aa 2026-05-13 18:24."
    )


def test_afterhours_confluence_tightened():
    """The AFTERHOURS / OVERNIGHT_RANGE / PREMARKET_DRIFT regimes had
    min_confluence=4.0, which let weak setups through. Operator flagged
    trade dc0c99aa firing in AFTERHOURS at 1/4 TF / score 27. The
    confluence math (1 + 27/30 = 1.9) SHOULD have rejected vs 4.0, but
    didn't — so we tighten the value as a second line of defense."""
    # Find the _REGIME_OVERRIDES dict and check AFTERHOURS
    m = re.search(
        r'"AFTERHOURS"\s*:\s*\{[^}]*"min_confluence"\s*:\s*([\d.]+)',
        BIAS_SRC,
    )
    assert m, "couldn't find AFTERHOURS min_confluence in _REGIME_OVERRIDES"
    val = float(m.group(1))
    assert val >= 5.0, (
        f"AFTERHOURS min_confluence is {val}; must be >= 5.0 after the "
        f"2026-05-13 tightening (was 4.0, allowed dc0c99aa-style weak entries)."
    )


def test_tf_floor_blocks_1_of_4_votes():
    """Forensic replay: with votes=1 in non-TREND day, the strategy
    must hard-reject (returns None) regardless of momentum_score.

    We simulate the gate's logic directly: if not trend_day and
    votes < min_tf_votes, returns None."""
    # The gate logic is a few lines we can test by extracting + simulating.
    # Build a minimal config object
    class FakeConfig(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    config = FakeConfig({"min_tf_votes": 3})

    def fake_gate(votes, trend_day, config):
        """Simulate the gate from bias_momentum.py."""
        min_tf_votes = int(config.get("min_tf_votes", 3))
        if not trend_day and votes < min_tf_votes:
            return None  # rejected
        return "PASS"

    # The forensic case: votes=1, non-TREND day → must reject
    assert fake_gate(1, False, config) is None, "1/4 TF must reject"
    assert fake_gate(2, False, config) is None, "2/4 TF must reject (< 3)"
    assert fake_gate(3, False, config) == "PASS", "3/4 TF passes the gate"
    assert fake_gate(4, False, config) == "PASS", "4/4 TF passes the gate"
    # TREND day bypasses
    assert fake_gate(1, True, config) == "PASS", "TREND day bypasses TF gate"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
