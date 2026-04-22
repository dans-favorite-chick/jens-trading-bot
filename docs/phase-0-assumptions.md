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

**Interval: 30 seconds** (`BaseBot.RUNTIME_RECON_INTERVAL_S`). Rationale:
- Fast enough to catch a mid-session orphan before a 4pm flatten
  (trading day has ~390 min of live hours → 30s = 0.13% of the day).
- Slow enough not to saturate NT8 with file reads — NT8 writes
  `*_position.txt` every tick, a 30s read rhythm is < 0.01% of that.
- Phase 1 will replace this timer with a broker-event stream; 30s is
  interim. Exposed as a module-level constant so tests monkeypatch it
  (all loop tests use 10 ms intervals to finish in fractions of a second).

**Reused existing `reconcile_positions_from_nt8` — no extraction refactor.**
The function was already parameterised with the `positions` manager and
writers, so the runtime timer just invokes it periodically. The sprint
plan's "extract into reusable function" step was already done at B77.

**Idempotency fix inside `reconcile_positions_from_nt8` (not in the loop).**
Before P0.3, calling the function twice against the same NT8 state
adopted the same orphan twice — duplicate Position records with
different `trade_id`s. Fix: scan `positions.active_positions` once at
the top of the function, build a set of already-tracked accounts, and
skip any account present in it. Means the runtime timer can re-enter
the function every 30s safely.

**Clean shutdown via `_shutdown_reconciliation` flag,** checked both
before and after each sleep so the loop doesn't hang for 30s after a
bot-stop signal. Exceptions in any single cycle are caught, logged,
and the loop carries on — one bad cycle doesn't kill the whole timer.

**Telegram notify hook reused from B77 adoption path.** The function
already fires a telegram alert per adopted orphan; no new wiring.
Mid-session orphans surface as "⚠️ Reconciled orphan …" messages.

**Tests: 7 new.** Coverage:
- 3 tests on `reconcile_positions_from_nt8` idempotency (first call
  adopts; second call doesn't re-adopt; new orphans on different
  accounts DO get adopted on the second call).
- 3 tests on the async loop (invocation cadence / shutdown flag
  respected / survives mid-cycle exception).
- 1 test verifying the telegram hook fires with account + direction.

Async tests use plain `asyncio.run()` rather than pytest-asyncio (not
installed in this env) — each test is a sync function that builds and
runs a coroutine.

## P0.4 Mandatory OIF Verification

**New `OIFStuckError(RuntimeError)`** with `trade_id` / `stuck_paths` /
`timeout_s` attributes. Subclass of `RuntimeError` so existing
`except RuntimeError:` catches still work — but callers SHOULD
`except OIFStuckError:` explicitly so a stuck OIF can't be silently
buried inside a broad exception handler.

**`_verify_consumed` now logs `CRITICAL` (was `ERROR`).** This is an
execution-layer integrity failure, not a recoverable per-trade glitch —
log severity matches.

**`raise_on_stuck: bool = False` kwarg** on `_verify_consumed`. Default
False for back-compat with any ad-hoc / diagnostic callers; every
PLACE/EXIT entry point inside `oif_writer.py` now passes True:
- `write_bracket_order` — was already verifying, escalated to raise
- `write_modify_stop` — **was missing** the check; added + raise
- `write_oif` (legacy) — **was missing** the check; added + raise
- `write_protection_oco` — already had bespoke retry-and-verify; locked
  in as "must surface stuck" via the test

**Timeout raised from 1.0s → 2.0s** on the new mandatory calls. 1s was
tuned for happy-path NT8; 2s gives the ATI parser a more realistic
budget under load before we declare stuck.

**Test-only bypass via `_PYTEST_BYPASS_CONSUME_CHECK` flag.** 30+
existing tests write OIFs to a tmp dir with no simulated NT8 consumer;
they would all trip the new raise. Rather than rewrite 30 tests to
simulate consumption, the autouse conftest fixture sets the flag True
so the check becomes a no-op. `test_verify_consumed_mandatory.py` flips
it back to False inside its own autouse fixture — the only test module
that actually exercises the check semantics.

