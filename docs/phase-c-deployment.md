# Phase C Deployment Notes — lab_bot → sim_bot Flip

Operator guide for cutting over from paper-only `lab_bot` to the live-sim `sim_bot`
(16 dedicated NT8 Sim accounts, real OIF writes, real NT8 slippage). Each strategy
is isolated: $2,000 start, $200/day loss cap, $1,500 floor kill-switch.

---

## 1. Pre-flip checklist (one-time)

Run these from the repo root (`C:\Trading Project\phoenix_bot`) in a PowerShell
session. Set `PYTHONPATH` once per shell so the imports resolve.

```powershell
$env:PYTHONPATH = "C:\Trading Project\phoenix_bot"
python tools\verify_routing.py
```

Expected: all 16 dedicated Sim accounts resolve, no "UNMAPPED" warnings.

```powershell
python -m pytest --tb=no -q
```

Expected: **558+ pass, 6 B15-backlog fail**. Any other failure count = stop and
investigate before flipping.

```powershell
Get-Content logs\strategy_halts.json
```

Expected: `{"halted":[]}` or file not present. If halts are listed from a previous
session, clear them (see section 4) or leave intentionally-halted entries in place.

**Manual NT8 UI check**: open NT8 → Connections → confirm all **17** Sim accounts
are connected (green) and ATI is enabled. The 17 = 16 dedicated + `Sim101` default.
Byte-exact names come from `config/account_routing.py` — do not re-type them.

---

## 2. Flip procedure

Ordered. Each step assumes the previous completed cleanly.

### 2a. Stop current lab_bot + watchdog

*Operator handles process termination per house rule.* For reference only:

```powershell
# Example — Jennifer runs this herself:
taskkill /F /IM python.exe /FI "WINDOWTITLE eq lab_bot*"
taskkill /F /IM python.exe /FI "WINDOWTITLE eq watchdog*"
```

### 2b. Start bridge (if not already running)

The bridge must be up before any bot connects. If already running on `:8767`, skip.

### 2c. Start sim_bot

```powershell
$env:PYTHONPATH = "C:\Trading Project\phoenix_bot"
python bots\sim_bot.py
```

### 2d. Start watchdog (updated to track sim_bot)

```powershell
python tools\watchdog.py
```

### 2e. Verify startup banner

sim_bot should print (verbatim tokens to look for):

```
[SIM] 16 strategies loaded — LIVE execution
[SIM] Per-strategy: $2000 start / $200 daily cap / $1500 floor
[SIM] Daily flatten: 16:00 CT
```

All 16 strategy names should be listed. If the count is wrong, stop and check
`config/account_routing.py`.

### 2f. Verify bridge health

```powershell
curl http://localhost:8767/health
```

Confirm the response's `bots_connected` array includes `"sim"`. During the
transition window (lab still running alongside sim), both `"lab"` and `"sim"`
will appear — that is expected.

---

## 3. Monitoring — first hour and first day

### History log
`logs/history/YYYY-MM-DD_sim.jsonl` — note the **`_sim`** suffix (vs `_lab`). This
is the source-of-truth trade log for the new bot. Tail it live:

```powershell
Get-Content logs\history\2026-04-21_sim.jsonl -Wait -Tail 20
```

### Watchdog
`logs/watchdog.log` — expect `sim:UP(Xm)` alongside `prod:UP` and `lab:UP`
(during the transition window where lab is still running).

### Per-strategy dashboard panel
Once the dashboard panel is wired (see section 7), each strategy's balance,
daily P&L, and halt state is visible there. Until then, read the state files
in `logs/` directly.

### First halt
When a strategy first kisses the $1,500 floor expect:

- `[CRITICAL] [FLOOR_HIT]` line in sim_bot stdout / log
- Entry appended to `logs/strategy_halts.json`
- No further orders from that strategy until manually re-enabled

---

## 4. Halt recovery

Use `tools/reenable_strategy.py`:

```powershell
# List everything currently halted:
python tools\reenable_strategy.py

# Clear a single top-level strategy:
python tools\reenable_strategy.py bias_momentum

# Clear a sub-strategy (dotted path):
python tools\reenable_strategy.py opening_session.orb

# Clear all halts (use sparingly — acknowledges every floor hit):
python tools\reenable_strategy.py --all
```

Halt state lives in `logs/strategy_halts.json`; the CLI is the supported way to
edit it — do not hand-edit the JSON while sim_bot is running.

---

## 5. Rollback

If sim_bot misbehaves:

1. **Stop sim_bot** — `taskkill` (operator's call, as in 2a).
2. **Restart lab_bot** from `bots/lab_bot.py` — the file remains on disk as a
   safety net. Same `PYTHONPATH` setup as above.
3. **Check NT8** — any pending/working orders on the 16 dedicated Sim accounts
   may need manual flatten. Inspect each sub-account in the NT8 UI before
   declaring the rollback clean.
4. **Reverting commits** — `git log --oneline` shows the Phase C commits. A
   true rollback requires a merge-base reset; **coordinate with the Claude chat
   before running any destructive git operation.**

---

## 6. Daily flatten behavior (operational contract)

- **16:00 CT every weekday**: ALL open sim positions across ALL 16 strategies
  are EXITed by `bots/daily_flatten.py`.
- **Globex pause**: 16:00–17:00 CT. Bot is quiet during the hour; signals are
  evaluated freely again after 17:00 CT.
- **Overnight holds** (17:00 CT → 16:00 CT next day) are **explicitly allowed**.
  The 16:00 flatten is the only forced exit.
- **Account routing on flatten**: every EXIT uses the registered per-strategy
  account. There is no cross-contamination — strategy A's flatten never fires
  on strategy B's sub-account.

---

## 7. Known gaps / follow-ups

- **Base_bot tick-exit loop** currently iterates the sole position only;
  multi-position iteration is a separate Stage 2b landing. Check the latest
  commit before assuming it's merged.
- **Dashboard** does not yet surface per-strategy risk registry state (balance,
  daily P&L, halt flag). Reading `logs/strategy_halts.json` + the history
  JSONL is the interim workaround.
- **Telegram notifications** are stream-unified — all 16 strategies share one
  channel. Per-strategy routing is future work.
