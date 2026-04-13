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
from core.rsi_divergence import RSIDivergenceDetector
from core.htf_pattern_scanner import HTFPatternScanner
from core.hmm_regime import HMMRegimeDetector
from core.smc_patterns import SMCDetector
from core.trade_rag import TradeRAG
from core.calendar_risk import CalendarRiskManager
from core.regime_playbooks import PlaybookManager
from core.intermarket_engine import IntermarketEngine
from core.edge_miner import EdgeMiner
from core.knowledge_rag import KnowledgeRAG
from core.pandas_ta_detector import PandasTADetector
from core.chart_patterns import ChartPatternDetector
from data_feeds.cot_feed import COTFeed
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

        # Phase 6+: RSI divergence + HTF pattern confluence
        self.rsi_divergence = RSIDivergenceDetector(rsi_length=14, pivot_left=5, pivot_right=5)
        self.htf_scanner = HTFPatternScanner(tick_size=TICK_SIZE)

        # Phase 7: AI Learning — HMM regime detection + trade similarity RAG
        self.hmm_regime = HMMRegimeDetector(n_regimes=3, warmup_bars=50)
        self.smc = SMCDetector(swing_lookback=5, tick_size=TICK_SIZE)
        self.trade_rag = TradeRAG(
            db_path=os.path.join(os.path.dirname(__file__), "..", "data", "trade_vectors")
        )

        # Phase 8: Knowledge Systems
        self.calendar_risk = CalendarRiskManager(check_interval_min=5)
        self.playbook_mgr = PlaybookManager()
        self.intermarket = IntermarketEngine(window=20)
        self.edge_miner = EdgeMiner(
            logs_dir=os.path.join(os.path.dirname(__file__), "..", "logs")
        )
        self.knowledge_rag = KnowledgeRAG(
            db_path=os.path.join(os.path.dirname(__file__), "..", "data", "knowledge_vectors")
        )

        # Phase 8: pandas-ta 62-pattern detector + COT institutional positioning
        self.pandas_ta = PandasTADetector(max_bars=100)
        self.chart_patterns = ChartPatternDetector(tick_size=TICK_SIZE, pivot_lookback=5)
        self.cot_feed = COTFeed(
            cache_dir=os.path.join(os.path.dirname(__file__), "..", "data")
        )

        self._last_rsi_divergence = None   # Latest divergence signal
        self._last_htf_confluence = None   # Latest HTF pattern confluence

        # State for dashboard
        self.status = "IDLE"
        self._ws = None  # Active websocket (for heartbeat sender)
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

        # Start heartbeat sender (bridge detects hung bots)
        asyncio.ensure_future(self._heartbeat_loop())

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
        """Push bot state to dashboard every 2s and poll for commands.
        Uses async HTTP to avoid blocking the event loop (which starves
        WebSocket keepalive pings and causes cascading disconnects).
        """
        url_state = f"http://127.0.0.1:{DASHBOARD_PORT}/api/bot-state"
        url_cmds = f"http://127.0.0.1:{DASHBOARD_PORT}/api/commands?bot={self.bot_name}"

        # Try aiohttp first (non-blocking), fall back to thread-pool urllib
        try:
            import aiohttp
            _use_aiohttp = True
        except ImportError:
            _use_aiohttp = False

        while True:
            try:
                if _use_aiohttp:
                    async with aiohttp.ClientSession() as sess:
                        # Push state
                        state_json = json.dumps(self.to_dict())
                        async with sess.post(url_state, data=state_json,
                                             headers={"Content-Type": "application/json"},
                                             timeout=aiohttp.ClientTimeout(total=2)):
                            pass

                        # Poll commands
                        try:
                            async with sess.get(url_cmds, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                                cmds = await resp.json()
                                for cmd in cmds:
                                    self._handle_dashboard_command(cmd)
                        except Exception:
                            pass
                else:
                    # Fallback: run blocking urllib in thread pool so it doesn't
                    # starve the event loop
                    loop = asyncio.get_event_loop()
                    state_json = json.dumps(self.to_dict()).encode("utf-8")
                    req = urllib.request.Request(
                        url_state, data=state_json,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=2))

                    # Poll commands
                    try:
                        resp = await loop.run_in_executor(
                            None, lambda: urllib.request.urlopen(url_cmds, timeout=2))
                        cmds = json.loads(resp.read().decode())
                        for cmd in cmds:
                            self._handle_dashboard_command(cmd)
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(f"Dashboard push failed: {e}")

            await asyncio.sleep(2)

    async def _heartbeat_loop(self):
        """Send periodic heartbeat to bridge so it can detect hung bots."""
        while True:
            try:
                if self._ws and self._ws.open:
                    await self._ws.send(json.dumps({
                        "type": "heartbeat",
                        "name": self.bot_name,
                        "status": self.status,
                        "ts": time.time(),
                    }))
            except Exception:
                pass  # Best effort — reconnect loop handles real failures
            await asyncio.sleep(10)

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
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            max_queue=1024,
        ) as ws:
            # Identify ourselves to the bridge
            await ws.send(json.dumps({
                "type": "identify",
                "name": self.bot_name,
            }))
            self._ws = ws
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

                # Yield to event loop — lets websockets handle ping/pong
                # Without this, rapid tick processing starves keepalive
                await asyncio.sleep(0)

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
                                # Phase 7: Log near-miss for AI learning
                                try:
                                    sig_dict = {
                                        "direction": signal.direction, "strategy": signal.strategy,
                                        "confidence": signal.confidence, "entry_score": signal.entry_score,
                                        "reason": signal.reason,
                                    }
                                    self.history.log_near_miss(sig_dict, market_snap, f"filter_sit_out: {verdict.reason}")
                                    self.trade_rag.add_near_miss(sig_dict, market_snap)
                                except Exception:
                                    pass
                                continue
                            # CAUTION handled in _enter_trade via self._filter_verdict
                        except Exception as e:
                            logger.warning(f"[FILTER] Error (defaulting to CLEAR): {e}")
                            self._filter_verdict = {"action": "CLEAR", "reason": f"Error: {e}", "source": "default"}

                    await self._enter_trade(ws, signal)

    # ─── Bar Event Handler ──────────────────────────────────────────
    def _on_bar(self, timeframe: str, bar):
        """Called by tick_aggregator when a bar completes."""
        # Feed SMC pattern detector on 1m and 5m bars
        if timeframe in ("1m", "5m"):
            try:
                smc_signals = self.smc.update(bar)
                for s in smc_signals:
                    logger.info(f"[SMC {timeframe}] {s.pattern} {s.direction} "
                                f"str={s.strength:.0f} — {s.description}")
            except Exception as e:
                logger.debug(f"[SMC] Update error (non-blocking): {e}")

            # Phase 8: Feed chart pattern detector on 1m and 5m bars
            try:
                chart_pats = self.chart_patterns.update(timeframe, bar)
                for cp in chart_pats:
                    logger.info(f"[CHART {timeframe}] {cp.pattern} {cp.direction} "
                                f"str={cp.strength:.0f} tgt={cp.target_price:.2f}")
            except Exception as e:
                logger.debug(f"[CHART PATTERNS] Update error (non-blocking): {e}")

        # Feed RSI divergence detector on every 1m bar close
        if timeframe == "1m":
            div = self.rsi_divergence.update(bar.close)
            if div:
                self._last_rsi_divergence = div
                logger.info(f"[RSI DIV] {div['type'].upper()} divergence "
                            f"strength={div['strength']:.0f} "
                            f"RSI={div['rsi_current']:.1f} "
                            f"bars_apart={div['bars_apart']}")

        # Phase 7: Feed HMM regime detector on 5m bar completions
        if timeframe == "5m":
            try:
                hmm_result = self.hmm_regime.update(bar)
                if hmm_result.get("change_point"):
                    logger.info(f"[HMM] Change point detected! Regime={hmm_result['regime']} "
                                f"conf={hmm_result['confidence']:.2f}")
            except Exception as e:
                logger.debug(f"[HMM] Update error (non-blocking): {e}")

            # Phase 8: Feed intermarket engine with NQ price on 5m bars
            try:
                self.intermarket.update_nq(bar.close)
            except Exception:
                pass

            # Phase 8: Feed pandas-ta detector on 5m bar completions
            try:
                self.pandas_ta.update(bar)
            except Exception as e:
                logger.debug(f"[PandasTA] Feed error (non-blocking): {e}")

        # Feed HTF pattern scanner on 5m/15m/60m bar completions
        if timeframe in ("5m", "15m", "60m"):
            htf_patterns = self.htf_scanner.on_bar(timeframe, bar)
            if htf_patterns:
                for sig in htf_patterns:
                    p = sig["pattern"]
                    logger.info(f"[HTF PATTERN] {timeframe} {p['pattern']} "
                                f"({p.get('direction','?')}) "
                                f"strength={p.get('strength',0)}")

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

        # Phase 8: Run edge miner at AFTERHOURS transition (once per day)
        if (regime == "AFTERHOURS" and self._last_regime != "AFTERHOURS"):
            try:
                self.edge_miner.load_trades(bot_name=self.bot_name)
                patterns = self.edge_miner.analyze()
                if patterns:
                    edges = [p for p in patterns if p.is_edge][:3]
                    anti = [p for p in patterns if not p.is_edge][:2]
                    for e in edges:
                        logger.info(f"[EDGE MINER] Edge: {e.description}")
                    for a in anti:
                        logger.warning(f"[EDGE MINER] Anti-edge: {a.description}")
            except Exception as e:
                logger.debug(f"[EDGE MINER] Analysis error (non-blocking): {e}")

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

        # Minimum bars guard — just 1 completed 1m bar (~60s after connect)
        # Strategies have their own regime-aware gates; no need to double-gate here.
        # The 100-tick buffer from the bridge means we often get bar #1 within seconds.
        bars_5m = list(self.aggregator.bars_5m.completed)
        bars_1m = list(self.aggregator.bars_1m.completed)
        if len(bars_1m) < 1:
            reason = f"Warming up ({len(bars_1m)} 1m bars — need 1, ~1 min)"
            self.last_rejection = reason
            self._last_eval["risk_blocked"] = reason
            logger.info(f"[WARMUP] {reason}")
            return

        # Get market state FIRST (needed by risk gate and everything below)
        market = self.aggregator.snapshot()

        # Enrich market snapshot with RSI + HTF pattern data for strategies
        market["rsi"] = self.rsi_divergence.get_current_rsi()
        market["rsi_divergence"] = self._last_rsi_divergence
        market["htf_patterns"] = self.htf_scanner.get_state().get("active_patterns", [])

        # Phase 7: Enrich with SMC pattern data
        try:
            smc_state = self.smc.get_state()
            market["smc_structure"] = smc_state.get("structure")
            market["smc_recent"] = smc_state.get("recent_signals", [])[-3:]
        except Exception:
            pass

        # Phase 7: Enrich with HMM regime data
        try:
            hmm_state = self.hmm_regime.get_state()
            market["hmm_regime"] = hmm_state.get("regime")
            market["hmm_confidence"] = hmm_state.get("confidence", 0)
            market["hmm_change_point"] = hmm_state.get("change_point", False)
            market["hmm_regime_params"] = hmm_state.get("regime_params", {})
        except Exception:
            pass

        # Phase 8: Enrich with intermarket risk signal
        try:
            market["intermarket"] = self.intermarket.get_risk_signal()
        except Exception:
            pass

        # Phase 8: Enrich with pandas-ta pattern data
        try:
            active = self.pandas_ta.get_active_patterns()
            if active:
                market["candlestick_patterns"] = active
                market["candlestick_confluence"] = self.pandas_ta.get_confluence_score(
                    "LONG" if market.get("tf_votes_bullish", 0) > market.get("tf_votes_bearish", 0) else "SHORT"
                )
        except Exception:
            pass

        # Phase 8: Enrich with geometric chart patterns
        try:
            chart_active = self.chart_patterns.get_active_patterns()
            if chart_active:
                bias_dir = "LONG" if market.get("tf_votes_bullish", 0) > market.get("tf_votes_bearish", 0) else "SHORT"
                market["chart_patterns"] = chart_active
                market["chart_pattern_confluence"] = self.chart_patterns.get_confluence_score(bias_dir)
        except Exception:
            pass

        # Phase 8: Enrich with COT institutional positioning
        try:
            cot = self.cot_feed.get_signal()
            if cot.get("leveraged_fund_net", 0) != 0:
                market["cot"] = cot
        except Exception:
            pass

        # Phase 8: Enrich with calendar risk
        try:
            cal_adj = self.calendar_risk.get_risk_adjustment()
            market["calendar_risk"] = {
                "blocked": cal_adj.blocked,
                "size_multiplier": cal_adj.size_multiplier,
                "stop_multiplier": cal_adj.stop_multiplier,
                "reason": cal_adj.reason,
                "next_event": cal_adj.next_event,
                "minutes_until": cal_adj.minutes_until,
            }
            if cal_adj.blocked:
                self.last_rejection = f"Calendar: {cal_adj.reason}"
                self._last_eval["risk_blocked"] = f"Calendar: {cal_adj.reason}"
                logger.warning(f"[CALENDAR RISK] BLOCKED: {cal_adj.reason}")
                return
        except Exception:
            pass

        # Phase 8: Update playbook based on HMM regime
        try:
            hmm_regime = market.get("hmm_regime", "DEFAULT")
            hmm_conf = market.get("hmm_confidence", 0)
            self.playbook_mgr.update_regime(hmm_regime, hmm_conf)
        except Exception:
            pass

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

        # Phase 8: Apply playbook strategy overrides based on HMM regime
        try:
            for strat in self.strategies:
                pb_overrides = self.playbook_mgr.get_strategy_overrides(strat.name)
                for k, v in pb_overrides.items():
                    strat.config[k] = v
        except Exception:
            pass

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
            # Phase 8: Playbook suppression check
            if self.playbook_mgr.is_strategy_suppressed(strat.name):
                logger.info(f"  [{strat.name}] SKIP — suppressed by {self.playbook_mgr.get_current().name} playbook")
                self._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_PLAYBOOK"})
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
                    reject = getattr(strat, '_last_reject', '')
                    if reject:
                        logger.info(f"  [{strat.name}] REJECTED: {reject}")
                        self._last_eval["strategies"].append({"name": strat.name, "result": "REJECTED", "reason": reject})
                        strat._last_reject = ''
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

            # Phase 6+: RSI divergence confluence boost
            try:
                rsi_div = self._last_rsi_divergence
                if rsi_div and rsi_div["strength"] > 20:
                    # Bullish div + LONG signal = boost, Bearish div + SHORT = boost
                    # Opposing divergence = observation only (never blocks)
                    div_aligned = ((rsi_div["type"] == "bullish" and best_signal.direction == "LONG") or
                                   (rsi_div["type"] == "bearish" and best_signal.direction == "SHORT"))
                    if div_aligned:
                        div_boost = min(15, int(rsi_div["strength"] / 5))
                        best_signal.confidence = min(100, best_signal.confidence + div_boost)
                        best_signal.confluences.append(
                            f"RSI {rsi_div['type']} div +{div_boost} "
                            f"(RSI={rsi_div['rsi_current']:.0f}, str={rsi_div['strength']:.0f})")
                        logger.info(f"[RSI DIV BOOST] +{div_boost} confidence -> "
                                     f"{best_signal.confidence:.0f} ({rsi_div['type']})")
                    else:
                        # Opposing divergence — log but don't block
                        best_signal.confluences.append(
                            f"Warning: opposing RSI {rsi_div['type']} div "
                            f"(RSI={rsi_div['rsi_current']:.0f})")
                        logger.info(f"[RSI DIV] Opposing {rsi_div['type']} divergence "
                                     f"(observation only, not blocking)")
                    self._last_eval["rsi_divergence"] = rsi_div
            except Exception as e:
                logger.debug(f"[RSI DIV] Error (non-blocking): {e}")

            # Phase 6+: HTF pattern confluence boost
            try:
                htf_conf = self.htf_scanner.get_confluence_score(best_signal.direction)
                self._last_htf_confluence = htf_conf
                if htf_conf["aligned_count"] > 0 and htf_conf["score"] > 10:
                    htf_boost = min(15, int(htf_conf["score"] / 5))
                    best_signal.confidence = min(100, best_signal.confidence + htf_boost)
                    strongest = htf_conf["strongest"] or "pattern"
                    strongest_tf = htf_conf["strongest_tf"] or "?"
                    best_signal.confluences.append(
                        f"HTF {strongest_tf} {strongest} +{htf_boost} "
                        f"({htf_conf['aligned_count']} aligned, score={htf_conf['score']:.0f})")
                    logger.info(f"[HTF BOOST] +{htf_boost} confidence -> "
                                 f"{best_signal.confidence:.0f} "
                                 f"({htf_conf['aligned_count']} aligned patterns, "
                                 f"strongest: {strongest_tf} {strongest})")
                elif htf_conf["opposing_count"] > 0:
                    best_signal.confluences.append(
                        f"Warning: {htf_conf['opposing_count']} opposing HTF patterns")
                self._last_eval["htf_confluence"] = htf_conf
            except Exception as e:
                logger.debug(f"[HTF PATTERNS] Error (non-blocking): {e}")

            # Phase 7: SMC pattern confluence boost
            try:
                smc_conf = self.smc.get_confluence_score(best_signal.direction)
                if smc_conf["aligned_count"] > 0 and smc_conf["score"] > 30:
                    smc_boost = min(20, int(smc_conf["score"] / 4))
                    best_signal.confidence = min(100, best_signal.confidence + smc_boost)
                    pat = smc_conf["strongest_pattern"] or "pattern"
                    best_signal.confluences.append(
                        f"SMC {pat} +{smc_boost} ({smc_conf['aligned_count']} aligned)")
                    logger.info(f"[SMC BOOST] +{smc_boost} confidence -> {best_signal.confidence:.0f} "
                                f"({smc_conf['strongest_description']})")
                self._last_eval["smc"] = smc_conf
            except Exception as e:
                logger.debug(f"[SMC] Confluence error (non-blocking): {e}")

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
            # Phase 7: Log near-miss
            try:
                sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                            "confidence": signal.confidence, "entry_score": signal.entry_score,
                            "reason": signal.reason}
                self.history.log_near_miss(sig_dict, market, "risk_tier_skip")
                self.trade_rag.add_near_miss(sig_dict, market)
            except Exception:
                pass
            return

        # Phase 8: Calendar risk size adjustment
        try:
            cal_adj = self.calendar_risk.get_risk_adjustment()
            if cal_adj.size_multiplier < 1.0:
                risk_dollars *= cal_adj.size_multiplier
                logger.info(f"[{tid}:CALENDAR] size={cal_adj.size_multiplier:.1f}x "
                             f"→ risk=${risk_dollars:.2f} ({cal_adj.reason})")
        except Exception:
            pass

        # Phase 8: Intermarket risk adjustment
        try:
            im_risk = self.intermarket.get_risk_signal()
            if im_risk["risk_off_score"] > 70:
                risk_dollars *= 0.5
                logger.info(f"[{tid}:INTERMARKET] High risk-off ({im_risk['risk_off_score']:.0f}) "
                             f"→ risk=${risk_dollars:.2f}")
            elif im_risk["risk_off_score"] > 55:
                risk_dollars *= 0.75
                logger.info(f"[{tid}:INTERMARKET] Elevated risk ({im_risk['risk_off_score']:.0f}) "
                             f"→ risk=${risk_dollars:.2f}")
        except Exception:
            pass

        # Phase 8: Playbook risk adjustment
        try:
            pb_risk = self.playbook_mgr.get_risk_overrides()
            pb_size = pb_risk.get("size_multiplier", 1.0)
            if pb_size != 1.0:
                risk_dollars *= pb_size
                logger.info(f"[{tid}:PLAYBOOK] size={pb_size:.1f}x → risk=${risk_dollars:.2f}")
        except Exception:
            pass

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

        # Phase 8: Calendar risk stop widening (post-event volatility expansion)
        try:
            cal_adj = self.calendar_risk.get_risk_adjustment()
            if cal_adj.stop_multiplier > 1.0:
                old_stop = stop_ticks
                stop_ticks = int(stop_ticks * cal_adj.stop_multiplier)
                logger.info(f"[{tid}:CALENDAR] stop widened {old_stop}→{stop_ticks}t ({cal_adj.reason})")
        except Exception:
            pass

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
            # Phase 7: Log near-miss
            try:
                sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                            "confidence": signal.confidence, "entry_score": signal.entry_score,
                            "reason": signal.reason}
                self.history.log_near_miss(sig_dict, market, "zero_contracts")
                self.trade_rag.add_near_miss(sig_dict, market)
            except Exception:
                pass
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

            # Phase 6: Close expectancy tracking BEFORE log_exit so MAE/MFE is included
            exp_analysis = self.expectancy.close_trade(
                exit_price=price,
                pnl_ticks=trade["pnl_ticks"],
                result=trade["result"],
            )

            # Log exit with MAE/MFE data from expectancy engine
            market_snap = self.aggregator.snapshot()
            if exp_analysis:
                market_snap["mae_ticks"] = exp_analysis.get("mae_ticks")
                market_snap["mfe_ticks"] = exp_analysis.get("mfe_ticks")
                market_snap["capture_ratio"] = exp_analysis.get("edge_captured_pct")
                market_snap["went_red_first"] = exp_analysis.get("went_red_first")
                market_snap["mae_time_s"] = exp_analysis.get("mae_time_s")
                market_snap["mfe_time_s"] = exp_analysis.get("mfe_time_s")
            self.history.log_exit(trade, market_snap)

            # Phase 7: Store trade in RAG vector DB for similarity search
            try:
                rag_outcome = {
                    "mae_ticks": market_snap.get("mae_ticks", 0),
                    "mfe_ticks": market_snap.get("mfe_ticks", 0),
                    "capture_ratio": market_snap.get("capture_ratio", 0),
                    "hold_seconds": trade.get("hold_time_s", 0),
                    "exit_reason": trade.get("exit_reason", ""),
                }
                self.trade_rag.add_trade(trade, market_snap, rag_outcome)
            except Exception as e:
                logger.debug(f"[RAG] add_trade error (non-blocking): {e}")

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
        """Poll for news alerts + external data every 2 minutes. Non-blocking."""
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

            # Phase 8: Refresh calendar risk events
            try:
                await self.calendar_risk.refresh_calendar()
            except Exception as e:
                logger.debug(f"[CALENDAR] Refresh error: {e}")

            # Phase 8: Refresh COT institutional positioning (daily)
            try:
                await self.cot_feed.refresh()
            except Exception as e:
                logger.debug(f"[COT] Refresh error: {e}")

            # Phase 8: Feed intermarket engine with external data
            try:
                from core.market_intel import get_full_intel
                intel = await get_full_intel()
                if intel:
                    im_data = {}
                    if "vix" in intel and intel["vix"]:
                        im_data["VIX"] = float(intel["vix"])
                    if "dxy" in intel and intel["dxy"]:
                        im_data["DXY"] = float(intel["dxy"])
                    if im_data:
                        self.intermarket.update_from_external(im_data)
                        logger.debug(f"[INTERMARKET] Updated: {im_data}")
            except Exception as e:
                logger.debug(f"[INTERMARKET] Feed error: {e}")

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
        # Core fields — must always succeed
        result = {
            "bot_name": self.bot_name,
            "status": self.status,
            "live_trading": LIVE_TRADING,
            "market": market,
            "last_signal": self.last_signal,
            "last_rejection": self.last_rejection,
            "last_eval": self._last_eval,
            "council": self._council_result,
            "filter_verdict": self._filter_verdict,
            "clustering": self._clustering_result,
            "rsi_last_divergence": self._last_rsi_divergence,
            "htf_last_confluence": self._last_htf_confluence,
        }
        # Each sub-component wrapped so one failure doesn't kill the entire state push
        _safe = {
            "position":              lambda: self.positions.to_dict(market.get("price", 0)),
            "risk":                  lambda: self.risk.to_dict(),
            "session":               lambda: self.session.to_dict(),
            "strategies":            lambda: [{"name": s.name, "enabled": s.enabled, "validated": s.validated, "params": s.params} for s in self.strategies],
            "trades":                lambda: self.trade_memory.recent(20),
            "strategy_performance":  lambda: self.tracker.to_dict(),
            "cockpit":               lambda: self.cockpit.to_dict(self._cockpit_result),
            "equity":                lambda: self.equity_tracker.to_dict(),
            "expectancy":            lambda: self.expectancy.to_dict(),
            "no_trade_fingerprints": lambda: self.no_trade_fp.to_dict(),
            "regime_transitions":    lambda: self.regime_transitions.to_dict(),
            "microstructure_filter": lambda: self.microstructure_filter.to_dict(),
            "crowding_detector":     lambda: self.crowding_detector.to_dict(),
            "counter_edge":          lambda: self.counter_edge.to_dict(),
            "execution_quality":     lambda: self.execution_quality.to_dict(),
            "rsi_divergence":        lambda: self.rsi_divergence.get_state(),
            "htf_patterns":          lambda: self.htf_scanner.get_state(),
            "hmm_regime":            lambda: self.hmm_regime.to_dict(),
            "trade_rag":             lambda: self.trade_rag.to_dict(),
            "smc_patterns":          lambda: self.smc.to_dict(),
            "calendar_risk":         lambda: self.calendar_risk.to_dict(),
            "playbook":              lambda: self.playbook_mgr.to_dict(),
            "intermarket":           lambda: self.intermarket.to_dict(),
            "edge_miner":            lambda: self.edge_miner.to_dict(),
            "knowledge_rag":         lambda: self.knowledge_rag.to_dict(),
            "pandas_ta":             lambda: self.pandas_ta.to_dict(),
            "chart_patterns":        lambda: self.chart_patterns.to_dict(),
            "cot_feed":              lambda: self.cot_feed.to_dict(),
        }
        for key, fn in _safe.items():
            try:
                result[key] = fn()
            except Exception as e:
                logger.debug(f"to_dict: {key} failed: {e}")
                result[key] = None
        return result

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
