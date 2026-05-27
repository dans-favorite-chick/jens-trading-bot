# Phoenix Bot — Hard Audit (2026-04-22 CDT)

_Stance: adversarial. Assume hidden failure until proven otherwise._
_Latest HEAD: `d6789d4` (B75 ships, CANCELALLORDERS blocked from bot paths)_
_Test count: 813 passing, 3 skipped, 4 xfailed (intentional WS-C markers)_

---

## A. CRITICAL FINDINGS (ranked by risk)

### A1. **Trailing stops are decorative — NT8 stop never moves** (HIGH — still partially open)
**File:** `bots/base_bot.py` rider/chandelier/BE-move paths; `core/chandelier_exit.py`
**Status:** WS-C confirmed this. Partially addressed (xfail tests shipped). Actual OIF-modify wiring **not in**.

**What happens:** Chandelier and BE-move logic update `pos.stop_price` in Python. **No OIF is written to NT8 to modify the bracket stop.** When price reverses, NT8's ORIGINAL wide stop fills, not your tightened trail. You eat the full original risk on every reversal.

**Evidence:** Grep log for `[CHANDELIER]` or `[TRAIL:]` — events fire, `pos.stop_price` changes, but no `write_oif` / stop-modify OIF emitted. Today the "+100pt giveback" pattern you saw was this exact bug.

**Live-trading impact:** Every winning trade that gets trailed in Python but not NT8 is a potential full-stop reversal loss instead of a locked-in profit. On a 1-contract MNQ position, that's the difference between +$100 and –$100 per trade.

**Fix required:** Capture NT8 order_ids at bracket submission → new `write_modify_stop(order_id, new_stop_price, account)` OIF function → wire into trail/BE/chandelier paths. Scope ~300-400 LoC. **Do not deploy to live until this ships.**

---

### A2. **Phoenix state vs NT8 state has no reconciliation on restart** (HIGH)
**Files:** `core/position_manager.py` (no persistence); `bots/base_bot.py` startup path

**What happens:** PositionManager is **in-memory only**. Bot restarts → Python thinks all accounts are flat. NT8 still holds every open position. Phoenix then either:
- Silently misses stops/targets (no PositionManager tracking → no exit logic runs on that position)
- Or fires new entries on accounts NT8 shows as already LONG → NT8 rejects with "Exceeds max pos qty"

B50 pre-entry guard (skip new entry if NT8 non-flat) mitigates the second case. **It does nothing for the first.** Positions opened before restart become orphans with no one managing them until user manually flattens.

**Today's evidence:** 3 orphan positions (SimNoise Area, SimSpring Setup × 2) resulted from this pattern combined with the B75 bug.

**Fix required:** Startup reconciliation — on bot boot, scan NT8 `outgoing/*_position.txt` for every routed account. For each non-FLAT position, reconstruct a Position object (trade_id synthesized as `RECONCILED_<account>_<uuid>`) with the NT8-reported direction/qty/avg-price. Mark `reconciled=True` so we know no strategy-side exit trigger should fire for these (adopt only passive protection: attach a wide stop immediately).

---

### A3. **Entry fill confirmation is timer-based, not event-driven** (MEDIUM-HIGH)
**Files:** `bridge/oif_writer.py wait_for_fill()`; `bots/base_bot.py` entry path

**What happens:** After OIF submit, Phoenix sleeps 5s polling `outgoing/` for a fill file. If the file doesn't land in 5s:
- If `LIVE_TRADING=True` → abort (correct)
- If account != Sim101 (any sim sub-account) → B39 phantom guard either alerts "REJECTED" or quietly treats as "ENTRY_PENDING" based on whether the OIF file is still in incoming/
- If account == Sim101 → B48 "assume filled" fallback (paper mode — acceptable)

Latency-sensitive: if NT8 is slow (overloaded, thin-liquidity LIMIT waiting for touch), the 5s timeout creates false-phantom or false-fill decisions. On LIMIT orders specifically, 5s is not enough — limits can sit for minutes before filling.

