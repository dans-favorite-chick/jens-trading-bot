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
import urllib.request
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env for API keys (GEMINI_API_KEY)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from config.settings import (BOT_WS_PORT, TICK_SIZE, LIVE_TRADING, DASHBOARD_PORT,
                             AGENT_COUNCIL_ENABLED, AGENT_PRETRADE_FILTER_ENABLED,
                             AGENT_DEBRIEF_ENABLED)
from config.strategies import STRATEGIES, STRATEGY_DEFAULTS
from core.tick_aggregator import TickAggregator
from core.risk_manager import RiskManager
from core.session_manager import SessionManager
from core.position_manager import PositionManager
from core.trade_memory import TradeMemory
from core.history_logger import HistoryLogger
from core.strategy_tracker import StrategyTracker
from core import telegram_notifier as tg
from core.cockpit import Cockpit
from core.equity_tracker import EquityTracker
from core.trade_clustering import TradeClustering
from core.telegram_commands import TelegramCommands
from core.position_scaler import PositionScaler
from core.expectancy_engine import ExpectancyEngine
from core.no_trade_fingerprint import NoTradeFingerprint
from core.regime_transitions import RegimeTransitionDetector
from core.microstructure_filter import MicrostructureFilter
from core.crowding_detector import CrowdingDetector
from core.counter_edge import CounterEdgeEngine
from core.execution_quality import ExecutionQuality
from strategies.base_strategy import BaseStrategy, Signal

# Phase 4: AI Agents (optional — failures never block trading)
try:
    from agents import council_gate, pretrade_filter, session_debriefer
    from agents.council_gate import council_to_dict
    AGENTS_AVAILABLE = True
