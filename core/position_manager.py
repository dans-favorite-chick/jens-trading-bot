"""
Phoenix Bot — Position Manager

Tracks open positions, unrealized P&L, and manages stop/target exits.
"""

import time
import logging
from dataclasses import dataclass

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import TICK_SIZE

logger = logging.getLogger("PositionManager")

# MNQ: each tick (0.25) = $0.50
DOLLAR_PER_TICK = TICK_SIZE * 2


@dataclass
class Position:
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    entry_time: float
    contracts: int
    stop_price: float
    target_price: float
    strategy: str
    reason: str
    market_snapshot: dict  # Snapshot of market data at entry


class PositionManager:
    def __init__(self):
        self.position: Position | None = None
        self.trade_history: list[dict] = []

    @property
    def is_flat(self) -> bool:
        return self.position is None

    @property
    def is_long(self) -> bool:
        return self.position is not None and self.position.direction == "LONG"

    @property
    def is_short(self) -> bool:
        return self.position is not None and self.position.direction == "SHORT"

    def open_position(self, direction: str, entry_price: float, contracts: int,
                      stop_price: float, target_price: float,
                      strategy: str, reason: str, market_snapshot: dict = None):
        """Open a new position. Raises if already in a position."""
        if self.position is not None:
            logger.warning("Cannot open position — already in trade")
            return False

        self.position = Position(
            direction=direction.upper(),
            entry_price=entry_price,
            entry_time=time.time(),
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            strategy=strategy,
            reason=reason,
            market_snapshot=market_snapshot or {},
        )
        logger.info(f"[OPEN] {direction} {contracts}x @ {entry_price} "
                     f"SL={stop_price} TP={target_price} strat={strategy}")
        return True

    def close_position(self, exit_price: float, exit_reason: str) -> dict | None:
        """Close current position and return trade record."""
        if self.position is None:
            return None

        pos = self.position
        if pos.direction == "LONG":
            ticks_pnl = (exit_price - pos.entry_price) / TICK_SIZE
        else:
            ticks_pnl = (pos.entry_price - exit_price) / TICK_SIZE

        dollar_pnl = ticks_pnl * DOLLAR_PER_TICK * pos.contracts
        hold_time = time.time() - pos.entry_time

        trade = {
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "contracts": pos.contracts,
            "stop_price": pos.stop_price,
            "target_price": pos.target_price,
            "pnl_ticks": round(ticks_pnl, 1),
            "pnl_dollars": round(dollar_pnl, 2),
            "result": "WIN" if dollar_pnl > 0 else "LOSS",
            "hold_time_s": round(hold_time, 1),
            "strategy": pos.strategy,
            "entry_reason": pos.reason,
            "exit_reason": exit_reason,
            "entry_time": pos.entry_time,
            "exit_time": time.time(),
            "market_snapshot": pos.market_snapshot,
        }

        self.trade_history.append(trade)
        self.position = None

        logger.info(f"[CLOSE] {trade['direction']} @ {exit_price} "
                     f"P&L=${trade['pnl_dollars']:.2f} ({trade['pnl_ticks']}t) "
                     f"reason={exit_reason} hold={trade['hold_time_s']:.0f}s")

        return trade

    def check_exits(self, current_price: float, max_hold_min: float = None) -> str | None:
        """
        Check if stop, target, or time stop is hit.
        Returns exit reason string, or None if no exit.
        """
        if self.position is None:
            return None

        pos = self.position

        # Stop loss
        if pos.direction == "LONG" and current_price <= pos.stop_price:
            return "stop_loss"
        if pos.direction == "SHORT" and current_price >= pos.stop_price:
            return "stop_loss"

        # Take profit
        if pos.direction == "LONG" and current_price >= pos.target_price:
            return "target_hit"
        if pos.direction == "SHORT" and current_price <= pos.target_price:
            return "target_hit"

        # Time stop
        if max_hold_min:
            hold_seconds = time.time() - pos.entry_time
            if hold_seconds >= max_hold_min * 60:
                return "time_stop"

        return None

    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L in dollars."""
        if self.position is None:
            return 0.0

        pos = self.position
        if pos.direction == "LONG":
            ticks = (current_price - pos.entry_price) / TICK_SIZE
        else:
            ticks = (pos.entry_price - current_price) / TICK_SIZE

        return ticks * DOLLAR_PER_TICK * pos.contracts

    def to_dict(self, current_price: float = 0.0) -> dict:
        """Serialize for dashboard."""
        if self.position is None:
            return {
                "status": "FLAT",
                "direction": None,
                "entry_price": None,
                "stop_price": None,
                "target_price": None,
                "contracts": 0,
                "strategy": None,
                "unrealized_pnl": 0.0,
                "hold_time_s": 0,
            }

        pos = self.position
        return {
            "status": "IN_TRADE",
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "stop_price": pos.stop_price,
            "target_price": pos.target_price,
            "contracts": pos.contracts,
            "strategy": pos.strategy,
            "reason": pos.reason,
            "unrealized_pnl": round(self.unrealized_pnl(current_price), 2),
            "hold_time_s": round(time.time() - pos.entry_time, 0),
        }

    def recent_trades(self, n: int = 20) -> list[dict]:
        """Return last N trades for dashboard trade log."""
        return self.trade_history[-n:]
