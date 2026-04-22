# Phase 0 Emergency Patches — Assumptions Log

**Sprint branch:** `feature/phase-0-emergency`
**Baseline:** 834 passed, 3 skipped (commit `d674189`)

Judgment calls made during the sprint. Each decision is recorded with the
rationale so Jennifer (and future Claude) can audit the tradeoffs.

## P0.1 Trade Memory Persistence

**Decision: opt-in via `load_history` kwarg on `PositionManager.__init__`.**

Many test files and adjacent tools construct a `PositionManager()` directly
and expect a clean empty slate. Flipping the default to `True` would silently
hydrate 968+ rows from disk during hundreds of unit tests, changing
`trade_history` length assertions and coupling tests to the live log. Making
it explicit keeps tests deterministic while letting `base_bot.py` opt in at
the one production call site.

**`base_bot` opts in via `PositionManager(load_history=True)`** — all three
bots (prod/sim/lab) benefit. Dashboard P&L survives restart.

**Failure modes mapped to log level:**
- Missing file → `INFO` ("starting fresh"). A first-boot / cleared-logs
  scenario is legitimate operator state, not an error.
- Corrupt JSON → `WARNING`. Tests cover `json.JSONDecodeError` and non-list
  top-level shapes.
- IO error → `WARNING`. Permissions / locked file / disk glitch — surface
  but don't crash.

**`TRADE_MEMORY_PATH` exposed at module level** so tests monkeypatch via
`position_manager.TRADE_MEMORY_PATH = <tmp>` without touching the real file.
Resolved via `sys.modules[__name__].TRADE_MEMORY_PATH` inside
`_load_trade_history` so the monkeypatch is picked up at call time.

**Schema-preserving row copy** — `trade_memory.json` rows carry many fields
(`pnl_dollars`, `exit_time`, `bot_id`, `strategy`, `sub_strategy`, …). Rows
are inserted as-is; downstream consumers pick what they need.

**Tests: 7 passing.** Coverage: missing file / corrupt JSON / wrong shape /
happy path / monkeypatched path / schema preservation / IO error.

## P0.2 OIF Author Tag + OIFGuard
_(to be filled in by S2)_

## P0.3 Runtime Reconciliation
_(to be filled in by S4)_

## P0.4 Mandatory OIF Verification
_(to be filled in by S3)_

## P0.5 Scale-Out Race Fix
_(to be filled in by S5)_

## P0.6 Close-Position Verification
_(to be filled in by S6)_
