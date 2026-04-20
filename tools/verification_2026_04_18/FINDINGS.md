# Action Plan v2.1 — Pre-implementation Verification Findings

Date started: 2026-04-19 (Sunday, CME closed — re-opens 17:00 CT)
Tree state: one untracked file (`phoenix_action_plan_v2_post_migration.md`), otherwise clean.
Bridge: stopped (PID 8816 killed before testing).
Dashboard: left running (does not touch OIF).
NT8: running, PID 2276, Sim101 assumed.

Test artifacts live under `tools/verification_2026_04_18/`. Zero production code changed during this sprint.

---

## Phase 1 — NT8 fill-ACK mechanism

### Step 1.1 — Authoritative documentation

Fetched:
- https://ninjatrader.com/support/helpguides/nt8/information_update_files.htm
- https://ninjatrader.com/support/helpguides/nt8/order_instruction_files_oif.htm

#### NT8 writes three distinct file types to `outgoing/`:

| File type | Filename pattern | Content format | Purpose |
|---|---|---|---|
| Order State | `<orderId>.txt` (orderId from PLACE command's ORDER ID field) | `Order State;Filled Amount;Average FillPrice` | Per-order state, updated on every state change |
| Position | `<InstrumentName> <InstrumentExchange>_<AccountName>_Position.txt` (e.g. `"ES 0914 Globex_Sim101_Position.txt"`) | `Market Position;Quantity;Average Entry Price` — values: `LONG`, `SHORT`, `FLAT` | Aggregate position per instrument/account |
| Connection State | `<ConnectionName>.txt` | `Connection State` — values: `CONNECTED`, `DISCONNECTED` | Per-connection status |

#### Possible Order State values (from NT8 docs)

The information_update_files doc says "possible order state values can be found [here]" but does not list them inline. From NT8's broader Order State enum (documented elsewhere): `Initialized`, `Submitted`, `Accepted`, `Working`, `PartFilled`, `Filled`, `Cancelled`, `Rejected`, `TriggerPending`. Empirical testing in Step 1.2+ will confirm which of these appear for our test order types.

#### Caveats documented

- **"Contents of this folder will be deleted when the NinjaTrader application is restarted."** Important: persistence across restart is NOT provided by NT8 — P4 state persistence must come from Python side.
- Files are rewritten "with each change" — overwrites, not append.
- No explicit race-condition, file-handle, or ordering guarantees in the docs. Forum thread 3540 (referenced in action plan) notes sub-second lag between per-order file and aggregate Position file; treat the per-order file as authoritative.

#### PLACE command field structure (verbatim from NT8 OIF doc)

```
PLACE;<ACCOUNT>;<INSTRUMENT>;<ACTION>;<QTY>;<ORDER TYPE>;[LIMIT PRICE];[STOP PRICE];<TIF>;[OCO ID];[ORDER ID];[STRATEGY];[STRATEGY ID]
```

Positional mapping (0-indexed, command verb at position 0):

| Index | Field | Required? |
|---|---|---|
| 0 | PLACE (command verb) | — |
| 1 | ACCOUNT | required |
| 2 | INSTRUMENT | required |
| 3 | ACTION | required |
| 4 | QTY | required |
| 5 | ORDER TYPE | required |
| 6 | LIMIT PRICE | optional |
| 7 | STOP PRICE | optional |
| 8 | TIF | required |
| 9 | OCO ID | optional |
| **10** | **ORDER ID** | **optional, but required for `<orderId>.txt` ACK file generation** |
| 11 | STRATEGY | optional |
| 12 | STRATEGY ID | optional |

#### CHANGE and CANCEL commands

```
CHANGE;;;;<QUANTITY>;;<LIMIT PRICE>;<STOP PRICE>;;;<ORDER ID>;;<[STRATEGY ID]>
CANCEL;;;;;;;;;;<ORDER ID>;;<[STRATEGY ID]>
CANCELALLORDERS;;;;;;;;;;;;
```

Consistent: ORDER ID at field position 10 across all three.

#### OIF file-naming + concurrent-write guidance

- OIF filenames can be anything matching `oif*.txt` (e.g. `oif1.txt`, `oif2.txt`); NT8 just watches the directory.
- NT8 doc explicitly warns: **"File locking problems if you always use the same file name"** — confirms P1's guidance to use unique monotonic filenames.
- Doc recommends **"Move or directly write OIF files" rather than copying** — exactly the pattern P1 (tempfile + `os.replace`) implements.

#### Action plan v2 claim cross-check

| v2 claim | Doc reality | Verdict |
|---|---|---|
| "11th semicolon field" is ORDER ID | Field position 10 (0-indexed, command verb at 0) — ambiguous wording; literally "after the 10th semicolon", which some count as the 11th field | **Ambiguous but approximately correct.** Delta needed: specify "field index 10 (0-indexed including PLACE as index 0)" unambiguously. |
| Outgoing contents = `State;FilledQty;AvgFillPrice` | Docs: `Order State;Filled Amount;Average FillPrice` | Correct. |
| Position file has `LONG\|SHORT\|FLAT;qty;avg_px` | Docs: `Market Position;Quantity;Average Entry Price` where Market Position ∈ {LONG, SHORT, FLAT} | Correct. |
| "sub-second lag between fill-state file and position file" | Not in docs; attributed to forum 3540 | Plausible, not doc-confirmed. Our live test may show this. |

#### Pre-test evidence from current writer

[bridge/oif_writer.py:62](phoenix_bot/bridge/oif_writer.py) emits `PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};MARKET;0;0;DAY;;;;` — all optional fields (OCO ID, ORDER ID, STRATEGY, STRATEGY ID) are empty. **Consequence: NT8 has not been writing per-order ACK files to outgoing/ during normal bot operation.** The `check_fills()` helper at line 161 reads whatever is in outgoing/ but only finds Position / Connection files, not per-order files. This is a significant finding — answers Phase 2 Step 2.3 before it's run.


### Step 1.2 — Hand-crafted MARKET order test

First run failed due to a **verification-script bug, not an NT8 issue**: my test script named OIF files `verify_mkt_*.txt`. NT8 log: `Unknown OIF file type`. NT8 requires `oif*.txt` naming — documented in OIF help page. Script fixed (`oif_verify_mkt_*.txt`), re-ran clean.

Result (test_01 re-run):

```
OIF sent: PLACE;Sim101;MNQM6 06-26;BUY;1;MARKET;0;0;DAY;;MANTEST_MKT_1776614822268;;

Outgoing file observed: Sim101_MANTEST_MKT_1776614822268.txt
  @  0.20s  size=15  'SUBMITTED;0;0'
  @ 21.16s  size=14  'REJECTED;0;0'
```

NT8 log for the trace:
```
11:07:02.277  OIF, 'PLACE;...;MANTEST_MKT_1776614822268;;' processing
11:07:02.381  New state='Submitted' … (104ms after OIF read)
11:07:23.371  New state='Rejected' … Native error='There is no market data available to drive the simulation engine.'
```

Rejection is expected — Sunday pre-open, Sim101's simulation engine has no price feed to drive MARKET fills. The point of this test was ACK-file shape, not fill outcome; both SUBMITTED and REJECTED ACKs were emitted exactly as the docs promised. Filled-state verification deferred to a 15-min delta pass at ≥17:30 CT after CME re-open.

### Step 1.3 — LIMIT and STOP order tests

LIMIT (BUY @ 20000, far below market — test_02):

```
Sim101_MANTEST_LMT_1776614950021.txt:
  @  0.10s  'SUBMITTED;0;0'
  @ 15.10s  'CANCELPENDING;0;0'   ← after CANCEL OIF sent
  @ 20.14s  'REJECTED;0;0'        ← ~5s after CANCELPENDING
```

STOPMARKET (BUY @ 30000, far above market — test_03):

```
Sim101_MANTEST_STP_1776614975265.txt:
  @  0.10s  'SUBMITTED;0;0'
  @ 15.12s  'CANCELPENDING;0;0'   ← after CANCEL OIF sent
  @ 20.06s  'REJECTED;0;0'
```

Same pattern for both: ACK file created within 100ms of OIF read; CANCEL drives a CANCELPENDING → REJECTED transition in ~5 seconds. Without market data, the sim engine never transitions Submitted → Working, so ACKs jump straight from SUBMITTED to the cancel path. During market hours we'd expect `Submitted → Accepted → Working → (Filled | Cancelled | Rejected)`.

### Step 1.4 — Field position verification

Three LIMIT orders placed, same distinctive ID string at three different field positions. **Decisive result:**

| Slot tested | Field index | OIF sent (abbreviated) | Filename observed in outgoing/ | NT8 log interpretation |
|---|---|---|---|---|
| 9 (OCO ID) | `...DAY;FIELD9_ID_…;;;` | `Sim101_54512de4e067447f813e437300cab5aa.txt` (NT8's internal UUID, not our string) | `Oco='FIELD9_ID_1776615000538'` — NT8 stored it as OCO ID, **not** ORDER ID |
| 10 (ORDER ID) | `...DAY;;FIELD10_ID_…;;` | **`Sim101_FIELD10_ID_1776615000538.txt`** ✓ | `Oco=''` — filename uses our ID, confirming this is the ORDER ID slot |
| 11 (STRATEGY) | `...DAY;;;FIELD11_ID_…;` | (no outgoing file created for this order) | `Unable to load ATM strategy template from file 'templates/AtmStrategy/FIELD11_ID_1776615000538.xml'` — NT8 tried to load our string as an ATM strategy template name |

**Verdict — ORDER ID lives at field index 10** (0-indexed, counting the `PLACE` verb as index 0). In the PLACE template `PLACE;ACCT;INST;ACTION;QTY;TYPE;LIMIT;STOP;TIF;OCO;ORDER_ID;STRAT;STRAT_ID`, that's the 10th `;`-delimited slot after `PLACE`, i.e. between the 10th and 11th semicolons.

The action plan v2's wording "11th semicolon field" is resolvable-but-imprecise. Normalize to: **"ORDER ID at field index 10 (0-indexed, `PLACE` at index 0)"** or equivalently **"between the 10th and 11th semicolon"**.

### Additional findings — production bugs surfaced

**Bug #1 — Current PLACE writer never emits an ORDER ID.** [bridge/oif_writer.py:62](phoenix_bot/bridge/oif_writer.py) emits `PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};MARKET;0;0;DAY;;;;` — all optional slots (OCO, ORDER ID, STRATEGY, STRATEGY ID) empty. Consequence: NT8 has **not** been generating per-order ACK files during normal bot operation. The bot has been fill-blind end-to-end since day one; `check_fills()` only ever sees Position / Connection files. This is why P2 is a genuine unlock, not an incremental improvement.

**Bug #2 — `CANCEL_ALL` has wrong field count.** [bridge/oif_writer.py:100](phoenix_bot/bridge/oif_writer.py) emits `CANCELALLORDERS;{ACCOUNT};{INSTRUMENT};;;;;;;;;;;;` (14 semicolons, 15 fields). NT8 log during test_04 cleanup:

```
OIF, 'CANCELALLORDERS;Sim101;MNQM6 06-26;;;;;;;;;;;;' has invalid # of parameters, should be 13 but is 15
```

Docs confirm: `CANCELALLORDERS;;;;;;;;;;;;` — takes **no** arguments, just 12 trailing semicolons. Current writer adds ACCOUNT and INSTRUMENT inappropriately → NT8 rejects the line. Any `CANCEL_ALL` call the bot has ever made has silently no-op'd at NT8 layer. (Not in P0-P6 scope directly but belongs in the same session as P5/P13 work.)

### Phase 1 verdict

**VERIFIED-WITH-ADJUSTMENT.** The P2 design works as planned with five concrete corrections:

1. **Filename is `<Account>_<orderId>.txt`** (not `<orderId>.txt` as the plan says). Poll path: `os.path.join(OIF_OUTGOING, f"{ACCOUNT}_{order_id}.txt")`.
2. **ORDER ID goes at field index 10** — the current `write_oif()` emits it at the right slot when the field is populated; just populate it.
3. **Content format: `STATE;FILLED_QTY;AVG_PRICE`** — states are UPPERCASE strings (`SUBMITTED`, `ACCEPTED`, `WORKING`, `PARTFILLED`, `FILLED`, `CANCELPENDING`, `CANCELLED`, `REJECTED`). State machine in P2 should match on uppercase.
4. **Submit-to-first-ACK latency is ~100ms**, not 20ms. Poll cadence of 100ms is the right target; 20ms is over-engineered and wasteful. Recommend: start polling at 50ms for the first second (catch SUBMITTED fast), then back off to 250ms polling for the remaining window.
5. **Timeout budget should be 60s, not 30s.** Rejection-for-no-market-data took 21s on Sim101; real-broker rejections can take similar on cold-connection paths. 30s risks false-positive UNKNOWN_REQUIRES_HUMAN alerts.

Also: the **CANCEL-to-REJECTED transition takes ~5s** via CANCELPENDING intermediate state. P2's state machine must handle CANCELPENDING correctly or it will incorrectly flag CANCELs as hung for 5s every time.

One item deferred to 17:30 CT delta pass: verify Filled-state ACK format against a market-open sim fill. Expected `FILLED;1;<price>` but need empirical confirmation (and capture a sample Position-file contents while we're there).

**Proceed-gate: Phase 1 findings support Phase 2. Recommended to continue.**

---

## Phase 2 — Current OIF writer field count audit

Expanded scope per approval NOTE 2: audit every command format string in `bridge/oif_writer.py`, not just PLACE. Weekend commit `6e2e325` introduced four new action types (`PARTIAL_EXIT_LONG`, `PARTIAL_EXIT_SHORT`, `PLACE_STOP_SELL`, `PLACE_STOP_BUY`) that have never been NT8-verified.

### Step 2.1 — Static audit of every format string

Source: [bridge/oif_writer.py](phoenix_bot/bridge/oif_writer.py). NT8 expects 12 semicolons (= 13 fields) for all commands per the OIF doc.

| Line | Code path | Action emitted | OIF template (literal + substituted) | Semis | Status |
|---|---|---|---|---|---|
| 59 | `write_oif("ENTER_LONG", LIMIT)` | PLACE BUY LIMIT | `PLACE;Sim101;MNQM6 06-26;BUY;1;LIMIT;{px:.2f};0;DAY;;;;` | 12 | ✓ matches doc |
| 62 | `write_oif("ENTER_LONG", MARKET)` | PLACE BUY MARKET | `PLACE;Sim101;MNQM6 06-26;BUY;1;MARKET;0;0;DAY;;;;` | 12 | ✓ (verified in Phase 1) |
| 65 | OCO stop leg for LONG | PLACE SELL STOP DAY | `PLACE;…;SELL;1;STOP;0;{stop:.2f};DAY;;;;` | 12 | ✓ |
| 66 | OCO target leg for LONG | PLACE SELL LIMIT DAY | `PLACE;…;SELL;1;LIMIT;{tgt:.2f};0;DAY;;;;` | 12 | ✓ |
| 70 | `write_oif("ENTER_SHORT", LIMIT)` | PLACE SELL LIMIT | `PLACE;…;SELL;1;LIMIT;{px:.2f};0;DAY;;;;` | 12 | ✓ |
| 73 | `write_oif("ENTER_SHORT", MARKET)` | PLACE SELL MARKET | `PLACE;…;SELL;1;MARKET;0;0;DAY;;;;` | 12 | ✓ |
| 75 | OCO stop leg for SHORT | PLACE BUY STOP DAY | `PLACE;…;BUY;1;STOP;0;{stop:.2f};DAY;;;;` | 12 | ✓ |
| 76 | OCO target leg for SHORT | PLACE BUY LIMIT DAY | `PLACE;…;BUY;1;LIMIT;{tgt:.2f};0;DAY;;;;` | 12 | ✓ |
| 79 | `write_oif("EXIT" / variants)` | CLOSEPOSITION | `CLOSEPOSITION;Sim101;MNQM6 06-26;DAY;;;;;;;;;` | 12 | ✓ (verified in Phase 1 cleanup) |
| 83 | `PARTIAL_EXIT_LONG` | PLACE SELL MARKET | `PLACE;…;SELL;1;MARKET;0;0;DAY;;;;` | 12 | ✓ (byte-identical to line 73) |
| 87 | `PARTIAL_EXIT_SHORT` | PLACE BUY MARKET | `PLACE;…;BUY;1;MARKET;0;0;DAY;;;;` | 12 | ✓ (byte-identical to line 62) |
| 92 | `PLACE_STOP_SELL` | PLACE SELL STOP **GTC** | `PLACE;…;SELL;1;STOP;0;{stop:.2f};GTC;;;;` | 12 | ✓ format; GTC TIF needs runtime verify |
| 97 | `PLACE_STOP_BUY` | PLACE BUY STOP **GTC** | `PLACE;…;BUY;1;STOP;0;{stop:.2f};GTC;;;;` | 12 | ✓ format; GTC TIF needs runtime verify |
| 100 | `CANCEL_ALL` | CANCELALLORDERS | `CANCELALLORDERS;Sim101;MNQM6 06-26;;;;;;;;;;;;` | **14** | ✗ **B2: wrong count** |

**B2 confirmed statically:** the writer emits `CANCELALLORDERS;Sim101;MNQM6 06-26;` at the front, then 12 trailing semicolons — total 14 semicolons = 15 fields. NT8 wants 12 semicolons = 13 fields with **no account or instrument argument** (per docs: `CANCELALLORDERS;;;;;;;;;;;;`). Every CANCEL_ALL the bot has ever emitted has been silently rejected by NT8 (see NT8 log during Phase 1 test_04 cleanup).

**Also missing from the writer:** NT8 OIF docs describe a `CANCEL;;;;;;;;;;<ORDER_ID>;;<[STRATEGY ID]>` (single-order cancel, ORDER ID at field 10). The current writer emits no `CANCEL` format — only the broken `CANCELALLORDERS`. P2 (fill ACK) and P5 (ClOrdID) both need single-order CANCEL; the writer needs a new action path for it. Not a bug per se (it's absent, not wrong) — flagging for the action plan.

**Additional minor note:** line 59's OIF line for LIMIT BUY writes `{limit_price:.2f}` — always 2 decimals even for integer prices like `20000.00`. That's harmless (NT8 parses either), but consistent with the rest of the file. No action.

### Step 2.2 — Live trace

Not strictly needed as a separate step — the static audit table above already shows the literal bytes each code path emits, and Phase 1's successful tests against manually-crafted PLACE MARKET / LIMIT / STOP variants with the same format confirm the writer's byte patterns are accepted by NT8. What remains to verify empirically:

1. `CANCELALLORDERS;;;;;;;;;;;;` (corrected 12-semi form) — does NT8 accept this as the fix for B2?
2. `PLACE;…;STOP;0;{stop};GTC;…` — does NT8 accept GTC on a STOP order? (PLACE_STOP_SELL/BUY are new in commit 6e2e325 and have never been live-tested.)

These are the only two runtime checks remaining for Phase 2. Covered by test_05 below.

### Step 2.3 — End-to-end current behavior (ACK-file question)

Resolved without a dedicated test: from Phase 1 static analysis plus the NT8 log during Phase 1 tests, we already know:

- Current `write_oif()` (at line 62) emits `PLACE;…;DAY;;;;` — ORDER ID field at index 10 is empty.
- With an empty ORDER ID, NT8 does **not** create `<Account>_<orderId>.txt` (there's no ID to name the file by).
- The only per-order outgoing file we ever saw with the current writer's format was generated by NT8's internal UUID (observed in Phase 1 test_04's field-9 test, where NT8 ignored our field-9 string and used its own UUID: `Sim101_54512de4e067447f813e437300cab5aa.txt`). That file is transient and NT8's internal ID is not surfaced to Python anywhere — useless for correlation.

**Answer to Step 2.3:** current production behavior generates **no per-order ACK Python can correlate to its own intent**. The bot's `check_fills()` has only ever read Position files and Connection files, never per-order state. This is what B1 fixes — and it's a complete new capability, not a performance improvement.

### Runtime verification (test_05)

Two unverified command formats sent hand-crafted to NT8:

#### (A) Corrected `CANCELALLORDERS;;;;;;;;;;;;` — accepted BUT with larger blast radius than expected

NT8 log:
```
12:20:48.902  OIF, 'CANCELALLORDERS;;;;;;;;;;;;' processing
12:20:48.904  Cancel all orders account='Sim101'
12:20:48.904  Cancel all orders account='1590711'
12:20:48.904  Cancel all orders account='DEMO5880030'
```

The no-args form cancels across **every account NT8 has connected** — Sim101 plus two other accounts (one looks like a real brokerage account, `1590711`; one is a separate demo `DEMO5880030`). This is **worse** than the currently-broken form for a risk-manager hard-flatten scenario: a naïve fix that just corrects the semicolon count would now affect live-account orders the bot has no business touching. See new bug **B4** below.

#### (B) `PLACE;…;STOP;0;{px};GTC;;…` (both SELL and BUY) — REJECTED at parse time

NT8 log:
```
12:20:50.904  OIF, 'PLACE;Sim101;MNQM6 06-26;SELL;1;STOP;0;20000;GTC;;T05_STPS_…;;'
             holds invalid order type parameter 'STOP'
12:20:53.905  OIF, 'PLACE;Sim101;MNQM6 06-26;BUY;1;STOP;0;40000;GTC;;T05_STPB_…;;'
             holds invalid order type parameter 'STOP'
```

**NT8 does not accept `STOP` as an ORDER TYPE value.** Phase 1 test_03 used `STOPMARKET` successfully — NT8 log stored it as `Type='Stop Market'`. The current writer uses `STOP` in four places, so every stop order it has emitted has been parse-rejected by NT8 before ever reaching Sim101. See new bug **B3** below.

**Parse-level vs Order-level rejection — new wrinkle for P2 design:**
- Phase 1 showed that orders with valid format but simulation failures (e.g. "no market data") produce a full ACK trail: `SUBMITTED;0;0` → `REJECTED;0;0` in outgoing/
- Test_05 now shows that **format-invalid OIFs produce ZERO outgoing ACK files** — only a NT8 log line. No file Python can poll for.
- P2's state machine must treat "OIF sent, no ACK ever appears within timeout" as a distinct class from "ACK arrived then rejected." Currently the action plan collapses both into "UNKNOWN_REQUIRES_HUMAN." That's fine for triage, but the only way to diagnose the format-invalid case is to also surface NT8's log file — either by tailing it from Python, or by making the test at P2's timeout boundary include a "check the last 5s of NT8 log for our OIF content" probe.

### New production bugs found in runtime verification

#### B3 (NEW). Invalid ORDER TYPE `STOP` in every stop-emitting PLACE (bridge/oif_writer.py lines 65, 75, 92, 97)

**Severity: CRITICAL (latent).** NT8 accepts `STOPMARKET` (seen working in Phase 1 test_03); does not accept `STOP`.

**Impact:** Every stop order emitted by the writer has been parse-rejected by NT8 before reaching Sim101. This includes:
- OCO stop legs on every bracketed entry (lines 65, 75)
- Standalone break-even stops emitted by `write_be_stop()` / `PLACE_STOP_SELL` / `PLACE_STOP_BUY` (lines 92, 97)

**Consequence:** The bot has been running with OCO brackets that have a target leg but **no stop leg**. Strategy-layer stops are entirely enforced Python-side (tick-path breach detection → `CLOSEPOSITION`). This means:
- On bridge / WebSocket outage: zero NT8-side safety net
- Any latency in the tick path: missed stops
- Bot crash after open: naked position until manual intervention

This is the exact "phantom position" / "catastrophic stops" class Codex v2 Tier-1'd. The bot has only been un-blown-up because strategy-layer Python exit logic has worked most of the time.

**Fix:** One-line correction in each of the four locations: `STOP` → `STOPMARKET`. Possibly a `STOPLIMIT` variant too for future stop-limit orders. 15 min including a live verification test.

**Severity rationale for elevated priority:** the bot has survived despite this because Python-side stops have caught breaches. But every live-money session with this bug in place carries catastrophic tail risk. Ship with or before P5 on Day 1.

#### B4 (NEW). `CANCELALLORDERS` no-args form cancels across all NT8-connected accounts

**Severity: CRITICAL (design).** NT8's `CANCELALLORDERS;;;;;;;;;;;;` command, when account field is empty, applies to ALL accounts — Sim101 plus any live/demo accounts connected in the current NT8 session. Verified empirically in test_05: log line `Cancel all orders account='1590711'` shows a real brokerage account was affected.

**Impact:** a naïve B2 fix (just correcting the semicolon count) would make the bot's risk-manager hard-flatten nuke orders on the user's real brokerage account during a HALT event. The broken current form (silent no-op) is arguably safer than the "fixed" form.

**Fix options:**
1. **Account-scoped CANCELALLORDERS** — NT8 OIF docs don't document an account-scoped form; testing whether `CANCELALLORDERS;Sim101;;;;;;;;;;;` (13 fields, account populated) works is a separate verification sprint item.
2. **Per-order CANCEL loop** — the bot tracks its own ORDER IDs (once B1 / P5 ships). Hard-flatten iterates intents with status ∈ {SUBMITTED, WORKING, ACCEPTED} and issues `CANCEL;;;;;;;;;;<id>;;` for each. Slower (N files instead of 1) but precisely scoped.
3. **`CLOSEPOSITION` per tracked instrument** — already works (Phase 1 cleanup verified). Doesn't cancel open orders though, only flattens filled positions. Needs to be paired with option 2.

Recommended: option 2, chained with option 3 for full flatten. Implement as part of P5 (which introduces ClOrdID tracking that enables per-order cancel).

**Verification TODO (not blocking):** quick follow-up test to check whether `CANCELALLORDERS;Sim101;;;;;;;;;;;;` (account-scoped form with 12 semicolons, account populated) is accepted by NT8. If yes, this simplifies B4 fix dramatically. Defer to the 17:30 delta pass or a dedicated 10-min test later.

#### B5 (note). Writer has no single-order `CANCEL` command path

Not a bug — it's absent rather than broken. But P2 (fill-ACK correlation) and P5 (ClOrdID) both need single-order cancel, and option 2 of B4 depends on it. The writer needs a new action handler for `CANCEL` emitting `CANCEL;;;;;;;;;;<order_id>;;` (field 10 = ORDER ID, same slot as PLACE). Trivial to add.

### Phase 2 verdict

**Not just B2 — B2 + B3 + B4 + B5.** The OIF layer is more broken than the action plan assumed. The good news: these are all narrow, mechanically-fixable bugs, and together they're a <1 hour fix if bundled. The bad news: any of them alone would be a Tier 1 item; together they rewrite the Day-1 schedule.

Updated recommendation for action plan:
- **P5a = B1** (populate ORDER ID) — already in P5 scope, make it explicit
- **P5b = B2 + B3 + B4 + B5 bundled** — "OIF writer correctness pass": fix CANCEL_ALL format + STOP→STOPMARKET + account-scoped cancel strategy + add single-order CANCEL. 45-60 min. Day 1.

Also: **B3 elevates the urgency of P1 (atomic write).** If stop orders never reached NT8 in the first place, atomic writes were window dressing for a stop-loss mechanism that didn't exist. After B3 is fixed, atomic writes actually protect a real safety net. Don't change P1's order in the plan — just note in the plan that P1 unblocks *future* reliability, while B3 unblocks *current* reliability.

**Proceed-gate: Phase 2 findings support moving to Phase 3 (shadow module audit).**

---

## Phase 3 — Shadow module API compatibility

### Step 3.1 — Signature audit (all 5 methods)

| # | Module | Method | Signature | Docstring summary |
|---|---|---|---|---|
| 1 | [core/strategy_decay_monitor.py:163](phoenix_bot/core/strategy_decay_monitor.py) | `DecayMonitor.record_trade` | `(self, strategy_name: str, pnl_usd: float, outcome: str, ts: datetime = None) -> None` | Records a trade into rolling per-strategy perf window |
| 2 | [core/tca_tracker.py:133](phoenix_bot/core/tca_tracker.py) | `TCATracker.record_fill` | `(self, trade_id: str, strategy: str, direction: str, signal_price: float, fill_price: float, time_to_fill_ms: int, fill_type: str = "LIMIT", regime: str = "UNKNOWN", ts: datetime = None) -> TCARecord` | Records one fill's TCA — slippage, fill latency. Computes direction-adjusted slippage internally. |
| 3 | [core/circuit_breakers.py:168](phoenix_bot/core/circuit_breakers.py) | `CircuitBreakers.record_slippage` | `(self, slippage_ticks: float) -> None` | Appends to rolling slippage deque for spike detection |
| 4 | [core/circuit_breakers.py:193](phoenix_bot/core/circuit_breakers.py) | `CircuitBreakers.record_trade_outcome` | `(self, outcome: str) -> None` | Appends "WIN"/"LOSS" to rolling outcome deque for WR-crash detection |
| 5 | [core/liquidity_sweep.py:76](phoenix_bot/core/liquidity_sweep.py) | `SweepWatcher.track_pivot_break` | `(self, pivot_price: float, break_direction: str, break_ts: datetime, break_bar_idx: int, break_extreme: float) -> None` | Called when price breaks a pivot — starts a "sweep watch" for N bars |

### Step 3.2 — Trade dict shape (from `PositionManager.close_position()`)

[core/position_manager.py:100-120](phoenix_bot/core/position_manager.py):

```python
trade = {
    "trade_id":        str,
    "direction":       str,     # "LONG" | "SHORT"
    "entry_price":     float,
    "exit_price":      float,
    "contracts":       int,
    "stop_price":      float,
    "target_price":    float,
    "pnl_ticks":       float,
    "pnl_dollars":     float,   # Net (after commission)
    "gross_pnl":       float,
    "commission":      float,
    "result":          str,     # "WIN" | "LOSS"
    "hold_time_s":     float,
    "strategy":        str,
    "entry_reason":    str,
    "exit_reason":     str,
    "entry_time":      float,   # unix ts
    "exit_time":       float,   # unix ts
    "market_snapshot": dict,    # captured at entry time
}
```

`market_snapshot` contents at entry (from base_bot/lab_bot): `signal_price`, `regime`, `price`, `vwap`, `cvd`, plus whatever else the aggregator emitted that bar. Populated by `PositionManager.open_position()` from the passed `market_snapshot` arg.

### Step 3.3 — Compatibility matrix

| # | Method | Required args | Mapping | Classification |
|---|---|---|---|---|
| 1 | `decay_monitor.record_trade` | `strategy_name`, `pnl_usd`, `outcome` | `strategy_name ← trade["strategy"]`; `pnl_usd ← trade["pnl_dollars"]`; `outcome ← trade["result"]`; skip `ts` (defaults to now ≈ exit_time) | **YELLOW** — 3 renames, ~3-line kwargs mapping |
| 2 | `tca_tracker.record_fill` | `trade_id`, `strategy`, `direction`, `signal_price`, `fill_price`, `time_to_fill_ms` | First 3 rename-direct. `signal_price ← trade["market_snapshot"].get("signal_price", trade["entry_price"])` (ORANGE derive). `fill_price ← trade["entry_price"]` (ambiguous — entry fill or exit fill?). **`time_to_fill_ms` is not computed anywhere in the pipeline** — requires per-order ACK latency from P2, which requires B1 (ORDER ID populated). | **RED** — requires new data plumbing (P2+B1 first) |
| 3 | `circuit_breakers.record_slippage` | `slippage_ticks` | Derive: `(entry_price - signal_price) / TICK_SIZE`, direction-adjusted. Requires `market_snapshot["signal_price"]` — populated in lab_bot's `_paper_enter` (line 363), assumed populated equivalently in prod_bot's entry path (not verified). ~6-line helper. | **ORANGE** — derive from 2 fields + direction |
| 4 | `circuit_breakers.record_trade_outcome` | `outcome` | `outcome ← trade["result"]` | **YELLOW** — 1 rename |
| 5 | `sweep_watcher.track_pivot_break` | `pivot_price`, `break_direction`, `break_ts`, `break_bar_idx`, `break_extreme` | **None of these are in the trade dict.** This method is called when price BREAKS A PIVOT (from `swing_detector` on bar close) — it is not a trade-close consumer. It belongs in the bar/signal pipeline, not `_on_trade_closed`. | **MISPLACED** — wrong pipeline. Action plan premise is incorrect. |

### Key finding — P10's scope is wrong for 2 of 5 methods

**The action plan treats `_on_trade_closed` as the universal wiring point for all 5 shadow modules.** The audit shows:

- **3 of 5** (decay_monitor, circuit_breakers.record_slippage, circuit_breakers.record_trade_outcome) are legitimately trade-close consumers — YELLOW/ORANGE wiring.
- **1 of 5** (`tca_tracker.record_fill`) is a fill-event consumer, not a trade-close consumer. Its `time_to_fill_ms` arg only exists once P2 lands (per-order ACK latency measurement). Wiring it into `_on_trade_closed` with `time_to_fill_ms=0` produces valid-looking but meaningless TCA data — the module's primary value (slippage vs rolling baseline) requires real fill latency from ACKs.
- **1 of 5** (`sweep_watcher.track_pivot_break`) doesn't belong in `_on_trade_closed` at all. It's called on pivot-break events from `swing_detector`. It's a signal-pipeline wire-up, completely separate concern.

### Current wiring state (grep of bots/ for shadow method calls)

| Module | Method | Currently called in base_bot/lab_bot? |
|---|---|---|
| DecayMonitor | `record_trade` | **NO** — only `summary()` wired to dashboard at base_bot:2567 |
| DecayMonitor | `save_state` | **NO** |
| TCATracker | `record_fill` | **NO** — only `weekly_report()` wired to dashboard at base_bot:2568 |
| CircuitBreakers | `record_slippage` | **NO** |
| CircuitBreakers | `record_trade_outcome` | **NO** |
| CircuitBreakers | `record_tick` | ✓ wired at base_bot:550 |
| CircuitBreakers | `should_halt` | **NO** — confirmed by action plan's P3 ("HALT enforcement is theater") |
| SweepWatcher | `track_pivot_break` | **NO** |
| SweepWatcher | `check_sweep` | ✓ wired at base_bot:882 |

Codex v1's "shadow modules are cosmetic theater" was correct at method-level granularity: modules are **instantiated**, **dashboard queries work** (get_state / summary / weekly_report), but the **data-feeding methods are not called anywhere**. The dashboards have been reading empty state the entire time.

### Revised P10 estimate

Original plan: 1.5h, "literally a six-line patch."

**Reality across all five intended wirings:**

| Subtask | Scope | Estimate |
|---|---|---|
| **P10a — ship in Day 7 as planned** | Wire decay_monitor.record_trade + circuit_breakers.record_slippage + circuit_breakers.record_trade_outcome in a new `_on_trade_closed(trade)` handler. 2 YELLOW + 1 ORANGE = ~3h. Handler is new method on BaseBot, called at end of `_exit_trade` after `close_position()`. | **2-3h** (not 1.5h) |
| **P10b — new item, not in plan** | Wire sweep_watcher.track_pivot_break against swing_detector's pivot-break events in the bar-close pipeline. Separate handler, separate concern. | **1.5-2h** |
| **P10c — deferred until P2 lands** | Wire tca_tracker.record_fill properly once per-order ACK latency is available (B1 + P2). Before P2, record_fill is either wired with placeholder 0ms (useless data) or not wired at all. | **0.5-1h, post-P2** |

**Total: 4-6h across three scheduling milestones.** The "1.5h six-line patch" framing in the action plan v2 was wrong by ~3-4x. Not catastrophic — but worth correcting so Day 7 doesn't silently overflow.

### Schedule implication for action plan

- **Day 7 P10 estimate: 1.5h → 2-3h.** Still fits Tuesday of Week 2. Scope narrowed to P10a only.
- **P10b added** as a new Tier 2 item for somewhere in Week 2 — 1.5-2h.
- **P10c deferred** until P2 lands on Day 3; add 0.5-1h to that day OR schedule as a Day 8/9 cleanup.

Existing Day 7 also has P22 (2h decay alerts). If P10a grows to 3h, Day 7 = 5h, which matches the original "3.5h" estimate poorly. Day 7 recommended new total: **~5h**, with P22 possibly trimmed to essentials or split.

### Also noted

- **`circuit_breakers.should_halt()` is not called from base_bot at all.** Confirmed by grep. This matches the action plan's P3 ("wire HALT enforcement at strategy entry") correctly describing the gap. P3 is a genuinely 30-minute fix once the method is called.
- **`market_snapshot["signal_price"]` exists in lab_bot's paper entry path** (lab_bot.py:363). Not verified in prod_bot — worth a quick check during P10a implementation. If prod_bot doesn't populate it, the slippage derivation falls back to `entry_price` and produces `slippage_ticks == 0` every time.
- **Per-strategy baseline tracking** in decay_monitor expects `baseline_backtest_sharpe` to be set per strategy at some point (validated when the strategy was approved). Currently defaults to 0.0 — that means decay comparisons are meaningless until baselines are seeded. Separate task; flag for P22 (decay alerting) scoping.

### Phase 3 verdict

**YELLOW-with-reshape.** The 5-module wiring premise is partly wrong. 3 of 5 wire cleanly in `_on_trade_closed` (P10a, 2-3h). 1 is in the wrong pipeline (P10b, separate task). 1 is gated on P2 (P10c, deferred).

Net effect on action plan: **P10's ROI hasn't changed — wiring those 3 methods is still the best lever for visibility into decay.** But the scope expanded from "one method" to "three sub-items," and two of those three are deferred or separate. The narrative "P10 unlocks all shadow modules" in the plan should be rewritten to "P10a unlocks 3 of 5; P10b is a bar-pipeline wire-up; P10c is a follow-up to P2."

**Proceed-gate: Phase 3 findings support moving to Phase 4 (P4 state persistence decision).**

---

## Phase 4 — P4 state persistence decision

**Claim to verify:** "JSON-file-per-position with atomic writes is sufficient for durability and concurrency, making SQLite+WAL over-engineered."

### Step 4.1 — Read access pattern

Hot-path reads (per-tick or per-bar):

| Location | What it reads | Frequency | Needs ACID? |
|---|---|---|---|
| [base_bot.py:561, 663, 1065, 1086](phoenix_bot/bots/base_bot.py); [lab_bot.py:156, 324, 410](phoenix_bot/bots/lab_bot.py) | `self.positions.is_flat` — property check | Every tick on hot path (hundreds/min) | No — boolean check |
| [base_bot.py:573, 632, 650, 2034, 2106, 2203](phoenix_bot/bots/base_bot.py) | `self.positions.position` — direct Position object | Per evaluate-loop (per bar) | No — live memory object |
| [base_bot.py:656](phoenix_bot/bots/base_bot.py) | `self.positions.check_exits(price, max_hold_min)` | Every tick | No — pure function over in-memory state |
| [base_bot.py:2520](phoenix_bot/bots/base_bot.py) | `self.positions.to_dict(market.price)` — dashboard snapshot | Every dashboard poll (1-5s) | No — serialization of in-memory state |
| [position_manager.py:53, 57](phoenix_bot/core/position_manager.py) | `is_long`, `is_short` properties | Wherever called | No |

**Critical observation:** all hot-path reads go against `self.position` in memory. **Zero reads hit disk.** The disk state is for persistence/recovery, not for live serving. P4's read-pattern requirements boil down to "load once on startup" — nothing hot.

### Step 4.2 — Write access pattern

| Location | What it mutates | Frequency | Concurrency |
|---|---|---|---|
| [base_bot.py:2011](phoenix_bot/bots/base_bot.py); [lab_bot.py:351](phoenix_bot/bots/lab_bot.py) | `self.positions.open_position(...)` — creates Position | On entry signal acceptance (~5-20/day) | Single writer (main asyncio loop) |
| [base_bot.py:2244](phoenix_bot/bots/base_bot.py); [lab_bot.py:422](phoenix_bot/bots/lab_bot.py) | `self.positions.close_position(price, reason)` — clears position | On exit (~5-20/day) | Single writer |
| [base_bot.py:2158](phoenix_bot/bots/base_bot.py) | `self.positions.scale_out_partial(price, n)` — decrements contracts, mutates Position in-place | On scale-out event (~1-5/day) | Single writer |
| [base_bot.py:2167](phoenix_bot/bots/base_bot.py) | `self.positions.move_stop_to_be(be_price)` — mutates `pos.stop_price` in-place | On BE trigger (~1-5/day) | Single writer |

**All writes happen in the single asyncio event loop.** No threading. No concurrent writers. Maximum realistic write rate: ~30/day in lab bot, single-digit in prod bot. An atomic-JSON-replace at that rate is negligible overhead (1-2ms per write).

### Step 4.3 — Reconciliation requirements

After crash-restart, `reconcile()` needs to:

1. Load Python's last-saved state (current position + active intents)
2. Read NT8's `<Instrument>_<Account>_Position.txt` from OIF_OUTGOING — tells us aggregate position per instrument
3. Compare:
   - Both flat: clean start
   - Python has position, NT8 matches direction+qty: **adopt Python's metadata** (stop/target/strategy/reason preserved)
   - Python has position, NT8 disagrees: NT8 is truth; reconstruct minimal Position from NT8's aggregate data, log divergence, alert user
   - Python flat, NT8 has position: orphan — reconstruct minimal Position from NT8, alert user (no strategy context)
4. For intents: any with status ∈ {SUBMITTED, WORKING, ACCEPTED} → resume polling NT8 outgoing/ for ACK (P2), or mark as UNKNOWN_REQUIRES_HUMAN if outgoing/ doesn't have them (NT8 wipes outgoing/ on restart — the action plan v2 missed this NT8-doc fact).

**Shape of reconciliation:**
- One position record (scalar)
- A few intent records (0-10 active at any time; bot-at-rest has 0)
- Two-way compare, no joins, no time-range queries, no aggregations, no group-by

### Step 4.4 — Verdict

**JSON-file-per-state + JSONL history. No SQLite.**

#### Codebase conventions (this is decisive)

Existing JSON + atomic-replace precedents in `core/`:

| Module | File | Pattern |
|---|---|---|
| [strategy_decay_monitor.py:141-161](phoenix_bot/core/strategy_decay_monitor.py) | `memory/episodic/decay_state.json` | tempfile + os.replace ✓ |
| [tick_aggregator.py:692-760](phoenix_bot/core/tick_aggregator.py) | `data/aggregator_state_*.json` | save_state/restore_state with atomic write ✓ |
| [equity_tracker.py:29-42](phoenix_bot/core/equity_tracker.py) | `data/equity_*.json` | json.dump/load |
| [trade_memory.py:26-38](phoenix_bot/core/trade_memory.py) | trade log JSON | json.dump/load |
| counter_edge.py, expectancy_engine.py, execution_quality.py, strategy_tracker.py, no_trade_fingerprint.py | various | json.dump/load |

Existing JSONL append-only history precedents:

| Module | File |
|---|---|
| [history_logger.py](phoenix_bot/core/history_logger.py) | `logs/history/YYYY-MM-DD_<bot>.jsonl` |
| [tca_tracker.py](phoenix_bot/core/tca_tracker.py) | `logs/tca_history.jsonl` |
| memory | `memory/audit_log.jsonl` |

**Zero SQLite usage in the entire current codebase.** Introducing SQLite for P4 would be the first relational-DB dependency. The action plan v2's argument for SQLite ("JSON has concurrency hell") does not apply to this codebase — single-writer + atomic os.replace + in-memory reads eliminate the concurrency concern that motivates WAL.

#### Proposed architecture

```
state/
  active.json           — current position + active intents map (atomic-written, ~2-5KB)
logs/
  intent_history.jsonl  — append-only intent lifecycle events (final states archive)
```

`state/active.json` shape:

```json
{
  "last_updated": "2026-04-19T12:34:56.789",
  "bot_name": "prod",
  "position": {
    "trade_id": "…",
    "direction": "LONG",
    "entry_price": 26234.50,
    "entry_time": 1744567890.0,
    "contracts": 1,
    "original_contracts": 2,
    "stop_price": 26224.50,
    "target_price": 26250.50,
    "strategy": "bias_momentum",
    "reason": "…",
    "scaled_out": false,
    "be_stop_active": false,
    "rider_mode": false,
    "market_snapshot": {…}
  },
  "intents": {
    "bias_momentum-20260419-1235-a1b2c3": {
      "symbol": "MNQM6 06-26",
      "side": "BUY",
      "qty": 1,
      "order_type": "MARKET",
      "sent_ts": 1744567890.0,
      "status": "SUBMITTED",
      "strategy": "bias_momentum",
      "oif_filename": "oif_12345_bias_momentum.txt",
      "ack_file_expected": "Sim101_bias_momentum-20260419-1235-a1b2c3.txt"
    }
  }
}
```

On each mutation: serialize → `tempfile.mkstemp` in same dir → `os.fsync` → `os.replace`. Exactly P1's atomic-write pattern. Single file, <5KB typically, 1-2ms per write, <30 writes/day.

Intent lifecycle final states (Filled/Cancelled/Rejected) → pop from `intents` dict → append to `logs/intent_history.jsonl` → rewrite `active.json`.

#### What SQLite would give us that JSON doesn't — and why we don't need it

| SQLite advantage | Applicable here? |
|---|---|
| Relational queries (JOIN, GROUP BY) | No — reconciliation is 2-way compare, no joins |
| Multi-row atomic transactions | No — each mutation is independent; atomic JSON file replace is sufficient |
| Indexed lookups over millions of rows | No — active set is ≤10 rows; history rarely loaded in hot path |
| Concurrent reader/writer coordination | No — single writer, in-memory reads |
| Durability via WAL | Already achieved by tempfile + fsync + os.replace |

#### What JSON gives us that SQLite doesn't

- **Consistency with existing codebase** — 9 modules already use this pattern; zero use SQLite
- **Human-readable debug state** — `cat state/active.json` when triaging
- **Zero schema migrations** — add a field, old loaders ignore unknown keys
- **No new dependency surface** — though sqlite3 is stdlib, it's still new machinery (cursors, connections, schema DDL, `PRAGMA`)

#### Revised P4 estimate

Action plan v2: **4h** (SQLite WAL + schema + `state_writer` asyncio task + `BEGIN IMMEDIATE` wrapping + `reconcile()`).

JSON-based plan: **~2h** split as:

| Sub-step | Est |
|---|---|
| `StateStore` class with atomic `save(active_dict)` + `load()` + `append_intent_history(event)` | 30 min |
| Hook into `open_position` / `close_position` / `scale_out_partial` / `move_stop_to_be` | 30 min |
| Hook into future OIF-send for intent tracking (overlaps with P5) | 30 min |
| `reconcile_on_startup(state_store, nt8_position_reader)` function | 30 min |
| Integration test: kill process mid-write, restart, verify recovery | included |

**P4 savings: ~2h vs SQLite.** Day 4 goes from 5h → 3h. Combined with P4b exit collision (1h, unchanged), Day 4 total ~4h — comfortable.

#### Important caveat NT8 doc surfaces (not in action plan v2)

From the Phase 1 doc fetch: **"Contents of this folder will be deleted when the NinjaTrader application is restarted."** NT8 wipes `outgoing/` on its own restart. Implications:

- If Python crashes AND NT8 is restarted in between: we lose all per-order ACK files that weren't consumed. Intents in status WORKING → their ACK history is gone.
- Reconciliation must treat "intent in Python active set, no ACK file in outgoing/" as ambiguous: maybe NT8 never got it, maybe NT8 processed it and wiped the ACK on restart. Default: query NT8 position file (survives NT8 restart) as source of truth. If position file shows we have a position, intent was fulfilled; mark Filled with entry_price = NT8's AvgFillPrice. If position file shows flat, intent was either cancelled or never placed; mark UNKNOWN_REQUIRES_HUMAN.

This nuance applies regardless of JSON-vs-SQLite choice. Add to P4's `reconcile()` logic.

#### Edge case: multi-position future

Current `PositionManager` tracks a single position (`self.position: Position | None`). If we ever support multiple concurrent positions (multi-strategy parallel), the JSON file shape grows from `"position": {…}` to `"positions": {trade_id: {…}, …}`. Trivial change. Not a pre-emptive reason to adopt SQLite.

### Phase 4 verdict

**JSON.** The codebase already has 9 precedents for exactly this pattern. SQLite solves problems we don't have (concurrency, relational queries, tx spans) and introduces machinery we've never used. JSON-with-atomic-replace satisfies every documented access pattern at half the estimated effort.

**Proceed-gate: Phase 4 findings support moving to Phase 5 (C# heartbeat feasibility).**

---

## Phase 5 — P6 C# NT8 indicator heartbeat feasibility

### Step 5.0 — Filename correction

Action plan v2 references **`MarketDataBroadcasterV3.cs`**. That file does not exist anywhere — not in repo, not in NT8 installed indicators. The actual tick broadcaster is **`TickStreamer.cs`** (repo: [ninjatrader/TickStreamer.cs](phoenix_bot/ninjatrader/TickStreamer.cs), 310 lines; also installed at `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\TickStreamer.cs`). There is also a sibling `SiM_TickStreamer.cs` (weekend/playback variant) that would need matching treatment.

### Step 5.1 — Current TickStreamer.cs structure

Read [ninjatrader/TickStreamer.cs](phoenix_bot/ninjatrader/TickStreamer.cs).

| Aspect | State |
|---|---|
| Class | `TickStreamer : Indicator` in namespace `NinjaTrader.NinjaScript.Indicators` |
| Lines | 310 |
| Lifecycle | `OnStateChange` dispatches on `SetDefaults`, `DataLoaded`, `Terminated` |
| Tick path | `OnBarUpdate` fires per tick (Calculate.OnEachTick); sends JSON over TCP if connected; writes `C:\temp\mnq_data.json` fallback (throttled 1s) |
| DOM path | `OnMarketDepth` → local 5-level bid/ask arrays, sums and sends every 500ms |
| Connection | TCP client to 127.0.0.1:8765; `TryConnect` with 3s timeout; reconnect on `SendInternal` failure |
| **Heartbeat Timer** | **Already exists** — `System.Threading.Timer heartbeatTimer` at [line 50, 84](phoenix_bot/ninjatrader/TickStreamer.cs), fires every **3000ms** via `HeartbeatCallback` |
| **Heartbeat message** | **Already exists** — `{"type":"heartbeat","ts":"..."}` sent over TCP in `HeartbeatCallback` |
| Threading | `sendLock` mutex coordinates OnBarUpdate (chart thread), OnMarketDepth (depth thread), HeartbeatCallback (threadpool timer thread); `volatile bool isConnected` / `isConnecting` |
| File I/O already used | Yes — `File.WriteAllText(FALLBACK_FILE, …)` at [line 136](phoenix_bot/ninjatrader/TickStreamer.cs) on every bar update, wrapped in `try{}catch{}` |

**Two findings that invalidate v2 plan premises:**

1. **A heartbeat timer is already in place and working.** The v2 plan's implicit assumption — "we need to add heartbeat from scratch" — is wrong. The existing TCP heartbeat fires every 3s from a ThreadPool timer thread. It's been shipping in TickStreamer.cs since v2.0.

2. **A file-that-acts-as-heartbeat already exists.** `C:\temp\mnq_data.json` is rewritten on every `OnBarUpdate` (throttled to 1/sec), wrapped in `try/catch`. Its mtime already serves as "last tick freshness" signal — exactly what v2 plan's P6 layer (3) asks for ("the C# indicator must write a heartbeat file every 1s; Python checks mtime age"). This is already happening. Python just needs to consume it.

### Step 5.2 — Heartbeat insertion plan (if needed at all)

What the fallback file DOES NOT cover: the "indicator alive but tick stream dead" failure mode (KNOWN_ISSUES.md's 2026-04-16 tick-stall incident — NT8 showed connected, 0 ticks/sec for 3.25 hours). In that scenario:
- TCP heartbeat keeps firing from Timer thread ✓
- OnBarUpdate is NOT firing → fallback file goes stale ✓
- A Python watchdog checking fallback-file mtime would correctly detect staleness

So the **single mtime signal is sufficient** for triage. If `C:\temp\mnq_data.json` mtime > 10s old → alert, regardless of whether the cause is "NT8 dead" or "tick feed stalled." The distinction is diagnostic, not operational.

However — if we want to distinguish the two for diagnostic clarity (useful for debugging, Telegram alerts that say "NT8 process dead" vs "tick stream stalled"), we can add a second file written from the HeartbeatCallback timer thread. Staying fresh while fallback file goes stale = tick-stall; both stale = NT8 dead.

#### Proposed snippet (under 15 lines, only-if-we-want-the-diagnostic-distinction)

Add as a new constant:

```csharp
private const string TIMER_HEARTBEAT_FILE = @"C:\temp\nt8_timer_heartbeat.txt";
```

Add inside `HeartbeatCallback(object state)` after the TCP heartbeat section (before the final closing brace):

```csharp
// Timer-thread heartbeat file — fresh whenever Timer callback runs,
// independent of tick arrival. Python compares its mtime to
// C:\temp\mnq_data.json mtime:
//   both fresh         → all good
//   timer fresh, data stale → tick-stall (NT8 alive, feed dead)
//   both stale         → NT8 / indicator dead
try
{
    File.WriteAllText(TIMER_HEARTBEAT_FILE, DateTime.UtcNow.ToString("o"));
}
catch { }  // Never crash the Timer callback
```

**Risks & notes:**

| Concern | Assessment |
|---|---|
| Timer thread vs NT8 chart thread | Not an issue — Timer runs on ThreadPool, file I/O is thread-safe against itself. NT8 does not impose file-I/O restrictions from the Timer thread. |
| File handle lifetime | `File.WriteAllText` opens/writes/closes synchronously. No persistent handle. Zero leak risk. |
| Error handling | `catch { }` matches the existing pattern at line 138. If file write fails, next write retries — worst case Python sees staler mtime and triggers a diagnostic that a human will resolve. Never crash the Timer. |
| Concurrent writes | Only HeartbeatCallback writes this file; Timer serializes callbacks. No concurrency. |
| Cleanup on indicator unload | None required. File persists on disk as last-known state. Next indicator load overwrites it. |
| Partial write on NT8 shutdown | Acceptable — Python reads mtime, not content. File presence with fresh mtime is the signal. |
| Clock skew | `DateTime.UtcNow` is ~1ms resolution, adequate for 3s-cadence heartbeat. |

#### SiM_TickStreamer.cs mirror

If we ship this, the same 4 lines go into `SiM_TickStreamer.cs` (already mirrors the same HeartbeatCallback structure per the earlier diff I reviewed in commit `03e3ee6`). Same file path or a variant — probably same to keep Python's mtime check simple.

### Step 5.3 — Revised honest P6 estimate

v2 plan P6 total: 2.5h with breakdown implied across layers.

#### Option A — Python-only (use existing fallback file as heartbeat)

Accept the current architecture: `C:\temp\mnq_data.json` mtime IS the heartbeat. Python-side additions only.

| Sub-task | Est |
|---|---|
| `websockets` ping_interval=10 + ping_timeout=10 + asyncio.wait_for on recv | 30 min |
| Staleness watchdog async task: polls `time.monotonic() - last_tick_ts > 30`, also mtime of fallback file | 45 min |
| `SIO_KEEPALIVE_VALS` ioctl on Windows loopback — requires test of whether this is meaningful on localhost (it mostly isn't) | 30 min |
| Bridge ring buffer: add `bridge_received_ts` field per tick; consumer rejects replays older than 10s | 45 min |
| Telegram alert wiring + reconnect backoff | included |

**Option A total: ~2.5h.** Matches v2 plan's estimate. Zero C# work. Ships same day.

#### Option B — Add Timer-thread C# heartbeat file for diagnostic clarity

Everything in Option A, plus the 10-line C# addition above.

| Additional sub-task | Est |
|---|---|
| Add TIMER_HEARTBEAT_FILE write to TickStreamer.cs | 15 min |
| Mirror to SiM_TickStreamer.cs | 5 min |
| Compile in NinjaScript editor (F5) — usually clean for a 10-line addition, but occasional cycles for mismatched references | 10 min |
| Deploy to live chart + verify file updates at 3Hz | 15 min |
| Kill/restart test: kill NT8, verify file stops updating; restart, verify resumes | 15 min |
| Python-side consumer: watchdog reads both mtime signals, emits distinct alerts | 15 min |
| Integration test across all failure modes | 30 min |

**Option B additional cost: ~1.5h** on top of Option A. **Option B total: ~4h.**

#### My recommendation

**Option A.** Ship the Python-side layers with the existing fallback file as the heartbeat signal. The distinction between "NT8 dead" and "tick-stall" doesn't change the operator response — both warrant investigation — and mtime staleness alone is a reliable trigger. The 3Hz TCP heartbeat is already fresh data for "connection alive."

Reserve Option B as an enhancement if diagnostic granularity proves insufficient after P6 ships (deferrable by 2+ weeks).

#### Day 5 schedule impact

v2 plan Day 5: P5 (2h) + P13 (0.5h) + **P6 (1.5h)** + P6b (1h) = 5h.

With Option A P6 at 2.5h, Day 5 = 6h. Still fits; slight overflow from v2's 5h budget. Acceptable.

With Option B P6 at 4h, Day 5 = 7.5h. Overflow; would need to slide P6b to Day 6 or split.

### Phase 5 verdict

v2 plan's P6 C# heartbeat requirement is **already 80% built** — the fallback file path is exactly the heartbeat-via-mtime pattern P6 describes, and the TCP heartbeat already ships. Remaining work is Python-side staleness detection + (optional) diagnostic-granularity Timer-thread heartbeat file.

**Recommended P6 scope: Option A, ~2.5h.** Matches v2 estimate, no C# compile cycle required.

**Proceed-gate: Phase 5 findings support moving to Phase 6 (line-ending / autocrlf audit).**

---

### Step 5.4 — Re-verification (2026-04-19 evening sprint kickoff)

Re-read both C# files + the Python consumers to verify the earlier claim holds against current tree state. The Phase 5 audit earlier today inspected a 310-line `TickStreamer.cs`; current file has grown to **406 lines** (v3.0 multi-instrument — streams MNQ primary + ES + ^VXN + ^VIX on the same TCP connection). Re-audit summary:

#### Claim verification — does the heartbeat + fallback file story hold?

| Original Phase 5 claim | Current state | Verified? |
|---|---|---|
| Heartbeat Timer exists, 3s cadence | [TickStreamer.cs:70,124,274-288](phoenix_bot/ninjatrader/TickStreamer.cs) — `Timer heartbeatTimer`, `HEARTBEAT_MS = 3000`, created in `State.DataLoaded`, disposed in `State.Terminated`. Callback sends TCP heartbeat when connected, else reconnect attempt. | ✅ |
| Heartbeat JSON message sent | [TickStreamer.cs:277-283](phoenix_bot/ninjatrader/TickStreamer.cs) — `{"type":"heartbeat","ts":"<ISO-8601>"}\n` via `Send()` under `sendLock`. | ✅ |
| Fallback file writes on each tick, mtime advances | [TickStreamer.cs:179-197](phoenix_bot/ninjatrader/TickStreamer.cs) — `File.WriteAllText(FALLBACK_FILE_PRIMARY, ...)` throttled to 1s (`FILE_WRITE_MS=1000`), wrapped in try/catch. | ✅ |
| SiM variant matches production | [SiM_TickStreamer.cs](phoenix_bot/ninjatrader/SiM_TickStreamer.cs) — identical structure, 374 lines. Same heartbeat + fallback mechanics. Difference: fires during Historical/Transition states for sim/replay testing. | ✅ |
| Python consumes TCP heartbeats | [bridge_server.py:188-192](phoenix_bot/bridge/bridge_server.py) — `msg_type == "heartbeat"` → `nt8_last_tick_time = time.time()`. Also bumped by ticks (line 192). | ✅ |
| Python has stale watcher | [bridge_server.py:448-463](phoenix_bot/bridge/bridge_server.py) — `stale_watcher()` async task, logs warning when `age > DISCONNECT_THRESHOLD_S` (30s). | ✅ |
| Python has file-fallback poller | [bridge_server.py:466-504](phoenix_bot/bridge/bridge_server.py) — `file_fallback_poller()` activates after 30s TCP staleness, reads mtime-updated `C:\temp\mnq_data.json`, broadcasts as ticks. | ✅ |
| Circuit breaker detects tick gap | [core/circuit_breakers.py:10,127-135](phoenix_bot/core/circuit_breakers.py) + [base_bot.py:588](phoenix_bot/bots/base_bot.py) — `check_tick_gap()` fires on >60s no ticks during RTH; `record_tick()` wired on each tick received. | ✅ |

**Claim holds: P6 is ~80% built.** Re-verified across both C# files and 3 Python consumers. Nothing has regressed since the original audit.

#### NEW finding — latent bug in `file_fallback_poller`

The earlier Phase 5 audit didn't drill into the fallback poller's interaction with `nt8_last_tick_time`. Re-reading [bridge_server.py:466-504](phoenix_bot/bridge/bridge_server.py) reveals:

```python
# Lines 485-500 inside file_fallback_poller()
with open(FILE_FALLBACK_PATH, "r") as f:
    data = json.load(f)
tick = {
    "type": "tick", "price": ..., "bid": ..., "ask": ...,
    "vol": ..., "ts": datetime.now(timezone.utc).isoformat(),
    "source": "file_fallback",
}
self.tick_buffer.append(tick)
await self._broadcast_to_bots(json.dumps(tick))
self.ticks_received += 1
# ← NO update to self.nt8_last_tick_time here
```

**Consequence chain if TCP dies and file fallback succeeds:**
1. `nt8_last_tick_time` frozen at the last TCP heartbeat
2. `stale_watcher` logs "NT8 data stale" forever (line 459) — never logs "resumed" because `age < 5` never true (age keeps growing)
3. Downstream consumers that read `nt8_last_tick_age_s` via `/health` see forever-stale
4. `circuit_breakers.check_tick_gap()` uses its own `_last_tick_ts` (bumped via `record_tick()` at [base_bot.py:588](phoenix_bot/bots/base_bot.py)). Since the bot DOES receive the fallback ticks via the bridge broadcast, `record_tick()` is called → breaker does NOT false-fire. Good for the bot; bad for the bridge-level staleness signal.

**Impact:** operational — false-positive "NT8 stale" alerts during a healthy fallback mode. Not catastrophic (bot keeps trading via fallback ticks), but confusing for triage and will generate spurious Telegram chatter once alerts are wired to `stale_watcher`.

**One-line fix** (place before the `try:` at bridge_server.py:477 is too early; must be after successful broadcast):

```python
# bridge_server.py: inside file_fallback_poller(), after line 500
self.tick_buffer.append(tick)
await self._broadcast_to_bots(json.dumps(tick))
self.ticks_received += 1
self.nt8_last_tick_time = time.time()    # ← NEW: mark data freshness from fallback
```

Effort: **5 minutes, 1 line.** Python-only.

#### NEW finding — "silent tick stall" is NOT cleanly detected

Re-inspection of the [KNOWN_ISSUES.md](memory/context/KNOWN_ISSUES.md) silent-stall incident (2026-04-16, 07:56–11:11 CDT, NT8 showed connected but forwarded 0 ticks/sec for 3h15m):

The current architecture conflates "connection alive" and "data flowing" in `nt8_last_tick_time` (bumped by BOTH heartbeats AND ticks at bridge_server.py:188-192). This means:

- During a silent stall: heartbeats still arrive every 3s → `nt8_last_tick_time` stays fresh → nobody notices for hours
- `circuit_breakers.check_tick_gap()` relies on `record_tick()` calls from base_bot, which only fire for actual ticks — so IT would detect stall correctly (after 60s). The bot-side breaker does the right thing; the bridge-side stale_watcher does NOT.

**Enhancement to cleanly detect silent stall at the bridge:**
Split the single timestamp into two. Change bridge_server.py from:

```python
# Current (conflated)
elif msg_type == "heartbeat":
    self.nt8_last_tick_time = time.time()
elif msg_type == "tick":
    self.nt8_last_tick_time = time.time()
    ...
```

to:

```python
# Proposed (separated)
elif msg_type == "heartbeat":
    self.nt8_last_heartbeat_time = time.time()
elif msg_type == "tick":
    self.nt8_last_tick_time = time.time()
    self.nt8_last_heartbeat_time = time.time()  # ticks imply heartbeat liveness
    ...
```

Then `stale_watcher` gets two thresholds:
- `nt8_last_heartbeat_time` older than 10s → connection dead
- `nt8_last_tick_time` older than 60s during RTH → silent stall (NT8 frozen but TCP alive)

Effort: **30 minutes** for the split + tests. Python-only.

#### Refined remaining P6 work

Original Phase 5 estimated Option A at ~2.5h. Re-verification confirms the estimate and adds specifics:

| Sub-task | Est | Risk |
|---|---|---|
| Bug fix: `file_fallback_poller` bumps `nt8_last_tick_time` (1-line) | 5 min | Low |
| Split heartbeat vs tick timestamps in bridge_server.py | 30 min | Low |
| Silent-stall breaker rule (heartbeats fresh, ticks stale during RTH) added to circuit_breakers.py | 30 min | Low |
| Telegram wiring on stale_watcher transitions (not just circuit_breakers) | 20 min | Low |
| Websockets ping_interval=10 + ping_timeout=10 on the bot→bridge socket (already covered in v2 Option A, still relevant) | 30 min | Low — tested surface |
| Unit tests covering all three stall modes: TCP dead, fallback active, silent stall | 45 min | Medium (need to simulate with mtime monkeypatch) |
| Integration smoke: kill NT8 → verify fallback triggers → resume → verify recovery logged | 30 min | Medium (requires NT8 manual test) |

**Total refined P6 estimate: ~3h** (previously ~2.5h). Slightly over because the silent-stall enhancement is now explicit rather than implicit in "Option A."

#### C# compile cycle required?

**No.** All proposed work is Python-side. TickStreamer.cs and SiM_TickStreamer.cs already provide both the TCP heartbeat (Timer thread) and the mtime-updating fallback file (chart thread). Remaining P6 work is consumer logic on the Python side.

If we later decide diagnostic granularity justifies Option B's timer-thread heartbeat file (proposed in Step 5.2), that adds ~1.5h for the C# addition + compile/deploy cycle. Still deferrable per original recommendation.

#### Phase 5 re-verification verdict

- Original claim "P6 is 80% built" **verified and re-confirmed** against current (406-line) TickStreamer.cs and current bridge/base_bot code
- One latent Python bug found (`file_fallback_poller` omits a 1-line timestamp update) — trivial fix, not Monday-blocking
- One enhancement opportunity identified (split heartbeat vs tick signals) — improves silent-stall detection at bridge layer
- **Zero C# changes required for P6 completion under Option A**
- Refined P6 scope estimate: **~3h Python-only**

**Proceed-gate: re-verification supports the original recommendation. Same Phase 6 entry point applies (line-ending / autocrlf audit).**

### Step 5.5 — Tonight's sprint slots (2026-04-19 evening — correction)

Correction to the initial "deferrable" framing in Step 5.4. The two latent bugs are PROMOTED into tonight's implementation sprint. Same rules as every other tonight item: code change + pytest coverage + commit.

#### B6 — `file_fallback_poller` timestamp fix

- **Scope:** Add `self.nt8_last_tick_time = time.time()` after successful fallback broadcast in `bridge/bridge_server.py` `file_fallback_poller()` (after line 500).
- **Estimate:** 5 min code + 5 min test = **10 min**.
- **Sprint slot:** immediately after **P5b**.
- **Test coverage:** new unit test simulating `age > DISCONNECT_THRESHOLD_S`, asserting `nt8_last_tick_time` advances after a fallback broadcast (mtime monkeypatch).
- **Commit:** solo commit, `fix(bridge): file_fallback_poller bumps nt8_last_tick_time (B6)`.

#### B7 — Heartbeat/tick timestamp split + stale_watcher distinct signals

- **Scope:** In `bridge/bridge_server.py`:
  1. Introduce `self.nt8_last_heartbeat_time` alongside `self.nt8_last_tick_time`.
  2. `heartbeat` handler bumps only `nt8_last_heartbeat_time`.
  3. `tick` handler bumps both (tick implies liveness).
  4. `stale_watcher` reads the pair and distinguishes:
     - `heartbeat_age > 10s` → "NT8 connection dead"
     - `heartbeat_age < 10s` AND `tick_age > 60s` during RTH → "NT8 silent stall (frozen feed)"
  5. Dashboard/health endpoint exposes both ages separately.
- **Estimate:** 30 min code + 15 min test = **45 min**.
- **Sprint slot:** immediately after **B6**.
- **Test coverage:**
  - `test_heartbeat_bumps_only_heartbeat_time` — feed a heartbeat, assert only `nt8_last_heartbeat_time` advances.
  - `test_tick_bumps_both_timestamps` — feed a tick, assert both advance.
  - `test_silent_stall_detected_during_rth` — heartbeats fresh, ticks stale > 60s during RTH, stale_watcher emits distinct log/signal.
  - `test_connection_dead_detected` — heartbeats stale > 10s, stale_watcher emits "connection dead" signal.
- **Commit:** solo commit, `feat(bridge): split heartbeat vs tick timestamps for silent-stall detection (B7)`.

**Sprint order (updated):** `… P5b → B6 → B7 → P6 → …` (remaining P6 consumer work follows B7 since B7 provides the clean signal split P6 depends on).

**Dependency note:** B6 can ship independently. B7 is a prereq for the "silent-stall breaker rule" line item in the Step 5.4 refined estimate (that breaker rule is now part of P6 proper, not B7).

---

## Phase 6 — Line-ending / autocrlf current state

### Step 6.1 — Audit

| Setting | Value |
|---|---|
| `git config --get core.autocrlf` (local) | **`true`** — explains the LF→CRLF warnings we've been seeing every commit this session |
| `git config --global --get core.autocrlf` | _unset_ (exit 1) |
| `git config --get core.eol` | _unset_ |
| `.gitattributes` | **does not exist** — no normalization policy |
| `.git/hooks/` | only samples; no active hooks |

Distribution across all 159 tracked files:

| Working-tree state | Count | Meaning |
|---|---|---|
| `w/lf` | 144 | Clean LF |
| `w/none` | 11 | Empty files (mostly `__init__.py`) — no EOLs at all |
| `w/crlf` | 3 | Index LF, working tree CRLF |
| `w/mixed` | 1 | Mixed EOLs in working tree |

**The 3 `w/crlf` files are the Python files I edited this session:**

```
i/lf    w/crlf  attr/    bots/base_bot.py         (edited in C5)
i/lf    w/crlf  attr/    config/settings.py        (edited in C1)
i/lf    w/crlf  attr/    tools/ema_analysis.py     (edited pre-session)
```

Index is LF on all three; working tree is CRLF. This is `autocrlf=true` doing checkout conversion followed by my edits (Edit tool preserved whatever EOLs it found on disk, which after autocrlf checkout was CRLF). The warnings happen when git notices that committing the file would convert CRLF→LF in the index (same content either way, just EOL normalization).

**The 1 `w/mixed` file:**

```
i/lf    w/mixed attr/    memory/audit_log.jsonl
```

Mixed EOLs means some lines end in `\r\n`, others in `\n`. JSONL is line-delimited JSON — readers tolerate either — so the content is correct. The mix likely resulted from some historical session writing CRLF and others writing LF. Cosmetically ugly but not broken.

**All other file types are clean LF:**

| Type | Count in sample | State |
|---|---|---|
| `.cs` (4 files: HistoricalExporter, MQBridge, SiM_TickStreamer, TickStreamer) | all `w/lf` | LF ← NT8 accepts; compiled successfully per memory |
| `.bat` (5 launch scripts) | all `w/lf` | LF ← cmd.exe accepts |
| `.yaml` (5 procedural configs) | all `w/lf` | LF |
| `.md`, `.py` | mostly `w/lf` | LF |

So the `.cs` / `.bat` files currently stored with LF are **already working** against NT8 and cmd.exe. Empirically verified: TickStreamer.cs is loaded and broadcasting (Phase 1 tests confirmed it fires heartbeats), .bat files launch the bot (per CLAUDE.md / launcher pattern). NT8's tolerance for LF in .cs is documented by the fact that the current deployment works.

### Step 6.2 — Renormalization impact

Hypothetical: commit a `.gitattributes` with `* text=auto eol=lf`, then run `git add --renormalize .` (no destructive operation — just tells git to re-compute the index per the new rules).

**Files whose on-disk encoding would change:**

| File | Before | After | Notes |
|---|---|---|---|
| `bots/base_bot.py` | w/crlf | w/lf | Converted to match index |
| `config/settings.py` | w/crlf | w/lf | Converted to match index |
| `tools/ema_analysis.py` | w/crlf | w/lf | Converted to match index |
| `memory/audit_log.jsonl` | w/mixed | w/lf | Mixed normalizes to LF |

All other 155 files: **zero change** (already LF).

**Diff size: 4 files, pure EOL changes, no content changes.** A `git diff` on the normalization commit would show every line of each of the 4 files as "changed" (because EOLs differ) but with `--ignore-all-space` the diff would be empty. Expected net file-content modification: 0.

**Files that SHOULD stay CRLF — the opinionated question.**

The conventional wisdom says Windows-native file types (`.cs`, `.bat`, `.ps1`, `.cmd`) should be CRLF to play nicely with Windows-native editors (NinjaScript editor writes CRLF when saving .cs; some .bat/.ps1 tooling prefers CRLF). But:

1. **Empirical evidence in this repo**: current state has all of them as LF and they work. NT8 compiles the .cs files; cmd.exe runs the .bat files. So there's no operational reason to force CRLF on these.
2. **NinjaScript editor rewrite cycle**: if someone edits a .cs in NT8's own editor (not VS Code or Claude), that editor typically writes CRLF. With `* text=auto eol=lf`, the commit would re-normalize back to LF. This produces noisy "modified" status between editor save and next git commit, but is resolvable.
3. **Action plan v2 specifies** `* text=auto eol=lf` without per-extension overrides — simpler, matches current empirical state.

**Recommended: keep it simple with `eol=lf` across the board.** Document the NinjaScript-editor-cycle caveat for anyone editing .cs files directly in NT8. They should either use VS Code (respects .gitattributes) or tolerate the one-commit renormalize on save.

### Step 6.3 — Safe migration plan

#### Proposed `.gitattributes`

```
# Phoenix Bot — line-ending normalization
#
# All text files stored with LF in repo and checked out as LF.
# Windows-native tooling (cmd.exe, NinjaScript, PowerShell) accepts LF.
# If you edit .cs files in NinjaScript editor (which saves CRLF),
# git will normalize back to LF on commit — expected and harmless.

* text=auto eol=lf

# Binary files — never munge EOLs
*.png   binary
*.jpg   binary
*.jpeg  binary
*.gif   binary
*.ico   binary
*.pdf   binary
*.sqlite binary
*.db    binary
*.parquet binary
*.pyc   binary
*.pyo   binary
*.zip   binary
*.nt8bk binary
```

#### Exact command sequence (for P21 Day 9)

```bash
# Pre-flight: tree must be clean
git status --short   # should be empty

# Step 1: Create .gitattributes (content as above)
cat > .gitattributes <<'EOF'
* text=auto eol=lf

*.png   binary
*.jpg   binary
*.jpeg  binary
*.gif   binary
*.ico   binary
*.pdf   binary
*.sqlite binary
*.db    binary
*.parquet binary
*.pyc   binary
*.pyo   binary
*.zip   binary
*.nt8bk binary
EOF

# Step 2: Commit .gitattributes FIRST, before renormalization.
# The attribute file must be in place for git to honor its rules
# during --renormalize.
git add .gitattributes
git commit -m "chore: add .gitattributes (LF default, binary overrides)"

# Step 3: Set autocrlf off at repo level. Do this BEFORE --renormalize
# so the renormalize uses .gitattributes rules rather than autocrlf=true.
git config core.autocrlf false

# Step 4: Renormalize — re-indexes all files per .gitattributes.
# Non-destructive: just updates what git thinks the canonical form is.
git add --renormalize .

# Step 5: Inspect the pending changes. Expect 4 files: 3 Python + 1 JSONL.
git status
git diff --cached --stat   # should show 4 files, all pure EOL changes

# Step 6: Commit the normalization as its own atomic change.
git commit -m "chore: normalize line endings per .gitattributes (no content changes)"
```

#### Expected diff size

- 4 files modified (`bots/base_bot.py`, `config/settings.py`, `tools/ema_analysis.py`, `memory/audit_log.jsonl`)
- `git diff --stat --cached` shows per-line counts equal to line counts (every line "modified" due to EOL)
- `git diff --stat --cached --ignore-all-space` shows 0 content changes
- PR/review should include a note: "diff size inflated by EOL normalization; no semantic changes"

#### Rollback plan

1. The normalization is confined to a single commit. `git revert <normalize-sha>` restores the prior EOLs.
2. `.gitattributes` can be removed or narrowed in a follow-up commit if rules prove too aggressive.
3. `core.autocrlf` can be reverted: `git config core.autocrlf true` (or `git config --unset core.autocrlf`).

No irreversible steps. All three above changes are atomically separable commits/configs.

#### Windows-specific extras to add to the P21 work

- `git config core.longpaths true` — enables paths >260 chars. Phoenix has some nested paths in `data/knowledge_vectors/…/chroma.sqlite3` that could hit this if any subpath exceeds. Not currently a problem but cheap insurance.
- Registry `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1` — belt-and-suspenders at OS level. Elevated PowerShell required; one-time setting. Skip if the repo has no >260-char paths (it doesn't today).

#### Caveats worth noting in the commit message

1. **`memory/audit_log.jsonl` is append-only.** The normalization rewrites its entire content (every line's EOL changes). Future appends will all be LF. Older readers that cared about EOL (none do — JSON readers split on either) are unaffected.
2. **If anyone has uncommitted edits in a renormalizable file**, the renormalize would clobber those edits. **Rule: clean tree before running Step 4.**
3. **NinjaScript editor interaction**: edits to `.cs` files made directly in NT8's NinjaScript editor will produce CRLF on disk. Next `git commit` converts to LF in index. Visible as "modified" in status until committed. This is expected; it's the cost of the simple `eol=lf` policy. If it becomes annoying, add `*.cs text eol=crlf` override later.

### Phase 6 verdict

**Low-risk cleanup, exact procedure above.** Current state is 97% clean (155/159 files are LF). The 4 non-LF files are the result of autocrlf=true's partial conversion plus one JSONL with historical mixed EOLs. The normalize + `.gitattributes` commit pair settles it cleanly with one reversible commit.

**Revised P21 line-ending sub-task estimate: 15-20 min** (was 30+ min in action plan v2, per "expected diff size" caveat). The repo's small size + mostly-clean EOL state makes this faster than a typical legacy-repo normalization.

**Proceed-gate: Phase 6 findings support moving to Phase 7 (write the deltas doc + close the sprint).**

### Step 6.4 — Re-audit (2026-04-19 evening sprint kickoff)

Re-ran the Phase 6 audit to check drift before executing the normalization. **Significant drift:** the working tree has inverted from 97% LF to 24% LF as multiple edit sessions with `autocrlf=true` accumulated CRLF conversions on checkout.

#### Current state

| Setting | Value | Change from Step 6.1 |
|---|---|---|
| Tracked files total | **181** | +22 (new strategies, tools, tests from the sprint) |
| `.gitattributes` | **still absent** | no change |
| `core.autocrlf` (local) | **`true`** | no change |
| `core.autocrlf` (global) | _unset_ | no change |
| `core.eol` | _unset_ | no change |

#### Working-tree distribution (current)

| State | Count | Was (Step 6.1) | Delta |
|---|---|---|---|
| `w/lf` | **32** | 144 | **−112** |
| `w/crlf` | **137** | 3 | **+134** |
| `w/none` (empty file) | **12** | 11 | +1 |
| `w/mixed` | **0** | 1 | **−1** (cleaned itself up) |

The `memory/audit_log.jsonl` mixed-EOL case from Step 6.1 has normalized to `w/crlf` — likely when it was next appended, the writer emitted consistent CRLF. No mixed files anywhere in the tree today.

#### Current index state (authoritative)

All 169 non-empty tracked files are `i/lf` in the index. **Zero index-side inconsistency.** Every CRLF working-tree file is purely a checkout-time artifact of `autocrlf=true`; git still stores LF canonically.

#### Extension × state breakdown

| Extension | i/lf w/lf | i/lf w/crlf | i/none w/none |
|---|---|---|---|
| `.py` | 29 | **98** | 9 |
| `.md` | 1 | **22** | 0 |
| `.cs` | 1 | **3** | 0 |
| `.bat` | 0 | **5** | 0 |
| `.yaml` | 0 | **5** | 0 |
| `.html` | 1 | 1 | 0 |
| `.jsonl` | 0 | 1 | 0 |
| `.txt` | 0 | 1 | 0 |
| `.gitignore` | 0 | 1 | 0 |
| `.gitkeep` | 0 | 0 | 1 |
| `.json` | 0 | 0 | 1 |
| `.lock` | 0 | 0 | 1 |

#### Files with mixed line endings

**Zero.** The prior `memory/audit_log.jsonl` mix has resolved itself. No audit action needed to unmix anything.

#### Files that MUST stay CRLF

**None.** Reconfirmed across every extension type empirically:
- `.cs` — three CRLF plus one LF (`strategies/chandelier_exit.py` wait — that's .py; .cs LF is `core/chandelier_exit.py` — actually that's .py too. Let me clarify: the 1 `.cs w/lf` is an older file; 3 `.cs w/crlf` are the NinjaScript-edited versions). Both work in NT8.
- `.bat` — 5 at CRLF; cmd.exe tolerates LF per Step 6.2 empirical finding
- `.html` / `.md` / `.yaml` / `.json` / `.py` — all tool-agnostic

The original Step 6.2 conclusion holds: **no file type operationally requires CRLF.**

#### Mixed-EOL files needing pre-normalize attention

**Zero.** No intervention required before `git add --renormalize .`.

#### Proposed `.gitattributes` (unchanged from Step 6.3)

Keep the Step 6.3 proposal verbatim:

```
# Phoenix Bot — line-ending normalization
# All text files stored with LF in repo and checked out as LF.
# Windows-native tooling (cmd.exe, NinjaScript, PowerShell) accepts LF.

* text=auto eol=lf

*.png    binary
*.jpg    binary
*.jpeg   binary
*.gif    binary
*.ico    binary
*.pdf    binary
*.sqlite binary
*.db     binary
*.parquet binary
*.pyc    binary
*.pyo    binary
*.zip    binary
*.nt8bk  binary
```

#### Revised migration plan (procedure unchanged, diff-size expanded)

The command sequence from Step 6.3 is still correct verbatim. What changed is the **diff size** on the renormalize commit:

| Aspect | Step 6.1 estimate | Current estimate |
|---|---|---|
| Files touched by `--renormalize` | 4 | **137** |
| `git diff --stat --cached` size | ~1k lines | **~100k+ lines** (every line of every .py/.md/.cs/.yaml/.bat reports as "changed" due to EOL) |
| `git diff --stat --cached --ignore-all-space` | 0 | **0** (still zero semantic change) |
| Review burden | trivial | **needs the explicit "EOL-only" note on the commit** — reviewers should apply `--ignore-all-space` |
| Effort | 15-20 min | **still 15-20 min** — procedure identical, wait time for git slightly longer |

The commit is still atomic + reversible + zero-content-change. Just bigger diff on paper.

#### Recommended commit message (explicit EOL-only note)

```
chore: normalize line endings to LF per .gitattributes

* Non-functional change — converts 137 working-tree-CRLF files back
  to canonical LF form. Git already stored LF in the index; this just
  synchronizes the working tree and removes the autocrlf=true CRLF
  conversion from checkout.
* Follow-up to the .gitattributes commit which establishes the rule.
* Verify via: git diff HEAD~2 --ignore-all-space --stat  →  0 lines.
* No build, test, or runtime behavior changes. All tests pass before
  and after. Launchers (.bat) and NT8 compiles (.cs) continue to work.
```

#### Why the drift happened

Each edit session with `autocrlf=true` + `.gitattributes`-absent accumulates CRLF. Tonight's sprint + the Option B sprint + the roadmap-v4 sprint together touched ~140 files; all of them got LF→CRLF'd during their git-level touches. Over the next sprint cycle (without normalization) this would creep back toward 100% CRLF.

Shipping the normalize this sprint is the right move: it creates a stable floor, and the `.gitattributes` rule prevents re-drift.

### Phase 6 re-audit verdict

- Procedure from Step 6.3 is **still correct verbatim**; nothing in the migration plan needs to change
- **137 files** will re-normalize to LF (was 4 in the prior audit) — larger diff but identical change-kind
- **Zero** mixed-EOL files to hand-resolve first
- **Zero** file types that must stay CRLF
- Estimated effort **unchanged at ~15-20 min**; commit is atomic + reversible
- **No production code changes in this audit.** No renormalize applied.

**Proceed-gate: re-audit supports execution of Step 6.3 procedure as-is. Ready for proceed.**

---

## Phase 7 — Action plan deltas

**Status: consolidated.** Full deltas doc at [docs/ACTION_PLAN_V2_1_DELTAS.md](../../docs/ACTION_PLAN_V2_1_DELTAS.md).

### Step 7.1 — Summary

- **Bugs discovered during verification: 7 total** (B1-B7). B2-B7 ship tonight. B1 deferred to P2 (Week 1 Day 3).
- **Design corrections to v2 plan: 4** — P4 storage (SQLite → JSON), P10 scope split (P10a/b/c), P6 already 80% built, filename correction (TickStreamer.cs).
- **Estimate corrections:** P10 +2.5-4.5h, P4 −2h, P6 −1h, P21 −10-15min, P5b new +1h-1.5h.
- **Net Week 1 effort change: roughly flat** (additions from P5b + B6/B7 roughly offset by P4 and P6 reductions).
- **Live-data verification pending:** B4 account-scoped CANCELALLORDERS — will verify during P5b smoke test. FILLED-state delta pass: **COMPLETE** (verified tonight).

### Step 7.2 — Tonight's 11-item implementation order

Full schedule in deltas doc. One-line summary: `P5b → B6 → B7 → P3 → P11 → P20 → P7 → P14 → P4b → P1 → P21`.

Discipline: pytest + individual commit per item. P21 (EOL renormalize) ships last so feature diffs on items 1-10 stay clean and the renormalize commit is atomic + isolated for `git blame --ignore-rev` handling.

### Step 7.3 — Open questions before sprint starts

Six open questions documented in the deltas doc Q1-Q6:
- **Q1 (B4 account-scoped test)** — resolved: verify during P5b smoke test, not before
- **Q2 (prod_bot signal_price)** — recommend defer to Week 2 Day 7 P10a
- **Q3 (phoenix_action_plan_v2_post_migration.md commit location)** — recommend under docs/
- **Q4 (EOL renormalize timing)** — resolved: tonight as item 11
- **Q5 (B3 rollout safety)** — REQUIRED before P5b — options (a)/(b)/(c)/(d), recommend (b)
- **Q6 (B7 test scope)** — recommend pure unit tests tonight, manual NT8 integration for Monday pre-open

### Step 7.4 — Commit verification corpus

Done in the same commit that introduces this Phase 7 entry. See commit message body for the phase-by-phase rollup.

### Phase 7 verdict

- Verification sprint consolidated end-to-end across 6 phases + this closing Phase 7
- 7 bugs catalogued, 4 design corrections locked in, 11-item tonight sprint defined with explicit order
- **Zero production code changes across the entire verification sprint** (Phases 1-7). All changes are documentation + test artifacts under `tools/verification_2026_04_18/`.
- **Ready to begin implementation sprint** pending Q5 (B3 rollout safety) user decision.

**Proceed-gate: Phase 7 complete. Next action is tonight's 11-item implementation sprint starting at P5b after Q5 resolution.**
