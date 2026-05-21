# Bugs & TODOs — Running List

**Purpose:** Single canonical list of things noticed during work that need
fixing or future attention. Operator's instruction (2026-05-20):
"Every time you come across something that might need fixing or might be a bug,
something that we might need to look at later, I want you to keep a running list
of them so that we don't forget."

**How to use this doc:**
- ANY agent (sub-agent or main session) adds new items here when discovered
- Items kept until explicitly closed (commit + cite when closing)
- Status emoji: 🔴 = bug needing fix · 🟡 = caveat to watch · 🟢 = fixed · ⚪ = informational
- Severity: CRITICAL (production impact), HIGH, MEDIUM, LOW

---

## 🔴 OPEN BUGS

### ~~B-001 — pandas 3.0 datetime precision idiom (HIGH)~~ → see B-CLOSED-005

### B-008 — Phase 13 target overrides silently no-op for late-pricing strategies (HIGH)
**Status:** FIXED in commit a03086e (deferred-recompute path), and again in
F-005 of pt2 audit (LIMIT-fill anchor). See B-CLOSED-007 below.

### B-009 — ChandelierPolicy + TimeExitPolicy never called per-bar (CRITICAL)
**Status:** FIXED in commit a03086e (per-bar enforcement loop in base_bot
`_on_bar()`). See B-CLOSED-008.

### B-010 — TWO sim_bot.py processes running simultaneously (HIGH)
**Discovered:** 2026-05-20 (ship audit pt2)
**Symptom:** PIDs 76700 + 66988 both running sim_bot.py since 2026-05-17 21:08:54.
Result: only `SimDom Pull Back` saw activity (~100 orders/day) — 27 other dedicated
NT8 accounts sat empty because the two bots raced and one was stuck.
**Fix:** Single-instance guard `core/single_instance.py` (F-009). Both
sim_bot.py and prod_bot.py now call `acquire_or_exit(bot_name)` at startup;
a duplicate prints a clear error + exits with code 17. Lives in commits
landing this session.
**Action needed (operator):** Kill BOTH existing sim_bot.py PIDs, then start
one fresh `python bots/sim_bot.py`. Future duplicates blocked by the guard.

### B-011 — DAILY_LOSS_LIMIT + PER_STRATEGY_DAILY_LOSS_CAP stuck at $1M sim values (HIGH)
**Status:** FIXED in commit a03086e (restored to $200 production values).

### B-012 — 8 strategies enabled=True that the plan kills or doesn't list (HIGH)
**Status:** PARTIALLY FIXED. Commit a03086e set validated=False on all 8.
F-004 (pt2) additionally set enabled=False on the 4 plan-killed ones:
orb_fade, compression_breakout_v2, compression_breakout_micro, orb_v2.
The 4 remaining (big_move_signal, dom_pullback, nq_lsr, footprint_cvd_reversal)
are still enabled=True but validated=False; sim_bot may still load them for
data collection until operator decides whether to enabled=False them too.

### B-013 — skip_on_stop_clamp left at False on 3 strategies (MEDIUM)
**Status:** FIXED in F-012 of pt2 audit. Restored to True on bias_momentum
+ vwap_pullback (disabled) + dom_pullback. The Phase 7 confirmation-stop
fallback is now wired so the clamp-skip is safe.

### B-014 — Universal lunch-hour skip filter never built (MEDIUM, +$5K/yr)
**Status:** FIXED in F-010 of pt2 audit. SKIP_HOURS_CT = [10, 11, 12, 13]
wired into base_bot._process_signal(). Strategies whose windows end before
10am CT (opening_session, asian) aren't affected. Per-strategy opt-out
via SKIP_HOURS_CT_EXEMPT in config/settings.py.

### B-015 — Plan-parity drift can land silently (process bug, HIGH)
**Status:** FIXED in F-011 of pt2 audit. `tests/test_plan_winners_parity.py`
adds 7 CI guardrails that fail if validated=True flips on a non-plan
strategy, if a plan winner is disabled, if a killed strategy is re-enabled,
or if routing drifts. The exact pattern that caused 2026-05-20's incident
will now fail in CI before commit.

### B-016 — _apply_phase13_overrides swallows import errors silently (HIGH)
**Status:** FIXED in B-005 of pt2 audit. Was `except Exception: return`
masking ImportErrors that disabled ALL Phase 13 overrides. Now logs
WARNING with exception detail.

