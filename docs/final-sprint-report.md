# Phoenix Bot — Final Sprint Report

_Completed: 2026-04-22 evening CDT._
_Branch: `feature/exit-audit-and-safety` — 15 commits ahead of main._
_Test count: 754 baseline → **809 passing + 4 xfailed**, 3 skipped, 0 failing._

---

## Executive summary

4 parallel workstreams. One high-impact finding that **answers Jennifer's core question**: *yes, Phoenix was tracking trailing stops — but the trailing stop updates were never written to NT8.* Python's state updated, NT8's bracket stop stayed put. That's the "+100 profit → reversal → stop-out at original stop" pattern she saw.

---

## WS-A: Guaranteed-Loss Audit

**Commits:**
- `a101af1` docs(b73): strategy config sanity audit
- `cdaa6e8` fix(b73): strategy classification flags + vwap_pullback wide-target marker
- `d5a0559` test(b73): CI-level strategy config sanity checks (+41 parametrized cases)

**Findings — all 10 enabled strategies audited:**

| Category | Count | Notes |
|---|---|---|
| CRITICAL | 0 | noise_area already patched at HEAD by earlier B61 |
| BROKEN | 0 | All bracket math produces valid geometry |
| SUSPECT | 1 | `vwap_pullback target_rr=20.0` with no trailing + no managed_exit + no exit_trigger → handed to WS-C |

**New CI-level defense:**
- `BaseStrategy.computes_own_target` / `computes_own_stop` flags — every strategy must either have `target_rr>=1.0` or explicitly declare it computes its own bracket
- `_wide_target_requires_trailing=True` marker on any `target_rr>=10` without a trailing mechanism
- `tests/test_strategy_config_sanity.py` — 41 parametrized cases run in CI, future regression fails the build

---

## WS-B: Opening Strategies Resurrection

**Commit:** `a6c94ee feat: 1m RTH aggregator unblocks opening_session strategies`

**Root cause** (confirmed): `strategies/opening_session.py` read 7 fields that `SessionLevelsAggregator.get_levels_dict()` never emitted → 88/88 SKIPs with `missing_fields` today.

**Missing fields identified:**
- `rth_1min_open`, `rth_1min_high`, `rth_1min_low`, `rth_1min_close`, `rth_1min_volume`
- `avg_1min_volume` (rolling 20-bar)
- `rth_5min_close_last` (distinct from existing `rth_5min_close`)

**Aggregator implementation** — extended `core/session_levels_aggregator.py`:
- `_update_rth_1min_rolling()` captures latest completed RTH 1m OHLCV + appends to `deque(maxlen=20)`
- `_update_rth_5min_rolling()` tracks latest 5m close during RTH
- RTH boundary 08:30–15:00 CT, reset daily
- `avg_1min_volume` returns None until 20-bar warmup complete
- Integration via existing `tick_aggregator.py:598` `session_levels.update()` call — no additional wiring needed

**Sub-strategies unblocked — all 6:**
- open_drive, open_test_drive, open_auction_in, open_auction_out, premarket_breakout, orb

**Tests:** +9 cases in `tests/test_session_levels_aggregator.py` (OHLCV capture, pre/post-RTH gating, 20-bar avg rolling, integration asserts no `missing_fields` skip)

---

## WS-C: Trailing Stop / BE-Move Audit — HIGH-IMPACT FINDING

**Commit:** `1cdaba1 docs+test(ws-c): trailing stop and BE-move audit`

### Per-strategy status

| Strategy | target_rr | Chandelier? | BE-move | **OIF reaches NT8?** |
|---|---|---|---|---|
| bias_momentum | 20.0 (rider) | NO | Rider BE @ 0.5R/1R | **❌ Python-only** |
| spring_setup | 1.5 | NO | none | n/a |
| vwap_pullback | 20.0 | NO | none | **❌ misconfig (WS-A SUSPECT)** |
| dom_pullback | 20.0 (rider) | NO | Rider BE | **❌ Python-only** |
| orb | 2.0 + runner | ✅ 3.0×ATR 5m | scale @ 1R → write_be_stop | ✅ (scale path only) |
| ib_breakout / compression / opening / vwap_band / noise_area | varied | NO | none | n/a |

### Jennifer's questions answered DIRECTLY

**1. Were bias_momentum / spring_setup chandelier-trailed today?**
**NO.** Zero `[CHANDELIER]` log lines for 2026-04-21. Only ORB attaches chandelier, and ORB didn't trigger today.

**2. Are stops being moved up during trades?**
**PARTIALLY — and the broken half is exactly the problem you saw.** Three `[TRAIL:...]` events fired today for bias_momentum rider positions. They ONLY updated `pos.stop_price` in Python. **NO OIF was written to modify NT8's bracket stop.** Same for rider BE-trigger + `move_stop_to_be`. NT8 kept the original 40-120t bracket stop the entire trade.

**3. Root cause of "+100 → giveback":**
1-contract rider trades force `target_rr=20.0` (base_bot.py:1948). Rely on stall+reversal exits. BE-stop "floor" exists only in Python. **When price whipsaws through the Python BE level between ticks, NT8's original far stop fills (or Python catches it a moment later at market).** Either way: no real trailing stop at the NT8 layer.

