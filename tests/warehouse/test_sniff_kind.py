# tests/warehouse/test_sniff_kind.py
"""Tests for the CSV kind sniffer.

The real implementation exports sniff_csv_kind as an alias for sniff_kind.
"""
from __future__ import annotations
from tools.warehouse.sniff import sniff_csv_kind


def test_legacy_trades(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_trades.csv")
    assert kind == "trades"


def test_extended_trades(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_trades_extended.csv")
    assert kind == "trades"


def test_wfa_windows(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_wfa_windows.csv")
    assert kind == "wfa_windows"


def test_wfa_summary(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_wfa_summary.csv")
    assert kind == "wfa_summary"


def test_summary(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_summary.csv")
    assert kind == "summary"


def test_mixed(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_mixed.csv")
    assert kind == "mixed"


def test_derived_by_filename(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "phase1_strategy_summary_sample.csv")
    assert kind == "derived"


def test_unknown(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_unknown.csv")
    assert kind == "error"
