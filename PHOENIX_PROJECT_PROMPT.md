# Phoenix Bot — Project Prompt (operator-facing architecture notes)

This document captures operator-facing architecture decisions that
don't fit in CLAUDE.md (developer reference) or BUILD_MAP.md (roadmap).
Edit inline when behavior changes.

---

## Daily Flatten Architecture (B84, 2026-04-22)

Phoenix uses a defense-in-depth schedule for end-of-day position closure
— the bot does the primary work; NT8 catches stragglers; CME enforces
the hard floor. All times are America/Chicago.

| Time     | Layer                             | Implemented in                                  |
|----------|-----------------------------------|-------------------------------------------------|
| 15:53 CT | Phoenix stops accepting NEW entries | `BaseBot._is_no_new_entries_window` guard fires at the top of `_enter_trade` |
| 15:54 CT | Phoenix `DailyFlattener` (PRIMARY) | `bots/daily_flatten.py` + `BaseBot._daily_flatten_loop` |
| 15:54:45 | Phoenix logs WARN if anything still open | `BaseBot._emit_grace_end_warn_if_open` |
| 15:55 CT | NT8 Auto Close Position (SAFETY NET) | **NT8 GUI — not Python**: Tools → Settings → Trading → Auto Close Position = 03:55:00 PM, All Instruments, platform timezone confirmed Central Time |
| 16:00 CT | CME globex 1-hour maintenance break (HARD FLOOR) | Exchange-side; no bot code |
| 17:00 CT | Globex reopens — new-entries gate lifts | `BaseBot._is_no_new_entries_window` returns False again |

### Source of truth

The Phoenix-side timings are configured by constants in `config/settings.py`:

```python
DAILY_FLATTEN_HOUR_CT        = 15
DAILY_FLATTEN_MINUTE_CT      = 54
NO_NEW_ENTRIES_HOUR_CT       = 15
NO_NEW_ENTRIES_MINUTE_CT     = 53
FILL_CONFIRMATION_GRACE_SECONDS = 45
```

`DailyFlattener` reads these as defaults via `_default_flatten_hour()` /
`_default_flatten_minute()`. Changing one constant moves the whole
system — do not hard-code times elsewhere.

### Strategy-level managed exits (interact with but don't replace the flatten)

Some strategies run their own managed-exit logic tied to cash-equity
session boundaries. These are expressed in **Eastern Time** and must
resolve to a CT time ≤ 15:54 CT so the bot flatten stays the primary:

| Strategy   | Mode     | `eod_flat_time_et` | CT equivalent |
|------------|----------|---------------------|----------------|
| noise_area | lab/sim  | `"16:54"` (B84 aligned)  | 15:54 CT       |
| noise_area | prod     | `"10:55"` (90-min window) | 09:55 CT       |
| ORB        | lab/sim  | `"16:54"` (B84 aligned)  | 15:54 CT       |
| ORB        | prod     | `"10:55"` (90-min window) | 09:55 CT       |

The prod 90-min-window values are deliberately earlier than the bot
flatten — prod strategies self-exit by 09:55 CT, and the 15:54 CT bot
flatten catches anything unexpectedly still open.

### NT8 GUI configuration reference

Operator must verify once per NT8 install / profile migration:

- **NT8 → Tools → Options → Trading → Auto Close Position**
  - Enabled: **Yes**
  - Time: **03:55:00 PM**
  - Instruments: **All instruments**
  - NT8 platform timezone: **Central Time** (verify via Tools → Options → General — offset should match local CDT/CST)

### Restart required for code changes to take effect

Changes to `DAILY_FLATTEN_HOUR_CT` / `_MINUTE_CT` / `NO_NEW_ENTRIES_*`
live in `config/settings.py`. **Running bot processes cache these at
import time** — a change requires restarting `sim_bot.py` (and
`prod_bot.py` if it's running positions) to take effect. The
`DailyFlattener` instance is created once per bot process.

---

## History log — `session_close` event (B84)

Emitted once at the 15:54 CT flatten by `HistoryLogger.log_session_close_event()`.
One line per day, in `logs/history/YYYY-MM-DD_<bot>.jsonl`. Fields:

- `event: "session_close"`
- `ts`: tz-aware CT ISO timestamp of the flatten moment
- `flattened_trade_ids`: list of trade_ids the bot closed itself
- `still_open_trade_ids`: list of trade_ids handed off to NT8 safety net
- `flattened_count`, `still_open_count`: integer counts
- `session_pnl`: today's P&L in dollars (best-effort until B13 ships)
- `b13_commission_applied`: bool — flags whether commission math has been
  corrected per B13 or is still best-effort gross
- `note`: string — "B13 commission math pending …" when b13 is False,
  else null

Consumers: AI debrief, daily recap, forensic review of days where NT8
Auto Close fires (`still_open_count > 0` means the bot missed some).
