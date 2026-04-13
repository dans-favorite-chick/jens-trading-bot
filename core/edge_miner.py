"""
Phoenix Bot — Personal Edge Mining

Analyzes YOUR trade history to discover your specific edge:
- What regime/time/strategy combos produce consistent wins
- What conditions correlate with losses
- Pattern clusters that predict outcome

"Your win rate drops 40% on FOMC weeks"
"You win 80% on SWEEP_LOW + DOM bid-heavy in first hour"
"Spring setups in RANGING regime: 72% win rate vs 45% in TRENDING"

This is knowledge no book can give — it's YOUR data.
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger("EdgeMiner")


@dataclass
class EdgePattern:
    """A discovered edge or anti-edge in the user's history."""
    name: str
    description: str
    win_rate: float
    sample_size: int
    is_edge: bool  # True = positive edge, False = anti-edge (avoid)
    conditions: dict  # What makes this pattern
    confidence: str  # "high" (30+ trades), "medium" (15+), "low" (5+)


class EdgeMiner:
    """Mines personal trade history for edge patterns."""

    def __init__(self, logs_dir: str = None):
        self._logs_dir = logs_dir or os.path.join(
            os.path.dirname(__file__), "..", "logs"
        )
        self._trades: list[dict] = []
        self._patterns: list[EdgePattern] = []
        self._last_analysis: float = 0
        self._min_sample = 5  # Minimum trades to form a pattern

    def load_trades(self, bot_name: str = None):
        """Load trade history from JSONL log files."""
        self._trades = []
        try:
            for fname in os.listdir(self._logs_dir):
                if not fname.endswith(".jsonl"):
                    continue
                if bot_name and bot_name not in fname:
                    continue
                fpath = os.path.join(self._logs_dir, fname)
                with open(fpath, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            if record.get("type") == "trade":
                                self._trades.append(record)
                        except json.JSONDecodeError:
                            continue
            logger.info(f"[EDGE MINER] Loaded {len(self._trades)} trades from logs")
        except Exception as e:
            logger.warning(f"[EDGE MINER] Load failed: {e}")

    def analyze(self) -> list[EdgePattern]:
        """Run full edge analysis on loaded trades."""
        if len(self._trades) < self._min_sample:
            logger.info(f"[EDGE MINER] Only {len(self._trades)} trades — need {self._min_sample} minimum")
            return []

        self._patterns = []

        # 1. Strategy x Regime analysis
        self._analyze_strategy_regime()

        # 2. Time-of-day analysis
        self._analyze_time_of_day()

        # 3. Direction bias analysis
        self._analyze_direction()

        # 4. Confluence factor analysis
        self._analyze_confluences()

        # 5. Streak analysis
        self._analyze_streaks()

        # Sort by confidence and edge strength
        self._patterns.sort(key=lambda p: (p.sample_size, abs(p.win_rate - 50)), reverse=True)

        self._last_analysis = datetime.now().timestamp()
        logger.info(f"[EDGE MINER] Found {len(self._patterns)} patterns "
                   f"({sum(1 for p in self._patterns if p.is_edge)} edges, "
                   f"{sum(1 for p in self._patterns if not p.is_edge)} anti-edges)")

        return self._patterns

    def _get_confidence(self, n: int) -> str:
        if n >= 30:
            return "high"
        elif n >= 15:
            return "medium"
        return "low"

    def _analyze_strategy_regime(self):
        """Find strategy x regime combos that outperform or underperform."""
        buckets = defaultdict(list)
        for t in self._trades:
            key = (t.get("strategy", "?"), t.get("regime", "?"))
            buckets[key].append(t)

        for (strat, regime), trades in buckets.items():
            if len(trades) < self._min_sample:
                continue
            wins = sum(1 for t in trades if t.get("result") == "WIN")
            wr = (wins / len(trades)) * 100
            is_edge = wr >= 55

            self._patterns.append(EdgePattern(
                name=f"{strat}_{regime}",
                description=f"{strat} in {regime}: {wr:.0f}% win rate ({len(trades)} trades)",
                win_rate=wr,
                sample_size=len(trades),
                is_edge=is_edge,
                conditions={"strategy": strat, "regime": regime},
                confidence=self._get_confidence(len(trades)),
            ))

    def _analyze_time_of_day(self):
        """Find time-of-day patterns."""
        buckets = defaultdict(list)
        for t in self._trades:
            ts = t.get("entry_time", t.get("timestamp", ""))
            try:
                hour = datetime.fromisoformat(ts).hour if ts else None
            except (ValueError, TypeError):
                hour = None
            if hour is not None:
                period = "pre_open" if hour < 8 else "open" if hour < 10 else "midday" if hour < 13 else "afternoon"
                buckets[period].append(t)

        for period, trades in buckets.items():
            if len(trades) < self._min_sample:
                continue
            wins = sum(1 for t in trades if t.get("result") == "WIN")
            wr = (wins / len(trades)) * 100

            self._patterns.append(EdgePattern(
                name=f"time_{period}",
                description=f"{period} session: {wr:.0f}% win rate ({len(trades)} trades)",
                win_rate=wr,
                sample_size=len(trades),
                is_edge=wr >= 55,
                conditions={"time_period": period},
                confidence=self._get_confidence(len(trades)),
            ))

    def _analyze_direction(self):
        """Find directional bias."""
        for direction in ("LONG", "SHORT"):
            trades = [t for t in self._trades if t.get("direction") == direction]
            if len(trades) < self._min_sample:
                continue
            wins = sum(1 for t in trades if t.get("result") == "WIN")
            wr = (wins / len(trades)) * 100

            self._patterns.append(EdgePattern(
                name=f"direction_{direction.lower()}",
                description=f"{direction} trades: {wr:.0f}% win rate ({len(trades)} trades)",
                win_rate=wr,
                sample_size=len(trades),
                is_edge=wr >= 55,
                conditions={"direction": direction},
                confidence=self._get_confidence(len(trades)),
            ))

    def _analyze_confluences(self):
        """Find which confluence factors correlate with wins."""
        factor_wins = defaultdict(int)
        factor_total = defaultdict(int)

        for t in self._trades:
            confluences = t.get("confluences", [])
            is_win = t.get("result") == "WIN"
            for conf in confluences:
                # Extract key factor (first word or phrase before colon/number)
                key = conf.split(":")[0].split("(")[0].strip()
                if len(key) > 3:
                    factor_total[key] += 1
                    if is_win:
                        factor_wins[key] += 1

        for factor, total in factor_total.items():
            if total < self._min_sample:
                continue
            wins = factor_wins[factor]
            wr = (wins / total) * 100

            self._patterns.append(EdgePattern(
                name=f"confluence_{factor[:20]}",
                description=f"With '{factor}': {wr:.0f}% win rate ({total} trades)",
                win_rate=wr,
                sample_size=total,
                is_edge=wr >= 55,
                conditions={"confluence_factor": factor},
                confidence=self._get_confidence(total),
            ))

    def _analyze_streaks(self):
        """Analyze performance after win/loss streaks."""
        if len(self._trades) < 10:
            return

        for streak_type in ("WIN", "LOSS"):
            for streak_len in (2, 3):
                after_streak = []
                for i in range(streak_len, len(self._trades)):
                    prev = self._trades[i-streak_len:i]
                    if all(t.get("result") == streak_type for t in prev):
                        after_streak.append(self._trades[i])

                if len(after_streak) < self._min_sample:
                    continue
                wins = sum(1 for t in after_streak if t.get("result") == "WIN")
                wr = (wins / len(after_streak)) * 100

                self._patterns.append(EdgePattern(
                    name=f"after_{streak_len}_{streak_type.lower()}s",
                    description=f"After {streak_len} {streak_type.lower()}s: {wr:.0f}% win rate ({len(after_streak)} trades)",
                    win_rate=wr,
                    sample_size=len(after_streak),
                    is_edge=wr >= 55,
                    conditions={"after_streak": streak_type, "streak_length": streak_len},
                    confidence=self._get_confidence(len(after_streak)),
                ))

    def get_edge_for_setup(self, strategy: str, regime: str, direction: str) -> dict:
        """Query: given this setup, what does my history say?"""
        relevant = [p for p in self._patterns
                    if p.conditions.get("strategy") == strategy
                    or p.conditions.get("regime") == regime
                    or p.conditions.get("direction") == direction]

        if not relevant:
            return {"has_data": False, "message": "Not enough history yet"}

        best_edge = max(relevant, key=lambda p: p.win_rate) if relevant else None
        worst_edge = min(relevant, key=lambda p: p.win_rate) if relevant else None

        return {
            "has_data": True,
            "relevant_patterns": len(relevant),
            "best_edge": {
                "name": best_edge.name,
                "win_rate": best_edge.win_rate,
                "description": best_edge.description,
            } if best_edge else None,
            "worst_edge": {
                "name": worst_edge.name,
                "win_rate": worst_edge.win_rate,
                "description": worst_edge.description,
            } if worst_edge else None,
            "recommendation": best_edge.description if best_edge and best_edge.is_edge else "No clear edge found",
        }

    def to_dict(self) -> dict:
        edges = [p for p in self._patterns if p.is_edge]
        anti_edges = [p for p in self._patterns if not p.is_edge]
        return {
            "total_trades": len(self._trades),
            "patterns_found": len(self._patterns),
            "edges": [{"name": p.name, "win_rate": p.win_rate,
                       "sample_size": p.sample_size, "confidence": p.confidence,
                       "description": p.description} for p in edges[:10]],
            "anti_edges": [{"name": p.name, "win_rate": p.win_rate,
                           "sample_size": p.sample_size, "confidence": p.confidence,
                           "description": p.description} for p in anti_edges[:5]],
            "last_analysis": self._last_analysis,
        }
