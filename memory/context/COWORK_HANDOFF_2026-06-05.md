# Cowork Session Handoff — 2026-06-05

**Living document.** Update on every meaningful change. When opening a new Cowork chat: paste this file's contents OR have Claude `Read` it at session start.

**Companion docs (canonical, always check these too):**
- `CLAUDE.md` — project rules
- `.claude/PROTECTED_FILES.md` — canonical protected-zone list (wins on disagreement with CLAUDE.md)
- `docs/findings_tracker.md` — audit ledger

---

## Last updated
2026-06-05 (late evening) — by Cowork Claude during T-BRIDGE engagement + Oracle R2 + PHANTOM-NT8 R3 session

## Current git state
- Branch: `weekly-evolution/2026-05-24`
- HEAD: `8ce3782` (== origin tip — no unpushed commits, no working-tree changes expected)
- ⚠️ **If HEAD has advanced since this doc was last updated, re-verify all "Shipped" claims below before acting.**

## Safety stack (verify before any action)
- `LIVE_TRADING` = `False` (config/settings.py:24)
- `FREEZE_ACTIVE` = `True` (config/strategies.py:52)
- `LIVE_STRATEGY_ALLOWLIST` = `("bias_momentum",)` (config/settings.py:42)
- `pending_changes.json` mtime: 2026-06-02 (untouched today)

---

## SHIPPED today (verified against git)

| Sprint | Commits | Brief |
|---|---|---|
| PHANTOM-NT8 R2 Guard A+B | `0d7c9d4` | Live-replay loop containment |
| Cluster 2 SLOT-INTERLOCK-BYPASS (sites 7+8) | `8db98ac` + `846928d` | Sized PARTIAL_EXIT at sibling sites |
| Oracle re-run + R2 sparse-filter diagnostic | `4df7abc` + `1f48b2b` + `1a71ec3` + `77d8b4c` | R2 Finding 1 REFUTED; Finding 3 deferred |
| PHANTOM-NT8 R3 H3 fix | `e1737de` (PROTECTED, OPERATOR-APPROVED 2026-06-05) + `534db89` | `is_flat_for` honors `_reconciled_<account>` |
| T-BRIDGE protected edit (bridge sized PARTIAL_EXIT) | `607ac74` (PROTECTED, OPERATOR-APPROVED 2026-06-05) + `ad95305` | Bridge translates WS EXIT qty>0 + direction → sized PARTIAL_EXIT. ⚠️ ad95305 bundled 3 PHANTOM-NT8 R3 files due to scope-hygiene breach; all complete, no broken code. |
| PHANTOM-NT8 R3 dedup + NT8_DISCONNECTED | (co-shipped in `ad95305`) | FindingDedup state machine in tools/watcher_agent.py; phase tags OPEN/ESCALATED/RESOLVED in SMS; operator recovery doc |
| Oracle R2 Finding 3 (detrended diagnostic) | `09f42e5` + `5bc7ecb` | `_PULL_MONTHS=13`, `check_regime_stability_detrended`, env-var routing. Verdict **MARGINAL** (red-team-corrected z≈2.91 from raw 3.35); HALT stands operationally. |
| T-BRIDGE engagement closure (bot WS payloads) | `42959f6` + `8ce3782` | All 3 bot WS EXIT senders now include `direction` field. Bug hunter caught getattr trap at site 3, fixed inline. |

---

## OPEN follow-ups (queued sprints)