### B-017 — Per-bar Phase 13 enforcement loop debug-only error handler (HIGH)
**Status:** FIXED in B-006 of pt2 audit. The per-bar loop that fires
ChandelierPolicy/TimeExitPolicy for 5 Phase 13 strategies was logging
errors at DEBUG; if the loop died the strategies silently fell back to
wide-bracket placeholder targets. Promoted to WARNING. Same fix for
legacy CHANDELIER (opening_session.orb) loop.

### B-018 — _PolicyPosAdapter initial_stop falls back to wrong value (MEDIUM)
**Status:** FIXED in B-008 of pt2 audit. `getattr(real_pos,
"initial_stop_price", real_pos.stop_price)` always returned 0.0
(dataclass default) instead of falling back to stop_price. Bug only
materialized if a Position was reconstructed from disk without
__post_init__ — but the fallback was the entire reason the code was
written that way. Fixed with `or real_pos.stop_price`.

### B-019 — Phase 13 wiring had ZERO test coverage (HIGH)
**Status:** FIXED in B-007 of pt2 audit. New `tests/test_phase13_overrides.py`
adds 16 tests covering `_apply_phase13_overrides`, `recompute_phase13_target`,
`_PolicyPosAdapter`, `_PolicyBarAdapter`. The 1.5R-vs-3R silent-no-op
bug fixed in commit a03086e would have been caught by these tests.

### B-020 — vwap_band_reversion validated=True but lab-only per plan (HIGH)
**Status:** FIXED in B-009 of pt2 audit. Demoted to validated=False.
Plan §1.1 lists 11 winners — vwap_band_reversion is in §1.2 with
`scale_out_1r + filter` exit, neither of which exist yet. Revert when
F-002 (scale_out_1r policy class) + F-003 (combo_ema_vol filter) ship.

### B-021 — MAX_ACTUAL_STOP_DOLLARS_PER_TRADE stuck at $100 sim value (HIGH)
**Status:** FIXED in B-010 of pt2 audit. Restored $100 → $50 per file
comment ("RESTORE before live").

### B-022 — opening_session 3 non-winner subs firing despite not being in plan (MEDIUM)
**Status:** FIXED in B-011 of pt2 audit. premarket_breakout,
open_auction_in, open_auction_out — all gated OFF by default; operator
can opt in via config "{sub}_enabled": True if they want them back.
Plan §1.1 ships only .orb and .open_drive.

### B-023 — Routing tests didn't cover the 4 new Phase 13 strategies (MEDIUM)
**Status:** FIXED in B-012 of pt2 audit. Added per-strategy assertions
for raschke_baseline, g_inside_bar_breakout, e_multi_day_breakout,
a_asian_continuation, and a regression test for the
vwap_band_reversion underscore-mismatch fix from 2026-05-19.

### B-024 — circuit_breakers Telegram alert failures silenced at debug (HIGH)
**Status:** FIXED in B-013 of pt2 audit. Two sites in
core/circuit_breakers.py logged Telegram dispatch failures at DEBUG —
including the HALT_BOT alert, which is the operator's last-resort
notification. Promoted both to CRITICAL.

### B-025 — vwap_pullback_v2 session-window SKIP logged at debug (MEDIUM)
**Status:** FIXED in B-014 of pt2 audit. New J.2 17:00-04:59 CT gate
(added 2026-05-20) was logging skips at DEBUG; operator running
RTH-only days would see 0 signals with no surface explanation.
Promoted to INFO.

### B-026 — pandas datetime-precision test masked import failures (LOW)
**Status:** FIXED in B-016 of pt2 audit. pytest.skip → pytest.fail
for the import-time exception path. A pandas 3.0 upgrade introducing
a new import error would have appeared as SKIPPED (green) in CI; now
correctly fails.

### B-027 — Universal 10-13:59 CT lunch-skip filter not implemented (MEDIUM, +$5K/yr)
**Status:** FIXED in F-010 of pt2 audit. SKIP_HOURS_CT = [10, 11, 12, 13]
added to config/settings.py; base_bot._process_signal() blocks all
signals during those hours per PHASE_13_IMPLEMENTATION_PLAN §A.3
"+$5K/year free". Per-strategy opt-out via SKIP_HOURS_CT_EXEMPT.

### B-028 — recompute_phase13_target used market price not LIMIT fill price (LATENT, MED)
**Status:** FIXED in F-005 of pt2 audit. The deferred-recompute was
called with `price` (market tick) even for LIMIT-entry strategies
where the fill is actually at `limit_price`. Added a second LIMIT-
specific recompute after limit_price is computed; chandelier-policy
strategies aren't affected today (10R wide bracket), but this paves
the way for scale_out_1r/fixed_rr on LIMIT strategies.