except ImportError as e:
    AGENTS_AVAILABLE = False

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
        self.session = SessionManager(bot_name=self.bot_name)
        self.positions = PositionManager()
        self.trade_memory = TradeMemory()
        self.history = HistoryLogger(bot_name=self.bot_name)
        self.tracker = StrategyTracker()
        self.strategies: list[BaseStrategy] = []

        # Phase 5: Cockpit, Equity, Clustering, Telegram Commands, Scaler
        self.cockpit = Cockpit()
        self.equity_tracker = EquityTracker()
        self.trade_clustering = TradeClustering()
        self.telegram_commands = TelegramCommands()
        self.position_scaler = PositionScaler(base_account=1000.0)

        # Phase 6: Deep learning modules
        self.expectancy = ExpectancyEngine()
        self.no_trade_fp = NoTradeFingerprint()
        self.regime_transitions = RegimeTransitionDetector()

        # Phase 6 secondary: Competitive edge arsenal
        self.microstructure_filter = MicrostructureFilter()
        self.crowding_detector = CrowdingDetector()
        self.counter_edge = CounterEdgeEngine()
        self.execution_quality = ExecutionQuality()

        # State for dashboard
        self.status = "IDLE"
        self.last_signal: dict | None = None
        self.last_rejection: str | None = None
        self._last_eval: dict = {}

        # Runtime config (from dashboard sliders)
        self._runtime_params = dict(STRATEGY_DEFAULTS)

        # Phase 4: AI Agent state
        self._council_result = None         # Latest CouncilResult dict
        self._council_ran_today = False     # Only run once per session day
        self._last_regime = None            # Track regime transitions
        self._filter_verdict = None         # Latest pre-trade filter verdict
        self._debrief_ran_today = False     # Only run once per session day
        self._pending_exit_reason = None    # Market close auto-exit
        self._current_date = None           # For daily reset detection
        self._last_bridge_ack_ok = True     # Bridge OIF write confirmation

        # Phase 5: Additional state
        self._cockpit_result = None         # Latest cockpit grading
        self._latest_intel = None           # Latest market intel (for TG commands)
        self._clustering_result = None      # Latest clustering analysis
        self._trades_since_cluster = 0      # Counter for clustering trigger
        self._equity_recorded_today = False # Only record equity once per day

        # Register bar callback
        self.aggregator.on_bar(self._on_bar)

    def load_strategies(self):
        """Load strategy instances from config. Override in subclass if needed."""
        from strategies.bias_momentum import BiasMomentumFollow
        from strategies.spring_setup import SpringSetup
        from strategies.vwap_pullback import VWAPPullback
        from strategies.high_precision import HighPrecisionOnly
        from strategies.ib_breakout import IBBreakout

        strategy_classes = {
            "bias_momentum": BiasMomentumFollow,
            "spring_setup": SpringSetup,
            "vwap_pullback": VWAPPullback,
            "high_precision_only": HighPrecisionOnly,
            "ib_breakout": IBBreakout,
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

        # Start dashboard state pusher in background
        asyncio.ensure_future(self._dashboard_loop())

        # Start news/momentum scanner in background (Phase 4+)
        asyncio.ensure_future(self._news_scanner_loop())

        # Phase 5: Start Telegram command listener
        asyncio.ensure_future(self.telegram_commands.poll_commands(self))

        while True:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error(f"Connection error: {e}")
            logger.info("Reconnecting in 5s...")
            await asyncio.sleep(5)

    # ─── Dashboard State Pusher ─────────────────────────────────────
    async def _dashboard_loop(self):
        """Push bot state to dashboard every 2s and poll for commands."""
        url_state = f"http://127.0.0.1:{DASHBOARD_PORT}/api/bot-state"
        url_cmds = f"http://127.0.0.1:{DASHBOARD_PORT}/api/commands?bot={self.bot_name}"

        while True:
            try:
                # Push state
                state_json = json.dumps(self.to_dict()).encode("utf-8")
                req = urllib.request.Request(
                    url_state,
                    data=state_json,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=2)

                # Poll for commands from dashboard
                try:
                    cmd_resp = urllib.request.urlopen(url_cmds, timeout=2)
                    cmds = json.loads(cmd_resp.read().decode())
                    for cmd in cmds:
                        self._handle_dashboard_command(cmd)
                except Exception:
                    pass

            except Exception as e:
                logger.debug(f"Dashboard push failed: {e}")

            await asyncio.sleep(2)

    def _handle_dashboard_command(self, cmd: dict):
        """Process a command from the dashboard."""
        cmd_type = cmd.get("type", "")
        if cmd_type == "set_profile":
            self.set_profile(cmd.get("profile", "balanced"))
        elif cmd_type == "toggle_strategy":
            self.toggle_strategy(cmd.get("name", ""), cmd.get("enabled", True))
        elif cmd_type == "update_params":
            self.update_runtime_params(cmd.get("params", {}))
        elif cmd_type == "test_trade":
            logger.info(f"[TEST TRADE] {cmd.get('action', 'ENTER_LONG')}")
            # TODO: fire test trade

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

                msg_type = tick.get("type")

                if msg_type == "dom":
                    self.aggregator.process_dom(tick)
                    continue

                if msg_type == "trade_ack":
                    # Bridge confirms it wrote OIF files (or didn't)
                    ack_files = tick.get("files", [])
                    ack_action = tick.get("action", "")
                    if not ack_files and ack_action not in ("CANCEL_ALL", "CANCELALLORDERS"):
                        logger.error(f"[BRIDGE ACK] Bridge wrote 0 OIF files for {ack_action}! "
                                      f"NT8 will NOT see this order.")
                        self._last_bridge_ack_ok = False
                    else:
                        self._last_bridge_ack_ok = True
                    continue

                if msg_type != "tick":
                    continue

                # Process tick through aggregator
                snapshot = self.aggregator.process_tick(tick)

                # Phase 6b: Feed microstructure filter on every tick
                self.microstructure_filter.update_tick(snapshot.get("price", 0))

                # Check position exits on every tick
                if not self.positions.is_flat:
                    price = snapshot.get("price", 0)
                    # Phase 6: Track MAE/MFE on every tick
                    self.expectancy.update_tick(price)
                    # Market close auto-exit
                    if hasattr(self, '_pending_exit_reason') and self._pending_exit_reason:
                        reason = self._pending_exit_reason
                        self._pending_exit_reason = None
                        await self._exit_trade(ws, price, reason)
                        continue
                    # Get max hold time from the active strategy's config
                    _max_hold = None
                    if self.positions.position:
                        _strat_name = self.positions.position.strategy
                        for _s in self.strategies:
                            if _s.name == _strat_name:
                                _max_hold = _s.config.get("max_hold_min")
                                break
                    exit_reason = self.positions.check_exits(price, max_hold_min=_max_hold)
                    if exit_reason:
                        await self._exit_trade(ws, price, exit_reason)

                # Execute pending signals from strategy evaluation
                if hasattr(self, '_pending_signal') and self._pending_signal and self.positions.is_flat:
                    signal = self._pending_signal
                    self._pending_signal = None

                    # Phase 4: Pre-trade filter (3s timeout, defaults to CLEAR)
                    if AGENTS_AVAILABLE and AGENT_PRETRADE_FILTER_ENABLED:
                        try:
                            market_snap = self.aggregator.snapshot()
                            regime = self.session.get_current_regime()
                            recent = self.trade_memory.recent(5)

                            # News awareness — inform AI filter but NEVER block trades
                            # News = opportunity, not restriction
                            try:
                                from core.market_intel import get_economic_calendar
                                cal = await get_economic_calendar()
                                if cal.get("trade_restricted"):
                                    event_name = cal.get('next_event', {}).get('name', 'event')
                                    logger.info(f"[NEWS SIGNAL] High-impact event: {event_name} "
                                                 f"— AI filter will factor this in")
                                    # Add to market context for AI filter (signal, not gate)
                                    market_snap["news_event_imminent"] = event_name
                                    asyncio.ensure_future(tg.notify_alert(
                                        "NEWS EVENT", f"{event_name} — trade with awareness"))
                            except Exception:
                                pass
                            verdict = await pretrade_filter.check(
                                signal=signal.to_dict() if hasattr(signal, 'to_dict') else {
                                    "direction": signal.direction,
                                    "strategy": signal.strategy,
                                    "reason": signal.reason,
                                    "confluences": signal.confluences,
                                    "confidence": signal.confidence,
                                    "entry_score": signal.entry_score,
                                    "stop_ticks": signal.stop_ticks,
                                    "target_rr": signal.target_rr,
                                },
                                market=market_snap,
                                recent_trades=recent,
                                regime=regime,
                            )
                            self._filter_verdict = {
                                "action": verdict.action,
                                "reason": verdict.reason,
                                "confidence": verdict.confidence,
                                "latency_ms": verdict.latency_ms,
                                "source": verdict.source,
                                "timestamp": datetime.now().isoformat(),
                            }
                            logger.info(f"[FILTER] {verdict.action} ({verdict.confidence:.0f}%) "
                                        f"in {verdict.latency_ms:.0f}ms: {verdict.reason}")

                            if verdict.action == "SIT_OUT":
                                logger.info(f"[FILTER] SIT_OUT — skipping trade")
                                self.last_rejection = f"AI filter: {verdict.reason}"
                                continue
                            # CAUTION handled in _enter_trade via self._filter_verdict
                        except Exception as e:
                            logger.warning(f"[FILTER] Error (defaulting to CLEAR): {e}")
                            self._filter_verdict = {"action": "CLEAR", "reason": f"Error: {e}", "source": "default"}

                    await self._enter_trade(ws, signal)

    # ─── Bar Event Handler ──────────────────────────────────────────
    def _on_bar(self, timeframe: str, bar):
        """Called by tick_aggregator when a bar completes."""
        # Evaluate on 1m AND 5m bar completions
        if timeframe not in ("1m", "5m"):
            return

        # Daily reset detection — reset all daily state at midnight
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_date and today != self._current_date:
            logger.info(f"[DAILY RESET] New day: {today}")
            self.risk.reset_daily()
            self._council_ran_today = False
            self._debrief_ran_today = False
        self._current_date = today

        # Update session regime
        regime = self.session.get_current_regime()

        # Log bar completion
        logger.info(f"[BAR {timeframe}] close={bar.close:.2f} vol={bar.volume} "
                     f"regime={regime} bars_1m={self.aggregator.bars_1m.bar_count} "
                     f"bars_5m={self.aggregator.bars_5m.bar_count}")

        # Persist bar to history
        market = self.aggregator.snapshot()
        self.history.log_bar(timeframe, bar, market, regime)

        # Phase 4: Council — run on OPEN_MOMENTUM start (once per day)
        if (AGENTS_AVAILABLE and AGENT_COUNCIL_ENABLED
                and regime == "OPEN_MOMENTUM"
                and self._last_regime != "OPEN_MOMENTUM"
                and not self._council_ran_today):
            self._council_ran_today = True
            asyncio.ensure_future(self._run_council(market))

        # Phase 4: Debrief — run when transitioning to AFTERHOURS (once per day)
        if (AGENTS_AVAILABLE and AGENT_DEBRIEF_ENABLED
                and regime == "AFTERHOURS"
                and self._last_regime != "AFTERHOURS"
                and not self._debrief_ran_today):
            self._debrief_ran_today = True
            asyncio.ensure_future(self._run_debrief())

        # Phase 5: Record daily equity at AFTERHOURS transition
        if (regime == "AFTERHOURS"
                and self._last_regime != "AFTERHOURS"
                and not self._equity_recorded_today):
            self._equity_recorded_today = True
            try:
                risk = self.risk.state
                tracker_data = self.tracker.get_all_summaries() if hasattr(self.tracker, 'get_all_summaries') else {}
                self.equity_tracker.record_day(
                    date_str=datetime.now().strftime("%Y-%m-%d"),
                    daily_pnl=risk.daily_pnl,
                    trades=risk.trades_today,
                    wins=risk.wins_today,
                    losses=risk.losses_today,
                    strategy_breakdown=tracker_data,
                )
            except Exception as e:
                logger.debug(f"[EQUITY] Record error (non-blocking): {e}")

        # Market close auto-exit: close open positions before session end
        # CLOSE_CHOP starts at 15:00, give 1 min warning before 16:15 close
        if (regime == "CLOSE_CHOP" and not self.positions.is_flat):
            from datetime import time as dtime
            now_time = datetime.now().time()
            # Auto-exit at 16:10 CST (5 min before close)
            if now_time >= dtime(16, 10):
                logger.warning(f"[MARKET CLOSE] Auto-exiting position before session end")
                self._pending_exit_reason = "market_close_auto"

        # Phase 6: Check regime transitions
        transition = self.regime_transitions.check_transition(regime)
        if transition:
            logger.info(f"[REGIME SHIFT] {transition['from']} -> {transition['to']} "
                         f"high_value={transition.get('is_high_value', False)}")

        self._last_regime = regime

        # Run strategy pipeline (async-safe: store signal for main loop)
        self._evaluate_strategies()

    def _evaluate_strategies(self):
        """Run all enabled strategies, pick best signal."""
        if not self.positions.is_flat:
            return  # Already in a trade

        # Enforce prod trading window — prod bot only trades during defined session
        if self.bot_name == "prod" and not self.session.is_prod_trading_window():
            return  # Prod bot: outside trading window, skip evaluation

        # Apply runtime profile overrides to strategy configs
        # (Safe/Balanced/Aggressive buttons on dashboard)
        profile_keys = ("min_confluence", "min_momentum", "min_momentum_confidence",
                        "min_precision", "risk_per_trade", "max_daily_loss")
        for strat in self.strategies:
            for key in profile_keys:
                if key in self._runtime_params:
                    strat.config[key] = self._runtime_params[key]

        # Session check
        session_info = self.session.to_dict()

        # Start building eval record for dashboard
        self._last_eval = {
            "ts": datetime.now().isoformat(),
            "regime": session_info.get("regime", "?"),
            "risk_blocked": None,
            "strategies": [],
            "best_signal": None,
        }

        # Minimum bars guard — 3 min max warmup after connect/reconnect
        # Only require 3 x 1-min bars (3 min). No 5-min bar requirement —
        # we don't want to sit out 5+ min and miss the golden window
        bars_5m = list(self.aggregator.bars_5m.completed)
        bars_1m = list(self.aggregator.bars_1m.completed)
        if len(bars_1m) < 3:
            reason = f"Warming up ({len(bars_1m)} 1m bars — need 3, ~3 min)"
            self.last_rejection = reason
            self._last_eval["risk_blocked"] = reason
            logger.info(f"[WARMUP] {reason}")
            return

        # Get market state FIRST (needed by risk gate and everything below)
        market = self.aggregator.snapshot()

        # Risk gate (pass ATR as volatility proxy since VIX requires external feed)
        atr_5m = market.get("atr_5m", 0)
        vix_proxy = min(50, atr_5m / 4) if atr_5m > 0 else 0
        can_trade, reason = self.risk.can_trade(vix=vix_proxy)
        if not can_trade:
            self.last_rejection = reason
            self._last_eval["risk_blocked"] = reason
            logger.debug(f"[RISK GATE] Blocked: {reason}")
            return

        logger.info(f"[EVAL] price={market.get('price',0):.2f} "
                     f"vwap={market.get('vwap',0):.2f} ema9={market.get('ema9',0):.2f} "
                     f"cvd={market.get('cvd',0):.0f} "
                     f"tf_bull={market.get('tf_votes_bullish',0)} tf_bear={market.get('tf_votes_bearish',0)} "
                     f"bars_1m={len(bars_1m)} bars_5m={len(bars_5m)} "
                     f"regime={session_info.get('regime','?')}")

        # Phase 5: Cockpit 12-layer grading (observation only -- never blocks)
        try:
            intel = self._latest_intel or {}
            self._cockpit_result = self.cockpit.grade(
                market=market,
                session_info=session_info,
                intel=intel,
                council_result=self._council_result,
            )
            self._last_eval["cockpit"] = self._cockpit_result.get("score", "?")
        except Exception as e:
            logger.debug(f"[COCKPIT] Grading error (non-blocking): {e}")

        # Phase 6b: Update crowding detector with current levels
        try:
            bars_1m_objs = list(self.aggregator.bars_1m.completed)
            self.crowding_detector.update_levels(market, bars_1m_objs)
        except Exception as e:
            logger.debug(f"[CROWDING] Update error (non-blocking): {e}")

        best_signal = None
        for strat in self.strategies:
            if not strat.enabled:
                logger.debug(f"  [{strat.name}] SKIP — disabled")
                self._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_DISABLED"})
                continue
            if not self.session.is_strategy_allowed(strat.name):
                logger.debug(f"  [{strat.name}] SKIP — not allowed in {session_info.get('regime')}")
                self._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_REGIME"})
                continue

            try:
                signal = strat.evaluate(market, bars_5m, bars_1m, session_info)
                if signal:
                    # Strategies are now regime-aware internally (bias_momentum uses
                    # _REGIME_OVERRIDES to loosen/tighten per regime). No external
                    # confluence override needed — that was comparing wrong dimensions.
                    logger.info(f"  [{strat.name}] SIGNAL: {signal.direction} conf={signal.confidence:.0f} "
                                 f"score={signal.entry_score:.0f} — {signal.reason}")
                    self._last_eval["strategies"].append({
                        "name": strat.name,
                        "result": "SIGNAL",
                        "direction": signal.direction,
                        "confidence": signal.confidence,
                        "reason": signal.reason,
                        "confluences": signal.confluences,
                    })
                    if signal.confidence > (best_signal.confidence if best_signal else 0):
                        best_signal = signal
                else:
                    logger.info(f"  [{strat.name}] no signal")
                    self._last_eval["strategies"].append({"name": strat.name, "result": "NO_SIGNAL"})
            except Exception as e:
                logger.error(f"  [{strat.name}] ERROR: {e}")
                self._last_eval["strategies"].append({"name": strat.name, "result": "ERROR", "reason": str(e)})

        if best_signal:
            # Phase 6: Apply regime transition bonus to best signal's confidence
            regime = session_info.get("regime", "UNKNOWN")
            transition_bonus = self.regime_transitions.get_transition_bonus(regime)
            if transition_bonus.get("active"):
                bonus = transition_bonus["bonus_score"]
                best_signal.confidence = min(100, best_signal.confidence + bonus)
                logger.info(f"[TRANSITION BONUS] +{bonus} confidence -> {best_signal.confidence:.0f} "
                             f"({transition_bonus['description']})")
                self._last_eval["transition_bonus"] = transition_bonus

            # Phase 6: No-trade fingerprint risk check (advisory only)
            fp_result = self.no_trade_fp.get_risk_score(
                market=market,
                session_info=session_info,
                signal=best_signal,
                trade_count_today=self.risk.state.trades_today,
            )
            self._last_eval["fingerprint"] = fp_result
            if fp_result["risk_score"] > 0:
                logger.info(f"[FINGERPRINT] Risk={fp_result['risk_score']} "
                             f"({fp_result['recommendation']}) "
                             f"matches={len(fp_result['matching_fingerprints'])}")

            # Phase 6b: Crowding score (observation only)
            try:
                crowding = self.crowding_detector.get_crowding_score(
                    entry_price=market.get("price", 0),
                    direction=best_signal.direction,
                    market=market,
                )
                self._last_eval["crowding"] = crowding
            except Exception as e:
                logger.debug(f"[CROWDING] Score error (non-blocking): {e}")

            # Phase 6b: Counter-edge check (observation only)
            try:
                counter = self.counter_edge.check_counter_signal(
                    strategy=best_signal.strategy,
                    direction=best_signal.direction,
                    regime=session_info.get("regime", "UNKNOWN"),
                    market=market,
                )
                if counter:
                    self._last_eval["counter_edge"] = counter
                    logger.info(f"[COUNTER] Counter-edge detected: {counter['description']}")
            except Exception as e:
                logger.debug(f"[COUNTER] Check error (non-blocking): {e}")

            logger.info(f"[TRADE QUEUED:{best_signal.trade_id}] {best_signal.direction} "
                         f"via {best_signal.strategy} conf={best_signal.confidence:.0f}")
            # Track signal as GENERATED (not taken yet — fill may fail or filter may block)
            self.tracker.record_signal(
                strategy=best_signal.strategy,
                direction=best_signal.direction,
                confidence=best_signal.confidence,
                taken=False,  # Will be updated to True only after confirmed fill
                regime=session_info.get("regime", "UNKNOWN"),
                trade_id=best_signal.trade_id,
            )
            self._last_eval["best_signal"] = {
                "direction": best_signal.direction,
                "strategy": best_signal.strategy,
                "confidence": best_signal.confidence,
                "reason": best_signal.reason,
            }
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

        # Persist full eval to history (every bar evaluation, signal or not)
        self.history.log_eval(self._last_eval, market)

    # ─── Trade Execution ────────────────────────────────────────────
    async def _enter_trade(self, ws, signal: Signal):
        """Execute entry via bridge → OIF with fill confirmation."""
        market = self.aggregator.snapshot()
        price = market.get("price", 0)
        atr_5m = market.get("atr_5m", 0)
        tid = signal.trade_id

        # Risk sizing (use ATR-based VIX proxy for volatility adjustment)
        vix_proxy = min(50, atr_5m / 4) if atr_5m > 0 else 0
        risk_dollars, tier = self.risk.get_risk_for_entry(signal.entry_score, vix=vix_proxy)
        if risk_dollars <= 0:
            self.last_rejection = f"Risk tier SKIP (score={signal.entry_score})"
            return

        # Apply session regime size_multiplier
        size_mult = self.session.get_size_multiplier()
        if size_mult < 1.0:
            risk_dollars *= size_mult
            logger.info(f"[{tid}] Regime size_mult={size_mult:.1f}x → risk=${risk_dollars:.2f}")

        # Phase 6: Apply regime transition size boost
        transition_bonus = self.regime_transitions.get_transition_bonus(regime)
        if transition_bonus.get("active") and transition_bonus["size_boost"] != 1.0:
            old_risk = risk_dollars
            risk_dollars *= transition_bonus["size_boost"]
            logger.info(f"[{tid}:TRANSITION] size_boost={transition_bonus['size_boost']:.1f}x "
                         f"→ risk ${old_risk:.2f} → ${risk_dollars:.2f} "
                         f"({transition_bonus['description']})")

        # Phase 4: CAUTION verdict = 50% size reduction
        if (self._filter_verdict and self._filter_verdict.get("action") == "CAUTION"):
            risk_dollars *= 0.5
            logger.info(f"[{tid}:FILTER] CAUTION — risk reduced to ${risk_dollars:.2f}")

        # Adjust stop for volatility
        stop_ticks = self.risk.calculate_stop_ticks(signal.stop_ticks, atr_5m)
        contracts = self.risk.calculate_contracts(risk_dollars, stop_ticks)

        # Phase 5: Position scaler — cap contracts by account equity and conditions
        regime = self.session.get_current_regime()
        max_contracts = self.position_scaler.get_max_contracts(
            account_equity=self.risk._risk_per_trade * 50,  # Approximate equity from risk setting
            entry_score=signal.entry_score,
            regime=regime,
        )
        contracts = min(contracts, max_contracts)

        # Reject 0-contract entries — never send to bridge
        if contracts < 1:
            logger.warning(f"[{tid}] Computed 0 contracts (risk=${risk_dollars:.2f}, "
                            f"stop={stop_ticks}t) — skipping entry")
            self.last_rejection = f"0 contracts computed (risk too low for stop distance)"
            return

        # Calculate prices
        tick_value = TICK_SIZE
        if signal.direction == "LONG":
            stop_price = price - (stop_ticks * tick_value)
            target_price = price + (stop_ticks * tick_value * signal.target_rr)
        else:
            stop_price = price + (stop_ticks * tick_value)
            target_price = price - (stop_ticks * tick_value * signal.target_rr)

        # Phase 6b: Microstructure filter check (advisory only -- does NOT block)
        try:
            micro_result = self.microstructure_filter.check(market, signal.direction)
            logger.info(f"[{tid}:MICRO] score={micro_result['score']} "
                         f"rec={micro_result['recommendation']} "
                         f"issues={micro_result['issues']}")
        except Exception as e:
            micro_result = {"score": 0, "recommendation": "N/A", "issues": [str(e)]}
            logger.debug(f"[{tid}:MICRO] Error (non-blocking): {e}")

        # Log INTENT before execution
        logger.info(f"[INTENT:{tid}] {signal.direction} {contracts}x @ {price:.2f} "
                     f"SL={stop_price:.2f} TP={target_price:.2f} "
                     f"risk=${risk_dollars} tier={tier} strat={signal.strategy}")

        # Send trade command to bridge (bridge writes OIF with OCO brackets)
        action = "ENTER_LONG" if signal.direction == "LONG" else "ENTER_SHORT"
        try:
            await ws.send(json.dumps({
                "type": "trade",
                "trade_id": tid,
                "action": action,
                "qty": contracts,
                "stop_price": round(stop_price, 2),
                "target_price": round(target_price, 2),
                "reason": signal.reason,
            }))
        except Exception as e:
            logger.error(f"[{tid}] Failed to send trade command: {e}")
            self.last_rejection = f"Bridge send failed: {e}"
            return

        # Wait for fill confirmation (5s timeout, defaults to FILLED for sim)
        from bridge.oif_writer import wait_for_fill
        fill_result = await wait_for_fill(tid, timeout_s=5.0)

        if fill_result["status"] == "REJECTED":
            logger.error(f"[{tid}] ORDER REJECTED by NT8: {fill_result['content']}")
            self.last_rejection = f"Order rejected: {fill_result['content']}"
            return

        if fill_result["status"] == "TIMEOUT":
            if not LIVE_TRADING:
                # Sim mode: assume filled (NT8 sim doesn't always write fill files)
                logger.info(f"[{tid}] No fill file (sim mode) — assuming filled")
            else:
                # LIVE mode: DO NOT proceed without fill confirmation
                logger.error(f"[{tid}] Fill timeout in LIVE mode — ABORTING entry. "
                              f"Check NT8 manually for order status.")
                self.last_rejection = f"Fill timeout in LIVE mode — entry aborted"
                return

        # Inject regime and Phase 6b data into market snapshot for analytics
        market["regime"] = self.session.get_current_regime()
        market["signal_price"] = price  # Price at signal generation time
        market["microstructure"] = micro_result
        market["fill_latency_ms"] = fill_result.get("latency_ms", 0)

        # NOW open position locally (after fill confirmation)
        self.positions.open_position(
            trade_id=tid,
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

        # Phase 6: Start expectancy tracking
        self.expectancy.start_tracking(
            trade_id=tid,
            direction=signal.direction,
            entry_price=price,
            signal_price=market.get("price", price),  # Price at signal time
            stop_price=stop_price,
            target_price=target_price,
            strategy=signal.strategy,
            regime=self.session.get_current_regime(),
        )

        # Phase 6: Consume transition bonus if active
        self.regime_transitions.mark_signal_used()

        logger.info(f"[FILLED:{tid}] {signal.direction} {contracts}x @ {price:.2f} "
                     f"fill_latency={fill_result.get('latency_ms', 0):.0f}ms")

        # NOW mark signal as actually taken (after confirmed fill)
        self.tracker.record_signal(
            strategy=signal.strategy, direction=signal.direction,
            confidence=signal.confidence, taken=True,
            regime=self.session.get_current_regime(), trade_id=tid,
        )

        self.history.log_entry(signal, price, contracts, stop_price,
                               target_price, risk_dollars, tier, market)

        # Telegram notification
        asyncio.ensure_future(tg.notify_entry(
            trade_id=tid, direction=signal.direction, strategy=signal.strategy,
            price=price, stop=stop_price, target=target_price,
            contracts=contracts, risk_dollars=risk_dollars, tier=tier,
            regime=self.session.get_current_regime(),
        ))

    async def _exit_trade(self, ws, price: float, reason: str):
        """Execute exit: send to NT8 FIRST, then close Python state."""
        if self.positions.is_flat:
            return

        pos = self.positions.position
        tid = pos.trade_id
        self.status = "EXIT_PENDING"
        logger.info(f"[EXIT_PENDING:{tid}] Sending exit for {pos.direction} @ {price:.2f}, reason={reason}")

        # STEP 1: Send CANCEL_ALL + EXIT to NT8 BEFORE touching Python state
        exit_sent = False
        try:
            await ws.send(json.dumps({
                "type": "trade", "trade_id": tid,
                "action": "CANCEL_ALL", "qty": 0,
                "reason": "cancel_oco_before_exit",
            }))
        except Exception:
            pass  # Best effort on bracket cancel

        try:
            await ws.send(json.dumps({
                "type": "trade", "trade_id": tid,
                "action": "EXIT", "qty": pos.contracts,
                "reason": reason,
            }))
            exit_sent = True
        except Exception as e:
            logger.error(f"[EXIT:{tid}] WS send failed: {e} — writing OIF fallback")
            try:
                from bridge.oif_writer import write_oif
                write_oif("EXIT", pos.contracts, trade_id=tid)
                exit_sent = True
            except Exception as e2:
                logger.error(f"[EXIT:{tid}] OIF fallback ALSO failed: {e2} — MANUAL EXIT NEEDED")
                asyncio.ensure_future(tg.notify_alert(
                    "CRITICAL: EXIT FAILED",
                    f"Trade {tid} exit failed. Position may still be open in NT8.\n"
                    f"MANUAL EXIT REQUIRED."))

        # STEP 2: NOW close Python position (after NT8 command sent)
        trade = self.positions.close_position(price, reason)
        if trade:
            self.risk.record_trade(trade["pnl_dollars"])
            self.trade_memory.record(trade)
            self.tracker.record_trade(trade)
            self.history.log_exit(trade, self.aggregator.snapshot())

            # Phase 6: Close expectancy tracking
            exp_analysis = self.expectancy.close_trade(
                exit_price=price,
                pnl_ticks=trade["pnl_ticks"],
                result=trade["result"],
            )

            # Phase 6: Learn fingerprint from losses
            if trade["result"] == "LOSS":
                self.no_trade_fp.learn_from_trade(trade, self.aggregator.snapshot())

            # Phase 6b: Counter-edge learning from losses
            if trade["result"] == "LOSS":
                try:
                    self.counter_edge.learn_from_loss(trade)
                except Exception as e:
                    logger.debug(f"[COUNTER] Learn error (non-blocking): {e}")

            # Phase 6b: Execution quality tracking
            try:
                snapshot = trade.get("market_snapshot", {})
                self.execution_quality.record(
                    trade_id=trade.get("trade_id", ""),
                    signal_price=snapshot.get("signal_price", trade["entry_price"]),
                    entry_price=trade["entry_price"],
                    exit_price=trade["exit_price"],
                    pnl_ticks=trade["pnl_ticks"],
                    fill_latency_ms=snapshot.get("fill_latency_ms", 0),
                    strategy=trade["strategy"],
                    regime=snapshot.get("regime", "UNKNOWN"),
                )
            except Exception as e:
                logger.debug(f"[EXEC_Q] Record error (non-blocking): {e}")

            asyncio.ensure_future(tg.notify_exit(
                trade_id=trade.get("trade_id", ""),
                direction=trade["direction"], strategy=trade["strategy"],
                entry_price=trade["entry_price"], exit_price=trade["exit_price"],
                pnl_dollars=trade["pnl_dollars"], pnl_ticks=trade["pnl_ticks"],
                result=trade["result"], exit_reason=trade["exit_reason"],
                hold_time_s=trade["hold_time_s"],
            ))

            # Clustering every 10 trades
            self._trades_since_cluster += 1
            if self._trades_since_cluster >= 10:
                self._trades_since_cluster = 0
                try:
                    self._clustering_result = self.trade_clustering.analyze(
                        self.trade_memory.recent(200))
                    for rec in (self._clustering_result.get("recommendations") or [])[:3]:
                        logger.info(f"[CLUSTERING] {rec}")
                except Exception:
                    pass

            if self.risk.state.recovery_mode and trade["result"] == "LOSS":
                asyncio.ensure_future(tg.notify_alert(
                    "RECOVERY MODE",
                    f"Daily P&L: ${self.risk.state.daily_pnl:.2f}\n"
                    f"Size reduced 50% until daily reset"))

            logger.info(f"[EXIT:{tid}] P&L=${trade['pnl_dollars']:.2f} reason={reason} "
                         f"exit_sent={'OK' if exit_sent else 'FAILED'}")

        self.status = "SCANNING"

    # ─── News Scanner Background Loop ─────────────────────────────────
    async def _news_scanner_loop(self):
        """Poll for news alerts every 2 minutes. Non-blocking."""
        while True:
            try:
                from core.news_scanner import NewsScanner
                if not hasattr(self, '_news_scanner'):
                    self._news_scanner = NewsScanner()
                alerts = await self._news_scanner.scan()
                if alerts:
                    for alert in alerts[:3]:  # Top 3 alerts
                        logger.info(f"[NEWS] {alert.get('type', '?')}: {alert.get('summary', '')[:80]}")
                    self._latest_news_alerts = alerts
            except ImportError:
                pass  # Module not yet available
            except Exception as e:
                logger.debug(f"[NEWS] Scanner error: {e}")
            await asyncio.sleep(120)  # Every 2 minutes

    # ─── Phase 4: AI Agent Runners ────────────────────────────────────
    async def _run_council(self, market: dict):
        """Run council gate in background. Non-blocking — errors logged only."""
        try:
            logger.info("[COUNCIL] Running 7-voter session bias vote...")
            recent = self.trade_memory.recent(10)

            # Enrich market with strategy performance for smarter voting
            market["strategy_performance"] = self.tracker.get_all_summaries()

            # Fetch live market intelligence (VIX, news, economic calendar)
            try:
                from core.market_intel import get_full_intel
                intel = await get_full_intel()
                market["intel"] = intel
                self._latest_intel = intel  # Phase 5: store for cockpit + TG commands
                logger.info(f"[COUNCIL] Intel loaded: VIX={intel.get('vix', 'N/A')}, "
                             f"news_tier={intel.get('highest_tier', 'N/A')}")
            except Exception as e:
                logger.warning(f"[COUNCIL] Market intel unavailable: {e}")
                market["intel"] = {}

            result = await council_gate.run_council(market, recent)
            self._council_result = council_to_dict(result)
            logger.info(f"[COUNCIL] Result: {result.bias} ({result.vote_count}) "
                        f"in {result.total_latency_ms:.0f}ms")
            asyncio.ensure_future(tg.notify_council(
                result.bias, result.vote_count, result.summary))
        except Exception as e:
            logger.error(f"[COUNCIL] Failed (non-blocking): {e}")

    async def _run_debrief(self):
        """Run session debrief in background. Non-blocking."""
        try:
            logger.info("[DEBRIEF] Running end-of-session coaching debrief...")
            path = await session_debriefer.run_debrief(bot_name=self.bot_name)
            if path:
                logger.info(f"[DEBRIEF] Saved to {path}")
            else:
                logger.warning("[DEBRIEF] No debrief generated (no data or AI failure)")
        except Exception as e:
            logger.error(f"[DEBRIEF] Failed (non-blocking): {e}")

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
            "last_eval": self._last_eval,
            "strategies": [
                {"name": s.name, "enabled": s.enabled, "validated": s.validated, "params": s.params}
                for s in self.strategies
            ],
            "trades": self.trade_memory.recent(20),
            "council": self._council_result,
            "filter_verdict": self._filter_verdict,
            "strategy_performance": self.tracker.to_dict(),
            # Phase 5
            "cockpit": self.cockpit.to_dict(self._cockpit_result),
            "equity": self.equity_tracker.to_dict(),
            "clustering": self._clustering_result,
            # Phase 6
            "expectancy": self.expectancy.to_dict(),
            "no_trade_fingerprints": self.no_trade_fp.to_dict(),
            "regime_transitions": self.regime_transitions.to_dict(),
            # Phase 6b
            "microstructure_filter": self.microstructure_filter.to_dict(),
            "crowding_detector": self.crowding_detector.to_dict(),
            "counter_edge": self.counter_edge.to_dict(),
            "execution_quality": self.execution_quality.to_dict(),
        }

    # ─── Runtime Control ────────────────────────────────────────────
    def set_profile(self, profile_name: str):
        """Apply an aggression profile from config. Updates strategies on next bar."""
        from config.strategies import STRATEGY_DEFAULTS
        profiles = STRATEGY_DEFAULTS.get("profiles", {})
        if profile_name in profiles:
            old = {k: self._runtime_params.get(k) for k in profiles[profile_name]}
            self._runtime_params.update(profiles[profile_name])
            # Also push risk params immediately
            p = profiles[profile_name]
            if "risk_per_trade" in p:
                self.risk.set_risk_per_trade(p["risk_per_trade"])
            if "max_daily_loss" in p:
                self.risk.set_daily_limit(p["max_daily_loss"])
            logger.info(f"[PROFILE] Switched to {profile_name.upper()}: {profiles[profile_name]}")

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
