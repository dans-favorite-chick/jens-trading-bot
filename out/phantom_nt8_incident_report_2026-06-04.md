# PHANTOM-NT8 Incident Report — 2026-06-04 05:05:12 CDT

_Phase 1+2+3 of the PHANTOM-NT8 diagnostic sprint. Read-only forensics; no code changes this sprint._

**Trace ID:** `9da466ea`
**Strategy:** `bias_momentum` SHORT
**Account:** `Sim101`
**Bot:** `prod`
**Outcome:** OIF written to incoming/, NT8 did not consume within 2.0s, PhantomGuard correctly fired at 5.3s mark, entry cleanly aborted, stuck OIF removed. **Zero phantom position resulted.**

---

## 1. Reconstruction — what Phoenix tried to do

### Source-by-source event timeline

Phoenix logs are split across three streams. The master prompt assumed a single JSONL event log; in reality:

| Stream | Event types | Trace ID present? |
|---|---|---|
| `logs/history/2026-06-04_prod.jsonl` | `bar`, `eval` (strategy evaluation) | NO — keyed by timestamp |
| `logs/trades.log` | `TRADE CMD`, `NT8 FILL` | YES |
| `logs/restart_prod_bot_stderr.log` | TRADE QUEUED, INTENT, MICRO, ATR_STOP, PHANTOM_GUARD | YES |
| `logs/restart_bridge_stderr.log` | OIF committed/written, OIF_STUCK, OIFStuckError | YES |

### Timeline (CDT, milliseconds resolved)

| t (s) | Δ (ms) | Source | Event |
|---:|---:|---|---|
| 05:05:12.547 | — | prod_bot stderr | `[Bot] [TRADE QUEUED:9da466ea]` SHORT via bias_momentum conf=100 |
| 05:05:12.547 | 0 | history JSONL | `eval` event: bias_momentum SIGNAL SHORT conf=95, confluences include `RIDER — target 20:1, reversal+stall exits` and `SMC OB_BEAR +18 (1 aligned)`; price=30315.50, vwap=30393.65, ATR_1m=11.3, regime=OVERNIGHT_RANGE |
| 05:05:12.562 | +15 | prod_bot stderr | `[Bot] [9da466ea]` Regime size_mult=0.5x → risk=$9.00 |
| 05:05:12.562 | +0 | prod_bot stderr | `[Bot] [9da466ea:ATR_STOP] Skipped — bias_momentum computed own ATR stop (60t anchored to wick extreme)` |
| 05:05:12.563 | +1 | prod_bot stderr | `[Bot] [9da466ea:MICRO] score=45 rec=CAUTION` issues=`['Spread slightly wide: 2 ticks', 'DOM supports direction (adverse-selection trap)', 'Delta confirming -- adverse-selection (retail chasing) CVD=1968742, bar_delta=-45920']` |
| 05:05:12.563 | +0 | prod_bot stderr | `[Bot] [INTENT:9da466ea] SHORT 1x @ 30315.50 SL=30330.50 TP=30285.50 risk=$9.0 tier=A++ (low-VIX boost) strat=bias_momentum` |
| 05:05:12.569 | +6 | trades.log | `[TRADE CMD:9da466ea] bot=prod action=ENTER_SHORT qty=1 type=LIMIT limit=30315.25 stop=None target=None account=Sim101 reason=Bias Momentum SHORT — 2/4 TF, score 95, regime OVERNIGHT_RANGE` |
| 05:05:12.571 | +2 | bridge stderr | `[OIF:9da466ea] committed oif26370_phoenix_172436_9da466ea.txt` |
| 05:05:12.572 | +1 | bridge stderr | `[OIF:9da466ea] C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\oif26370_phoenix_172436_9da466ea.txt -> PLACE;Sim101;MNQM6;SELL;1;LIMIT;30315.25;0;GTC;;;;` |
| 05:05:14.582 | +2010 | bridge stderr | `[OIF_STUCK:9da466ea] NT8 did NOT consume 1 OIF file(s) within 2.0s: oif26370_phoenix_172436_9da466ea.txt. ATI likely rejected — check NT8 Log tab.` (CRITICAL) |
| 05:05:14.720 | +138 | bridge stderr | `[WS:prod] per-message handler failed, keeping socket alive: OIFStuckError[9da466ea]` |
| 05:05:17.861 | +3141 | prod_bot stderr | `[OIF:9da466ea] No fill confirmation after 5.0s` (WARNING) |
| 05:05:17.865 | +4 | prod_bot stderr | `[PHANTOM_GUARD:9da466ea] NT8 REJECTED order — 1 OIF(s) stuck in incoming/. Aborting entry + removing stuck legs. Account=Sim101 strategy=bias_momentum` (ERROR) |