**Real risk:** Phoenix could declare a position PENDING (no Python open) but NT8 fills 10 seconds later → orphan position. Or Phoenix could declare PHANTOM_GUARD reject and abort, but NT8 actually accepted the order and it fills later → orphan.

**Fix required:** Move fill detection to file-system-event-driven (watchdog library or Windows IOCP) so we react in <100ms. Secondary: B47 post-fill verify (already shipped) catches most of these at the position-file layer.

---

### A4. **LIMIT entries can be working unseen for hours** (MEDIUM)
**Files:** `bridge/oif_writer.py write_bracket_order`; `bots/base_bot.py` entry path

**What happens:** Many strategies (spring_setup, vwap_pullback, compression_breakout) submit LIMIT entries. If price doesn't touch, the LIMIT sits Working indefinitely (GTC, 60-day max). Phoenix's 5s timeout treats this as ENTRY_PENDING and moves on. No Python tracking.

Market eventually touches → NT8 fills → position opens with NO Python tracker → no strategy-side exit logic → orphan.

**Fix required:** Either:
- Cancel LIMIT after 60-90s via per-order CANCEL (requires B75-OptionA order-id tracking)
- Or switch all entries to STOPMARKET (fill certain, lose price precision)
- Or poll NT8 outgoing for entry fill confirmation out-of-band and adopt the position reactively

---

### A5. **CANCELALLORDERS cross-account bug FIXED today (B75)** — verify in prod
**Status:** Shipped in commit `d6789d4`. Bots restarted.
**Residual risk:** if operator clicks "Cancel All Orders" button in NT8 Control Center UI, NT8 still cancels across every account. Phoenix can't prevent that — it's an NT8 UI action.

**Guidance:** Never use "Cancel All" button in NT8 UI. Cancel individual orders in the Orders tab.

---

### A6. **ALPACA_BASE_URL in `.env` has duplicated /v2** (LOW, cosmetic — not trading-impacting)
`paper-api.alpaca.markets/v2` + SDK appends `/v2` → 404 on trading-API calls. VIX market-data path uses `data.alpaca.markets` separately so yfinance race still wins. Fix when convenient.

---

### A7. **Live account ATI guard is code-only** (MEDIUM)
**File:** `bridge/oif_writer.py _reject_live_account()` (B59)

**What happens:** Phoenix can never write to account `1590711` (tested). But NT8's ATI still accepts manual OIFs from other sources targeting that account. Today's rogue fills on 1590711 came from NT8 Chart Trader (non-Phoenix).

**Fix required:** Disable ATI on the live account in NT8 Control Center. This is a user-side action, not code.

---

## B. ORDERS AND STOPS DEEP DIVE

### How orders are created today

1. **Signal generation** (`strategies/<name>.py`) → returns `Signal(direction, strategy, reason, confluences, stop_price, target_price)` or None
2. **Bot receives signal** in `bots/base_bot.py::_enter_trade(signal)`:
   - B50 pre-entry check: `verify_nt8_position(account, expected="FLAT")` — skip if NT8 already has position
   - B59 live-account guard — refuse if `_account == LIVE_ACCOUNT`
   - B62 sanity gate — refuse if bracket geometry wrong
3. **WS send to bridge**: `{type:trade, action:ENTER_LONG/ENTER_SHORT, qty, stop_price:None, target_price:None, account, limit_price}`  
   *(B55 split-submit: stop/target intentionally None here — sent post-fill)*
4. **Bridge** (`bridge/bridge_server.py`) receives WS msg → `write_oif(action="ENTER_LONG", ..., account=X)` → writes OIF file to NT8 `incoming/`
5. **NT8 ATI** reads file, places order on specified account
6. **Phoenix waits** 5s via `wait_for_fill()` — polls `outgoing/<account>_<order_id>.txt`
7. **If fill confirmed**: B47 `verify_nt8_position` sanity-checks NT8's position file matches `expected_direction/qty`
8. **If verified**: B55 second WS send for `PLACE_PROTECTION` with stop+target → bridge writes OCO pair → up to 3 retries → emergency-flatten on all-retries-failed

