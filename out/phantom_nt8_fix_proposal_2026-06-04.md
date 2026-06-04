# PHANTOM-NT8 Fix Proposal

_Phase 7 output of the 2026-06-04 diagnostic sprint. This is a PROPOSAL only — the protected-edit sprint that ships any of this is a separate, OA-required workstream. **Operator should walk through Section 0 BEFORE any code-side change ships, because the root cause is NT8-side and may resolve operator-side without code at all.**_

---

## 0. Operator pre-flight (no code; do this first)

The diagnostic could not independently disambiguate four possible NT8-side causes. Walking these in order takes ~10 minutes and may eliminate the need for code changes:

| Step | Action | What it tells you |
|---|---|---|
| 0.1 | In NT8: **Tools → Options → General → ATI** tab. Confirm "Enable" is checked AND the path is `C:\Users\Trading PC\Documents\NinjaTrader 8\` (or matches `NT8_DATA_ROOT`). If unchecked, check + restart NT8. | Most-probable cause #1. |
| 0.2 | In NT8: **NinjaScript Editor → AddOns folder** → confirm `PhoenixOIFGuard` shows as loaded with green checkmark. If not, F5 → restart NT8. | Most-probable cause #2 — and disconfirms red-team’s "silence may mean alive" reading either way. |
| 0.3 | In Windows: **Defender → Virus & Threat Protection → Exclusions** → confirm `C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\` is excluded. (2026-04-23 `PhoenixOIFGuard.log` shows `Access to the path is denied` — a Defender signature.) | Most-probable cause #3. |
| 0.4 | In NT8: **Control Center → Connections** → confirm the primary connection (not `My Coinbase`) is Connected. If primary is down, reconnect. | Most-probable cause #4. |
| 0.5 | After steps 0.1–0.4: drop a manual test OIF in `incoming/` and watch NT8 Log tab + Phoenix bridge log. If NT8 processes it within 100 ms, the OIF consumer is healthy and the rest of this proposal is for the longer-term observability gap, not an active fix. | Differentiates class-1 (24-h outage) from class-2 (per-OIF transient). |

If after Section 0 the NT8 OIF consumer is alive and processing test OIFs, **DO NOT ship code from Sections 2–4 today.** The 232 stuck files in `incoming/` will drain naturally (or operator can manually clear them via `del C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\oif*_RECONCILED_*.txt`). Schedule the code-side work for next sprint as observability hardening, not incident response.

If Section 0 does NOT recover the consumer, proceed to Section 2+.

---

## 1. Proposed fix — minimum scope (no protected files)

Three changes; all in **non-protected** files. The red-team review explicitly disallowed touching `bridge/oif_writer.py` or `bridge/bridge_server.py` because `diagnose_oif_pipeline_health()` already exists at `bridge/oif_writer.py:50-126` and provides the data needed.

### Fix A — Reconciliation-path cleanup mirror of `_enter_trade`'s PhantomGuard
**File:** `core/position_manager.py` (and any reconciliation orchestrator that catches `OIFStuckError` from `oif_writer._verify_consumed`)
**Protected:** NO (per `.claude/PROTECTED_FILES.md` — only the *risk* and *bridge* core files are protected; position_manager is not)
**Change:** mirror the `bots/_trade_entry.py:912-951` PhantomGuard pattern. On `OIFStuckError` raised inside a reconciliation loop, instead of retry-write, invoke the same `os.remove` cleanup on the stuck OIFs, log a CRITICAL `[PHANTOM_GUARD:RECONCILED_<tid>]` entry, and stop the retry loop after N=3 attempts.
**Acceptance:** simulated reconcile-loop test: write 10 RECONCILED OIFs against a monkeypatched non-consumer NT8, expect ≤ N×count_strategies files in `incoming/` after 30 s — current behavior leaves all 10, fix leaves at most 3.

### Fix B — Surface `diagnose_oif_pipeline_health()` to the dashboard + watcher_agent
**File:** `dashboard/server.py` (display) + `core/watcher_agent.py` or `agents/watcher_agent.py` (alerting)
**Protected:** NO
**Change:** import and call `diagnose_oif_pipeline_health(stale_incoming_threshold_s=30)` every 60 s from the watcher; if `healthy is False`, fire a single dedup-keyed Telegram alert + flip a `PIPELINE_DEAD` flag the dashboard surfaces in a red banner. Already-built helper at `bridge/oif_writer.py:50-126`. **No new code in the protected file** — just invocations from non-protected callers.
**Acceptance:** force a file to be 5 minutes old in `incoming/` (or use a tmp fixture); assert one Telegram fire + dashboard banner appears.

### Fix C — Instrument PhantomGuard fires as structured `logs/history/*.jsonl` events
**File:** `bots/_trade_entry.py` (the `[PHANTOM_GUARD:…]` log line at line 923 and Case-(b) log at line 977)
**Protected:** NO
**Change:** in addition to the existing `logger.error(...)` and `logger.info(...)` lines, emit a JSON line via `self.bot.history.append({...})` with `event="phantom_guard_fire"` or `"entry_pending"`, including trade_id, account, strategy, stuck-file count, and an `oif_stuck` boolean. Makes Phase-4-style audits automatic for any future investigation.
**Acceptance:** trigger an actual PhantomGuard fire in a test fixture; assert one new JSON line in the day’s history file with the expected fields.

---

## 2. Files touched + protection status

| Path | Protected? | Change kind |
|---|---|---|
| `core/position_manager.py` (Fix A) | NO | Add reconciliation-cleanup branch on `OIFStuckError`. |
| `dashboard/server.py` (Fix B) | NO | Import + call existing `diagnose_oif_pipeline_health()`; add red-banner state. |
| `core/watcher_agent.py` (Fix B) | NO | Periodic call + Telegram dedup-fire on unhealthy result. |
| `bots/_trade_entry.py` (Fix C) | NO | Add JSONL event emission alongside existing logger.error/info. |

**Zero protected-file edits.** `bridge/oif_writer.py` and `bridge/bridge_server.py` are deliberately untouched — `diagnose_oif_pipeline_health()` already exists and is callable from non-protected code.

---

## 3. OPERATOR-APPROVED required? — **NO** (for the Section 2 fix scope)

Section 2 fixes do not modify any file in `.claude/PROTECTED_FILES.md`'s zone. They can ship via the normal commit + push workflow.

**Operator OA required** only if a future sprint chooses to harden the bridge’s own `_verify_consumed` retry behavior (which would touch `bridge/oif_writer.py` and is explicitly out of scope here).

---

## 4. Estimated effort

- **Fix A (reconciliation cleanup):** **S** — ~20 lines mirroring `bots/_trade_entry.py:912-951`, one new test in `tests/test_reconcile_phantom_guard.py`.
- **Fix B (health surface + alert):** **S** — ~30 lines across two files, one test.
- **Fix C (structured PhantomGuard events):** **S** — ~10 lines, one assertion in existing PhantomGuard tests.

Total: **S–M** for a single afternoon protected-edit-free sprint.

---

## 5. Acceptance criteria

After ship, simulate 100 signal-to-fill cycles against a monkeypatched NT8 that:

- (run 1) consumes every OIF within 200 ms → expect 100 / 100 fills, zero PhantomGuard catches, zero pipeline-health alerts.
- (run 2) consumes 95 / 100 OIFs and silently drops 5 → expect 95 / 100 fills, **5 PhantomGuard catches with 5 distinct stuck-file deletions and 5 JSONL `phantom_guard_fire` events**, one dedup-keyed Telegram. Pipeline-health alert fires only if the oldest stuck file persists > 30 s.
- (run 3) consumes 0 / 100 OIFs (total NT8 outage) → expect 0 / 100 fills, 100 PhantomGuard catches with deletions, **1 (deduped) Telegram alert per strategy / account combo**, **continuous pipeline-health DEAD banner on dashboard**, reconciliation-cleanup branch fires after the 3rd retry on any flatten attempt (so RECONCILED OIFs don't accumulate past `3 × strategy_count`).

---

## 6. Rollback plan

All three fixes are additive. Rollback = `git revert <sha>`. No state migration. No data shape change. No protected-file rollback complexity.

If a fix accidentally breaks: Fix A’s worst case is reconciliation files accumulate as they do today (no regression). Fix B’s worst case is the dashboard banner doesn’t fire (no regression). Fix C’s worst case is the JSONL line doesn’t emit (no regression — the existing logger.error / logger.info remain). None of the fixes change the live-trade execution path semantics; they add observability and clean up an accumulator.

---

## 7. Out of scope — deferred to a separate protected-edit sprint

- Widening `bridge/oif_writer.py`'s `_verify_consumed` 2 s timeout. Tempting given the red-team's adversarial NT8-slow-thread analysis, but would require operator OA and risks introducing the *opposite* failure mode (orders accepted late, exiting at a bad price). Defer.
- Modifying `bots/_trade_entry.py`'s 5 s no-fill-ack timeout. Same reasoning — protected-zone-adjacent and easy to get wrong.
- Implementing a `FILE_SHARE_DELETE`-aware delete guard against the FN-1 race-condition the red-team highlighted. Material follow-up but needs its own design / OA cycle.
- Replacing the file-based OIF transport with NT8 native ATI sockets. Architectural change. Out of any near-term sprint.

---

## 8. Next recommended Cowork action

Review this fix proposal and the consolidated report at:
- `out/phantom_nt8_consolidated_report_2026-06-04.md`
- `out/phantom_nt8_fix_proposal_2026-06-04.md`

If Section 0 (operator pre-flight) recovers the OIF consumer, **defer Sections 2–4 to a routine observability-hardening sprint** rather than treating as incident response. If it does not, expand Sections 2–4 into a full master prompt for a non-protected-edit Cowork sprint — operator OA NOT required for that sprint scope.
