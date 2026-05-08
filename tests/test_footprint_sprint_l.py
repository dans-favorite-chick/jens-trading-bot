"""Sprint L (2026-05-08) — Footprint + CVD enhancements.

Tests four research-backed enhancements applied to footprint_cvd_reversal:

  #1 Per-level imbalance graduated scoring  (test_footprint_cvd_reversal.py)
  #2 N+1 confirmation gate                  (this file)
  #3 Wick + ATR stop anchoring               (this file)
  #5 Pattern × level-confluence multiplier  (this file)

The discretionary→code translation losses these address:
  - Coders fire on the trigger bar; pros wait one bar to confirm the wick held.
  - Pure ATR stops drift through structure; pure wick stops can sit too far
    in volatile regimes. Wick + ATR-buffer is the consensus.
  - Levels are categorical multipliers, not continuous additions: 50-IQS at
    3-level confluence > 75-IQS at 1-level.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import strategies.footprint_cvd_reversal as mod
from strategies.footprint_cvd_reversal import (
    FootprintCVDReversal,
    _check_pending_confirmation,
    _compute_atr_from_history,
    _count_extra_htf_confluences,
)

CT = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def reset_module_state():
    """Pending state is module-global. Reset before each test."""
    mod._pending_signals.clear()
    mod._data_unavailable_logged = False
    yield
    mod._pending_signals.clear()


# ═══════════════════════════════════════════════════════════════════
# #5 — Pattern × level-confluence multiplier
# ═══════════════════════════════════════════════════════════════════
class TestExtraHTFConfluences:
    def test_no_levels_within_buffer(self):
        market = {"prior_day_high": 28000.0, "prior_day_low": 27500.0}
        extra, labels = _count_extra_htf_confluences(
            price=27800.0, market=market,
            buffer_ticks=8, tick_size=0.25,
        )
        assert extra == 0
        assert labels == []

    def test_one_level_in_buffer_zero_extra(self):
        market = {"prior_day_low": 27800.0, "prior_day_high": 28500.0}
        extra, labels = _count_extra_htf_confluences(
            price=27800.5, market=market,
            buffer_ticks=8, tick_size=0.25,
        )
        assert extra == 0  # 1 level total, 0 "extra"
        assert "PDL" in labels

    def test_three_level_confluence(self):
        market = {
            "prior_day_low": 27800.0,
            "session_poc": 27800.5,
            "vwap": 27801.0,
            "prior_day_high": 28500.0,  # outside buffer
        }
        extra, labels = _count_extra_htf_confluences(
            price=27800.5, market=market,
            buffer_ticks=8, tick_size=0.25,
        )
        assert extra == 2  # 3 levels - 1 = 2 extra
        assert set(labels) == {"PDL", "POC", "VWAP"}

    def test_dedup_levels_within_one_tick(self):
        """PDL at 27800 and POC at 27800 should count as ONE level."""
        market = {
            "prior_day_low": 27800.0,
            "session_poc": 27800.0,
        }
        extra, labels = _count_extra_htf_confluences(
            price=27800.5, market=market,
            buffer_ticks=8, tick_size=0.25,
        )
        assert extra == 0  # deduplicated to 1 unique level


# ═══════════════════════════════════════════════════════════════════
# #2 — N+1 confirmation gate
# ═══════════════════════════════════════════════════════════════════
class TestPendingConfirmation:
    def test_no_pending_returns_NONE(self):
        action, reason = _check_pending_confirmation(
            "long",
            latest={"ts": "2026-05-08T09:30:00", "low": 27800, "high": 27801},
            bars_history=[],
        )
        assert action is None
        assert reason == "NONE"

    def test_same_bar_returns_WAIT(self):
        mod._pending_signals["long"] = {
            "trigger_ts": "2026-05-08T09:30:00",
            "trigger_low": 27800.0,
            "trigger_high": 27801.0,
        }
        action, reason = _check_pending_confirmation(
            "long",
            latest={"ts": "2026-05-08T09:30:00", "low": 27800.5, "high": 27801.5},
            bars_history=[],
        )
        assert action is None
        assert reason == "WAIT"

    def test_long_confirms_when_low_held(self):
        mod._pending_signals["long"] = {
            "trigger_ts": "2026-05-08T09:30:00",
            "trigger_low": 27800.0,
            "trigger_high": 27801.0,
        }
        action, reason = _check_pending_confirmation(
            "long",
            # New bar — low STAYED above trigger's low
            latest={"ts": "2026-05-08T09:30:30", "low": 27800.5, "high": 27802.0},
            bars_history=[],
        )
        assert action is not None
        assert reason == "FIRE"
        assert action["trigger_low"] == 27800.0

    def test_long_discards_when_low_violated(self):
        mod._pending_signals["long"] = {
            "trigger_ts": "2026-05-08T09:30:00",
            "trigger_low": 27800.0,
            "trigger_high": 27801.0,
        }
        action, reason = _check_pending_confirmation(
            "long",
            # New bar — low BROKE trigger's low (absorption failed)
            latest={"ts": "2026-05-08T09:30:30", "low": 27799.5, "high": 27801.0},
            bars_history=[],
        )
        assert action is None
        assert reason == "DISCARD_VIOLATED"

    def test_short_confirms_when_high_held(self):
        mod._pending_signals["short"] = {
            "trigger_ts": "2026-05-08T09:30:00",
            "trigger_low": 27800.0,
            "trigger_high": 27801.0,
        }
        action, reason = _check_pending_confirmation(
            "short",
            latest={"ts": "2026-05-08T09:30:30", "low": 27799.0, "high": 27800.5},
            bars_history=[],
        )
        assert action is not None
        assert reason == "FIRE"

    def test_short_discards_when_high_violated(self):
        mod._pending_signals["short"] = {
            "trigger_ts": "2026-05-08T09:30:00",
            "trigger_low": 27800.0,
            "trigger_high": 27801.0,
        }
        action, reason = _check_pending_confirmation(
            "short",
            latest={"ts": "2026-05-08T09:30:30", "low": 27800.0, "high": 27801.5},
            bars_history=[],
        )
        assert action is None
        assert reason == "DISCARD_VIOLATED"

    def test_independent_directions(self):
        """LONG pending and SHORT pending are tracked independently —
        a SHORT setup shouldn't affect a LONG pending."""
        mod._pending_signals["long"] = {
            "trigger_ts": "2026-05-08T09:30:00",
            "trigger_low": 27800.0,
            "trigger_high": 27801.0,
        }
        # Confirming SHORT shouldn't touch LONG pending
        latest = {"ts": "2026-05-08T09:30:30", "low": 27800.5, "high": 27801.5}
        action, reason = _check_pending_confirmation("short", latest, [])
        assert reason == "NONE"
        assert "long" in mod._pending_signals  # untouched