### How stops are placed today

- Initial stop: part of post-fill OCO pair in step 8 above. `PLACE;<account>;MNQM6;SELL/BUY;<qty>;STOPMARKET;0;<stop_price>;GTC;<oco_id>;;;`
- Written to NT8 via OIF file → ATI processes → NT8 creates working STOPMARKET order linked via OCO
- **NT8 side: real working order, fills automatically when price touches**

### How stops are "moved" today — **BROKEN**

- Chandelier: `core/chandelier_exit.py` computes new stop price on every tick
- BE-move: `bots/base_bot.py` rider/strategy code checks MFE and moves stop to BE+small when conditions met
- Both paths: **only update `pos.stop_price` in Python memory**. **No OIF written to NT8.**

**This means:** NT8's original STOPMARKET order stays at the original price forever. Python "thinks" the stop has been trailed/moved. When market reverses, NT8 fills at the original wide stop, not the trailed level.

### Is the current stop system trustworthy?

**For fixed-stop trades (spring_setup, noise_area): YES.** Initial OCO attaches correctly (B55 verified, B63 both-leg check, B75 no-more-CANCEL_ALL nuke). Stop fills at the price you submitted. Position closes.

**For trail/BE/chandelier trades (bias_momentum, dom_pullback rider modes): NO.** NT8-side stop never moves despite Python showing updated `pos.stop_price`. This is the single most dangerous gap in the system.

---

## C. STRATEGY-BY-STRATEGY ASSESSMENT

Ranked from production-ready to weakest.

### 🟢 Production-ready

**1. `spring_setup`** — `target_rr=1.5`, fixed ATR-anchored stop at wick extreme. Geometry sane, 5:1 or 3:1 brackets reachable, no trailing dependency. **Ship.**

**2. `noise_area`** — After B61 fix (target=entry bug eliminated) + `uses_managed_exit=True` flag + 300t safety-net target. Managed exit logic fires on momentum reversal. Works with fixed bracket protection. **Ship.**

### 🟡 Conditionally production-ready (need WS-C OIF-modify fix)

**3. `bias_momentum`** — Rider config forces `target_rr=20.0` + Python-only trailing. Without NT8-side stop moves, the 20:1 target is unreachable and the "+100 giveback" pattern repeats. **DO NOT ship to live until WS-C fix lands.** Works okay in sim because all fills are fake.

**4. `dom_pullback`** — Same rider config + same trailing gap. **Same constraint.**

**5. `orb` (standalone)** — Has chandelier attached + BE-move + scale-out. Best-configured rider. **Still has the NT8-side trailing gap** but the scale-out path writes real exit OIFs so partial profits lock in even without trail. **Ship with caution; acknowledge second contract rides on stale stop.**

### 🟠 Structurally weak

**6. `vwap_pullback`** — `target_rr=20.0` with NO trailing, NO managed exit, NO exit_trigger. Only exit is `max_hold_min=60`. **Targets unreachable; trades exit on time, not on edge.** Flagged by WS-A. **Either reduce target_rr to 2-3 OR add trailing before shipping.**

**7. `vwap_band_pullback`** — Similar concerns; less validated than vwap_pullback. Uncertain production readiness.

**8. `ib_breakout`** — Structural stop at IB opposite boundary can be 80-320 ticks; B20 added max_stop_ticks=120 skip guard but still allows wide stops. Risk scales inversely with IB range. **Review stop-ticks clamp tuning before live.**

**9. `compression_breakout` (15m + 30m)** — ATR-based stops (40/120t clamps from Fix 7). Target reasonableness depends on ATR regime. Fewer validated trades than momentum strategies. **Observe 2 weeks before live.**

### 🔴 Structural blockers / unknown

**10. `opening_session` (6 sub-strategies)** — B66 observability + WS-B 1m aggregator shipped, but **no production-traded evals yet**. Tomorrow morning is the first real test. Classification logic, PRE-RTH data quality, warmup race — all unvalidated in live conditions. **Observe 5-10 sessions before live.**

