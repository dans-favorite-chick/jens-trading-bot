"""
Standalone test for the Candlestick Pattern Recognition Engine.

Run:  python -m tests.test_candlestick_patterns   (from phoenix_bot/)
  or: python tests/test_candlestick_patterns.py
"""

import sys
import os

# Ensure phoenix_bot is on the path when run directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import dataclass
from core.candlestick_patterns import CandlestickAnalyzer, get_pattern_confluence

TICK = 0.25  # MNQ tick size


@dataclass
class FakeBar:
    """Minimal Bar stand-in for testing."""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 100
    tick_count: int = 10
    start_time: float = 0.0
    end_time: float = 0.0


def make_bar(o, h, l, c):
    return FakeBar(open=o, high=h, low=l, close=c)


def print_patterns(label, patterns):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if not patterns:
        print("  (none detected)")
    for p in patterns:
        print(f"  [{p['strength']:3d}] {p['pattern']:25s} dir={p['direction']:7s} | {p['description']}")


def test_hammer():
    """Hammer after downtrend: small body top, long lower wick."""
    bars = [
        make_bar(100, 100, 98, 98),   # bearish
        make_bar(98, 98, 96, 96),     # bearish
        make_bar(96, 96, 94, 94),     # bearish
        make_bar(94, 94, 92, 92),     # bearish
        make_bar(92, 92, 90, 90),     # bearish
        make_bar(90, 90, 88, 88),     # bearish (trend established)
        make_bar(87.50, 88.50, 85, 88.25),  # hammer: body=0.75, lower wick=2.5, upper wick=0.25
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Hammer after downtrend", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Hammer" in names or "Dragonfly Doji" in names, "Expected Hammer or Dragonfly Doji"


def test_shooting_star():
    """Shooting star after uptrend: small body bottom, long upper wick."""
    bars = [
        make_bar(90, 92, 90, 92),
        make_bar(92, 94, 92, 94),
        make_bar(94, 96, 94, 96),
        make_bar(96, 98, 96, 98),
        make_bar(98, 100, 98, 100),
        make_bar(100, 102, 100, 102),
        make_bar(102, 106, 102, 102.50),  # shooting star: body ~0.5, upper wick=3.5
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Shooting Star after uptrend", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Shooting Star" in names, "Expected Shooting Star"


def test_doji():
    """Doji: open == close."""
    bars = [
        make_bar(100, 101, 99, 100),  # doji with wicks
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Doji", patterns)
    names = [p["pattern"] for p in patterns]
    assert any("Doji" in n for n in names), "Expected a Doji variant"


def test_marubozu():
    """Marubozu: full body, no wicks."""
    bars = [
        make_bar(100, 103, 100, 103),  # bullish marubozu
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Bullish Marubozu", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Bullish Marubozu" in names, "Expected Bullish Marubozu"


def test_bullish_engulfing():
    """Bullish engulfing: bearish bar then larger bullish bar."""
    bars = [
        make_bar(100, 100, 98, 98),   # padding
        make_bar(100, 100.25, 98, 98.50),   # prior: bearish
        make_bar(97, 101, 97, 101),          # current: bullish engulfs
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Bullish Engulfing", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Bullish Engulfing" in names, "Expected Bullish Engulfing"


def test_bearish_engulfing():
    """Bearish engulfing: bullish bar then larger bearish bar."""
    bars = [
        make_bar(98, 100, 98, 100),
        make_bar(98, 100.25, 98, 99.50),    # prior: bullish
        make_bar(101, 101, 97, 97),          # current: bearish engulfs
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Bearish Engulfing", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Bearish Engulfing" in names, "Expected Bearish Engulfing"


def test_morning_star():
    """Morning star: bearish, small star, bullish closing above bar1 mid."""
    bars = [
        make_bar(100, 100, 96, 96),    # bar1: bearish (mid = 98)
        make_bar(96, 96.50, 95.50, 96.25),  # bar2: small body (star)
        make_bar(96, 100, 96, 99),     # bar3: bullish, closes > 98
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Morning Star", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Morning Star" in names, "Expected Morning Star"


def test_evening_star():
    """Evening star: bullish, small star, bearish closing below bar1 mid."""
    bars = [
        make_bar(96, 100, 96, 100),    # bar1: bullish (mid = 98)
        make_bar(100, 100.50, 99.50, 100.25),  # bar2: small star
        make_bar(100, 100, 96, 97),    # bar3: bearish, closes < 98
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Evening Star", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Evening Star" in names, "Expected Evening Star"


def test_three_white_soldiers():
    """Three consecutive bullish bars, each opening within prior body."""
    bars = [
        make_bar(96, 98, 96, 98),      # bullish
        make_bar(97, 100, 97, 100),    # bullish, opens within prior body, closes higher
        make_bar(99, 102, 99, 102),    # bullish, opens within prior body, closes higher
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Three White Soldiers", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Three White Soldiers" in names, "Expected Three White Soldiers"


def test_three_black_crows():
    """Three consecutive bearish bars, each opening within prior body."""
    bars = [
        make_bar(102, 102, 100, 100),  # bearish
        make_bar(101, 101, 98, 98),    # bearish, opens within prior body
        make_bar(99, 99, 96, 96),      # bearish, opens within prior body
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Three Black Crows", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Three Black Crows" in names, "Expected Three Black Crows"


def test_tweezer_bottom():
    """Tweezer bottom: matching lows, bearish then bullish."""
    bars = [
        make_bar(100, 100, 96, 97),    # bearish, low=96
        make_bar(97, 100, 96, 99),     # bullish, low=96 (match)
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Tweezer Bottom", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Tweezer Bottom" in names, "Expected Tweezer Bottom"


def test_piercing_line():
    """Piercing line: bearish bar, then bullish opens below prior low, closes above mid."""
    bars = [
        make_bar(100, 100, 96, 96),    # bearish, mid=98
        make_bar(95, 99.50, 95, 99),   # opens below 96, closes above 98
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Piercing Line", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Piercing Line" in names, "Expected Piercing Line"


def test_confluence_helper():
    """Test get_pattern_confluence scoring."""
    patterns = [
        {"pattern": "Bullish Engulfing", "direction": "LONG", "strength": 75,
         "bar_index": -2, "description": "test"},
        {"pattern": "Hammer", "direction": "LONG", "strength": 65,
         "bar_index": -1, "description": "test"},
        {"pattern": "Bearish Harami", "direction": "SHORT", "strength": 55,
         "bar_index": -2, "description": "test"},
    ]

    conf = get_pattern_confluence(patterns, "LONG")
    print(f"\n{'='*60}")
    print(f"  Confluence for LONG")
    print(f"{'='*60}")
    print(f"  Aligned:  {len(conf['aligned_patterns'])} patterns, score {sum(p['strength'] for p in conf['aligned_patterns'])}")
    print(f"  Opposing: {len(conf['opposing_patterns'])} patterns, score {sum(p['strength'] for p in conf['opposing_patterns'])}")
    print(f"  Net score: {conf['net_score']}")
    print(f"  Description: {conf['description']}")

    assert conf["net_score"] == (75 + 65) - 55, f"Expected net 85, got {conf['net_score']}"
    assert len(conf["aligned_patterns"]) == 2
    assert len(conf["opposing_patterns"]) == 1
    assert conf["strongest_aligned"]["pattern"] == "Bullish Engulfing"


def test_no_bars():
    """Empty bar list should return no patterns."""
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze([], TICK)
    assert patterns == [], "Expected empty list for empty bars"
    print_patterns("No bars (empty input)", patterns)


def test_gravestone_doji():
    """Gravestone doji: open/close at low, long upper wick."""
    bars = [
        make_bar(100, 103, 100, 100),  # open=close=low=100, high=103
    ]
    analyzer = CandlestickAnalyzer()
    patterns = analyzer.analyze(bars, TICK)
    print_patterns("Gravestone Doji", patterns)
    names = [p["pattern"] for p in patterns]
    assert "Gravestone Doji" in names, "Expected Gravestone Doji"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_no_bars,
        test_hammer,
        test_shooting_star,
        test_doji,
        test_gravestone_doji,
        test_marubozu,
        test_bullish_engulfing,
        test_bearish_engulfing,
        test_piercing_line,
        test_tweezer_bottom,
        test_morning_star,
        test_evening_star,
        test_three_white_soldiers,
        test_three_black_crows,
        test_confluence_helper,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  >> PASS: {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  >> FAIL: {t.__name__} -- {e}")
        except Exception as e:
            failed += 1
            print(f"  >> ERROR: {t.__name__} -- {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'='*60}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
