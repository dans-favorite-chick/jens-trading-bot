"""
Phoenix Bot — Strategy Halt Recovery CLI (Phase C, 2026-04-21)

Per-strategy risk isolation auto-halts any strategy whose account drops to
$1,500 from a $2,000 starting balance. The halt is persisted to
logs/strategy_halts.json and SURVIVES bot restart on purpose — a blown
strategy should not silently resume trading just because the bot was
cycled.

This CLI is the only sanctioned way to clear a halt. Intended workflow:
  1. Operator reviews the halted strategy's trade log / debrief.
  2. Operator manually runs this tool with the exact strategy key.
  3. Next bot start (or next signal) resumes trading for that key.

Usage:
  python tools/reenable_strategy.py                 # list halted
  python tools/reenable_strategy.py <strategy_key>  # clear one halt
  python tools/reenable_strategy.py --all           # clear every halt
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.strategy_risk_registry import StrategyRiskRegistry


def _split_key(key: str) -> tuple[str, str | None]:
    if "." in key:
        strategy, sub = key.split(".", 1)
        return strategy, sub
    return key, None


def _list_halts(registry: StrategyRiskRegistry) -> int:
    halted = sorted(registry._halted)
    if not halted:
        print("No halted strategies.")
        return 0
    print(f"{len(halted)} halted strateg{'y' if len(halted) == 1 else 'ies'}:")
    for key in halted:
        reason = registry._halt_reasons.get(key, "(no reason recorded)")
        print(f"  {key}: {reason}")
    return 0


def _clear_all(registry: StrategyRiskRegistry) -> int:
    keys = sorted(registry._halted)
    count = 0
    for key in keys:
        strategy, sub = _split_key(key)
        if registry.reenable(strategy, sub):
            count += 1
    print(f"Cleared {count} halt{'s' if count != 1 else ''}.")
    return 0


def _clear_one(registry: StrategyRiskRegistry, key: str) -> int:
    strategy, sub = _split_key(key)
    if not registry.is_halted(strategy, sub):
        print(f"Strategy '{key}' is not halted.")
        return 1
    registry.reenable(strategy, sub)
    print(f"Re-enabled '{key}'.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    registry = StrategyRiskRegistry()

    if not args:
        return _list_halts(registry)
    if len(args) == 1 and args[0] == "--all":
        return _clear_all(registry)
    if len(args) == 1:
        return _clear_one(registry, args[0])

    print("Usage: reenable_strategy.py [<strategy_key> | --all]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
