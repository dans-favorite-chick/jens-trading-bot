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
**Related**: Possibly connected to TL1 (dashboard datetime
serialize) which has a fix on fix/tl1-dashboard-datetime-serialize
branch — not yet merged.
**Fix owner**: TBD. Not scheduled.

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
**Fix owner**: TBD. Schedule after 50 lab trades per Fix 6 strategy
to get empirical baseline.

## B19 — simple_sizing.py stale default
**Discovered**: 2026-04-20 during Task D / daily cap audit
**Severity**: Low
**Root cause**: core/simple_sizing.py line 33 has max_daily_loss_usd
hardcoded default of 15.0, but line 68 imports from settings. The
line 33 default is dead code.
**Impact**: None in production, but misleading if someone reads
the module and assumes 15.0 applies.
**Fix owner**: TBD. Cleanup task.
