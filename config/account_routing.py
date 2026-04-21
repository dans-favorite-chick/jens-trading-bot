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
    # opening_session sub-strategies — each routes to a dedicated account.
    # open_drive and open_test_drive share an account because they trade
    # the same 8:30-9:00 CT window and rarely fire on the same day.
    "opening_session": {
        "open_drive":          "SimOpenDrive",
        "open_test_drive":     "SimOpenDrive",
        "open_auction_in":     "SimOpenAuctionInRange",
        "open_auction_out":    "SimOpenAuctionOutOfRange",
        "premarket_breakout":  "SimPremarketBreakout",
        "orb":                 "SimORB",
    },

    # Top-level strategies — one account each.
    "bias_momentum":        "SimBiasMomentum",
    "spring_setup":         "SimSpringSetup",
    "vwap_pullback":        "SimVWappPullback",   # NT8 config uses this literal
    "vwap_band_pullback":   "SimVWappPullback",   # shares with vwap_pullback
    "dom_pullback":         "SimDomPullBack",
    "ib_breakout":          "SimIBBreakout",
    "compression_breakout": "SimCompressionBreakout",
    "noise_area":           "SimNoiseArea",

    # Fallback.
    "_default":             _DEFAULT_ACCOUNT,
}


def get_account_for_signal(
    strategy_name: str,
    sub_strategy: str | None = None,
) -> str:
    """
    Resolve a strategy (and optional sub-strategy) to its NT8 account.
    Unknown strategies and unknown sub-strategies fall back to _default.
    """
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
