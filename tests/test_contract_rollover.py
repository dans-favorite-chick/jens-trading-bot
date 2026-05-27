"""Tests for core/contract_rollover.py — P2-3 auto-roll machinery
(2026-05-24).

Covers the existing get_active_contract() classification AND the new
auto-flatten / settings-swap / persistence path.

All tests use:
  - a fake clock (no real datetime.now())
  - a fake PositionManager (no OIF writes, no NT8)
  - a tmp_path settings.py copy (no edits to the real config/settings.py)
  - a tmp roll_state.json (no edits to the real logs/roll_state.json)

Run:
  cd "C:\\Trading Project\\phoenix_bot"
  python -m pytest tests/test_contract_rollover.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The module under test
from core import contract_rollover as cr
from core.contract_rollover import (
    CT,
    flatten_for_roll,
    get_active_contract,
    is_no_new_entries_for_roll,
    is_roll_day,
    is_roll_window,
    is_t_minus_15_pre_roll,
    load_roll_state,
    log_rollover_status,
    mark_rolled,
    swap_instrument_in_settings,
)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════
class FakePosition:
    def __init__(self, trade_id: str, price: float = 19_500.25, contracts: int = 1):
        self.trade_id = trade_id
        self.last_known_price = price
        self.contracts = contracts
        self.account = "Sim101"
        self.sub_strategy = None


class FakePM:
    """Mock PositionManager — exposes .active_positions list and a
    .close_position() sink. Records every close call for assertions."""

    def __init__(self, positions=None):
        self.active_positions = positions if positions is not None else []
        self.closed: list[dict] = []

    def close_position(self, exit_price, exit_reason, trade_id=None):
        self.closed.append({
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "trade_id": trade_id,
        })


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect the module-level ROLL_STATE_FILE to a per-test tmp file."""
    p = tmp_path / "roll_state.json"
    monkeypatch.setattr(cr, "ROLL_STATE_FILE", str(p))
    return str(p)


@pytest.fixture
def tmp_settings(tmp_path):
    """A throwaway copy of the INSTRUMENT lines from settings.py."""
    p = tmp_path / "settings.py"
    p.write_text(
        'INSTRUMENT = "MNQM6"                 # comment trails\n'
        'CONTRACT_EXPIRATION = "2026-06-19"\n'
        'NEXT_CONTRACT = "MNQU6 09-26"\n'
        'NEXT_CONTRACT_EXPIRATION = "2026-09-18"\n'
        "OTHER = 1\n",
        encoding="utf-8",
    )
    return str(p)


def _ct(y, m, d, hh=12, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=CT)


# ═══════════════════════════════════════════════════════════════════
# get_active_contract — preserved behavior
# ═══════════════════════════════════════════════════════════════════
def test_normal_trading_uses_front_month():
    info = get_active_contract(today=date(2026, 4, 17))
    assert info["symbol"] == "MNQM6"
    assert info["should_roll"] is False
    assert info["warning"] is None


def test_roll_window_triggers_switch():
    info = get_active_contract(today=date(2026, 6, 11))   # ~6 trading days out
    assert info["symbol"] == "MNQU6 09-26"
    assert info["should_roll"] is True
    assert info["warning"] is not None


def test_post_expiration_uses_next():
    info = get_active_contract(today=date(2026, 7, 1))
    assert info["symbol"] == "MNQU6 09-26"
    assert info["should_roll"] is False  # already past expiry → no "rolling"


# ═══════════════════════════════════════════════════════════════════
# Predicates
# ═══════════════════════════════════════════════════════════════════
def test_is_roll_window_far_out_is_false():
    assert is_roll_window(today=date(2026, 4, 17)) is False


def test_is_roll_window_inside_window_is_true():
    assert is_roll_window(today=date(2026, 6, 11)) is True


def test_is_roll_day_only_on_expiration():
    assert is_roll_day(today=date(2026, 6, 19)) is True
    assert is_roll_day(today=date(2026, 6, 18)) is False
    assert is_roll_day(today=date(2026, 6, 20)) is False


def test_t_minus_15_window_pre_and_post():
    # Roll day, 15:44:59 → False
    assert is_t_minus_15_pre_roll(_ct(2026, 6, 19, 15, 44, 59)) is False
    # Roll day, 15:45:00 → True
    assert is_t_minus_15_pre_roll(_ct(2026, 6, 19, 15, 45, 0)) is True
    # Roll day, 15:59:59 → True
    assert is_t_minus_15_pre_roll(_ct(2026, 6, 19, 15, 59, 59)) is True
    # Roll day, 16:00:00 → False (window closes)
    assert is_t_minus_15_pre_roll(_ct(2026, 6, 19, 16, 0, 0)) is False
    # NOT roll day, even at 15:50 → False
    assert is_t_minus_15_pre_roll(_ct(2026, 6, 18, 15, 50, 0)) is False


