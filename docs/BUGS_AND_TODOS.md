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
