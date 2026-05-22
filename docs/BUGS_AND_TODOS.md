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

### B-030 — sim_bot ZERO_GATE override neutered every protective gate (CRITICAL)
**Discovered:** 2026-05-21 by operator pushback ("there's no way today's bias_momentum
losses are just noise — there's a bug").
**Symptom:** bias_momentum live sim WR was **8.3% (1W/11L, -$330) today** vs 5y
backtest WR 38.8% (+$308K, PF 1.45, 6/6 years positive).
**Root cause:** `bots/sim_bot.py:156` defined `SIM_STRATEGY_OVERRIDES` that, on every
strategy load, OVERWROTE production config with lab-harness values:
- bias_momentum: `min_tf_votes: 1` (vs prod 2), `min_confluence: 0.0` (vs regime
  5.0+), `min_momentum: 0` (vs regime 60), `max_ema_dist_ticks: 999`
- Same pattern on vwap_pullback, high_precision_only, ib_breakout, spring_setup
- Bot-wide `SIM_ZERO_GATE` set `risk_per_trade=$15`, `max_daily_loss=$10K`

**The 5y backtest validated PRODUCTION gates; sim_bot fired on ZERO_GATE — every
weak signal made it through.** Origin: commit `03687ef` (2026-04-21) — sim_bot was
born as a paper-only lab harness with "Strategies still fire across all regimes for
data collection". That design predates the Phase 13 ship plan.

**Fix:** Emptied both override dicts. sim_bot now uses production config straight
from `config/strategies.py`. Per-strategy account routing + StrategyRiskRegistry
caps still provide isolation. Added `tests/test_sim_bot_no_zero_gate.py` (3 tests)
to prevent regression.

**Expected impact:** sim_bot's WR should climb from today's 8.3% back to the
backtest's ~38-40% as the gates filter out weak signals.



### B-031 — msu_score (microstructure score) is ANTI-predictive — IC -0.152 (HIGH)
**Discovered:** 2026-05-22 by per-strategy confluence-voter agent a16cf0ef during
the 5y backtest of every voter on 6 RTH directional strategies.
**Symptom:** the `msu_score` field (computed in core/microstructure.py and surfaced
in market snapshots) has an Information Coefficient of **-0.152** across 12,000+
trades — i.e. HIGHER msu_score → LOWER win rate, statistically significant.
**Suspected root cause:** the score weights "absorption + delta divergence + spread
expansion" as bullish/bearish microstructure flags, but the live formula appears
to be inverted vs. what the voter research expects. Possibly an adverse-selection
trap (the score lights up at the worst entry moments — late chase / blow-off / fade
opportunities).
**Status:** ⚪ INFORMATIONAL — msu_score is **advisory-only** in the bot (no
strategy uses it as a hard gate), so no live $ damage. But if anyone ever wires
it into a gate they will systematically lose money. Audit the formula:
  1. Grep for `msu_score` callers and confirm none gate on it (only log/observe)
  2. Open core/microstructure.py — check sign of the absorption + delta-divergence terms
  3. Re-run the IC test after any formula change
**Priority:** MEDIUM (no live damage today, but a footgun for any future "add a
microstructure confluence" PR).



### B-032 — Volumetric snapshot recorder broken (PARTIAL FIX, HIGH)
**Discovered:** 2026-05-22 by live-data confluence agent attempting to read the
live volumetric capture to cross-check the 5y voter findings.
**Symptom:** `data/historical/volumetric/` contained exactly **1 file (648B) from
2026-05-18** — the recorder dropped every snapshot for 4 days straight.

**Root cause (Python side) — FIXED 2026-05-22 00:05 CT:**
The `PhoenixVolumetricRecorder` scheduled task was firing every 10 min with
`Execute = "python"` (no path). The Trading PC scheduled-task user has no
`python` in PATH → `LastTaskResult: 2147942402` (ERROR_FILE_NOT_FOUND) on
every fire since 2026-05-18.
Fix: `Set-ScheduledTask` updated `Execute` to the absolute interpreter path
`C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe`.
Manual trigger immediately wrote `2026-05-22.jsonl` (success). Next auto-fire
at every :03, :13, :23 ... etc.

**Root cause (NT8 side) — STILL OPEN:**
`data/volumetric_latest.json` itself is **stale since 2026-05-19 23:03** — the
TickStreamer.cs indicator stopped writing volumetric snapshots 3 days ago.
The recorder is now dedup-skipping the same stale ts on every fire.
**Operator action required:** reload the TickStreamer indicator on the MNQM6
chart in NinjaTrader (right-click → Indicators → Phoenix TickStreamer → Apply
again, or remove + re-add). The writer should resume on next bar close.
After that, every 10 min the recorder will append a new snapshot line to
`data/historical/volumetric/YYYY-MM-DD.jsonl`.

**Hardening (not yet done):** add a heartbeat alert that fires if no new
TickStreamer ts has been seen in 30 min during RTH. Currently the failure is
silent — no log message, no Telegram alert.
**Priority:** HIGH on the NT8-side fix (every day is permanent data-loss).



### B-033 — tf_1m + orb_direction in min_confluence is noise, not signal (LOW)
**Discovered:** 2026-05-22 by per-strategy confluence agent a16cf0ef.
**Symptom:** several strategies include `tf_1m` and `orb_direction` in their
min_confluence vote tally. Per-voter IC tests show both are essentially **random**:
  - tf_1m: IC -0.012 (noise; 1m bias flips every 2-3 bars in MNQ)
  - orb_direction: IC +0.018 (noise; the 15m OR break direction doesn't predict
    further continuation once the actual breakout has triggered the strategy)
**Action:** remove both voters from every strategy's confluence tally. Lowers
min_confluence threshold marginally but removes false-positive votes that mask
when the GOOD voters (tf_60m, tf_5m, es_correlation, vwap_relation) disagree.
**Strategies affected:** bias_momentum, vwap_pullback_v2, high_precision_only,
ib_breakout (any that compute votes = sum of TF biases).
**Priority:** LOW (no $ damage, just confluence-score noise).



### B-034 — high_precision_only needs tick_rate_60s ≥ 600 gate (MEDIUM)
**Discovered:** 2026-05-22 by per-strategy confluence agent a16cf0ef.
**Symptom:** high_precision_only fires in low-tick-rate periods (overnight,
lunch) where its microstructure pattern detection is unreliable. Voter research
shows WR lifts from 47% → 62% on the 35% of historical trades where
`tick_rate_60s ≥ 600` (rough proxy: active RTH with real flow).
**Action:** add a hard gate at the top of high_precision_only.evaluate():
```python
if (market.get("tick_rate_60s") or 0) < 600:
    return None
```
Plus the standard `require_tick_rate_gate` config flag for back-out.
**Priority:** MEDIUM (improves WR by 15pp on a strategy that's already in the
plan winners list).



### B-035 — spring_setup uses dom_imbalance but the live signal is INVERTED (MEDIUM)
**Discovered:** 2026-05-22 by per-strategy confluence agent a16cf0ef when
cross-checking 5y findings against the 2-month live capture (where it exists).
**Symptom:** spring_setup's dom_imbalance "votes" appear to use the wrong sign —
when the live DOM shows heavy bid stacking (which the strategy reads as bullish
support for a LONG spring), the trade actually performs WORSE. Suspect cause:
DOM aggregation reads ask-side liquidity for the "bullish" calc and bid-side
for "bearish" (inverted from intent).
**Action:** trace `dom_imbalance` from feeder → strategy. Confirm sign with 2-week
live A/B (gate ON vs OFF). If the bug is confirmed, either flip the sign or rip
out the voter entirely.
**Priority:** MEDIUM (spring_setup is a plan winner; getting this right adds
~$3-5K/yr per the 5y bias estimate).



### B-036 — ib_breakout + es_nq_confluence small-n; defer hard gates (DEFERRED)
**Discovered:** 2026-05-22 by per-strategy confluence agent a16cf0ef.
**Symptom:** voter research wanted to recommend gates on ib_breakout and
es_nq_confluence, but both strategies have **<100 trades / 5y** in the
backtest. Wilson 95% CI on any per-voter delta is too wide to make a
statistically defensible call.
**Decision:** intentionally **DEFER** any new gates on these two until they
accumulate ≥100 trades. Track via `tools/validation_tracker.py` weekly.
**Priority:** ⚪ INFORMATIONAL — capture the deferral so a future "add gates
everywhere" PR doesn't re-do this work and ship a hot opinion on n=43.



### B-037 — opening_session.open_drive Bug B2 fix needs cross-verification (LOW)
**Discovered:** 2026-05-22 while applying tf60m+ES gate to .open_drive.
**Symptom:** the Bug B2 fix (target = entry ± 2R instead of pivot_pp) is in
place and looks correct, but no end-to-end live trade has hit T1 yet to confirm
the new target math under live commissions/slippage. Want a paper sample of at
least 5 LONG + 5 SHORT live signals before declaring B2 fully closed.
**Action:** none required — the new tf60m+ES gate may slow down signal volume,
so widen the watch period from 1 week → 4 weeks.
**Priority:** ⚪ INFORMATIONAL.



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

### B-029 — opening_session.open_drive Phase 13 override is DEAD CODE (HIGH)
**Status:** FIXED in Finding-2-fix of pt2 audit (after 5y backtest agent).
**Discovered:** 2026-05-20 5y backtest verification agent (aaef89ad).
**Symptom:** `PHASE_13_EXIT_ASSIGNMENTS["opening_session.open_drive"] =
("fixed_rr", {"rr": 3.0})` never matched at runtime. Signal emits
`signal.strategy="opening_session"` (parent), with sub identifier in
`signal.metadata["sub_strategy"]`. The override dispatcher did lookup
on `signal.strategy` alone — never appended `.{sub_strategy}`.
**Empirical evidence:** All 267 open_drive trades in the 5y
`opening_session_sub_breakdown.csv` shipped at RR=2.0 (strategy's
internal default `target_distance = 2.0 * one_r` post-B2 fix), NOT
the plan §1.2's specified 3R.
**Fix:** Added `_resolve_key()` helper in `_apply_phase13_overrides`
and `sub_strategy` param to `recompute_phase13_target()`. Now looks
up dotted form first (e.g. `opening_session.open_drive`), falls back
to bare strategy name. Applied to all 4 call sites:
- `_apply_phase13_overrides` step 1 (PHASE_13_ORDER_TYPES)
- `_apply_phase13_overrides` step 2 (PHASE_13_EXIT_ASSIGNMENTS)
- `recompute_phase13_target` deferred path (market-price + LIMIT-anchored)
- Per-bar enforcement loop (uses Position.sub_strategy field)
3 new regression tests in `test_phase13_overrides.py`.

### W-008 — bias_momentum 5y baseline stale by ~$130K after F-012 (DOCUMENTATION)
**Discovered:** 2026-05-20 5y backtest agent (aaef89ad).
**Status:** WATCH — not a bug, but plan baseline needs updating.
**Symptom:** PHOENIX_BEST_PLAN.md §1.1 shows bias_momentum at
$178,379 / 5y / PF 1.33 / 13,790 trades. Fresh 5y rerun today
returns $308,381 / PF 1.45 / 36,559 trades (+73% improvement).
**Root cause:** F-012 (in commit 0708a07 today) restored
`skip_on_stop_clamp=True` on bias_momentum. With matched-by-design
`stop_fallback_mode="confirmation"` (Phase 7 CODE PATCH 3), this
swaps clamped 200t ATR stops for structurally-anchored ~8-40t
confirmation stops. Tighter, more-honest stops → higher turnover +
better win-rate-per-trade economics.
**Action needed:** Update PHOENIX_BEST_PLAN.md §1.1 bias_momentum
row with new baseline + cite F-012 as the cause. Verify same hasn't
happened to other strategies whose stop configs changed today.

### I-005 — Backtest engine is provably deterministic
**Documented:** 2026-05-20 5y backtest agent ran 3x identical runs.
**Evidence:** 1y window 6 strategies × 3 runs = identical to the
dollar; 5y bias_momentum × 2 runs = $308,381 both times. The earlier
`UnicodeEncodeError` in `print_summary()` was a cp1252 console issue
(∞ char) fixed in commit 0708a07.
**Implication:** Test-fixture leakage is the only known source of
backtest non-determinism. With B-CLOSED-006 + B-007 (test coverage)
shipped, we have high confidence in run-to-run reproducibility.





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

### F-001 — Compounding tier_3000 sizing (CRITICAL — HIGHEST $$ UPSIDE)
**Discovered:** Initial plan §I.4 (5y projection $1.5K → $1.09M+ compounding curve)
**Shipped:** 2026-05-20 (this commit)
**Location:** `core/tier_sizer.py` (new), `bots/base_bot.py` dispatcher,
`config/settings.py` (SIZING_MODE + STARTING_EQUITY), `data/equity_state.json` (created on first run when SIZING_MODE="tier_3000")
**What changed:**
- New `core/tier_sizer.py` with full Plan §I.4 / §5.4 policy: 1 contract per
  $3K equity, MAX 30 contracts, per-strategy multipliers from
  STRATEGY_SIZE_MULT (bias_momentum 1.5×, vwap_band_* 0.5×, etc.), 85%-of-ATH
  scale-down (-1 tier), 4% daily circuit breaker (HALT new entries), 3-loss
  halving (next-trade size /= 2, floor 1). LOUD logging at every decision
  per I-002.
- `bots/base_bot.py` dispatcher: when `SIZING_MODE="tier_3000"`, route
  through tier_sizer (the daily breaker returns 0 → entry skipped). When
  `"flat_1"` (DEFAULT), legacy PositionScaler path is preserved.
- `_on_trade_closed` feeds tier_sizer.record_trade_close() so equity ATH
  and consec-losses stay in sync — quiescent for flat_1 operators.
- 38 new tests in `tests/test_tier_sizer.py` covering tier math, ATH
  invariants, DD scale-down, circuit breaker, halving, persistence,
  session-roll, dispatcher contract.
**Default-OFF:** `SIZING_MODE="flat_1"` ships as the default. Operator
opts in to `"tier_3000"` per docs/OPERATOR_BRIEF_PT2.md F-001 activation
section. **Backward compat verified:** no behavior change while default.
**Pending operator action:** flip SIZING_MODE + initialize equity_state
once Phase A (30 trading days at flat_1) completes per Plan §5.3.

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
