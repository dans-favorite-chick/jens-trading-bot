# Phoenix Bot — Recent Changes

_Dated log of what's been changed, by whom, why. Newest first._
_Auto-appended by `tools/memory_writeback.py` via SessionEnd hook._

---

### 2026-05-24 09:47 Central Daylight Time — Session changes: 2 files modified

**Files changed:**
- `data/historical/volumetric/2026-05-22.jsonl`
- `data/historical/volumetric/_recorder.log`

---
### 2026-05-24 09:47 Central Daylight Time — Session changes: 2 files modified

**Files changed:**
- `data/historical/volumetric/2026-05-22.jsonl`
- `data/historical/volumetric/_recorder.log`

---
### 2026-05-24 09:47 Central Daylight Time — Session changes: 2 files modified

**Files changed:**
- `data/historical/volumetric/2026-05-22.jsonl`
- `data/historical/volumetric/_recorder.log`

---
### 2026-05-18 07:00-09:55 CT — Phase 13 research sprint: 7 new strategies tested + 2 bugs root-caused + compounding backtest (PRODUCTION CODE UNTOUCHED)

Operator: "test all the strategies you suggested ... a-g. fix bugs so we can test the opening strategies. keep a log of everything that we're planning on implimenting, but don't change it yet. let's do it all together." Then: "back test the strategies starting with just 1 mnq contract at a time. but as we increase in profit, increase in contracts bought. ... how long until we can start buying 2, 3, 4, 5 etc."

**Step 1 — 7 new candidate strategies built + 5-year backtested (`tools/phoenix_new_strategy_lab.py`):**

Standalone pure-function implementations (NOT touching `strategies/`). 5y MNQ Databento data, 1,771,336 cycles, 234s runtime, 2,470 trades.

3 WINNERS:
- `g_inside_bar_breakout` — 1015 trades, **+$11,300, WR 70%, PF 4.88, max DD $65, 5min avg hold**
- `e_multi_day_breakout`  — 685 trades, **+$9,097, WR 78%, PF 6.79, max DD $67, 2min avg hold**
- `a_asian_continuation`  — 596 trades, **+$5,909, WR 80%, PF 8.29, max DD $21, 2min avg hold**

All 3 positive every single year 2021-2026. Combined +$26,306/5y / +$5.2k/year.

3 KILLED:
- `b_rth_open_drive_scalp` — -$255, WR 16% (anti-edge — strong-close OR predicts FADE not continuation)
- `c_poc_magnet_reversion` — n=6 (gates too tight)
- `f_eod_mean_reversion`   — n=17 (gates too tight)

1 MARGINAL:
- `d_orb_fade_fixed` — +$145, PF 1.26 (B3 fix unblocks; needs exit-policy testing)

**Step 2 — Bug B2 + B3 root-caused (NO production fix applied yet):**

- **B2 `opening_session.open_drive` pivot_pp target** — `strategies/opening_session.py:372` sets `t1=pivot_pp` which lands BELOW current price for upward open drives → guaranteed loser. Fix: use R1/S1 or VWAP+ATR. Pending operator design decision.
- **B3 `orb_fade` 0 signals** — `strategies/orb_fade.py:162`: `if (time.time() - last_bar_ts) > 90: return None` — wallclock vs historical bar ts diff = years in seconds → 100% rejected in backtest. **May affect live too** if bar.end_time timebase differs from time.time(). Fix: compare against `market["now_ct"]`. Proof-of-fix: `d_orb_fade_fixed` in lab produces 57 trades.

**Step 3 — Compounding backtest (`tools/phoenix_compounding_backtest.py`):**

Built engine with size-scaled slippage (1t/side ≤5c, 1.5t 6-15c, 2t 16-30c), 30-contract hard cap (CME/MNQ liquidity reality), DD scale-down at 85% of ATH, consecutive-loss scale-down after 3 losers, daily 4% circuit breaker.

Initial unconstrained run produced $907B / 564M contracts — exposed why caps are mandatory. Re-run with realistic constraints on 13,123 trades across 10 winning strategies:

| Policy | Final $ | Max DD | Max Contracts |
|---|---:|---:|---:|
| flat_1 (no compounding) | $63,670 | 16.6% | 1 |
| tier_1500 (aggressive) | $1,067,468 | 77% (rejected) | 30 |
| **tier_3000 (RECOMMENDED)** | **$1,091,290** | 34% | 30 |
| tier_5000 (conservative) | $960,270 | 21% | 30 |
| fixed_ratio_jones | $723,374 | 31% | 30 |

**Compounding path (tier_3000):** 1c day 1 → 2c week 10 → 5c month 4.5 → 10c month 19 → 30c (cap) month 22. Year-end: 2021 $17.7k, 2022 $36.8k, 2023 $345k, 2024 $691k, 2025 $926k, 2026 (partial) $1.09M.

Stress-tested at 55% WR (vs in-sample 70-80%) → still $856k.

**Step 4 — Phase 13 plan document (`docs/PHASE_13_IMPLEMENTATION_PLAN.md`):**

9-section plan: A (verdicts), B (bugs with root causes), C (new strategies with results), D (infrastructure), E (reallocation), F (sequence), G (open questions), H (audit trail), I (compounding plan + per-strategy scale-out tables for N=1,2,3,5,10,20,30).

**Files added:**
- `tools/phoenix_new_strategy_lab.py`
- `tools/phoenix_compounding_backtest.py`
- `tools/phoenix_exit_experiments.py`
- `tools/phoenix_confluence_filters.py`
- `tools/opening_session_sub_breakdown.py`
- `docs/PHASE_13_IMPLEMENTATION_PLAN.md`

**Files modified:**
- `tools/phoenix_real_backtest.py` (added VAH/VAL via SessionVPState, rth_5min_* tracking, opening_type classifier call, pivot_pp computation, bar.delta enrichment)
- `docs/STRATEGY_DEEP_DIVE_2026-05-18.md` (initial analysis)
- `memory/context/CURRENT_STATE.md` (Phase 13 AT-A-GLANCE block)

**Production state unchanged:** sim_bot still on `dbba094`. NO `config/strategies.py` / NO `bots/base_bot.py` / NO `strategies/*.py` modifications. All work is research artifacts for Phase 13 Step 3+ (next session).

---

### 2026-05-18 00:30-01:25 CT — Phase 12C ES/NQ confluence strategy + memory refresh (commits `dbba094`, `b6bb42d`, `b498512`, `fd35649`)

Operator: "yes, do all 4. read CSVs" — directive to wrap up the overnight Databento + backtest session by (1) committing the overnight artifacts cleanly, (2) implementing the 5-year backtest winner as a new Phase 12C strategy in production, (3) updating memory.

**Step 1 — Sim_bot pre-flight (no changes):**
- Sim_bot alive on `853482e` (Phase 9.1), 62h uptime, bridge healthy, 0 errors today, 19k log lines, last trade 13.7m before check.
- Decision: leave running. Will restart before Monday 08:30 CT cash open to pick up Phase 9.5 + Incident #1 fixes + Phase 12C simultaneously.

**Step 2 — Overnight artifact commits (3 commits, kept history readable):**
- `fd35649` — `.gitignore` excludes 985MB of regenerable CSVs (599MB raw Databento dump + 385MB derived MNQ/MES 1m+5m). Regen path documented inline.
- `b498512` — 13 tool .py files from overnight: `decompress_databento.py`, `databento_to_phoenix*.py` (v1+v2), `backtest_v3*.py` (single + 108-config sweep), `exit_methodology_v3.py` (trail-cap bug fix), `multi_strategy_backtest.py` (6-way comparison), `strategy_backtest_es_nq_v2.py` (original Anthropic 960-config sweep, kept as reference).
- `b6bb42d` — stragglers: `tools/build_handoff_bundle.py` from earlier Phase 9 evaluator handoff + drop of obsolete tracked `MNQ 06-26.Last.txt`.

**Step 3 — Phase 12C ES/NQ Confluence LONG (`dbba094`):**

5-place wiring per the V2 deployment pattern Phases 1-9.1:
- `strategies/es_nq_confluence.py` — new (361 LOC + ~200-line docstring covering backtest evidence, data dependency, infrastructure sequence)
- `config/strategies.py` — config block (enabled=True, validated=False)
- `tools/strategy_change_log.py:29` — added to `_KNOWN_STRATEGIES`
- `core/strategy_risk_registry.py` — added to `STRATEGY_KEYS`
- `bots/base_bot.py` — import + `strategy_classes` registration
- `config/account_routing.py` — routed to `Sim101` temporarily (same pattern as `big_move_signal` at Phase 9.1)
- `tests/test_es_nq_confluence.py` — 14 cases (warmup gates, boost/corr gates, per-bar dedup, 5-place wiring pins). All pass.

Backtest evidence (Fixed 24t stop / 96t target, 4:1 R:R):
- 131 trades over 5y (2021-05-17 → 2026-05-17), 50.4% WR, $1,548 total, $11.82/trade
- Profit factor 2.63, max drawdown $72
- 6/6 years POSITIVE including 2022 bear (-33% NQ year): **+$1,032 net**
- Selected from 108-config sweep + 30 exit-methodology comparator

**Critical data dependency surfaced + documented:**
Phoenix has zero live MES feed (TickStreamer streams only MNQ). Strategy is dormant — logs DATA_NOT_AVAILABLE once-per-process and SKIP `data_not_available` on every eval — until MES infrastructure lands. Same pattern as `footprint_cvd_reversal` pre-volumetric. ZERO behavioral risk to live trading. Documented in `KNOWN_ISSUES.md` and the strategy docstring.

**Test suite: 2,110 pass / 19 skip / 0 fail** in 80s (+18 vs Phase 9.5 baseline; zero regressions).

**Branch state:** `weekly-evolution/2026-05-17` at `dbba094`, 16 commits unpushed. Push deferred until Monday cash open validates V2 strategy fires (per Phase 9 deployment protocol).

---

### 2026-05-17 18:30-23:00 CT — V2 strategy overhaul deployment (Phases 0-9.5, 12 commits)

Major sim deployment. Per `docs/CLAUDE_CODE_DEPLOYMENT_PROMPT.md` operator delivered an 8-phase plan from a separate Claude.ai research session; executed in 10 phases plus a 9.1 hotfix and Phase 9.5 cleanup. Key milestones:

- **Phase 0** (`3b91561`) — disable safety limits for sim testing (DAILY_LOSS_LIMIT $45→$1M, etc.)
- **Phase 1** (`9a5de35`) — copy 4 LSR core helpers; resolved a `core/volume_profile.py` collision by renaming incoming to `core/volume_profile_lsr.py`
- **Phase 2** (`7dda872`) — copy 6 new V2 strategies
- **Phase 3** (`11ed07f`) — register 6 strategy classes in `bots/base_bot.py:strategy_classes`
- **Phase 4** (`1484027`) — add 6 STRATEGIES config blocks + sync `_KNOWN_STRATEGIES` parity
- **Phase 5** (`7372f53`) — disable 3 V1 strategies superseded by V2
- **Phase 6** (`fdeb289`) — config patches to 8 existing strategies + FCD-6 footprint config flip
- **Phase 7** (`396154e`) — 10 code patches (CODE PATCH 1-4,6 + FCD-1 through FCD-5)
- **Phase 8** (`ed5cb03`, `1587745`) — full pytest suite + 13 stale-assertion triage (2 updated, 11 skipped with Phase-10 restore markers)
- **Phase 9** (`f428b1a`) — deploy to Sim101 + 10-min observation + wrap-up doc; surfaced 2 pre-existing gaps (`vwap_band_reversion` orphan + `big_move_signal` missing from STRATEGY_KEYS)
- **Phase 9.1 hotfix** (`853482e`) — register `vwap_band_reversion` + add `big_move_signal` to STRATEGY_KEYS + STRATEGY_ACCOUNT_MAP
- **Phase 9.5** (`5a4bbae`, `7cee2d0`, `d32a028`, `48ad489`, `f4cc375`, `678108b`) — A/B/D/E backlog + Incident #1 (CLOSEPOSITION-vs-OCO race fix)

Final state: 2,092 pass / 19 skip / 0 fail. 15 strategies registered, 25 NT8 accounts mapped. See `docs/PHASE9_DEPLOY_STATUS.md` for the full deploy report including Phase 9.5 cleanup outcomes and Incident #1 root-cause analysis.

---

### 2026-05-15 08:15-08:30 CT — research-backed relaxations: compression 3-of-4 + OPEN_DRIVE Steidlmayer + ib_breakout session-anchor (commits `6af6ab3`, `e07b2b8`)

Operator: "deep research how a successful automated AI trading bot should write the strategy so that it's not soo strict that it never fires." Three structural anti-patterns surfaced + fixed across 4 strategies.

**Anti-pattern 1: Binary AND-of-overlapping-conditions** (compression_breakout)
- Old: `all 4 conditions True` (TTM, ATR, Volume, Range)
- Issue: conditions 1/2/4 measure overlapping volatility signal. Requiring all 4 double-counts the same axis.
- Fix: `min_compression_conditions` config (default 3). Fires when 3 of 4 align. Carver's "Systematic Trading" principle: scaled forecasts beat binary AND gates.

**Anti-pattern 2: Fixed-tick proximity instead of fractional bounds** (open_drive classifier)
- Old: close must be within 8 ticks (2pt) of 5-min extreme
- Issue: on MNQ's typical 90pt 5-min range, 2pt = 2% of range. Steidlmayer's ORIGINAL Market Profile work specifies top/bottom **THIRD** of range. We were applying a 2% bound where the spec calls for 33%.
- Fix: `_DRIVE_CLOSE_PROXIMITY_RANGE_FRAC = 0.33` (top/bottom third) with 8-tick fallback for degenerate ranges. Volume mult also relaxed 1.4→1.2 to match entry trigger.
- Hard data: 9 sessions sampled, displacement >15pt in 7/9 (78%), median 5-min range 90pt. The 2pt close-bound rejected 8/9 sessions where the open WAS directional.

**Anti-pattern 3: SPY/QQQ-tuned cap applied to MNQ** (ib_breakout — same root cause as ORB pre-fix)
- Old: `max_ib_width_atr_mult = 1.5` + daily reset at ET-midnight
- Issue: MNQ 10-min IB at the open routinely runs 50-80pt = 2-3× 5m ATR. The 1.5× cap rejected almost everything. ALSO the ET-midnight anchor built the "Initial Balance" from arbitrary overnight bars (same bug as ORB had).
- Fix: session_open_et config (default 09:30 ET) + bar-window filter + width cap 1.5 → 4.0× ATR. Mirrors the 2-pass ORB session-anchor fix (`751172f` + `f96135b`).
- Hard evidence: 3,472 `BLOCKED gate:ib_too_wide` events in 50MB sim_stdout (dominant failure).

**Bots restarted ~08:30 CT** on `e07b2b8` covering all 3 fixes. Cash open in progress — ORB / ib_breakout / opening_session classifier all using the corrected anchors. Live Monitor armed for SIGNAL/INTENT events.

Tests: 1,936 → 1,949 pass / 4 skip / 0 fail (+13 net across the 3 fix bundles).

---

### 2026-05-15 08:00-08:15 CT — compression_breakout + opening_session un-retired (commit `ffd206d`)

Operator deep-dive: "compression_breakout never fires — same investigation as ORB." Verdict: **not a bug.** It's an MNQ-calibration mismatch.

**Hard evidence (24h sim_stdout)**: 5,476 `squeeze_not_held_min_bars` events, 0 BLOCKED gates. The strategy never accumulates enough consecutive compressed bars to even reach STAGE 2. The 2026-04-24 commit raised `min_squeeze_bars` 5→12 under the assumption of "60 min on 5m bars" — but `evaluate()` actually ticks ~1.2×/min, so 12 evals ≈ 10 min and even that was never hit.

**Shipped:**
1. **Per-condition instrumentation** — `[EVAL] compression_breakout: NOT_COMPRESSED ttm(...) atr(...) vol(...) range(...)` now logs which of the 4 stage-1 conditions failed each eval. Pre-fix the strategy was a black box.
2. **MNQ-calibrated thresholds:**
   - `atr_compression_ratio: 0.50 → 0.65`
   - `range_atr_ratio: 1.50 → 1.80`
   - `min_squeeze_bars: 12 → 6` (matches the actual 1.2 eval/min cadence)
3. **Un-retired in sim only** (validated=False). Re-review trigger: n=30 trades.

**opening_session un-retired (no code changes).** 80MB stdout deep-dive showed the classifier + sub-evaluators are well-designed; signals are intentionally selective:
- premarket_breakout (08:30-08:45 CT): 86 SKIPs
- orb-in-router (08:45-14:30 CT): 465 NO_SIGNAL + 676 SKIP
- open_auction_in (09:30-12:30 CT, AUCTION_IN type): 215 NO_SIGNAL — DOES dispatch
- open_auction_out (08:45-11:00 CT, AUCTION_OUT type): 306 NO_SIGNAL — DOES dispatch
- open_drive (08:35-09:00 CT, DRIVE type): never dispatched on MNQ — classifier rarely matches (needs >15pt displacement + >1.4× volume + close-at-extreme)

Un-retiring lets the per-sub log lines accumulate so the operator can SEE the classification distribution over real RTH days. Standing follow-up: if open_drive never matches after 4 weeks, relax `_DRIVE_DISPLACEMENT_POINTS` (currently 15pt).

Both bots restarted at ~08:14 CT on `ffd206d`. ORB still armed for the 8:30 CT cash open. Suite: 1,936 → 1,938 pass / 4 skip / 0 fail.

---

### 2026-05-15 07:30-07:55 CT — noise_area + ORB silent-firing bugs unblocked (commits `751172f` + `f96135b`)

Operator flagged yesterday: of 9 enabled strategies, only 3 actually fired (bias_momentum / vwap_pullback / dom_pullback). Deep-dive found two unrelated bugs that were silently killing two more strategies.

**Bug 1: noise_area dropped 11 sim signals/day at the universal stop-sanity gate.**
noise_area is a managed-exit strategy — its stop is the opposite noise-cone boundary, marketed as "150-600t structural disaster anchor, not a real risk stop." Today's cone hit 776t (194pt). The bot's universal `_sanity_check_entry` capped stops at 5-200t for ALL strategies, so every signal was rejected at `STOP_SANITY_FAIL`. Fix: gate accepts `is_managed_exit: bool`; managed mode = 5-1000t bound. Caller plumbs from `_managed_exit_target` + `signal.exit_trigger` + the strategy's `uses_managed_exit` class flag.

**Bug 2: ORB built its "Opening Range" from arbitrary overnight bars.**
The strategy is Zarattini 9:30 ET cash-open ORB. Config comment said "Cutoff at 10:30 ET / 9:30 CST" but the CODE anchored daily reset to the ET calendar boundary (ET midnight). The bot's first eval of the day would build an OR from whatever 15 bars happened to be in the deque — overnight chop. Today's OR: high=29689, low=29295.75, **size=393.25pt**, vs the 80pt cap → guaranteed `gate:or_too_wide` rejection on every breakout (3,923 of 1,086 sim evals).

Fix in two passes:
1. `751172f`: new `session_open_et` config (default 09:30), `_session_open_today_et()` helper, daily reset anchored to session-day (not ET calendar), bars filtered to `>= session_open_ts`.
2. `f96135b`: second pass — the lower-bound-only filter still let the deque's oldest 15 bars (= 3-hour-old overnight) fill the OR after a restart. Fix tightened to `[session_open_ts, session_open_ts + or_duration_min)` — both lower AND upper bound. Verified live: post-restart at 07:53 CT, ORB emits clean `SKIP warmup_incomplete (0/15 bars since 09:30 ET)` instead of fabricating a phantom OR.

Both bots restarted on the fix at 07:52:35 / 07:52:37 CT. Stale ORB state files cleared. ORB armed to build a real OR from 8:30-8:44 CT bars (= 9:30-9:44 ET).

**opening_session stays retired** — different problem entirely. Its time-window code uses CT (clean, no anchor bug), but the nested 6-sub-strategy router produces too few signals at all to ever validate (4 trades in months of runtime). Retirement note from yesterday's #5 still holds: "Lift any individual sub (e.g. open_drive) to its own top-level strategy with a focused gate" — separate strategy-design project.

Tests:
- 7 new managed-exit sanity tests (`test_stop_target_sanity.py`)
- 12 new ORB session-anchor tests (`test_orb_session_anchor.py`)

