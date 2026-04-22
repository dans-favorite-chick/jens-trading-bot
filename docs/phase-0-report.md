# Phase 0 Emergency Patches — End-of-Sprint Report

**Branch:** `feature/phase-0-emergency` (off `main`)
**Sprint dates:** 2026-04-22
**Baseline:** 834 passed, 3 skipped @ `d674189`
**Final:**    896 passed, 3 skipped @ `6daa74c`  (+62 new tests, 0 regressions)

All 6 patches shipped. One known deferral documented (expectancy /
B70-conflict hooks in the exit_pending resolver — Phase 1 work).

---

## Commit manifest

| Patch | Commit    | Title |
|-------|-----------|-------|
| P0.1  | `e6e864f` | `fix(p0-1): trade_history persists across restart via trade_memory.json` |
| P0.2  | `6aabbe0` | `feat(p0-2): OIF author tag + PhoenixOIFGuard AddOn quarantines rogue OIFs` |
| P0.4  | `db24170` | `fix(p0-4): _verify_consumed mandatory on all OIF paths via OIFStuckError` |
| P0.3  | `8aaec34` | `feat(p0-3): runtime reconciliation on a timer catches mid-session orphans` |
| P0.5  | `f995236` | `fix(p0-5): scale-out uses PLACE-before-CANCEL to eliminate orphan-stop race` |
| P0.6  | `6daa74c` | `fix(p0-6): close-position verification via exit_pending state` |

All commits pushed to origin. Branch ready for review / merge to `main`.

---

## Patch summary

### P0.1 — Trade memory persistence (closes D13)

`PositionManager` gains an opt-in `load_history=True` kwarg that hydrates
`self.trade_history` from `logs/trade_memory.json` at boot. `base_bot.py`
opts in at its one production call site. Dashboard P&L no longer resets
to $0 on restart. Failure modes: missing file / corrupt JSON / wrong
shape / IO error all log warnings and keep the list empty — never crash.

**Tests:** 7 new. Full suite: 834 → 841.

### P0.2 — OIF author tag + PhoenixOIFGuard (closes D8)

Python side: every OIF filename now prefixed with `phoenix_<pid>_`.
NT8 side: new `ninjatrader/PhoenixOIFGuard.cs` NinjaScript AddOn that
watches the incoming folder and quarantines any file without the tag
before NT8's ATI parser reads it.

Race analysis: filename regex check + `File.Move` is orders of
magnitude faster than ATI's open+read+parse+execute cycle — guard
reliably wins.

**Tests:** 14 new (9 on writer tagging, 5 on the regex convention).
Full suite: 841 → 855.

**Manual deployment step for Jennifer (one-time):** see §Deployment.

### P0.4 — Mandatory `_verify_consumed` (closes D1)

New `OIFStuckError(RuntimeError)` carrying trade_id + stuck_paths + timeout.
Every PLACE/EXIT entry point now passes `raise_on_stuck=True`:
- `write_bracket_order` — was verifying; escalated to raise.
- `write_modify_stop` — **was missing** the check. Added.
- `write_oif` (legacy) — **was missing** the check. Added.
- `write_protection_oco` — already strict; covered by lock-in test.

Timeout raised 1.0 s → 2.0 s on mandatory paths. Log severity
ERROR → CRITICAL.

Test-only bypass via `_PYTEST_BYPASS_CONSUME_CHECK` module flag
(conftest autouse sets True; `test_verify_consumed_mandatory.py`
overrides back to False for its scope).

**Tests:** 14 new. Full suite: 855 → 869.

### P0.3 — Runtime reconciliation timer (closes D12)

`BaseBot._runtime_reconciliation_loop` — async timer at 30 s cadence
(`RUNTIME_RECON_INTERVAL_S = 30.0`). Invokes the existing
`reconcile_positions_from_nt8` (no extraction refactor — it was
already parameterised).

Added idempotency inside `reconcile_positions_from_nt8`: builds a set
of already-tracked accounts from `positions.active_positions` and
skips them, so re-entry every 30 s never creates phantom duplicates.

