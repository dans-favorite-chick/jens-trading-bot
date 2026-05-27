"""Trade exit — extracted from base_bot.py 2026-05-24 (P4-1 Stage 4).

⚠ CRITICAL EXECUTION PATH. Writes OIF (via _sink_submit_exit and
related). Handles EXIT_PENDING debounce, OCO-vs-CLOSEPOSITION race
fix (Phase 9.5 Incident #1), Phase 13 BIG_MOVE_EXIT preservation,
trail-stop completion, position close, _on_trade_closed dispatch.

Live blast radius bounded by core/live_canary_gate.py.

Original location: bots/base_bot.py async def _exit_trade.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from bots._oif_emitter import submit_exit as _sink_submit_exit
from core import telegram_notifier as tg

logger = logging.getLogger("TradeExit")


class TradeExit:
    def __init__(self, bot):
        self.bot = bot

    async def exit_trade(self, ws, price: float, reason: str,
                          trade_id: str | None = None) -> None:
        """Body of BaseBot._exit_trade, behaviorally verbatim.

        Execute exit: send to NT8 FIRST, then close Python state.

        Phase C: trade_id optional. When supplied, exits that specific
        position (used by the multi-position tick-exit iteration at the
        top of _connect_and_listen). When None, falls back to the sole
        active position (legacy single-position behavior).
        """
        # P4-2 (2026-05-24): the position carries the trace_id (persisted via
        # market_snapshot at open_position time in _trade_entry). Rebind it
        # for the exit context so OCO race logs, exit OIF writes, expectancy
        # close, RAG add, telegram fire, conflict-close logging all carry
        # the same [TRACE:xxx] prefix as the entry.
        _trace = None
        try:
            _pos_for_trace = (
                self.bot.positions.get_position(trade_id) if trade_id is not None
                else self.bot.positions.position
            )
            if _pos_for_trace is not None:
                _trace = getattr(_pos_for_trace, 'trace_id', None) or (
                    (_pos_for_trace.market_snapshot or {}).get('trace_id')
                    if hasattr(_pos_for_trace, 'market_snapshot') else None
                )
        except Exception:
            _trace = None
        from core.trace_id import TraceContext as _TraceContext
        with _TraceContext(_trace):
            if self.bot.positions.is_flat:
                return

            if trade_id is not None:
                pos = self.bot.positions.get_position(trade_id)
            else:
                pos = self.bot.positions.position
            if pos is None:
                return
            tid = pos.trade_id

            # 2026-04-24 debounce. On a reconciled-phantom position at 09:29:07
            # the exit path fired hundreds of times in 500ms (see
            # logs/sim_bot_stdout.log: 94,433 EXIT_PENDING lines lifetime). Once
            # an exit is in flight for a trade_id, suppress duplicate sends for
            # 2 seconds — runtime reconciliation (every 30s) is the proper
            # driver for unwinding pending exits, not the tick-exit loop.
            if not hasattr(self.bot, "_last_exit_send_ts"):
                self.bot._last_exit_send_ts: dict[str, float] = {}
            _now = time.time()
            _prev = self.bot._last_exit_send_ts.get(tid, 0.0)
            if _now - _prev < 2.0:
                logger.debug(f"[EXIT_PENDING:{tid}] debounce skip — last send {_now - _prev:.2f}s ago")
                return
            self.bot._last_exit_send_ts[tid] = _now

            self.bot.status = "EXIT_PENDING"
            logger.info(f"[EXIT_PENDING:{tid}] Sending exit for {pos.direction} @ {price:.2f}, reason={reason}")

            # 2026-05-17 Phase 9.5 Incident #1 fix: CLOSEPOSITION-vs-OCO race.
            #
            # When the exit reason is "stop_loss" or "target_hit", NT8's OCO
            # bracket is ALREADY firing (that's what triggered our detection).
            # Sending an additional EXIT (which becomes CLOSEPOSITION) creates
            # a race: both arrive at NT8 simultaneously, the OCO closes the
            # position first, the CLOSEPOSITION then hits a FLAT account and
            # NT8 interprets it as a fresh BUY/SELL — phantom reverse position.
            # Reconciler catches it ~25s later and market-flattens, but slippage
            # on the round-trip 4x'd the loss on 2026-05-17 21:25 SimBias
            # Momentum trade (-$54.82 realized vs -$12.50 intent).
            #
            # Fix: for stop/target exits, SKIP the EXIT WS send. Just transition
            # to EXIT_PENDING and wait for NT8's FLAT confirmation. The runtime
            # reconciler (every 30s) detects the FLAT state and finalizes the
            # Python position. If the OCO somehow doesn't fire (silently
            # cancelled by some other race), the existing retry-loop at
            # position-management lines 1067-1108 kicks in after
            # EXIT_PENDING_TIMEOUT_S (60s) and sends a directional MARKET as
            # backup — that path already exists and is race-safe.
            #
            # For all OTHER exit reasons (managed exits like cvd_flip,
            # ema_dom_exit, time_exit, BIG_MOVE_EXIT, etc.), the OCO is NOT
            # firing; the bot must actively close. Keep existing EXIT WS send
            # for those. (Future hardening: convert THOSE to directional MARKET
            # too, eliminating CLOSEPOSITION entirely. Out of scope here.)
            _OCO_HANDLED_REASONS = ("stop_loss", "target_hit")
            exit_sent = False
            if reason in _OCO_HANDLED_REASONS:
                logger.info(
                    f"[EXIT_PENDING:{tid}] reason={reason} — OCO handles this. "
                    f"Skipping EXIT WS send to avoid CLOSEPOSITION-vs-OCO race. "
                    f"Reconciler will finalize once NT8 reports FLAT."
                )
                exit_sent = True  # not actually sent — just gate the fallback path
            else:
                # STEP 1: B75 — SKIP pre-exit CANCELALLORDERS. Rely on NT8 OCO
                # auto-cancel: when the EXIT MARKET order fills, position goes
                # flat, and NT8 automatically cancels the orphaned OCO stop +
                # target because their OCO group detects position closure.
                # Pre-B75 behavior (CANCELALLORDERS before EXIT) wiped OCOs on
                # every connected account because NT8 ATI ignores the account
                # field on CANCELALLORDERS. Send EXIT directly — OCO cleans up.
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
            self.bot._rider_active = False
            pos.rider_mode = False if self.bot.positions.position else False

            # B70: capture pre-close conflict state so we can emit a
            # conflict_closed event if this exit resolves a conflict pair.
            _pre_close_conflicts: list[dict] = []
            _closing_pos_snapshot = None
            try:
                from core.strategy_risk_registry import StrategyRiskRegistry
                _reg = getattr(self.bot, "_conflict_reg", None)
                if _reg is None:
                    _reg = StrategyRiskRegistry()
                    self.bot._conflict_reg = _reg
                _pre_close_conflicts = _reg.detect_directional_conflicts(self.bot.positions)
                _closing_pos_snapshot = self.bot.positions.get_position(tid)
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
                self.bot.positions.mark_exit_pending(tid, price, reason)
                trade = None  # finalized later by runtime reconciliation
            else:
                logger.critical(
                    f"[EXIT_FORCE_CLOSE:{tid}] NT8 exit send failed; force-"
                    f"closing Python state to avoid leaked Position record. "
                    f"Operator MUST verify NT8 is flat on {pos.account}."
                )
                trade = self.bot.positions.close_position(price, reason, trade_id=tid)

            # P1-3 (F-07/F-20) portfolio gate exit hook — drop the closed
            # position from the rolling-window so its contracts free up
            # capacity for subsequent entries (otherwise the entry would
            # sit there until the natural 60s TTL). Gate is exception-safe.
            try:
                _gate = getattr(self.bot, "_portfolio_risk_gate", None)
                if _gate is not None:
                    _gate.record_exit(tid)
            except Exception:
                pass  # gate must never break the exit path

            # B70: if the closing trade was in any conflict pair, log it.
            try:
                if _pre_close_conflicts and _closing_pos_snapshot is not None:
                    was_involved = any(
                        tid in (c["trade_id_a"], c["trade_id_b"])
                        for c in _pre_close_conflicts
                    )
                    if was_involved:
                        from core import conflict_logger as _cflog
                        _reg = self.bot._conflict_reg
                        remaining = _reg.detect_directional_conflicts(self.bot.positions)
                        exposure = _reg.exposure_snapshot(self.bot.positions)
                        _cflog.log_conflict_closed(
                            _closing_pos_snapshot, remaining, exposure,
                        )
            except Exception as _e:
                logger.warning(f"[CONFLICT] post-close logging failed: {_e}")
            if trade:
                self.bot.risk.record_trade(trade["pnl_dollars"])
                self.bot.trade_memory.record(trade, bot_id=self.bot.bot_name)
                self.bot.tracker.record_trade(trade)
                self.bot._on_trade_closed(trade)  # P3: wire circuit breakers (stub; P10a completes wiring)

                # P1-8 (2026-05-24): clear persisted stop_order_id so
                # data/active_stops.json doesn't grow unbounded and stale IDs
                # from closed trades can't be cancel-replaced by accident.
                # Wrapped — a clear failure NEVER blocks exit-path bookkeeping.
                try:
                    from core.nt8_order_id_capture import clear_stop_id
                    clear_stop_id(tid)
                except Exception as _clr_e:
                    logger.debug(f"[STOP_ID_CLEAR_FAIL:{tid}] {_clr_e}")

                # Phase 6: Close expectancy tracking BEFORE log_exit so MAE/MFE is included
                exp_analysis = self.bot.expectancy.close_trade(
                    exit_price=price,
                    pnl_ticks=trade["pnl_ticks"],
                    result=trade["result"],
                )

                # Log exit with MAE/MFE data from expectancy engine
                market_snap = self.bot.aggregator.snapshot()
                # B14 Phase 4: inject gamma context into exit snapshot.
                self.bot._enrich_market_with_gamma(market_snap)
                if exp_analysis:
                    market_snap["mae_ticks"] = exp_analysis.get("mae_ticks")
                    market_snap["mfe_ticks"] = exp_analysis.get("mfe_ticks")
                    market_snap["capture_ratio"] = exp_analysis.get("edge_captured_pct")
                    market_snap["went_red_first"] = exp_analysis.get("went_red_first")
                    market_snap["mae_time_s"] = exp_analysis.get("mae_time_s")
                    market_snap["mfe_time_s"] = exp_analysis.get("mfe_time_s")
                self.bot.history.log_exit(trade, market_snap)

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
                    self.bot.trade_rag.add_trade(trade, market_snap, rag_outcome)
                except Exception as e:
                    logger.debug(f"[RAG] add_trade error (non-blocking): {e}")

                # Phase 6: Learn fingerprint from losses
                if trade["result"] == "LOSS":
                    self.bot.no_trade_fp.learn_from_trade(trade, self.bot.aggregator.snapshot())

                # Phase 6b: Counter-edge learning from losses
                if trade["result"] == "LOSS":
                    try:
                        self.bot.counter_edge.learn_from_loss(trade)
                    except Exception as e:
                        logger.debug(f"[COUNTER] Learn error (non-blocking): {e}")

                # Phase 6b: Execution quality tracking
                try:
                    snapshot = trade.get("market_snapshot", {})
                    self.bot.execution_quality.record(
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
                self.bot._trades_since_cluster += 1
                if self.bot._trades_since_cluster >= 10:
                    self.bot._trades_since_cluster = 0
                    try:
                        self.bot._clustering_result = self.bot.trade_clustering.analyze(
                            self.bot.trade_memory.recent(200))
                        for rec in (self.bot._clustering_result.get("recommendations") or [])[:3]:
                            logger.info(f"[CLUSTERING] {rec}")
                    except Exception:
                        pass

                # Sprint D F2 (2026-05-04): RECOVERY MODE one-shot per day.
                # Previously fired every loss after the threshold — ~5 pages
                # per recovery day. Now fires ONCE on the first transition
                # into recovery for a given session date, and an "EXITED
                # RECOVERY" telegram fires once at the next daily reset.
                if self.bot.risk.state.recovery_mode and trade["result"] == "LOSS":
                    today = datetime.now().date()
                    if self.bot._recovery_alert_session_date != today:
                        asyncio.ensure_future(tg.notify_alert(
                            "RECOVERY MODE",
                            f"Daily P&L: ${self.bot.risk.state.daily_pnl:.2f}\n"
                            f"Size reduced 50% until daily reset"))
                        self.bot._recovery_alert_session_date = today
                        logger.info(
                            f"[RECOVERY_ALERT] one-shot fired for {today}; "
                            f"daily P&L=${self.bot.risk.state.daily_pnl:.2f}"
                        )

                logger.info(f"[EXIT:{tid}] P&L=${trade['pnl_dollars']:.2f} reason={reason} "
                             f"exit_sent={'OK' if exit_sent else 'FAILED'}")

            self.bot.status = "SCANNING"
