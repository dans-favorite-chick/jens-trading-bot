"""Lock-in test for Fix F (2026-04-24): spring_setup retired (enabled=False)."""

from __future__ import annotations

from config.strategies import STRATEGIES


def test_spring_setup_disabled_in_config():
    cfg = STRATEGIES["spring_setup"]
    assert cfg["enabled"] is False, "spring_setup must remain DISABLED"


def test_spring_setup_retire_documented():
    """Config must contain the retirement note so future readers know why."""
    import importlib.resources
    src = importlib.resources.files("config").joinpath("strategies.py").read_text(encoding="utf-8")
    # Look for the retirement comment
    assert "RETIRED" in src and "spring_setup" in src


def test_spring_setup_strategy_file_still_present():
    """File should still exist (in case we want to retool later) but
    the strategy is gated off via config['enabled']=False.
    """
    import importlib.resources
    # Just verify the file exists; strategy loader skips if disabled.
    src = importlib.resources.files("strategies").joinpath("spring_setup.py").read_text(encoding="utf-8")
    assert "class" in src   # something class-like remains
