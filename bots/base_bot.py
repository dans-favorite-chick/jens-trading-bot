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
from typing import Optional

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
from core.tape_reader import TapeReader
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

# ─── P4-1 Stage 1 extracted loops (2026-05-24) ──────────────────────
# Each module wraps one async loop that previously lived as a method on
# BaseBot. The loops are observational/keep-alive — no OIF, no risk gates.
# See docs/audits/BASE_BOT_DECOMPOSITION_PLAN.md for the full plan.
from bots._decay_monitor import DecayMonitor as _DecayMonitorLoop
from bots._heartbeat import HeartbeatSender as _HeartbeatSenderLoop
from bots._ws_watchdog import WSWatchdog as _WSWatchdogLoop
# P1-7 (2026-05-25): pending-entry lifecycle sweeper. Background coroutine
# that cancels any LIMIT entry older than PENDING_ENTRY_TIMEOUT_S.
from bots._pending_entry_sweeper import PendingEntrySweeper as _PendingEntrySweeperLoop
from bots._news_scanner import NewsScanner as _NewsScannerLoop
# Stage 2 (2026-05-24):
from bots._session_levels_refresher import SessionLevelsRefresher
from bots._dashboard_pusher import DashboardPusher
# Stage 3 (2026-05-24): AI runners (currently disabled via P0-4) extracted.
# OIF emitter extracted to bots/_oif_emitter.py and ACTIVE as of P4-1
# Stage 3. The dual-cache issue (formerly bb._OIF_SINK vs
# _oif_emitter._OIF_SINK) was resolved by making bb._OIF_SINK the single
# source of truth: _oif_emitter lazy-imports bb._get_oif_sink at call
# time, and the five _sink_submit_* names below are re-exports of
# _oif.submit_*. Tests that mutate bb._OIF_SINK (e.g.
# tests/test_risk_gate_migration.py:251) continue to control sink
# construction for every OIF write from either entry point.
from bots import _oif_emitter as _oif
from bots._ai_runners import AIRunners as _AIRunnersImpl
# Stage 2 round 2 (2026-05-24): observability + reconciliation extractions.
# All are pure read/dispatch — no OIF write, no risk gate mutation.
from bots._runtime_reconciliation import RuntimeReconciliationLoop
from bots._dashboard_commands import DashboardCommandDispatcher
from bots._market_enricher import MarketEnricher
# Stage 3 (2026-05-24): strategy dispatch — _evaluate_strategies body lives here.
from bots._strategy_dispatch import StrategyDispatch
# Stage 4 (2026-05-24): the OIF-write methods. Live blast radius bounded by
# core/live_canary_gate.py — only LIVE_STRATEGY_ALLOWLIST strategies reach
# live execution. Each module is behaviorally verbatim with self.X -> bot.X.
from bots._trade_entry import TradeEntry
from bots._trade_exit import TradeExit
from bots._scale_out import ScaleOut as _ScaleOutImpl
from bots._signal_router import SignalRouter as _SignalRouterImpl
from bots._ws_dispatcher import WSDispatcher as _WSDispatcherImpl
from core.tca_tracker import TCATracker
from core.circuit_breakers import CircuitBreakers, HALT_MARKER_FILE
from core.chart_patterns_v1 import extract_v1_patterns
from core.vix_term_structure import get_cached as get_vix_term_cached
# Chart overlay JSONL writer (read by ninjatrader/PhoenixTradeOverlay.cs)
# All emit_*() calls are wrapped in try/except inside the helper, so a
# visualizer failure never breaks the bot. Hook points: signal emit,
# fill confirmation, BE stop move, exit.
from core import signal_visualizer as _signal_viz
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


def should_reject_on_rsi_div(
    signal_direction: str,
    div_type: str,
    div_strength: float,
    hard_gate_enabled: bool,
    min_strength: float = 20,
) -> bool:
    """Fix (2026-05-03): rsi_div_hard_gate evaluator.

    Returns True when the signal should be REJECTED due to opposing
    RSI divergence on a strategy that has the hard-gate enabled.

    Conditions for rejection (all must hold):
      - hard_gate_enabled is True (per-strategy config)
      - div_strength >= min_strength (otherwise too weak to act on)
      - divergence direction OPPOSES the signal direction:
          bullish-div + LONG = aligned (no rejection)
          bullish-div + SHORT = opposing (REJECT)
          bearish-div + LONG = opposing (REJECT)
          bearish-div + SHORT = aligned (no rejection)

    Forensic basis: opposing RSI div appeared in 6 losers / 0 winners
    in the bias_momentum dataset.
    """
    if not hard_gate_enabled:
        return False
    if div_strength < min_strength:
        return False
    aligned = (
        (div_type == "bullish" and signal_direction == "LONG")
        or (div_type == "bearish" and signal_direction == "SHORT")
    )
    return not aligned


def should_suppress_trend_stall(held_s: float, grace_s: int) -> bool:
    """Fix A (2026-05-03): trend_stall grace period.

    Returns True when the position has been held for less than the
    configured grace window — the trend_stall exit should be SUPPRESSED.

    Returns False when:
      - grace_s <= 0 (feature disabled / legacy behavior), OR
      - held_s >= grace_s (grace elapsed)

    Forensic context: 12 of the 71 audit trades exited at duration_s ≤ 0
    via trend_stall — entry-vs-exit gates disagreed on the same bar's
    data. A grace window prevents this instant unwind.
    """
    if grace_s <= 0:
        return False
    return held_s < grace_s


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


# ════════════════════════════════════════════════════════════════════
# PHASE 13 SECTION U: per-strategy overrides (tick-validated)
# ════════════════════════════════════════════════════════════════════

