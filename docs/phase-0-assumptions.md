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

**Author-tag format: `phoenix_<pid>_` prefix on every OIF filename.**

Chosen over more elaborate schemes (HMAC signature of content, explicit
manifest sidecar) because:
- **O(1) filename-only check** on the NT8 guard side — the guard must beat
  ATI's open+read+parse, and filename regex is the fastest possible check.
- **Any pid accepted.** Multiple Phoenix processes (prod/sim/lab) write
  concurrently — rejecting anything but "my pid" would drop legitimate
  OIFs from sibling bots. The `_PHOENIX_PID` per-process stamp exists for
  forensic attribution in logs, not for access control.
- **Tamper-resistance is NOT a design goal.** The threat model is
  pytest / accidental / benign misconfiguration — not a hostile attacker
  with filesystem access. A crypto-strong author tag buys nothing against
  that threat model and costs speed + C# complexity.

**PhoenixOIFGuard deploys as an AddOn, not an Indicator.** AddOns run at
NT8 platform startup regardless of chart state. Indicators only run when
attached to a chart — unreliable for a guard that must always be active.

**Race win vs ATI parser:** both FileSystemWatcher consumers receive
Created events independently. The guard wins because (a) regex check is
nanosecond-level, (b) File.Move within the same volume is an atomic
rename — orders of magnitude faster than ATI's parse+execute cycle. Even
under heavy load the guard completes before ATI finishes reading. If the
guard loses a race (file vanishes before Move), that's logged as WARN
and we carry on — ATI already processed it, nothing to do.

**Filesystem layout:**
- `.../NinjaTrader 8/incoming/` — watched folder (ATI's own target)
- `.../NinjaTrader 8/quarantine/` — **NEW** — rogue files go here with
  `<ts>__<original_name>` format so successive rogues don't clobber.
- `.../NinjaTrader 8/log/PhoenixOIFGuard.log` — append-only event log.

**No .tmp→.txt atomic staging required on the Python side any more:**
earlier `_stage_oif` variants wrote a `.tmp` then renamed. Current
implementation writes directly to the final filename. The author-tag
defense makes the old atomic-rename dance unnecessary — NT8 can safely
pick up the file as soon as it appears because the guard has already
verified the author.

**Tests: 14 new.** Breakdown:
- 9 tests cover `write_oif` / `write_bracket_order` / `write_partial_exit`
  / `write_be_stop` — every public entrypoint emits tagged filenames.
- 5 tests lock the naming convention into a Python-side regex mirror so
  any future change to the prefix shape breaks the tests (and therefore
  must be consciously coordinated with the C# regex in OIFGuard).

**Manual deployment by Jennifer (one-time):**
1. Copy `ninjatrader/PhoenixOIFGuard.cs` into
   `%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\`
2. Open it in NT8 NinjaScript Editor → F5 to compile
3. Restart NT8 (AddOns load at platform startup only)
4. Confirm NT8 Output shows `[PhoenixOIFGuard] Watching <path>`
5. Optional smoke test: drop a `rogue_test.txt` PLACE command into
   incoming/ — should appear in quarantine/, not execute on chart.

## P0.3 Runtime Reconciliation
_(to be filled in by S4)_

## P0.4 Mandatory OIF Verification
_(to be filled in by S3)_

## P0.5 Scale-Out Race Fix
_(to be filled in by S5)_

## P0.6 Close-Position Verification
_(to be filled in by S6)_
