# NT8 Disconnect Recovery — Operator Runbook

_Filed 2026-06-05 alongside FINDING-2026-06-05-NT8-DISCONNECT-OBSERVABILITY._

## When you'll see this

The watcher fires a `nt8_disconnected` incident (RED_ALERT during
market hours, MINOR otherwise) when **both** of the following are
stale at the bridge:

- **Tick age** > 300 s (last MNQ tick from NT8)
- **Heartbeat age** > 60 s (TCP-level keepalive from TickStreamer.cs)

OR when the bridge has already logged a `NT8 SOCKET_DEAD` event in
its recent `connection_events` ring.

This is **distinct** from `silent_stall`. Silent stall means NT8 is
still emitting heartbeats but the tick feed has frozen (chart locked
up, data subscription paused). NT8 disconnect means NT8 itself is
gone (application closed, machine asleep, network down).

The 2026-06-04 21:43 → 2026-06-05 09:00 outage was an
NT8-disconnect, not a silent stall: NT8 was manually disconnected
overnight (cause unknown, presumed operator action or NT8-side
dropout).

## What to check, in order

1. **Is NT8 running?** Look for `NinjaTrader.exe` in Task Manager or
   on the Windows taskbar. If the application is closed, launch it
   via the desktop shortcut (PhoenixStart) — that also re-launches
   the bot stack.
2. **Is the Control Center connected?** In NT8: **Connections menu →
   Connect → <your data feed name>**. A red dot next to the
   connection name means disconnected; green is connected.
3. **Is the chart still receiving ticks?** Look at the bottom-right
   status bar of NT8 — the data-feed indicator should show "Live"
   and a recent timestamp. If it shows "No data" or a stale
   timestamp, the data subscription dropped — re-connect via step 2.
4. **Is the machine awake?** Windows can suspend overnight if the
   power plan isn't set to "Always on". Move the mouse to wake; if
   the machine had been sleeping, the rest of the stack may need a
   restart.
5. **Network check.** Open a browser and confirm `https://google.com`
   loads. Data-feed providers route over the same internet
   connection; an upstream outage hits NT8 first.
6. **TickStreamer.cs indicator.** In NT8: **NinjaScript Editor →
   Indicators → TickStreamer**. Make sure it's compiled (no red
   error indicators) and applied to the active MNQ chart.

## When NT8 is back

The next watcher cycle (≤ 60 s) will see fresh ticks. The dedup
state machine will:

1. Detect that `nt8_disconnected` is no longer active.
2. Emit ONE `phase=RESOLVED` incident with the active-duration in
   seconds — this is the all-clear text.
3. Stop emitting until the next OPEN transition.

If you've silenced SMS, the `[RESOLVED]` text is your cue to
re-enable. The phase tag (`[OPEN]` / `[ESCALATED]` / `[RESOLVED]`)
also surfaces in the Twilio SMS body and the Telegram message.

## When the alert is wrong

If you confirm NT8 is connected and the bridge is healthy but the
watcher keeps emitting `nt8_disconnected`:

- Confirm the bridge `http://127.0.0.1:8767/health` endpoint reports
  fresh `nt8_last_tick_age_s` and `nt8_last_heartbeat_age_s`. If
  those numbers are stale at the bridge but NT8 is sending data, the
  WebSocket between TickStreamer and the bridge is the failed link —
  inspect `logs/bridge_stderr.log` for connection errors.
- The compound threshold (`tick > 300s AND heartbeat > 60s`) is
  intentionally conservative. If you see false-positives during a
  low-volatility lunch session, raise `NT8_DISCONNECT_TICK_S` in
  `tools/watcher_agent.py` — but be aware the operator-side cost of
  a false positive is much lower than missing a real 12-hour stall.

## Cross-references

- `tools/watcher_agent.py:NT8_DISCONNECT_TICK_S` /
  `NT8_DISCONNECT_HB_S` — the compound thresholds.
- `tools/watcher_agent.py:_check_bridge_health` — the spot-check
  emit site.
- `bridge/bridge_server.py:780-838` — the upstream
  SOCKET_DEAD / SILENT_STALL detection (PROTECTED).
- `tools/nt8_silent_stall_recovery.py` — optional auto-restart
  daemon (gated by `PHOENIX_NT8_AUTO_RECOVERY=1`).
- `out/propose_silent_stall_halt_2026-06-05.md` — proposal for a
  bot-side signal-fire HALT when NT8 is disconnected (deferred per
  Round 3 sprint).
- FINDING-2026-06-05-PHANTOMGUARD-DEDUP-BUG — why this runbook
  matters: pre-fix, a 12-h disconnect produced 700+ duplicate SMS
  alerts and the operator silenced the channel. The dedup layer
  ensures one OPEN + escalations at the operator-controlled cadence
  + one RESOLVED on clear.