# ═══════════════════════════════════════════════════════════════════
# #3 — Wick + ATR stop anchoring
# ═══════════════════════════════════════════════════════════════════
class TestATRComputation:
    def test_empty_history_returns_zero(self):
        assert _compute_atr_from_history([], tick_size=0.25) == 0.0

    def test_atr_in_ticks(self):
        bars = [
            {"high": 27805.0, "low": 27800.0},  # 5 pts = 20 ticks
            {"high": 27810.0, "low": 27802.5},  # 7.5 pts = 30 ticks
            {"high": 27808.0, "low": 27803.0},  # 5 pts = 20 ticks
        ]
        atr = _compute_atr_from_history(bars, tick_size=0.25, period=14)
        # mean = (20 + 30 + 20) / 3 = ~23.3 ticks
        assert 23 < atr < 24

    def test_atr_period_caps_history(self):
        """Only the last `period` bars are used."""
        bars = [{"high": 1, "low": 0}] * 100  # 4 ticks each
        atr = _compute_atr_from_history(bars, tick_size=0.25, period=14)
        assert atr == pytest.approx(4.0)


# ═══════════════════════════════════════════════════════════════════
# Integration — wick + ATR buffer end-to-end
# ═══════════════════════════════════════════════════════════════════
def _stage_volumetric_setup(root: Path, stacked_buy=True, oversized=True,
                             abs_delta=-200, hist_count=30, with_imbalances=True):
    """Stage a maximally-bullish volumetric setup at PDL=27800."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)

    bar_ts = datetime(2026, 5, 5, 9, 30, tzinfo=CT).isoformat()
    imbalances = [
        {"price": 27800.00, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
        {"price": 27800.25, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
        {"price": 27800.50, "bid_vol": 5, "ask_vol": 50, "ratio": 10.0, "side": "buy"},
    ] if with_imbalances else []

    latest = {
        "type": "volumetric_bar",
        "ts": bar_ts,
        "instrument": "MNQM6",
        "bar_size_ticks": 1500,
        "delta": abs_delta, "total_volume": 1500,
        "buy_volume": 650, "sell_volume": 850,
        "open": 27801.5, "close": 27801, "high": 27802, "low": 27800,
        "poc": 27800.5, "imbalances": imbalances,
        "stacked_buy": stacked_buy, "stacked_sell": False,
        "max_imbalance_ratio": 12.0 if oversized else 5.0,
        "cvd_session": -50,
    }
    (root / "data" / "volumetric_latest.json").write_text(
        json.dumps(latest), encoding="utf-8",
    )

    hist = []
    for i in range(20):
        hist.append({
            "ts": f"2026-05-05T09:0{i//6}:{i%6:02d}",
            "delta": 100, "high": 27820 - i * 0.5, "low": 27815 - i * 0.5,
            "open": 27818 - i * 0.5, "close": 27818 - i * 0.5,
            "total_volume": 500, "cvd_session": -50 - i * 8,
        })
    for i in range(hist_count - 20):
        hist.append({
            "ts": f"2026-05-05T09:2{i // 6}:{i%6:02d}",
            "delta": 30, "high": 27805 - i * 0.25, "low": 27800 - i * 0.25,
            "open": 27802 - i * 0.25, "close": 27803 - i * 0.25,
            "total_volume": 500, "cvd_session": -150 + i * 5,
        })
    hist.append(latest)
    (root / "logs" / "volumetric_history.jsonl").write_text(
        "\n".join(json.dumps(b) for b in hist) + "\n", encoding="utf-8",
    )
    return latest


def _patch_root(monkeypatch, root):
    monkeypatch.setattr(mod, "_DATA_ROOT", root)


class TestStopAnchoringIntegration:
    def test_stop_anchored_to_trigger_bar_wick_with_atr_buffer(
        self, tmp_path, monkeypatch,
    ):
        """Sprint L: stop should be anchored to TRIGGER bar's wick (low for
        long), with buffer = max(stop_buffer_ticks, 0.3 × ATR)."""
        trigger = _stage_volumetric_setup(tmp_path)
        _patch_root(monkeypatch, tmp_path)
        strat = FootprintCVDReversal({})
        market = {"price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
                  "prior_day_low": 27800.0}
        session = {"now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT)}

        # First call — sets pending
        first = strat.evaluate(market, [], [], session)
        assert first is None

        # Advance to a confirming bar
        next_bar = dict(trigger)
        next_bar["ts"] = datetime(2026, 5, 5, 9, 30, 30, tzinfo=CT).isoformat()
        next_bar["low"] = 27800.5  # held the trigger's 27800 low
        next_bar["high"] = 27802.5
        (tmp_path / "data" / "volumetric_latest.json").write_text(
            json.dumps(next_bar), encoding="utf-8",
        )
        with (tmp_path / "logs" / "volumetric_history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(next_bar) + "\n")

        session_2 = {"now_ct": datetime(2026, 5, 5, 9, 30, 35, tzinfo=CT)}
        signal = strat.evaluate(market, [], [], session_2)
        assert signal is not None
        # Stop anchored to TRIGGER bar's low (27800), not next bar's low (27800.5)
        assert signal.metadata["trigger_low"] == 27800.0
        assert signal.stop_price < 27800.0  # below the wick + buffer
        # Buffer should be at least stop_buffer_ticks * tick_size
        assert signal.metadata["atr_buffer_ticks"] >= 4.0  # default stop_buffer_ticks


class TestNPlusOneGateRejectsOnViolation:
    def test_pending_discarded_when_next_bar_violates_wick(
        self, tmp_path, monkeypatch,
    ):
        """If the next bar's low BREAKS the trigger bar's low, the
        pending signal must be discarded — absorption failed."""
        trigger = _stage_volumetric_setup(tmp_path)
        _patch_root(monkeypatch, tmp_path)
        strat = FootprintCVDReversal({})
        market = {"price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
                  "prior_day_low": 27800.0}
        session = {"now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT)}

        # First call: sets pending
        assert strat.evaluate(market, [], [], session) is None
        assert "long" in mod._pending_signals

        # Next bar violates trigger's low
        next_bar = dict(trigger)
        next_bar["ts"] = datetime(2026, 5, 5, 9, 30, 30, tzinfo=CT).isoformat()
        next_bar["low"] = 27799.5  # BROKE 27800
        next_bar["high"] = 27801.0
        (tmp_path / "data" / "volumetric_latest.json").write_text(
            json.dumps(next_bar), encoding="utf-8",
        )
        with (tmp_path / "logs" / "volumetric_history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(next_bar) + "\n")

        session_2 = {"now_ct": datetime(2026, 5, 5, 9, 30, 35, tzinfo=CT)}
        signal = strat.evaluate(market, [], [], session_2)
        assert signal is None  # Discarded; absorption failed
        # Pending should be cleared
        assert "long" not in mod._pending_signals


class TestConfluenceMultiplier:
    def test_multiplier_lifts_marginal_setup_at_multi_level_confluence(
        self, tmp_path, monkeypatch,
    ):
        """Two-level confluence (PDL + POC at same price) should lift
        IQS via the multiplier so the same pattern that failed at one
        level passes here."""
        # Setup with weaker signal — only stacked_buy, no absorption
        trigger = _stage_volumetric_setup(
            tmp_path, stacked_buy=True, oversized=False, abs_delta=10,
            with_imbalances=False,
        )
        _patch_root(monkeypatch, tmp_path)
        strat = FootprintCVDReversal({})
        # TWO levels at the bar's vicinity: PDL=27800 + POC=27800
        market = {
            "price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
            "prior_day_low": 27800.0,
            "session_poc": 27800.0,
            "vwap": 27801.0,  # third level
        }
        session = {"now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT)}

        # First call sets pending. We're checking that the multiplier
        # is applied — which makes the IQS qualify for pending storage.
        assert strat.evaluate(market, [], [], session) is None

        # Pending should be stored only if IQS hit threshold on bar N
        # (i.e., the multiplier did its job).
        assert "long" in mod._pending_signals, (
            "Sprint L #5: with 3-level confluence (PDL + POC at same node "
            "+ VWAP), the multiplier should lift a marginal pattern over "
            "the threshold and store a pending signal."
        )
        pending = mod._pending_signals["long"]
        assert pending["extra_levels"] >= 1  # at least 1 extra
        assert pending["confluence_multiplier"] >= 1.25


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
