# BUG #3 — 4 LOW/MEDIUM Audit Items Still Pending

Source: `logs/oracle/research/2026-06-02_bug_audit.md`. Commit
`de777f3` addressed C-1, H-1, H-2, H-3 (as comments), M-1, M-2, M-3.

This document classifies the remaining 4 items as INFRASTRUCTURE,
TUNING, or OBSOLETE.

---

## M-4 — Archive script DDL/DML transaction-ordering assumption

**Classification: OBSOLETE.**

**Location:** `tools/warehouse/archive_and_reingest_noise_area.py:76-109`.

**Original concern:** `CREATE TABLE … AS SELECT` (DDL) and `UPDATE
runs …` (DML) execute in the same connection before a single
`conn.commit()` at line 109. DuckDB's DDL-in-transaction behavior is
version-dependent; a crash between steps would leave the archive
present but `logical_group` not updated. The idempotence guard checks
only `archive_exists AND n_before==0`, so a re-run would skip step 2.

**Why obsolete:** the audit itself notes "this is a one-shot script
that has already been run successfully (40,525 rows confirmed
archived)." The script is not on a recurring cron and there is no
planned re-run against a different dataset. The transaction-ordering
hazard is academic.

**Action for implementation session:** if (and only if) the operator
plans to re-purpose this script for a future archive job, add the
two-commit pattern from the audit at that time. No proactive fix
today.

---

## L-1 — `test_research_budget` assertion change

**Classification: OBSOLETE (already correct).**

**Location:** `tests/test_strategy_oracle.py:219`.

The audit verified the assertion update (200_000 → 600_000) is
correct, with the commit hash cited inline. No regression concern.

**Action for implementation session:** none.

---

## L-2 — Lost "all-hours restoration" reminder in test rename

**Classification: TUNING (comment-only, opinion-driven).**

**Location:** `tests/test_prod_bot_validated_gate.py:45-62`.

**Concern:** the renamed test body says "If the operator wants to
revert to all-hours, update this test with the rationale" — preserving
intent but losing the specific Sprint-H window numbers
`[08:30-08:59, 10:00-13:29]`. Future readers can recover via
`git blame`.

**Why classified as TUNING:** how verbose to make test-file comments
is a style choice with no functional impact. The audit author's
preference (restore the specific windows) is reasonable; an opposing
preference (rely on git blame / commit messages) is equally
reasonable. Not the kind of thing a bug-sweep should auto-fix.

**Action for implementation session:** optional — if the operator
prefers more verbose test comments, append the window numbers in a
one-line edit. Otherwise leave as-is. Cosmetic.

---

## L-3 — No hard cap on Oracle spend per run

**Classification: INFRASTRUCTURE (but blocked on operator decision).**

**Location:** `agents/strategy_oracle.py:97` (cost projection),
`agents/strategy_oracle.py:1296` (soft-cap nudge).

**Concern:** the commit message acknowledges "$6-9 per research run —
operator-acceptable" but no runtime check enforces this. The existing
`token_budget` soft-cap nudge asks the model to "wrap up gracefully"
but does not hard-stop the loop. A prompt-engineering regression
that causes infinite `propose_change` retries could balloon the bill.

**Why classified as INFRASTRUCTURE (not TUNING):** unlike L-2's
cosmetic preference, this is a real defensive-coding gap. A hard
cap is the kind of fail-closed protection that should exist around
any LLM loop.

**Why "blocked on operator decision":** the actual numeric cap is a
business decision. Reasonable values are $10/run (1.1× operator
estimate), $15/run (1.7×), or $25/run (3×). The operator should pick
the value; the wiring is straightforward.

**Proposed fix outline (NO CODE):**
1. Add `ORACLE_HARD_CAP_USD` to `config/settings.py` (default
   $15.00 — operator can tune).
2. In `agents/strategy_oracle.py`, accumulate USD spend per call
   (already tracked for token_budget — extend to dollar terms).
3. When `accumulated_usd >= ORACLE_HARD_CAP_USD`, break the loop
   with a CRITICAL log + Telegram alert: "Oracle run aborted at
   $X.XX (hard cap). Partial output preserved."

**Action for implementation session:** ask operator for the cap
value, then implement. Single-file change in `agents/strategy_oracle.py`
plus a one-line `config/settings.py` addition. `agents/` is not
protected; `config/settings.py` is protected only for specific symbols
(LIVE_TRADING, allowlist, loss limits, instrument) — adding a new
`ORACLE_HARD_CAP_USD` constant does not touch any protected symbol.

---

## Summary table

| ID | Class | Action |
|---|---|---|
| M-4 | OBSOLETE | None (script already ran successfully) |
| L-1 | OBSOLETE | None (test already correct) |
| L-2 | TUNING | Optional cosmetic edit (operator choice) |
| L-3 | INFRASTRUCTURE — blocked on operator | Pick cap value, then implement |

Net: 1 INFRASTRUCTURE item, blocked on operator, single-file
implementation when unblocked. 3 items require no code change.

## Self-second-guess

- L-3 is the only item with real teeth. If the operator wants the
  guardrail but doesn't pick a value, defaulting to $15 (1.7× the
  estimated typical cost) is conservative without being painful.
- L-2 is a 60-second edit; classifying it as TUNING is a soft call.
  The implementation session may decide it's worth bundling with a
  test cleanup pass if one is already happening.
- M-4 is correctly OBSOLETE unless the operator surfaces a planned
  re-run.
