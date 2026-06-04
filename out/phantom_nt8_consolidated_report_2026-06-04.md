# PHANTOM-NT8 Diagnostic Sprint — Consolidated Report

**Date:** 2026-06-04
**Branch:** `weekly-evolution/2026-05-24`
**Refs:** `FINDING-2026-06-04-PHANTOM-NT8`

---

## 1. Executive Summary

Phoenix's bias_momentum SHORT signal at trace `9da466ea` was correctly generated at 2026-06-04 05:05:12.547 CDT, evaluated through the risk and micro-filter gates in 16 ms, and committed to NT8's `incoming/` folder as `oif26370_phoenix_172436_9da466ea.txt` containing a byte-correct `PLACE;Sim101;MNQM6;SELL;1;LIMIT;30315.25;0;GTC;;;;` instruction at 05:05:12.572. NT8 did not consume the file within 2 seconds. The bridge declared `OIF_STUCK` at 05:05:14.582; the bot's PhantomGuard fired at 05:05:17.865, deleted the stuck OIF, marked the entry cancelled, and routed a `(strategy, account)`-deduped Telegram alert. **Zero phantom position resulted.** The Phoenix safety net performed exactly as designed; the 05:05 incident is a *successful catch* of an underlying NT8-side condition, not a Phoenix bug.

Investigation across the bridge log, NT8 trace, NT8 main log, and PhoenixOIFGuard AddOn log establishes that NT8's OIF file-consumer was effectively offline during a **19-hour 27-minute window** from 2026-06-03 22:20:53 CDT to 2026-06-04 17:47:51 CDT. During that window NT8 processed zero file-based OIFs while its AtmStrategy and chart-UI order paths via the `(My Coinbase)` connection continued working normally. The 30-day audit shows this pattern is recurrent: **11 of 29 trading days had zero NT8-processed OIFs**, with multi-day blackouts in late May (05-27 through 06-01). Today is the worst working day in the window: Phoenix committed 125 OIFs (4 live entries, 121 reconciliation flattens); NT8 processed only 7 PLACE/CANCEL/MODIFY-class OIFs and 225 backlog CLOSEPOSITIONs after 17:47:51.

The red-team review (§5 below) downgraded the original H2′ verdict (PhoenixOIFGuard AddOn stuck/unloaded) from "high-confidence #1" to "most-probable but not independently confirmed", surfaced four alternative causes not ruled out by the diagnostic (Windows Defender RT Protection, NT8 ATI Tools→Options state, `(My Coinbase)` connection routing, backlog throttling), and narrowed the fix scope to remove protected-file edits in favor of leveraging the existing `diagnose_oif_pipeline_health()` function at `bridge/oif_writer.py:50-126`. The final fix proposal (Section 9) reflects those corrections.

---

## 2. The 05:05 Incident — full reconstruction

See `out/phantom_nt8_incident_report_2026-06-04.md` for the millisecond-resolved timeline, OIF disk content (`PLACE;Sim101;MNQM6;SELL;1;LIMIT;30315.25;0;GTC;;;;` — 41 bytes UTF-8 no-BOM CRLF), and the price-discrepancy note (INTENT logged @ 30315.50, OIF written @ 30315.25 — one-tick offset for sell-side fill, intentional). Key timings:

| Span | Duration |
|---|---:|
| SIGNAL → INTENT | 16 ms |
| INTENT → file in incoming/ | 9 ms |
| File in incoming/ → bridge `_verify_consumed` declares STUCK | 2 010 ms (2 s timeout) |
| File in incoming/ → bot PhantomGuard fires `[PHANTOM_GUARD:…] NT8 REJECTED order` | 5 294 ms (5 s no-fill-ack timeout) |
| Total SIGNAL → guard action | 5.32 s |

PhantomGuard then deleted `oif26370_phoenix_172436_9da466ea.txt`, marked PendingEntry cancelled with `reason="phantom_guard_rejected"`, and emitted a Telegram alert dedup-keyed `phantom_guard:bias_momentum:Sim101`. The trade did not enter; no exposure was taken.

---

## 3. The 30-day pattern audit (Phase 4)

See `out/phantom_nt8_pattern_audit_2026-06-04.md`.

