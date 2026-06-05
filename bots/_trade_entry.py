"""Trade entry — extracted from base_bot.py 2026-05-24 (P4-1 Stage 4).

CRITICAL EXECUTION PATH. This module is the active entry-write path.
Calls OIF wrappers (_sink_submit_place / _sink_submit_protect), runs
risk gates, sets stops, applies the P1-1 enrichment merge, the P2-3
roll gate, and the P4-3 signal_to_oif latency stamp.

Live blast radius is bounded by core/live_canary_gate.py — only the
strategies in LIVE_STRATEGY_ALLOWLIST (currently bias_momentum) reach
live execution. Sim mode runs everything.

Behavior preservation guarantee: every gate, every log line, every
try/except is verbatim from the original BaseBot._enter_trade. Every
`self.X` BaseBot reference has been rewritten to `self.bot.X`. The
following in-method edits made earlier this session are preserved:
  - P1-1 Stage 1 enrichment merge (post-snapshot, pre-price-extract)
  - P2-3 (F-14) roll entry gate (right after the 15:53 window check)
  - P4-3 (F-23) signal_to_oif latency wrap around the ws.send() call

Logger name is "Bot" — IDENTICAL to base_bot.py:148. Operator greps
log files and tools/daily_session_summary.py filter on this exact
prefix. If you change it, every "[INTENT:...]" / "[FILLED:...]" /
"[PROTECT:...]" log line emitted by this module will route under a
different name and break the dashboard's log filter.

Original location: bots/base_bot.py async def _enter_trade
(lines 2862-3918 as of extraction, 1057 LOC body).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from config.settings import (
    TICK_SIZE, LIVE_TRADING, ENTRY_ORDER_TYPE, LIMIT_OFFSET_TICKS,
    SCALE_OUT_ENABLED, SCALE_OUT_RR, TREND_RIDER_ENABLED,
    ATR_STOP_ENABLED, ATR_STOP_TF, ATR_STOP_MULTIPLIER,
    ATR_STOP_MIN_TICKS, ATR_STOP_MAX_TICKS,
    INSTRUMENT as _INSTRUMENT_DEFAULT,
)
# P1-7 (2026-05-25): pending-entry lifecycle tracker. Registered for every
# LIMIT submit (NOT MARKET, since MARKET fills immediately).
from core.pending_entry_tracker import get_pending_entry_tracker as _get_pe_tracker
from core import telegram_notifier as tg
from core import signal_visualizer as _signal_viz
from core.contract_rollover import is_no_new_entries_for_roll

from strategies.base_strategy import Signal

# Logger name preserved as "Bot" — IDENTICAL to base_bot.py:148.
logger = logging.getLogger("Bot")


def should_run_inverse_phantom_guard(account, position_manager) -> bool:
    """REDTEAM-1-FIX-3B: decide whether to run the B50 inverse-phantom
    `verify_nt8_position` check before a new entry on ``account``.

    Returns True for any non-empty, non-Sim101 account (unchanged behavior).

    Returns True for Sim101 iff the local PositionManager has zero
    NON-RECONCILED open positions on Sim101 — i.e. either the slot is
    empty or the only open positions there are reconciled orphans. In
    that case the guard MUST run, because any NT8-reported position
    discovered by ``verify_nt8_position`` is by definition an
    orphan-vs-real-signal double-fill in flight.

    Returns False for Sim101 when at least one non-reconciled (real
    bot) position is already open there — the legit multi-strategy
    concurrent-entry path. The verify would false-positive on that
    real bot position.

    Returns False for an empty/None account string (defensive — no
    routing target means nothing to verify against).

    Extracted to module scope so the regression can be unit-tested
    without rebuilding the full TradeEntry stack (await_fill_confirmation,
    OIF writer, pending_entry_tracker, telegram, history). See
    ``tests/test_trade_entry_inverse_phantom_guard.py``.
    """
    if not account:
        return False
    if account != "Sim101":
        return True
    open_on_sim101 = [
        p for p in position_manager.active_positions
        if getattr(p, "account", None) == "Sim101"
    ]
    # If empty OR every open position is reconciled (orphan), run the guard.
    return all(p.reconciled for p in open_on_sim101)


class TradeEntry:
    """Wraps BaseBot._enter_trade. See module docstring for the full
    read/write surface and behavior-preservation invariants."""

    def __init__(self, bot):
        self.bot = bot

    async def enter_trade(self, ws, signal: Signal) -> None:
        """Execute entry via bridge → OIF with fill confirmation."""
        # P4-2 (2026-05-24): bind the per-signal trace ID to this context
        # so every downstream log (OIF write, fill wait, NT8 verify, OCO
        # attach, position open, expectancy start, telegram fire) auto-
        # carries the [TRACE:xxx] prefix via the root logging filter.
        # Lazy-imported to keep this module's import surface unchanged.
        from core.trace_id import TraceContext as _TraceContext
        with _TraceContext(getattr(signal, 'trace_id', None)):
            # Lazy module-level helpers from base_bot — imported here to avoid
            # circular imports at module load time.
            from bots.base_bot import (
                _sink_submit_place, _sink_submit_protect, _sink_submit_exit,
                _sanity_check_entry, recompute_phase13_target,
            )

            # B84 no-new-entries gate. From 15:53 CT until the next globex
            # session opens at 17:00 CT, the bot refuses new positions so
            # we don't take a trade that would immediately be unwound by
            # the 15:54 flatten (or race the NT8 Auto Close at 15:55).
            if self.bot._is_no_new_entries_window():
                logger.info(
                    f"[NO_NEW_ENTRIES:{signal.trade_id}] {signal.strategy} "
                    f"{signal.direction} @ signal — rejected (within the "
                    f"15:53→17:00 CT pre-flatten window)"
                )
                self.bot.last_rejection = "Within 15:53-17:00 CT no-new-entries window"
                return

            # P2-3 (F-14) 2026-05-24: contract-roll entry gate.
            if is_no_new_entries_for_roll():
                logger.info(
                    f"[NO_NEW_ENTRIES_ROLL:{signal.trade_id}] {signal.strategy} "
                    f"{signal.direction} — rejected (contract roll window)"
                )
                self.bot.last_rejection = "Contract roll window — no new entries"
                return

            # 2026-06-02: NT8-sink-health gate (auto-pause from
            # PROTECT-all-3-retries-failed). Lazy import so this module's
            # import surface is unchanged.
            from core.nt8_sink_health import get_sink_health
            _sink = get_sink_health(self.bot.bot_name)
            if _sink.is_paused():
                logger.info(
                    f"[SKIP_NT8_DEAD:{signal.trade_id}] {signal.strategy} "
                    f"{signal.direction} — NT8 sink paused since "
                    f"{_sink.state.paused_at} ({_sink.state.paused_reason})"
                )
                self.bot.last_rejection = (
                    f"NT8 sink paused: {_sink.state.paused_reason}"
                )
                try:
                    sig_dict = {
                        "direction": signal.direction,
                        "strategy": signal.strategy,
                        "confidence": signal.confidence,
                        "entry_score": signal.entry_score,
                        "reason": signal.reason,
                    }
                    # Gate must be cheap — skip aggregator snapshot here.
                    self.bot.history.log_near_miss(sig_dict, {}, "nt8_sink_paused")
                except Exception:
                    pass
                return

            market = self.bot.aggregator.snapshot()
            # 2026-05-24 P1-1 Stage 1: merge strategy-time enrichment fields
            # (day_type, cr_verdict, cvd_health, cvd_health_short, es_nq_rs,
            # intermarket, advisor_guidance, mq_direction_bias) that aren't in
            # the fresh aggregator snapshot. Only fields NOT already in `market`
            # are merged — fresh values (price, ATRs at execution time) are
            # preserved. This makes trade_memory's market_snapshot record what
            # the strategy actually evaluated against.
            # Pure observability change — does not affect trade execution.
            if self.bot._last_enriched_market:
                for _k in (
                    "day_type", "day_type_reason", "cr_verdict", "cr_mom_score",
                    "cr_direction", "cr_confidence", "cr_at_resistance",
                    "cr_at_support", "cvd_health", "cvd_health_short",
                    "es_nq_rs", "intermarket", "advisor_guidance",
                    "mq_direction_bias",
                ):
                    if _k in self.bot._last_enriched_market and _k not in market:
                        market[_k] = self.bot._last_enriched_market[_k]
            price = market.get("price", 0)
            atr_5m = market.get("atr_5m", 0)
            tid = signal.trade_id

            # Risk sizing (use ATR-based VIX proxy for volatility adjustment)
            vix_proxy = min(50, atr_5m / 4) if atr_5m > 0 else 0
            risk_dollars, tier = self.bot.risk.get_risk_for_entry(signal.entry_score, vix=vix_proxy)
            if risk_dollars <= 0:
                self.bot.last_rejection = f"Risk tier SKIP (score={signal.entry_score})"
                # Phase 7: Log near-miss
                try:
                    sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                                "confidence": signal.confidence, "entry_score": signal.entry_score,
                                "reason": signal.reason}
                    self.bot.history.log_near_miss(sig_dict, market, "risk_tier_skip")
                    self.bot.trade_rag.add_near_miss(sig_dict, market)
                except Exception:
                    pass
                return

            # Phase 8: Calendar risk size adjustment
            try:
                cal_adj = self.bot.calendar_risk.get_risk_adjustment()
                if cal_adj.size_multiplier < 1.0:
                    risk_dollars *= cal_adj.size_multiplier
                    logger.info(f"[{tid}:CALENDAR] size={cal_adj.size_multiplier:.1f}x "
                                 f"→ risk=${risk_dollars:.2f} ({cal_adj.reason})")
            except Exception:
                pass

            # Phase 8: Intermarket risk adjustment
            try:
                im_risk = self.bot.intermarket.get_risk_signal()
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
                pb_risk = self.bot.playbook_mgr.get_risk_overrides()
                pb_size = pb_risk.get("size_multiplier", 1.0)
                if pb_size != 1.0:
                    risk_dollars *= pb_size
                    logger.info(f"[{tid}:PLAYBOOK] size={pb_size:.1f}x → risk=${risk_dollars:.2f}")
            except Exception:
                pass

            # Apply session regime size_multiplier
            size_mult = self.bot.session.get_size_multiplier()
            if size_mult < 1.0:
                risk_dollars *= size_mult
                logger.info(f"[{tid}] Regime size_mult={size_mult:.1f}x → risk=${risk_dollars:.2f}")

            # Phase 6: Apply regime transition size boost
            regime = self.bot.session.get_current_regime()
            transition_bonus = self.bot.regime_transitions.get_transition_bonus(regime)
            if transition_bonus.get("active") and transition_bonus["size_boost"] != 1.0:
                old_risk = risk_dollars
                risk_dollars *= transition_bonus["size_boost"]
                logger.info(f"[{tid}:TRANSITION] size_boost={transition_bonus['size_boost']:.1f}x "
                             f"→ risk ${old_risk:.2f} → ${risk_dollars:.2f} "
                             f"({transition_bonus['description']})")

            # Phase 4: CAUTION verdict = 50% size reduction
            if (self.bot._filter_verdict and self.bot._filter_verdict.get("action") == "CAUTION"):
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
            stop_ticks = self.bot.risk.calculate_stop_ticks(signal.stop_ticks, atr_5m)

            # Phase 8: Calendar risk stop widening (post-event volatility expansion)
            try:
                cal_adj = self.bot.calendar_risk.get_risk_adjustment()
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
                (s for s in self.bot.strategies if s.name == signal.strategy), None
            )
            contracts = self.bot.risk.calculate_contracts(
                risk_dollars, stop_ticks, strategy=_strat_obj
            )

            # ── F-001 sizing dispatcher (2026-05-20) ────────────────────
            # When SIZING_MODE="tier_3000", route through core.tier_sizer for
            # compounding contracts (1 per $3K equity, ATH scale-down, daily
            # circuit breaker, 3-loss halving, per-strategy multipliers).
            # Default "flat_1" preserves the legacy PositionScaler path so
            # nothing changes for existing operators until they opt in.
            try:
                from config.settings import SIZING_MODE as _SIZING_MODE
            except Exception:
                _SIZING_MODE = "flat_1"

            if _SIZING_MODE == "tier_3000":
                try:
                    from core.tier_sizer import compute_contracts as _tier_compute
                    tier_contracts = _tier_compute(
                        strategy=signal.strategy,
                        score=signal.entry_score,
                    )
                except Exception as _e:
                    logger.warning(
                        f"[{tid}:TIER_SIZER] failed ({_e!r}) — falling back to flat_1"
                    )
                    tier_contracts = 1

                # tier_compute returns 0 when the daily circuit breaker has
                # tripped. Skip the entry outright in that case.
                if tier_contracts <= 0:
                    logger.warning(
                        f"[{tid}:TIER_SIZER] HALT — entry skipped "
                        f"(daily circuit breaker tripped or 0 contracts)"
                    )
                    self.bot.last_rejection = "tier_sizer halt (daily breaker)"
                    try:
                        sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                                    "confidence": signal.confidence, "entry_score": signal.entry_score,
                                    "reason": signal.reason}
                        self.bot.history.log_near_miss(sig_dict, market, "tier_sizer_halt")
                    except Exception:
                        pass
                    return

                # Tier_3000 OWNS the contract count — clamp the risk-based
                # sizing UP TO the tier ceiling. (We still keep the risk-based
                # `contracts` as a floor so a wide-stop trade doesn't blow
                # past the per-trade risk budget.)
                logger.info(
                    f"[{tid}:TIER_SIZER] mode=tier_3000 strategy={signal.strategy} "
                    f"risk_sized={contracts} tier_sized={tier_contracts} -> using "
                    f"{min(contracts, tier_contracts) if contracts > 0 else tier_contracts}"
                )
                contracts = min(contracts, tier_contracts) if contracts > 0 else tier_contracts
            else:
                # Phase 5: Position scaler — cap contracts by account equity and conditions
                max_contracts = self.bot.position_scaler.get_max_contracts(
                    account_equity=self.bot.risk._risk_per_trade * 50,  # Approximate equity from risk setting
                    entry_score=signal.entry_score,
                    regime=regime,
                )
                contracts = min(contracts, max_contracts)

            # Reject 0-contract entries — never send to bridge
            if contracts < 1:
                logger.warning(f"[{tid}] Computed 0 contracts (risk=${risk_dollars:.2f}, "
                                f"stop={stop_ticks}t) — skipping entry")
                self.bot.last_rejection = f"0 contracts computed (risk too low for stop distance)"
                # Phase 7: Log near-miss
                try:
                    sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                                "confidence": signal.confidence, "entry_score": signal.entry_score,
                                "reason": signal.reason}
                    self.bot.history.log_near_miss(sig_dict, market, "zero_contracts")
                    self.bot.trade_rag.add_near_miss(sig_dict, market)
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

            # ── 2026-05-20 PHASE 13 SHIP-AUDIT FIX ─────────────────────────
            # Re-apply Phase 13 exit-policy target now that stop_price and
            # entry price (`price`) are finalized. The earlier call in
            # _apply_phase13_overrides() silently no-oped for any strategy
            # that didn't pre-compute prices on the Signal — that was every
            # strategy except ORB. Today (2026-05-20) spring_setup shipped
            # 1.5R targets instead of 3R Phase 13 because of this. The
            # deferred-tag was set in step 2 of _apply_phase13_overrides;
            # consume it here and override.
            if getattr(signal, "_phase13_target_deferred", False) and not _managed_exit_target:
                try:
                    # Finding 2 fix: pass sub_strategy so opening_session.open_drive
                    # → fixed_rr(rr=3.0) actually fires (was dead code before).
                    _sub_for_recompute = (
                        (getattr(signal, "metadata", {}) or {}).get("sub_strategy")
                    )
                    _new_target = recompute_phase13_target(
                        signal.strategy, signal.direction, price, stop_price,
                        sub_strategy=_sub_for_recompute,
                    )
                    if _new_target is not None and _new_target != target_price:
                        _old = target_price
                        target_price = _new_target
                        _key_str = (f"{signal.strategy}.{_sub_for_recompute}"
                                    if _sub_for_recompute else signal.strategy)
                        logger.info(
                            f"[Phase13 override] {_key_str}: target_price "
                            f"{_old:.2f} -> {target_price:.2f} (deferred recompute, "
                            f"entry={price:.2f} stop={stop_price:.2f})"
                        )
                except Exception as _e:
                    logger.warning(
                        f"[Phase13 override] deferred recompute failed for "
                        f"{signal.strategy}: {_e!r}"
                    )

            # Phase 6b: Microstructure filter check (advisory only -- does NOT block)
            try:
                micro_result = self.bot.microstructure_filter.check(market, signal.direction)
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

            # ── 2026-05-15: Hard dollar-budget gate on ACTUAL placed stop ──
            # The operator's standing constraint: NEVER lose more than ~$50
            # per trade. Phoenix's prior model — risk-reference stop for
            # sizing + structural stop for OCO bracket (B21 managed-exit
            # pattern) — could report $18 of risk while exposing $137 in
            # practice (today's noise_area trades). The fix: compute the
            # ACTUAL dollar loss if the placed stop hits, and skip if it
            # exceeds MAX_ACTUAL_STOP_DOLLARS_PER_TRADE. The strategy's
            # entry may be valid; just SKIP this particular signal if its
            # natural-stop placement exceeds the operator's risk budget.
            # This is per the Bandy / Tomasini-Jaekle principle: position
            # sizing controls dollar risk; stops are placed where the signal
            # invalidates. On MNQ we can't size below 1 contract, so the
            # only honest action when natural stop > budget is to skip.
            import config.settings as _settings_mod
            _ts_budget = getattr(_settings_mod, "TICK_SIZE", 0.25)
            _tv_budget = getattr(_settings_mod, "TICK_VALUE_PER_CONTRACT", 0.50)
            _MAX_ACTUAL_STOP_DOLLARS = float(getattr(
                _settings_mod, "MAX_ACTUAL_STOP_DOLLARS_PER_TRADE", 50.0,
            ))
            _actual_stop_ticks = abs(price - stop_price) / _ts_budget
            _actual_stop_dollars = _actual_stop_ticks * _tv_budget * max(1, contracts)
            if _actual_stop_dollars > _MAX_ACTUAL_STOP_DOLLARS:
                logger.warning(
                    f"[BUDGET_SKIP:{tid}] {signal.strategy} {signal.direction}: "
                    f"actual stop ${_actual_stop_dollars:.2f} > budget "
                    f"${_MAX_ACTUAL_STOP_DOLLARS:.2f} "
                    f"(stop={_actual_stop_ticks:.0f}t × {contracts}ct × "
                    f"${_tv_budget:.2f}/t). Signal valid but stop too wide "
                    f"for risk budget — skipping."
                )
                self.bot.last_rejection = (
                    f"BUDGET_SKIP: stop ${_actual_stop_dollars:.0f} > "
                    f"${_MAX_ACTUAL_STOP_DOLLARS:.0f}"
                )
                try:
                    sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                                "confidence": signal.confidence, "entry_score": signal.entry_score,
                                "reason": signal.reason}
                    self.bot.history.log_near_miss(
                        sig_dict, market,
                        f"budget_skip:stop_${_actual_stop_dollars:.0f}_gt_${_MAX_ACTUAL_STOP_DOLLARS:.0f}",
                    )
                except Exception:
                    pass
                return

            # B62 Universal sanity gate — fail-closed geometry & distance check.
            # Runs AFTER stop/target resolution, BEFORE any OCO submission.
            # 2026-05-15: pass is_managed_exit so managed-exit strategies (e.g.
            # noise_area on wide-cone days) get the wider 5-1000t bound. Today
            # 11 noise_area signals were silently dropped at 776t against the
            # default 200t cap.
            _is_managed = bool(_managed_exit_target) or bool(
                getattr(signal, "exit_trigger", None)
            ) or any(
                getattr(s, "name", None) == getattr(signal, "strategy", None)
                and getattr(s, "uses_managed_exit", False)
                for s in self.bot.strategies
            )
            _ok, _reason = _sanity_check_entry(
                signal, price, stop_price, target_price,
                is_managed_exit=_is_managed,
            )
            if not _ok:
                logger.critical(f"[STOP_SANITY_FAIL:{tid}] {signal.strategy} "
                                f"{signal.direction}: {_reason}")
                self.bot.last_rejection = f"STOP_SANITY_FAIL: {_reason}"
                try:
                    sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                                "confidence": signal.confidence, "entry_score": signal.entry_score,
                                "reason": signal.reason}
                    self.bot.history.log_near_miss(sig_dict, market, f"stop_sanity_fail:{_reason}")
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

                # 2026-05-20 SHIP-AUDIT pt2 (F-005): for LIMIT entries with a
                # Phase 13 exit policy, re-anchor the Phase 13 target to the
                # LIMIT FILL price instead of the market tick price the earlier
                # deferred-recompute used. Without this, when scale_out_1r or
                # fixed_rr eventually ships for g_inside_bar / e_multi_day,
                # the target lands LIMIT_OFFSET_TICKS off from the fill →
                # systematic R-distance error. Chandelier strategies don't
                # care today (10R wide-bracket), but this paves the way.
                if getattr(signal, "_phase13_target_deferred", False) and not _managed_exit_target:
                    try:
                        # Finding 2 fix: sub-strategy aware lookup.
                        _sub_for_recompute = (
                            (getattr(signal, "metadata", {}) or {}).get("sub_strategy")
                        )
                        _ph13_target = recompute_phase13_target(
                            signal.strategy, signal.direction, limit_price, stop_price,
                            sub_strategy=_sub_for_recompute,
                        )
                        if _ph13_target is not None and _ph13_target != target_price:
                            _old = target_price
                            target_price = round(_ph13_target, 2)
                            _key_str = (f"{signal.strategy}.{_sub_for_recompute}"
                                        if _sub_for_recompute else signal.strategy)
                            logger.info(
                                f"[Phase13 override] {_key_str}: LIMIT-anchored "
                                f"target {_old:.2f} -> {target_price:.2f} "
                                f"(limit_fill={limit_price:.2f} stop={stop_price:.2f})"
                            )
                    except Exception as _e:
                        logger.warning(
                            f"[Phase13 override] LIMIT-anchored recompute failed for "
                            f"{signal.strategy}: {_e!r}"
                        )
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
            _force = getattr(self.bot, "FORCE_ACCOUNT", None)
            if _force:
                _account = _force
                logger.debug(f"[ROUTING] {self.bot.bot_name}: FORCE_ACCOUNT -> {_account} "
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
                if self.bot.positions.has_pending_entry(_account):
                    _pending = self.bot.positions.get_pending_entry(_account)
                    _age = int(time.time() - _pending["submitted_at"]) if _pending else -1
                    logger.info(
                        f"[ENTRY_GATE:{self.bot.bot_name}] {signal.strategy} "
                        f"{signal.direction} → {_account} SKIPPED: pending "
                        f"LIMIT entry in flight (trade={_pending['trade_id']}, "
                        f"age={_age}s). Waiting for fill or expiry."
                    )
                    self.bot.last_rejection = (
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
                    f"[LIVE_GUARD] {self.bot.bot_name} BLOCKED {signal.strategy} "
                    f"{signal.direction} signal — resolved to live account "
                    f"'{_account}'. Entry aborted. Check FORCE_ACCOUNT and "
                    f"config/account_routing.py."
                )
                self.bot.last_rejection = f"LIVE_GUARD: refused to route to {_account}"
                try:
                    from core.telegram_notifier import send_sync
                    send_sync(
                        f"🛑 [LIVE_GUARD] {self.bot.bot_name} BLOCKED {signal.strategy} "
                        f"routed to LIVE account '{_account}'. Entry aborted.",
                        dedup_key=f"live_guard:{self.bot.bot_name}:{signal.strategy}",
                    )
                except Exception:
                    pass
                return

            # B50: pre-entry position-reconcile guard (inverse phantom).
            # If NT8 already reports a position on this account (e.g. Python
            # state was lost on restart but NT8 still holds the real fill),
            # abort the new entry — otherwise NT8 rejects with "Exceeds
            # account's maximum position quantity" and leaves orphan OCO legs.
            #
            # REDTEAM-1-FIX-3B (remediation 2026-06-04 round 2): the original
            # wholesale-skip on Sim101 was historically necessary because
            # Sim101 (multi-strategy account / live-mode catch-all) legit-
            # imately hosts concurrent positions from different strategies,
            # and the inverse-phantom check would false-positive on those.
            # But once startup_reconciliation began adopting orphan NT8
            # fills as `reconciled=True` positions, the wholesale-skip
            # decoupled Sim101 from any defense-in-depth against orphan-
            # vs-real-signal double-fill. Topology-aware labeling (Phase 3a)
            # restores the strategy-slot interlock for SINGLE-strategy
            # accounts, but Sim101 itself is multi-strategy by design, so
            # the slot interlock can't help there. Narrow the skip:
            #
            #   - Sim101 + a non-reconciled (real bot) position open →
            #     skip the verify (preserve legit multi-strategy concurrency).
            #   - Sim101 + zero or only-reconciled positions open →
            #     RUN the verify (catch the orphan-vs-real double-fill).
            #   - Non-Sim101 → run the verify as before.
            if should_run_inverse_phantom_guard(_account, self.bot.positions):
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
                        self.bot.last_rejection = (
                            f"Pre-entry skip: {_account} already "
                            f"{pre.get('observed_direction')} {pre.get('observed_qty')}"
                        )
                        return
                except Exception as e:
                    logger.debug(f"[PREENTRY:{tid}] reconcile check failed "
                                 f"(non-blocking): {e}")

            # Chart overlay hook 1/4: emit signal event for PhoenixTradeOverlay.
            # Writes JSONL line so the NT8 indicator can draw the entry triangle
            # (strategy-colored) + stop/target dashed lines. Non-blocking; helper
            # swallows all errors so visualization never breaks the trade path.
            _signal_viz.emit_signal(
                strategy=signal.strategy,
                direction=signal.direction,
                entry=float(price),
                stop=float(stop_price),
                target=float(target_price) if target_price is not None else 0.0,
                trade_id=tid,
            )

            # 2026-06-02: parallel emit to NEW phoenix_markers.jsonl consumed
            # by the PhoenixTradeMarkers indicator. ChartMarkerWriter is
            # no-throw — never breaks the trading path.
            try:
                from core.nt8_chart_markers import get_chart_markers
                get_chart_markers().record_entry(
                    trade_id=tid,
                    strategy=signal.strategy,
                    direction=signal.direction,
                    entry_price=float(price),
                    stop=float(stop_price),
                    target=float(target_price) if target_price is not None else None,
                )
            except Exception:
                pass  # belt-and-suspenders; writer is already no-throw

            # P1-3 (F-07/F-20) portfolio risk gate — directional cap + correlation.
            # WARN-default; PHOENIX_PORTFOLIO_CAP_BLOCK=1 enforces.
            # Lazy-instantiate per-bot so the wiring works even if BaseBot.__init__
            # didn't create it (defensive — gate must NEVER crash the entry path).
            try:
                _gate = getattr(self.bot, "_portfolio_risk_gate", None)
                if _gate is None:
                    from core.portfolio_risk_gate import PortfolioRiskGate
                    _gate = PortfolioRiskGate(self.bot)
                    self.bot._portfolio_risk_gate = _gate
            except Exception as _e:
                _gate = None
                logger.warning(
                    f"[PORTFOLIO_CAP] gate unavailable ({_e!r}) — bypassing"
                )
            if _gate is not None:
                try:
                    gate_decision = _gate.check_entry(
                        strategy_name=signal.strategy,
                        direction=signal.direction,
                        contracts=contracts,
                        signal_price=price,
                    )
                except Exception as _e:
                    logger.warning(
                        f"[PORTFOLIO_CAP] check_entry raised ({_e!r}) — bypassing"
                    )
                    gate_decision = {"decision": "ACCEPT", "contracts": contracts,
                                     "reason": "gate-exception-passthrough"}
                if gate_decision["decision"] == "REFUSE":
                    logger.warning(f"[PORTFOLIO_CAP] REFUSE: {gate_decision['reason']}")
                    self.bot.last_rejection = f"Portfolio cap: {gate_decision['reason']}"
                    return
                elif gate_decision["decision"] == "REDUCE":
                    logger.warning(f"[PORTFOLIO_CAP] REDUCE: {gate_decision['reason']}")
                    contracts = gate_decision["contracts"]

            # B55: split bracket submit — ENTRY first, stop+target AFTER fill.
            # Prior behavior submitted entry + OCO stop/target in one burst; NT8
            # rejected protection legs with "Exceeds account's maximum position
            # quantity" because it counts pending sides before the entry fills.
            # Now we submit entry alone, wait for fill confirmation, then attach
            # OCO protection to the filled position.
            # P4-3 (F-23) 2026-05-24: signal_to_oif latency instrumentation.
            try:
                from core.latency_tracker import get_latency_tracker as _get_lt
                _t_signal = time.time()
            except Exception:
                _get_lt = None
                _t_signal = None
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
                if _get_lt is not None and _t_signal is not None:
                    try:
                        _get_lt().record("signal_to_oif", _t_signal, time.time())
                    except Exception:
                        pass
                # P1-7 (2026-05-25): register pending-entry lifecycle for
                # LIMIT submits ONLY. MARKET entries fill immediately or
                # fail at submission and never need a pending tracker.
                if signal_entry_type == "LIMIT":
                    try:
                        from config.settings import PENDING_ENTRY_TIMEOUT_S as _PE_TO
                        _get_pe_tracker().register(
                            trade_id=tid,
                            strategy=signal.strategy,
                            account=_account,
                            instrument=_INSTRUMENT_DEFAULT,
                            side=("BUY" if signal.direction == "LONG" else "SELL"),
                            qty=int(contracts),
                            limit_price=float(limit_price),
                            timeout_s=float(_PE_TO),
                        )
                    except Exception as _pe_e:
                        logger.debug(
                            f"[PENDING:{tid}] tracker register failed (non-fatal): {_pe_e!r}"
                        )
                # P1-3 (F-07/F-20) Feed the portfolio gate so subsequent entries
                # in the rolling window see this new directional exposure.
                # trade_id stamp lets the exit path drop the entry on close.
                try:
                    if _gate is not None:
                        _gate.record_entry(
                            strategy_name=signal.strategy,
                            direction=signal.direction,
                            contracts=contracts,
                            signal_price=price,
                            trade_id=tid,
                        )
                except Exception:
                    pass  # gate must never break the entry path
            except Exception as e:
                logger.error(f"[{tid}] Failed to send trade command: {e}")
                self.bot.last_rejection = f"Bridge send failed: {e}"
                return

            # Wait for fill confirmation (5s timeout, defaults to FILLED for sim)
            from bridge.oif_writer import wait_for_fill
            fill_result = await wait_for_fill(tid, timeout_s=5.0)

            if fill_result["status"] == "REJECTED":
                logger.error(f"[{tid}] ORDER REJECTED by NT8: {fill_result['content']}")
                self.bot.last_rejection = f"Order rejected: {fill_result['content']}"
                # P1-7: NT8-rejected LIMIT submissions terminate immediately
                # as "cancelled" — they never became working orders.
                if signal_entry_type == "LIMIT":
                    try:
                        _get_pe_tracker().mark_cancelled(tid, reason="nt8_rejected")
                    except Exception:
                        pass
                return

            if fill_result["status"] == "TIMEOUT":
                if LIVE_TRADING:
                    # LIVE mode: DO NOT proceed without fill confirmation
                    logger.error(f"[{tid}] Fill timeout in LIVE mode — ABORTING entry. "
                                  f"Check NT8 manually for order status.")
                    self.bot.last_rejection = f"Fill timeout in LIVE mode — entry aborted"
                    return
                # B39 hardening (B48 refinement): on any non-LIVE account,
                # distinguish
                #   (a) NT8 REJECTED order (OIF still in incoming/) → phantom risk,
                #       loud alert + cleanup stop/target orphans
                #   (b) NT8 ACCEPTED but waiting for limit/stop trigger (OIF
                #       consumed, no fill yet) → NOT a phantom, skip quietly
                # 2026-06-02 fix: the original guard exempted _account == "Sim101"
                # on the assumption Sim101 was a fully-mocked path with no real
                # OIFs. That assumption broke when prod_bot started writing real
                # OIFs against Sim101 — 14 NT8-rejected entry OIFs sat in
                # incoming/ during a 09:02-09:39 ATI outage and re-fired as
                # naked BUY LIMITs at ~11:00 when ATI recovered. PHANTOM_GUARD
                # now runs for ALL non-LIVE accounts. See
                # logs/oracle/research/2026-06-02_chart_orders_root_cause.md.
                if _account:
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
                        # P1-7: phantom-guard rejection = terminal cancel.
                        if signal_entry_type == "LIMIT":
                            try:
                                _get_pe_tracker().mark_cancelled(
                                    tid, reason="phantom_guard_rejected",
                                )
                            except Exception:
                                pass
                        self.bot.last_rejection = (
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
                        self.bot.positions.record_pending_entry(
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
                # Defensive fall-through: PHANTOM_GUARD above runs for any
                # truthy _account, so this only fires if account resolution
                # returned an empty string/None (should not happen in normal
                # operation — both prod_bot and sim_bot always have an
                # account). Kept as a safety net for the impossible case.
                logger.info(f"[{tid}] No fill file (paper mode) — assuming filled")

            # Inject regime and Phase 6b data into market snapshot for analytics
            market["regime"] = self.bot.session.get_current_regime()
            market["signal_price"] = price  # Price at signal generation time
            market["microstructure"] = micro_result
            market["fill_latency_ms"] = fill_result.get("latency_ms", 0)

            # B47: For sim_bot, verify the fill actually happened by reading
            # NT8's outgoing/ position file for this account. If NT8 reports
            # FLAT or wrong direction/qty, we have a phantom — reject the entry.
            #
            # REDTEAM-1-FIX-3B: same wholesale-skip-on-Sim101 issue as the
            # B50 pre-entry guard above — Sim101 hosts legitimate concurrent
            # multi-strategy positions, but skipping the check wholesale also
            # masks orphan-stacking. Same helper, same narrowing logic.
            if should_run_inverse_phantom_guard(_account, self.bot.positions):
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
                        self.bot.last_rejection = (
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
                        # REDTEAM-R2-1-FIX (remediation 2026-06-04 round 2,
                        # superseded by REDTEAM-R2-1-FIX-V2 below):
                        # if NT8 reports wrong_qty or wrong_direction, the new
                        # fill DID land — it just stacked on top of an existing
                        # position (typically a reconciled orphan on Sim101 the
                        # B50 pre-fill verify missed due to its 0.5s file-race
                        # returning "missing" which the gate treats as flat).
                        # The bare `return` previously left the new `contracts`
                        # qty NAKED on NT8 (no OCO, no Phoenix tracking) — one
                        # timing race away from a live double-fill loss the
                        # moment LIVE_TRADING=True flips.
                        #
                        # REDTEAM-R2-1-FIX-V2 (focused red-team round 2):
                        # the V1 fix called the legacy full-flatten primitive
                        # `_sink_submit_exit` with qty=contracts but that
                        # primitive ultimately emits the OIF line
                        #     CLOSEPOSITION;{account};{INSTRUMENT};GTC;;;;;;;;;
                        # with the qty FIELD EMPTY (see bridge/oif_writer.py:354
                        # close_position_line + bridge/oif_writer.py:1296). NT8
                        # CLOSEPOSITION flattens the entire account net position
                        # regardless of the qty arg the Python wrapper accepts —
                        # the qty was COSMETIC. Result: the orphan + our fill
                        # both flatten, plus the orphan's safety-net OCO becomes
                        # zombie working orders against a flat position.
                        #
                        # The fix-V2 uses _sink_submit_partial_exit (a sized
                        # PARTIAL_EXIT MARKET order NT8 honors at the requested
                        # qty per bots/_oif_emitter.py:135-155) so only OUR
                        # contracts qty closes and the orphan + its safety-net
                        # OCO survive. The retry loop mirrors the OCO-fail
                        # pattern at lines ~1050 below — 3 attempts with 1s
                        # backoff. record_protect_failed gets an explicit
                        # reason= so the operator's pause-message text says
                        # "STACKED FILL" not "PROTECT FAILED" (different root
                        # causes, different remediations).
                        #
                        # 'flat' / 'missing' don't reach this code path — by
                        # definition no fill landed, so there's nothing on NT8
                        # to clean up. Only wrong_qty / wrong_direction (the
                        # actual stacking scenarios) trigger the flatten.
                        if pos_check["status"] in ("wrong_qty", "wrong_direction"):
                            logger.critical(
                                f"[NT8_VERIFY:{tid}] STACKED FILL DETECTED on "
                                f"{_account} — flattening OUR {contracts}x "
                                f"{signal.direction} portion via sized "
                                f"PARTIAL_EXIT (preserving any existing orphan "
                                f"+ its safety-net OCO)."
                            )
                            try:
                                from core.nt8_sink_health import get_sink_health
                                get_sink_health(self.bot.bot_name).record_protect_failed(
                                    trade_id=tid,
                                    strategy=signal.strategy,
                                    direction=signal.direction,
                                    account=_account,
                                    reason=(
                                        f"STACKED FILL on {tid} "
                                        f"({signal.strategy} {signal.direction} "
                                        f"on {_account})"
                                    ),
                                )
                            except Exception as _sink_err:
                                logger.warning(
                                    f"[NT8_VERIFY:{tid}] sink-health trip failed: {_sink_err!r}"
                                )
                            # Retry up to 3x with 1s backoff (mirrors the
                            # OCO-protect-fail-flatten retry pattern at lines
                            # ~1050 below). If all three attempts fail
                            # (REFUSE / ERROR / exception), the operator gets
                            # the critical log + Telegram + sink-health pause
                            # and must intervene manually.
                            partial_exit_ok = False
                            for _attempt in range(1, 4):
                                try:
                                    _ef_resp = _sink_submit_partial_exit(
                                        direction=signal.direction,
                                        n_contracts=contracts,
                                        trade_id=f"{tid}_redteam_r2_1_flatten{_attempt}",
                                        account=_account,
                                    )
                                    if _ef_resp.get("decision") == "ACCEPT":
                                        logger.info(
                                            f"[NT8_VERIFY:{tid}] STACKED FILL "
                                            f"PARTIAL_EXIT accepted on attempt "
                                            f"#{_attempt} (flattened OUR "
                                            f"{contracts}x {signal.direction})."
                                        )
                                        partial_exit_ok = True
                                        break
                                    logger.critical(
                                        f"[NT8_VERIFY:{tid}] STACKED FILL "
                                        f"PARTIAL_EXIT refused on attempt "
                                        f"#{_attempt} by sink "
                                        f"{_ef_resp.get('sink','?')}: "
                                        f"{_ef_resp.get('reason','?')}"
                                    )
                                except Exception as _ef_err:
                                    logger.critical(
                                        f"[NT8_VERIFY:{tid}] STACKED FILL "
                                        f"PARTIAL_EXIT attempt #{_attempt} "
                                        f"error: {_ef_err}"
                                    )
                                if _attempt < 3:
                                    await asyncio.sleep(1.0)
                            if not partial_exit_ok:
                                logger.critical(
                                    f"[NT8_VERIFY:{tid}] STACKED FILL "
                                    f"PARTIAL_EXIT FAILED ALL 3 ATTEMPTS — "
                                    f"OUR {contracts}x {signal.direction} on "
                                    f"{_account} may remain NAKED on NT8. "
                                    f"Operator intervention required."
                                )
                            try:
                                from core.telegram_notifier import send_sync
                                send_sync(
                                    f"🚨 [STACKED_FILL] {signal.strategy} → {_account}: "
                                    f"NT8 reports {pos_check['status']} after entry. "
                                    f"PARTIAL_EXIT "
                                    f"{'succeeded' if partial_exit_ok else 'FAILED ALL 3 RETRIES'} "
                                    f"for OUR {contracts}x portion. "
                                    f"Check NT8 + reconciliation.",
                                    dedup_key=f"stacked_fill:{signal.strategy}:{_account}",
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
                    # 2026-06-02: trip the NT8-sink-health auto-pause so
                    # subsequent entries are gated upstream until the
                    # operator clears via dashboard or /nt8_clear.
                    try:
                        from core.nt8_sink_health import get_sink_health
                        get_sink_health(self.bot.bot_name).record_protect_failed(
                            trade_id=tid,
                            strategy=signal.strategy,
                            direction=signal.direction,
                            account=_account,
                        )
                    except Exception as _sink_err:
                        logger.warning(
                            f"[PROTECT:{tid}] sink-health trip failed: {_sink_err!r}"
                        )
                    # P1-7: emergency flatten also cancels any pending LIMIT
                    # entries scoped to this account.
                    try:
                        if hasattr(self.bot, "_flatten_pending_entries"):
                            self.bot._flatten_pending_entries(
                                reason="unprotected_flatten", account=_account,
                            )
                    except Exception:
                        pass
                    # 2026-06-05 FINDING-2026-06-05-SLOT-INTERLOCK-BYPASS T1
                    # (Cluster 2 audit): swap to sized PARTIAL_EXIT. The prior
                    # _sink_submit_exit emitted CLOSEPOSITION (account-wide,
                    # qty-ignored), which on multi-strategy Sim101 would have
                    # flattened OTHER strategies' positions too. Same root
                    # cause class as the Phase 3g (REDTEAM-R2-1-FIX-V2,
                    # commit 354bb3c) swap at the B47 STACKED FILL block
                    # above. _sink_submit_partial_exit packages op="PARTIAL_EXIT"
                    # with qty=n_contracts and NT8 honors the qty per
                    # bots/_oif_emitter.py:135-155.
                    logger.info(
                        f"[SLOT-INTERLOCK] [PROTECT:{tid}] EMERGENCY FLATTEN "
                        f"via sized PARTIAL_EXIT — strategy={signal.strategy} "
                        f"account={_account} direction={signal.direction} "
                        f"n_contracts={contracts}"
                    )
                    try:
                        # Sink-mediated emergency flatten via sized PARTIAL_EXIT.
                        # With the gate engaged, a PARTIAL_EXIT op should always
                        # be ACCEPT'd (the gate doesn't block close-position
                        # orders); fail-soft fallback covers the case where
                        # the gate is unreachable.
                        _ef_resp = _sink_submit_partial_exit(
                            direction=signal.direction,
                            n_contracts=contracts,
                            trade_id=f"{tid}_emergency_flatten",
                            account=_account,
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
                    self.bot.last_rejection = f"Unprotected position flattened: {_account}"
                    return

            # NOW open position locally (after fill confirmation)
            # LIMIT / STOPMARKET entries fill at limit_price; MARKET fills near tick price.
            effective_entry_price = (
                limit_price if (signal_entry_type in ("LIMIT", "STOPMARKET") and limit_price > 0)
                else price
            )
            # P4-2 (2026-05-24): persist the per-signal trace_id into the
            # market_snapshot so it survives onto the Position object and into
            # the trade_memory record. Lets `_trade_exit.exit_trade` and
            # `_trade_closer.on_trade_closed` rebind the trace context downstream.
            try:
                market["trace_id"] = getattr(signal, "trace_id", None)
            except Exception:
                pass
            self.bot.positions.open_position(
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
                # Sprint F: persist tier classifier through to the closed-trade
                # record so indicator_audit can rank A++/A/B/C predictive value.
                # signal.tier preferred; fall back to metadata['tier'] for older
                # strategies that stuff it into the metadata dict.
                tier=(getattr(signal, "tier", None)
                      or (getattr(signal, "metadata", {}) or {}).get("tier")),
            )

            # Fix A: entry actually filled (direct fill path) — clear any
            # pending-entry record for this account.
            try:
                self.bot.positions.clear_pending_entry(_account)
            except Exception:
                pass
            # P1-7: terminate the pending-entry lifecycle row with "filled".
            # No-op for MARKET entries (never registered).
            if signal_entry_type == "LIMIT":
                try:
                    _get_pe_tracker().mark_filled(tid, reason="open_position")
                except Exception:
                    pass

            # 2026-05-27 (P4-6 prerequisite): stash AI agent verdicts on
            # the freshly-opened Position so close_position() can copy
            # them onto the recorded trade row. Without this, the
            # pretrade_filter + council verdicts evaporate (they live on
            # self.bot._filter_verdict / self.bot._council_result, both
            # of which get overwritten by the next signal). The harness
            # at tools/ai_uplift_harness.py reads these to compute
            # Cohort A (GO) / Cohort B (NO_GO) lift CIs. Failure here is
            # NEVER allowed to block trade entry — the verdict is
            # observability data, not execution data.
            try:
                _pos_obj = self.bot.positions.get_position(tid)
                if _pos_obj is not None:
                    from datetime import datetime as _dt
                    _filter_v = getattr(self.bot, "_filter_verdict", None) or {}
                    _council_v = getattr(self.bot, "_council_result", None) or {}
                    # Normalize: extract the action string from the dict
                    # form (or pass through if already a string).
                    def _norm_verdict(v):
                        if v is None:
                            return None
                        if isinstance(v, str):
                            return v
                        if isinstance(v, dict):
                            return (v.get("action")
                                    or v.get("verdict")
                                    or v.get("bias")
                                    or None)
                        return None
                    _pos_obj.agent_verdicts = {
                        "pretrade": _norm_verdict(_filter_v),
                        "council":  _norm_verdict(_council_v),
                        "debrief":  None,  # populated post-exit by future
                                           # per-trade reflection (TBD).
                    }
                    _pos_obj.agent_decision_ts_ct = _dt.now().astimezone().isoformat(timespec="seconds")
            except Exception as _verdict_err:
                logger.debug(f"[AGENT_VERDICT_STASH] non-blocking err: {_verdict_err}")

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
                _new_pos = self.bot.positions.get_position(tid)
                if _new_pos is not None and _ids:
                    _new_pos.stop_order_id = _ids.get("stop", "") or ""
                    _new_pos.target_order_id = _ids.get("target", "") or ""
                    logger.info(
                        f"[OID_CAPTURE:{tid}] stop_oid={(_new_pos.stop_order_id or 'MISS')[:12]} "
                        f"target_oid={(_new_pos.target_order_id or 'MISS')[:12]}"
                    )
                else:
                    # 2026-05-08: demoted from WARNING to INFO. Audit of last
                    # 24h showed 220 NT8 outgoing/ files — ALL terminal-state
                    # (FILLED/CANCELLED/REJECTED), zero WORKING. NT8's ATI
                    # only writes status files on order completion, not on
                    # acceptance. So scan_outgoing_for_order_id (which matches
                    # by price tolerance against parts[2]) cannot capture
                    # protective-stop / target order_ids while they sit
                    # WORKING — those are far from current price and never
                    # produce a matching outgoing file until they fill or
                    # cancel. Capture rate ~3% / target capture rate ~0%.
                    # The "Python-only" fallback is therefore the production
                    # behavior, not an exception. A real fix requires a
                    # NinjaScript that subscribes to OnOrderUpdate and pushes
                    # order events back to Phoenix via the WebSocket — out of
                    # scope for OID_CAPTURE. This log line stays as INFO so
                    # the cadence remains visible without flooding the log.
                    logger.info(f"[OID_CAPTURE:{tid}] no order_ids captured — "
                                f"stop-moves use Python-only fallback (expected; see comment)")
            except Exception as _e:
                logger.warning(f"[OID_CAPTURE:{tid}] failed: {_e}")

            # ── B70: directional conflict observability (non-blocking) ──────
            # Detect cross-strategy LONG-vs-SHORT, log to jsonl, dedup-alert.
            # Conflicts are ALLOWED — data-gathering only.
            try:
                from core.strategy_risk_registry import StrategyRiskRegistry
                from core import conflict_logger as _cflog
                _reg = getattr(self.bot, "_conflict_reg", None)
                if _reg is None:
                    _reg = StrategyRiskRegistry()
                    self.bot._conflict_reg = _reg
                all_conflicts = _reg.detect_directional_conflicts(self.bot.positions)
                # Filter to only pairs that include the just-opened trade.
                involved = [c for c in all_conflicts
                            if tid in (c["trade_id_a"], c["trade_id_b"])]
                if involved:
                    exposure = _reg.exposure_snapshot(self.bot.positions)
                    new_pos = self.bot.positions.get_position(tid)
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
            self.bot._stall_detector.reset()
            self.bot._rider_active = False

            # ── Rider mode: active on ALL days for rider-eligible strategies ──────
            # bias_momentum and dom_pullback always use rider mode — stall detector
            # + reversal exit (DOM + wick confirmed) are the exit mechanism.
            # Smart exit is disabled for these strategies on every day type.
            # Goal: hold for 20-50 pts on range days, 100+ on trend days.
            _RIDER_STRATEGIES = {"bias_momentum"}  # dom_pullback removed 2026-05-21
            if TREND_RIDER_ENABLED and signal.strategy in _RIDER_STRATEGIES:
                _pos = self.bot.positions.position
                if _pos:
                    _pos.rider_mode = True
                    _be_level = (_pos.entry_price + abs(_pos.entry_price - _pos.stop_price)
                                 if _pos.direction == "LONG"
                                 else _pos.entry_price - abs(_pos.entry_price - _pos.stop_price))
                    logger.info(f"[{tid}] RIDER ON ({self.bot._day_type} day) — "
                                f"smart exit OFF, reversal+stall exit driving. "
                                f"Entry={_pos.entry_price:.2f} stop={_pos.stop_price:.2f} "
                                f"BE@{_be_level:.2f} (+{abs(_pos.entry_price-_pos.stop_price)/TICK_SIZE:.0f}t = "
                                f"{abs(_pos.entry_price-_pos.stop_price):.2f}pts)")
            else:
                # Non-rider strategies (spring_setup, ib_breakout, etc.) — fixed target mode
                _mom_score = getattr(self.bot._last_cr, "momentum_score", 0) if self.bot._last_cr else 0
                _cr_verdict = getattr(self.bot._last_cr, "verdict", "UNKNOWN") if self.bot._last_cr else "UNKNOWN"
                if SCALE_OUT_ENABLED and contracts >= 2:
                    logger.info(f"[{tid}] Scale-out eligible: contracts={contracts} "
                                f"cr_verdict={_cr_verdict} mom_score={_mom_score} "
                                f"(rider triggers at RR={SCALE_OUT_RR})")
                else:
                    logger.info(f"[{tid}] Fixed target mode ({signal.strategy}): "
                                f"{contracts}ct, target_rr={signal.target_rr:.1f}")

            self.bot.status = "IN_TRADE"

            # Phase 6: Start expectancy tracking
            self.bot.expectancy.start_tracking(
                trade_id=tid,
                direction=signal.direction,
                entry_price=price,
                signal_price=market.get("price", price),  # Price at signal time
                stop_price=stop_price,
                target_price=target_price,
                strategy=signal.strategy,
                regime=self.bot.session.get_current_regime(),
            )

            # Phase 6: Consume transition bonus if active
            self.bot.regime_transitions.mark_signal_used()

            logger.info(f"[FILLED:{tid}] {signal.direction} {contracts}x @ {price:.2f} "
                         f"fill_latency={fill_result.get('latency_ms', 0):.0f}ms")

            # Chart overlay hook 2/4: fill confirmed at NT8.
            _signal_viz.emit_fill(trade_id=tid, fill_price=float(price))

            # NOW mark signal as actually taken (after confirmed fill)
            self.bot.tracker.record_signal(
                strategy=signal.strategy, direction=signal.direction,
                confidence=signal.confidence, taken=True,
                regime=self.bot.session.get_current_regime(), trade_id=tid,
            )

            self.bot.history.log_entry(signal, price, contracts, stop_price,
                                   target_price, risk_dollars, tier, market)

            # Telegram notification
            asyncio.ensure_future(tg.notify_entry(
                trade_id=tid, direction=signal.direction, strategy=signal.strategy,
                price=price, stop=stop_price, target=target_price,
                contracts=contracts, risk_dollars=risk_dollars, tier=tier,
                regime=self.bot.session.get_current_regime(),
            ))
