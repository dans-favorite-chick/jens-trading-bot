"""
tests/test_plan_winners_parity.py — Plan §1.1 ↔ live config parity
====================================================================

2026-05-20 (Phase 13 Ship Audit Pt 2, F-011):

Created in response to the 2026-05-20 incident where an operator override
on 2026-05-17 had silently flipped `big_move_signal` and `dom_pullback` to
`validated=True` despite neither being in `PHOENIX_BEST_PLAN.md §1.1`'s
11-winner roster. The bot ran for 3 days firing trades the plan had no
evidence for; today's -$117 loss on big_move_signal was the catalyst that
surfaced the drift.

This test enforces that:

1. Every plan winner has a matching STRATEGIES entry that is enabled.
2. Every plan winner has a STRATEGY_ACCOUNT_MAP entry.
3. Every plan winner has a STRATEGY_KEYS entry (per-strategy risk).
4. No strategy is `validated=True` unless it is in the plan winners list
   OR is `vwap_band_reversion` (explicitly per-strategy-specced in
   plan §1.2 without being in the 11-winner roster).

The "operator override" guardrail (#4) is the most important — it would
have failed CI on 2026-05-17 and prevented the 2026-05-20 incident.

To re-add a strategy to the plan, update the WINNERS_PHASE13 tuple
below. To intentionally promote a non-winner to validated=True (e.g.
after a 5-year backtest justifies the promotion), add it to
WINNERS_BEYOND_PLAN with a citation comment.
"""
from __future__ import annotations

import pytest


# ─── Source of truth ─────────────────────────────────────────────────
# PHOENIX_BEST_PLAN.md §1.1 (CURRENT - SHIP PLAN) lists exactly these
# 11 strategies as Phase 13 winners. Note opening_session is the
# umbrella; it ships 4-5 sub-strategies (open_drive, open_auction_in,
# open_auction_out, premarket_breakout, orb) — the parent key is the
# one that appears in STRATEGIES / STRATEGY_ACCOUNT_MAP.
WINNERS_PHASE13 = (
    "bias_momentum",
    "spring_setup",
    "vwap_pullback_v2",
    "opening_session",          # parent for 4 enabled subs + orb
    "raschke_baseline",
    "g_inside_bar_breakout",
    "e_multi_day_breakout",
    "a_asian_continuation",
    "es_nq_confluence",         # dormant until MES feed (§6.6)
    "vwap_band_pullback",
    "ib_breakout",
)

# Per PHOENIX_BEST_PLAN.md §1.2 — not in §1.1's "winners" pool but
# explicitly per-strategy-specced. Considered an OK validated=True.
WINNERS_BEYOND_PLAN = (
    "vwap_band_reversion",  # §1.2 row 12 (retest, scale_out_1r + filter)
    # Future legitimate promotions land here with a citation comment.
)

# Also include the standalone top-level `orb` strategy here — plan
# §1.1 lists opening_session.orb but the umbrella strategy ALSO has a
# top-level orb in STRATEGIES that's set enabled=False per the plan
# (only the sub-evaluator inside opening_session is intended to fire).
# It's allowed to exist as long as it's NOT validated=True.
ALLOWED_DISABLED_LEGACIES = (
    "orb",                # superseded by opening_session.orb sub-evaluator
    "vwap_pullback",      # superseded by vwap_pullback_v2 (Phase 5, 2026-05-17)
    "compression_breakout",  # superseded by compression_breakout_v2 (which was then KILLED)
    "noise_area",         # retired 2026-05-15 (target=entry bug + anti-edge)
    "high_precision_only", # retired 2026-05-13 (557 trades / 29% WR / -$1,082)
    # 2026-05-22 pt8 (per agent ac705046): cover all the disabled legacies
    # so a "promote-by-vibes" PR can't silently flip enabled=True without
    # tripping CI. Each of these has empirical evidence supporting the
    # disabled state — see docs/PHASE_13_IMPLEMENTATION_PLAN.md §A.
    "big_move_signal",    # demoted 2026-05-21 (not in plan §1.1, no backtest)
    "nq_lsr",             # demoted 2026-05-21 (not in plan §1.1)
)


