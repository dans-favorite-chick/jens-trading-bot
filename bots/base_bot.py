"""
Phoenix Bot — Base Bot

Shared logic for prod_bot and lab_bot:
- Connects to bridge on :8766
- Receives ticks, feeds to tick_aggregator
- Runs strategy pipeline on each new bar
- Manages position entry/exit via OIF
- Reports state to dashboard
"""

import asyncio
import json
import logging
import time

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import BOT_WS_PORT, TICK_SIZE, LIVE_TRADING
from config.strategies import STRATEGIES, STRATEGY_DEFAULTS
from core.tick_aggregator import TickAggregator
from core.risk_manager import RiskManager
from core.session_manager import SessionManager
from core.position_manager import PositionManager
from strategies.base_strategy import BaseStrategy, Signal

try:
    import websockets
except ImportError:
    print("ERROR: pip install websockets")
    sys.exit(1)

logger = logging.getLogger("Bot")


class BaseBot:
    """
    Base bot that connects to the bridge, processes ticks, and runs strategies.
    Subclasses (prod_bot, lab_bot) configure which strategies to load.
    """

    bot_name: str = "base"
    only_validated: bool = False  # Prod overrides to True

    def __init__(self):
        self.aggregator = TickAggregator()
        self.risk = RiskManager()
        self.session = SessionManager()
        self.positions = PositionManager()
        self.strategies: list[BaseStrategy] = []

        # State for dashboard
        self.status = "IDLE"
        self.last_signal: dict | None = None
        self.last_rejection: str | None = None

        # Runtime config (from dashboard sliders)
        self._runtime_params = dict(STRATEGY_DEFAULTS)

        # Register bar callback
        self.aggregator.on_bar(self._on_bar)

    def load_strategies(self):
        """Load strategy instances from config. Override in subclass if needed."""
        from strategies.bias_momentum import BiasMomentumFollow
        from strategies.spring_setup import SpringSetup

        strategy_classes = {
            "bias_momentum": BiasMomentumFollow,
            "spring_setup": SpringSetup,
        }

        for name, config in STRATEGIES.items():
            if name not in strategy_classes:
                continue
            if self.only_validated and not config.get("validated", False):
                continue
            if not config.get("enabled", True):
                continue

            strat = strategy_classes[name](config)
            self.strategies.append(strat)
            logger.info(f"Loaded strategy: {name} (validated={strat.validated})")

    # ─── Main Loop ──────────────────────────────────────────────────
    async def run(self):
        self.load_strategies()
        logger.info(f"{'=' * 50}")
        logger.info(f"  PHOENIX {self.bot_name.upper()} BOT")
        logger.info(f"  Strategies: {[s.name for s in self.strategies]}")
        logger.info(f"  Live trading: {LIVE_TRADING}")
        logger.info(f"{'=' * 50}")

        while True:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error(f"Connection error: {e}")
            logger.info("Reconnecting in 5s...")
            await asyncio.sleep(5)

    async def _connect_and_listen(self):
        uri = f"ws://127.0.0.1:{BOT_WS_PORT}"
        logger.info(f"Connecting to bridge at {uri}...")

        async with websockets.connect(
            uri,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            # Identify ourselves to the bridge
            await ws.send(json.dumps({
                "type": "identify",
                "name": self.bot_name,
            }))
            logger.info(f"Connected to bridge as '{self.bot_name}'")
            self.status = "SCANNING"

            async for message in ws:
                try:
                    tick = json.loads(message)
                except json.JSONDecodeError:
                    continue

                if tick.get("type") != "tick":
                    continue

                # Process tick through aggregator
                snapshot = self.aggregator.process_tick(tick)

                # Check position exits on every tick
                if not self.positions.is_flat:
                    price = snapshot.get("price", 0)
                    exit_reason = self.positions.check_exits(price)
                    if exit_reason:
                        await self._exit_trade(ws, price, exit_reason)

    # ─── Bar Event Handler ──────────────────────────────────────────
    def _on_bar(self, timeframe: str, bar):
        """Called by tick_aggregator when a bar completes."""
        # Only evaluate strategies on 5m bar completions
        if timeframe != "5m":
            return

        # Update session regime
        self.session.get_current_regime()

        # Run strategy pipeline (async-safe: store signal for main loop)
        self._evaluate_strategies()

    def _evaluate_strategies(self):
        """Run all enabled strategies, pick best signal."""
        if not self.positions.is_flat:
            return  # Already in a trade

        # Risk gate
        can_trade, reason = self.risk.can_trade()
        if not can_trade:
            self.last_rejection = reason
            return

        # Session check
        session_info = self.session.to_dict()

        # Get market state
        market = self.aggregator.snapshot()
        bars_5m = list(self.aggregator.bars_5m.completed)
        bars_1m = list(self.aggregator.bars_1m.completed)

        # Apply runtime confluence override
        min_conf = self._runtime_params.get("min_confluence", 3.5)
        regime_override = self.session.get_confluence_override()
        if regime_override:
            min_conf = max(min_conf, regime_override)

        best_signal = None
        for strat in self.strategies:
            if not strat.enabled:
                continue
            if not self.session.is_strategy_allowed(strat.name):
                continue

            try:
                signal = strat.evaluate(market, bars_5m, bars_1m, session_info)
                if signal and signal.confidence > (best_signal.confidence if best_signal else 0):
                    best_signal = signal
            except Exception as e:
                logger.error(f"Strategy {strat.name} error: {e}")

        if best_signal:
            self.last_signal = {
                "direction": best_signal.direction,
                "strategy": best_signal.strategy,
                "confidence": best_signal.confidence,
                "entry_score": best_signal.entry_score,
                "reason": best_signal.reason,
                "confluences": best_signal.confluences,
            }
            # Queue trade (will be executed in main async loop)
            self._pending_signal = best_signal
        else:
            self.last_signal = None
            self._pending_signal = None

    # ─── Trade Execution ────────────────────────────────────────────
    async def _enter_trade(self, ws, signal: Signal):
        """Execute entry via bridge → OIF."""
        market = self.aggregator.snapshot()
        price = market.get("price", 0)
        atr_5m = market.get("atr_5m", 0)

        # Risk sizing
        risk_dollars, tier = self.risk.get_risk_for_entry(signal.entry_score)
        if risk_dollars <= 0:
            self.last_rejection = f"Risk tier SKIP (score={signal.entry_score})"
            return

        # Adjust stop for volatility
        stop_ticks = self.risk.calculate_stop_ticks(signal.stop_ticks, atr_5m)
        contracts = self.risk.calculate_contracts(risk_dollars, stop_ticks)

        # Calculate prices
        tick_value = TICK_SIZE
        if signal.direction == "LONG":
            stop_price = price - (stop_ticks * tick_value)
            target_price = price + (stop_ticks * tick_value * signal.target_rr)
        else:
            stop_price = price + (stop_ticks * tick_value)
            target_price = price - (stop_ticks * tick_value * signal.target_rr)

        # Open position locally
        self.positions.open_position(
            direction=signal.direction,
            entry_price=price,
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            strategy=signal.strategy,
            reason=signal.reason,
            market_snapshot=market,
        )
        self.status = "IN_TRADE"

        # Send trade command to bridge
        action = "ENTER_LONG" if signal.direction == "LONG" else "ENTER_SHORT"
        await ws.send(json.dumps({
            "type": "trade",
            "action": action,
            "qty": contracts,
            "reason": signal.reason,
        }))

        logger.info(f"[ENTRY] {signal.direction} {contracts}x @ {price:.2f} "
                     f"SL={stop_price:.2f} TP={target_price:.2f} "
                     f"risk=${risk_dollars} tier={tier} strat={signal.strategy}")

    async def _exit_trade(self, ws, price: float, reason: str):
        """Execute exit via bridge → OIF."""
        trade = self.positions.close_position(price, reason)
        if trade:
            self.risk.record_trade(trade["pnl_dollars"])
            self.status = "SCANNING"

            await ws.send(json.dumps({
                "type": "trade",
                "action": "EXIT",
                "qty": trade["contracts"],
                "reason": reason,
            }))

            logger.info(f"[EXIT] P&L=${trade['pnl_dollars']:.2f} reason={reason}")

    # ─── Dashboard State ────────────────────────────────────────────
    def to_dict(self) -> dict:
        market = self.aggregator.snapshot()
        return {
            "bot_name": self.bot_name,
            "status": self.status,
            "live_trading": LIVE_TRADING,
            "position": self.positions.to_dict(market.get("price", 0)),
            "risk": self.risk.to_dict(),
            "session": self.session.to_dict(),
            "market": market,
            "last_signal": self.last_signal,
            "last_rejection": self.last_rejection,
            "strategies": [
                {"name": s.name, "enabled": s.enabled, "validated": s.validated, "params": s.params}
                for s in self.strategies
            ],
            "trades": self.positions.recent_trades(20),
        }

    # ─── Runtime Control ────────────────────────────────────────────
    def set_profile(self, profile_name: str):
        """Apply an aggression profile from config."""
        from config.strategies import STRATEGY_DEFAULTS
        profiles = STRATEGY_DEFAULTS.get("profiles", {})
        if profile_name in profiles:
            self._runtime_params.update(profiles[profile_name])
            logger.info(f"Profile set: {profile_name}")

    def toggle_strategy(self, strategy_name: str, enabled: bool):
        for s in self.strategies:
            if s.name == strategy_name:
                s.enabled = enabled
                logger.info(f"Strategy {strategy_name} {'enabled' if enabled else 'disabled'}")
                return True
        return False

    def update_runtime_params(self, updates: dict):
        self._runtime_params.update(updates)
        # Also update risk manager if relevant
        if "risk_per_trade" in updates:
            self.risk.set_risk_per_trade(updates["risk_per_trade"])
        if "max_daily_loss" in updates:
            self.risk.set_daily_limit(updates["max_daily_loss"])
