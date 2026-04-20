# Phoenix Bot — Open Bugs

Tracking file for bugs discovered but not yet scheduled for fix.
Newest first. B15 backlog (6 pre-existing test failures) tracked
separately via the pytest suite itself.

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
