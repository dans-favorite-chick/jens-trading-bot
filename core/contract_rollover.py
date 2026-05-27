"""
Phoenix Bot — Contract Rollover Helper (P2-3 extended 2026-05-24)

Detects when the front-month contract is near expiration and drives the
automated roll: T-15 pre-flatten of all positions on the roll day,
refusal of new entries from T-15 through the next globex session open,
atomic swap of `INSTRUMENT` in `config/settings.py`, and a persistent
state file so a restart mid-roll doesn't re-roll.

Called at bot startup (base_bot.py) and once per minute by the daily
flatten loop on the roll-day window.

Rollover rule (CME quarterly cycle):
  H/M/U/Z = March/June/September/December, 3rd Friday expiration.
  Front-month typically rolls ~8 trading days before expiration.

Safety:
  Auto-flatten and INSTRUMENT swap are GATED. The action only fires when
  one of the following is true:
    - env var `PHOENIX_ROLL_ENABLED=1`
    - `simulate=True` is passed to flatten_for_roll() (test / dry-run)
  Otherwise the helper LOGS what it WOULD do and returns without
  touching positions or `config/settings.py`. This protects against an
  unattended bot accidentally re-rolling during the next quarter window.

Per-bot persistence:
  `logs/roll_state.json` records `{"last_roll_date": "YYYY-MM-DD",
  "rolled_to": "MNQU6", "rolled_from": "MNQM6"}`. The same date is never
  rolled twice. Cleared by hand if the operator needs to force a re-roll
  in the same session (rare, only if the settings.py swap failed).
"""

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from config.settings import (
    INSTRUMENT,
    CONTRACT_EXPIRATION,
    NEXT_CONTRACT,
    NEXT_CONTRACT_EXPIRATION,
    ROLL_DAYS_BEFORE_EXPIRATION,
)

logger = logging.getLogger("Rollover")

CT = ZoneInfo("America/Chicago")

# ─── Roll-day timing constants (CT) ────────────────────────────────
# On the roll day (defined as the EXPIRATION date — 3rd Friday), we
# pre-flatten 15 minutes before NT8's own session shutdown. CME settles
# the front month at 16:00 CT on the 3rd Friday for index futures.
ROLL_DAY_FLATTEN_HOUR_CT = 15
ROLL_DAY_FLATTEN_MINUTE_CT = 45  # T-15 = 15:45 CT (15:60 - 15 = 15:45)
ROLL_DAY_SESSION_END_HOUR_CT = 16
ROLL_DAY_SESSION_END_MINUTE_CT = 0

# ─── Persistence ──────────────────────────────────────────────────
ROLL_STATE_FILE = os.path.join(
    os.path.dirname(__file__), "..", "logs", "roll_state.json"
)

# Env var gate for ACTUAL flatten/swap (default OFF for safety)
ROLL_ENABLE_ENV = "PHOENIX_ROLL_ENABLED"


# ═══════════════════════════════════════════════════════════════════
# Date math
# ═══════════════════════════════════════════════════════════════════
def _trading_days_until(target: date, today: date = None) -> int:
    """Approx trading days between today and target (skips weekends)."""
    if today is None:
        today = date.today()
    if target <= today:
        return 0
    days = 0
    cur = today
    while cur < target:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon-Fri
            days += 1
    return days


