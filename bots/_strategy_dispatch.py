"""Strategy dispatch / signal selection — extracted from base_bot.py
2026-05-24 (P4-1 Stage 3).

Runs every enabled strategy on a bar close, picks the best signal,
and stashes it on self.bot._pending_signal for the main loop to pick up.

CRITICAL: this module does NOT write OIF. The OIF write happens in
BaseBot._enter_trade which is downstream of this module's
_pending_signal stash. Behavior preservation guarantee: every gate,
every log line, every try/except is verbatim from the original
BaseBot._evaluate_strategies. Coupling kept tight (self.bot.X)
intentionally so the diff is minimal and review-able.

Original location: bots/base_bot.py: BaseBot._evaluate_strategies
(lines 2880-3702 as of session resume, ~822 lines of body).

Lazy imports preserved verbatim (inside the method body):
- agents.market_advisor.enrich_market_snapshot
- core.continuation_reversal.assess
- core.momentum_score.get_trajectory
- config.settings.PLAYBOOK_ENABLED

State READS from self.bot:
    positions, circuit_breakers, session, strategies, aggregator,
    rsi_divergence, htf_scanner, cvd_health, big_move, smc, hmm_regime,
    intermarket, pandas_ta, chart_patterns, cot_feed, calendar_risk,
    playbook_mgr, swing_state_5m, volume_profile, reversal_detector,
    sweep_watcher, gamma_flip_detector, pinning_detector, tape_reader,
    cockpit, crowding_detector, counter_edge, regime_transitions,
    no_trade_fp, risk, tracker, history, _day_classifier, _runtime_params,
    _last_rsi_divergence, _last_footprint_signals, _last_cr, _latest_intel,
    _council_result, bot_name, _day_type, _enrich_market_with_gamma (method)

State WRITES to self.bot:
    last_rejection, last_signal, _last_eval, _last_cr, _day_type,
    _last_chart_patterns_v1, _last_vix_term, _last_pinning_state,
    _last_opex_status, _last_es_confirmation, _last_structural_bias,
    _cockpit_result, _last_htf_confluence, _last_enriched_market,
    _pending_signal, _bias_log_counter (lazy attr)

Module-level imports inherited from base_bot.py:
    datetime (stdlib)
    HALT_MARKER_FILE (from core.circuit_breakers)
    extract_v1_patterns (from core.chart_patterns_v1)
    get_vix_term_cached (from core.vix_term_structure)
    get_opex_status (from core.opex_calendar)
    check_es_confirmation (from core.es_confirmation)
    compute_structural_bias (from core.structural_bias)
"""
from __future__ import annotations

import logging
from datetime import datetime

from core.circuit_breakers import HALT_MARKER_FILE
from core.chart_patterns_v1 import extract_v1_patterns
from core.vix_term_structure import get_cached as get_vix_term_cached
from core.opex_calendar import get_opex_status
from core.es_confirmation import check_confirmation as check_es_confirmation
from core.structural_bias import compute_structural_bias

# Logger name preserved as "Bot" — IDENTICAL to base_bot.py:148
# (logger = logging.getLogger("Bot")). Operator greps log files and
# tools/daily_session_summary.py filter on this exact prefix. If you
# change it, every "[EVAL] price=..." / "[REJECT:...]" / "[TRADE
# QUEUED:...]" log line emitted by this module will route under a
# different name and break the dashboard's log filter.
logger = logging.getLogger("Bot")


