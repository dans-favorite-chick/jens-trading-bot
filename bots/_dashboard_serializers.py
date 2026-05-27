"""Dashboard serializers — extracted from base_bot.py 2026-05-24 (P4-1 Stage 2).

Three module-level functions that build JSON-serializable dicts from
bot state, for the Flask dashboard. Pure read operations — no state
mutation, no I/O, no side effects.

Called from:
- BaseBot.to_dict() (thin delegation) -> DashboardPusher pushes to Flask
- BaseBot._menthorq_to_dict() (thin delegation) - MQ retired Sprint J,
  but the method is kept as a stub for backward compat.
- BaseBot._cr_to_dict() (thin delegation) - continuation/reversal panel.

Originals: bots/base_bot.py:5492, 5502, 5528.

Each `_safe` lambda is wrapped in try/except in `bot_to_dict` so one
panel failure does NOT kill the entire state push to the dashboard.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("DashboardSerializers")


def menthorq_to_dict(bot) -> dict:
    """MQ panel state (retired Sprint J — returns empty stub).

    Body of BaseBot._menthorq_to_dict() (base_bot.py:5492).

    Subscription cancelled. Dashboard MenthorQ panel removed in the
    same commit; this function is preserved as a stub so any legacy
    serialization map that still references it doesn't KeyError.
    """
    return {"gamma_regime": "UNKNOWN", "retired": True}


def cr_to_dict(bot) -> dict:
    """Continuation/reversal panel state.

    Body of BaseBot._cr_to_dict() (base_bot.py:5502). Reads
    `bot._last_cr` (a CRAssessment dataclass or None).
    """
    cr = getattr(bot, "_last_cr", None)
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


def bot_to_dict(bot) -> dict:
    """Full bot state for the dashboard.

    Body of BaseBot.to_dict() (base_bot.py:5528). Aggregates ~50
    sub-component snapshots. Each sub-component is wrapped in
    try/except so one failure doesn't kill the entire state push.

    Reads `LIVE_TRADING` indirectly via the bot module's globals —
    we re-import it here from config.settings to keep parity with
    the original (which used the module-level LIVE_TRADING constant
    imported at the top of base_bot.py).
    """
    # LIVE_TRADING is a module-level constant in base_bot.py; importing
    # from config.settings preserves the same value.
    from config.settings import LIVE_TRADING

    market = bot.aggregator.snapshot()
    # Core fields — must always succeed
    result = {
        "bot_name": bot.bot_name,
        "status": bot.status,
        "live_trading": LIVE_TRADING,
        "market": market,
        "last_signal": bot.last_signal,
        "last_rejection": bot.last_rejection,
        "last_eval": bot._last_eval,
        "day_type": bot._day_classifier.get_state(),   # TREND/RANGE/VOLATILE + params
        "council": bot._council_result,
        "filter_verdict": bot._filter_verdict,
        "clustering": bot._clustering_result,
        "rsi_last_divergence": bot._last_rsi_divergence,
        "htf_last_confluence": bot._last_htf_confluence,
    }
    # Each sub-component wrapped so one failure doesn't kill the entire state push
    _safe = {
        "position":              lambda: bot.positions.to_dict(market.get("price", 0)),
        "risk":                  lambda: bot.risk.to_dict(),
        "session":               lambda: bot.session.to_dict(),
        "strategies":            lambda: [{"name": s.name, "enabled": s.enabled, "validated": s.validated, "params": s.params} for s in bot.strategies],
        "trades":                lambda: bot.trade_memory.recent(20),
        "strategy_performance":  lambda: bot.tracker.to_dict(),
        "cockpit":               lambda: bot.cockpit.to_dict(bot._cockpit_result),
        "equity":                lambda: bot.equity_tracker.to_dict(),
        "expectancy":            lambda: bot.expectancy.to_dict(),
        "no_trade_fingerprints": lambda: bot.no_trade_fp.to_dict(),
        "regime_transitions":    lambda: bot.regime_transitions.to_dict(),
        "microstructure_filter": lambda: bot.microstructure_filter.to_dict(),
        "crowding_detector":     lambda: bot.crowding_detector.to_dict(),
        "counter_edge":          lambda: bot.counter_edge.to_dict(),
        "execution_quality":     lambda: bot.execution_quality.to_dict(),
        "rsi_divergence":        lambda: bot.rsi_divergence.get_state(),
        "htf_patterns":          lambda: bot.htf_scanner.get_state(),
        "hmm_regime":            lambda: bot.hmm_regime.to_dict(),
        "trade_rag":             lambda: bot.trade_rag.to_dict(),
        "smc_patterns":          lambda: bot.smc.to_dict(),
        "calendar_risk":         lambda: bot.calendar_risk.to_dict(),
        "menthorq":              lambda: menthorq_to_dict(bot),
        "cr_assessment":         lambda: cr_to_dict(bot),
        "playbook":              lambda: bot.playbook_mgr.to_dict(),
        "intermarket":           lambda: bot.intermarket.to_dict(),
        "edge_miner":            lambda: bot.edge_miner.to_dict(),
        "knowledge_rag":         lambda: bot.knowledge_rag.to_dict(),
        "pandas_ta":             lambda: bot.pandas_ta.to_dict(),
        "chart_patterns":        lambda: bot.chart_patterns.to_dict(),
        "cot_feed":              lambda: bot.cot_feed.to_dict(),
        # ─── NEW Apr 2026 SHADOW modules ───────────────────────────
        "structural_bias":       lambda: bot._last_structural_bias,
        "footprint_signals":     lambda: bot._last_footprint_signals,
        "footprint_current":     lambda: (bot.footprint_5m.current_bar().__dict__
                                          if bot.footprint_5m.current_bar() else {}),
        "footprint_last_completed": lambda: (bot.footprint_5m.last_completed().__dict__
                                              if bot.footprint_5m.last_completed() else {}),
        "swing_state":           lambda: bot.swing_state_5m.to_dict(),
        "volume_profile":        lambda: bot.volume_profile.to_dict(),
        "climax_state":          lambda: bot.reversal_detector.get_state(),
        "sweep_state":           lambda: bot.sweep_watcher.get_state(),
        "tape_state":            lambda: bot.tape_reader.get_state(),
        "chart_patterns_v1":     lambda: bot._last_chart_patterns_v1,
        "gamma_flip_state":      lambda: bot.gamma_flip_detector.get_state(),
        "vix_term_structure":    lambda: bot._last_vix_term,
        "pinning_state":         lambda: bot._last_pinning_state,
        "opex_status":           lambda: bot._last_opex_status,
        "es_confirmation":       lambda: bot._last_es_confirmation,
        "decay_monitor_summary": lambda: bot.decay_monitor.summary(),
        "tca_weekly_report":     lambda: bot.tca_tracker.weekly_report(),
        "circuit_breakers_state": lambda: bot.circuit_breakers.get_state(),
        "sizing_config":         lambda: bot.simple_sizer.config,
    }
    for key, fn in _safe.items():
        try:
            result[key] = fn()
        except Exception as e:
            logger.debug(f"bot_to_dict: {key} failed: {e}")
            result[key] = None
    return result