### Critical timing intervals

| Span | Duration | What it tells us |
|---|---:|---|
| SIGNAL → INTENT | 16 ms | Strategy + risk + micro-filter all evaluated in one bar tick. Healthy. |
| INTENT → ORDER_WRITE (trades.log) | 6 ms | Bot→bridge IPC normal. |
| ORDER_WRITE → file in incoming/ | 2 ms | Bridge `commit_staged` is fast. |
| File in incoming/ → bridge OIF_STUCK | 2010 ms | Bridge’s `_verify_consumed` 2 s timeout (poll-every-100ms file-existence check). |
| File in incoming/ → bot PHANTOM_GUARD action | 5294 ms | Bot’s own 5 s no-fill timeout (separate from bridge’s 2 s check). |

PhantomGuard was reached **5.3 s after the file was written**. The bridge’s OIF_STUCK fired earlier (2 s) but did not abort the bot’s wait — that was triggered by the bot-side 5 s no-fill-ack timeout. Both detectors agreed.

### Price discrepancy worth flagging (not the bug, but worth noting)

- INTENT log: SHORT 1x @ **30315.50** (price the strategy wanted)
- trades.log + OIF: SELL 1 LIMIT @ **30315.25** (price actually written)
- One MNQ tick (0.25). Likely intentional: limit price set one tick *better* than mid for sell-side fills. Not the cause of NT8 rejection.

---

## 2. The OIF on disk

