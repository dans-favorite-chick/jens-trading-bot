# Phoenix Protected Files — Canonical Policy

_Last updated: 2026-05-27_

This is the **canonical source of truth** for files that require operator
sign-off before any edit. `CLAUDE.md` references this document; the
pre-commit hook at `.githooks/commit-msg` enforces it; the test suite
at `tests/test_protected_files_policy.py` keeps it honest.

If a file appears in this document, you (Claude or any agent operating in
this repo) MUST follow the protocol below before editing it. Bypassing
this policy can cost real money on the next live trade.

---

## Protocol

When a change to a protected file is needed:

1. **Propose the diff in chat first.** Describe what you want to change in
   plain English, show the exact `old_string` / `new_string` (or the
   patch hunk), and explain the rationale.
2. **Wait for an explicit go-ahead from the operator** — phrases like
   "yes, ship it", "approved", "go ahead", or a thumbs-up emoji count.
   Silence does NOT count. Vague signals ("sure", "if you want") do
   NOT count. Ambiguous? Ask.
3. **Ship the edit.** Run the relevant focused test files immediately,
   then the full pytest suite. Report the totals.
4. **Commit message MUST include** an `OPERATOR-APPROVED: <YYYY-MM-DD>`
   line on its own line. Example:

   ```
   risk_manager: tighten weekly cap to $600 per F-02 closure

   OPERATOR-APPROVED: 2026-05-25

   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   ```

   The pre-commit hook will reject commits that touch a protected file
   without that exact regex pattern in the message.

5. **Audit trail.** The approval date becomes searchable forever via
   `git log --grep "OPERATOR-APPROVED"`.

If a non-protected file edit accidentally requires editing a protected
one to work (e.g. you need to add a config knob the risk_manager reads):
**STOP and ask.** Do not bundle the protected-file change into a "small
refactor" commit. Split it.

---

## Protected Zone — full file list

### Execution + risk + live-mode interlock

| File | Why protected |
|---|---|
| `bridge/oif_writer.py` | Writes OIF orders to NT8. Any bug here can place wrong-side/wrong-qty live orders. |
| `bridge/bridge_server.py` | WS hub between NT8 and bots. A bug breaks the entire pipeline. |
| `core/risk_manager.py` | Daily/weekly loss caps, recovery mode, Kelly sizing. |
| `core/portfolio_risk_gate.py` | Cross-strategy exposure cap. Single source of truth for portfolio risk. |
| `core/pending_entry_tracker.py` | 6-terminal-state guarantee on every LIMIT entry. |
| `core/nt8_order_id_capture.py` | Atomic stop modify (cancel-replace). Bug = orphaned stops. |
| `core/live_canary_gate.py` | The interlock that refuses to start prod_bot with non-allowlisted strategies. |

### Position + state plumbing

| File | Why protected |
|---|---|
| `core/position_manager.py` | Position state machine, open/close/scale-out math. |
| `core/trade_memory.py` | Canonical reader/writer. Raw json open breaks SQLite shadow. |

### Live-mode config switches

`config/settings.py` is NOT fully off-limits — only these specific symbols:

| Symbol | Why protected |
|---|---|
| `LIVE_TRADING` | The master live/sim flag. |
| `LIVE_STRATEGY_ALLOWLIST` | Which strategies are allowed in live. Currently `("bias_momentum",)` (canary). |
| `DAILY_LOSS_LIMIT` / `WEEKLY_LOSS_LIMIT` | Cap hierarchy. |
| `PER_STRATEGY_DAILY_LOSS_CAP` | Per-strategy halt. |
| `INSTRUMENT` / `NEXT_CONTRACT` / `ROLL_DAYS_BEFORE_EXPIRATION` | Wrong instrument = wrong trades. |
| `PENDING_ENTRY_TIMEOUT_S` | If raised too high, stale limits can fill late. |
| `AGENT_*_ENABLED` flags (all of them) | Re-enabling AI in live without uplift evidence violates P0-4. |

