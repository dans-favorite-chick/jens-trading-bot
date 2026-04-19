# Action Plan v2.1 — Deltas

Updates to `phoenix_action_plan_v2_post_migration.md` generated during the
2026-04-18/19 pre-implementation verification sprint.

---

## Summary

**Bugs discovered: 5** (all on the OIF writer path, all latent for the entire bot's lifetime).

| ID | File / location | Severity | Status |
|---|---|---|---|
| B1 | `bridge/oif_writer.py:62` — ORDER ID never populated → no per-order fill ACKs ever generated | CRITICAL (latent) | Ship via P5 |
| B2 | `bridge/oif_writer.py:100` — CANCELALLORDERS has 14 semicolons, NT8 wants 12 → every CANCEL_ALL silently rejected | CRITICAL (latent) | Ship via P5b |
| B3 | `bridge/oif_writer.py:65,75,92,97` — stop orders use `STOP` but NT8 wants `STOPMARKET` → every NT8-side stop order parse-rejected; bot has been running with no NT8 stop protection | CRITICAL (latent) | Ship via P5b (highest priority) |
| B4 | NT8 design — `CANCELALLORDERS;;;;;;;;;;;;` no-args form cancels orders across ALL accounts (Sim101 + live brokerage + demo) | CRITICAL (design) | Ship via P5b (per-order CANCEL loop, not no-args form) |
| B5 | `bridge/oif_writer.py` — no single-order CANCEL action path exists | MEDIUM (gap) | Ship via P5b (add `CANCEL` action) |

**Design corrections to v2 plan: 4**

1. **P4 storage**: SQLite+WAL → JSON + JSONL (codebase has 9 JSON+atomic-replace precedents, 0 SQLite)
2. **P10 scope**: one universal `_on_trade_closed` handler → splits into **P10a** (3 trade-close consumers), **P10b** (bar-pipeline wiring for `sweep_watcher.track_pivot_break`), **P10c** (deferred to post-P2 for `tca_tracker.record_fill`)
3. **P6 already 80% built**: TickStreamer.cs v2.0 already has a `System.Threading.Timer` heartbeat + `C:\temp\mnq_data.json` fallback file with 1Hz mtime signal. Python-only scope (Option A) satisfies v2's P6 intent.
4. **Filename correction**: v2 plan references `MarketDataBroadcasterV3.cs`. That file does not exist; the actual tick broadcaster is `ninjatrader/TickStreamer.cs`.

**Estimate corrections**

| Item | v2 estimate | Revised | Delta |
|---|---|---|---|
| P4 (state persistence) | 4h | 2h | **−2h** |
| P6 (staleness detection) | 4h implied | 2.5h (Option A) | **−1.5h** |
| P10 (shadow module wiring) | 1.5h | 4-6h across P10a/b/c | **+2.5-4.5h** (but spread across days) |
| P21 line-ending sub-task | 30+ min implied | 15-20 min | **−10-15 min** |
| P5b (new) | — | 45-60 min | **+1h on Day 1** |
| Day 1 total | 1.5h | ~2.5h | **+1h** |
| Day 4 total | 5h | ~4h | **−1h** |
| Day 5 total | 5h | 6h | **+1h** |

**Net Week 1 effort change: roughly flat.** P5b (+1h Day 1) offsets P4 (−2h Day 4). P6 Option-A savings absorb the Day 5 overflow. P10 sub-items redistribute into Week 2 without breaking the overall week-1 shape.

**Items requiring live-data verification before final implementation**

1. **17:30 CT Sunday delta pass**: re-run `tools/verification_2026_04_18/test_01_market_ack.py` once CME re-opens (17:00 CT Sun) to observe the `FILLED;<qty>;<price>` ACK-content format and a sample `<Instrument>_<Account>_Position.txt` file. 15-min task. User-owned.
2. **B4 account-scoped CANCELALLORDERS test**: 10-min test of `CANCELALLORDERS;Sim101;;;;;;;;;;;;` (12 semicolons, account populated). If accepted, B4 fix simplifies from per-order CANCEL loop to a single account-scoped invocation. Can run tonight before P5b, during, or deferred.

---

## Tonight's implementation sequence (2026-04-19 evening)

Compressed Day-1+2+4 sprint. Target: items 1-6 complete by ~9 PM CT; items 7-8 contingent on 9 PM energy check.

| # | Item | Est | v2 original day | Rationale for tonight |
|---|---|---|---|---|
| 1 | **P5b — OIF writer correctness** (B2+B3+B4+B5 bundle) | 60-90 min | Day 1 | B3 fix is the top-priority safety item; bundled with B2/B4/B5 for atomic OIF-layer cleanup |
| 2 | **P3 — HALT enforcement** at strategy entry | 30 min | Day 1 | Single `if` at entrypoint; highest-ROI patch in plan |
| 3 | **P11 — AI filter safe REJECT default** on timeout | 15 min | Day 1 | One-line fix |
| 4 | **P20 — `in_cooldown()` side-effect split** | 15 min | Day 1 | Pure-query vs mutator split |
| 5 | **P7 — Daily-reset date marker** | 30 min | Day 1 | 15-line patch, disk-persisted `_current_date` |
| 6 | **P14 — Telegram HTML escape** | 30 min | Day 2 (low-priority) | Pulled forward — cheap alongside P5b's Telegram work |
| 7 | *P4b — Exit collision priority function* | 60 min | Day 4 | Pulled forward if capacity; `decide_exit()` single-decision function |
| 8 | *P1 — Atomic OIF writer* | 2h | Day 2 | Pulled forward if capacity; tempfile + fsync + `os.replace` retry loop |

Items 1-6 subtotal: ~3h target.
Items 7-8 conditional: ~3h more if both land.

**P5b goes first** because it fixes the critical B3 stop-loss bug (bot has been running with no NT8-side stops since day one). Every subsequent item benefits from having working order-execution primitives.

---

## Revised Week 1 schedule

Assumes tonight's items 1-6 land; items 7-8 contingent.

| Day | v2 planned items | Revised items (this plan) | Revised total |
|---|---|---|---|
| **Mon (Day 1)** | P3, P11, P20, P7 (1.5h) | **All landed tonight** — use Day 1 for overflow/polish + B4 account-scoped test + prod_bot signal_price sanity check | ~0-1h buffer |
| **Tue (Day 2)** | P1 atomic OIF writer (2h) | P1 (if not tonight) + P14 (already tonight) | 2-2.5h |
| **Wed (Day 3)** | P2 fill-ACK gate (3h) | **P2 fill-ACK gate** — state machine now 3-outcome per Phase 1 findings (ACK arrives / REJECTED / never arrives → check NT8 log). Uses corrected filename pattern `<Account>_<orderId>.txt`. | 3-3.5h |
| **Thu (Day 4)** | P4 (4h) + P4b (1h) = 5h | **P4 JSON + JSONL** (2h) + P4b (1h if not tonight) | 3h |
| **Fri (Day 5)** | P5 (2h) + P13 (0.5h) + P6 (1.5h) + P6b (1h) = 5h | P5 (2h) + P13 (0.5h) + **P6 Option A Python-only** (2.5h) + P6b (1h) | 6h |
| **Week 1 total** | ~16.5h | ~14.5-15.5h | slight savings |

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

## Open questions — need user input before tonight's implementation starts

### Q1. Tonight's pre-P5b diagnostic tests — run them first?

Two 10-minute tests that would simplify P5b implementation if run BEFORE:

- **B4 account-scoped CANCELALLORDERS test**: confirm whether `CANCELALLORDERS;Sim101;;;;;;;;;;;;` is accepted. If yes, P5b can ship an account-scoped cancel instead of per-order loop (saves complexity now, can still migrate later when B1/P5 ships).
- **17:30 CT FILLED-state delta pass**: observe actual `FILLED;<qty>;<price>` ACK + position-file format on live sim feed. Informs P2's state machine design (Day 3 work, but P5b lays foundation).

Both are non-blocking. Options:
- **(a)** Run both tonight before P5b (extra 20 min, lower risk)
- **(b)** Run just B4 test tonight (10 min, only B4 blocks P5b design)
- **(c)** Defer both to Monday morning (start P5b immediately, accept per-order loop design for B4)

### Q2. prod_bot signal_price verification

P10a's slippage derivation requires `market_snapshot["signal_price"]` to be populated at entry. **Confirmed in lab_bot** (line 363). **Not verified in prod_bot.** Options:
- **(a)** Add a 15-min prod_bot read tonight (quick grep, confirm entry path populates signal_price)
- **(b)** Defer to Day 7 P10a implementation window

### Q3. `phoenix_action_plan_v2_post_migration.md` — commit where?

This is the v2 plan document the deltas reference repeatedly (BEFORE/AFTER citations). Currently untracked at repo root. Your earlier guidance treated it as "counts as clean." Options:
- **(a)** Leave untracked (current state) — references in deltas doc don't resolve in repo browsing
- **(b)** Commit as part of tonight's verification corpus (self-contained audit trail)
- **(c)** Commit separately under `docs/` with a cleaner name

### Q4. Line-ending renormalize — tonight or Day 9?

Doing the line-ending cleanup **before** tonight's implementation work would:
- Stop the persistent `LF will be replaced by CRLF` warnings on every commit tonight
- Mean tonight's commits land as clean LF (no EOL noise in diffs)

Doing it **Day 9 as planned** means tonight's commits inherit current autocrlf=true behavior. Options:
- **(a)** Tonight, as an 0th step before P5b (15-20 min)
- **(b)** Day 9 as originally planned

### Q5. B3 rollout safety concern

Fixing B3 (`STOP` → `STOPMARKET`) activates NT8-side stop orders that have never been active. Strategies have been running for 697 trades with Python-side-only stop enforcement. Turning on NT8-side stops may:
- Trigger unexpected premature stop-outs if NT8's stop handling differs from Python's (e.g., NT8 uses last-trade price, Python uses tick close)
- Create double-stop races (both NT8 and Python try to flatten on breach)

Options:
- **(a)** Ship B3 as-is tonight; observe Monday; accept this is the "correct" state we should have been in from day one
- **(b)** Ship B3 tonight but with a feature flag (`ENABLE_NT8_SIDE_STOPS = False` initially in settings) — gradual rollout
- **(c)** Ship B3 but also disable Python-side stops once NT8-side is confirmed working (avoid double-stop)
- **(d)** Defer B3 to a dedicated careful review session; ship B2/B4/B5 tonight without B3

**Recommendation:** (b). Ship the fix with a kill-switch so rollback is instantaneous if behavior diverges.

---

*End of deltas doc. Live updates during tonight's implementation session logged at `tools/verification_2026_04_18/SESSION_2026_04_19.md`.*