**11. `high_precision_only`** — `enabled=False` in config. Not evaluated. Not reviewed here.

---

## D. FAILURE MODES

### D1. Operational

- **Bot restart loses position state.** 3 orphans in one day (today). B50 mitigates new entries but not existing positions.
- **Watchdog auto-restart on failed dashboard API** — if dashboard hangs, watchdog can't restart bots. Watchdog → dashboard → bot process chain has 3 failure points.
- **Log files grow unbounded.** No rotation. 9 MB prod stdout today alone. Eventually disk pressure + I/O starvation.
- **NT8 UI "Cancel All" button** — one click wipes every OCO on every account. Phoenix can't prevent this.

### D2. Architectural

- **PositionManager not persisted.** Already covered. This is the #1 operational pain point.
- **Every strategy reads from a shared `market_snapshot` dict.** If any component upstream (aggregator, menthorq_feed, session_levels) returns stale or wrong data, every downstream strategy is poisoned. No data-freshness assertions at consumer boundaries.
- **Bridge is single-threaded.** If NT8 WS blocks, entire trade flow stalls. No timeout-circuit-breaker at bridge layer.
- **Two bots trading independent accounts.** Prod + sim running simultaneously. Each can submit OIFs. No mutex between them. Pre-B75 this caused the CANCELALLORDERS bleed.

### D3. Execution

