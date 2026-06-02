# VALIDATED_STRATEGIES Refactor — Design (Monday 2026-06-08 Review)

_Design-only artifact. No code changes tonight. Follow-up to
`2026-06-08-freeze-lift-plan.md`; this is the evolution of the freeze
gate that lets us lift validation per-strategy instead of globally._

## 1. Problem

`config/strategies.py:11-52` defines a single project-wide invariant:

```python
FREEZE_ACTIVE: bool = True
```

It gates production decisions (kill list, `validated=True` flips,
parameter retunes, sizing prep, `enabled=True` promotions, Phase 13
verdicts) for the entire strategy set as one atomic unit.

The reconciliation harness in `tools/reconcile_sim_vs_backtest.py`
operates one strategy at a time (the smoke run on 2026-06-02 produced
a per-strategy report for `bias_momentum`). If `bias_momentum`'s
reconciliation passes Monday and we flip `FREEZE_ACTIVE` to `False`,
we are implicitly claiming validation parity for the other 14+
strategies in `STRATEGIES` whose recon has not been run.

That is a category error. The freeze flag's scope is the whole project;
the recon evidence is per-strategy. Lifting one with the other
silently re-opens production decisions for everything.

## 2. Proposal

Replace the boolean with a per-strategy validation registry.

```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ValidationRecord:
    validated_at: str                  # ISO date — "2026-06-08"
    reconciliation_report: str         # repo-relative path to the recon .md
    operator_signoff_commit: str       # SHA of the commit carrying OPERATOR-APPROVED
    tolerance_used: dict[str, float]   # exact tolerance config that PASSED
    divergence_observed: dict[str, float]  # the measured numbers at that pass
    config_hash: Optional[str] = None  # sha256 of the strategy's config-as-of validation
                                        # (see open question §8)

VALIDATED_STRATEGIES: dict[str, ValidationRecord] = {}
```

A strategy is "validated" iff its name is a key in
`VALIDATED_STRATEGIES`. The freeze-lift event becomes:

```python
VALIDATED_STRATEGIES = {
    "bias_momentum": ValidationRecord(
        validated_at="2026-06-08",
        reconciliation_report="out/reconciliation_2026-06-08_bias_momentum.md",
        operator_signoff_commit="<sha of this commit>",
        tolerance_used={
            "entry_time_seconds": 60,
            "entry_price_ticks":   2,
            "stop_price_ticks":    2,
            "net_pnl_pct":         25.0,
        },
        divergence_observed={
            "entry_time_seconds_mean": <measured>,
            "entry_price_ticks_mean":  <measured>,
            "stop_price_ticks_mean":   <measured>,
            "net_pnl_pct_mean":        <measured>,
            "blocked_count":           0,
        },
        config_hash="<sha256 of bias_momentum config dict>",
    ),
}
```

`FREEZE_ACTIVE` is removed. Tooling that read it now reads
`is_validated(name)` (see §4).

## 3. Migration plan

Phase 0 (this design, no code).

Phase 1 — code-equivalence step (one commit):
1. Add the `ValidationRecord` dataclass and `VALIDATED_STRATEGIES = {}`
   to `config/strategies.py`, replacing the `FREEZE_ACTIVE` block.
2. Empty dict is semantically equivalent to today's
   `FREEZE_ACTIVE = True` — every strategy is unvalidated.
3. Update `tools/validation_tracker.py` and any other consumers to
   read `is_validated(name)` instead of `FREEZE_ACTIVE`. Banner text
   changes from "FREEZE ACTIVE" to "STRATEGY <name> NOT VALIDATED".
4. Rename `tests/test_freeze_interlock.py` →
   `tests/test_validation_interlock.py` (see §5).

