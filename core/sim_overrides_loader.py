"""Loader for config/sim_overrides.py — gated by PHOENIX_SIM_OVERRIDES=1.

Called from prod_bot.main() and sim_bot.main() before BaseBot instantiation.
See config/sim_overrides.py for the why.

Closes F-08 / F-09 from docs/audits/SYNTHESIS_2026-05-24.md:
- Forces overrides through a single named channel instead of inline comments.
- Refuses to start when sim overrides + LIVE_TRADING=True are combined
  (the live-trading interlock).
- Logs every applied override at CRITICAL level so the banner is unmissable.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("SIM_OVERRIDES")


class SimOverrideLiveConflict(RuntimeError):
    """Raised when PHOENIX_SIM_OVERRIDES=1 AND settings.LIVE_TRADING=True.

    These two states are mutually exclusive: sim overrides are for
    sim-only testing; live trading must run on production config.
    """


def _coerce_flag(value: str | None) -> bool:
    """PHOENIX_SIM_OVERRIDES is on if env value is exactly '1'."""
    return value == "1"


def load_and_apply_sim_overrides() -> dict[str, Any]:
    """Apply sim overrides if enabled. Returns a status dict for the banner.

    Status dict shape:
        {
            "active": bool,
            "settings_count": int,
            "strategies_count": int,
            "applied_settings": list[str],
            "applied_strategies": list[str],
        }
    """
    flag_raw = os.environ.get("PHOENIX_SIM_OVERRIDES")
    flag = _coerce_flag(flag_raw)

    status: dict[str, Any] = {
        "active": False,
        "settings_count": 0,
        "strategies_count": 0,
        "applied_settings": [],
        "applied_strategies": [],
    }

    if not flag:
        logger.info(
            "[CONFIG] sim_overrides: none (PHOENIX_SIM_OVERRIDES=%r)", flag_raw,
        )
        return status

    # Live-trading interlock — refuse before doing anything else.
    from config import settings as _settings

    if getattr(_settings, "LIVE_TRADING", False):
        raise SimOverrideLiveConflict(
            "PHOENIX_SIM_OVERRIDES=1 is incompatible with LIVE_TRADING=True. "
            "Either unset PHOENIX_SIM_OVERRIDES or set LIVE_TRADING=False in "
            "config/settings.py."
        )

    try:
        from config import sim_overrides as _so
    except ImportError:
        logger.warning(
            "[CONFIG] PHOENIX_SIM_OVERRIDES=1 but config/sim_overrides.py "
            "not found — nothing applied",
        )
        return status

    settings_overrides: dict[str, Any] = getattr(_so, "SETTINGS_OVERRIDES", {}) or {}
    strategy_overrides: dict[str, dict[str, Any]] = (
        getattr(_so, "STRATEGY_OVERRIDES", {}) or {}
    )

    # Apply settings overrides
    for key, new_value in settings_overrides.items():
        if not hasattr(_settings, key):
            logger.warning(
                "[CONFIG] sim_overrides: settings.%s does not exist — skipped",
                key,
            )
            continue
        old_value = getattr(_settings, key)
        setattr(_settings, key, new_value)
        status["applied_settings"].append(key)
        logger.critical(
            "[CONFIG] sim_overrides: settings.%s = %r (was %r)",
            key, new_value, old_value,
        )
    status["settings_count"] = len(status["applied_settings"])

    # Apply strategy overrides. Per-strategy configs live in
    # config.strategies.STRATEGIES (NOT STRATEGY_DEFAULTS — that's the
    # global slider defaults dict).
    if strategy_overrides:
        try:
            from config import strategies as _strats
        except ImportError:
            logger.error(
                "[CONFIG] sim_overrides: cannot import config.strategies — "
                "strategy overrides skipped",
            )
        else:
            strategies_dict = getattr(_strats, "STRATEGIES", {})
            for name, patches in strategy_overrides.items():
                if name not in strategies_dict:
                    logger.warning(
                        "[CONFIG] sim_overrides: strategy %r not in "
                        "STRATEGIES — skipped", name,
                    )
                    continue
                for field, new_value in patches.items():
                    old_value = strategies_dict[name].get(field)
                    strategies_dict[name][field] = new_value
                    logger.critical(
                        "[CONFIG] sim_overrides: STRATEGIES[%r][%r] = %r "
                        "(was %r)", name, field, new_value, old_value,
                    )
                status["applied_strategies"].append(name)
    status["strategies_count"] = len(status["applied_strategies"])

    status["active"] = True
    logger.critical(
        "[CONFIG] sim_overrides ACTIVE: %d settings, %d strategies "
        "(env PHOENIX_SIM_OVERRIDES=1)",
        status["settings_count"], status["strategies_count"],
    )
    return status