def test_is_no_new_entries_for_roll_on_roll_day_after_T15(tmp_state):
    # 15:45 on roll day → blocked
    assert is_no_new_entries_for_roll(_ct(2026, 6, 19, 15, 45)) is True
    # 15:44 on roll day → allowed
    assert is_no_new_entries_for_roll(_ct(2026, 6, 19, 15, 44)) is False
    # 12:00 on a non-roll day with no state → allowed
    assert is_no_new_entries_for_roll(_ct(2026, 6, 18, 12, 0)) is False


def test_is_no_new_entries_for_roll_after_state_marked_blocks_until_1700(tmp_state):
    # Pretend the bot already marked today as rolled
    mark_rolled(when=date(2026, 6, 19), rolled_from="MNQM6", rolled_to="MNQU6",
                path=tmp_state)
    # 16:30 same day → still blocked (state says rolled, before 17:00)
    assert is_no_new_entries_for_roll(_ct(2026, 6, 19, 16, 30)) is True
    # 17:00 same day → unblocked (next globex session)
    assert is_no_new_entries_for_roll(_ct(2026, 6, 19, 17, 0)) is False
    # 09:00 NEXT day → unblocked (state was for 6-19, today is 6-20)
    assert is_no_new_entries_for_roll(_ct(2026, 6, 20, 9, 0)) is False


# ═══════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════
def test_mark_rolled_writes_state_atomically(tmp_state):
    payload = mark_rolled(when=date(2026, 6, 19), rolled_from="MNQM6",
                          rolled_to="MNQU6", path=tmp_state)
    assert payload["last_roll_date"] == "2026-06-19"
    assert payload["rolled_from"] == "MNQM6"
    assert payload["rolled_to"] == "MNQU6"

    on_disk = json.loads(Path(tmp_state).read_text(encoding="utf-8"))
    assert on_disk == payload


def test_mark_rolled_is_idempotent_same_day(tmp_state):
    a = mark_rolled(when=date(2026, 6, 19), path=tmp_state)
    b = mark_rolled(when=date(2026, 6, 19), path=tmp_state)
    assert a == b  # same-day re-mark is no-op


def test_load_roll_state_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "ROLL_STATE_FILE", str(tmp_path / "absent.json"))
    assert load_roll_state() == {}


def test_load_roll_state_corrupt_file_returns_empty(tmp_state):
    Path(tmp_state).write_text("{not json", encoding="utf-8")
    assert load_roll_state(tmp_state) == {}


# ═══════════════════════════════════════════════════════════════════
# Settings swap — regex single-line replacement, atomic write
# ═══════════════════════════════════════════════════════════════════
def test_swap_instrument_replaces_only_instrument_line(tmp_settings):
    changed, msg = swap_instrument_in_settings("MNQU6", settings_path=tmp_settings)
    assert changed is True
    after = Path(tmp_settings).read_text(encoding="utf-8")
    # INSTRUMENT line updated
    assert 'INSTRUMENT = "MNQU6"' in after
    # comment preserved
    assert "# comment trails" in after
    # NEXT_CONTRACT line untouched
    assert 'NEXT_CONTRACT = "MNQU6 09-26"' in after
    # Unrelated line preserved
    assert "OTHER = 1" in after


def test_swap_instrument_dry_run_does_not_write(tmp_settings):
    before = Path(tmp_settings).read_text(encoding="utf-8")
    changed, msg = swap_instrument_in_settings("MNQU6", settings_path=tmp_settings,
                                               dry_run=True)
    after = Path(tmp_settings).read_text(encoding="utf-8")
    assert changed is True
    assert "DRY_RUN" in msg
    assert before == after  # file unchanged


def test_swap_instrument_no_op_when_already_target(tmp_settings):
    Path(tmp_settings).write_text(
        'INSTRUMENT = "MNQU6"\n', encoding="utf-8",
    )
    changed, msg = swap_instrument_in_settings("MNQU6", settings_path=tmp_settings)
    assert changed is False
    assert "already" in msg.lower()


def test_swap_instrument_missing_line_returns_false(tmp_settings):
    Path(tmp_settings).write_text("# nothing here\n", encoding="utf-8")
    changed, msg = swap_instrument_in_settings("MNQU6", settings_path=tmp_settings)
    assert changed is False
    assert "not found" in msg


def test_swap_instrument_missing_file_returns_false(tmp_path):
    changed, msg = swap_instrument_in_settings(
        "MNQU6",
        settings_path=str(tmp_path / "ghost.py"),
    )
    assert changed is False
    assert "not found" in msg


# ═══════════════════════════════════════════════════════════════════
# flatten_for_roll — end-to-end, simulated roll day
# ═══════════════════════════════════════════════════════════════════
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_flatten_for_roll_disabled_by_default(tmp_state, tmp_settings, monkeypatch):
    monkeypatch.delenv(cr.ROLL_ENABLE_ENV, raising=False)
    pm = FakePM(positions=[FakePosition("t1"), FakePosition("t2")])

    res = _run(flatten_for_roll(
        pm, ws_send=None,
        now_ct=_ct(2026, 6, 19, 15, 45),
        simulate=False,
        settings_path=tmp_settings,
    ))

    assert res["executed"] is False
    assert "disabled" in (res["skipped_reason"] or "")
    assert pm.closed == []  # nothing flattened
    # settings unchanged
    assert 'INSTRUMENT = "MNQM6"' in Path(tmp_settings).read_text(encoding="utf-8")


