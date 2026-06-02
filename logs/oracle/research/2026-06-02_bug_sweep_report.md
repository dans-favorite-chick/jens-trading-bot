# Phoenix Bug Sweep Report — 2026-06-02 (MASTER HANDOFF)

**For the implementation session: read this first.** It is the
index for everything investigated in the 2026-06-02 bug-sweep
session and tells you what to ship next.

---

## TL;DR

The sweep examined 7 named bugs. Of those, **2 are real
infrastructure bugs that should be fixed** in the implementation
session; the other 5 are non-bugs, operator-tuning decisions, or
already documented. There is also **1 spec ready to implement**.

| Item | Verdict | Detail document | Implementation effort |
|---|---|---|---|
| BUG #1 disabled-strategy bleed | NOT-A-BUG (procedural) | [`bug1_disabled_bleed.md`](2026-06-02_bug1_disabled_bleed.md) | None |
| BUG #2 signal-to-trade gap | BUG-FOUND — root cause now understood | [`chart_orders_root_cause.md`](2026-06-02_chart_orders_root_cause.md) | Two-fix: (a) 1-line `_trade_entry.py:802` exemption removal, (b) NT8 auto-pause spec |
| BUG #3 audit remainders | M-4 OBSOLETE, L-1 OBSOLETE, L-2 TUNING, L-3 INFRASTRUCTURE-blocked-on-operator | [`bug3_audit_remainders.md`](2026-06-02_bug3_audit_remainders.md) | L-3 only — single-file when operator picks cap value |
| BUG #4 inert target_rr | DOCUMENTED-TUNING — divergence already documented inline | [`bug4_inert_target_rr.md`](2026-06-02_bug4_inert_target_rr.md) | None (operator-only decision) |
| BUG #5 zero-signal strategies | TUNING-ONLY | (in prior `bug_sweep_report` round 1, this same path) | None |
| BUG #6 SKIP_DAY_TYPE RANGE | TUNING-ONLY (classifier behaving as designed) | (same) | None |
| BUG #7 TF_VOTES 1/4 | TUNING-ONLY (gate logic correct) | (same) | None |
| **NT8 auto-pause spec** | READY TO IMPLEMENT | [`docs/superpowers/specs/2026-06-02-nt8-sink-auto-pause-design.md`](../../../docs/superpowers/specs/2026-06-02-nt8-sink-auto-pause-design.md) | ~250 LOC + 12 tests across 7 numbered tasks |

---

## What broke today (one-paragraph version)

NT8 ATI silently rejected every OIF prod_bot wrote between 09:02 and
09:39 CT. Prod_bot detected the rejection but, because account=Sim101
hits an explicit `PHANTOM_GUARD` exemption at `bots/_trade_entry.py:802`,
fell through to the "assume filled (paper mode)" path. Every assumed-
filled entry then attempted a PROTECT cycle (also stuck) and a
CANCEL/flatten (also stuck), leaving 14 stale LIMIT entry OIFs in
NT8's `incoming/` folder. When NT8 ATI came back around 11:00, those
stale OIFs were processed as working orders — the 7+ BUY LIMITs the
operator saw on the Sim1 chart at mid-day. Sim_bot's per-strategy
sub-accounts (e.g., "SimBias Momentum") do NOT hit the Sim101
exemption, so sim_bot ran PHANTOM_GUARD and aborted cleanly — zero
naked LIMITs from sim_bot.

---

## Recommended implementation order

Implementation session should ship in this order:

### Ship A — Prerequisite fix (1 line)

Remove (or narrow) the Sim101 exemption at
[`bots/_trade_entry.py:802`](../../../bots/_trade_entry.py:802) so
PHANTOM_GUARD runs for prod_bot's Sim101 account.

- **Time:** 30 minutes including the new test case.
- **Risk:** low. The exemption was added per inline comments B39/B48
  for "Sim101-only mock tracking"; re-read those comments first to
  confirm the original intent is no longer load-bearing. If still
  load-bearing, gate behind a new
  `config/settings.PHANTOM_GUARD_SKIP_ACCOUNTS` opt-in list (default
  empty).
- **Why first:** without this, today's incident's first naked LIMIT
  still leaks through even after Ship B.