Alternative considered + rejected: monkeypatching `_commit_staged` to
delete files immediately would have simulated NT8 consumption in tests.
Rejected because some tests assert on file existence post-write
(`test_p1_legacy_atomic.py` in particular). The bypass flag is more
honest: these tests don't care about the consume-check, so bypass it
explicitly.

**Tests: 14 new.** Coverage:
- `OIFStuckError` shape (3 tests): subclass, forensic attrs, message.
- `_verify_consumed` core (4 tests): happy path / stuck-legacy-return /
  stuck-raise / raise-but-nothing-stuck.
- Every PLACE/EXIT entry point under a mocked stuck-filesystem: 6 tests
  (bracket / modify_stop / exit / place_stop / cancel_single /
  protection_oco).
- Happy-path: NT8 simulated as consuming → no raise, write succeeds.

**Caller responsibility (NOT in this patch):** base_bot's
`_enter_trade` / exit / scale-out paths should treat OIFStuckError as
either (a) alert + retry with bounded backoff or (b) emergency flatten.
Catching-and-ignoring defeats the defence. This is in-scope for S4/S5/S6
since those streams touch the caller sites.

## P0.5 Scale-Out Race Fix

**Root cause analysis.** Before P0.5, `_scale_out_trade` called
`write_be_stop` (pure PLACE, no CANCEL) to place a NEW break-even stop
after the partial-exit market fill. Meanwhile the ORIGINAL OCO stop —
auto-reduced by NT8 from qty=2 → qty=1 when the partial fill cleared —
remained working at its original trigger price.

Result: TWO stops on the same position. If price moved adversely, the
BE stop fired first (closing the position). If price then bounced back
past the ORIGINAL stop, NT8 placed a REVERSAL fill — the orphan-phantom
SHORT/LONG signature seen on 2026-04-22.

**Fix.** Switched `_scale_out_trade` from `write_be_stop(...)` to
`_move_nt8_stop(pos, pos.entry_price, be_price)`. `_move_nt8_stop`
threads through the existing B76 `write_modify_stop` cancel+replace
path, which uses `pos.stop_order_id` (captured when the bracket was
entered) to target the exact OCO stop leg — no dangling stops.

**Commit-order ordering in `write_modify_stop` (critical).** Previously
staged CANCEL-old THEN PLACE-new. `_commit_staged` walks the list in
order, so the cancel committed first — brief no-stop window until the
new stop lands. P0.5 flipped the stage order: PLACE-new FIRST, CANCEL
SECOND.

Sprint-spec hierarchy:
```
CHANGE > PLACE_NEW + CANCEL_OLD > CANCEL_OLD + PLACE_NEW  (FORBIDDEN)
```
NT8 ATI doesn't expose a true CHANGE verb via OIF, so the middle
option is our best available. CHANGE-via-event-stream is Phase-1 work.

**Why PLACE-before-CANCEL is safe even with a brief two-stop window:**
if price races past both triggers during the ~100 ms window, NT8 fills
the FIRST-touched stop — which is the better-priced one for our P&L
direction. The second stop then tries to fire on a flat position and
NT8 rejects it cleanly. Worst case: one unnecessary round-trip of
OIF noise. Contrast with CANCEL-first: if price gaps in the no-stop
window, we're exposed to unlimited loss. Asymmetric: PLACE-first's
downside is noise, CANCEL-first's downside is money.

**Tests: 6 new.** Coverage:
- `write_modify_stop` commit order: new-stop file FIRST, cancel file SECOND.
- First file body starts with `PLACE…STOPMARKET`, second with `CANCEL`.
- Source-level grep on `_scale_out_trade`: MUST NOT contain
  `write_be_stop(` call (regression guard for the orphan-stop bug).
- Source-level grep on `_scale_out_trade`: MUST contain `_move_nt8_stop`.
- Source-level grep on `_move_nt8_stop`: MUST route through
  `write_modify_stop` (not a bare place path).
- Source-order check inside `write_modify_stop`: `stop_replace` stage
  must appear before `stop_cancel` stage in the function source.

