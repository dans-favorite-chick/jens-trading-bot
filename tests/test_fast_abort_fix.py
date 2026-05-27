"""Regression — bias_momentum fast-abort bug fix (2026-05-13).

Forensic observation (sim_bot 2026-05-13 17:21 and 17:39):

Trade f9781751 was held 8 seconds. Sequence from logs:
  17:39:29.063  OPEN @ 29561.0   (filled +1t above signal)
  17:39:30.279  TRAIL: Stop trailed to 29561.25 (mid)
  17:39:30.280  RIDER BE STOP (UNKNOWN, 50%R) stop moved to 29561.50
  17:39:30.280  EXIT_PENDING @ 29561.50, reason=stop_loss

Three compounding bugs:
1. `_trail_stop()` fired unconditionally even with only 1 tick of profit.
   `mid = (entry + price) / 2 = 29561.25` = entry + 1 tick = a 1-tick stop.
2. BE_STOP recomputed `stop_dist = abs(entry - stop_price)` AFTER the trail
   had already moved stop_price tight. So stop_dist = 1 tick, BE-trigger
   at 0.5R = 0.5 tick, BE-stop = entry + 2 ticks = current price.
3. `trend_stall_grace_s` (60s) only suppressed `exit_signal`, not
   `tighten_stop`. So within the grace window, trail still fired.

This test covers the 4 surgical fixes that close the loop:
  a. Position has `initial_stop_price` capturing entry-time stop
  b. `open_position()` sets it
  c. `_trail_stop()` requires min_profit_ticks before firing
  d. BE_STOP uses `initial_stop_price` for stop_dist
  e. Grace window suppresses tighten_stop in addition to exit_signal
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# P4-1 (2026-05-24): search all extracted bot modules, not base_bot.py alone
from tests._bot_src_search import bot_combined_source as _bcs
BASE_BOT_SRC = _bcs()
POS_MGR_SRC = (ROOT / "core" / "position_manager.py").read_text(encoding="utf-8")


# ── Position field + open_position capture ─────────────────────────────

def test_position_has_initial_stop_price_field():
    """Position dataclass must carry initial_stop_price."""
    from core.position_manager import Position
    p = Position(
        trade_id="t", direction="LONG", entry_price=100.0, entry_time=0,
        contracts=1, stop_price=99.0, target_price=102.0,
        strategy="x", reason="r", market_snapshot={},
    )
    assert hasattr(p, "initial_stop_price"), (
        "Position dataclass must have `initial_stop_price` field "
        "(stores the entry-time stop so BE_STOP can compute R from "
        "the ORIGINAL distance, not whatever TRAIL has shrunk it to)"
    )


def test_open_position_captures_initial_stop_price():
    """open_position() must set initial_stop_price = stop_price."""
    from core.position_manager import PositionManager
    pm = PositionManager()
    ok = pm.open_position(
        trade_id="t1", direction="LONG", entry_price=29561.0,
        contracts=1, stop_price=29531.75, target_price=30141.0,
        strategy="bias_momentum", reason="test",
    )
    assert ok
    pos = pm.get_position("t1")
    assert pos.initial_stop_price == 29531.75, (
        f"expected initial_stop_price=29531.75, got {pos.initial_stop_price}"
    )
    assert pos.stop_price == 29531.75


# ── _trail_stop min-profit floor ───────────────────────────────────────

def test_trail_stop_does_nothing_under_min_profit():
    """_trail_stop must not fire if price hasn't moved at least
    min_profit_ticks in favor of the trade. The forensic bug case:
    LONG @ 29561.0 with current price 29561.5 (= +2 ticks of profit) —
    pre-fix, trail moved stop to (29561.0+29561.5)/2 = 29561.25 = a 1-tick
    stop that triggered on the next adverse blip."""
    from bots.base_bot import _trail_stop

    # Replicate the exact forensic state
    pos = MagicMock()
    pos.entry_price = 29561.0
    pos.direction = "LONG"
    pos.stop_price = 29531.75
    pos.high_water_price = 29561.0   # initialized at entry
    pos.trade_id = "test-fast-abort"

    # Only +2 ticks of profit — well under the default 8-tick floor.
    # Pre-fix: TRAIL would fire and move stop to 29561.25 = entry + 1 tick.
    # Post-fix: TRAIL must no-op.
    _trail_stop(pos, price=29561.5)

    assert pos.stop_price == 29531.75, (
        f"_trail_stop fired prematurely on +2 ticks of profit — "
        f"stop_price changed from 29531.75 to {pos.stop_price}. "
        f"With min_profit_ticks=8 (default), TRAIL must wait for at "
        f"least 8 ticks of in-the-money movement before activating."
    )


def test_trail_stop_fires_above_min_profit():
    """Once price has moved >= min_profit_ticks favorably, TRAIL fires.
    With high-water-mark formula and 16-tick default buffer."""
    from bots.base_bot import _trail_stop

    pos = MagicMock()
    pos.entry_price = 29561.0
    pos.direction = "LONG"
    pos.stop_price = 29531.75
    pos.trade_id = "test-trail-active"
    pos.high_water_price = 29561.0  # initialized at entry

    # Price at +12 ticks of profit (= 3.0 MNQ points) — past the 8-tick floor.
    # peak = 29564.0 (= entry + 12t). Stop = peak - 16t = 29560.0.
    # But 29560.0 < pos.stop_price (29531.75 — no wait, 29560 > 29531.75).
    # So stop should move to 29560.0.
    _trail_stop(pos, price=29564.0)

    # Expected: peak=29564.0, stop=peak - 16t = 29560.0
    assert pos.stop_price == 29560.0, (
        f"expected stop trailed to 29560.0 (peak 29564.0 − 16t buffer), got {pos.stop_price}"
    )
    # high_water_price should have been updated
    assert pos.high_water_price == 29564.0


def test_trail_uses_high_water_not_current():
    """Trail should anchor at peak price, NOT current price. If price
    pulls back from peak, trail stays at peak−buffer, doesn't follow
    price down."""
    from bots.base_bot import _trail_stop

    pos = MagicMock()
    pos.entry_price = 29561.0
    pos.direction = "LONG"
    pos.stop_price = 29531.75
    pos.trade_id = "test-hwm-anchor"
    pos.high_water_price = 29561.0

    # First call: price hits a high of 29577.0 (+64 ticks = 16 pts). Trail kicks in.
    _trail_stop(pos, price=29577.0)
    # peak = 29577.0, stop = peak - 16t = 29573.0
    assert pos.stop_price == 29573.0
    assert pos.high_water_price == 29577.0

    # Second call: price pulls back to 29570.0 (still profitable but lower than peak)
    # The high_water_price should NOT update down, and stop should NOT move.
    _trail_stop(pos, price=29570.0)
    assert pos.high_water_price == 29577.0, (
        "high_water must NOT drop when price pulls back from peak"
    )
    assert pos.stop_price == 29573.0, (
        "stop must NOT move down when price pulls back"
    )

    # Third call: price makes a new high at 29585.0. Trail ratchets up.
    _trail_stop(pos, price=29585.0)
    assert pos.high_water_price == 29585.0
    assert pos.stop_price == 29581.0  # 29585.0 - 16t


# ── BE_STOP uses initial_stop_price (static check on the source) ───────

def test_be_stop_uses_initial_stop_price():
    """The BE_STOP block in _on_bar / RIDER mode must read pos.initial_stop_price
    for stop_dist, not pos.stop_price."""
    # Locate the BE_STOP code block in base_bot.py.
    m = re.search(
        r"if not pos\.be_stop_active:.*?be_stop_active = True",
        BASE_BOT_SRC, re.DOTALL,
    )
    assert m, "couldn't locate the BE_STOP block in base_bot.py"
    body = m.group(0)

    # Strip comments before checking the active code
    non_comment = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )

    assert "initial_stop_price" in non_comment, (
        "BE_STOP block must reference pos.initial_stop_price (the entry-"
        "time stop) when computing stop_dist. Otherwise a prior TRAIL "
        "that tightened pos.stop_price will make BE fire at trivial "
        "profit, exiting the trade on entry-noise."
    )


# ── Grace window covers tighten_stop ───────────────────────────────────

def test_grace_window_covers_tighten_stop():
    """The trend_stall_grace_s suppression must apply to tighten_stop as
    well as exit_signal. Pre-fix only exit_signal was gated."""
    m = re.search(
        r"_in_grace = should_suppress_trend_stall.*?_trail_stop\(pos, price\)",
        BASE_BOT_SRC, re.DOTALL,
    )
    assert m, "couldn't locate the stall-handling block in base_bot.py"
    body = m.group(0)

    # Must have a path where tighten_stop AND in_grace suppresses the trail.
    # Strip comments first.
    non_comment = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )

    # The active grace-AND-tighten branch must exist
    assert re.search(r'stall\["tighten_stop"\]\s+and\s+_in_grace', non_comment), (
        "Grace window must suppress tighten_stop within the same grace "
        "period that suppresses exit_signal. Pre-fix the TRAIL would "
        "still fire on stall MODERATE within seconds of entry, "
        "compounding with BE_STOP to kill trades in 8s."
    )


# ── End-to-end: replay the exact forensic scenario ─────────────────────

def test_398523b9_hwm_trail_keeps_room():
    """Replay the 2026-05-13 18:00 trade 398523b9 scenario:
      - Entry @ 29592.25 (LONG)
      - Peak @ 29597.75 (+22t)
      - Retrace to 29594.75 (back to +10t, a 12t pullback from peak)

    Pre-v2 fix: midpoint trail moved stop to 29595.12 (entry + 11.5t).
      The 12t retrace clipped the stop → exit at +10t / $0.18 net.

    Post-v2 fix: HWM trail moves stop to peak - 16t = 29593.75 (entry + 6t).
      The 12t retrace from peak does NOT trigger (29594.75 > 29593.75).
      Trade survives to develop further."""
    from bots.base_bot import _trail_stop

    pos = MagicMock()
    pos.entry_price = 29592.25
    pos.direction = "LONG"
    pos.stop_price = 29562.75  # initial 117t stop
    pos.high_water_price = 29592.25
    pos.trade_id = "398523b9-replay"

    # Price runs to peak +22t
    _trail_stop(pos, price=29597.75)
    expected_stop = 29597.75 - 16 * 0.25
    assert pos.stop_price == expected_stop, (
        f"expected stop at peak-16t = {expected_stop}, got {pos.stop_price}"
    )
    assert pos.high_water_price == 29597.75

    # Now price retraces to 29594.75 (12t pullback from peak)
    # NEW trail should NOT move stop (peak unchanged), and
    # the stop at 29593.75 is BELOW the retrace low of 29594.75
    # so the position survives the pullback.
    _trail_stop(pos, price=29594.75)
    assert pos.stop_price == 29593.75, (
        f"expected stop unchanged at 29593.75, got {pos.stop_price}"
    )
    assert 29594.75 > pos.stop_price, (
        "retrace low 29594.75 must remain ABOVE the trail stop 29593.75 — "
        "this is the entire point of the HWM-trail fix vs the old "
        "midpoint formula which would have set stop to 29595.0+ and "
        "been triggered by the same retrace."
    )


def test_forensic_replay_trade_survives_with_fixes():
    """Reproduce the 2026-05-13 17:39 trade f9781751 conditions and confirm
    the post-fix code path does NOT immediately exit.

    Setup:
      Entry: LONG @ 29561.0
      Initial stop: 29531.75 (117 ticks = 29.25 pts away)
      Current price 1s after entry: 29561.5 (+2 ticks)
      Day type: UNKNOWN  →  be_mult = 0.5

    Pre-fix sequence on this state would be:
      TRAIL fires → stop_price = 29561.25 (mid)
      BE_STOP fires → stop_dist=1t, trigger at +0.5t, stop=29561.5
      Price 29561.5 hits new stop → exit at -3.82.

    Post-fix expectation:
      TRAIL no-ops (profit < 8t floor)
      Stop stays at 29531.75 (the original wide stop)
      Trade lives.
    """
    from core.position_manager import PositionManager
    from bots.base_bot import _trail_stop

    pm = PositionManager()
    pm.open_position(
        trade_id="f9781751", direction="LONG", entry_price=29561.0,
        contracts=1, stop_price=29531.75, target_price=30141.0,
        strategy="bias_momentum", reason="forensic replay",
    )
    pos = pm.get_position("f9781751")

    # Sanity check: initial_stop_price was captured
    assert pos.initial_stop_price == 29531.75

    # 1 second after entry, price is +2 ticks. TRAIL should NOT fire.
    _trail_stop(pos, price=29561.5)
    assert pos.stop_price == 29531.75, (
        "Post-fix replay: stop_price must remain at the entry-time stop "
        f"29531.75 when profit is only 2 ticks. Got {pos.stop_price} — "
        f"the fast-abort bug has regressed."
    )

    # Even if we simulate the BE_STOP block computing stop_dist from
    # initial_stop_price, the answer is reasonable:
    init_stop = pos.initial_stop_price
    stop_dist = abs(pos.entry_price - init_stop)
    assert stop_dist == 29.25  # 117 ticks
    be_mult = 0.5  # UNKNOWN day
    be_trigger = pos.entry_price + stop_dist * be_mult
    assert be_trigger == 29575.625  # entry + 58.5 ticks
    # Current price 29561.5 is FAR below this trigger → BE does not fire.
    assert 29561.5 < be_trigger
