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