**Headlines:**
- **2026-06-04 is the worst working day** in the 29-day window. NT8 processed only 7 routable OIFs (PLACE/CANCEL/MODIFY) plus 225 CLOSEPOSITIONs (mass backlog flush after 17:47:51). Phoenix committed 125 — implied silent-rejection rate ≈ 94 % on the routable subset (one operator-side caveat: see §5 red-team item on no-op CLOSEPOSITIONs).
- **The 19-hour outage is real and multi-source confirmed.** Last 06-03 OIF: 22:20:53. First 06-04 OIF: 17:47:51. PhoenixOIFGuard log dormant since 05-27 (interpretive caveat in §5).
- **11 / 29 days zero NT8 OIFs**, mostly aligning with weekends + the 05-27–06-01 multi-day idle stretch.
- **No strategy / regime / account concentration** beyond Sim101's higher exposure (it is the prod-bot account).

---

## 4. PhantomGuard reliability verdict (Phase 5)

See `out/phantom_nt8_phantomguard_audit_2026-06-04.md`.

- **Live-entry catch rate today: 4 / 4 = 100 %.** All four 05:05 / 05:11×2 / 05:14 entries were cleanly aborted with no phantom position. Telegram alerts fired with deduplication.
- **Reconciliation-path cleanup gap**: 232 RECONCILED OIFs accumulated in `incoming/` (two trace IDs `41a6a4ef`, `7dc435fd`) because the reconcile-loop in `position_manager` catches `OIFStuckError` and retries instead of invoking the same `os.remove`-on-stuck cleanup as `_enter_trade`. Sub-bug for the follow-up sprint.
- **Three latent FN candidates** documented (race on 5 s delete; missed FILL ACK; reconciliation cleanup), all unexercised in the audit window. Red-team’s adversarial NT8-slow-thread re-estimation (see §5) raises the FN-1 race-condition probability above Phase 5’s estimate when NT8 consume latency reaches 6–30 s.

---

## 5. Subagent + red-team verdicts (Phase 6 + 6.7)

### Phase 6 — three parallel verification subagents

- **R6.1 (OIF format check)** — *Phase 3 verdict CONFIRMED.* OIF byte-correct against `bridge/oif_writer.py:201-232` builders. Identical to successful 06-03 22:20:53 OIFs (`PLACE;Sim101;MNQM6;SELL;1;LIMIT;30256.00;0;GTC;;;;`) modulo price. Strongest counter-argument: format conformance is not the bug.
- **R6.2 (pattern re-tabulation)** — *Phase 4 bridge counts CONFIRMED (125 / 125). Phase 4 NT8-side count of "3 OIFs processed" UNDERSHOOTS the true value.* True 06-04 NT8 processed total ≈ 232 (7 PLACE / 0 CANCEL / 0 MODIFY + 225 CLOSEPOSITIONs flushed post-17:47:51 from backlog). The 19-hour gap shape is independently confirmed.
- **R6.3 (NT8 trace cross-ref)** — *Phase 2 evidence CONFIRMED.* No multi-line entries missed; no log rotation; PhoenixOIFGuard log last-touched 2026-05-27 22:55:59 confirmed; 19h gap confirmed in main log. 06-04 17:47:51 first OIF was a CLOSEPOSITION reconciliation flush, not a fresh entry — qualifying the "reader resumed" claim.

### Phase 6.7 — red-team review verdict: **HIGH CONCERN, not CRITICAL HALT**

The red-team accepted the safety-net verdict (PhantomGuard correctly caught the live entry; no phantom resulted) but flagged four diagnostic gaps and one scope-creep issue:

1. **PhoenixOIFGuard "dormant" evidence is misread.** Silence in the AddOn log is consistent with the AddOn being alive and silently passing phoenix-tagged files (its only logged events are startup / shutdown / quarantine / error). The "10-days-dormant" inference was wrong-direction. The AddOn’s state on 06-04 is *unknown*, not "confirmed unloaded."
2. **CLOSEPOSITION on flat account may be a silent NT8 no-op.** NT8 may accept a CLOSEPOSITION for a flat account, decline to act, AND not delete the file. The bridge's 2-second `_verify_consumed` would still fire `OIF_STUCK`. The implied 94 % silent-rejection rate on 06-04 could therefore be inflated by no-op CLOSEPOSITIONs that NT8 did process. The true *live-PLACE* rejection rate is 4 / 4 — still total, but the headline number needs scoping.
3. **Four alternative causes not ruled out.** In priority: (a) Windows Defender RT Protection locking files (2026-04-23 PhoenixOIFGuard.log showed `Access to the path is denied` errors — exact Defender signature); (b) NT8 Tools→Options→ATI tab disabled (non-auditable from disk); (c) `(My Coinbase)` connection routing — Sim101 orders consistently routed via Coinbase connection in NT8 trace, possible primary-connection-loss masking ATI state; (d) per-session ATI backlog throttle once the 232-file backlog accumulated.
4. **PhantomGuard FN re-estimation against adversarial NT8 slow-thread (consume latency 6–30 s) raises FN-1 race probability** above Phase 5’s "low under normal NT8 load" estimate. Phase 5’s verdict is sound under the specific 06-04 scenario (total NT8 ATI outage); it does not extrapolate to partial-latency scenarios.
5. **Scope creep on proposed fix.** Both protected-file edits the original proposal flagged (`bridge/oif_writer.py`, `bridge/bridge_server.py`) are unnecessary. The existing function `diagnose_oif_pipeline_health()` at `bridge/oif_writer.py:50-126` already does the equivalent of the proposed heartbeat and can be called from `dashboard/server.py` or `watcher_agent` — both non-protected.

---

## 6. Root-cause verdict (single sentence)

**NT8's file-based OIF consumer was unavailable for a 19-hour 27-minute window starting 2026-06-03 22:20:53 CDT, by one of (most-probable → least, none independently confirmed) NT8 ATI tab disabled / PhoenixOIFGuard AddOn unloaded after the last NT8 restart / Windows Defender Real-Time Protection blocking file reads / `(My Coinbase)` connection state masking ATI account routing — and Phoenix's PhantomGuard at `bots/_trade_entry.py:920` correctly caught the resulting 05:05 phantom risk with zero exposure taken.**

---

## 7. Self-critique (5 sentences)

1. **What could be wrong with the root-cause verdict.** The verdict names "NT8 OIF consumer unavailable" as the *what* but enumerates four possible *whys* without picking one — that is honest given the diagnostic limits, but means the fix proposal cannot be confident-pointed at a single mechanism, and an operator-side check sequence (see §9) is mandatory before any code-side change is shipped.
2. **What was assumed about NT8 ATI behavior but not verified.** That the AddOn log being silent for 10 days proves the AddOn isn’t running (red-team disconfirmed); that CLOSEPOSITION on a flat account triggers an NT8 log line (not verified — may be a silent no-op which would inflate the stuck rate); that NT8’s file watcher uses the same filter as `PhoenixOIFGuard.cs:136`’s `*.txt` (the `phoenix_markers.jsonl` errors suggest NT8 fires on `.jsonl` too).
3. **What sibling code paths could share the same bug.** Any subsystem writing files to `incoming/` outside the PhantomGuard-protected `_enter_trade` path is exposed — confirmed: `core/position_manager.py`’s reconciliation loop accumulates 121 unread OIFs today; suspected: emergency-flatten and OCO-cancel paths if NT8 hangs mid-trade.
4. **Smallest reproducible regression test.** Write a single test-only OIF to a tmp `incoming/` dir, monkeypatch the consumer to not delete it, run `bots/_trade_entry.py` end-to-end with a synthetic 5-second wait, assert `PHANTOM_GUARD` log line + `os.remove` call + `mark_cancelled("phantom_guard_rejected")` invocation + Telegram dedup-key match — gates against any future regression of the live-entry catch path (this test already exists at `tests/test_trade_entry_phantom_guard.py`; the regression coverage gap is on the reconciliation cleanup path which has no equivalent test).
5. **Best adversarial counter-argument re: the proposed fix.** An NT8 support engineer would say: "Your bridge is writing files faster than NT8’s ATI can processing-loop them under the current backlog; the 2-second `_verify_consumed` is too aggressive and turns a slow-but-working NT8 into a 'stuck' classification" — which would imply the right fix is widening the bridge timeout, not adding observability; this counter-argument is plausible for class-2 (per-OIF transient) but not for class-1 (19-hour outage) and the proposal therefore correctly prioritizes observability over timeout tuning.