def get_active_contract(today: date = None) -> dict:
    """
    Return dict with active contract info + rollover status.

    Returns:
      {
        "symbol": "MNQM6",
        "expiration": date(2026, 6, 19),
        "days_to_expiration": 63,
        "should_roll": False,
        "roll_target": "MNQU6 09-26",
        "warning": None  # or human-readable warning string
      }
    """
    if today is None:
        today = date.today()

    current_exp = datetime.strptime(CONTRACT_EXPIRATION, "%Y-%m-%d").date()
    next_exp = datetime.strptime(NEXT_CONTRACT_EXPIRATION, "%Y-%m-%d").date()

    days_current = _trading_days_until(current_exp, today)
    days_next = _trading_days_until(next_exp, today)

    if days_current <= 0:
        active = {
            "symbol": NEXT_CONTRACT,
            "expiration": next_exp,
            "days_to_expiration": days_next,
            "should_roll": False,
            "roll_target": None,
            "warning": f"Front-month {INSTRUMENT} expired {today - current_exp} days ago. Using {NEXT_CONTRACT}.",
        }
    elif days_current <= ROLL_DAYS_BEFORE_EXPIRATION:
        active = {
            "symbol": NEXT_CONTRACT,
            "expiration": next_exp,
            "days_to_expiration": days_next,
            "should_roll": True,
            "roll_target": NEXT_CONTRACT,
            "warning": f"ROLLOVER: {INSTRUMENT} expires in {days_current} trading days. Switching to {NEXT_CONTRACT}.",
        }
    else:
        active = {
            "symbol": INSTRUMENT,
            "expiration": current_exp,
            "days_to_expiration": days_current,
            "should_roll": False,
            "roll_target": NEXT_CONTRACT,
            "warning": None,
        }

    return active


def log_rollover_status(today: date = None) -> None:
    """Log rollover status at bot startup."""
    info = get_active_contract(today)
    if info["warning"]:
        logger.warning(f"[ROLLOVER] {info['warning']}")
    else:
        logger.info(
            f"[ROLLOVER] Active: {info['symbol']} "
            f"(expires {info['expiration']}, {info['days_to_expiration']} trading days)"
        )


# ═══════════════════════════════════════════════════════════════════
# Roll-window predicates (drive runtime hooks in base_bot)
# ═══════════════════════════════════════════════════════════════════
def is_roll_window(today: date = None) -> bool:
    """True when we are within ROLL_DAYS_BEFORE_EXPIRATION of the
    front-month expiration. Cheap to call on every loop tick."""
    info = get_active_contract(today)
    return bool(info["should_roll"])


def is_roll_day(today: date = None) -> bool:
    """True only on the actual expiration date (3rd Friday).
    Distinct from is_roll_window — the window is multi-day, the DAY is one."""
    if today is None:
        today = date.today()
    current_exp = datetime.strptime(CONTRACT_EXPIRATION, "%Y-%m-%d").date()
    return today == current_exp


def is_t_minus_15_pre_roll(now_ct: Optional[datetime] = None) -> bool:
    """True only on the roll day, between T-15 (15:45 CT) and session
    end (16:00 CT). The base_bot calls flatten_for_roll() exactly once
    in this window; subsequent ticks no-op because RollState records
    the date."""
    if now_ct is None:
        now_ct = datetime.now(CT)
    if not is_roll_day(now_ct.date()):
        return False
    t = now_ct.time()
    flat_t = time(ROLL_DAY_FLATTEN_HOUR_CT, ROLL_DAY_FLATTEN_MINUTE_CT)
    end_t = time(ROLL_DAY_SESSION_END_HOUR_CT, ROLL_DAY_SESSION_END_MINUTE_CT)
    return flat_t <= t < end_t


def is_no_new_entries_for_roll(now_ct: Optional[datetime] = None) -> bool:
    """True when the bot should refuse new entries because of the roll.

    Two cases (logical OR):
      1. ROLL_DAY: from 15:45 CT through end-of-day (carries past 16:00
         and across the globex break — we don't accept new entries until
         the NEXT globex session re-opens at 17:00 CT and the
         instrument has been swapped).
      2. POST_ROLL but BEFORE swap landed: state file shows we rolled
         today and the active symbol no longer matches settings.INSTRUMENT
         (the operator hasn't restarted yet to pick up the new INSTRUMENT).
    """
    if now_ct is None:
        now_ct = datetime.now(CT)

    # Case 1: roll day, T-15 through next globex open (17:00 CT).
    # Block covers the maintenance break — entries resume in the new
    # 17:00 session on the new symbol.
    if is_roll_day(now_ct.date()):
        t = now_ct.time()
        if time(ROLL_DAY_FLATTEN_HOUR_CT, ROLL_DAY_FLATTEN_MINUTE_CT) <= t < time(17, 0):
            return True

    # Case 2: state file shows a roll today; defensive — covers a
    # manually-forced roll on a non-Friday. Refuse entries until 17:00 CT.
    state = load_roll_state()
    last_roll = state.get("last_roll_date")
    if last_roll and last_roll == now_ct.date().isoformat():
        if now_ct.time() < time(17, 0):
            return True

    return False


