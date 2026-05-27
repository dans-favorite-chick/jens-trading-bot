"""
Phoenix Bot — Multi-Account OIF Routing (Phase 4C)

Maps each strategy (and opening_session sub-strategy) to its dedicated
NT8 sim account so per-strategy P&L tracking and validation gates work
independently. Fallback account is Sim101 (matches config.settings.ACCOUNT).

Resolution order
================
get_account_for_signal(strategy_name, sub_strategy=None):
  1. If strategy_name maps to a nested dict (e.g. "opening_session"):
       - sub_strategy given + key present → use nested value
       - sub_strategy missing OR key unknown → fall back to _default
  2. If strategy_name maps to a string → use it directly
  3. Otherwise (unknown strategy) → fall back to _default

Accounts listed here MUST match what Jennifer has configured in NT8.
Names are BYTE-EXACT NT8 display-name literals (including spaces and
mixed case). A single-character mismatch silently routes to Sim101
fallback with a [ROUTING] WARN — so grep the logs if anything looks off.

validate_account_map() returns the unique set for a visual cross-check
at bot startup.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Union

logger = logging.getLogger("AccountRouting")


# Default NT8 account used when no explicit mapping matches. Must match
# config.settings.ACCOUNT so that a routing-failure fallback still lands
# on a real, configured account rather than raising at NT8.
_DEFAULT_ACCOUNT = "Sim101"


STRATEGY_ACCOUNT_MAP: Dict[str, Union[str, Dict[str, str]]] = {
    # opening_session sub-strategies — each routes to its own dedicated
    # NT8 account for clean per-sub-strategy P&L isolation.
    "opening_session": {
        "open_drive":          "SimOpenDrive",
        "open_test_drive":     "SimOpen Test Drive",
        "open_auction_in":     "SimOpen Auction In Range",
        "open_auction_out":    "SimOpen Auction Out of Range",
        "premarket_breakout":  "SimPremarket Breakout",
        "orb":                 "SimORB",
    },

    # Top-level strategies — one account each. Names are BYTE-EXACT NT8
    # display names (including spaces and mixed case). Do not "tidy" —
    # a single character mismatch silently routes to Sim101 fallback.
    "bias_momentum":             "SimBias Momentum",
    "spring_setup":              "SimSpring Setup",
    "vwap_pullback":             "SimVWapp Pullback",
    "vwap_band_pullback":        "SimVwap Band Pullback",
    # 2026-05-19: operator's NT8 account is "SimVwap_Reversion" (underscore,
    # not space). Earlier map had "SimVwap Reversion" which silently routed
    # every fill to Sim101 fallback. Caught during Phase 13 ship audit.
    "vwap_band_reversion":       "SimVwap_Reversion",
    # "dom_pullback": deleted 2026-05-21 (0 trades / 5y backtest).
    # SimDom Pull Back account is orphaned in NT8 — safe to delete.
    "ib_breakout":               "SimIB Breakout",
    "compression_breakout_15m":  "SimCompression Breakout",
    "compression_breakout_30m":  "SimCompression Break out 30 MIN",
    "noise_area":                "SimNoise Area",
    "orb":                       "SimStand alone ORB",

    # Sprint H v3 (2026-05-04): institutional 4-confluence reversal
    # on a 1,500-tick volumetric stream. Lab-only (validated=False)
    # until 50+ trades + PF > 1.3. Operator must create this account
    # in NT8 ATI before signals will fill — meanwhile, signals route
    # but get dropped at the NT8 side.
    "footprint_cvd_reversal":    "SimFootprintchart",

    # 2026-05-17 Phase 9.1 hotfix: temp Sim101 routing; create dedicated
    # SimBigMove account when graduation decision is needed. Required to
    # clear the StrategyRiskRegistry warning + keep STRATEGY_KEYS in sync
    # with the routing map (the parity test in
    # tests/test_strategy_risk_registry.py enforces this).
    "big_move_signal":           "Sim101",

    # ── 2026-05-17: V2 overhaul deployment — 6 new strategy accounts ──
    # Note the naming convention shift: existing accounts use spaces
    # ("SimBias Momentum"); these new ones use underscores/hyphens
    # ("Sim_LSR", "Sim_ORB-Fade"). Strings are BYTE-EXACT NT8 display
    # names — do not normalize. SimORB_v2 deliberately has no
    # underscore between Sim and ORB (operator named it that way).
    "nq_lsr":                      "Sim_LSR",
    "orb_fade":                    "Sim_ORB-Fade",
    "orb_v2":                      "SimORB_v2",
    "compression_breakout_v2":     "Sim_Compression_v2",
    "compression_breakout_micro":  "Sim_Compression_Micro",
    "vwap_pullback_v2":            "Sim_VWAP_Pullback_v2",

    # 2026-05-18 Phase 12C: ES/NQ confluence LONG strategy. Routes to
    # Sim101 temporarily (same pattern as big_move_signal at Phase 9.1)
    # because creating a dedicated SimESNQConfluence account in NT8
    # requires operator GUI action, AND because the strategy is dormant
    # until the MES feed lands so there are no live trades to isolate
    # yet. Promote to a dedicated account when graduation decision
    # is needed (post-30-live-trades + Wilson-CI clearance).
    "es_nq_confluence":          "Sim101",

    # ── 2026-05-19: Phase 13 ship audit — 4 new dedicated accounts ──
    # Lab winners promoted to production strategies in commit 2c77d35.
    # Account names are BYTE-EXACT NT8 display names verified against
    # operator's account list screenshot. Note Sim_multi_Day's lowercase
    # 'm' — that is intentional, NT8 display is literally "Sim_multi_Day".
    "raschke_baseline":          "Sim_Raschke",
    "g_inside_bar_breakout":     "Sim_Inside_Bar",
    "e_multi_day_breakout":      "Sim_multi_Day",
    "a_asian_continuation":      "Sim_Asian",

    # Fallback — lands here on any unmapped strategy or sub_strategy.
    "_default":                  _DEFAULT_ACCOUNT,
}


def get_account_for_signal(
    strategy_name: str,
    sub_strategy: str | None = None,
) -> str:
    """
    Resolve a strategy (and optional sub-strategy) to its NT8 account.
    Unknown strategies and unknown sub-strategies fall back to _default.

    2026-04-21 HOTFIX (B40): NT8 ATI is not currently configured to
    auto-execute orders routed to the 16 dedicated Sim sub-accounts — only
    Sim101 receives actual fills. Until multi-account ATI is wired up in
    NT8, `MULTI_ACCOUNT_ROUTING_ENABLED=False` in settings forces every
    strategy to Sim101 so trades actually execute. Per-strategy risk
    isolation remains intact (that's Python-side via StrategyRiskRegistry).
    """
    # ── Multi-account ATI kill-switch (B40) ────────────────────────────
    # Dynamic attribute read so tests / runtime can toggle the flag.
    #
    # 2026-05-24 (operator directive): when LIVE_TRADING=True, ALWAYS force
    # single-account routing regardless of MULTI_ACCOUNT_ROUTING_ENABLED.
    # Live canary is a one-account-one-instrument-one-to-three-strategies
    # operation; multi-account routing is a sim-bot research mode that
    # has no place in live trading. This makes it IMPOSSIBLE for a
    # forgotten flag flip to route a real-money order to a sim sub-account.
    try:
        import config.settings as _s
        if getattr(_s, "LIVE_TRADING", False):
            return _DEFAULT_ACCOUNT
        if not getattr(_s, "MULTI_ACCOUNT_ROUTING_ENABLED", True):
            return _DEFAULT_ACCOUNT
    except Exception:
        pass  # Import failure → legacy multi-account behavior

    mapping = STRATEGY_ACCOUNT_MAP.get(strategy_name)
    default = STRATEGY_ACCOUNT_MAP.get("_default", _DEFAULT_ACCOUNT)

    if isinstance(mapping, dict):
        if sub_strategy is None:
            # Nested strategies (opening_session) must always carry a
            # sub_strategy — if one isn't provided, the caller has a bug.
            logger.warning(
                "[ROUTING] '%s' is nested but no sub_strategy passed — "
                "falling back to '%s'", strategy_name, default,
            )
            return default
        if sub_strategy not in mapping:
            logger.warning(
                "[ROUTING] unknown sub_strategy '%s' for '%s' — "
                "falling back to '%s'", sub_strategy, strategy_name, default,
            )
            return default
        return mapping[sub_strategy]

    if isinstance(mapping, str):
        return mapping

    logger.warning(
        "[ROUTING] unknown strategy '%s' — falling back to '%s'",
        strategy_name, default,
    )
    return default


def validate_account_map() -> List[str]:
    """
    Return the sorted unique set of NT8 account names referenced by the
    map. Call at bot startup and log the list so Jennifer can eyeball-
    compare against the NT8 account-config dropdown.
    """
    accounts: set[str] = set()
    for key, value in STRATEGY_ACCOUNT_MAP.items():
        if key == "_default":
            accounts.add(value)
        elif isinstance(value, str):
            accounts.add(value)
        elif isinstance(value, dict):
            accounts.update(value.values())
    return sorted(accounts)
