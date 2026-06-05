# Propose-for-OA — bridge_server.py:639 WS EXIT → sized PARTIAL_EXIT

_Filed by Forensics Cluster 2 audit (2026-06-05). PROTECTED-file proposal — operator OA required before any edit ships._

## Background

Site #4 in `out/slot_interlock_bypass_audit_2026-06-05.md` — the bridge-level WS EXIT handler at `bridge/bridge_server.py:639`:

```python
paths = write_oif(action, qty, stop_price, target_price, trade_id=trade_id,
                  order_type=order_type, limit_price=limit_price,
                  account=account, direction=direction)
```

For `action == "EXIT"`, `write_oif` (PROTECTED at `bridge/oif_writer.py:1217`) emits a CLOSEPOSITION line at `bridge/oif_writer.py:354` and `:1296`. CLOSEPOSITION is account-wide on the NT8 side — the qty field is empty and NT8 flattens the net position across all strategies on that account. On multi-strategy Sim101, an exit from strategy A would flatten strategy B's position too.

This is the SAME root-cause class addressed in this sprint by:
- Site 5 fix (Phase 3g, `354bb3c`) — B47 STACKED FILL recovery
- Site 7 fix (this sprint, T1) — OCO-fail EMERGENCY FLATTEN
- Site 8 fix (this sprint, T2) — OIF EXIT fallback

The unprotected sites are now closed. The PRIMARY exit path remains: `bots/_trade_exit.py:134-140` sends WS `{"action": "EXIT", "qty": pos.contracts, "account": pos.account}` → `bridge_server.py:639` → `write_oif("EXIT", qty=pos.contracts, ...)` → CLOSEPOSITION. The bot passes a qty but the bridge translates to unbounded CLOSEPOSITION. The fix must live at the bridge layer.

## Why this is protected

`bridge/bridge_server.py` and `bridge/oif_writer.py` are both in `.claude/PROTECTED_FILES.md` Execution + risk + live-mode interlock zone. Per the canonical doc:

> A bug introduced here can cost real money on the next live trade. Treat them as the "Protected Zone" — never edit without an explicit operator go-ahead in chat.

## Proposed fix

Two surgical options. Option A is preferred (smaller blast radius); Option B has wider applicability but touches more code.

### Option A — bridge translates EXIT action to PARTIAL_EXIT op when qty > 0

**File:** `bridge/bridge_server.py` only (`write_oif` and `write_partial_exit` stay as-is)
**Estimated diff:** ~15 lines

In the WS message handler that calls `write_oif(action, ...)`:

```python
# 2026-06-05 SLOT-INTERLOCK-BYPASS T-bridge fix proposal:
# Translate EXIT (account-wide CLOSEPOSITION) to PARTIAL_EXIT (sized)
# when the bot passes a positive qty. Preserves CLOSEPOSITION semantics
# only for the explicit kill-switch / panic-flatten path where qty=0.
if action == "EXIT" and int(qty or 0) > 0 and direction:
    # Map to sized PARTIAL_EXIT — direction is from filled-position side,
    # PARTIAL_EXIT issues opposite-side MARKET sized to qty.
    from bridge.oif_writer import write_partial_exit
    paths = write_partial_exit(
        direction=direction, n_contracts=int(qty),
        trade_id=trade_id, account=account,
    )
else:
    paths = write_oif(action, qty, stop_price, target_price, trade_id=trade_id,
                      order_type=order_type, limit_price=limit_price,
                      account=account, direction=direction)
```

**Behavior change:**
- For typical exits (`action="EXIT"`, `qty > 0`, `direction` present from `pos.direction`): emits sized PARTIAL_EXIT instead of CLOSEPOSITION.
- For panic flattens / kill-switch (`action="EXIT"` with `qty == 0`): preserves CLOSEPOSITION semantics.

**Test plan:**
1. New `tests/test_bridge_ws_exit_partial_exit_translation.py`:
   - T-bridge-1: WS message with `action="EXIT", qty=2, direction="LONG"` → bridge calls `write_partial_exit(direction="LONG", n_contracts=2, ...)` and NOT `write_oif("EXIT", ...)`.
   - T-bridge-2: WS message with `action="EXIT", qty=0` (kill-switch path) → bridge calls `write_oif("EXIT", qty=0, ...)`.
2. Full pytest suite — no regression on existing bridge tests.

### Option B — `write_oif` internally dispatches EXIT-with-qty to write_partial_exit

**Files:** `bridge/oif_writer.py` (PROTECTED) — write_oif body
**Estimated diff:** ~20 lines

Make the dispatch live inside `write_oif` itself so other callers (if any future code calls write_oif directly with action="EXIT") inherit the fix automatically. Slightly wider blast radius but more uniform.

## Acceptance gate (operator)

Before approving:
1. Confirm `qty=0` is the canonical kill-switch path. Check `bots/_trade_entry.py` `_flatten_pending_entries`, `core/risk_manager.py` daily-loss-halt handler, and any other code that calls EXIT with intentional account-wide semantics.
2. Confirm `direction` field is always populated for normal exits (it's read from `pos.direction` in `bots/_trade_exit.py:139`).
3. Confirm `write_partial_exit` and `write_oif("EXIT", ...)` produce equivalent fill semantics on NT8 ATI for a single-strategy account (where their effects should be identical).

## Effort

- Option A: **S** (~15 LOC + 2 tests, ~1 hour)
- Option B: **M** (~20 LOC + 4 tests + audit of write_oif call sites, ~2-3 hours)

## Risk

LOW for Option A:
- Sized PARTIAL_EXIT semantics already proven on three other paths (sites 5, 7, 8) in this sprint and prior.
- The kill-switch carve-out (`qty == 0`) preserves CLOSEPOSITION semantics where intentionally account-wide.

LOW-MEDIUM for Option B:
- Wider call-site audit needed first.

## Recommendation

**Option A**, gated on operator sign-off + the acceptance-gate checks above. Ship in a dedicated protected-edit sprint with explicit `OPERATOR-APPROVED: 2026-06-XX` line in the commit message.

## Handoff coordination

PHANTOM-NT8 is currently editing `bots/_runtime_reconciliation.py`, `core/startup_reconciliation.py`, and may touch `core/position_manager.py` per its master prompt scope. PHANTOM-NT8 chat is NOT editing `bridge/bridge_server.py` so there should be no merge surface conflict — but for safety, this proposal should be shipped AFTER PHANTOM-NT8 closes its current round.

If PHANTOM-NT8 picks this up: see also `out/handoff_to_phantom_nt8_2026-06-05.md` for coordination notes.