### B-002 — orb_v2 strategy only ever produced 1 trade in 5y backtest (MEDIUM)
**Discovered:** Section S validator output
**Location:** `strategies/orb_v2.py` evaluate() — gates may be impossibly strict
**Symptom:** `orb_v2` has 1 trade ever in 5y phoenix_real_5year.csv
**Action needed:** Diagnose like we did for bias_momentum (silent-stop bug ruled out — this is real gate rejection)
**Status:** Not blocking ship — strategy currently not enabled

### B-003 — opening_session.open_drive Bug B2 still has 30-day gap (LOW)
**Discovered:** Section O + validator output
**Symptom:** After Bug B2 fix, open_drive last fired 2026-04-14 (30d before data end). open_test_drive last fired 2026-02-16 (87d). Both classified as "legitimate gate rejection" not bug, but worth periodic check.
**Status:** Watch only — no action needed unless gaps grow much larger

---

## 🟡 OPEN CAVEATS / WATCH LIST

### W-001 — bias_momentum 56% max DD on compounding curve (HIGH)
**Documented:** Section S.6 + PHOENIX_BEST_PLAN.md §5.3
**Concern:** Operator must commit to staying in plan during this DD. Pre-commit + phased rollout required.
**Mitigation:** tier scale-down at 85% of ATH, consecutive-loss halving, daily 4% circuit breaker

### W-002 — In-sample WR may degrade out-of-sample (MEDIUM)
**Documented:** Section S.7
**Concern:** 70-80% WR on new strategies (inside_bar, multi_day, asian) is suspicious vs literature (50-65% typical). Expect mean-reversion to 60-70%.
**Mitigation:** Monitor monthly; if any strategy drops below baseline WR for 2 quarters, re-evaluate

### W-003 — Strategies with stops "TOO TIGHT" per MFE/MAE (MEDIUM)
**Documented:** Section T MFE/MAE table
**Strategies:** es_nq_confluence (ratio 2.36), raschke_baseline (1.68), asian_continuation (1.56), inside_bar_breakout (1.50)
**Mitigation:** Exit policy compensates (chandelier_50_3x for breakouts, time_30min for fast-resolving)
**Watch:** Monitor whether per-strategy P&L lags backtest by >20% in first quarter live

### W-004 — limit_5s order timeout not yet implemented (MEDIUM)
**Documented:** PHOENIX_BEST_PLAN.md §6.4
**Concern:** `_apply_phase13_overrides` sets entry_type=LIMIT for g_inside_bar + e_multi_day but doesn't implement the "cancel after 5 sec + market" fallback.
**Current behavior:** Plain LIMIT (sits as working order until next signal); usually fine but doesn't have the 5s timeout the analysis assumed.
**Action:** Implement in focused next sprint; estimated 1-2 hours

### W-005 — Full retest-wait loop deferred (LOW-MEDIUM)
**Documented:** PHOENIX_BEST_PLAN.md §6.3
**Concern:** `core/entry_modes.py` flags retest mode for 4 strategies but base_bot doesn't actually wait — submits market order anyway, just logs the intent.
**Expected lift if implemented:** +$3-4K/year per Section V.1
**Action:** Next sprint — requires per-strategy tick buffer + cancellation + timeout

### W-006 — Tick analysis based on only 2 months of TBBO (LOW)
**Documented:** Multiple sections
**Concern:** Section U + V tick conclusions used 2026-03-17 to 2026-05-15 data. Different volatility regimes may shift findings.
**Mitigation:** Re-run tick analysis after 6+ months of new TBBO data accumulated, OR purchase more historical TBBO via Databento ($463/yr)

### W-007 — Chandelier exits need per-bar position tracking in live (LOW)
**Documented:** PHOENIX_BEST_PLAN.md §6 deferred
**Concern:** `core/exit_policies.py ChandelierPolicy` has `should_exit()` but base_bot doesn't currently call it per-bar for active positions. Chandelier currently relies on the very-wide target placeholder.
**Action:** Wire base_bot to call exit_policy.should_exit() each bar close for open positions

---

## ⚪ INFORMATIONAL / DESIGN NOTES

### I-001 — TBBO data has multiple expirations + calendar spreads
**Documented:** Section Z (TBBO data hygiene)
**Note:** Always use `tools.tbbo_cache_builder.load_clean_ticks()` instead of re-parsing the raw .dbn.zst. Future tools that bypass this WILL hit the contamination issue.