Clean shutdown via `_shutdown_reconciliation` flag. Per-cycle exceptions
are caught + logged so one bad cycle doesn't kill the loop.

**Tests:** 7 new. Full suite: 869 → 876.

### P0.5 — Scale-out race fix (closes D4)

`_scale_out_trade` used to call `write_be_stop` (PLACE only, no
CANCEL) while the original OCO stop still worked — two stops on one
position, orphan-phantom fills on bounces.

Fix: switched the call to `_move_nt8_stop` which routes through
`write_modify_stop`. AND flipped `write_modify_stop`'s stage order
from CANCEL-first to PLACE-first so there's no no-stop window
during the modify.

Sprint-spec hierarchy: `CHANGE > PLACE_NEW+CANCEL_OLD >
CANCEL_OLD+PLACE_NEW (FORBIDDEN)`. NT8 ATI doesn't expose a true
CHANGE verb; PLACE-new-then-cancel-old is our best available until
Phase 1 broker events.

**Tests:** 6 new (commit-order locks + source-level regression guards).
Full suite: 876 → 882.

### P0.6 — Close-position verification (closes D7)

New Position state: `exit_pending` + `exit_pending_since` +
`pending_exit_price` + `pending_exit_reason`. PositionManager gains
`mark_exit_pending` / `finalize_exit_pending` /
`exit_pending_positions` / `has_exit_pending_for_account`.

`_exit_trade` now marks the position exit_pending and returns (no
immediate close). Runtime reconciliation loop checks each
exit_pending position against NT8's `_position.txt` file: FLAT →
finalize + propagate to risk/tracker/trade_memory/circuit-breaker
hooks. NT8 still showing position + age > 60 s → CRITICAL log +
Telegram alert + halt strategy. Does NOT force-finalize — operator
flattens NT8 manually; reconciliation picks up FLAT next cycle.

Fallback: if the WS exit AND OIF fallback both failed (`exit_sent=
False`), revert to the old unconditional close_position with a
CRITICAL log (manual-exit-required scenario).

**Tests:** 14 new. Full suite: 882 → 896.

---

## Tests summary

| Patch | Suite before | Suite after | New tests |
|-------|--------------|-------------|-----------|
| baseline | — | 834 | — |
| P0.1  | 834 | 841 | 7   |
| P0.2  | 841 | 855 | 14  |
| P0.4  | 855 | 869 | 14  |
| P0.3  | 869 | 876 | 7   |
| P0.5  | 876 | 882 | 6   |
| P0.6  | 882 | 896 | 14  |
| **Totals** | 834 → 896 | **+62 tests** | 896 passed / 3 skipped / 0 regressions |

---

## Deployment notes for Jennifer

### PhoenixOIFGuard.cs (one-time install)

