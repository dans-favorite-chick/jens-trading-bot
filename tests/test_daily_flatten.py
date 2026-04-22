"""Tests for bots/daily_flatten.py — B84 flatten timing (15:54 CT).

Prior eras:
- Pre-B83: flattened at 16:00 CT (CME globex pause) — orders queued past break.
- B83 interim: 15:58 CT (2-min runway before break).
- B84 (current): 15:54 CT, one minute ahead of NT8 Auto Close at 15:55 CT.

The new default lives in config.settings.DAILY_FLATTEN_HOUR_CT /
DAILY_FLATTEN_MINUTE_CT and is picked up automatically.
"""
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


# ═══════════════════════════════════════════════════════════════════
# should_flatten_now — new default is 15:54 CT
# ═══════════════════════════════════════════════════════════════════
def test_15_53_is_too_early():
    assert should_flatten_now(_ct(2026, 4, 21, 15, 53), None) is False


def test_15_54_first_fire():
    """B84: new default fires at 15:54 CT."""
    assert should_flatten_now(_ct(2026, 4, 21, 15, 54), None) is True


def test_15_54_same_day_no_refire():
    assert should_flatten_now(_ct(2026, 4, 21, 15, 54), date(2026, 4, 21)) is False


def test_15_54_next_day_refires():
    assert should_flatten_now(_ct(2026, 4, 22, 15, 54), date(2026, 4, 21)) is True


def test_15_58_still_fires_under_new_default():
    """Any time >= 15:54 passes the gate."""
    assert should_flatten_now(_ct(2026, 4, 21, 15, 58), None) is True


def test_explicit_16_00_kwargs_preserves_back_compat():
    """Callers that explicitly want the old 16:00 gate should still get it."""
    assert should_flatten_now(
        _ct(2026, 4, 21, 15, 59), None, flatten_hour=16, flatten_minute=0,
    ) is False
    assert should_flatten_now(
        _ct(2026, 4, 21, 16, 0), None, flatten_hour=16, flatten_minute=0,
    ) is True


# ═══════════════════════════════════════════════════════════════════
# Stubs
# ═══════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════
# DailyFlattener.check_and_flatten — new default is 15:54 CT
# ═══════════════════════════════════════════════════════════════════
def test_no_positions_is_noop():
    pm = FakePM([])
    log = FakeLogger()
    f = DailyFlattener(pm, logger=log)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 54)))
    assert n == 0
    assert pm.closed == []
    assert f.last_flatten_date == date(2026, 4, 21)


def test_single_position_closes_at_1554():
    """B84: reason encodes the fire time as HHMM so trade_memory consumers
    can distinguish pre-B83 / B83 / B84 closes."""
    pm = FakePM([FakePosition("T1", 200.5)])
    log = FakeLogger()
    f = DailyFlattener(pm, logger=log)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 54)))
    assert n == 1
    assert pm.closed[0]["trade_id"] == "T1"
    assert pm.closed[0]["reason"] == "daily_flatten_1554CT"
    assert pm.closed[0]["price"] == 200.5
    assert f.last_flatten_date == date(2026, 4, 21)
    assert any("daily_flatten fired" in m for m in log.infos)


def test_three_positions_all_close():
    pm = FakePM([FakePosition(f"T{i}") for i in range(3)])
    f = DailyFlattener(pm)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 54)))
    assert n == 3
    assert [c["trade_id"] for c in pm.closed] == ["T0", "T1", "T2"]


def test_called_twice_same_day_fires_once():
    pm = FakePM([FakePosition("T1")])
    f = DailyFlattener(pm)
    n1 = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 54)))
    pm.active_positions.append(FakePosition("T2"))
    n2 = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 16, 5)))
    assert n1 == 1
    assert n2 == 0
    assert len(pm.closed) == 1


def test_called_next_day_fires_again():
    pm = FakePM([FakePosition("T1")])
    f = DailyFlattener(pm)
    asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 54)))
    pm.active_positions.append(FakePosition("T2"))
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 22, 15, 54)))
    assert n == 2
    assert f.last_flatten_date == date(2026, 4, 22)


def test_too_early_does_not_fire_at_1553():
    """B84 edge: 15:53 is still too early under the new default
    (matches the NO_NEW_ENTRIES gate one minute before flatten)."""
    pm = FakePM([FakePosition("T1")])
    f = DailyFlattener(pm)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 53)))
    assert n == 0
    assert pm.closed == []
    assert f.last_flatten_date is None


def test_websocket_send_fn_used_when_provided():
    pm = FakePM([FakePosition("T1"), FakePosition("T2")])
    sent = []

    async def ws_send(trade_id, reason):
        sent.append((trade_id, reason))

    f = DailyFlattener(pm, websocket_send_fn=ws_send)
    n = asyncio.run(f.check_and_flatten(_ct(2026, 4, 21, 15, 54)))
    assert n == 2
    assert sent == [
        ("T1", "daily_flatten_1554CT"),
        ("T2", "daily_flatten_1554CT"),
    ]
    assert pm.closed == []


def test_timezone_correctness_ct_vs_pt():
    """15:54 in CT is the same instant as 13:54 in PT."""
    ct_time = _ct(2026, 4, 21, 15, 54)
    pt_equiv = ct_time.astimezone(PT)
    assert pt_equiv.hour == 13
    assert pt_equiv.minute == 54
    assert CT.key == "America/Chicago"
    assert should_flatten_now(ct_time, None) is True


# ═══════════════════════════════════════════════════════════════════
# B84: explicit tests for the defense-in-depth architecture
# ═══════════════════════════════════════════════════════════════════
def test_flatten_fires_at_15_54_ct():
    """Named per B84 spec — pins the canonical fire-moment."""
    assert should_flatten_now(_ct(2026, 4, 22, 15, 54), None) is True
    # And 15:53:59 is still early.
    assert should_flatten_now(_ct(2026, 4, 22, 15, 53), None) is False


def test_fires_1min_before_nt8_auto_close_safety_net():
    """Phoenix primary at 15:54 CT; NT8 Auto Close safety net at 15:55 CT.
    Phoenix must have fired by the time NT8's watchdog activates."""
    ct_primary = _ct(2026, 4, 21, 15, 54)
    ct_nt8_auto_close = _ct(2026, 4, 21, 15, 55)
    gap_s = (ct_nt8_auto_close - ct_primary).total_seconds()
    assert gap_s == 60
    # Primary fires at 15:54, safety-net window opens at 15:55.
    assert should_flatten_now(ct_primary, None) is True


def test_flattener_records_last_flatten_metadata():
    """B84 grace-window watcher reads last_flatten_fired_at_ct and
    last_flatten_trade_ids — pin the contract."""
    pm = FakePM([FakePosition("T1"), FakePosition("T2")])
    f = DailyFlattener(pm)
    fired_at = _ct(2026, 4, 21, 15, 54)
    asyncio.run(f.check_and_flatten(fired_at))
    assert f.last_flatten_fired_at_ct == fired_at
    assert set(f.last_flatten_trade_ids) == {"T1", "T2"}
