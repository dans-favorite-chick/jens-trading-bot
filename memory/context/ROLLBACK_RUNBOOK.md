# Phoenix Bot — Rollback Runbook

_How to revert if weekend changes misbehave._
_Last updated: 2026-04-17 Friday Tier 1 rebuild_

## Pre-rebuild baseline

**Git tag:** `v-pre-rebuild-2026-04-17`
**Commit:** `a5677c7` — "Fix warmup problem: persist aggregator state across restarts"
**Branch:** `feature/knowledge-injection-systems`

## Quick diagnostic — is rollback needed?

If you're unsure whether to roll back, first check:

```bash
cd "C:\Trading Project\phoenix_bot"
tail -20 logs/prod_bot_stdout.log     # any new ERRORs since restart?
grep -iE "traceback|error" logs/prod_bot_stdout.log | tail -10
git status                            # any file changes you didn't expect?
python tools/memory_writeback.py --check-pending
```

A single error doesn't mean rollback — check if it's a known non-blocking warning (COTFeed, CalendarRisk both fail non-fatally today). Only roll back if:

- Bot crashes on startup
- Bot fires signals but no trades execute (OIF pipeline broken)
- Bot loses money with no clear explanation for 3+ trades
- Integrity check fails (memory corruption)

## Full rollback procedure

### Step 1: Stop the bots

```bash
# Find PIDs
tasklist | grep -iE "python.exe"
# Or on Windows cmd:
#   tasklist /FI "IMAGENAME eq python.exe"

# Kill prod + lab (not bridge/watchdog/dashboard)
taskkill //PID <prod_pid> //F
taskkill //PID <lab_pid> //F
```

### Step 2: Git reset to pre-rebuild tag

**⚠️ DESTRUCTIVE — wipes all uncommitted changes since the tag.**

```bash
cd "C:\Trading Project\phoenix_bot"
# Save anything you don't want to lose first:
git stash push -m "pre-rollback backup" 2>&1

# Reset to tag
git reset --hard v-pre-rebuild-2026-04-17
```

### Step 3: Restore only the memory directory (OPTIONAL)

If you want to keep the memory logs but roll back code:

```bash
# Do NOT include this step in a normal rollback.
# Memory is append-only — it's safe to keep.
# Skip this unless you specifically need a clean memory slate.
git checkout HEAD -- memory/
```

### Step 4: Restart bots

```bash
cd "C:\Trading Project\phoenix_bot"
# Use launch_*.bat scripts or direct Python:
python bots/prod_bot.py &
python bots/lab_bot.py &

# Verify watchdog sees them
tail -5 logs/watchdog.log
```

### Step 5: Verify healthy

```bash
# Should see "prod:UP" and "lab:UP" within 30s
tail -10 logs/watchdog.log

# Should see no Tracebacks in bot stdout
tail -20 logs/prod_bot_stdout.log
tail -20 logs/lab_bot_stdout.log
```

### Step 6: Notify + document

- Message yourself on Telegram: "Rolled back to pre-rebuild state at [time]"
- Append entry to `memory/context/RECENT_CHANGES.md` documenting the rollback
- `python tools/memory_writeback.py --summary "Rollback: rolled back to v-pre-rebuild-2026-04-17" --decisions "Reason: [what broke]" --commit`

## Partial rollback — roll back ONE module

If only one specific module is broken:

```bash
cd "C:\Trading Project\phoenix_bot"
# Revert a single file to pre-rebuild state
git checkout v-pre-rebuild-2026-04-17 -- strategies/bias_momentum.py

# Or revert a whole directory
git checkout v-pre-rebuild-2026-04-17 -- strategies/

# Restart affected bot
taskkill //PID <prod_pid> //F
python bots/prod_bot.py &
```

## What CANNOT be rolled back

- **NT8 indicators you deployed** (MQBridge, TickStreamer) — those live in `C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\bin\Custom\Indicators\`. To roll back, manually uninstall from NT8.
- **Claude Code hooks in `~/.claude/settings.json`** — manually remove the `"hooks"` block if they misbehave.
- **`C:\temp\menthorq_levels.json`** content — MQBridge overwrites every 60s. To stop updates, remove MQBridge from NT8 chart.
- **Scheduled tasks** in Claude — manage via `/scheduled` command in Claude CLI.

## Emergency halt (different from rollback)

If you don't want to rollback but DO want to stop trading immediately:

```bash
# Create HALT marker file (Saturday build adds watchdog support for this)
echo "halted by user at $(date)" > "C:\Trading Project\phoenix_bot\memory\.HALT"

# Or manual: kill the bots
tasklist | grep python
taskkill //PID <prod_pid> //F
taskkill //PID <lab_pid> //F
```

## Contact list

- MQBridge / NT8 indicator issues → see CLAUDE.md + KNOWN_ISSUES.md
- Telegram issues → `.env` file has TELEGRAM_TOKEN + TELEGRAM_CHAT_ID
- If totally stuck → restore from OneDrive backup (`C:\Trading Project\phoenix_bot` is in OneDrive)
