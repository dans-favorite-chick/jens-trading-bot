# Exit Sprint Assumptions

## S5 trade_memory hygiene (B69/B70) — 2026-04-22

- Phase-C cutoff = `2026-04-21` (ISO date, lexicographic compare on `recorded_at`).
- Heuristic for null bot_id backfill:
  - `account == "Sim101"` AND `recorded_at >= 2026-04-21` -> `prod`
  - `account` starts with `"Sim"` (dedicated SimXxx) -> `sim`
  - `recorded_at < 2026-04-21` -> `legacy`
  - else -> `unknown`
- 38 rows dated 2026-04-21 before the Phase-C merge landed (20:39) have no
  `account` field and were classified `unknown` — they predate per-bot
  account routing and cannot be attributed post-hoc.
- Write-time guard in `core/trade_memory.record()` defaults missing bot_id
  to `"unknown"` + WARNING log rather than silently writing null.

## S1 stop/target math audit — 2026-04-21

- **Jennifer's "bias_momentum 600-pt stop" is the TARGET, not the stop.**
  Log line interpreted as `stop=27434` was actually `target=27434.5`
  on LONG @ 26834.5. Stop was 30.25 pts below entry (correct side,
  121t). The 600-pt target comes from `_RIDER_STRATEGIES` forcing
  `target_rr=20.0` in base_bot (lines 1913-1919). Flagging to Jennifer
  but not treating as a math bug.
- **noise_area `target_rr=0` is intentional.** Signal has
  `target_price=None` + `exit_trigger` set. Real bug was base_bot
  materializing `target_price=entry` when `signal.target_price is None`
  and `target_rr==0`. Fix landed in base_bot, NOT in noise_area config.
- **Universal sanity gate** placed in `base_bot._sanity_check_entry`,
  invoked in `_execute_trade` right before OCO submission. Ticks range
  [5, 200] per sprint spec. Logs `[STOP_SANITY_FAIL]` CRITICAL and
  aborts the signal.
- No strategy files under `strategies/` needed edits — all produce
  correct geometry; the defect was in base_bot's target-resolution
  path for managed-exit signals.
