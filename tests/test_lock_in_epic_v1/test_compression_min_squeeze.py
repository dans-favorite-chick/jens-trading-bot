"""Lock-in test for Fix E (2026-04-24): compression_breakout min_squeeze_bars 5 -> 12."""

from __future__ import annotations

from config.strategies import STRATEGIES


def test_compression_min_squeeze_bars_is_12():
    cfg = STRATEGIES["compression_breakout"]
    assert cfg["min_squeeze_bars"] == 12
    assert cfg["enabled"] is True


def test_compression_strategy_reads_config_value():
    """Strategy must default to the config value, not a hardcoded 5."""
    import importlib.resources
    src = importlib.resources.files("strategies").joinpath("compression_breakout.py").read_text(encoding="utf-8")
    # The strategy uses self.config.get("min_squeeze_bars", 5) — that
    # default of 5 is the fallback if the config key is missing. Today
    # the config supplies 12, so the strategy default never kicks in.
    assert 'self.config.get("min_squeeze_bars"' in src
