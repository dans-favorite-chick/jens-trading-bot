"""
Phase 4B integration tests for base_bot: strategy registration + async
refresh task wiring.

Run: pytest tests/test_base_bot.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bots.base_bot import BaseBot
from strategies.opening_session import OpeningSessionStrategy


# ═══════════════════════════════════════════════════════════════════
# opening_session strategy class is registered in base_bot
# ═══════════════════════════════════════════════════════════════════
def test_opening_session_strategy_registered_in_base_bot():
    b = BaseBot()
    b.load_strategies()
    names = [s.name for s in b.strategies]
    assert "opening_session" in names
    osi = next(s for s in b.strategies if s.name == "opening_session")
    assert isinstance(osi, OpeningSessionStrategy)


# ═══════════════════════════════════════════════════════════════════
# Daily refresh task exists and is a coroutine
# ═══════════════════════════════════════════════════════════════════
def test_daily_refresh_task_is_a_coroutine():
    b = BaseBot()
    assert hasattr(b, "_session_levels_refresh_task")
    assert inspect.iscoroutinefunction(b._session_levels_refresh_task)


# ═══════════════════════════════════════════════════════════════════
# TickAggregator on the bot carries bot_name + session_levels
# ═══════════════════════════════════════════════════════════════════
def test_aggregator_has_session_levels_and_bot_name():
    b = BaseBot()
    assert b.aggregator.bot_name == b.bot_name
    # session_levels may be None if the ctor fell back to disabled, but
    # the attribute must always exist so the refresh task can check it.
    assert hasattr(b.aggregator, "session_levels")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
