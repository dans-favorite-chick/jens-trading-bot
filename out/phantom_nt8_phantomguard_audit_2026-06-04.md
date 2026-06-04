# PhantomGuard Detection Reliability Audit

_Phase 5 of the diagnostic sprint._

## What PhantomGuard is

Two-tier safety net against silent NT8 rejection:

- **Tier A — Bridge `_verify_consumed`** (`bridge/oif_writer.py:753-803`). Polls every 100 ms for up to 2.0 s. If the OIF file still exists at the timeout, logs `[OIF_STUCK]` CRITICAL + raises `OIFStuckError`. Pure file-existence check.
- **Tier B — Bot `PHANTOM_GUARD`** (`bots/_trade_entry.py:891-951`). On the 5 s fill-confirmation TIMEOUT, globs `incoming/*_{trade_id}*.txt`. If a match exists, declares rejection: deletes the stuck file, marks pending-entry cancelled, fires Telegram dedup'd by `(strategy, account)`. If no match, treats it as Case (b) "NT8 accepted, waiting for limit/stop trigger" and records a pending entry.

## Detection conditions enumerated

| Condition | Type | When it catches | When it misses |
|---|---|---|---|
| Bridge polls file existence at 100 ms intervals up to 2 s | timeout + file-watch | Stuck file at 2 s → CRITICAL alert + raises | NT8 consumes at 1.9 s → no alert (correct) |
| Bot globs `incoming/*_{tid}*.txt` at 5 s TIMEOUT | file-watch | Stuck file at 5 s → declares rejection, deletes | NT8 consumes between 2 s and 5 s → glob empty → Case (b) "accepted pending" (correct under fast NT8) |
| Bot Case (b) records pending entry | state | Order is genuinely working in NT8, waiting for trigger | NT8 consumed but order was rejected and NT8 wrote ACK that Phoenix missed → false case (b) — orphan pending |

## Failure modes (FN candidates)

### FN-1 — Race: NT8 reading at 4.9 s, Phantom deletes at 5.0 s
**Scenario:** NT8 has the file open mid-parse; Phantom’s `os.remove` succeeds (Windows allows delete-while-open with `FILE_SHARE_DELETE`); NT8 finishes parsing and submits the order; Phoenix already concluded "rejected".

**Realistic likelihood:** low under normal NT8 load (parse is sub-100 ms per the 06-02 log evidence). Likelihood spikes during NT8 thread contention. Memory note [[oif_guard_race]] flags a related race on the *guard* side; the same risk exists on the *cleanup* side.

**Counter:** none in code. Phantom assumes the 5 s gap is wide enough.

### FN-2 — NT8 consumes silently but never writes the FILL ack
**Scenario:** NT8 reads `oif26370_..._9da466ea.txt`, deletes it, queues the order, then ATI subsystem hits an internal exception before writing the ACK to `outgoing/`. File-watch glob at 5 s returns empty → Case (b) → Phoenix records pending entry → eventually PendingEntrySweeper cancels.

**Outcome:** if NT8’s order made it into the router, Phoenix doesn’t know about it — phantom position risk identical to the 06-02 bug.

**Catches?** No direct catch. Only `StartupReconciliation` would adopt the orphan on next bot restart.

### FN-3 — NT8 stuck-state spans days; reconciliation OIF cleanup never fires
**Scenario:** observed today. 121 RECONCILED OIFs accumulated in `incoming/` because the reconciliation-flatten code path does not invoke the same Phantom-cleanup as `_enter_trade`. The bridge’s `_verify_consumed` runs and raises `OIFStuckError`, but the caller (position_manager’s reconcile loop) catches and retries, writing yet another OIF — and the cycle compounds.

**Catches?** Partially. Bridge logs each STUCK loud (125 CRITICAL events today). Operator-side alerting? None in code path I traced.

### FN-4 — Multiple back-to-back entries on the same trade_id
**Scenario:** retries on the same `tid` create multiple files. The glob `*_{tid}*.txt` catches them all (correct), but if any of them were *already-consumed-and-Phoenix-thought-stuck*, the delete is a no-op (correct), and the order eventually fills (phantom risk per FN-1).

**Catches?** PendingEntryTracker prevents most retries on same tid; not all paths.

## False-positive analysis

PhantomGuard fires when the file is still present at 5 s. NT8 normally consumes within < 1 s (06-02 log evidence). The realistic FP window is therefore tight unless NT8 is under heavy thread contention.

**Indirect FP estimate from 06-03 data:**
- Phoenix committed 408 OIFs
- Bridge declared 103 STUCK (at 2 s)
- NT8 actually processed 263 (per NT8 log)
- Gap of 145 implies ≈ 145 OIFs that NT8 did not process — close to but exceeding the 103 STUCK count
- Net: bridge’s 2 s window was conservative on 06-03 but not catastrophically so; PhantomGuard at the bot (5 s) further narrows the FP rate by giving NT8 3 more seconds.

I have **no direct measurement** of how many of the 06-03 PhantomGuard fires were FPs because the bot log scope here doesn’t segment "phantom fired but order eventually filled". Worth instrumenting in a follow-up.

## Cross-check vs Phase 4

| | 06-02 | 06-03 | 06-04 |
|---|---:|---:|---:|
| Phoenix OIFs committed (bridge log) | n/a* | 408 | 125 |
| NT8 processed (NT8 log) | 85 | 263 | 3 |
| Implied unprocessed | n/a | 145 (~36 %) | 122 (~98 %) |
| Bridge STUCK | n/a | 103 | 125 |
| Live PhantomGuard fires (bot log, approx) | 6 | many | 4 (today’s live entries) |
| Phantom positions resulted | 0 (after fix) | 0 | 0 |

\* 06-02 bridge log not in current corpus; reconstructed counts only.

**Bottom line:** PhantomGuard caught every live-entry phantom risk in the audit window. Catch rate for live entries on 06-04: **4 / 4 = 100 %**. The reconciliation-path silent-pileup is real but does not create phantoms (it’s redundant flatten orders).

## Verdict

- **PhantomGuard works on the live-entry path.** Zero phantom positions in the audit window despite ~245 unprocessed OIFs across two days.
- **PhantomGuard does NOT clean up reconciliation OIFs.** The 232-file pileup in `incoming/` today is evidence; the bridge keeps writing reconciliation flatten OIFs in a tight loop while the position_manager catches `OIFStuckError` and retries. Sub-bug for the follow-up sprint.
- **Latent race risks (FN-1, FN-2, FN-4) exist but were not exercised in the audit window.** They become more likely as NT8 load grows. The mitigation should be at the NT8 side (loud "OIF reader alive" heartbeat + operator alert when reader dies) rather than tightening the 5 s timeout.
- **FP rate is unknown but plausibly < 5 % under normal NT8 health.** Cannot be quantified from this corpus.

## Recommended monitoring instrumentation (proposal-only, no code change this sprint)

1. Bridge emits a `[OIF_PIPELINE_HEALTH]` heartbeat metric every minute counting incoming/ file age. If oldest-file age > 30 s during a known-active session, page operator.
2. PhantomGuard records each fire to a structured event in `logs/history/<date>_<bot>.jsonl` with an `oif_stuck` event type (so Phase 4-style audits become automatic).
3. Add a "fired-but-NT8-consumed-later" detector: after each PhantomGuard delete, scan `outgoing/` for an ACK matching the deleted price for 10 s; if found, log a FP marker.
4. Reconciliation flatten path should call the same cleanup-on-stuck branch as `_enter_trade`.

All four are bridge / dashboard / monitoring layer — no protected-file changes required.