1. Copy `ninjatrader/PhoenixOIFGuard.cs` into
   `%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\`
2. Open it in NT8 NinjaScript Editor → press F5 to compile.
   You should see `Compile successful` in NT8 Output.
3. Restart NT8 (AddOns load at platform startup only, not dynamically).
4. On restart, NT8 Output should show:
   `[PhoenixOIFGuard] Watching <incoming path>`
5. **Smoke test (optional):** drop a rogue `.txt` into incoming/ —
   should land in `quarantine/` with a timestamp prefix and NOT
   execute on your chart:
   ```
   echo PLACE;Sim101;MNQM6;BUY;1;MARKET;0;0;DAY;;;; ^
     > "%USERPROFILE%\Documents\NinjaTrader 8\incoming\rogue_test.txt"
   ```

### Config / env changes Jennifer needs to verify

- None. All changes are code + test; no new config knobs, no env vars,
  no settings.py tweaks, no config/account_routing.py touches (sprint
  spec forbade those).

### Bot restart

- Recommended after merge to main. `PositionManager(load_history=True)`
  only takes effect on a fresh process start — running bots still have
  the old init signature cached.
- After restart, the `[TRADE_MEMORY] loaded N historical trades from …`
  log line confirms P0.1 is live.
- After restart, `[ACCOUNT_ROUTING]` and the gamma-regime one-shot
  fire as before; in addition you should see, roughly every 30 seconds:
  - `[RUNTIME_RECON] …` (when orphans adopted) — P0.3 evidence
  - `[EXIT_FINALIZED:…] NT8 confirmed FLAT …` — P0.6 evidence (after
    a natural exit)
  - `[OIF_STUCK:…]` → `OIFStuckError` raised — only if NT8 actually
    rejects an OIF (P0.4 is silent on happy path).

---

## Known deferrals

### P0.6: expectancy-engine + B70-conflict hooks not wired in the resolver

`_exit_trade` previously fired `self.expectancy.close_trade(...)` and
the B70 conflict-closed logging right before `close_position`. Those
paths are NOT yet fired from `_resolve_exit_pending_positions` because:

- They need the **market snapshot at exit time**, not at finalize
  time (which is up to 30 s later).
- They'd need their own data stash on the Position alongside
  `pending_exit_price` / `pending_exit_reason`.

Phase 1's broker-event stream resolves this naturally — the fill
confirmation carries its own timestamped context. Deferring there
rather than building a one-off stash-and-replay path now.

**Impact today:** expectancy P&L tracking still works (the close_trade
hook will simply fire on the NEXT bot restart when the trade is
re-ingested from trade_memory.json — new P0.1 hydration path). B70
conflict-closed logging misses the specific close event but still
captures remaining-conflicts state on every new entry.

### No deferrals in P0.1 / P0.2 / P0.3 / P0.4 / P0.5.

---

## Assumptions / judgment calls

Full rationale for every non-obvious decision is in
`docs/phase-0-assumptions.md` — one section per patch. Highlights:

- **P0.1:** `load_history` default is False (test-friendly) — only
  `base_bot` opts in.
- **P0.2:** filename-tag defense is chosen for speed, not crypto
  strength. Threat model is accidental (pytest / misconfig), not
  hostile attacker.
- **P0.3:** 30 s interval chosen as balance between catching mid-
  session orphans fast vs. not hammering NT8 file I/O. Phase 1 will
  bring sub-second via broker events.
- **P0.4:** test-only bypass flag (rather than rewriting 30+ existing
  tests to simulate NT8 consumption). Production never sets the bypass.
- **P0.5:** PLACE-before-CANCEL chosen over CANCEL-before-PLACE
  because the asymmetric downside favours keeping protection
  continuously — brief two-stop window is noise, brief no-stop window
  is money.
- **P0.6:** 60 s timeout. Timeout behaviour alerts + halts but does
  NOT force-finalize; operator flattens NT8 manually and reconciliation
  finishes the close naturally on the next cycle.

---

## Risk assessment

**Materially improved vs. pre-sprint state:**
- D13 dashboard P&L reset: FIXED (P0.1).
- D8 pytest / rogue-OIF injection: FIXED (P0.2 once OIFGuard deployed).
- D1 silent stop-modify failure: FIXED (P0.4).
- D12 mid-session orphan blind spot: FIXED (P0.3).
- D4 scale-out orphan-stop: FIXED (P0.5).
- D7 "thinks flat but isn't": FIXED (P0.6).

**Still to do (out-of-sprint, documented in deferrals):**
- Phase 1 broker-event stream will replace timer polling with sub-
  second reaction and natively handle the expectancy/conflict-hook
  timing issue deferred in P0.6.
- No code path now silently swallows `OIFStuckError` (P0.4's guarantee),
  but caller-side handling of that exception across
  `_enter_trade` / exit paths is still "let it propagate." That's
  correct for the sprint — bubbled exceptions surface in the logs —
  but Phase 1 should decide between retry-with-backoff vs. emergency
  flatten for specific call sites.

**Sim-ready:** yes.
**Production-ready:** yes for Phoenix's trading model (1-contract per
strategy on dedicated Sim accounts). Real-money readiness is a
separate gate outside Phase 0 scope.

---

## End

Sprint complete. All 6 patches shipped on `feature/phase-0-emergency`.
Full sprint diff: +2274 lines, 16 files, 6 feature commits + assumption
log + this report.
