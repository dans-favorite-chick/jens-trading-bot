# Slot Interlock Bypass Audit — 2026-06-05

_Forensics Cluster 2 — sibling code path audit triggered by PHANTOM-NT8 Round 2 (`0d7c9d4`) Bucket 2 red-team finding. This document is the coverage matrix referenced by `FINDING-2026-06-05-SLOT-INTERLOCK-BYPASS-T1/T2/T3`._

## Scope

Every code path that emits an OIF — either via `bridge.oif_writer.write_*` directly or via the `bots._oif_emitter.submit_*` sink. Categorize each as:

- **(A) Safe** — calls `is_flat_for` correctly (for entry paths) OR operates on a known position with correct attribution (for exit/modify paths)
- **(B) Bypass** — emits without consulting `is_flat_for` where it should, OR uses unbounded primitive (CLOSEPOSITION) instead of sized op
- **(C) Bypass-likely** — calls `is_flat_for` with wrong strategy attribution (e.g., `_reconciled_<account>` or mis-routed strategy)

## Background: what "bypass" actually means

The PHANTOM-NT8 0→SHORT 7 emergency (2026-06-04 ~17:50 CDT) was rooted in `_reconciled_<account>` attribution shifting a position out of its real strategy's slot. `is_flat_for("bias_momentum")` returned True even though there was an open Sim101 position attributed to `_reconciled_Sim101`. The next entry signal passed the gate, emitted a bracket order, and Sim101 ended up with two positions — one held by bias_momentum, one held by the orphan.

Sibling concerns:
1. **Entry-side gates** — a path that opens a new position MUST consult `is_flat_for` with the correct strategy attribution. Skipping = duplicate position.
2. **Exit-side scope** — a path that exits an existing position MUST use a SIZED op (PARTIAL_EXIT with qty N) rather than an account-wide CLOSEPOSITION. Account-wide CLOSEPOSITION on a multi-strategy account flattens unrelated strategies' positions.
3. **Reconciliation attribution** — a position adopted from orphan reconciliation under `_reconciled_<account>` makes is_flat_for stale. (Closed by REDTEAM-1 in `84ebf28` for single-strategy accounts; sub-strategy accounts remain OPEN as `FINDING-2026-06-04-REDTEAM-R2-2`.)

## Coverage matrix

