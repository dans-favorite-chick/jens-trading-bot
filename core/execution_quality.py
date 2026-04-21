"""
Phoenix Bot -- Execution Quality Tracker

Tracks execution quality metrics per strategy.
Answers: 'Is our edge real after slippage and latency?'

OBSERVATION + ADVISORY -- logs and scores execution, does NOT block trades.
"""

import json
import logging
import os
import time
from collections import defaultdict

logger = logging.getLogger("ExecutionQuality")

TICK_SIZE = 0.25


class ExecutionQuality:
    """
    Tracks execution quality metrics per strategy.
    Answers: 'Is our edge real after slippage and latency?'
    """

    def __init__(self):
        self._records: list[dict] = []
        self._file = "logs/performance/execution_quality.json"
        self._load()

    def _load(self):
        """Load persisted records from disk."""
        try:
            if os.path.exists(self._file):
                with open(self._file, "r") as f:
                    data = json.load(f)
                self._records = data.get("records", [])
                logger.info(f"[EXEC_Q] Loaded {len(self._records)} execution records")
        except Exception as e:
            logger.debug(f"[EXEC_Q] Load error (starting fresh): {e}")
            self._records = []

    def _save(self):
        """Persist to disk."""
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({
                    "records": self._records[-500:],  # Keep last 500
                    "updated": time.time(),
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"[EXEC_Q] Save error (non-blocking): {e}")

    def record(self, trade_id: str, signal_price: float, entry_price: float,
               exit_price: float, pnl_ticks: float, fill_latency_ms: float,
               strategy: str, regime: str):
        """
        Record execution metrics for one trade.

        Args:
            trade_id: Unique trade identifier
            signal_price: Price when signal was generated
            entry_price: Actual fill price
            exit_price: Actual exit price
            pnl_ticks: Realized P&L in ticks
            fill_latency_ms: Time from signal to fill in milliseconds
            strategy: Strategy name
            regime: Market regime at time of trade
        """
        # Entry slippage: how many ticks did we lose on entry?
        # Positive = unfavorable (paid more for long / got less for short)
        entry_slip = round((entry_price - signal_price) / TICK_SIZE, 1)

        # Paper edge = what P&L would have been with perfect fill at signal price
        # For a long: paper_pnl = (exit - signal) / tick_size
        # For simplicity, paper edge = pnl_ticks + abs(entry_slip)
        paper_edge = round(pnl_ticks + abs(entry_slip), 1)

        # Edge decay = how much edge lost to execution
        if paper_edge != 0:
            edge_decay_pct = round(abs(entry_slip) / abs(paper_edge) * 100, 1)
        else:
            edge_decay_pct = 0.0

        record = {
            "trade_id": trade_id,
            "signal_price": signal_price,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_slippage_ticks": entry_slip,
            "pnl_ticks": pnl_ticks,
            "paper_edge_ticks": paper_edge,
            "edge_decay_pct": edge_decay_pct,
            "fill_latency_ms": fill_latency_ms,
            "strategy": strategy,
            "regime": regime,
            "timestamp": time.time(),
        }

        self._records.append(record)

        logger.info(
            f"[EXEC_Q] {trade_id}: slip={entry_slip:+.1f}t "
            f"latency={fill_latency_ms:.0f}ms "
            f"paper={paper_edge:.1f}t realized={pnl_ticks:.1f}t "
            f"decay={edge_decay_pct:.0f}%"
        )

        self._save()

    def get_strategy_quality(self, strategy: str = None) -> dict:
        """
        Get execution quality metrics, optionally filtered by strategy.

        Args:
            strategy: Filter to specific strategy, or None for all

        Returns: {
            avg_entry_slippage_ticks: float,
            avg_fill_latency_ms: float,
            paper_edge_ticks: float,
            realized_edge_ticks: float,
            edge_decay_pct: float,
            best_execution_regime: str,
            worst_execution_regime: str,
            trade_count: int,
        }
        """
        records = self._records
        if strategy:
            records = [r for r in records if r.get("strategy") == strategy]

        if not records:
            return {
                "avg_entry_slippage_ticks": 0.0,
                "avg_fill_latency_ms": 0.0,
                "paper_edge_ticks": 0.0,
                "realized_edge_ticks": 0.0,
                "edge_decay_pct": 0.0,
                "best_execution_regime": "N/A",
                "worst_execution_regime": "N/A",
                "trade_count": 0,
            }

        n = len(records)
        avg_slip = round(sum(r["entry_slippage_ticks"] for r in records) / n, 2)
        avg_latency = round(sum(r["fill_latency_ms"] for r in records) / n, 1)
        avg_paper = round(sum(r["paper_edge_ticks"] for r in records) / n, 2)
        avg_realized = round(sum(r["pnl_ticks"] for r in records) / n, 2)

        if avg_paper != 0:
            total_decay = round(abs(avg_slip) / abs(avg_paper) * 100, 1)
        else:
            total_decay = 0.0

        # Per-regime analysis
        regime_quality: dict[str, list] = defaultdict(list)
        for r in records:
            regime_quality[r.get("regime", "UNKNOWN")].append(
                abs(r["entry_slippage_ticks"])
            )

        best_regime = "N/A"
        worst_regime = "N/A"
        if regime_quality:
            regime_avg = {
                regime: sum(slips) / len(slips)
                for regime, slips in regime_quality.items()
                if len(slips) >= 2  # Need at least 2 trades
            }
            if regime_avg:
                best_regime = min(regime_avg, key=regime_avg.get)
                worst_regime = max(regime_avg, key=regime_avg.get)

        return {
            "avg_entry_slippage_ticks": avg_slip,
            "avg_fill_latency_ms": avg_latency,
            "paper_edge_ticks": avg_paper,
            "realized_edge_ticks": avg_realized,
            "edge_decay_pct": total_decay,
            "best_execution_regime": best_regime,
            "worst_execution_regime": worst_regime,
            "trade_count": n,
        }

    def to_dict(self) -> dict:
        """For dashboard display."""
        overall = self.get_strategy_quality()

        # Per-strategy breakdown
        strategies = set(r.get("strategy", "") for r in self._records)
        per_strategy = {}
        for strat in strategies:
            if strat:
                sq = self.get_strategy_quality(strat)
                if sq["trade_count"] > 0:
                    per_strategy[strat] = sq

        return {
            "overall": overall,
            "per_strategy": per_strategy,
            "total_records": len(self._records),
        }