class StrategyDispatch:
    """Wraps BaseBot._evaluate_strategies. See module docstring for the
    full read/write surface and behavior-preservation invariants."""

    def __init__(self, bot):
        self.bot = bot

    def evaluate(self) -> None:
        """Run all enabled strategies, pick best signal."""
        if not self.bot.positions.is_flat:
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
        elif self.bot.circuit_breakers.should_halt():
            _halt_reason = f"HALTED — circuit breaker: {self.bot.circuit_breakers.halted_reason or 'active'}"
        if _halt_reason is not None:
            logger.warning(f"[HALT] {_halt_reason} — blocking strategy evaluation")
            self.bot.last_rejection = _halt_reason
            self.bot._last_eval = {
                "ts": datetime.now().isoformat(),
                "regime": self.bot.session.to_dict().get("regime", "?"),
                "risk_blocked": _halt_reason,
                "strategies": [],
                "best_signal": None,
            }
            return

        # 2026-05-13: prod trading-window gate REMOVED.
        #
        # Was: a 'if self.bot.bot_name == "prod": if not is_prod_trading_window(): return'
        # check restricting prod to 08:30-11:00 + 13:00-14:30 CST (primary +
        # secondary windows defined in core/session_manager.py:is_prod_trading_window).
        # The early-return was SILENT — no log line, no _last_eval update — so
        # outside-window skips were invisible to dashboard / logs.
        #
        # Removed because:
        #   1. SilentFailures anti-pattern (memory/feedback_silent_failures.md):
        #      operator saw "SCANNING" status all day with no trades and no
        #      indication why. 2026-05-13 incident: NT8 internet outage
        #      08:30-11:09 meant prod missed its entire primary window; by the
        #      time NT8 came back, the gate silently skipped every eval. Sim
        #      (which doesn't have this gate) booked 4 wins / $114.22. Prod
        #      booked nothing.
        #   2. Operator preference: prod is paper-only (Sim101), capped at
        #      $5/trade and $15/day. Restricting it to 3.5 hours/day for
        #      "highest edge" was an over-conservative legacy choice — running
        #      24/7 like sim gives more behavioral-validation coverage at zero
        #      real-money risk.
        #   3. Sprint H (2026-05-04) already opened up STRATEGIES for prod
        #      (only_validated=False). This is the natural next step.
        #
        # Strategy-level time windows (e.g. orb 08:30-14:30, opening_session
        # 08:30-08:45) still apply — they're intentional per-strategy filters,
        # not a bot-level gate. The per-trade and daily caps in SimpleSizer +
        # RiskManager are the actual risk limits.
        #
        # Preserved (these DO log, so they're not silent):
        #   - HALT marker file check     (line ~2403)
        #   - circuit_breakers.should_halt() (line ~2405)
        #   - positions.is_flat early return (line ~2393)
        #   - bars warmup guard          (line ~2458)
        #
        # If you ever want to re-introduce a window gate, do it as a
        # _last_eval-updating + once-per-N-skips logging path so it's
        # operator-visible — not as a bare `return`.

        # Apply runtime profile overrides to strategy configs
        # (Safe/Balanced/Aggressive buttons on dashboard)
        profile_keys = ("min_confluence", "min_momentum", "min_momentum_confidence",
                        "min_precision", "risk_per_trade", "max_daily_loss")
        for strat in self.bot.strategies:
            for key in profile_keys:
                if key in self.bot._runtime_params:
                    strat.config[key] = self.bot._runtime_params[key]

        # Session check
        session_info = self.bot.session.to_dict()

        # Start building eval record for dashboard
        self.bot._last_eval = {
            "ts": datetime.now().isoformat(),
            "regime": session_info.get("regime", "?"),
            "risk_blocked": None,
            "strategies": [],
            "best_signal": None,
        }

        # Minimum bars guard — just 1 completed 1m bar (~60s after connect)
        # Strategies have their own regime-aware gates; no need to double-gate here.
        # The 100-tick buffer from the bridge means we often get bar #1 within seconds.
        bars_5m = list(self.bot.aggregator.bars_5m.completed)
        bars_1m = list(self.bot.aggregator.bars_1m.completed)
        if len(bars_1m) < 1:
            reason = f"Warming up ({len(bars_1m)} 1m bars — need 1, ~1 min)"
            self.bot.last_rejection = reason
            self.bot._last_eval["risk_blocked"] = reason
            logger.info(f"[WARMUP] {reason}")
            return

        # Get market state FIRST (needed by risk gate and everything below)
        market = self.bot.aggregator.snapshot()

        # 2026-05-17: Phase 7 CODE PATCH 6 — pass bar lists through the
        # market dict so sub-evaluators that only receive `market` (like
        # opening_session's _evaluate_orb / _evaluate_orb_fade) can access
        # bars without changing the public evaluate(market, ...) signature.
        # No copy; just a reference. Zero perf impact.
        market["_bars_1m"] = bars_1m
        market["_bars_5m"] = bars_5m
        try:
            market["_bars_15m"] = list(self.bot.aggregator.bars_15m.completed)
        except AttributeError:
            market["_bars_15m"] = []

        # Enrich market snapshot with RSI + HTF pattern data for strategies
        market["rsi"] = self.bot.rsi_divergence.get_current_rsi()
        market["rsi_divergence"] = self.bot._last_rsi_divergence
        market["htf_patterns"] = self.bot.htf_scanner.get_state().get("active_patterns", [])

        # 2026-05-13: CVD trend-health pre-assessment for both directions.
        # Strategies pick the dict that matches their intended direction at
        # entry-time. Stored separately to avoid recomputing inside each
        # strategy. The "cvd_health" key holds the LONG assessment by
        # default; strategies that consider both should also read
        # "cvd_health_short". Cheap to compute (linear regression over 6 bars).
        try:
            market["cvd_health"] = self.bot.cvd_health.assess("LONG")
            market["cvd_health_short"] = self.bot.cvd_health.assess("SHORT")
        except Exception as _cvd_assess_err:
            logger.debug(f"[CVD] health assess failed: {_cvd_assess_err!r}")
            market["cvd_health"] = {"veto": False, "agreement": 0.0, "reason": "assess error"}
            market["cvd_health_short"] = {"veto": False, "agreement": 0.0, "reason": "assess error"}

        # 2026-05-15: Big-Move Detector — pre-move score logged once per
        # eval cycle. Strategies that want to gate entry on it can read
        # `market["big_move_pre"]` (score 0-100 + likely direction).
        # Exhaustion is computed per-position in the position loop.
        try:
            bars_1m_for_bm = list(self.bot.aggregator.bars_1m.completed)
            pre_move = self.bot.big_move.detect_pre_move(
                bars_1m_for_bm, market, atr_5m=market.get("atr_5m", 0),
            )
            market["big_move_pre"] = {
                "score": pre_move.score,
                "likely_direction": pre_move.likely_direction,
                "flags": pre_move.flags,
                "reason": pre_move.reason,
            }
            if pre_move.score >= 50:
                logger.info(
                    f"[BIG_MOVE_PRE] score={pre_move.score} "
                    f"dir={pre_move.likely_direction} flags={pre_move.flags}"
                )
        except Exception as _bm_err:
            logger.debug(f"[BIG_MOVE_PRE] err (non-blocking): {_bm_err!r}")
            market["big_move_pre"] = {"score": 0, "likely_direction": "UNKNOWN", "flags": [], "reason": "detector error"}

        # B14 Phase 4: enrich with MenthorQ gamma state (regime, nearest wall,
        # pin-zone flag, raw GammaLevels). Strategies can read these for
        # context; the entry-wall filter (below, post best_signal pick)
        # uses the same gamma_levels for rejection decisions.
        self.bot._enrich_market_with_gamma(market)

        # Phase 7: Enrich with SMC pattern data
        try:
            smc_state = self.bot.smc.get_state()
            market["smc_structure"] = smc_state.get("structure")
            market["smc_recent"] = smc_state.get("recent_signals", [])[-3:]
        except Exception:
            pass

        # Phase 7: Enrich with HMM regime data
        try:
            hmm_state = self.bot.hmm_regime.get_state()
            market["hmm_regime"] = hmm_state.get("regime")
            market["hmm_confidence"] = hmm_state.get("confidence", 0)
            market["hmm_change_point"] = hmm_state.get("change_point", False)
            market["hmm_regime_params"] = hmm_state.get("regime_params", {})
        except Exception:
            pass

        # Phase 8: Enrich with intermarket risk signal
        try:
            market["intermarket"] = self.bot.intermarket.get_risk_signal()
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
            active = self.bot.pandas_ta.get_active_patterns()
            if active:
                market["candlestick_patterns"] = active
                market["candlestick_confluence"] = self.bot.pandas_ta.get_confluence_score(
                    "LONG" if market.get("tf_votes_bullish", 0) > market.get("tf_votes_bearish", 0) else "SHORT"
                )
        except Exception:
            pass

        # Phase 8: Enrich with geometric chart patterns
        try:
            chart_active = self.bot.chart_patterns.get_active_patterns()
            if chart_active:
                bias_dir = "LONG" if market.get("tf_votes_bullish", 0) > market.get("tf_votes_bearish", 0) else "SHORT"
                market["chart_patterns"] = chart_active
                market["chart_pattern_confluence"] = self.bot.chart_patterns.get_confluence_score(bias_dir)
        except Exception:
            pass

        # Phase 8: Enrich with COT institutional positioning
        try:
            cot = self.bot.cot_feed.get_signal()
            if cot.get("leveraged_fund_net", 0) != 0:
                market["cot"] = cot
        except Exception:
            pass

        # Phase 8: Enrich with calendar risk
        try:
            cal_adj = self.bot.calendar_risk.get_risk_adjustment()
            market["calendar_risk"] = {
                "blocked": cal_adj.blocked,
                "size_multiplier": cal_adj.size_multiplier,
                "stop_multiplier": cal_adj.stop_multiplier,
                "reason": cal_adj.reason,
                "next_event": cal_adj.next_event,
                "minutes_until": cal_adj.minutes_until,
            }
            if cal_adj.blocked:
                self.bot.last_rejection = f"Calendar: {cal_adj.reason}"
                self.bot._last_eval["risk_blocked"] = f"Calendar: {cal_adj.reason}"
                logger.warning(f"[CALENDAR RISK] BLOCKED: {cal_adj.reason}")
                return
        except Exception:
            pass

        # Phase 8: Update playbook based on HMM regime
        try:
            hmm_regime = market.get("hmm_regime", "DEFAULT")
            hmm_conf = market.get("hmm_confidence", 0)
            self.bot.playbook_mgr.update_regime(hmm_regime, hmm_conf)
        except Exception:
            pass

        # 2026-05-06 Sprint J: removed MenthorQ market-snapshot enrichment
        # block (subscription retired). Kept the safe-default keys that
        # legacy consumer code may still read defensively. They're inert.
        market["gamma_regime"] = "UNKNOWN"
        market["above_hvl"] = True
        market["mq_direction_bias"] = "NEUTRAL"

        # Continuation/Reversal Assessment (Quinn-style)
        # Runs every bar — lightweight trajectory lookup + level proximity check
        #
        # B2-3 FIX (2026-05-25): the call previously passed `_mq_snap` which
        # was deleted by the Sprint J MenthorQ cleanup (2026-05-06). Every
        # bar raised NameError, landed in `except Exception:`, and wrote
        # cr_verdict="UNKNOWN" — the CR assessment was silently OFF for 19
        # days. `core.continuation_reversal.assess()` documents mq_snap as
        # deprecated and ignored, so `None` is the correct value.
        # The bare `except Exception:` also got upgraded to log so future
        # silent failures are loud (matches B-006 chandelier policy).
        try:
            from core.continuation_reversal import assess as cr_assess
            from core.momentum_score import get_trajectory
            _cr_traj = get_trajectory(10)
            _cr = cr_assess(market, None, _cr_traj)
            market["cr_verdict"]   = _cr.verdict        # "CONTINUATION"|"REVERSAL"|"CONTESTED"
            market["cr_confidence"]= _cr.confidence     # "LOW"|"MEDIUM"|"HIGH"
            market["cr_direction"] = _cr.direction_bias # "LONG"|"SHORT"|"NEUTRAL"
            market["cr_mom_score"] = _cr.momentum_score
            market["cr_at_resistance"] = _cr.at_call_resistance or _cr.at_day_max
            market["cr_at_support"]    = _cr.at_put_support or _cr.at_day_min
            self.bot._last_cr = _cr  # Store for dashboard and pre-trade prompt
        except Exception as _cr_err:
            logger.warning(f"[CR] assess failed (non-blocking): {_cr_err!r}")
            market["cr_verdict"] = "UNKNOWN"
            self.bot._last_cr = None

        # ── Day Type Classification ────────────────────────────────────
        # Classify the session as TREND / RANGE / VOLATILE and apply
        # day-appropriate parameter overrides (spacing, targets, size).
        # Runs every bar so it adapts if character changes mid-session.
        try:
            _cr_v = market.get("cr_verdict", "UNKNOWN")
            _cr_s = market.get("cr_mom_score", 0) or 0
            _atr  = market.get("atr_5m", 0) or 0
            _vix  = market.get("vix", 0) or 0
            _day  = self.bot._day_classifier.classify(_cr_v, _cr_s, _atr, _vix)

            if _day.day_type != self.bot._day_type:
                self.bot._day_type = _day.day_type
                # Adjust trade spacing dynamically
                self.bot.risk.set_trade_spacing(_day.params["trade_spacing_min"])
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
        can_trade, reason = self.bot.risk.can_trade(vix=vix_proxy)
        if not can_trade:
            self.bot.last_rejection = reason
            self.bot._last_eval["risk_blocked"] = reason
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
            intel = self.bot._latest_intel or {}
            self.bot._cockpit_result = self.bot.cockpit.grade(
                market=market,
                session_info=session_info,
                intel=intel,
                council_result=self.bot._council_result,
            )
            self.bot._last_eval["cockpit"] = self.bot._cockpit_result.get("score", "?")
        except Exception as e:
            logger.debug(f"[COCKPIT] Grading error (non-blocking): {e}")

        # Phase 6b: Update crowding detector with current levels
        try:
            bars_1m_objs = list(self.bot.aggregator.bars_1m.completed)
            self.bot.crowding_detector.update_levels(market, bars_1m_objs)
        except Exception as e:
            logger.debug(f"[CROWDING] Update error (non-blocking): {e}")

        # ─── NEW Apr 2026 SHADOW: compute structural_bias composite ─────
        # Runs alongside old tf_bias. Dual-write — does NOT gate strategies.
        try:
            # Enrich market snapshot with new-module outputs
            _enriched = dict(market)
            _enriched["swing_state"] = self.bot.swing_state_5m.to_dict()
            _enriched["volume_profile"] = self.bot.volume_profile.to_dict()
            _enriched["climax_state"] = self.bot.reversal_detector.get_state()
            _enriched["sweep_state"] = self.bot.sweep_watcher.get_state()
            _enriched["footprint_signals"] = self.bot._last_footprint_signals
            # Chart patterns v1: wrap existing detector output with context weighting
            try:
                _cp_state = {"active_5m": [], "active_15m": [], "active_60m": []}
                _chart_pats_v1 = extract_v1_patterns(_cp_state, _enriched)
                self.bot._last_chart_patterns_v1 = [p.to_dict() for p in _chart_pats_v1]
                _enriched["chart_patterns_v1"] = self.bot._last_chart_patterns_v1
            except Exception:
                _enriched["chart_patterns_v1"] = []

            # 2026-05-06 Sprint J: removed MenthorQ context enrichment
            # (subscription retired). Empty dict preserved so legacy
            # consumers that read _enriched["menthorq"] don't KeyError.
            _enriched["menthorq"] = {}

            # Gamma flip state
            _enriched["gamma_flip_state"] = self.bot.gamma_flip_detector.get_state()

            # VIX term structure (cached, refreshes every 10 min)
            try:
                _vix = get_vix_term_cached()
                self.bot._last_vix_term = _vix.to_dict()
                _enriched["vix_term_structure"] = self.bot._last_vix_term
            except Exception:
                _enriched["vix_term_structure"] = {}

            # Pinning state (last 90 min of RTH)
            try:
                _last_5m = list(self.bot.aggregator.bars_5m.completed)[-1] if self.bot.aggregator.bars_5m.completed else None
                _vol_ma = self.bot.aggregator.atr.get("5m", 0) * 1000  # Rough volume baseline
                _pin = self.bot.pinning_detector.update(
                    datetime.now(), _mq_price, _enriched.get("menthorq", {}),
                    _last_5m, _vol_ma
                )
                self.bot._last_pinning_state = {
                    "pin_risk_active": _pin.pin_risk_active,
                    "pinning_level": _pin.pinning_level,
                    "pin_level_name": _pin.pin_level_name,
                    "distance_ticks": _pin.distance_ticks,
                    "reasoning": _pin.reasoning,
                }
                _enriched["pinning_state"] = self.bot._last_pinning_state
            except Exception:
                _enriched["pinning_state"] = {}

            # OpEx status
            try:
                _opex = get_opex_status()
                self.bot._last_opex_status = {
                    "is_opex_day": _opex.is_opex_day,
                    "is_triple_witching": _opex.is_triple_witching,
                    "size_reduction_factor": _opex.size_reduction_factor,
                    "veto_continuation_patterns": _opex.veto_continuation_patterns,
                    "reasoning": _opex.reasoning,
                }
                _enriched["opex_status"] = self.bot._last_opex_status
            except Exception:
                _enriched["opex_status"] = {}

            # ES confirmation
            try:
                _es = check_es_confirmation(_enriched.get("menthorq", {}).get("gex_regime", "UNKNOWN"))
                self.bot._last_es_confirmation = {
                    "aligned": _es.aligned,
                    "confluence_adjust": _es.confluence_adjust,
                    "es_data_available": _es.es_data_available,
                    "reasoning": _es.reasoning,
                    "es_regime": _es.es_regime,
                    "nq_regime": _es.nq_regime,
                }
                _enriched["es_confirmation"] = self.bot._last_es_confirmation
            except Exception:
                _enriched["es_confirmation"] = {}

            # Compute the composite
            bias = compute_structural_bias(_enriched)
            self.bot._last_structural_bias = bias.to_dict()
            # Log periodically (every 10 calls) to avoid noise
            if not hasattr(self.bot, "_bias_log_counter"):
                self.bot._bias_log_counter = 0
            self.bot._bias_log_counter += 1
            if self.bot._bias_log_counter % 10 == 0:
                logger.info(f"[STRUCTURAL BIAS] {bias.label} score={bias.score:+d} "
                            f"conf={bias.confidence}% vetoes={len(bias.vetoes)}")
        except Exception as e:
            logger.debug(f"[STRUCTURAL BIAS] compute error (non-blocking): {e}")

        # 2026-05-21 SHIP AUDIT pt4 (S-001 — B-030 CLONE FIX):
        # core/regime_playbooks.py PLAYBOOKS dict was silently overriding
        # production strategy gates with HMM-regime-conditioned values
        # using the EXACT same pattern as the sim_bot ZERO_GATE bug we
        # just fixed in B-030. For bias_momentum TRENDING regime, PLAYBOOKS
        # loosens min_confluence 5.5 → 1.5 (73% looser), min_momentum
        # 80 → 25 (69% looser). Same hidden-lab-era values.
        #
        # Currently MASKED on prod_bot because FORCE_ACCOUNT="Sim101" =
        # paper. But the moment prod_bot.FORCE_ACCOUNT changes or
        # LIVE_TRADING=True, this lands B-030 on real money. Sim_bot
        # already overrides _evaluate_strategies and doesn't call super,
        # so this block has NEVER run on sim — only prod (paper).
        #
        # Gate behind PLAYBOOK_ENABLED. Default False until each playbook
        # entry is validated against the same 5y backtest that production
        # gates passed. Backtest tool (tools/phoenix_real_backtest.py:1104)
        # uses STRATEGIES directly — does NOT apply playbooks — so PLAYBOOKS
        # values are unvalidated.
        try:
            from config.settings import PLAYBOOK_ENABLED
        except ImportError:
            PLAYBOOK_ENABLED = False
        if PLAYBOOK_ENABLED:
            try:
                for strat in self.bot.strategies:
                    pb_overrides = self.bot.playbook_mgr.get_strategy_overrides(strat.name)
                    for k, v in pb_overrides.items():
                        strat.config[k] = v
            except Exception:
                pass

        # ── Day-type strategy suppression ─────────────────────────────
        # On RANGE days bias_momentum underperforms; on VOLATILE days breakouts fail.
        # DayClassifier sets which strategies to suppress for the current day type.
        _day_suppressed = set(self.bot._day_classifier.params.get("suppressed_strategies", []))
        _day_target_rr  = self.bot._day_classifier.params.get("default_target_rr", 0)

        # ── Sprint M Tier 1.2: enrich `market` with context fields ───
        # sweep_watcher.get_state() and structural_bias.to_dict() are
        # computed up in the shadow-pipeline block (~line 2563) into a
        # separate `_enriched` dict, but until today the strategy loop
        # passed the un-enriched `market` to evaluate(), so strategies
        # never saw these context signals. footprint_cvd_reversal's
        # IQS context-bonus (Sprint M) needs them, so we merge selectively
        # here. Selective rather than wholesale-replace because some
        # strategies are sensitive to dict shape; only fields with known
        # consumers get plumbed.
        try:
            if getattr(self.bot, "_last_structural_bias", None):
                market["structural_bias"] = self.bot._last_structural_bias
            _sweep_state = self.bot.sweep_watcher.get_state()
            if _sweep_state:
                market["sweep_state"] = _sweep_state
            # Sprint M Tier 2.3: tape reader's rolling large-print window.
            # Pure observation field — consumed only by future analysis
            # tooling tonight, no strategy reads it yet.
            market["tape_state"] = self.bot.tape_reader.get_state()
        except Exception as _e:
            logger.debug(f"[CONTEXT_ENRICH] market enrich failed: {_e!r}")

        best_signal = None
        for strat in self.bot.strategies:
            if not strat.enabled:
                logger.debug(f"  [{strat.name}] SKIP — disabled")
                self.bot._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_DISABLED"})
                continue
            if not self.bot.session.is_strategy_allowed(strat.name):
                logger.debug(f"  [{strat.name}] SKIP — not allowed in {session_info.get('regime')}")
                self.bot._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_REGIME"})
                continue
            # Day-type suppression (RANGE suppresses bias_momentum, VOLATILE suppresses breakouts)
            if strat.name in _day_suppressed:
                logger.info(f"  [{strat.name}] SKIP — suppressed on {self.bot._day_type} day")
                self.bot._last_eval["strategies"].append({
                    "name": strat.name, "result": "SKIP_DAY_TYPE",
                    "reason": f"{self.bot._day_type} day"
                })
                continue
            # Phase 8: Playbook suppression check
            if self.bot.playbook_mgr.is_strategy_suppressed(strat.name):
                logger.info(f"  [{strat.name}] SKIP — suppressed by {self.bot.playbook_mgr.get_current().name} playbook")
                self.bot._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_PLAYBOOK"})
                continue

            try:
                signal = strat.evaluate(market, bars_5m, bars_1m, session_info)
                if signal:
                    # Strategies are now regime-aware internally (bias_momentum uses
                    # _REGIME_OVERRIDES to loosen/tighten per regime). No external
                    # confluence override needed — that was comparing wrong dimensions.
                    # P4-2 (2026-05-24): stamp the SIGNAL log line with the per-signal
                    # trace ID. Lazy-imported to keep core.trace_id off this module's
                    # import path (it's already loaded via strategies.base_strategy,
                    # but the lazy form keeps the dependency explicit).
                    try:
                        from core.trace_id import format_trace_log, STAGE_SIGNAL
                        _trace_prefix = format_trace_log(
                            STAGE_SIGNAL, signal.trace_id, ""
                        ).rstrip()
                    except Exception:
                        _trace_prefix = ""
                    logger.info(f"{_trace_prefix}  [{strat.name}] SIGNAL: {signal.direction} conf={signal.confidence:.0f} "
                                 f"score={signal.entry_score:.0f} — {signal.reason}")
                    # ── Rider strategies: unlimited target on ALL days ─────────
                    # Target 20:1 = 800 ticks (200 pts) from entry. The OCO bracket
                    # exists as a safety net, not a profit target. Real exits come from:
                    #   - Reversal exit (DOM + wick both confirmed, 10pt+ profit)
                    #   - Stall detector STRONG (trend genuinely exhausted)
                    #   - BE stop → stop_loss at breakeven (worst case: no loss)
                    # Goal: 20-50 pts on range/volatile days, 100+ on trend days.
                    # Not 3-point scalps that leave 100 points on the table.
                    _RIDER_STRATEGIES = {"bias_momentum"}  # dom_pullback removed 2026-05-21
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
                            f"Target widened to {_day_target_rr}:1 ({self.bot._day_type} day)"
                        )
                    self.bot._last_eval["strategies"].append({
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
                        self.bot._last_eval["strategies"].append({"name": strat.name, "result": "REJECTED", "reason": reject})
                        strat._last_reject = ''
                    else:
                        logger.info(f"  [{strat.name}] no signal")
                        self.bot._last_eval["strategies"].append({"name": strat.name, "result": "NO_SIGNAL"})
            except Exception as e:
                logger.error(f"  [{strat.name}] ERROR: {e}")
                self.bot._last_eval["strategies"].append({"name": strat.name, "result": "ERROR", "reason": str(e)})

        # ── Always capture HTF scanner state (even when no signal fires) ──
        try:
            htf_state = self.bot.htf_scanner.get_state()
            self.bot._last_eval["htf_state"] = htf_state
        except Exception:
            pass

        # 2026-05-06 Sprint J: removed gamma-wall entry filter
        # (depended on MenthorQ levels — subscription retired). Self.gamma_levels
        # is always None now so this gate would never fire anyway.

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
                if self.bot._last_cr is not None:
                    cr_mom_score = getattr(self.bot._last_cr, "momentum_score", 0) or 0
                if (cr_verdict == "CONTINUATION" and
                        cr_mom_score >= 4 and
                        best_signal.strategy in _CR_SUPPRESSED_STRATEGIES):
                    block_reason = (f"C/R bias filter: BULLISH CONTINUATION day "
                                    f"(score={cr_mom_score}) — SHORT from {best_signal.strategy} suppressed")
                    logger.info(f"[CR BIAS FILTER] {block_reason}")
                    self.bot.last_rejection = block_reason
                    try:
                        sig_dict = {"direction": best_signal.direction,
                                    "strategy": best_signal.strategy,
                                    "confidence": best_signal.confidence,
                                    "entry_score": best_signal.entry_score,
                                    "reason": best_signal.reason}
                        self.bot.history.log_near_miss(sig_dict, market, f"cr_bias: {block_reason}")
                    except Exception:
                        pass
                    best_signal = None
            except Exception as e:
                logger.debug(f"[CR BIAS FILTER] Non-blocking error: {e}")

        # 2026-05-06 Sprint J: MenthorQ HVL direction gate + stop multiplier
        # block REMOVED. Subscription retired; the gate was always allowing
        # all directions and stop_multiplier=1.0 (verified pre-removal).

        if best_signal:
            # Phase 6: Apply regime transition bonus to best signal's confidence
            regime = session_info.get("regime", "UNKNOWN")
            transition_bonus = self.bot.regime_transitions.get_transition_bonus(regime)
            if transition_bonus.get("active"):
                bonus = transition_bonus["bonus_score"]
                best_signal.confidence = min(100, best_signal.confidence + bonus)
                logger.info(f"[TRANSITION BONUS] +{bonus} confidence -> {best_signal.confidence:.0f} "
                             f"({transition_bonus['description']})")
                self.bot._last_eval["transition_bonus"] = transition_bonus

            # Phase 6+: RSI divergence confluence boost
            try:
                rsi_div = self.bot._last_rsi_divergence
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
                        # 2026-05-03 fix(rsi): opposing-RSI-div is now a hard
                        # gate when the source strategy has rsi_div_hard_gate=True.
                        # Forensic audit: this confluence appeared in 6 losers /
                        # 0 winners in bias_momentum dataset. Research-backed —
                        # regular RSI divergence during established trend is a
                        # documented ~65%+ accurate momentum-exhaustion signal
                        # (tradealgo.com, alchemymarkets.com).
                        _hard_gate = False
                        for _strat in self.bot.strategies:
                            if getattr(_strat, "name", None) == best_signal.strategy:
                                _hard_gate = bool(_strat.config.get("rsi_div_hard_gate", False))
                                break
                        if _hard_gate:
                            logger.info(
                                f"[REJECT:{best_signal.strategy}] opposing RSI "
                                f"{rsi_div['type']} divergence — hard gate "
                                f"(was 0W/6L empirically; trend-tiring signal)"
                            )
                            self.bot._last_eval["rsi_divergence"] = rsi_div
                            self.bot._last_eval["hard_gate_rejected"] = (
                                f"opposing RSI {rsi_div['type']} div"
                            )
                            # Early return — short-circuits the rest of the
                            # eval pipeline. No entry will fire for this tick.
                            return
                        else:
                            # Legacy soft-warning behavior (flag disabled)
                            best_signal.confluences.append(
                                f"Warning: opposing RSI {rsi_div['type']} div "
                                f"(RSI={rsi_div['rsi_current']:.0f})")
                            logger.info(f"[RSI DIV] Opposing {rsi_div['type']} divergence "
                                         f"(observation only, not blocking)")
                    if best_signal is not None:
                        self.bot._last_eval["rsi_divergence"] = rsi_div
            except Exception as e:
                logger.debug(f"[RSI DIV] Error (non-blocking): {e}")

            # Phase 6+: HTF pattern confluence boost
            try:
                htf_conf = self.bot.htf_scanner.get_confluence_score(best_signal.direction)
                self.bot._last_htf_confluence = htf_conf
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
                self.bot._last_eval["htf_confluence"] = htf_conf
            except Exception as e:
                logger.debug(f"[HTF PATTERNS] Error (non-blocking): {e}")

            # Phase 7: SMC pattern confluence boost
            try:
                smc_conf = self.bot.smc.get_confluence_score(best_signal.direction)
                if smc_conf["aligned_count"] > 0 and smc_conf["score"] > 30:
                    smc_boost = min(20, int(smc_conf["score"] / 4))
                    best_signal.confidence = min(100, best_signal.confidence + smc_boost)
                    pat = smc_conf["strongest_pattern"] or "pattern"
                    best_signal.confluences.append(
                        f"SMC {pat} +{smc_boost} ({smc_conf['aligned_count']} aligned)")
                    logger.info(f"[SMC BOOST] +{smc_boost} confidence -> {best_signal.confidence:.0f} "
                                f"({smc_conf['strongest_description']})")
                self.bot._last_eval["smc"] = smc_conf
            except Exception as e:
                logger.debug(f"[SMC] Confluence error (non-blocking): {e}")

            # Phase 6: No-trade fingerprint risk check (advisory only)
            fp_result = self.bot.no_trade_fp.get_risk_score(
                market=market,
                session_info=session_info,
                signal=best_signal,
                trade_count_today=self.bot.risk.state.trades_today,
            )
            self.bot._last_eval["fingerprint"] = fp_result
            if fp_result["risk_score"] > 0:
                logger.info(f"[FINGERPRINT] Risk={fp_result['risk_score']} "
                             f"({fp_result['recommendation']}) "
                             f"matches={len(fp_result['matching_fingerprints'])}")

            # Phase 6b: Crowding score (observation only)
            try:
                crowding = self.bot.crowding_detector.get_crowding_score(
                    entry_price=market.get("price", 0),
                    direction=best_signal.direction,
                    market=market,
                )
                self.bot._last_eval["crowding"] = crowding
            except Exception as e:
                logger.debug(f"[CROWDING] Score error (non-blocking): {e}")

            # Phase 6b: Counter-edge check (observation only)
            try:
                counter = self.bot.counter_edge.check_counter_signal(
                    strategy=best_signal.strategy,
                    direction=best_signal.direction,
                    regime=session_info.get("regime", "UNKNOWN"),
                    market=market,
                )
                if counter:
                    self.bot._last_eval["counter_edge"] = counter
                    logger.info(f"[COUNTER] Counter-edge detected: {counter['description']}")
            except Exception as e:
                logger.debug(f"[COUNTER] Check error (non-blocking): {e}")

            logger.info(f"[TRADE QUEUED:{best_signal.trade_id}] {best_signal.direction} "
                         f"via {best_signal.strategy} conf={best_signal.confidence:.0f}")
            # Track signal as GENERATED (not taken yet — fill may fail or filter may block)
            self.bot.tracker.record_signal(
                strategy=best_signal.strategy,
                direction=best_signal.direction,
                confidence=best_signal.confidence,
                taken=False,  # Will be updated to True only after confirmed fill
                regime=session_info.get("regime", "UNKNOWN"),
                trade_id=best_signal.trade_id,
            )
            self.bot._last_eval["best_signal"] = {
                "direction": best_signal.direction,
                "strategy": best_signal.strategy,
                "confidence": best_signal.confidence,
                "reason": best_signal.reason,
            }
            self.bot.last_signal = {
                "direction": best_signal.direction,
                "strategy": best_signal.strategy,
                "confidence": best_signal.confidence,
                "entry_score": best_signal.entry_score,
                "reason": best_signal.reason,
                "confluences": best_signal.confluences,
            }
            # 2026-05-24 P1-1 Stage 1: stash the fully-enriched market dict so
            # _enter_trade can merge it into the persisted market_snapshot.
            # `market` at this point contains all the enrichment fields
            # (day_type, cr_verdict, cvd_health, cvd_health_short, es_nq_rs,
            # intermarket, advisor_guidance, etc.) that aren't in the
            # aggregator's fresh snapshot. Without this stash, those fields
            # are missing from trade_memory and the reconciliation harness
            # cannot deterministically replay the trade.
            # Shallow-copy because some downstream paths mutate `market`.
            self.bot._last_enriched_market = dict(market)
            # Queue trade (will be executed in main async loop)
            self.bot._pending_signal = best_signal
        else:
            self.bot.last_signal = None
            # DON'T clear _pending_signal here — a prior eval may have set it
            # and the tick loop hasn't consumed it yet (race condition with
            # rapid 1m+5m bar completions).

        # Persist full eval to history (every bar evaluation, signal or not)
        self.bot.history.log_eval(self.bot._last_eval, market)
