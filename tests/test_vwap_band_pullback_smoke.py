"""
Smoke test for strategies/vwap_band_pullback.py — ported from b12.

Scope: loads the class, instantiates it, feeds an insufficient-bars
snapshot through evaluate() and asserts no exception. Full algorithm
coverage is intentionally deferred to lab validation (50+ trades).
"""


class _Bar:
    def __init__(self, o, h, l, c, v=100):
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v


def test_import_and_class_available():
    from strategies.vwap_band_pullback import VwapBandPullback
    assert VwapBandPullback.name == "vwap_band_pullback"


def test_instantiates_with_empty_config():
    from strategies.vwap_band_pullback import VwapBandPullback
    strat = VwapBandPullback({})
    assert strat.name == "vwap_band_pullback"


def test_evaluate_returns_none_on_insufficient_bars():
    from strategies.vwap_band_pullback import VwapBandPullback
    strat = VwapBandPullback({"min_bars": 50})
    market = {"tf_votes_bullish": 4, "tf_votes_bearish": 0}
    # 10 bars < min_bars 50 → returns None via SKIP path
    bars_5m = [_Bar(21000, 21001, 20999, 21000) for _ in range(10)]
    result = strat.evaluate(market, bars_5m, [], {"regime": "UNKNOWN"})
    assert result is None


def test_evaluate_returns_none_when_mtf_not_aligned():
    from strategies.vwap_band_pullback import VwapBandPullback
    strat = VwapBandPullback({})
    market = {"tf_votes_bullish": 1, "tf_votes_bearish": 1}
    bars_5m = [_Bar(21000, 21001, 20999, 21000) for _ in range(60)]
    result = strat.evaluate(market, bars_5m, [], {"regime": "UNKNOWN"})
    assert result is None


def test_registered_in_base_bot_strategy_classes():
    """Confirms the strategy is wired into base_bot's strategy_classes dict."""
    import inspect
    from bots import base_bot
    src = inspect.getsource(base_bot.BaseBot.load_strategies)
    assert "vwap_band_pullback" in src
    assert "VwapBandPullback" in src


def test_registered_in_strategies_config():
    from config.strategies import STRATEGIES
    assert "vwap_band_pullback" in STRATEGIES
    cfg = STRATEGIES["vwap_band_pullback"]
    assert cfg["enabled"] is True
    assert cfg["validated"] is False
    assert cfg["min_stop_ticks"] == 40
    assert cfg["max_stop_ticks"] == 120
