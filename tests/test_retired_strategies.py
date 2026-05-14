"""Retired strategies (#5, 2026-05-13).

Three strategies were formally retired after the deep-dive analysis:

- `high_precision_only` — 557 trades / 29% WR / -$1,082 net
- `compression_breakout` — 18 trades total (signal too rare)
- `opening_session`      — 4 trades total (nested router fires too rarely)

These tests:
1. Pin the `enabled=False` + `retired=True` markers so any accidental
   re-enable (e.g. a dashboard "reset to defaults" click) breaks CI.
2. Confirm the bot's load_strategies() actually skips them.
3. Document the retirement reason so the rationale survives in code.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RETIRED = ("high_precision_only", "compression_breakout", "opening_session")


@pytest.mark.parametrize("name", RETIRED)
def test_retired_strategy_is_disabled(name):
    from config.strategies import STRATEGIES
    cfg = STRATEGIES[name]
    assert cfg["enabled"] is False, (
        f"{name} must stay enabled=False — see retired_reason in config/strategies.py"
    )


@pytest.mark.parametrize("name", RETIRED)
def test_retired_strategy_marked_retired(name):
    """retired=True is the durable marker — survives dashboard 'reset'
    flows that might re-toggle 'enabled' but not delete the marker."""
    from config.strategies import STRATEGIES
    cfg = STRATEGIES[name]
    assert cfg.get("retired") is True, (
        f"{name} should carry retired=True so the dashboard and any "
        f"future audit tool can surface its status."
    )
    assert cfg.get("retired_at"), f"{name} missing retired_at date"
    assert cfg.get("retired_reason"), f"{name} missing retired_reason"


@pytest.mark.parametrize("name", RETIRED)
def test_retired_strategy_is_not_validated(name):
    """A retired strategy must NOT carry validated=True — that would
    let it slip into prod_bot on a flip of `enabled`."""
    from config.strategies import STRATEGIES
    cfg = STRATEGIES[name]
    assert cfg.get("validated", False) is False, (
        f"{name} is retired AND validated=True — fix one or the other "
        f"before any future re-enable."
    )