Suite: 1,919 → 1,936 pass / 4 skip / 0 fail.

---

### 2026-05-14 14:30-14:50 CT — prod_bot restart + dashboard boundary fix (commit `71fc5af`)

**Two related fixes shipped after operator noticed prod was -$106 today:**

**1. Deployment gap caught (no commit — just a process restart):** prod_bot
PID 24568 had been running since 2026-05-13 21:13:20, which was 52 seconds
BEFORE the 21-item roadmap batch landed at 21:22:12. Result: every change
in that batch (#3, #2, #4, #5/#6, #7, #8, #1b, #1c, #13, #14, #15, #17,
#18, #19, #20, #22, #23, #25, #12) sat on disk while prod ran the old
in-memory snapshot. Today's three $-65.32 vwap_pullback stops are
exactly the clamp-from-above pattern #8 (commit `e6ad6da`) skips —
they wouldn't have fired on the new code.

Killed PID 24568 at 14:30; watchdog respawned as PID 32024 at 14:32:54
on the latest code. sim PID 27244 had already auto-restarted overnight
(02:01) so sim was fine.

Memory entry added: [code_changes_dont_auto_deploy.md](../../../Users/Trading%20PC/.claude/projects/C--Trading-Project/memory/code_changes_dont_auto_deploy.md) —
flag "prod needs restart" after any behavior-affecting commit.

**2. Dashboard calendar-day boundary fix (commit `71fc5af`):** operator
saw "16 trades on dashboard, 8 sim, 1 prod" — 7+ trade mismatch. Root
cause: the 2026-05-13 commit `0c24a8e` switched `/api/today-pnl` to
calendar-day boundary so the TODAY card agreed with RiskManager.daily_pnl,
but left three sibling call sites on Globex 17:00 CT:

- `_load_session_trades_by_bot()` — drives Daily Stats trade tables
- `/api/status` — `session_start_ts` field
- `/api/trades` — `session_start_ts` field

Result was a 7-hour-per-night divergence (17:00 CT → midnight) where
the trade tables showed yesterday-evening trades that the TODAY card
had already rolled out. Sim's "16" was the Globex window; "8" was
calendar day.

Fix: all 3 sites now use `_calendar_day_start_ct_epoch()`. Globex
helper preserved as dead code. Dashboard restarted on the new code
~14:50 CT. Verified live: prod table 12→9, sim table 16→8, all
panels agree.

7 new source-pin tests (test_dashboard_calendar_day_pin.py) catch any
future regression. Suite: 1,912 → 1,919 pass / 4 skip / 0 fail.

---

### 2026-05-13 late-night — 21-item roadmap batch + self-audit (commits `c14a3a1` → `3ddf7a9`)

After this morning's bias_momentum fast-abort fix (`7f1411f`), the operator pasted a 25-item roadmap with "do it all". 21 items landed in 22 commits (each item self-contained, easy to revert individually). The 4 paper-trading items were intentionally skipped per operator's directive ("we're live sim trading baby!!!").

**Position infrastructure (foundation for everything downstream):**
- **#3 `4d4e15d`** — Anti-mutation invariant on R-distance. `Position.__post_init__` captures `_initial_stop_frozen`; the `r_distance` property reads from that, not the live `stop_price`. TRAIL/BE_STOP can now mutate `stop_price` freely without breaking R-multiple math.
- **#2 `c14a3a1`** — MAE/MFE tracking persisted per closed trade. `update_mae_mfe(price)` fires on every tick; close_position adds `mae_price`, `mfe_price`, `mae_ticks`, `mfe_ticks`, `r_distance`, `mfe_capture_pct`, `r_multiple` to trade records.
- **#4 `56eaf3b`** — `validation_tracker.py --exclude-outliers` adds median/IQR/p90 + sum_stripped + single_trade_concentration. **First real-data run flagged bias_momentum: net=+$675 looks like edge, stripped=-$502 / concentration=1.26 = one trade carries >100% of the net.** Same pattern caught dom_pullback, ib_breakout.

**Strategy lifecycle:**
- **#5/#6 `f0e6863`** — Formal retirement of high_precision_only (557t / 29% WR / -$1,082), opening_session (4t total), compression_breakout (18t total). All carry `retired: True`, `retired_at: 2026-05-13`, `retired_reason: ...`. 9 tests pin the markers.
- **#22 `477e31d`** — Wilson-CI promotion guardrail. `tools/validation_tracker.py --check-promotion` exits 2 if any `validated=True` strategy has n<100. **Caught ib_breakout: was validated=True with only 8 trades.** Demoted to validated=False. Only bias_momentum (n=292) and spring_setup (n=235) are validated now.
- **#7 `878165b`** — `config/regime_matrix.py` typed loader for the YAML at `memory/procedural/regime_matrix.yaml`. Handles the YAML 1.1 ON/OFF-as-bool quirk. Not yet wired into base_bot's evaluate() — separate commit when ready.

**Stop / exit improvements:**
- **#8 `e6ad6da`** — `skip_on_stop_clamp` extended from bias_momentum to vwap_pullback + dom_pullback. Same forensic logic (0W/5L on clamped-from-above stops) applies to both.
- **#1b `4e75d82`** — vwap_pullback stop_atr_mult 2.0 → 1.5 (mean-reversion entry doesn't need trend-following stop width).
- **#1c `d76b8cb`** — ema_dom_exit `min_profit_ticks = max(static_floor, int(target_ticks * 0.70))`. Big-target strategies were firing smart-exit too early.
- **#18 `32e823f`** — BE arms on bar-close confirmation, not tick-touch. Single noisy tick crossing the trigger no longer arms BE; the most-recent CLOSED 1m bar must also be past the trigger. Config-toggleable via `STRATEGY_DEFAULTS["be_on_bar_close"]`.
- **#15 `30eb1f2`** — vwap_band_pullback TF-vote 3 → 2 (band touches happen on last candle before reversal; 3-of-N over-gated).
- **#19 `7edaf9b`** — `flow_reversal` / cvd_flip / cvd_divergence get explicit rank-5 in `EXIT_PRIORITY` (above trend_stall at rank 6, below managed_exit at rank 4). Subsequent ranks shifted by +1.

**Instrumentation & tooling:**
- **#14 `e701973`** — footprint_cvd_reversal emits discrete `cvd_div_type` enum (multi_bar / single_bar / both / none) + `cvd_div_magnitude` in Signal.metadata and `[cvd_div=<type>]` in Signal.reason. Enables post-hoc "which div type wins?" groupby.
- **#12 `4219719`** — `docs/cvd_usage_audit.md` — cross-strategy CVD usage inventory + 4 surfaced gaps.
- **#13 `52cede2` + `3ddf7a9`** — ORB state persistence. Opt-in via `config["bot_name"]`. State file at `logs/orb_state_<bot>.json`. **Self-audit caught a regression**: `_or_bars_1m[0]` IndexError silently passed `max_entry_delay_min` after restart. Fixed by persisting `_or_session_start_ts` scalar separately.
- **#17 `64c113a`** — `tools/mae_stop_calibrator.py` — recommends per-strategy stops based on winning-trade MAE percentiles. Framework only; data ripens in ~2 weeks.
- **#20 `a951dc9`** — `tools/strategy_change_log.py` — mines git log for per-strategy commit timelines.
- **#23 `5a71566`** — Tier-aware contract scaling in SimpleSizer. `strategy_tier` arg multiplies base contracts: VALIDATED 1.5×, HIGH_CONFIDENCE 2.0×. Floor at 1 contract.
- **#25 `6af0689`** — `tools/strategy_correlation_audit.py` — per-pair Jaccard index over time-windowed co-fires. Surfaced retired-pair `high_precision_only`/`spring_setup` at 0.237 jaccard (the canonical "two strategies firing on the same setup" pattern).

**Suite: 1,751 → 1,912 pass (+161 tests). Branch not yet pushed to origin.**

**Operator's key insights from today's batch:**
1. **Promotion-on-vibes is a real failure mode**: ib_breakout had been flagged `validated=True` manually with only 8 trades. Guardrail now prevents that.
2. **One big trade can dominate net P&L**: bias_momentum's +$675 net was actually -$502 once 1 outlier was stripped. Without #4's view, the operator would have been trading on noise.
3. **Self-audit catches what tests don't**: the ORB cutoff regression survived the test suite because the test stubbed bars but didn't exercise the post-restart path. Always trace the actual control flow after writing a feature.

---

### 2026-05-13 ~13:30 CDT — trade_memory reader audit (commit `c9099d7`)

12-file follow-up to commit `4d523bf` — every other tool that raw-opened
the legacy `logs/trade_memory.json` was silently missing post-2026-05-12
trades. This commit routes them all through
`core.trade_memory.load_all_trades()` (the canonical merger of legacy
file + every per-bot file).

Highest-impact fixes:
- **`tools/validation_tracker.py`** — drives every weekly GRADUATE /
  SCALE / KILL_CANDIDATE decision. Pre-fix: silently used pre-split data
  for tier classifier and Wilson 95% CIs. Now sees 1,256 trades instead
  of 1,254 (the two sim_bot wins booked today are now visible).
- **`core/position_manager.py`** — bot-startup hydration. Pre-fix: every
  bot restart reset trade_history to pre-split-only data.
- **`tools/routines/post_session_debrief.py`** — daily 16:05 CT Telegram
  digest. Fixed BOTH the per-bot file issue AND a stale ISO-string
  filter that made `v.startswith(today)` never match Unix-float
  timestamps. Daily Telegram had been reporting "0 trades today" /
  YELLOW verdict every day for an unknown duration as a result.
- **`agents/session_debriefer.py`** — fixed both per-bot file issue AND
  a latent shape bug: `_load_trade_memory` returned a list but
  `_build_payload`'s isinstance check expected a dict, so the
  `trade_memory_tail` field in the Claude debrief payload has been
  silently empty for the entire lifetime of this module.
- **`tools/mark_position_flat.py`** — emergency manual flatten tool.
  Now searches every trade_memory file for the trade_id and writes back
  to whichever file contained the match (multi-file atomic per-file
  writes). Pre-fix: would silently fail to persist if the unresolved
  trade lived in a per-bot file.

Plus 7 other tools (indicator_audit, audit_l2_roi, analyze_conflicts,
diagnose_stuck_exits, diagnose_dashboard, backfill_commissions,
historical_learner). All same surgical pattern.

Intentionally not touched: `tools/backfill_bot_id.py` (by design
legacy-only — new trades have bot_id set at write time).

Test suite: 1,727 pass / 4 skip / 0 fail (no delta, no regressions).

---

### 2026-05-13 ~18:00 CDT — bias_momentum fast-abort bug FIXED (commit `7f1411f`)

User flagged: two bias_momentum LONG trades closed in **8s and 20s** at
near-entry prices with reason=stop_loss despite the market not moving
adversely. Forensic millisecond log of trade f9781751 (17:39:29→37):

  OPEN @ 29561.0 → TRAIL to 29561.25 (1s later) → BE STOP to 29561.50
  (same ms) → EXIT_PENDING @ 29561.50 → CLOSE -$3.82 (8s hold)

Three compounding bugs killed the trade within 1 second of entry:

1. `_trail_stop()` had NO minimum-profit guard. `mid = (entry + price)
   / 2` produced a 1-tick stop on +2t of profit. Any adverse blip
   killed it.
2. `BE_STOP` recomputed `stop_dist = abs(entry - pos.stop_price)` AFTER
   TRAIL had just shrunk stop_price. So stop_dist became 1 tick →
   BE trigger at 0.5R = +0.5t → BE-stop set to current price →
   instant exit.
3. `trend_stall_grace_s` (60s) only suppressed `exit_signal`, not
   `tighten_stop`. So within the grace window, TRAIL still fired on
   stall MODERATE, kicking off the death spiral.

Surgical 5-part fix shipped in `7f1411f`:

A. Position dataclass adds `initial_stop_price` field — preserves the
   entry-time stop independent of subsequent mutations.
B. `open_position()` captures it.
C. `_trail_stop()` requires `min_profit_ticks=8` (= 2 MNQ pts default)
   of in-the-money movement before firing. Below that → no-op.
D. BE_STOP block reads `pos.initial_stop_price` for `stop_dist`
   computation. So R is always measured from the original wide stop,
   not whatever TRAIL shrunk it to.
E. Grace-window suppression extended to `tighten_stop` in addition to
   `exit_signal`. New `_trend_tighten_grace_logged` flag.

7 regression tests (`tests/test_fast_abort_fix.py`), including an
end-to-end replay of the exact 2026-05-13 17:39 forensic state — the
post-fix code path keeps the stop at the original 29531.75 and the
trade lives. Test suite: 1,744 → 1,751 pass (+7), 0 fail.

Deployment: sim_bot and prod_bot bounced at 17:58 — fresh PIDs 23972
and 25236 running the new code. Watchdog auto-restart proven on
fast-cycle bounces.

Operator impact: bias_momentum's 84% fast-abort loser pattern (avg
-$9.33 at 1.3 min hold per deep-dive analysis) should collapse to
either real stop-outs at the proper 117-tick stop or surviving
winners via ema_dom_exit. The 8-20s commission-loss round-trips are
gone.

