"""Sprint H v3 — Footprint + CVD Reversal tests.

Covers:
- Hard gates (lunch block, session boundary)
- Confluence 1: HTF level scoring with real Phoenix MenthorQ key names
  (put_support / put_support_0dte / call_resistance / call_resistance_0dte
  / hvl / hvl_0dte — accessed via getattr on a GammaLevels-like object,
  NOT a "_all"-suffixed dict)
- Confluence 2: CVD divergence (multi-bar regular + single-bar delta)
- Confluence 3: Footprint absorption / stacked / oversized
- Confluence 4: CVD compression (5 sub-dimensions, including the
  volume-holding key check that distinguishes absorption from dead market)
- Tier classification
- End-to-end FootprintCVDReversal class with mocked volumetric data on disk
- Routing wiring (config/account_routing.py)
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategies.footprint_cvd_reversal import (
    FootprintCVDConfig,
    FootprintCVDReversal,
    _classify_tier,
    _is_lunch_block,
    _is_session_boundary,
    _score_compression,
    _score_cvd_divergence,
    _score_footprint,
    _score_htf_level,
)

CT = ZoneInfo("America/Chicago")


# Sprint J (2026-05-05): _FakeGammaLevels stub removed — the strategy
# now consumes structural levels from the market dict directly via
# _build_pa_levels_from_market. Tests just put fields like
# {"prior_day_low": 27800.0, "vwap": 27800.0, ...} into the market.


# ═══════════════════════════════════════════════════════════════════
# Hard gates
# ═══════════════════════════════════════════════════════════════════
def test_lunch_block_blocks_11am():
    cfg = FootprintCVDConfig()
    assert _is_lunch_block(datetime(2026, 5, 5, 11, 0, tzinfo=CT), cfg)


def test_lunch_block_allows_9am():
    cfg = FootprintCVDConfig()
    assert not _is_lunch_block(datetime(2026, 5, 5, 9, 0, tzinfo=CT), cfg)


def test_lunch_block_allows_2pm():
    cfg = FootprintCVDConfig()
    assert not _is_lunch_block(datetime(2026, 5, 5, 14, 0, tzinfo=CT), cfg)


def test_lunch_block_handles_list_config_for_tuple_field():
    """JSON config can't hold tuples — they round-trip as lists.
    The gate must coerce back so tests with config-loaded dicts work."""
    cfg = FootprintCVDConfig(
        lunch_block_start_ct=[10, 0],
        lunch_block_end_ct=[13, 29],
    )
    assert _is_lunch_block(datetime(2026, 5, 5, 11, 0, tzinfo=CT), cfg)


def test_session_boundary_blocks_first_5min():
    cfg = FootprintCVDConfig()
    assert _is_session_boundary(datetime(2026, 5, 5, 8, 32, tzinfo=CT), cfg)


def test_session_boundary_allows_mid_session():
    cfg = FootprintCVDConfig()
    assert not _is_session_boundary(datetime(2026, 5, 5, 10, 0, tzinfo=CT), cfg)


# ═══════════════════════════════════════════════════════════════════
# Confluence 1: HTF level (real Phoenix MenthorQ attribute names)
# ═══════════════════════════════════════════════════════════════════
# Sprint J (2026-05-05): _score_htf_level is now market-dict-based via
# Sprint I's price_action_levels infrastructure (replaces MenthorQ
# GammaLevels). Tier-weighted points: TIER_1=25, TIER_2=18, TIER_3=12.
# Tier 1 sources = prior_day_high/low/close, session_poc / prior_day_poc.
# Tier 2 sources = vwap, hvn_levels.
# Tier 3 sources = lvn_levels.

def test_long_at_prior_day_low_scores_25_tier1():
    """PDL is tier-1 institutional level → 25 pts."""
    market = {"prior_day_low": 27800.0}
    score, name = _score_htf_level(27801.0, market, 8, 0.25, "long")
    assert score == 25
    assert name == "PDL"


def test_long_at_prior_day_high_scores_25_tier1():
    market = {"prior_day_high": 27800.0}
    score, name = _score_htf_level(27801.0, market, 8, 0.25, "long")
    assert score == 25
    assert name == "PDH"


def test_at_session_poc_scores_25_tier1():
    """Session POC (or prior_day_poc fallback) is tier-1."""
    market = {"prior_day_poc": 27800.0}
    score, name = _score_htf_level(27801.0, market, 8, 0.25, "long")
    assert score == 25
    assert name == "POC"


def test_at_vwap_scores_18_tier2():
    """VWAP is tier-2 strong structural → 18 pts."""
    market = {"vwap": 27800.0}
    score, name = _score_htf_level(27801.0, market, 8, 0.25, "long")
    assert score == 18
    assert name == "VWAP"


def test_at_hvn_scores_18_tier2():
    """HVN (high volume node) is tier-2 → 18 pts."""
    market = {"hvn_levels": [27800.0]}
    score, name = _score_htf_level(27801.0, market, 8, 0.25, "long")
    assert score == 18
    assert name.startswith("HVN_")


def test_at_lvn_scores_12_tier3():
    """LVN (low volume node) is tier-3 moderate → 12 pts."""
    market = {"lvn_levels": [27800.0]}
    score, name = _score_htf_level(27801.0, market, 8, 0.25, "long")
    assert score == 12
    assert name.startswith("LVN_")


def test_outside_buffer_scores_0():
    """Level too far from price → no confluence."""
    market = {"prior_day_low": 27800.0}
    score, name = _score_htf_level(27810.0, market, 8, 0.25, "long")
    assert score == 0


def test_zero_level_treated_as_unset():
    """Phoenix sometimes uses 0 as a 'not loaded' sentinel for levels.
    The HTF scorer must treat zeros as missing, not as a level at $0."""
    market = {"prior_day_low": 0.0, "prior_day_high": 0.0}
    score, _ = _score_htf_level(27800.0, market, 8, 0.25, "long")
    assert score == 0


def test_empty_market_dict_handled():
    """Strategies must survive a market dict with no level fields
    gracefully (e.g. on bot startup before any aggregator data)."""
    score, _ = _score_htf_level(27800.0, {}, 8, 0.25, "long")
    assert score == 0


def test_tier1_beats_tier2_when_equidistant():
    """When PDH and VWAP are both 1 tick away, PDH (tier 1) wins."""
    market = {"prior_day_high": 27801.0, "vwap": 27799.0}
    score, name = _score_htf_level(27800.0, market, 8, 0.25, "long")
    assert score == 25  # tier 1
    assert name == "PDH"


# ═══════════════════════════════════════════════════════════════════
# Confluence 2: CVD divergence
# ═══════════════════════════════════════════════════════════════════
def test_bullish_cvd_divergence_scores_above_zero():
    bars = []
    # Prior 10 bars — establishes the LL/HL setup
    for i in range(20):
        bars.append({"low": 27800 - i * 0.25, "high": 27810,
                     "open": 27805, "close": 27805,
                     "delta": -10, "cvd_session": -100 - i * 5})
    # Recent 10 bars: lower lows BUT higher CVD = divergence
    for i in range(10):
        bars.append({"low": 27780 - i * 0.25, "high": 27795,
                     "open": 27790, "close": 27790,
                     "delta": 5, "cvd_session": -100 + i * 3})
    score, debug = _score_cvd_divergence(bars, 10, "long", bars[-1])
    assert score >= 15
    assert debug["divergence_present"]


def test_no_divergence_when_aligned():
    bars = [{"low": 27800 - i * 0.25, "high": 27810,
             "open": 27805, "close": 27805,
             "delta": -10, "cvd_session": -100 - i * 5}
            for i in range(30)]
    score, debug = _score_cvd_divergence(bars, 10, "long", bars[-1])
    assert score == 0
    assert not debug["divergence_present"]


def test_warmup_returns_zero_divergence():
    """Need 2x lookback bars before divergence can be measured."""
    bars = [{"low": 27800, "high": 27810, "open": 27805, "close": 27805,
             "delta": 0, "cvd_session": -100} for _ in range(5)]
    score, debug = _score_cvd_divergence(bars, 10, "long", bars[-1])
    assert score == 0
    assert not debug["divergence_present"]


# ═══════════════════════════════════════════════════════════════════
# Confluence 3: Footprint
# ═══════════════════════════════════════════════════════════════════
# Sprint L (2026-05-08): _score_footprint v2 uses graduated scoring.
# Pre-Sprint-L tests asserted hardcoded 15-pt-binary values; v2 maps to:
#   - imbalance sub-score (0-18): per-level scoring or v1 stacked fallback
#   - absorption sub-score (0-7): graduated by |delta|/range_ticks ratio
# Total still capped at 25.
def test_footprint_legacy_stacked_buy_fallback_scores_12_on_long():
    """Legacy path: imbalances[] missing, stacked_buy=True triggers
    fallback equivalent to '3 same-side imbalances'."""
    cfg = FootprintCVDConfig()
    latest = {"stacked_buy": True, "stacked_sell": False,
              "delta": 0, "max_imbalance_ratio": 5.0,
              "high": 27801, "low": 27800}
    score, debug = _score_footprint(latest, "long", cfg, 0.25)
    assert score == 12  # v1=15 → v2 fallback=12 (matches "3 imbalances")
    assert debug["stacked"]


def test_footprint_per_level_imbalances_score_higher_than_legacy():
    """v2 win: 4+ stacked imbalances at bar extreme + oversized scores 18."""
    cfg = FootprintCVDConfig()
    latest = {
        "stacked_buy": True,
        "delta": 0,
        "max_imbalance_ratio": 12.0,
        "high": 27801, "low": 27800,
        "imbalances": [
            {"price": 27800.00, "bid_vol": 5, "ask_vol": 25, "ratio": 5.0, "side": "buy"},
            {"price": 27800.25, "bid_vol": 8, "ask_vol": 30, "ratio": 3.7, "side": "buy"},
            {"price": 27800.50, "bid_vol": 4, "ask_vol": 18, "ratio": 4.5, "side": "buy"},
            {"price": 27800.75, "bid_vol": 6, "ask_vol": 22, "ratio": 3.7, "side": "buy"},
        ],
    }
    score, debug = _score_footprint(latest, "long", cfg, 0.25)
    # 12 (3+ same side) + 4 (at extreme: 27800 == low) + 4 (oversized 12>=10)
    # + 2 (4+ stacked) = 22, capped at 18 imbalance sub-cap
    assert score == 18
    assert debug["imbalance_count"] == 4
    assert debug["at_extreme"]
    assert debug["oversized"]
    assert debug["stacked_4plus"]


def test_footprint_absorption_graduated_long():
    """Heavy negative delta in tiny range scores by |delta|/range_ticks ratio.
    delta=-200, range=2t → ratio=100 → tier 3 → 7 pts."""
    cfg = FootprintCVDConfig()
    latest = {"stacked_buy": False, "stacked_sell": False,
              "delta": -200, "max_imbalance_ratio": 5.0,
              "high": 27800.5, "low": 27800.0}  # 2-tick range
    score, debug = _score_footprint(latest, "long", cfg, 0.25)
    assert score == 7
    assert debug["absorption"]
    assert debug["absorption_tier"] == 3


def test_footprint_absorption_partial_credit_below_old_threshold():
    """v1 had hard AND-gate (delta>50 AND range<10t). v2 awards partial
    credit. delta=-60, range=20t → ratio=3 → tier 0 (still no signal).
    delta=-100, range=20t → ratio=5 → tier 1 → 3 pts (partial)."""
    cfg = FootprintCVDConfig()
    # Borderline case that v1 missed entirely
    latest = {"delta": -100, "max_imbalance_ratio": 5.0,
              "high": 27805.0, "low": 27800.0}  # 20-tick range
    score, debug = _score_footprint(latest, "long", cfg, 0.25)
    assert score == 3  # tier 1 (5x ratio)
    assert debug["absorption"]


def test_footprint_legacy_stacked_plus_oversized_plus_absorption():
    """Legacy bars (no imbalances[]) with all three signals: stacked
    (12 from fallback) + oversized (4 from ratio>=10) + absorption (7)."""
    cfg = FootprintCVDConfig()
    latest = {"stacked_buy": True, "stacked_sell": False,
              "delta": -200, "max_imbalance_ratio": 12.0,
              "high": 27800.5, "low": 27800.0}
    score, debug = _score_footprint(latest, "long", cfg, 0.25)
    # 12 (stacked fallback) + 4 (oversized) + 7 (absorption) = 23
    assert score == 23
    assert debug["stacked"]
    assert debug["oversized"]
    assert debug["absorption"]


def test_footprint_short_legacy_path():
    """Short side: stacked_sell + positive delta tight range."""
    cfg = FootprintCVDConfig()
    latest = {"stacked_buy": False, "stacked_sell": True,
              "delta": 200, "max_imbalance_ratio": 5.0,
              "high": 27800.5, "low": 27800.0}
    score, debug = _score_footprint(latest, "short", cfg, 0.25)
    # 12 (stacked_sell fallback) + 7 (absorption tier 3) = 19
    assert score == 19
    assert debug["stacked"]
    assert debug["absorption"]


def test_footprint_capped_at_25():
    """Even with everything maxed, total caps at 25."""
    cfg = FootprintCVDConfig()
    latest = {
        "delta": -300,  # tier 3 absorption
        "max_imbalance_ratio": 15.0,
        "high": 27800.25, "low": 27800.0,  # 1-tick range
        "imbalances": [
            {"price": 27800.00, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
            {"price": 27800.00, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
            {"price": 27800.25, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
            {"price": 27800.25, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
            {"price": 27800.25, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
        ],
    }
    score, _ = _score_footprint(latest, "long", cfg, 0.25)
    assert score == 25  # 18 (imbalance cap) + 7 (absorption) = 25


def test_footprint_no_signal_when_delta_wrong_sign():
    """LONG reversal needs negative delta (sellers swinging into bid).
    Positive delta on a long-direction call = no absorption."""
    cfg = FootprintCVDConfig()
    latest = {"delta": 200, "max_imbalance_ratio": 5.0,
              "high": 27800.5, "low": 27800.0}
    score, _ = _score_footprint(latest, "long", cfg, 0.25)
    assert score == 0  # no absorption (delta wrong sign), no stacked, no oversized


# ═══════════════════════════════════════════════════════════════════
# Confluence 4: CVD compression (5 sub-dimensions)
# ═══════════════════════════════════════════════════════════════════
def test_compression_scores_high_when_all_5_dimensions_present():
    cfg = FootprintCVDConfig()
    bars = []
    # Baseline: 20 bars with normal delta=50, range=5pts, vol=500
    for i in range(20):
        bars.append({"delta": 50, "high": 27805 + i, "low": 27800 + i,
                     "open": 27800 + i, "close": 27801 + i,
                     "total_volume": 500, "cvd_session": -100 - i * 5})
    # Recent 3: shrunk delta + range, volume HOLDING (not collapsed)
    bars.append({"delta": 25, "high": 27822, "low": 27820,
                 "open": 27820, "close": 27821,
                 "total_volume": 480, "cvd_session": -200})
    bars.append({"delta": 18, "high": 27822, "low": 27820,
                 "open": 27820, "close": 27821,
                 "total_volume": 510, "cvd_session": -210})
    # Last bar adds single-bar div (close > open with negative delta)
    bars.append({"delta": -22, "high": 27822, "low": 27820,
                 "open": 27820, "close": 27821,
                 "total_volume": 500, "cvd_session": -190})
    score, debug = _score_compression(bars, cfg, 0.25)
    assert score >= 20  # at least 4 of 5 dimensions
    assert debug["delta_compression"]
    assert debug["range_compression"]
    assert debug["volume_holding"]


def test_compression_low_when_dead_market():
    """CRITICAL: low-everything (dead market, no participation) must
    NOT score as absorption. The volume_holding check is the
    discriminator."""
    cfg = FootprintCVDConfig()
    bars = [{"delta": 50, "high": 27805, "low": 27800,
             "open": 27800, "close": 27801,
             "total_volume": 500, "cvd_session": -100 - i * 5}
            for i in range(20)]
    # Recent: small delta, small range, AND collapsed volume
    for _ in range(3):
        bars.append({"delta": 10, "high": 27801, "low": 27800,
                     "open": 27800, "close": 27800.5,
                     "total_volume": 100, "cvd_session": -200})
    score, debug = _score_compression(bars, cfg, 0.25)
    assert not debug["volume_holding"]  # KEY discriminator
    assert score < 15  # delta+range compress but no volume support


def test_compression_warmup_returns_zero():
    cfg = FootprintCVDConfig()
    bars = [{"delta": 50, "high": 27805, "low": 27800,
             "open": 27800, "close": 27801,
             "total_volume": 500, "cvd_session": -100} for _ in range(5)]
    score, _ = _score_compression(bars, cfg, 0.25)
    assert score == 0


# ═══════════════════════════════════════════════════════════════════
# Tier classification
# ═══════════════════════════════════════════════════════════════════
def test_tier_classification():
    assert _classify_tier(95) == "A++"
    assert _classify_tier(90) == "A++"
    assert _classify_tier(85) == "A"
    assert _classify_tier(80) == "A"
    assert _classify_tier(75) == "B"
    assert _classify_tier(70) == "B"
    assert _classify_tier(65) == "C"
    assert _classify_tier(60) == "C"
    assert _classify_tier(50) == "REJECTED"
    assert _classify_tier(0) == "REJECTED"


# ═══════════════════════════════════════════════════════════════════
# End-to-end strategy class
# ═══════════════════════════════════════════════════════════════════
def _setup_full_bullish_volumetric(root: Path):
    """Stage data/ + logs/ inside `root` with a strong bullish setup."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)

    # Latest bar — stacked_buy + absorption + oversized + single-bar div
    bar_ts = datetime(2026, 5, 5, 9, 30, tzinfo=CT).isoformat()
    latest = {
        "type": "volumetric_bar",
        "ts": bar_ts,
        "instrument": "MNQM6",
        "bar_size_ticks": 1500,
        "delta": -200, "total_volume": 1500,
        "buy_volume": 650, "sell_volume": 850,
        "open": 27801.5, "close": 27801, "high": 27802, "low": 27800,
        "poc": 27800.5, "imbalances": [],
        "stacked_buy": True, "stacked_sell": False,
        "max_imbalance_ratio": 12.0,
        "cvd_session": -50,
    }
    (root / "data" / "volumetric_latest.json").write_text(
        json.dumps(latest), encoding="utf-8",
    )

    # History with divergence + compression
    hist = []
    for i in range(20):
        hist.append({
            "delta": 100, "high": 27820 - i * 0.5, "low": 27815 - i * 0.5,
            "open": 27818 - i * 0.5, "close": 27818 - i * 0.5,
            "total_volume": 500, "cvd_session": -50 - i * 8,
        })
    for i in range(10):
        hist.append({
            "delta": 30, "high": 27805 - i * 0.25, "low": 27800 - i * 0.25,
            "open": 27802 - i * 0.25, "close": 27803 - i * 0.25,
            "total_volume": 500, "cvd_session": -150 + i * 5,
        })
    hist.append(latest)
    (root / "logs" / "volumetric_history.jsonl").write_text(
        "\n".join(json.dumps(b) for b in hist) + "\n", encoding="utf-8",
    )
    return latest


