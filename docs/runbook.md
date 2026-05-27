# Phoenix Bot — Runbook

How to start, stop, kill, recover. Operator-facing.

For the immutable technical rules that bind every change, see
[architecture.md §3](architecture.md). For incident history, see
[incidents.md](incidents.md).

---

## 1. Daily start (Mon–Fri)

Most of this is automated via scheduled tasks (`PhoenixBoot` fires `AtLogOn`,
`PhoenixWatcher` runs continuously). If something didn't come up:

```powershell
# 1. Start NinjaTrader 8, load TickStreamer on MNQM6 chart.
# 2. Start the bridge.
cd "C:\Trading Project\phoenix_bot"
python bridge/bridge_server.py

# 3. Start prod_bot and sim_bot.
python bots/prod_bot.py
python bots/sim_bot.py

# 4. Start the dashboard.
python dashboard/server.py    # then visit http://localhost:5000

# 5. (Optional, but on by default via scheduled task) Start the watcher.
python tools/watcher_agent.py
```

The startup verification block — run from any PowerShell:

```powershell
# Bot processes alive?
Get-CimInstance Win32_Process -Filter "Name='python.exe'" `
  | Where-Object CommandLine -match '_bot\.py|bridge_server|dashboard|watchdog|watcher_agent' `
  | Select-Object Id, @{N='Cmd';E={if($_.CommandLine -match '([\w_]+\.py)'){$matches[1]}}}

# NT8 streaming ticks?
(Invoke-WebRequest 'http://127.0.0.1:8767/health' -UseBasicParsing).Content `
  | ConvertFrom-Json | Select-Object nt8_status, tick_rate_10s, nt8_last_tick_age_s

# Dashboard healthy?
(Invoke-WebRequest 'http://127.0.0.1:5000/api/today-pnl' -UseBasicParsing).Content `
  | ConvertFrom-Json
```

Expected first hour: `[strategies.*]` log lines from each bar close, Daily Stats
panel populated, Telegram debrief at 16:05.

---

## 2. Stop / shutdown

Two ways. Use the graceful path when you can.

```powershell
# Graceful shutdown via dashboard command queue (commit dda680c):
Invoke-WebRequest -Method Post -Uri 'http://127.0.0.1:5000/shutdown' -UseBasicParsing
```

If the dashboard is dead, kill processes directly:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" `
  | Where-Object CommandLine -match '_bot\.py|bridge_server|dashboard' `
  | ForEach-Object { Stop-Process -Id $_.Id -Force }
```

⚠ Don't use `Ctrl+C` on the bot's terminal: it can trigger the
`CREATE_NEW_PROCESS_GROUP` zombie bug (see [incidents.md](incidents.md)
2026-05-12). Use the methods above or `taskkill /F /PID <pid>`.

---

## 3. Kill switch

Hard halt of new entries without touching open positions:

```powershell
cd "C:\Trading Project\phoenix_bot"
python tools/oif_kill_switch.py
```

This writes `outgoing/halt_all.json`. `prod_bot` and `sim_bot` watch for it on
every cycle and refuse new entries until it's cleared.

**Verify it actually halted entries** (don't trust the file's existence — see
the silent-failures rule, [architecture.md §3 rule 9](architecture.md)):

```powershell
# Tail the bot log for the halt signature:
Get-Content logs/prod_bot.log -Tail 50 | Select-String 'KILL_SWITCH|halt_all'
```

Expected: a `[KILL_SWITCH] halt_all.json detected — refusing entries` line
within one bar's worth of cycles.

To clear: delete `outgoing/halt_all.json`. Bot returns to normal on next
cycle.

---

## 4. Strategy-level controls

```powershell
# Re-enable a single strategy after a floor halt:
python tools/reenable_strategy.py --strategy <name>

# Verify account routing (every strategy → correct sub-account):
python tools/verify_routing.py

# Diagnose a strategy's recent P&L pattern:
python tools/diagnose_vwap_pullback.py --strategy <name>
```

---

## 5. Recovery: NT8 silent stall

`nt8_status: live` AND `tick_rate_10s = 0` for > 1 min = NT8 has lost its
data feed without disconnecting. **There is currently no auto-recovery** (see
[incidents.md](incidents.md) 2026-04-16; roadmap P1-4 will fix).