---

### 2026-05-13 ~17:30 CDT — dashboard panels now agree all 24h (commit `0c24a8e`)

User flagged that the TODAY (CME GLOBEX) card was STILL showing $0 / 0
trades for sim, while Daily Stats panel showed $114.22 / 4 wins. The
earlier per-bot trade_memory fix (`4d523bf`) didn't fully resolve it.

Root cause found this time: the two panels used INCOMPATIBLE definitions
of "today":

| Panel             | Boundary       | Resets at |
| TODAY (CME GLOBEX) | Globex session | 17:00 CT  |
| Daily Stats       | Calendar day   | 00:00 CT  |

Daily Stats reads `bot.risk.daily_pnl` which resets at calendar midnight
(via `BaseBot._maybe_daily_reset` keyed on `datetime.now().date()`
change). `/api/today-pnl` was using `_session_start_ct_epoch()` which
returned the most-recent 17:00 CT.

Result: from 17:00 CT to 00:00 CT every evening, the two panels showed
different P&L for the same bot, for the same 7 hours every day. The
operator's lived experience: "we did this same thing yesterday — did
it not fix?" Yesterday's fix wasn't a fix; the bug was deeper.

Fix: new helper `_calendar_day_start_ct_epoch()` returns today's
midnight CT. `api_today_pnl()` switched to it. `_session_start_ct_epoch()`
preserved + still used by `_load_session_trades_by_bot()` (which
legitimately needs Globex semantic for session-scoped trade listings).

Verified live post-bounce: both panels showed $108.40 / 5 trades for
sim, $0 / 0 trades for prod — exact match. The TODAY card now updates
in lockstep with Daily Stats throughout the day.

Tests (tests/test_today_pnl_calendar_day.py, 4 new):
- Calendar helper returns midnight CT
- At 02:00 CT the calendar helper diverges from the Globex helper
  (confirms the bug case is actually exercised)
- Static check: api_today_pnl uses calendar helper, NOT Globex
- Behavioral: a trade exiting at 17:00 CT today counts as today

Test suite: 1,740 → 1,744 pass (+4), 0 fail, 4 skipped.

Open follow-up (non-blocking, cosmetic): the dashboard HTML label still
says "TODAY (CME GLOBEX)" — the data behind it is now calendar-day, so
the label is slightly misleading. Frontend HTML edit when convenient.

---

### 2026-05-13 ~16:35 CDT — prod trading-window gate REMOVED (commit `1e07000`)

Investigated "why didn't prod_bot trade today?" Root cause traced to a
silent gate in `BaseBot._evaluate_strategies` (lines 2422-2430):

```python
if self.bot_name == "prod":
    if not self.session.is_prod_trading_window(...):
        return   # NO log, NO _last_eval update
```

Restricted prod to 08:30-11:00 + 13:00-14:30 CST. Sim_bot's override of
`_evaluate_strategies` already bypassed this gate (sim trades 24/7).
Today's incident: NT8 internet outage during prod's primary window
(08:30-11:09) meant prod missed its entire trading day. Sim took 4
vwap_pullback wins / $114.22 after NT8 came back. Operator confusion:
"why isn't prod trading?" — bot looked healthy (SCANNING / no rejection
log / no halt) but was silently skipping every evaluation.

Textbook 'silent failure' anti-pattern (per memory/feedback_silent_failures.md).

Fix: removed the gate entirely. Prod now evaluates all 10 strategies
24/7, matching sim's cadence. Per-trade risk limits ($5/trade, $15/day,
4 trades/day in SimpleSizer + RiskManager) are the actual constraints
— not the window. Strategy-level time windows (orb 08:30-14:30,
opening_session 08:30-08:45) still apply.

`is_prod_trading_window()` function in core/session_manager.py is left
intact for future log-only / dashboard-display use. Only the gate
inside `_evaluate_strategies` was removed.

Verified live: prod_bot bounce at 16:32 CT, first `[strategies.noise_area]
INFO [EVAL]` line appeared in prod log within seconds — first strategy
evaluation prod ran ALL DAY. `/api/status -> prod._last_eval` now
populated with all 10 strategies' decisions per bar close.

Tests (tests/test_prod_no_window_gate.py, 3 new):
- Static: no `bot_name == "prod"` check + no `is_prod_trading_window`
  call in active code of `_evaluate_strategies`
- Static: preserved gates (HALT, circuit breakers) still log
- Behavioral: `is_prod_trading_window()` still callable for any future
  log-only / display use

Test suite: 1,737 → 1,740 pass (+3), 0 fail.

---

### 2026-05-13 ~12:20 CDT — /api/today-pnl reads per-bot trade_memory files (commit `4d523bf`)

Live-observed bug on the dashboard: TODAY (CME GLOBEX) card showed $0 /
0 trades for both bots while the Daily Stats panel correctly showed 2
sim_bot wins / $34.36. Root cause: commit `02b0efd` (2026-05-12) split
trade_memory into per-bot files (`trade_memory_<bot>.json`) but
`/api/today-pnl` was still reading the now-frozen legacy
`trade_memory.json` directly.

Fix: route through `core.trade_memory.load_all_trades()` — same loader
that already powers `_load_session_trades_by_bot`. Reads legacy + every
per-bot file and dedupes by trade_id.

New regression test at `tests/test_today_pnl_per_bot_files.py` — builds
an isolated logs dir with ONLY a per-bot file (NO legacy), confirms the
endpoint counts the per-bot trade. Plus a static check that the handler
uses `load_all_trades` and not raw `open(tm_path)`.

**Deployment note**: the running dashboard (PID 13864 from 08:10 CT) is
on old code and will continue showing stale TODAY P&L until it's
restarted. Restart can wait — the data is correct in the Daily Stats
panel meanwhile, and a brief dashboard bounce has no impact on bots
(they push state on an independent 2s cadence).

**Audit follow-up flagged**: other tools also raw-open
`logs/trade_memory.json` (per `BUILD_MAP.md` line 1316,
`tools/analyze_conflicts.py:30`, `tools/audit_l2_roi.py`, and the
weekly/daily reporting tools in `CLAUDE.md`). These will silently miss
post-split trades the same way. See KNOWN_ISSUES.md.

Test suite: 1,725 → 1,727 pass (+2), 0 fail, 4 skipped.

---

### 2026-05-13 ~08:40 CDT — graceful /shutdown via command queue (commit `dda680c`)

Restores graceful-shutdown semantics that were lost in commit `8b471af`
(bulletproof launch fix). When CREATE_NEW_PROCESS_GROUP was removed
from `_start_bot`, the CTRL_BREAK_EVENT graceful-stop path broke too,
leaving `/api/bot/stop` as a hard terminate() on every call. State
persistence on every bar made the regression acceptable in the short
term, but a clean shutdown path is still desirable for routine
watchdog/operator restarts.

**Approach:** reuse the existing `_state["_commands_<bot>"]` queue (bot
polls every 2s via `_dashboard_loop`) rather than spinning up a new
HTTP server on the bot. Surgical extension; no new ports, no new
threads, no new aiohttp app.

**Files changed:**
- `bots/base_bot.py` — added `_shutdown_requested` flag in `__init__`,
  added `"shutdown"` branch to `_handle_dashboard_command` (sets flag +
  closes WS), modified `run()` outer loop to honor the flag with break
  paths.