def _apply_phase13_overrides(signal) -> None:
    """Mutate a Signal in-place to apply the tick-validated per-strategy
    overrides from core/exit_policies.py:
      - order_type: from PHASE_13_ORDER_TYPES (market | limit_5s)
      - exit_policy: from PHASE_13_EXIT_ASSIGNMENTS; if it computes a target,
        overwrite signal.target_price

    Safe no-op for strategies not in the registry. Logs at INFO when an
    override is applied so we can see it in the live logs.

    Called at the top of BaseBot._process_signal — see Section U of
    docs/PHASE_13_IMPLEMENTATION_PLAN.md for the rationale.
    """
    try:
        from core.exit_policies import (
            PHASE_13_EXIT_ASSIGNMENTS,
            PHASE_13_ORDER_TYPES,
            get_policy,
        )
    except Exception as _e:
        # 2026-05-20 SHIP AUDIT pt2 (B-005): was a silent return that
        # masked import errors. If core.exit_policies fails to import,
        # EVERY Phase 13 target override silently no-ops — strategies
        # ship with legacy 1.5R/2R/target_rr targets and the operator
        # has NO visibility. Promoting to WARNING.
        logger.warning(
            f"[Phase13 override] core.exit_policies import failed "
            f"({_e!r}) — Phase 13 overrides DISABLED for this process. "
            f"Strategies will use their Signal-emitted targets/order types."
        )
        return

    strat = getattr(signal, "strategy", None)
    if not strat:
        return

    # 2026-05-20 SHIP AUDIT pt2 (Finding 2 / 5y backtest spawn):
    # Sub-strategy routing. opening_session emits Signal(strategy=
    # "opening_session") with the sub identifier in signal.metadata
    # ["sub_strategy"]. PHASE_13_EXIT_ASSIGNMENTS uses dot-notation
    # keys like "opening_session.open_drive". Without this lookup, the
    # plan's "open_drive → fixed_rr(rr=3.0)" override is dead code —
    # every open_drive trade ships at 2R (strategy's internal default).
    # Empirically verified by the 5y backtest agent: 267 open_drive
    # trades all shipped at RR=2.0, not the plan's 3.0.
    _sub = (getattr(signal, "metadata", {}) or {}).get("sub_strategy")
    _dotted = f"{strat}.{_sub}" if _sub else None

    # Resolve the highest-specificity key that exists in each registry.
    def _resolve_key(registry: dict) -> str:
        if _dotted and _dotted in registry:
            return _dotted
        return strat

    # 0) Entry mode override (Section V.1: pilot RETEST for 4 strategies)
    # For now, log the intent only. Full retest-wait implementation
    # deferred to next sprint (requires per-strategy tick buffer).
    try:
        from core.entry_modes import is_retest_strategy
        if is_retest_strategy(strat):
            # Annotate the signal so downstream code/logs can see the
            # intent. Actual retest-wait logic will be added later.
            setattr(signal, "entry_mode", "retest")
            logger.info(
                f"[Phase13 override] {strat}: entry_mode=retest (per Section V.1) "
                f"-- mode flagged; retest-wait logic pending implementation"
            )
        else:
            setattr(signal, "entry_mode", "first_touch")
    except Exception:
        pass  # entry_modes module not present, default behavior

    # 1) Order type override (uses sub-strategy dotted key when applicable)
    _order_key = _resolve_key(PHASE_13_ORDER_TYPES)
    if _order_key in PHASE_13_ORDER_TYPES:
        order_type = PHASE_13_ORDER_TYPES[_order_key]
        if order_type == "limit_5s":
            # Map to Phoenix's LIMIT entry type. NT8 OIF + bracket logic
            # will use the signal.entry_price as the limit price. The
            # "5s cancel + market" behavior requires a separate mechanism
            # (TODO: implement in Phase 14 via limit timeout watcher).
            # For now, plain LIMIT at signal price.
            if getattr(signal, "entry_type", "MARKET") != "LIMIT":
                signal.entry_type = "LIMIT"
                logger.info(
                    f"[Phase13 override] {strat}: entry_type "
                    f"-> LIMIT (per Section U slippage analysis)"
                )
        # market stays at default (MARKET)

    # 2) Exit policy override — set target_price if policy computes one
    #
    # 2026-05-20 BUG FIX (Phase 13 ship audit): this block has a SILENT
    # no-op problem. Most strategies (spring_setup, bias_momentum,
    # vwap_pullback_v2, ib_breakout, vwap_band_pullback) emit a Signal
    # WITHOUT entry_price/stop_price set — those are computed later in
    # the trade execution path. So when this runs at the top of
    # _process_signal(), the guard `if initial_stop is not None and
    # entry_price is not None` is False and the override silently bails
    # with no log line. Today (2026-05-20) spring_setup fired with
    # target=1.5R (legacy) instead of 3R (Phase 13) because of this.
    #
    # FIX: leave the early attempt here (handles strategies that DO set
    # prices at emit time, like ORB), but ALSO fire a loud warning when
    # the early attempt no-ops so the operator can see the deferred
    # re-application is the only path actually working. The actual
    # re-apply happens via `recompute_phase13_target()` below, called
    # from the trade execution path after stop/entry are finalized.
    _exit_key = _resolve_key(PHASE_13_EXIT_ASSIGNMENTS)
    if _exit_key in PHASE_13_EXIT_ASSIGNMENTS:
        pname, params = PHASE_13_EXIT_ASSIGNMENTS[_exit_key]
        try:
            policy = get_policy(pname, params)
            initial_stop = getattr(signal, "stop_price", None)
            entry_price = getattr(signal, "entry_price", None)
            direction = getattr(signal, "direction", None)
            if initial_stop is not None and entry_price is not None and direction:
                new_target = policy.compute_initial_target(
                    direction, float(entry_price), float(initial_stop)
                )
                # Only override if the policy provided a target (None means
                # "let the strategy keep its own target", used by managed_existing)
                if new_target is not None:
                    old_target = getattr(signal, "target_price", None)
                    if old_target != new_target:
                        signal.target_price = new_target
                        logger.info(
                            f"[Phase13 override] {strat}: target_price "
                            f"{old_target} -> {new_target} via {pname}({params})"
                        )
            else:
                # Prices not yet set on Signal — deferred re-apply needed.
                # Tag the signal so the trade execution path knows to call
                # recompute_phase13_target() once stop/entry are finalized.
                # FAIL LOUDLY (Phoenix's I-002 lesson) — if the deferred
                # path is missing, this warning makes it visible.
                setattr(signal, "_phase13_target_deferred", True)
                logger.info(
                    f"[Phase13 override] {strat}: target deferred — "
                    f"signal has no entry/stop yet (policy={pname}); "
                    f"will re-apply after price computation"
                )
        except Exception as e:
            logger.warning(f"[Phase13 override] exit policy error for {strat}: {e!r}")


class _PolicyPosAdapter:
    """Map core.position_manager.Position -> the field names ExitPolicy
    implementations expect (initial_stop, entry_ts, policy_state).
    Lightweight wrapper; mutating policy_state mutates the real position.
    """
    __slots__ = ("_real", "entry_price", "initial_stop", "direction",
                 "entry_ts", "policy_state")

    def __init__(self, real_pos):
        self._real = real_pos
        self.entry_price = float(real_pos.entry_price)
        # Position has `initial_stop_price` (frozen at open); policies want `initial_stop`.
        # 2026-05-20 SHIP AUDIT pt2 (B-008): bare getattr always returns
        # `initial_stop_price` (defined on the dataclass with default 0.0)
        # so the fallback to `stop_price` never fires. If a Position was
        # ever reconstructed from disk without __post_init__ running,
        # initial_stop=0.0 → policy._stop_distance ≈ entry_price → trail
        # never activates → strategy degrades to wide-bracket placeholder.
        # Use `or` so 0.0 also falls through to stop_price.
        _isp = getattr(real_pos, "initial_stop_price", 0.0) or real_pos.stop_price
        self.initial_stop = float(_isp)
        self.direction = str(real_pos.direction)
        # Position has `entry_time` (epoch seconds); policies want `entry_ts`.
        # TimeExitPolicy accepts float directly via the isinstance check at line 320.
        self.entry_ts = float(real_pos.entry_time)
        # policy_state persists on the real Position across bar calls.
        if not hasattr(real_pos, "_phase13_policy_state"):
            object.__setattr__(real_pos, "_phase13_policy_state", {})
        self.policy_state = real_pos._phase13_policy_state


class _PolicyBarAdapter:
    """Map TickAggregator.bars_1m bar objects -> the field names ExitPolicy
    implementations expect (high, low, close, end_time).
    """
    __slots__ = ("high", "low", "close", "end_time")

    def __init__(self, bar):
        self.high = float(bar.high)
        self.low = float(bar.low)
        self.close = float(bar.close)
        # Bar.end_time is a datetime; policy code uses it in arithmetic.
        # Convert to epoch-seconds float so TimeExitPolicy's subtraction
        # with pos.entry_ts (also float) is well-defined.
        _et = getattr(bar, "end_time", None)
        if _et is None:
            self.end_time = time.time()
        elif hasattr(_et, "timestamp"):
            self.end_time = _et.timestamp()
        else:
            self.end_time = float(_et)


def recompute_phase13_target(strategy: str, direction: str,
                              entry_price: float, stop_price: float,
                              sub_strategy: str | None = None) -> float | None:
    """Re-apply the Phase 13 exit policy now that entry+stop are finalized.

    Called from the trade execution path AFTER `stop_price` and `entry_price`
    are computed locally (not from the Signal). Returns the new target
    price, or None if no override should be applied (managed_existing
    policy, or strategy not in PHASE_13_EXIT_ASSIGNMENTS).

    2026-05-20 SHIP AUDIT pt2 (Finding 2): added sub_strategy parameter.
    PHASE_13_EXIT_ASSIGNMENTS uses dot-keys like "opening_session.open_drive";
    look up the dotted form first, fall back to the bare strategy name.
    Without this, opening_session.open_drive's plan-specified 3R override
    was silently dead code — all 267 open_drive trades in the 5y backtest
    shipped at 2R (strategy's internal default).

    2026-05-20 ship-audit fix: pairs with the deferred-target tag set
    in `_apply_phase13_overrides` step 2. Fixes the silent no-op that
    caused spring_setup to ship 1.5R targets instead of 3R Phase 13.

    2026-05-20 follow-up fix: import PHASE_13_EXIT_ASSIGNMENTS / get_policy
    inside the function. The original a03086e shipped these unresolved at
    module scope -> every call raised NameError at line 414 (BEFORE the
    try-except), bricking the deferred-recompute path. The caller in
    _process_signal catches the NameError and silently logs warning, so
    every deferred-target strategy (spring_setup, bias_momentum,
    vwap_pullback_v2, vwap_band_pullback, ib_breakout, a_asian,
    e_multi_day, g_inside_bar, raschke_baseline, es_nq_confluence)
    silently fell back to the legacy 1.5R target. Caught by phase-13
    verification harness on 2026-05-20.
    """
    try:
        from core.exit_policies import (
            PHASE_13_EXIT_ASSIGNMENTS,
            get_policy,
        )
    except Exception as e:
        logger.warning(
            f"[Phase13 override] recompute_phase13_target import failed "
            f"for {strategy}: {e!r}"
        )
        return None
    # Sub-strategy-aware lookup (Finding 2 fix).
    _dotted = f"{strategy}.{sub_strategy}" if sub_strategy else None
    if _dotted and _dotted in PHASE_13_EXIT_ASSIGNMENTS:
        lookup_key = _dotted
    elif strategy in PHASE_13_EXIT_ASSIGNMENTS:
        lookup_key = strategy
    else:
        return None
    pname, params = PHASE_13_EXIT_ASSIGNMENTS[lookup_key]
    try:
        policy = get_policy(pname, params)
        new_target = policy.compute_initial_target(
            direction, float(entry_price), float(stop_price)
        )
        return new_target  # None = managed_existing, no override
    except Exception as e:
        logger.warning(
            f"[Phase13 override] recompute_phase13_target error for "
            f"{lookup_key}: {e!r}"
        )
        return None


