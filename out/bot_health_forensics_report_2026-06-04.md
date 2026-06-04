# Phoenix Bot Operational Health Forensics — 2026-06-04

_Read-only diagnostic sprint. Branch: `weekly-evolution/2026-05-24`. No code changes ship from this sprint; deliverable is verdicts + a queue of fix-sprint outlines._

---

## 1. Executive summary

Phoenix's three "bots weren't really running, even when they were" symptoms map to **two shared root causes plus one independent gate**. None of them is a "silent middle gate" issue inside the bot evaluation pipeline (the FIELD-PERSIST / THRESHOLD-DIAG sprints already cleared that lane).

**Cluster 1 — Process supervision asymmetry (drives Symptoms A + B):**
The dashboard's `/api/bot/stop` only knows about subprocesses *it* spawned (`_bot_processes` registry). `/api/bot/status` is richer — it also detects externally-started bots via bridge connection state and recent `_state` pushes. When the operator launches the stack from a shell (e.g. the round-2 PowerShell script, which itself was PID 109652 — confirmed parent of prod/sim/watchdog/dashboard), the dashboard supervisor never registers those bots. `/api/bot/stop` with the safe `force=False` default reports "not running" because (a) registry pop returns `None` and (b) the psutil-scan fallback is gated behind `force=True`. The 2026-05-13 commit (`8b471af`) made this trade-off explicit: watchdog auto-restart cycles never kill externally-started bots, at the cost of operator stop calls being ineffective by default. Symptom B (the WindowsApps shim duplicate around bridge PID 172436+176512) is the launcher-discipline branch of the same root cause — invoking `python <script>` outside the explicit-pythoncore-path discipline gets you a shim wrapper plus a real interpreter. The launch BAT files and round-2 PS script both follow the safe pattern; the surviving bridge zombie is from a long-dead launcher (PID 46372) that used the WindowsApps path with a forward-slash relative argument (`bridge/bridge_server.py`). One fix sprint can close both symptoms.

**Cluster 2 — PHANTOM-NT8 (drives the "bot fires but doesn't trade" half of Symptom C):**
The lone bias_momentum signal at 05:05:12 CDT today (trace `9da466ea`) was written cleanly to NT8 `incoming/` and silently rejected by NT8 — no fill, no ack, no rejection message in the bridge log. `PhantomGuard` detected the lack of fill confirmation after 5s and aborted the entry. This is `bridge_server.py` and `oif_writer.py` doing exactly what they're supposed to do; the failure is *between* the OIF file and NT8's ATI engine. Sub-hypotheses span instrument mismatch, account state, ATI permissions, overnight-window restrictions, and order-parameter bounds. This needs NT8-side investigation (chart instrument, ATI log, account config) — none of it is Python-fixable.