Manual recovery:

```powershell
# 1. Confirm the stall:
(Invoke-WebRequest 'http://127.0.0.1:8767/health' -UseBasicParsing).Content `
  | ConvertFrom-Json | Select-Object nt8_status, tick_rate_10s
# nt8_status:live tick_rate_10s:0 = stall

# 2. Restart NT8 (manual, from the GUI). When it comes back, the indicator
#    auto-reconnects and the WS watchdog detects the new stream.

# 3. After NT8 is back, force a position reconciliation:
python tools/reconcile_positions.py    # (or restart prod_bot — it runs the reconciliation pass at startup)
```

---

## 6. Recovery: bridge / bot / scheduled task

```powershell
# Restart PhoenixWatcher (the alerting daemon).
# IMPORTANT: PhoenixWatcher uses Repetition: PT5M; if it died via Ctrl+C
# it will respawn within 5 min. To force-restart immediately:
Start-ScheduledTask -TaskName PhoenixWatcher

# Re-register scheduled tasks after a reboot or task corruption:
.\scripts\register_phoenix_boot_task.ps1   # (must run as Admin)
.\scripts\register_watcher_task.ps1
.\scripts\register_phoenix_grading_task.ps1
.\scripts\register_morning_ritual_task.ps1
.\scripts\register_post_session_debrief_task.ps1
.\scripts\register_weekly_evolution_task.ps1
```

The scheduled-task lattice is documented in
[`memory/context/CURRENT_STATE.md`](../memory/context/CURRENT_STATE.md) §"Scheduled task lattice."

---

## 7. Recovery: position state drift between NT8 and Python

If you suspect Python thinks it's flat but NT8 has an open position (or
vice-versa):

```powershell
# 1. Inspect what Python thinks:
python tools/diagnose_dashboard.py

# 2. Inspect what NT8 thinks (read outgoing folder for last fill):
Get-ChildItem "C:\Users\Trading PC\Documents\NinjaTrader 8\outgoing\" `
  | Sort-Object LastWriteTime -Descending | Select-Object -First 10

# 3. If they disagree, the recovery is to MANUALLY flatten via NT8 ChartTrader,
#    then mark Python flat:
python tools/mark_position_flat.py --trade-id <id>
```

⚠ `mark_position_flat.py` searches every trade_memory file and writes to
whichever one contains the match (post-2026-05-13 audit). Do NOT raw-edit
`logs/trade_memory.json` — that file is frozen; the canonical reader is
`core.trade_memory.load_all_trades()`.

---

## 8. Roll handling (MNQ quarterly)

⚠ **There is no automatic roll handling at the moment** (synthesis F-14;
roadmap P2-3).

| Code | Month | 2026 3rd Friday |
|------|-------|------------------|
| H | March | — |
| M | June | 2026-06-19 ← current contract `MNQM6` |
| U | September | 2026-09-18 ← next contract `MNQU6` |
| Z | December | — |

`ROLL_DAYS_BEFORE_EXPIRATION = 8` in `config/settings.py` — the intended
switch-over window. Until P2-3 ships, manual checklist:

1. Around 8 trading days before expiration:
   - Flatten any open multi-day position (`vwap_pullback_v2`,
     `e_multi_day_breakout`).
   - Stop the bot at EOD.
   - Edit `config/settings.py:17-21`:
     - `INSTRUMENT = "MNQU6"` (or next month code)
     - `CONTRACT_EXPIRATION` and `NEXT_CONTRACT` updated.
   - Switch the NT8 chart to the new contract; reload `TickStreamer`.
   - Restart the bot stack.

---

## 9. Daily monitoring workflow

Run these after the session closes (also automated by `PhoenixGrading`,
`PhoenixPostSessionDebrief`, `PhoenixWeeklyEvolution`). All read-only, all
write to `out/`.

```powershell
# After every session:
python tools/daily_session_summary.py
# Read out/daily_summary_<today>.md — flagged anomalies (silent strategies,
# signal-volume drops) are early warnings of a Sprint A gate misfiring.

# Weekly:
python tools/validation_tracker.py --post-b13-only
python tools/indicator_audit.py --post-b13-only

