"""Sim-testing overrides — OPT-IN via PHOENIX_SIM_OVERRIDES=1 env var.

Why this file exists
--------------------
The "operator override" inline-comment pattern in config/settings.py and
config/strategies.py was identified in the 2026-05-24 audit synthesis as
findings F-08 and F-09 — production source carried sim-testing values for
weeks; one ($50 trade cap) survived 3 days late on restore. The mechanism
(text comments saying "RESTORE before live") does not work.

This file is the new opt-in channel for sim-testing overrides. Anything
that's an override goes HERE, never in production config files. The loader
(core/sim_overrides_loader.py) refuses to apply any of this unless the env
var PHOENIX_SIM_OVERRIDES=1 is set, and it REFUSES TO START the bot at all
if PHOENIX_SIM_OVERRIDES=1 is combined with LIVE_TRADING=True.

Behavior
--------
- env unset or "0": this file is not loaded; production config wins.
  Startup banner reads "[CONFIG] sim_overrides: none".
- env "1": SETTINGS_OVERRIDES and STRATEGY_OVERRIDES are applied at bot
  startup. CRITICAL log lines name every overridden value. Startup banner
  reads "[CONFIG] sim_overrides ACTIVE: N settings, M strategies".
- env "1" AND LIVE_TRADING=True: bot raises SimOverrideLiveConflict and
  refuses to start.

Format
------
SETTINGS_OVERRIDES: dict[str, Any]
    Keys are attribute names on config.settings (e.g., "DAILY_LOSS_LIMIT").
    Unknown keys log a warning and are skipped.

STRATEGY_OVERRIDES: dict[str, dict[str, Any]]
    Keys are strategy names from config.strategies.STRATEGY_DEFAULTS.
    Values are partial dicts of field patches (e.g., {"validated": True}).
    Unknown strategies log a warning and are skipped.

Examples (commented out — uncomment when needed for sim testing)
----------------------------------------------------------------
SETTINGS_OVERRIDES = {
    # "DAILY_LOSS_LIMIT": 1_000_000,          # disable daily cap for stress test
    # "MAX_ACTUAL_STOP_DOLLARS_PER_TRADE": 100,  # widen per-trade cap for V2 deploy
}

STRATEGY_OVERRIDES = {
    # "bias_momentum": {"validated": True},  # force-validate for early data collection
    # "spring_setup": {"enabled": True, "validated": True},
}
"""
from __future__ import annotations

from typing import Any

# Empty by default — production state. Add entries only when sim-testing
# something that requires bypassing the production rule, AND make sure
# PHOENIX_SIM_OVERRIDES=1 is set in your shell before launching the bot.
SETTINGS_OVERRIDES: dict[str, Any] = {}

STRATEGY_OVERRIDES: dict[str, dict[str, Any]] = {}
