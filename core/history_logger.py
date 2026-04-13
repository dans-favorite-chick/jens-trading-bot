"""
Phoenix Bot — History Logger

Writes a rich chronological event log every trading day.
One JSONL file per day: logs/history/YYYY-MM-DD.jsonl

Each line is a self-contained JSON event:
  bar    — every completed 1m/5m bar with full market snapshot
  eval   — every strategy evaluation (signal or no-signal, with why)
  entry  — trade entry with full market context
  exit   — trade exit with P&L and reason

This is the raw material for all AI learning agents:
  - Session debriefer reads today's file at close
  - Weekly learner reads the last N days
  - Historical analyzer reads everything

Schema is append-only — never modify past events.
"""

import json
import logging
import os
from datetime import datetime, date

logger = logging.getLogger("HistoryLogger")

HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "history")


class HistoryLogger:
    """
    Lightweight append-only event logger.
    Opens a new file each trading day, writes one JSON object per line.
    Thread-safe for single-threaded async use (no locks needed).
    """

    def __init__(self, bot_name: str = "bot"):
        self.bot_name = bot_name
        self._today: date | None = None
        self._file = None
        os.makedirs(HISTORY_DIR, exist_ok=True)

    # ─── Internal ───────────────────────────────────────────────────
    def _get_file(self):
        """Return open file handle for today, rotating at midnight."""
        today = datetime.now().date()
        if today != self._today:
            if self._file:
                self._file.close()
            path = os.path.join(HISTORY_DIR, f"{today}_{self.bot_name}.jsonl")
            self._file = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered
            self._today = today
            logger.info(f"[HistoryLogger] Writing to {path}")
        return self._file

    def _write(self, event: dict):
        try:
            f = self._get_file()
            f.write(json.dumps(event, default=str) + "\n")
        except Exception as e:
            logger.warning(f"[HistoryLogger] Write failed: {e}")

    # ─── Public API ─────────────────────────────────────────────────
    def log_bar(self, timeframe: str, bar, market: dict, regime: str):
        """Log a completed bar with full market snapshot."""
        self._write({
            "event":     "bar",
            "ts":        datetime.now().isoformat(),
            "bot":       self.bot_name,
            "timeframe": timeframe,
            # Bar OHLCV
            "open":      bar.open,
            "high":      bar.high,
            "low":       bar.low,
            "close":     bar.close,
            "volume":    bar.volume,
            "tick_count": bar.tick_count,
            # Market context at bar close
            "regime":    regime,
            "vwap":      market.get("vwap"),
            "ema9":      market.get("ema9"),
            "ema21":     market.get("ema21"),
            "atr_1m":    market.get("atr_1m"),
            "atr_5m":    market.get("atr_5m"),
            "cvd":       market.get("cvd"),
            "bar_delta": market.get("bar_delta"),
            "dom_imbalance": market.get("dom_imbalance"),
            "dom_bid_heavy": market.get("dom_bid_heavy"),
            "dom_ask_heavy": market.get("dom_ask_heavy"),
            "tf_bias":       market.get("tf_bias"),
            "tf_votes_bullish": market.get("tf_votes_bullish"),
            "tf_votes_bearish": market.get("tf_votes_bearish"),
            "bars_1m":   market.get("bars_1m"),
            "bars_5m":   market.get("bars_5m"),
        })

    def log_eval(self, eval_record: dict, market: dict):
        """Log a full strategy evaluation — signals, skips, blocks, and why."""
        self._write({
            "event":        "eval",
            "ts":           datetime.now().isoformat(),
            "bot":          self.bot_name,
            "regime":       eval_record.get("regime"),
            "risk_blocked": eval_record.get("risk_blocked"),
            "strategies":   eval_record.get("strategies", []),
            "best_signal":  eval_record.get("best_signal"),
            # Market snapshot at eval time
            "price":        market.get("price"),
            "vwap":         market.get("vwap"),
            "ema9":         market.get("ema9"),
            "cvd":          market.get("cvd"),
            "bar_delta":    market.get("bar_delta"),
            "atr_5m":       market.get("atr_5m"),
            "dom_imbalance":market.get("dom_imbalance"),
            "tf_votes_bullish": market.get("tf_votes_bullish"),
            "tf_votes_bearish": market.get("tf_votes_bearish"),
        })

    def log_entry(self, signal, price: float, contracts: int,
                  stop_price: float, target_price: float,
                  risk_dollars: float, tier: str, market: dict):
        """Log a trade entry with full context."""
        self._write({
            "event":        "entry",
            "ts":           datetime.now().isoformat(),
            "bot":          self.bot_name,
            "direction":    signal.direction,
            "strategy":     signal.strategy,
            "reason":       signal.reason,
            "confluences":  signal.confluences,
            "confidence":   signal.confidence,
            "entry_score":  signal.entry_score,
            "price":        price,
            "contracts":    contracts,
            "stop_price":   stop_price,
            "target_price": target_price,
            "risk_dollars": risk_dollars,
            "tier":         tier,
            "stop_ticks":   signal.stop_ticks,
            "target_rr":    signal.target_rr,
            # Full market at entry
            "market":       {k: v for k, v in market.items()
                             if k not in ("tf_bias",)},  # keep it compact
            "tf_bias":      market.get("tf_bias"),
        })

    def log_exit(self, trade: dict, market: dict):
        """Log a trade exit with P&L and full context."""
        self._write({
            "event":        "exit",
            "ts":           datetime.now().isoformat(),
            "bot":          self.bot_name,
            "direction":    trade.get("direction"),
            "strategy":     trade.get("strategy"),
            "entry_price":  trade.get("entry_price"),
            "exit_price":   trade.get("exit_price"),
            "contracts":    trade.get("contracts"),
            "pnl_dollars":  trade.get("pnl_dollars"),
            "pnl_ticks":    trade.get("pnl_ticks"),
            "exit_reason":  trade.get("exit_reason"),
            "duration_s":   trade.get("hold_time_s"),  # PositionManager uses hold_time_s
            "entry_reason": trade.get("entry_reason"),
            "confluences":  trade.get("confluences"),
            # Market at exit
            "exit_price_actual": market.get("price"),
            "vwap_at_exit":      market.get("vwap"),
            "cvd_at_exit":       market.get("cvd"),
            "atr_at_exit":       market.get("atr_5m"),
            # MAE/MFE from ExpectancyEngine (Phase A: persisted for ML training)
            "mae_ticks":       market.get("mae_ticks"),
            "mfe_ticks":       market.get("mfe_ticks"),
            "capture_ratio":   market.get("capture_ratio"),
            "went_red_first":  market.get("went_red_first"),
            "mae_time_s":      market.get("mae_time_s"),
            "mfe_time_s":      market.get("mfe_time_s"),
        })

    def log_near_miss(self, signal: dict, market: dict, reason: str):
        """Log a signal that was generated but not taken (for Phase B RAG training)."""
        self._write({
            "event":        "near_miss",
            "ts":           datetime.now().isoformat(),
            "bot":          self.bot_name,
            "direction":    signal.get("direction"),
            "strategy":     signal.get("strategy"),
            "confidence":   signal.get("confidence"),
            "entry_score":  signal.get("entry_score"),
            "reason":       signal.get("reason"),
            "skip_reason":  reason,
            # Market context at signal time
            "price":        market.get("price"),
            "vwap":         market.get("vwap"),
            "atr_5m":       market.get("atr_5m"),
            "cvd":          market.get("cvd"),
            "dom_imbalance":market.get("dom_imbalance"),
            "regime":       market.get("regime"),
            "tf_votes_bullish": market.get("tf_votes_bullish"),
            "tf_votes_bearish": market.get("tf_votes_bearish"),
        })

    def log_session_summary(self, risk_dict: dict, trade_count: int):
        """Log end-of-session summary for AI debrief consumption."""
        self._write({
            "event":       "session_summary",
            "ts":          datetime.now().isoformat(),
            "bot":         self.bot_name,
            "date":        str(datetime.now().date()),
            "trade_count": trade_count,
            "pnl_today":   risk_dict.get("daily_pnl", 0),
            "win_rate":    risk_dict.get("win_rate_today", 0),
            "consecutive_losses": risk_dict.get("consecutive_losses", 0),
            "recovery_mode": risk_dict.get("recovery_mode", False),
        })

    def close(self):
        if self._file:
            self._file.close()
            self._file = None