# Weekly OR after any risk-code change:
python tools/verify_halt_signatures.py

# As needed (after trade_memory grows significantly):
python tools/backfill_commissions.py
```

### P4-4 SQLite dual-write (2026-05-25)

The canonical reader stays JSON — `core.trade_memory.load_all_trades()`
merges `logs/trade_memory.json` (legacy) plus every per-bot
`logs/trade_memory_<bot>.json` and dedupes by `trade_id`. P4-4 adds a
**shadow** SQLite store at `data/trade_memory.db` that
`TradeMemory.record()` writes on every trade close. JSON is still the
source of truth; SQLite is observed only.

**Backfill once (after pulling P4-4, then idempotent forever):**

```powershell
cd "C:\Trading Project\phoenix_bot"
python tools/migrate_trade_memory_to_sqlite.py           # writes data/trade_memory.db
python tools/migrate_trade_memory_to_sqlite.py --dry-run # report-only
```

The CLI uses `INSERT OR REPLACE` keyed on `trade_id`, so a re-run after
any sim/prod session is safe and reconciles any rows the dual-write
path missed (e.g. a SQLite hiccup that the warning log caught).

**Inspect:**

```powershell
sqlite3 data/trade_memory.db
sqlite> PRAGMA user_version;                              -- 1
sqlite> SELECT COUNT(*) FROM trades;
sqlite> SELECT strategy, COUNT(*), SUM(pnl_dollars)
   ...> FROM trades GROUP BY strategy ORDER BY 2 DESC;
sqlite> SELECT * FROM strategy_halts WHERE cleared_at IS NULL;
sqlite> .schema trades
```

`data/trade_memory.db` is gitignored (already covered by
`data/**/*.db` in `.gitignore`).

**Cutover gate — DO NOT FLIP without these.** SQLite stays a shadow
store for ~30 days of dual-write. Flip the canonical reader to
SQLite-first with JSON fallback ONLY when:

1. **30 days** of dual-write data accumulated since the first prod
   trade post-P4-4. (Calendar days, not trading days — drift can
   happen on a weekend backfill.)
2. **Trade-count parity:** `COUNT(*)` in SQLite within **±1%** of
   `len(load_all_trades())` per `bot_id`.
3. **P&L parity:** `SUM(pnl_dollars)` per strategy within **±1%** between
   the two stores. Bigger deltas mean a row diverged — investigate
   before flipping.
4. **Zero `SQLite dual-write failed` warnings** in the last 7 days of
   `logs/phoenix.log` (grep that exact string).
5. **One full PhoenixWatcher reboot cycle** observed (the WAL file
   survives a `python.exe` kill cleanly).

The whole point of the dual-write window is to surface the
silent-failure class documented in `memory/feedback_silent_failures.md`
BEFORE we trust SQLite enough to read from it. If any of the five
gates is red, do not flip — the JSON path keeps working unchanged.

**Grader / config alignment (F-19, 2026-05-24):** the post-session
grader now queries `config/strategies.py` — there is no hardcoded
retired list. Un-retiring a strategy in config (flip `enabled=True` /
drop the `retired` flag) no longer breaks the grader; the next run
automatically stops asserting silence for that strategy. Conversely,
retiring a strategy (`enabled=False` OR `retired=True`) auto-enrolls it
into the P6 silence check with no code change required.

Statistical tier reference (used in `validation_tracker` decisions):

| Tier | Trades | Confidence | Decisions allowed |
|------|-------:|-----------:|---|
| INSUFFICIENT_SAMPLE | < 30 | none | WATCH only |
| PRELIMINARY | 30–99 | ~70% | WATCH or KILL if PF<0.7 |
| TENTATIVE | 100–384 | ~90% | + GRADUATE candidate |
| VALIDATED | 385–665 | ~95% | + SCALE candidate |
| HIGH_CONFIDENCE | 666+ | ~99% | full confidence |

Phoenix's 50-trade graduation gate sits inside PRELIMINARY — enough to start
making directional decisions, NOT enough to bet the farm on. The
validation_tracker tool surfaces this uncertainty explicitly via Wilson 95% CI
on win rate.

---

## 10. After any behavior-affecting commit

```powershell
# Run the test suite:
python -m pytest --tb=no -q
# Expected: 2,110+ pass / 19 skip / 0 fail (target as of 2026-05-18)

