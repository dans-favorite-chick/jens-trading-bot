"""Sprint I (2026-05-03): prod_bot routes per STRATEGY_ACCOUNT_MAP.

Operator request 2026-05-03: with Sprint H expanding the prod strategy
roster from 2 -> 10, the B57 single-account pin (FORCE_ACCOUNT="Sim101")
became a single-account bottleneck — only ONE position at a time across
the whole prod bot, while sim_bot ran all 10 concurrently.

Sprint I removes the FORCE_ACCOUNT override on prod_bot. Routing now
falls through to config/account_routing.py:get_account_for_signal(),
mirroring sim_bot's per-strategy account topology.

LIVE-MODE SAFETY NOTES (documented, not test-enforced):
  - Pre-Sprint-I, every prod signal hit Sim101 even on real money.
  - Post-Sprint-I, signals route to whatever STRATEGY_ACCOUNT_MAP says.
  - When LIVE_TRADING=True is flipped, operator MUST audit
    STRATEGY_ACCOUNT_MAP first — otherwise live signals route to the
    mapped Sim* accounts (which is fine for paper but wrong for live).
  - Restoration: set FORCE_ACCOUNT to a single account name on
    prod_bot.ProdBot to re-pin everything to that account.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─── current state assertions ───────────────────────────────────────

def test_prod_bot_force_account_is_none():
    """Operator decision 2026-05-03: prod routes per STRATEGY_ACCOUNT_MAP."""
    from bots.prod_bot import ProdBot
    bot = ProdBot.__new__(ProdBot)
    force = getattr(bot, "FORCE_ACCOUNT", None)
    assert force is None, (
        f"ProdBot.FORCE_ACCOUNT must be None per Sprint I operator "
        f"decision (per-strategy routing on prod for concurrent "
        f"positions across the expanded Sprint H roster). Got "
        f"{force!r}. If you're seeing this assertion fail, someone "
        f"re-pinned the override — verify with the operator before "
        f"changing back."
    )


def test_prod_bot_routes_via_strategy_account_map():
    """With FORCE_ACCOUNT=None, the routing path in base_bot._enter()
    falls through to get_account_for_signal(). Verify the resolver
    returns each strategy's mapped account, not the Sim101 default."""
    from bots.prod_bot import ProdBot
    from config.account_routing import (
        STRATEGY_ACCOUNT_MAP,
        get_account_for_signal,
    )

    bot = ProdBot.__new__(ProdBot)
    # Mirror the routing branch from base_bot.py:
    #   _force = getattr(self, "FORCE_ACCOUNT", None)
    #   if _force: _account = _force
    #   else:      _account = get_account_for_signal(strategy, sub)
    _force = getattr(bot, "FORCE_ACCOUNT", None)
    assert _force is None  # precondition

    # Top-level strategies: each must resolve to its mapped account.
    for strategy_name, mapped in STRATEGY_ACCOUNT_MAP.items():
        if strategy_name == "_default":
            continue
        if isinstance(mapped, dict):
            # Nested (opening_session) — exercise sub-strategies below.
            for sub, sub_account in mapped.items():
                resolved = get_account_for_signal(strategy_name, sub)
                assert resolved == sub_account, (
                    f"opening_session.{sub} should route to "
                    f"{sub_account!r}, got {resolved!r}"
                )
        else:
            resolved = get_account_for_signal(strategy_name, None)
            assert resolved == mapped, (
                f"{strategy_name} should route to {mapped!r}, "
                f"got {resolved!r}"
            )


def test_prod_bot_no_longer_pins_to_sim101():
    """Sanity: at least one mapped strategy resolves to a non-Sim101
    account. If the entire map collapses to Sim101 (kill-switch on),
    prod gains nothing from removing FORCE_ACCOUNT."""
    from config import settings
    from config.account_routing import get_account_for_signal

    if not getattr(settings, "MULTI_ACCOUNT_ROUTING_ENABLED", True):
        pytest.skip(
            "MULTI_ACCOUNT_ROUTING_ENABLED is False — kill-switch "
            "active, every signal routes to Sim101 regardless of "
            "FORCE_ACCOUNT. Per-strategy routing test N/A in this state."
        )

    # bias_momentum is a representative top-level strategy with its own
    # account mapping. If this returns Sim101, something is wrong.
    resolved = get_account_for_signal("bias_momentum", None)
    assert resolved != "Sim101", (
        f"bias_momentum should route to its dedicated account, not "
        f"Sim101. Got {resolved!r}. Either STRATEGY_ACCOUNT_MAP is "
        f"missing the entry or the kill-switch is being read wrong."
    )


# ─── regression: future-pinning detection ───────────────────────────

def test_no_force_account_re_added_silently():
    """Defensive: if a future edit re-pins prod to a single account,
    fail loudly. Restoration is fine; silent re-pin is not."""
    from bots.prod_bot import ProdBot
    bot = ProdBot.__new__(ProdBot)
    force = getattr(bot, "FORCE_ACCOUNT", None)
    if force is not None:
        pytest.fail(
            f"ProdBot.FORCE_ACCOUNT re-pinned to {force!r}. If this is "
            f"intentional restoration (e.g. operator wants single-"
            f"account live-money routing), update this test with the "
            f"new expected value and rationale. If it's accidental, "
            f"set back to None."
        )
