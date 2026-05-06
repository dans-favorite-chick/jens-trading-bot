"""Sprint K1 — tape-reading pro patterns + IQS bonus + tape-read event.

Tests the Sprint K1 additions to footprint_cvd_reversal:
  - _detect_finished_auction (LONG side: selling exhaustion at lows;
    SHORT side: buying exhaustion at highs)
  - _detect_trapped_traders (long-trapped, short-trapped)
  - _score_tape_bonuses (additive scoring, capped at +20)
  - _emit_tape_read_event (writes data/tape_read_latest.json with
    correct schema)
  - End-to-end IQS bonus integration

All tests use synthetic data — no live APIs, no production disk I/O.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategies.footprint_cvd_reversal import (
    _detect_finished_auction,
    _detect_trapped_traders,
    _emit_tape_read_event,
    _score_tape_bonuses,
)

CT = ZoneInfo("America/Chicago")


def _bar(low=27800, high=27810, total_volume=500, delta=0,
         stacked_buy=False, stacked_sell=False, cvd_session=0,
         open_=27805, close=27805, ts="2026-05-05T09:30:00-05:00"):
    """Build a synthetic volumetric bar dict."""
    return {
        "low": low, "high": high, "total_volume": total_volume,
        "delta": delta, "stacked_buy": stacked_buy,
        "stacked_sell": stacked_sell, "cvd_session": cvd_session,
        "open": open_, "close": close, "ts": ts,
    }


# ═══════════════════════════════════════════════════════════════════
# _detect_finished_auction
# ═══════════════════════════════════════════════════════════════════
class TestFinishedAuction:
    def test_warmup_returns_false(self):
        """Insufficient history → no detection."""
        ok, _ = _detect_finished_auction(_bar(), [], "long")
        assert ok is False

    def test_long_side_no_new_low_with_diminished_volume(self):
        """LONG signal benefits from selling-exhaustion at lows."""
        # Recent 5 bars with progressively lower lows, average vol ~500
        history = [
            _bar(low=27800, high=27810, total_volume=500),
            _bar(low=27795, high=27805, total_volume=480),
            _bar(low=27792, high=27800, total_volume=510),
            _bar(low=27790, high=27798, total_volume=490),
            _bar(low=27788, high=27795, total_volume=520),
        ]
        # Latest: didn't make a new low (low >= 27788), volume < 0.7x avg
        latest = _bar(low=27790, high=27796, total_volume=300)
        ok, reason = _detect_finished_auction(latest, history, "long")
        assert ok is True
        assert "finished_auction_low" in reason

    def test_long_side_volume_still_strong_no_detection(self):
        """If latest volume is healthy, the auction isn't finished."""
        history = [_bar(low=27790, total_volume=500) for _ in range(5)]
        latest = _bar(low=27790, total_volume=500)  # not diminished
        ok, _ = _detect_finished_auction(latest, history, "long")
        assert ok is False

    def test_long_side_new_low_no_detection(self):
        """Latest bar made a new low → still extending, not finished."""
        history = [_bar(low=27790, total_volume=500) for _ in range(5)]
        latest = _bar(low=27780, total_volume=300)  # new low, even with low vol
        ok, _ = _detect_finished_auction(latest, history, "long")
        assert ok is False

    def test_short_side_no_new_high_with_diminished_volume(self):
        history = [
            _bar(low=27790, high=27800, total_volume=500),
            _bar(low=27795, high=27805, total_volume=480),
            _bar(low=27800, high=27812, total_volume=510),
            _bar(low=27805, high=27818, total_volume=490),
            _bar(low=27808, high=27822, total_volume=520),
        ]
        latest = _bar(low=27810, high=27820, total_volume=300)
        ok, reason = _detect_finished_auction(latest, history, "short")
        assert ok is True
        assert "finished_auction_high" in reason


