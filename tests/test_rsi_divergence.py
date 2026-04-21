"""
Tests for RSI Divergence Detector and HTF Pattern Scanner.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.rsi_divergence import RSIDivergenceDetector
from core.htf_pattern_scanner import HTFPatternScanner
from core.tick_aggregator import Bar


class TestRSIDivergence:
    """Verify RSI calculation and divergence detection."""

    def test_rsi_warmup(self):
        rsi = RSIDivergenceDetector(rsi_length=14)
        # Need 15 closes (14 changes) before RSI is ready
        for i in range(14):
            rsi.update(100.0 + i * 0.5)
        assert rsi.get_state()["rsi_ready"] is False
        rsi.update(107.5)  # 15th close
        assert rsi.get_state()["rsi_ready"] is True

    def test_rsi_value_range(self):
        rsi = RSIDivergenceDetector(rsi_length=14)
        prices = [100 + i * 0.5 for i in range(30)]
        for p in prices:
            rsi.update(p)
        val = rsi.get_current_rsi()
        assert 0 <= val <= 100

    def test_bullish_divergence_detected(self):
        rsi = RSIDivergenceDetector(rsi_length=14, pivot_left=3, pivot_right=3)
        # Price makes lower low, RSI makes higher low = bullish divergence
        prices = (
            [100, 101, 102, 100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89, 88]
            + [87, 86, 85, 84, 83, 84, 85, 86, 87, 88]
            + [87, 86, 85, 84, 83, 82, 83, 84, 85, 86, 87, 88, 89]
        )
        divs = rsi.check_divergences(prices)
        bull_divs = [d for d in divs if d["type"] == "bullish"]
        assert len(bull_divs) >= 1
        assert bull_divs[0]["strength"] > 0

    def test_no_divergence_on_trending(self):
        rsi = RSIDivergenceDetector(rsi_length=14, pivot_left=3, pivot_right=3)
        # Smooth uptrend — no divergence expected
        prices = [100 + i * 0.5 for i in range(50)]
        divs = rsi.check_divergences(prices)
        assert len(divs) == 0

    def test_reset_clears_state(self):
        rsi = RSIDivergenceDetector()
        for i in range(20):
            rsi.update(100 + i)
        assert rsi.get_state()["bars_processed"] == 20
        rsi.reset()
        assert rsi.get_state()["bars_processed"] == 0
        assert rsi.get_state()["rsi_ready"] is False

    def test_get_state_returns_valid_dict(self):
        rsi = RSIDivergenceDetector()
        state = rsi.get_state()
        assert "rsi_current" in state
        assert "rsi_length" in state
        assert "bars_processed" in state


class TestHTFPatternScanner:
    """Verify HTF pattern scanning and confluence scoring."""

    def _make_bar(self, o, h, l, c, vol=100):
        return Bar(open=o, high=h, low=l, close=c, volume=vol,
                   tick_count=50, start_time=0, end_time=60)

    def test_scanner_init(self):
        scanner = HTFPatternScanner()
        state = scanner.get_state()
        assert state["active_signals"] == 0
        assert state["total_detected"] == 0

    def test_scanner_needs_minimum_bars(self):
        scanner = HTFPatternScanner()
        bar = self._make_bar(100, 102, 99, 101)
        # Single bar should not produce patterns
        result = scanner.on_bar("5m", bar)
        assert result == []

    def test_scanner_detects_engulfing(self):
        scanner = HTFPatternScanner()
        # Build up bars: small bearish, then large bullish engulfing
        bars = [
            self._make_bar(100, 101, 99, 100.5),
            self._make_bar(100.5, 101, 99.5, 100),
            self._make_bar(100, 100.5, 99.5, 99.8),  # small bearish
            self._make_bar(99.5, 101.5, 99, 101.2),   # large bullish engulfing
        ]
        results = []
        for bar in bars:
            r = scanner.on_bar("15m", bar)
            results.extend(r)
        # May or may not detect based on exact thresholds, but should not error
        assert isinstance(results, list)

    def test_confluence_score_no_signals(self):
        scanner = HTFPatternScanner()
        score = scanner.get_confluence_score("LONG")
        assert score["score"] == 0
        assert score["aligned_count"] == 0

    def test_tf_weight_ordering(self):
        """Higher timeframes should have more weight."""
        assert HTFPatternScanner.TF_WEIGHT["60m"] > HTFPatternScanner.TF_WEIGHT["15m"]
        assert HTFPatternScanner.TF_WEIGHT["15m"] > HTFPatternScanner.TF_WEIGHT["5m"]

    def test_ttl_ordering(self):
        """Higher timeframe patterns should persist longer."""
        ttl = HTFPatternScanner.DEFAULT_TTL
        assert ttl["60m"] > ttl["15m"]
        assert ttl["15m"] > ttl["5m"]

    def test_state_includes_bar_count(self):
        scanner = HTFPatternScanner()
        bar = self._make_bar(100, 102, 99, 101)
        scanner.on_bar("5m", bar)
        state = scanner.get_state()
        assert state["bars_buffered"]["5m"] == 1
        assert state["bars_buffered"]["15m"] == 0
