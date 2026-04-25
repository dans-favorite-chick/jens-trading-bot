---
description: Phoenix weekly strategy-evolution proposal — aggregates the week's grades, identifies consistent failures, runs adaptive_params, AI-reviews each proposal, creates a draft git branch with validation-checkbox commit body. Never auto-pushes. Never auto-merges. Run on Sundays.
---

# Phoenix weekly evolution

Run the Sunday-evening evolution routine. Aggregates the past week's grades
from `out/grades/`, drafts adaptive-params proposals, runs Claude review on
each, creates a `weekly-evolution/YYYY-MM-DD` git branch with a commit body
that explicitly includes CPCV / DSR / PBO validation checkboxes (all reading
"NOT YET RUN — Phase C dependency" until the meta-labeler ships).

Steps:

1. Run the routine — `--no-commit` for a dry-run that still drafts proposals
   but does NOT touch git:
   ```
   python tools/routines/weekly_evolution.py --no-commit
   ```
   For the real Sunday run (auto-creates branch, never pushes):
   ```
   python tools/routines/weekly_evolution.py
   ```

2. Surface to the user:
   - Number of sessions found in the week (must be ≥ 2 to bother)
   - Pass / fail counts per prediction id (P1-P6)
   - Consistent failures (the predictions worth investigating)
   - Number of proposals drafted
   - The Claude AI review (one short paragraph per proposal)
   - Branch name and commit SHA (if git op succeeded)
   - The path to the proposal markdown

3. **Critical rule:** never push the branch. Never merge. Surface to the user
   that the branch lives only locally — they must review on their own time
   before deciding whether to merge.

4. If the AI review flags a proposal as REJECT, explicitly call that out — it
   means Claude found a safety / regime / coverage problem that the
   adaptive_params heuristic missed.

5. The commit body MUST include the validation-status checkboxes section.
   Verify it does by reading the artifact at
   `out/weekly_evolution/proposals_YYYY-MM-DD.md` after the run.

6. After the run completes, the routine also fires a Telegram digest. Don't
   send a duplicate.

This routine fires automatically on Sunday 18:00 CT via the scheduled task
(see `scripts/register_weekly_evolution_task.ps1`). Manual invocation is
common when iterating on the proposal generator itself.
