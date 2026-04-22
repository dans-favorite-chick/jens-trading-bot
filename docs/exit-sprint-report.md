# Exit Architecture + Safety Sprint — Final Report

_Completed: 2026-04-22 ~11:30 CDT._
_Branch: `feature/exit-audit-and-safety` (off main `854a602`)._
_Test count: **714 baseline → 743 final** (+29 net, 0 fails, 3 skipped)._

---

## ✅ Summary of issues — status at sprint close

| # | Issue | Severity | Status | Ship |
|---|---|---|---|---|
| 1 | bias_momentum "600-point stop" | HIGH | ✅ **Misdiagnosed — was target, not stop** | B62 sanity gate still catches if regresses |
| 2 | Targets not firing on winners | HIGH | ✅ **Diagnosed + BOTH-leg verify shipped** | B63/B64 (1cd3b38, 92a36af) |
| 3 | noise_area `target_rr=0` (guaranteed-loss) | HIGH | ✅ **FIXED** | B61 (eee96ba) |
| 4 | Opening strategies silent 8:30-10:00 AM | MED | ⚠️ **Real root cause found; structural fix deferred** | B66 observability (2f34042); R1 deferred |
| 5 | B59 live-account hard-guard | HIGH | ✅ **SHIPPED** | B59 (244b031) |
| 6 | 955 null-bot_id trade_memory rows | LOW | ✅ **BACKFILLED + write-guard** | B69/B70 (1bda9f4, fd6b295, 53d0011, 7f69c47) |

---

## 🎯 Stream outcomes

### S1 — Stop/target math audit

**Commit**: `eee96ba feat(b61,b62): fix noise_area target=entry bug; add universal stop/target sanity gate`

**Root cause reveal (important):**
- Jennifer's report was "bias_momentum LONG @ 26834, stop 27434 (600pt wrong direction)"
- **The 27434 figure was actually the TARGET, not the stop.** Actual trade: LONG @ 26834.5, stop=26804.25 (30pt below ✓ correct), target=27434.5 (600pt above).
- 600pt target is produced by base_bot `_RIDER_STRATEGIES` block forcing `target_rr=20.0` for bias_momentum/dom_pullback against a 120t-clamped stop. That's 20× RR — extreme but mathematically correct; worth a separate review whether that's intentional.
- **No bias_momentum math bug existed.** All 11 strategies' stop/target formulas produce correct geometry (see `docs/stop_target_math_audit.md`).

**Real bug found + fixed:**
- **noise_area** uses managed-exit with `target_price=None` + `target_rr=0`. base_bot synthesized `target = entry + stop_ticks × tick × 0 = entry` → OCO attached a TP at the entry price → every fill triggered TP instantly for commission loss (-$1.72 per trade).
- Fix: base_bot now detects managed-exit signals and substitutes a 300t safety-net OCO target so the stop leg still attaches while the strategy's `exit_trigger` drives the real exit.

**B62 Universal sanity gate (new):**
- `_sanity_check_entry()` in `bots/base_bot.py` called in `_execute_trade` after stop/target resolution, BEFORE OCO submission
- Rejects wrong-side geometry (LONG with stop above entry, SHORT with target above entry, etc.)
- Rejects distances outside 5-200 ticks
- Logs `[STOP_SANITY_FAIL] strategy=X entry=Y stop=Z target=W reason=...` CRITICAL
- 18 new tests in `tests/test_stop_target_sanity.py`

### S2 — Target fire verification

**Commits**:
- `1cd3b38 feat(b63): verify both OCO legs after PROTECT + retry`
- `92a36af feat(b64): target-miss forensic audit report + sprint notes`

**Audit finding (2026-04-21 + 2026-04-22 sim sample):**
- 7/7 trades with MFE ≥ 20 ticks exited **without** `target_hit` (100% target-miss rate on winners-that-ran)
- **Root cause is strategy-side, not OCO-side**: 7/7 non-target exits were `ema_dom_exit` / `trend_stall` / `time_stop` firing BEFORE price reached the LIMIT
- Two biggest winners (spring_setup LONG 105t and 83t MFE) had targets placed 150+ points away — physically unreachable within a 5-minute hold
- No active OCO half-attach bug observed today; `incoming/` empty at audit time