def test_every_plan_winner_in_strategies_dict():
    """Phase 13 §1.1 winners must each have a STRATEGIES entry."""
    from config.strategies import STRATEGIES
    for name in WINNERS_PHASE13:
        assert name in STRATEGIES, (
            f"Plan §1.1 winner '{name}' is missing from "
            f"config/strategies.py::STRATEGIES. Either add the strategy "
            f"class + config or remove it from WINNERS_PHASE13."
        )


def test_every_plan_winner_is_enabled():
    """Phase 13 §1.1 winners must be enabled=True (not disabled)."""
    from config.strategies import STRATEGIES
    for name in WINNERS_PHASE13:
        cfg = STRATEGIES.get(name, {})
        assert cfg.get("enabled") is True, (
            f"Plan §1.1 winner '{name}' has enabled={cfg.get('enabled')} "
            f"in config/strategies.py. Plan ships these strategies; "
            f"disabling them silently drops backtest-validated P&L. "
            f"If intentionally taking the strategy offline (e.g. data "
            f"feed dependency), update PHOENIX_BEST_PLAN.md §1.1 first."
        )


def test_every_plan_winner_validated_except_dormant():
    """Phase 13 §1.1 winners should be validated=True, EXCEPT
    `es_nq_confluence` which is documented as dormant pending MES feed
    (plan §6.6)."""
    from config.strategies import STRATEGIES
    DORMANT = {"es_nq_confluence"}
    for name in WINNERS_PHASE13:
        if name in DORMANT:
            continue
        cfg = STRATEGIES.get(name, {})
        validated = cfg.get("validated")
        # opening_session.validated is the umbrella flag; subs gate themselves
        assert validated is True, (
            f"Plan §1.1 winner '{name}' has validated={validated} in "
            f"config/strategies.py. Plan ships these as validated."
        )


def test_no_unplanned_validated_true_strategies():
    """OPERATOR OVERRIDE GUARDRAIL: no strategy may be validated=True
    unless it's in WINNERS_PHASE13 or WINNERS_BEYOND_PLAN.

    This is the test that would have caught the 2026-05-17 silent
    operator override of big_move_signal + dom_pullback to True.
    Failing this test means a strategy was promoted without plan support;
    either add it to WINNERS_BEYOND_PLAN (with a citation comment) or
    set validated=False.
    """
    from config.strategies import STRATEGIES
    allowed = set(WINNERS_PHASE13) | set(WINNERS_BEYOND_PLAN)
    offenders = [
        name for name, cfg in STRATEGIES.items()
        if cfg.get("validated") is True and name not in allowed
    ]
    assert not offenders, (
        f"Strategies with validated=True but not in WINNERS_PHASE13 "
        f"or WINNERS_BEYOND_PLAN: {offenders}.\n"
        f"This is the silent-override pattern that caused 2026-05-20's "
        f"-$117 big_move_signal incident.\n"
        f"To fix: either set validated=False in config/strategies.py, "
        f"OR add the strategy to WINNERS_BEYOND_PLAN in this test with "
        f"a citation comment showing the backtest evidence."
    )


def test_every_plan_winner_routes_to_dedicated_account():
    """Every plan winner must have a non-Sim101 routing in
    STRATEGY_ACCOUNT_MAP (with documented exceptions for dormant
    strategies still on Sim101 temp-routing)."""
    from config.account_routing import STRATEGY_ACCOUNT_MAP
    SIM101_OK = {
        # Per account_routing.py comments — intentionally on Sim101
        # until graduation. F-004 in OPERATOR_MORNING_BRIEF.md may
        # demote big_move_signal off Sim101 entirely (validated=False
        # means routing doesn't matter for it).
        "es_nq_confluence",  # dormant pending MES
        "big_move_signal",   # Phase 9.1 hotfix (now validated=False post-F-004)
    }
    for name in WINNERS_PHASE13:
        route = STRATEGY_ACCOUNT_MAP.get(name)
        if isinstance(route, dict):
            # opening_session: each sub must route to its own account
            for sub_name, sub_acct in route.items():
                assert sub_acct != "Sim101", (
                    f"Plan §1.1 winner '{name}.{sub_name}' routes to Sim101 "
                    f"instead of a dedicated account."
                )
        elif name in SIM101_OK:
            continue
        else:
            assert route is not None and route != "Sim101", (
                f"Plan §1.1 winner '{name}' has routing={route} in "
                f"STRATEGY_ACCOUNT_MAP. Should be a dedicated NT8 sim "
                f"account, not Sim101 (or missing)."
            )