Alternative considered + rejected: passing a `cancel_old_stop=True` kwarg
to `write_be_stop` so it internally does the cancel+replace. Rejected
because `write_modify_stop` already exists for exactly that purpose;
duplicating the logic inside `write_be_stop` would fork the cancel-replace
policy into two places — higher maintenance burden, higher drift risk.

## P0.6 Close-Position Verification

**New Position fields:** `exit_pending: bool`, `exit_pending_since: float`,
`pending_exit_price: float`, `pending_exit_reason: str`. Default
empty — a freshly opened position has no pending exit.

**New PositionManager methods:**
- `mark_exit_pending(trade_id, exit_price, exit_reason, now=None)` —
  flips the flags but keeps the Position in `_positions`. Dashboard
  and `is_flat` / `active_count` continue to see it. Idempotent-ish:
  calling twice just refreshes timestamps.
- `finalize_exit_pending(trade_id)` — completes the close using the
  stashed exit price/reason/time. Returns the same trade dict that
  `close_position` does, so post-close hooks work identically. **No-op
  (returns None + WARNING log) if the position is NOT in pending
  state** — prevents silent close bugs if the resolver is called on a
  still-live position.
- `exit_pending_positions()` — list accessor for the resolver.
- `has_exit_pending_for_account(account)` — entry-side gate. Callers
  that open new signals should check this to prevent double-fills
  during the reconciliation window.

**Timeout: 60 seconds** (`BaseBot.EXIT_PENDING_TIMEOUT_S`). Rationale:
- NT8 sim fills usually happen within 1–3 seconds. 60s is 20× the
  happy-path window — anything longer is a real failure mode (ATI
  reject, bridge crash, connection loss).
- Module-level constant so tests monkeypatch it.

**Timeout behaviour: CRITICAL log + Telegram + halt strategy. Does NOT
force-finalize.** Critical distinction: if Python silently marks the
position closed while NT8 still has a live position, the operator loses
visibility of the real risk. Better to leave the position tagged
exit_pending, halt new entries on its strategy, and surface the alert
so the operator flattens manually and then reconciliation picks up the
natural FLAT state.

**`_exit_trade` wired to the new path:** if the exit WS-send succeeded
it calls `mark_exit_pending(...)` and returns `trade=None` — no
immediate close, no immediate trade_history append. If BOTH the
WS-send AND the OIF fallback failed (`exit_sent=False`), we fall back
to the old unconditional `close_position` to avoid leaking a Position
record nobody will ever close — but log CRITICAL because this is the
manual-exit-required scenario.

**Post-close hooks moved into resolver** (`_resolve_exit_pending_positions`
inside the runtime-reconciliation loop): when NT8 confirms FLAT and
`finalize_exit_pending` returns a trade dict, the resolver fires the
same `risk.record_trade` / `trade_memory.record` / `tracker.record_trade`
/ `_on_trade_closed` hooks that `_exit_trade` used to fire synchronously.
Hooks are each wrapped in try/except — one bad consumer doesn't stop
the others from running.

**Known deferral: expectancy engine + conflict resolution logging.**
`_exit_trade` also called `self.expectancy.close_trade(...)` and the
B70 conflict-closed logging path BEFORE close_position. Those paths
aren't wired into the resolver yet because (a) they need market-snap
context captured at exit time, not at finalize time; (b) they'd need
their own data stash on the Position alongside pending_exit_price /
pending_exit_reason. Flagged in `docs/phase-0-report.md` as a known
gap — Phase 1 broker-event stream will resolve it naturally.

**Tests: 14 new.**
- `mark_exit_pending`: 3 tests (flips flags, keeps position, refuses
  unknown trade_id).
- `finalize_exit_pending`: 4 tests (produces trade record, removes
  from active, appends to trade_history, no-op when not pending).
- Entry blocking: 4 tests (no-pending unblocked / pending blocks
  same account / doesn't block other accounts / pending_positions
  accessor returns only pending).
- Runtime resolution: 3 tests (finalize when NT8 FLAT / stays pending
  when NT8 still shows position / CRITICAL fires after timeout).
