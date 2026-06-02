# BUG #1 — Disabled-Strategy Bleed

**Verdict: NOT-A-BUG (procedural).** The loader and eval gates work
correctly. The 29 SIGNAL events from `enabled: False` strategies in
`logs/history/2026-06-02_prod.jsonl` are stale artifacts from a
**prod_bot process that was running before today's 07:53:04 restart**.

---

## Observation

In `logs/history/2026-06-02_prod.jsonl`, 4 strategies marked
`enabled: False` in `config/strategies.py` emitted SIGNAL events:

| Strategy | SIGNALs today |
|---|---:|
| spring_setup | 18 |
| vwap_band_reversion | 6 |
| vwap_pullback_v2 | 4 |
| compression_breakout_v2 | 1 |

---

## The gates (both work correctly)

**Load-time gate** at [bots/base_bot.py:1727-1730](bots/base_bot.py:1727):
```python
if self.only_validated and not config.get("validated", False):
    continue
if not config.get("enabled", True):
    continue
```
Skips instantiation entirely. A strategy with `enabled: False` is
never added to `self.strategies`.

**Eval-time gate** at [bots/_strategy_dispatch.py:601-605](bots/_strategy_dispatch.py:601):
```python
for strat in self.bot.strategies:
    if not strat.enabled:
        ...
        continue
```
Second-line defense — if the instance somehow exists with
`enabled=False`, it is skipped at evaluation.

**Instance attribute** at [strategies/base_strategy.py:124](strategies/base_strategy.py:124):
```python
self.enabled = config.get("enabled", True)
```
Correctly mirrors the config value.

---

## Why the SIGNALs appeared

`logs/prod_bot_stdout.log:299818-299819` shows a restart banner at
2026-06-02 07:53:04 CT. The post-restart `Strategies: [...]` line
enumerates 11 strategies — the 4 disabled names are absent. The
first post-restart eval at 08:00:48 in `logs/history/2026-06-02_prod.jsonl`
also enumerates only those 11.

All 29 disabled-strategy SIGNAL events have timestamps **before**
07:53:04. The latest is `spring_setup` at 07:38:58. The earlier
prod_bot process (booted 2026-05-28 19:24:19 per
`logs/prod_bot_stdout.log:290178`) had loaded the 4 strategies when
they were still `enabled: True` and kept them in memory until killed.

The operator-approved disable commits sat on disk while the old
process kept its boot-time roster — the canonical
`code_changes_dont_auto_deploy.md` failure pattern documented in
operator memory.

---

## Trade execution impact

**Zero.** No `TRADE QUEUED` lines for spring_setup, vwap_band_reversion,
vwap_pullback_v2, or compression_breakout_v2 appear in
`logs/prod_bot_stdout.log` on 2026-06-02. The disabled strategies
emitted SIGNAL records to history (because the old process still had
them loaded) but no trades were attempted on those names.

---

## Proposed fix (NO CODE — for implementation session)

The existing gates are correct; no code fix is required for the gate
logic itself.

If the operator wants belt-and-suspenders defense, the implementation
session could add (optional, future-proof):

1. **Config drift detector** — periodic background task (every 5 min,
   or once per session-start) that compares the loaded
   `self.strategies` names against the current on-disk
   `config/strategies.py` `enabled` flags. CRITICAL-logs any
   mismatch + fires Telegram. Catches "forgot to restart after
   config edit" in minutes instead of waiting for the operator to
   spot stale SIGNAL records.
2. **Restart banner enforcement** — extend the post-restart banner
   to include a one-line diff against the prior session's roster
   (e.g., `Strategies removed since last boot: [spring_setup,
   vwap_band_reversion, vwap_pullback_v2, compression_breakout_v2]`)
   so the operator sees the intended change confirmed at restart.

Neither is required to close BUG #1. The bug as originally framed is
not a code bug; it is an operational reminder pattern that the
operator already knows about (see `memory/code_changes_dont_auto_deploy.md`).

## Self-second-guess

- This conclusion assumes the operator's prod_bot restart at 07:53:04
  was deliberate (i.e., the new process is the intended one). If the
  *old* process is still running somewhere alongside the new one, the
  disabled-strategy SIGNALs could be from a still-live ghost process.
  A quick `tasklist | findstr python` (or equivalent on the Trading
  PC) would confirm only one prod_bot PID exists; recommended as a
  sanity step before the implementation session ships any defensive
  feature. (Note: my BUG #2 chart-orders investigation already shows
  the new PID 48828 is the active one and is correctly loading only
  11 strategies — so the ghost-process scenario is unlikely.)
- The config-drift detector idea is a NEW FEATURE; it is NOT required
  by today's evidence and should only be picked up if the operator
  values the additional automation.