### Operator-owned (require Jennifer's presence)
1. **PHANTOM-NT8 R4 restart + 5-min smoke** ⏳
   - Restart `bridge_server` + `prod_bot` + `sim_bot` + `watcher_agent` in cold-start order
   - Verify FindingDedup: ≤ 3 incident files per active condition
   - Verify T-BRIDGE engagement: `[SLOT-INTERLOCK] [T-BRIDGE:<tid>] EXIT translated to sized PARTIAL_EXIT` log line appears on every normal exit
   - Verify H3 fix: `[H3] is_flat_for ... BLOCKED` lines visible when reconciled orphans present
   - Archive (don't delete) ~29,050 stale incident files at `logs/phantom_guard/` to `logs/phantom_guard_pre_R4_archive_2026-06-05/`
   - Re-enable SMS channel after dedup confirmed working
   - Runbook: see prior session output (hybrid runbook + small CC verification prompt). If lost, regenerate using prompt-master.

2. **NT8 connection check** — confirm live data flowing before any smoke window (NT8 was disconnected ~12h on 2026-06-04, reconnected; verify it's still up)

### CC chats ready to dispatch (after R4 smoke)
3. **Oracle sample-size-weighted detrended variant** — discharges `FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING` (HIGH). Prompt prebuilt in prior session; key spec: new `check_regime_stability_detrended_weighted` function, Welch-style SE OR refuse-to-compute when `latest_trade_count < 0.7 × baseline_median`, behind `ORACLE_REGIME_GATE_DETREND_WEIGHTED=1` mutex with existing flags. Tests must reproduce May 826/1372 → z≈2.91.

### Lower-priority queue
4. **Post-mortem on 2026-06-04 OIF replay loop** — full timeline, root-cause narrative, findings rows. PHANTOM-NT8 + Cluster 2 + T-BRIDGE all factor in.
5. **NT8 auto-reconnect AddOn** (`PhoenixAutoReconnect.cs`) — ~150 LOC C# AddOn, self-healing on disconnect. Prompt drafted in earlier session; check chat history if needed.
6. **READ-ONLY MCP evaluation** — CrossTrade subscription vs open-source GitHub MCP. Observability only, NOT execution.
7. **Sibling code path audit** — every path that bypasses `is_flat_for` beyond what Cluster 2 covered.
8. **Footprint follow-ups** — new_strategy_buildout + filter_integration.
9. **Freeze-lift sprint** — PREREQ: emergency fully cleared + 48h verified live.

---

## INVESTIGATIONS (root-cause queue, post-emergency)

### 🚨 TOP PRIORITY: Tracker working-tree drift + git index corruption
Recurring all session. Symptoms:
- `docs/findings_tracker.md` drifted from 73 → 23 lines in working tree (while HEAD had 73, then 78 after Oracle commit)
- `git status --short` errored with progressing index format corruption: `0x21000000` → `0x4c460000`
- Multiple "memory: Session changes: N files modified" commits today: `addcd02` (18 files), `883bccf` (17), `970a35a` (20) — pattern recurs after every CC chat
- **Prime suspect: the "memory" commits.** First investigation step: `git show 970a35a -- docs/findings_tracker.md` and `git show 883bccf -- docs/findings_tracker.md` — if either touches the tracker, that's the culprit
- Other candidates: SessionEnd hook in CC, OneDrive remnant despite 2026-04-18 migration, antivirus scanner, CRLF/LF normalization, `.githooks/post-checkout`
- Stop-gap restore commands (PowerShell on Windows):
  ```powershell
  cd "C:\Trading Project\phoenix_bot"
  git checkout HEAD -- docs/findings_tracker.md
  Remove-Item .git/index
  git reset --mixed HEAD
  (Get-Content docs/findings_tracker.md).Count   # MUST equal HEAD's tracker line count
  ```

### Other investigations
- CHAT 2's scope-hygiene breach (PHANTOM-NT8 R3 files re-staged after `git reset HEAD`) — likely same SessionEnd hook as above. Documented prevention: use `git rm --cached` for new files, `git reset HEAD` for modified, verify `git diff --cached --name-only` IMMEDIATELY before commit.
- `bots/sim_bot.py:519` diff was cut off in operator's paste at one point; resolved by `42959f6`'s LITERAL Phase 0 guard. Pattern lesson: always include LITERAL/exact verification in Phase 0 when working from a partial paste.

---

## OPEN findings (from `docs/findings_tracker.md`)

⚠️ **Re-grep the tracker before trusting this list** — it has drifted multiple times today.

Known active as of 8ce3782:
- `FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING` (HIGH, NEW) — Oracle weighted-variant sprint discharges this
- `FINDING-2026-06-05-SILENT-STALL-NT8-12H` — partially superseded by dedup + NT8_DISCONNECTED; orthogonal bot-side signal-fire HALT proposal at `out/propose_silent_stall_halt_2026-06-05.md` remains for a future sprint
- `FINDING-2026-06-04-REDTEAM-R2-2` — pre-existing opening_session sub-strategy colon-form bug; not regressed; defer
- Deferred post-emergency: PROVENANCE-BOT-AGGREGATORS (HIGH), SUB-STRAT-INTERLOCK (HIGH), STRUCTURAL-TEST-PATTERN (HIGH), CONTRARIAN-SIGNAL OOS (MEDIUM), RECONCILE-EMPTY-DICT (MEDIUM), WS-WATCHDOG-MOCK (MEDIUM), CLOSEPOSITION-SIBLINGS sibling extension

---

## KEY LEARNINGS from today (don't repeat)
1. CLAUDE.md Protected Zone table is INCOMPLETE vs canonical `.claude/PROTECTED_FILES.md`. Check BOTH.
2. Phase 0 stale-audit guards prevent sprint stacking. KEEP THEM in every prompt.
3. Structural tests can lie — behavioral tests against real OIF/serialized-payload emission paths are the only reliable defense.
4. "Diagnostic-only" sprints can MISS the live impact of their findings. Severity-rank carefully.
5. RECONCILED-tagged OIF paths bypass the entire risk-gate stack because framed as "post-fill adoption" not "fresh order." Wrong framing → zero safety stack protection.
6. `is_flat_for()` can be bypassed by entries labeled `_reconciled_<account>`. Slot interlock is fragile; every emit path needs explicit verification.
7. NT8 ATI can go silent 19+ hours without surfacing in ANY Phoenix log. Phoenix needs its own NT8-side observability (R4 + the C# AddOn).
8. NT8 reconnecting after outage processes BACKLOG as fresh live orders. PHANTOM-NT8 Guard A+B is the defense.
9. PhantomGuard dedup bug → operator silenced SMS → 12h blind window. Source-side dedup beats adding more notification channels.
10. Long Cowork sessions saturate context. Behavioral signals (operator duplicating things, missing obvious details) > any percentage indicator. Handoff at first symptom.
11. **NEW: Cowork TaskList tool itself is not reliable across context windows.** Tasks vanished mid-session. Persistent file (this doc) is the source of truth.
12. **NEW: "Optimize" pass via prompt-master skill yields measurable tightening (~15% denser prompts, stronger MUST/NEVER signals). Worth doing for every protected-file or multi-file sprint prompt.**

---

## STANDING OPERATOR INSTRUCTIONS (carry across all sessions)
- After every save-and-commit action: `git push origin <branch>` (per `memory/feedback_auto_push_after_commit.md`)
- After every fix: output the **Phase completed** + **Findings fixed** lines (per `memory/feedback_phase_findings_output.md`)
- Never raw-open `logs/trade_memory.json` — use `core.trade_memory.load_all_trades()`
- Protected files: propose diff in chat first, wait for explicit "yes / ship it / approved", then commit with `OPERATOR-APPROVED: <YYYY-MM-DD>` line. Pre-commit hook enforces.
- Concise & direct. Cheery. Second-guess yourself. Verify, don't assume.

## CC PROMPT CONVENTIONS in use this session
Every prompt MUST include:
- Phase 0 stale-audit (verify HEAD, mtimes, baseline pytest count, no uncommitted edits on target files)
- LITERAL file scope (EDIT / ADOPT / NEW / NEVER TOUCH)
- TDD red-first via stash-pop trick when working tree already contains the fix
- 4 parallel verification subagents (R1 Regression, R2 Bug Hunter, R3 Test Quality, R4 Cross-Sprint)
- Pre-commit red-team adversarial review
- Acceptance table (numeric pytest delta pinned exactly)
- Stop conditions (mandatory)
- 4-bucket reporting (Shipped / Open / Quietly wrong / Next)
- For protected files: `OPERATOR-APPROVED: <date>` line in commit message
- For agentic tools (Claude Code): explicit forbidden actions + stop conditions are non-negotiable

---

## HOW TO USE THIS FILE IN A NEW CHAT
1. At session start, paste this file's contents into the new Cowork chat OR have Claude `Read` it
2. Run quick verification: `git log --oneline -10`, `git rev-parse HEAD`, check FREEZE_ACTIVE + LIVE_TRADING
3. Compare HEAD to "Current git state" — if drifted, re-verify "Shipped" rows
4. Pick from "OPEN follow-ups" based on operator availability + cross-sprint conflicts
5. **Update this file** at the end of every meaningful change — additions to Shipped, changes to OPEN, new investigations, new learnings
6. If you spawn CC chats: log them in a temporary "In flight" section of this file with prompt SHA + dispatch time + expected scope

---

*End of handoff. Last byte should always be a newline.*
