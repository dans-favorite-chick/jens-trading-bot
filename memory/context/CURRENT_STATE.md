# Phoenix Bot — Current State

_Last updated: 2026-04-21 evening Central Daylight Time (post Phases E–H merge)_
_Next Claude session: read this FIRST for situational awareness_

## Bot operational state (as of Tuesday afternoon, 2026-04-21)

- **Prod bot:** UP, flat, Sim101 account (LIVE_TRADING=False, ~28 hours uptime)
- **Lab bot:** **DECOMMISSIONED** — paper-only flow ended 2026-04-21 15:38 CDT.
  `bots/lab_bot.py` preserved on disk as rollback safety net only.
- **Sim bot:** **UP**, Phase C live-sim execution on 16 dedicated NT8 Sim accounts.
  24/7 trading, per-strategy risk isolation, real OIF writes, 10 strategies loaded.
- **Bridge:** UP on :8765 (NT8) + :8766 (bots) + :8767 (health)
- **Dashboard:** UP on :5000, now with per-strategy risk panel
- **Watchdog:** UP, tracks prod + sim (lab dropped from default on 2026-04-21)
- **NT8:** live, MNQM6, ~5 ticks/s (afternoon session)

## Phase C deployment (2026-04-21 afternoon sprint)

Full transformation from lab paper-mode to sim live-trade-mode in a single
sprint. Feature branch `feature/knowledge-injection-systems` merged to `main`
at commit `4f444eb`.

### New execution model

- **16 strategies → 16 dedicated NT8 Sim accounts** via `config/account_routing.py`
  (byte-exact NT8 display names; Sim101 = default fallback)
- **Per-strategy risk isolation** via `core/strategy_risk_registry.py`
  - $2,000 starting balance per strategy
  - $200/day loss cap per strategy (resets daily)
  - $1,500 floor → halt + alert, **manual re-enable only**
  - Halt state persists to `logs/strategy_halts.json` across restarts
- **Multi-position runtime** — `core/position_manager.py` refactored to
  `dict[trade_id → Position]`. Multiple strategies can hold concurrent
  positions. Legacy single-position API preserved for back-compat.
- **4:00 PM CT daily flatten** — `bots/daily_flatten.py` closes all
  positions at CME globex pause start; globex reopens 17:00 CT; overnight
  holds OK during 17 CT → 16 CT next day globex session.
- **Dashboard per-strategy panel** — `/api/strategy-risk` endpoint exposes
  registry snapshot (balance / daily P&L / halted state) rendered as
  sortable table with 2s poll, halted rows in red.
- **Telegram per-strategy routing** — optional `TELEGRAM_STRATEGY_CHAT_OVERRIDES`
  dict routes notifications to strategy-specific chat IDs; `[strategy]` tag
  prepended to all notifications (controllable via `TELEGRAM_TAG_STRATEGY`).

### Phase C test coverage added

- 24 tests for `StrategyRiskRegistry` (init, isolation, balance, floor halt, persistence)
- 13 tests for `DailyFlattener` (time gating, multi-position iteration, timezone)
- 8 tests for Telegram routing (default, override, tagging, sub-strategy resolution)
- 7 tests for `reenable_strategy.py` CLI (list, clear-one, --all, exit codes)
- 18 tests for PositionManager multi-position (incl. concurrent strategies)

Full suite: **566 passing** / 6 B15-backlog pre-existing failures (unchanged).

## Account state

- **Real live account balance:** $300 (too small for Kelly sizing; small_account_mode active)
- **Live trading status:** PAUSED — prod stays Sim101 until account reaches $2,000
- **Sim bot:** live-sim only, $2,000 × 16 strategies = $32,000 virtual pool across dedicated sub-accounts

## Today's MenthorQ regime (2026-04-21)

- GEX: (Net GEX not in paste today — HVL proxy fallback active)
- Call Resistance: 26,500 (monthly) / 26,800 (0DTE)
- Put Support: 25,000 (monthly) / 26,560 (0DTE)
- HVL: 25,275 (monthly) / 26,700 (0DTE)
- 1D range: 26,421.44 – 27,076.06
- Regime classification: **NEGATIVE_NORMAL** (price below 0DTE HVL by ~50+ ticks)

## Phases E–H sprint (2026-04-21 evening) — merged to main

- **700 tests passing / 0 failing** (was 566 + 6 failing at sprint start)
- **Main commit:** `bd04875` (merge: `bdff605`)
- **AI agent stack built but NOT yet activated on the live bot** — agent hooks wired into `bots/base_bot.py` and `bots/sim_bot.py`, but require `.env` API keys + bot restart.
- Phase E (gamma rewire), Phase F (B15 backlog cleared), Phase G (B26/B37/B38), Phase H (5 agents + infra) — all shipped.
- See `docs/phase-eh-deployment.md` for runbook.

## Immediate to-dos

See `OPEN_QUESTIONS.md` and `memory/bugs/OPEN_BUGS.md` for deferred items.

Priority follow-ups from Phase C afternoon sprint:
1. Watch first-hour sim activity; verify per-strategy account routing in real NT8 output
2. Populate `TELEGRAM_STRATEGY_CHAT_OVERRIDES` if per-strategy channels desired
3. If MenthorQ publishes Net GEX today, update
   `data/menthorq/gamma/2026-04-21_levels.txt` with `, Net GEX, <val>, Total GEX, <val>, IV, <pct>`
   to switch from HVL proxy to authoritative regime
4. First floor-kill test: manually trigger a strategy to -$500 cumulative to
   validate halt + persistence + Telegram alert path