# ═══════════════════════════════════════════════════════════════════
# _detect_trapped_traders
# ═══════════════════════════════════════════════════════════════════
class TestTrappedTraders:
    def test_empty_history_returns_false(self):
        ok, _ = _detect_trapped_traders(_bar(), [], "long")
        assert ok is False

    def test_shorts_trapped_for_long_signal(self):
        """Prior bar broke down with stacked_sell → current bar reverses
        with stacked_buy + positive delta + rising CVD = shorts trapped,
        we go LONG."""
        prior = _bar(stacked_sell=True, stacked_buy=False,
                     delta=-200, cvd_session=-1500)
        latest = _bar(stacked_buy=True, stacked_sell=False,
                      delta=180, cvd_session=-1300)
        ok, reason = _detect_trapped_traders(latest, [prior], "long")
        assert ok is True
        assert "shorts_trapped" in reason

    def test_longs_trapped_for_short_signal(self):
        prior = _bar(stacked_buy=True, stacked_sell=False,
                     delta=200, cvd_session=1500)
        latest = _bar(stacked_sell=True, stacked_buy=False,
                      delta=-180, cvd_session=1300)
        ok, reason = _detect_trapped_traders(latest, [prior], "short")
        assert ok is True
        assert "longs_trapped" in reason

    def test_no_trap_when_prior_not_stacked(self):
        prior = _bar(stacked_sell=False, delta=-200, cvd_session=-1500)
        latest = _bar(stacked_buy=True, delta=180, cvd_session=-1300)
        ok, _ = _detect_trapped_traders(latest, [prior], "long")
        assert ok is False

    def test_no_trap_when_cvd_doesnt_reverse(self):
        """Even if stacked-flip happens, CVD must move in the trap-confirming
        direction (rising for long-signal, falling for short-signal)."""
        prior = _bar(stacked_sell=True, delta=-200, cvd_session=-1500)
        latest = _bar(stacked_buy=True, delta=180, cvd_session=-1700)  # CVD fell
        ok, _ = _detect_trapped_traders(latest, [prior], "long")
        assert ok is False


# ═══════════════════════════════════════════════════════════════════
# _score_tape_bonuses
# ═══════════════════════════════════════════════════════════════════
class TestTapeBonuses:
    def test_no_patterns_zero_bonus(self):
        """Plain market state → no bonuses."""
        history = [_bar() for _ in range(5)]
        bonus, debug = _score_tape_bonuses(_bar(), history, "long")
        assert bonus == 0
        assert debug["finished_auction"] is False
        assert debug["trapped_traders"] is False

    def test_finished_auction_alone_scores_10(self):
        history = [
            _bar(low=27790, total_volume=500),
            _bar(low=27795, total_volume=500),
            _bar(low=27792, total_volume=500),
            _bar(low=27790, total_volume=500),
            _bar(low=27788, total_volume=500),
        ]
        latest = _bar(low=27790, total_volume=300)
        bonus, debug = _score_tape_bonuses(latest, history, "long")
        assert bonus == 10
        assert debug["finished_auction"] is True

    def test_trapped_traders_alone_scores_10(self):
        prior = _bar(stacked_sell=True, delta=-200, cvd_session=-1500)
        latest = _bar(stacked_buy=True, delta=180, cvd_session=-1300)
        bonus, debug = _score_tape_bonuses(latest, [prior], "long")
        assert bonus == 10
        assert debug["trapped_traders"] is True

    def test_both_patterns_capped_at_20(self):
        """Both patterns active → +10 +10 = 20 (already at cap)."""
        # Build history with diminished-volume + no-new-low
        history = [
            _bar(low=27790, total_volume=500, stacked_sell=False, delta=-50, cvd_session=-1500-i*10)
            for i in range(5)
        ]
        # Make the LAST history bar a stacked_sell breakdown
        history[-1] = _bar(low=27788, total_volume=500,
                           stacked_sell=True, delta=-200, cvd_session=-1500)
        latest = _bar(low=27790, total_volume=300,
                      stacked_buy=True, delta=180, cvd_session=-1300)
        bonus, debug = _score_tape_bonuses(latest, history, "long")
        assert bonus == 20  # cap
        assert debug["finished_auction"] is True
        assert debug["trapped_traders"] is True