# If tests pass AND the commit changes runtime behavior:
# RESTART the bots — code changes DO NOT auto-deploy (rule 10).
```

`git commit` does not update a running bot. The process keeps its in-memory
code snapshot from launch. A bot that was launched on `<old-commit>` keeps
running `<old-commit>` until you kill and relaunch it. This cost $-106 on
2026-05-14 (see [incidents.md](incidents.md)).

---

## 11. Live-trading promotion gate (NOT YET — gated)

`LIVE_TRADING` stays `False` until:

1. Account ≥ $2,000 (currently $300).
2. Reconciliation harness (roadmap P1-1) passes for every enabled strategy.
3. 60 days of clean stack health with no `process_down` incidents.
4. Per-strategy Wilson-CI lower bound > 0.5 at TENTATIVE+ tier (n ≥ 100 live).
5. External dead-man's switch (P1-5) verified.
6. NT8 silent-stall auto-recovery (P1-4) verified.

The flip itself is two changes in `config/settings.py`: `LIVE_TRADING = True`
plus changing `ACCOUNT` from `"Sim101"` to the operator's live broker account
code. There is no canary-trade phase or "1 strategy goes live first" toggle —
all strategies on the validated set route to the live account. This is a known
weakness (synthesis Step 3 §"Capital-at-risk discipline" weak evidence
section); when the gate is otherwise green, build a canary toggle first.

---

## 12. P4-5 Statistical Validation (walk-forward + CPCV + DSR + PBO)

The promotion-gate harness. Closes F-24 in
`docs/audits/SYNTHESIS_2026-05-24.md`. Replaces the legacy "NOT YET
RUN (Phase C dependency)" checkboxes in `weekly_evolution.py`.

### When to run

- **Automatically:** every Sunday 18:00 CT via `PhoenixWeeklyEvolution`.
  Each commit body now embeds a per-strategy checkbox row with the
  actual numbers (DSR p-value, PBO, walk-forward verdict).
- **Manually before any `validated=True` flip:**

  ```powershell
  cd "C:\Trading Project\phoenix_bot"
  python tools/walk_forward_harness.py --strategy bias_momentum
  # Outputs:
  #   out/walk_forward_<date>_bias_momentum.md
  #   out/walk_forward_<date>_bias_momentum.json
  ```

  Optional flags:
  - `--since 2026-04-22`  — filter to trades on/after this date.
  - `--min-trades 200`    — sample-size gate for the top-level verdict
    (default 200, matches the §1 KNOWN_ISSUES heuristic).
  - `--out <prefix>`      — write to a custom path prefix.

### How to read the output

The CLI prints the verdict per sub-test:

```
=== bias_momentum — verdict: FAIL ===
  reason: failing sub-test(s): walk_forward, DSR, PBO
  n_trades: 361 (min 200)
  walk_forward: FAIL
  cpcv:         FAIL
  dsr:          FAIL
  pbo:          FAIL
```

Each verdict is one of:
- `PASS` — sub-test gate cleared.
- `FAIL` — gate failed; numbers visible in the markdown report.
- `INSUFFICIENT_DATA` — sample below that sub-test's threshold
  (walk-forward 60, CPCV 100, DSR 30, PBO 60). The report still
  produces a shape (no `KeyError` for downstream consumers) but no
  promotion decision can be made.

The `.md` artifact shows per-fold walk-forward Sharpes, the full DSR
breakdown (sample Sharpe, skew, excess kurtosis, expected-max-SR under
selection bias, DSR z-score, PSR, p-value), and the PBO logits summary.

### Promotion gate — what greenlights `validated=True`

A strategy may be promoted to `validated=True` ONLY when ALL of:

1. **walk-forward all-folds-positive** — every test-window Sharpe > 0
   across the 5 expanding-train folds (with 2% embargo).
2. **DSR p-value < 0.05** — the Deflated Sharpe Ratio test rejects the
   null of "true Sharpe ≤ 0 after selection-bias correction." Per
   Bailey & Lopez de Prado (2014).
3. **PBO < 0.5** — Probability of Backtest Overfitting strictly below
   chance. Per Bailey, Borwein, Lopez de Prado & Zhu (2017).

This is exactly what `tools/walk_forward_harness.run_all()` reports as
the top-level verdict `PASS`. If the verdict is `FAIL` or
`INSUFFICIENT_DATA`, DO NOT promote.

CPCV (Combinatorial Purged CV) is included for visibility but is
informational, not a gate — its median test Sharpe and fraction-positive
flag overfitting symptoms that DSR + PBO already catch.

### What the weekly commit body looks like now

For each enabled validated strategy (`DEFAULT_VALIDATION_STRATEGIES`
in `tools/routines/weekly_evolution.py`), the commit body emits one
checkbox row:

```
## Validation status