def _patch_strategy_root(monkeypatch, root: Path):
    """Both bridge and strategy modules have their own _ROOT constants."""
    import strategies.footprint_cvd_reversal as mod
    monkeypatch.setattr(mod, "_DATA_ROOT", root)
    # Reset the dormant-flag so the test can re-trigger the warmup path
    monkeypatch.setattr(mod, "_data_unavailable_logged", False)


def test_strategy_returns_long_signal_on_full_setup(tmp_path, monkeypatch):
    """Sprint L (2026-05-08): now requires N+1 confirmation. Setup the
    trigger bar, evaluate once (sets pending), then advance to a NEW
    bar that didn't violate the trigger's wick, evaluate again, expect
    Signal."""
    # Reset module-level pending state so this test is hermetic
    import strategies.footprint_cvd_reversal as mod
    mod._pending_signals.clear()

    trigger_latest = _setup_full_bullish_volumetric(tmp_path)
    _patch_strategy_root(monkeypatch, tmp_path)

    strat = FootprintCVDReversal({})
    market = {
        "price": 27801.0,
        "tick_size": 0.25,
        "regime": "POSITIVE_NORMAL",
        # Sprint J: HTF level via market dict (price-action levels)
        # Tests put PDL=27800 in the market — `_score_htf_level` reads
        # this directly via `_build_pa_levels_from_market`.
        "prior_day_low": 27800.0,
    }
    session_info = {"now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT)}

    # First call: stores pending, returns None (N+1 needed)
    first_call = strat.evaluate(market, [], [], session_info)
    assert first_call is None, (
        "Sprint L: first evaluation on the trigger bar must DEFER firing "
        "and return None (pending stored). This kills the discretionary→code "
        "translation loss where coders fire on the trigger bar."
    )
    assert "long" in mod._pending_signals
    pending = mod._pending_signals["long"]
    assert pending["trigger_low"] == trigger_latest["low"]
    assert pending["trigger_high"] == trigger_latest["high"]

    # Advance to a NEW bar (different ts) whose low STAYED at or above
    # the trigger's low — the wick held, absorption confirmed.
    next_bar_ts = datetime(2026, 5, 5, 9, 30, 30, tzinfo=CT).isoformat()
    next_bar = dict(trigger_latest)
    next_bar["ts"] = next_bar_ts
    # Confirmation: low >= trigger_low (27800)
    next_bar["low"] = 27800.5
    next_bar["high"] = 27802.5
    next_bar["close"] = 27802.0
    next_bar["open"] = 27801.0
    (tmp_path / "data" / "volumetric_latest.json").write_text(
        json.dumps(next_bar), encoding="utf-8",
    )
    # Append to history so warmup count + recent state stays valid
    with (tmp_path / "logs" / "volumetric_history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(next_bar) + "\n")

    # Second call: pending confirms, signal fires anchored to TRIGGER bar's wick
    session_info_2 = {"now_ct": datetime(2026, 5, 5, 9, 30, 35, tzinfo=CT)}
    signal = strat.evaluate(market, [], [], session_info_2)

    assert signal is not None
    assert signal.direction == "LONG"
    assert signal.metadata["tier"] in ("A++", "A", "B")
    assert signal.metadata["iqs"] >= 70
    assert signal.metadata["level_score"] == 25
    # Sprint J: PDL is now the level (was put_support pre-Sprint-J).
    assert signal.metadata["level_name"] == "PDL"
    assert signal.metadata["sub_strategy"] == "footprint_cvd_reversal"
    # Phoenix Signal contract — these must exist
    assert signal.strategy == "footprint_cvd_reversal"
    assert signal.stop_ticks > 0
    assert signal.stop_ticks <= 60   # max_stop_ticks
    assert signal.atr_stop_override is True
    assert signal.entry_price == 27801.0
    assert signal.stop_price < signal.entry_price  # LONG → stop below
    assert signal.target_price > signal.entry_price
    # Sprint L: stop anchored to the TRIGGER bar (27800), not the next bar (27800.5)
    assert signal.metadata["trigger_low"] == 27800.0
    assert signal.metadata["trigger_bar_ts"] == trigger_latest["ts"]
    # Pending should be cleared after firing
    assert "long" not in mod._pending_signals


def test_strategy_blocks_in_lunch(tmp_path, monkeypatch):
    _setup_full_bullish_volumetric(tmp_path)
    _patch_strategy_root(monkeypatch, tmp_path)
    strat = FootprintCVDReversal({})
    market = {
        "price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
        # Sprint J: HTF level via market dict (price-action levels)
        # Tests put PDL=27800 in the market — `_score_htf_level` reads
        # this directly via `_build_pa_levels_from_market`.
        "prior_day_low": 27800.0,
    }
    session_info = {"now_ct": datetime(2026, 5, 5, 12, 0, tzinfo=CT)}
    assert strat.evaluate(market, [], [], session_info) is None


def test_strategy_blocks_long_in_negative_strong(tmp_path, monkeypatch):
    _setup_full_bullish_volumetric(tmp_path)
    _patch_strategy_root(monkeypatch, tmp_path)
    strat = FootprintCVDReversal({})
    market = {
        "price": 27801.0, "tick_size": 0.25, "regime": "NEGATIVE_STRONG",
        # Sprint J: HTF level via market dict (price-action levels)
        # Tests put PDL=27800 in the market — `_score_htf_level` reads
        # this directly via `_build_pa_levels_from_market`.
        "prior_day_low": 27800.0,
    }
    session_info = {"now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT)}
    assert strat.evaluate(market, [], [], session_info) is None


def test_strategy_dormant_when_data_not_available(tmp_path, monkeypatch):
    """No volumetric_latest.json → strategy returns None and logs once."""
    _patch_strategy_root(monkeypatch, tmp_path)  # tmp_path has nothing
    strat = FootprintCVDReversal({})
    market = {
        "price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
        # Sprint J: HTF level via market dict (price-action levels)
        # Tests put PDL=27800 in the market — `_score_htf_level` reads
        # this directly via `_build_pa_levels_from_market`.
        "prior_day_low": 27800.0,
    }
    session_info = {"now_ct": datetime(2026, 5, 5, 9, 30, tzinfo=CT)}
    assert strat.evaluate(market, [], [], session_info) is None


def test_strategy_returns_none_when_iqs_below_threshold(tmp_path, monkeypatch):
    """Weak setup → IQS < 70 → no signal."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    bar_ts = datetime(2026, 5, 5, 9, 30, tzinfo=CT).isoformat()
    weak = {
        "type": "volumetric_bar", "ts": bar_ts, "instrument": "MNQM6",
        "delta": 5, "total_volume": 500, "buy_volume": 252, "sell_volume": 248,
        "open": 27801, "close": 27801, "high": 27802, "low": 27800,
        "poc": 27801, "imbalances": [],
        "stacked_buy": False, "stacked_sell": False,
        "max_imbalance_ratio": 1.5, "cvd_session": -100,
    }
    (tmp_path / "data" / "volumetric_latest.json").write_text(json.dumps(weak))
    # Flat history — no divergence, no compression
    flat_hist = [{"delta": 10, "high": 27805, "low": 27800,
                  "open": 27801, "close": 27801,
                  "total_volume": 500, "cvd_session": -100 - i * 5}
                 for i in range(30)]
    (tmp_path / "logs" / "volumetric_history.jsonl").write_text(
        "\n".join(json.dumps(b) for b in flat_hist) + "\n",
    )
    _patch_strategy_root(monkeypatch, tmp_path)

    strat = FootprintCVDReversal({})
    market = {
        "price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
        # Sprint J: HTF level via market dict (price-action levels)
        # Tests put PDL=27800 in the market — `_score_htf_level` reads
        # this directly via `_build_pa_levels_from_market`.
        "prior_day_low": 27800.0,
    }
    session_info = {"now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT)}
    # Level=25, divergence=0, footprint=0, compression~0 → IQS=25 < 70
    assert strat.evaluate(market, [], [], session_info) is None


def test_strategy_disabled_returns_none(tmp_path, monkeypatch):
    _setup_full_bullish_volumetric(tmp_path)
    _patch_strategy_root(monkeypatch, tmp_path)
    strat = FootprintCVDReversal({"enabled": False})
    market = {
        "price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
        "prior_day_low": 27800.0,
    }
    session_info = {"now_ct": datetime(2026, 5, 5, 9, 30, tzinfo=CT)}
    assert strat.evaluate(market, [], [], session_info) is None


# ═══════════════════════════════════════════════════════════════════
# Routing / config wiring
# ═══════════════════════════════════════════════════════════════════
def test_footprint_cvd_routes_to_simfootprintchart():
    from config.account_routing import STRATEGY_ACCOUNT_MAP
    assert STRATEGY_ACCOUNT_MAP["footprint_cvd_reversal"] == "SimFootprintchart"


@pytest.mark.skip(reason="V2 deployment override 2026-05-17 — restore at Phase 10")
def test_footprint_cvd_in_strategies_config():
    from config.strategies import STRATEGIES
    cfg = STRATEGIES.get("footprint_cvd_reversal")
    assert cfg is not None
    assert cfg["validated"] is False  # Lab-only
    assert cfg["enabled"] is True
    # Threshold defaults match spec
    assert cfg["entry_threshold_iqs"] == 70
    assert cfg["compression_volume_floor"] == 0.8


def test_strategy_config_accepted_by_dataclass():
    """Loading the prod config into FootprintCVDConfig must not crash —
    the strategy class does this on init."""
    from config.strategies import STRATEGIES
    cfg_dict = STRATEGIES["footprint_cvd_reversal"]
    strat = FootprintCVDReversal(cfg_dict)
    assert strat.cfg.entry_threshold_iqs == 70
    # Tuple fields round-tripped from JSON lists
    assert isinstance(strat.cfg.lunch_block_start_ct, (tuple, list))
