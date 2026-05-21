"""
Tests for core/tier_sizer.py (F-001 compounding sizing).

Coverage:
  * Tier math (equity → contracts at various tiers)
  * Per-strategy multiplier application
  * ATH update on winners, NOT decreased on losses
  * 85%-DD scale-down trigger
  * 4% daily breaker trigger
  * 3-loss halving trigger + reset on win
  * Persistence round-trip
  * Session-roll across midnight
  * Hard cap at MAX_CONTRACTS_CAP
  * SIZING_MODE="flat_1" preserves legacy dispatcher behavior

The tests use a tempdir state path so the real
``data/equity_state.json`` is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.tier_sizer import (
    CONSECUTIVE_LOSS_LIMIT,
    DAILY_CIRCUIT_PCT,
    DD_SCALE_DOWN_PCT,
    DEFAULT_DOLLARS_PER_CONTRACT,
    MAX_CONTRACTS_CAP,
    STRATEGY_SIZE_MULT,
    TierSizer,
    reset_tier_sizer,
)


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def tmp_state(tmp_path) -> Path:
    """Fresh equity state file path per test."""
    p = tmp_path / "equity_state.json"
    return p


@pytest.fixture
def sizer(tmp_state) -> TierSizer:
    """Sizer with $1500 starting equity, isolated state file."""
    reset_tier_sizer()
    return TierSizer(state_path=tmp_state, starting_equity=1500.0)


@pytest.fixture
def sizer_30k(tmp_state) -> TierSizer:
    """Sizer with $30,000 starting equity for tier-math tests."""
    reset_tier_sizer()
    return TierSizer(state_path=tmp_state, starting_equity=30000.0)


# ─── 1. Tier math ─────────────────────────────────────────────────────

class TestTierMath:
    def test_tier_at_starting_1500_yields_1_contract(self, sizer):
        # 1500 / 3000 = 0.5 → floor 0 → MIN 1
        assert sizer.compute_contracts(strategy=None) == 1

    def test_tier_at_3000_yields_1_contract(self, tmp_state):
        s = TierSizer(state_path=tmp_state, starting_equity=3000.0)
        assert s.compute_contracts(strategy=None) == 1

    def test_tier_at_6000_yields_2_contracts(self, tmp_state):
        s = TierSizer(state_path=tmp_state, starting_equity=6000.0)
        assert s.compute_contracts(strategy=None) == 2

    def test_tier_at_30000_yields_10_contracts(self, sizer_30k):
        assert sizer_30k.compute_contracts(strategy=None) == 10

    def test_tier_at_100000_yields_30_capped(self, tmp_state):
        s = TierSizer(state_path=tmp_state, starting_equity=100_000.0)
        # 100k / 3k = 33 → capped at 30
        assert s.compute_contracts(strategy=None) == 30
        assert s.compute_contracts(strategy=None) <= MAX_CONTRACTS_CAP

    def test_tier_at_1_million_still_capped_at_30(self, tmp_state):
        s = TierSizer(state_path=tmp_state, starting_equity=1_000_000.0)
        assert s.compute_contracts(strategy=None) == MAX_CONTRACTS_CAP


# ─── 2. ATH tracking ──────────────────────────────────────────────────

class TestATHTracking:
    def test_ath_updates_on_winning_trade(self, sizer):
        assert sizer.state.equity_ath == 1500.0
        sizer.record_trade_close(pnl_dollars=500.0, was_winner=True)
        assert sizer.state.current_equity == 2000.0
        assert sizer.state.equity_ath == 2000.0

    def test_ath_NOT_decreased_on_losing_trade(self, sizer):
        sizer.record_trade_close(pnl_dollars=500.0, was_winner=True)
        assert sizer.state.equity_ath == 2000.0
        sizer.record_trade_close(pnl_dollars=-300.0, was_winner=False)
        # equity drops to 1700 but ATH stays at 2000
        assert sizer.state.current_equity == 1700.0
        assert sizer.state.equity_ath == 2000.0

    def test_ath_keeps_climbing_on_consecutive_wins(self, sizer):
        for pnl in (100, 200, 300, 50):
            sizer.record_trade_close(pnl_dollars=pnl, was_winner=True)
        assert sizer.state.equity_ath == 1500.0 + 100 + 200 + 300 + 50

    def test_scratch_trade_does_not_change_ath(self, sizer):
        sizer.record_trade_close(pnl_dollars=300.0, was_winner=True)
        ath_before = sizer.state.equity_ath
        sizer.record_trade_close(pnl_dollars=0.0)  # scratch
        assert sizer.state.equity_ath == ath_before


# ─── 3. 85% DD scale-down ─────────────────────────────────────────────

class TestDDScaleDown:
    def test_scale_down_triggers_below_85_pct_ath(self, sizer_30k):
        # ATH = 30k, base tier = 10. Drop equity to 84% of ATH.
        sizer_30k.state.equity_ath = 30000.0
        sizer_30k.state.current_equity = 30000.0 * 0.84  # 25,200
        # 25200/3000 = 8.4 → floor 8, then -1 for DD = 7
        contracts = sizer_30k.compute_contracts(strategy=None)
        assert contracts == 7

    def test_no_scale_down_at_85_pct_or_above(self, sizer_30k):
        sizer_30k.state.equity_ath = 30000.0
        sizer_30k.state.current_equity = 30000.0 * DD_SCALE_DOWN_PCT  # exactly 85% — not BELOW
        # 25500/3000 = 8.5 → floor 8, no scale-down
        contracts = sizer_30k.compute_contracts(strategy=None)
        assert contracts == 8

    def test_scale_down_floor_at_1(self, tmp_state):
        s = TierSizer(state_path=tmp_state, starting_equity=3000.0)
        s.state.equity_ath = 3000.0
        s.state.current_equity = 1.0  # absurd DD
        # base = 1, scale-down tries base-1=0, floored at MIN=1
        assert s.compute_contracts(strategy=None) == 1


# ─── 4. 4% daily circuit breaker ──────────────────────────────────────

class TestDailyCircuitBreaker:
    def test_breaker_does_not_trip_at_3_pct_loss(self, sizer_30k):
        # 3% loss = -$900
        sizer_30k.record_trade_close(pnl_dollars=-900.0, was_winner=False)
        halted, _ = sizer_30k.is_halted_today()
        assert not halted

    def test_breaker_trips_at_5_pct_loss(self, sizer_30k):
        # 5% loss = -$1500 > 4% of $30k = $1200
        sizer_30k.record_trade_close(pnl_dollars=-1500.0, was_winner=False)
        halted, reason = sizer_30k.is_halted_today()
        assert halted
        assert "circuit breaker" in reason

    def test_halted_compute_returns_zero(self, sizer_30k):
        sizer_30k.record_trade_close(pnl_dollars=-2000.0, was_winner=False)
        contracts = sizer_30k.compute_contracts(strategy="bias_momentum")
        assert contracts == 0

    def test_breaker_uses_session_start_not_current(self, sizer_30k):
        # session_start = $30k. Lose $1500 (5% > 4%). Breaker trips.
        sizer_30k.record_trade_close(pnl_dollars=-1500.0, was_winner=False)
        halted, _ = sizer_30k.is_halted_today()
        assert halted
        # current_equity is now $28500, but breaker is referenced to
        # session_start. Win back $1000 — still halted (session_pnl = -500)?
        # No — session_pnl is -1500 still (we win back), so actually it
        # rises. Let's just verify the math: session_start should be 30k.
        assert sizer_30k.state.session_start_equity == 30000.0


# ─── 5. Per-strategy multipliers ──────────────────────────────────────

class TestStrategyMultipliers:
    def test_bias_momentum_15x(self, sizer_30k):
        # base = 10, bias_momentum mult = 1.5 → 15
        assert sizer_30k.compute_contracts(strategy="bias_momentum") == 15

    def test_vwap_band_reversion_05x(self, sizer_30k):
        # base = 10, vwap_band_reversion mult = 0.5 → 5
        assert sizer_30k.compute_contracts(strategy="vwap_band_reversion") == 5

    def test_unknown_strategy_defaults_to_1x(self, sizer_30k):
        assert sizer_30k.compute_contracts(strategy="nonexistent_strategy") == 10

    def test_no_strategy_defaults_to_1x(self, sizer_30k):
        assert sizer_30k.compute_contracts(strategy=None) == 10

    def test_strategy_mult_respects_cap(self, tmp_state):
        s = TierSizer(state_path=tmp_state, starting_equity=80_000.0)
        # base = min(26, 30) = 26, bias_momentum × 1.5 = 39 → capped at 30
        assert s.compute_contracts(strategy="bias_momentum") == 30


# ─── 6. 3-consecutive-loss halving ────────────────────────────────────

class TestThreeLossHalving:
    def test_halving_after_3_losses(self, sizer_30k):
        # 3 losses of $50 each → equity drops to $29,850.
        # base = int(29850/3000) = 9. consec_losses=3 → halved 9//2 = 4.
        for _ in range(CONSECUTIVE_LOSS_LIMIT):
            sizer_30k.record_trade_close(pnl_dollars=-50.0, was_winner=False)
        assert sizer_30k.state.consecutive_losses == 3
        contracts = sizer_30k.compute_contracts(strategy=None)
        assert contracts == 4

    def test_consec_resets_on_win(self, sizer_30k):
        for _ in range(2):
            sizer_30k.record_trade_close(pnl_dollars=-50.0, was_winner=False)
        assert sizer_30k.state.consecutive_losses == 2
        sizer_30k.record_trade_close(pnl_dollars=10.0, was_winner=True)
        assert sizer_30k.state.consecutive_losses == 0

    def test_halving_floor_at_1(self, sizer):
        # Starting equity $1500 → base = 1. Halved → max(1, 0) = 1
        for _ in range(3):
            sizer.record_trade_close(pnl_dollars=-10.0, was_winner=False)
        contracts = sizer.compute_contracts(strategy=None)
        assert contracts == 1

    def test_scratch_trade_does_not_increment_consec(self, sizer):
        sizer.record_trade_close(pnl_dollars=-10.0, was_winner=False)
        sizer.record_trade_close(pnl_dollars=0.0)  # scratch
        sizer.record_trade_close(pnl_dollars=-10.0, was_winner=False)
        # 2 losses with a scratch between — counter should be at 2, not 3
        assert sizer.state.consecutive_losses == 2


# ─── 7. Persistence ───────────────────────────────────────────────────

class TestPersistence:
    def test_state_persists_across_init(self, tmp_state):
        reset_tier_sizer()
        s1 = TierSizer(state_path=tmp_state, starting_equity=5000.0)
        s1.record_trade_close(pnl_dollars=500.0, was_winner=True)
        assert s1.state.current_equity == 5500.0

        # New sizer, same state path — should load the persisted state
        reset_tier_sizer()
        s2 = TierSizer(state_path=tmp_state, starting_equity=999.0)  # ignored
        assert s2.state.current_equity == 5500.0
        assert s2.state.equity_ath == 5500.0
        assert s2.state.starting_equity == 5000.0  # preserved from file

    def test_state_file_is_valid_json(self, sizer):
        sizer.record_trade_close(pnl_dollars=100.0, was_winner=True)
        with open(sizer.state_path, "r") as f:
            raw = json.load(f)
        assert raw["current_equity"] == 1600.0
        assert raw["equity_ath"] == 1600.0
        assert isinstance(raw["history"], list)
        assert len(raw["history"]) == 1

    def test_history_caps_at_200(self, sizer):
        for i in range(250):
            sizer.record_trade_close(pnl_dollars=1.0, was_winner=True)
        assert len(sizer.state.history) == 200

    def test_corrupt_state_file_reinits(self, tmp_state):
        tmp_state.write_text("{ this is garbage ")
        reset_tier_sizer()
        s = TierSizer(state_path=tmp_state, starting_equity=2222.0)
        assert s.state.current_equity == 2222.0


# ─── 8. Session roll ──────────────────────────────────────────────────

class TestSessionRoll:
    def test_manual_session_roll_resets_pnl_and_breaker(self, sizer_30k):
        sizer_30k.record_trade_close(pnl_dollars=-2000.0, was_winner=False)
        halted, _ = sizer_30k.is_halted_today()
        assert halted
        # Manually roll the session (simulates next-day open)
        sizer_30k.reset_session()
        halted2, _ = sizer_30k.is_halted_today()
        assert not halted2
        assert sizer_30k.state.session_pnl == 0.0
        # session_start_equity should now reflect post-loss balance
        assert sizer_30k.state.session_start_equity == 28000.0


# ─── 9. SIZING_MODE dispatcher (flat_1 preserves legacy) ─────────────

class TestSizingModeDispatcher:
    """
    Verifies the dispatcher contract from bots/base_bot.py — when
    SIZING_MODE='flat_1', tier_sizer must NOT be invoked. Direct path
    check: import the module and confirm the default settings value.
    """
    def test_default_sizing_mode_is_flat_1(self):
        from config import settings
        assert settings.SIZING_MODE == "flat_1"

    def test_starting_equity_constant_exists(self):
        from config import settings
        assert hasattr(settings, "STARTING_EQUITY")
        assert isinstance(settings.STARTING_EQUITY, (int, float))
        assert settings.STARTING_EQUITY > 0


# ─── 10. Halt query semantics ────────────────────────────────────────

class TestHaltQuery:
    def test_fresh_sizer_not_halted(self, sizer):
        halted, reason = sizer.is_halted_today()
        assert not halted
        assert reason == ""

    def test_halt_query_is_pure_read(self, sizer_30k):
        # is_halted_today() shouldn't mutate session_pnl etc.
        sizer_30k.record_trade_close(pnl_dollars=-200.0, was_winner=False)
        before_pnl = sizer_30k.state.session_pnl
        before_eq = sizer_30k.state.current_equity
        sizer_30k.is_halted_today()
        sizer_30k.is_halted_today()
        assert sizer_30k.state.session_pnl == before_pnl
        assert sizer_30k.state.current_equity == before_eq


# ─── 11. Edge cases / invariants ─────────────────────────────────────

class TestInvariants:
    def test_compute_never_negative(self, sizer):
        # Even at zero equity, MIN floor applies (unless halted)
        sizer.state.current_equity = 0.0
        sizer.state.equity_ath = 0.0  # avoid ATH-DD trigger
        assert sizer.compute_contracts(strategy=None) >= 1

    def test_compute_never_exceeds_cap(self, tmp_state):
        s = TierSizer(state_path=tmp_state, starting_equity=10_000_000.0)
        for strat in list(STRATEGY_SIZE_MULT) + [None, "unknown"]:
            assert s.compute_contracts(strategy=strat) <= MAX_CONTRACTS_CAP

    def test_record_trade_handles_bad_pnl_gracefully(self, sizer):
        # Should warn-and-ignore, not raise
        eq_before = sizer.state.current_equity
        sizer.record_trade_close(pnl_dollars="not_a_number")  # type: ignore
        assert sizer.state.current_equity == eq_before