Per `tools/walk_forward_harness.py` (P4-5, closes F-24). Gate:
PBO < 0.5 AND DSR p-value < 0.05 AND walk-forward all-folds-positive.

- [ ] **bias_momentum** — `FAIL` (walk_forward=FAIL, DSR p=0.3014, PBO=0.314, n=361)
- [x] **high_precision_only** — `PASS` (walk_forward all-folds-positive, DSR p=0.0123, PBO=0.117, n=557)
- [~] **vwap_pullback** — `INSUFFICIENT_DATA` (n=94, need ≥200; walk_forward=INSUFFICIENT_DATA, DSR n/a, PBO n/a)

**DO NOT MERGE** — at least one strategy FAILED the P4-5 gate.
```

Glyphs: `[x]` PASS, `[ ]` FAIL, `[~]` INSUFFICIENT_DATA.

The cold-start fallback (no trades available at all) still emits the
historical `[ ] NOT YET RUN (Phase C dependency)` template so the gate
text is never silently absent.

---

## 13. P1-8 NT8 ATI verification

End-to-end check that Phoenix is capturing NT8 stop-order IDs and persisting
them so a bot restart can still cancel-and-replace its working stops.

While NT8 is live in afterhours sim mode:

```
1. Tail the bot's log:
   Get-Content logs/sim_bot.log -Tail 50 -Wait

2. Trigger any strategy to place a stop (or manually inject a stop via
   a small Python script that calls bridge.oif_writer.write_oif).

3. After ~3 seconds, check the NT8 outgoing folder:
   Get-ChildItem "C:\Users\Trading PC\Documents\NinjaTrader 8\outgoing\"
   Look for a `Sim101_<orderid>.txt` (or similar account-prefix). First
   line should be `WORKING;0;<price>`.

4. Check the persistence file:
   Get-Content data/active_stops.json
   Should now contain {"<trade_id>": "<stop_order_id>"}.

5. Trigger a stop-move (any strategy with managed exit). Tail the log
   for [STOP_ID_RECOVERED:...] or [STOP_MOVE_NO_ID:...] — recovery
   should fire if the in-memory ID was lost.
```

Health signals to expect in the log:

- `[NT8_ID_CAPTURE:<trade_id>] captured stop_order_id=<oid>` — the poll
  loop in `core.nt8_order_id_capture` saw a new WORKING file.
- `[STOP_ID_RECOVERED:<trade_id>] loaded <oid> from active_stops.json` —
  in-memory `pos.stop_order_id` was empty (bot restart, scale-out reset)
  and the recovery path picked up the persisted ID before a stop-move.
- `[STOP_MOVE_NO_ID:<trade_id>]` — both in-memory AND persisted lookups
  failed. The NT8 stop will NOT move; investigate why capture missed
  (NT8 ATI off, outgoing folder misconfigured, WORKING file format
  changed). This is the regression we are guarding against.
- `[STOP_ID_CLEAR_FAIL:<trade_id>]` (DEBUG-level) — `clear_stop_id`
  call on position close failed. Non-fatal; the file just keeps the
  stale entry until next overwrite. Audit `data/active_stops.json` if
  it grows past a few dozen entries.

If `data/active_stops.json` is missing after a stop was placed, capture
silently failed — check that `OIF_OUTGOING` in `config/settings.py`
points at NT8's real outgoing folder and that NT8 ATI is configured to
emit `{account}_{order_id}.txt` files.