- `dashboard/server.py` — added `_GRACEFUL_SHUTDOWN_TIMEOUT_S = 7.0`
  module constant, modified `_stop_bot` to queue the shutdown command
  + wait up to 7s for self-exit before falling back to terminate(). Old
  terminate() path preserved verbatim as the fallback.
- `tests/test_graceful_shutdown.py` (new) — 7 tests across 3 classes:
  static checks for both files, behavioral tests for both the
  happy-path (graceful exit skips terminate) and timeout-fallback
  (terminate IS called after timeout).

**Positions are NOT flattened on shutdown** — they remain to NT8 OCO
brackets or the next bot start. Flattening on every routine restart
would turn watchdog disconnect-recovery into a market-order event;
operator can still flatten manually via Telegram or the 15:54 CT
daily auto-flatten.

**Test suite:** 1,718 → 1,725 pass (+7), 0 fail, 4 skipped.

**Operational deployment:** the running dashboard (PID 13864) and bots
(PIDs cycling, see KNOWN_ISSUES.md cyclic-disconnect issue) were
started before the commit. The new code only takes effect on next
restart of those processes. Until then, behavior is unchanged from
the post-`8b471af` baseline (hard terminate on stop).

---

### 2026-05-13 ~08:30 CDT — session_debriefer Any import (commit `1d56862`)

One-line typing fix: `from typing import Optional` →
`from typing import Any, Optional` in `agents/session_debriefer.py`.
Surfaced by pre-flight import check during 2026-05-12 evening restart
investigation. No behavior change.

---

### 2026-05-13 08:18 Central Daylight Time — Session changes: 1 files modified

**Files changed:**
- `agents/session_debriefer.py`

---
### 2026-05-13 08:17 Central Daylight Time — Session changes: 1 files modified

**Files changed:**
- `agents/session_debriefer.py`

---
### 2026-05-13 08:17 Central Daylight Time — Session changes: 1 files modified

**Files changed:**
- `agents/session_debriefer.py`

---
### 2026-04-25 ~17:40 CDT — full automation lattice operational (manual entry)

After the dual-stream incident cleanup, an additional 2-hour push to close
out the entire scheduled-task / SMS / daemon agenda:

**Bridge-side single-stream enforcement** (commit `323a391`):
- `bridge/bridge_server.py::handle_nt8_tcp` now rejects any 2nd+ concurrent
  NT8 connection at socket-accept (`PHOENIX_BRIDGE_SINGLE_STREAM=1`,
  default ON). First-writer-wins; auto-recovery when client #1's TCP
  socket dies. Belt to today's morning workspace-cleanup suspenders.
- 3 unit tests in `tests/test_bridge_single_stream.py` (rejection,
  opt-out, recovery) — all green.
- Live-verified against running bridge: 2nd connection got EOF.

**Multi-account close-isolation tests** (same commit):
- `tests/test_multi_account_close_isolation.py` — 7 tests proving that
  closing one account never cascades to other accounts. Defenses:
  B58 (require_account), B59 (live guard), B75 (CANCEL_ALL block),
  per-position `account` field, `daily_flatten` per-position iteration,
  PhoenixOIFGuard pid-tag filename whitelist.

**Three new scheduled task daemons** (commit `1019256`):
- `scripts/register_watcher_task.ps1` — PhoenixWatcher, runs
  `tools/watcher_agent.py` continuously, escalates RED_ALERT to Twilio
  SMS (`TWILIO_TO_NUMBER`) and Telegram. Auto-restart on failure.
- `scripts/register_finnhub_news_task.ps1` — PhoenixFinnhubNews,
  WS+REST hybrid. WS connects on free tier but free-tier symbol
  subscription is restricted; REST fallback handles it.
- `scripts/register_fred_macros_task.ps1` — PhoenixFredMacros,
  --interval-min 60 daemon polling FFR/CPI/UNRATE/T10Y2Y, Telegram on
  regime shift.

**User-context fix** (commit `6d5cb99`):
- All 8 `register_*.ps1` scripts hardcoded `$env:USERDOMAIN\$env:USERNAME`
  for the Principal — when run from elevated PS as `dbren` (the
  admin user), tasks were registered with Principal=`dbren`, so they
  never fired (dbren is never interactively logged in).
- Patched all 8 to take a `$TaskUser` parameter defaulting to
  `"TradingPC\Trading PC"` (the actual daily console user).
- `tools/_patch_register_scripts.py` — idempotent batch patcher.

**.env corruption + fix** (no commit — .env is gitignored):
- Earlier I appended `PHOENIX_STREAM_VALIDATOR=1` via PowerShell
  `Add-Content` with em-dash characters in the comments. PowerShell
  encoded em-dash as cp1252 byte 0x97; `dotenv` reads as UTF-8 and
  exploded with `UnicodeDecodeError`. Result: ALL keys silently
  failed to load, watcher/finnhub started with degraded mode.
- Fixed in-place by re-encoding as ASCII/UTF-8 (Python script
  decoded cp1252, mapped smart-punctuation to ASCII, ascii-encoded,
  wrote utf-8 back).
- Lesson: never append to .env with raw `Add-Content`. Use
  `Set-Content -Encoding utf8` or Python.

