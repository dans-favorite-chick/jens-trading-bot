"""
Phoenix Bot — Per-Strategy Risk Registry (Phase C, 2026-04-21)

Maintains one RiskManager instance per strategy (and per opening_session
sub-strategy) so each of the 16 validation accounts has isolated:
  - daily P&L tracking
  - $200/day loss cap (configurable via PER_STRATEGY_DAILY_LOSS_CAP)
  - $1,500 floor kill-switch (PER_STRATEGY_FLOOR)
  - cumulative P&L (for floor-check against $2,000 starting balance)

Halt state persists to logs/strategy_halts.json so a bot restart cannot
resurrect a strategy that hit its floor — only tools/reenable_strategy.py
(manual) can clear the halt.

Strategy keys match those in config/account_routing.py, with nested
opening_session sub-strategies keyed as "opening_session.<sub>".
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict

from config.settings import (
    PER_STRATEGY_ACCOUNT_SIZE,
    PER_STRATEGY_DAILY_LOSS_CAP,
    PER_STRATEGY_FLOOR,
    STRATEGY_HALT_STATE_FILE,
)
from core.risk_manager import RiskManager

logger = logging.getLogger("StrategyRiskRegistry")


# All strategy keys the registry must initialize. MUST stay aligned with
# STRATEGY_ACCOUNT_MAP in config/account_routing.py — if a key there is
# added or renamed, update this list AND add a test.
STRATEGY_KEYS: list[str] = [
    # opening_session sub-strategies (6) — each on its own account.
    "opening_session.open_drive",
    "opening_session.open_test_drive",
    "opening_session.open_auction_in",
    "opening_session.open_auction_out",
    "opening_session.premarket_breakout",
    "opening_session.orb",
    # Top-level strategies (10).
    "bias_momentum",
    "spring_setup",
    "vwap_pullback",
    "vwap_band_pullback",
    "dom_pullback",
    "ib_breakout",
    "compression_breakout_15m",
    "compression_breakout_30m",
    "noise_area",
    "orb",
]


def _key_for(strategy: str, sub_strategy: str | None = None) -> str:
    """Canonical registry key builder."""
    if sub_strategy:
        return f"{strategy}.{sub_strategy}"
    return strategy


class StrategyRiskRegistry:
    """One RiskManager per strategy + cumulative-balance tracking + halts."""

    def __init__(self):
        self._managers: Dict[str, RiskManager] = {}
        # Cumulative P&L per strategy (separate from daily — never resets).
        # Starting balance = PER_STRATEGY_ACCOUNT_SIZE; current balance =
        # starting + cumulative_pnl. Floor check uses current_balance.
        self._cumulative_pnl: Dict[str, float] = {}
        # Halted strategies (persisted).
        self._halted: set[str] = set()
        self._halt_reasons: Dict[str, str] = {}

        self._load_halt_state()
        for key in STRATEGY_KEYS:
            self._managers[key] = self._make_manager(key)
            self._cumulative_pnl[key] = 0.0

    # ─── Instance construction ────────────────────────────────────────

    def _make_manager(self, key: str) -> RiskManager:
        """Create a RiskManager configured for per-strategy limits."""
        rm = RiskManager()
        # Override the bot-wide daily cap with the per-strategy one.
        rm.set_daily_limit(PER_STRATEGY_DAILY_LOSS_CAP)
        return rm

    # ─── Lookup ──────────────────────────────────────────────────────

    def get(self, strategy: str, sub_strategy: str | None = None) -> RiskManager:
        """Return the RiskManager for a strategy (or sub_strategy).

        Unknown keys return a lazily-instantiated fallback manager AND
        log a WARNING — in sim mode this should never happen because
        STRATEGY_KEYS is kept in sync with the routing map. Surfaces
        loudly rather than silently landing on shared state.
        """
        key = _key_for(strategy, sub_strategy)
        if key not in self._managers:
            logger.warning(
                "[RISK] unknown strategy key '%s' — using ephemeral fallback. "
                "STRATEGY_KEYS may be out of sync with account_routing map.",
                key,
            )
            self._managers[key] = self._make_manager(key)
            self._cumulative_pnl.setdefault(key, 0.0)
        return self._managers[key]

    def known_keys(self) -> list[str]:
        return list(self._managers.keys())

    # ─── Balance tracking ────────────────────────────────────────────

    def current_balance(self, strategy: str, sub_strategy: str | None = None) -> float:
        """Strategy account balance: starting + cumulative P&L."""
        key = _key_for(strategy, sub_strategy)
        return PER_STRATEGY_ACCOUNT_SIZE + self._cumulative_pnl.get(key, 0.0)

    def record_trade_result(self, strategy: str, pnl_dollars: float,
                            sub_strategy: str | None = None) -> bool:
        """Update cumulative P&L for a strategy + check floor.

        Returns True if the trade pushed the strategy below the floor
        (caller should consult is_halted() and/or log accordingly).
        Also propagates the result to the underlying RiskManager for
        daily-cap and cooloff bookkeeping.
        """
        key = _key_for(strategy, sub_strategy)
        rm = self.get(strategy, sub_strategy)
        rm.record_trade(pnl_dollars)

        self._cumulative_pnl[key] = self._cumulative_pnl.get(key, 0.0) + pnl_dollars
        balance = PER_STRATEGY_ACCOUNT_SIZE + self._cumulative_pnl[key]

        if balance <= PER_STRATEGY_FLOOR and key not in self._halted:
            self.halt(
                strategy, sub_strategy,
                reason=f"balance ${balance:.2f} <= floor ${PER_STRATEGY_FLOOR:.2f}"
            )
            return True
        return False

    def total_unrealized(self) -> float:
        """Sum of all cumulative P&L across strategies (for dashboard)."""
        return sum(self._cumulative_pnl.values())

    # ─── Halt management (persisted) ────────────────────────────────

    def is_halted(self, strategy: str, sub_strategy: str | None = None) -> bool:
        return _key_for(strategy, sub_strategy) in self._halted

    def halt(self, strategy: str, sub_strategy: str | None = None, reason: str = ""):
        """Permanently halt a strategy. Persisted to disk.

        Subsequent bot restarts will re-read the halt state and continue
        blocking. Only tools/reenable_strategy.py can clear it.
        """
        key = _key_for(strategy, sub_strategy)
        if key in self._halted:
            return
        self._halted.add(key)
        self._halt_reasons[key] = reason
        logger.critical(
            "[HALT] strategy '%s' halted — %s. Manual re-enable required "
            "(tools/reenable_strategy.py).",
            key, reason,
        )
        self._save_halt_state()

    def reenable(self, strategy: str, sub_strategy: str | None = None) -> bool:
        """Clear a halt. Returns True if the key was halted, else False.

        Intended to be called ONLY from tools/reenable_strategy.py after
        explicit operator review.
        """
        key = _key_for(strategy, sub_strategy)
        if key not in self._halted:
            return False
        self._halted.discard(key)
        self._halt_reasons.pop(key, None)
        self._save_halt_state()
        logger.info("[HALT] strategy '%s' re-enabled", key)
        return True

    def halt_reason(self, strategy: str, sub_strategy: str | None = None) -> str | None:
        return self._halt_reasons.get(_key_for(strategy, sub_strategy))

    # ─── Persistence ────────────────────────────────────────────────

    def _load_halt_state(self):
        path = STRATEGY_HALT_STATE_FILE
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._halted = set(data.get("halted", []))
            self._halt_reasons = dict(data.get("reasons", {}))
            if self._halted:
                logger.warning(
                    "[HALT] loaded %d halted strategies from %s: %s",
                    len(self._halted), path, sorted(self._halted),
                )
        except Exception as e:
            logger.error("[HALT] failed to load halt state from %s: %s", path, e)

    def _save_halt_state(self):
        path = STRATEGY_HALT_STATE_FILE
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "halted": sorted(self._halted),
                    "reasons": self._halt_reasons,
                }, f, indent=2)
        except Exception as e:
            logger.error("[HALT] failed to save halt state to %s: %s", path, e)

    # ─── Dashboard ───────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Dashboard snapshot — full per-strategy state map."""
        out: dict[str, dict] = {}
        for key, rm in self._managers.items():
            out[key] = {
                "daily_pnl": rm.state.daily_pnl,
                "cumulative_pnl": self._cumulative_pnl.get(key, 0.0),
                "current_balance": PER_STRATEGY_ACCOUNT_SIZE + self._cumulative_pnl.get(key, 0.0),
                "trades_today": rm.state.trades_today,
                "wins_today": rm.state.wins_today,
                "losses_today": rm.state.losses_today,
                "consecutive_losses": rm.state.consecutive_losses,
                "halted": key in self._halted,
                "halt_reason": self._halt_reasons.get(key),
            }
        return out

    def daily_reset(self):
        """Call at session rollover to reset all per-strategy daily state.

        Cumulative P&L and halt state are PRESERVED — only daily P&L,
        trade counts, and cooloff timers reset.
        """
        for rm in self._managers.values():
            # Reach into RiskState for a clean reset matching the
            # underlying RiskManager daily semantics.
            rm.state.daily_pnl = 0.0
            rm.state.trades_today = 0
            rm.state.wins_today = 0
            rm.state.losses_today = 0
            rm.state.consecutive_losses = 0
            rm.state.cooloff_until = 0.0
        logger.info("[RISK] per-strategy daily state reset")