Phase 2 — bias_momentum entry (same commit OR the immediately
following one, operator's choice):
1. Add `bias_momentum`'s `ValidationRecord` to the dict, populated
   from the recon report.
2. Commit message MUST include `OPERATOR-APPROVED: 2026-06-08` on its
   own line (PROTECTED-FILE rule from `.claude/PROTECTED_FILES.md`).

Phase 3 — every other strategy stays out of the dict until its own
recon passes. The other 14 strategies remain effectively frozen.

## 4. Per-strategy gate API

In `config/strategies.py`:

```python
def is_validated(name: str) -> bool:
    """Return True iff `name` has a ValidationRecord in
    VALIDATED_STRATEGIES. Replaces the old project-wide
    FREEZE_ACTIVE check."""
    return name in VALIDATED_STRATEGIES
```

Live promotion eligibility (replaces today's "freeze must be lifted
AND strategy must have `validated=True`" check):

```python
def is_live_eligible(name: str) -> bool:
    """A strategy may appear in LIVE_STRATEGY_ALLOWLIST iff:
       1. It has a ValidationRecord (reconciliation passed), AND
       2. Its own STRATEGIES[name].get('validated', False) is True
          (operator's explicit per-strategy promotion flag, unchanged
          from today's semantics)."""
    if not is_validated(name):
        return False
    cfg = STRATEGIES.get(name, {})
    return bool(cfg.get("validated", False))
```

The existing per-strategy `validated=True` flag in each
`STRATEGIES[name]` block keeps its current meaning: "the operator
has explicitly authorized this strategy for live trading." The new
`is_validated()` adds a second, mechanical gate: "the recon harness
has produced a defensible divergence number for this strategy."

Both must hold for the strategy to be live-eligible.

## 5. Test interlock — `tests/test_validation_interlock.py`

Rename from `tests/test_freeze_interlock.py`. New assertions:

1. **Module-level existence.** `VALIDATED_STRATEGIES` is defined and
   is a `dict[str, ValidationRecord]`.
2. **Record completeness.** Every entry has all 5 required fields
   (`validated_at`, `reconciliation_report`, `operator_signoff_commit`,
   `tolerance_used`, `divergence_observed`). `config_hash` is optional
   in v1 — see §8.
3. **Report path exists.** For each entry, the path in
   `reconciliation_report` resolves under the repo root.
4. **Allowlist consistency.** Every name in `LIVE_STRATEGY_ALLOWLIST`
   either:
   - is in `VALIDATED_STRATEGIES`, OR
   - the test is being run in "freeze-still-on" mode (i.e.,
     `VALIDATED_STRATEGIES == {}` AND no live-allowlist promotion has
     been attempted post-refactor — operator's explicit transitional
     escape hatch, removed once bias_momentum is added).
5. **Operator-approved commit shape.** For each entry, the
   `operator_signoff_commit` SHA is a valid 40-char hex string. (We
   do not git-verify it at test time — that would couple unit tests
   to git state — but format-check it.)

## 6. Estimated effort

| Phase | Hours |
|---|---|
| Dataclass + `VALIDATED_STRATEGIES = {}` + `is_validated()` | 2-3 |
| Sweep consumers (`tools/validation_tracker.py`, any banner code) | 1-2 |
| Test rewrite (`test_validation_interlock.py`, 5 cases) | 4-6 |
| Operator review of the refactor diff | 2-3 |
| **Total** | **~1 day code + 1 day tests + 0.5 day review** |

Bundles cleanly with the freeze-lift workstream — both touch
`config/strategies.py` in the same commit window.

## 7. Execution gate

**Monday 2026-06-08 morning** if operator approves this design AND
the freeze-lift criteria from `2026-06-08-freeze-lift-plan.md` §2 are
met (`bias_momentum` recon PASSES, report committed, sign-off given).

`config/strategies.py` is a PROTECTED FILE per
`.claude/PROTECTED_FILES.md`. The commit message MUST include:

```
OPERATOR-APPROVED: 2026-06-08
```

on its own line.

## 8. Open questions for operator

1. **Config-hash binding (the big one).** Should `is_validated(name)`
   compare a hash of the strategy's *current* config dict against the
   `config_hash` recorded at validation time? Without this binding,
   the failure mode is:

   - Day 1: `bias_momentum` recon PASSES at config X. Record added.
   - Day 5: operator nudges a `bias_momentum` slider to Y via the
     dashboard. `validated=True` flag untouched. `is_validated()`
     still returns True because the dict entry is still there.
   - Day 6: bot trades live with config Y — a config that was never
     reconciled.

   Two design choices:
   - **(A) Record-only** (v1 default): `config_hash` is stored for
     future reference but `is_validated()` ignores it. Operator
     discipline catches drift.
   - **(B) Strict hash-match**: `is_validated(name)` recomputes the
     hash of `STRATEGIES[name]`'s current dict and compares against
     the recorded hash. Mismatch → `False`, dashboard banner shows
     "VALIDATION STALE — recon required." This is more defensive
     but introduces a re-validation burden on every legitimate tune.

   Default proposal: ship (A) Monday; add a `--enforce-hash` flag to
   the validation tracker as a follow-up (one week out) so the
   operator can experiment with (B) without it being load-bearing.

2. **Per-tier scope.** Does a `ValidationRecord` apply to
   `sim_bot` + `prod_bot` symmetrically, or do we need a per-tier
   record? Proposal: one record per strategy; the recon harness
   runs against `sim_bot` output and that's the validation that
   gates live promotion. A separate live-vs-sim reconciliation
   (the 5-day canary loop from the freeze-lift doc §9) is a
   different artifact, not a `ValidationRecord`.

3. **Validation expiry.** Should records have a `valid_until` field
   that forces re-recon after N days of live drift? Proposal: no
   for v1. The canary monitoring loop is the right place to catch
   live-vs-sim divergence; baking an expiry into the record
   conflates two different problems.

4. **Dict mutation safety.** The record is `frozen=True`, but the
   dict itself is mutable. Should we expose only an accessor and
   make the dict module-private (`_VALIDATED_STRATEGIES`)? Proposal:
   yes — `is_validated()` is the only public read path; the dict
   itself is `_VALIDATED_STRATEGIES`. Stops drive-by code from
   mutating it at runtime.

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Gate bug silently validates a strategy whose config changed | Medium | High (live trades on untested config) | Open question §8.1: ship `config_hash` field even if not enforced; add `--enforce-hash` follow-up |
| Consumer sweep misses a call site still reading `FREEZE_ACTIVE` | Medium | Medium (stale gate, wrong banner) | grep -r FREEZE_ACTIVE before commit; CI test asserts the constant is removed |
| Test renames break import chains in unrelated files | Low | Low | grep -r test_freeze_interlock before commit |
| Empty `VALIDATED_STRATEGIES = {}` ships without bias_momentum entry, breaking everything | Low | Medium (no live trades possible) | Acceptable failure mode — equivalent to today's freeze. The whole point of Phase 1 being code-equivalent is to land the refactor safely even if Phase 2's recon flunks. |
| Operator forgets `OPERATOR-APPROVED:` line on commit | Medium | Low (pre-commit hook should catch) | Verify `.claude/PROTECTED_FILES.md` hook works before Monday; if not, manual check at commit time |

## 10. Out-of-scope (for this design)

- Multi-strategy parallel recon. Each strategy's record is added
  independently as its own recon passes. No batch validation.
- Live-vs-sim reconciliation (the 5-day canary loop). That produces
  its own artifact, not a `ValidationRecord` mutation.
- Auto-tuning tolerances. The `tolerance_used` field captures
  whatever the operator agreed to at validation time; learning
  tolerances from observed variance is a separate Phase II item.
- A web UI for browsing validation records. Read the dict literal
  in `config/strategies.py`; that's the source of truth.
