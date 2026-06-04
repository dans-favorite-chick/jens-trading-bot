# Red-Team Review — Confluence Sprint (2026-06-04)
Reviewer: Senior Principal Engineer / Red-Team Architect
Branch: `weekly-evolution/2026-05-24`, HEAD `08fa958`
Scope: Path X (field persistence) NO-SHIP verdict, Path Y (threshold drift) NO-SHIP verdict, new test file `tests/test_strategy_blocking_fields_persistence.py`

---

## 1. Verdict

**PASS-WITH-CAVEATS**

No HALT-CRITICAL. The NO-SHIP verdicts are defensible. The test file is NOT a pure false-confidence sentinel, but it has three verifiable weaknesses that reduce its regression coverage below what the docstring claims.

---

## 2. Findings

### F-01 — MEDIUM | `tests/test_strategy_blocking_fields_persistence.py:65-69` + `bots/_trade_entry.py:177-186`
**The hole:** `_apply_trade_entry_merge` is a hand-maintained copy of the production loop. The only divergence detector is the test asserting the *outcome*, not that the replica matches the source. If a developer adds a key to the production tuple at `_trade_entry.py:178-184` and forgets to update `_MERGE_KEYS` in the test, the new key is silently absent from the merge, the production snapshot loses that field, and all five tests continue to pass because none of them assert that `_MERGE_KEYS` is complete.
**Impact:** Future merge-key additions on the production side will not trip any test. The "fail-loud" claim in the test docstring (line 62) is false unless `_MERGE_KEYS` is actively maintained.
**Recommended fix:** Add a test (or assertion inside the existing tests) that imports the production tuple directly — e.g., parse `_trade_entry.py` for the tuple literal and assert set equality with `_MERGE_KEYS` — or extract the tuple to a shared constant both files import so divergence is structurally impossible.

### F-02 — MEDIUM | `tests/test_strategy_blocking_fields_persistence.py:277-325` + `tools/reconcile_sim_vs_backtest.py:159`
**The hole:** `_blocking_field_status` passes on `cvd_health = {}` (an empty dict). The check is `ms.get(f) in (None, "")`, which does not match `{}`. The empirical test `test_recent_sim_bias_momentum_trades_carry_blocking_fields` therefore counts a trade with `cvd_health={}` as "not blocked" and passes — while the reconciliation harness cannot actually replay the strategy branch on a stub-empty dict. Confirmed live: one trade in the last 30 has `cvd_health = {"veto": False, "agreement": 0.0, "reason": "stub"}` (non-empty but useless). The falsy-dict case would slip through both the harness and this test.
**Impact:** The 80% floor test can pass while some fraction of trades carry semantically-empty `cvd_health` dicts. Reconciliation accuracy is overstated.
**Recommended fix:** Extend `_blocking_field_status` to treat `cvd_health = {}` (empty dict) or `cvd_health` dicts where `veto is None and agreement == 0.0` as BLOCKED. Pin the test fixture to include a case for `cvd_health = {}`.

### F-03 — LOW | `tests/test_strategy_blocking_fields_persistence.py:136-165`
**The hole:** `test_evaluate_preserves_blocking_fields_in_market` swallows ALL exceptions from `evaluate()` with a bare `except Exception: pass` (line 153). If `evaluate()` raises before reaching any code that could mutate the fields, the test passes trivially — not because the fields survived, but because the mutation site was never reached. The test fixture at `_market_with_blocking_fields()` may be incomplete enough to trigger an early-bail path every time this runs.
**Impact:** Low probability of a false positive on the specific mutation-deletion failure mode, but the test may not be exercising the code it claims to exercise. If `evaluate()` consistently bails at an early gate, the assertion at line 156 is vacuously true.
**Recommended fix:** Log or assert that `evaluate()` returned without exception, OR restructure the fixture to be sufficient for a full evaluation pass. At minimum, add `# pragma: no cover`-style documentation on what path is actually exercised.

---

## 3. Verdict-stress

**Path X (NO-SHIP: wiring already present from 856f317):** The verdict holds. The stash is set at `sim_bot.py:789` and `_strategy_dispatch.py:911`, the merge loop is at `_trade_entry.py:177-186`, and `open_position` is called with the merged `market` dict at line 1306. Live data confirms 90% of the last 10 bias_momentum trades carry all four fields. The verdict would be wrong if there were a code path through `_enter_trade` that set a *different* stash reference or bypassed the merge — I found no such path. It would also be wrong if `_last_enriched_market` could be `None` at entry time due to a race; there is no lock, but the stash is set synchronously before `_pending_signal` is queued, and the event loop is single-threaded. The operator should trust this verdict.

**Path Y (NO-SHIP: threshold not a binding gate):** The verdict holds for the current 7-day window, but it makes a second, unstated assumption: that `min_confluence` is evaluated against the same score scale in both backtest and live. If `bias_momentum.evaluate()` computes confluence differently today than the backtester replays it (which the Phase 13 reconciliation unreconciled-to-live note in MEMORY.md explicitly flags as unresolved), zero `min_confluence` rejects in 7 days proves nothing about whether the threshold is correctly calibrated. The operator should NOT trust this verdict as a statement about threshold calibration — only as a statement that the threshold is not currently rejecting signals.

---

## 4. The honest "I don't know"

I could not verify (a) whether `test_evaluate_preserves_blocking_fields_in_market` actually executes past `evaluate()`'s first gate or exits early every run — running the test suite would confirm this; (b) whether the `es_nq_rs = None` trade in the blocked-1-of-30 count represents a genuine data gap (no intel poll before entry) or a regression introduced after 856f317; (c) whether the `cvd_health = {"reason": "stub"}` pattern observed in live data represents a real degradation path or a deliberate error-fallback that the reconcile harness handles downstream.
