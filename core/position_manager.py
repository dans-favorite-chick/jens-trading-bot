"""
Phoenix Bot — Position Manager

Tracks open positions, unrealized P&L, and manages stop/target exits.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import TICK_SIZE, COMMISSION_PER_SIDE

logger = logging.getLogger("PositionManager")

# MNQ: each tick (0.25) = $0.50
DOLLAR_PER_TICK = TICK_SIZE * 2


@dataclass
class Position:
    trade_id: str   # Unique ID flowing through the whole pipeline
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    entry_time: float
    contracts: int
    stop_price: float
    target_price: float
    strategy: str
    reason: str
    market_snapshot: dict  # Snapshot of market data at entry

    # ── Phase 4C multi-account routing ─────────────────────────────
    # account is the NT8 sim account the entry was routed to; exit /
    # scale-out / BE-stop OIFs must use the same account.
    # sub_strategy is the opening_session sub-evaluator name (or None
    # for flat strategies) — used by resolver at exit time.
    account: str = "Sim101"
    sub_strategy: Optional[str] = None

    # ── Scale-out / Trend Rider state ───────────────────────────────
    original_contracts: int = 0    # Set at open (0 = not in rider mode)
    scaled_out: bool = False       # True once partial scale-out has been executed
    be_stop_active: bool = False   # True once stop moved to break-even
    rider_mode: bool = False       # True when holding remaining contract for trend

    # ── Managed-exit state (Noise Area, strategies with dynamic exits) ──
    exit_trigger: Optional[str] = None   # e.g. "price_returns_inside_noise_area"
                                         #      "chandelier_trail_3atr"
    eod_flat_time_et: Optional[str] = None
    metadata: dict = field(default_factory=dict)  # Strategy-specific (UB/LB for Noise Area)

    # ── Per-signal scale-out override (ORB=1.0R per Zarattini 2024) ─────
    scale_out_rr: Optional[float] = None

    # ── Chandelier trail config + live state ────────────────────────────
    trail_config: Optional[dict] = None           # {"atr_mult": 3.0, ...}
    trail_state: object = None                    # ChandelierTrailState instance


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

    def open_position(self, trade_id: str, direction: str, entry_price: float,
                      contracts: int, stop_price: float, target_price: float,
                      strategy: str, reason: str, market_snapshot: dict = None,
                      exit_trigger: str = None, eod_flat_time_et: str = None,
                      metadata: dict = None,
                      scale_out_rr: float = None, trail_config: dict = None,
                      account: str = "Sim101", sub_strategy: str | None = None):
        """Open a new position. Raises if already in a position."""
        if self.position is not None:
            logger.warning(f"[{trade_id}] Cannot open position — already in trade")
            return False

        # Lazy-instantiate the Chandelier trail if the Signal asked for one.
        trail_state = None
        if exit_trigger and exit_trigger.startswith("chandelier_trail") and trail_config:
            try:
                from core.chandelier_exit import ChandelierTrailState
                trail_state = ChandelierTrailState(
                    direction=direction.upper(),
                    entry_price=entry_price,
                    atr_mult=float(trail_config.get("atr_mult", 3.0)),
                )
            except Exception as e:
                logger.warning(f"[{trade_id}] Chandelier trail init failed (non-blocking): {e}")

        self.position = Position(
            trade_id=trade_id,
            direction=direction.upper(),
            entry_price=entry_price,
            entry_time=time.time(),
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            strategy=strategy,
            reason=reason,
            market_snapshot=market_snapshot or {},
            original_contracts=contracts,  # Capture for scale-out math
            exit_trigger=exit_trigger,
            eod_flat_time_et=eod_flat_time_et,
            metadata=metadata or {},
            scale_out_rr=scale_out_rr,
            trail_config=trail_config,
            trail_state=trail_state,
            account=account,
            sub_strategy=sub_strategy,
        )
        logger.info(f"[OPEN:{trade_id}] {direction} {contracts}x @ {entry_price} "
                     f"SL={stop_price} TP={target_price} strat={strategy} "
                     f"account={account}"
                     + (f"/{sub_strategy}" if sub_strategy else ""))
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

        gross_pnl = ticks_pnl * DOLLAR_PER_TICK * pos.contracts
        commission = COMMISSION_PER_SIDE * 2 * pos.contracts  # Round-trip: entry + exit
        dollar_pnl = gross_pnl - commission
        hold_time = time.time() - pos.entry_time

        trade = {
            "trade_id": pos.trade_id,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "contracts": pos.contracts,
            "stop_price": pos.stop_price,
            "target_price": pos.target_price,
            "pnl_ticks": round(ticks_pnl, 1),
            "pnl_dollars": round(dollar_pnl, 2),      # Net P&L (after commission)
            "gross_pnl": round(gross_pnl, 2),          # Gross P&L (before commission)
            "commission": round(commission, 2),         # Commission deducted
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

        logger.info(f"[CLOSE:{pos.trade_id}] {trade['direction']} @ {exit_price} "
                     f"P&L=${trade['pnl_dollars']:.2f} ({trade['pnl_ticks']}t) "
                     f"reason={exit_reason} hold={trade['hold_time_s']:.0f}s")

        return trade

    def scale_out_partial(self, exit_price: float, n_contracts: int,
                          exit_reason: str = "scale_out") -> dict | None:
        """
        Exit N contracts, keep remaining open. Records a partial trade.
        If n_contracts >= current contracts, delegates to close_position().

        Returns the partial trade record, or None if flat.
        """
        if self.position is None:
            return None

        pos = self.position

        # Delegate to full close if exiting everything
        if n_contracts >= pos.contracts:
            return self.close_position(exit_price, exit_reason)

        # Compute P&L for exited portion only
        if pos.direction == "LONG":
            ticks_pnl = (exit_price - pos.entry_price) / TICK_SIZE
        else:
            ticks_pnl = (pos.entry_price - exit_price) / TICK_SIZE

        gross_pnl = ticks_pnl * DOLLAR_PER_TICK * n_contracts
        commission = COMMISSION_PER_SIDE * 2 * n_contracts  # Round-trip for this portion
        dollar_pnl = gross_pnl - commission

        partial_trade = {
            "trade_id":      pos.trade_id + "_scale1",
            "direction":     pos.direction,
            "entry_price":   pos.entry_price,
            "exit_price":    exit_price,
            "contracts":     n_contracts,
            "pnl_ticks":     round(ticks_pnl, 1),
            "pnl_dollars":   round(dollar_pnl, 2),
            "gross_pnl":     round(gross_pnl, 2),
            "commission":    round(commission, 2),
            "result":        "WIN" if dollar_pnl > 0 else "LOSS",
            "hold_time_s":   round(time.time() - pos.entry_time, 1),
            "strategy":      pos.strategy,
            "entry_reason":  pos.reason,
            "exit_reason":   exit_reason,
            "entry_time":    pos.entry_time,
            "exit_time":     time.time(),
            "partial":       True,
            "market_snapshot": pos.market_snapshot,
        }

        # Reduce live position by exited contracts
        pos.contracts -= n_contracts
        pos.scaled_out = True

        self.trade_history.append(partial_trade)

        logger.info(f"[SCALE_OUT:{pos.trade_id}] Exited {n_contracts}x @ {exit_price:.2f} "
                    f"P&L=${dollar_pnl:.2f} ({ticks_pnl:.1f}t) | "
                    f"{pos.contracts}x still open")
        return partial_trade

    def move_stop_to_be(self, be_price: Optional[float] = None):
        """
        Move the stop price to break-even (entry price by default).
        Only moves stop in the favorable direction — never worsens risk.
        """
        if self.position is None:
            return
        pos = self.position
        if be_price is None:
            be_price = pos.entry_price

        old_stop = pos.stop_price
        # Safety: only move stop if it improves our position
        if pos.direction == "LONG" and be_price <= old_stop:
            return   # Would move stop further from entry — wrong direction
        if pos.direction == "SHORT" and be_price >= old_stop:
            return

        pos.stop_price = be_price
        pos.be_stop_active = True
        logger.info(f"[BE_STOP:{pos.trade_id}] Stop {old_stop:.2f} -> {be_price:.2f} (BE locked)")

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
            "original_contracts": pos.original_contracts,
            "strategy": pos.strategy,
            "reason": pos.reason,
            "unrealized_pnl": round(self.unrealized_pnl(current_price), 2),
            "hold_time_s": round(time.time() - pos.entry_time, 0),
            "scaled_out": pos.scaled_out,
            "be_stop_active": pos.be_stop_active,
            "rider_mode": pos.rider_mode,
        }

    def recent_trades(self, n: int = 20) -> list[dict]:
        """Return last N trades for dashboard trade log."""
        return self.trade_history[-n:]
