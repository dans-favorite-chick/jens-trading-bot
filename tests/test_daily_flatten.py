"""Tests for bots/daily_flatten.py."""
import asyncio
import sys
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bots.daily_flatten import should_flatten_now, DailyFlattener, CT

PT = ZoneInfo("America/Los_Angeles")


def _ct(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CT)


# ---------- should_flatten_now ----------
#
# B83 (2026-04-22): default flatten time moved 16:00 → 15:58 CT to give
# the EXIT MARKET orders a 2-minute transit+fill runway before the CME
# globex maintenance break closes the book at 16:00. These tests pin
# the new defaults; the prior 16:00 expectations are kept as explicit-
# kwarg tests so regression guards still cover the old-default shape.

def test_15_57_is_too_early():
    assert should_flatten_now(_ct(2026, 4, 21, 15, 57), None) is False


def test_15_58_first_fire():
    """B83: new default fires at 15:58 CT, NOT 16:00."""
    assert should_flatten_now(_ct(2026, 4, 21, 15, 58), None) is True


def test_15_58_same_day_no_refire():
    assert should_flatten_now(_ct(2026, 4, 21, 15, 58), date(2026, 4, 21)) is False


def test_15_58_next_day_refires():
    assert should_flatten_now(_ct(2026, 4, 22, 15, 58), date(2026, 4, 21)) is True


def test_16_00_still_fires_under_new_default():
    """15:58-default gate lets 16:00 through too — useful parity check
    with the old 16:00-default behaviour."""
    assert should_flatten_now(_ct(2026, 4, 21, 16, 0), None) is True


def test_16_01_first_of_day():
    assert should_flatten_now(_ct(2026, 4, 21, 16, 1), None) is True


def test_explicit_16_00_kwargs_preserves_old_behavior():
    """Back-compat: callers that explicitly want the old 16:00 gate
    should still get it by passing kwargs."""
    # Old 16:00 gate: 15:59 too early, 16:00 fires
    assert should_flatten_now(
        _ct(2026, 4, 21, 15, 59), None, flatten_hour=16, flatten_minute=0,
    ) is False
    assert should_flatten_now(
        _ct(2026, 4, 21, 16, 0), None, flatten_hour=16, flatten_minute=0,
    ) is True


# ---------- Stubs ----------

class FakePosition:
    def __init__(self, trade_id, price=100.0):
        self.trade_id = trade_id
        self.last_known_price = price


class FakePM:
    def __init__(self, positions=None):
        self.active_positions = positions if positions is not None else []
        self.closed = []

    def close_position(self, price, reason, trade_id=None):
        self.closed.append({"price": price, "reason": reason, "trade_id": trade_id})


class FakeLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def info(self, msg):
        self.infos.append(msg)

    def error(self, msg):
        self.errors.append(msg)


# ---------- DailyFlattener.check_and_flatten ----------

def test_no_positions_is_noop():
    pm = FakePM([])
    log = FakeLogger()
    f = DailyFlattener(pm, logger=log)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 58)))
    assert n == 0
    assert pm.closed == []
    assert f.last_flatten_date == date(2026, 4, 21)


def test_single_position_closes_at_1558():
    """B83: default fires at 15:58 CT with reason='daily_flatten_1558CT'."""
    pm = FakePM([FakePosition("T1", 200.5)])
    log = FakeLogger()
    f = DailyFlattener(pm, logger=log)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 58)))
    assert n == 1
    assert len(pm.closed) == 1
    assert pm.closed[0]["trade_id"] == "T1"
    assert pm.closed[0]["reason"] == "daily_flatten_1558CT"
    assert pm.closed[0]["price"] == 200.5
    assert f.last_flatten_date == date(2026, 4, 21)
    assert any("daily_flatten fired" in m for m in log.infos)


def test_three_positions_all_close():
    pm = FakePM([FakePosition(f"T{i}") for i in range(3)])
    f = DailyFlattener(pm)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 58)))
    assert n == 3
    assert [c["trade_id"] for c in pm.closed] == ["T0", "T1", "T2"]


def test_called_twice_same_day_fires_once():
    pm = FakePM([FakePosition("T1")])
    f = DailyFlattener(pm)
    n1 = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 58)))
    # Add a new position afterwards; second call same day should still no-op
    pm.active_positions.append(FakePosition("T2"))
    n2 = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 16, 5)))
    assert n1 == 1
    assert n2 == 0
    assert len(pm.closed) == 1


def test_called_next_day_fires_again():
    pm = FakePM([FakePosition("T1")])
    f = DailyFlattener(pm)
    asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 58)))
    pm.active_positions.append(FakePosition("T2"))
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 22, 15, 58)))
    assert n == 2  # both T1 (still in list) and T2 get closed
    assert f.last_flatten_date == date(2026, 4, 22)


def test_too_early_does_not_fire_at_1557():
    """B83 edge: 15:57 is still too early under the new default."""
    pm = FakePM([FakePosition("T1")])
    f = DailyFlattener(pm)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 57)))
    assert n == 0
    assert pm.closed == []
    assert f.last_flatten_date is None


def test_websocket_send_fn_used_when_provided():
    pm = FakePM([FakePosition("T1"), FakePosition("T2")])
    sent = []

    async def ws_send(trade_id, reason):
        sent.append((trade_id, reason))

    f = DailyFlattener(pm, websocket_send_fn=ws_send)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 58)))
    assert n == 2
    assert sent == [
        ("T1", "daily_flatten_1558CT"),
        ("T2", "daily_flatten_1558CT"),
    ]
    # When ws_send provided, pm.close_position should not be called
    assert pm.closed == []


def test_timezone_correctness_ct_vs_pt():
    """15:58 in CT is the same instant as 13:58 in PT."""
    ct_time = _ct(2026, 4, 21, 15, 58)
    pt_equiv = ct_time.astimezone(PT)
    assert pt_equiv.hour == 13
    assert pt_equiv.minute == 58
    # And the CT default tz attached in module is America/Chicago
    assert CT.key == "America/Chicago"
    # should_flatten_now uses wall-clock .time() of whatever tz the dt carries,
    # so a CT-aware dt at 15:58 should fire under the new default.
    assert should_flatten_now(ct_time, None) is True


# ---------- B83: runway between 15:58 fire and 16:00 globex pause ----------

def test_fires_with_2min_runway_before_maintenance_break():
    """At 15:58:00, there are exactly 2 minutes until the 16:00 CME
    maintenance break. An EXIT MARKET submitted now has time to transit
    bridge → NT8 ATI → CME gateway and fill BEFORE the book closes."""
    pm = FakePM([FakePosition("T1")])
    f = DailyFlattener(pm)
    fire_at = _ct(2026, 4, 21, 15, 58)
    n = asyncio.run(f.check_and_flatten(fire_at))
    assert n == 1
    # The gap between fire time and 16:00 is the safety margin.
    maintenance = _ct(2026, 4, 21, 16, 0)
    runway_s = (maintenance - fire_at).total_seconds()
    assert runway_s == 120, f"expected 120s runway, got {runway_s}"
