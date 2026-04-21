"""
Tests for strategies/opening_session.py — 6 sub-evaluators + universal guards.

Run:  pytest tests/test_opening_session.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time as dtime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.strategies import STRATEGIES
from core.menthorq_gamma import GammaLevels
from strategies.base_strategy import Signal
from strategies.opening_session import ExitPlan, OpeningSessionStrategy


TICK = 0.25


# ─── Helpers ────────────────────────────────────────────────────────
def make_strategy(**config_overrides) -> OpeningSessionStrategy:
    config = dict(STRATEGIES["opening_session"])
    config.update(config_overrides)
    return OpeningSessionStrategy(config)


def ct(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 4, 20, hh, mm, ss)


def make_gamma_levels(call_resistance: float | None = None,
                      put_support: float | None = None) -> GammaLevels:
    return GammaLevels(
        symbol="NQ",
        data_date=date(2026, 4, 20),
        call_resistance=call_resistance,
        put_support=put_support,
        hvl=None,
        one_d_min=None,
        one_d_max=None,
        call_resistance_0dte=None,
        put_support_0dte=None,
        hvl_0dte=None,
        gamma_wall_0dte=None,
        gex_levels=(),
        blind_spots=(),
        loaded_at=datetime(2026, 4, 20, 7, 0),
    )


# ─── Per-sub-evaluator baseline markets ─────────────────────────────
def open_drive_market(**overrides) -> dict:
    m = {
        "now_ct": ct(8, 50),
        "opening_type": "OPEN_DRIVE",
        # Narrow 5-min OR, 20-pt bullish displacement, close at high
        "rth_open_price": 25000.0,
        "rth_5min_high": 25020.0,
        "rth_5min_low": 24998.0,
        "rth_5min_close": 25020.0,
        "rth_5min_volume": 150.0,
        "avg_5min_volume": 100.0,
        # Current snapshot — break confirmed above OR high
        "price": 25025.0,
        "rth_1min_volume": 150.0,
        "avg_1min_volume": 100.0,
        "pivot_pp": 25050.0,
        # Keep prior-day values so classifier is stable, but no ORB/PM fields.
        "prior_day_vah": 25050.0,
        "prior_day_val": 24950.0,
        "prior_day_high": 25100.0,
        "prior_day_low": 24900.0,
    }
    m.update(overrides)
    return m


def open_test_drive_market(**overrides) -> dict:
    m = {
        "now_ct": ct(8, 50),
        "opening_type": "OPEN_TEST_DRIVE",
        "rth_open_price": 25008.0,
        "rth_5min_high": 25015.0,  # wicked above pd_high
        "rth_5min_low": 25000.0,
        "rth_5min_close": 25002.0,
        "rth_5min_volume": 100.0,
        "avg_5min_volume": 100.0,
        "price": 25001.0,          # back below rth_open → SHORT confirmed
        "rth_1min_volume": 130.0,
        "avg_1min_volume": 100.0,
        "prior_day_high": 25010.0,
        "prior_day_low": 24950.0,
        "prior_day_vah": 25005.0,
        "prior_day_val": 24970.0,
        "prior_day_poc": 24990.0,
    }
    m.update(overrides)
    return m


def auction_in_market(**overrides) -> dict:
    m = {
        "now_ct": ct(10, 0),
        "opening_type": "OPEN_AUCTION_IN",
        "rth_60min_high": 25100.0,
        "rth_60min_low": 24950.0,
        "rth_1min_open": 25080.0,
        "rth_1min_high": 25105.0,   # touched IB high
        "rth_1min_low": 25080.0,
        "rth_1min_close": 25085.0,  # rejected back below
        "rth_1min_volume": 130.0,
        "avg_1min_volume": 100.0,
        "prior_day_poc": 25000.0,
        "price": 25085.0,
    }
    m.update(overrides)
    return m


def auction_out_market(**overrides) -> dict:
    m = {
        "now_ct": ct(9, 0),
        "opening_type": "OPEN_AUCTION_OUT",
        "rth_open_price": 25100.0,  # gap up above pd_high=25050
        "prior_day_high": 25050.0,
        "prior_day_low": 24950.0,
        "prior_day_poc": 25000.0,
        "pivot_r1": 25200.0,
        "pivot_s1": 24900.0,
        "price": 25051.0,           # pullback to pd_high (acceptance)
        "rth_1min_volume": 130.0,
        "avg_1min_volume": 100.0,
    }
    m.update(overrides)
    return m


def pm_market(**overrides) -> dict:
    m = {
        "now_ct": ct(8, 35),
        "opening_type": "INDETERMINATE",
        "pmh": 25000.0,
        "pml": 24985.0,             # 15-pt PM range
        "price": 25002.0,           # break above PMH + 2 ticks
        "rth_1min_volume": 150.0,
        "avg_1min_volume": 100.0,
        "pivot_pp": 25030.0,
    }
    m.update(overrides)
    return m


def orb_market(**overrides) -> dict:
    m = {
        "now_ct": ct(9, 1),
        "opening_type": "INDETERMINATE",
        "rth_15min_high": 25030.0,
        "rth_15min_low": 25010.0,
        "rth_open_price": 25020.0,
        "rth_5min_close_last": 25033.0,
        "price": 25033.0,
    }
    m.update(overrides)
    return m


# ═══════════════════════════════════════════════════════════════════
# Universal guards
# ═══════════════════════════════════════════════════════════════════
class TestUniversalGuards:
    def test_blocks_after_max_daily_trades(self):
        s = make_strategy()
        s._daily_trades_today = 2
        s._trade_date = "2026-04-20"
        assert s.evaluate(orb_market()) is None

    def test_blocks_during_news_blackout(self):
        s = make_strategy()
        m = orb_market()
        m["news_calendar"] = [{"time_ct": ct(9, 1), "impact": "high"}]
        assert s.evaluate(m) is None

    def test_blocks_after_day_flat_time(self):
        s = make_strategy()
        m = orb_market(now_ct=ct(14, 31))
        assert s.evaluate(m) is None

    def test_gamma_gate_rejects_entry_into_wall(self):
        s = make_strategy()
        m = orb_market()
        # Call resistance a few ticks above entry → LONG blocked.
        m["gamma_levels"] = make_gamma_levels(call_resistance=m["price"] + 5 * TICK)
        assert s.evaluate(m) is None
        # Counter not bumped when gamma blocks.
        assert s._daily_trades_today == 0


# ═══════════════════════════════════════════════════════════════════
# Universal stop math
# ═══════════════════════════════════════════════════════════════════
class TestUniversalStopMath:
    def test_stop_widens_to_min_when_structural_too_tight(self):
        # Narrow Open Drive OR → mid is only ~18 ticks from entry; widen to 40.
        s = make_strategy()
        m = open_drive_market(
            rth_5min_high=25020.0, rth_5min_low=25015.0, rth_5min_close=25020.0,
            price=25022.0,
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.stop_ticks == 40
        assert sig.stop_price == pytest.approx(25022.0 - 40 * TICK)

    def test_stop_rejects_signal_when_structural_too_wide(self):
        # ORB with 125-pt OR → stop 500 ticks away, exceeds 100-tick max.
        s = make_strategy()
        m = orb_market(
            rth_15min_high=25125.0, rth_15min_low=25000.0,
            rth_5min_close_last=25130.0, price=25130.0,
        )
        assert s.evaluate(m) is None


# ═══════════════════════════════════════════════════════════════════
# Open Drive
# ═══════════════════════════════════════════════════════════════════
class TestOpenDrive:
    def test_open_drive_long_fires_with_confirmation(self):
        s = make_strategy()
        sig = s.evaluate(open_drive_market())
        assert sig is not None
        assert sig.direction == "LONG"
        assert sig.metadata["sub_name"] == "open_drive"
        assert sig.metadata["invalidation"] == "price_re_enters_5min_or"

    def test_open_drive_short_fires_with_confirmation(self):
        s = make_strategy()
        m = open_drive_market(
            rth_5min_high=25002.0, rth_5min_low=24980.0, rth_5min_close=24980.0,
            price=24975.0, pivot_pp=24950.0,
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "SHORT"

    def test_open_drive_skips_when_volume_low(self):
        s = make_strategy()
        m = open_drive_market(rth_1min_volume=100.0)  # 1.0x avg, below 1.2x
        assert s.evaluate(m) is None

    def test_open_drive_only_fires_in_window(self):
        s = make_strategy()
        m = open_drive_market(now_ct=ct(9, 15))  # past 09:00 end
        # ORB fields absent, Auction windows blocked by opening_type; nothing fires.
        assert s.evaluate(m) is None

    def test_open_drive_t1_is_pivot_pp(self):
        s = make_strategy()
        m = open_drive_market()
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.metadata["t1"] == pytest.approx(m["pivot_pp"])
        assert sig.target_price == pytest.approx(m["pivot_pp"])


# ═══════════════════════════════════════════════════════════════════
# Open Test Drive
# ═══════════════════════════════════════════════════════════════════
class TestOpenTestDrive:
    def test_open_test_drive_short_after_failed_bull_test(self):
        s = make_strategy()
        sig = s.evaluate(open_test_drive_market())
        assert sig is not None
        assert sig.direction == "SHORT"
        assert sig.metadata["sub_name"] == "open_test_drive"

    def test_open_test_drive_long_after_failed_bear_test(self):
        s = make_strategy()
        m = open_test_drive_market(
            rth_open_price=24992.0,
            rth_5min_high=25000.0,
            rth_5min_low=24985.0,      # below pd_low=24990
            rth_5min_close=24998.0,    # back inside, above open
            price=24999.0,
            prior_day_high=25050.0,
            prior_day_low=24990.0,
            prior_day_vah=25020.0,
            prior_day_val=24980.0,
            prior_day_poc=25010.0,
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "LONG"

    def test_open_test_drive_skips_without_full_reversal(self):
        # SHORT setup but price is still above rth_open (no reversal through open).
        s = make_strategy()
        m = open_test_drive_market(price=25010.0)  # > rth_open=25008
        assert s.evaluate(m) is None

    def test_open_test_drive_time_exit_75_min(self):
        s = make_strategy()
        m = open_test_drive_market(now_ct=ct(8, 50))
        sig = s.evaluate(m)
        assert sig is not None
        # 08:50 + 75 min = 10:05
        assert sig.metadata["time_exit_ct"] == dtime(10, 5)


# ═══════════════════════════════════════════════════════════════════
# Open Auction In
# ═══════════════════════════════════════════════════════════════════
class TestOpenAuctionIn:
    def test_open_auction_in_short_at_ib_high_with_rejection(self):
        s = make_strategy()
        sig = s.evaluate(auction_in_market())
        assert sig is not None
        assert sig.direction == "SHORT"
        assert sig.metadata["sub_name"] == "open_auction_in"

    def test_open_auction_in_long_at_ib_low_with_rejection(self):
        s = make_strategy()
        m = auction_in_market(
            rth_1min_open=24970.0,
            rth_1min_high=24970.0,
            rth_1min_low=24945.0,      # touched IB low (24950)
            rth_1min_close=24965.0,    # rejected back above
            price=24965.0,
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "LONG"

    def test_open_auction_in_target_is_prior_poc(self):
        s = make_strategy()
        m = auction_in_market()
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.metadata["t1"] == pytest.approx(m["prior_day_poc"])

    def test_open_auction_in_no_be_move(self):
        s = make_strategy()
        sig = s.evaluate(auction_in_market())
        assert sig is not None
        assert sig.metadata["be_milestone"] is None


# ═══════════════════════════════════════════════════════════════════
# Open Auction Out
# ═══════════════════════════════════════════════════════════════════
class TestOpenAuctionOut:
    def test_open_auction_out_acceptance_long_on_pullback(self):
        s = make_strategy()
        sig = s.evaluate(auction_out_market())
        assert sig is not None
        assert sig.direction == "LONG"
        assert sig.metadata["scenario"] == "ACCEPTANCE"
        assert sig.metadata["t1"] == pytest.approx(25200.0)  # pivot_r1

    def test_open_auction_out_acceptance_short_on_pullback(self):
        s = make_strategy()
        m = auction_out_market(
            rth_open_price=24900.0,     # gap down
            price=24949.0,              # pullback to pd_low from below
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "SHORT"
        assert sig.metadata["scenario"] == "ACCEPTANCE"
        assert sig.metadata["t1"] == pytest.approx(24900.0)  # pivot_s1

    def test_open_auction_out_rejection_short_after_gap_up_fill(self):
        s = make_strategy()
        m = auction_out_market(
            rth_open_price=25060.0,     # smaller gap up (10 pts above pd_high)
            price=25049.0,              # back inside prior range
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "SHORT"
        assert sig.metadata["scenario"] == "REJECTION"
        assert sig.metadata["t1"] == pytest.approx(25000.0)  # pd_poc

    def test_open_auction_out_rejection_long_after_gap_down_fill(self):
        s = make_strategy()
        m = auction_out_market(
            rth_open_price=24940.0,     # small gap down
            price=24951.0,              # back inside prior range
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "LONG"
        assert sig.metadata["scenario"] == "REJECTION"
        assert sig.metadata["t1"] == pytest.approx(25000.0)  # pd_poc

    def test_open_auction_out_only_fires_after_845(self):
        s = make_strategy()
        # 08:40 is before the Auction Out window (08:45-11:00).
        m = auction_out_market(now_ct=ct(8, 40))
        assert s.evaluate(m) is None


# ═══════════════════════════════════════════════════════════════════
# Premarket Breakout
# ═══════════════════════════════════════════════════════════════════
class TestPremarketBreakout:
    def test_premarket_breakout_long_above_pmh(self):
        s = make_strategy()
        sig = s.evaluate(pm_market())
        assert sig is not None
        assert sig.direction == "LONG"
        assert sig.metadata["sub_name"] == "premarket_breakout"

    def test_premarket_breakout_short_below_pml(self):
        s = make_strategy()
        m = pm_market(
            pmh=25015.0, pml=25000.0,   # 15-pt range
            price=24997.0,               # below pml - 2 ticks (24999.5)
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "SHORT"

    def test_premarket_breakout_skips_when_range_too_small(self):
        s = make_strategy()
        m = pm_market(pmh=25005.0, pml=25000.0)  # 5-pt range < 10
        assert s.evaluate(m) is None

    def test_premarket_breakout_skips_without_volume_confirmation(self):
        s = make_strategy()
        m = pm_market(rth_1min_volume=120.0)  # 1.2x avg < 1.4x
        assert s.evaluate(m) is None

    def test_premarket_breakout_target_is_pivot_pp(self):
        s = make_strategy()
        m = pm_market()
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.metadata["t1"] == pytest.approx(m["pivot_pp"])


# ═══════════════════════════════════════════════════════════════════
# ORB
# ═══════════════════════════════════════════════════════════════════
class TestORB:
    def test_orb_long_fires_above_15min_high(self):
        s = make_strategy()
        sig = s.evaluate(orb_market())
        assert sig is not None
        assert sig.direction == "LONG"
        assert sig.metadata["sub_name"] == "orb"

    def test_orb_short_fires_below_15min_low(self):
        s = make_strategy()
        m = orb_market(
            rth_5min_close_last=25007.0,
            price=25007.0,
        )
        sig = s.evaluate(m)
        assert sig is not None
        assert sig.direction == "SHORT"

    def test_orb_skips_when_range_too_large(self):
        s = make_strategy()
        m = orb_market(
            rth_15min_high=25300.0, rth_15min_low=25000.0,  # 300-pt OR = 1.2% of 25000
            rth_5min_close_last=25310.0, price=25310.0,
        )
        assert s.evaluate(m) is None

    def test_orb_target_is_50pct_of_or(self):
        s = make_strategy()
        m = orb_market()
        sig = s.evaluate(m)
        assert sig is not None
        or_size = m["rth_15min_high"] - m["rth_15min_low"]
        expected_t1 = m["price"] + 0.5 * or_size
        assert sig.metadata["t1"] == pytest.approx(expected_t1)

    def test_orb_one_trade_per_day_after_first_break(self):
        s = make_strategy()
        # First break LONG fires.
        first = s.evaluate(orb_market())
        assert first is not None
        assert first.direction == "LONG"
        # Reset the trade counter so max-per-day doesn't confound the one-trade rule.
        s._daily_trades_today = 0
        # Second break in OPPOSITE direction must be rejected by the one-trade rule.
        m = orb_market(
            now_ct=ct(9, 5),
            rth_5min_close_last=25007.0, price=25007.0,
        )
        assert s.evaluate(m) is None


# ═══════════════════════════════════════════════════════════════════
# Exit planner
# ═══════════════════════════════════════════════════════════════════
class TestExitPlan:
    def test_determine_exits_returns_exit_plan_for_single_contract(self):
        s = make_strategy()
        sig = s.evaluate(open_drive_market())
        assert sig is not None
        plan = s.determine_exits(sig, snapshot={}, contract_count=1)
        assert isinstance(plan, ExitPlan)
        assert plan.primary_target == pytest.approx(sig.metadata["t1"])
        assert plan.be_move_at == pytest.approx(sig.metadata["be_milestone"])
        assert plan.time_exit_ct == sig.metadata["time_exit_ct"]

    def test_determine_exits_raises_on_multi_leg(self):
        s = make_strategy()
        sig = s.evaluate(open_drive_market())
        assert sig is not None
        with pytest.raises(NotImplementedError):
            s.determine_exits(sig, snapshot={}, contract_count=2)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
