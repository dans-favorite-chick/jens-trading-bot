# Next-Sprint Outline — PHANTOM-NT8 Investigation

_Source: `out/bot_health_forensics_report_2026-06-04.md` §5. References `out/trade_firing_diagnostic_2026-06-04.md` lines 29-36 for the `9da466ea` timeline._

## Symptom + root cause

Today's lone bias_momentum SHORT signal (trace `9da466ea`, 05:05:12 CDT) was written cleanly to NT8 `incoming/` by `bridge/oif_writer.py`. NT8 silently failed to act on it — no fill, no acknowledgement, no rejection. PhantomGuard detected the absence of fill confirmation after 5s and aborted the entry, removing the stuck OIF leg. Phoenix's side of the boundary is intact: the bridge wrote the file, the guardrail caught the silence, the bot cleanly aborted. The failure is between the file landing in `incoming/` and NT8's ATI engine acting on it.

This is `FINDING-2026-06-04-PHANTOM-NT8` (OPEN in `docs/findings_tracker.md`). The finding row already explicitly notes "do NOT silently edit `bridge/oif_writer.py` (protected)" — confirming the investigation should happen NT8-side first.

## Proposed fix (3-5 bullets)

- **Diagnostic phase (no code change):** Read the NT8 trace log at `C:\Users\Trading PC\Documents\NinjaTrader 8\trace\20260604*.txt` for the 05:05:12 ± 60s window. Look for any "order rejected" / "instrument not found" / "outside session hours" / "ATI disabled" / "account not connected" lines tied to `9da466ea` or the OIF's order tag.
- **Confirm the NT8 chart instrument** matches `config/contract.json` exactly (front-month vs next-quarter mismatch silently swallows orders during the roll window). The roll is approximately 8 days before 3rd-Friday expiration — check whether today straddles that.
- **Verify ATI configuration in NT8 UI** (Tools → Options → ATI). Confirm: ATI enabled, file-watching path matches `config/settings.py:OIF_INCOMING`, "Confirm orders / modifications" trusted state. Re-enable requires NT8 restart per `nq-trading-skills` rule #5.
- **Check Sim101 account state at the same timestamp**: NT8 Control Center → Connections panel for any "disconnected" / "rejected" markers on Sim101 around 05:05 CDT. Includes data-feed status.
- **Build a smoke test:** `tools/oif_smoke_test.py` that writes a known-good test OIF (BUY 1 MNQ MARKET on Sim101 with $0 SL/TP placeholders) and waits for a fill confirmation. Run during off-hours to baseline NT8's ATI round-trip latency. Re-run before flipping any strategy to live. NOT in protected zone — uses `bridge/oif_writer.py`'s public write API, doesn't modify it.

## Files touched + protection

- **NT8 UI + NT8 trace logs** — operator inspection. No Phoenix code change.
- `config/contract.json` — read-only inspection (single source of truth, do not hardcode).
- `tools/oif_smoke_test.py` (new) — NOT protected. Safe to create.
- **`bridge/oif_writer.py` — PROTECTED.** If the investigation surfaces a writer bug, a separate fix-sprint with explicit operator OA is required.
- **`config/settings.py:OIF_INCOMING` / `OIF_OUTGOING` / `OIF_STAGING`** — read-only inspection.

## Operator OA needed?

**YES**, but only in two flavors:
- For NT8 UI changes (re-enable ATI, change account state) — operator must perform the click themselves.
- For any `bridge/oif_writer.py` edit if root cause surfaces a writer bug — explicit chat go-ahead per the `CLAUDE.md` protected-zone protocol, and commit message must include `OPERATOR-APPROVED: <YYYY-MM-DD>`.

For the diagnostic phase + the smoke test, NO operator OA needed — strictly read-only + new unprotected file.

## Effort: **S** (diagnostic) to **M** (if smoke test + NT8 trace analysis surface a writer issue)

- Diagnostic phase: 1-2h (NT8 trace grep + UI inspection).
- Smoke test: 2-4h (writer + verifier + off-hours run + baseline doc).

## Dependencies

- Independent of supervisor-drift / multi-PID / watchdog sprints.
- Round-2 remediation + confluence sprint already closed (Phase 0).
- Needs operator to be at the trading PC for NT8 UI inspection. Phoenix-side diagnostic (NT8 trace log grep) can run remotely.
- The smoke test should run during a quiet market window (e.g. weekend pre-Sunday-open or after RTH on Friday) — DO NOT run during an active trading session.

## Rank in queue

**#1 by severity.** Every signal that survives the firing-rate gates must clear this NT8 gate. 1-of-1 of today's signals failed (statistically meaningless sample, structurally complete failure mode). High operational risk even if low daily frequency.