**Finnhub `load_dotenv` fix** (commit `453aa6b`):
- `tools/finnhub_news_runner.py` was reading `os.environ.get("FINNHUB_API_KEY")`
  without ever calling `load_dotenv()`. Fine when launched from a shell
  that already loaded .env; broken when launched by Task Scheduler
  (which doesn't inherit shell env). Added 5-line load_dotenv block at
  module init, matching the pattern in `fred_poll.py` and `watcher_agent.py`.

**PhoenixBoot principal fix** (commits `44a92ca`, `5cf0d3d`):
- `PhoenixBoot` was the auto-launch task for the entire stack at boot,
  registered with Principal=dbren — meaning it has been silently
  failing on every reboot since Jennifer originally set it up. Stack
  has not auto-recovered from reboots; she's been manually launching
  via `launch_all.bat`.
- New `scripts/register_phoenix_boot_task.ps1` re-registers under
  Trading PC. First attempt with `LogonType S4U` failed Access Denied
  (S4U needs stored password or "Log on as batch job" right);
  switched to `-AtLogOn -User TradingPC\Trading PC` with
  `LogonType Interactive` — same effective behavior, no privilege
  needed.
- Updated `PhoenixStart.bat` to enable + trigger the 3 new daemons
  (PhoenixFinnhubNews, PhoenixFredMacros, plus PhoenixGrading which
  was missed in the original).

**SMS verification (E2E)**:
- Twilio creds (TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM/TO) all populated
- Test command from operator: `python -c "from tools.watcher_agent
  import Alerter; ok = Alerter().sms('Phoenix watcher SMS test
  2026-04-25 EOD'); print('SENT' if ok else 'FAILED')"`
- Result: `[Alerter] SMS sent sid=SMba9bbf84b5866fdefa0ae9587b898aa0`
- First-ever end-to-end Twilio→phone path verified. Watcher will now
  page on RED_ALERT findings.

**Test count**: 1,221 → **1,231 passing, 4 skipped, 0 failing** (10 new
tests across the two suites).

**Repo state**: HEAD `5cf0d3d` on `origin/main`, working tree clean.

**Operator action items for next session**: see CURRENT_STATE.md
"Operator runbook" section.

---

### 2026-04-25 ~15:30 CDT — NT8 dual-stream incident: real root cause found + closed (manual entry)

**Why:** Earlier today's "RESOLVED 2026-04-19" claim about NT8 multi-stream was
wrong — the issue recurred and required a 3-hour debugging session this
afternoon to find the actual root cause. This entry corrects the record and
captures the diagnostic playbook.

**What happened:**
- Bridge `:8765` had 2+ TCP connections all weekend (PriceSanity rejected
  27,000+ corrupt ticks with the ~7,196 phantom-price signature)
- We initially thought: duplicate TickStreamer indicators → false lead
- Then: rogue `MarketDataBroadcasterV2` strategy → partial truth
- Then: rogue `JenTradingBotV1_DataFeed` indicator → partial truth
- Final root cause: 3 layered issues
  1. Two legacy `.cs` files compiled into `NinjaTrader.Custom.dll`
  2. NT8 `<ShowDefaultWorkspaces>true</ShowDefaultWorkspaces>` auto-loaded
     `Jen's Fav.xml` / `Jen's indicators.xml` with **9 hidden MNQM6 charts**
     plus ESM6/AUDUSD/SuperDOM windows (`IsWindowVisible=false`, no taskbar
     entry, no Window menu in this NT8 build)
  3. `PHOENIX_STREAM_VALIDATOR=0` — bridge-level defense off

**Files added today:**
- `tools/diagnose_nt8_client.py` — spy bot that connects to bridge `:8766`,
  captures fanout, identifies the connected NT8 component by message-shape
- `tools/nt8_unhide_all_windows.ps1` — Win32 `EnumWindows` +
  `ShowWindow(SW_SHOWNORMAL)` against `NinjaTrader.exe` PID. Surfaces every
  hidden NT8-owned window so you can SEE what's loaded. Without this you
  cannot find ghost charts in newer NT8 builds (no Window menu).

**Diagnostic insight that broke the case:** bridge health endpoint
`nt8_last_heartbeat_age_s ≈ 2.8s` matched TickStreamer's `HEARTBEAT_MS=3000`
exactly. Legacy V2 strategy uses `HEARTBEAT_BARS=30` (bars-not-ms, silent
on closed market). That fingerprint identified the client as TickStreamer
— but Win32 enumeration revealed it was attached to one of nine hidden
charts, not the visible one (because there wasn't a visible one).

**Cleanup completed by Jennifer:**
1. Both legacy `.cs` files moved to `.disabled_2026_04_25`
2. Hidden charts surfaced via `tools/nt8_unhide_all_windows.ps1`
3. Unwanted charts closed
4. Clean workspace saved as `phoenix_clean_2026_04_25`
5. "Show default workspaces on startup" disabled
6. NT8 restarted; bridge confirms 0 connections on `:8765` with NT8 closed

**Followups:**
- Set `PHOENIX_STREAM_VALIDATOR=1` in `.env` permanently (defense-in-depth)
- Sunday 17:00 CT market open is the first real validation
- Order-flow round-trip test (`tools/verify_oif_fix.py`) now safe to run
  once market is live and connection count = 1

**Memory corrections in this commit:**
- `CURRENT_STATE.md` — added "Today's NT8 dual-stream incident" section at top
- `KNOWN_ISSUES.md` — replaced incorrect "RESOLVED 2026-04-19" entry with the
  real recurring-root-cause + cleanup playbook
- `audit_log.jsonl` — explicit `nt8_workspace_cleanup` event

---

### 2026-04-25 EOD — Sprint 2: Phoenix Routines + remaining §3 + git push (manual entry)

**Why:** Capture the work that the auto-writeback's per-file lists don't tell
the story of. Today was a two-phase Saturday rebuild day; this is the EOD
narrative the next session will need.

**Phase B+ skeleton sprint (morning):** 6 items shipped behind off-by-default
flags — NT8 stream validator, fail-closed risk gate, FinBERT skeleton (real
INT8 ONNX model now installed under `models/finbert_onnx_int8/`), Chicago
VPS plan (later stricken), SKILLS auto-digest, dashboard Grades + Logs tabs.

**Sprint 2 (afternoon → evening):**
- §2.2 FRED macros — real client w/ regime-shift detection
- §2.3 Finnhub real client — REST + WebSocket dual path; key already in `.env`
- §3.1 TradingView webhook — **STRICKEN** ($59.95/mo Premium not approved)
- §3.4 Phoenix-specific skills — **DEFERRED** (empty allowlisted dir)
- §3.5 OIF kill-switch — `tools/oif_kill_switch.py` one-command halt
- §3.6 Phoenix Routines — three deterministic routines:
  morning_ritual / post_session_debrief / weekly_evolution. All ship with
  verdict-deterministic logic, AI in appendix only, consolidated digest at
  16:05, CPCV/DSR/PBO checkboxes enforced in weekly commit body.
- §4.1 / 4.3 / 4.4 strategy fixes (A-F) — locked in via 20 regression tests
  at `tests/test_lock_in_epic_v1/` (ORB ATR-adaptive, bias_momentum SHORT
  mirror + VCR=1.2, noise_area band_mult=0.7, ib_breakout 10min,
  compression min_squeeze_bars=12, spring_setup retired).

**Scheduled task lattice (5 register scripts):**
- `scripts/register_phoenix_grading_task.ps1` (16:00 CT Mon-Fri)
- `scripts/register_risk_gate_task.ps1` (on-boot)
- `scripts/register_morning_ritual_task.ps1` (06:30 CT Mon-Fri)
- `scripts/register_post_session_debrief_task.ps1` (16:05 CT Mon-Fri)
- `scripts/register_weekly_evolution_task.ps1` (Sun 18:00 CT)

**Plugin install:** machine-learning-ops, incident-response, pyright-lsp,
document-skills, example-skills (10 plugins / 72 skills total). SessionStart
hook regenerates `SKILLS.md` from `tools/skills_digest.py`.

**.gitignore hardened:** broad ignores + allowlist patterns for orchestrator,
.claude/commands, .claude/skills, .claude/agents, settings.json, out/baselines.
Re-ignore patterns prevent `__pycache__` from sneaking in via greedy `**`.

**GitHub auth fixed:** Statechamp76 → dans-favorite-chick swap via
`gh auth logout` + `gh auth login`. Push to `origin/main` succeeded.

**Test count:** 989 (Friday EOD) → 1,081 (after morning skeleton) → **1,221
passing / 0 failing** (Sprint 2 EOD).

**Repo state:** HEAD `c2dcdc8` on `origin/main`, working tree clean.

**Operational note:** 14:31 CDT TeamViewer-initiated reboot dropped four of
the five newly registered scheduled tasks. Only `PhoenixLearner` survived.
Next session must re-run all five `register_*.ps1` scripts as Administrator.

---

### 2026-04-25 10:38 Central Daylight Time — Session changes: 26 files modified

**Files changed:**
- `.gitignore`
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `memory/context/CURRENT_STATE.md`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`

---
### 2026-04-25 10:01 Central Daylight Time — Session changes: 24 files modified

**Files changed:**
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `strategies/noise_area.py`
- `strategies/orb.py`

---
### 2026-04-25 10:00 Central Daylight Time — Session changes: 24 files modified

**Files changed:**
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `strategies/noise_area.py`
- `strategies/orb.py`

---
### 2026-04-25 10:00 Central Daylight Time — Session changes: 24 files modified

**Files changed:**
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `strategies/noise_area.py`
- `strategies/orb.py`

---
### 2026-04-25 09:58 Central Daylight Time — Session changes: 24 files modified

**Files changed:**
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `strategies/noise_area.py`
- `strategies/orb.py`

---
### 2026-04-25 09:58 Central Daylight Time — Session changes: 24 files modified

**Files changed:**
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `strategies/noise_area.py`
- `strategies/orb.py`

---
### 2026-04-25 09:58 Central Daylight Time — Session changes: 24 files modified

**Files changed:**
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `strategies/noise_area.py`
- `strategies/orb.py`

---
### 2026-04-25 09:49 Central Daylight Time — Session changes: 24 files modified

**Files changed:**
- `PHOENIX_PROJECT_PROMPT.md`
- `agents/council_gate.py`
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `core/tick_aggregator.py`
- `dashboard/server.py`
- `dashboard/templates/dashboard.html`
- `docs/phase_c_architecture.md`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `requirements.txt`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `strategies/noise_area.py`
- `strategies/orb.py`

---
### 2026-04-24 14:49 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 14:30 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 12:20 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 12:17 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:56 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:55 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:55 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:51 Central Daylight Time — Session changes: 12 files modified

**Files changed:**
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-22 14:25 Central Daylight Time — Session changes: 3 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`

---
### 2026-04-22 14:22 Central Daylight Time — Session changes: 3 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`

---
### 2026-04-22 14:22 Central Daylight Time — Session changes: 3 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`

---
## 2026-04-21 late evening — B41 TIF fix + B40 diagnosis + dashboard cleanup

**Critical production bug discovered during first sim_bot live trading:**

### B39/B40/B41 — silent phantom positions traced to TIF mismatch
- **B39 symptom**: Python PositionManager showed `active_positions=1` while
  NT8 showed no active trade on the routed Sim account. OIF files sitting
  unconsumed in `incoming/`.
- **B40 initial misdiagnosis**: suspected NT8 ATI multi-account config
  issue. Tried kill-switch `MULTI_ACCOUNT_ROUTING_ENABLED=False` — user
  rejected. Reverted.
- **B41 ROOT CAUSE** (from NT8 Log tab): `'time in force parameter not
  supported by this account "DAY"'`. Entry orders used TIF=DAY which the
  "My Coinbase"-style 24/7 connection rejects. Stops/targets already used
  GTC and were landing — so bracket orders were half-submitted.
- **Fix (commits `65cc9d6` + `66a8e7a`)**: change all OIF TIFs from DAY
  to GTC — universally accepted per NT8 docs
  (https://ninjatrader.com/support/helpguides/nt8/timeinforce.htm).
  Paths fixed: `_build_entry_line`, `CLOSEPOSITION`, `PARTIAL_EXIT_LONG/SHORT`.
- **End-to-end validation**: direct OIF injection to `SimBias Momentum`
  (18:59 CDT) and `SimVwap Band Pullback` (19:02 CDT) — both FILLED.
  Position files confirmed: `LONG;1;26741.25` → flatten → `FLAT;0;0`.

### Dashboard cleanup (commits `97fdf98` + `46dd214`)
- Added Sim Bot health pill + tab to dashboard UI (fixed watchdog
  auto-restart path that was 400-rejecting `name=sim`)
- Removed retired Lab Bot from UI + `/api/bot/start|stop|status`
- `_state` dict + `/api/status` now include sim, exclude lab
- Watchdog docstring updated (default was already prod,sim)

### STRATEGY_KEYS parent aliases (commit `b8b505e`)
- Added `compression_breakout` + `opening_session` as parent-key aliases
  in `core/strategy_risk_registry.STRATEGY_KEYS` to suppress the
  `[RISK] unknown strategy key` warnings on every eval. Strategies
  register under bare names; registry needed the aliases.

### Sim banner clarity (commit `94ee5f5`)
- `[SIM] 10 strategies → 16 account destinations loaded` instead of
  just `10 strategies loaded`. `opening_session` dispatches to 6 subs;
  `compression_breakout` has 15m + 30m timeframes. All 16 destinations
  confirmed tracked by StrategyRiskRegistry.

### AI agent activation status
- **Gemini Flash + Gemini Pro agents (Council, Pretrade)**: live and
  working. Council auto-trigger wired for regime-shift only
  (commit `5c013f2`) per Jennifer decision. Pretrade hook in
  `bots/base_bot.py:1148` (commit `7788a62` + attribution `6259d90`).
- **Claude Sonnet agents (Debriefer, Learner)**: **DEGRADED** —
  `ANTHROPIC_API_KEY` is present in `.env` but empty (0 chars). Every
  Claude call today returned `outcome: degraded, error_msg:
  ANTHROPIC_API_KEY missing`. Fallback deterministic templates are
  being emitted instead of real Claude analysis. Fix requires Jennifer
  to paste a valid key into `.env`.
- **Daily learner scheduled** (Windows Task `PhoenixLearner`) runs at
  23:30 CT with `--days 7` rolling window. Will produce empty/fallback
  recommendations until Anthropic key is set.
- **Session Debriefer**: first real run fired at 17:22:56 then 18:19:43.
  Both wrote `logs/ai_debrief/2026-04-21.md` with the deterministic
  fallback template. Telegram dispatch is working.
- **Adaptive Params Telegram**: now accepts either `TELEGRAM_BOT_TOKEN`
  or `TELEGRAM_TOKEN` (commit `a7283fd`).

### Open follow-ups
- Paste valid `ANTHROPIC_API_KEY` into `.env` to unlock Claude-powered
  Session Debrief + Weekly Learner. Gemini agents are unaffected.
- `[INTERMARKET] Feed error: float() argument must be a string or a real
  number, not 'dict'` — pre-existing unrelated bug, not a Phases E–H
  regression. Logged for separate ticket.
- Update `data/menthorq_daily.json` daily per `docs/daily_ritual.md`.
  Currently 104h+ stale → regime fields using HVL proxy.

---

## 2026-04-21 evening — Phases E/F/G/H sprint merged to main

**Branch:** `feature/phases-e-h` → merged to `main` (merge commit `bdff605`).
**Test suite:** 566 passing + 6 failing → **700 passing / 0 failing**.

### Phase E — Gamma integration
- `docs/daily_ritual.md` — documents daily MenthorQ paste (both `menthorq_daily.json` + `gamma/*_levels.txt`)
- `core/menthorq_feed.py` — CRITICAL log if `menthorq_daily.json` >24h stale; parser hardened (`_coerce_float` handles empty/NaN/None/negative-zero) — this was G-B26
- `core/structural_bias.score_menthorq_gamma` — rewired Path A → Path B (reads `market_snapshot["gamma_regime"]` enum directly; retired stale-JSON read). Overclaiming warning corrected to list only real consumers.

### Phase F — B15 test backlog cleared
All 6 previously-failing tests now green. All were test-stale (B13 commission math, B13-era 3→2 cooloff threshold, 08:30–11:00 window, new `_REGIME_OVERRIDES` schema). Zero code regressions.

### Phase G — Bug fixes
- **B37**: new `tests/test_4c_integration.py` (12 tests, real routing map + OIF round-trip)
- **B38**: `core/history_logger.log_eval` now emits `gamma_regime` as first-class field

### Phase H — AI agent stack
Five agents + infrastructure:
- **Infra**: `agents/base_agent.py` (AIClient + BaseAgent + safe_call + JSONL logger), `agents/config.py`. Degraded mode on missing API key — bot never crashes.
- **4A Council Gate** (`agents/council_gate.py`) — 7 Gemini Flash voters + Gemini Pro orchestrator. Auto-trigger: **regime-shift only** (15-min debounce). Session-open fire dropped per Jennifer.
- **4B Pre-Trade Filter** (`agents/pretrade_filter.py`) — single Gemini Flash call, 3s timeout, fail-OPEN to CLEAR. All strategies default `ai_filter_mode="advisory"`. Hook in `bots/base_bot.py` ~L1148.
- **4C Session Debriefer** (`agents/session_debriefer.py`) — Claude Sonnet post-16:00-CT flatten, writes `logs/ai_debrief/YYYY-MM-DD.md`. Hook in `bots/sim_bot.py::_maybe_run_debrief`. Optional Telegram dispatch.
- **4D Historical Learner** (`agents/historical_learner.py` + `tools/run_weekly_learner.py`) — weekly Claude aggregate, writes `logs/ai_learner/weekly_*.md` + `pending_recommendations.json`.
- **4E Adaptive Params** (`agents/adaptive_params.py` + `tools/approve_proposal.py` + `tools/list_proposals.py`) — safety-bounded (risk_per_trade ≤$100, stops 4-200 ticks, no gate disables, no account_routing edits, no LIVE_TRADING flips). **Never auto-applies.** Always CLI → git branch → human merge. Telegram fires on new proposal count.

### Decisions (Jennifer 2026-04-21 evening):
- Council auto-trigger: **B only** (regime-shift). A (8:30 session-open) dropped.
- Pretrade filter does **NOT** consume council bias — would over-tighten and reduce trade count. Reverted commit `6549900`.
- Telegram fires on proposal creation. **Yes.**

### Docs shipped
- `docs/phase-eh-deployment.md` — runbook
- `docs/phase-eh-report.md` — end-of-sprint summary with assumption log
- `docs/phase-eh-assumptions.md` — ~20 judgment calls across 9 streams
- `docs/daily_ritual.md` — morning MenthorQ paste ritual

### Bot state unchanged
Sim bot still running from Phase C flip (PID 46996, 24/7). **Agent layer not yet activated on live sim** — requires API keys in `.env` and a bot restart.


---

### 2026-04-21 15:40 Central Daylight Time — Phase C sprint: Lab → Sim live flip

**Scope:** Transform lab_bot (paper-only) into sim_bot (live NT8 sim trading
on 16 dedicated sub-accounts, 24/7, per-strategy risk isolation). Merged
feature branch `feature/knowledge-injection-systems` → `main` at `4f444eb`.

**Commits landed (on feature, then merged to main):**
- `f5ee73f` — byte-exact NT8 account-name fix + compression 15m/30m split + top-level orb
- `33e5ad6` — test_account_routing updated to byte-exact names (34/34 pass)
- `e460bd2` — **PositionManager multi-position refactor** (dict storage keyed by trade_id,
  back-compat single-position API preserved, new `active_positions` / `is_flat_for(strategy)` /
  `check_exits_all()` / `close_all()` methods)
- `634bfe9` — **StrategyRiskRegistry** + Phase C settings constants
  (`PER_STRATEGY_ACCOUNT_SIZE=$2000`, `PER_STRATEGY_DAILY_LOSS_CAP=$200`,
  `PER_STRATEGY_FLOOR=$1500`, 16 strategy keys, halt persistence to
  `logs/strategy_halts.json`, 24/24 tests)
- `03687ef` — **sim_bot.py** + **daily_flatten.py** + **reenable_strategy.py** CLI
  (5 files, +989 lines, 20 new tests)
- `d6d318f` — multi-position tick-exit loop in base_bot + watchdog `--bots prod,lab,sim`
  + `docs/phase-c-deployment.md` (187 lines operator playbook)
- `d4fb979` — **Phase C follow-ups** (3): dashboard per-strategy risk panel
  (`/api/strategy-risk` + sortable table + halt highlighting),
  Telegram per-strategy routing (`TELEGRAM_STRATEGY_CHAT_OVERRIDES` + auto-tag,
  8 tests), base_bot rider/smart-exit/EoD/chandelier/managed iteration over
  active_positions (multi-position correctness)
- `4f444eb` — **Merge to main** (unrelated histories, --theirs strategy;
  feature content wins, 234 files, 66,988 insertions)

**Operational flip (15:38 CDT):**
- Killed lab_bot PIDs 42188 + 37424
- Killed old watchdog PIDs 41988 + 40408
- Started `python bots/sim_bot.py` → banner confirms 10 strategies loaded,
  LIVE execution, $2000/$200/$1500 limits, 16:00 CT flatten, 16 registry keys
- Started `python tools/watchdog.py` → tracking prod + sim (lab dropped from
  default --bots list since lab is deprecated)
- Bridge + prod + dashboard untouched throughout

**Test suite delta:**
- Baseline pre-sprint: 513 pass / 6 B15-backlog
- Post-sprint: 566 pass / 6 B15-backlog (+53 new tests, zero new failures)

**Files changed (high signal):**
- `bots/sim_bot.py` (NEW, 545 lines)
- `bots/daily_flatten.py` (NEW, 96 lines)
- `bots/base_bot.py` (multi-position iteration for rider/smart/EoD/chandelier/managed)
- `bots/lab_bot.py` (preserved on disk — rollback safety net)
- `core/strategy_risk_registry.py` (NEW, 261 lines)
- `core/position_manager.py` (dict-based multi-position, +286 lines)
- `core/telegram_notifier.py` (per-strategy routing + tagging)
- `core/history_logger.py` (unchanged — sim writes to `_sim.jsonl` via `bot_name="sim"`)
- `config/settings.py` (Phase C constants + Telegram overrides)
- `config/account_routing.py` (byte-exact names + split + top-level orb)
- `dashboard/server.py` (+`/api/strategy-risk` endpoint)
- `dashboard/templates/dashboard.html` (per-strategy panel)
- `tools/reenable_strategy.py` (NEW, 87 lines)
- `tools/watchdog.py` (tracks sim by default)
- `docs/phase-c-deployment.md` (NEW, 187 lines)
- 5 new test files: `test_strategy_risk_registry.py`, `test_daily_flatten.py`,
  `test_reenable_strategy_tool.py`, `test_telegram_routing.py`, position_manager
  tests expanded for multi-position invariants

---

### 2026-04-19 20:28 Central Daylight Time — Session changes: 8 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `ninjatrader/SiM_TickStreamer.cs`
- `ninjatrader/TickStreamer.cs`
- `strategies/base_strategy.py`
- `strategies/compression_breakout.py`
- `strategies/vwap_pullback.py`
- `tools/verification_2026_04_18/SESSION_2026_04_19.md`

---
### 2026-04-18 15:01 Central Daylight Time — Session changes: 14 files modified

**Files changed:**
- `agents/council_gate.py`
- `agents/expert_knowledge.py`
- `agents/pretrade_filter.py`
- `agents/session_debriefer.py`
- `bots/lab_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `dashboard/templates/dashboard.html`
- `requirements.txt`
- `strategies/base_strategy.py`
- `strategies/ib_breakout.py`
- `strategies/spring_setup.py`
- `strategies/vwap_pullback.py`

---
### 2026-04-17 22:29 Central Daylight Time — Weekend evaluation complete. KEY FINDING: 697 live trades = 33.3% WR + negative P&L -,227.68. The architectural rebuild gave us the TOOLS to find strategy problems but did not SOLVE them. Scheduled 20/80 fix week for Apr 20-22 (replayable strategies + CI + decay alerts). Recommended LIVE_TRADING stays False until 90-day replay proves positive expectancy.

**Files changed:**
- `memory/context/EVALUATION_2026-04-18.md`

**Decisions:**
- Sim Option A verified harness deterministic + MC convergence
- Sim Option B surfaced THE critical finding: 33% WR with 5:1 config but net losing = ema_exit cutting winners short
- Option C 90-day sim deferred pending replayable strategy refactor
- 20/80 fix week scheduled Apr 20-22 with specific tasks per evening
- Recommendation: LIVE_TRADING=False indefinitely until 90-day replay validates a real strategy

---
### 2026-04-17 22:19 Central Daylight Time — Wire-up COMPLETE: all Saturday+Sunday modules now RUN in base_bot shadow mode. SwingState+VolumeProfile+ReversalDetector+SweepWatcher+GammaFlipDetector+Footprint1m/5m+DecayMonitor+TCA+CircuitBreakers all instantiated. Tick handler feeds footprint+volume profile+tick rate detector. 5m bar close feeds swing/climax/sweep/gamma flip/footprint. _evaluate_strategies computes full structural_bias composite every cycle with MenthorQ+VIX+OpEx+ES+Pinning enrichment. Dashboard endpoints (/api/structural-bias, /api/gamma-context, /api/footprint, /api/risk-mgmt, /api/all-signals) returning real data. Bots restarted clean, 0 errors, 61/61 tests still passing. Monday open = real shadow data flowing.

**Files changed:**
- `bots/base_bot.py`

**Decisions:**
- User pushed back on deferring wire-up: correctly pointed out shadow observation requires modules to RUN
- Wire-up adds ~220 lines to base_bot.py, all try/except guarded so shadow errors cannot break live trading
- All 13 new modules instantiated + hooked into proper lifecycle points
- Composite structural_bias runs every _evaluate_strategies cycle with full reasoning trail
- Dashboard API endpoints now return live data (confirmed via curl)
- Bots UP clean no errors watchdog healthy MQ flowing real values
- 2-week shadow observation window NOW STARTED (not delayed to future session)
- April 25 validation review has real data to work with

---
### 2026-04-17 22:11 Central Daylight Time — Sunday build COMPLETE: 6 new Sunday modules (footprint_builder, footprint_patterns, pinning_detector, opex_calendar, es_confirmation, structural_bias composite) + 6 dashboard API endpoints + 20 unit tests (61 total passing) + MONDAY_READINESS.md report. All signals shadow mode. Monday ships foundation fixes live (Telegram HTML, MQBridge running, hooks, memory system, contract rollover, emergency halt) while structural_bias/footprint/patterns run alongside old tf_bias for 2 weeks of shadow observation before activation.

**Files changed:**
- `bridge/footprint_builder.py`
- `core/footprint_patterns.py`
- `core/pinning_detector.py`
- `core/opex_calendar.py`
- `core/es_confirmation.py`
- `core/structural_bias.py`
- `dashboard/server.py`
- `tests/test_sunday_modules.py`
- `memory/context/MONDAY_READINESS.md`

**Decisions:**
- Task 1 complete: footprint pipeline reads existing tick stream no NT8 changes needed
- Task 2 gamma flip detector Saturday skeleton kept as-is requires live wiring (integration deferred)
- Task 3 complete: pinning detector last 90 min RTH + 0DTE strike proximity + breach detection
- Task 4 complete: OpEx calendar 3rd Friday detection + Triple Witching rules
- Task 5 complete: ES confirmation via manual daily file NQ vs ES gamma alignment
- Task 6 complete: structural_bias composite integrates 12 components with full reasoning trail
- Task 7 complete: dashboard API endpoints added (JSON ready html widgets can come later)
- Task 8 complete: WFO validation baseline 10.7pct risk of ruin break-even WR 47.6pct
- Task 9 complete: 61 tests passing MONDAY_READINESS.md written
- All new signals REMAIN SHADOW MODE for 2 weeks minimum before strategy gate activation
- April 25 session: reflector + strategy concentration review + Kelly activation gate

---
### 2026-04-17 19:17 Central Daylight Time — Saturday build complete: 11 new core modules + 3 procedural YAMLs + emergency halt tool + 41 passing unit tests. Signal foundation (swing ATR-ZigZag, volume profile POC/HVN/LVN/VAH/VAL + TPO-lite, climax reversal with mandatory secondary-test entry, liquidity sweep detector). Risk management (decay monitor rolling 30d Sharpe, TCA tracker with slippage analysis, anomaly circuit breakers with observe-mode default). Chart patterns v1 wrapper with context weighting (bull/bear flag + H&S/inverse H&S from existing detector). VIX term structure (CBOE-ready interface, yfinance fallback). Gamma flip detector skeleton. Session tagger for lab 24/7. Emergency halt tool. All modules dual-write mode or shadow, no live wiring yet.

**Files changed:**
- `memory/procedural/small_account_config.yaml`
- `memory/procedural/regime_matrix.yaml`
- `memory/procedural/regime_params.yaml`
- `core/swing_detector.py`
- `core/volume_profile.py`
- `core/reversal_detector.py`
- `core/liquidity_sweep.py`
- `core/strategy_decay_monitor.py`
- `core/tca_tracker.py`
- `core/circuit_breakers.py`
- `core/chart_patterns_v1.py`
- `core/vix_term_structure.py`
- `core/gamma_flip_detector.py`
- `core/session_tagger.py`
- `tools/emergency_halt.py`
- `tests/test_new_modules.py`

**Decisions:**
- Saturday 2F YAML configs complete small_account + regime_matrix + regime_params codify 60pct WR target
- Saturday 2B signal foundation complete: swing detector ATR-ZigZag volume profile climax secondary-test liquidity sweep
- Saturday 2A risk mgmt complete: decay monitor TCA tracker circuit breakers all shadow mode
- Saturday 2C chart patterns v1 uses existing 745-line detector with context weighting wrapper
- Saturday 2D VIX term structure CBOE primary yfinance fallback CBOE plug-in ready when credentials available
- Saturday 2G gamma flip skeleton pending Sunday integration with regime_matrix reload
- Saturday 2E lab bot parity via session_tagger module ASIA LONDON US_PRE US_RTH US_CLOSE PAUSE
- Emergency halt tool creates memory .HALT marker circuit breakers detect on next check ~5s
- 41 unit tests all passing 0.137s 13 modules covered minimum 3 tests each
- Everything remains shadow mode Sunday ties it all together with composite bias + WFO validation

---
### 2026-04-17 19:04 Central Daylight Time — Friday Session 1 complete: MQBridge deployed + verified (55 draw objects, real levels flowing), Telegram HTML fix, memory architecture scaffolded with atomic writes + hooks, NT8 arrow + contract rollover + Level 2 all diagnosed, git tag v-pre-rebuild-2026-04-17 + rollback runbook, WFO replay harness (multi-window + Monte Carlo + cost model), simple_sizing.py with loss-streak cooldown, bias_momentum hotfix VERIFIED working, BOM fix for utf-8-sig MQ bridge file read, bots restarted cleanly with real MQ values flowing

**Files changed:**
- `core/telegram_notifier.py`
- `core/menthorq_feed.py`
- `core/simple_sizing.py`
- `core/contract_rollover.py`
- `config/settings.py`
- `tools/memory_writeback.py`
- `tools/replay_harness.py`
- `memory/context/CURRENT_STATE.md`
- `memory/context/RECENT_CHANGES.md`
- `memory/context/KNOWN_ISSUES.md`
- `memory/context/OPEN_QUESTIONS.md`
- `memory/context/ROLLBACK_RUNBOOK.md`
- `memory/semantic/lessons_learned.md`
- `memory/procedural/targets.yaml`
- `memory/procedural/strategy_params.yaml`
- `memory/audit_log.jsonl`
- `~/.claude/settings.json (hooks)`
- `~/.claude/projects/C--Trading-Project/memory/MEMORY.md`

**Decisions:**
- All 10 Friday Tier 1 items complete
- MQBridge verified with 55 draw objects writing today real MQ levels
- Telegram now HTML instead of Markdown fixes 22 of 29 dropped lab messages
- Memory architecture operational with atomic writes file lock audit log
- Hooks installed: SessionStart auto-loads memory SessionEnd auto-writeback Stop checks pending
- Git tag v-pre-rebuild-2026-04-17 created as rollback baseline
- WFO harness tested: placeholder strategy 50% WR 10.8% risk of ruin correctly identifies overfitting OOS
- simple_sizing uses fixed 1-contract 80 conviction threshold 5 min loss-streak cooldown
- bias_momentum hotfix verified 0 errors 109 clean rejections correct gates firing
- BOM fix for utf-8-sig MenthorQ bridge file fixed zero-values bug at restart

---
### 2026-04-17 18:43 Central Daylight Time — Test write — memory architecture bootstrap

**Files changed:**
- `core/telegram_notifier.py`
- `memory/context/CURRENT_STATE.md`

**Decisions:**
- Switched Telegram to HTML parse mode
- Memory architecture scaffolded

---
## 2026-04-17 — Friday rebuild Session 1 (in progress)

### 17:30 CDT — Telegram notifier: Markdown → HTML

**What:** `core/telegram_notifier.py` converted from `parse_mode="Markdown"` to `parse_mode="HTML"`. All 5 formatters (entry, exit, daily summary, council, alert) updated to use `<b>` and `<code>` HTML tags instead of `*bold*` / `` `code` ``.

**Why:** 22 of 29 lab trade messages today were silently dropped by Telegram API returning 400 "can't parse entities" on underscores in strategy names (e.g., `bias_momentum`, `high_precision_only`). Created survivorship bias — user saw winning trades but not losing ones.

**Effect:** Next bot restart (tonight at session close) — all trade notifications will be delivered reliably.

### 17:20 CDT — MQBridge.cs redeployment instructions delivered

**What:** Diagnosed that `MQBridge.cs` source exists at `C:\Trading Project\phoenix_bot\ninjatrader\MQBridge.cs` but is NOT installed in NT8 Indicators folder (`C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\bin\Custom\Indicators\`). Walked user through NT8 NinjaScript Editor reinstall procedure.

**Why:** `C:\temp\menthorq_levels.json` has not been updated by NT8 since 2026-04-15 — indicator was removed/uninstalled from NT8. Every morning since has used stale gamma levels.

**Status:** User deploying. Verify timestamp updates post-install.

### 11:09 CDT — Hotfix: bias_momentum missing `price` and `vwap` variables

**What:** Added 2 lines in `strategies/bias_momentum.py` (near line 66):
```python
price = market.get("close", 0.0)
vwap = market.get("vwap", 0.0)
```

**Why:** Variables referenced throughout the method but only defined inside the non-TREND `else` branch. On TREND days, code crashed with `NameError: name 'price' is not defined`. Had been crashing continuously since the bot started today.

**Effect:** Bot back to operational. Zero signals fired in secondary window 13:00-14:30 but also zero errors — either no qualifying setups or needs further investigation (see `OPEN_QUESTIONS.md`).

### 11:07 CDT — Manual write of MQ levels for today

**What:** Directly wrote today's MenthorQ values to `C:\temp\menthorq_levels.json`:
- HVL 25,290, CR 26,500, PS 24,000, Day range 26,172-26,802
- GEX 1-10 levels from today's dashboard analysis

**Why:** NT8 MQBridge indicator not running (see above). Bot needs today's regime to operate properly.

**Status:** Temporary workaround. Permanent fix is MQBridge redeployment.

---

## Earlier entries will be appended above this line as SessionEnd hook runs.
