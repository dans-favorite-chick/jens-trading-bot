# Phoenix Bot — Current State

_Last updated: 2026-05-25 08:38 Central Daylight Time_

**Quick refs:**
- **[RECENT_CHANGES.md](RECENT_CHANGES.md)** — running dated log of every change, newest first
- **[KNOWN_ISSUES.md](KNOWN_ISSUES.md)** — open technical issues with status
- **[OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)** — decisions waiting on operator input
- **This file's lower section** — historical sprint context (May 4 onwards). Always defer to the top section + the bot itself for current operational truth.

---

## AT A GLANCE — 2026-05-18 09:55 CT (Monday morning, Phase 13 research complete)

| Item | State |
|---|---|
| Branch | `weekly-evolution/2026-05-17` (Phase 13 deliverables pending commit) |
| **Phase 13 Status** | **Research COMPLETE — production code untouched per operator instruction.** Ready for Step 3 (apply verdicts) in next session. |
| **Phase 13 deliverable: `docs/PHASE_13_IMPLEMENTATION_PLAN.md`** | Full 9-section plan: kill list, promotions, filter additions, exit policies, 7 new strategies tested, bug B2+B3 root causes, infrastructure D1-D4, reallocation, compounding plan (Section I). |
| **NEW 3 winning strategies (Phase 13C)** | `inside_bar_breakout` (+$11.3k/5y, PF 4.88), `multi_day_breakout` (+$9.1k, PF 6.79), `asian_continuation` (+$5.9k, PF 8.29, DD $21). All standalone in `tools/phoenix_new_strategy_lab.py`. Promote to production classes in next session. |
| **Bug B2: open_drive pivot_pp** | Root cause confirmed at `strategies/opening_session.py:372` — uses `t1=pivot_pp` which lands wrong side of entry. Fix: use R1/S1 or VWAP+ATR. Pending operator design decision. |
| **Bug B3: orb_fade 0 signals** | Root cause confirmed at `strategies/orb_fade.py:162` — wallclock freshness check (`time.time() - last_bar_ts > 90`) always rejects in backtest. **May affect live too** — needs one-line log check. Fix: compare against `market["now_ct"]`. |
| **Compounding backtest** | $1,500 → **$1.09M over 5y** using `tier_3000` policy (1 contract per $3k equity, 30-contract cap, size-scaled slippage). Max DD 34%. Engine: `tools/phoenix_compounding_backtest.py`. Stress-tested at 55% WR → still $856k. |
| **Strategy roster (post-Phase-13 target)** | 11 enabled winners, 4 killed (compression_v2, compression_micro, open_test_drive, noise_area). 3 NEW winners to add. Currently 16 strategies enabled in code; needs reconciliation. |
| Test suite | **2,110 pass / 19 skip / 0 fail** (last verified 01:25 CT — should re-run before Phase 13 ship). |
| Sim_bot | Still running on `dbba094` from 01:25 CT — NO Phase 13 changes loaded (research-only sprint). |

### Phase 13 next-session checklist

1. Implement D1 (per-strategy `exit_policy` config field) in `bots/base_bot.py`
2. Implement D2 (universal `skip_hours_ct: [10,11,12,13,14]` filter) in global risk config
3. Apply verdicts to `config/strategies.py` — kill list, exit policies, entry filters
4. Promote 3 new winners to production strategy classes (`strategies/inside_bar_breakout.py`, etc.)
5. Fix B2 (open_drive pivot) — pending operator design intent
6. Fix B3 (orb_fade freshness) — one-line fix
7. Re-run 5-year backtest with all Phase 13 changes applied
8. Run full test suite — must stay at 2,110+ pass / 0 fail
9. Restart sim_bot + monitor

### Operator decisions still needed (Section G of plan)
1. Demote `bias_momentum` out of PROD? (data says yes)
2. Close 5 dead NT8 sub-accounts? (frees $9,914 capital)
3. open_drive (B2) — continuation (R1/S1) or reversion (PP) design?
4. Footprint historical data — Databento MBO ($100-500/mo) or free snapshot recording?
5. Phase 13 ship target date?

---

## AT A GLANCE — 2026-05-18 01:25 CT (Monday pre-cash-open, ~7h to 08:30 CT)

| Item | State |
|---|---|
| Branch | `weekly-evolution/2026-05-17` (**16 commits unpushed**) |
| HEAD | `dbba094` (Phase 12C: ES/NQ Confluence LONG strategy) |
| Test suite | **2,110 pass / 19 skip / 0 fail** in 80s |
| Sim_bot freshness | Running ~62h on commit `853482e` (Phase 9.1). **Needs restart before 08:30 CT cash open** to pick up Phase 9.5 fixes + Incident #1 fix + Phase 12C. |
| Prod_bot | Untouched — runs known-good pre-V2 code by operator scope. Will re-evaluate prod after sim validates 1-2 sessions. |
| **Strategy roster** | **16 strategies** (15 from V2 deployment + 1 new Phase 12C). 14 enabled/validated, 1 enabled/sim-only (big_move_signal), 1 enabled/dormant pending MES feed (es_nq_confluence). |
| **NEW today: Phase 12C ES/NQ confluence** | 5-year Databento backtest: 131 trades, 50.4% WR, $1,548 / $11.82/trade, **PF 2.63, max DD $72, 6/6 years positive incl. 2022 bear +$1,032**. Currently DORMANT — needs MES feed wiring before it'll fire (see KNOWN_ISSUES). |
| **Phase 0 sim-testing overrides STILL ACTIVE** | DAILY_LOSS_LIMIT=$1M, PER_STRATEGY_DAILY_LOSS_CAP=$1M, MAX_ACTUAL_STOP_DOLLARS_PER_TRADE=$100, skip_on_stop_clamp=False on 3 strategies, validated=True on 7 strategies (operator override). **MUST RESTORE before live** — Phase 10 todo. |
| **Phase 9.5 + Incident #1 fixes committed but NOT loaded** | dupe_test halt cleanup, [EVAL] SKIP logging on 3 silent strategies, CNN F&G 24h cache, CLOSEPOSITION-vs-OCO race fix. All effective at next sim_bot restart. |
| Databento dataset | 5y MNQ + MES 1-min OHLCV (599MB raw + 385MB derived), gitignored. Regen via `tools/decompress_databento.py` + `databento_to_phoenix_v2.py`. |
| NT8 + TickStreamer | Live (MNQ only — **MES feed NOT yet wired**, blocks Phase 12C live firing). |
| Alerting | Self-healing — PhoenixWatcher 5-min auto-respawn pattern in place. |

### Resumption protocol for Monday 2026-05-18 08:30 CT cash open

1. **~08:00 CT** — verify sim_bot still alive. Confirm bridge healthy. Confirm COOLOFF cleared.
2. **~08:15 CT** — restart sim_bot (kill PIDs 76700/66988, relaunch). New process loads `dbba094` — picks up all Phase 9.5 fixes + Incident #1 fix + Phase 12C in one go.
3. **08:30-09:30 CT** — tail logs for V2 session-windowed strategies firing (`nq_lsr`, `orb_fade`, `orb_v2`) hitting their windows. Confirm 15+ strategies show [EVAL] activity within the first hour. Confirm `es_nq_confluence` logs DATA_NOT_AVAILABLE (expected — no MES feed).
4. **After 30 min of clean V2 fires + correct account routing** — push branch + declare Phase 9 production-validated. Phase 12C remains dormant pending MES feed (separate sprint).

---

## (Historical sections below — defer to AT A GLANCE above for current state)

## Pre-overnight: AT A GLANCE — 2026-05-15 08:34 CT (Friday, 4 min into cash open)

