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
from datetime import datetime, date

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env for API keys (GEMINI_API_KEY, ANTHROPIC_API_KEY, etc.)
# CRITICAL: override=True — host OS may have these vars set (e.g. empty
# ANTHROPIC_API_KEY from Claude Code OAuth shim). Without override, dotenv
# silently skips keys that already exist in os.environ, even if empty,
# which leaves agents in DEGRADED mode. (B42 2026-04-21)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
except ImportError:
    pass

from config.settings import (BOT_WS_PORT, TICK_SIZE, LIVE_TRADING, DASHBOARD_PORT,
                             AGENT_COUNCIL_ENABLED, AGENT_PRETRADE_FILTER_ENABLED,
                             AGENT_DEBRIEF_ENABLED, ENTRY_ORDER_TYPE, LIMIT_OFFSET_TICKS,
                             SCALE_OUT_ENABLED, SCALE_OUT_RR, TREND_RIDER_ENABLED,
                             TREND_RIDER_MIN_SCORE,
                             ATR_STOP_ENABLED, ATR_STOP_TF, ATR_STOP_MULTIPLIER,
                             ATR_STOP_MIN_TICKS, ATR_STOP_MAX_TICKS,
                             NT8_DATA_ROOT, OIF_INCOMING, OIF_OUTGOING)
from config.strategies import STRATEGIES, STRATEGY_DEFAULTS
from core.tick_aggregator import TickAggregator
from core.risk_manager import RiskManager
from core.session_manager import SessionManager
from core.position_manager import PositionManager
from core.trend_stall import TrendStallDetector
from core.day_classifier import DayClassifier
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
# ─── NEW Apr 2026 rebuild modules (shadow mode — no trade gating) ───
from core.swing_detector import SwingState, bias_from_swings
from core.volume_profile import VolumeProfile
from core.reversal_detector import ReversalDetector
from core.liquidity_sweep import SweepWatcher
from core.strategy_decay_monitor import DecayMonitor
from core.tca_tracker import TCATracker
from core.circuit_breakers import CircuitBreakers, HALT_MARKER_FILE
from core.chart_patterns_v1 import extract_v1_patterns
from core.vix_term_structure import get_cached as get_vix_term_cached
from core.gamma_flip_detector import GammaFlipDetector
from core.pinning_detector import PinningDetector
from core.opex_calendar import get_opex_status
from core.es_confirmation import check_confirmation as check_es_confirmation
from core.session_tagger import session_for as session_tag_for
from core.structural_bias import compute_structural_bias
from bridge.footprint_builder import FootprintAccumulator
from core.footprint_patterns import scan_bar as scan_footprint_bar
from core.contract_rollover import get_active_contract, log_rollover_status
from core.simple_sizing import get_sizer as get_simple_sizer
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


def _validate_nt8_paths():
    """
    Verify NT8-dependent paths exist on disk before the bot starts any work.

    A bot pointed at a nonexistent NT8 folder is worse than one that refuses
    to start — OIF writes silently drop and fill-ACK polls read nothing.
    On any missing path: log CRITICAL, fire a Telegram alert if configured,
    and exit(1) before tick handling begins.
    """
    paths = [
        ("NT8_DATA_ROOT", NT8_DATA_ROOT),
        ("OIF_INCOMING", OIF_INCOMING),
        ("OIF_OUTGOING", OIF_OUTGOING),
    ]
    missing = [(name, p) for name, p in paths if not os.path.isdir(p)]
    if not missing:
        return

    for name, p in missing:
        logger.critical(f"[STARTUP] NT8 path missing: {name}={p}")

    summary = ", ".join(f"{n}={p}" for n, p in missing)
    try:
        tg.send_sync(
            "\U0001F6A8 <b>BOT FAILED TO START</b>\n"
            f"Missing NT8 paths: <code>{summary}</code>\n"
            "Check config/settings.py NT8_DATA_ROOT — NT8 data folder may have moved."
        )
    except Exception as e:
        # Never let an alert failure mask the real startup failure
        logger.warning(f"[STARTUP] Telegram alert also failed: {e}")

    sys.exit(1)


# ── OIF Sink (Phase B+ migration: PHOENIX_RISK_GATE) ──────────────────────
#
# Every OIF write that base_bot performs flows through `_get_oif_sink()`.
# When PHOENIX_RISK_GATE is unset or "0" (the default) the helper returns
# a DirectFileSink which simply re-dispatches to the legacy
# bridge.oif_writer functions — behavior is byte-for-byte identical to
# the pre-migration code. When PHOENIX_RISK_GATE=1, the helper returns a
# RiskGateSink which forwards each request to the gate over a Windows
# named pipe and respects the ACCEPT/REFUSE response.
#
# RiskGateSink fails soft: if the gate process isn't running, it logs
# WARN once and falls back to DirectFileSink so the bot keeps trading.
# That preserves the safety property: enabling the flag must NEVER make
# the bot worse than the legacy path; in the worst case, it degrades to
# the legacy path with a visible warning.
#
# Helper functions wrap each legacy call so the surrounding logic
# (account routing, B59 live-guard, _move_nt8_stop, scale-out, exit
# fallback, emergency flatten, post-fill OCO attach) stays untouched.

_OIF_SINK = None  # cached per-process; rebuilt on env-flag flip is not supported


def _get_oif_sink():
    """Return the configured OIF sink (DirectFileSink by default,
    RiskGateSink if PHOENIX_RISK_GATE=1). Cached per-process for speed."""
    global _OIF_SINK
    if _OIF_SINK is None:
        from phoenix_bot.orchestrator.oif_writer import get_default_sink
        _OIF_SINK = get_default_sink()
    return _OIF_SINK


def _sink_submit_place(direction: str, qty: int, entry_type: str,
                       entry_price: float, stop_price: float,
                       target_price, trade_id: str, account: str,
                       strategy: str = "", sub_strategy=None) -> dict:
    """Sink-mediated bracket PLACE. Returns the sink response dict.
    On REFUSE, the caller should treat as a no-op and skip the entry."""
    from config.settings import INSTRUMENT
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "PLACE",
        "strategy": strategy or "",
        "account": account or "",
        "instrument": INSTRUMENT,
        "action": "BUY" if str(direction).upper() == "LONG" else "SELL",
        "qty": int(qty),
        "order_type": str(entry_type).upper(),
        "tif": "GTC",
        "price_ref": float(entry_price or 0.0),
        "entry_price": float(entry_price or 0.0),
        "stop_price": float(stop_price) if stop_price is not None else None,
        "target_price": float(target_price) if target_price is not None else None,
        "trade_id": trade_id or "",
        "sub_strategy": sub_strategy,
    }
    return sink.submit(req)


def _sink_submit_protect(direction: str, qty: int, stop_price: float,
                         target_price: float, trade_id: str,
                         account: str) -> dict:
    """Sink-mediated post-fill OCO protection (PROTECT op)."""
    from config.settings import INSTRUMENT
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "PROTECT",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        # PROTECT carries direction of the FILLED position; the legacy
        # writer derives the opposite side for stop+target.
        "direction": str(direction).upper(),
        "action": "SELL" if str(direction).upper() == "LONG" else "BUY",
        "qty": int(qty),
        "order_type": "OCO",
        "tif": "GTC",
        "stop_price": float(stop_price),
        "target_price": float(target_price),
        "trade_id": trade_id,
    }
    return sink.submit(req)


def _sink_submit_exit(qty: int, trade_id: str, account: str,
                      reason: str = "") -> dict:
    """Sink-mediated EXIT (CLOSEPOSITION)."""
    from config.settings import INSTRUMENT
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "EXIT",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        "action": "SELL",  # not actually used for EXIT; legacy writer issues CLOSEPOSITION
        "qty": int(qty),
        "order_type": "MARKET",
        "tif": "GTC",
        "trade_id": trade_id,
        "reason": reason,
    }
    return sink.submit(req)


def _sink_submit_partial_exit(direction: str, n_contracts: int,
                              trade_id: str, account: str) -> dict:
    """Sink-mediated PARTIAL_EXIT (scale-out)."""
    from config.settings import INSTRUMENT
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "PARTIAL_EXIT",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        "direction": str(direction).upper(),
        "action": "SELL" if str(direction).upper() == "LONG" else "BUY",
        "qty": int(n_contracts),
        "order_type": "MARKET",
        "tif": "GTC",
        "trade_id": trade_id,
    }
    return sink.submit(req)


def _sink_submit_modify_stop(direction: str, new_stop_price: float,
                             n_contracts: int, trade_id: str,
                             account: str, old_stop_order_id: str) -> dict:
    """Sink-mediated stop cancel+replace (MODIFY_STOP)."""
    from config.settings import INSTRUMENT
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "MODIFY_STOP",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        "direction": str(direction).upper(),
        "action": "SELL" if str(direction).upper() == "LONG" else "BUY",
        "qty": int(n_contracts),
        "order_type": "STOPMARKET",
        "tif": "GTC",
        "new_stop_price": float(new_stop_price),
        "trade_id": trade_id,
        "old_stop_order_id": old_stop_order_id,
    }
    return sink.submit(req)


# ── Trend Rider helpers (module-level, pure functions) ───────────────────────

def _should_scale_out(pos, price: float, scale_rr: float) -> bool:
    """True when price has moved scale_rr * stop_distance in our favor."""
    stop_dist = abs(pos.entry_price - pos.stop_price)
    if stop_dist == 0:
        return False
    if pos.direction == "LONG":
        return price >= pos.entry_price + stop_dist * scale_rr
    else:
        return price <= pos.entry_price - stop_dist * scale_rr


def _move_nt8_stop(pos, old_stop_price: float, new_stop_price: float) -> None:
    """B76: cancel + replace the NT8 STOPMARKET so a Python-side stop move
    actually takes effect at the broker. Safe no-op if pos.stop_order_id
    wasn't captured."""
    if not getattr(pos, "stop_order_id", ""):
        logger.warning(
            f"[STOP_MOVE_NO_ID:{pos.trade_id}] pos.stop_order_id not captured — "
            f"Python stop moved to {new_stop_price:.2f}, NT8 stop unchanged"
        )
        return
    try:
        from bridge.oif_writer import scan_outgoing_for_order_id
        # Phase B+: route through Sink Protocol. With PHOENIX_RISK_GATE=0
        # this dispatches to bridge.oif_writer.write_modify_stop exactly
        # as before. With PHOENIX_RISK_GATE=1 it goes through RiskGate.
        resp = _sink_submit_modify_stop(
            direction=pos.direction,
            new_stop_price=new_stop_price,
            n_contracts=pos.contracts,
            trade_id=pos.trade_id,
            account=pos.account,
            old_stop_order_id=pos.stop_order_id,
        )
        if resp.get("decision") == "ACCEPT":
            new_oid = scan_outgoing_for_order_id(pos.account, new_stop_price)
            if new_oid:
                pos.stop_order_id = new_oid
            logger.info(
                f"[STOP_MOVED:{pos.trade_id}] {old_stop_price:.2f} -> {new_stop_price:.2f}"
            )
        else:
            logger.error(
                f"[STOP_MOVE_FAILED:{pos.trade_id}] sink {resp.get('sink','?')} "
                f"REFUSED: {resp.get('reason','?')}"
            )
    except Exception as e:
        logger.error(f"[STOP_MOVE_EXCEPTION:{pos.trade_id}] {e}")


def _trail_stop(pos, price: float):
    """
    Trail stop to midpoint between entry and current price.
    Only moves in favorable direction — never worsens risk.

    B76: after mutating pos.stop_price, emit write_modify_stop OIF to
    actually move the NT8 stop via cancel+replace.
    """
    mid = (pos.entry_price + price) / 2
    new_stop = None
    if pos.direction == "LONG" and mid > pos.stop_price:
        new_stop = round(mid, 2)
    elif pos.direction == "SHORT" and mid < pos.stop_price:
        new_stop = round(mid, 2)
    if new_stop is None:
        return
    old_stop = pos.stop_price
    pos.stop_price = new_stop
    logger.info(f"[TRAIL:{pos.trade_id}] Stop trailed to {pos.stop_price:.2f} (mid)")
    _move_nt8_stop(pos, old_stop, new_stop)


# ── B62: Universal stop/target sanity gate (Exit Sprint S1) ──────────────
def _sanity_check_entry(signal, entry_price, stop_price, target_price):
    """Fail-closed geometry + distance check before OCO submission.

    Returns (ok: bool, reason: str|None). On failure, caller logs
    [STOP_SANITY_FAIL] CRITICAL and aborts the trade.

    Rules:
      - LONG: stop < entry < target (target may be None for managed exits)
      - SHORT: target < entry < stop (target may be None for managed exits)
      - Stop distance: 5-200 MNQ ticks (0.25 tick size).
    """
    tick_size = 0.25  # MNQ
    if entry_price is None or stop_price is None:
        return False, f"missing price: entry={entry_price} stop={stop_price}"
    if signal.direction == "LONG":
        if not (stop_price < entry_price):
            return False, (f"LONG order geometry wrong: stop={stop_price} "
                           f"entry={entry_price} target={target_price}")
        if target_price is not None and not (entry_price < target_price):
            return False, (f"LONG order geometry wrong: stop={stop_price} "
                           f"entry={entry_price} target={target_price}")
    else:  # SHORT
        if not (stop_price > entry_price):
            return False, (f"SHORT order geometry wrong: stop={stop_price} "
                           f"entry={entry_price} target={target_price}")
        if target_price is not None and not (target_price < entry_price):
            return False, (f"SHORT order geometry wrong: stop={stop_price} "
                           f"entry={entry_price} target={target_price}")
    stop_ticks = abs(entry_price - stop_price) / tick_size
    if stop_ticks < 5 or stop_ticks > 200:
        return False, f"stop distance {stop_ticks:.0f}t outside 5-200 range"
    return True, None