| # | Site | Op | Qty source | Account | Strategy attribution | Could open new pos? | is_flat_for relevant? | Tag | Notes |
|---|------|-----|-----------|---------|----------------------|---------------------|-----------------------|-----|-------|
| 1 | `bots/_ws_dispatcher.py:725` | (dispatch gate) | n/a | n/a | `candidate.strategy` | n/a | ✓ ENTRY GATE | **A** | Per-signal interlock check before processing |
| 2 | `bots/_ws_dispatcher.py:730` | (dispatch gate) | n/a | n/a | `_pending_signal.strategy` | n/a | ✓ ENTRY GATE | **A** | Pending-signal interlock check |
| 3 | `bots/sim_bot.py:694` | (dispatch gate) | n/a | n/a | `strat.name` | n/a | ✓ ENTRY GATE | **A** | Sim-bot eval loop interlock |
| 4 | `bridge/bridge_server.py:639` | `write_oif(action, qty, ...)` | `data["qty"]` | `data["account"]` | implicit (action-dispatched) | YES (action=PLACE) | downstream of entry gate | **A*** | Receives WS-validated actions; bot has passed gate. **WS EXIT path uses CLOSEPOSITION** (see protected-file proposal). |
| 5 | `bots/_trade_entry.py:1109` | `_sink_submit_partial_exit` (STACKED FILL recovery) | `contracts` (entry's) | `_account` | `signal.strategy` (in tid + log) | NO (PARTIAL_EXIT, sized) | N (exit semantics) | **A** | Already remediated in `354bb3c` (REDTEAM-R2-1-FIX-V2). Mature pattern. |
| 6 | `bots/_trade_entry.py:1177` | `_sink_submit_protect` (OCO post-fill) | `contracts` | `_account` | `signal.strategy` (in tid) | NO (stop+target OCO only) | N | **A** | OCO bracket attaches to confirmed position; no new entry |
| 7 | `bots/_trade_entry.py:1255` | `_sink_submit_exit` (OCO-fail EMERGENCY FLATTEN) | `contracts` | `_account` | (no strategy passed) | NO (CLOSEPOSITION) | N (exit semantics) but **unbounded primitive** | **B** | Closed in this sprint: swapped to `_sink_submit_partial_exit` sized to `contracts`. Account-wide CLOSEPOSITION would flatten unrelated strategies on multi-strategy Sim101. |
| 8 | `bots/_trade_exit.py:146` | `_sink_submit_exit` (OIF FALLBACK after WS fails) | `pos.contracts` | `pos.account` | (no strategy passed) | NO (CLOSEPOSITION) but **unbounded primitive** | N | **B** | OIF fallback closed in this sprint: swapped to `_sink_submit_partial_exit` sized to `pos.contracts`. The PRIMARY WS path is at `bridge/bridge_server.py:639` (PROTECTED — propose-for-OA at `out/propose_bridge_server_ws_exit_sized_2026-06-05.md`). |
| 9 | `bots/_scale_out.py:92` | `_sink_submit_partial_exit` (SCALE-OUT) | `n_exit=1` | `pos.account` | implicit (from pos) | NO (PARTIAL_EXIT, sized) | N | **A** | Sized exit, position-attributed |
| 10 | `bots/base_bot.py:625` | `_sink_submit_modify_stop` (stop cancel+replace) | `pos.contracts` | `pos.account` | implicit (from pos) | NO (modify existing stop) | N | **A** | Sized cancel+replace |

## A* note on site 4 (bridge_server.py:639)

The bridge-level `write_oif` is the WS-message handler that the bot uses for primary action dispatch (entry PLACE, OCO PROTECT, EXIT, MODIFY_STOP, CANCEL). The bot has ALREADY passed `is_flat_for` before reaching this point for entry signals. For EXIT action specifically, the bridge writes an unbounded CLOSEPOSITION line — the same primitive that sites 7 & 8 used until this sprint. The bridge fix is documented in `out/propose_bridge_server_ws_exit_sized_2026-06-05.md` — DEFERRED to a protected-file sprint with operator OA.

## `original_contracts` hazard (Phase 3)

**Master prompt hypothesis:** "scale_out_partial leaves pos.original_contracts at the FULL original count after partial exit. If _positions is re-read post scale-out and the OCO writer fires against the original count → quantity-mismatched OIF."

**Audit verdict: HAZARD ABSENT IN CURRENT CODE — sentinel test shipped.**

Production reads of `pos.original_contracts`:
1. `bots/_ws_dispatcher.py:474` — `pos.original_contracts >= 2` (eligibility gate for scale-out)
2. `core/position_manager.py:1151` — `to_dict()` serialization

Production writes:
- `core/position_manager.py:668` — `original_contracts=contracts` set at open

**There is no production code path that reads `original_contracts` as a quantity argument to any OIF emit.** The field is designed as a stable property of the trade for eligibility gating only.

Sentinel shipped at `tests/test_scale_out_original_contracts.py`:
- Asserts `original_contracts` stays at the open-time value after `scale_out_partial`
- Asserts no production grep match exists for `original_contracts` passed as `qty`/`n_contracts` to any OIF emitter

Any future PR that violates the sentinel trips the test and forces a tracker discussion.

## Verdict summary

| Tag | Count | Sites |
|---|---|---|
| (A) Safe | 7 | 1, 2, 3, 5, 6, 9, 10 |
| (A*) Safe via upstream gate, with separate protected-file fix proposal | 1 | 4 |
| (B) Bypass — closed in this sprint | 2 | 7, 8 (OIF fallback half only) |
| (C) Bypass-likely | 0 | — |

**Net unprotected bypasses fixed: 2.** **Protected-file proposals filed: 1.** **Sentinels shipped: 2.**

## Cross-references

- `out/bot_health_forensics_report_2026-06-04.md` §5 — original PHANTOM-NT8 NT8 silent-rejection analysis
- `out/phantom_nt8_fix_proposal_2026-06-04.md` — PHANTOM-NT8 fix scope (no overlap with this audit)
- `docs/findings_tracker.md` rows: `FINDING-2026-06-04-CLOSEPOSITION-SIBLINGS` (now RESOLVED via sites 7+8 swap), `FINDING-2026-06-04-REDTEAM-R2-2` (sub-strategy slot interlock — OPEN, separate sprint), `FINDING-2026-06-05-SLOT-INTERLOCK-BYPASS-T1/T2/T3` (new this sprint), `FINDING-2026-06-05-STALE-ORIGINAL-CONTRACTS-SENTINEL` (new this sprint, STALE-NO-CODE-FIX-NEEDED with regression test)
