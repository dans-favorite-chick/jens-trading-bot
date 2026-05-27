# Action Plan v2.1 — Deltas

Updates to `phoenix_action_plan_v2_post_migration.md` generated during the
2026-04-18/19 pre-implementation verification sprint.

---

## Summary

- **Bugs discovered during verification: 7 total**
  - **B1**: `bridge/oif_writer.py` ORDER ID never populated (Priority P2, deferred)
  - **B2**: CANCEL_ALL semicolon count 15→13 (fix in P5b tonight)
  - **B3**: STOP→STOPMARKET at all 4 order-type sites (fix in P5b tonight)
  - **B4**: CANCELALLORDERS cross-account risk (fix in P5b tonight)
  - **B5**: No single-order CANCEL function (add in P5b tonight)
  - **B6**: `file_fallback_poller` doesn't bump `nt8_last_tick_time` (fix tonight after P5b)
  - **B7**: Bridge conflates heartbeat/tick timestamps — silent-stall blindspot (real incident 2026-04-16, fix tonight after B6)

- **Design corrections to v2 plan: 4**
  - P4 SQLite → JSON (codebase already decided)
  - P10 scope split into P10a/b/c
  - P6 already ~80% built (TickStreamer.cs has Timer + fallback mtime)
  - Filename correction: TickStreamer.cs NOT MarketDataBroadcasterV3.cs