### Why the production fix isn't in this sprint

WS-C shipped the **audit, tests, and spec**. The actual OIF-writing code wasn't committed because it requires:
1. `Position.old_stop_order_id` tracking at bracket-place time (currently not captured)
2. A new `bridge.oif_writer.write_modify_stop()` function
3. Caller updates across rider trail, chandelier, and BE-move paths
4. Bridge-side validation so stale modifies don't resurrect cancelled orders

Those 4 changes span `bridge/oif_writer.py`, `bots/base_bot.py`, and `core/position_manager.py`. Each requires careful integration testing against NT8's order-state machine. Scoping: ~400-600 LoC + tests. **Deferred to its own sprint slot.**

Spec and caller update list are in `docs/trailing_stop_audit.md`.

### Tests (+5 pass / +4 xfail)
- `tests/test_chandelier_trailing.py` — 3 pass (Python state) + 2 xfail(strict) encoding the missing OIF wiring
- `tests/test_be_move.py` — 2 pass (Python state) + 2 xfail(strict) encoding the missing OIF wiring

The xfail tests will START FAILING THE BUILD the moment the OIF writes are added, forcing real green coverage.

---

## WS-D: OPEN_BUGS.md Closure

**No commits needed — file already at zero.**

| Status | Count |
|---|---|
| RESOLVED (with commit refs) | 17 |
| PARKED / CLOSED / SUPERSEDED | 10 |
| DEFERRED | 0 |
| DUPLICATE | 0 |
| **Net OPEN at end** | **0** ✅ |

All prior-sprint fixes are properly attributed. Agent verified no genuinely-open entries exist. No speculative padding.

---

## Final stats

| | |
|---|---|
| **Commits on branch** | 15 (from B59 + 6 streams) |
| **Tests added** | 754 → 809 passing (+55), 4 xfailed (intentional) |
| **Test regressions** | 0 |
| **Strategies production-trading** | 10 enabled + 6 opening sub-strategies now UNBLOCKED (all 16 account destinations live) |
| **Strategies disabled** | 0 |
| **Strategies with config concern** | 1 (vwap_pullback — flagged, not disabled) |

---

## Recommended next actions for Jennifer

### 1. Merge `feature/exit-audit-and-safety` → `main`
**YES** — branch is green, tests pass, 15 commits of forward progress, 4 xfails are intentional markers (missing OIF-modify wiring). No regressions.

### 2. Next sprint slot (priority order)

**A. Write stop-modify OIF wiring (HIGH).** The WS-C finding. Without this, every rider-mode trade can still give back a 100-point winner. Spec in `docs/trailing_stop_audit.md`.

**B. Resolve vwap_pullback target_rr=20 misconfig.** Either attach chandelier trailing OR drop target_rr to 2-3. WS-A flagged; WS-C confirmed no trailing exists. Pick one.

**C. Observe opening_session actually firing tomorrow 8:30-10:00 CT.** WS-B unblocked the 6 sub-strategies. Tomorrow's session is the real proof-of-life test. After warmup (first 20 RTH minutes), expect signals from open_drive / open_test_drive / open_auction_* / premarket_breakout / opening-ORB.

### 3. Strategies to watch carefully next session

- **All 6 opening_session sub-strategies** — first live run post-R1 fix
- **bias_momentum + dom_pullback** — confirm no "+100 giveback" trades (root cause still present until OIF-modify lands)
- **noise_area** — managed-exit + 300t safety-net target shipped in prior sprint, confirm no commission bleed

### 4. Config values to tune based on observation

Defer until you have 3-5 sessions of data with the new aggregator + sanity tests active. No blind tuning.

---

## Everything shipped this sprint (branch `feature/exit-audit-and-safety`)

```
1cdaba1  docs+test(ws-c)  trailing stop and BE-move audit
d5a0559  test(b73)         CI-level strategy config sanity checks
cdaa6e8  fix(b73)          strategy classification flags + vwap_pullback marker
a101af1  docs(b73)         strategy config sanity audit
a6c94ee  feat              1m RTH aggregator unblocks opening_session
8cdda40  feat(b72)          tools/analyze_conflicts.py CLI
98489e9  feat(b71)          conflict section in daily 17:00 CT briefing
63e1d51  feat(b70)          detect + log directional conflicts
d9b3685  docs               exit-sprint final report (prior sprint)
92a36af  feat(b64)          target-miss forensic audit
1cd3b38  feat(b63)          verify both OCO legs after PROTECT
eee96ba  feat(b61,b62)      noise_area target=entry + sanity gate
2f34042  fix(b66)           opening_session.last_skip_reason observability
7f69c47  test(b70)          b16 bot_id test update
53d0011  test(b69)          trade_memory integrity check
fd6b295  feat(b70-write)    trade_memory null bot_id rejection
1bda9f4  feat(b69)          backfill tool + 955 row backfill
244b031  fix(b59)           live-account hard-guard + 10 tests
```

All pushed. Branch ready for merge.
