"""Live-canary gate — hard choke point that turns the multi-strategy research
bot into a 1-strategy production canary when LIVE_TRADING=True.

Why this exists (operator directive, 2026-05-24):

  The bot currently has 12+ enabled strategies, multi-account routing, AI
  advisory modules, and external data feeds. For real-money trading we need
  the opposite: one account, one instrument, one to three strategies max,
  no unvalidated strategies, no multi-account routing, no AI/external-feed
  dependency in the entry path. The reduction is not "delete files." It is
  "make it impossible for accidental complexity to place a real order."

  This module is that choke point. When LIVE_TRADING=True the gate refuses
  to let the bot start unless every live-mode constraint is satisfied. When
  LIVE_TRADING=False (the default sim/paper state) the gate is a no-op.

How it works:

  Two enforcement layers:
    1. `validate_live_config()` — called at bot startup BEFORE strategy
       loading. Raises LiveCanaryViolation if any live-mode constraint is
       violated. The bot fails to start. CRITICAL log line names every
       violation so the operator sees them.
    2. `filter_strategies_for_live(strategies)` — called by
       BaseBot.load_strategies(). Returns only the strategies that:
         - have their `name` in `LIVE_STRATEGY_ALLOWLIST`
         - have `validated=True` in config/strategies.py
         - have `enabled=True` in config/strategies.py
       Anything else is dropped with a CRITICAL log line.

  In sim mode (LIVE_TRADING=False) both layers are no-ops — sim_bot keeps
  its full multi-strategy / multi-account testing scope.

Constraints enforced when LIVE_TRADING=True:

  - `LIVE_STRATEGY_ALLOWLIST` is a non-empty tuple of strategy names.
  - Every name in the allowlist exists in `config.strategies.STRATEGIES` and
    is `validated=True` and `enabled=True`.
  - `MULTI_ACCOUNT_ROUTING_ENABLED` is False (canary uses ONE account).
  - `SIZING_MODE == "flat_1"` (no compounding in canary phase).
  - All three `AGENT_*_ENABLED` flags are False (no AI in the entry path).
  - `PHOENIX_SIM_OVERRIDES` env var is unset or "0" (already enforced by
    `core/sim_overrides_loader.py`, redundantly checked here).

Lifting the canary is a deliberate multi-step process documented in
`docs/audits/SYNTHESIS_2026-05-24.md` §P4-7.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, List

logger = logging.getLogger("LiveCanaryGate")


class LiveCanaryViolation(RuntimeError):
    """Raised when LIVE_TRADING=True and the canary-mode constraints are
    not satisfied. Bot startup must abort — this is the whole point of
    the gate."""


def _coerce_bool(value) -> bool:
    """Treat None / "" / "0" / False / 0 as False, everything else as True."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip() not in ("", "0", "false", "False", "FALSE")
    return bool(value)