- **Estimate corrections:**
  - P10: 1.5h → 4-6h
  - P4: 4h → 2h
  - P6: 4h → 3h (pure Python, zero C# compile cycle)
  - P21: 30min → 15-20 min
  - P5b (new): +60-90 min (was not in original plan)

- **Net Week 1 effort change: roughly flat** (additions from P5b + B6/B7 roughly offset by P4 and P6 reductions).

- **Items requiring verification against live data before final implementation:**
  - FILLED-state delta pass — **COMPLETE** (verified tonight)
  - B4 account-scoped CANCELALLORDERS — will verify during P5b live smoke test

### Bug detail table

| ID | File / location | Severity | Status |
|---|---|---|---|
| B1 | `bridge/oif_writer.py:62` — ORDER ID never populated → no per-order fill ACKs ever generated | CRITICAL (latent) | Ship via P2 (deferred from P5, carried into Week 1 Day 3) |
| B2 | `bridge/oif_writer.py:100` — CANCELALLORDERS has 14 semicolons, NT8 wants 12 → every CANCEL_ALL silently rejected | CRITICAL (latent) | Ship via P5b tonight |
| B3 | `bridge/oif_writer.py:65,75,92,97` — stop orders use `STOP` but NT8 wants `STOPMARKET` → every NT8-side stop order parse-rejected; bot has been running with no NT8 stop protection | CRITICAL (latent) | Ship via P5b tonight (highest priority) |
| B4 | NT8 design — `CANCELALLORDERS;;;;;;;;;;;;` no-args form cancels orders across ALL accounts (Sim101 + live brokerage + demo) | CRITICAL (design) | Ship via P5b tonight |
| B5 | `bridge/oif_writer.py` — no single-order CANCEL action path exists | MEDIUM (gap) | Ship via P5b tonight (add `CANCEL` action) |
| B6 | `bridge/bridge_server.py:500` — `file_fallback_poller` broadcasts fallback ticks but never bumps `self.nt8_last_tick_time` → `stale_watcher` logs "NT8 data stale" forever even while fallback healthy | LOW (cosmetic, will turn into Telegram spam when alerts are wired) | Ship tonight, slot 2 (after P5b) |
| B7 | `bridge/bridge_server.py:188-192` — heartbeat and tick both bump the same `nt8_last_tick_time`; silent tick-stall (NT8 frozen but TCP alive) is undetectable at the bridge layer. Reproduced in the 2026-04-16 3h15m stall incident | MEDIUM (real operational incident) | Ship tonight, slot 3 (after B6) |

---

## Tonight's implementation sequence (2026-04-19 evening)

**All 11 items ship tonight.** Strict discipline: pytest + individual commit after every item.

| # | Item | Est | Notes |
|---|---|---|---|
| 1 | **P5b — OIF correctness** (B2+B3+B4+B5 bundle) | ~60-90 min | B3 stop-loss fix is top priority; bundled with B2/B4/B5 for atomic OIF-layer cleanup |
| 2 | **B6 — `file_fallback_poller` timestamp fix** | ~10 min | 1-line fix + pytest coverage; `self.nt8_last_tick_time = time.time()` after fallback broadcast |
| 3 | **B7 — heartbeat/tick timestamp split** | ~45 min | Split `nt8_last_tick_time` into `nt8_last_heartbeat_time` + `nt8_last_tick_time`; `stale_watcher` emits distinct "connection dead" vs "silent stall" signals; 4 tests |
| 4 | **P3 — HALT enforcement** at strategy entry | ~30 min | Single `if` at strategy-eval entrypoint; highest-ROI patch in plan |
| 5 | **P11 — AI filter safe REJECT default** on timeout | ~15 min | One-line safe-default fix |
| 6 | **P20 — `in_cooldown()` side-effect split** | ~15 min | Pure-query vs mutator split |
| 7 | **P7 — Daily reset date marker** | ~30 min | 15-line patch, disk-persisted `_current_date` |
| 8 | **P14 — Telegram HTML escape** | ~30 min | Escape in all `send*()` entrypoints; fixes past corrupted notification incident |
| 9 | **P4b — Exit collision priority function** | ~60 min | `decide_exit()` single-decision function resolves simultaneous stop/target/time/trail hits |
| 10 | **P1 — Atomic OIF writer** | ~2h | tempfile + fsync + `os.replace` retry loop; precedes P2's fill-ACK gate |
| 11 | **P21 — EOL renormalize** (137-file sweep) | ~15-20 min | Isolated commit, ships LAST so feature diffs on items 1-10 stay clean |

**Strict discipline:** pytest + individual commit after every item. P21 ships last so feature diffs on items 1-10 stay clean and the renormalize commit is atomic + isolated for easy `git blame --ignore-rev` handling.

**P5b goes first** because B3 fixes the critical stop-loss bug (bot has been running with no NT8-side stops since day one). Every subsequent item benefits from working order-execution primitives.

**B6 + B7 ship in slots 2-3** because the silent-stall blindspot (B7) is a real operational bug that caused the 2026-04-16 3h15m outage; no dependencies on later items.

---

## Revised Week 1 schedule

Tonight compresses what was originally Day 1 + Day 2 + Day 4 into a single session. Remaining days open up.

| Day | v2 planned items | Revised items (this plan) | Revised total |
|---|---|---|---|
| **Tonight (Sun evening)** | — | **11-item sprint** (see table above): P5b → B6 → B7 → P3 → P11 → P20 → P7 → P14 → P4b → P1 → P21 | ~6-7h |
| **Mon (Day 1)** | P3, P11, P20, P7 (1.5h) | **All landed tonight.** Day 1 reserved for overflow / polish / prod_bot signal_price sanity check / live B4 account-scoped test | 0-1h buffer |
| **Tue (Day 2)** | P1 atomic OIF writer (2h) | **P1 landed tonight.** Day 2 reserved for overflow / PR review / any smoke tests deferred from tonight | 0-1h buffer |
| **Wed (Day 3)** | P2 fill-ACK gate (3h) | **P2 fill-ACK gate** — state machine 3-outcome per Phase 1 findings (ACK arrives / REJECTED / never arrives → check NT8 log). Uses corrected filename `<Account>_<orderId>.txt`. | 3-3.5h |
| **Thu (Day 4)** | P4 (4h) + P4b (1h) = 5h | **P4 JSON + JSONL** (2h). P4b already landed tonight. | 2h |
| **Fri (Day 5)** | P5 (2h) + P13 (0.5h) + P6 (1.5h) + P6b (1h) = 5h | P5 (2h) + P13 (0.5h) + **P6 Option A Python-only** (2.5h, now including silent-stall breaker rule enabled by B7) + P6b (1h) | 6h |
| **Week 1 total** | ~16.5h | ~12-13h | **~4h saved** by the tonight-compression + P4/P6 estimate corrections |

Week 2 gains: P10 scope work absorbs 2.5-4.5 extra hours (P10a: 2-3h Day 7; P10b: 1.5-2h; P10c: 0.5-1h post-P2). Net Week 2 effort still fits the original schedule with minor shuffling.

---

## Production bugs surfaced during verification

### B1. ORDER ID never populated (bridge/oif_writer.py:62)

Severity: CRITICAL (latent). Not in v2 plan as a standalone item because it was assumed to be working. Discovered during Phase 1 field-index test.

**Impact:** Bot has never received per-order fill ACKs. All trade outcome inference has been from aggregate position file only. Partial fills undetectable. P2's design now has a compounded justification: it's not "add better fill tracking," it's "enable fill tracking that currently does not exist."

**Fix:** Ship as part of P5 (ClOrdID generation) since ClOrdID IS the ORDER ID. Item already in v2 plan — re-tag in docs that P5 is a bug fix AND a feature.

### B2. CANCEL_ALL emits wrong semicolon count (bridge/oif_writer.py:100)

Severity: CRITICAL (latent). Every CANCEL_ALL invocation has been silently rejected by NT8 with error `invalid # of parameters, should be 13 but is 15`.

**Impact:** Any risk-manager hard-flatten on daily-loss-limit hit, HALT escalation, or manual kill-switch has been a no-op. Positions stayed open; bot believed flat. Exactly the class of phantom-state bug Codex v2 Tier-1'd.

**Fix:** **NOT just a format correction.** See B4 — the corrected no-args form cancels across all NT8-connected accounts including live brokerage. Real fix is per-order CANCEL loop (requires B1/P5). Ship as part of P5b bundle on Day 1.

### B3. Every stop order uses invalid ORDER TYPE `STOP` (bridge/oif_writer.py:65, 75, 92, 97)

Severity: CRITICAL (latent). NT8 accepts ORDER TYPE `STOPMARKET` (verified in Phase 1 test_03); does NOT accept `STOP`. Writer uses `STOP` in four places — every OCO stop leg, every break-even stop.

**Impact:** Every stop order the writer has emitted has been parse-rejected by NT8 before reaching Sim101. Bot has been running with OCO brackets that have a target leg but **no stop leg**. Strategy-layer stops are enforced Python-side only (tick-breach detection → CLOSEPOSITION). On bridge/WebSocket outage, any tick-path latency, or bot crash after open-position: naked position, no NT8 safety net. This is the exact "phantom position" / "catastrophic stops" class Codex v2 flagged, and it's been latent the entire time. The bot has only survived because Python-side stops have worked most of the time.

**Fix:** `STOP` → `STOPMARKET` in four locations. Possibly add `STOPLIMIT` too. 15 minutes including a runtime verification test. Bundle into P5b on Day 1. **Tonight's top priority.**

### B4. CANCELALLORDERS no-args form affects ALL NT8-connected accounts

Severity: CRITICAL (design), discovered 2026-04-19 test_05. The docs-correct `CANCELALLORDERS;;;;;;;;;;;;` form cancels orders across every account NT8 has a connection to — not just Sim101. Empirical: test_05 log line `Cancel all orders account='1590711'` shows a real brokerage account was affected. `DEMO5880030` also affected.

**Impact:** a naïve B2 fix (just correcting the semicolon count) would make the bot's risk-manager hard-flatten nuke orders on user's real brokerage account during a HALT event. The currently-broken form (silent no-op) is arguably safer than the literally-correct form.

**Fix:** **Per-order CANCEL loop**, not no-args CANCELALLORDERS. Once B1/P5 gives us ORDER ID tracking, the hard-flatten iterates intents with status ∈ {SUBMITTED, ACCEPTED, WORKING} and issues `CANCEL;;;;;;;;;;<order_id>;;` for each — precisely scoped to bot-originated orders. Pair with `CLOSEPOSITION` per tracked instrument for full flatten (positions after fill). Total effort: 30 min once B1/P5 is in place.

**Follow-up test (not blocking, 10 min):** verify whether `CANCELALLORDERS;Sim101;;;;;;;;;;;;` (12 semis, account populated) works as an account-scoped form. If yes, simplifies B4 significantly. Schedule alongside 17:30 CT FILLED-state verification pass — or tonight before P5b.

### B5. Writer has no single-order CANCEL path (bridge/oif_writer.py)

Severity: MEDIUM (capability gap, not bug). No action handler in `write_oif()` emits the single-order CANCEL form (`CANCEL;;;;;;;;;;<order_id>;;`). Required by P2 (fill-ACK correlation), P5 (ClOrdID tracking), and B4's per-order cancel loop. Trivially addable alongside the B1–B4 fixes.

**Fix:** Add `elif action == "CANCEL"` handler emitting the correct single-order CANCEL format with `trade_id` populated at field 10. 10 min. Bundle with P5b.

---

## P5b consolidated (Day 1 / Tonight)

B2 + B3 + B4 + B5 ship together as a single **OIF writer correctness pass** (~60-90 min including live tests):

1. Remove `{ACCOUNT};{INSTRUMENT}` from CANCELALLORDERS line (→ 12 semis)
2. Change `STOP` → `STOPMARKET` in 4 places ([lines 65, 75, 92, 97](../bridge/oif_writer.py))
3. Add a new `CANCEL` action path to `write_oif()` emitting `CANCEL;;;;;;;;;;<trade_id>;;`
4. Replace `CANCEL_ALL` callsites with a per-order cancel loop. **Stopgap** before B1/P5 fully lands: hard-flatten uses `CLOSEPOSITION` per tracked instrument instead of CANCELALLORDERS. **Preferred** once B1 ships: iterate active intents and CANCEL each.
5. Live runtime verification: send corrected OIFs, confirm NT8 log shows `processing` rather than `invalid`.

**Prerequisite test (tonight, before P5b):** 10-min B4 account-scoped test (`CANCELALLORDERS;Sim101;;;;;;;;;;;;`). If accepted by NT8, the per-order loop in step 4 can be skipped in favor of the account-scoped form.

Day 1 grows from 1.5h → ~2.5h including P5b. Entirely worth it — fixes every silent-failure class on the order-execution path simultaneously.

---

## Phase 3 deltas — P10 scope correction

The v2 plan treats `_on_trade_closed` as a single universal wiring point for all 5 shadow modules. Audit shows this is only correct for 3 of 5. P10 splits into three sub-items:

### P10a. Wire trade-close consumers (original P10 scope, corrected)

- `decay_monitor.record_trade` — YELLOW (3 key renames from trade dict)
- `circuit_breakers.record_slippage` — ORANGE (derive slippage from `entry_price` vs `market_snapshot["signal_price"]`, direction-adjusted)
- `circuit_breakers.record_trade_outcome` — YELLOW (1 rename)

**Estimate: 2-3h** (was 1.5h "six-line patch"). Scope correction: not six lines. New `_on_trade_closed(trade)` method on BaseBot, called from the end of `_exit_trade` after `positions.close_position()`. Three `record_*` calls with a helper for slippage derivation.

Caveat: slippage derivation requires `market_snapshot["signal_price"]` to be populated by the entry path. Confirmed populated in lab_bot's `_paper_enter` (line 363). **Not verified in prod_bot's entry path** — 15-min sanity check during P10a implementation (or tonight if time allows).

Ship on Day 7 (Tuesday Week 2) as planned. Day 7 total grows from 3.5h → ~5h.

### P10b. Wire `sweep_watcher.track_pivot_break` — NEW item, not in v2

`sweep_watcher.track_pivot_break` is NOT a trade-close consumer. It fires when `swing_detector` detects price breaking a prior pivot — a bar-pipeline event, not a trade event. Wiring it into `_on_trade_closed` is the wrong pipeline.

Real wiring: in the bar-close path where `swing_detector` is already invoked, feed its pivot-break output into `sweep_watcher.track_pivot_break()`.

**Estimate: 1.5-2h.** Add as Tier 2 item for somewhere in Week 2.

### P10c. Wire `tca_tracker.record_fill` — DEFERRED until P2 lands

`record_fill` requires `time_to_fill_ms` = time from OIF write to NT8 fill confirmation. This data doesn't exist until B1 (populate ORDER ID) and P2 (poll outgoing/`<Account>_<orderId>.txt` for SUBMITTED→FILLED transitions) are both in place.

Before P2: wiring `record_fill(time_to_fill_ms=0)` is technically correct but produces useless TCA data (slippage baselines compute against zero-latency fills). Not worth shipping.

After P2: the fill-ACK poller in P2 has the timestamps. Wire `record_fill` from there, not from `_on_trade_closed`. Different lifecycle: fill events can be multiple per trade (partial fills), record them as they happen.

**Estimate: 0.5-1h** post-P2 landing. Schedule Day 8 or 9 of Week 2.

### Related finding: decay_monitor baselines are unset

`DecayMonitor.StrategyPerformance.baseline_backtest_sharpe` defaults to 0.0. Alert thresholds in the monitor compare rolling Sharpe against this baseline — with baseline=0, thresholds are meaningless. P22 (decay alerts) needs a prerequisite sub-item: **seed baselines from historical validation data** (the 697-trade history at least gives us per-strategy baseline Sharpe to populate). 30 min to script, separate from P22's alert logic.

---

## Phase 4 deltas — P4 state persistence architecture

The v2 plan specifies SQLite+WAL with `state_writer` asyncio task and `BEGIN IMMEDIATE` transactions. Audit of actual access patterns shows this is over-engineered. **Recommended: JSON-file-per-state + JSONL history**, matching existing codebase conventions.

### Why JSON wins for this codebase

**Access patterns:** all hot-path position reads use in-memory state (`self.position`). Zero disk reads per tick. Writes happen only on state-change events: ~30/day max in lab bot, single-digit in prod. Single asyncio writer, no concurrency. Reconciliation is a 2-way compare with NT8's aggregate position file — no joins, no queries, no aggregations.

**Codebase conventions:** 9 existing modules use JSON + tempfile + os.replace atomic write (decay_monitor, tick_aggregator, equity_tracker, trade_memory, counter_edge, expectancy_engine, execution_quality, strategy_tracker, no_trade_fingerprint). **Zero modules use SQLite.** Introducing SQLite for P4 would be the first relational-DB dependency. Append-only history has 3 JSONL precedents (history_logger, tca_tracker, audit_log).

**What SQLite would give us that JSON doesn't:**
- Relational queries — we have none (2-way compare only)
- Multi-row atomic transactions — each mutation is independent
- Indexed lookups over millions of rows — active set is ≤10 rows
- Concurrent reader/writer coordination — single writer, in-memory reads
- Durability via WAL — achieved by tempfile + fsync + os.replace (P1)

**What JSON gives us that SQLite doesn't:**
- Consistency with 9-module precedent
- Human-readable debug state (`cat state/active.json`)
- Zero schema migrations (unknown keys just ignored)
- No new machinery (no cursors, connections, DDL, PRAGMA)

### Proposed P4 architecture

```
state/
  active.json           — position (if any) + active intents map
logs/
  intent_history.jsonl  — append-only intent lifecycle events
```

`state/active.json`: ~2-5KB, rewritten atomically on every mutation.
`logs/intent_history.jsonl`: append-only, one line per intent state transition (including final Filled/Cancelled/Rejected events).

### Revised P4 estimate

Original v2: **4h** (SQLite WAL + schema + state_writer asyncio task + BEGIN IMMEDIATE transactions + reconcile()).

Revised: **~2h**
- `StateStore` class with atomic save/load + jsonl append: 30 min
- Hook into open_position / close_position / scale_out_partial / move_stop_to_be: 30 min
- Hook into OIF-send for intent tracking (overlaps with P5): 30 min
- `reconcile_on_startup()` function: 30 min
- Integration test (kill mid-write, restart, verify recovery): included

**Savings: ~2h.** Day 4 goes 5h → 3h. With P4b (exit collision, 1h unchanged), Day 4 total: 4h (was 5h).

### Critical reconciliation nuance — NT8 wipes outgoing/ on restart

Phase 1 doc fetch surfaced: **"Contents of this folder will be deleted when the NinjaTrader application is restarted."** Not in v2 plan.

Implications for P4's `reconcile()`:
- If Python crashed AND NT8 was restarted: per-order ACK files are gone. Intents last seen WORKING have no ACK to confirm final state.
- NT8's aggregate position file (`<Instrument>_<Account>_Position.txt`) survives NT8 restart — use as ground truth.
- Intent-in-Python-no-ACK-in-outgoing logic: query position file first. If position exists, intent was fulfilled; reconstruct Filled state from position file's AvgFillPrice. If position is flat, intent was either cancelled or never placed; mark UNKNOWN_REQUIRES_HUMAN.

Apply this regardless of storage choice.

---

## Phase 5 deltas — P6 C# heartbeat scope reduction

### Filename correction

v2 plan references `MarketDataBroadcasterV3.cs`. That file does not exist. Actual broadcaster is `TickStreamer.cs` at `ninjatrader/TickStreamer.cs` (repo) and `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\TickStreamer.cs` (installed). Sibling `SiM_TickStreamer.cs` handles weekend/playback.

### Two v2 plan premises invalidated by code read

1. **Heartbeat timer already exists.** `TickStreamer.cs` line 50, 84 has a `System.Threading.Timer` firing every 3s; line 186-200 is `HeartbeatCallback` that sends `{"type":"heartbeat","ts":"..."}` over TCP. Has been shipping since v2.0 of the indicator.
2. **File-that-acts-as-heartbeat already exists.** `C:\temp\mnq_data.json` is written on every OnBarUpdate (throttled 1/sec) at line 136, wrapped in try/catch. Its mtime already serves as "last tick freshness" — exactly what v2 plan P6 layer (3) asks for. Python just needs to consume it as a staleness signal.

### Revised P6 approach — Option A confirmed

**Option A (APPROVED): Python-only, use existing fallback file as heartbeat.**

No C# changes. Python watchdog polls `C:\temp\mnq_data.json` mtime. If >10s old during RTH → alert. Distinguishing "NT8 dead" from "tick-stall" is diagnostic, not operational — operator response is the same for both.

**Estimate: 2.5h**
- websockets ping_interval=10 + ping_timeout=10 + asyncio.wait_for: 30 min
- Staleness watchdog task (both TCP heartbeat timestamp + fallback file mtime): 45 min
- SIO_KEEPALIVE_VALS ioctl on Windows (low value on loopback, included for completeness): 30 min
- Bridge ring buffer tick timestamps + replay staleness check: 45 min

**Option B** (Timer-thread heartbeat file for diagnostic granularity) **deferred indefinitely** per approval. Revisit only if real-incident experience shows mtime staleness is insufficient to diagnose failure modes. Not building speculatively.

### Day 5 schedule impact

v2 plan: P5 (2h) + P13 (0.5h) + P6 (1.5h) + P6b (1h) = 5h.

Option A: P6 → 2.5h → Day 5 = 6h. Still fits, slight overflow (acceptable).

---

## Phase 6 deltas — P21 line-ending scope refined

### Current state (audit 2026-04-19)

- `core.autocrlf = true` at repo level (source of all `LF will be replaced by CRLF` warnings this session)
- No `.gitattributes` file
- 155/159 files already clean LF
- 3 Python files edited this session have `w/crlf` (autocrlf's doing): `bots/base_bot.py`, `config/settings.py`, `tools/ema_analysis.py`
- 1 JSONL file has mixed EOLs: `memory/audit_log.jsonl`
- All `.cs`, `.bat`, `.yaml` files are clean LF and working fine against NT8 / cmd.exe empirically

### Recommended `.gitattributes`

```gitattributes
* text=auto eol=lf

*.png *.jpg *.jpeg *.gif *.ico *.pdf binary
*.sqlite *.db *.parquet binary
*.pyc *.pyo *.zip *.nt8bk binary
```

Single simple rule: LF everywhere for text files, binary never-touched. v2 plan's `* text=auto eol=lf` matches exactly. No per-extension CRLF overrides needed — empirical state shows .cs / .bat already work fine as LF.

### Exact command sequence (for P21 Day 9)

```bash
git status --short                              # MUST be empty
# write .gitattributes as above
git add .gitattributes
git commit -m "chore: add .gitattributes (LF default, binary overrides)"
git config core.autocrlf false
git add --renormalize .
git status                                      # expect 4 files
git commit -m "chore: normalize line endings per .gitattributes"
```

Order matters: `.gitattributes` committed **before** `--renormalize`; `core.autocrlf false` between the two commits.

### Expected diff

- 4 files modified (bots/base_bot.py, config/settings.py, tools/ema_analysis.py, memory/audit_log.jsonl)
- Pure EOL changes, zero content changes (`git diff --cached --ignore-all-space` = empty)

### NinjaScript editor caveat

If anyone edits `.cs` files directly in NT8's NinjaScript editor, that editor writes CRLF on save. Next `git commit` re-normalizes to LF. Visible as "modified" in status until committed — expected, harmless. If it becomes painful in practice, add `*.cs text eol=crlf` override later.

### Revised P21 line-ending sub-task estimate

v2 plan P21 total was 2h including pre-commit setup + test runner standardization + this line-ending work. The line-ending sub-task alone: **15-20 min** (was implicit 30+ min). The repo is already 97% clean; this is a light lift.

---

## Open questions — need user input before sprint starts

### Q1. B4 account-scoped CANCELALLORDERS test (resolved — "during P5b smoke test")

Summary line item #2 locks this in: B4 account-scoped form will be verified during P5b's live smoke test rather than as a pre-P5b diagnostic. P5b implementation should code the per-order CANCEL loop regardless; the account-scoped form is an optional simplification if the smoke test confirms NT8 accepts it.

**Action:** No pre-P5b test. Proceed directly to P5b; verify B4 scoping live.

### Q2. prod_bot signal_price verification — tonight or Week 2?

P10a's slippage derivation requires `market_snapshot["signal_price"]` to be populated at entry. **Confirmed in lab_bot** (line 363). **Not verified in prod_bot.** Options:
- **(a)** 15-min prod_bot read tonight between items (quick grep, confirm entry path populates signal_price)
- **(b)** Defer to Day 7 P10a implementation window

**Recommendation:** (b) — P10a is Week 2; no need to rush verification tonight when all 11 sprint items already fill the session.

### Q3. `phoenix_action_plan_v2_post_migration.md` — commit where?

This is the v2 plan document the deltas reference repeatedly (BEFORE/AFTER citations). Currently untracked at repo root. Options:
- **(a)** Leave untracked — references in deltas doc don't resolve in repo browsing
- **(b)** Commit as part of tonight's verification corpus (self-contained audit trail)
- **(c)** Commit separately under `docs/` with a cleaner name

**Recommendation:** (b) — committing under `docs/ACTION_PLAN_V2.md` or similar keeps the audit trail resolvable. Not blocking tonight's sprint either way.

### Q4. Line-ending renormalize — RESOLVED

**Decision (2026-04-19 evening):** Ships tonight as item 11 of 11. Lands AFTER all feature commits so feature diffs stay clean and the renormalize commit is atomic + isolated for easy `git blame --ignore-rev` handling.

### Q5. B3 rollout safety — needs decision before P5b

Fixing B3 (`STOP` → `STOPMARKET`) activates NT8-side stop orders that have never been active. Strategies have been running for 697 trades with Python-side-only stop enforcement. Turning on NT8-side stops may:
- Trigger unexpected premature stop-outs if NT8's stop handling differs from Python's (e.g., NT8 uses last-trade price, Python uses tick close)
- Create double-stop races (both NT8 and Python try to flatten on breach)

Options:
- **(a)** Ship B3 as-is tonight; observe Monday; accept this is the "correct" state we should have been in from day one
- **(b)** Ship B3 tonight but with a feature flag (`ENABLE_NT8_SIDE_STOPS = False` initially in settings) — gradual rollout
- **(c)** Ship B3 but also disable Python-side stops once NT8-side is confirmed working (avoid double-stop)
- **(d)** Defer B3 to a dedicated careful review session; ship B2/B4/B5 tonight without B3

**Recommendation:** (b). Ship the fix with a kill-switch so rollback is instantaneous if behavior diverges.

**Required before P5b starts.**

### Q6. B7 test scope — pure unit tests or NT8-dependent integration?

B7's `stale_watcher` correctness depends on subtle timing around heartbeat vs tick timestamps. Options:
- **(a)** Pure unit tests only (inject fake timestamps, verify decisions) — fast CI, no NT8 dependency
- **(b)** Unit tests + manual NT8 integration check (kill NT8 mid-session, verify stale_watcher distinguishes "connection dead" from "silent stall")

**Recommendation:** (a) for tonight's sprint commit. Manual NT8 integration goes on the Monday pre-open smoke-test checklist.

---

*End of deltas doc. Live updates during tonight's implementation session logged at `tools/verification_2026_04_18/SESSION_2026_04_19.md`.*
