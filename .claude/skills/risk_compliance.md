---
name: risk_compliance
description: Phoenix risk + compliance layer (OIF, risk manager, stop orders, position caps, execution, kill switch). Read this before touching bridge/oif_writer.py, phoenix_bot/orchestrator/oif_writer.py, core/risk_manager.py, or anything in the execution path.
---

# Layer: Risk + Compliance

## What this layer does
Translates accepted `Signal` objects into NT8 orders via the OIF (Order Instruction File) pipeline, enforces risk limits (per-trade, daily, weekly), polices kill switches, and provides the live-execution guardrails. The OIF path is **the most fragile and most operator-monitored part of Phoenix.**

## 🛑 STOP GATES — explicit operator sign-off required
Any change to ANY of these files requires explicit operator sign-off BEFORE you start editing:

- `bridge/oif_writer.py`
- `phoenix_bot/orchestrator/oif_writer.py`
- `core/risk_manager.py`

Per SYNTHESIS_2026-05-24.md, every P1+ task touching these files is gated behind a STOP banner (P1-2, P1-3, P1-7, P1-8, P4-1, P4-2 etc.). The operator's standing instruction: **stop, ask, do not proceed with code edits until they approve.**

## Phase 0 OIF + AI freeze (active per SYNTHESIS_2026-05-24.md)
A Phase 0 sign-off gate is active. The four P0 tasks (P0-1 stack health, P0-2 sim_overrides extraction, P0-3 weekly-loss hierarchy, P0-4 AI agents disabled) must be done and Phase 0 §3 checks pass BEFORE proceeding to P1. Test suite must still be at 2,110+ pass / 0 fail. Until then, do NOT initiate any P1+ changes that touch OIF or execution.

## Immutable OIF rules
1. **OIF format**: `PLACE;Account;Instrument;Action;Qty;OrderType;LimitPrice;StopPrice;TIF;OcoId;;;`
2. **All stops are STOPMARKET** (execution certainty over price precision)
3. **All targets are LIMIT** (price precision over fill certainty)
4. **Bracket orders are staged atomic write**: all `.tmp` files created first, then renamed to `.txt` in order (stop → target → entry). If any tmp write fails, nothing becomes visible to NT8.
5. **`.tmp` → `.txt` atomic rename** — never write `.txt` directly. This is the foundation of the "no half-bracket" guarantee.
6. **Filename prefix MUST be `phoenix_<pid>_`** — `_PHOENIX_PID = os.getpid()` captured once at module load in `bridge/oif_writer.py:41`. The NT8-side PhoenixOIFGuard AddOn quarantines any file in `incoming/` whose name does NOT start with `phoenix_<pid>_`. This is the other half of the pytest-leak defence (B81 conftest fixture stopped pytest leaks globally; the prefix stops rogue scripts).
7. **PhoenixOIFGuard quarantine regex** matches `phoenix_<pid>_*` exactly — do NOT change the regex or the prefix scheme without coordinating with the NT8-side AddOn (operator-controlled, manual deploy).
8. **OIF counter is time-seeded**: `_oif_counter = int(time.time() * 1000) % 1000000` (bridge/oif_writer.py:29) — avoids restart collisions. Do not reset to 0 on bot start.
9. **Tests must use the autouse OIF_INCOMING isolation fixture** (`tests/conftest.py:20-37`). Never write OIFs to the real `incoming/` in tests.

## RiskGateSink fail-open vs fail-closed (P1-2)
Today's behavior at `phoenix_bot/orchestrator/oif_writer.py:186-225`: when `PHOENIX_RISK_GATE=1` is set but the pipe is unreachable, the code falls back to `DirectFileSink`. That is "risk gate off without telling anyone." P1-2 will change this to fail-CLOSED when the env flag is set EXPLICITLY (refuse to write OIF + emit CRITICAL log). Keep fail-soft only when `PHOENIX_RISK_GATE` is unset or 0. **🛑 P1-2 has a STOP banner — sign-off required before implementing.**

