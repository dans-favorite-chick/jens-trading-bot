# NT8 Multi-Stream Recovery — Operator Runbook

**Last updated:** 2026-04-25
**Audience:** Operator (Jennifer) when the bridge reports >1 NT8 client and prices look corrupted
**Related code:**
- `core/bridge/stream_validator.py` — auto-detector
- `tools/check_nt8_outgoing.py --list-clients` — diagnostic
- `tools/nt8_stream_quarantine.py` — live monitor

---

## Background

On **2026-04-24** the bridge reported **3 NT8 client connections** (TCP source ports 55117, 55116, 55779), all self-identifying as `instrument=MNQM6`. One of those streams was feeding prices in the **~7,150 band** (a stale or rolled-over MNQ contract chart), while the genuine front-month MNQM6 was trading at ~27,415. The PRICE_SANITY guard added that day rejected ~30% of inbound ticks, but the underlying issue — extra `TickStreamer` indicators attached to the wrong charts in NT8 — remained.

This runbook is the manual recovery procedure plus the automated detector that catches this class of failure before any tick reaches strategy code.

---

## Symptoms

You will see **at least one** of:

- `tools/check_nt8_outgoing.py --list-clients` reports **`Active NT8 client connections: 2+`** with all instruments labeled the same.
- `[PRICE_SANITY] TICK REJECTED` log entries on sim_bot / prod_bot at sustained rates (5+ per minute).
- `out/nt8_clients_*.json` shows multiple `port` entries.
- `tools/nt8_stream_quarantine.py --watch` displays one or more rows with `quarantined: True`.
- `heartbeat/bridge_alert.json` exists with `event: client_quarantined`.

---

## Step 1 — Confirm with the diagnostic

```bash
cd "C:\Trading Project\phoenix_bot"
python tools/check_nt8_outgoing.py --list-clients --json > out/nt8_clients_$(Get-Date -Format yyyy-MM-dd).json
type out/nt8_clients_*.json
```

If `clients` length is `0` or `1` and prices look right, **stop here** — you don't have a multi-stream problem (NT8 may be down for the maintenance window — that's normal post-16:00 CT).

If `clients` length is `≥ 2`, proceed.

---

## Step 2 — Identify the offending chart in NT8

This is the manual part. The bridge sees source ports but cannot tell you *which chart in NT8 owns each port*. To find them:

1. Open NT8 → **Connections** tab (top menu bar).
2. For each entry in the Connections list, **right-click → "Show Charts"**. NT8 will list every chart attached to that connection.
3. For each chart returned:
   - Note the **instrument** in the title bar (e.g., `MNQ 06-26`, `MNQ 12-25`, `NQ 06-26`).
   - Verify the **last price** displayed matches the live MNQM6 price (around 27,000–28,000 as of late April 2026, per CME).
   - **The chart whose price is in a different band is the culprit.**

Common culprits:
- An old expiry (e.g., `MNQ 12-25`, `MNQ 03-26`) still attached with `TickStreamer.cs`. Roll-overs leave the previous chart open.
- A different root (`NQ 06-26` vs `MNQ 06-26`) — full-size NQ is at similar prices, but a chart on `MNQH9` from years ago can read at ~7,150.
- A test/scratch chart that someone forgot to close.

Verify the active expiry in **Tools → Database Management → Rollover** before assuming what "current" means.

---

## Step 3 — Recover

For the offending chart(s), **in this order**:

1. Right-click the chart → **Indicators…**
2. **Remove `TickStreamer`** from that chart. (Do NOT remove from your live chart.)
3. Click **OK**. The TCP socket to the bridge will close within seconds.
4. If the chart is on a wrong expiry, optionally update the instrument selector (top-left of chart) to the current front month. Press **F5** to reload data.
5. **Do NOT restart all of NT8** — it's not necessary and risks disconnecting the live broker session.

Re-run Step 1's diagnostic. The `clients` count should now be `1` (the legitimate live chart), and `[PRICE_SANITY] TICK REJECTED` should drop to zero within a minute on the sim_bot log.

---

## Step 4 — Confirm the auto-detector is armed

The validator described in `core/bridge/stream_validator.py` is wired into the bridge fanout. In a healthy state, you will see:

- `tools/nt8_stream_quarantine.py --watch` shows one row per active port, all with `quarantined: False` and `reason: ok`.
- `heartbeat/bridge_alert.json` is **either absent or older than 30 minutes**.
- `core.bridge.stream_validator.StreamValidator` health snapshot from the dashboard reports `accepted >> rejected` per port.

If a future rogue stream returns, the detector will:
1. Reject ticks via the static price band (cheapest signal).
2. Reject via cross-client MAD if the rogue claims the same instrument.
3. Reject via tick-grid alignment if the rogue is sending fractional non-grid prices.
4. After **5 consecutive rejections**, mark the port quarantined and write `heartbeat/bridge_alert.json`.

---

## Step 5 — Document the incident

Append a row to `memory/audit_log.jsonl` with:

```json
{"ts":"...","event":"nt8_multistream_recovery","ports_seen":[55117,55116,55779],"culprit_chart":"MNQ 03-26","action":"removed TickStreamer","tick_reject_count_before":XXX,"tick_reject_count_after":0}
```

This way the next time the issue recurs, future-you (and future-Claude-sessions) can see prior occurrences via `git log memory/audit_log.jsonl` or by grepping for `nt8_multistream_recovery`.

---

## Why this is hard to catch automatically

The bridge protocol does not currently track which TCP source-port owns which chart in NT8. NT8's `TickStreamer.cs` indicator does not (and cannot easily) include a chart-ID in its outbound JSON. Until that wire format includes a `source_chart` field, the detector can only:

- Quarantine a port that consistently violates the band / MAD / tick-grid signals.
- Surface the count of active clients to the operator.
- Page on `heartbeat/bridge_alert.json` for human follow-up.

Manual chart inspection is the authoritative step. The auto-detector buys time and prevents losses; it does not replace the operator opening NT8 and looking at chart titles.

---

## Future improvements (tracked in `OPEN_QUESTIONS.md`)

1. **`TickStreamer.cs` v2** — embed `source_chart_id` (a stable UUID per chart instance) in every tick payload. Bridge stores the mapping; quarantine can then auto-disconnect by chart, not just by socket port.
2. **Per-port instrument tagging** — bridge connection_events already log `NT8 instrument:` after each connect, but the binding is positional (last connect). Refactor so every tick carries the bridge-resolved instrument so cross-client MAD is bullet-proof.
3. **Auto-close-orphan-chart** AddOn — a NinjaScript that detects "TickStreamer attached to non-front-month MNQ" at NT8 startup and prompts the operator to remove. ~1 day of work.