- **Why labeled "fix" not "tuning":** the original exemption's
  premise (Sim101 = fully mocked, no real OIFs) is no longer true.
  Today proved prod_bot writes real OIFs against Sim101.

### Ship B — NT8 Auto-Pause spec (~250 LOC, 7 tasks)

Implement [`docs/superpowers/specs/2026-06-02-nt8-sink-auto-pause-design.md`](../../../docs/superpowers/specs/2026-06-02-nt8-sink-auto-pause-design.md)
end-to-end. Tasks are numbered 0–7 in the spec. Task 0 = Ship A
above; the spec lists it for completeness even though it's already
in Ship A.

- **Time:** estimated 4-6 hours including tests + manual verification.
- **Risk:** low. New module + thin call-sites in non-protected files.
  No strategy code, no config tuning, no protected files.
- **Net effect** of Ship A + Ship B: today's 14 naked LIMITs would
  have been 0; the 26 wasted SIGNAL→PROTECT cycles would have been
  1 (the first one triggers both the abort and the pause).

### Ship C — L-3 Oracle hard cap (blocked on operator)

Per [`bug3_audit_remainders.md`](2026-06-02_bug3_audit_remainders.md),
once the operator picks a cap value ($10/$15/$25), implement the
hard cap in `agents/strategy_oracle.py` + add
`ORACLE_HARD_CAP_USD` constant to `config/settings.py`. Single-file
fix when unblocked.

- **Time:** 1-2 hours including telegram alert wiring.
- **Risk:** none — defensive guardrail, fails closed.

### Skip — Everything else

BUGs #1, #3 (M-4/L-1/L-2), #4, #5, #6, #7 require no code change.
See individual detail docs for the reasoning.

---

## Blocked on operator decision

- **L-3 cap value** — operator picks $10 / $15 / $25 / other.
- **Ship A B39/B48 confirmation** — operator confirms the original
  Sim101 exemption rationale is no longer load-bearing. If
  load-bearing, the fix becomes a configurable opt-out list rather
  than an unconditional removal.
- **BUG #4 target_rr direction** — operator picks one of three paths
  (strip keys, patch Oracle, or build chandelier/time_exit into the
  backtest simulator). All three are tuning/intent decisions.

Nothing blocks Ship B (the NT8 auto-pause spec). It can ship before
the L-3 cap value is set and before the BUG #4 direction is chosen.

---

## Files written this session

In `logs/oracle/research/` (gitignored — committed with `git add -f`):

1. `2026-06-02_chart_orders_root_cause.md` — PHASE 1 verdict + evidence
2. `2026-06-02_bug1_disabled_bleed.md` — PHASE 2a
3. `2026-06-02_bug4_inert_target_rr.md` — PHASE 2b
4. `2026-06-02_bug3_audit_remainders.md` — PHASE 2c
5. `2026-06-02_bug_sweep_report.md` — PHASE 4 (this file)

In `docs/superpowers/specs/`:

6. `2026-06-02-nt8-sink-auto-pause-design.md` — PHASE 3

No `.py` files, no `config/` files, no `strategies/` files were
modified. Protected files were not touched.

---

## Counts

- Bugs investigated: **7**
- Real infrastructure bugs found: **1** (BUG #2 / PHANTOM_GUARD exemption)
- Specs ready to implement: **1** (NT8 auto-pause)
- Documents written: **6**
- Operator decisions still required: **3** (L-3 cap value, B39/B48 confirm, BUG #4 direction)
- Code changes shipped: **0** (per directive — investigation + spec only)

## Self-second-guess

- I'm framing the PHANTOM_GUARD Sim101-exemption as the "real bug"
  for today's chart-orders incident. That's confident. The other
  framing — "NT8 ATI dying without warning" — is also true, and is
  what the auto-pause spec addresses. Both are needed for full
  protection; neither alone is sufficient.
- The "Recommended implementation order" puts Ship A before Ship B
  because Ship A is the prerequisite. If you only have time for one,
  Ship A alone is more valuable (prevents naked LIMITs at the
  first-entry threshold); Ship B alone is less valuable but
  operator-visible. Both together is the strong outcome.
- The estimated effort numbers are based on the LOC + test counts in
  the spec. Actual integration with the dashboard banner styling and
  Telegram dedup might add an hour. I've under-estimated dashboard
  work historically in this codebase; treat the 4-6h estimate as a
  floor.
