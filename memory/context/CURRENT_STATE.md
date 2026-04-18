# Phoenix Bot — Current State

_Last updated: 2026-04-17 19:04 Central Daylight Time_
_Next Claude session: read this FIRST for situational awareness_

## Bot operational state (as of Friday evening, 2026-04-17)

- **Prod bot:** UP, flat, Sim101 account (LIVE_TRADING=False)
- **Lab bot:** UP, flat, paper trading
- **Bridge:** UP on :8765 (NT8) + :8766 (bots)
- **Dashboard:** UP on :5000
- **Watchdog:** healthy
- **NT8:** market closed (after 15:00 CDT)

## Account state

- **Real account balance:** $300 (too small for Kelly sizing; small_account_mode active)
- **Live trading status:** PAUSED — prod stays Sim101 until account reaches $2,000
- **Lab bot:** paper only, always

## Today's MenthorQ regime (2026-04-17)

- GEX: POSITIVE +5.84B
- Call Resistance: 26,500 (primary gamma wall)
- Put Support: 24,000
- HVL: 25,290 (regime flip line)
- 1D range: 26,172.25 - 26,802.25
- direction_bias: LONG
- allow_longs: true / allow_shorts: false
- Strategy type: MEAN_REVERSION
- VIX: ~18.88 (low vol regime)

## What's happening this weekend

Three-session Phoenix rebuild Friday evening + Saturday + Sunday. See `RECENT_CHANGES.md` for what's been done as changes land.

## Immediate to-dos post this weekend

See `OPEN_QUESTIONS.md` for deferred items.
