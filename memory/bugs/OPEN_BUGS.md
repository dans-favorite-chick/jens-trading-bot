# Phoenix Bot — Open Bugs

Tracking file for bugs discovered but not yet scheduled for fix.
Newest first. B15 backlog (6 pre-existing test failures) tracked
separately via the pytest suite itself.

---

## B12 — fix/b12-vwap-pullback-base-strategy SUPERSEDED
**Status**: SUPERSEDED (not merged)
**Reason**: b12's BaseStrategy-contract concern was already resolved
by Fix 6's refactor of vwap_pullback. b12's algorithm rewrite
(1σ/2σ bands + RSI(2)) was reshaped into a new strategy file
`strategies/vwap_band_pullback.py` via feat/vwap-band-pullback-from-b12
(commit adf6b4e, merged 69dcfd4) so it runs alongside vwap_pullback
for head-to-head lab data collection.
**Branch disposition**: fix/b12-vwap-pullback-base-strategy left on
origin but should not be merged — kept for historical reference.

---

## B16 — Trade memory bot_id attribution missing
**Discovered**: 2026-04-20 during Task B P&L diagnostic
**Severity**: Medium
**Root cause**: All 864+ trade_memory.json entries have bot_id=None.
Dashboard cannot separate lab vs prod P&L because the attribution
field is never populated on trade commit.
**Impact**: Dashboard P&L panel unreliable; post-hoc analysis
cannot split lab/prod performance.
**Location**: core/trade_memory.py commit path or wherever bot
passes bot_id on trade close.
**Fix owner**: TBD. Not scheduled.

## B17 — Dashboard state push stale after full reboot
**Discovered**: 2026-04-20 during Task B P&L diagnostic
**Severity**: Medium
**Root cause**: After full-stack reboot 07:47, dashboard's in-memory
_state["lab"]["trades"] never refreshed despite 40 fresh trades in
trade_memory.json. Dashboard /api/trades returned 07:45 data as of
09:07 (1h22m stale).
**Impact**: Dashboard P&L panel shows stale data after any reboot.
**Resolution**: 2026-04-20. Merged fix/tl1-dashboard-datetime-serialize
(base_bot.py + tests/test_dashboard_serialize.py). Datetime fields
now serialize to ISO strings; state-push warnings eliminated.
Merge commit: f4647b1.
**Status**: RESOLVED

## B18 — Stop placement audit: remaining fixed-tick strategies
**Discovered**: 2026-04-20 during Fix 6 research
**Severity**: Low-Medium
**Root cause**: Fix 6 refactored vwap_pullback, bias_momentum, and
dom_pullback to NQ-calibrated ATR-anchored stops (2.0x / 40-120
clamp / 64 fallback). Other strategies not audited against
research-consensus NQ stop standards.
**Impact**: If any other strategy uses undersized stops, it will
be stop-hunted by NQ noise similarly to what Fix 6 addressed.
**Audit targets**: spring_setup (1.1x — intentional structure
anchor, likely correct), compression_breakout (ATR-pure, verify
multiplier), ib_breakout (structural, verify buffer), orb
(structural, verify buffer), noise_area (structural, verify buffer).
**Resolution**: 2026-04-20 Fix 7 + Fix 8.
- compression_breakout + spring_setup clamps raised 8/40 → 40/120
  (Fix 7, commit 645b097).
- ib_breakout gains max_stop_ticks=120 skip-guard mirroring ORB
  pattern (Fix 8, commit 7e0dab1).
- orb already compliant (min_or_size_points=10 floor,
  max_stop_points=25pt/100t ceiling skip).
- noise_area uses managed exit; stop_ticks inflation tracked
  separately as B21.
**Status**: RESOLVED

## B20 — ib_breakout structural stop exceeds NQ ceiling
**Discovered**: 2026-04-20 during Fix 6 follow-up audit
**Severity**: Medium
**Root cause**: Structural stop at opposite IB boundary produced
80-320 tick stops on normal-to-high vol days.
**Resolution**: 2026-04-20 Fix 8. Added max_stop_ticks=120
skip-signal guard. Commit: 7e0dab1.
**Status**: RESOLVED

## B21 — noise_area stop_ticks inflates risk manager position sizing
**Discovered**: 2026-04-20 during Fix 6 follow-up audit
**Severity**: Low-Medium
**Root cause**: noise_area reports 150-600 tick "stops" to risk
manager, but actual strategy uses managed-exit (momentum/
reversal/EoD), not stop orders. Inflated stop_ticks feeds
into position_manager sizing logic and may reduce position
sizes inappropriately.
**Impact**: Positions may be sized smaller than intended on
noise_area trades.
**Fix owner**: TBD. Architectural — not a parameter tweak.
Needs investigation of whether position sizing should read
"disaster stop" or a different risk reference.
**Status**: OPEN