`config/strategies.py` is NOT fully off-limits — only these specific patterns:

| Pattern | Why protected |
|---|---|
| `FREEZE_ACTIVE` flag | Production-decision freeze. Flipping it to False re-opens kill-list / Wilson-CI promotion / `tier_3000` decisions. |
| `validated: True` flips on any strategy | Promotion to live. Requires Wilson n≥100 + walk_forward_gate PASS. |
| `walk_forward_gate: "hard_block"` flips | Strictest gate; only `bias_momentum` carries it today. |
| `enabled` flips on a `validated=True` strategy | Killing or reviving a live strategy. |

### Bot entrypoints

| File | Why protected |
|---|---|
| `bots/prod_bot.py` — `only_validated` property + main entrypoint | The last gate that blocks `validated=False` strategies in live. |

---

## What IS safe to edit without operator sign-off

- Any file under `strategies/*.py` EXCEPT `base_strategy.py` (the `Signal` interface contract).
- Any file under `tests/`.
- Any file under `tools/` (read-only analysis tools).
- Any file under `dashboard/` (operator-facing display, doesn't touch execution).
- Documentation under `docs/`, `memory/`, `.claude/` (except this file).
- Strategy parameter tuning within an existing strategy's config block
  (e.g. `stop_atr_mult`), **as long as** `validated`, `enabled`, and
  `walk_forward_gate` are unchanged.
- New files in any directory.

If you're unsure, the safe default is: **assume protected and ask.**

---

## Test enforcement

`tests/test_protected_files_policy.py` keeps three invariants honest:

1. Every file listed above exists in the repo (catches a rename that
   left the policy stale).
2. The pre-commit hook script at `.githooks/commit-msg` exists and
   matches the canonical SHA hash recorded in the test (catches a
   hook disablement attempt).
3. The list in this file matches the list in `CLAUDE.md` (catches
   drift between the two).

When you add a new protected file: update this file, update
`CLAUDE.md`, and update the test fixture.

---

## Commit-msg hook details

Script: `.githooks/commit-msg` (Python).
Activate per-clone: `python tools/install_git_hooks.py`
(sets `git config core.hooksPath .githooks` and verifies).

Why commit-msg (not pre-commit):
  Pre-commit fires BEFORE the user provides the commit message, so it
  cannot validate the message — there's nothing to read yet. commit-msg
  is git's documented stage for message validation; git passes the
  pending message file as `sys.argv[1]`. An earlier 2026-05-27
  implementation was placed at .githooks/pre-commit and relied on
  `git log -1 HEAD` as a fallback, which returns the PREVIOUS commit's
  message and never has the new approval tag — that falsely blocked
  every protected commit. Moved to commit-msg the same day.

Behavior:
- Scans the **staged** diff (not the working tree) for any file or
  protected-symbol change.
- If found, reads the pending commit message from the file git passes
  as `sys.argv[1]`.
- Pattern `^OPERATOR-APPROVED:\s+(\d{4}-\d{2}-\d{2})\s*$` must match
  on its own line.
- Approval date must be within the last 7 days (older = stale).
- On failure: prints the offending file + a clear remediation message
  to stderr, exits 1, commit is aborted.

Bypass (operator-only, emergency): `git commit --no-verify`.
`--no-verify` SHOULD be a last resort.

---

## Standing operator instructions

- "Always `git push origin <branch>` after any save-and-commit action"
  (per `memory/feedback_auto_push_after_commit.md`) — applies AFTER
  protected edit + approved commit, NOT before.
- "After every fix output the Phase completed and the Findings fixed"
  (per `memory/feedback_phase_findings_output.md`) — applies to ALL
  fixes, but especially important for protected-zone changes since they
  tie to audit findings.
- Never raw-open `logs/trade_memory.json` — use
  `core.trade_memory.load_all_trades()`. (Enforced because the legacy
  single-file path is frozen and reading it gives stale data.)
