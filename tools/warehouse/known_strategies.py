"""
tools.warehouse.known_strategies — Load strategy keys from config/strategies.py.

Used by the WFA filename sniffer to match wfa_windows_p13_<name>.csv → strategy key.
Falls back to an empty set with a warning if config is unavailable.
"""

import logging
from functools import lru_cache

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_known_strategies() -> frozenset[str]:
    """Return frozenset of strategy keys defined in config/strategies.py."""
    try:
        from config.strategies import STRATEGIES  # type: ignore[import]
        keys = frozenset(STRATEGIES.keys())
        log.debug("known_strategies: loaded %d strategies from config", len(keys))
        return keys
    except Exception as exc:
        log.warning("could not load config/strategies.py (%s) — WFA filename sniff disabled", exc)
        return frozenset()


# Alias for plan-driven test compatibility.
load_known_strategies = get_known_strategies