### I-002 — Phoenix's silent failures = #1 historical bug class
**Documented:** `memory/feedback_silent_failures.md`
**Note:** EVERY new feature should fail LOUDLY (logger.warning, not logger.debug). Add coverage to `validate_backtest_quality.py` for any new backtest output.

### I-003 — "Strongest" S/R levels are anti-edge
**Documented:** Sprint B finding (commit c92b931)
**Note:** Counterintuitive empirical finding — S/R levels with strength ≥0.70 (heavily-tested) systematically UNDERPERFORM medium-strength levels (0.50-0.70). Heavily-tested levels accumulate stop liquidity; when they break they break with conviction. Not currently actionable but worth remembering if revisiting S/R.

### I-004 — `core/sr_zones.py` engine has zero production use cases
**Documented:** PHOENIX_BEST_PLAN.md §6.1
**Note:** Tested across 4 use cases (bounce, failed-hold, VETO, CONFLUENCE) — all negative or marginal. Keep as research toolkit. Revisit if a NEW use case emerges.

---

## 🟢 CLOSED (kept for reference)

### B-CLOSED-001 — Silent-stop bug in simulate_trade
**Fixed:** Commit `a9a5ef9`
**Impact:** Was corrupting bias_momentum (40 trades → 13,790 trades after fix) and several other strategies. Cost was ~$176K in unrecognized bias_momentum P&L over 5y.

### B-CLOSED-002 — Bug B2: open_drive pivot_pp target
**Fixed:** Commit `58878d2`
**Impact:** open_drive flipped from -$106K/5y loser to +$3.8K winner. $110K swing.

### B-CLOSED-003 — Bug B3: orb_fade wallclock freshness check
**Fixed:** Commit `58878d2`
**Impact:** orb_fade went from 0 signals in 5y backtest to 96 signals in 6mo test window.

### B-CLOSED-004 — Silent-stop variant in deque saturation
**Fixed:** Commit `717d23f` (during S/R lab build)
**Impact:** New silent-stop pattern caught by Sprint 3 agent. `len(bars_5m)` saturates because `bars_5m` is `deque(maxlen=200)`. Cache went permanently stale → trades stopped at 2023-05-15. Fixed with `bars_5m[-1].end_time`. Validator updated.

### B-CLOSED-005 — pandas 3.0 datetime precision idiom (HIGH)
**Fixed:** Commit `c62b72d` (audit agent ad9edd2e...)
**Impact:** Found 1 HIGH bug (`tools/phoenix_sr_confluence_analyzer.py:169`) and 1 MEDIUM defensive fix (`tools/phoenix_tick_entry_quality.py:149`). 10 other files SAFE. **Production code (bots/, core/, bridge/, strategies/) was CLEAN — no live trade path affected.** Audit report: `docs/PANDAS_30_DATETIME_AUDIT.md`. 6 regression tests added (`tests/test_pandas_30_datetime_precision.py`).

### B-CLOSED-006 — test fixture pollutes production strategy_halts.json (MEDIUM)
**Discovered:** 2026-05-19 (overnight ship-completeness audit)
**Location:** `tests/test_halt_log_signature.py:20-29` (fixture `fresh_registry`)
**Root cause:** Fixture used `monkeypatch.setattr(srr_module, "_HALTS_FILE", ..., raising=False)` — the `_HALTS_FILE` attribute does NOT exist on `core.strategy_risk_registry` (real name: `STRATEGY_HALT_STATE_FILE`). The `raising=False` flag silently made the monkeypatch a no-op, so every halt the test created was written to the REAL `logs/strategy_halts.json` (a phantom `dupe_test` key persisted across test runs and made it into commits).
**Fix:** Use the correct attribute name + drop `raising=False` so an attribute typo fails loudly in the future. Pattern matches `tests/test_strategy_risk_registry.py::halt_file_tmp`.
**Lesson:** `monkeypatch.setattr(..., raising=False)` is dangerous for tests that depend on the patch having an effect — it silently passes when the target is wrong.

---

## How to add new items

When you find a bug or worry:
1. Pick the next free ID (B-NNN for bugs, W-NNN for watch items, I-NNN for info)
2. Use this template:
   ```
   ### B-NNN — One-line title (SEVERITY)
   **Discovered:** YYYY-MM-DD by [agent/spawn]
   **Location:** file:line or system area
   **Symptom:** what's happening
   **Action needed:** what to do
   **Status:** investigating / fixed in commit XXXXX / deferred to next sprint
   ```
3. Commit so the next agent sees it
