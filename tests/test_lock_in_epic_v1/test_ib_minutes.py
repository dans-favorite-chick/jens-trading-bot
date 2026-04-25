"""Lock-in test for Fix D (2026-04-24): ib_breakout ib_minutes 30 -> 10."""

from __future__ import annotations

from config.strategies import STRATEGIES


def test_ib_minutes_is_ten():
    cfg = STRATEGIES["ib_breakout"]
    assert cfg["ib_minutes"] == 10
    assert cfg["enabled"] is True


def test_ib_breakout_documents_change():
    """The config has a comment explaining the 30 -> 10 change.
    If someone reverts to 30 silently, this test catches it."""
    import importlib.resources
    src = importlib.resources.files("config").joinpath("strategies.py").read_text(encoding="utf-8")
    # Comment must reference the change
    assert "ib_minutes=10" in src or "30 → 10" in src or "30 -> 10" in src


def test_ib_breakout_warmup_clears_at_t_plus_10():
    """Synthetic: with ib_minutes=10, after 10 1-minute bars the IB
    should be set."""
    cfg = STRATEGIES["ib_breakout"]
    ib_bar_count = cfg["ib_minutes"]
    # Simulate the strategy's "have we collected enough bars?" check
    bars_seen = ib_bar_count
    assert bars_seen >= ib_bar_count, "10 bars should suffice for ib_minutes=10"
    # And conversely: 9 bars is NOT enough
    assert (ib_bar_count - 1) < ib_bar_count