# ── Dashboard push JSON default (BUG-TL1) ────────────────────────────────
def _json_default_safe(obj):
    """
    json.dumps default= handler — called only for non-JSON-serializable values.

    Historically the dashboard-push path raised "Object of type datetime is
    not JSON serializable" whenever a `to_dict()` producer leaked a raw
    datetime or date into the snapshot. This helper coerces those at the
    push boundary so producer code doesn't have to hunt-and-ISO-encode
    every field path. Any other exotic type falls back to str() so we
    never re-raise into the dashboard loop.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "isoformat") and callable(obj.isoformat):
        # Covers pandas.Timestamp, time, and similar date/time-like objects.
        try:
            return obj.isoformat()
        except Exception:
            pass
    try:
        return str(obj)
    except Exception:
        return f"<unserializable {type(obj).__name__}>"


class BaseBot:
    """
    Base bot that connects to the bridge, processes ticks, and runs strategies.
    Subclasses (prod_bot, lab_bot) configure which strategies to load.
    """

    bot_name: str = "base"
    only_validated: bool = False  # Prod overrides to True

    def __init__(self):
        _validate_nt8_paths()
        # B59: one-time startup banner documenting the live-account hard-guard.
        _live_guard = os.environ.get("LIVE_ACCOUNT", "").strip()
        if _live_guard:
            logger.critical(
                f"[LIVE_GUARD] armed — any order routed to account "
                f"'{_live_guard}' will hard-fail (B59 defensive guard)."
            )
        else:
            logger.warning(
                "[LIVE_GUARD] DISARMED — LIVE_ACCOUNT not set in .env. "
                "Bot will NOT reject orders targeting any account."
            )
        self.aggregator = TickAggregator(bot_name=self.bot_name)
        # Restore aggregator state from disk (survive restarts — no warmup needed)
        self._aggregator_state_path = os.path.join(
            os.path.dirname(__file__), "..", "data", f"aggregator_state_{self.bot_name}.json"
        )
        os.makedirs(os.path.dirname(self._aggregator_state_path), exist_ok=True)
        if self.aggregator.restore_state(self._aggregator_state_path):
            logger.info(f"[WARMUP] Aggregator state restored — indicators pre-loaded")
        self.risk = RiskManager()
        self.session = SessionManager(bot_name=self.bot_name)
        # P0.1 (D13): load durable trade history so dashboard P&L and any
        # in-process consumer of trade_history survive bot restart.
        self.positions = PositionManager(load_history=True)
        self.trade_memory = TradeMemory()
        self.history = HistoryLogger(bot_name=self.bot_name)
        self.tracker = StrategyTracker()
        self.strategies: list[BaseStrategy] = []

        # B84: DailyFlattener is shared infrastructure (was sim-only pre-B84).
        # Prod now inherits the 15:54 CT flatten too. ws_send is plumbed in
        # lazily once the WS connection comes up — see _daily_flatten_loop.
        from bots.daily_flatten import DailyFlattener as _DailyFlattener
        self._flattener = _DailyFlattener(
            positions_manager=self.positions,
            websocket_send_fn=None,
            logger=logger,
        )
        # B84 grace-window bookkeeping: flip to True once a flatten has fired
        # today; the post-flatten watcher (15:54 → 15:54:45) reads this.
        self._flatten_grace_logged_for: Optional["date"] = None  # type: ignore[name-defined]

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

        # ─── NEW Apr 2026 rebuild modules (SHADOW MODE) ───────────────
        # These modules RUN but do NOT gate strategy signals (dual-write).
        # Strategies continue using old tf_bias until WFO validates structural_bias.
        # Data flows: tick/bar → these modules → market snapshot enrichment → dashboard.
        self.swing_state_5m = SwingState()
        self.volume_profile = VolumeProfile()
        self.reversal_detector = ReversalDetector()
        self.sweep_watcher = SweepWatcher()
        self.gamma_flip_detector = GammaFlipDetector()
        self.pinning_detector = PinningDetector()
        self.footprint_1m = FootprintAccumulator(bar_length_s=60)
        self.footprint_5m = FootprintAccumulator(bar_length_s=300)
        self.decay_monitor = DecayMonitor(shadow_mode=True)
        self.tca_tracker = TCATracker()
        self.circuit_breakers = CircuitBreakers(observe_mode=True)
        self.simple_sizer = get_simple_sizer()
        # Latest outputs (exposed to dashboard via _state)
        self._last_structural_bias = None
        self._last_footprint_signals: list = []
        self._last_chart_patterns_v1: list = []
        self._last_climax_warning = None
        self._last_sweep_event = None
        self._last_vix_term = None
        self._last_pinning_state = None
        self._last_opex_status = None
        self._last_es_confirmation = None
        self._last_gamma_flip_event = None
        # Contract rollover check at startup
        try:
            log_rollover_status()
        except Exception as _e:
            logger.warning(f"[ROLLOVER] check failed at startup: {_e}")

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
        self._last_cr = None               # Latest C/R assessment (continuation_reversal)
        self._debrief_ran_today = False     # Only run once per session day

        # Trend rider state
        self._stall_detector = TrendStallDetector(lookback=5)
        self._rider_active = False          # True while holding runner contract
        self._day_classifier = DayClassifier()
        self._day_type = "UNKNOWN"          # TREND | RANGE | VOLATILE | UNKNOWN
        self._price_bar_highs: list[float] = []   # Recent bar highs (for stall detector)
        self._price_bar_lows:  list[float] = []   # Recent bar lows
        self._pending_exit_reason = None    # Market close auto-exit
        self._current_date = None           # For daily reset detection
        self._last_bridge_ack_ok = True     # Bridge OIF write confirmation

        # Phase 5: Additional state
        self._cockpit_result = None         # Latest cockpit grading
        self._latest_intel = None           # Latest market intel (for TG commands)
        self._clustering_result = None      # Latest clustering analysis
        self._trades_since_cluster = 0      # Counter for clustering trigger
        self._equity_recorded_today = False # Only record equity once per day

        # ─── B14 Phase 4: MenthorQ gamma integration ──────────────────
        # Load daily gamma levels from data/menthorq/gamma/ at startup.
        # An async watcher (_gamma_reload_watcher) polls for mtime changes
        # every 60s so intraday repastes take effect without a bot restart.
        # Used for: (a) entry-wall filter via is_entry_into_wall,
        # (b) snapshot enrichment (regime, nearest wall, pin-zone flag).
        # NOTE: natural_stop_for_entry() exists in core/menthorq_gamma but
        # is intentionally NOT wired here — strategies will opt in later.
        from pathlib import Path as _GammaPath
        from core.menthorq_gamma import load_latest_gamma
        from config.settings import MENTHORQ_GAMMA_DIR, MENTHORQ_MAX_DATA_AGE_HOURS
        self.gamma_data_dir = _GammaPath(MENTHORQ_GAMMA_DIR)
        self._gamma_max_age_hours = MENTHORQ_MAX_DATA_AGE_HOURS
        try:
            self.gamma_levels = load_latest_gamma(
                self.gamma_data_dir, max_age_hours=self._gamma_max_age_hours
            )
            if self.gamma_levels is not None:
                logger.info(
                    f"[GAMMA] Loaded: date={self.gamma_levels.data_date} "
                    f"symbol={self.gamma_levels.symbol} "
                    f"HVL={self.gamma_levels.hvl} "
                    f"HVL_0DTE={self.gamma_levels.hvl_0dte} "
                    f"complete={self.gamma_levels.is_complete}"
                )
            else:
                logger.warning(
                    f"[GAMMA] No gamma data loaded from {self.gamma_data_dir} "
                    f"— entry-wall filter will be inactive"
                )
        except Exception as _e:
            logger.warning(f"[GAMMA] Failed to load gamma data: {_e}")
            self.gamma_levels = None
        # Track the mtime we loaded from so the reload watcher can detect
        # newer files in the directory.
        self._gamma_mtime = 0.0
        try:
            _levels_files = list(self.gamma_data_dir.glob("*_levels.txt"))
            if _levels_files:
                self._gamma_mtime = max(p.stat().st_mtime for p in _levels_files)
        except Exception:
            pass

        # Register bar callback
        self.aggregator.on_bar(self._on_bar)

    def _reconcile_positions_from_nt8(self) -> list[dict]:
        """B77 + P0.3: scan NT8 outgoing/ for non-FLAT positions and adopt
        them.

        Called at startup AND periodically during the session (every
        RUNTIME_RECON_INTERVAL_S seconds via _runtime_reconciliation_loop)
        so a mid-session orphan can't drift unnoticed until next restart.

        The underlying reconcile_positions_from_nt8 is idempotent: it
        skips any account with an already-tracked Position in the
        PositionManager, so re-calling every 30s never creates phantoms.
        """
        from core.startup_reconciliation import reconcile_positions_from_nt8
        from config.settings import OIF_OUTGOING, INSTRUMENT

        telegram_notify = None
        try:
            from core.telegram_notifier import send_sync as _tg_send
            telegram_notify = _tg_send
        except Exception:
            pass

        # Fix C (2026-04-23): scope reconciliation to THIS bot's own accounts.
        # Pre-fix: both prod_bot and sim_bot scanned all 17 routed accounts.
        # Whichever bot's 30s timer fired first adopted any orphan — so prod
        # was booking P&L on trades that belonged to sim's sub-accounts.
        # Evidence: 2026-04-23 05:49 both bots adopted the same SimSpring
        # Setup LONG 6 seconds apart and both booked exits on it.
        #
        # Resolution rule:
        #   - FORCE_ACCOUNT set (prod_bot) → reconcile only that one account.
        #   - FORCE_ACCOUNT None (sim_bot) → reconcile the bot's resolvable
        #     account set (STRATEGY_ACCOUNT_MAP minus Sim101, which prod owns).
        routed = self._resolve_reconciliation_scope()
        return reconcile_positions_from_nt8(
            positions=self.positions,
            outgoing_dir=OIF_OUTGOING,
            instrument=INSTRUMENT,
            telegram_notify=telegram_notify,
            routed_accounts=routed,
        )

    def _resolve_reconciliation_scope(self) -> list[str]:
        """Per-bot reconciliation account scope (Fix C, 2026-04-23).

        - If the bot class defines FORCE_ACCOUNT, return exactly that one
          account — prod_bot must not adopt sim-account orphans.
        - Otherwise return the 16 per-strategy sim accounts from the
          routing map, excluding Sim101 (prod owns Sim101).
        """
        _force = getattr(self, "FORCE_ACCOUNT", None)
        if _force:
            return [_force]
        try:
            from config.account_routing import STRATEGY_ACCOUNT_MAP
        except Exception:
            return []
        accounts: set[str] = set()
        for key, value in STRATEGY_ACCOUNT_MAP.items():
            if key == "_default":
                continue  # _default = Sim101 fallback; prod owns it
            if isinstance(value, str):
                accounts.add(value)
            elif isinstance(value, dict):
                accounts.update(value.values())
        accounts.discard("Sim101")  # prod-exclusive
        return sorted(accounts)

    # P0.3 (D12) runtime reconciliation interval. 30s is the interim
    # polling cadence — Phase 1's broker-event stream will replace the
    # timer with sub-second reaction later. Module-level constant so
    # tests can monkeypatch (via bots.base_bot.RUNTIME_RECON_INTERVAL_S).
    RUNTIME_RECON_INTERVAL_S: float = 30.0

    # P0.6 (D7) exit_pending timeout. If a position sits in exit_pending
    # state for longer than this, the CLOSEPOSITION OIF probably never
    # filled at NT8 — fire CRITICAL + halt the strategy so a "Python
    # thinks flat but NT8 isn't" divergence doesn't bleed silently.
    EXIT_PENDING_TIMEOUT_S: float = 60.0

    async def _runtime_reconciliation_loop(self) -> None:
        """P0.3 + P0.6: periodic NT8-ledger reconciliation during the
        session.

        Each cycle:
          1. Run `_reconcile_positions_from_nt8` to adopt any orphan NT8
             position not tracked in PositionManager (P0.3).
          2. Walk every `exit_pending` position: if NT8 shows FLAT for
             its account+instrument, call `finalize_exit_pending` to
             promote it to a closed trade. If a position has been
             exit_pending longer than EXIT_PENDING_TIMEOUT_S, fire a
             CRITICAL alert so the operator can investigate.

        A clean-shutdown flag (`self._shutdown_reconciliation`) lets
        run() stop the loop gracefully without hanging on sleep.

        Exceptions are caught + logged so one bad cycle doesn't kill the
        loop — the next tick keeps trying.
        """
        while not getattr(self, "_shutdown_reconciliation", False):
            try:
                await asyncio.sleep(self.RUNTIME_RECON_INTERVAL_S)
                if getattr(self, "_shutdown_reconciliation", False):
                    break
                # P0.3: orphan adoption.
                adopted = self._reconcile_positions_from_nt8()
                if adopted:
                    logger.info(
                        f"[RUNTIME_RECON] adopted {len(adopted)} orphan "
                        f"position(s) mid-session (accounts: "
                        f"{[a['account'] for a in adopted]})"
                    )
                # P0.6: exit_pending resolution.
                self._resolve_exit_pending_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[RUNTIME_RECON] cycle failed (will retry next interval): {e!r}"
                )

    def _resolve_exit_pending_positions(self) -> None:
        """P0.6 (D7): finalize exit_pending positions when NT8 confirms
        FLAT, or escalate when the pending window exceeds
        EXIT_PENDING_TIMEOUT_S.

        NT8's position file shape: `<instrument> Globex_<account>_position.txt`
        containing `LONG;qty;price` / `SHORT;qty;price` / `FLAT;0;0`.
        `FLAT` (or file missing) means the account+instrument has no
        open position — safe to finalize our pending exit.
        """
        pending = self.positions.exit_pending_positions()
        if not pending:
            return

        from config.settings import OIF_OUTGOING, INSTRUMENT
        from core.startup_reconciliation import _read_position_file

        now = time.time()
        for pos in pending:
            nt8_state = _read_position_file(OIF_OUTGOING, INSTRUMENT, pos.account)
            if nt8_state is None:
                # FLAT or unreadable → treat as confirmed-closed; finalize.
                trade = self.positions.finalize_exit_pending(pos.trade_id)
                logger.info(
                    f"[EXIT_FINALIZED:{pos.trade_id}] NT8 confirmed FLAT "
                    f"on {pos.account} — closed {pos.direction} @ "
                    f"{pos.pending_exit_price:.2f} "
                    f"reason={pos.pending_exit_reason}"
                )
                # Propagate to risk / tracker / trade_memory / circuit
                # breakers — same post-close hooks that _exit_trade used
                # to fire synchronously before P0.6 moved the finalize
                # out here.
                if trade:
                    try:
                        self.risk.record_trade(trade["pnl_dollars"])
                    except Exception as _e:
                        logger.error(f"[EXIT_FINALIZE] risk.record_trade failed: {_e!r}")
                    try:
                        self.trade_memory.record(trade, bot_id=self.bot_name)
                    except Exception as _e:
                        logger.error(f"[EXIT_FINALIZE] trade_memory.record failed: {_e!r}")
                    try:
                        self.tracker.record_trade(trade)
                    except Exception as _e:
                        logger.error(f"[EXIT_FINALIZE] tracker.record failed: {_e!r}")
                    try:
                        self._on_trade_closed(trade)
                    except Exception as _e:
                        logger.error(f"[EXIT_FINALIZE] _on_trade_closed failed: {_e!r}")
                continue

            # NT8 still shows a position on this account. Check timeout.
            age_s = now - pos.exit_pending_since
            if age_s > self.EXIT_PENDING_TIMEOUT_S:
                msg = (
                    f"[EXIT_PENDING_TIMEOUT:{pos.trade_id}] {pos.account} "
                    f"still shows {nt8_state[0]} {nt8_state[1]}@{nt8_state[2]} "
                    f"after {age_s:.0f}s — Python thinks flat but NT8 is not. "
                    f"CLOSEPOSITION OIF likely never filled. Halting "
                    f"strategy '{pos.strategy}' — operator flatten required."
                )
                logger.critical(msg)
                try:
                    from core.telegram_notifier import send_sync
                    send_sync(
                        f"🚨 [EXIT_TIMEOUT] {pos.account} stuck exit_pending "
                        f"{age_s:.0f}s — {msg}",
                        dedup_key=f"exit_pending_timeout:{pos.trade_id}",
                    )
                except Exception:
                    pass
                # Strategy halt hook — existing StrategyRiskRegistry has a
                # halt_strategy method; best-effort.
                try:
                    from core.strategy_risk_registry import StrategyRiskRegistry
                    reg = getattr(self, "_conflict_reg", None) or StrategyRiskRegistry()
                    if hasattr(reg, "halt_strategy"):
                        reg.halt_strategy(pos.strategy, reason="exit_pending_timeout")
                except Exception as _e:
                    logger.error(
                        f"[EXIT_PENDING_TIMEOUT] halt_strategy failed: {_e!r}"
                    )

    def load_strategies(self):
        """Load strategy instances from config. Override in subclass if needed."""
        from strategies.bias_momentum import BiasMomentumFollow
        from strategies.spring_setup import SpringSetup
        from strategies.vwap_pullback import VWAPPullback
        from strategies.high_precision import HighPrecisionOnly
        from strategies.ib_breakout import IBBreakout
        from strategies.compression_breakout import CompressionBreakout
        from strategies.dom_pullback import DOMPullback
        from strategies.orb import OpeningRangeBreakout
        from strategies.noise_area import NoiseAreaMomentum
        from strategies.vwap_band_pullback import VwapBandPullback
        from strategies.opening_session import OpeningSessionStrategy

        strategy_classes = {
            "bias_momentum": BiasMomentumFollow,
            "spring_setup": SpringSetup,
            "vwap_pullback": VWAPPullback,
            "vwap_band_pullback": VwapBandPullback,
            "high_precision_only": HighPrecisionOnly,
            "ib_breakout": IBBreakout,
            "compression_breakout": CompressionBreakout,
            "dom_pullback": DOMPullback,
            "orb": OpeningRangeBreakout,
            "noise_area": NoiseAreaMomentum,
            "opening_session": OpeningSessionStrategy,
        }

        is_prod = (self.bot_name == "prod")

        for name, config in STRATEGIES.items():
            if name not in strategy_classes:
                continue
            if self.only_validated and not config.get("validated", False):
                continue
            if not config.get("enabled", True):
                continue

            # Prod vs lab session-window flag (ORB + Noise Area use this
            # to pick eod_flat_time_et = 10:55 ET vs 15:55 ET).
            enriched = dict(config)
            enriched["is_prod_bot"] = is_prod
            strat = strategy_classes[name](enriched)

            # Defensive: strategies that don't inherit BaseStrategy (e.g. v2
            # rewrites emitting their own Signal class — bias_momentum_v2,
            # vwap_pullback v2) won't have .validated / .params / .check_exit
            # and can't be driven by the main loop. Skip them with a clear
            # warning instead of crashing the whole bot on startup.
            if not isinstance(strat, BaseStrategy):
                logger.warning(
                    f"[LOAD] Skipping '{name}' — class {type(strat).__name__} "
                    f"does not inherit BaseStrategy (no .validated / canonical "
                    f"Signal). Needs an adapter before promotion — see "
                    f"memory/context/OPEN_QUESTIONS.md for the promotion gate."
                )
                continue

            self.strategies.append(strat)

            # Noise Area: seed sigma_open_table from data/sigma_open_table.json.
            # Loader returns None on any failure; strategy still works (it will
            # accrue sigma_open live until 14 sessions pass the min_noise_history gate).
            if isinstance(strat, NoiseAreaMomentum):
                from tools.load_sigma_open_warmup import load_sigma_open_warmup
                _warmup = load_sigma_open_warmup()
                if _warmup is not None:
                    strat.seed_history(_warmup)
                    logger.info(f"[noise_area] seeded {len(_warmup)} minute-buckets from data/sigma_open_table.json")
                else:
                    logger.info("[noise_area] no warmup — will accrue live")

            logger.info(f"Loaded strategy: {name} (validated={strat.validated})")

    # ─── Main Loop ──────────────────────────────────────────────────
    async def run(self):
        self.load_strategies()
        logger.info(f"{'=' * 50}")
        logger.info(f"  PHOENIX {self.bot_name.upper()} BOT")
        logger.info(f"  Strategies: {[s.name for s in self.strategies]}")
        logger.info(f"  Live trading: {LIVE_TRADING}")
        logger.info(f"{'=' * 50}")

        # Phase 4C: visual confirmation of per-strategy account routing at
        # startup so Jennifer can eyeball-match against the NT8 config.
        try:
            from config.account_routing import validate_account_map
            accounts = validate_account_map()
            logger.info(
                f"[ACCOUNT_ROUTING] {len(accounts)} account routes configured: "
                f"{len([a for a in accounts if a != 'Sim101'])} dedicated + 1 default"
            )
            logger.info(f"[ACCOUNT_ROUTING] accounts: {', '.join(accounts)}")
        except Exception as e:
            logger.warning(f"[ACCOUNT_ROUTING] validate_account_map failed: {e!r}")

        # B77 startup reconciliation (2026-04-21): adopt any orphan NT8
        # positions left over from a crash / restart and attach safety-net
        # OCOs. Must run BEFORE the tick loop accepts new signals so we
        # don't route new trades on top of an unprotected orphan.
        try:
            self._reconcile_positions_from_nt8()
        except Exception as e:
            logger.error(f"[RECONCILE] startup reconciliation failed: {e!r}")

        # P0.3 (D12) runtime reconciliation: schedule the async timer
        # BEFORE the tick loop starts so mid-session orphans get caught
        # within RUNTIME_RECON_INTERVAL_S of appearing. Clean-shutdown
        # flag drives the loop's termination condition.
        self._shutdown_reconciliation = False
        asyncio.ensure_future(self._runtime_reconciliation_loop())

        # Phase 4C: one-shot [SESSION+GAMMA] regime log fires from inside
        # the tick loop once last_price > 0 and gamma_levels is loaded.
        self._startup_regime_logged = False

        # Start dashboard state pusher in background
        asyncio.ensure_future(self._dashboard_loop())

        # Start heartbeat sender (bridge detects hung bots)
        asyncio.ensure_future(self._heartbeat_loop())

        # Start news/momentum scanner in background (Phase 4+)
        asyncio.ensure_future(self._news_scanner_loop())

        # Phase 5: Start Telegram command listener
        asyncio.ensure_future(self.telegram_commands.poll_commands(self))

        # B14 Phase 4: MenthorQ gamma reload watcher (60s poll)
        asyncio.ensure_future(self._gamma_reload_watcher())

        # Phase 4B: session-levels prior-day refresh at 00:01 CT daily
        asyncio.ensure_future(self._session_levels_refresh_task())

        # B84: 15:54 CT daily flatten + 15:54:45 fill-confirmation watcher.
        # Subclasses that need post-flatten hooks (e.g. sim_bot's debrief +
        # recap) override _daily_flatten_loop rather than scheduling their
        # own parallel loop.
        asyncio.ensure_future(self._daily_flatten_loop())

        # Hourly decay health check + 15:10 CT daily summary Telegram push.
        asyncio.ensure_future(self._decay_monitor_loop())

        # 2026-04-24: FMP market-data cross-check loop. Fetches NDX/QQQ
        # from financialmodelingprep.com, converts to MNQ-equivalent, and
        # compares against the local accepted tick. If local drifts more
        # than 1.5% from FMP on two consecutive checks, writes the HALT
        # marker so circuit_breakers pauses new entries within ~5s. Safe
        # no-op when FMP_API_KEY is unset.
        try:
            from core import fmp_sanity
            asyncio.ensure_future(fmp_sanity.poll_loop(interval_s=60.0,
                                                      halt_on_divergence_pct=0.015))
        except Exception as e:
            logger.warning(f"[FMP] sanity loop failed to start (non-blocking): {e!r}")

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
                        # Push state. default=_json_default_safe coerces
                        # any leaked datetime/date in sub-component to_dict()
                        # outputs to ISO strings (BUG-TL1 fix).
                        state_json = json.dumps(self.to_dict(), default=_json_default_safe)
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
                    state_json = json.dumps(
                        self.to_dict(), default=_json_default_safe
                    ).encode("utf-8")
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

    async def _gamma_reload_watcher(self):
        """B14 Phase 4: poll data/menthorq/gamma/ every 60s; reload when
        a newer *_levels.txt appears (intraday repaste, new day). Silent
        on no-change; logs at INFO when a reload fires.
        """
        from core.menthorq_gamma import load_latest_gamma
        while True:
            await asyncio.sleep(60)
            try:
                files = list(self.gamma_data_dir.glob("*_levels.txt"))
                if not files:
                    continue
                current_mtime = max(p.stat().st_mtime for p in files)
                if current_mtime > self._gamma_mtime:
                    new_levels = load_latest_gamma(
                        self.gamma_data_dir,
                        max_age_hours=self._gamma_max_age_hours,
                    )
                    if new_levels is not None:
                        self.gamma_levels = new_levels
                        self._gamma_mtime = current_mtime
                        logger.info(
                            f"[GAMMA] Reloaded — date={new_levels.data_date} "
                            f"complete={new_levels.is_complete}"
                        )
            except Exception as e:
                logger.warning(f"[GAMMA] Reload watcher error: {e}")

    def _enrich_market_with_gamma(self, market: dict) -> dict:
        """B14 Phase 4: inject gamma_levels, gamma_regime, nearest wall,
        and pin-zone flag into the snapshot. No-op when gamma data missing
        (strategies and downstream code tolerate None)."""
        from core.menthorq_gamma import (
            classify_regime, distance_to_nearest_wall, is_at_hvl_gravity,
            GammaRegime,
        )
        levels = self.gamma_levels
        market["gamma_levels"] = levels
        if levels is None:
            market["gamma_regime"] = GammaRegime.UNKNOWN
            market["gamma_nearest_wall"] = ("none", 9999.0)
            market["gamma_in_pin_zone"] = False
            return market
        price = market.get("price", 0) or 0
        if price <= 0:
            market["gamma_regime"] = GammaRegime.UNKNOWN
            market["gamma_nearest_wall"] = ("none", 9999.0)
            market["gamma_in_pin_zone"] = False
            return market
        market["gamma_regime"] = classify_regime(price, levels)
        # Nearest wall relative to current price (no direction — both sides
        # considered via LONG look-up; we scan both then pick the closest).
        long_wall = distance_to_nearest_wall(price, "LONG", levels)
        short_wall = distance_to_nearest_wall(price, "SHORT", levels)
        if long_wall[1] < short_wall[1]:
            market["gamma_nearest_wall"] = long_wall
        else:
            market["gamma_nearest_wall"] = short_wall
        market["gamma_in_pin_zone"] = is_at_hvl_gravity(price, levels)
        return market

    async def _session_levels_refresh_task(self):
        """
        Phase 4B: recompute prior-day OHLC + volume profile + pivots at
        00:01 CT each day so a long-running bot picks up the newly
        written JSONL history without a restart. Errors are logged and
        the loop retries in 1 hour so one bad day doesn't wedge the task.
        """
        from datetime import datetime as _dt, timedelta as _td
        while True:
            try:
                now = _dt.now()
                next_refresh = now.replace(hour=0, minute=1, second=0, microsecond=0)
                if next_refresh <= now:
                    next_refresh += _td(days=1)
                sleep_secs = (next_refresh - now).total_seconds()
                logger.info(
                    f"[SESSION_LEVELS] next refresh in {sleep_secs / 3600:.1f}h"
                )
                await asyncio.sleep(sleep_secs)
                sl = getattr(self.aggregator, "session_levels", None)
                if sl is not None:
                    sl.load_prior_day()
                    logger.info(
                        f"[SESSION_LEVELS] refreshed prior-day "
                        f"H={sl.prior_day_high} L={sl.prior_day_low} "
                        f"POC={sl.prior_day_poc} PP={sl.pivot_pp}"
                    )
                else:
                    logger.warning(
                        "[SESSION_LEVELS] refresh skipped — aggregator has no session_levels"
                    )
            except Exception as e:
                logger.error(f"[SESSION_LEVELS] refresh task error: {e!r}")
                await asyncio.sleep(3600)  # retry in 1h rather than tight-loop

    # ─── B84: daily flatten + grace window + no-new-entries gate ──────
    def _is_no_new_entries_window(self, now_ct: Optional["datetime"] = None) -> bool:
        """B84: True between NO_NEW_ENTRIES_HOUR/MINUTE_CT (default 15:53)
        and the start of the next globex session (17:00 CT). Used by
        _enter_trade to refuse new positions in the final runway before
        the 15:54 daily flatten."""
        from datetime import datetime as _dt, time as _t
        from zoneinfo import ZoneInfo as _ZI
        try:
            from config.settings import (
                NO_NEW_ENTRIES_HOUR_CT, NO_NEW_ENTRIES_MINUTE_CT,
            )
        except Exception:
            NO_NEW_ENTRIES_HOUR_CT, NO_NEW_ENTRIES_MINUTE_CT = 15, 53
        ct = _ZI("America/Chicago")
        n = now_ct if now_ct is not None else _dt.now(ct)
        # Cutoff window: 15:53:00 CT → 17:00:00 CT (globex reopen).
        # After 17:00 a new session starts and entries are allowed again.
        cutoff = _t(NO_NEW_ENTRIES_HOUR_CT, NO_NEW_ENTRIES_MINUTE_CT)
        session_open = _t(17, 0)
        t = n.time()
        return cutoff <= t < session_open

    async def _daily_flatten_loop(self) -> None:
        """B84: poll every 30s — fire DailyFlattener at 15:54 CT, then
        watch the 45-second fill-confirmation grace window. Subclasses
        override this to bolt on post-flatten hooks (sim_bot adds the
        AI debrief + 17:00 daily recap)."""
        from datetime import datetime as _dt, date as _date
        from zoneinfo import ZoneInfo as _ZI
        ct = _ZI("America/Chicago")
        try:
            from config.settings import FILL_CONFIRMATION_GRACE_SECONDS as _GRACE_S
        except Exception:
            _GRACE_S = 45

        while True:
            try:
                # Hook the ws sender lazily — _ws is set by the base's
                # _connect_and_listen and may rotate on reconnect.
                self._flattener.ws_send = self._get_ws_send_fn()
                now_ct = _dt.now(ct)
                n = await self._flattener.check_and_flatten(now_ct)
                if n:
                    logger.info(
                        f"[DAILY_FLATTEN] closed {n} position(s) at "
                        f"{self._flattener.flatten_hour:02d}:"
                        f"{self._flattener.flatten_minute:02d} CT"
                    )
                    # B84: log the session_close_event immediately so the
                    # forensic trail captures pre-grace state.
                    await self._log_session_close_event(now_ct, n)

                # B84 grace window: between flatten fire and +GRACE_S,
                # log AWAITING_FILL_CONFIRMATION. At end of grace, if
                # any position is still open, WARN that NT8 Auto Close
                # will catch it.
                await self._watch_flatten_grace_window(now_ct, _GRACE_S)
            except Exception as e:
                logger.warning(f"[DAILY_FLATTEN] poll error: {e!r}")
            await asyncio.sleep(30)

    async def _watch_flatten_grace_window(self, now_ct, grace_s: int) -> None:
        """B84: after DailyFlattener fires, watch open positions for
        grace_s seconds. Log AWAITING_FILL_CONFIRMATION on entry, WARN
        if anything is still open at the end of the window."""
        fired_at = getattr(self._flattener, "last_flatten_fired_at_ct", None)
        if fired_at is None:
            return
        # Only run the grace-window hook once per day.
        if self._flatten_grace_logged_for == fired_at.date():
            return
        # Only enter the grace window if we're within it.
        elapsed = (now_ct - fired_at).total_seconds()
        if elapsed < 0 or elapsed > grace_s:
            # If we're past grace, check for lingering positions exactly once.
            if elapsed > grace_s and self._flatten_grace_logged_for != fired_at.date():
                self._emit_grace_end_warn_if_open(fired_at)
                self._flatten_grace_logged_for = fired_at.date()
            return
        open_count = len(self.positions.active_positions)
        logger.info(
            f"[AWAITING_FILL_CONFIRMATION] {open_count} position(s) still "
            f"open {elapsed:.0f}s after flatten fire at "
            f"{fired_at.strftime('%H:%M:%S')} CT — NT8 safety net at "
            f"15:55 CT will catch any remaining"
        )

    def _emit_grace_end_warn_if_open(self, fired_at) -> None:
        """B84: called once when the grace window ends. If positions
        are still open, they've effectively been handed off to the NT8
        Auto Close safety net; log WARN so the operator sees it."""
        still_open = list(self.positions.active_positions)
        if not still_open:
            logger.info(
                f"[FILL_CONFIRMED] all flatten exits confirmed closed "
                f"within grace window (fired {fired_at.strftime('%H:%M:%S')} CT)"
            )
            return
        ids = [getattr(p, "trade_id", "?") for p in still_open]
        logger.warning(
            f"[FLATTEN_INCOMPLETE] {len(still_open)} position(s) STILL OPEN "
            f"{fired_at.strftime('%H:%M:%S')} CT + grace: {ids} — NT8 "
            f"Auto Close (15:55 CT) will close these as safety net"
        )

    async def _log_session_close_event(self, now_ct, flattened_count: int) -> None:
        """B84: emit a single structured session_close_event to the
        history log. Captures which positions the bot flattened vs.
        which remain open for the NT8 safety net to catch."""
        try:
            flattened_ids = list(
                getattr(self._flattener, "last_flatten_trade_ids", []) or []
            )
            still_open = [
                getattr(p, "trade_id", "?")
                for p in self.positions.active_positions
            ]
            # Session P&L — best-effort from today's trade_history. B13
            # commission math correction is independent; if B13 hasn't
            # shipped, session_pnl is best-effort gross.
            session_pnl = 0.0
            b13_applied = False
            try:
                today = now_ct.date()
                for t in self.positions.trade_history:
                    exit_ts = t.get("exit_time") or 0
                    if not exit_ts:
                        continue
                    from datetime import datetime as _dt2
                    dt = _dt2.fromtimestamp(float(exit_ts), tz=now_ct.tzinfo)
                    if dt.date() != today:
                        continue
                    session_pnl += float(t.get("pnl_dollars") or 0.0)
                # If B13 landed, pnl_dollars already carries the fix.
                try:
                    from config.settings import B13_COMMISSION_APPLIED  # noqa: F401
                    b13_applied = True
                except Exception:
                    b13_applied = False
            except Exception:
                pass

            if hasattr(self.history, "log_session_close_event"):
                self.history.log_session_close_event(
                    now_ct=now_ct,
                    flattened_trade_ids=flattened_ids,
                    still_open_trade_ids=still_open,
                    session_pnl=session_pnl,
                    b13_applied=b13_applied,
                )
        except Exception as e:
            logger.warning(f"[SESSION_CLOSE_EVENT] log failed: {e!r}")

    def _get_ws_send_fn(self):
        """Default WS EXIT sender for the DailyFlattener. sim_bot
        overrides this to include per-strategy account routing."""
        ws = getattr(self, "_ws", None)
        if ws is None:
            return None

        async def _send(trade_id: str, reason: str = "daily_flatten_1554CT"):
            pos = self.positions.get_position(trade_id) if hasattr(
                self.positions, "get_position"
            ) else None
            if pos is None:
                return
            try:
                await ws.send(json.dumps({
                    "type": "trade", "trade_id": trade_id,
                    "action": "EXIT", "qty": pos.contracts,
                    "reason": reason,
                    "account": getattr(pos, "account", None),
                    "sub_strategy": getattr(pos, "sub_strategy", None),
                }))
            except Exception as e:
                logger.error(f"[DAILY_FLATTEN] WS EXIT send failed for {trade_id}: {e}")
        return _send

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
                # BUG-TL2 guard: the dispatch phase (json.loads + msg_type
                # routing) has its own inner json guard; non-tick messages
                # early-return via `continue`. Wrap the whole dispatch just
                # to catch any other unexpected error without kicking the WS.
                try:
                    tick = json.loads(message)
                except json.JSONDecodeError:
                    continue
                except Exception as _dispatch_err:
                    logger.error(
                        f"[WS DISPATCH] error decoding message "
                        f"(keeping WS alive): {type(_dispatch_err).__name__}: {_dispatch_err}"
                    )
                    continue

                msg_type = tick.get("type")

                if msg_type == "dom":
                    try:
                        self.aggregator.process_dom(tick)
                    except Exception as _dom_err:
                        logger.debug(f"[DOM] process failed: {_dom_err}")
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

                # Tick loop heartbeat — detect frozen loops
                self._last_tick_time = time.time()

                # BUG-TL2 guard: aggregator.process_tick is the highest-risk
                # call on the tick path — raw tick dict parsing, bar builder,
                # all indicators. If this raises, the unhandled exception
                # bubbles out of `async for`, websockets sends code=1011 to
                # the bridge, and the bot is kicked off (observed 22:50 CT
                # 2026-04-19). Subsequent per-tick work below (footprint,
                # strategies, exits) already has narrow try/except guards.
                try:
                    snapshot = self.aggregator.process_tick(tick)
                except Exception as _agg_err:
                    logger.error(
                        f"[TICK AGGREGATOR] process_tick failed "
                        f"(keeping WS alive): {type(_agg_err).__name__}: {_agg_err}"
                    )
                    continue

                # Phase 4C: one-shot startup regime log. Fires after the first
                # tick populates last_price and gamma_levels is loaded. Deferred
                # from 4B because at TickAggregator init time last_price=0 and
                # gamma_levels isn't on the aggregator.
                if (not getattr(self, "_startup_regime_logged", True)
                        and self.gamma_levels is not None
                        and self.aggregator.last_price > 0):
                    try:
                        from core.menthorq_gamma import classify_regime
                        regime = classify_regime(
                            self.aggregator.last_price, self.gamma_levels
                        )
                        logger.info(
                            f"[SESSION+GAMMA] Regime at startup: {regime.name} "
                            f"(price={self.aggregator.last_price}, "
                            f"net_gex={self.gamma_levels.net_gex})"
                        )
                    except Exception as e:
                        logger.warning(f"[SESSION+GAMMA] startup log failed: {e!r}")
                    self._startup_regime_logged = True

                # NEW (shadow): feed footprint builders on every tick (fast path, no branching)
                try:
                    self.footprint_1m.process_tick(tick)
                    self.footprint_5m.process_tick(tick)
                except Exception:
                    pass  # Footprint errors must not break tick loop

                # NEW (shadow): feed volume profile
                try:
                    from datetime import datetime as _dt
                    _price = float(snapshot.get("price", 0) or 0)
                    _vol = float(tick.get("vol", 0) or 0)
                    if _price > 0 and _vol > 0:
                        self.volume_profile.update_tick(_price, _vol, _dt.now())
                except Exception:
                    pass

                # NEW (shadow): feed circuit breakers tick-rate detector
                try:
                    self.circuit_breakers.record_tick()
                except Exception:
                    pass

                # Phase 6b: Feed microstructure filter on every tick
                self.microstructure_filter.update_tick(snapshot.get("price", 0))

                # Track intra-bar price extremes (for EMA+DOM smart exit wick detection)
                self._stall_detector.update_tick_price(snapshot.get("price", 0))

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

                    # ── Trend Rider: runner management (single & multi-contract) ──
                    # Phase C (2026-04-21): iterate ALL active positions so
                    # rider-mode/scale-out logic applies correctly when multiple
                    # strategies hold concurrent positions. list(...) snapshots
                    # the set so in-loop exits don't break iteration.
                    for pos in list(self.positions.active_positions):
                        if not TREND_RIDER_ENABLED:
                            break

                        if pos.rider_mode:
                            # ── RIDER MODE ─────────────────────────────────────────────
                            # Break-even stop: day-type aware trigger.
                            #
                            # TREND day: BE at 1R (full stop distance). Trend moves extend
                            #   well beyond 1R — no need to lock in early.
                            #
                            # RANGE/VOLATILE/UNKNOWN: BE at 0.5R (half the stop distance).
                            #   Data: choppy day extension P50 = 25 ticks. A 40-tick BE
                            #   trigger would NEVER fire before reversal. 0.5R = ~20t on
                            #   a 40t stop = activates at +10 pts, protecting the gain
                            #   before the inevitable chop-day reversal hits.
                            if not pos.be_stop_active:
                                stop_dist = abs(pos.entry_price - pos.stop_price)
                                if stop_dist > 0:
                                    # BE trigger: 1R on trend days, 0.5R otherwise
                                    be_mult = 1.0 if self._day_type == "TREND" else 0.5
                                    be_trigger = (pos.entry_price + stop_dist * be_mult
                                                  if pos.direction == "LONG"
                                                  else pos.entry_price - stop_dist * be_mult)
                                    if ((pos.direction == "LONG" and price >= be_trigger) or
                                            (pos.direction == "SHORT" and price <= be_trigger)):
                                        be_stop = (round(pos.entry_price + TICK_SIZE * 2, 2)
                                                   if pos.direction == "LONG"
                                                   else round(pos.entry_price - TICK_SIZE * 2, 2))
                                        _old_stop_px = pos.stop_price
                                        pos.stop_price = be_stop
                                        pos.be_stop_active = True
                                        logger.info(f"[RIDER:{pos.trade_id}] BE STOP "
                                                    f"({self._day_type}, {be_mult:.0%}R) — "
                                                    f"stop moved to {be_stop:.2f} "
                                                    f"(price={price:.2f}, +{(price-pos.entry_price)/TICK_SIZE:.0f}t)")
                                        # B76: actually move NT8 stop via cancel+replace
                                        _move_nt8_stop(pos, _old_stop_px, be_stop)

                            # Stall detector drives exit — check every tick (already rate-limited inside)
                            stall = self._stall_detector.check(snapshot, pos.direction)
                            if stall["exit_signal"]:
                                logger.info(f"[RIDER:{pos.trade_id}] Trend stall STRONG "
                                            f"— exiting runner. Reasons: {stall['reasons']}")
                                await self._exit_trade(ws, price, "trend_stall",
                                                       trade_id=pos.trade_id)
                            elif stall["tighten_stop"]:
                                _trail_stop(pos, price)

                        elif SCALE_OUT_ENABLED and not pos.scaled_out and pos.original_contracts >= 2:
                            # Original multi-contract scale-out path.
                            # Per-signal override: ORB et al. can supply
                            # Signal.scale_out_rr (stashed on Position) to
                            # override the global (Zarattini ORB = 1.0R).
                            _scale_rr = getattr(pos, "scale_out_rr", None) or SCALE_OUT_RR
                            if _should_scale_out(pos, price, _scale_rr):
                                await self._scale_out_trade(ws, price)

                    # ── Smart Exit: EMA extension + DOM reversal + candle wick ──
                    # Fires when: (1) held 120s+ (2) in profit N ticks+ (3) extended from
                    # EMA9 (4) DOM sellers stacking AND candle wicking (BOTH required).
                    # SKIPPED when pos.rider_mode=True — on TREND day runners, DOM wobbles
                    # are noise, not reversals. Stall detector (above) handles those exits.
                    #
                    # Day-type aware min profit:
                    #   TREND days:  40t (10pts) — big moves have room, protect real gains
                    #   Other days:  20t (5pts)  — choppy P50 extension = 25t, gate at 40t
                    #                              would never fire; 20t still filters noise
                    # Phase C: smart-exit per position (iterate all; rider-mode
                    # positions are skipped as before).
                    for _pos in list(self.positions.active_positions):
                        if _pos.rider_mode:
                            continue
                        from config.settings import TICK_SIZE as _TICK_SIZE
                        _min_profit = 40 if self._day_type == "TREND" else 20
                        smart = self._stall_detector.check_ema_dom_exit(
                            snapshot, _pos.direction,
                            tick_size=_TICK_SIZE,
                            entry_price=_pos.entry_price,
                            entry_time=_pos.entry_time,
                            min_profit_ticks=_min_profit,
                        )
                        if smart["exit_signal"]:
                            logger.info(f"[SMART EXIT:{_pos.trade_id}] {smart['reason']}")
                            await self._exit_trade(ws, price, "ema_dom_exit",
                                                   trade_id=_pos.trade_id)

                    # ── Universal EoD flat hook ─────────────────────────────────
                    # Any position whose Signal set eod_flat_time_et gets
                    # auto-flattened when current ET time >= that value.
                    # Single code path — works for ORB, Noise Area, and any
                    # future strategy that opts in.
                    # Phase C: EoD flat per position.
                    try:
                        from zoneinfo import ZoneInfo
                        _now_et = datetime.now(ZoneInfo("America/New_York"))
                        _now_hm = _now_et.strftime("%H:%M")
                        for _pos in list(self.positions.active_positions):
                            _eod = getattr(_pos, "eod_flat_time_et", None)
                            if _eod and _now_hm >= _eod:
                                logger.info(
                                    f"[EOD_FLAT:{_pos.trade_id}] "
                                    f"{_now_hm} ET >= {_eod} — closing"
                                )
                                await self._exit_trade(ws, price, "eod_flat_universal",
                                                       trade_id=_pos.trade_id)
                    except Exception as e:
                        logger.debug(f"[EOD_FLAT] check error (non-blocking): {e}")

                    # ── Chandelier trail update + exit ──────────────────────────
                    # Strategies with exit_trigger starting "chandelier_trail"
                    # get a trail-state update each bar; bracket stop stays
                    # ALSO active as a disaster stop (whichever fires first).
                    # Phase C: chandelier trail per position.
                    try:
                        _bars = list(self.aggregator.bars_1m.completed)
                        for _pos in list(self.positions.active_positions):
                            if getattr(_pos, "trail_state", None) is None:
                                continue
                            if not str(getattr(_pos, "exit_trigger", "")).startswith("chandelier_trail"):
                                continue
                            _cfg = _pos.trail_config or {}
                            _atr_key = f"atr_{_cfg.get('atr_timeframe', '5m')}"
                            _atr = market.get(_atr_key, 0) or 0
                            if _bars and _atr > 0:
                                _last = _bars[-1]
                                _pos.trail_state.update(_last.high, _last.low, _atr)
                                if _pos.trail_state.should_exit(price):
                                    logger.info(
                                        f"[CHANDELIER:{_pos.trade_id}] price {price:.2f} "
                                        f"violated trail {_pos.trail_state.current_trail:.2f} — exiting"
                                    )
                                    await self._exit_trade(ws, price, "chandelier_trail_hit",
                                                           trade_id=_pos.trade_id)
                    except Exception as e:
                        logger.debug(f"[CHANDELIER] update error (non-blocking): {e}")

                    # Phase C: managed-exit hook per position — strategies with
                    # dynamic exits (Noise Area) get a chance to close their own
                    # position on a signal flip before bracket checks fire.
                    for _pos in list(self.positions.active_positions):
                        if not getattr(_pos, "exit_trigger", None):
                            continue
                        _strat_obj = next((s for s in self.strategies if s.name == _pos.strategy), None)
                        if _strat_obj is None:
                            continue
                        try:
                            _snap = self.aggregator.snapshot()
                            _sess = self.session.to_dict()
                            should_exit, exit_reason_mgd = _strat_obj.check_exit(
                                _pos, _snap,
                                list(self.aggregator.bars_1m.completed),
                                _sess,
                            )
                            if should_exit:
                                logger.info(
                                    f"[MANAGED_EXIT:{_pos.trade_id}] "
                                    f"{_pos.strategy} → {exit_reason_mgd}"
                                )
                                await self._exit_trade(ws, price, exit_reason_mgd,
                                                       trade_id=_pos.trade_id)
                        except Exception as e:
                            logger.debug(f"[MANAGED_EXIT] check_exit error (non-blocking): {e}")

                    # Normal stop/target/time exits
                    _max_hold = None
                    if self.positions.position:
                        _strat_name = self.positions.position.strategy
                        for _s in self.strategies:
                            if _s.name == _strat_name:
                                _max_hold = _s.config.get("max_hold_min")
                                break
                    # Phase C (2026-04-21): iterate all open positions so
                    # stops/targets fire correctly in multi-position mode.
                    # In single-position mode this yields 0 or 1 trigger and
                    # behaves identically to pre-Phase-C.
                    exit_triggers = self.positions.check_exits_all(price, max_hold_min=_max_hold)
                    for _tid, _exit_reason in exit_triggers:
                        await self._exit_trade(ws, price, _exit_reason, trade_id=_tid)

                # Execute pending signals from strategy evaluation
                # HARD TIMEOUT: entire signal→trade path must complete in 15s
                # or we abandon it. This prevents the tick loop from ever freezing.
                # Phase C: per-strategy flat check unlocks concurrent entries
                # for different strategies (each on its own NT8 sub-account).
                signal = None
                pending_signals = getattr(self, "_pending_signals", None)
                if isinstance(pending_signals, list) and pending_signals:
                    # Drop stale queued signals for strategies that already
                    # have a live position; then process the next eligible one.
                    while pending_signals:
                        candidate = pending_signals.pop(0)
                        if self.positions.is_flat_for(candidate.strategy):
                            signal = candidate
                            break
                    self._pending_signals = pending_signals
                elif hasattr(self, '_pending_signal') and self._pending_signal and \
                        self.positions.is_flat_for(self._pending_signal.strategy):
                    signal = self._pending_signal
                    self._pending_signal = None

                if signal is not None:
                    try:
                        await asyncio.wait_for(
                            self._process_signal(ws, signal),
                            timeout=15.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error(f"[SIGNAL TIMEOUT] Signal processing took >15s — "
                                      f"abandoned {signal.direction} via {signal.strategy}. "
                                      f"Tick loop continues.")
                        self.last_rejection = "Signal processing timeout (15s)"
                    except Exception as e:
                        logger.error(f"[SIGNAL ERROR] {e}")
                        self.last_rejection = f"Signal error: {e}"

    # ─── Signal Processing (extracted for timeout wrapper) ──────────
    async def _process_signal(self, ws, signal):
        """Process a pending signal: run AI filter, then enter trade.
        Called inside asyncio.wait_for(timeout=15s) so it can never
        freeze the tick loop.
        """
        # Phase 4: Pre-trade filter (3s timeout, defaults to CLEAR)
        if AGENTS_AVAILABLE and AGENT_PRETRADE_FILTER_ENABLED:
            try:
                market_snap = self.aggregator.snapshot()
                regime = self.session.get_current_regime()
                recent = self.trade_memory.recent(5)

                # News awareness — inform AI filter but NEVER block trades
                try:
                    from core.market_intel import get_economic_calendar
                    cal = await get_economic_calendar()
                    if cal.get("trade_restricted"):
                        event_name = cal.get('next_event', {}).get('name', 'event')
                        logger.info(f"[NEWS SIGNAL] High-impact event: {event_name} "
                                     f"— AI filter will factor this in")
                        market_snap["news_event_imminent"] = event_name
                        asyncio.ensure_future(tg.notify_alert(
                            "NEWS EVENT", f"{event_name} — trade with awareness"))
                except Exception:
                    pass
                # Query strategy knowledge for AI context
                strategy_context = ""
                try:
                    query = f"{signal.direction} {signal.strategy} {regime} intraday"
                    strat_results = self.knowledge_rag.query_strategies(query, n_results=3)
                    if strat_results:
                        lines = []
                        for sr in strat_results:
                            lines.append(f"- {sr['title']} ({sr['category']}): "
                                          f"regimes={sr['regimes']}, ATR={sr['atr_preference']}")
                        strategy_context = "\n".join(lines)
                except Exception:
                    pass

                # Inject Menthor Q regime context into AI filter
                mq_context = ""
                try:
                    from core.menthorq_feed import get_snapshot, to_prompt_context
                    mq_snap = get_snapshot()
                    mq_context = to_prompt_context(mq_snap, market_snap.get("price", 0))
                    if strategy_context:
                        strategy_context = mq_context + "\n\n" + strategy_context
                    else:
                        strategy_context = mq_context
                except Exception:
                    pass

                # Inject Continuation/Reversal assessment (Quinn-style)
                try:
                    if hasattr(self, "_last_cr") and self._last_cr is not None:
                        from core.continuation_reversal import to_prompt_context as cr_prompt
                        cr_block = cr_prompt(self._last_cr)
                        strategy_context = cr_block + "\n\n" + (strategy_context or "")
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
                    strategy_context=strategy_context,
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

                # [AI-PRETRADE-HOOK] S6/H-4B — mode-aware skip. Advisory = log only.
                _mode = pretrade_filter.get_filter_mode(signal.strategy)
                if _mode == "blocking" and verdict.action == "SIT_OUT":
                    self.last_rejection = f"AI filter (blocking): {verdict.reason}"
                    logger.info(f"[FILTER] SIT_OUT (blocking mode) — skipping {signal.strategy}")
                    return
                # advisory mode or non-SIT_OUT verdict: trade proceeds; CAUTION handled in _enter_trade
            except Exception as e:
                logger.warning(f"[FILTER] Error (defaulting to CLEAR): {e}")
                self._filter_verdict = {"action": "CLEAR", "reason": f"Error: {e}", "source": "default"}

        try:
            await self._enter_trade(ws, signal)
        except Exception as e:
            logger.error(f"[ENTRY ERROR] _enter_trade crashed: {e}")
            self.last_rejection = f"Entry error: {e}"

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

            # ─── NEW Apr 2026 modules: close footprint bars + feed reversal/sweep ───
            # All wrapped in try/except — SHADOW MODE must never break live trading.
            if timeframe == "1m":
                try:
                    self.footprint_1m.close_bar()
                    self.volume_profile.on_bar_close()
                except Exception as e:
                    logger.debug(f"[FOOTPRINT 1m] close error: {e}")

            if timeframe == "5m":
                try:
                    fp_bar = self.footprint_5m.close_bar()
                    if fp_bar is not None:
                        history = self.footprint_5m.completed_bars[:-1]
                        signals = scan_footprint_bar(fp_bar, history)
                        self._last_footprint_signals = [s.to_dict() for s in signals]
                        for s in signals:
                            logger.info(f"[FOOTPRINT 5m] {s.pattern} {s.direction} "
                                        f"sev={s.severity:.2f} @ {s.price:.2f}")
                except Exception as e:
                    logger.debug(f"[FOOTPRINT 5m] close error: {e}")

                # Feed swing detector on 5m bars (ATR-ZigZag)
                try:
                    atr_5m = self.aggregator.atr.get("5m", 5.0) or 5.0
                    bar_idx = len(self.swing_state_5m.pivots) + 100  # Running index
                    new_pivot = self.swing_state_5m.update(bar, bar_idx, atr_5m)
                    if new_pivot:
                        logger.info(f"[SWING 5m] {new_pivot.classification} "
                                    f"@ {new_pivot.price:.2f}")
                        # Feed sweep watcher with pivot breaks
                        try:
                            # On a new HIGH pivot, the prior UP move may have broken a prior LOW pivot
                            # (simplified: we track the pivot extremes for sweep watcher)
                            # The full mechanism requires pivot break event detection; simplified here.
                            pass
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[SWING] update error: {e}")

                # Feed climax/reversal detector on 5m bars
                try:
                    atr_5m = self.aggregator.atr.get("5m", 5.0) or 5.0
                    session_cvd = getattr(self.aggregator, "cvd_session", 0)
                    bar_idx_rev = len(self.aggregator.bars_5m.completed)
                    warning, signal = self.reversal_detector.update(
                        bar, atr_5m, session_cvd, bar_idx_rev
                    )
                    if warning:
                        self._last_climax_warning = {
                            "direction": warning.direction,
                            "climax_extreme": warning.climax_extreme,
                            "bars_ago": 0,
                        }
                    if signal:
                        logger.info(f"[REVERSAL CONFIRMED] {signal.direction} "
                                    f"@ {signal.entry_price:.2f} "
                                    f"(stop {signal.stop_price:.2f})")
                except Exception as e:
                    logger.debug(f"[REVERSAL] update error: {e}")

                # Feed sweep watcher — check for failed-BOS sweeps
                try:
                    bar_idx_sw = len(self.aggregator.bars_5m.completed)
                    sweep = self.sweep_watcher.check_sweep(bar, bar_idx_sw)
                    if sweep:
                        self._last_sweep_event = {
                            "direction": sweep.reversal_direction,
                            "pivot_price": sweep.pivot_price,
                            "sweep_extreme": sweep.sweep_extreme,
                        }
                except Exception as e:
                    logger.debug(f"[SWEEP] check error: {e}")

                # Feed gamma flip detector on 5m bars
                try:
                    from core.menthorq_feed import get_snapshot as _mq_snap
                    _mq = _mq_snap()
                    _hvl = getattr(_mq, "hvl", 0) or 0
                    flip = self.gamma_flip_detector.update(bar, _hvl, news_event_recent=False)
                    if flip:
                        self._last_gamma_flip_event = {
                            "direction": flip.direction,
                            "hvl": flip.hvl_level,
                            "breach_price": flip.breach_price,
                            "ts": flip.ts.isoformat(),
                        }
                        logger.warning(f"[GAMMA FLIP] {flip.direction} confirmed at "
                                       f"HVL {flip.hvl_level:.2f}")
                except Exception as e:
                    logger.debug(f"[GAMMA FLIP] update error: {e}")

        # Feed RSI divergence detector on every 1m bar close
        if timeframe == "1m":
            div = self.rsi_divergence.update(bar.close)
            if div:
                self._last_rsi_divergence = div
                logger.info(f"[RSI DIV] {div['type'].upper()} divergence "
                            f"strength={div['strength']:.0f} "
                            f"RSI={div['rsi_current']:.1f} "
                            f"bars_apart={div['bars_apart']}")

            # Feed trend stall detector bar history (keep last 10)
            try:
                bar_high  = getattr(bar, "high",  bar.close)
                bar_low   = getattr(bar, "low",   bar.close)
                self._stall_detector.update_bar(bar_high, bar_low, bar.close)
                self._price_bar_highs.append(bar_high)
                self._price_bar_lows.append(bar_low)
                # Trim to keep only the last 10 bars
                if len(self._price_bar_highs) > 10:
                    self._price_bar_highs = self._price_bar_highs[-10:]
                    self._price_bar_lows  = self._price_bar_lows[-10:]
            except Exception:
                pass

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

        # Persist aggregator state on every bar (survive restarts)
        try:
            self.aggregator.save_state(self._aggregator_state_path)
        except Exception:
            pass

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

        # Record daily momentum score at CLOSE_CHOP→AFTERHOURS transition (EOD)
        # This captures the day's final momentum state for multi-day trajectory tracking
        if regime == "AFTERHOURS" and self._last_regime not in ("AFTERHOURS", None):
            try:
                from core.momentum_score import record_daily
                from core.menthorq_feed import get_snapshot
                _eod_mq = get_snapshot()
                _eod_market = self.aggregator.snapshot()
                eod_rec = record_daily(_eod_market, _eod_mq)
                logger.info(
                    f"[MOMENTUM SCORE] EOD recorded: {eod_rec.get('detail', '')} "
                    f"(price={eod_rec.get('price', 0):.2f})"
                )
            except Exception as e:
                logger.warning(f"[MOMENTUM SCORE] EOD record failed: {e}")

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

        # ── HALT gates — checked BEFORE anything else (P3) ────────────
        # Two independent signals:
        #   (1) .HALT marker file (user-managed emergency halt; always enforced)
        #   (2) CircuitBreakers.should_halt() (breaker-managed; honors observe_mode)
        # Either triggering skips the entire evaluation cycle and records the
        # reason in _last_eval for dashboard visibility.
        _halt_reason = None
        if HALT_MARKER_FILE.exists():
            _halt_reason = f"HALTED — emergency marker file at {HALT_MARKER_FILE}"
        elif self.circuit_breakers.should_halt():
            _halt_reason = f"HALTED — circuit breaker: {self.circuit_breakers.halted_reason or 'active'}"
        if _halt_reason is not None:
            logger.warning(f"[HALT] {_halt_reason} — blocking strategy evaluation")
            self.last_rejection = _halt_reason
            self._last_eval = {
                "ts": datetime.now().isoformat(),
                "regime": self.session.to_dict().get("regime", "?"),
                "risk_blocked": _halt_reason,
                "strategies": [],
                "best_signal": None,
            }
            return

        # Enforce prod trading window — prod bot only trades during defined session.
        # Exception: TREND days with session_unrestricted=True bypass window checks —
        # high-conviction trend days should be traded all session, not just 2 windows.
        if self.bot_name == "prod":
            _session_unrestricted = self._day_classifier.params.get("session_unrestricted", False)
            if _session_unrestricted:
                pass  # TREND day — trade all day, no window restriction
            else:
                _cr_verdict = getattr(self._last_cr, "verdict", None) if self._last_cr else None
                _cr_score   = getattr(self._last_cr, "momentum_score", 0) if self._last_cr else 0
                if not self.session.is_prod_trading_window(cr_verdict=_cr_verdict, cr_score=_cr_score):
                    return  # Prod bot: outside all trading windows, skip evaluation

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

        # B14 Phase 4: enrich with MenthorQ gamma state (regime, nearest wall,
        # pin-zone flag, raw GammaLevels). Strategies can read these for
        # context; the entry-wall filter (below, post best_signal pick)
        # uses the same gamma_levels for rejection decisions.
        self._enrich_market_with_gamma(market)

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

        # 2026-04-24 Market advisor guidance. Deterministic producer that
        # synthesizes MQ + FMP + tick-agg state into sentiment / volatility
        # / market_regime / suggested_rr_tier / caution_flags. Strategies
        # can opt in by reading market["advisor_guidance"]["suggested_rr_tier"]
        # to adjust their RR (2:1 for CHOPPY, 3:1 for TRENDING, 1.5:1 for
        # OVEREXTENDED per Jennifer). Guidance is also surfaced into the
        # council's voter prompt further down. Failures never crash eval.
        try:
            from agents.market_advisor import enrich_market_snapshot as _enrich_advisor
            market = _enrich_advisor(market)
            _g = market.get("advisor_guidance") or {}
            if _g:
                logger.debug(
                    f"[ADVISOR] sent={_g.get('sentiment')} "
                    f"regime={_g.get('market_regime')} "
                    f"rr={_g.get('suggested_rr_tier')} "
                    f"flags={','.join(_g.get('caution_flags', [])) or '-'}"
                )
        except Exception as e:
            logger.debug(f"[ADVISOR] enrichment failed (non-blocking): {e!r}")

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

        # MenthorQ gamma regime — enrich market snapshot for strategies
        # gamma_regime: "POSITIVE" (above HVL, suppress vol) | "NEGATIVE" (below HVL, amplify)
        # above_hvl: bool — real-time price vs HVL flip line
        # mq_day_min/max: gamma-implied range for today
        _mq_snap = None
        try:
            from core.menthorq_feed import get_snapshot, regime_for_price
            _mq_snap = get_snapshot()
            _mq_regime = regime_for_price(_mq_snap, market.get("price", 0))
            market["gamma_regime"] = _mq_regime.get("gamma_regime", "UNKNOWN")
            market["above_hvl"]    = _mq_regime.get("above_hvl", True)
            market["mq_hvl"]       = _mq_regime.get("hvl", 0.0)
            market["mq_day_min"]   = _mq_regime.get("day_min", 0.0)
            market["mq_day_max"]   = _mq_regime.get("day_max", 0.0)
            market["mq_nearest_resistance"] = _mq_regime.get("nearest_resistance", 0.0)
            market["mq_nearest_support"]    = _mq_regime.get("nearest_support", 0.0)
            market["mq_direction_bias"]     = _mq_snap.direction_bias if _mq_snap else "NEUTRAL"
        except Exception as _mq_err:
            import traceback as _tb
            logger.warning(f"[MQ] Snapshot load error (mq_direction_bias=NEUTRAL): "
                           f"{_mq_err} | {_tb.format_exc().splitlines()[-1]}")
            market["gamma_regime"] = "UNKNOWN"
            market["above_hvl"] = True
            market["mq_direction_bias"] = "NEUTRAL"

        # Continuation/Reversal Assessment (Quinn-style)
        # Runs every bar — lightweight trajectory lookup + level proximity check
        try:
            from core.continuation_reversal import assess as cr_assess
            from core.momentum_score import get_trajectory
            _cr_traj = get_trajectory(10)
            _cr = cr_assess(market, _mq_snap, _cr_traj)
            market["cr_verdict"]   = _cr.verdict        # "CONTINUATION"|"REVERSAL"|"CONTESTED"
            market["cr_confidence"]= _cr.confidence     # "LOW"|"MEDIUM"|"HIGH"
            market["cr_direction"] = _cr.direction_bias # "LONG"|"SHORT"|"NEUTRAL"
            market["cr_mom_score"] = _cr.momentum_score
            market["cr_at_resistance"] = _cr.at_call_resistance or _cr.at_day_max
            market["cr_at_support"]    = _cr.at_put_support or _cr.at_day_min
            self._last_cr = _cr  # Store for dashboard and pre-trade prompt
        except Exception:
            market["cr_verdict"] = "UNKNOWN"
            self._last_cr = None

        # ── Day Type Classification ────────────────────────────────────
        # Classify the session as TREND / RANGE / VOLATILE and apply
        # day-appropriate parameter overrides (spacing, targets, size).
        # Runs every bar so it adapts if character changes mid-session.
        try:
            _cr_v = market.get("cr_verdict", "UNKNOWN")
            _cr_s = market.get("cr_mom_score", 0) or 0
            _atr  = market.get("atr_5m", 0) or 0
            _vix  = market.get("vix", 0) or 0
            _day  = self._day_classifier.classify(_cr_v, _cr_s, _atr, _vix)

            if _day.day_type != self._day_type:
                self._day_type = _day.day_type
                # Adjust trade spacing dynamically
                self.risk.set_trade_spacing(_day.params["trade_spacing_min"])
                logger.info(
                    f"[DAY TYPE] {_day.day_type} | {_day.reason} | "
                    f"spacing={_day.params['trade_spacing_min']}min "
                    f"target={_day.params['default_target_rr']}:1 "
                    f"size={_day.params['size_multiplier']}x "
                    f"rider={'ON' if _day.params['trend_rider_enabled'] else 'OFF'}"
                )
            market["day_type"] = _day.day_type
            market["day_type_reason"] = _day.reason
        except Exception as e:
            logger.debug(f"[DAY TYPE] Non-blocking classification error: {e}")
            market["day_type"] = "UNKNOWN"

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

        # ─── NEW Apr 2026 SHADOW: compute structural_bias composite ─────
        # Runs alongside old tf_bias. Dual-write — does NOT gate strategies.
        try:
            # Enrich market snapshot with new-module outputs
            _enriched = dict(market)
            _enriched["swing_state"] = self.swing_state_5m.to_dict()
            _enriched["volume_profile"] = self.volume_profile.to_dict()
            _enriched["climax_state"] = self.reversal_detector.get_state()
            _enriched["sweep_state"] = self.sweep_watcher.get_state()
            _enriched["footprint_signals"] = self._last_footprint_signals
            # Chart patterns v1: wrap existing detector output with context weighting
            try:
                _cp_state = {"active_5m": [], "active_15m": [], "active_60m": []}
                _chart_pats_v1 = extract_v1_patterns(_cp_state, _enriched)
                self._last_chart_patterns_v1 = [p.to_dict() for p in _chart_pats_v1]
                _enriched["chart_patterns_v1"] = self._last_chart_patterns_v1
            except Exception:
                _enriched["chart_patterns_v1"] = []

            # MenthorQ context enrichment
            try:
                from core.menthorq_feed import get_snapshot as _mq_snap_fn, regime_for_price
                _mq_snap = _mq_snap_fn()
                _mq_price = float(market.get("close", 0) or 0)
                _mq_regime = regime_for_price(_mq_snap, _mq_price) if _mq_snap else {}
                _enriched["menthorq"] = {
                    "gex_regime": getattr(_mq_snap, "gex_regime", "UNKNOWN") if _mq_snap else "UNKNOWN",
                    "hvl": getattr(_mq_snap, "hvl", 0),
                    "call_resistance_all": getattr(_mq_snap, "call_resistance_all", 0),
                    "put_support_all": getattr(_mq_snap, "put_support_all", 0),
                    "call_resistance_0dte": getattr(_mq_snap, "call_resistance_0dte", 0),
                    "put_support_0dte": getattr(_mq_snap, "put_support_0dte", 0),
                    "gamma_wall_0dte": getattr(_mq_snap, "gamma_wall_0dte", 0),
                    "allow_longs": _mq_regime.get("allow_long", True),
                    "allow_shorts": _mq_regime.get("allow_short", True),
                    "age_hours": 0.0,  # MQBridge refreshes every 60s
                }
            except Exception:
                _enriched["menthorq"] = {}

            # Gamma flip state
            _enriched["gamma_flip_state"] = self.gamma_flip_detector.get_state()

            # VIX term structure (cached, refreshes every 10 min)
            try:
                _vix = get_vix_term_cached()
                self._last_vix_term = _vix.to_dict()
                _enriched["vix_term_structure"] = self._last_vix_term
            except Exception:
                _enriched["vix_term_structure"] = {}

            # Pinning state (last 90 min of RTH)
            try:
                _last_5m = list(self.aggregator.bars_5m.completed)[-1] if self.aggregator.bars_5m.completed else None
                _vol_ma = self.aggregator.atr.get("5m", 0) * 1000  # Rough volume baseline
                _pin = self.pinning_detector.update(
                    datetime.now(), _mq_price, _enriched.get("menthorq", {}),
                    _last_5m, _vol_ma
                )
                self._last_pinning_state = {
                    "pin_risk_active": _pin.pin_risk_active,
                    "pinning_level": _pin.pinning_level,
                    "pin_level_name": _pin.pin_level_name,
                    "distance_ticks": _pin.distance_ticks,
                    "reasoning": _pin.reasoning,
                }
                _enriched["pinning_state"] = self._last_pinning_state
            except Exception:
                _enriched["pinning_state"] = {}

            # OpEx status
            try:
                _opex = get_opex_status()
                self._last_opex_status = {
                    "is_opex_day": _opex.is_opex_day,
                    "is_triple_witching": _opex.is_triple_witching,
                    "size_reduction_factor": _opex.size_reduction_factor,
                    "veto_continuation_patterns": _opex.veto_continuation_patterns,
                    "reasoning": _opex.reasoning,
                }
                _enriched["opex_status"] = self._last_opex_status
            except Exception:
                _enriched["opex_status"] = {}

            # ES confirmation
            try:
                _es = check_es_confirmation(_enriched.get("menthorq", {}).get("gex_regime", "UNKNOWN"))
                self._last_es_confirmation = {
                    "aligned": _es.aligned,
                    "confluence_adjust": _es.confluence_adjust,
                    "es_data_available": _es.es_data_available,
                    "reasoning": _es.reasoning,
                    "es_regime": _es.es_regime,
                    "nq_regime": _es.nq_regime,
                }
                _enriched["es_confirmation"] = self._last_es_confirmation
            except Exception:
                _enriched["es_confirmation"] = {}

            # Compute the composite
            bias = compute_structural_bias(_enriched)
            self._last_structural_bias = bias.to_dict()
            # Log periodically (every 10 calls) to avoid noise
            if not hasattr(self, "_bias_log_counter"):
                self._bias_log_counter = 0
            self._bias_log_counter += 1
            if self._bias_log_counter % 10 == 0:
                logger.info(f"[STRUCTURAL BIAS] {bias.label} score={bias.score:+d} "
                            f"conf={bias.confidence}% vetoes={len(bias.vetoes)}")
        except Exception as e:
            logger.debug(f"[STRUCTURAL BIAS] compute error (non-blocking): {e}")

        # Phase 8: Apply playbook strategy overrides based on HMM regime
        try:
            for strat in self.strategies:
                pb_overrides = self.playbook_mgr.get_strategy_overrides(strat.name)
                for k, v in pb_overrides.items():
                    strat.config[k] = v
        except Exception:
            pass

        # ── Day-type strategy suppression ─────────────────────────────
        # On RANGE days bias_momentum underperforms; on VOLATILE days breakouts fail.
        # DayClassifier sets which strategies to suppress for the current day type.
        _day_suppressed = set(self._day_classifier.params.get("suppressed_strategies", []))
        _day_target_rr  = self._day_classifier.params.get("default_target_rr", 0)

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
            # Day-type suppression (RANGE suppresses bias_momentum, VOLATILE suppresses breakouts)
            if strat.name in _day_suppressed:
                logger.info(f"  [{strat.name}] SKIP — suppressed on {self._day_type} day")
                self._last_eval["strategies"].append({
                    "name": strat.name, "result": "SKIP_DAY_TYPE",
                    "reason": f"{self._day_type} day"
                })
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
                    # ── Rider strategies: unlimited target on ALL days ─────────
                    # Target 20:1 = 800 ticks (200 pts) from entry. The OCO bracket
                    # exists as a safety net, not a profit target. Real exits come from:
                    #   - Reversal exit (DOM + wick both confirmed, 10pt+ profit)
                    #   - Stall detector STRONG (trend genuinely exhausted)
                    #   - BE stop → stop_loss at breakeven (worst case: no loss)
                    # Goal: 20-50 pts on range/volatile days, 100+ on trend days.
                    # Not 3-point scalps that leave 100 points on the table.
                    _RIDER_STRATEGIES = {"bias_momentum", "dom_pullback"}
                    if (signal.strategy in _RIDER_STRATEGIES and
                            signal.target_rr < 20.0):
                        signal.target_rr = 20.0
                        signal.confluences.append(
                            "RIDER — target 20:1, reversal+stall exits (not OCO)"
                        )
                    # Standard day-type override for non-rider strategies
                    elif _day_target_rr > 0 and _day_target_rr > signal.target_rr:
                        signal.target_rr = _day_target_rr
                        signal.confluences.append(
                            f"Target widened to {_day_target_rr}:1 ({self._day_type} day)"
                        )
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

        # ── Always capture HTF scanner state (even when no signal fires) ──
        try:
            htf_state = self.htf_scanner.get_state()
            self._last_eval["htf_state"] = htf_state
        except Exception:
            pass

        # ── B14 Phase 4: Gamma-Wall Entry Filter ──────────────────────
        # Reject entries that would immediately push into an opposing
        # gamma wall (within NO_TRADE_INTO_WALL_BUFFER_TICKS, default 12)
        # OR that enter at a countertrend reversal-zone (LONG just below
        # resistance / SHORT just above support — price bounces against
        # the trade). See core/menthorq_gamma.is_entry_into_wall.
        if best_signal is not None and self.gamma_levels is not None:
            from core.menthorq_gamma import is_entry_into_wall
            _price = market.get("price", 0) or 0
            _wall = is_entry_into_wall(_price, best_signal.direction, self.gamma_levels)
            if _wall:
                logger.info(
                    f"[GAMMA_GATE] {best_signal.strategy} {best_signal.direction} "
                    f"@ {_price:.2f}: REJECT — entry too close to {_wall}"
                )
                self.last_rejection = (
                    f"gamma_wall:{_wall} blocked {best_signal.strategy} {best_signal.direction}"
                )
                best_signal = None

        # ── C/R Day Bias Filter ───────────────────────────────────────
        # On strong CONTINUATION/BULLISH C/R days (score >= 4), suppress
        # unconfirmed SHORT signals from counter-trend-prone strategies.
        # These strategies (spring, high_precision) can still fire LONG but
        # should not fade a strong up-day without explicit bearish C/R verdict.
        # Strategies with their own TF gates (bias_momentum, ib_breakout) are exempt.
        _CR_SUPPRESSED_STRATEGIES = {"spring_setup", "high_precision_only"}
        if best_signal and best_signal.direction == "SHORT":
            try:
                cr_verdict = market.get("cr_verdict", "UNKNOWN")
                # Get numeric momentum score from last CR result
                cr_mom_score = 0
                if self._last_cr is not None:
                    cr_mom_score = getattr(self._last_cr, "momentum_score", 0) or 0
                if (cr_verdict == "CONTINUATION" and
                        cr_mom_score >= 4 and
                        best_signal.strategy in _CR_SUPPRESSED_STRATEGIES):
                    block_reason = (f"C/R bias filter: BULLISH CONTINUATION day "
                                    f"(score={cr_mom_score}) — SHORT from {best_signal.strategy} suppressed")
                    logger.info(f"[CR BIAS FILTER] {block_reason}")
                    self.last_rejection = block_reason
                    try:
                        sig_dict = {"direction": best_signal.direction,
                                    "strategy": best_signal.strategy,
                                    "confidence": best_signal.confidence,
                                    "entry_score": best_signal.entry_score,
                                    "reason": best_signal.reason}
                        self.history.log_near_miss(sig_dict, market, f"cr_bias: {block_reason}")
                    except Exception:
                        pass
                    best_signal = None
            except Exception as e:
                logger.debug(f"[CR BIAS FILTER] Non-blocking error: {e}")

        if best_signal:
            # ── Menthor Q Direction Gate ─────────────────────────────
            # Check HVL + GEX regime BEFORE any confidence boosts.
            # This is a hard gate — Menthor Q regime overrides strategy direction.
            try:
                from core.menthorq_feed import get_snapshot, regime_for_price
                mq_snap = get_snapshot()
                mq_regime = regime_for_price(mq_snap, market.get("price", 0))
                self._last_mq_regime = mq_regime  # Store for dashboard

                mq_blocks = False
                if best_signal.direction == "LONG" and not mq_regime.get("allow_long", True):
                    mq_blocks = True
                    block_reason = (f"MenthorQ HVL gate: price below HVL {mq_snap.hvl} "
                                    f"in {mq_snap.gex_regime} gamma regime — LONGs blocked")
                elif best_signal.direction == "SHORT" and not mq_regime.get("allow_short", True):
                    mq_blocks = True
                    block_reason = (f"MenthorQ gate: {mq_snap.gex_regime} gamma, "
                                    f"shorts blocked — regime={mq_snap.direction_bias}")

                if mq_blocks:
                    logger.info(f"[MQ GATE] {block_reason}")
                    self.last_rejection = block_reason
                    try:
                        sig_dict = {"direction": best_signal.direction,
                                    "strategy": best_signal.strategy,
                                    "confidence": best_signal.confidence,
                                    "entry_score": best_signal.entry_score,
                                    "reason": best_signal.reason}
                        self.history.log_near_miss(sig_dict, market, f"mq_gate: {block_reason}")
                    except Exception:
                        pass
                    best_signal = None
                else:
                    # Apply MQ stop multiplier to the signal (wider stops in negative gamma)
                    if mq_regime.get("stop_multiplier", 1.0) > 1.0:
                        best_signal.stop_ticks = int(
                            best_signal.stop_ticks * mq_regime["stop_multiplier"]
                        )
                        best_signal.confluences.append(
                            f"MQ stop widened {mq_regime['stop_multiplier']}x "
                            f"({mq_snap.gex_regime} gamma)"
                        )
                    # Log MQ context as confluence info
                    if mq_snap.gex_regime != "UNKNOWN":
                        best_signal.confluences.append(
                            f"MQ: GEX {mq_snap.gex_regime} ({mq_snap.net_gex_bn:+.1f}B), "
                            f"HVL {mq_snap.hvl}, {mq_snap.direction_bias} bias"
                        )
            except Exception as e:
                logger.debug(f"[MQ GATE] Non-blocking error: {e}")

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
            # DON'T clear _pending_signal here — a prior eval may have set it
            # and the tick loop hasn't consumed it yet (race condition with
            # rapid 1m+5m bar completions).

        # Persist full eval to history (every bar evaluation, signal or not)
        self.history.log_eval(self._last_eval, market)

    # ─── Trade Execution ────────────────────────────────────────────
    async def _enter_trade(self, ws, signal: Signal):
        """Execute entry via bridge → OIF with fill confirmation."""
        # B84 no-new-entries gate. From 15:53 CT until the next globex
        # session opens at 17:00 CT, the bot refuses new positions so
        # we don't take a trade that would immediately be unwound by
        # the 15:54 flatten (or race the NT8 Auto Close at 15:55).
        if self._is_no_new_entries_window():
            logger.info(
                f"[NO_NEW_ENTRIES:{signal.trade_id}] {signal.strategy} "
                f"{signal.direction} @ signal — rejected (within the "
                f"15:53→17:00 CT pre-flatten window)"
            )
            self.last_rejection = "Within 15:53-17:00 CT no-new-entries window"
            return

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
        regime = self.session.get_current_regime()
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

        # ── ATR-Based Stop Loss ──────────────────────────────────────
        # Derive stop distance from current ATR rather than a fixed strategy value.
        # Skipped when signal.atr_stop_override=True — strategy already computed
        # its own ATR stop (e.g. spring_setup anchors to wick extreme, not entry).
        if ATR_STOP_ENABLED and not getattr(signal, "atr_stop_override", False):
            atr_key = f"atr_{ATR_STOP_TF}"
            atr_val = market.get(atr_key, 0) or 0
            if atr_val > 0:
                raw_atr_ticks = atr_val / TICK_SIZE
                atr_stop = int(raw_atr_ticks * ATR_STOP_MULTIPLIER)
                atr_stop = max(ATR_STOP_MIN_TICKS, min(ATR_STOP_MAX_TICKS, atr_stop))
                logger.info(f"[{tid}:ATR_STOP] {ATR_STOP_TF} ATR={atr_val:.2f}pts "
                            f"→ {raw_atr_ticks:.1f}t × {ATR_STOP_MULTIPLIER} "
                            f"= {atr_stop}t (clamped {ATR_STOP_MIN_TICKS}-{ATR_STOP_MAX_TICKS}t) "
                            f"[strategy default was {signal.stop_ticks}t]")
                signal.stop_ticks = atr_stop
            else:
                logger.debug(f"[{tid}:ATR_STOP] {atr_key} not ready — using strategy default "
                             f"({signal.stop_ticks}t)")
        elif getattr(signal, "atr_stop_override", False):
            logger.info(f"[{tid}:ATR_STOP] Skipped — {signal.strategy} computed own ATR stop "
                        f"({signal.stop_ticks}t anchored to wick extreme)")

        # Further adjust stop for volatility regime (HIGH/VERY_HIGH = widen 20-50%)
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

        # B21: pass strategy instance so managed-exit strategies (noise_area)
        # get a risk-reference stop for sizing instead of the structural
        # 150-600t disaster anchor they report.
        _strat_obj = next(
            (s for s in self.strategies if s.name == signal.strategy), None
        )
        contracts = self.risk.calculate_contracts(
            risk_dollars, stop_ticks, strategy=_strat_obj
        )

        # Phase 5: Position scaler — cap contracts by account equity and conditions
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

        # Calculate prices — honor signal's explicit prices if set (ORB, Noise Area)
        tick_value = TICK_SIZE
        if getattr(signal, "stop_price", None) is not None:
            stop_price = signal.stop_price
        elif signal.direction == "LONG":
            stop_price = price - (stop_ticks * tick_value)
        else:
            stop_price = price + (stop_ticks * tick_value)

        # B61: managed-exit signals (noise_area, ORB chandelier) set
        # target_price=None intentionally. If target_rr is also 0, we must NOT
        # synthesize a formula target (that would land at entry). The OCO TP
        # leg will be skipped below (see line 2644 guard).
        _managed_exit_target = (
            getattr(signal, "target_price", None) is None
            and getattr(signal, "target_rr", 0) == 0
        )
        if getattr(signal, "target_price", None) is not None:
            target_price = signal.target_price
        elif _managed_exit_target:
            # Safety-net OCO target far beyond realistic fills (300t = 75pts).
            # The real exit comes from signal.exit_trigger; this is just so
            # the OCO bracket still attaches the STOP leg correctly. Prior
            # behavior synthesized target=entry (via target_rr=0 formula),
            # making the TP leg fill immediately — B61 fix.
            _safety_ticks = 300
            if signal.direction == "LONG":
                target_price = price + (_safety_ticks * tick_value)
            else:
                target_price = price - (_safety_ticks * tick_value)
        elif signal.direction == "LONG":
            target_price = price + (stop_ticks * tick_value * signal.target_rr)
        else:
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
        _tp_str = f"{target_price:.2f}" if target_price is not None else "MANAGED"
        logger.info(f"[INTENT:{tid}] {signal.direction} {contracts}x @ {price:.2f} "
                     f"SL={stop_price:.2f} TP={_tp_str} "
                     f"risk=${risk_dollars} tier={tier} strat={signal.strategy}")

        # B62 Universal sanity gate — fail-closed geometry & distance check.
        # Runs AFTER stop/target resolution, BEFORE any OCO submission.
        _ok, _reason = _sanity_check_entry(signal, price, stop_price, target_price)
        if not _ok:
            logger.critical(f"[STOP_SANITY_FAIL:{tid}] {signal.strategy} "
                            f"{signal.direction}: {_reason}")
            self.last_rejection = f"STOP_SANITY_FAIL: {_reason}"
            try:
                sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                            "confidence": signal.confidence, "entry_score": signal.entry_score,
                            "reason": signal.reason}
                self.history.log_near_miss(sig_dict, market, f"stop_sanity_fail:{_reason}")
            except Exception:
                pass
            return

        # Send trade command to bridge (bridge writes OIF with OCO brackets)
        action = "ENTER_LONG" if signal.direction == "LONG" else "ENTER_SHORT"

        # Determine order type — signal override wins over global config
        signal_entry_type = getattr(signal, "entry_type", None) or ENTRY_ORDER_TYPE
        signal_entry_type = signal_entry_type.upper()

        # Signal may provide an explicit entry_price (ORB STOPMARKET at OR, Noise Area LIMIT at break)
        sig_entry_price = getattr(signal, "entry_price", None)

        if signal_entry_type == "LIMIT":
            if sig_entry_price is not None:
                limit_price = round(sig_entry_price, 2)
            else:
                offset = LIMIT_OFFSET_TICKS * TICK_SIZE
                if signal.direction == "LONG":
                    limit_price = round(price + offset, 2)
                else:
                    limit_price = round(price - offset, 2)
            # Realign stop/target to the limit fill price ONLY if the signal didn't
            # compute them itself (ORB + Noise Area pre-compute exact prices).
            if getattr(signal, "stop_price", None) is None:
                if signal.direction == "LONG":
                    stop_price = round(limit_price - (stop_ticks * TICK_SIZE), 2)
                else:
                    stop_price = round(limit_price + (stop_ticks * TICK_SIZE), 2)
            if getattr(signal, "target_price", None) is None and not _managed_exit_target:
                if signal.direction == "LONG":
                    target_price = round(limit_price + (stop_ticks * TICK_SIZE * signal.target_rr), 2)
                else:
                    target_price = round(limit_price - (stop_ticks * TICK_SIZE * signal.target_rr), 2)
            elif _managed_exit_target:
                # Re-anchor safety-net target to the limit fill price
                _safety_ticks = 300
                if signal.direction == "LONG":
                    target_price = round(limit_price + (_safety_ticks * TICK_SIZE), 2)
                else:
                    target_price = round(limit_price - (_safety_ticks * TICK_SIZE), 2)
        elif signal_entry_type == "STOPMARKET":
            # Breakout entry: trigger when price crosses entry_price, fills at market.
            limit_price = round(sig_entry_price if sig_entry_price is not None else price, 2)
        else:  # MARKET
            limit_price = 0.0

        # Phase 4C: resolve NT8 account for this signal so the OIF writer
        # routes fills to the per-strategy sim account instead of Sim101.
        # B57: if the bot class defines FORCE_ACCOUNT, override routing —
        # prod_bot uses this to pin everything to Sim101 (single-account
        # P&L tracking for the first-go-live candidate).
        from config.account_routing import get_account_for_signal
        _sub_strategy = (getattr(signal, "metadata", {}) or {}).get("sub_strategy")
        _force = getattr(self, "FORCE_ACCOUNT", None)
        if _force:
            _account = _force
            logger.debug(f"[ROUTING] {self.bot_name}: FORCE_ACCOUNT -> {_account} "
                         f"(strategy={signal.strategy}, sub={_sub_strategy})")
        else:
            _account = get_account_for_signal(signal.strategy, _sub_strategy)

        # Fix A (2026-04-23): if there's a pending LIMIT entry still
        # working on this account, do not fire another entry — NT8 would
        # reject the second one as "Exceeds account's maximum position
        # quantity". Reconciliation (running every 30s) will adopt the
        # first entry once it fills. Pre-fix, this branch didn't exist:
        # the ENTRY_PENDING branch discarded its own record, so every new
        # spring_setup / vwap_pullback signal fired another entry that
        # NT8 rejected.
        try:
            if self.positions.has_pending_entry(_account):
                _pending = self.positions.get_pending_entry(_account)
                _age = int(time.time() - _pending["submitted_at"]) if _pending else -1
                logger.info(
                    f"[ENTRY_GATE:{self.bot_name}] {signal.strategy} "
                    f"{signal.direction} → {_account} SKIPPED: pending "
                    f"LIMIT entry in flight (trade={_pending['trade_id']}, "
                    f"age={_age}s). Waiting for fill or expiry."
                )
                self.last_rejection = (
                    f"Pending entry in flight on {_account}"
                )
                return
        except Exception as _e:
            logger.debug(f"[ENTRY_GATE] pending-entry check failed: {_e!r}")

        # B59 hard-guard: never route to the live account. If a future
        # routing bug or config error tries to, abort the signal loudly
        # instead of submitting a real-money trade.
        _live = os.environ.get("LIVE_ACCOUNT", "").strip()
        if _live and str(_account).strip() == _live:
            logger.critical(
                f"[LIVE_GUARD] {self.bot_name} BLOCKED {signal.strategy} "
                f"{signal.direction} signal — resolved to live account "
                f"'{_account}'. Entry aborted. Check FORCE_ACCOUNT and "
                f"config/account_routing.py."
            )
            self.last_rejection = f"LIVE_GUARD: refused to route to {_account}"
            try:
                from core.telegram_notifier import send_sync
                send_sync(
                    f"🛑 [LIVE_GUARD] {self.bot_name} BLOCKED {signal.strategy} "
                    f"routed to LIVE account '{_account}'. Entry aborted.",
                    dedup_key=f"live_guard:{self.bot_name}:{signal.strategy}",
                )
            except Exception:
                pass
            return

        # B50: pre-entry position-reconcile guard (inverse phantom).
        # If NT8 already reports a position on this account (e.g. Python
        # state was lost on restart but NT8 still holds the real fill),
        # abort the new entry — otherwise NT8 rejects with "Exceeds
        # account's maximum position quantity" and leaves orphan OCO legs.
        if (_account and _account != "Sim101"):
            try:
                from bridge.oif_writer import verify_nt8_position
                pre = verify_nt8_position(
                    account=_account, expected_direction="FLAT",
                    expected_qty=0, timeout_s=0.5,
                )
                if pre["status"] not in ("flat", "missing"):
                    logger.warning(
                        f"[PREENTRY_SKIP:{tid}] NT8 already has position on "
                        f"{_account}: {pre.get('observed_direction')} "
                        f"{pre.get('observed_qty')} @ {pre.get('observed_price')}. "
                        f"Skipping {signal.strategy} {signal.direction} entry "
                        f"(inverse-phantom guard)."
                    )
                    self.last_rejection = (
                        f"Pre-entry skip: {_account} already "
                        f"{pre.get('observed_direction')} {pre.get('observed_qty')}"
                    )
                    return
            except Exception as e:
                logger.debug(f"[PREENTRY:{tid}] reconcile check failed "
                             f"(non-blocking): {e}")

        # B55: split bracket submit — ENTRY first, stop+target AFTER fill.
        # Prior behavior submitted entry + OCO stop/target in one burst; NT8
        # rejected protection legs with "Exceeds account's maximum position
        # quantity" because it counts pending sides before the entry fills.
        # Now we submit entry alone, wait for fill confirmation, then attach
        # OCO protection to the filled position.
        try:
            await ws.send(json.dumps({
                "type": "trade",
                "trade_id": tid,
                "action": action,
                "qty": contracts,
                # stop/target DELIBERATELY OMITTED — sent post-fill below
                "stop_price": None,
                "target_price": None,
                "reason": signal.reason,
                "order_type": signal_entry_type,
                "limit_price": limit_price,
                "account": _account,
                "sub_strategy": _sub_strategy,
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
            if LIVE_TRADING:
                # LIVE mode: DO NOT proceed without fill confirmation
                logger.error(f"[{tid}] Fill timeout in LIVE mode — ABORTING entry. "
                              f"Check NT8 manually for order status.")
                self.last_rejection = f"Fill timeout in LIVE mode — entry aborted"
                return
            # B39 hardening (B48 refinement): on sim bot, distinguish
            #   (a) NT8 REJECTED order (OIF still in incoming/) → phantom risk,
            #       loud alert + cleanup stop/target orphans
            #   (b) NT8 ACCEPTED but waiting for limit/stop trigger (OIF
            #       consumed, no fill yet) → NOT a phantom, skip quietly
            if (_account and _account != "Sim101"):
                import glob as _glob
                try:
                    from config.settings import NT8_DATA_ROOT
                    incoming_dir = os.path.join(NT8_DATA_ROOT, "incoming")
                except Exception:
                    incoming_dir = r"C:\Users\Trading PC\Documents\NinjaTrader 8\incoming"
                # Any of this trade's OIFs still sitting in incoming/?
                stuck = _glob.glob(os.path.join(incoming_dir, f"*_{tid}*.txt"))
                if stuck:
                    # Case (a): NT8 rejected — real phantom risk
                    logger.error(f"[PHANTOM_GUARD:{tid}] NT8 REJECTED order — "
                                  f"{len(stuck)} OIF(s) stuck in incoming/. "
                                  f"Aborting entry + removing stuck legs. "
                                  f"Account={_account} strategy={signal.strategy}")
                    for p in stuck:
                        try: os.remove(p)
                        except OSError: pass
                    self.last_rejection = (
                        f"Phantom-guard: NT8 rejected {_account}/{signal.strategy}"
                    )
                    try:
                        from core.telegram_notifier import send_sync
                        send_sync(
                            f"⚠️ [PHANTOM_GUARD] {signal.strategy} → {_account} "
                            f"NT8 REJECTED order ({len(stuck)} OIF stuck). "
                            f"Check NT8 Log tab.",
                            dedup_key=f"phantom_guard:{signal.strategy}:{_account}",
                        )
                    except Exception:
                        pass
                    return
                # Case (b): order accepted, waiting for trigger. Clean up
                # orphan stop/target legs (they'll have no position to protect
                # if entry never fills) and skip quietly.
                orphans = _glob.glob(os.path.join(incoming_dir, f"*_{tid}_stop.txt"))
                orphans += _glob.glob(os.path.join(incoming_dir, f"*_{tid}_target.txt"))
                for p in orphans:
                    try: os.remove(p)
                    except OSError: pass
                # Fix A (2026-04-23): record the pending limit so the next
                # signal on this account sees it and skips. Pre-fix the bot
                # would fire a second LIMIT BUY on the same account while
                # the first was still working → NT8 rejected "Exceeds
                # account's maximum position quantity". Reconciliation
                # (Fix B + C) adopts the position when it actually fills.
                try:
                    self.positions.record_pending_entry(
                        account=_account,
                        trade_id=tid,
                        strategy=signal.strategy,
                        direction=signal.direction,
                        limit_price=limit_price,
                        qty=contracts,
                    )
                except Exception as _e:
                    logger.debug(f"[PENDING_ENTRY:{tid}] record failed: {_e}")
                logger.info(f"[ENTRY_PENDING:{tid}] NT8 accepted entry @ "
                             f"{limit_price:.2f} on {_account}, waiting for trigger. "
                             f"Skipping Python open (re-eval next tick). "
                             f"Cleaned {len(orphans)} orphan OCO legs.")
                return
            # Paper mode (prod_bot with LIVE_TRADING=False): keep legacy
            # "assume filled" behavior for Sim101-only mock tracking.
            logger.info(f"[{tid}] No fill file (paper mode) — assuming filled")

        # Inject regime and Phase 6b data into market snapshot for analytics
        market["regime"] = self.session.get_current_regime()
        market["signal_price"] = price  # Price at signal generation time
        market["microstructure"] = micro_result
        market["fill_latency_ms"] = fill_result.get("latency_ms", 0)

        # B47: For sim_bot, verify the fill actually happened by reading
        # NT8's outgoing/ position file for this account. If NT8 reports
        # FLAT or wrong direction/qty, we have a phantom — reject the entry.
        if (_account and _account != "Sim101"):
            try:
                from bridge.oif_writer import verify_nt8_position
                pos_check = verify_nt8_position(
                    account=_account,
                    expected_direction=signal.direction,
                    expected_qty=contracts,
                    timeout_s=3.0,
                )
                if pos_check["status"] != "confirmed":
                    logger.error(
                        f"[NT8_VERIFY:{tid}] Fill verification FAILED: "
                        f"status={pos_check['status']} "
                        f"observed={pos_check.get('observed_direction')}"
                        f"/{pos_check.get('observed_qty')} "
                        f"@ {pos_check.get('observed_price')} — aborting entry."
                    )
                    self.last_rejection = (
                        f"NT8 position verify {pos_check['status']} on {_account}"
                    )
                    try:
                        from core.telegram_notifier import send_sync
                        send_sync(
                            f"⚠️ [NT8_VERIFY] {signal.strategy} → {_account}: "
                            f"NT8 reports {pos_check['status']} after entry. "
                            f"Expected {signal.direction}/{contracts}, got "
                            f"{pos_check.get('observed_direction')}"
                            f"/{pos_check.get('observed_qty')}. Entry aborted.",
                            dedup_key=f"nt8_verify:{signal.strategy}:{_account}:{pos_check['status']}",
                        )
                    except Exception:
                        pass
                    return
                logger.info(f"[NT8_VERIFY:{tid}] Position confirmed: "
                            f"{pos_check['observed_direction']} {pos_check['observed_qty']} "
                            f"@ {pos_check['observed_price']}")
            except Exception as e:
                logger.warning(f"[NT8_VERIFY:{tid}] verify failed (non-blocking): {e}")

        # B55: Attach OCO stop + target NOW (post-fill, post-verify).
        # Retry up to 3 times with 1s backoff. If all attempts fail —
        # UNPROTECTED POSITION is in NT8 — FLATTEN immediately to prevent
        # unbounded loss, then loud-alert.
        if stop_price and target_price:
            # Phase B+ Sink-mediated PROTECT. PHOENIX_RISK_GATE=0 -> identical
            # behavior to the legacy write_protection_oco call.
            protection_ok = False
            for attempt in range(1, 4):
                try:
                    resp = _sink_submit_protect(
                        direction=signal.direction,
                        qty=contracts,
                        stop_price=round(stop_price, 2),
                        target_price=round(target_price, 2),
                        trade_id=f"{tid}_protect{attempt}",
                        account=_account,
                    )
                    paths = resp.get("oif_paths") or (
                        [resp["oif_path"]] if resp.get("decision") == "ACCEPT"
                        and resp.get("oif_path") else []
                    )
                    if resp.get("decision") == "REFUSE":
                        logger.warning(
                            f"[RISK_GATE] PROTECT refused for {tid} "
                            f"(attempt {attempt}): {resp.get('reason')}"
                        )
                    if paths:
                        logger.info(
                            f"[PROTECT:{tid}] OCO attached on attempt #{attempt} "
                            f"stop={stop_price:.2f} target={target_price:.2f}"
                        )
                        # B76: propagate captured order_ids from the protect
                        # trade_id key to the canonical tid for later pickup.
                        try:
                            from bridge.oif_writer import _recent_order_ids
                            _pk = f"{tid}_protect{attempt}"
                            if _pk in _recent_order_ids:
                                _recent_order_ids[tid] = _recent_order_ids[_pk]
                        except Exception:
                            pass
                        protection_ok = True
                        break
                    logger.warning(
                        f"[PROTECT:{tid}] attempt #{attempt} — NT8 did not "
                        f"consume OCO legs, retrying in 1s"
                    )
                    await asyncio.sleep(1.0)
                except Exception as e:
                    logger.error(f"[PROTECT:{tid}] attempt #{attempt} error: {e}")
                    await asyncio.sleep(1.0)

            if not protection_ok:
                # CRITICAL: unprotected position in NT8. Flatten before it
                # bleeds. Send a CLOSEPOSITION + Telegram alert.
                logger.critical(
                    f"[PROTECT:{tid}] ALL 3 RETRIES FAILED — flattening "
                    f"unprotected {signal.direction} position on {_account}"
                )
                try:
                    # Sink-mediated emergency flatten. With the gate engaged,
                    # an EXIT op should always be ACCEPT'd (the gate doesn't
                    # block close-position orders); fail-soft fallback covers
                    # the case where the gate is unreachable.
                    _ef_resp = _sink_submit_exit(
                        qty=contracts,
                        trade_id=f"{tid}_emergency_flatten",
                        account=_account,
                        reason="UNPROTECTED_FLATTEN",
                    )
                    if _ef_resp.get("decision") != "ACCEPT":
                        logger.critical(
                            f"[PROTECT:{tid}] EMERGENCY FLATTEN refused by "
                            f"sink {_ef_resp.get('sink','?')}: "
                            f"{_ef_resp.get('reason','?')}"
                        )
                except Exception as e:
                    logger.critical(f"[PROTECT:{tid}] EMERGENCY FLATTEN FAILED: {e}")
                try:
                    from core.telegram_notifier import send_sync
                    send_sync(
                        f"🚨 [UNPROTECTED] {signal.strategy} → {_account}: "
                        f"OCO failed 3× post-fill. FLATTENING position "
                        f"({signal.direction} {contracts}@{pos_check.get('observed_price') if 'pos_check' in dir() else '?'}). "
                        f"Check NT8.",
                        dedup_key=f"unprotected:{signal.strategy}:{_account}",
                    )
                except Exception:
                    pass
                self.last_rejection = f"Unprotected position flattened: {_account}"
                return

        # NOW open position locally (after fill confirmation)
        # LIMIT / STOPMARKET entries fill at limit_price; MARKET fills near tick price.
        effective_entry_price = (
            limit_price if (signal_entry_type in ("LIMIT", "STOPMARKET") and limit_price > 0)
            else price
        )
        self.positions.open_position(
            trade_id=tid,
            direction=signal.direction,
            entry_price=effective_entry_price,
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            strategy=signal.strategy,
            reason=signal.reason,
            market_snapshot=market,
            exit_trigger=getattr(signal, "exit_trigger", None),
            eod_flat_time_et=getattr(signal, "eod_flat_time_et", None),
            metadata=dict(getattr(signal, "metadata", {}) or {}),
            scale_out_rr=getattr(signal, "scale_out_rr", None),
            trail_config=getattr(signal, "trail_config", None),
            account=_account,
            sub_strategy=_sub_strategy,
        )

        # Fix A: entry actually filled (direct fill path) — clear any
        # pending-entry record for this account.
        try:
            self.positions.clear_pending_entry(_account)
        except Exception:
            pass

        # B76: stash NT8-assigned order_ids on the freshly-opened Position
        # so subsequent stop-moves (trail / BE / chandelier) can cancel +
        # replace by id. Best-effort: if capture missed (NT8 outgoing/ not
        # yet populated), base_bot logs [STOP_MOVE_NO_ID] at move time.
        try:
            from bridge.oif_writer import _recent_order_ids, scan_outgoing_for_order_id
            _ids = _recent_order_ids.pop(tid, None)
            if _ids is None:
                _ids = {}
                if stop_price:
                    _so = scan_outgoing_for_order_id(_account, stop_price, timeout_s=0.5)
                    if _so:
                        _ids["stop"] = _so
                if target_price:
                    _to = scan_outgoing_for_order_id(_account, target_price, timeout_s=0.5)
                    if _to:
                        _ids["target"] = _to
            _new_pos = self.positions.get_position(tid)
            if _new_pos is not None and _ids:
                _new_pos.stop_order_id = _ids.get("stop", "") or ""
                _new_pos.target_order_id = _ids.get("target", "") or ""
                logger.info(
                    f"[OID_CAPTURE:{tid}] stop_oid={(_new_pos.stop_order_id or 'MISS')[:12]} "
                    f"target_oid={(_new_pos.target_order_id or 'MISS')[:12]}"
                )
            else:
                logger.warning(f"[OID_CAPTURE:{tid}] no order_ids captured — "
                               f"stop-moves will fall back to Python-only")
        except Exception as _e:
            logger.warning(f"[OID_CAPTURE:{tid}] failed: {_e}")

        # ── B70: directional conflict observability (non-blocking) ──────
        # Detect cross-strategy LONG-vs-SHORT, log to jsonl, dedup-alert.
        # Conflicts are ALLOWED — data-gathering only.
        try:
            from core.strategy_risk_registry import StrategyRiskRegistry
            from core import conflict_logger as _cflog
            _reg = getattr(self, "_conflict_reg", None)
            if _reg is None:
                _reg = StrategyRiskRegistry()
                self._conflict_reg = _reg
            all_conflicts = _reg.detect_directional_conflicts(self.positions)
            # Filter to only pairs that include the just-opened trade.
            involved = [c for c in all_conflicts
                        if tid in (c["trade_id_a"], c["trade_id_b"])]
            if involved:
                exposure = _reg.exposure_snapshot(self.positions)
                new_pos = self.positions.get_position(tid)
                _cflog.log_conflict_opened(new_pos, involved, exposure)
                # Dedup Telegram alert: alphabetically sort pair names.
                try:
                    from core.telegram_notifier import send_sync
                    pair_names = sorted({c["strategy_a"] for c in involved} |
                                         {c["strategy_b"] for c in involved})
                    # Pick the primary pair involving the new entry for the
                    # human-readable line.
                    c0 = involved[0]
                    if c0["trade_id_a"] == tid:
                        new_s, new_d, new_e = c0["strategy_a"], c0["dir_a"], c0["entry_a"]
                        oth_s, oth_d, oth_e = c0["strategy_b"], c0["dir_b"], c0["entry_b"]
                    else:
                        new_s, new_d, new_e = c0["strategy_b"], c0["dir_b"], c0["entry_b"]
                        oth_s, oth_d, oth_e = c0["strategy_a"], c0["dir_a"], c0["entry_a"]
                    sorted_pair = "-".join(sorted([new_s, oth_s]))
                    send_sync(
                        f"⚠️ CONFLICT | {new_s} {new_d} @ {new_e:.2f} vs "
                        f"{oth_s} {oth_d} @ {oth_e:.2f}\n"
                        f"Both positions active. Net exposure: "
                        f"{exposure.get('net')}. Allowing (data mode).",
                        dedup_key=f"conflict_opened:{sorted_pair}",
                    )
                except Exception:
                    pass
        except Exception as _e:
            logger.warning(f"[CONFLICT] post-open detection failed: {_e}")

        # Reset stall detector for fresh rider tracking on this trade
        self._stall_detector.reset()
        self._rider_active = False

        # ── Rider mode: active on ALL days for rider-eligible strategies ──────
        # bias_momentum and dom_pullback always use rider mode — stall detector
        # + reversal exit (DOM + wick confirmed) are the exit mechanism.
        # Smart exit is disabled for these strategies on every day type.
        # Goal: hold for 20-50 pts on range days, 100+ on trend days.
        _RIDER_STRATEGIES = {"bias_momentum", "dom_pullback"}
        if TREND_RIDER_ENABLED and signal.strategy in _RIDER_STRATEGIES:
            _pos = self.positions.position
            if _pos:
                _pos.rider_mode = True
                _be_level = (_pos.entry_price + abs(_pos.entry_price - _pos.stop_price)
                             if _pos.direction == "LONG"
                             else _pos.entry_price - abs(_pos.entry_price - _pos.stop_price))
                logger.info(f"[{tid}] RIDER ON ({self._day_type} day) — "
                            f"smart exit OFF, reversal+stall exit driving. "
                            f"Entry={_pos.entry_price:.2f} stop={_pos.stop_price:.2f} "
                            f"BE@{_be_level:.2f} (+{abs(_pos.entry_price-_pos.stop_price)/TICK_SIZE:.0f}t = "
                            f"{abs(_pos.entry_price-_pos.stop_price):.2f}pts)")
        else:
            # Non-rider strategies (spring_setup, ib_breakout, etc.) — fixed target mode
            _mom_score = getattr(self._last_cr, "momentum_score", 0) if self._last_cr else 0
            _cr_verdict = getattr(self._last_cr, "verdict", "UNKNOWN") if self._last_cr else "UNKNOWN"
            if SCALE_OUT_ENABLED and contracts >= 2:
                logger.info(f"[{tid}] Scale-out eligible: contracts={contracts} "
                            f"cr_verdict={_cr_verdict} mom_score={_mom_score} "
                            f"(rider triggers at RR={SCALE_OUT_RR})")
            else:
                logger.info(f"[{tid}] Fixed target mode ({signal.strategy}): "
                            f"{contracts}ct, target_rr={signal.target_rr:.1f}")

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

    async def _scale_out_trade(self, ws, price: float):
        """
        Trend rider scale-out: exit 1 contract at SCALE_OUT_RR, keep runner.

        1. Cancel NT8 OCO brackets
        2. Write partial exit OIF (sell/buy 1 contract at market)
        3. Record partial P&L
        4. Move stop to break-even
        5. Place new BE stop order in NT8
        6. Activate rider mode — stall detector now owns the exit
        """
        pos = self.positions.position
        if not pos or pos.scaled_out or pos.contracts < 2:
            return

        tid = pos.trade_id
        n_exit = 1   # Always exit 1 contract, keep remainder running

        # Check momentum score — only ride when score >= threshold
        mom_score = 0
        try:
            if self._last_cr:
                mom_score = self._last_cr.momentum_score
        except Exception:
            pass

        rider_eligible = (mom_score >= TREND_RIDER_MIN_SCORE or
                          (self._last_cr and self._last_cr.verdict == "CONTINUATION"))

        if not rider_eligible:
            logger.info(f"[SCALE_OUT:{tid}] Score {mom_score} < {TREND_RIDER_MIN_SCORE} "
                        f"— using full exit instead of scale-out")
            # Fall through to normal target_hit exit; don't scale
            return

        logger.info(f"[SCALE_OUT:{tid}] Initiating: price={price:.2f} "
                    f"dir={pos.direction} contracts={pos.contracts} "
                    f"mom_score={mom_score}")

        # STEP 1: SKIP pre-exit CANCEL_ALL (B75).
        # Pre-B75 we sent account-scoped CANCELALLORDERS here to clear
        # the OCO bracket before submitting new orders. Problem: NT8 ATI
        # IGNORES the account field on CANCELALLORDERS and nukes every
        # pending order on every connected account, wiping sim_bot's
        # OCO protection on unrelated positions (root cause of the
        # 2026-04-22 orphan-long incidents on SimSpring Setup + SimNoise
        # Area). Solution: don't explicitly cancel. When we send the
        # partial-exit MARKET order below, position contracts decrease
        # from N to N-n_exit. The existing OCO stop/target remain but
        # will now target a smaller position; NT8 automatically adjusts
        # the OCO qty to match the remaining position size. If qty
        # reaches zero, NT8 auto-cancels the OCO.

        # STEP 2: Write partial exit OIF (exit n_exit contracts at market)
        # Sink-mediated PARTIAL_EXIT — DirectFileSink delegates straight to
        # bridge.oif_writer.write_partial_exit when the gate flag is off.
        try:
            _se_resp = _sink_submit_partial_exit(
                direction=pos.direction,
                n_contracts=n_exit,
                trade_id=f"{tid}_scale1",
                account=pos.account,
            )
            if _se_resp.get("decision") != "ACCEPT":
                logger.error(
                    f"[SCALE_OUT:{tid}] Partial exit refused by sink "
                    f"{_se_resp.get('sink','?')}: {_se_resp.get('reason','?')}"
                )
                return
        except Exception as e:
            logger.error(f"[SCALE_OUT:{tid}] Partial exit OIF failed: {e}")
            return

        # STEP 3: Record partial P&L in Python position manager
        partial = self.positions.scale_out_partial(price, n_exit, "scale_out_target")
        if partial:
            self.risk.record_trade(partial["pnl_dollars"])
            self.trade_memory.record(partial, bot_id=self.bot_name)
            logger.info(f"[SCALE_OUT:{tid}] Partial P&L: ${partial['pnl_dollars']:.2f} "
                        f"({partial['pnl_ticks']:.1f}t)")

        # STEP 4: Move stop to break-even in Python
        be_price = pos.entry_price
        self.positions.move_stop_to_be(be_price)

        # STEP 5: Move the EXISTING OCO stop to break-even via the B76
        # cancel+replace flow. Do NOT use write_be_stop (which only PLACES
        # a new stop without cancelling the old one) — that leaves TWO
        # stops on the account:
        #   - the original OCO stop (auto-reduced by NT8 from qty=2 to
        #     qty=1 after the partial-exit market order filled), still at
        #     the original entry-stop_price
        #   - the new BE stop at entry_price
        # If the market moves adversely, the BE stop fires for qty=1,
        # CLOSING the position. But the OCO stop is still working — if
        # price bounces and hits IT, NT8 places a REVERSAL fill. That's
        # the orphan-phantom-trade signature from the 2026-04-22 incident.
        # P0.5 (D4) fix: use _move_nt8_stop → write_modify_stop, which
        # stages PLACE-new-stop + CANCEL-old-stop atomically (PLACE first,
        # CANCEL second — the safe ordering; see write_modify_stop docstring
        # for the full hierarchy).
        try:
            _move_nt8_stop(pos, pos.entry_price, be_price)
        except Exception as e:
            logger.warning(f"[SCALE_OUT:{tid}] BE stop-modify failed (non-blocking): {e}")

        # STEP 6: Activate rider mode — stall detector now owns the exit
        pos.rider_mode = True
        self._rider_active = True
        self._stall_detector.reset()  # Fresh stall tracking for the runner

        self.status = "IN_TRADE"
        logger.info(f"[SCALE_OUT:{tid}] Complete — {pos.contracts}x running "
                    f"BE@{be_price:.2f}, stall detector active")

        asyncio.ensure_future(tg.notify_alert(
            "SCALE OUT - RIDER ACTIVE",
            f"{pos.direction} partial exit {n_exit}x @ {price:.2f} "
            f"(+${partial['pnl_dollars']:.2f})\n"
            f"Runner: {pos.contracts}x | BE stop @ {be_price:.2f} | "
            f"Momentum score: {mom_score}"
        ))

    def _on_trade_closed(self, trade: dict) -> None:
        """
        Shadow-module wiring at trade close (P3 stub for P10a full wiring).

        Feeds circuit_breakers' rolling counters so breaker detection
        (slippage spike, WR crash) has data to work with on the next tick.
        Called from _exit_trade after positions.close_position() returns.

        Currently wires 2 of 5 shadow-module consumers. The remaining 3
        (decay_monitor.record_trade, sweep_watcher.track_pivot_break,
        tca_tracker.record_fill) will wire here during P10a/b/c on Day 7+.

        trade.get('slippage_ticks', 0) is a placeholder — the trade dict
        does not yet carry a slippage field. P10a will compute slippage
        from (entry_price vs market_snapshot['signal_price']) and attach
        it to the trade dict before this method is called.
        """
        if not trade:
            return
        try:
            self.circuit_breakers.record_slippage(trade.get("slippage_ticks", 0))
        except Exception as e:
            logger.debug(f"[_on_trade_closed] record_slippage error (non-blocking): {e}")
        try:
            self.circuit_breakers.record_trade_outcome(trade.get("result", "UNKNOWN"))
        except Exception as e:
            logger.debug(f"[_on_trade_closed] record_trade_outcome error (non-blocking): {e}")

    async def _exit_trade(self, ws, price: float, reason: str,
                          trade_id: str | None = None):
        """Execute exit: send to NT8 FIRST, then close Python state.

        Phase C: trade_id optional. When supplied, exits that specific
        position (used by the multi-position tick-exit iteration at the
        top of _connect_and_listen). When None, falls back to the sole
        active position (legacy single-position behavior).
        """
        if self.positions.is_flat:
            return

        if trade_id is not None:
            pos = self.positions.get_position(trade_id)
        else:
            pos = self.positions.position
        if pos is None:
            return
        tid = pos.trade_id

        # 2026-04-24 debounce. On a reconciled-phantom position at 09:29:07
        # the exit path fired hundreds of times in 500ms (see
        # logs/sim_bot_stdout.log: 94,433 EXIT_PENDING lines lifetime). Once
        # an exit is in flight for a trade_id, suppress duplicate sends for
        # 2 seconds — runtime reconciliation (every 30s) is the proper
        # driver for unwinding pending exits, not the tick-exit loop.
        if not hasattr(self, "_last_exit_send_ts"):
            self._last_exit_send_ts: dict[str, float] = {}
        _now = time.time()
        _prev = self._last_exit_send_ts.get(tid, 0.0)
        if _now - _prev < 2.0:
            logger.debug(f"[EXIT_PENDING:{tid}] debounce skip — last send {_now - _prev:.2f}s ago")
            return
        self._last_exit_send_ts[tid] = _now

        self.status = "EXIT_PENDING"
        logger.info(f"[EXIT_PENDING:{tid}] Sending exit for {pos.direction} @ {price:.2f}, reason={reason}")

        # STEP 1: B75 — SKIP pre-exit CANCELALLORDERS. Rely on NT8 OCO
        # auto-cancel: when the EXIT MARKET order fills, position goes
        # flat, and NT8 automatically cancels the orphaned OCO stop +
        # target because their OCO group detects position closure.
        # Pre-B75 behavior (CANCELALLORDERS before EXIT) wiped OCOs on
        # every connected account because NT8 ATI ignores the account
        # field on CANCELALLORDERS. Send EXIT directly — OCO cleans up.
        exit_sent = False
        try:
            await ws.send(json.dumps({
                "type": "trade", "trade_id": tid,
                "action": "EXIT", "qty": pos.contracts,
                "account": pos.account,
                "reason": reason,
            }))
            exit_sent = True
        except Exception as e:
            logger.error(f"[EXIT:{tid}] WS send failed: {e} — writing OIF fallback")
            try:
                # Sink-mediated EXIT fallback. Identical to the legacy
                # write_oif('EXIT', ...) call when PHOENIX_RISK_GATE=0.
                _ex_resp = _sink_submit_exit(
                    qty=pos.contracts, trade_id=tid, account=pos.account,
                    reason=reason,
                )
                if _ex_resp.get("decision") == "ACCEPT":
                    exit_sent = True
                else:
                    logger.error(
                        f"[EXIT:{tid}] sink {_ex_resp.get('sink','?')} "
                        f"REFUSED EXIT: {_ex_resp.get('reason','?')}"
                    )
            except Exception as e2:
                logger.error(f"[EXIT:{tid}] OIF fallback ALSO failed: {e2} — MANUAL EXIT NEEDED")
                asyncio.ensure_future(tg.notify_alert(
                    "CRITICAL: EXIT FAILED",
                    f"Trade {tid} exit failed. Position may still be open in NT8.\n"
                    f"MANUAL EXIT REQUIRED."))

        # STEP 2: NOW close Python position (after NT8 command sent)
        # Reset rider state regardless of outcome
        self._rider_active = False
        pos.rider_mode = False if self.positions.position else False

        # B70: capture pre-close conflict state so we can emit a
        # conflict_closed event if this exit resolves a conflict pair.
        _pre_close_conflicts: list[dict] = []
        _closing_pos_snapshot = None
        try:
            from core.strategy_risk_registry import StrategyRiskRegistry
            _reg = getattr(self, "_conflict_reg", None)
            if _reg is None:
                _reg = StrategyRiskRegistry()
                self._conflict_reg = _reg
            _pre_close_conflicts = _reg.detect_directional_conflicts(self.positions)
            _closing_pos_snapshot = self.positions.get_position(tid)
        except Exception:
            pass

        # P0.6 (D7): mark the position exit_pending and let runtime
        # reconciliation finalize it only when NT8 confirms FLAT. Before
        # P0.6, close_position was called unconditionally — Python
        # thought the position was closed even if NT8 never filled the
        # EXIT (e.g. ATI rejection, bridge crash post-send). That left
        # dashboards showing flat while a bleeding position sat on NT8.
        #
        # Fallback: if exit WS-send failed AND OIF fallback also failed
        # (exit_sent=False), we STILL close the Python position to avoid
        # leaking a Position record nobody will ever close — but we log
        # CRITICAL because this is the manual-exit-required scenario.
        if exit_sent:
            self.positions.mark_exit_pending(tid, price, reason)
            trade = None  # finalized later by runtime reconciliation
        else:
            logger.critical(
                f"[EXIT_FORCE_CLOSE:{tid}] NT8 exit send failed; force-"
                f"closing Python state to avoid leaked Position record. "
                f"Operator MUST verify NT8 is flat on {pos.account}."
            )
            trade = self.positions.close_position(price, reason, trade_id=tid)

        # B70: if the closing trade was in any conflict pair, log it.
        try:
            if _pre_close_conflicts and _closing_pos_snapshot is not None:
                was_involved = any(
                    tid in (c["trade_id_a"], c["trade_id_b"])
                    for c in _pre_close_conflicts
                )
                if was_involved:
                    from core import conflict_logger as _cflog
                    _reg = self._conflict_reg
                    remaining = _reg.detect_directional_conflicts(self.positions)
                    exposure = _reg.exposure_snapshot(self.positions)
                    _cflog.log_conflict_closed(
                        _closing_pos_snapshot, remaining, exposure,
                    )
        except Exception as _e:
            logger.warning(f"[CONFLICT] post-close logging failed: {_e}")
        if trade:
            self.risk.record_trade(trade["pnl_dollars"])
            self.trade_memory.record(trade, bot_id=self.bot_name)
            self.tracker.record_trade(trade)
            self._on_trade_closed(trade)  # P3: wire circuit breakers (stub; P10a completes wiring)

            # Phase 6: Close expectancy tracking BEFORE log_exit so MAE/MFE is included
            exp_analysis = self.expectancy.close_trade(
                exit_price=price,
                pnl_ticks=trade["pnl_ticks"],
                result=trade["result"],
            )

            # Log exit with MAE/MFE data from expectancy engine
            market_snap = self.aggregator.snapshot()
            # B14 Phase 4: inject gamma context into exit snapshot.
            self._enrich_market_with_gamma(market_snap)
            if exp_analysis:
                market_snap["mae_ticks"] = exp_analysis.get("mae_ticks")
                market_snap["mfe_ticks"] = exp_analysis.get("mfe_ticks")
                market_snap["capture_ratio"] = exp_analysis.get("edge_captured_pct")
                market_snap["went_red_first"] = exp_analysis.get("went_red_first")
                market_snap["mae_time_s"] = exp_analysis.get("mae_time_s")
                market_snap["mfe_time_s"] = exp_analysis.get("mfe_time_s")
            self.history.log_exit(trade, market_snap)

            # B64: target-miss forensic logging. Emit [EXIT_FORENSIC] on
            # every exit so we can audit target-fire correctness. If
            # MFE >= 20 ticks AND exit_reason != "target_hit", escalate
            # with [TARGET_MISS_SUSPECT] at WARN level — these are the
            # trades Jennifer flagged: big favorable excursion that never
            # triggered the LIMIT target (possible causes: target leg not
            # Working in NT8, cancelled by unrelated CANCEL_ALL, or
            # managed-exit fired before price reached target).
            try:
                _mfe = market_snap.get("mfe_ticks") or 0
                _mae = market_snap.get("mae_ticks") or 0
                _mfe_t = market_snap.get("mfe_time_s") or 0
                _reason = trade.get("exit_reason", "")
                _tid = trade.get("trade_id", "")
                logger.info(
                    f"[EXIT_FORENSIC] tid={_tid} reason={_reason} "
                    f"mfe_ticks={_mfe:.0f} mae_ticks={_mae:.0f} "
                    f"time_at_mfe={_mfe_t:.1f}s "
                    f"pnl=${trade.get('pnl_dollars', 0):.2f}"
                )
                if _mfe >= 20 and _reason != "target_hit":
                    logger.warning(
                        f"[TARGET_MISS_SUSPECT] tid={_tid} "
                        f"strategy={trade.get('strategy','?')} "
                        f"direction={trade.get('direction','?')} "
                        f"entry={trade.get('entry_price',0):.2f} "
                        f"exit={trade.get('exit_price',0):.2f} "
                        f"target={trade.get('target_price') or 0:.2f} "
                        f"mfe_ticks={_mfe:.0f} reason={_reason} — "
                        f"favorable excursion did not trigger LIMIT "
                        f"target; investigate OCO attachment or "
                        f"managed-exit timing."
                    )
            except Exception as _e:
                logger.debug(f"[EXIT_FORENSIC] log error (non-blocking): {_e}")

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

    # ─── Decay Monitor Background Loop ────────────────────────────────

    async def _decay_monitor_loop(self) -> None:
        """Hourly decay check + 15:10 CT daily summary push.

        - CRITICAL strategies → immediate Telegram alert (every hour)
        - WARNING strategies → Telegram alert at most once per 4 hours
        - 15:10 CT → daily summary: P&L, trades, top exit reason, degraded strats
        """
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        import collections

        ct_tz = _ZI("America/Chicago")
        _last_warning_alert: float = 0.0       # epoch seconds
        _daily_summary_fired_for: object = None  # date object

        while True:
            try:
                now_ct = _dt.now(ct_tz)

                # ── hourly decay check ─────────────────────────────────
                try:
                    summary = self.decay_monitor.summary()
                    reports = summary.get("reports", {})
                    criticals = [n for n, r in reports.items() if r.get("status") == "CRITICAL"]
                    warnings  = [n for n, r in reports.items() if r.get("status") == "WARNING"]

                    if criticals:
                        await tg.notify_alert(
                            "STRATEGY DECAY CRITICAL",
                            f"Strategies: {criticals}\nCheck dashboard /api/risk-mgmt",
                        )
                    elif warnings:
                        import time as _time
                        if _time.monotonic() - _last_warning_alert > 4 * 3600:
                            await tg.notify_alert(
                                "STRATEGY DECAY WARNING",
                                f"Strategies: {warnings}\nMonitoring — not yet critical.",
                            )
                            _last_warning_alert = _time.monotonic()
                except Exception as _e:
                    logger.debug(f"[DECAY_MONITOR] check error: {_e}")

                # ── 15:10 CT daily summary ─────────────────────────────
                if (now_ct.hour == 15 and now_ct.minute == 10
                        and _daily_summary_fired_for != now_ct.date()):
                    try:
                        today_str = str(now_ct.date())
                        all_trades = list(getattr(self.trade_memory, "trades", []))
                        trades_today = [
                            t for t in all_trades
                            if str(t.get("exit_time") or t.get("ts") or "").startswith(today_str)
                        ]
                        wins   = sum(1 for t in trades_today if t.get("pnl_dollars", 0) > 0)
                        losses = sum(1 for t in trades_today if t.get("pnl_dollars", 0) < 0)
                        pnl    = sum(float(t.get("pnl_dollars", 0) or 0) for t in trades_today)
                        n      = len(trades_today)
                        wr     = (wins / n * 100) if n else 0.0

                        # top exit reason
                        reasons = [t.get("exit_reason", "unknown") for t in trades_today if t.get("exit_reason")]
                        top_reason = collections.Counter(reasons).most_common(1)
                        top_reason_str = top_reason[0][0] if top_reason else "n/a"

                        # degraded strategies from decay monitor
                        try:
                            _sum = self.decay_monitor.summary()
                            degraded = [
                                n for n, r in _sum.get("reports", {}).items()
                                if r.get("status") in ("WARNING", "CRITICAL")
                            ]
                        except Exception:
                            degraded = []

                        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                        degraded_str = ", ".join(degraded) if degraded else "none"
                        msg = (
                            f"\U0001F4CB <b>15:10 DAILY SUMMARY</b>\n"
                            f"P&amp;L: <b>{pnl_str}</b>\n"
                            f"Trades: {n} ({wins}W / {losses}L) WR={wr:.0f}%\n"
                            f"Top exit: {top_reason_str}\n"
                            f"Degraded: {degraded_str}"
                        )
                        from core.telegram_notifier import send as _tg_send
                        await _tg_send(msg)
                        _daily_summary_fired_for = now_ct.date()
                        logger.info(f"[DECAY_MONITOR] 15:10 daily summary sent: pnl={pnl_str} {n}t")
                    except Exception as _e:
                        logger.warning(f"[DECAY_MONITOR] daily summary error: {_e}")

            except Exception as _outer:
                logger.debug(f"[DECAY_MONITOR] loop error: {_outer}")

            await asyncio.sleep(3600)  # check once per hour

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
            # B51: get_vix() and get_intermarket() return DICTS, not floats.
            # Unwrap .get("vix"/"vix_proxy"/"dxy") before float() conversion.
            try:
                from core.market_intel import get_full_intel
                intel = await get_full_intel()
                if intel:
                    im_data = {}
                    vix_raw = intel.get("vix")
                    if isinstance(vix_raw, dict):
                        v = vix_raw.get("vix") or vix_raw.get("vix_proxy")
                        if v:
                            im_data["VIX"] = float(v)
                    elif vix_raw:
                        im_data["VIX"] = float(vix_raw)
                    dxy_raw = intel.get("dxy")
                    if isinstance(dxy_raw, dict):
                        d = dxy_raw.get("dxy") or dxy_raw.get("value")
                        if d:
                            im_data["DXY"] = float(d)
                    elif dxy_raw:
                        im_data["DXY"] = float(dxy_raw)
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
    def _menthorq_to_dict(self) -> dict:
        """Expose MenthorQ gamma regime and all levels for dashboard."""
        try:
            from core.menthorq_feed import get_snapshot, regime_for_price
            snap = get_snapshot()
            price = self.aggregator.snapshot().get("price", 0)
            regime = regime_for_price(snap, price)
            return {
                "gamma_regime": regime.get("gamma_regime", "UNKNOWN"),
                "live_gamma":   regime.get("live_gamma", "UNKNOWN"),
                "above_hvl":    regime.get("above_hvl", True),
                "hvl":          snap.hvl,
                "hvl_0dte":     snap.hvl_0dte,
                "gamma_wall_0dte": snap.gamma_wall_0dte,
                "call_resistance": snap.call_resistance_all,
                "put_support":     snap.put_support_all,
                "call_resistance_0dte": snap.call_resistance_0dte,
                "put_support_0dte":     snap.put_support_0dte,
                "day_min":      snap.day_min,
                "day_max":      snap.day_max,
                "nearest_resistance": regime.get("nearest_resistance", 0.0),
                "nearest_support":    regime.get("nearest_support", 0.0),
                "gex_levels": [
                    snap.gex_level_1, snap.gex_level_2, snap.gex_level_3,
                    snap.gex_level_4, snap.gex_level_5, snap.gex_level_6,
                    snap.gex_level_7, snap.gex_level_8, snap.gex_level_9,
                    snap.gex_level_10,
                ],
                "stop_multiplier": regime.get("stop_multiplier", 1.0),
                "allow_long":  regime.get("allow_long", True),
                "allow_short": regime.get("allow_short", True),
                "dex":    snap.dex,
                "vanna":  snap.vanna,
                "cta":    snap.cta_positioning,
                "net_gex_bn": snap.net_gex_bn,
                "source": snap.source,
                "is_stale": snap.is_stale,
                "summary": regime.get("summary", ""),
            }
        except Exception as e:
            return {"gamma_regime": "UNKNOWN", "error": str(e)}

    def _cr_to_dict(self) -> dict:
        """Expose continuation/reversal assessment for dashboard."""
        cr = getattr(self, "_last_cr", None)
        if cr is None:
            return {"verdict": "UNKNOWN", "confidence": "LOW"}
        return {
            "verdict":           cr.verdict,
            "confidence":        cr.confidence,
            "direction_bias":    cr.direction_bias,
            "momentum_score":    cr.momentum_score,
            "momentum_direction":cr.momentum_direction,
            "consecutive_days":  cr.consecutive_days,
            "momentum_trend":    cr.momentum_trend,
            "exhaustion_warning":cr.exhaustion_warning,
            "at_day_max":        cr.at_day_max,
            "at_day_min":        cr.at_day_min,
            "at_call_resistance":cr.at_call_resistance,
            "at_put_support":    cr.at_put_support,
            "gamma_regime":      cr.gamma_regime,
            "above_hvl":         cr.above_hvl,
            "iv_regime":         cr.iv_regime,
            "continuation_factors": cr.continuation_factors,
            "reversal_factors":  cr.reversal_factors,
            "summary_table":     cr.summary_table,
        }

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
            "day_type": self._day_classifier.get_state(),   # TREND/RANGE/VOLATILE + params
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
            "menthorq":              lambda: self._menthorq_to_dict(),
            "cr_assessment":         lambda: self._cr_to_dict(),
            "playbook":              lambda: self.playbook_mgr.to_dict(),
            "intermarket":           lambda: self.intermarket.to_dict(),
            "edge_miner":            lambda: self.edge_miner.to_dict(),
            "knowledge_rag":         lambda: self.knowledge_rag.to_dict(),
            "pandas_ta":             lambda: self.pandas_ta.to_dict(),
            "chart_patterns":        lambda: self.chart_patterns.to_dict(),
            "cot_feed":              lambda: self.cot_feed.to_dict(),
            # ─── NEW Apr 2026 SHADOW modules ───────────────────────────
            "structural_bias":       lambda: self._last_structural_bias,
            "footprint_signals":     lambda: self._last_footprint_signals,
            "footprint_current":     lambda: (self.footprint_5m.current_bar().__dict__
                                              if self.footprint_5m.current_bar() else {}),
            "footprint_last_completed": lambda: (self.footprint_5m.last_completed().__dict__
                                                  if self.footprint_5m.last_completed() else {}),
            "swing_state":           lambda: self.swing_state_5m.to_dict(),
            "volume_profile":        lambda: self.volume_profile.to_dict(),
            "climax_state":          lambda: self.reversal_detector.get_state(),
            "sweep_state":           lambda: self.sweep_watcher.get_state(),
            "chart_patterns_v1":     lambda: self._last_chart_patterns_v1,
            "gamma_flip_state":      lambda: self.gamma_flip_detector.get_state(),
            "vix_term_structure":    lambda: self._last_vix_term,
            "pinning_state":         lambda: self._last_pinning_state,
            "opex_status":           lambda: self._last_opex_status,
            "es_confirmation":       lambda: self._last_es_confirmation,
            "decay_monitor_summary": lambda: self.decay_monitor.summary(),
            "tca_weekly_report":     lambda: self.tca_tracker.weekly_report(),
            "circuit_breakers_state": lambda: self.circuit_breakers.get_state(),
            "sizing_config":         lambda: self.simple_sizer.config,
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