## Risk limits (config/settings.py)
- `MAX_LOSS_PER_TRADE = $20`
- `DAILY_LOSS_LIMIT = $45` (recovery mode at -$30: 50% size reduction)
- `WEEKLY_LOSS_LIMIT` — **bug**: currently violates `WEEKLY > DAILY × 3`. P0-3 fixes this (raise weekly to ≥ $600).
- `LIVE_TRADING = False` by default
- AI agent enables: `AGENT_*_ENABLED` flags (P0-4 disables all three for the freeze)

## Stop-order ID capture (P1-8)
Strategies with managed exits that modify stops require an order ID to atomically cancel-replace. Today's logs show `[STOP_MOVE_NO_ID]` for some strategies — without an order ID the modify path can't run safely. P1-8 fixes this; until shipped, do NOT rely on stop-modify for any active strategy without ID capture confirmed in NT8 outgoing-file parsing. **🛑 P1-8 has a STOP banner.**

## Phase 0 OIF verification checklist (SYNTHESIS 0.4–0.7)
- 0.4 OIF atomic write + `phoenix_<pid>_` prefix verified — `python tools/verify_oif_fix.py`
- 0.5 PhoenixOIFGuard quarantine regex matches `phoenix_<pid>_*` — drop test file `notphoenix_test.txt` in `incoming/`; AddOn quarantines within 1s
- 0.6 `tests/conftest.py` autouse fixture isolates OIF_INCOMING per-test ✅
- 0.7 Restart-safe OIF counter seeding ✅

## Code-changes-don't-auto-deploy reminder
`git commit` ≠ "running code is updated." A long-running prod_bot keeps its in-memory code snapshot from process start. After any behavior-affecting commit to OIF / risk / execution code, **always flag "prod needs restart"** to the operator, or ask permission to restart. Cost the operator -$106 on 2026-05-14 when a stop-clamp fix sat on disk while prod ran the old code.

## Reference files
- `bridge/oif_writer.py:1-50` — OIF format, atomic write, PID prefix
- `phoenix_bot/orchestrator/oif_writer.py:186-225` — RiskGateSink (P1-2 target)
- `phoenix_bot/orchestrator/OIFSink` Protocol — required when `PHOENIX_RISK_GATE=1`
- `core/risk_manager.py` — daily/weekly limits, VIX filter, recovery mode, sizing
- `tests/conftest.py:20-37` — autouse OIF_INCOMING isolation
- `tools/verify_oif_fix.py` — Phase 0 §0.4 verifier
- `tools/verify_halt_signatures.py` — weekly halt verification
- `docs/audits/SYNTHESIS_2026-05-24.md` — Phase 0 + P1-2 + P1-8 + STOP banner taxonomy
- `memory/oif_guard_race.md` — known PhoenixOIFGuard race condition (tripwire only)
- `memory/windows_subprocess_zombie.md` — `creationflags=0` (NOT `CREATE_NEW_PROCESS_GROUP`)

## DO NOT
- Do NOT touch `bridge/oif_writer.py`, `phoenix_bot/orchestrator/oif_writer.py`, or `core/risk_manager.py` without explicit operator sign-off.
- Do NOT change the `phoenix_<pid>_` filename prefix or the PhoenixOIFGuard regex without coordinated NT8-side AddOn deploy.
- Do NOT write `.txt` directly to `incoming/` — always `.tmp` → atomic rename.
- Do NOT reset `_oif_counter` to 0 on bot start — time-seeded value avoids restart collisions.
- Do NOT add a strategy that writes OIFs directly. Strategies emit `Signal`; `bots/base_bot._enter_trade` is the single OIF gateway.
- Do NOT change RiskGateSink fallback semantics until P1-2 sign-off — the fail-open behavior, while unsafe, is operator-known and operator-tolerated as of 2026-05-24.
- Do NOT skip the "prod needs restart" flag after behavior-affecting commits to OIF / risk / execution code.
- Do NOT initiate any P1+ task while the Phase 0 freeze is active.