def validate_live_config() -> None:
    """Validate that, when LIVE_TRADING=True, every live-mode constraint is
    satisfied. Raise LiveCanaryViolation listing every violation if not.

    Called from prod_bot.main() and sim_bot.main() at startup, BEFORE the
    bot instantiates. A no-op when LIVE_TRADING=False.

    Why "raise" instead of "warn": silent failures are the documented
    Phoenix anti-pattern (memory/feedback_silent_failures.md). The canary
    gate is the LAST line of defense; if any constraint is violated the
    bot must REFUSE TO START, not start with weakened guards.
    """
    from config import settings as _settings

    if not getattr(_settings, "LIVE_TRADING", False):
        # Sim/paper mode — canary gate is a no-op.
        logger.info("[CANARY] LIVE_TRADING=False — canary gate inactive (sim mode).")
        return

    violations: list[str] = []

    # 1. LIVE_STRATEGY_ALLOWLIST must exist and be a non-empty tuple/list/set.
    allowlist = getattr(_settings, "LIVE_STRATEGY_ALLOWLIST", None)
    if not allowlist:
        violations.append(
            "LIVE_STRATEGY_ALLOWLIST is empty or missing — refusing to "
            "trade live with zero allowed strategies."
        )
    elif not isinstance(allowlist, (tuple, list, set, frozenset)):
        violations.append(
            f"LIVE_STRATEGY_ALLOWLIST must be a tuple/list/set, "
            f"got {type(allowlist).__name__}."
        )
    else:
        # Every name must exist in STRATEGIES, be validated, and enabled.
        try:
            from config.strategies import STRATEGIES
        except ImportError:
            violations.append(
                "config.strategies.STRATEGIES not importable — cannot "
                "validate the allowlist."
            )
            STRATEGIES = None  # type: ignore

        if STRATEGIES is not None:
            for name in allowlist:
                cfg = STRATEGIES.get(name)
                if cfg is None:
                    violations.append(
                        f"LIVE_STRATEGY_ALLOWLIST contains {name!r} which "
                        "is not in config.strategies.STRATEGIES."
                    )
                    continue
                if not cfg.get("validated", False):
                    violations.append(
                        f"LIVE_STRATEGY_ALLOWLIST contains {name!r} but "
                        "config.strategies.STRATEGIES[name].validated is False."
                    )
                if not cfg.get("enabled", True):
                    violations.append(
                        f"LIVE_STRATEGY_ALLOWLIST contains {name!r} but "
                        "config.strategies.STRATEGIES[name].enabled is False."
                    )

    # 2. MULTI_ACCOUNT_ROUTING_ENABLED — defense in depth.
    # config/account_routing.py:account_for_strategy() also forces single-
    # account when LIVE_TRADING=True regardless of this flag, but we still
    # require the flag to be False so the intent is EXPLICIT in config —
    # not just implicit in runtime behavior. Two layers, both must agree.
    if getattr(_settings, "MULTI_ACCOUNT_ROUTING_ENABLED", True):
        violations.append(
            "MULTI_ACCOUNT_ROUTING_ENABLED is True — canary uses ONE "
            "account. Set MULTI_ACCOUNT_ROUTING_ENABLED=False in "
            "config/settings.py for live mode. (Defense in depth: "
            "account_routing already force-single-accounts in live mode, "
            "but this flag must also be flipped so config matches runtime.)"
        )

    # 3. SIZING_MODE must be flat_1 — no compounding in canary phase.
    sizing = getattr(_settings, "SIZING_MODE", "flat_1")
    if sizing != "flat_1":
        violations.append(
            f"SIZING_MODE is {sizing!r} — canary requires \"flat_1\". "
            "tier_3000 compounding is gated behind P4-7 reconciliation "
            "(docs/audits/SYNTHESIS_2026-05-24.md)."
        )

    # 4. No AI agents in the entry path.
    for flag in ("AGENT_COUNCIL_ENABLED", "AGENT_PRETRADE_FILTER_ENABLED",
                 "AGENT_DEBRIEF_ENABLED"):
        if getattr(_settings, flag, False):
            violations.append(
                f"{flag} is True — canary requires all three AGENT_* flags "
                "to be False (P0-4 disabled them; live mode must enforce)."
            )

    # 5. No sim_overrides active (already checked by sim_overrides_loader
    #    but enforce here for defense-in-depth).
    if os.environ.get("PHOENIX_SIM_OVERRIDES", "0") == "1":
        violations.append(
            "PHOENIX_SIM_OVERRIDES=1 is set — sim overrides are incompatible "
            "with LIVE_TRADING=True. Unset the env var."
        )

    if violations:
        message_lines = [
            "[CANARY] LIVE_TRADING=True but canary-mode constraints VIOLATED. "
            "Refusing to start the bot.",
            "",
            "Violations:",
        ]
        for i, v in enumerate(violations, 1):
            message_lines.append(f"  {i}. {v}")
        message_lines.extend([
            "",
            "To fix: address every violation above, then restart. The canary "
            "exists to make it impossible for accidental complexity to place "
            "a real order. See core/live_canary_gate.py docstring + "
            "docs/audits/SYNTHESIS_2026-05-24.md §P4-7.",
        ])
        full = "\n".join(message_lines)
        logger.critical(full)
        raise LiveCanaryViolation(full)

    # All constraints satisfied — log the canary roster for the operator.
    logger.critical(
        "[CANARY] LIVE_TRADING=True — canary mode ENGAGED. Allowed "
        "strategies: %s. Account: %s. Instrument: %s. Sizing: %s.",
        tuple(allowlist),
        getattr(_settings, "ACCOUNT", "?"),
        getattr(_settings, "INSTRUMENT", "?"),
        sizing,
    )


def filter_strategies_for_live(strategies: Iterable) -> List:
    """Return the subset of `strategies` allowed in live mode.

    `strategies` is an iterable of loaded strategy instances (post-config-
    construction, post-BaseStrategy-isinstance check). Each must have a
    `.name` attribute that maps to a key in `config.strategies.STRATEGIES`.

    When LIVE_TRADING=False, returns `strategies` unchanged.

    When LIVE_TRADING=True, returns only strategies that satisfy:
      - `name` in `LIVE_STRATEGY_ALLOWLIST`
      - `STRATEGIES[name].validated is True`
      - `STRATEGIES[name].enabled is True`
    Anything else is dropped with a CRITICAL log line.

    If the result is empty, that itself is a fatal-state condition — the
    caller (`base_bot.load_strategies`) is responsible for converting an
    empty result into a startup failure. (Done there, not here, so this
    function stays pure-filter.)
    """
    from config import settings as _settings

    if not getattr(_settings, "LIVE_TRADING", False):
        return list(strategies)

    allowlist = set(getattr(_settings, "LIVE_STRATEGY_ALLOWLIST", ()) or ())
    try:
        from config.strategies import STRATEGIES
    except ImportError:
        STRATEGIES = {}  # type: ignore

    kept = []
    for strat in strategies:
        name = getattr(strat, "name", None)
        if name is None:
            logger.critical(
                "[CANARY] strategy instance has no .name attribute — DROPPED."
            )
            continue
        if name not in allowlist:
            logger.critical(
                "[CANARY] strategy %r DROPPED — not in LIVE_STRATEGY_ALLOWLIST.",
                name,
            )
            continue
        cfg = STRATEGIES.get(name, {})
        if not cfg.get("validated", False):
            logger.critical(
                "[CANARY] strategy %r DROPPED — validated=False.", name,
            )
            continue
        if not cfg.get("enabled", True):
            logger.critical(
                "[CANARY] strategy %r DROPPED — enabled=False.", name,
            )
            continue
        kept.append(strat)
    return kept