## B19 — simple_sizing.py stale default
**Discovered**: 2026-04-20 during Task D / daily cap audit
**Severity**: Low
**Root cause**: core/simple_sizing.py line 33 has max_daily_loss_usd
hardcoded default of 15.0, but line 68 imports from settings. The
line 33 default is dead code.
**Impact**: None in production, but misleading if someone reads
the module and assumes 15.0 applies.
**Fix owner**: TBD. Cleanup task.

## B22 — EVAL debug logs invisible at lab INFO level
**Discovered**: 2026-04-20 during Phase 5 restart R5 observation
**Severity**: Low-Medium
**Root cause**: Fix 5 logs [EVAL] BLOCKED/SKIP/NO_SIGNAL events at
`logger.debug()` level. Lab bot ran at logging.INFO, so those
reject-reason events never surfaced in the log — defeating Fix 5's
observability goal for lab data collection. Only SIGNAL events
(logger.info) were visible.
**Impact**: Could not see *why* strategies passed on setups in lab
— only that they did or didn't fire.
**Resolution**: 2026-04-20. Lab bot log level raised INFO → DEBUG.
Prod stays INFO (production logs stay quiet). Commit e22a4a1,
merge fbe215d.
**Status**: RESOLVED

## B26 — MenthorQ parser empty-value robustness
**Discovered**: 2026-04-21 during gamma ingest blocker review
**Severity**: Low
**Root cause**: core/menthorq_gamma.parse_gamma_paste has not been
tested against paste strings with empty values, e.g.
`Put Support 0DTE, ,` (comma with whitespace where a number should
be). Current parser may crash or swallow the key silently instead
of treating empty values as explicit None.
**Impact**: If Jennifer's paste accidentally includes a blank value
on a Tier 1 field, the daily ingest could fail loudly or — worse —
silently load with a corrupted level.
**Fix owner**: TBD. Add test case + graceful None handling.
**Status**: OPEN

## B27 — GammaLevels schema ext: Net GEX + Total GEX magnitudes
**Discovered**: 2026-04-21 during gamma ingest blocker review
**Severity**: Low-Medium → PRIORITY: HIGH after 2026-04-21 regime
disagreement between HVL proxy and MenthorQ's Net GEX authoritative.
**Root cause**: GammaLevels dataclass stored individual strike
levels but not the absolute Net GEX magnitude that MenthorQ reports.
Regime classification was relative (price vs HVL), which disagreed
with MenthorQ's ground-truth Net GEX sign.
**Resolution**: 2026-04-21 on feat/b27-net-gex-regime. Schema
extended with `net_gex` / `total_gex` / `iv_30d`. Classifier
rewritten with Net GEX as primary signal and HVL as fallback.
6-value enum `GammaRegime` (POSITIVE_STRONG/NORMAL, NEUTRAL,
NEGATIVE_NORMAL/STRONG, UNKNOWN) replaces legacy 4-value. Parser
accepts K/M/B suffixes. Migration touched 2 call sites inside
`core/menthorq_gamma.py` (classify_regime + regime_multipliers);
`bots/base_bot.py` needed no change because it only used
`GammaRegime.UNKNOWN`, which is preserved. Thresholds
(`MENTHORQ_NET_GEX_STRONG_THRESHOLD=3_000_000`,
`MENTHORQ_NET_GEX_NORMAL_THRESHOLD=500_000`) live in
`config/settings.py`. Paste format documented in
`data/menthorq/gamma/README.md`.
**Status**: FIXED

## B23 — Third-party DEBUG log noise
**Discovered**: 2026-04-20 post-restart observation + 2026-04-21 crash review
**Severity**: LOW-MEDIUM
**Root cause**: After B22 raised lab to DEBUG log level, `websockets.client`
emits ~10 lines/sec of raw tick dumps and yfinance/peewee/httpcore emit
dozens of lines per intermarket cycle. Log grew 199 MB in 22h, drowning
the Fix 5 [EVAL] reject-reason output and potentially contributing to
silent-crash hypothesis (log I/O pressure).
**Resolution**: 2026-04-21 commit 62b2085. Added logger-level overrides
in bots/lab_bot.py immediately after basicConfig: websockets.client/server,
yfinance, httpcore (+ connection + http11), peewee, chromadb all set to INFO.
Bot-level DEBUG preserved for strategy observability.
**Effect**: Reduces log volume ~10× while keeping [EVAL] signal visibility.
Takes hold on next lab restart.
**Status**: FIXED

