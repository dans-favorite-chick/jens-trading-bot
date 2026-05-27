# Phoenix Bot — Incident Log

**This file is dated incident history. Entries are preserved verbatim, in
descending order. Do not edit past entries — append new ones at the top.**

Cross-references with `file:line` are load-bearing: a date plus a commit hash
plus a line number reconstructs an investigation. If you "modernize" an entry,
you destroy that.

For the *current* known-issues list (open, not yet resolved), see
[`memory/context/KNOWN_ISSUES.md`](../memory/context/KNOWN_ISSUES.md).

---

## 2026-05-25 — F-26 / F-12 / F-25 closure + B2-3 silent-failure audit + P1-8 verified

**Source:** session log 2026-05-25, against [`audits/SYNTHESIS_2026-05-24.md`](audits/SYNTHESIS_2026-05-24.md).

**Findings closed:**

- **F-26 — Bug B2 `open_drive` target FULL fix (R1/S1 continuation).** Original
  target = `pivot_pp` landed on the wrong side of entry on strong drives →
  instant losers. The 2026-05-18 partial fix used 2R fixed. Operator chose
  Continuation R1/S1: LONG → `R1 = 2·PP − PD_L`, SHORT → `S1 = 2·PP − PD_H`,
  with 1.5R minimum distance fallback to 2R fixed. File:
  [`strategies/opening_session.py:408-446`](../strategies/opening_session.py).
  Tests: 3 new in [`tests/test_opening_session.py`](../tests/test_opening_session.py).

- **F-12 (PARTIAL) — `dom_pullback` restored + re-enabled in sim heavy-test.**
  Deleted 2026-05-21 after 0 trades in the 5y canonical backtest (entry above).
  Operator directive: re-add and accumulate live-paper data, decide later.
  Class file restored from `git show b35f6c7^:strategies/dom_pullback.py`.
  Config block re-added with `enabled=True, validated=False`. Live safety
  intact: `LIVE_STRATEGY_ALLOWLIST` excludes it, `prod_bot.only_validated`
  gate blocks it, `walk_forward_gate=informational`.

- **F-25 — Per-strategy `walk_forward_gate` field + validation_tracker wiring.**
  Added to 8 strategies and wired into
  [`tools/validation_tracker.py`](../tools/validation_tracker.py)
  `--check-promotion`. `bias_momentum` gets `hard_block` (REFUSES promotion
  without a PASS walk-forward report); the other 7 get `informational`.
  Verified: `bias_momentum` is now BLOCKED from staying `validated=True`
  until the harness runs.

**Latent bugs killed (B2-3 silent-failure audit):**

- **CR assessment dead since 2026-05-06.** `_mq_snap` NameError in
  [`bots/_strategy_dispatch.py:356`](../bots/_strategy_dispatch.py). CR
  verdict was stuck on "UNKNOWN" every bar for 19 days; day-classifier
  downstream was degraded. Fix: pass `None`;
  `core.continuation_reversal.assess()` now documents `mq_snap` as
  deprecated.
- **Big-Move exhaustion exit never fired since 2026-05-15.** `market`
  NameError in [`bots/_ws_dispatcher.py:441`](../bots/_ws_dispatcher.py)
  swallowed at `logger.debug`. Fix: use `bot.aggregator.snapshot()`.
  Upgraded log to `warning` per B-006 silent-failure policy.
- **Chandelier trail broken (pre-decomposition).** `market` NameError in
  [`bots/_ws_dispatcher.py:551`](../bots/_ws_dispatcher.py) swallowed at
  `logger.warning` — B-006 had upgraded the level on 2026-05-20, so the
  error had been firing into the void. Fix: hoist
  `_market = bot.aggregator.snapshot()`.

All three are textbook `feedback_silent_failures.md` cases: process alive,
dashboard healthy, the actual feature deaf.

**P1-8 verification (full pipeline confirmed live):**

2026-05-25 09:22:07 — `SimIB Breakout` stop placed → working → filled →
execution captured → position updated. Full Phoenix → NT8 → ATI → fill →
position pipeline confirmed live. NT8 ATI settings audited (Settings →
Automated trading interface → "Submit as is" for both order types under
the TradeStation email interface, which is disabled so the setting is
irrelevant). **No order-type conversion is happening.**