def test_flatten_for_roll_simulate_closes_positions_dry_swap(tmp_state, tmp_settings):
    pm = FakePM(positions=[FakePosition("t1"), FakePosition("t2")])

    res = _run(flatten_for_roll(
        pm, ws_send=None,
        now_ct=_ct(2026, 6, 19, 15, 45),
        simulate=True,
        settings_path=tmp_settings,
    ))

    assert res["executed"] is True
    assert res["simulated"] is True
    assert res["flattened_count"] == 2
    assert set(res["flattened_ids"]) == {"t1", "t2"}
    # close_position called for each (no ws_send)
    assert {c["trade_id"] for c in pm.closed} == {"t1", "t2"}
    for c in pm.closed:
        assert "contract_roll_MNQM6_to_MNQU6" in c["exit_reason"]

    # Dry-run swap should NOT have rewritten settings.py
    assert 'INSTRUMENT = "MNQM6"' in Path(tmp_settings).read_text(encoding="utf-8")
    assert res["instrument_swap"]["changed"] is True
    assert "DRY_RUN" in res["instrument_swap"]["message"]

    # Roll state was persisted
    state = load_roll_state(tmp_state)
    assert state["last_roll_date"] == "2026-06-19"
    assert state["rolled_to"] == "MNQU6"


def test_flatten_for_roll_env_enabled_writes_settings(tmp_state, tmp_settings, monkeypatch):
    monkeypatch.setenv(cr.ROLL_ENABLE_ENV, "1")
    pm = FakePM(positions=[FakePosition("t1")])

    res = _run(flatten_for_roll(
        pm, ws_send=None,
        now_ct=_ct(2026, 6, 19, 15, 45),
        simulate=False,
        settings_path=tmp_settings,
    ))

    assert res["executed"] is True
    assert res["simulated"] is False
    assert res["flattened_count"] == 1
    # Real swap landed on disk
    after = Path(tmp_settings).read_text(encoding="utf-8")
    assert 'INSTRUMENT = "MNQU6"' in after
    assert res["instrument_swap"]["changed"] is True


def test_flatten_for_roll_idempotent_same_day(tmp_state, tmp_settings):
    pm = FakePM(positions=[FakePosition("t1")])

    first = _run(flatten_for_roll(
        pm, ws_send=None,
        now_ct=_ct(2026, 6, 19, 15, 45),
        simulate=True,
        settings_path=tmp_settings,
    ))
    assert first["executed"] is True

    pm2 = FakePM(positions=[FakePosition("ghost")])
    second = _run(flatten_for_roll(
        pm2, ws_send=None,
        now_ct=_ct(2026, 6, 19, 15, 46),
        simulate=True,
        settings_path=tmp_settings,
    ))
    # Second call no-ops via already_rolled_today
    assert second["executed"] is False
    assert second["skipped_reason"] == "already_rolled_today"
    assert pm2.closed == []  # didn't flatten the ghost


def test_flatten_for_roll_uses_ws_send_when_provided(tmp_state, tmp_settings):
    sent: list[tuple[str, str]] = []

    async def fake_ws(trade_id, reason="default"):
        sent.append((trade_id, reason))

    pm = FakePM(positions=[FakePosition("a"), FakePosition("b")])

    res = _run(flatten_for_roll(
        pm, ws_send=fake_ws,
        now_ct=_ct(2026, 6, 19, 15, 45),
        simulate=True,
        settings_path=tmp_settings,
    ))

    assert res["flattened_count"] == 2
    assert {tid for tid, _ in sent} == {"a", "b"}
    # ws_send path → close_position is NOT called
    assert pm.closed == []


def test_flatten_for_roll_with_empty_positions_still_swaps(tmp_state, tmp_settings):
    pm = FakePM(positions=[])
    res = _run(flatten_for_roll(
        pm, ws_send=None,
        now_ct=_ct(2026, 6, 19, 15, 45),
        simulate=True,
        settings_path=tmp_settings,
    ))
    assert res["executed"] is True
    assert res["flattened_count"] == 0
    assert res["instrument_swap"]["changed"] is True


# ═══════════════════════════════════════════════════════════════════
# log_rollover_status smoke
# ═══════════════════════════════════════════════════════════════════
def test_log_rollover_status_smoke_no_crash():
    log_rollover_status(today=date(2026, 4, 17))   # normal day
    log_rollover_status(today=date(2026, 6, 11))   # roll-window day
    log_rollover_status(today=date(2026, 7, 1))    # post-expiry