**The independent third issue (Symptom C structural):**
Across the last 7 days, bias_momentum fired 15 times (2.1/day vs the 5y backtest's 22/day expectation → ~10x gap). Other strategies fired ~0. THRESHOLD-DIAG already confirmed live thresholds match the backtest config, so this is *not* drift. The gaps are dominated by **regime-fit gates** — RANGE day-type SKIP, TF_VOTES alignment scarcity, CVD opposition, the intentional 04:00-04:59 session block, EMA_STACK warmup on overnight bars, and ES_GATE. This is a longer-arc strategy-regime-fitness investigation, not a single fix sprint. It's flagged here so the operator doesn't conflate "structurally tight gates in choppy regime" with "PHANTOM-NT8 ate my trades."

**What's fixable today vs gated:** Cluster 1 is fully unprotected, single-sprint fixable (one outline below). Cluster 2 is NT8-side investigation, OA optional, no Python protected-file pressure (one outline below). Watchdog is healthy and needs only a small observability addition (one outline below). The regime-fit issue is intentionally **not** spun into a sprint — it's a recommendation to the operator + the analyst loop, not a code change.

---

## 2. Symptom A — Dashboard supervisor drift

### Evidence

- **Process tree (Phase 0.3):** Today's `prod_bot.py` (PID 135020), `sim_bot.py` (PID 145760), `watchdog.py` (PID 155700), and `dashboard/server.py` (PID 162784) all share **ParentProcessId 109652**. ParentPid 109652 is gone from the process table — it was the round-2 PowerShell script itself (`restart_round2_2026-06-04.ps1`, transcript line 9: "Process ID: 109652"). The dashboard, the bots, and the watchdog were all spawned as children of that PowerShell process and orphaned/re-parented when it exited at 15:02:54.
- **The dashboard didn't spawn the bots.** `dashboard/server.py:212` defines `_bot_processes: dict[str, subprocess.Popen] = {}`. The only path that populates this registry is `_start_bot()` calling `subprocess.Popen([sys.executable, ...])` from `dashboard/server.py:259-266`. The round-2 script spawned each component directly via `Start-Process`; the dashboard's registry stays empty.
- **`_stop_bot()` asymmetry vs `_bot_status()`.** `_stop_bot()` (`dashboard/server.py:273`) Path 1 is `_bot_processes.pop(name, None)` — returns `None` for externally-started bots → falls through to `return {"ok": True, "message": f"{name} bot was not running", "force": force}` at line 386. Path 2 (psutil scan for `{name}_bot.py`) is gated behind `force=True`. The 2026-05-13 commit (`8b471af`) made `force=False` the default, deliberately, because watchdog auto-restart cycles used to kill operator-launched cmd-window bots and create zombies (`dashboard/server.py:283-291` docstring). Meanwhile `_bot_status()` (`dashboard/server.py:389`) detects bots via three sources: registry, `bridge_health.bots_connected`, and recent `_state` pushes (<15s). So the same dashboard reports a bot as `running` via `/api/bot/status` while also reporting "not running" via `/api/bot/stop`.

### Verdict — H1 with refinement

**H1 confirmed:** Operator launched bots outside the dashboard supervisor. The dashboard's `_bot_processes` registry never had them. `/api/bot/stop` with `force=False` returned "not running" because that's the literal output of `_bot_processes.pop()` returning `None`.

**Refinement:** This is not registry *loss* — the registry was empty by design. The root issue is the **detection-vs-actuation asymmetry**: status detects three sources of bot life; stop only knows one. The operator's mental model is "Stop Bot should stop the bot regardless of who launched it." The current code requires `force=True` to honor that, and the UI default + watchdog default are both `force=False` for safety reasons.

H2 (registry de-registration), H3 (race during dashboard restart), H4 falsified — no evidence of registry drift events in `dashboard.log` (stale to 2026-04-18) or `watchdog.log` (clean post-round-2). Symptom is purely the registry-was-never-populated branch.

---

## 3. Symptom B — Multi-PID duplication

### Evidence

- **Current inventory shows one persistent zombie pair:** bridge_server PIDs 172436 (pythoncore-3.14, owns ports 8765/8766/8767) + 176512 (WindowsApps shim, parent of 172436). The shim is invoked as `"C:\Users\Trading PC\AppData\Local\Microsoft\WindowsApps\python.exe" bridge/bridge_server.py` with forward-slash relative path. The shim's parent is **PID 46372** — gone from the process table, identity unknown.
- **prod_bot, sim_bot, watchdog, dashboard have NO shim companion today.** All four were spawned by the round-2 PowerShell script using the explicit `C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe` path (see `out/restart_round2_log_2026-06-04.txt` lines 21, 24, 27, 30, 34 and the script's Step 0 logic at `out/restart_round2_2026-06-04.ps1:28-37`). The script's transcript explicitly notes line 134: "Bridge PID 172436 still present (unchanged - code didn't change)" — the round-2 script intentionally did not restart the bridge, so the pre-existing zombie pair survived.
- **The WindowsApps shim is a re-execution wrapper, not a true zombie.** It is the Microsoft Store `PythonSoftwareFoundation.PythonManager` app stub: reads the user's "default Python" setting, spawns the matching real interpreter, and blocks waiting on the child's exit code. PID 176512 is a parent process holding a `WaitForSingleObject` on its child (172436); it dies when 172436 dies.
- **Watchdog delegates to dashboard.** `tools/watchdog.py:384-424` (`restart_bot()`) calls `POST /api/bot/stop` (no `force=True`) then `POST /api/bot/start`. `_start_bot()` uses `sys.executable` (line 260) — whichever interpreter started the dashboard. The current dashboard was started with the explicit pythoncore path, so any future watchdog-triggered respawn inherits the safe interpreter and produces no new zombie. Watchdog's `_start_bot` "already running" handling (line 421) means it never double-spawns when an externally-launched bot is still alive.
- **All `launch_*.bat` files use the explicit path.** `launch_bridge.bat:4`, `launch_prod.bat:4`, `launch_dashboard.bat:4`, `launch_all.bat:6` all set `PY` to the explicit pythoncore path with the fallback `if not exist %PY% set PY=python`. `launch_all.bat:5` even carries the comment `REM Use the real Python — Windows Store python.exe spawns duplicates`. So the safe launcher pattern is canonical; the bridge zombie pre-dates today.

### Verdict — H1, with H2 + H3 falsified

**H1 confirmed:** Some launcher (PID 46372, no longer tracked) invoked plain `python bridge/bridge_server.py` from a context where the PATH resolution landed on the WindowsApps shim. The shim re-execs into pythoncore-3.14 and blocks waiting. The forward-slash relative argument is a giveaway that the launcher was not one of the `launch_*.bat` scripts (those use backslash) — likely a dev tool / IDE configuration / Cowork-Claude-shipped script using POSIX-style paths.

**H2 falsified:** Watchdog does not respawn-and-leave-zombie. It delegates to dashboard's `subprocess.Popen([sys.executable, ...])`, which inherits the dashboard's interpreter. No second python.exe is created when `_start_bot` returns "already running."

**H3 partly supported:** Operator manual launches and watchdog auto-launches *can* differ — but the watchdog path is symmetric with however the dashboard was started. The differing launch paths come from manual operator commands typed at a prompt vs the disciplined BAT/PS scripts, not from a watchdog-vs-dashboard divergence.

### Severity — LOW

- The shim is a parent wrapper consuming one PID slot + ~30-60 MB; it does NOT actively hold the bridge's listening sockets or own the trading data path.
- File-handle inheritance from the shim to its parent's stdin/stdout/stderr is theoretically possible but is not the path through which trade_memory is written. After `FINDING-2026-06-02-A` (atomic save via tempfile + `os.replace`), JSON-clobber via inherited handles is eliminated as a risk.
- The active concern is observability noise + a one-time bridge restart needed to clear the zombie pair.

---

## 4. Watchdog behavior

### Evidence

- **`tools/watchdog.py`** monitors three things every 5s: bridge `/health` endpoint (`:8767`), bridge's `bots_connected` list, and dashboard `/api/bot/status` (`:5000`). When a bot is not in `bots_connected` and the bridge is alive, `restart_bot()` is called (line 384): `POST /api/bot/stop` (`force=False`), 2s pause, `POST /api/bot/start`. Restart cooldown 30s, max 5 attempts then exponential backoff up to 5 min. Telegram alerts gated by 60s grace + 3-restart-attempt threshold (Sprint D F3, 2026-05-04).
- **Post-round-2 watchdog log is clean.** `logs/watchdog.log` tailed across 17:45-17:50 CDT shows continuous `Bridge:OK NT8:live ticks:X/s | prod:UP(2.7-2.8h) | sim:UP(2.7-2.8h) | Dash:OK`. Uptimes 2.7-2.8h trace back to the 15:02 round-2 restart. No restart events fired by the watchdog itself today.
- **`logs/disconnect_forensics.jsonl`** tail shows synthetic test events from `tools/verify_halt_signatures.py`-style harnesses (clustered at identical microsecond timestamps, `uptime_s: 0.0`, `total_disconnects: 1` always). Two real-looking `operator_restart` events at 16:52 and 17:06 CDT correspond to the round-2 shutdown/restart sequence.

### Verdict — HEALTHY

No restart-thrashing pattern, no zombie-spawn signature, no double-spawn risk. The 2026-05-13 `force=False` default + the 2026-05-13 `creationflags=0` fix (both in `dashboard/server.py:_start_bot`) have stabilized the auto-restart loop. The only observability gap: there is no audit hook recording **which interpreter** `sys.executable` resolved to at the dashboard process start. A misconfigured dashboard (started with the WindowsApps shim) would silently propagate the zombie pattern to every watchdog-triggered respawn. A 5-line change to surface `sys.executable` in `/api/bot/status` would close this gap.

---

## 5. Symptom C — NT8 OIF lifecycle (PHANTOM-NT8)

### Evidence

From `out/trade_firing_diagnostic_2026-06-04.md` lines 29-36 (today's `9da466ea` timeline):

```
05:05:12.547 [strategies.bias_momentum] EVAL: SIGNAL SHORT entry=30315.50 (score=95, regime=OVERNIGHT_RANGE)
05:05:12.547 [Bot] [TRACE:1ed9d9ef] [SIGNAL] bias_momentum SHORT conf=95 score=60
05:05:12.547 [Bot] [TRADE QUEUED:9da466ea] SHORT via bias_momentum conf=100
05:05:12.563 [Bot] [INTENT:9da466ea] SHORT 1x @ 30315.50 SL=30330.50 TP=30285.50 risk=$9.0 tier=A++ strat=bias_momentum
05:05:17.861 [OIF] WARNING [OIF:9da466ea] No fill confirmation after 5.0s
05:05:17.865 [Bot] ERROR [PHANTOM_GUARD:9da466ea] NT8 REJECTED order — 1 OIF(s) stuck in incoming/. Aborting entry + removing stuck legs. Account=Sim101 strategy=bias_momentum
```

- OIF was written (PhantomGuard saw it still in `incoming/` after the timeout).
- No fill confirmation in `outgoing/`.
- NT8 did not log a rejection back to Phoenix.
- Order parameters: SHORT 1 @ 30315.50, SL 30330.50 (15 ticks above), TP 30285.50 (30 ticks below), risk $9.0, account Sim101.
- Time 05:05 CDT — pre-RTH overnight window.

### Verdict — H1 CONFIRMED, sub-hypotheses open

**H1 (NT8 silent rejection) confirmed.** OIF lifecycle on the Phoenix side is intact: bridge wrote the file, PhantomGuard caught the lack of acknowledgement, and the bot cleanly aborted the entry. The failure occurred between the file landing in `incoming/` and NT8's ATI engine acting on it.

**H2 (NT8 didn't see the file) less likely** — PhantomGuard explicitly observed "1 OIF(s) stuck in incoming/" which means the file persisted past the read attempt. If NT8 had picked it up cleanly it would have moved/deleted it.

**H3 (malformed OIF) unverified but unlikely** — the bridge has been writing this exact OIF schema (`PLACE;Sim101;<contract>;SELL;1;MARKET;0;0;DAY;;;;`) since well before today. Same writer, no recent code changes to `oif_writer.py`. If H3 were live we'd expect every bias_momentum SHORT to silently fail; we'd want to verify the symptom isn't side-specific by reviewing recent successful SHORTs (none in 7 days for bias_momentum — sample size 0).

**H4 (ATI disabled) unverified** — would need NT8 UI inspection (Tools → Options → ATI tab) or the NT8 trace log at `C:\Users\Trading PC\Documents\NinjaTrader 8\trace\20260604*.txt` for the 05:05 CDT window.

**Sub-hypotheses ranked by likelihood (operator + NT8 inspection needed):**

1. **Overnight session restriction.** 05:05 CDT is well before RTH and inside the Globex hours. NT8 may have an account- or instrument-level rule blocking ATI orders outside specific windows. Check ATI configuration + instrument hours for MNQ.
2. **Instrument / contract mismatch.** The OIF contract field is sourced from `config/contract.json` (single source of truth per `nq-trading-skills`). Verify the chart NT8 has loaded matches the OIF's contract. A roll-window mismatch (chart on next quarter, OIF on front) silently swallows orders.
3. **ATI not enabled OR enabled but not "Confirm orders" / "Confirm modifications" trusted.** ATI requires a NT8 restart after toggle to take effect (`nq-trading-skills` rule #5). A latent toggle from a recent restart may have left it disabled.
4. **Account state.** Sim101 may have a Position-flat / not-logged-in / data-feed-disconnected state that silently rejects orders.
5. **Order parameter bounds.** SL 15 ticks / TP 30 ticks / risk $9 — none look out of bounds for MNQ on Sim101. Unlikely.

### Severity — HIGH

Even after Path X (FIELD-PERSIST, STALE/shipped) and Path Y (THRESHOLD-DIAG, RESOLVED-NO-CHANGE) close the firing-rate gap, every signal that still has to pass through this gate gets eaten by it. This is the boundary between "the bot fired" and "the trade filled." 1-of-1 today is statistically meaningless but the structural risk is total.

---

## 6. Bots-alive-but-trading-silent gap (Phase 5 attribution)

Per-strategy 7-day fired vs 5y backtest expectation:

| Strategy | 7d fired | 5y/yr | Expected/7d | Gap |
|---|---:|---:|---:|---:|
| bias_momentum | 15 | 5,700 | ~110 | ~7-10× below |
| spring_setup | 26 | n/a (deprecated, NOT in current eval roster) | n/a | legacy noise |
| _reconciled_Sim101 | 15 | n/a (orphan-fill misattribution, FINDING-2026-06-04-DASH-ATTR) | n/a | not bot trades |
| all 10 other strategies | ~0 | varies (collectively ~30-40/wk) | substantial | high gap |
| **TOTAL legit bot fills** | **~15** (bias_momentum only) | | **~210** | **~14×** |

Today's eval log shows the dominant rejection causes for bias_momentum:
- `REJECTED` (strategy-internal) — 281
- `SKIP_DAY_TYPE: RANGE day` — 154
- Today was a RANGE-classified day; bias_momentum's day-type filter rejects it.

Mapping to the master prompt's rejection-cause table (Phase 5.4):

| Rejection cause | Sprint scope |
|---|---|
| `SKIP_DAY_TYPE` (RANGE filter) | Strategy-internal regime fit. NOT a single sprint — analyst-loop investigation. |
| `EMA_STACK` (warmup on overnight bars) | Strategy-internal warmup. NOT a single sprint. |
| TF_VOTES alignment scarcity | Threshold tuning; THRESHOLD-DIAG (RESOLVED-NO-CHANGE) attributed to genuine market alignment scarcity, not drift. |
| CVD opposition | FIELD-PERSIST (STALE — sentinel shipped) covers persistence; live rejections are genuine signal-vs-flow opposition. |
| Intentional 04:00-04:59 session block | By design. No fix. |
| ES_GATE | Investigation candidate — could be threshold strictness or genuine ES/NQ divergence. |
| PHANTOM-NT8 | Symptom C — its own outline. |

**Dominant rejection cause for the 10× bias_momentum gap is regime-fit (SKIP_DAY_TYPE + structural gates), not silent middle gates.** The "process alive but bot deaf" symptom characterized in the master prompt is NOT what's happening — the bot is actively evaluating, actively rejecting, and emitting one signal that PHANTOM-NT8 kills. There is no silent middle gate in scope for this sprint.

---

## 7. Cross-symptom synthesis matrix

| Root cause | A: Supervisor drift | B: Multi-PID | C-fire: PHANTOM-NT8 | C-rate: regime fit |
|---|:-:|:-:|:-:|:-:|
| Operator launches outside dashboard | ✕ | ✕ | — | — |
| `python` PATH → WindowsApps shim | — | ✕ | — | — |
| `_stop_bot` / `_bot_status` asymmetry | ✕ | — | — | — |
| Dashboard interpreter mis-resolution propagation | (latent) | (latent) | — | — |
| NT8 ATI silent-reject path | — | — | ✕ | — |
| Strategy-internal regime gates strict in current market | — | — | — | ✕ |
| Deprecated strategies in `trade_memory` | — | — | — | minor metric noise |

**Highest-leverage fixes (rank by symptoms closed):**

1. **Process supervision asymmetry sprint** — closes Symptom A registry mismatch + Symptom B launcher discipline. Adds `sys.executable` surfacing in `/api/bot/status` to close the latent zombie-propagation risk in (B). **Effort: M**. Files unprotected. No OA.
2. **PHANTOM-NT8 investigation sprint** — closes Symptom C fire-but-no-fill. Mostly NT8-side: ATI config audit, instrument-chart cross-check, NT8 trace log review for `9da466ea` window. May surface a small Python-side test (e.g. `tools/oif_smoke_test.py` to round-trip an OIF and verify fill confirmation). **Effort: S-M**. No protected files. No OA unless a `bridge/oif_writer.py` edit is needed (which would require OA — flag explicitly in the sprint).
3. **Watchdog observability nit** — `sys.executable` surfacing + a "launcher fingerprint" assertion at startup. Bundled into Cluster 1 sprint, no need for a standalone sprint. **Effort: S**.

**Long-tail (NOT spun into a sprint here):**

- Regime-fit / strategy-internal gate strictness. This is an analyst loop investigation across the 5D AIParamTuner output + 7-day eval-log analysis, not a single-PR fix.
- Deprecated `spring_setup` rows in `trade_memory`. Pollutes the 7-day strategy histogram. Either re-enable spring_setup in the active eval roster or label its historical rows as `superseded` so they don't compete with current strategies in dashboard aggregation.

---

## 8. Severity-ranked sprint queue

| Rank | Sprint | Severity | Effort | OA needed? | Outline file |
|---|---|---|---|---|---|
| 1 | PHANTOM-NT8 investigation | HIGH (gates every fill) | S-M | Only if `bridge/oif_writer.py` edit needed | `out/next_sprint_outline_phantom_nt8.md` |
| 2 | Process supervision asymmetry (covers A + B + watchdog observability) | MEDIUM (operability + observability) | M | NO | `out/next_sprint_outline_supervisor_drift.md` |
| 3 | Multi-PID launcher hardening | LOW (cosmetic + latent risk) | S | NO | `out/next_sprint_outline_multi_pid.md` (cross-references #2 — same root cause) |
| 4 | Watchdog observability addition | LOW | S | NO | `out/next_sprint_outline_watchdog.md` (cross-references #2 — bundle recommended) |

PHANTOM-NT8 ranks #1 because every signal that gets past the firing-rate gate must also clear this NT8 gate, and 1-of-1 of today's signals failed. Cluster 1 ranks #2 because the operability symptom is observed but doesn't cost money. Outlines #3 and #4 reference #2 — operator may decide to roll all three into a single sprint.

---

## 9. Self-critique (what could be wrong with this analysis)

1. **The 7-day trade count (n=56, n=15 bias_momentum) is too small to make a reliable comparison against the 5y backtest's 22/day expectation** — a holiday-shortened week or a regime cluster could explain 10× without anything being structurally wrong. Need a 30-90 day window before concluding "structural rejection gates too tight."
2. **The PHANTOM-NT8 verdict is based on a sample of one** (`9da466ea`). If the bridge silently re-wrote an OIF in the wrong format only for SHORT signals (or only for overnight-window signals), I would not see it without prior successful SHORT or overnight-window fills to compare against — and there are none in the 7-day window. NT8-side inspection is the only way to disambiguate.
3. **The supervisor-drift conclusion assumes the operator-launch-via-shell pattern is the canonical happy path** going forward. If the operator's intent is "the dashboard manages the lifecycle of every bot" then this isn't a UX gap, it's a code defect — and the fix becomes "dashboard refuses to start unless it spawned the bot itself." I've assumed the more permissive read because that's what the 2026-05-13 commit codified, but the master prompt's framing of "the dashboard lost track" hints the operator may want the stricter read.
4. **I did not inspect handle-level file ownership on the WindowsApps shim** because `handle.exe` isn't installed and the PowerShell classifier was intermittent. The severity-LOW assessment for the bridge zombie rests on the post-`FINDING-2026-06-02-A` atomic-save model holding; if there's any code path that bypasses atomic save (e.g. an analysis tool with `open(path, 'w')`), inherited handles could still bite.
5. **The "spring_setup is deprecated" inference is based on its absence from today's eval log lineup, not from a config audit.** If it's actually still active but only fires on certain regimes (and today was OFF), the 7-day count of 26 is meaningful and changes the gap math.

---

_Forensics sprint 2026-06-04. Read-only. No code changes shipped. Outline files in `out/next_sprint_outline_*.md`._