def _get_oif_sink():
    """Return the configured OIF sink (DirectFileSink by default,
    RiskGateSink if PHOENIX_RISK_GATE=1). Cached per-process for speed."""
    global _OIF_SINK
    if _OIF_SINK is None:
        from phoenix_bot.orchestrator.oif_writer import get_default_sink
        _OIF_SINK = get_default_sink()
    return _OIF_SINK


# P4-1 Stage 3 (2026-05-24): body lives in bots/_oif_emitter.py. The
# function below is a re-export so existing call sites (and
# `hasattr(bb, "_sink_submit_place")` checks in
# tests/test_risk_gate_migration.py) keep working unchanged.
_sink_submit_place = _oif.submit_place


# P4-1 Stage 3 (2026-05-24): body lives in bots/_oif_emitter.py.
_sink_submit_protect = _oif.submit_protect


# P4-1 Stage 3 (2026-05-24): body lives in bots/_oif_emitter.py.
_sink_submit_exit = _oif.submit_exit


# P4-1 Stage 3 (2026-05-24): body lives in bots/_oif_emitter.py.
_sink_submit_partial_exit = _oif.submit_partial_exit


# P4-1 Stage 3 (2026-05-24): body lives in bots/_oif_emitter.py.
_sink_submit_modify_stop = _oif.submit_modify_stop


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
    # P1-8 (2026-05-24): if in-memory ID was lost (bot restart, scale-out
    # reset), try the persisted state file before logging NO_ID.
    if not getattr(pos, "stop_order_id", ""):
        try:
            from core.nt8_order_id_capture import load_stop_id
            recovered = load_stop_id(pos.trade_id)
            if recovered:
                pos.stop_order_id = recovered
                logger.info(
                    f"[STOP_ID_RECOVERED:{pos.trade_id}] loaded {recovered} "
                    f"from active_stops.json"
                )
        except Exception as _e:
            logger.debug(f"[STOP_ID_RECOVERY_FAIL:{pos.trade_id}] {_e}")
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


def _trail_stop(pos, price: float, min_profit_ticks: int = 8,
                trail_distance_ticks: int = 16):
    """
    High-water-mark trailing stop. Stop anchors at the BEST (peak) price
    seen since entry, trailed `trail_distance_ticks` behind it. Only
    moves in favorable direction — never worsens risk.

    2026-05-13 v2 (post-7f1411f): replaced the original
    `(entry + price) / 2` midpoint formula. Midpoint trail gave back
    50% of every unrealized peak — observed trade 398523b9 closed at
    +$0.18 after peaking at +23t, because midpoint moved stop to
    entry+11t and a normal 12t retrace clipped it. Research review
    (2026-05-13) confirmed midpoint isn't a published pattern;
    high-water-mark with fixed buffer is the Chandelier shape minus
    ATR dependency. Buffer of 16t = ~1× ATR(5m) on current MNQ tape.

    Mechanism:
      1. Update pos.high_water_price to the new peak if applicable.
      2. Require min_profit_ticks of peak-profit before activating (so
         trail can't fire from a momentary tick blip past entry).
      3. Compute candidate stop = peak - trail_distance_ticks.
      4. Move pos.stop_price only if candidate is BETTER than current.

    B76: after mutating pos.stop_price, emit write_modify_stop OIF to
    actually move the NT8 stop via cancel+replace.
    """
    try:
        _tick = TICK_SIZE
    except NameError:
        _tick = 0.25

    # 1. Update high-water-mark (peak favorable price since entry)
    if not getattr(pos, "high_water_price", 0):
        pos.high_water_price = pos.entry_price
    if pos.direction == "LONG":
        if price > pos.high_water_price:
            pos.high_water_price = price
    else:  # SHORT
        if price < pos.high_water_price:
            pos.high_water_price = price

    # 2. Require minimum peak profit before activating
    peak_profit_ticks = (
        (pos.high_water_price - pos.entry_price) / _tick
        if pos.direction == "LONG"
        else (pos.entry_price - pos.high_water_price) / _tick
    )
    if peak_profit_ticks < min_profit_ticks:
        return

    # 3. Compute candidate stop: peak minus the trail buffer
    new_stop = None
    if pos.direction == "LONG":
        candidate = pos.high_water_price - trail_distance_ticks * _tick
        if candidate > pos.stop_price:
            new_stop = round(candidate, 2)
    else:
        candidate = pos.high_water_price + trail_distance_ticks * _tick
        if candidate < pos.stop_price:
            new_stop = round(candidate, 2)
    if new_stop is None:
        return

    # 4. Move the stop (and emit NT8 modify-stop OIF via cancel+replace)
    old_stop = pos.stop_price
    pos.stop_price = new_stop
    logger.info(
        f"[TRAIL:{pos.trade_id}] Stop trailed to {pos.stop_price:.2f} "
        f"(peak={pos.high_water_price:.2f}, buffer={trail_distance_ticks}t, "
        f"+{peak_profit_ticks:.0f}t peak profit)"
    )
    _move_nt8_stop(pos, old_stop, new_stop)


# ── B62: Universal stop/target sanity gate (Exit Sprint S1) ──────────────
# 2026-05-15 fix: split the upper-bound check by exit mode so noise_area
# (and any other managed-exit strategy) isn't silently blocked. Managed-
# exit strategies (`uses_managed_exit=True`, `target_price=None`) carry
# WIDE structural disaster stops by design — noise_area's stop is the
# opposite noise-cone boundary +2t buffer, which can reach 600-1000t on
# wide-cone days. Today's 11 noise_area signals were all dropped at
# 776t > 200t cap. The sanity gate now widens to 1000t for those.
_SANITY_STOP_MIN_TICKS = 5
_SANITY_STOP_MAX_TICKS_DEFAULT = 200       # ordinary bracket strategies
_SANITY_STOP_MAX_TICKS_MANAGED = 1000      # managed-exit (noise_area, footprint_cvd_reversal)