| Item | State |
|---|---|
| Branch | `weekly-evolution/2026-05-10` (pushed through `e07b2b8`) |
| HEAD | `84632c1` (VOLATILE day-classifier suppression removed for breakout strategies) |
| Test suite | **1,950 pass / 4 skip / 0 fail** |
| Today's P&L | (cash open just started 8:30 CT) |
| **Process freshness** | Bots restarted 08:44:04 / 08:44:06 CT on `84632c1`. Now 14 min into cash open with the VOLATILE-suppression bypass live. |
| **Known issue (not blocking)** | `footprint_cvd_reversal` is DATA_STALE — volumetric stream from NT8 frozen at yesterday 15:56 CT. Operator-side TickStreamer.cs reload needed (separate from today's fixes). Affects only this 1 strategy; other 9 are fully active. |
| ORB / ib_breakout status | Both armed to build correct windows: ORB from 09:30-09:44 ET, ib_breakout from 09:30-09:40 ET (`ib_minutes=10`). |
| Today's "make them fire" fixes | 6 commits: noise_area stop-sanity (`751172f`), ORB session-anchor (`751172f`+`f96135b`), compression_breakout instrumentation+relax (`ffd206d`), un-retires (`ffd206d`), 3-of-4 compression + OPEN_DRIVE Steidlmayer (`6af6ab3`), ib_breakout session-anchor (`e07b2b8`) |
| Dashboard panels | TODAY card + Daily Stats + trade table all on calendar-day boundary |
| Validated-set | **bias_momentum, spring_setup** only (unchanged) |
| Retired strategies | **high_precision_only** only — compression_breakout + opening_session UN-retired 2026-05-15 (sim, validated=False) |
| Stack health | bridge / dashboard / watchdog / watcher_agent / prod / sim — all alive, all on latest code |
| NT8 + TickStreamer | Live |
| Alerting | Self-healing — PhoenixWatcher 5-min auto-respawn pattern installed |
| Gemini AI | Working on fresh GCP project (new `GOOGLE_API_KEY`) |
| Live trading | PAUSED — prod stays Sim101 until real account ≥ $2,000 (currently $300) |

## Strategies and what to evaluate when operator returns

| Strategy | Pre-fix state | Today's fix | What to look for |
|---|---|---|---|
| `noise_area` | 11 silent rejections/day at 200t cap | Sanity gate now 5-1000t for managed exits | SIGNAL/INTENT events on band touches |
| `orb` | 1,086 evals/day, 0 SIGNAL — built OR from overnight chop | Anchored to 9:30 ET cash open + window upper bound | `OR_SET` log at ~09:44 ET, then 5m close breakouts → SIGNAL |
| `ib_breakout` | 3,472 `ib_too_wide` rejections — same anchor bug + 1.5× cap too tight | Anchored to 9:30 ET + cap 1.5→4.0× ATR | `IB_SET` log at ~09:40 ET |
| `compression_breakout` | 5,476 `squeeze_not_held` events — 4-of-4 AND never satisfied | 3-of-4 voting + relaxed ATR/range/min_bars + instrumentation | Watch `NOT_COMPRESSED` per-condition logs to see firing distribution; SIGNAL when 3 conditions align for 6+ evals |
| `opening_session.open_drive` | Never dispatched — classifier needed top 2% of range | Steidlmayer top/bottom third + vol 1.4→1.2 | OPEN_DRIVE classifications in log (≥1 per Mon-Fri morning expected) |
| `opening_session.open_auction_in/out` | 215 + 306 NO_SIGNAL events (DOES dispatch) | No code change — un-retired only | Watch dispatched-sub log frequency |

### Today's fixes (2026-05-15) — two silent-firing bugs unblocked

**Bug 1: noise_area dropped 11 signals/day at the universal stop-sanity gate**
- `_sanity_check_entry` capped stop distance at 5-200 ticks for ALL strategies
- noise_area is managed-exit; its stop is the opposite cone boundary (150-1000t by design)
- Yesterday: 11 sim noise_area signals all rejected with `stop distance 776t outside 5-200 range`
- Fix (commit `751172f`): sanity gate accepts `is_managed_exit: bool`. Managed mode = 5-1000t bound. Caller plumbs from `_managed_exit_target` + `signal.exit_trigger` + `uses_managed_exit` class flag.

**Bug 2: ORB anchored its daily reset to ET-midnight instead of 9:30 ET cash open**
- Strategy spec is Zarattini 9:30 ET — config comment said so, code anchored to ET calendar
- Yesterday's prod OR: high=29689, low=29295.75, **size=393.25pt** (5× the 80pt cap)
- 3,923 of 1,086 sim evals rejected as `gate:or_too_wide` (every single one)
- Fix (commits `751172f` + `f96135b`):
  - New `session_open_et` config (default "09:30")
  - `_session_open_today_et()` computes the current ET session-day
  - OR built from bars STRICTLY inside `[session_open_ts, session_open_ts + or_duration)` — both lower AND upper bound
  - Verified live at 07:53 CT: ORB emitted clean `SKIP warmup_incomplete (0/15 bars since 09:30 ET)` instead of building a phantom OR
- opening_session stays retired (different problem — too few signals at all, not a time-anchor bug)

---

## ROADMAP BATCH (2026-05-13 late-night, 21 items + 1 audit fix)

All shipped, tested (1,912 pass), committed. **Not yet pushed to origin** — operator review before push.

### Position infrastructure (foundation)
| # | Commit | What |
|---|---|---|
| #3 | `4d4e15d` | Anti-mutation invariant on R-distance — `_initial_stop_frozen` captured in `__post_init__` |
| #2 | `c14a3a1` | MAE/MFE/R-multiple tracking — persisted per closed trade |
| #4 | `56eaf3b` | Outlier-stripped P&L view in validation_tracker (`--exclude-outliers`) |

### Strategy lifecycle
| # | Commit | What |
|---|---|---|
| #5/#6 | `f0e6863` | Retired high_precision_only / opening_session / compression_breakout (formal `retired=True` markers) |
| #22 | `477e31d` | Wilson-CI promotion guardrail + demoted ib_breakout (had validated=True with n=8) |
| #7 | `878165b` | `config/regime_matrix.py` — typed loader for the operator-editable YAML |

### Stop / exit improvements
| # | Commit | What |
|---|---|---|
| #8 | `e6ad6da` | skip_on_stop_clamp wired into vwap_pullback + dom_pullback (was bias_momentum-only) |
| #1b | `4e75d82` | vwap_pullback stop_atr_mult 2.0 → 1.5 |
| #1c | `d76b8cb` | ema_dom_exit dynamic min_profit_ticks (max of static floor and 70% of target) |
| #18 | `32e823f` | BE arms on bar-close, not tick-touch (config-toggleable, default ON) |
| #15 | `30eb1f2` | vwap_band_pullback TF-vote gate 3 → 2 (config-driven) |
| #19 | `7edaf9b` | Explicit `flow_reversal` / cvd_flip / cvd_divergence priority in exit cascade (rank 5) |

### Instrumentation & tooling
| # | Commit | What |
|---|---|---|
| #14 | `e701973` | footprint_cvd_reversal emits `cvd_div_type` enum + `cvd_div_magnitude` in metadata/reason |
| #12 | `4219719` | docs/cvd_usage_audit.md — single source-of-truth for CVD usage across strategies |
| #13 | `52cede2` + `3ddf7a9` | ORB state persistence across bot restarts (+ audit caught session_start_ts gap → fixed) |
| #17 | `64c113a` | MAE-calibrated stop recommender framework (`tools/mae_stop_calibrator.py`) |
| #20 | `a951dc9` | `tools/strategy_change_log.py` — per-strategy commit timeline from git |
| #23 | `5a71566` | Tier-aware contract scaling in SimpleSizer (VALIDATED 1.5×, HIGH_CONFIDENCE 2.0×) |
| #25 | `6af0689` | `tools/strategy_correlation_audit.py` — Jaccard co-fire matrix per strategy pair |

### Audit catch (after the batch)
| Commit | What |
|---|---|
| `3ddf7a9` | ORB cutoff regression: post-restart `_or_bars_1m[0]` raised IndexError → silently passed max_entry_delay_min check. Fixed by persisting `_or_session_start_ts` separately. **Found by self-audit, not by tests** — illustrative of why the "review your own work" pass matters. |

---

## STRUCTURAL FIXES SHIPPED TODAY (2026-05-13)

All landed, tested, deployed in running processes, pushed to origin.

| Commit | Fix | Impact |
|---|---|---|
| `1d56862` | `session_debriefer.py` typing import fix | One-line correctness |
| `dda680c` | Graceful /shutdown via dashboard command queue | Replaces lost CTRL_BREAK_EVENT path |
| `4d523bf` | Dashboard `/api/today-pnl` reads per-bot trade_memory files | Fixed the user-visible $0 vs $114 discrepancy |
| `c9099d7` | **12-file trade_memory reader audit** — all readers via `load_all_trades()` | Closed silent data drift across all analytical tools |
| `4e29ce5` + `d7e081a` | RiskManager hydrates daily counters from disk + filters by bot_id | Daily Stats panel survives restarts; correct per-bot attribution |
| `2b59342` | `tools/diagnose_vwap_pullback.py` + CURRENT_STATE refresh | Surfaced the vwap_pullback bleed structurally |
| `1e07000` | **Prod trading-window gate REMOVED** | Prod now evaluates 24/7 (was 3.5h/day with silent skips) |
| `c209202` | Sprint M Tier 1 C# side live end-to-end (operator F5 + chart reload) | `imbalance_ratio` field now flowing in volumetric_bar messages |
| `4f171e5` | Memory refresh — scannable AT-A-GLANCE + current OPEN_QUESTIONS | Doc readability |
| `0c24a8e` | `/api/today-pnl` switched to calendar-day boundary (was Globex 17:00) | Dashboard TODAY card + Daily Stats panel now agree all 24h instead of disagreeing 17:00-midnight |

---

## OPERATIONAL CHANGES YOU SHOULD KNOW

### Prod_bot is now 24/7 (commit `1e07000`)

Pre-today: prod only evaluated strategies during 08:30-11:00 + 13:00-14:30 CST. Outside those windows, the eval function silently returned. **2026-05-13 incident**: NT8 internet outage 08:30-11:09 meant prod missed its entire primary window. Sim caught 4 wins, prod caught 0. Operator confusion: "why isn't prod trading?" — bot looked healthy but was silently skipping.

Now: prod evaluates all 10 strategies on every bar close, all hours, same as sim. Risk limits ($5/trade, $15/day, 4 trades/day in SimpleSizer) are the actual constraints. Strategy-level time windows (e.g. ORB 08:30-14:30) still apply as intentional per-strategy filters.

### Trade memory has ONE canonical reader (commit `c9099d7`)

`core.trade_memory.load_all_trades(logs_dir=...)` merges the legacy `logs/trade_memory.json` (frozen 2026-05-12, holds pre-split history) with every per-bot `logs/trade_memory_<bot>.json` file. Dedupes by `trade_id`, per-bot files win on collision.

**Never raw-open trade_memory.json anywhere.** Today's audit fixed 12 such call sites. Future tools must route through this function (see auto-memory entry `trade_memory_canonical_reader.md`).

### Alerting is now self-healing (PhoenixWatcher Repetition pattern)

Scheduled task `PhoenixWatcher` has `Repetition: PT5M` on its `AtLogOn` trigger. If `watcher_agent.py` ever dies (Ctrl+C, crash, logoff), Windows tries to relaunch it every 5 minutes. Max alerting downtime is ≤ 5 min. Was previously a silent failure mode (AtLogOn fires once, daemon dies, no respawn until next logon — bit us this morning).

### Sprint M Tier 1 fully live (Python `a4ab967` + C# `c209202`)

- Python side deployed since 2026-05-12 (commit `a4ab967`)
- C# `TickStreamer.cs` recompiled today via operator F5 — DLL mtime 16:42
- Volumetric chart configured 1500-tick (operator action this evening)
- New `imbalance_ratio` field confirmed in `data/volumetric_latest.json` at 17:01
- No further operator action required unless `TickStreamer.cs` source changes again

---

## OPEN / DEFERRED — see linked files for detail

| Item | Status | Where |
|---|---|---|
| NT8 silent-stall (106s reconnect cycle) | 🟠 Open, recurring | [KNOWN_ISSUES.md](KNOWN_ISSUES.md) |
| vwap_pullback bleed (65% WR, net -$169) | 🔴 Decision pending | [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) #1 |
| Live trading flip | Gated at $2,000 acct | [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) #2 |
| Sprint M Tier 2 (2.1 / 2.2 / 2.4) | Scheduled 2026-05-19 | `SPRINT_M_TIER_2_SCHEDULED.md` |
| CPCV / DSR / PBO validation harness | Phase C dependency | [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) deferred |

---

## TOMORROW MORNING CHECK (run these before market open)

```powershell
# 1. Bot stack alive?
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? CommandLine -match '_bot\.py|bridge_server|dashboard|watchdog|watcher_agent' | Select Id, CreationDate, @{N='Cmd';E={if($_.CommandLine -match '([\w_]+\.py)'){$matches[1]}}}

# 2. NT8 streaming ticks?
(Invoke-WebRequest 'http://127.0.0.1:8767/health' -UseBasicParsing).Content | ConvertFrom-Json | Select nt8_status, tick_rate_10s, nt8_last_tick_age_s

# 3. Dashboard healthy?
(Invoke-WebRequest 'http://127.0.0.1:5000/api/today-pnl' -UseBasicParsing).Content | ConvertFrom-Json

# 4. Watcher_agent running (alerting alive)?
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? CommandLine -match 'watcher_agent' | Select Id
# Empty result = alerting offline. Start manually: `Start-ScheduledTask -TaskName PhoenixWatcher`
```

Expected first hour:
- Prod_bot should now start producing `[strategies.*]` log lines from the very first bar close (the window gate is gone)
- Daily Stats panel should show prod's `daily_pnl` populated from previous day's tail trades, then update with today's
- Daily 16:05 Telegram debrief should report actual trade counts (was previously broken — fixed in `c9099d7`)

---

## HISTORICAL SPRINT CONTEXT (May 4 onwards)
_Below is the previous "current state" snapshot from 2026-05-04. Retained
for sprint-by-sprint context — useful background on strategy designs,
infrastructure decisions, and incident playbooks. Operational truth has
moved on; defer to the section above and to RECENT_CHANGES.md for
anything time-sensitive._

## Sprint H v3 — Footprint + CVD Reversal w/ IQS (2026-05-04)

New strategy `footprint_cvd_reversal`. Institutional 4-confluence
reversal at MenthorQ HTF levels with composite Institutional Quality
Score (IQS, 0-100). Lab-only (`validated=False`) until 50+ trades + PF > 1.3.

### Why v3 vs v2

v2 was halted by CC at Phase 1 — discovered 4 spec mismatches with
Phoenix conventions. v3 ships Option A (match Phoenix conventions
exactly):
- BaseStrategy subclass instead of free-function evaluate()
- Real Signal constructor (8 required fields + atr_stop_override)
- Real MenthorQ attribute names: `put_support` / `put_support_0dte` /
  `call_resistance` / `call_resistance_0dte` / `hvl` / `hvl_0dte`
  on the GammaLevels dataclass instance accessed via getattr (NOT
  the `_all`-suffixed dict from MenthorQSnapshot which is only used
  for AI-prompt context)
- VP POC via `market["prior_day_poc"]` (the real key)
- Async bridge handler integrated into the existing TCP message router

### Tick chart decision

Single 1,500-tick volumetric stream (per emini-watch + NT8-forum
research). Rejected 250 (too noisy at NQ open), 750 (still noisy),
2,250 (effectively dormant at lunch), and 4,500 (HTF context already
covered by MenthorQ + Net GEX). Single-stream chosen over multi-stream
to avoid documented NT8 "thin liquidity + high volatility" issues.

### IQS scoring system

Each confluence contributes 0-25 pts; composite IQS 0-100.

- **HTF level (max 25)**: 25pts MenthorQ confluence; 15pts VP POC fallback
- **CVD divergence (max 25)**: multi-bar regular + single-bar delta
- **Footprint (max 25)**: stacked / absorption / oversized (>=10x ratio)
- **CVD compression (max 25)**: 5 sub-dimensions x 5pts each
  - Delta magnitude shrinking (< 0.6x 20-bar baseline)
  - Bar range shrinking (< 0.6x baseline)
  - **Volume holding/elevated (>= 0.8x baseline)** — KEY check;
    distinguishes absorption (low delta + low range + normal volume)
    from dead market (low everything)
  - Effort/result spike (> 1.5x baseline)
  - Single-bar delta divergence

Entry threshold: IQS >= 70.
Tier (in `metadata['tier']`): A++ >= 90, A >= 80, B >= 70, C >= 60.

### Operator action items (in order)

1. **NT8 chart setup**: add 1,500-tick volumetric chart on MNQ
   - Bar type: Volumetric (Order Flow+)
   - Base period: 1,500 ticks
   - Ticks per level: 1 (finest granularity)
   - Delta type: BidAsk (UpDownTick fallback if Last-only)
   - Imbalance ratio: 3.0
   - Stacked threshold: 3
2. **Implement TickStreamer.cs volumetric emitter** per Sprint H v3
   Phase 2a spec — emit `type:"volumetric_bar"` typed messages on
   each bar close
3. Recompile NinjaScript in NT8
4. Verify `data/volumetric_latest.json` updates each bar:
   `cat data/volumetric_latest.json`
5. Verify `SimFootprintchart` account exists in NT8 control center
   (signals route to it but get dropped if it doesn't exist)
6. Restart sim_bot to pick up the new strategy
7. Watch `[FOOTPRINT_CVD]` logs for IQS scoring of every evaluation
8. After ~50 trades:
   ```
   python tools/validation_tracker.py --strategy footprint_cvd_reversal --post-b13-only
   python tools/indicator_audit.py --strategy footprint_cvd_reversal
   ```
9. Promote to `validated=True` only if PF > 1.3 + WR > 50% + sample
   tier reaches TENTATIVE (n >= 100)

### Tuning knobs (in `config/strategies.py`)

- `entry_threshold_iqs` (default 70): raise for pickier; lower for more signals
- `compression_size_threshold` (default 0.6): lower for stricter compression detection
- `compression_volume_floor` (default 0.8): higher for stricter dead-market filter
- `divergence_lookback_bars` (default 10): lower to catch faster reversals

### Until TickStreamer.cs ships

Strategy stays dormant. `[FOOTPRINT_CVD] DATA_NOT_AVAILABLE` is logged
**once** per session run (not per evaluation) so the log isn't spammed.

---

## ✅ Saturday EOD — full operational state

**The entire Phoenix automation lattice is now live and verified:**

| Layer | Status | Evidence |
|---|---|---|
| Bridge `:8765` single-stream enforcement | ✅ ON | `PHOENIX_BRIDGE_SINGLE_STREAM=1`, 3 unit tests + live spy verification |
| Bridge `:8765` peer-MAD validator | ✅ ON | `PHOENIX_STREAM_VALIDATOR=1` (was 0 all weekend until today) |
| Multi-account close isolation | ✅ Tested | 7 unit tests; closes only target account, never cross-cancels |
| Twilio SMS escalation | ✅ E2E verified | `sid=SMba9bbf84b5866fdefa0ae9587b898aa0` delivered to phone |
| Telegram alerts | ✅ Live | Watcher logged `[Alerter] Telegram ready` |
| Gemini AI investigator | ✅ Live | Watcher logged `[Investigator] Gemini client ready` |
| 11 scheduled tasks under `TradingPC\Trading PC` | ✅ Registered | See task table below |
| 7 Phoenix processes | ✅ Running | prod_bot, sim_bot, bridge, dashboard, watcher, finnhub, fred |
| Test suite | ✅ Green | 1,231 passing, 4 skipped, 0 failing |
| Repo | ✅ Pushed | `5cf0d3d` on `origin/main` |

## Scheduled task lattice (final state, all under Trading PC user)

| Task | Trigger | Currently | Notes |
|---|---|---|---|
| `PhoenixBoot` | AtLogOn | Ready | Auto-launches stack via PhoenixStart.bat at logon (was broken with dbren Principal until today) |
| `PhoenixWatcher` | AtLogOn (daemon) | **Running** | SMS/Telegram escalation, 3-strike rule, NT8 SILENT_STALL detection |
| `PhoenixFinnhubNews` | AtLogOn (daemon) | **Running** | News feed; WS connected (free-tier limited, REST fallback active) |
| `PhoenixFredMacros` | AtLogOn (daemon, --interval-min 60) | **Running** | FFR/CPI/UNRATE/T10Y2Y poller, Telegram on regime shift |
| `PhoenixGrading` | 16:00 CT Mon-Fri | Ready | Daily prediction grader |
| `PhoenixMorningRitual` | 06:30 CT Mon-Fri | Ready | Pre-market 7-check, deterministic verdict |
| `PhoenixPostSessionDebrief` | 16:05 CT Mon-Fri | Ready | Consolidated digest (Telegram) |
| `PhoenixWeeklyEvolution` | Sun 18:00 CT | Ready | Auto-PR with adaptive params + CPCV/DSR/PBO checkboxes |
| `PhoenixRiskGate` | AtBoot/AtLogOn | Ready | Fail-closed gate (gated by `PHOENIX_RISK_GATE=0`, off by default) |
| `PhoenixRiskWatchdog` | AtBoot/AtLogOn | Ready | Heartbeat watchdog for risk gate |
| `PhoenixLearner` | 23:30 CT daily | Ready | Historical learner (only one that survived all reboots — was correctly principled originally) |

## Saturday afternoon root-cause + cleanup summary

After the morning Phase B+ Sprint 2 work, the afternoon was a 4-hour
incident response on the dual-stream pollution bug. Final root cause
turned out to be 3 layered failures (see KNOWN_ISSUES.md for full
playbook):

1. Two legacy NT8 source files were still compiled in `NinjaTrader.Custom.dll`:
   - `Indicators\JenTradingBotV1DataFeed.cs` (V1-era WS indicator)
   - `Strategies\OLDDONTUSEMarketDataBroadcasterv2.cs` (V2-era strategy)
2. NT8 `<ShowDefaultWorkspaces>true</ShowDefaultWorkspaces>` auto-loaded a
   workspace with **9 hidden MNQM6 charts** (`IsWindowVisible=false`,
   invisible in taskbar, no Window menu in this NT8 version)
3. `PHOENIX_STREAM_VALIDATOR=0` (default) — bridge defense was off

**Defense layers added today (preventing recurrence):**

- `bridge/bridge_server.py::handle_nt8_tcp` — rejects 2nd+ NT8 connection
  at socket-accept layer (`PHOENIX_BRIDGE_SINGLE_STREAM=1`)
- `tools/nt8_unhide_all_windows.ps1` — Win32 EnumWindows + ShowWindow
  to surface hidden NT8 chart windows; required because newer NT8
  builds have no Window menu
- `tools/diagnose_nt8_client.py` — spy bot that classifies the
  connected NT8 client by message-shape fingerprint
- `tools/_patch_register_scripts.py` — idempotent helper that
  retroactively fixed all 8 register scripts to use `$TaskUser`
  (`TradingPC\Trading PC`) instead of `$env:USERDOMAIN\$env:USERNAME`

## .env state (relevant flags only — secrets redacted)

```
PHOENIX_STREAM_VALIDATOR=1          # peer-MAD validator (NEW today)
PHOENIX_BRIDGE_SINGLE_STREAM=1      # socket-accept reject (NEW today)
SENTIMENT_FLOW_ACTIVE=false         # FinBERT voter (deferred)
SENTIMENT_FLOW_WEIGHT=0.10          # ignored while ACTIVE=false
LIVE_ACCOUNT=1590711                # B59 hard-guard target
SIM_ACCOUNT=Sim101                  # prod_bot default routing
```

Twilio + Telegram + Gemini + Google + Finnhub + FRED + Anthropic +
OpenAI + Groq + Grok + Alpaca + MenthorQ keys all populated and
verified loadable by `dotenv` (after a brief mid-afternoon
.env corruption incident — em-dash byte 0x97 from PowerShell
`Add-Content`, fixed in-place by re-encoding as ASCII/UTF-8).

## Operator runbook (next session)

**Sunday 17:00 CT — market reopen first test:**
1. NT8 reconnects → bridge log shows ONE `NT8 client connected from`
2. `(Get-NetTCPConnection -LocalPort 8765 -State Established).Count` = 1
3. Stream validator silently approves real ticks
4. Watcher's "bridge_down" false-alarm clears

**Monday 06:30 CT — first scheduled-task fire:**
1. `PhoenixMorningRitual` → `out/morning_ritual/2026-04-27.md`
2. Order round-trip test: `python tools/verify_oif_fix.py`
3. `PhoenixGrading` 16:00 CT → `PhoenixPostSessionDebrief` 16:05 CT
   consolidated Telegram

**Deferred (not blocking):**
- `PHOENIX_RISK_GATE=1` — flip when ready to intercept every OIF
- `SENTIMENT_FLOW_ACTIVE=true` — flip after shadow data validates
- CPCV/DSR/PBO harness — Phase C dependency



## ⚠️ Today's NT8 dual-stream incident — RESOLVED (this afternoon)

**Symptom:** bridge `:8765` had 2+ established TCP connections all weekend.
PriceSanity logged 27,000+ tick rejections over Friday→Saturday with corrupt
~7,196-class prices alongside real ~27,440 MNQ prices ("phantom $40K trade"
signature).

**Real root cause** (turned out to be 3 layered issues, not the simple
"single TickStreamer dupe" we thought):

1. **Two legacy NT8 source files were still installed and compiled:**
   - `Indicators\JenTradingBotV1DataFeed.cs` — V1-era WebSocket indicator
     with `IsSuspendedWhileInactive=false`, broadcasting synthetic
     mom/prec/conf fields plus a secondary data series whose price scale
     was the source of the corrupt 7,196 stream.
   - `Strategies\OLDDONTUSEMarketDataBroadcasterv2.cs` — V2-era WebSocket
     strategy, also targeting `:8765`, with its own ATI write path.
2. **NT8 auto-loaded a bloated workspace via
   `<ShowDefaultWorkspaces>true</ShowDefaultWorkspaces>`** — `Jen's Fav.xml`
   and/or `Jen's indicators.xml` brought up **9 hidden MNQM6 charts** plus
   ESM6/AUDUSD/SuperDOM windows (`IsWindowVisible=false`). Charts were
   alive in NT8 memory holding TickStreamer instances + TCP connections,
   but invisible — not in taskbar, no Window menu in this NT8 build to
   reveal them.
3. **The system was operating one PriceSanity edge case away from a real
   loss the entire weekend.** PriceSanity caught all corrupt ticks at the
   bot level; the OIF builders' price-sanity guard would have caught any
   that slipped past. But `PHOENIX_STREAM_VALIDATOR=0` meant the bridge-
   level defense built specifically for this scenario was off.

**Cleanup playbook (now embedded in `tools/nt8_unhide_all_windows.ps1`):**

1. Move both legacy `.cs` files to `.disabled_2026_04_25` quarantine.
2. Run `tools/nt8_unhide_all_windows.ps1` from elevated PS — uses
   Win32 `EnumWindows` + `ShowWindow(SW_SHOWNORMAL)` to surface every
   hidden NT8-owned window. Without this you cannot find the 9 ghost
   charts.
3. Manually close every chart not needed; keep one MNQM6 with
   TickStreamer attached.
4. Workspaces → Save As → `phoenix_clean_2026_04_25` (clean baseline).
5. Tools → Options → General → uncheck "Show default workspaces on
   startup" — prevents recurrence.
6. NT8 NinjaScript Editor → F5 to recompile (purges legacy classes
   from cached `NinjaTrader.Custom.dll`).
7. Full NT8 restart. Verify: with NT8 running but no chart open,
   `(Get-NetTCPConnection -LocalPort 8765 -State Established).Count`
   must return 0. Open one chart → count = 1. Close chart → back to 0.
8. **Set `PHOENIX_STREAM_VALIDATOR=1` in `.env`** — bridge-level
   defense for any future workspace pollution.

**Diagnostic insight that broke the case:** `nt8_last_heartbeat_age_s ≈ 2.8`
on bridge health endpoint matched **TickStreamer's `HEARTBEAT_MS=3000`**
timer exactly. Legacy V2 strategy uses `HEARTBEAT_BARS=30` (bars, not
milliseconds — silent on a closed Saturday market). That fingerprint
proved the connecting client was TickStreamer, but a Win32 window
enumeration revealed it was attached to one of nine hidden charts.

**Status as of 15:30 CDT:** Jennifer completed cleanup. NT8 is currently
closed; bridge confirms `nt8_status: disconnected`, 0 connections on
`:8765`. Next NT8 startup should bring 0 connections (until a chart is
manually opened).

## Bot operational state (as of Saturday 2026-04-25 EOD)

- **Prod bot:** UP, flat, Sim101 account (LIVE_TRADING=False). PID and uptime
  re-stabilized after the 14:31 CDT reboot (TeamViewer-initiated).
- **Sim bot:** UP, Phase C live-sim execution on 16 dedicated NT8 Sim
  accounts. 24/7 trading, per-strategy risk isolation, real OIF writes,
  10 strategies loaded.
- **Lab bot:** **DECOMMISSIONED** — paper-only flow ended 2026-04-21.
  `bots/lab_bot.py` preserved on disk as rollback safety net only.
- **Bridge:** UP on :8765 (NT8) + :8766 (bots) + :8767 (health)
- **Dashboard:** UP on :5000, with per-strategy risk panel, Grades tab,
  Logs tab, and (new today) sentiment-flow surface.
- **Watchdog:** UP, tracks prod + sim
- **NT8:** live, MNQM6, single client confirmed (multi-stream issue
  resolved per Sunday 2026-04-19 diagnostic).

## Today's sprint summary (2026-04-25)

A two-phase Saturday rebuild day. **All work shipped to `origin/main` at
commit `c2dcdc8`.** Defaults remain SAFE (`PHOENIX_RISK_GATE=0`,
`PHOENIX_STREAM_VALIDATOR=0`, `SENTIMENT_FLOW_ACTIVE=false`).

### Phase B+ skeleton sprint (morning)

Six items shipped behind off-by-default flags:

1. **NT8 stream validator** — `core/bridge/stream_validator.py` +
   `tools/nt8_stream_quarantine.py`. Price-band / peer-MAD / tick-grid
   checks. Gated by `PHOENIX_STREAM_VALIDATOR=1`.
2. **Fail-closed risk gate** — `core/risk/risk_gate.py` +
   `tools/risk_gate_runner.py` + `tools/watchdog_runner.py`. Named-pipe
   gate (`\\.\pipe\phoenix_risk_gate`), OIFSink shim, atomic OIF writer,
   heartbeat watchdog. Gated by `PHOENIX_RISK_GATE=1`.
3. **FinBERT sentiment skeleton** — `core/sentiment_finbert.py` +
   `agents/sentiment_flow_agent.py`. **Real INT8 ONNX model now installed
   under `models/finbert_onnx_int8/`** (downloaded via optimum-cli;
   gitignored). Council voter wired at `DEFAULT_WEIGHT = 0.0`.
4. **Chicago VPS migration plan** — `docs/archive/chicago_vps_migration_plan.md`
   + `tools/verify_jsonl_continuity.py`. **STRICKEN per Jennifer
   2026-04-25** — Phoenix stays on the Trading PC. Doc preserved for
   reference; no infra moves planned.
5. **SKILLS auto-digest** — `tools/skills_digest.py` generates
   `SKILLS.md` and is wired into the SessionStart hook.
6. **Dashboard Grades + Logs tabs** — surface `tools/grade_open_predictions.py`
   output directly in the Flask dashboard.

### Phase B+ "remaining §3" sprint (afternoon → evening)

After greenlight on §2.2 / §2.3 / §3.5 / §3.6 / §4.1 / §4.3 / §4.4:

1. **§2.2 FRED macros** — real `core/fred_client.py` with regime-shift
   detection on FFR / CPI / UNRATE / T10Y2Y; cached at
   `data/cache/fred/`.
2. **§2.3 Finnhub real client** — REST + WebSocket dual-path with token
   bucket; key already present in `.env`.
3. **§3.1 TradingView webhook** — **STRICKEN** (Premium $59.95/mo not
   worth the cost). HMAC-SHA256 webhook scaffolding removed from active
   roadmap; placeholder code retained but not imported anywhere.
4. **§3.4 Phoenix-specific skills** — **DEFERRED**. `.claude/skills/`
   directory created (empty), allowlisted in `.gitignore`, future-ready.
5. **§3.5 OIF kill-switch** — `tools/oif_kill_switch.py`. One-command
   manual halt: writes `outgoing/halt_all.json`, prod_bot watches for
   it on every cycle, refuses new entries until cleared.
6. **§3.6 Phoenix Routines** — three deterministic routines:
   - `tools/routines/morning_ritual.py` (06:30 CT, Mon-Fri)
   - `tools/routines/post_session_debrief.py` (16:05 CT, Mon-Fri,
     chains PhoenixGrading at 16:00)
   - `tools/routines/weekly_evolution.py` (Sun 18:00 CT)
   - Shared `tools/routines/_shared.py` with: `RoutineReport`
     (verdict-deterministic), `DigestQueue` (file-backed FIFO at
     `out/digest_queue.jsonl`), AI wrappers (`call_claude` /
     `call_gemini` fail-soft), Telegram dispatch
     (`send_telegram_now` for RED, `send_consolidated_digest` for
     EOD), PDF assembly via reportlab, `stack_health_snapshot()`.
   - **Three Jennifer amendments locked in:**
     - Verdict is computed from deterministic checks ONLY; AI commentary
       is appendix and CANNOT influence GREEN/AMBER/RED.
     - Every weekly_evolution commit body MUST include CPCV / DSR / PBO
       checkboxes with status "NOT YET RUN (Phase C dependency)".
     - All routine output is queued to `out/digest_queue.jsonl` and
       drained as ONE consolidated Telegram at 16:05 (only RED-verdict
       items fire an immediate interrupting Telegram).
7. **§4.1 / 4.3 / 4.4 Strategy fixes (A-F)** — locked-in regression
   tests committed at `tests/test_lock_in_epic_v1/` (20 new tests):
   - ORB ATR-adaptive stops
   - bias_momentum SHORT mirror + VCR=1.2 threshold
   - noise_area silent cadence + band_mult=0.7
   - ib_breakout 10-minute window
   - compression min_squeeze_bars=12
   - spring_setup retired

### Tooling, infra, plugins

- **Plugins installed:** machine-learning-ops, incident-response,
  pyright-lsp, document-skills, example-skills (via `claude plugin
  install` CLI subcommand). Total: **10 plugins / 72 skills indexed**.
- **`.gitignore` hardened:** broad ignores for venv/.venv-ml/models/
  out/.claude/ etc. with allowlist patterns for
  `phoenix_bot/orchestrator/`, `.claude/commands/`, `.claude/skills/`,
  `.claude/agents/`, `.claude/settings.json`, `out/baselines/`. Re-ignore
  patterns to stop bytecode caches sneaking in via greedy `**` allowlists.
- **GitHub auth fixed** — Statechamp76 → dans-favorite-chick mapping
  via `gh auth logout` + `gh auth login`. Push to `origin/main`
  succeeded at commit `c2dcdc8`.

## Test count

- **Before today:** 989 (Friday EOD 2026-04-24)
- **After Phase B+ skeleton sprint:** 1,081
- **After Routines + remaining §3 sprint:** **1,221 passing / 0 failing**

## Scheduled task state (2026-04-25 EOD)

After the 14:31 reboot, currently registered:

| Task | Schedule | Status |
|---|---|---|
| `PhoenixLearner` | 23:30 CT daily | ✅ REGISTERED (survived reboot) |
| `PhoenixGrading` | 16:00 CT Mon-Fri | ⏳ Script ready: `scripts/register_phoenix_grading_task.ps1` |
| `PhoenixRiskGate` | on-boot | ⏳ Script ready: `scripts/register_risk_gate_task.ps1` |
| `PhoenixMorningRitual` | 06:30 CT Mon-Fri | ⏳ Script ready: `scripts/register_morning_ritual_task.ps1` |
| `PhoenixPostSessionDebrief` | 16:05 CT Mon-Fri | ⏳ Script ready: `scripts/register_post_session_debrief_task.ps1` |
| `PhoenixWeeklyEvolution` | Sun 18:00 CT | ⏳ Script ready: `scripts/register_weekly_evolution_task.ps1` |
| `PhoenixWatcher` | AtLogOn (daemon) | ⏳ Script ready: `scripts/register_watcher_task.ps1` (added 2026-04-25 ~16:30 CDT — escalates RED_ALERT to Twilio SMS + Telegram; runs watcher_agent.py continuously with auto-restart) |
| `PhoenixFinnhubNews` | AtLogOn (daemon) | ⏳ Script ready: `scripts/register_finnhub_news_task.ps1` (added 2026-04-25 ~16:50 CDT — Finnhub WS+REST news feed, persists to logs/finnhub_news.jsonl) |
| `PhoenixFredMacros` | AtLogOn (daemon, --interval-min 60) | ⏳ Script ready: `scripts/register_fred_macros_task.ps1` (added 2026-04-25 ~16:50 CDT — FFR/CPI/UNRATE/T10Y2Y poller, regime-shift Telegram alerts) |

### `.env` flags flipped 2026-04-25 ~16:50 CDT (defense-in-depth)

- **`PHOENIX_STREAM_VALIDATOR=1`** — peer-MAD price-band validation at the
  bridge layer. Built specifically for today's dual-stream incident class.
- **`PHOENIX_BRIDGE_SINGLE_STREAM=1`** — explicit (default already 1)
  rejecting any 2nd+ NT8 connection at socket-accept.

**ACTION:** Re-run the four `register_*.ps1` scripts as Administrator
to restore the full schedule. Each script is idempotent (replaces any
existing task of the same name). The em-dash / schtasks / python-alias
issues are all fixed in the current versions.

## AI agent live status

- **Gemini (Council, Pre-Trade Filter):** ACTIVE.
- **Claude (Session Debriefer, Historical Learner):** ACTIVE — the
  ANTHROPIC_API_KEY issue was resolved 2026-04-21 via commit `eac5ae4`
  (`load_dotenv override=True`). The 108-char key has been verified
  end-to-end.

## Account state

- **Real live account balance:** $300 (small_account_mode active)
- **Live trading status:** PAUSED — prod stays Sim101 until account
  reaches $2,000.
- **Sim bot:** $2,000 × 16 strategies = $32,000 virtual pool.

## Ground rules locked in this weekend

- Phoenix stays on the Trading PC. No VPS migration.
- TradingView Premium not approved. Stricken from roadmap.
- Per-strategy risk isolation is the unit of accounting.
- AI is **advisory only** — every routine verdict is deterministic.
- Telegram is the ONLY runtime alert channel (Twilio SMS is escalation
  only, behind WatcherAgent).

## Repository state

- Branch: `main`
- HEAD: `c2dcdc8` (chore(gitignore): re-ignore __pycache__ in
  allowlisted dirs; drop stale .pyc; add phoenix_bot/__init__.py)
- Working tree: **clean**
- Pushed to `origin/main`: ✅
- 10 plugins installed via `claude plugin install`
- 72 skills indexed via `tools/skills_digest.py` → `SKILLS.md`

## Immediate to-dos for the next session

1. Re-run all five `register_*.ps1` scripts as Administrator to restore
   the scheduled task lattice after the 14:31 reboot.
2. Verify `PhoenixMorningRitual` fires Monday 2026-04-27 at 06:30 CT —
   look for `out/morning_ritual/2026-04-27.md` + a non-RED verdict.
3. Verify `PhoenixPostSessionDebrief` consolidated digest arrives on
   Telegram Monday 16:05 CT containing the morning_ritual snippet.
4. First floor-kill test still pending — manually trigger a strategy
   to -$500 cumulative to validate halt + persistence + Telegram alert
   path.
5. CPCV / DSR / PBO validation harness implementation when Phase C
   data depth allows (currently the weekly_evolution checkboxes read
   "NOT YET RUN").

## Sprint C — Observability + Validation Hardening (2026-05-03)

Six commits shipped on top of Sprint A:

| Commit  | Tool / change |
|---------|---------------|
| `c78d6f6` | feat(b13-stats): backfill quality assessment |
| `e56c522` | fix(dashboard): pre/post-B13 backwards-compat |
| `2d216a1` | feat(daily): tools/daily_session_summary.py |
| `05d2af4` | feat(validation): tools/validation_tracker.py |
| `f3d73d3` | feat(halt-verify): tools/verify_halt_signatures.py |
| (this)    | docs: update CURRENT_STATE + CLAUDE.md |

### New tools (all read-only, all produce markdown reports in out/)

- `python tools/backfill_commissions.py` — historical net-P&L recompute
  + baseline quality flags. Run once, then again any time
  trade_memory.json grows significantly. Output:
  `out/historical_pnl_recompute_<today>.md`.
- `python tools/daily_session_summary.py [--date YYYY-MM-DD] [--bot sim|prod|both]`
  — daily heartbeat. Run after every session. Includes 7-day-baseline
  anomaly detection (silent strategies, signal volume drops). Output:
  `out/daily_summary_<today>.md`.
- `python tools/validation_tracker.py [--since YYYY-MM-DD] [--post-b13-only]`
  — statistical-tier decision support. Wilson 95% CI on WR. Run weekly.
  Output: `out/validation_status_<today>.md`.
- `python tools/verify_halt_signatures.py` — synthetic halt path
  end-to-end test. Run after any risk_manager / registry change.
  Output: `out/halt_verify_<today>.md`.

### Statistical tier reference (key insight)

Phoenix's project 50-trade graduation gate is **PRELIMINARY** by
published research standards:

| Tier | Trades | Confidence |
|---|---:|---:|
| INSUFFICIENT_SAMPLE | < 30 | none |
| PRELIMINARY | 30–99 | ~70% |
| TENTATIVE | 100–384 | ~90% |
| VALIDATED | 385–665 | ~95% |
| HIGH_CONFIDENCE | 666+ | ~99% |

`validation_tracker.py` surfaces this in the Decision column —
GRADUATE / SCALE recommendations require TENTATIVE tier (n ≥ 100)
at minimum, KILL_CANDIDATE only fires at PF < 0.7 with n ≥ 30.

### Live findings from first Sprint C run

Backfill (1,105 trades, 20-day span):
- Avg `|gross|/trade` = $108.45 with 1.00 contracts/trade — strongly
  suggests legacy NQ data has been mixed into the baseline (1 NQ tick
  = $5 vs 1 MNQ tick = $0.50). **Use `--post-b13-only` for clean
  validation comparisons** once enough post-B13 trades accumulate.
- `_reconciled` strategy n=3, net=−$80,216 — manual cleanup outliers,
  not real strategy trades. Excluded from validation comparisons.

Validation tracker:
- `high_precision_only` n=557 VALIDATED tier: WR 29% (CI 25–33%) — at
  KILL_CANDIDATE threshold.
- `bias_momentum` n=225 TENTATIVE: WR 28% (22–34%) — Sprint A's gate
  fixes (skip_on_stop_clamp, rsi_div_hard_gate, trend_stall_grace_s)
  should improve this on go-forward data; track via `--post-b13-only`.

Halt verification: ALL 4 SIGNATURES PASS — Sprint A's Fix E logging
reaches the production call chain end-to-end.

### Daily / weekly workflow during validation window

```
Morning (pre-session, optional):
  python tools/validation_tracker.py --post-b13-only

After session close:
  python tools/daily_session_summary.py
  # Read out/daily_summary_<today>.md
  # If anomalies present -> investigate before next session

Weekly:
  python tools/backfill_commissions.py     # refresh historical baseline
  python tools/verify_halt_signatures.py   # re-confirm halt paths intact
  python tools/validation_tracker.py --post-b13-only
```

### Test suite delta

Sprint A baseline: 1,295 passing
Sprint C final:    1,339 passing (+44)
0 failing, 4 skipped throughout.

## Sprint D — Stuck-exit fixes + alert noise reduction (2026-05-04)

Six commits over the morning:
- `e6129d8` fix(stuck-position): bulletproof auto-retry flatten + OIF guard
- `9c3e74b` fix(stuck-position): cover-action uses NT8 direction, not Python
- `2a7aae6` fix(exits): defensive observability + reconciliation tools
- `00e5a68` fix(alerts): EXIT_TIMEOUT one-shot + hourly rollup + RESOLVED
- `c7486c7` fix(alerts): RECOVERY MODE one-shot per session day
- `7b5144e` fix(alerts): watchdog 60s disconnect grace + restart threshold
- `1c39b8b` feat(alerts): low-priority alert digest (4h or 10-msg flush)

Estimated alert noise reduction: ~30/day → ~5-7 actionable/day.

## Sprint E — Daily roadmap calibration + indicator audit (2026-05-04)

Two commits:
- `9cf9b70` feat(quin): tools/quin_roadmap_log.py — daily roadmap capture
  + post-session calibration. Read-only labeled-dataset builder for
  regime-conditional gating evaluation. After 10-15 days of calibration
  data with avg score >= 0.70 → regime classifier earns authority over
  strategy gating (Sprint F territory).
- `561e61d` feat(audit): tools/indicator_audit.py — predictive-value ranking

### New tool: indicator_audit

`python tools/indicator_audit.py [--discover] [--post-b13-only]
[--since YYYY-MM-DD] [--strategy NAME] [--min-sample N]`

Reads logs/trade_memory.json. Computes per-indicator lift over base rate
with Wilson 95% CIs and significance flags. Writes
`out/indicator_audit_<today>.md`.

**Two design choices worth knowing:**

1. `result` and `exit_reason` are EXCLUDED from feature extraction —
   they're post-hoc outcomes, not pre-trade predictors. Including them
   yielded tautological 100% lift rows.
2. Boolean confluence features (e.g. `conf:VWAP reclaim`) are compared
   "with feature vs without feature at all" (different populations)
   instead of the within-feature pivot, which collapses to nothing for
   binary-presence features.

### Interpretation guide

1. **Top Predictive** — confluences/features that fire more on winning
   trades, statistically significant. Boost weight or make required.
2. **Top Contra-Indicators** — fire MORE on losing trades. Either remove
   as confluences (noise) or invert sign (bot was reading backwards).
3. **Tier Classifier Validation** — verifies whether A++/A/B/C ordering
   matches outcome. If NOT predictive → defer Sprint B's tier-sizing
   proposal; tier feature is currently noise, not signal.
4. **Per-Strategy** — drives strategy-specific config decisions.
5. **Per-Regime** — evidence for regime-conditional gating (Sprint F+).

### Workflow

```bash
# Weekly during validation window:
python tools/validation_tracker.py --post-b13-only
python tools/indicator_audit.py --post-b13-only

# Run full lifecycle once enough post-B13 data exists:
python tools/indicator_audit.py             # all data (quick view)
python tools/indicator_audit.py --post-b13-only  # clean baseline
```

Findings at PRELIMINARY tier are *hypotheses*. Findings at TENTATIVE+
with non-overlapping CIs are *evidence*. Don't act on hypotheses.

### Live audit findings on current trade_memory (1,123 trades, mixed eras)

All findings at PRELIMINARY tier — informational only:
- `market.pivot_s2=Q1` → WR 54.5% (lift +36.7pp, n=55)
- `account=SimBias Momentum` → WR 16.5% (lift -29.9pp, n=91)
  - Consistent with Sprint C validation_tracker flagging bias_momentum
    as KILL_CANDIDATE territory.

Post-B13-only run yielded 17 trades — too few for significance. The
audit will sharpen as the validation window fills.

### Test suite delta

Sprint C final:  1,339 passing
Sprint D final:  1,418 passing (+79)
Sprint E final:  1,468 passing (+50)
Sprint F final:  1,484 passing (+16)

Test count: **1,484 passing, 4 skipped, 0 failing** (post-Sprint-F)

## Sprint F — Collision Forensic + Tier Persistence (2026-05-04)

Two commits shipped:
- `3b5ea7f` feat(diagnose): tools/diagnose_account_collisions.py
- `d4a59ba` fix(observability): persist tier field at exit

Phase 3 (cap reset) was HALTED — premise was wrong. bias_momentum was
not halted at weekly cap; the schema the prompt assumed didn't match
reality. The autonomous tool correctly refused to mutate against a
fictional schema. No commit shipped for Phase 3.

### Account routing audit findings

- 0 shared accounts in `STRATEGY_ACCOUNT_MAP` — each strategy maps to
  its own unique `Sim*` account.
- 3-strategy bot overlap (sim_bot + prod_bot can both load the same
  strategy) but routing-side separation prevents OIF collision.
- 0 collision evidence in last 72h of logs.
- Re-audit only if `config/account_routing.py` changes structurally:
  `python tools/diagnose_account_collisions.py --hours 72`.

### Tier persistence

`Position.tier` now propagates from signal through close into the
trade record (and through `scale_out_partial` for partial exits).
Sprint E's `indicator_audit.py` "Tier Classifier Validation" section
will populate within ~30-60 trades, answering empirically whether
A++/A/B/C ordering predicts outcome.

### bias_momentum current state (as of 2026-05-04)

bias_momentum is **NOT halted** (correcting any prior assumption). As
of Sprint F's audit on 2026-05-04, it is the top-performing strategy
this week: **+$142.62 net P&L across 9 trades** (33% WR — runner-mode
wins masking lower WR).

Active gates (post-Sprint-A):
- `session_block_windows` (08:30-08:59 + 10:00-13:29 CT blocked)
- `short_extra_gates` (requires both 1m + 5m bearish bias for SHORT)
- `target_rr=2.5`
- `rsi_div_hard_gate=True`
- `skip_on_stop_clamp=True`
- `trend_stall_grace_s=60`

Sprint E's indicator audit (2026-05-04) flagged
`account=SimBias Momentum` at WR 16.5% / lift -29.9pp at PRELIMINARY
tier (n=91). This is an *informational* finding at PRELIMINARY
confidence — re-check at TENTATIVE tier (n>=100) with non-overlapping
CIs before any kill decision.

**Discipline:** always check
`python tools/validation_tracker.py --post-b13-only` for current
strategy state. This memory file is historical context, not live truth.

### Why prod dashboard shows only 2 strategies

`prod_bot.only_validated=True` filters strategies by
`config['validated']`. Currently 2 of 10 pass:
- `bias_momentum` (validated=True)
- `ib_breakout`   (validated=True)

The other 8 strategies are in lab/sim validation. The dashboard
showing only 2 prod strategies is **expected behavior, not a bug**.

`validated=True` is a sticky flag set at promotion time. Nothing
currently auto-demotes a strategy whose post-promotion data shows
KILL_CANDIDATE characteristics (Sprint G candidate after tier-
persistence data accumulates).

## Sprint G — Dashboard Permanent Fix (2026-05-04)

Three commits:
- `0b4a9db` feat(diagnose): tools/diagnose_dashboard.py
- `cbaddb7` fix(dashboard): permanent fix for $0 + missing strategies
- (this) docs: Sprint G + operator verification checklist

### The two persistent dashboard issues, finally diagnosed

**1. Dashboard shows $0 P&L despite ~$238 in actual trades**

  Root cause (NOT a bug — UX defaults misleading):
  - `let activeBot = 'prod'` was the default tab
  - prod_bot only runs `validated=True` strategies (currently 2:
    bias_momentum + ib_breakout)
  - Prod bot legitimately has 0 trades on most days during the
    validation phase
  - The actual trades happen on sim_bot, which was on the inactive
    second tab
  - `/api/today-pnl` always returned correct data; the operator never
    saw it because they were looking at the prod tab

**2. Dashboard shows only 2 strategies**

  Root cause (also UX, also correct prod behavior):
  - prod_bot's strategy roster IS 2 (validated=True && enabled=True =
    bias_momentum + ib_breakout)
  - sim_bot's roster is 10 strategies (everything in `config.strategies`)
  - Operator was viewing the prod tab and seeing prod's correct count

### The fix — UX correction, NO trading-code change

`dashboard/templates/dashboard.html` only:

1. **New combined both-bots summary card above the tabs.** Always shows
   BOTH sim + prod P&L / trades / WR simultaneously, fetched from
   `/api/today-pnl`. Independent of which tab is active.

2. **Default tab changed from prod → sim.** Sim is where validation
   activity happens — that's the bot the operator wants to see first.

3. **Inline help text on the summary card** explaining the validated
   filter and pointing at `config/strategies.py` for promotion. Stops
   the "why is prod $0?" question recurring.

4. **`refreshBothBotsSummary()` JS function** called per poll cycle
   parallel to the existing `/api/status` fetch. Best-effort — failure
   won't break the main poll.

### New diagnostic tool

```bash
python tools/diagnose_dashboard.py
```

Read-only audit of dashboard backend + frontend state. Run any time
the operator suspects a display bug. Output:
`out/dashboard_diagnostic_<today>.md` — covers all backend routes,
frontend fetch calls, render references, live API responses, validated
flags, per-bot loaded strategies.

### Operator verification checklist

After the dashboard restart Sprint G triggered:

1. **Hard-reload browser** (Ctrl+Shift+R or Cmd+Shift+R) — Flask
   sometimes serves cached templates AND browsers cache HTML
2. Confirm the "Today (CME Globex)" card appears ABOVE the bot tabs
   showing both sim + prod current P&L
3. Confirm "Sim Bot" tab is active by default (was prod)
4. Click "Prod Bot" tab — confirm it correctly shows the 2-strategy
   subset (this is BY DESIGN, not a bug)

If dashboard ever shows $0 again on the sim tab when trades happened:
**rerun `python tools/diagnose_dashboard.py` first**, before
shipping any "fix". The diagnostic catches all 6 known causes
(wrong endpoint, wrong field, date filter, hardcoded filter,
prod-only endpoint, expectation mismatch).

### What's NOT in this sprint

- No live trading code touched (base_bot, position_manager,
  risk_manager, strategy code all untouched)
- No strategies auto-promoted to prod (promotion remains an operator
  decision after lab validation per discipline)
- No backend API contract changes (`/api/today-pnl`, `/api/status`,
  `/api/strategies` all return identical shapes — frontend just reads
  them now)

### Test suite delta

Sprint F final:  1,484 passing
Sprint G final:  1,494 passing (+10)
0 failing, 4 skipped throughout.

## L2 Data Subscription Decision (2026-05-04)

Audit run via `python tools/audit_l2_roi.py` against current
trade_memory (1,126 trades, 21 days). Full report:
`out/l2_roi_audit_2026-05-04.md`. **Verdict: KEEP — re-audit weekly.**

### Three views, one decision

**View 1 — Statistical lift:** 8 DOM fields captured at VALIDATED tier
(n=1,091, well past 666 threshold). Best lift `dom_ask_heavy` = +5.6pp
WR but **not significant** (Wilson 95% CIs overlap). All other DOM
fields show <6pp lift, also non-significant. **DOM does not predict
outcome on its own with current data.**

**View 2 — Architectural dependency:** 7 strategies reference DOM/CVD
in code; 3 are heavy users (>5 lines of decision code):

| Strategy | DOM/CVD refs |
|---|---:|
| `bias_momentum` | 18 |
| `ib_breakout` | 11 |
| `spring_setup` | 7 |
| `high_precision` | 5 |
| `vwap_pullback` | 5 |
| `dom_pullback` | 4 |
| `base_strategy` | 1 |

**View 3 — Economic ROI:** $100/mo over ~1,609 trades/month = **$0.06
per trade**. ~22% of trades had a DOM-keyword in entry_reason or
confluences ($0.28 per DOM-tagged trade). No significant edge
detected, so the cost is essentially defensive — paying for the
strategies' input feed even though it isn't statistically proven yet.

### Why KEEP despite no proven edge

The audit's hard rule: *"Don't recommend cancellation if any strategy
is hard-dependent."* Cancelling L2 right now would break or degrade
`bias_momentum` and `ib_breakout` — both currently `validated=True`
in prod. That's an unacceptable trade for $100/month savings, even
without proven edge.

### Re-audit cadence + cancel criteria

Weekly:
```bash
python tools/audit_l2_roi.py --post-b13-only
```

The L2 subscription becomes a CANCEL candidate when EITHER:
1. **No strategy code references DOM** (operator manually refactored
   bias_momentum + ib_breakout off DOM), OR
2. **DOM features show statistically significant NEGATIVE lift** at
   TENTATIVE+ tier (DOM is actively misleading), OR
3. **30 days pass without lift reaching significant + TENTATIVE
   POSITIVE** AND operator decides the lock-in is acceptable to break.

Until then, the $100/month is the cost of keeping the decision space
open.

### Test suite delta

Sprint G final:  1,494 passing
L2 audit added:  1,511 passing (+17)
0 failing, 4 skipped throughout.