**B63 shipped (preemptive defense):**
- After `write_protection_oco`, verify BOTH stop AND target files were consumed by NT8 (not just "one of them")
- Half-success path: `[PROTECT_HALF]` warning, single-leg retry
- Both-fail after retry: cleanup `CANCEL_ALL` then escalate to B55 emergency-flatten
- 4 new tests in `tests/test_target_verification.py`

**B64 (forensic audit)**: `docs/target_fire_audit.md` + sprint notes.
`[EXIT_FORENSIC]` / `[TARGET_MISS_SUSPECT]` logging was already inline in `bots/base_bot.py` via S1's commit.

**S2's follow-up flag for S1 team**: targets on spring_setup / bias_momentum may be tuned too far for realistic MFE. Consider a "let target work" zone in managed-exit triggers once MFE ≥ ~50% of target distance. Would reduce rider-strategy exit-before-target rate.

### S3 — Opening strategies silence

**Commit**: `2f34042 fix(b66): surface last sub-evaluator reason in opening_session`

**Root cause identified (contradicts original hypotheses):**
1. **Sim_bot first-start today was 09:07:52 CDT** — the bot wasn't running during 08:30-09:07. Premarket_breakout (08:30-08:45) had zero chance of firing.
2. **Structural defect — producer/consumer field-name mismatch:** `strategies/opening_session.py` reads `rth_5min_close_last`, `rth_1min_open/high/low/close/volume`, `avg_1min_volume` — **none of which are produced** by `SessionLevelsAggregator.get_levels_dict()` (emits `rth_5min_close` only, no 1m bar fields at all).
3. **Every one of the 88 opening_session evals from 09:08-10:22 returned `SKIP orb missing_fields`** — confirmed in logs.
4. Gamma-stale hypothesis CONFIRMED FALSE (Jennifer updated menthorq_daily.json this morning; file was ~2h old at bot start). B67 NOT shipped (would have been based on a false premise).

**What shipped (B66):**
- `OpeningSessionStrategy.last_skip_reason` property — silent `NO_SIGNAL` events now diagnostic
- 4 new tests in `tests/test_opening_session.py`

**What's deferred (R1):**
- Add 1m rolling-bar aggregator + avg-1m-volume EMA to `SessionLevelsAggregator`
- Spec written in `docs/opening_strategies_silence.md`
- Non-trivial (~100 LoC + tests); deserves its own slot with proper test coverage
- **Until R1 ships: opening_session sub-strategies (other than premarket_breakout and standalone ORB) CANNOT fire** because they depend on 1m bar fields that don't exist in the aggregator output

### S4 — Live-account hard-guard (B59)

**Commit**: `244b031 fix(b59): hard-guard against live-account OIF writes`

**5 guard points:**
1. `oif_writer._require_account()` — transitive cover of all PLACE/EXIT paths via `_reject_live_account()`
2. `oif_writer.cancel_all_orders_line()` — explicit check
3. `oif_writer.write_bracket_order()` — top-of-function guard
4. `oif_writer.write_protection_oco()` — top-of-function guard
5. `oif_writer.write_oif()` — top-of-function guard on legacy entrypoint

**Plus:**
- `bridge/bridge_server.py _handle_trade_command` — short-circuits with `[LIVE_GUARD] BLOCKED` log before dispatch
- `bots/base_bot.py _enter_trade` — aborts after routing resolution + Telegram alert (deduped)
- `BaseBot.__init__` startup banner: `[LIVE_GUARD] armed — any order to account 'XXXXXXX' will hard-fail`
- 10 unit tests in `tests/test_live_account_guard.py`, all pass

**Behavior:**
- If env has `LIVE_ACCOUNT=1590711`, any attempt to write OIF targeting that account raises `RuntimeError` BEFORE file IO
- If `LIVE_ACCOUNT` is empty/unset, guard is disarmed (logged as WARNING at startup)
- Paired with Jennifer disabling ATI on the live account in NT8 Control Center — belt-and-suspenders

### S5 — trade_memory hygiene

**Commits**:
- `1bda9f4 feat(b69-backfill): tools/backfill_bot_id.py + run against historical rows`
- `fd6b295 feat(b70-write-guard): core/trade_memory.record() rejects null bot_id`
- `53d0011 test(b69): data-integrity check for trade_memory.json`
- `7f69c47 test(b70): update b16 null-bot_id expectation to new 'unknown' default`

**Backfill summary (ran live):**
| bot_id | Count |
|---|---|
| prod | 0 |
| sim | 5 (+ 9 already populated before backfill) |
| legacy | 912 |
| unknown | 38 |
| **total backfilled** | **955** |

