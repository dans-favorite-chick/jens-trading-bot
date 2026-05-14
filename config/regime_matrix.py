"""Regime activation matrix loader (#7, 2026-05-13).

Reads `memory/procedural/regime_matrix.yaml` and exposes typed
accessors so base_bot can answer "is strategy X allowed in regime Y?"
without re-reading YAML on every signal.

The YAML is the source of truth (editable without code reload);
this module is the typed view of it.

Public API:
    load_matrix(path=None) -> RegimeMatrix
    RegimeMatrix.state(strategy, regime) -> StrategyState
    RegimeMatrix.is_active(strategy, regime) -> bool
    RegimeMatrix.requires_higher_score(strategy, regime) -> bool

Regime states:
    ON       — full conviction threshold, full size
    REDUCED  — +10 conviction points required, half size
    OFF      — strategy does not fire in this regime
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("RegimeMatrix")

_DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent
    / "memory" / "procedural" / "regime_matrix.yaml"
)


class StrategyState(str, Enum):
    ON = "ON"
    REDUCED = "REDUCED"
    OFF = "OFF"


# Known regime keys — kept lowercase comparison-safe. If the YAML
# introduces a new regime, add it here so callers can fail loudly on typos.
KNOWN_REGIMES: frozenset[str] = frozenset({
    "POS_GEX_LOW_VIX", "POS_GEX_HIGH_VIX",
    "NEG_GEX_LOW_VIX", "NEG_GEX_HIGH_VIX",
    "UNKNOWN",
})


@dataclass(frozen=True)
class RegimeMatrix:
    """Immutable typed view of the YAML matrix.

    Use `load_matrix()` rather than instantiating directly — the loader
    handles the YAML import + fallback when PyYAML isn't installed.
    """
    by_strategy: dict[str, dict[str, StrategyState]]
    source_path: Optional[Path] = None

    def state(self, strategy: str, regime: str) -> StrategyState:
        """Returns the state for (strategy, regime). Defaults:
          - Unknown strategy → ON (don't accidentally disable a strategy
            just because it isn't in the matrix yet).
          - Unknown regime    → REDUCED (conservative — slow it down
            until the matrix is updated).
        """
        per_regime = self.by_strategy.get(strategy)
        if per_regime is None:
            return StrategyState.ON
        return per_regime.get(regime, StrategyState.REDUCED)

    def is_active(self, strategy: str, regime: str) -> bool:
        """True iff the strategy is allowed to fire in this regime
        (state in {ON, REDUCED})."""
        return self.state(strategy, regime) != StrategyState.OFF

    def requires_higher_score(self, strategy: str, regime: str) -> bool:
        """True iff REDUCED (caller should add +10 to conviction
        threshold + halve size)."""
        return self.state(strategy, regime) == StrategyState.REDUCED


def _parse_state(raw: object) -> StrategyState:
    """Normalize a YAML-loaded state string → StrategyState. Unknown =>
    REDUCED (conservative; caller can override).

    YAML 1.1 gotcha: unquoted ON/OFF parse as booleans (True/False), not
    strings — handle that explicitly. The real regime_matrix.yaml uses
    unquoted ON/REDUCED/OFF, so this is the hot path."""
    if isinstance(raw, StrategyState):
        return raw
    if isinstance(raw, bool):
        return StrategyState.ON if raw else StrategyState.OFF
    if not isinstance(raw, str):
        return StrategyState.REDUCED
    up = raw.strip().upper()
    if up == "ON":
        return StrategyState.ON
    if up == "REDUCED":
        return StrategyState.REDUCED
    if up == "OFF":
        return StrategyState.OFF
    return StrategyState.REDUCED


def load_matrix(path: Optional[Path] = None) -> RegimeMatrix:
    """Load the YAML matrix. Returns an empty matrix on any failure —
    callers should treat empty as 'no opinion' (strategy.state == ON for
    every (strategy, regime) pair). Logs the failure reason."""
    p = path or _DEFAULT_PATH
    if not p.exists():
        logger.warning(f"[regime_matrix] not found at {p} — empty matrix")
        return RegimeMatrix(by_strategy={}, source_path=p)
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("[regime_matrix] PyYAML not installed — empty matrix")
        return RegimeMatrix(by_strategy={}, source_path=p)
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"[regime_matrix] load failed ({e}) — empty matrix")
        return RegimeMatrix(by_strategy={}, source_path=p)
    if not isinstance(raw, dict):
        logger.warning(f"[regime_matrix] malformed YAML at {p} — empty matrix")
        return RegimeMatrix(by_strategy={}, source_path=p)
    sm = raw.get("strategy_matrix", raw)
    if not isinstance(sm, dict):
        logger.warning(f"[regime_matrix] malformed YAML at {p} — empty matrix")
        return RegimeMatrix(by_strategy={}, source_path=p)
    by_strategy: dict[str, dict[str, StrategyState]] = {}
    for strat, per_regime in sm.items():
        if not isinstance(per_regime, dict):
            continue
        by_strategy[str(strat)] = {
            str(reg): _parse_state(state)
            for reg, state in per_regime.items()
        }
    return RegimeMatrix(by_strategy=by_strategy, source_path=p)