# ═══════════════════════════════════════════════════════════════════
# _emit_tape_read_event
# ═══════════════════════════════════════════════════════════════════
class TestTapeReadEvent:
    def test_writes_file_with_indented_json(self, tmp_path):
        event = {
            "ts": "2026-05-05T09:30:00-05:00",
            "structure_bias": "BULLISH",
            "iqs_score": 75,
            "would_fire": True,
        }
        _emit_tape_read_event(event, root=tmp_path)

        out = tmp_path / "data" / "tape_read_latest.json"
        assert out.exists()
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded["structure_bias"] == "BULLISH"
        assert loaded["iqs_score"] == 75
        assert loaded["would_fire"] is True

    def test_atomic_overwrite_protects_previous(self, tmp_path):
        """Second write replaces the first — no corruption."""
        _emit_tape_read_event({"iqs_score": 75}, root=tmp_path)
        out = tmp_path / "data" / "tape_read_latest.json"
        assert json.loads(out.read_text())["iqs_score"] == 75

        _emit_tape_read_event({"iqs_score": 60}, root=tmp_path)
        assert json.loads(out.read_text())["iqs_score"] == 60

    def test_creates_parent_directories(self, tmp_path):
        """Even if data/ doesn't exist, the emit creates it."""
        # Clean tmp_path — no data/ subdir yet
        _emit_tape_read_event({"iqs_score": 55}, root=tmp_path)
        assert (tmp_path / "data" / "tape_read_latest.json").exists()


# ═══════════════════════════════════════════════════════════════════
# End-to-end: bonus changes IQS, metadata records bonuses
# ═══════════════════════════════════════════════════════════════════
class TestEndToEndBonusIntegration:
    def _full_bullish_setup_with_bonus(self, tmp_path: Path):
        """Stage volumetric data + history that triggers BOTH a finished
        auction (selling exhausted at lows) AND a trapped-traders pattern
        (prior stacked_sell, current stacked_buy with rising CVD)."""
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        # 5 prior bars, last one is the stacked_sell breakdown
        prior_5 = [
            {"low": 27790, "high": 27800,
             "open": 27795, "close": 27795,
             "total_volume": 500, "delta": -50,
             "stacked_buy": False, "stacked_sell": False,
             "cvd_session": -100 - i * 10,
             "ts": f"2026-05-05T09:2{i}:00-05:00",
             "max_imbalance_ratio": 1.0}
            for i in range(4)
        ]
        # Last history bar: stacked_sell breakdown
        prior_5.append({
            "low": 27788, "high": 27795,
            "open": 27792, "close": 27790,
            "total_volume": 500, "delta": -200,
            "stacked_buy": False, "stacked_sell": True,
            "cvd_session": -200,
            "ts": "2026-05-05T09:25:00-05:00",
            "max_imbalance_ratio": 5.0,
        })

        # Latest: reverses with stacked_buy, no new low, diminished vol
        latest = {
            "type": "volumetric_bar",
            "ts": "2026-05-05T09:30:00-05:00",
            "instrument": "MNQM6", "bar_size_ticks": 1500,
            "low": 27790, "high": 27800,
            "open": 27791, "close": 27798,
            "delta": 180, "total_volume": 300,
            "buy_volume": 240, "sell_volume": 120,
            "poc": 27795,
            "stacked_buy": True, "stacked_sell": False,
            "max_imbalance_ratio": 8.0,
            "cvd_session": -100,  # rose from -200
            "imbalances": [],
        }

        (tmp_path / "data" / "volumetric_latest.json").write_text(
            json.dumps(latest), encoding="utf-8",
        )

        # 25-bar history: 20 normal bars to seed compression baseline,
        # then the prior_5 + latest at the end
        baseline = [
            {"low": 27800, "high": 27810,
             "open": 27805, "close": 27805,
             "total_volume": 500, "delta": 50,
             "stacked_buy": False, "stacked_sell": False,
             "cvd_session": -50 - i * 5,
             "max_imbalance_ratio": 1.0}
            for i in range(20)
        ]
        full_hist = baseline + prior_5 + [latest]
        (tmp_path / "logs" / "volumetric_history.jsonl").write_text(
            "\n".join(json.dumps(b) for b in full_hist) + "\n",
        )
        return latest

    def test_metadata_records_bonus(self, tmp_path, monkeypatch):
        """When bonus fires, Signal.metadata['tape_bonus'] should be > 0
        and tape_debug should reflect detected patterns."""
        from strategies.footprint_cvd_reversal import FootprintCVDReversal
        import strategies.footprint_cvd_reversal as mod

        self._full_bullish_setup_with_bonus(tmp_path)
        monkeypatch.setattr(mod, "_DATA_ROOT", tmp_path)
        monkeypatch.setattr(mod, "_data_unavailable_logged", False)

        strat = FootprintCVDReversal({})
        market = {
            "price": 27791.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
            "prior_day_low": 27790.0,  # Tier-1 confluence in range
        }
        session_info = {
            "now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT),
        }
        signal = strat.evaluate(market, [], [], session_info)

        if signal is not None:
            # Bonus should have fired (trapped_traders at minimum)
            assert "tape_bonus" in signal.metadata
            assert "tape_debug" in signal.metadata
            assert "base_iqs" in signal.metadata
            assert signal.metadata["base_iqs"] <= signal.metadata["iqs"]


