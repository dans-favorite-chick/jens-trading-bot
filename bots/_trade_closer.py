"""Trade-close bookkeeping — extracted from base_bot.py 2026-05-24
(P4-1 Stage 3). Post-trade observability only — no OIF, no entry, no
risk-gate mutation.

Updates circuit_breakers rolling counters, feeds the tier_3000 equity
tracker when active, and emits the chart-overlay exit marker when a
trade closes. Called from BaseBot when a position resolves.

Original location: bots/base_bot.py: BaseBot._on_trade_closed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("TradeCloser")


class TradeCloser:
    def __init__(self, bot):
        self.bot = bot

    def on_trade_closed(self, trade: dict) -> None:
        """Body of BaseBot._on_trade_closed, behaviorally verbatim.

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
        # P4-2 (2026-05-24): pull the trace_id from the trade record's
        # market_snapshot (persisted by _trade_entry) and rebind it so the
        # CLOSE-stage log lines (circuit_breakers.record_*, tier_sizer,
        # chart overlay emit_exit) carry the same [TRACE:xxx] prefix as
        # the rest of the lifecycle.
        _trace = (trade.get("market_snapshot", {}) or {}).get("trace_id")
        from core.trace_id import TraceContext as _TraceContext
        with _TraceContext(_trace):
            try:
                self.bot.circuit_breakers.record_slippage(trade.get("slippage_ticks", 0))
            except Exception as e:
                logger.debug(f"[_on_trade_closed] record_slippage error (non-blocking): {e}")
            try:
                self.bot.circuit_breakers.record_trade_outcome(trade.get("result", "UNKNOWN"))
            except Exception as e:
                logger.debug(f"[_on_trade_closed] record_trade_outcome error (non-blocking): {e}")

            # F-001: feed equity tracker only when tier_3000 is active. The
            # tracker mutates data/equity_state.json — keep it quiescent for
            # flat_1 operators so nothing on disk changes for them.
            try:
                from config.settings import SIZING_MODE as _SIZING_MODE
                if _SIZING_MODE == "tier_3000":
                    from core.tier_sizer import record_trade_close as _tier_record
                    _pnl = float(trade.get("pnl_dollars", 0.0) or 0.0)
                    _result = (trade.get("result") or "").upper()
                    _was_winner = None
                    if _result == "WIN":
                        _was_winner = True
                    elif _result == "LOSS":
                        _was_winner = False
                    _tier_record(
                        pnl_dollars=_pnl,
                        was_winner=_was_winner,
                        strategy=trade.get("strategy"),
                        trade_id=trade.get("trade_id"),
                    )
            except Exception as e:
                logger.warning(
                    f"[_on_trade_closed] tier_sizer.record_trade_close failed: {e!r}"
                )

            # Chart overlay hook 4/4: trade closed. Writes X marker + P&L
            # annotation to NT8 PhoenixTradeOverlay indicator.
            # _signal_viz is a module-level import in base_bot.py; the
            # delegator imports it lazily here to avoid a hard import cycle.
            from bots import base_bot as _bb
            _bb._signal_viz.emit_exit(
                trade_id=trade.get("trade_id", ""),
                exit_price=float(trade.get("exit_price", 0.0)),
                exit_reason=str(trade.get("exit_reason", "unknown")),
                pnl=float(trade.get("pnl_dollars", 0.0)),
            )