# ═══════════════════════════════════════════════════════════════════
# Roll-state persistence (replay-proof)
# ═══════════════════════════════════════════════════════════════════
def load_roll_state(path: Optional[str] = None) -> dict:
    """Read the persisted roll state. Returns {} on first run or any
    read error (state-file corruption never blocks the bot).

    `path` resolves lazily to the module-level ROLL_STATE_FILE when
    None — this lets tests monkeypatch the module-level constant
    instead of having to thread `path=` through every call site."""
    if path is None:
        path = ROLL_STATE_FILE
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.warning(f"[ROLLOVER] roll_state read failed ({e!r}); treating as empty")
        return {}


def _atomic_write_json(path: str, payload: dict) -> None:
    """Write JSON to `path` atomically via temp + replace."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".roll_state_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def mark_rolled(
    when: Optional[date] = None,
    rolled_from: Optional[str] = None,
    rolled_to: Optional[str] = None,
    path: Optional[str] = None,
) -> dict:
    """Record that a roll has occurred. Idempotent — same-day re-marks
    are no-ops as long as `last_roll_date` matches.

    Returns the persisted state dict.
    """
    if path is None:
        path = ROLL_STATE_FILE
    if when is None:
        when = date.today()
    if rolled_from is None:
        rolled_from = INSTRUMENT
    if rolled_to is None:
        rolled_to = _bare_symbol(NEXT_CONTRACT)

    existing = load_roll_state(path)
    if existing.get("last_roll_date") == when.isoformat():
        return existing

    payload = {
        "last_roll_date": when.isoformat(),
        "rolled_from": rolled_from,
        "rolled_to": rolled_to,
        "marked_at": datetime.now(CT).isoformat(),
    }
    _atomic_write_json(path, payload)
    return payload


def already_rolled_today(today: Optional[date] = None, path: Optional[str] = None) -> bool:
    """Has mark_rolled() been called for `today` (default: now)?"""
    if today is None:
        today = date.today()
    if path is None:
        path = ROLL_STATE_FILE
    state = load_roll_state(path)
    return state.get("last_roll_date") == today.isoformat()


def _bare_symbol(raw: str) -> str:
    """Strip the " 09-26" expiration suffix off NEXT_CONTRACT.
    `"MNQU6 09-26" -> "MNQU6"`. Settings.INSTRUMENT uses the bare form."""
    return (raw or "").split(" ", 1)[0].strip()


# ═══════════════════════════════════════════════════════════════════
# Atomic INSTRUMENT swap inside config/settings.py
# ═══════════════════════════════════════════════════════════════════
def _settings_path() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "config", "settings.py")
    )


# Regex compiled once. Matches:
#   INSTRUMENT = "MNQM6"
#   INSTRUMENT="MNQM6"
#   INSTRUMENT = 'MNQM6'   (with anything trailing on the line e.g. a comment)
# Captures group 1 = the symbol literal so we can swap it in-place.
_INSTRUMENT_RE = re.compile(
    r'^(\s*INSTRUMENT\s*=\s*)(["\'])([^"\']+)\2',
    re.MULTILINE,
)
# Same shape for the two metadata constants the roll updates.
_NEXT_CONTRACT_RE = re.compile(
    r'^(\s*NEXT_CONTRACT\s*=\s*)(["\'])([^"\']+)\2',
    re.MULTILINE,
)
_CURRENT_EXP_RE = re.compile(
    r'^(\s*CONTRACT_EXPIRATION\s*=\s*)(["\'])([^"\']+)\2',
    re.MULTILINE,
)
_NEXT_EXP_RE = re.compile(
    r'^(\s*NEXT_CONTRACT_EXPIRATION\s*=\s*)(["\'])([^"\']+)\2',
    re.MULTILINE,
)


def swap_instrument_in_settings(
    new_symbol: str,
    settings_path: Optional[str] = None,
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Atomically rewrite the `INSTRUMENT = "..."` line in
    config/settings.py to `new_symbol`. Returns (changed, message).

    Only the captured group is replaced; the rest of the line (comment,
    whitespace, quote style) is preserved. The entire file is read,
    transformed, written via temp + os.replace.

    `dry_run=True` reports the diff intent without writing.

    NEVER edits by hand-rewriting the whole settings.py — only the
    INSTRUMENT assignment line is touched.
    """
    path = settings_path or _settings_path()
    if not os.path.exists(path):
        return False, f"settings.py not found at {path}"

    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    m = _INSTRUMENT_RE.search(original)
    if not m:
        return False, f"INSTRUMENT = \"...\" line not found in {path}"

    old_symbol = m.group(3)
    if old_symbol == new_symbol:
        return False, f"INSTRUMENT already set to {new_symbol!r}; no change"

    def _replace(match: "re.Match[str]") -> str:
        prefix, quote, _old = match.group(1), match.group(2), match.group(3)
        return f"{prefix}{quote}{new_symbol}{quote}"

    new_content = _INSTRUMENT_RE.sub(_replace, original, count=1)

    if dry_run:
        return True, f"[DRY_RUN] would swap INSTRUMENT {old_symbol!r} → {new_symbol!r}"

    # Atomic write
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".settings_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    return True, f"INSTRUMENT swapped {old_symbol!r} → {new_symbol!r} in {path}"