Backup: `logs/trade_memory.json.bak-20260422-112929`

**Write-time guard:**
- `TradeMemory.record()` now logs WARN and defaults to `bot_id="unknown"` if caller doesn't pass one
- Rejects explicit `None` with assertion
- Prevents future null-pollution

**Integrity test:**
- `tests/test_trade_memory_integrity.py` asserts every row has non-null bot_id ∈ {prod, sim, legacy, unknown}
- CI-friendly (skips if file absent)

---

## 📊 Test delta

| | Baseline | Final | Delta |
|---|---|---|---|
| Passed | 714 | **743** | +29 |
| Skipped | 3 | 3 | 0 |
| Failed | 0 | 0 | 0 |
| New test files | — | 4 | `test_stop_target_sanity.py`, `test_live_account_guard.py`, `test_trade_memory_integrity.py`, `test_opening_session.py` (extended) |

---

## 🚨 Remaining work flagged

### R1 — Opening session 1-minute bar aggregator (deferred from S3)
**Blocker for**: open_drive, open_test_drive, open_auction_in, open_auction_out, opening-ORB
**Scope**: extend `core/session_levels_aggregator.py` to maintain rolling 1m bar state + avg-1m-volume EMA, expose via `get_levels_dict()`
**Estimated effort**: ~100 LoC + tests
**Recommendation**: own sprint slot — structural, wants careful test coverage

### Bias_momentum + spring_setup target tuning (cross-stream flag)
S2's audit: rider-mode strategies (bias_momentum target_rr=20, spring_setup wide fixed-target) place targets at 150+ points, rarely reached. Strategy-side exits (`ema_dom_exit`, `trend_stall`, `time_stop`) fire first.
**Recommendation:** implement "let target work" zone — once MFE ≥ 50% of target distance, suppress managed-exit triggers to give price a chance to reach target. Out of sprint scope; flag for next cycle.

### Bias_momentum target_rr=20 review
Not a bug per se but worth Jennifer's eyes: is 20:1 R:R really intentional for rider-mode strategies? Current behavior: LONG entries use a 120-tick stop + 2400-tick target (= 600 points). Very wide target → most trades end at stop. Consider whether `_RIDER_STRATEGIES` should use trailing exit semantics instead of fixed 20:1.

---

## 🛠️ Current operational state

**Running processes:**
- Bridge :8765/6/7 — alive
- Dashboard :5000 — alive
- Watchdog :5001 — `auto_restart=True`, tracking `prod` + `sim`
- **ProdBot** (PID 65648) — pinned to Sim101 via FORCE_ACCOUNT (B57)
- **SimBot** (PID 34544) — routes 16 dedicated accounts, full B39/B47/B50/B55/B58 hardening
- Both bots have B59 live-guard armed

**Active positions (NT8):**
- SimNoise Area: LONG 1 @ 26950.5 (OCO protected)
- SimSpring Setup: LONG 1 @ 26984.0 (OCO protected)

**Account 1590711 (live)**: FLAT; ATI guarded at code level (B59), should be disabled in NT8 Control Center separately.

---

## 📦 Branch state

```
feature/exit-audit-and-safety (9 commits ahead of main):
  92a36af  feat(b64)       target-miss forensic audit report + sprint notes
  1cd3b38  feat(b63)       verify both OCO legs after PROTECT + retry
  eee96ba  feat(b61,b62)   noise_area target=entry fix + sanity gate
  2f34042  fix(b66)         opening_session.last_skip_reason observability
  7f69c47  test(b70)        b16 null-bot_id now defaults to unknown
  53d0011  test(b69)        trade_memory integrity check
  fd6b295  feat(b70)        trade_memory.record() rejects null bot_id
  1bda9f4  feat(b69)        tools/backfill_bot_id.py + backfill run
  244b031  fix(b59)         live-account hard-guard + 10 tests
```

All pushed to origin.

**Ready to merge `feature/exit-audit-and-safety` → `main`** once S2 lands (or merge now and S2 lands on a follow-up branch).

---

## Questions for Jennifer

1. **Bias_momentum target_rr=20**: intentional wide target, or should it scale with stop distance / use trailing exits instead?
2. **R1 (1m bar aggregator for opening strategies)**: next sprint slot, or defer until opening strategies get their own redesign pass?
3. **Merge feature/exit-audit-and-safety to main now, or wait for S2?**
