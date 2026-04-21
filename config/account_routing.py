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
    "dom_pullback":              "SimDom Pull Back",
    "ib_breakout":               "SimIB Breakout",
    "compression_breakout_15m":  "SimCompression Breakout",
    "compression_breakout_30m":  "SimCompression Break out 30 MIN",
    "noise_area":                "SimNoise Area",
    "orb":                       "SimStand alone ORB",

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
    try:
        import config.settings as _s
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