def _sanity_check_entry(signal, entry_price, stop_price, target_price,
                        is_managed_exit: bool = False):
    """Fail-closed geometry + distance check before OCO submission.

    Returns (ok: bool, reason: str|None). On failure, caller logs
    [STOP_SANITY_FAIL] CRITICAL and aborts the trade.

    Rules:
      - LONG: stop < entry < target (target may be None for managed exits)
      - SHORT: target < entry < stop (target may be None for managed exits)
      - Stop distance:
          - 5-200 MNQ ticks for ordinary bracket strategies
          - 5-1000 MNQ ticks when `is_managed_exit=True` (the strategy's
            stop is a structural disaster anchor, not a real risk stop —
            real exit comes from signal.exit_trigger / managed exit path)

    `is_managed_exit` should be True when both `signal.target_price is None`
    AND the underlying strategy class has `uses_managed_exit=True`. The
    caller computes that combination (see `_managed_exit_target` in the
    entry path).
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
    upper = (_SANITY_STOP_MAX_TICKS_MANAGED if is_managed_exit
             else _SANITY_STOP_MAX_TICKS_DEFAULT)
    if stop_ticks < _SANITY_STOP_MIN_TICKS or stop_ticks > upper:
        mode = "managed" if is_managed_exit else "bracket"
        return False, (f"stop distance {stop_ticks:.0f}t outside "
                       f"{_SANITY_STOP_MIN_TICKS}-{upper} range ({mode} mode)")
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

    # 2026-05-04 (Sprint D F2): track the session date on which the
    # RECOVERY MODE telegram has already fired. Reset to None at every
    # daily-reset boundary so day N+1 can fire once, then quiet until
    # the next reset. None at boot means "haven't paged yet today".
    _recovery_alert_session_date = None

    def __init__(self):
        # Startup banner FIRST — before _validate_nt8_paths() (which may
        # exit the process) so the operator always sees the loaded
        # safety configuration snapshot, even on a fast-fail boot. See
        # docs/audits/SYNTHESIS_2026-05-24.md and _print_startup_banner
        # for rationale. Banner is the first log line in prod_bot.log /
        # sim_bot.log — operator reads it after restart, verifies
        # LIVE_TRADING / FREEZE_ACTIVE / LIVE_STRATEGY_ALLOWLIST against
        # expectations, and kills the bot immediately if any value is
        # not what they expect.
        self._print_startup_banner()
        _validate_nt8_paths()
        # Per-instance reset of the class-level recovery dedup state.
        self._recovery_alert_session_date = None
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
        # 2026-05-13: hydrate today's risk counters from the just-loaded
        # trade_history. Without this, daily_pnl/trades_today/wins_today/
        # losses_today reset to 0 on every restart — making the dashboard
        # Daily Stats panel show "$0.00 / 0 trades" even when today's
        # trades are visible in the TODAY (CME Globex) summary card and
        # the per-bot trade_memory files on disk.
        #
        # CRITICAL: filter to THIS BOT'S trades only (bot_id == bot_name).
        # position_manager.trade_history is hydrated from load_all_trades()
        # which merges legacy + EVERY per-bot file — so it contains both
        # prod's and sim's history. Without the filter, prod's risk
        # counters would include sim's trades and vice versa (incorrect
        # daily_pnl attribution; observed live 2026-05-13 first cut where
        # both bots showed identical $114.22 because they both hydrated
        # from sim's 4 wins).
        try:
            from datetime import datetime as _dt, time as _dt_time
            _midnight_local_today = _dt.combine(
                _dt.now().date(), _dt_time.min
            ).timestamp()
            _my_trades = [
                t for t in self.positions.trade_history
                if (t.get("bot_id") or "") == self.bot_name
            ]
            self.risk.hydrate_from_trades(
                _my_trades,
                since_ts=_midnight_local_today,
            )
        except Exception as _e:
            logger.warning(
                "[RISK_HYDRATE] startup hydration failed (non-blocking): %r", _e
            )
        # 2026-05-12: per-bot trade memory file (avoids prod/sim shared-file
        # write race that previously dropped prod's closed trades when sim
        # rewrote the file with its older in-memory view).
        self.trade_memory = TradeMemory(bot_id=self.bot_name)
        # Sprint M Tier 2.3 (2026-05-12): tape reader — rolling capture of
        # large-print (>=25 contract) ticks + aggressor-side classification.
        # Surfaced in market snapshot as `tape_state`; pure observation
        # tonight (no IQS gating, no entry influence) so the next ~30 days
        # accumulate data on whether direction-aligned large prints predict
        # subsequent moves before we wire them as a bonus.
        self.tape_reader = TapeReader()
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
        # 2026-05-12: timestamp of the last WS message received from the
        # bridge (any type — tick / dom / trade_ack / etc). Drives the
        # application-level WS watchdog (_ws_watchdog_loop) that defends
        # against silent half-close. Sentinel 0 = no message yet, watchdog
        # skips its check until first message arrives.
        self._last_ws_message_time: float = 0.0

        # ── P4-1 Stage 1: extracted loop runners (2026-05-24) ────────
        # Each runner wraps one async loop that used to be a BaseBot
        # method. They read bot state but do not mutate critical state
        # (no OIF writes, no risk-gate calls). Constructed here; started
        # in run() alongside the remaining inline loops.
        self._decay_monitor_runner = _DecayMonitorLoop(self)
        self._heartbeat_sender = _HeartbeatSenderLoop(self)
        self._ws_watchdog = _WSWatchdogLoop(self)
        # P1-7: pending-entry sweeper — cancels stale LIMIT entries.
        self._pending_entry_sweeper = _PendingEntrySweeperLoop(self)
        self._news_scanner_runner = _NewsScannerLoop(self)
        # Stage 2 runners (read-only loops, no OIF / no risk gates):
        self._session_levels_refresher = SessionLevelsRefresher(self)
        self._dashboard_pusher = DashboardPusher(self)
        # Stage 3 — AI runners (P0-4 disabled the underlying agents, so
        # these methods early-return; extracted for structural cleanup).
        self._ai_runners = _AIRunnersImpl(self)
        # Stage 2 round 2 — observability + reconciliation runners.
        self._runtime_reconciliation = RuntimeReconciliationLoop(self)
        self._dashboard_commands = DashboardCommandDispatcher(self)
        # P4-1 Stage 3 (2026-05-24): trade close bookkeeping extracted.
        from bots._trade_closer import TradeCloser
        self._trade_closer = TradeCloser(self)
        # P4-1 Stage 3 (2026-05-24): _evaluate_strategies body extracted.
        self._strategy_dispatch = StrategyDispatch(self)
        # P4-1 Stage 4 (2026-05-24): the OIF-write methods extracted.
        self._trade_entry = TradeEntry(self)
        self._trade_exit = TradeExit(self)
        self._scale_out = _ScaleOutImpl(self)
        self._signal_router = _SignalRouterImpl(self)
        self._ws_dispatcher = _WSDispatcherImpl(self)
        # P4-3 (2026-05-24): latency tracker bridge-in stash. None until
        # the first tick arrives; _on_bar reads this to record tick_to_bar.
        self._last_t_bridge_in: float | None = None
        self._market_enricher = MarketEnricher(self)

        self.last_signal: dict | None = None
        self.last_rejection: str | None = None
        self._last_eval: dict = {}

        # Runtime config (from dashboard sliders)
        self._runtime_params = dict(STRATEGY_DEFAULTS)

        # Phase 4: AI Agent state
        # 2026-05-24 P1-1 Stage 1: stash the enriched market dict here
        # at signal-time so _enter_trade can persist the same dict the
        # strategy actually evaluated against. Without this, the
        # market_snapshot recorded into trade_memory is missing 4
        # strategy-blocking fields (day_type, cr_verdict, cvd_health*,
        # es_nq_rs) that the reconciliation harness needs to deterministically
        # replay the trade. See docs/audits/SYNTHESIS_2026-05-24.md F-13 +
        # out/reconciliation_inspect_2026-05-24.md §7.
        self._last_enriched_market: dict | None = None
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

        # ── 2026-05-13: CVD-based detectors (operator trade-flow methodology) ──
        # - cvd_health: pre-entry filter, vetoes trades fighting institutional flow
        # - cvd_flip:   mid-trade exit signal when per-bar delta flips against position
        # - cvd_div:    classic bear/bull divergence at confirmed swing points
        # All three are updated on every bar close in _on_bar. Strategies read
        # cvd_health.assess() at entry; exit loop reads cvd_flip/cvd_div on the
        # active position.
        from core.cvd_trend_health import CVDTrendHealth
        from core.cvd_bar_flip import BarDeltaFlipDetector
        from core.cvd_swing_divergence import SwingDivergenceDetector
        from core.big_move_detector import BigMoveDetector
        self.cvd_health = CVDTrendHealth(lookback_bars=6, veto_threshold=-0.3)
        self.cvd_flip = BarDeltaFlipDetector(lookback=5)
        self.cvd_div = SwingDivergenceDetector(
            swing_strength=3, min_bars_between=10, max_bars_between=40,
        )
        # 2026-05-15: Big-Move Detector — predict pre-move setups + peak
        # exhaustion across all strategies. Adds two assessment scores
        # (0-100) to the eval loop:
        #   pre_move_score: when >= 60, a big squeeze is likely imminent
        #   exhaustion_score (per active position): when >= 70, the
        #     current move appears to be peaking — trigger exit
        self.big_move = BigMoveDetector()
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

        # ─── MenthorQ gamma integration RETIRED 2026-05-06 (Sprint J) ──
        # Subscription cancelled. self.gamma_levels stays None forever;
        # downstream consumers (entry-wall filter, snapshot enrichment,
        # reload watcher) all gracefully no-op when this is None.
        # Attribute kept for backward compat with code that still reads
        # self.gamma_levels (rare after Sprint J cleanup, but defensive).
        self.gamma_levels = None
        self._gamma_mtime = 0.0  # Reload watcher reads this, sees no change

        # 2026-05-13: graceful shutdown flag — set when the dashboard
        # queues a {"type": "shutdown"} command (see
        # _handle_dashboard_command). Replaces the CTRL_BREAK_EVENT path
        # that was lost when we removed CREATE_NEW_PROCESS_GROUP from
        # dashboard _start_bot in commit 8b471af. On set: WS is closed →
        # async-for in _connect_and_listen unblocks → run()'s outer loop
        # exits → asyncio.run() returns → process exits cleanly.
        self._shutdown_requested = False

        # Register bar callback
        self.aggregator.on_bar(self._on_bar)

    # ─── Startup banner ────────────────────────────────────────────────
    def _print_startup_banner(self) -> None:
        """Emit a multi-line safety-configuration snapshot at boot.

        This is the FIRST log line of every prod_bot / sim_bot process
        and is the operator's post-restart verification surface. If any
        value (LIVE_TRADING, LIVE_STRATEGY_ALLOWLIST, FREEZE_ACTIVE,
        walk-forward gates, loss caps, AI-agent state) doesn't match
        what they expect, they kill the bot before it sends a tick.
        See docs/audits/SYNTHESIS_2026-05-24.md for the audit that
        motivated making the safety config inescapable on startup.

        Reads live config values via late imports so that:
          - missing modules / attrs degrade to `<unset>` (never crash);
          - the banner reflects the actually-loaded config, not stale
            constants captured at base_bot import time.
        """
        sep = "=" * 64

        def _safe(getter, default="<unset>"):
            try:
                v = getter()
                return v if v is not None else default
            except Exception:
                return default

        # Process identity
        process = f"{self.bot_name}_bot" if getattr(self, "bot_name", None) else "<unknown>"
        try:
            from core import bot_kind as _bk  # optional, may not exist
            _kind = getattr(_bk, "BOT_KIND", None) or getattr(_bk, "kind", None)
            if _kind:
                process = str(_kind)
        except Exception:
            pass  # bot_kind module is optional
        pid = os.getpid()
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S CT")

        # Settings (already imported at module scope)
        live_trading = _safe(lambda: LIVE_TRADING)
        # LIVE_STRATEGY_ALLOWLIST lives in config.settings but isn't in
        # the module-scope import block — load lazily.
        live_allowlist = _safe(
            lambda: __import__("config.settings", fromlist=["LIVE_STRATEGY_ALLOWLIST"])
            .LIVE_STRATEGY_ALLOWLIST
        )
        freeze_active = _safe(
            lambda: __import__("config.strategies", fromlist=["FREEZE_ACTIVE"])
            .FREEZE_ACTIVE
        )
        weekly_loss_limit = _safe(
            lambda: __import__("config.settings", fromlist=["WEEKLY_LOSS_LIMIT"])
            .WEEKLY_LOSS_LIMIT
        )
        # Daily loss cap: prefer the runtime slider default; fall back
        # to settings.DAILY_LOSS_LIMIT.
        daily_loss = _safe(
            lambda: STRATEGY_DEFAULTS.get("max_daily_loss")
            if STRATEGY_DEFAULTS.get("max_daily_loss") is not None
            else __import__("config.settings", fromlist=["DAILY_LOSS_LIMIT"])
            .DAILY_LOSS_LIMIT
        )

        # Strategies that this bot will load. Mirrors the load_strategies
        # filtering rules: enabled=True, and (if only_validated) validated=True.
        only_validated = bool(getattr(self, "only_validated", False))
        try:
            enabled_strats = [
                name for name, cfg in STRATEGIES.items()
                if cfg.get("enabled", True)
                and (not only_validated or cfg.get("validated", False))
            ]
        except Exception:
            enabled_strats = []

        # Allowlist intersection — which of the loaded strats actually
        # reach live execution. The rest are sim-only by canary policy.
        try:
            _allow = tuple(live_allowlist) if isinstance(live_allowlist, (list, tuple)) else ()
        except Exception:
            _allow = ()
        live_strats = [s for s in enabled_strats if s in _allow]
        sim_only_strats = [s for s in enabled_strats if s not in _allow]

        # Walk-forward gates per strategy (from config.strategies).
        try:
            gates = {
                name: STRATEGIES.get(name, {}).get("walk_forward_gate", "<unset>")
                for name in enabled_strats
            }
        except Exception:
            gates = {}
        hard_block = [n for n, g in gates.items() if g == "hard_block"]
        informational = [n for n, g in gates.items() if g == "informational"]
        other_gates = [
            (n, g) for n, g in gates.items()
            if g not in ("hard_block", "informational")
        ]

        # AI agent state (live canary disables all three by default).
        try:
            ai_any = bool(
                AGENT_COUNCIL_ENABLED
                or AGENT_PRETRADE_FILTER_ENABLED
                or AGENT_DEBRIEF_ENABLED
            )
        except Exception:
            ai_any = False
        ai_state = "ENABLED" if ai_any else "DISABLED (live canary)"

        # Trace IDs — assume enabled if core.trace_id is importable.
        try:
            __import__("core.trace_id")
            trace_state = "enabled"
        except Exception:
            trace_state = "<unset>"

        # Format the loss caps as dollars when numeric.
        def _money(v):
            try:
                return f"${float(v):.2f}"
            except Exception:
                return str(v)

        # ── Emit the banner ──
        logger.info(sep)
        logger.info("Phoenix Bot starting — safety configuration snapshot")
        logger.info(sep)
        logger.info(f"  Process:           {process} (PID {pid})")
        logger.info(f"  Started at:        {started_at}")
        logger.info(f"  LIVE_TRADING:      {live_trading}")
        logger.info(f"  LIVE_STRATEGY_ALLOWLIST: {live_allowlist}")
        logger.info(f"  FREEZE_ACTIVE:     {freeze_active}")
        if enabled_strats:
            logger.info(f"  Strategies loading: {', '.join(enabled_strats)}")
            logger.info(
                f"    ({len(enabled_strats)} enabled, "
                f"{len(live_strats)} in allowlist, "
                f"{len(sim_only_strats)} sim-only)"
            )
        else:
            logger.info("  Strategies loading: <none>")
        logger.info("  Walk-forward gates:")
        if hard_block:
            for n in hard_block:
                logger.info(f"    {n}: hard_block (REQUIRES walk_forward_harness PASS)")
        if informational:
            logger.info(f"    {len(informational)} strategies: informational")
        for n, g in other_gates:
            logger.info(f"    {n}: {g}")
        if not gates:
            logger.info("    <unset>")
        logger.info(f"  WEEKLY_LOSS_LIMIT: {_money(weekly_loss_limit)}")
        logger.info(f"  Daily loss cap:    {_money(daily_loss)}")
        logger.info(f"  AI agents:         {ai_state}")
        logger.info(f"  Trace IDs:         {trace_state}")
        logger.info(sep)

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
                # Sprint D F1 (2026-05-04): if we previously paged the
                # operator about this stuck position, fire ONE RESOLVED
                # confirmation now that NT8 is FLAT. Captured BEFORE the
                # finalize_exit_pending() call removes the Position record.
                _was_alerted = bool(getattr(pos, "_exit_timeout_alerted", False))
                _resolved_age_s = now - pos.exit_pending_since
                trade = self.positions.finalize_exit_pending(pos.trade_id)
                logger.info(
                    f"[EXIT_FINALIZED:{pos.trade_id}] NT8 confirmed FLAT "
                    f"on {pos.account} — closed {pos.direction} @ "
                    f"{pos.pending_exit_price:.2f} "
                    f"reason={pos.pending_exit_reason}"
                )
                if _was_alerted:
                    try:
                        from core.telegram_notifier import send_sync
                        send_sync(
                            f"✅ [EXIT_TIMEOUT_RESOLVED] {pos.account} "
                            f"({pos.strategy}) flattened after "
                            f"{_resolved_age_s:.0f}s of retries.",
                            dedup_key=f"exit_resolved:{pos.trade_id}",
                        )
                        logger.info(
                            f"[EXIT_TIMEOUT_RESOLVED:{pos.trade_id}] "
                            f"closed after {_resolved_age_s:.0f}s of "
                            f"alerted-stuck — operator paged on initial "
                            f"escalation, now resolved."
                        )
                    except Exception:
                        pass
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

            # NT8 still shows a position on this account. Auto-retry the
            # flatten before escalating. 2026-05-04 fix: previously this
            # path just logged CRITICAL + halted the strategy and demanded
            # an "operator flatten". Forensic: 2 stuck SHORT positions
            # (SimDom Pull Back, SimVWapp Pullback) on 2026-05-03/04 were
            # NOT recoverable for hours because the bot only logged and
            # gave up. Root causes were a CLOSEPOSITION-vs-OCO-stop race
            # (NT8 opens a fresh reverse position when CLOSEPOSITION arrives
            # at the same moment the OCO stop fills) AND PhoenixOIFGuard
            # quarantining filenames without a trailing `_<word>` token.
            # Both are now mitigated upstream, but this loop must also
            # retry instead of giving up.
            #
            # Retry strategy: write a directional MARKET order (BUY-to-cover
            # SHORT, SELL-to-flatten LONG) every reconciliation cycle. This
            # bypasses CLOSEPOSITION entirely so the OCO race cannot
            # double-fill into a reverse position. After RETRY_ESCALATE_S
            # seconds of retries we ALSO fire the telegram (deduped) so the
            # operator knows something is wrong — but we keep retrying.
            age_s = now - pos.exit_pending_since
            from bridge.oif_writer import write_oif as _write_oif
            try:
                # 2026-05-21 PHASE 13 SHIP AUDIT pt3 (Finding-3 from EXIT_RETRY
                # deep-dig agent a6529f16): switched from PLACE...{cover_side}
                # MARKET to CLOSEPOSITION. Forensic evidence: today (2026-05-20
                # → 2026-05-21) the prior `PLACE...SELL 1 MARKET` retry on
                # SimBias Momentum was REJECTED 22 times in a row by NT8 with
                # "Exceeds account's maximum position quantity" because sim
                # sub-accounts have max-position-qty=1 — NT8's pre-trade guard
                # sees `LONG 1 + working SELL 1` as "could go SHORT" which
                # exceeds the cap. CLOSEPOSITION bypasses this guard because
                # it explicitly tells NT8 "flatten whatever you have" — NT8
                # cancels the OCO bracket and submits an internal Order
                # Name='Close' market that doesn't trip the pre-trade gross-
                # exposure check. Verified empirically in NT8 log 2026-05-21
                # 02:42:06 (trade 30787e00) where CLOSEPOSITION cleanly
                # flattened a position after 2 PLACE...SELL retries were
                # rejected. The directional-MARKET path is kept as a fallback
                # in case CLOSEPOSITION ever fails to write (filesystem etc).
                #
                # Side note (preserving prior 2026-05-04 comment context):
                # The previous code derived cover_action from NT8's reported
                # direction (nt8_state[0]) rather than pos.direction to handle
                # the phantom-reverse case. CLOSEPOSITION sidesteps that
                # issue entirely — NT8 closes whatever it has, no direction
                # logic needed in our code. We still log direction-desync for
                # forensic visibility.
                nt8_dir = nt8_state[0]
                nt8_qty = int(nt8_state[1] or 1)
                used_action = None
                try:
                    paths = _write_oif(
                        "CLOSEPOSITION",
                        qty=nt8_qty,
                        account=pos.account,
                        trade_id=f"{pos.trade_id}_retry{int(age_s)}",
                    )
                    used_action = "CLOSEPOSITION"
                    if not paths:
                        # OIF writer returned no paths — write didn't land.
                        # Fall through to directional MARKET so we keep
                        # retrying SOMETHING.
                        raise RuntimeError("CLOSEPOSITION write returned no paths")
                except Exception as _close_err:
                    cover_action = "BUY" if nt8_dir == "SHORT" else "SELL"
                    _write_oif(
                        cover_action,
                        qty=nt8_qty,
                        account=pos.account,
                        order_type="MARKET",
                        trade_id=f"{pos.trade_id}_retry{int(age_s)}_fb",
                    )
                    used_action = f"{cover_action}_MARKET_FALLBACK"
                    logger.warning(
                        f"[EXIT_RETRY:{pos.trade_id}] CLOSEPOSITION failed "
                        f"({_close_err!r}); used {used_action} fallback"
                    )
                if pos.direction != nt8_dir:
                    logger.warning(
                        f"[EXIT_RETRY:{pos.trade_id}] STATE DESYNC: "
                        f"Python pos.direction={pos.direction!r} but NT8 "
                        f"shows {nt8_dir!r} — using {used_action} to flatten. "
                        f"Likely an OCO-vs-other race created phantom reverse."
                    )
                logger.warning(
                    f"[EXIT_RETRY:{pos.trade_id}] {pos.account} still "
                    f"{nt8_dir} {nt8_qty}@{nt8_state[2]} after "
                    f"{age_s:.0f}s — sent {used_action} to flatten "
                    f"(attempt at age={age_s:.0f}s)"
                )
            except Exception as _e:
                logger.error(
                    f"[EXIT_RETRY:{pos.trade_id}] retry write_oif failed: {_e!r}"
                )

            # Telegram + strategy halt: Sprint D F1 (2026-05-04) — one-shot
            # at first crossing of RETRY_ESCALATE_S, then hourly rollup if
            # still stuck. Previous behavior dispatched a deduped telegram
            # every cycle past the threshold; the dedup TTL was 15 minutes
            # but the same condition kept re-firing each window — 26
            # duplicate pages in 13h on the SimVWapp Pullback /
            # SimDom Pull Back incident.
            #
            # Now: track per-Position _exit_timeout_alerted flag. Telegram
            # fires exactly ONCE on first threshold crossing. If still stuck
            # an hour later, ONE rollup ("still stuck for Xh") fires; that
            # repeats hourly while stuck. When the position finally
            # finalizes (NT8 confirms FLAT), _on_exit_timeout_resolved
            # fires a single RESOLVED telegram (see finalize hook at the
            # top of this method).
            RETRY_ESCALATE_S = 5 * 60   # 5 minutes of retries before paging
            HOURLY_ROLLUP_S  = 3600     # 1 hour between rollup pages
            should_alert = False
            is_initial = False
            is_rollup = False
            if age_s > RETRY_ESCALATE_S:
                if not pos._exit_timeout_alerted:
                    should_alert = True
                    is_initial = True
                elif now - pos._exit_timeout_last_alert_ts >= HOURLY_ROLLUP_S:
                    should_alert = True
                    is_rollup = True

            if should_alert:
                if is_initial:
                    msg = (
                        f"[EXIT_PENDING_TIMEOUT:{pos.trade_id}] {pos.account} "
                        f"still shows {nt8_state[0]} {nt8_state[1]}@{nt8_state[2]} "
                        f"after {age_s:.0f}s — Python thinks flat but NT8 is not. "
                        f"Bot is auto-retrying directional flatten every cycle. "
                        f"Halting strategy '{pos.strategy}' until resolved."
                    )
                    tg_msg = (
                        f"🚨 [EXIT_TIMEOUT] {pos.account} stuck exit_pending "
                        f"{age_s:.0f}s — bot is auto-retrying flatten. {msg}"
                    )
                else:
                    hours = int(age_s // 3600)
                    msg = (
                        f"[EXIT_PENDING_TIMEOUT_STILL_STUCK:{pos.trade_id}] "
                        f"{pos.account} still {nt8_state[0]} after ~{hours}h. "
                        f"Operator action still required."
                    )
                    tg_msg = (
                        f"⚠ [EXIT_TIMEOUT_STILL_STUCK] {pos.account} "
                        f"({pos.strategy}) stuck for ~{hours}h - operator "
                        f"action still required."
                    )
                logger.critical(msg)
                try:
                    from core.telegram_notifier import send_sync
                    send_sync(
                        tg_msg,
                        # Per-attempt key so dedup TTL doesn't re-fire the
                        # SAME message every 15min. The flag-based gating
                        # above is the real one-shot mechanism.
                        dedup_key=(
                            f"exit_pending_timeout:{pos.trade_id}:"
                            f"{'initial' if is_initial else f'rollup_{int(age_s // 3600)}h'}"
                        ),
                    )
                except Exception:
                    pass
                pos._exit_timeout_alerted = True
                pos._exit_timeout_last_alert_ts = now
                # Strategy halt hook — fires once at initial escalation.
                if is_initial:
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
        # dom_pullback deleted 2026-05-21 (b9a3b2e+): 0 trades in 5y
        # canonical backtest. Not a winning strategy. Class + config +
        # registry entries all removed.
        from strategies.orb import OpeningRangeBreakout
        from strategies.noise_area import NoiseAreaMomentum
        from strategies.vwap_band_pullback import VwapBandPullback
        # 2026-05-17 Phase 9.1 hotfix: vwap_band_reversion was added to
        # config/strategies.py but never registered here, so the loader
        # silently skipped it via the "if name not in strategy_classes"
        # guard at line 1235. Discovered when Phase 9 sim deploy showed
        # 14/15 strategies loaded.
        from strategies.vwap_band_reversion import VwapBandReversion
        from strategies.opening_session import OpeningSessionStrategy
        from strategies.footprint_cvd_reversal import FootprintCVDReversal
        from strategies.big_move_signal import BigMoveSignal
        # 2026-05-17: V2 strategy overhaul deployment (Phase 3).
        # 6 new strategy classes from phoenix_lsr_build/strategies/. Each
        # has a unique strategy.name field so it coexists with the V1
        # version until Phase 5 disables the superseded V1 (compression_breakout,
        # vwap_pullback, orb). nq_lsr / orb_fade / compression_breakout_micro
        # are net-new (no V1 counterpart).
        from strategies.nq_lsr import NQLiquiditySweepReversal
        from strategies.orb_fade import ORBFade
        from strategies.orb_v2 import ORBv2
        from strategies.compression_breakout_v2 import CompressionBreakoutV2
        from strategies.compression_breakout_micro import CompressionBreakoutMicro
        from strategies.vwap_pullback_v2 import VWAPPullbackV2
        # 2026-05-18 Phase 12C: ES/NQ confluence LONG strategy. Dormant
        # until MES feed is wired (logs DATA_NOT_AVAILABLE every eval),
        # but registered so the moment market["mes_bars_5m"] starts
        # flowing the strategy auto-activates with no further wiring.
        from strategies.es_nq_confluence import ESNQConfluence
        # 2026-05-19 Phase 13 ship: 4 NEW production strategy classes
        # ported from tools/phoenix_new_strategy_lab.py + phoenix_trend_pullback_lab.py.
        # All four are tick-validated winners with per-strategy exit policy +
        # entry order type in core/exit_policies.PHASE_13_EXIT_ASSIGNMENTS.
        # See docs/STRATEGY_SHIP_AUDIT.md for ship-blocker rationale.
        from strategies.a_asian_continuation import AsianContinuation
        from strategies.e_multi_day_breakout import MultiDayBreakout
        from strategies.g_inside_bar_breakout import InsideBarBreakout
        from strategies.raschke_baseline import RaschkeBaseline

        strategy_classes = {
            "bias_momentum": BiasMomentumFollow,
            "spring_setup": SpringSetup,
            "vwap_pullback": VWAPPullback,
            "vwap_band_pullback": VwapBandPullback,
            # 2026-05-17 Phase 9.1 hotfix: register vwap_band_reversion.
            "vwap_band_reversion": VwapBandReversion,
            "high_precision_only": HighPrecisionOnly,
            "ib_breakout": IBBreakout,
            "compression_breakout": CompressionBreakout,
            # dom_pullback removed 2026-05-21 (0 trades / 5y backtest).
            "orb": OpeningRangeBreakout,
            "noise_area": NoiseAreaMomentum,
            "opening_session": OpeningSessionStrategy,
            "footprint_cvd_reversal": FootprintCVDReversal,
            # 2026-05-15: standalone entry on BigMoveDetector score >= 90.
            # Validation evidence: 15:11:19 score=100 LONG predicted +47pt
            # rally in 8 minutes today (sim). Sim only until n>=30.
            "big_move_signal": BigMoveSignal,
            # 2026-05-17: V2 strategy overhaul deployment (Phase 3).
            # Dormant until Phase 4 adds matching blocks to config.strategies.
            # STRATEGIES — the loop below silently skips entries with no config.
            "nq_lsr": NQLiquiditySweepReversal,
            "orb_fade": ORBFade,
            "orb_v2": ORBv2,
            "compression_breakout_v2": CompressionBreakoutV2,
            "compression_breakout_micro": CompressionBreakoutMicro,
            "vwap_pullback_v2": VWAPPullbackV2,
            # 2026-05-18 Phase 12C: ES/NQ confluence LONG (regime-robust
            # backtest 6/6 years incl. 2022 bear, max DD $72, PF 2.63).
            "es_nq_confluence": ESNQConfluence,
            # 2026-05-19 Phase 13 ship: Phase 13 winners promoted from
            # lab to production class. Each has a tick-validated
            # exit policy + entry order type assignment in
            # core/exit_policies.PHASE_13_EXIT_ASSIGNMENTS that base_bot
            # applies in _apply_phase13_overrides() at signal emit.
            "a_asian_continuation": AsianContinuation,
            "e_multi_day_breakout": MultiDayBreakout,
            "g_inside_bar_breakout": InsideBarBreakout,
            "raschke_baseline": RaschkeBaseline,
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
            # 2026-05-13 (#13): pass bot_name so strategies that opt
            # into state persistence (e.g. orb) can scope their state
            # file per-bot. prod and lab/sim share a Python codebase
            # but must NOT share state files.
            enriched["bot_name"] = self.bot_name
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

            # NOTE: canary filtering happens AFTER this append — see the
            # filter_strategies_for_live() call at the bottom of this method.
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

        # ── LIVE CANARY GATE (2026-05-24, operator directive) ─────────
        # When LIVE_TRADING=True, drop every strategy that isn't in the
        # allowlist + validated + enabled. CRITICAL-logs each rejection.
        # When LIVE_TRADING=False, this is a no-op — sim_bot keeps its
        # full multi-strategy roster for testing. If the live filter
        # leaves zero strategies, REFUSE TO START — silent zero-strategy
        # live mode is the documented anti-pattern (P0-1 / F-04).
        from core.live_canary_gate import filter_strategies_for_live
        from config.settings import LIVE_TRADING as _LIVE
        _before = len(self.strategies)
        self.strategies = filter_strategies_for_live(self.strategies)
        _after = len(self.strategies)
        if _LIVE:
            logger.critical(
                "[CANARY] live filter: %d strategies in → %d kept "
                "(allowlist applied)", _before, _after,
            )
            if _after == 0:
                raise RuntimeError(
                    "LIVE_TRADING=True but the canary filter left ZERO "
                    "strategies. Refusing to start a live bot with no "
                    "strategies — see core/live_canary_gate.py and "
                    "config/settings.py:LIVE_STRATEGY_ALLOWLIST."
                )

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
        asyncio.ensure_future(self._runtime_reconciliation.run())

        # Phase 4C: one-shot [SESSION+GAMMA] regime log fires from inside
        # the tick loop once last_price > 0 and gamma_levels is loaded.
        self._startup_regime_logged = False

        # Start dashboard state pusher in background
        asyncio.ensure_future(self._dashboard_pusher.run())

        # Start heartbeat sender (bridge detects hung bots)
        asyncio.ensure_future(self._heartbeat_sender.run())

        # Start news/momentum scanner in background (Phase 4+)
        asyncio.ensure_future(self._news_scanner_runner.run())

        # Phase 5: Start Telegram command listener
        asyncio.ensure_future(self.telegram_commands.poll_commands(self))

        # MenthorQ gamma reload watcher RETIRED 2026-05-06 (Sprint J)

        # Phase 4B: session-levels prior-day refresh at 00:01 CT daily
        asyncio.ensure_future(self._session_levels_refresher.run())

        # B84: 15:54 CT daily flatten + 15:54:45 fill-confirmation watcher.
        # Subclasses that need post-flatten hooks (e.g. sim_bot's debrief +
        # recap) override _daily_flatten_loop rather than scheduling their
        # own parallel loop.
        asyncio.ensure_future(self._daily_flatten_loop())

        # Hourly decay health check + 15:10 CT daily summary Telegram push.
        asyncio.ensure_future(self._decay_monitor_runner.run())

        # 2026-05-12: application-level WS watchdog. Forces a reconnect
        # if the WS goes silent for >90s outside the NT8 daily-maintenance
        # window. Defends against silent half-close (bridge-side TCP dies
        # without FIN, `async for message in ws` blocks forever). See
        # `bots/_ws_watchdog.py` docstring for the 2026-05-12 08:09 CT incident.
        asyncio.ensure_future(self._ws_watchdog.run())

        # P1-7 (2026-05-25): pending-entry lifecycle sweeper. Cancels any
        # LIMIT entry older than PENDING_ENTRY_TIMEOUT_S (default 90s).
        # See bots/_pending_entry_sweeper.py for the READY_GUARD_S grace
        # period that defers the first sweep until startup reconciliation
        # has had time to adopt pre-existing NT8 orders.
        asyncio.ensure_future(self._pending_entry_sweeper.run())

        # 2026-04-24: FMP market-data cross-check loop. Fetches NDX/QQQ
        # from financialmodelingprep.com, converts to MNQ-equivalent, and
        # compares against the local accepted tick. If local drifts more
        # than 1.5% from FMP on two consecutive checks, writes the HALT
        # marker so circuit_breakers pauses new entries within ~5s. Safe
        # no-op when FMP_API_KEY is unset.
        try:
            from core import fmp_sanity
            # 2026-05-08: was passing `halt_on_divergence_pct=` (legacy
            # name), which fmp_sanity.poll_loop didn't accept — TypeError
            # killed the loop on every bot start. Now uses correct kwarg
            # name; signature also added **legacy_kwargs as a safety net.
            asyncio.ensure_future(fmp_sanity.poll_loop(interval_s=60.0,
                                                      divergence_threshold_pct=0.015))
        except Exception as e:
            logger.warning(f"[FMP] sanity loop failed to start (non-blocking): {e!r}")

        while not self._shutdown_requested:
            try:
                await self._connect_and_listen()
            except Exception as e:
                if self._shutdown_requested:
                    break
                logger.error(f"Connection error: {e}")
            if self._shutdown_requested:
                break
            logger.info("Reconnecting in 5s...")
            await asyncio.sleep(5)
        logger.info("[SHUTDOWN] run() loop exited — process will terminate")

    # _gamma_reload_watcher RETIRED 2026-05-06 (Sprint J).
    # MenthorQ subscription cancelled — no gamma files to reload.

    def _enrich_market_with_gamma(self, market: dict) -> dict:
        """⚠️  No-op since 2026-05-06 (Sprint J cleanup).

        MenthorQ subscription was retired. Method preserved as a stub
        so existing callers don't need changes; just returns the
        market dict unchanged.
        """
        return market

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

    def _flatten_pending_entries(self, reason: str = "emergency_flatten",
                                  account: str | None = None) -> int:
        """P1-7 (2026-05-25): cancel every in-flight LIMIT entry as part of
        an emergency or daily flatten.

        Walks the canonical pending-entry tracker, marks every non-terminal
        entry as ``flattened`` (or scoped to a single account when given),
        writes a CANCEL OIF for each via bridge.oif_writer.write_oif, and
        records a terminal_state row in trade_memory.

        Returns the count of pending entries flattened. Never raises —
        flatten paths are critical and must continue even if cancel OIFs
        fail (operator can clean residual NT8 orders manually).
        """
        try:
            from core.pending_entry_tracker import get_pending_entry_tracker
            tracker = get_pending_entry_tracker()
        except Exception as e:
            logger.error(f"[PENDING_FLATTEN] tracker unavailable: {e!r}")
            return 0

        flattened = tracker.mark_all_flattened(reason=reason, account=account)
        if not flattened:
            return 0

        # Reuse the sweeper's helpers for the CANCEL OIF + trade_memory
        # write so behavior is identical across the timeout / flatten paths.
        try:
            sweeper = getattr(self, "_pending_entry_sweeper", None)
        except Exception:
            sweeper = None
        for pe in flattened:
            # Best-effort clear of the per-account legacy dict.
            try:
                self.positions.clear_pending_entry(pe.account)
            except Exception:
                pass
            if sweeper is not None:
                try:
                    sweeper._emit_cancel_oif(pe)
                except Exception as e:
                    logger.error(
                        f"[PENDING_FLATTEN:{pe.trade_id}] cancel OIF failed: {e!r}"
                    )
                try:
                    sweeper._record_terminal_to_trade_memory(pe)
                except Exception as e:
                    logger.warning(
                        f"[PENDING_FLATTEN:{pe.trade_id}] trade_memory record failed: {e!r}"
                    )
        logger.warning(
            f"[PENDING_FLATTEN] cancelled {len(flattened)} pending entry/entries "
            f"(reason={reason}, account={account or 'all'})"
        )
        return len(flattened)

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
                    # P1-7: also flatten any pending LIMIT entries so they
                    # don't sneak a fill after the 15:55 NT8 Auto Close.
                    try:
                        self._flatten_pending_entries(reason="daily_flatten")
                    except Exception as _pe_err:
                        logger.error(
                            f"[DAILY_FLATTEN] pending-entry flatten failed: {_pe_err!r}"
                        )

                # P2-3 (F-14) 2026-05-24: contract-roll auto-flatten at T-15.
                # Front-month MNQM6 expires 2026-06-19. On roll day, fire
                # flatten_for_roll which (when PHOENIX_ROLL_ENABLED=1) closes
                # all positions and atomically rewrites INSTRUMENT in
                # config/settings.py. Operator must restart the bot post-roll
                # to pick up the new INSTRUMENT into memory (per
                # code_changes_dont_auto_deploy lesson).
                if is_t_minus_15_pre_roll(now_ct):
                    try:
                        await flatten_for_roll(
                            self.positions,
                            ws_send=self._get_ws_send_fn(),
                            now_ct=now_ct,
                        )
                        # P1-7: cancel any in-flight LIMIT entries — the
                        # front-month is about to roll, so any pending
                        # entry against the old contract is obsolete.
                        try:
                            self._flatten_pending_entries(reason="contract_roll")
                        except Exception as _pe_err:
                            logger.error(
                                f"[ROLLOVER] pending-entry flatten failed: {_pe_err!r}"
                            )
                    except Exception as _roll_err:
                        logger.error(
                            f"[ROLLOVER] flatten_for_roll failed: {_roll_err!r}"
                        )

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

    def _handle_dashboard_command(self, cmd: dict):
        """Process a command from the dashboard. Delegates to
        DashboardCommandDispatcher (extracted P4-1 Stage 2)."""
        self._dashboard_commands.handle(cmd)

    async def _connect_and_listen(self):
        """Body extracted P4-1 Stage 4 (2026-05-24) to bots/_ws_dispatcher.py."""
        await self._ws_dispatcher.connect_and_listen()
    async def _process_signal(self, ws, signal):
        """Body extracted P4-1 Stage 4 (2026-05-24) to bots/_signal_router.py."""
        await self._signal_router.process_signal(ws, signal)
    def _on_bar(self, timeframe: str, bar):
        """Called by tick_aggregator when a bar completes."""
        # P4-3 (2026-05-24): tick_to_bar latency record. Stamped from
        # the last tick's bridge-in timestamp (stashed in _connect_and_listen).
        if self._last_t_bridge_in is not None:
            try:
                from core.latency_tracker import get_latency_tracker
                get_latency_tracker().record(
                    "tick_to_bar", self._last_t_bridge_in, time.time(),
                )
            except Exception:
                pass  # latency tracking must never break the bar handler
        # 2026-05-24 P4-1 Stage 2: detector-feeding extracted to
        # bots/_market_enricher.py — pure observability, no OIF/risk/positions.
        self._market_enricher.enrich(timeframe, bar)

        # Evaluate on 1m AND 5m bar completions
        if timeframe not in ("1m", "5m"):
            return

        # Daily reset detection — reset all daily state at midnight
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_date and today != self._current_date:
            logger.info(f"[DAILY RESET] New day: {today}")
            # Sprint D F2: if we paged RECOVERY MODE today, fire the
            # "EXITED RECOVERY" confirmation at the day boundary so the
            # operator sees that yesterday's recovery state cleared.
            if self._recovery_alert_session_date is not None:
                try:
                    asyncio.ensure_future(tg.notify_alert(
                        "RECOVERY EXITED",
                        f"Recovery mode cleared at session reset "
                        f"(was active on {self._recovery_alert_session_date})"
                    ))
                except Exception:
                    pass
            self._recovery_alert_session_date = None
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
            asyncio.ensure_future(self._ai_runners.run_council(market))

        # Phase 4: Debrief — run when transitioning to AFTERHOURS (once per day)
        if (AGENTS_AVAILABLE and AGENT_DEBRIEF_ENABLED
                and regime == "AFTERHOURS"
                and self._last_regime != "AFTERHOURS"
                and not self._debrief_ran_today):
            self._debrief_ran_today = True
            asyncio.ensure_future(self._ai_runners.run_debrief())

        # Record daily momentum score at CLOSE_CHOP→AFTERHOURS transition (EOD)
        # This captures the day's final momentum state for multi-day trajectory tracking
        if regime == "AFTERHOURS" and self._last_regime not in ("AFTERHOURS", None):
            try:
                from core.momentum_score import record_daily
                # 2026-05-06 Sprint J: was passing MQ snapshot; momentum
                # score now ignores mq_snap argument (HVL factor retired).
                _eod_market = self.aggregator.snapshot()
                eod_rec = record_daily(_eod_market, None)
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
        """Run all enabled strategies, pick best signal.

        Body extracted 2026-05-24 (P4-1 Stage 3) to
        bots/_strategy_dispatch.py — behaviorally verbatim. See that
        module for read/write surface.
        """
        self._strategy_dispatch.evaluate()

    async def _enter_trade(self, ws, signal: Signal):
        """Body extracted P4-1 Stage 4 (2026-05-24) to bots/_trade_entry.py.
        CRITICAL EXECUTION PATH. Live blast radius bounded by canary gate."""
        await self._trade_entry.enter_trade(ws, signal)
    async def _scale_out_trade(self, ws, price: float):
        """Body extracted P4-1 Stage 4 (2026-05-24) to bots/_scale_out.py."""
        await self._scale_out.scale_out_trade(ws, price)
    def _on_trade_closed(self, trade: dict) -> None:
        """Post-trade bookkeeping. Extracted to bots/_trade_closer.py
        (P4-1 Stage 3, 2026-05-24). SimBot.super()._on_trade_closed(trade)
        keeps working via this slim wrapper."""
        self._trade_closer.on_trade_closed(trade)

    async def _exit_trade(self, ws, price: float, reason: str,
                          trade_id: str | None = None):
        """Body extracted P4-1 Stage 4 (2026-05-24) to bots/_trade_exit.py."""
        await self._trade_exit.exit_trade(ws, price, reason, trade_id)
    def _menthorq_to_dict(self) -> dict:
        from bots._dashboard_serializers import menthorq_to_dict
        return menthorq_to_dict(self)

    def _cr_to_dict(self) -> dict:
        from bots._dashboard_serializers import cr_to_dict
        return cr_to_dict(self)

    def to_dict(self) -> dict:
        from bots._dashboard_serializers import bot_to_dict
        return bot_to_dict(self)

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
        # P3-1 (2026-05-24): wire the two additional sliders that the
        # dashboard exposes (min_trade_spacing, max_trades_per_session).
        # Without this, slider movements only mutate _runtime_params but
        # never reach RiskManager — the dashboard UI looks live but the
        # bot ignores the values.
        if "min_trade_spacing" in updates:
            try:
                self.risk.set_trade_spacing(int(updates["min_trade_spacing"]))
            except (TypeError, ValueError):
                pass
        if "max_trades_per_session" in updates:
            try:
                self.risk.set_max_trades(int(updates["max_trades_per_session"]))
            except (TypeError, ValueError):
                pass