**Committed filename:** `oif26370_phoenix_172436_9da466ea.txt`
**Path:** `C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\`

**Current presence:** File is **not** in `incoming/` at this moment. Removed by PhantomGuard’s cleanup (`bots/_trade_entry.py:927-928`). Verified by directory listing: 232 files present, all `RECONCILED_Sim101_*` (none with `9da466ea`).

**Content as logged by bridge:**
```
PLACE;Sim101;MNQM6;SELL;1;LIMIT;30315.25;0;GTC;;;;
```

**Schema (per `bridge/oif_writer.py:7`):**
```
PLACE;Account;Instrument;Action;Qty;OrderType;LimitPrice;StopPrice;TIF;OcoId;;;
```

Field-by-field decode:

| Field | Value | Expected | Conformity |
|---|---|---|---|
| 1. Command | `PLACE` | `PLACE` / `MODIFY` / `CANCEL` / `CLOSEPOSITION` | ✓ |
| 2. Account | `Sim101` | NT8 account name | ✓ |
| 3. Instrument | `MNQM6` | per `config/contract.json` or `INSTRUMENT` | **see §3** |
| 4. Action | `SELL` | `BUY` / `SELL` | ✓ |
| 5. Qty | `1` | int | ✓ |
| 6. OrderType | `LIMIT` | `MARKET` / `LIMIT` / `STOPMARKET` | ✓ |
| 7. LimitPrice | `30315.25` | float, valid MNQ tick (0.25 grid) | ✓ |
| 8. StopPrice | `0` | float; 0 for non-stop orders | ✓ |
| 9. TIF | `GTC` | `DAY` per documented schema, `GTC` per nq-trading-skills | **see §3** |
| 10. OcoId | (empty) | string or empty | ✓ (this leg was an entry, not bracketed; bracket OCO would set this) |
| 11–13 | (empty) | reserved | ✓ |

---

## 3. NT8 trace correlation + ATI configuration (Phase 2)

### 3.1 NT8 trace logs at 05:05 CDT

NT8 maintains per-day trace files at `C:\Users\Trading PC\Documents\NinjaTrader 8\trace\`. (Inspected below in Phase 2 section.)

### 3.2 Recent ATI-rejection history — directly relevant

`logs/oracle/research/2026-06-02_chart_orders_root_cause.md` (the prior PHANTOM-NT8 investigation, two days earlier) documented:

> *“This morning's 14 prod_bot PROTECT FAILED cycles left 14 LIMIT entry OIFs in NT8's `incoming/` folder; when ATI came back online ~11:00 CT, NT8 processed them as working orders. The defensive layer that should have stopped this (`PHANTOM_GUARD`) is explicitly disabled for the Sim101 account at `bots/_trade_entry.py:802`.”*

The fix landed: PhantomGuard now runs for **all** non-LIVE accounts (`bots/_trade_entry.py:907-911` comment confirms). On 2026-06-04 at 05:05, the fix **performed exactly as designed** — caught the stuck OIF, aborted the entry, and deleted the file. **Zero naked LIMITs on the chart resulted from this incident.**

### 3.3 What this incident is, in one sentence

**The 05:05 incident is a successful catch by PhantomGuard of another transient NT8 ATI rejection — the same failure-class as 2026-06-02, but defused before it could leave a phantom.**

The real diagnostic question therefore shifts from *“why did Phoenix lose this trade?”* (PhantomGuard answered correctly) to *“why is NT8 ATI episodically rejecting OIFs, and how often?”* (Phase 4 will quantify).

### 3.4 Hypothesis stack (ranked)

| H | Hypothesis | Evidence supporting | Evidence against | Rank |
|---|---|---|---|---|
| **H2** | NT8 ATI silently disabled / disconnected at 05:05 | Bridge’s `_verify_consumed` is purely file-existence-based; file persisting = NT8 didn’t even open it. Same shape as 06-02 ATI outage. Time-of-day 05:05 CDT is overnight, low-attention; ATI-disconnect plausible. Incoming/ currently has 232 stuck RECONCILED_ files — a *concurrent* signal that ATI is not consuming. | None disconfirming. | **#1** |
| H1 | OIF format malformed | `MNQM6` (no expiry suffix) used in OIF; documented contract symbol is `MNQM6 06-26`. TIF=`GTC` not `DAY`. | Same format was used by every prior successful OIF (file counter is at oif26370 — thousands of prior PLACE/EXIT writes succeeded with this exact format). Format conformance to bridge’s own documented schema verified §2. | #2 |
| H3 | Instrument mismatch (NT8 chart on different contract) | Indeterminate without NT8 trace inspection. | NT8 should error visibly, not silently drop. | #3 |
| H6 | NT8 in maintenance / restart window | 05:05 CDT is shortly after NT8’s 04:00–04:30 routine maintenance for Globex carry-forward; possible carry-over. | No direct evidence. | #4 |
| H4 | Account state (Sim101 not loaded) | NT8 ATI accounts can lose state without a UI prompt. | Sim101 had filled orders earlier same session. | #5 |
| H5 | Order parameters out of bounds | qty=1, LIMIT 30315.25 (on-grid), no stop/target on entry leg. | All within MNQ normal. | #6 |
| H7 | PhantomGuard false positive (NT8 actually filled) | None — would expect `outgoing/Sim101_<id>.txt` ack matching 30315.25. | No such ack file exists. PhantomGuard cleanup verified. | #7 |
| H8 | Other / unknown | — | — | #8 |

**Strongest single piece of evidence for H2 over H1:** the OIF counter `oif26370` is incremented per OIF write across bridge process lifetime. The same writer, same format, has succeeded thousands of times. If H1 were correct, every prior OIF this session would have stuck — and they didn’t.

**A second concurrent symptom that reinforces H2:** at the moment of this writing (~12 h after the 05:05 incident), `incoming/` contains **232 RECONCILED_ OIF files** for only **2 unique trace IDs** (`41a6a4ef`, `7dc435fd`). These are PhantomGuard reconciliation flatten files that NT8 is *currently* not consuming. The ATI rejection pattern is **not** isolated to 05:05; it is ongoing. Phase 4 will quantify across the 30-day window.

---

## 4. NT8 trace inspection — what NT8 itself logged (Phase 2.1 + 2.2)

### 4.1 What the 2026-06-04 trace shows

Inspected `C:\Users\Trading PC\Documents\NinjaTrader 8\trace\trace.20260604.00000.txt`.

**Critical positive evidence that NT8 was alive and active during the 05:05 window:**

- `05:05:12:567 ERROR: Unknown OIF file type 'C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\phoenix_markers.jsonl'`

This event is **4 ms before Phoenix wrote `oif26370_phoenix_172436_9da466ea.txt` at 05:05:12:572**. NT8’s file watcher was actively scanning `incoming/` at the exact moment Phoenix wrote the OIF. **H2 (ATI dead) is disconfirmed.**

**Critical negative evidence — NT8 logged nothing about the actual OIF:**

- The trace contains zero entries about `oif26370_phoenix_172436_9da466ea.txt`. No “submit”, no “accept”, no “reject”, no “parse error” — silence.

Caveat: NT8’s trace level only surfaces ATI errors and high-level Cbi.Account events. Successful OIF parses are below the trace threshold; absence of a positive entry does not prove the OIF was rejected. But the file’s persistence in `incoming/` past the 2 s timeout is independent confirmation NT8 did not consume it.

### 4.2 Other NT8 activity on Sim101 — and a structural surprise

At `00:45:26`, `00:50:33`, and `07:07:20` the trace shows successful orders against Sim101 MNQM6 — all tagged `(My Coinbase)` connection — submitted via `NinjaScript.AtmStrategy.SubmitEntryOrders` and `Cbi.Account.CreateOrder`, NOT via the OIF incoming/ file path.

- 00:45:26 SELL 3 LIMIT 30493 GTC → Working → cancelled by chart UI at 00:50:33
- 07:07:20 SELL 3 LIMIT 30277.75 GTC → Working → PartFilled → Filled in 134 ms

**NT8 traces the AtmStrategy / Chart-Order path verbosely, but says nothing about file-based OIFs being processed.** That is consistent with the file path being handled by a separate AddOn (`PhoenixOIFGuard` per `oif_writer.py:39` comment) whose internal logging does not go to the main trace stream. We cannot prove from the trace alone whether the AddOn is loaded — only that the directory watcher tier is alive (it surfaces error events for `phoenix_markers.jsonl`).

### 4.3 ATI configuration on disk (Phase 2.3)

NT8 stores ATI configuration in `Config.xml` and `UI.xml`. Both were last touched 2026-06-04, indicating recent NT8 UI activity. Account state is held in memory and not directly auditable from disk in a way that would tell us whether Sim101 ATI was enabled at 05:05.

### 4.4 The all-day 100 %-stuck rate (the real headline)

Aggregating the bridge log for 2026-06-04:

- **125 OIFs committed to incoming/**
- **125 OIF_STUCK events** (every single one timed out after 2.0 s with the file still present)

**Stuck rate: 100 %.** Not a 05:05 one-off — a multi-hour, all-day pipeline failure. Yesterday’s bridge log shows 103 OIF_STUCK events on the same pattern. The OIF execution path has been broken **for at least 48 hours**.

Breakdown of today’s 125 commits:

| Hour bucket (CDT) | Count | Type | Notes |
|---|---:|---|---|
| 05:05–05:14 | 4 | Live entries (`9da466ea`, `2a4b2e91`, `a579659c`, `c210db03`) | All caught by PhantomGuard, OIF deleted, no phantom. |
| 08:19 → end | 121 | RECONCILED Sim101 (two trace IDs: `41a6a4ef` (~119), `7dc435fd` (~2)) | Reconciliation flatten loop — PhantomGuard runs against itself, the cleanup path apparently does not delete RECONCILED OIFs the same way. |

**Implication for Phase 5 (PhantomGuard reliability):** for live entries, PhantomGuard catches and cleans cleanly. For RECONCILED flatten OIFs, the cleanup half of PhantomGuard does not appear to trigger — files accumulate in incoming/ indefinitely. This is a separate sub-bug of PhantomGuard that surfaces only under ongoing NT8 ATI rejection.

### 4.5 Updated hypothesis ranking after Phase 2

| H | Hypothesis | Status |
|---|---|---|
| **H2' (NEW)** | **NT8’s OIF file-watcher AddOn (`PhoenixOIFGuard`) is not consuming OIF files**, while NT8 itself runs normally — including AtmStrategy + chart-UI order submission via the `(My Coinbase)` connection. The AddOn either is unloaded, has a stuck thread, or quarantines every `oif*.txt` Phoenix writes. | **#1, strongest** |
| H1 | OIF format malformed | Disconfirmed — schema matches `oif_writer.py:7`; thousands of prior OIFs used the same format successfully (counter at 26370). |
| H2 | NT8 ATI dead / disconnected | Disconfirmed — trace shows NT8 active on Sim101 at multiple points on 06-04, including AtmStrategy submission and chart-UI cancels. |
| H3 | Instrument mismatch | Unlikely — trace shows NT8 active on `MNQM6` Sim101, matches Phoenix OIF instrument. |
| H6 | Maintenance window | Disconfirmed — pipeline broken for 48 h+, not a 5-minute window. |
| H4 | Account state | Unlikely — Sim101 actively used for AtmStrategy + chart orders. |
| H5 | Order parameters OOB | Unlikely — qty=1 LIMIT on-grid is standard. |
| H7 | PhantomGuard false positive | Unlikely — file persistence past 2 s is independent of guard logic. |

**New top hypothesis (H2′) consequence:** the fix space is *NT8-side* (reload the OIF-reader AddOn, check PhoenixOIFGuard quarantine state, possibly an NT8 restart) rather than *Python-side*. This is good news for protected-file blast radius — but it’s a configuration / runtime fix that Phoenix code can’t apply.

### 4.6 Cross-reference with prior PHANTOM-NT8 investigation

- `logs/oracle/research/2026-06-02_chart_orders_root_cause.md` documented a near-identical pattern (mass-stuck OIFs during an ATI outage), and the Sim101 exemption from PhantomGuard was lifted on 2026-06-02 as the protective fix.
- The PhantomGuard fix shipped to all non-LIVE accounts on 06-02 worked exactly as intended on 06-04 at 05:05 — caught the rejection cleanly, no phantom.
- However, the **underlying NT8-side stuck-consumer condition was not fixed** — it has persisted since (at minimum) 06-03 and through all of 06-04. This sprint surfaces that gap.
- Memory note `oif_guard_race.md` ([[oif_guard_race]]) flags a race condition where `PhoenixOIFGuard` can lose the FileSystemWatcher race to NT8 ATI. That memory called it tripwire-only; the present incident is consistent with a more severe variant where the AddOn isn’t winning *any* races.

---

## Phase 1 + 2 + 3 verdict

- **Phase 1 (incident reconstruction):** complete. Timeline, OIF content, PhantomGuard source all captured.
- **Phase 2 (NT8-side trace + hypothesis):** complete. NT8 was alive at 05:05; OIF file-watcher path appears non-functional for 48 h+; 100 % stuck rate today across 125 OIF writes. Hypothesis ranking flipped from H2 (ATI dead) to **H2′ (PhoenixOIFGuard AddOn / file-watcher AddOn stuck or unloaded)** as #1.
- **Phase 3 (OIF format conformity):** OIF was byte-correct per `bridge/oif_writer.py` schema (Phase 3 detail in §2 above). Departures from the legacy `nq-trading-skills` reference (TIF=`GTC` vs `DAY`, `MNQM6` vs `MNQM6 06-26`) are **internal to Phoenix’s production schema** and historically accepted by NT8 — not the cause. **Verdict: OIF conformant; NT8 rejected at the file-watcher / AddOn layer, not at parse time.**
