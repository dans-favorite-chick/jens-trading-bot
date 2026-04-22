"""B21 — managed-exit strategies don't let structural stop_ticks crush sizing."""
from core.risk_manager import RiskManager
from strategies.noise_area import NoiseAreaMomentum
from strategies.base_strategy import BaseStrategy


def test_noise_area_has_managed_exit_flag():
    s = NoiseAreaMomentum({"enabled": True})
    assert s.uses_managed_exit is True


def test_base_strategy_default_is_real_stops():
    class Dummy(BaseStrategy):
        name = "dummy"
    assert Dummy({}).uses_managed_exit is False


def test_managed_exit_sizing_uses_risk_reference_not_structural():
    rm = RiskManager()
    # Real stop strategy: 600t stop + $15 risk => 0 contracts (min 1)
    real = rm.calculate_contracts(risk_dollars=15.0, stop_ticks=600)
    assert real == 1  # clamp floor

    class ME(BaseStrategy):
        name = "managed"
        uses_managed_exit = True
    me = ME({})
    # Managed exit: 600t should be capped to ATR_STOP_MAX_TICKS (40) for sizing
    # $15 / (40t * $0.50) = 0.75 -> clamps to 1
    managed = rm.calculate_contracts(risk_dollars=15.0, stop_ticks=600, strategy=me)
    assert managed >= real

    # Higher risk budget shows the real difference
    # $100 / (40t * $0.50) = 5 contracts; vs real: $100 / (600*$0.50) = 0.33 -> 1
    managed_big = rm.calculate_contracts(risk_dollars=100.0, stop_ticks=600, strategy=me)
    real_big = rm.calculate_contracts(risk_dollars=100.0, stop_ticks=600)
    assert managed_big > real_big
    assert managed_big == 5


def test_managed_exit_preserves_small_structural_stops():
    """If structural stop is already <= cap, use it as-is."""
    class ME(BaseStrategy):
        name = "managed"
        uses_managed_exit = True
    rm = RiskManager()
    # 20t stop is below ATR_STOP_MAX_TICKS (40), should not be inflated
    n = rm.calculate_contracts(risk_dollars=20.0, stop_ticks=20, strategy=ME({}))
    # $20 / (20t * $0.50) = 2
    assert n == 2