def test_every_plan_winner_has_per_strategy_risk_key():
    """Every plan winner top-level key must be in STRATEGY_KEYS so the
    per-strategy risk registry tracks daily caps + halt persistence."""
    from core.strategy_risk_registry import STRATEGY_KEYS
    keys_set = set(STRATEGY_KEYS)
    for name in WINNERS_PHASE13:
        # opening_session: the parent key and its 4 subs all need entries
        if name == "opening_session":
            for sub in ("open_drive", "open_auction_in",
                         "open_auction_out", "premarket_breakout", "orb"):
                assert f"opening_session.{sub}" in keys_set, (
                    f"opening_session sub '{sub}' missing from STRATEGY_KEYS"
                )
        else:
            assert name in keys_set, (
                f"Plan §1.1 winner '{name}' missing from STRATEGY_KEYS in "
                f"core/strategy_risk_registry.py."
            )


def test_allowed_legacies_stay_disabled():
    """2026-05-22 pt8 (agent ac705046 audit gap): assert that every
    ALLOWED_DISABLED_LEGACIES entry actually IS disabled today. The
    list exists to grant amnesty to legacy files that still live in
    the repo + import paths but must never fire. Without this test,
    a casual `enabled=True` flip on any legacy slips through CI.

    To re-promote a legacy: remove it from ALLOWED_DISABLED_LEGACIES
    AND add it to WINNERS_PHASE13 (or BEYOND_PLAN) with a backtest
    citation. Both edits required — by design.
    """
    from config.strategies import STRATEGIES
    for name in ALLOWED_DISABLED_LEGACIES:
        cfg = STRATEGIES.get(name)
        if cfg is None:
            # Strategy entirely removed from config = fine; the legacy
            # tuple is for files in the repo, not for config presence.
            continue
        assert cfg.get("enabled") is False, (
            f"Legacy strategy '{name}' has enabled={cfg.get('enabled')}, "
            f"but it's listed in ALLOWED_DISABLED_LEGACIES. Either:\n"
            f"  (a) revert the enabled flag, or\n"
            f"  (b) promote it: remove from ALLOWED_DISABLED_LEGACIES, "
            f"add to WINNERS_PHASE13/BEYOND_PLAN with backtest evidence."
        )


def test_no_killed_strategies_are_enabled():
    """Strategies on PHASE_13_IMPLEMENTATION_PLAN.md's explicit kill list
    must be enabled=False. Same guardrail logic as F-004 of the audit.
    Catches the case where a future operator override re-enables a
    plan-killed strategy."""
    from config.strategies import STRATEGIES
    KILLED = (
        "orb_fade",                   # §O EXPLICIT KILL (PF 0.34, anti-edge)
        "compression_breakout_v2",    # §A Bug B4 EXPLICIT KILL (anti-edge)
        "compression_breakout_micro", # §A anti-edge sibling
        "orb_v2",                     # B-002: only 1 trade in 5y
    )
    for name in KILLED:
        cfg = STRATEGIES.get(name, {})
        # missing from STRATEGIES is fine (means removed entirely)
        if not cfg:
            continue
        assert cfg.get("enabled") is False, (
            f"Killed strategy '{name}' has enabled={cfg.get('enabled')}. "
            f"Plan explicitly killed it; must stay enabled=False unless a "
            f"new backtest reverses the kill verdict (then update KILLED "
            f"tuple here)."
        )
