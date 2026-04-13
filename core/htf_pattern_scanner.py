"""
Phoenix Bot -- Higher Timeframe Pattern Scanner

Runs the existing 23-pattern CandlestickAnalyzer against completed bars from
higher timeframes (5m, 15m, 60m) to detect reversal and continuation patterns
that provide strong confluence signals for lower-timeframe strategy entries.

Key insight from LuxAlgo's HTF Reversal Divergences indicator: a 15m bullish
engulfing appearing during a 1m strategy evaluation is a powerful confluence
boost. We already have the pattern engine -- this module applies it to HTF bars.

Usage:
    scanner = HTFPatternScanner()
    scanner.on_bar("5m", bar)    # Call from tick_aggregator bar callback
    scanner.on_bar("15m", bar)
    signals = scanner.get_active_signals("LONG")  # Get confluence for a direction
"""

import logging
import time
from collections import deque

from core.candlestick_patterns import CandlestickAnalyzer, get_pattern_confluence

logger = logging.getLogger("HTFPatterns")


class HTFPatternScanner:
    """
    Scans higher-timeframe completed bars for candlestick patterns.

    Maintains a rolling window of bars per timeframe and re-analyzes
    on each new bar completion. Detected patterns decay over time
    (configurable TTL) so stale signals don't influence decisions.
    """

    # How long a detected pattern stays "active" (seconds)
    # 5m patterns: 10 min, 15m: 30 min, 60m: 2 hours
    DEFAULT_TTL = {
        "1m": 180,      # 3 min
        "5m": 600,      # 10 min
        "15m": 1800,    # 30 min
        "60m": 7200,    # 2 hours
    }

    # Confluence weight per timeframe — higher TFs carry more weight
    TF_WEIGHT = {
        "1m": 0.5,
        "5m": 1.0,
        "15m": 1.5,
        "60m": 2.0,
    }

    def __init__(self, tick_size: float = 0.25, ttl_override: dict = None):
        self.tick_size = tick_size
        self.analyzer = CandlestickAnalyzer()

        # Rolling bar windows per timeframe (need ~20 bars for chart patterns)
        self._bars: dict[str, deque] = {
            "5m": deque(maxlen=30),
            "15m": deque(maxlen=30),
            "60m": deque(maxlen=30),
        }

        # Active detected patterns: list of {pattern_dict, timeframe, detected_at}
        self._active_signals: list[dict] = []

        # TTL config
        self._ttl = dict(self.DEFAULT_TTL)
        if ttl_override:
            self._ttl.update(ttl_override)

        # Stats
        self._total_patterns_detected = 0
        self._patterns_by_tf: dict[str, int] = {"5m": 0, "15m": 0, "60m": 0}

    def on_bar(self, timeframe: str, bar) -> list[dict]:
        """
        Called when a bar completes on a given timeframe.

        Args:
            timeframe: "5m", "15m", or "60m"
            bar: Completed Bar object from TickAggregator

        Returns:
            List of newly detected patterns (may be empty)
        """
        if timeframe not in self._bars:
            return []

        self._bars[timeframe].append(bar)
        bars = list(self._bars[timeframe])

        # Need at least 3 bars for multi-bar patterns
        if len(bars) < 3:
            return []

        # Run the full 23-pattern analyzer on this timeframe's bars
        patterns = self.analyzer.analyze(bars, self.tick_size)

        if not patterns:
            return []

        # Wrap patterns with metadata and add to active signals
        now = time.time()
        new_signals = []
        for p in patterns:
            signal = {
                "pattern": p,
                "timeframe": timeframe,
                "detected_at": now,
                "ttl": self._ttl.get(timeframe, 600),
                "weight": self.TF_WEIGHT.get(timeframe, 1.0),
                "bar_close": bar.close,
            }
            self._active_signals.append(signal)
            new_signals.append(signal)
            self._total_patterns_detected += 1
            self._patterns_by_tf[timeframe] = self._patterns_by_tf.get(timeframe, 0) + 1

        if new_signals:
            names = [s["pattern"]["pattern"] for s in new_signals]
            logger.info(f"[HTF {timeframe}] Detected: {', '.join(names)} "
                        f"(close={bar.close:.2f})")

        # Prune expired signals
        self._prune_expired()

        return new_signals

    def get_active_signals(self, direction: str = None) -> list[dict]:
        """
        Get all currently active HTF pattern signals.

        Args:
            direction: Optional "LONG" or "SHORT" to filter aligned patterns.
                      If None, returns all active patterns.

        Returns:
            List of active signal dicts, sorted by weight (strongest first)
        """
        self._prune_expired()

        if direction is None:
            return sorted(self._active_signals,
                         key=lambda s: s["weight"] * s["pattern"].get("strength", 50),
                         reverse=True)

        # Filter to patterns aligned with direction
        aligned = []
        for sig in self._active_signals:
            p = sig["pattern"]
            p_direction = p.get("direction", "NEUTRAL")

            if direction == "LONG" and p_direction in ("BULLISH", "LONG"):
                aligned.append(sig)
            elif direction == "SHORT" and p_direction in ("BEARISH", "SHORT"):
                aligned.append(sig)

        return sorted(aligned,
                     key=lambda s: s["weight"] * s["pattern"].get("strength", 50),
                     reverse=True)

    def get_confluence_score(self, direction: str) -> dict:
        """
        Get a composite confluence score for a given trade direction
        across all active HTF patterns.

        Args:
            direction: "LONG" or "SHORT"

        Returns:
            {
                "score": float (0-100),
                "aligned_count": int,
                "opposing_count": int,
                "strongest": str or None (name of strongest aligned pattern),
                "strongest_tf": str or None,
                "details": list of pattern summaries,
            }
        """
        self._prune_expired()

        aligned = []
        opposing = []

        for sig in self._active_signals:
            p = sig["pattern"]
            p_direction = p.get("direction", "NEUTRAL")
            weight = sig["weight"]
            strength = p.get("strength", 50)

            if direction == "LONG":
                if p_direction in ("BULLISH", "LONG"):
                    aligned.append((sig, weight * strength))
                elif p_direction in ("BEARISH", "SHORT"):
                    opposing.append((sig, weight * strength))
            elif direction == "SHORT":
                if p_direction in ("BEARISH", "SHORT"):
                    aligned.append((sig, weight * strength))
                elif p_direction in ("BULLISH", "LONG"):
                    opposing.append((sig, weight * strength))

        # Composite score
        aligned_score = sum(s for _, s in aligned)
        opposing_score = sum(s for _, s in opposing)

        # Net score: aligned minus opposing, normalized to 0-100
        if aligned_score + opposing_score > 0:
            net = (aligned_score - opposing_score * 0.5)  # Opposing has half weight
            score = max(0, min(100, net / 2))  # Normalize
        else:
            score = 0

        # Find strongest aligned
        strongest_name = None
        strongest_tf = None
        if aligned:
            best = max(aligned, key=lambda x: x[1])
            strongest_name = best[0]["pattern"]["pattern"]
            strongest_tf = best[0]["timeframe"]

        details = []
        for sig, weighted_score in aligned:
            details.append({
                "name": sig["pattern"]["pattern"],
                "timeframe": sig["timeframe"],
                "direction": sig["pattern"].get("direction", "?"),
                "strength": sig["pattern"].get("strength", 0),
                "weighted": round(weighted_score, 1),
                "age_s": round(time.time() - sig["detected_at"], 0),
            })

        return {
            "score": round(score, 1),
            "aligned_count": len(aligned),
            "opposing_count": len(opposing),
            "strongest": strongest_name,
            "strongest_tf": strongest_tf,
            "details": details,
        }

    def get_state(self) -> dict:
        """Return scanner state for dashboard."""
        self._prune_expired()
        return {
            "active_signals": len(self._active_signals),
            "total_detected": self._total_patterns_detected,
            "by_timeframe": dict(self._patterns_by_tf),
            "bars_buffered": {tf: len(bars) for tf, bars in self._bars.items()},
            "active_patterns": [
                {
                    "name": s["pattern"]["pattern"],
                    "direction": s["pattern"].get("direction", "?"),
                    "strength": s["pattern"].get("strength", 0),
                    "timeframe": s["timeframe"],
                    "age_s": round(time.time() - s["detected_at"], 0),
                }
                for s in self._active_signals
            ],
        }

    def _prune_expired(self):
        """Remove signals that have exceeded their TTL."""
        now = time.time()
        before = len(self._active_signals)
        self._active_signals = [
            s for s in self._active_signals
            if (now - s["detected_at"]) < s["ttl"]
        ]
        pruned = before - len(self._active_signals)
        if pruned > 0:
            logger.debug(f"Pruned {pruned} expired HTF patterns")