## B32 — Alpaca VIX API returning 401 Unauthorized
**Discovered**: 2026-04-21 during lab session reconciliation
**Severity**: MEDIUM
**Root cause**: Alpaca API credentials for VIX endpoint appear invalid or expired;
intermarket filter unable to fetch VIX readings, running blind on one signal.
**Impact**: Intermarket filter degrades gracefully but VIX-based regime input absent.
**Fix owner**: TBD — rotate Alpaca key or investigate whether VIX moved to different endpoint.
**Status**: OPEN

## B33 — Parallel MenthorQ data sources with stale Path A feeding structural_bias
**Discovered**: 2026-04-21 during post-merge 4C verification session
**Severity**: MEDIUM (downgraded from initial HIGH after scope precision)

**Architecture diagnosis** (confirmed via code-archaeology on 2026-04-21):
Two independent MenthorQ data paths currently coexist:

Path A — `data/menthorq/menthorq_daily.json` (stale since 2026-04-17):
  - Writer: dashboard/server.py (user edits via UI)
  - Reader: core/menthorq_feed.py → market_snapshot["menthorq"]
  - Consumer: core/structural_bias.py score_menthorq_gamma() — 15-pt weight

Path B — `data/menthorq/gamma/YYYY-MM-DD_levels.txt` (fresh, B14/B27):
  - Writer: user paste
  - Reader: core/menthorq_gamma.py → self.gamma_levels + B27 regime classifier
  - Consumers:
    * bots/base_bot.py entry-wall gate (critical path)
    * strategies/opening_session.py is_entry_into_wall() (direct)
    * snapshot enrichment (market_snapshot["gamma_regime"])

**Impact**: structural_bias scoring layer is degraded (reading 4-day-old gamma),
  biasing composite scores based on stale Net GEX. Entry-wall gate and
  opening_session direct gamma checks remain on fresh data — not broken,
  but bias-scored trades are being evaluated against stale context.

**NOT affected** (contrary to startup warning text in menthorq_feed.py):
  - spring_setup.py (zero menthorq references)
  - gamma_flip_detector.py (zero menthorq references)
  - Fix: tighten the warning message to mention structural_bias only.

**Fix plan**:
  (a) Tactical: update menthorq_daily.json daily alongside the gamma paste
      until (b) lands. Zero-risk, 5-min add to paste ritual.
  (b) Strategic: re-wire score_menthorq_gamma to read from
      market_snapshot["gamma_regime"] (the B27 6-value enum) instead of
      the legacy menthorq dict. Retire Path A once structural_bias migrated.

**Status**: OPEN (tactical fix applicable immediately; strategic fix queued)

## B35 — bots/base_bot.py:2421 missing account= parameter
**Discovered**: 2026-04-21 during CC code review
**Resolution**: FALSE ALARM. Line 2421 is a `return` statement, not a write_oif call.
Earlier grep output misread due to PowerShell Select-String row-duplication artifact.
Verified via Read: line 2702 is the single EXIT write_oif call site and correctly
passes account=pos.account. No bug exists.
**Status**: CLOSED (not a bug)

## B36 — Lab bot silent crash PID 40908 (23.7 min uptime)
**Discovered**: 2026-04-21 10:42:40 CT during 4C verification session
**Severity**: LOW (parked pending recurrence)
**Signature**: WS close code=1006, no Python traceback, no Windows Error Reporting event,
no shutdown log, process vanished.
**Hypotheses investigated and ruled out**:
- Native extension crash: no WER event
- Missing account→ValueError: all call sites correctly pass account=
- Log-volume threshold: PID 32412 has exceeded 40908's log size without crashing
**Remaining hypothesis**: event-triggered transient (bad tick sequence, transient
resource spike, yfinance/intermarket API stall during RTH burst).
**Mitigation in flight**: B23 (log-noise silencer, commit 62b2085) may address
the log-pressure sub-hypothesis even if the primary trigger is something else.
Takes effect on next lab restart.
**Fix owner**: Monitor for recurrence. If crash #3 happens, capture last 500 non-websockets
log lines + Windows System event log around crash timestamp.
**Status**: PARKED

## B37 — 4C integration test gap
**Discovered**: 2026-04-21 during test coverage review
**Severity**: LOW
**Root cause**: test_account_routing.py covers map correctness, _require_account raises,
and wire-format position — all unit-level. No end-to-end integration test covers
signal → bot._enter_trade → bridge → write_oif path with account plumbing, nor
bridge-side handling of missing account in WS message, nor write_bracket_order's
delegated guard.
**Fix owner**: Add tests/test_4c_integration.py in a follow-up testing sprint.
**Status**: OPEN
