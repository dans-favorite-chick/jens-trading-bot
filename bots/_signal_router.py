"""Signal router — extracted from base_bot.py 2026-05-24 (P4-1 Stage 4).

Receives signals from the eval pipeline, applies pre-trade AI filter
(disabled by P0-4), dispatches to _enter_trade. Currently a thin
router; pending-order lifecycle state machine (P1-7) will live here
in a follow-up.

Live blast radius bounded by core/live_canary_gate.py.

Original location: bots/base_bot.py async def _process_signal.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from config.settings import (
    AGENT_PRETRADE_FILTER_ENABLED,
)
from core import telegram_notifier as tg

# Mirror base_bot's AGENTS_AVAILABLE / pretrade_filter import shape so a
# missing agents/ package degrades to "filter disabled" instead of
# blowing up on import.
try:
    from agents import pretrade_filter  # noqa: F401
    AGENTS_AVAILABLE = True
except ImportError:
    AGENTS_AVAILABLE = False

# NOTE: `_apply_phase13_overrides` is imported lazily inside process_signal
# (rather than at module scope) because base_bot imports this module — a
# top-level `from bots.base_bot import ...` would create a circular-import
# deadlock at base_bot load time. This pattern mirrors _trade_closer,
# _oif_emitter, etc.

logger = logging.getLogger("SignalRouter")


class SignalRouter:
    def __init__(self, bot):
        self.bot = bot

    async def process_signal(self, ws, signal) -> None:
        """Process a pending signal: run AI filter, then enter trade.
        Called inside asyncio.wait_for(timeout=15s) so it can never
        freeze the tick loop.
        """
        # P4-2 (2026-05-24): bind the per-signal trace ID to this context
        # so every downstream log call (including across `await` into
        # _enter_trade, pretrade_filter, ws.send, etc.) is auto-stamped
        # with `[TRACE:xxx]` by the filter installed in core.trace_id.
        # Lazy-imported to keep this module's import surface unchanged.
        from core.trace_id import TraceContext as _TraceContext
        with _TraceContext(getattr(signal, "trace_id", None)):
            # ── 2026-05-20 PHASE 13 SHIP AUDIT pt2 (F-010): lunch-skip ──
            # PHOENIX_BEST_PLAN.md §D.2: skip all signals 10:00-13:59 CT
            # ("lunch zone" — whippy + low edge, costs ~$5K/yr in 5y backtest).
            # Strategies whose window ends before 10:00 CT (opening_session,
            # a_asian_continuation) won't see this fire. Strategies that
            # WANT lunch coverage can opt out via SKIP_HOURS_CT_EXEMPT.
            try:
                from config.settings import (
                    SKIP_HOURS_CT_ENABLED, SKIP_HOURS_CT, SKIP_HOURS_CT_EXEMPT
                )
                if SKIP_HOURS_CT_ENABLED:
                    _now_ct = self.bot.session.now_ct() if hasattr(self.bot.session, "now_ct") else None
                    if _now_ct is None:
                        from zoneinfo import ZoneInfo
                        _now_ct = datetime.now(ZoneInfo("America/Chicago"))
                    if (_now_ct.hour in SKIP_HOURS_CT
                            and signal.strategy not in SKIP_HOURS_CT_EXEMPT):
                        logger.info(
                            f"[HOUR_SKIP] {signal.strategy} {signal.direction}: "
                            f"skipped (now {_now_ct.strftime('%H:%M')} CT in "
                            f"lunch-zone {SKIP_HOURS_CT}) per F-010 universal filter"
                        )
                        self.bot.last_rejection = (
                            f"Lunch-zone skip {_now_ct.strftime('%H:%M')} CT"
                        )
                        return
            except Exception as _e:
                logger.debug(f"[HOUR_SKIP] filter check error (non-blocking): {_e!r}")

            # ── PHASE 13 SECTION U: tick-validated per-strategy overrides ──
            # Apply per-strategy entry order_type AND exit policy from the
            # canonical assignments in core/exit_policies.py. Safe no-op if
            # the strategy isn't in the registry — strategies that aren't
            # listed there keep their Signal-provided defaults.
            try:
                from bots.base_bot import _apply_phase13_overrides
                _apply_phase13_overrides(signal)
            except Exception as _e:
                logger.warning(f"[Phase13 override] {_e!r}")

            # Phase 4: Pre-trade filter (3s timeout, defaults to CLEAR)
            if AGENTS_AVAILABLE and AGENT_PRETRADE_FILTER_ENABLED:
                try:
                    market_snap = self.bot.aggregator.snapshot()
                    regime = self.bot.session.get_current_regime()
                    recent = self.bot.trade_memory.recent(5)

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
                        strat_results = self.bot.knowledge_rag.query_strategies(query, n_results=3)
                        if strat_results:
                            lines = []
                            for sr in strat_results:
                                lines.append(f"- {sr['title']} ({sr['category']}): "
                                              f"regimes={sr['regimes']}, ATR={sr['atr_preference']}")
                            strategy_context = "\n".join(lines)
                    except Exception:
                        pass

                    # 2026-05-06 Sprint J: removed MenthorQ regime context
                    # injection (subscription retired). AI filter now reads
                    # only structural_bias / footprint context.

                    # Inject Continuation/Reversal assessment (Quinn-style)
                    try:
                        if hasattr(self.bot, "_last_cr") and self.bot._last_cr is not None:
                            from core.continuation_reversal import to_prompt_context as cr_prompt
                            cr_block = cr_prompt(self.bot._last_cr)
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
                    self.bot._filter_verdict = {
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
                        self.bot.last_rejection = f"AI filter (blocking): {verdict.reason}"
                        logger.info(f"[FILTER] SIT_OUT (blocking mode) — skipping {signal.strategy}")
                        return
                    # advisory mode or non-SIT_OUT verdict: trade proceeds; CAUTION handled in _enter_trade
                except Exception as e:
                    logger.warning(f"[FILTER] Error (defaulting to CLEAR): {e}")
                    self.bot._filter_verdict = {"action": "CLEAR", "reason": f"Error: {e}", "source": "default"}

            try:
                await self.bot._enter_trade(ws, signal)
            except Exception as e:
                logger.error(f"[ENTRY ERROR] _enter_trade crashed: {e}")
                self.bot.last_rejection = f"Entry error: {e}"
