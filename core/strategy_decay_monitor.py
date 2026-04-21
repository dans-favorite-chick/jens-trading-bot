"""
Phoenix Bot — Strategy Decay Monitor

Tracks rolling performance per strategy. Alerts when a strategy's edge is
eroding. Optionally auto-demotes to OBSERVE_ONLY after sustained degradation.

Research basis (2026):
- Alpha decays ~12 months on average in most systematic strategies
- Causes: crowding, regime change, microstructure shifts
- Detection: monitor rolling 30-day Sharpe, WR, R:R vs baseline
- Also: backtest-vs-live P&L drift (real-world drift signal)

SHADOW MODE for first 2 weeks of live operation:
- Monitor and log, do NOT auto-demote
- Collect baseline stats to know what "normal" looks like
- After 2 weeks, user approves activating auto-demote

Thresholds from memory/procedural/targets.yaml.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("DecayMonitor")

PHOENIX_ROOT = Path(__file__).parent.parent
TARGETS_YAML = PHOENIX_ROOT / "memory" / "procedural" / "targets.yaml"
DECAY_STATE_FILE = PHOENIX_ROOT / "memory" / "episodic" / "decay_state.json"

# Hard-coded defaults — YAML load overrides
DEFAULT_WARNING = {"sharpe": 0.5, "win_rate": 0.50, "profit_factor": 1.5}
DEFAULT_CRITICAL = {"sharpe": 0.0, "win_rate": 0.40, "profit_factor": 1.0}
DEFAULT_ROLLING_DAYS = 30
DEFAULT_MIN_TRADES_FOR_JUDGEMENT = 30
DEFAULT_DRIFT_THRESHOLD_PCT = 0.20  # 20% live-vs-backtest divergence


@dataclass
class StrategyPerformance:
    """Rolling perf stats per strategy."""
    strategy_name: str
    trades: deque[dict] = field(default_factory=lambda: deque(maxlen=500))  # 500 trades history
    baseline_backtest_sharpe: float = 0.0  # set when strategy was validated

    def add_trade(self, trade: dict) -> None:
        """Record a trade. trade dict: {ts, pnl_usd, outcome: WIN|LOSS, strategy}"""
        self.trades.append(trade)

    def rolling_trades(self, days: int = DEFAULT_ROLLING_DAYS) -> list[dict]:
        """Return trades from the last N days."""
        cutoff = datetime.now() - timedelta(days=days)
        result = []
        for t in self.trades:
            ts = t.get("ts")
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
            if ts and ts >= cutoff:
                result.append(t)
        return result

    def metrics(self, days: int = DEFAULT_ROLLING_DAYS) -> dict:
        recent = self.rolling_trades(days)
        if len(recent) < 5:
            return {"insufficient_data": True, "trade_count": len(recent)}
        pnls = [t["pnl_usd"] for t in recent]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        mean_pnl = statistics.mean(pnls)
        sd_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
        sharpe = mean_pnl / sd_pnl if sd_pnl > 0 else 0.0
        wr = len(wins) / len(pnls)
        gross_profit = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 0
        pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)
        avg_win = statistics.mean(wins) if wins else 0
        avg_loss = statistics.mean(losses) if losses else 0
        return {
            "trade_count": len(pnls),
            "win_rate": round(wr, 4),
            "sharpe": round(sharpe, 3),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "net_pnl_usd": round(sum(pnls), 2),
        }


class DecayMonitor:
    """Track all strategies. Query for alerts."""

    def __init__(self, shadow_mode: bool = True):
        self.strategies: dict[str, StrategyPerformance] = defaultdict(
            lambda: StrategyPerformance(strategy_name="")
        )
        self.shadow_mode = shadow_mode
        self._load_config()
        self._load_state()

    def _load_config(self) -> None:
        """Load thresholds from targets.yaml if available."""
        try:
            import yaml
            if TARGETS_YAML.exists():
                with open(TARGETS_YAML) as f:
                    cfg = yaml.safe_load(f) or {}
                self.warning_thresholds = cfg.get("warning_thresholds", DEFAULT_WARNING)
                self.critical_thresholds = cfg.get("critical_thresholds", DEFAULT_CRITICAL)
                return
        except Exception as e:
            logger.debug(f"targets.yaml load failed ({e}), using defaults")
        self.warning_thresholds = DEFAULT_WARNING
        self.critical_thresholds = DEFAULT_CRITICAL

    def _load_state(self) -> None:
        """Restore decay state from disk if present."""
        if not DECAY_STATE_FILE.exists():
            return
        try:
            with open(DECAY_STATE_FILE) as f:
                data = json.load(f)
            for strat_name, info in data.get("strategies", {}).items():
                perf = StrategyPerformance(strategy_name=strat_name)
                for t in info.get("trades", []):
                    perf.trades.append(t)
                perf.baseline_backtest_sharpe = info.get("baseline_backtest_sharpe", 0.0)
                self.strategies[strat_name] = perf
        except Exception as e:
            logger.warning(f"decay_state load failed: {e}")

    def save_state(self) -> None:
        """Persist to disk (called periodically + at shutdown)."""
        DECAY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "last_save": datetime.now().isoformat(),
                "strategies": {
                    name: {
                        "trades": list(perf.trades),
                        "baseline_backtest_sharpe": perf.baseline_backtest_sharpe,
                    }
                    for name, perf in self.strategies.items()
                },
            }
            tmp = DECAY_STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, default=str)
            import os
            os.replace(tmp, DECAY_STATE_FILE)
        except Exception as e:
            logger.warning(f"decay_state save failed: {e}")

    def record_trade(self, strategy_name: str, pnl_usd: float,
                     outcome: str, ts: datetime = None) -> None:
        if ts is None:
            ts = datetime.now()
        perf = self.strategies.get(strategy_name)
        if perf is None or perf.strategy_name == "":
            perf = StrategyPerformance(strategy_name=strategy_name)
            self.strategies[strategy_name] = perf
        perf.add_trade({
            "ts": ts.isoformat(),
            "pnl_usd": pnl_usd,
            "outcome": outcome,
        })

    def check_strategy(self, strategy_name: str) -> dict:
        """
        Evaluate a single strategy. Returns:
          {"status": "HEALTHY" | "INSUFFICIENT_DATA" | "WARNING" | "CRITICAL",
           "reasons": [...], "metrics": {...}, "recommend_demote": bool}
        """
        perf = self.strategies.get(strategy_name)
        if perf is None or not perf.trades:
            return {"status": "INSUFFICIENT_DATA", "reasons": ["No trades recorded"],
                    "metrics": {}, "recommend_demote": False}

        m = perf.metrics()
        if m.get("insufficient_data"):
            return {"status": "INSUFFICIENT_DATA", "reasons": [f"Only {m['trade_count']} trades in window"],
                    "metrics": m, "recommend_demote": False}

        if m["trade_count"] < DEFAULT_MIN_TRADES_FOR_JUDGEMENT:
            return {"status": "INSUFFICIENT_DATA",
                    "reasons": [f"Need {DEFAULT_MIN_TRADES_FOR_JUDGEMENT} trades for judgement, have {m['trade_count']}"],
                    "metrics": m, "recommend_demote": False}

        reasons = []
        status = "HEALTHY"

        # Critical checks first
        if m["sharpe"] <= self.critical_thresholds["sharpe"]:
            reasons.append(f"Sharpe {m['sharpe']:.2f} ≤ critical {self.critical_thresholds['sharpe']}")
            status = "CRITICAL"
        if m["win_rate"] <= self.critical_thresholds["win_rate"]:
            reasons.append(f"WR {m['win_rate']:.1%} ≤ critical {self.critical_thresholds['win_rate']:.1%}")
            status = "CRITICAL"
        pf = m.get("profit_factor")
        if pf is not None and pf <= self.critical_thresholds["profit_factor"]:
            reasons.append(f"PF {pf:.2f} ≤ critical {self.critical_thresholds['profit_factor']}")
            status = "CRITICAL"

        # Warning checks (only if not already critical)
        if status == "HEALTHY":
            if m["sharpe"] < self.warning_thresholds["sharpe"]:
                reasons.append(f"Sharpe {m['sharpe']:.2f} < warning {self.warning_thresholds['sharpe']}")
                status = "WARNING"
            if m["win_rate"] < self.warning_thresholds["win_rate"]:
                reasons.append(f"WR {m['win_rate']:.1%} < warning {self.warning_thresholds['win_rate']:.1%}")
                status = "WARNING"
            if pf is not None and pf < self.warning_thresholds["profit_factor"]:
                reasons.append(f"PF {pf:.2f} < warning {self.warning_thresholds['profit_factor']}")
                status = "WARNING"

        recommend_demote = (status == "CRITICAL" and not self.shadow_mode)

        return {
            "status": status,
            "reasons": reasons,
            "metrics": m,
            "recommend_demote": recommend_demote,
            "shadow_mode": self.shadow_mode,
        }

    def summary(self) -> dict:
        """Summary across all tracked strategies."""
        return {
            "shadow_mode": self.shadow_mode,
            "strategies_tracked": list(self.strategies.keys()),
            "reports": {name: self.check_strategy(name) for name in self.strategies.keys()},
        }