**New issues discovered (spawned to separate tasks, not fixed here):**

- NT8 Log tab flooded with "Unknown OIF file type" errors because NT8 reads
  `.tmp` files Phoenix creates during atomic OIF writes. Pipeline still
  works (rename outpaces NT8's reject-and-delete) but ~50% Log tab noise.
  Spawned task: fix [`bridge/oif_writer.py`](../bridge/oif_writer.py) to
  stage `.tmp` outside `incoming/`.
- NT8 `outgoing/` folder has hundreds of stale UUID order-ack files.
  Spawned task: build `tools/clean_nt8_outgoing.py` janitor.

**Test results:** 2489 passed, 14 skipped, 0 failed.

---

## 2026-05-24 — `prod_bot` repeated process_down, Gemini quota exhausted

**Timestamp:** 10:34–10:36 CDT, multiple incident files per minute.
**Severity:** RED_ALERT, `process_down`.
**Source:** [`logs/incidents/incident_2026-05-24_10-36-17.txt`](../logs/incidents/) and surrounding files.

Excerpt (verbatim):
```
Severity  : RED_ALERT
Category  : process_down
Detail    : prod_bot not running (consecutive checks missed: 47). Watchdog has failed to restart after 3 tries — paging.
Context   : { "process": "prod_bot", "signature": "prod_bot.py", "consecutive_misses": 47 }

=== AI ANALYSIS (Gemini) ===
Root cause     : Gemini call failed: ResourceExhausted('You exceeded your current quota...
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests,
  limit: 20, model: gemini-2.5-flash
Please retry in 42.342244342s.')
Fix available  : no
Fix type       : manual_required
```

Two failure modes stacked: (1) `prod_bot` was down, watchdog couldn't recover;
(2) the incident AI analyzer itself was paged through Gemini, which was over
quota. Manual operator action required.

**Linked synthesis finding:** F-04 in [`audits/SYNTHESIS_2026-05-24.md`](audits/SYNTHESIS_2026-05-24.md). Audit C
(Codex) caught this; A and B did not check current process state.

---

## 2026-05-21 — `dom_pullback` deleted

**Action:** strategy file removed from `strategies/`. Entry in
[`config/strategies.py:267-275`](../config/strategies.py):

> dom_pullback DELETED 2026-05-21 ──────────────────────────
> Reason: 0 trades / 0 signals in 5-year canonical backtest
> (tools/phoenix_real_backtest.py with --strategies dom_pullback
> --start 2021-05-17 --end 2026-05-15). Strategy gates never
> produced an entry in 1.77M 1m-bar cycles. Live sim showed 6/6
> wins on incomplete data.

**Disputed.** Audit B (AntiGravity) flagged this as premature: the canonical
backtest cannot evaluate L2/DOM-dependent strategies (the data isn't there).
See `audits/SYNTHESIS_2026-05-24.md` C-1 and roadmap P2-5.

---

## 2026-05-20 — Phase 13 ship audit pt2 (B-010): $50 hard-cap restore — 3 days late

**Source:** [`config/settings.py:39-44`](../config/settings.py) comment.

> 2026-05-20 PHASE 13 SHIP AUDIT pt2 (B-010): restored $100 → $50.
> Was raised to $100 on 2026-05-17 for V2 deployment with
> max_stop_ticks=200, under the "RESTORE before live" comment. The
> Phase 13 audit on 2026-05-20 restored DAILY_LOSS_LIMIT and
> PER_STRATEGY_DAILY_LOSS_CAP but missed this one.

**Pattern:** comment-driven "restore before live" overrides survive longer than
intended because nobody greps comments. The override mechanism itself is the
bug. See roadmap P0-2.

---

## 2026-05-18 — B3 fix: `orb_fade` wallclock freshness check

**Commit:** PHASE 13 BUG B3 FIX, dated 2026-05-18.
**Source:** [`strategies/orb_fade.py:159-166`](../strategies/orb_fade.py).

Previous: `time.time() - last_bar_ts > 90` rejected every backtest signal
because `time.time()` returns the 2026 wallclock while `last_bar_ts` is the
historical bar epoch. Likely also broke live silently (operator needed to
confirm via grep).

Fix: compare against `now_ct.timestamp()` — the strategy's "now," which works
in both backtest and live.

**Note:** Audit A (Claude) listed this as still-open. The fix shipped a week
before the audit. Treat A's claim on B3 as stale.

---

## 2026-05-18 — B2 partial fix: `open_drive` removes `pivot_pp` require-check

**Source:** [`strategies/opening_session.py:361-368`](../strategies/opening_session.py).

> PHASE 13 BUG B2 FIX (2026-05-18): we no longer require pivot_pp
> because we don't use it as the target. We DO still want PP for
> metadata logging when available, plus prior_day H/L for R1/S1
> computation when we want a structural target.

**Status:** require-check relaxed; *target-design* decision still pending
operator (continuation R1/S1 vs reversion PP). Tracked in
`memory/context/OPEN_QUESTIONS.md`.

---

## 2026-05-14 — $-106 loss from in-memory stale code

**Source:** auto-memory entry `code_changes_dont_auto_deploy.md`.

A stop-clamp fix was committed; the running prod_bot kept its in-memory code
snapshot from process start. Bot continued to take trades governed by the
broken stop-clamp logic on disk-fixed code. Cost: -$106 in real terms.

**Lesson codified as rule 10 in [architecture.md](architecture.md):** code
changes do NOT auto-deploy. Always flag "prod needs restart" after
behavior-affecting commits, or ask permission to restart.

---

## 2026-05-13 (AM) — Trade memory canonical reader audit (commit `c9099d7`)

**Source:** [`memory/context/KNOWN_ISSUES.md`](../memory/context/KNOWN_ISSUES.md) §RESOLVED.

The 2026-05-12 split of trade memory into per-bot files (`trade_memory_<bot>.json`)
left 12 readers still raw-opening the legacy `trade_memory.json`. They
silently missed every post-split trade. Audit pass shipped in commit
`c9099d7`:

Production-path readers updated to use `core.trade_memory.load_all_trades()`:
- `core/position_manager.py` (bot startup hydration)
- `agents/historical_learner.py` (daily 23:30 CT)
- `agents/session_debriefer.py` (post-session — also fixed latent
  trade_memory_tail-always-empty bug)
- `tools/routines/post_session_debrief.py` (daily 16:05 CT — also fixed stale
  ISO-string filter that made "0 trades today" YELLOW verdict the default)

Analytical tools: `tools/validation_tracker.py`, `tools/indicator_audit.py`,
`tools/audit_l2_roi.py`, `tools/backfill_commissions.py`,
`tools/analyze_conflicts.py`, `tools/diagnose_stuck_exits.py`,
`tools/diagnose_dashboard.py`.

Write side: `tools/mark_position_flat.py` now searches every trade_memory file
and writes back to whichever file contained the match.

Verified by smoke test: `validation_tracker.load_all_trades()` count went
1,254 → 1,256 (the two sim wins booked that day). Test suite still 1,727 / 4 / 0.

**Lesson:** rule 7 in [architecture.md](architecture.md). Never raw-open
`logs/trade_memory.json`.

---

## 2026-05-13 (AM) — NT8 internet outage, prod silently skipped its window (commit `1e07000`)

**Source:** [`memory/context/CURRENT_STATE.md`](../memory/context/CURRENT_STATE.md) "Prod_bot is now 24/7" section.

Pre-fix: prod only evaluated strategies during 08:30–11:00 + 13:00–14:30 CST.
NT8 internet outage 08:30–11:09 meant prod missed its entire primary window.
Sim caught 4 wins; prod caught 0. Operator confusion: "why isn't prod
trading?" — bot looked healthy but was silently skipping.

Fix (commit `1e07000`): prod evaluates all strategies on every bar close, all
hours, same as sim. Strategy-level time windows still apply as intentional
per-strategy filters.

---

## 2026-05-13 — 106s WS-watchdog reconnect cycle during 0-tick markets

**Source:** [`memory/context/KNOWN_ISSUES.md`](../memory/context/KNOWN_ISSUES.md) §"Bot disconnects every ~106s during 0-tick market conditions" — 🟠 OPEN.

Sample (verbatim):
```
08:32:39 Restart command sent — PID=7960  (prod)
08:32:46 RECONNECTED after 9.1s downtime  (prod)
... 106 seconds of UP ...
08:34:32 DISCONNECTED — reason=nt8_stale_1215s, uptime_was=107s, total_disconnects=10
```

Hypothesis: WS watchdog (`_ws_watchdog_loop` at [`bots/base_bot.py:5620`](../bots/base_bot.py))
fires on "no message" but cannot distinguish "no tick" from "WS dead."
0-tick lulls trigger defensive reconnects.

Still open. See roadmap P1-6.

---

## 2026-05-12 — Windows subprocess zombie bug (commit `8b471af`)

**Source:** auto-memory entry `windows_subprocess_zombie.md`.

`creationflags=CREATE_NEW_PROCESS_GROUP` killed child bots in 2–3 minutes on
Windows. Watchdog appeared to restart correctly but the new process died.
Fix: use `creationflags=0`. Codified as rule 8 in [architecture.md](architecture.md).

---

## 2026-04-25 — NT8 dual-stream "phantom $40K trade" incident

**Source:** [`memory/context/CURRENT_STATE.md`](../memory/context/CURRENT_STATE.md) §"Today's NT8 dual-stream incident" (preserved verbatim below).

**Symptom:** bridge `:8765` had 2+ established TCP connections all weekend.
PriceSanity logged 27,000+ tick rejections over Friday→Saturday with corrupt
~7,196-class prices alongside real ~27,440 MNQ prices ("phantom $40K trade"
signature).

**Real root cause** (3 layered issues, not the simple "single TickStreamer dupe"):

1. **Two legacy NT8 source files were still installed and compiled:**
   - `Indicators\JenTradingBotV1DataFeed.cs` — V1-era WebSocket indicator with
     `IsSuspendedWhileInactive=false`, broadcasting synthetic mom/prec/conf
     fields plus a secondary data series whose price scale was the source of
     the corrupt 7,196 stream.
   - `Strategies\OLDDONTUSEMarketDataBroadcasterv2.cs` — V2-era WebSocket
     strategy, also targeting `:8765`, with its own ATI write path.
2. **NT8 auto-loaded a bloated workspace via
   `<ShowDefaultWorkspaces>true</ShowDefaultWorkspaces>`** — `Jen's Fav.xml`
   and/or `Jen's indicators.xml` brought up **9 hidden MNQM6 charts** plus
   ESM6/AUDUSD/SuperDOM windows (`IsWindowVisible=false`). Charts were alive
   in NT8 memory holding TickStreamer instances + TCP connections, but
   invisible — not in taskbar, no Window menu in this NT8 build to reveal them.
3. **The system was operating one PriceSanity edge case away from a real loss
   the entire weekend.** PriceSanity caught all corrupt ticks at the bot
   level; the OIF builders' price-sanity guard would have caught any that
   slipped past. But `PHOENIX_STREAM_VALIDATOR=0` meant the bridge-level
   defense built specifically for this scenario was off.

**Cleanup playbook (embedded in `tools/nt8_unhide_all_windows.ps1`):**

1. Move both legacy `.cs` files to `.disabled_2026_04_25` quarantine.
2. Run `tools/nt8_unhide_all_windows.ps1` from elevated PS — uses Win32
   `EnumWindows` + `ShowWindow(SW_SHOWNORMAL)` to surface every hidden NT8-owned window.
3. Manually close every chart not needed; keep one MNQM6 with TickStreamer.
4. Workspaces → Save As → `phoenix_clean_2026_04_25` (clean baseline).
5. Tools → Options → General → uncheck "Show default workspaces on startup."
6. NinjaScript Editor → F5 to recompile (purges legacy classes from cached DLL).
7. Full NT8 restart. With NT8 running but no chart open,
   `(Get-NetTCPConnection -LocalPort 8765 -State Established).Count` must
   return 0.
8. Set `PHOENIX_STREAM_VALIDATOR=1` in `.env` permanently.

**Defense layers added today (prevent recurrence):**
- `bridge/bridge_server.py::handle_nt8_tcp` rejects 2nd+ NT8 connection at
  socket-accept layer (`PHOENIX_BRIDGE_SINGLE_STREAM=1`)
- `tools/nt8_unhide_all_windows.ps1` — Win32 EnumWindows + ShowWindow for
  hidden windows
- `tools/diagnose_nt8_client.py` — spy bot that classifies the connected NT8
  client by message-shape fingerprint
- `tools/_patch_register_scripts.py` — idempotent helper that retroactively
  fixed 8 register scripts to use `$TaskUser` (`TradingPC\Trading PC`)

**Diagnostic insight that broke the case:** `nt8_last_heartbeat_age_s ≈ 2.8`
on bridge health endpoint matched TickStreamer's `HEARTBEAT_MS=3000` timer
exactly. Legacy V2 strategy uses `HEARTBEAT_BARS=30` (bars, not milliseconds —
silent on a closed Saturday market). That fingerprint proved the connecting
client was TickStreamer, but a Win32 window enumeration revealed it was
attached to one of nine hidden charts.

---

## 2026-04-22 — pytest leaked test-literal stop prices to live OIF folder (P0.2)

**Source:** [`bridge/oif_writer.py:31-42`](../bridge/oif_writer.py) comment block and [`tests/conftest.py`](../tests/conftest.py).

On 2026-04-22 pytest tests leaked test-literal stop prices (100.00 then
21000.00) into real OIFs that NT8 placed on Jennifer's live chart. The B81
conftest fixture (`tests/conftest.py`, autouse) now stops pytest leaks
globally — every test gets a tempdir for OIF_INCOMING and the consume-check /
sanity-check are bypassed by default. But any rogue process could still
inject; the NT8-side PhoenixOIFGuard AddOn quarantines any file in `incoming/`
whose name does NOT start with `phoenix_<pid>_` — so the filename tagging at
`bridge/oif_writer.py:41` is the other half of that defense.

---

## 2026-04-21 — `ANTHROPIC_API_KEY` empty bug

**Source:** [`memory/context/KNOWN_ISSUES.md`](../memory/context/KNOWN_ISSUES.md) §RESOLVED.

Resolved via commit `eac5ae4` (`load_dotenv override=True`). Key was never
missing — 108-char value on line 19 of `.env`. Root cause: host OS had
`ANTHROPIC_API_KEY=""` set by Claude Code's OAuth shim; `load_dotenv()` default
behavior skips any key already present in `os.environ` even if empty. Fix:
`override=True` across all `load_dotenv` call sites.

---

## 2026-04-18 — NT8 data folder migrated out of OneDrive

**Action:** NT8 data folder was migrated from OneDrive to local `Documents\`.
**Why:** OneDrive sync intermittently locked OIF files mid-write or replayed
stale ones on restore.
**Where:** `config/settings.py` constant `NT8_DATA_ROOT` is the single
hardcoded path; downstream constants derive from it.

---

## 2026-04-16 — NT8 SILENT_STALL, missed entire primary trading window

**Source:** [`memory/context/KNOWN_ISSUES.md`](../memory/context/KNOWN_ISSUES.md) §"NT8 SILENT_STALL pattern" — 🔴 OPEN.

NT8 reported "connected" but forwarded 0 ticks/second for an extended window.
Bot's watchdog detected `NT8:live ticks:0/s` but only logged — did not restart
NT8 or page the operator.

**Originally observed:** 2026-04-16 from 07:56 to 11:11 CDT — the entire
primary trading window.

**Status (2026-04-25):** WatcherAgent now escalates this pattern (60s →
Telegram, 5min → Twilio SMS). The "trader doesn't know" failure mode is
mitigated. The underlying NT8 freeze is still not auto-recovered; clean NT8
restart is the only fix. See roadmap P1-4.

---

## Historical playbook — CLOSEPOSITION suffix bug (Phase 9.5)

Documented in CLAUDE.md as part of the "documented OIF incident history": OIF
CLOSEPOSITION instructions were dropping a required field, racing the OCO
cancel. Fixed in Phase 9.5; the lesson — "audit every existing `except ...:
pass` for whether a formerly-impossible state is now possible" — is codified
in auto-memory entry `index_error_pass_silent_pattern.md`.

---

## Historical playbook — OIF filename-format failures

Documented in CLAUDE.md and `bridge/oif_writer.py`. OIF filename must match
`phoenix_<pid>_<counter>.txt`. PhoenixOIFGuard quarantines anything else.
Pre-tagging, any rogue process (test, manual script, corrupt tool) could
inject. The 2026-04-22 pytest leak (see entry above) is the canonical
incident.