# ═══════════════════════════════════════════════════════════════════
# Flatten driver — called from base_bot.daily_flatten_loop
# ═══════════════════════════════════════════════════════════════════
def _flatten_enabled(simulate: bool) -> bool:
    """Gate: real flatten only when explicitly enabled."""
    if simulate:
        return True
    return os.environ.get(ROLL_ENABLE_ENV, "").strip() in ("1", "true", "TRUE", "yes")


async def flatten_for_roll(
    positions_manager: Any,
    ws_send: Optional[Callable] = None,
    *,
    now_ct: Optional[datetime] = None,
    simulate: bool = False,
    settings_path: Optional[str] = None,
) -> dict:
    """Run the T-15 pre-roll flatten + INSTRUMENT swap.

    Args:
      positions_manager: anything with .active_positions (list/dict of
        Position-like objects exposing .trade_id, .last_known_price,
        .contracts, optionally .account/.sub_strategy). Same duck-typing
        that DailyFlattener uses.
      ws_send: async callable (trade_id, reason=...) — sends the EXIT
        message to the bridge. If None, falls back to
        `positions_manager.close_position(price, reason, trade_id)`.
      now_ct: injectable clock (tests).
      simulate: when True, runs end-to-end against the supplied PM but
        gates on simulate (bypasses env var) and uses dry_run=True on
        the settings swap.
      settings_path: override for tests.

    Returns a structured dict describing what happened:
      {
        "executed": bool,         # did we actually do anything?
        "simulated": bool,
        "flattened_count": int,
        "flattened_ids": [str],
        "instrument_swap": {
          "changed": bool,
          "message": str,
          "from": "MNQM6",
          "to": "MNQU6",
        },
        "roll_state": {...},      # contents of roll_state.json
        "skipped_reason": Optional[str],  # set when executed=False
      }
    """
    if now_ct is None:
        now_ct = datetime.now(CT)

    result: dict = {
        "executed": False,
        "simulated": simulate,
        "flattened_count": 0,
        "flattened_ids": [],
        "instrument_swap": {"changed": False, "message": "", "from": INSTRUMENT, "to": None},
        "roll_state": {},
        "skipped_reason": None,
    }

    today = now_ct.date()

    # Idempotency: never re-roll the same date.
    if already_rolled_today(today):
        result["skipped_reason"] = "already_rolled_today"
        result["roll_state"] = load_roll_state()
        logger.info("[ROLLOVER] already_rolled_today — flatten_for_roll() no-op")
        return result

    if not _flatten_enabled(simulate):
        result["skipped_reason"] = "disabled (set PHOENIX_ROLL_ENABLED=1 or pass simulate=True)"
        logger.warning(
            "[ROLLOVER] flatten_for_roll() invoked but gate is OFF — "
            f"set env {ROLL_ENABLE_ENV}=1 to actually flatten. NO-OP."
        )
        return result

    # 1) Flatten every open position via the same WS-EXIT path the
    #    daily flattener uses. ws_send is plumbed by base_bot exactly
    #    the way DailyFlattener.ws_send is wired.
    target_symbol_full = NEXT_CONTRACT
    new_symbol = _bare_symbol(NEXT_CONTRACT)
    reason = f"contract_roll_{INSTRUMENT}_to_{new_symbol}_{today.isoformat()}"

    active = getattr(positions_manager, "active_positions", None) or []
    if isinstance(active, dict):
        positions = list(active.values())
    else:
        positions = list(active)

    flattened_ids: list[str] = []
    for pos in positions:
        tid = getattr(pos, "trade_id", None) or (
            pos.get("trade_id") if isinstance(pos, dict) else None
        )
        last_price = (
            getattr(pos, "last_known_price", None)
            or (pos.get("last_known_price") if isinstance(pos, dict) else None)
            or 0.0
        )
        try:
            if ws_send is not None:
                await ws_send(tid, reason=reason)
            else:
                positions_manager.close_position(last_price, reason, trade_id=tid)
            if tid:
                flattened_ids.append(tid)
        except Exception as e:
            logger.error(f"[ROLLOVER] flatten failed trade_id={tid}: {e!r}")

    result["flattened_count"] = len(flattened_ids)
    result["flattened_ids"] = flattened_ids
    logger.info(
        f"[ROLLOVER] T-15 pre-roll flatten fired at {now_ct.isoformat()} — "
        f"closed {len(flattened_ids)} position(s) for roll {INSTRUMENT} → {new_symbol}"
    )

    # 2) Swap INSTRUMENT in config/settings.py atomically.
    changed, msg = swap_instrument_in_settings(
        new_symbol,
        settings_path=settings_path,
        dry_run=simulate,
    )
    result["instrument_swap"] = {
        "changed": changed,
        "message": msg,
        "from": INSTRUMENT,
        "to": new_symbol,
    }
    if changed:
        logger.warning(f"[ROLLOVER] {msg}")
    else:
        logger.info(f"[ROLLOVER] settings swap: {msg}")

    # 3) Persist the roll record. mark_rolled() is the bottom of the
    #    barrel — once it lands, future flatten_for_roll() calls bail
    #    early via already_rolled_today(). The simulate path also marks
    #    so unit tests can exercise the no-double-roll guarantee, but
    #    tests should pass a temp `path`.
    state = mark_rolled(
        when=today,
        rolled_from=INSTRUMENT,
        rolled_to=new_symbol,
    )
    result["roll_state"] = state
    result["executed"] = True

    logger.warning(
        f"[ROLLOVER] ACTION REQUIRED: restart bot to load new INSTRUMENT={new_symbol}. "
        f"Manually swap NT8 chart Data Series to {target_symbol_full} and save workspace."
    )

    return result


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    info = get_active_contract()
    print(f"Active contract: {info['symbol']}")
    print(f"Expiration: {info['expiration']} ({info['days_to_expiration']} trading days)")
    print(f"Should roll: {info['should_roll']}")
    if info["warning"]:
        print(f"WARNING: {info['warning']}")
    print(f"Already rolled today: {already_rolled_today()}")
    print(f"Roll state: {load_roll_state()}")
