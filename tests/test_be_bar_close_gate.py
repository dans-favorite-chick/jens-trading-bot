"""BE-stop bar-close confirmation gate (#18, 2026-05-13).

Before #18: a single tick crossing the BE trigger armed the BE stop
immediately, even if the price was just spiking. A retracement on the
next tick could stop us out on entry noise.

After #18: BE only arms if (a) the tick price has crossed the trigger
AND (b) the most-recent CLOSED 1m bar has also closed past the trigger.
Falls back to tick-only mode when no bar yet (first 60s of session).

These tests pin the boolean structure via expression replicas — the
actual arm-call lives in the tick loop and would require mocking the
entire bot to drive end-to-end.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from types import SimpleNamespace


def _should_arm_be(
    direction: str,
    price: float,
    be_trigger: float,
    last_bar_close: float | None,
    be_on_bar_close: bool,
) -> bool:
    """Mirror of the gate in bots/base_bot.py:~1878-1900."""
    tick_crossed = (
        (direction == "LONG" and price >= be_trigger)
        or (direction == "SHORT" and price <= be_trigger)
    )
    if not be_on_bar_close:
        return tick_crossed
    # bar-close mode
    if last_bar_close is None:
        return tick_crossed  # fallback during first minute
    bar_confirms = (
        (direction == "LONG" and last_bar_close >= be_trigger)
        or (direction == "SHORT" and last_bar_close <= be_trigger)
    )
    return tick_crossed and bar_confirms


# ── Legacy (tick-only) mode ────────────────────────────────────────────

def test_legacy_mode_arms_on_tick_touch():
    assert _should_arm_be(
        "LONG", price=20010.0, be_trigger=20005.0,
        last_bar_close=20003.0,  # would BLOCK in bar-close mode
        be_on_bar_close=False,
    ) is True


# ── Bar-close mode: confirmation required ──────────────────────────────

def test_long_armed_when_both_tick_and_bar_close_above_trigger():
    assert _should_arm_be(
        "LONG", price=20010.0, be_trigger=20005.0,
        last_bar_close=20006.0,
        be_on_bar_close=True,
    ) is True


def test_long_blocked_when_tick_crosses_but_bar_below_trigger():
    """The noisy-tick case — without the gate, BE would arm here."""
    assert _should_arm_be(
        "LONG", price=20010.0, be_trigger=20005.0,
        last_bar_close=20003.0,  # bar still below trigger
        be_on_bar_close=True,
    ) is False


def test_long_blocked_when_bar_above_but_tick_dropped():
    """Edge case: bar closed past trigger but current tick has pulled
    back below — caller should NOT arm yet."""
    assert _should_arm_be(
        "LONG", price=20003.0, be_trigger=20005.0,
        last_bar_close=20007.0,
        be_on_bar_close=True,
    ) is False


def test_short_armed_when_both_below_trigger():
    assert _should_arm_be(
        "SHORT", price=19995.0, be_trigger=19998.0,
        last_bar_close=19996.0,
        be_on_bar_close=True,
    ) is True


def test_short_blocked_when_bar_above_trigger():
    """Mirror of the noisy-tick case for SHORT."""
    assert _should_arm_be(
        "SHORT", price=19995.0, be_trigger=19998.0,
        last_bar_close=20000.0,  # bar still above trigger
        be_on_bar_close=True,
    ) is False


def test_bar_close_fallback_when_no_bar_yet():
    """First minute of the session: no completed bars yet. The gate
    should fall back to tick-only mode so BE isn't permanently blocked."""
    assert _should_arm_be(
        "LONG", price=20010.0, be_trigger=20005.0,
        last_bar_close=None,
        be_on_bar_close=True,
    ) is True


# ── Default + wiring pins ──────────────────────────────────────────────

def test_be_on_bar_close_default_is_true():
    from config.strategies import STRATEGY_DEFAULTS
    assert STRATEGY_DEFAULTS.get("be_on_bar_close") is True, (
        "BE bar-close confirmation should be ON by default — the legacy "
        "tick-touch mode is the regression-risk path."
    )


def test_base_bot_reads_be_on_bar_close_default():
    src = (ROOT / "bots" / "base_bot.py").read_text(encoding="utf-8")
    assert 'STRATEGY_DEFAULTS.get("be_on_bar_close"' in src, (
        "BE gate should read the global default — wiring must exist "
        "for the config flag to actually drive behavior."
    )