- **LIMIT entries can hang for hours** (D4 above). GTC TIF + no expiry enforcement. Mitigation requires order-id tracking.
- **Partial fills not handled.** All entries are qty=1 currently so not hit. If sizing >1 ever lands, partial-fill behavior is undefined.
- **OCO auto-cancel relies on NT8 internal logic.** We verified it works in today's 12:30:18 fill (target filled → stop auto-cancelled 1 ms later). But if NT8 had a bug where auto-cancel fails, Phoenix wouldn't notice until next startup reconcile (which doesn't exist either).

### D4. Risk management

- **Per-strategy daily caps are Python-side only.** If the bot restarts mid-day, cumulative P&L reloads from disk but `RiskManager.daily_pnl` in-memory resets to zero. A bot restart during a losing streak could silently restart the daily-loss budget.
- **No circuit breaker on market-wide halt.** If MNQ goes limit-down, no Phoenix code checks for "halted" state.
- **No unusual-spread protection.** Microstructure filter logs `DOM opposing` / `spread wide` but doesn't block.
- **$200 daily loss cap + $1500 floor are per-strategy but not aggregate.** Total exposure across 16 accounts could hit $3200 daily loss ($200×16) before anything halts globally.

### D5. Data integrity

- **MenthorQ daily JSON is manual paste.** 5-minute daily ritual. Skipping it → stale regime data → strategies using wrong gamma context.
- **Trade memory backfill** — 955 rows retroactively classified. Historical Learner weekly analysis on unknown-legacy data is noisy.

---

## E. BULLETPROOFING PLAN

### Must-fix before live trading (do NOT skip)

1. **B76: NT8-side stop-modify OIF wiring** (A1). Without this, live trailing is fiction. **400-600 LoC + tests.** Single most important item.

2. **B77: Startup reconciliation** (A2). Scan NT8 outgoing/position.txt for every account on boot; rebuild Position objects; attach passive protection. **~150 LoC + tests.**

3. **B78: Event-driven fill detection** (A3). Replace polling wait_for_fill with file-system watcher (watchdog package). Reduces fill-timeout false-positives. **~100 LoC.**

4. **Disable ATI on live account 1590711** (A7). User-side, 1-minute NT8 config change. Non-optional.

5. **Reduce or disable `vwap_pullback`** (C6). `target_rr=20` with no trailing is a structurally unprofitable config. Either drop target_rr to 2-3 or add chandelier or disable.

### Should-fix soon (within 2 weeks)

6. **B79: LIMIT entry expiry** (A4 / D3). Auto-cancel LIMIT entries after 90-120s if not filled. Requires order-id tracking from B76.

7. **B80: Aggregate daily-loss circuit breaker** (D4). Halt ALL strategies if total daily P&L < -$1000 across all accounts.

8. **B81: Log rotation** (D1). Cap per-log file at 100MB with 5-file rotation.

9. **Persist PositionManager state** (D2). JSON dump on every position open/close + load on startup. Pair with B77 reconcile.

### Nice to have later

10. Data-freshness assertions at market_snapshot consumer boundaries
11. Dashboard Working Orders panel → add "NT8-verified" column (once B76 order-id tracking exists)
12. Partial-fill handling
13. Market-halt detection
14. Microstructure-filter blocking mode (not just logging)

---

## F. VERIFICATION PLAN

### Tests to add

1. **B76 (stop-modify)**: integration test that submits a bracket, issues a modify-stop OIF, reads outgoing/ to confirm NT8 accepted the new stop price. Must run against a real NT8 instance in sim — xfail current tests expose the gap.

2. **B77 (reconciliation)**: start bot with seeded NT8 position file → assert Position objects created → assert protection OIFs submitted within 5s of boot.

3. **B78 (event-driven)**: mock file-system event on outgoing/ → assert fill detected <200ms.

4. **B79 (LIMIT expiry)**: submit LIMIT entry → fast-forward 120s → assert CANCEL OIF emitted with matching order_id.

5. **B80 (aggregate cap)**: seed 16 strategies with -$100 each → assert aggregate halt fires at -$1000 threshold.

### Runtime safeguards

6. **Startup self-check**: on every bot boot, log `[SELFCHECK]` with env keys, NT8 paths, OCO auto-cancel test (submit dummy 0-qty order, confirm it rejects with expected error).

7. **Heartbeat**: every 60s, emit `[HEARTBEAT]` with {positions_count, last_tick_age, last_fill_age, cumulative_pnl, last_error}. Watchdog asserts heartbeat present.

8. **Position-state diff alert**: every 30s, compare `PositionManager.active_positions` against NT8 `outgoing/*_position.txt`. If diff exists → Telegram [STATE_DIVERGENCE] alert.

### Log checks to automate

9. `tools/scan_for_orphans.py` — daily cron: scan today's NT8 log for `Cancel all orders` waves, correlate with `Market position=Long Operation=Operation_Add` events, report any positions that stayed OPEN after their OCO got wiped.

10. `tools/verify_trail_moves.py` — daily cron: scan for `[TRAIL]` log events, confirm each has a matching `PLACE;...STOPMARKET;...` OIF written within 100ms. Fail if any trail event has no corresponding OIF.

11. `tools/position_reconciliation_check.py` — daily cron: read `trade_memory.json` for each trade today, correlate with NT8 log fills, report any trades where Python P&L disagrees with NT8 executions.

### Live-readiness checklist (before first real-money trade)

- [ ] B76, B77, B78 shipped and tested
- [ ] ATI disabled on live account in NT8
- [ ] vwap_pullback target_rr reduced or strategy disabled
- [ ] Aggregate daily cap active
- [ ] 5+ consecutive sim sessions with zero orphans
- [ ] 5+ consecutive sim sessions with NT8 trail-move OIFs verified against chandelier events
- [ ] Live-readiness team review of this audit + sign-off

---

## Summary verdict

**Current system is safe for sim paper trading. It is NOT safe for live trading.**

The trailing-stop gap (A1) alone makes the bot inferior to simply placing a fixed stop manually. Combine with state-reconciliation gaps (A2) and LIMIT-entry-hang risk (A4) and you have a system that looks functional in logs but produces silent orphan positions and phantom profits.

**Priority:** ship B76 before anything else. That one fix eliminates the highest-risk class of bug. After B76, the remaining issues become manageable with B77+B78 as follow-ons.

Do not deploy to live until the must-fix list is green and the live-readiness checklist is fully signed off.