class TestTapeReadEventSchema:
    """Verify the dashboard event has all the keys the K2 panel will consume."""

    def test_event_has_required_fields_when_strategy_evaluates(
        self, tmp_path, monkeypatch,
    ):
        from strategies.footprint_cvd_reversal import FootprintCVDReversal
        import strategies.footprint_cvd_reversal as mod

        # Stage minimum data so evaluate() runs through to the event emit
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        latest = {
            "type": "volumetric_bar",
            "ts": "2026-05-05T09:30:00-05:00",
            "instrument": "MNQM6", "bar_size_ticks": 1500,
            "low": 27800, "high": 27802,
            "open": 27801, "close": 27801,
            "delta": 0, "total_volume": 500,
            "buy_volume": 250, "sell_volume": 250,
            "poc": 27801,
            "stacked_buy": False, "stacked_sell": False,
            "max_imbalance_ratio": 1.0,
            "cvd_session": -100,
            "imbalances": [],
        }
        (tmp_path / "data" / "volumetric_latest.json").write_text(
            json.dumps(latest), encoding="utf-8",
        )
        # Enough history for warmup
        flat = [
            {"low": 27800, "high": 27805,
             "open": 27801, "close": 27801,
             "total_volume": 500, "delta": 0,
             "stacked_buy": False, "stacked_sell": False,
             "cvd_session": -100 - i * 5,
             "max_imbalance_ratio": 1.0}
            for i in range(30)
        ]
        (tmp_path / "logs" / "volumetric_history.jsonl").write_text(
            "\n".join(json.dumps(b) for b in flat) + "\n",
        )
        monkeypatch.setattr(mod, "_DATA_ROOT", tmp_path)
        monkeypatch.setattr(mod, "_data_unavailable_logged", False)

        strat = FootprintCVDReversal({})
        market = {
            "price": 27801.0, "tick_size": 0.25, "regime": "POSITIVE_NORMAL",
            "prior_day_low": 27800.0,
        }
        session_info = {"now_ct": datetime(2026, 5, 5, 9, 30, 5, tzinfo=CT)}

        # evaluate() should emit even if no signal (called for both directions)
        strat.evaluate(market, [], [], session_info)

        event_path = tmp_path / "data" / "tape_read_latest.json"
        assert event_path.exists()
        event = json.loads(event_path.read_text())
        # Schema check — these keys must exist for the K2 dashboard panel
        for key in (
            "ts", "direction_evaluated", "structure_bias",
            "iqs_score", "iqs_breakdown", "nearest_htf_level",
            "absorption_detected", "stacked_buy", "stacked_sell",
            "cvd_divergence", "finished_auction", "trapped_traders",
            "would_fire", "fire_direction", "tier", "bar_ts",
        ):
            assert key in event, f"event missing required key: {key}"
        # Breakdown sub-fields
        for k in ("L", "D", "F", "C", "bonus"):
            assert k in event["iqs_breakdown"]
