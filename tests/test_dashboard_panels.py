"""Sprint K2 — tests for dashboard/panels.py data builders.

The HTML/JS rendering itself is hard to unit-test, but the data
preparation must be deterministic and handle missing fields
gracefully.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard.panels import (
    _classify_tape,
    build_bias_panel_data,
    build_tape_reader_panel_data,
)


# ═══════════════════════════════════════════════════════════════════
# build_bias_panel_data — 3-column synthesis
# ═══════════════════════════════════════════════════════════════════
class TestBiasPanel:
    def test_empty_state_returns_neutral(self):
        out = build_bias_panel_data({})
        assert out["structure"]["verdict"] == "NEUTRAL"
        assert out["momentum"]["verdict"] == "NEUTRAL"
        assert out["tape"]["verdict"] == "NEUTRAL"
        assert out["overall"]["verdict"] == "NEUTRAL"

    def test_none_state_returns_neutral(self):
        out = build_bias_panel_data(None)
        assert out["overall"]["verdict"] == "NEUTRAL"

    def test_strong_bullish_when_all_three_align(self):
        out = build_bias_panel_data({
            "structure_bias": "BULLISH",
            "cvd_delta_5": 350,
            "tape_read": {
                "stacked_buy": True, "cvd_divergence": "",
                "trapped_traders": "", "finished_auction": False,
            },
        })
        assert out["structure"]["verdict"] == "BULLISH"
        assert out["momentum"]["verdict"] == "BULLISH"
        assert out["tape"]["verdict"] == "BULLISH"
        assert out["overall"]["verdict"] == "STRONG_BULLISH"

    def test_strong_bearish_when_all_three_align(self):
        out = build_bias_panel_data({
            "structure_bias": "BEARISH",
            "cvd_delta_5": -350,
            "tape_read": {
                "stacked_sell": True, "cvd_divergence": "",
                "trapped_traders": "", "finished_auction": False,
            },
        })
        assert out["overall"]["verdict"] == "STRONG_BEARISH"

    def test_moderate_bullish_when_2_of_3(self):
        """STRUCTURE bullish + MOMENTUM bullish + TAPE neutral = MODERATE."""
        out = build_bias_panel_data({
            "structure_bias": "BULLISH",
            "cvd_delta_5": 350,
            # No tape_read → tape verdict NEUTRAL
        })
        assert out["overall"]["verdict"] == "MODERATE_BULLISH"

    def test_mixed_when_columns_disagree(self):
        out = build_bias_panel_data({
            "structure_bias": "BULLISH",
            "cvd_delta_5": -350,  # BEARISH momentum
        })
        assert out["overall"]["verdict"] == "MIXED"

    def test_momentum_threshold_at_200(self):
        """CVD delta at exactly 200 is NOT directional (need > 200)."""
        out = build_bias_panel_data({
            "structure_bias": "NEUTRAL",
            "cvd_delta_5": 200,
        })
        assert out["momentum"]["verdict"] == "NEUTRAL"

    def test_momentum_threshold_above_200(self):
        out = build_bias_panel_data({
            "structure_bias": "NEUTRAL",
            "cvd_delta_5": 201,
        })
        assert out["momentum"]["verdict"] == "BULLISH"

    def test_momentum_no_data_neutral(self):
        out = build_bias_panel_data({"structure_bias": "NEUTRAL"})
        assert out["momentum"]["verdict"] == "NEUTRAL"
        assert "no recent CVD data" in out["momentum"]["reasons"][0]


# ═══════════════════════════════════════════════════════════════════
# _classify_tape — internal helper
# ═══════════════════════════════════════════════════════════════════
class TestClassifyTape:
    def test_empty_tape_neutral(self):
        verdict, _ = _classify_tape({})
        assert verdict == "NEUTRAL"

    def test_stacked_buy_alone_bullish(self):
        verdict, _ = _classify_tape({"stacked_buy": True})
        assert verdict == "BULLISH"

    def test_stacked_sell_alone_bearish(self):
        verdict, _ = _classify_tape({"stacked_sell": True})
        assert verdict == "BEARISH"

    def test_bullish_div_bullish(self):
        verdict, _ = _classify_tape({"cvd_divergence": "BULLISH_DIV"})
        assert verdict == "BULLISH"

    def test_bearish_div_bearish(self):
        verdict, _ = _classify_tape({"cvd_divergence": "BEARISH_DIV"})
        assert verdict == "BEARISH"

    def test_shorts_trapped_bullish(self):
        verdict, _ = _classify_tape({"trapped_traders": "shorts_trapped"})
        assert verdict == "BULLISH"

    def test_finished_auction_with_long_fire_bullish(self):
        verdict, _ = _classify_tape({
            "finished_auction": True, "fire_direction": "LONG",
        })
        assert verdict == "BULLISH"

    def test_balanced_signals_neutral(self):
        """1 bullish signal + 1 bearish signal → NEUTRAL."""
        verdict, _ = _classify_tape({
            "stacked_buy": True, "stacked_sell": True,
        })
        assert verdict == "NEUTRAL"


# ═══════════════════════════════════════════════════════════════════
# build_tape_reader_panel_data — file reader
# ═══════════════════════════════════════════════════════════════════
class TestTapeReaderPanel:
    def test_no_file_returns_unavailable(self, tmp_path):
        result = build_tape_reader_panel_data(root=tmp_path)
        assert result["available"] is False
        assert "message" in result

    def test_corrupt_file_returns_unavailable(self, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "tape_read_latest.json").write_text("not valid json")
        result = build_tape_reader_panel_data(root=tmp_path)
        assert result["available"] is False
        assert "error" in result["message"].lower()

    def test_full_event_shapes_correctly(self, tmp_path):
        (tmp_path / "data").mkdir()
        event = {
            "ts": "2026-05-05T09:30:00-05:00",
            "structure_bias": "BULLISH",
            "iqs_score": 85,
            "iqs_breakdown": {"L": 25, "D": 20, "F": 18, "C": 12, "bonus": 10},
            "nearest_htf_level": "PDL",
            "absorption_detected": True,
            "stacked_buy": True,
            "stacked_sell": False,
            "cvd_divergence": "BULLISH_DIV",
            "finished_auction": True,
            "trapped_traders": "shorts_trapped",
            "would_fire": True,
            "fire_direction": "LONG",
            "tier": "A",
            "bar_ts": "2026-05-05T09:30:00-05:00",
        }
        (tmp_path / "data" / "tape_read_latest.json").write_text(json.dumps(event))

        result = build_tape_reader_panel_data(root=tmp_path)
        assert result["available"] is True
        assert result["iqs_score"] == 85
        assert result["nearest_htf_level"] == "PDL"
        assert result["would_fire"] is True
        assert result["fire_direction"] == "LONG"
        assert result["tier"] == "A"

        # Patterns list shape
        assert isinstance(result["patterns"], list)
        labels = [p["label"] for p in result["patterns"]]
        assert "Absorption" in labels
        assert any("Stacked buy" in lbl for lbl in labels)

        # Active flags should match event state
        for p in result["patterns"]:
            if p["label"] == "Absorption":
                assert p["active"] is True
            if p["label"] == "Finished auction":
                assert p["active"] is True

    def test_inactive_patterns_marked_off(self, tmp_path):
        """When a pattern isn't active, it should still appear in the list
        but with active=False — so the dashboard can show it grayed out."""
        (tmp_path / "data").mkdir()
        empty_event = {
            "ts": "2026-05-05T09:30:00-05:00",
            "structure_bias": "NEUTRAL",
            "iqs_score": 25,
            "iqs_breakdown": {"L": 25, "D": 0, "F": 0, "C": 0, "bonus": 0},
            "nearest_htf_level": "PDL",
            "absorption_detected": False,
            "stacked_buy": False,
            "stacked_sell": False,
            "cvd_divergence": "",
            "finished_auction": False,
            "trapped_traders": "",
            "would_fire": False,
            "fire_direction": "",
            "tier": "REJECTED",
            "bar_ts": "2026-05-05T09:30:00-05:00",
        }
        (tmp_path / "data" / "tape_read_latest.json").write_text(
            json.dumps(empty_event),
        )
        result = build_tape_reader_panel_data(root=tmp_path)
        # All patterns should be inactive
        active_count = sum(1 for p in result["patterns"] if p["active"])
        assert active_count == 0
        assert result["would_fire"] is False
