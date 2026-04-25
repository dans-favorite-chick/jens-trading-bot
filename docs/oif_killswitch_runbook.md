# OIF Kill-Switch Runbook

Phoenix Phase B+ section 3.2. Operator-facing reference for flattening
NT8 across every configured account by writing OIF files directly into
NT8's `incoming/` folder.

## What this is

`tools/oif_killswitch.py` is the NT8-side counterpart to `KillSwitch.bat`.

- **`KillSwitch.bat`** kills Phoenix Python processes. NT8 keeps running
  with whatever working orders / open positions it had, so a stop-loss
  bracket might still be sitting unattached in NT8 after Python exits.
- **`tools/oif_killswitch.py`** writes two OIFs per account into NT8's
  `incoming/` folder so NT8 itself flattens:
  1. `CANCELALLORDERS;<account>;;;;;;;;;;;`     (kill working orders)
  2. `CLOSEPOSITION;<account>;<instrument>;GTC;;;;;;;;;`  (flatten any
     open position -- skipped automatically if the outgoing/ position
     file already shows `FLAT;0;0`)

`KillSwitch.bat` invokes the kill-switch script BEFORE killing Python,
so the desktop shortcut now does both jobs.

## When to use it

- **Lock-up.** NT8 GUI freezes / stops responding to manual flatten.
- **Runaway strategy.** A bot is mis-sizing or mis-pricing and you want
  every working order pulled across every Sim account at once.
- **End-of-day emergency flatten.** The 15:54 CT DailyFlattener didn't
  fire (bridge crashed, bot crashed, scheduled task disabled).
- **Stranded brackets after a Python crash.** Phoenix died mid-trade and
  there are now stop / target orders sitting in NT8 with no Python
  process tracking them.

## Two ways to invoke

### A. Big red button (covers most cases)

Double-click `KillSwitch.bat` (or its desktop shortcut). It runs
`tools/oif_killswitch.py` first (flattens NT8 across every configured
account), then kills the Phoenix Python stack.

### B. Standalone CLI (finer control)

```
python tools/oif_killswitch.py
```

Common flags:

| Flag                       | What it does                                              |
|----------------------------|-----------------------------------------------------------|
| `--account NAME`           | Target one account; repeat to target several.             |
| `--cancel-only`            | Only CANCELALLORDERS; leave open positions alone.         |
| `--close-only`             | Only CLOSEPOSITION; leave working orders alone.           |
| `--dry-run`                | Print the plan, write nothing.                            |
| `--instrument MNQU6`       | Override instrument used in CLOSEPOSITION (default: settings.INSTRUMENT). |

**Always do a dry-run first if you are unsure of the blast radius:**

```
python tools/oif_killswitch.py --dry-run --account Sim101
```

Examples:

```
# Cancel all working orders on every configured account, but DON'T
# touch any open positions.
python tools/oif_killswitch.py --cancel-only

# Force-close just one stuck position.
python tools/oif_killswitch.py --close-only --account "SimBias Momentum"

# Full flatten of one account (cancel + close).
python tools/oif_killswitch.py --account Sim101
```

## Verification after firing

1. Wait ~3 seconds. NT8's ATI consumes incoming/ files within
   milliseconds; the position files in outgoing/ update on the next
   account heartbeat.
2. Run:
   ```
   python tools/check_nt8_outgoing.py
   ```
   Every account in the "Current positions" block should read `FLAT 0@0`.
   Any line that still shows `LONG`/`SHORT` means NT8 didn't consume the
   CLOSEPOSITION (see next section).

## What to do if NT8 doesn't consume the OIF

The `incoming/` folder accumulating files (rather than emptying) means
NT8's ATI isn't reading them. Most common causes, in order of frequency:

1. **ATI not connected.** Open NT8 -> Connections -> verify each Sim
   account is connected and that ATI (Tools -> Options -> Automated
   Trading Interface) is enabled. Re-enable connection and the backlog
   in `incoming/` will be consumed within a second.
2. **NT8 not running.** `KillSwitch.bat` does NOT kill NT8. If NT8 was
   already shut down when the kill-switch ran, the OIFs sit in
   `incoming/` until NT8 starts. Either start NT8 or manually delete
   them from `C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\`.
3. **Account name mis-spelled.** `python tools/oif_killswitch.py
   --dry-run` prints the literal account name written into each OIF.
   Compare to the NT8 account dropdown -- a single character mismatch
   will be silently rejected.
4. **LIVE_ACCOUNT guard tripped.** If the script printed
   "SKIPPED (LIVE_ACCOUNT guard) -- refusing CANCEL/CLOSE on live
   account" for the account you wanted to flatten, that account matches
   the `LIVE_ACCOUNT` env var and the script will not touch it. This is
   intentional: Phoenix never writes OIFs to the live brokerage account.
   Flatten the live account manually in the NT8 GUI.

## Exit codes

| Code | Meaning                                                              |
|------|----------------------------------------------------------------------|
| 0    | Every targeted account had its OIFs written successfully.            |
| 1    | Partial -- at least one write failed, or LIVE_ACCOUNT skipped some.  |
| 2    | Configuration error (no accounts resolvable, mutually exclusive flags). |
