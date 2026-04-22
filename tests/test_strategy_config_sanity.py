"""
CI-level strategy config sanity checks (WS-A guaranteed-loss audit, 2026-04-21).

Locks the door that Jennifer's noise_area bug walked through:

1. Every enabled strategy in config/strategies.py has either
     target_rr >= 1.0   OR   uses_managed_exit = True on the class.
2. Every enabled strategy has a positive stop_ticks in config OR computes
   its own stop (ATR-anchored / structural — stop_method, min_stop_ticks,
   or atr_stop_multiplier set).
3. Every strategy key in STRATEGIES maps to an importable module in the
   strategies/ directory with a BaseStrategy subclass.
4. If target_rr >= 10, the strategy must either set uses_managed_exit,
   produce an exit_trigger, or the config must carry the explicit marker
   `_wide_target_requires_trailing=True` flagging it for follow-up.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

from config.strategies import STRATEGIES
from strategies.base_strategy import BaseStrategy


# ── Mapping strategy config name → (module, class) ────────────────────────
# Derived dynamically below so a new strategy doesn't have to touch this
# fixture, only add a BaseStrategy subclass whose `name` matches the key.
def _discover_strategy_classes() -> dict[str, type[BaseStrategy]]:
    import strategies as strategies_pkg

    found: dict[str, type[BaseStrategy]] = {}
    for mod_info in pkgutil.iter_modules(strategies_pkg.__path__):
        name = mod_info.name
        if name.startswith("_") or name == "base_strategy":
            continue
        mod = importlib.import_module(f"strategies.{name}")
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseStrategy)
                and attr is not BaseStrategy
            ):
                strat_name = getattr(attr, "name", None)
                if strat_name:
                    found[strat_name] = attr
    return found


STRATEGY_CLASSES = _discover_strategy_classes()

ENABLED_STRATEGIES = [name for name, cfg in STRATEGIES.items() if cfg.get("enabled")]


# ── 3. Every config key has a matching strategy module ────────────────────

@pytest.mark.parametrize("name", list(STRATEGIES.keys()))
def test_strategy_config_has_module(name):
    assert name in STRATEGY_CLASSES, (
        f"Strategy '{name}' in STRATEGIES has no matching BaseStrategy "
        f"subclass with .name = '{name}' in the strategies/ package. "
        f"Discovered: {sorted(STRATEGY_CLASSES.keys())}"
    )


# ── 1. target_rr >= 1 OR uses_managed_exit ────────────────────────────────

@pytest.mark.parametrize("name", ENABLED_STRATEGIES)
def test_target_rr_or_managed_exit(name):
    cfg = STRATEGIES[name]
    cls = STRATEGY_CLASSES.get(name)
    assert cls is not None, f"No class for {name} (test_strategy_config_has_module covers this)"

    target_rr = cfg.get("target_rr", None)
    uses_managed = getattr(cls, "uses_managed_exit", False)
    computes_own_target = getattr(cls, "computes_own_target", False)

    if uses_managed or computes_own_target:
        return  # Managed / self-targeting strategies legitimately omit target_rr

    # If target_rr is absent, base_bot would synthesize target=entry via
    # stop_ticks * 0 — the exact bug we're preventing.
    assert target_rr is not None, (
        f"{name}: target_rr missing in config. Either set target_rr >= 1.0 or "
        f"flag the class with uses_managed_exit=True."
    )
    assert target_rr >= 1.0, (
        f"{name}: target_rr={target_rr} < 1.0 — OCO target would land at or "
        f"behind entry. Set >= 1.0 or flag uses_managed_exit=True on the class."
    )


# ── 2. Positive stop OR self-computed stop ────────────────────────────────

def _strategy_computes_own_stop(cfg: dict) -> bool:
    """A strategy is 'self-stopping' if its config declares any of the
    structural-stop knobs that the strategy file actually uses."""
    return any(
        key in cfg
        for key in (
            "stop_method",           # e.g. "atr_anchored"
            "min_stop_ticks",        # ATR/structural clamp
            "atr_stop_multiplier",   # spring_setup pattern
            "stop_multiplier",       # fallback wick multiplier
            "max_stop_points",       # orb
            "stop_buffer_ticks",     # compression / opening_session
            "stop_at_structure",     # spring_setup
            "stop_at_ib_midpoint",   # ib_breakout
        )
    )


@pytest.mark.parametrize("name", ENABLED_STRATEGIES)
def test_stop_ticks_positive_or_self_computed(name):
    cfg = STRATEGIES[name]
    cls = STRATEGY_CLASSES.get(name)
    stop_ticks = cfg.get("stop_ticks", None)

    if stop_ticks is not None:
        assert stop_ticks > 0, f"{name}: stop_ticks={stop_ticks} must be > 0"
        return

    if getattr(cls, "computes_own_stop", False):
        return

    assert _strategy_computes_own_stop(cfg), (
        f"{name}: no stop_ticks and no self-stopping config keys "
        f"(stop_method/min_stop_ticks/atr_stop_multiplier/stop_buffer_ticks/"
        f"max_stop_points/stop_at_structure/stop_at_ib_midpoint). "
        f"base_bot would have no stop distance to work with."
    )


# ── 4. Wide targets (>= 10) require trailing / managed exit / marker ─────

WIDE_TARGET_THRESHOLD = 10.0


@pytest.mark.parametrize("name", ENABLED_STRATEGIES)
def test_wide_target_has_trailing_or_marker(name):
    cfg = STRATEGIES[name]
    cls = STRATEGY_CLASSES.get(name)
    target_rr = cfg.get("target_rr", 0) or 0
    if target_rr < WIDE_TARGET_THRESHOLD:
        return

    uses_managed = getattr(cls, "uses_managed_exit", False) if cls else False
    has_marker = cfg.get("_wide_target_requires_trailing", False)

    # We can't easily introspect exit_trigger at test time without a live
    # market snapshot, so uses_managed_exit OR the config marker is the
    # CI-enforceable contract.
    assert uses_managed or has_marker, (
        f"{name}: target_rr={target_rr} >= {WIDE_TARGET_THRESHOLD} but strategy "
        f"is not flagged uses_managed_exit and config lacks "
        f"`_wide_target_requires_trailing=True`. This is an unreachable "
        f"target. Either implement trailing / managed exit, bring target_rr "
        f"under {WIDE_TARGET_THRESHOLD}, or (if intentional for research) add "
        f"the marker to document follow-up."
    )
