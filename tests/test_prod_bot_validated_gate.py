"""Sprint H: prod_bot loads ALL enabled strategies (operator request).

Operator request 2026-05-04: prod must run all strategies, all hours,
for debug visibility before going live. The validated=True safety gate
is OFF by operator decision.

These tests verify the post-Sprint-H state and serve as a regression
guard for any future re-tightening:

  - prod_bot.only_validated == False (UNCONDITIONAL)
  - load_strategies filter therefore loads all enabled strategies
  - bias_momentum.session_block_windows is empty (no time-based blocks)

LIVE-MODE SAFETY NOTES (documented, not test-enforced):
  - Pre-Sprint-H, only_validated=True meant prod loaded only validated
    strategies even on real money — that gate is now removed.
  - Pre-Sprint-H, session_block_windows blocked bias_momentum during
    08:30-08:59 + 10:00-13:29 CT (forensically losing windows).
  - When LIVE_TRADING=True is flipped, both gates remain OFF unless
    operator restores them. Restoration commands documented in commit
    messages and config comments.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─── current state assertions ───────────────────────────────────────

def test_prod_bot_only_validated_is_false():
    """Operator decision 2026-05-04: prod loads all strategies."""
    from bots.prod_bot import ProdBot
    bot = ProdBot.__new__(ProdBot)
    assert bot.only_validated is False, (
        "ProdBot.only_validated must be False per Sprint H operator "
        "decision (full strategy roster on prod for debug visibility). "
        "If you're seeing this assertion fail, someone re-tightened the "
        "gate — verify with the operator before changing back."
    )


def test_bias_momentum_session_block_windows_empty():
    """Operator decision 2026-05-04: bias_momentum trades all hours."""
    from config.strategies import STRATEGIES
    bm = STRATEGIES["bias_momentum"]
    assert bm["session_block_windows"] == [], (
        "bias_momentum.session_block_windows must be empty per Sprint H "
        "operator decision (all-hours trading for prod debug). The pre-"
        "Sprint-H windows [08:30-08:59, 10:00-13:29] are documented in "
        "the config comment for restoration before go-live."
    )


# ─── strategy roster sanity (matches sim_bot's roster) ──────────────

def test_prod_loads_same_strategies_as_sim():
    """With only_validated=False, prod's load filter must match sim's
    behavior: load every enabled strategy regardless of validated."""
    from bots.prod_bot import ProdBot
    from config.strategies import STRATEGIES
    bot = ProdBot.__new__(ProdBot)
    # Replicate base_bot.load_strategies filter exactly:
    #   if self.only_validated and not config.get("validated", False): skip
    #   if not config.get("enabled", True): skip
    loaded = []
    for name, config in STRATEGIES.items():
        if bot.only_validated and not config.get("validated", False):
            continue
        if not config.get("enabled", True):
            continue
        loaded.append(name)
    enabled_count = sum(
        1 for c in STRATEGIES.values() if c.get("enabled", True)
    )
    assert len(loaded) == enabled_count, (
        f"Prod loaded {len(loaded)} strategies but {enabled_count} are "
        f"enabled in config. Filter incorrect."
    )
    # Specifically: previously-blocked unvalidated strategies must now
    # be loaded.
    for must_be_loaded in ("vwap_pullback", "dom_pullback", "noise_area",
                            "compression_breakout", "orb"):
        if STRATEGIES.get(must_be_loaded, {}).get("enabled", True):
            assert must_be_loaded in loaded, (
                f"`{must_be_loaded}` is enabled but not loaded by prod"
            )


# ─── regression: future-tightening detection ────────────────────────

def test_no_session_block_windows_re_added_silently():
    """Defensive: if a future config edit re-adds blocks to
    bias_momentum.session_block_windows, this test fails loudly so the
    operator notices before deploying. Restoration is fine; silent
    re-add is not."""
    from config.strategies import STRATEGIES
    bm_blocks = STRATEGIES["bias_momentum"]["session_block_windows"]
    if bm_blocks:
        pytest.fail(
            f"bias_momentum.session_block_windows is non-empty: "
            f"{bm_blocks}. If this is intentional restoration before "
            f"go-live, update this test. If it's accidental, remove "
            f"the windows."
        )


def test_no_validated_gate_re_added_silently():
    """Defensive: catch silent re-addition of the validated gate on
    prod. If operator wants the gate back, they must update this test
    with the rationale."""
    from bots.prod_bot import ProdBot
    bot = ProdBot.__new__(ProdBot)
    if bot.only_validated:
        pytest.fail(
            "ProdBot.only_validated re-tightened to True. If this is "
            "intentional restoration before go-live, update this test. "
            "If it's accidental, set back to False."
        )


# ─── live-mode awareness (informational, not enforced) ──────────────

def test_live_trading_flag_state_is_documented():
    """Sanity: LIVE_TRADING flag exists and its state is readable.
    Sprint H removed the conditional gate that read this flag, but the
    flag itself remains as the canonical 'are we trading real money'
    indicator. Useful for operator audits."""
    from config.settings import LIVE_TRADING
    # Just verify we can read it. Don't assert a specific value —
    # operator may flip it for go-live testing.
    assert LIVE_TRADING in (True, False), (
        f"LIVE_TRADING must be a boolean, got {type(LIVE_TRADING).__name__}"
    )
