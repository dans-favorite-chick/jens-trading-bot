"""
Phoenix Bot — pandas-ta Pattern Detector (62 Candlestick Patterns)

Runs ALL 62 TA-Lib candlestick patterns via pandas-ta on a background
thread so it never blocks the live tick processing pipeline.

Upgrades our hand-coded ~15 patterns to the full institutional library:
  - Doji variants (dragonfly, gravestone, long-legged, etc.)
  - Engulfing patterns (bullish, bearish)
  - Hammer/Hanging Man/Shooting Star
  - Three White Soldiers / Three Black Crows
  - Morning/Evening Star
  - Harami patterns
  - Marubozu, Spinning Top, etc.
  - 40+ more patterns

Also computes supplementary indicators: MACD histogram, Bollinger Band
position, Stochastic crossover — all as confluence factors.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("PandasTA")

try:
    import pandas as pd
    try:
        import pandas_ta as ta  # legacy package
    except ImportError:
        import pandas_ta_classic as ta  # B52: maintained fork (preferred)
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False
    logger.warning("[PandasTA] pandas-ta not installed — pattern detection disabled")

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False


@dataclass
class PatternResult:
    """Result of pattern detection on a single bar."""
    patterns: dict = field(default_factory=dict)  # {pattern_name: signal_value}
    bullish_count: int = 0
    bearish_count: int = 0
    strongest_bullish: str = ""
    strongest_bearish: str = ""
    supplementary: dict = field(default_factory=dict)  # MACD, BB, Stoch, etc.
    timestamp: float = 0.0


class PandasTADetector:
    """
    62-pattern candlestick detector using pandas-ta.

    Runs detection on a background thread to avoid blocking the main
    event loop. Results are available via get_latest() which returns
    the most recent completed analysis.
    """

    def __init__(self, max_bars: int = 100):
        self._max_bars = max_bars
        self._bars: deque = deque(maxlen=max_bars)
        self._latest_result: PatternResult = PatternResult()
        self._lock = threading.Lock()
        self._bar_count = 0

        # Background thread state
        self._pending_update = False
        self._worker_thread: threading.Thread | None = None
        self._running = True

        # Start background worker
        if PANDAS_TA_AVAILABLE:
            self._worker_thread = threading.Thread(
                target=self._worker_loop, daemon=True, name="PandasTA-Worker"
            )
            self._worker_thread.start()
            logger.info("[PandasTA] Background detector started (62 patterns)")
        else:
            logger.warning("[PandasTA] Disabled — install pandas-ta-classic")

    def update(self, bar) -> None:
        """Add a completed bar. Detection runs async on background thread."""
        self._bars.append({
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": getattr(bar, "volume", 0),
        })
        self._bar_count += 1
        self._pending_update = True

    def get_latest(self) -> PatternResult:
        """Get the most recent pattern detection result (thread-safe)."""
        with self._lock:
            return self._latest_result

    def get_active_patterns(self) -> dict:
        """Get only non-zero pattern detections. For strategy consumption."""
        result = self.get_latest()
        return {k: v for k, v in result.patterns.items() if v != 0}

    def get_pattern_features(self) -> list:
        """Get normalized feature vector for XGBoost/ML consumption.

        Returns top pattern signals normalized to [-1, 1] range,
        plus supplementary indicator values.
        """
        result = self.get_latest()
        features = []

        # Top 10 strongest pattern signals (normalized from -100/+100 to -1/+1)
        sorted_patterns = sorted(
            result.patterns.items(),
            key=lambda x: abs(x[1]),
            reverse=True,
        )[:10]

        for i in range(10):
            if i < len(sorted_patterns):
                features.append(sorted_patterns[i][1] / 100.0)
            else:
                features.append(0.0)

        # Supplementary indicators
        supp = result.supplementary
        features.append(supp.get("macd_hist_norm", 0.0))
        features.append(supp.get("bb_position", 0.5))  # 0=lower band, 1=upper band
        features.append(supp.get("stoch_k", 50.0) / 100.0)
        features.append(supp.get("stoch_d", 50.0) / 100.0)
        features.append(1.0 if supp.get("stoch_cross_bullish") else
                         -1.0 if supp.get("stoch_cross_bearish") else 0.0)

        return features

    def get_confluence_score(self, direction: str) -> dict:
        """Get pattern confluence for a trade direction.

        Used by strategies as a confluence factor.
        """
        result = self.get_latest()
        if not result.patterns:
            return {"score": 0, "aligned": 0, "opposing": 0,
                    "strongest": None, "patterns": []}

        aligned = []
        opposing = []

        for name, val in result.patterns.items():
            if val == 0:
                continue
            if direction == "LONG":
                if val > 0:
                    aligned.append((name, val))
                else:
                    opposing.append((name, val))
            else:  # SHORT
                if val < 0:
                    aligned.append((name, abs(val)))
                else:
                    opposing.append((name, val))

        score = sum(v for _, v in aligned) - sum(v for _, v in opposing) * 0.5
        strongest = max(aligned, key=lambda x: x[1])[0] if aligned else None

        return {
            "score": round(score, 1),
            "aligned": len(aligned),
            "opposing": len(opposing),
            "strongest": strongest,
            "patterns": [{"name": n, "strength": v} for n, v in aligned[:5]],
        }

    # ─── Background Worker ─────────────────────────────────────────
    def _worker_loop(self):
        """Background thread that runs pattern detection."""
        while self._running:
            if self._pending_update and len(self._bars) >= 5:
                self._pending_update = False
                try:
                    result = self._detect_patterns()
                    with self._lock:
                        self._latest_result = result
                except Exception as e:
                    logger.debug(f"[PandasTA] Detection error: {e}")
            time.sleep(0.1)  # Check for updates 10x/sec

    def _detect_patterns(self) -> PatternResult:
        """Run all 62 candlestick patterns + supplementary indicators."""
        start = time.perf_counter()

        # Build DataFrame from bar deque
        bars = list(self._bars)
        df = pd.DataFrame(bars)

        result = PatternResult(timestamp=time.time())

        # ── 62 Candlestick Patterns ────────────────────────────────
        try:
            # pandas-ta cdl_pattern(name="all") runs all 62 patterns
            patterns_df = df.ta.cdl_pattern(name="all")
            if patterns_df is not None and not patterns_df.empty:
                # Get the last row (most recent bar)
                last_row = patterns_df.iloc[-1]
                for col in patterns_df.columns:
                    val = last_row[col]
                    if pd.notna(val) and val != 0:
                        # Clean column name: "CDL_DOJI_10_0.1" → "DOJI"
                        clean_name = col.replace("CDL_", "").split("_")[0]
                        result.patterns[clean_name] = int(val)

                        if val > 0:
                            result.bullish_count += 1
                            if not result.strongest_bullish or val > result.patterns.get(result.strongest_bullish, 0):
                                result.strongest_bullish = clean_name
                        elif val < 0:
                            result.bearish_count += 1
                            if not result.strongest_bearish or val < result.patterns.get(result.strongest_bearish, 0):
                                result.strongest_bearish = clean_name
        except Exception as e:
            logger.debug(f"[PandasTA] Candlestick pattern error: {e}")

        # ── Supplementary Indicators ───────────────────────────────
        try:
            # MACD
            macd = df.ta.macd(fast=12, slow=26, signal=9)
            if macd is not None and not macd.empty:
                hist_col = [c for c in macd.columns if "h" in c.lower()]
                if hist_col:
                    hist_val = macd[hist_col[0]].iloc[-1]
                    if pd.notna(hist_val):
                        # Normalize histogram by ATR for comparability
                        atr_val = df.ta.atr(length=14)
                        if atr_val is not None:
                            atr_last = atr_val.iloc[-1]
                            if pd.notna(atr_last) and atr_last > 0:
                                result.supplementary["macd_hist_norm"] = round(float(hist_val / atr_last), 4)
                            else:
                                result.supplementary["macd_hist_norm"] = 0.0

            # Bollinger Bands — position within bands (0 = lower, 1 = upper)
            bb = df.ta.bbands(length=20, std=2)
            if bb is not None and not bb.empty:
                upper_col = [c for c in bb.columns if "u" in c.lower()]
                lower_col = [c for c in bb.columns if "l" in c.lower()]
                if upper_col and lower_col:
                    upper = bb[upper_col[0]].iloc[-1]
                    lower = bb[lower_col[0]].iloc[-1]
                    price = df["close"].iloc[-1]
                    if pd.notna(upper) and pd.notna(lower) and upper > lower:
                        bb_pos = (price - lower) / (upper - lower)
                        result.supplementary["bb_position"] = round(float(max(0, min(1, bb_pos))), 3)

            # Stochastic
            stoch = df.ta.stoch(k=14, d=3)
            if stoch is not None and not stoch.empty:
                k_col = [c for c in stoch.columns if "k" in c.lower()]
                d_col = [c for c in stoch.columns if "d" in c.lower()]
                if k_col and d_col:
                    k_val = stoch[k_col[0]].iloc[-1]
                    d_val = stoch[d_col[0]].iloc[-1]
                    if pd.notna(k_val) and pd.notna(d_val):
                        result.supplementary["stoch_k"] = round(float(k_val), 1)
                        result.supplementary["stoch_d"] = round(float(d_val), 1)
                        # Cross detection
                        if len(stoch) >= 2:
                            prev_k = stoch[k_col[0]].iloc[-2]
                            prev_d = stoch[d_col[0]].iloc[-2]
                            if pd.notna(prev_k) and pd.notna(prev_d):
                                result.supplementary["stoch_cross_bullish"] = (
                                    prev_k < prev_d and k_val > d_val and k_val < 30
                                )
                                result.supplementary["stoch_cross_bearish"] = (
                                    prev_k > prev_d and k_val < d_val and k_val > 70
                                )

        except Exception as e:
            logger.debug(f"[PandasTA] Supplementary indicator error: {e}")

        elapsed = (time.perf_counter() - start) * 1000
        if result.bullish_count > 0 or result.bearish_count > 0:
            logger.info(f"[PandasTA] {result.bullish_count} bullish, {result.bearish_count} bearish "
                        f"patterns detected ({elapsed:.0f}ms) "
                        f"— strongest: bull={result.strongest_bullish or 'none'} "
                        f"bear={result.strongest_bearish or 'none'}")

        return result

    def to_dict(self) -> dict:
        """For dashboard state push."""
        result = self.get_latest()
        active = {k: v for k, v in result.patterns.items() if v != 0}
        return {
            "available": PANDAS_TA_AVAILABLE,
            "bar_count": self._bar_count,
            "active_patterns": active,
            "bullish_count": result.bullish_count,
            "bearish_count": result.bearish_count,
            "strongest_bullish": result.strongest_bullish,
            "strongest_bearish": result.strongest_bearish,
            "supplementary": result.supplementary,
            "last_update": result.timestamp,
        }

    def stop(self):
        """Shutdown background thread."""
        self._running = False
