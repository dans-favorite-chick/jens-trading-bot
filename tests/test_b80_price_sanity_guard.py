"""B80 — price sanity guard on OIF builders.

A stop at $100 on a ~$27000 market (seen in chart 2026-04-22) was the last
straw. Any nonsense price — tick-count used as price, $-loss used as price,
parse offset error, default 0.0 sneaking through — must be refused at the
narrowest choke (the three OIF line builders) before it reaches NT8.

Bounds: MNQ plausible range 10000-50000 (current ~27000, decades headroom).
"""
from __future__ import annotations

import pytest

from bridge.oif_writer import (
    _build_stop_line,
    _build_target_line,
    _build_entry_line,
    MNQ_PRICE_MIN,
    MNQ_PRICE_MAX,
)


class TestStopLineGuard:
    def test_rejects_tiny_stop_price(self):
        """The exact bug: stop at 100 on a 27000 market."""
        with pytest.raises(RuntimeError, match="PRICE_SANITY"):
            _build_stop_line("SELL", 1, stop_price=100.0, account="Sim101")

    def test_rejects_zero_stop_price(self):
        with pytest.raises(RuntimeError, match="PRICE_SANITY"):
            _build_stop_line("SELL", 1, stop_price=0.0, account="Sim101")

    def test_rejects_negative_stop_price(self):
        with pytest.raises(RuntimeError, match="PRICE_SANITY"):
            _build_stop_line("SELL", 1, stop_price=-27000.0, account="Sim101")

    def test_rejects_huge_stop_price(self):
        with pytest.raises(RuntimeError, match="PRICE_SANITY"):
            _build_stop_line("SELL", 1, stop_price=100_000.0, account="Sim101")

    def test_accepts_realistic_mnq_stop(self):
        line = _build_stop_line("SELL", 1, stop_price=27003.00, account="Sim101")
        assert "STOPMARKET" in line and "27003.00" in line


class TestTargetLineGuard:
    def test_rejects_tiny_target(self):
        with pytest.raises(RuntimeError, match="PRICE_SANITY"):
            _build_target_line("SELL", 1, target_price=100.0, account="Sim101")

    def test_accepts_realistic_target(self):
        line = _build_target_line("SELL", 1, target_price=27174.00, account="Sim101")
        assert "LIMIT" in line and "27174.00" in line


class TestEntryLineGuard:
    def test_stopmarket_entry_rejects_tiny_stop(self):
        with pytest.raises(RuntimeError, match="PRICE_SANITY"):
            _build_entry_line(
                "BUY", 1, "STOPMARKET",
                limit_price=0.0, stop_price=100.0, account="Sim101",
            )

    def test_limit_entry_rejects_tiny_limit(self):
        with pytest.raises(RuntimeError, match="PRICE_SANITY"):
            _build_entry_line(
                "BUY", 1, "LIMIT",
                limit_price=100.0, stop_price=0.0, account="Sim101",
            )

    def test_market_entry_bypasses_guard(self):
        """MARKET orders carry no prices — guard must not fire."""
        line = _build_entry_line(
            "BUY", 1, "MARKET",
            limit_price=0.0, stop_price=0.0, account="Sim101",
        )
        assert ";MARKET;0;0;" in line


class TestBoundsAreAdvertised:
    def test_bounds_are_module_level_constants(self):
        """Ops should be able to widen bounds without hunting for magic numbers."""
        assert MNQ_PRICE_MIN < 27000 < MNQ_PRICE_MAX
