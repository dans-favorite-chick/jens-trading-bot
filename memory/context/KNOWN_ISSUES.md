# Phoenix Bot — Known Issues

_Open issues that haven't been resolved yet. Resolved issues moved to semantic/lessons_learned.md._

_Last refreshed: 2026-05-13 AM (graceful /shutdown shipped; 106s reconnect cycle observed)._

## 🟠 OPEN — Bot disconnects every ~106s during 0-tick market conditions

**Symptom (observed 2026-05-13 08:22 – 08:34 CDT, 10 cycles):**
Both `prod_bot` and `sim_bot` disconnect from the bridge with
`reason=nt8_stale_NNNs` after exactly ~106 seconds of uptime, every cycle.
Watchdog auto-restarts via the new bulletproof `_start_bot` path (commit
`8b471af`) — all restarts succeed; the bots reconnect within ~9s. But
the cycle repeats indefinitely until ticks start flowing.

**Sample (from `logs/watchdog.log`):**

```
08:32:39 Restart command sent — PID=7960  (prod)
08:32:46 RECONNECTED after 9.1s downtime  (prod)
... 106 seconds of UP ...
08:34:32 DISCONNECTED — reason=nt8_stale_1215s, uptime_was=107s, total_disconnects=10
```

**Hypothesis (not yet confirmed):**
The bot's application-level WS watchdog (`_ws_watchdog_loop` from commit
`31efe2f`) is designed to force a reconnect if WS goes silent for >90s,
to defend against silent TCP half-close. During 0-tick market lulls,
the WS technically IS silent (no tick frames inbound), so the watchdog
fires a defensive reconnect. The reconnect succeeds, then 90+s later it
fires again, in a steady cycle.

**Important — this is NOT caused by the CREATE_NEW_PROCESS_GROUP zombie
bug fixed in 8b471af.** The cycle is a clean disconnect → clean
auto-restart → reconnect, repeating. The bulletproof fix IS WORKING:
10 successful restarts via the formerly-zombie path is the strongest
production verification of `8b471af` to date.

**Side observation:**
Bridge health endpoint reports `nt8_status: live, nt8_last_heartbeat_age_s:
0.1` while `ticks:0/s` — meaning TickStreamer's TCP-level heartbeat
(`HEARTBEAT_MS=3000`) is fine, but no MARKET DATA ticks are flowing.
This could be an NT8 data subscription issue, a TickStreamer bug, or
just a genuinely silent overnight window. Whichever it is, the bot's
WS watchdog shouldn't be reconnecting in a tight loop.

**Action (deferred, not blocking):**
1. Add a non-tick "is the WS itself alive?" check — e.g., bridge could
   send a periodic `wsping` to bots that the bot's WS watchdog could
   count as proof of life, distinct from tick flow.
2. OR raise the WS watchdog threshold from 90s to e.g. 180-300s for
   overnight/quiet hours so it doesn't fire on legitimate low-flow.
3. OR investigate why TickStreamer is sending heartbeats but no ticks
   during an open market window (likely a NT8 data subscription issue).

**First observed:** 2026-05-13 08:22 CDT.
**Total disconnects since first observation:** 10+ (still going as of
08:34 CDT).

## 🟡 Sprint M Tier 2.3 — tape reader tuning candidates (low priority, data-collection phase)

## 🟡 Sprint M Tier 2.3 — tape reader tuning candidates (low priority, data-collection phase)

### Tape reader threshold may be too low for active regimes

**Symptom**: with `threshold_contracts=25` (default in `core/tape_reader.py`), the 50-print rolling buffer filled within seconds of bot launch overnight. RTH activity will likely fill it even faster, meaning the "rolling window" effectively becomes a sliding view of the last few minutes only.

**Action**: after a few RTH sessions of data collection, audit the size distribution of recorded prints. If the median is well above 25, raise the threshold (try 50, 100). Pass via `TapeReader(threshold_contracts=N)` in `bots/base_bot.py:__init__`. Or — better — make the threshold adaptive based on session-average size × K (e.g., 5x).

**First observed**: 2026-05-12 23:19 CDT (immediately after Tier 2.3 shipped).

### NT8 `vol` field semantics — per-trade vs aggregated

**Symptom**: tape_reader recorded 218 / 219 / 221 contract single-tick sizes at 23:18:56 CDT all at the same price. Three consecutive identical-price ticks of 200+ contracts each is consistent with an institutional sweep, BUT could also be an artifact of NT8 reporting aggregated bar volume rather than per-trade size on some tick subtypes.

**Why this matters**: side classification via the Lee-Ready quote rule (in `_classify_side`) assumes each tick = one trade. If `vol` is aggregated, the "last price" used for side detection may not represent the actual mix of buy vs sell flow inside the aggregation window.

**Action**: open a forensic pass on `logs/volumetric_history.jsonl` or raw tick stream from the bridge to count how often `vol >= 50` ticks have `bid != ask` consistency with the quote rule. If side classifications match other footprint-side detection (in `footprint_cvd_reversal`), the data is fine. If not, may need to filter by tick subtype or use a different size proxy.

**Reference**: TickStreamer.cs emits tick records as `{"vol": <int>}` per the docstring at line ~332 of base_bot.py.

## 🔴 OPEN

### NT8 SILENT_STALL pattern (data subscription freeze)

**Symptom:** NT8 reports "connected" but forwards 0 ticks/second for an
extended window. Bot's watchdog detects `NT8:live ticks:0/s` but only
logs — does not currently restart NT8 or page the operator.

**Status (2026-04-25):** WatcherAgent has an explicit escalation path
for this pattern (60s threshold → Telegram, 5min threshold → Twilio
SMS) that was added during the watcher build. That mitigates the
"trader doesn't know" failure mode. The underlying NT8 freeze is still
not auto-recovered; a clean restart of NT8 is the only fix. Logged
here because it's still the highest-impact open reliability gap.

**Originally observed:** 2026-04-16 from 07:56 to 11:11 CDT, missed
the entire primary trading window.

**Action:** Dedicated session to add an NT8 auto-restart hook (kill +
re-launch via shortcut) when stall exceeds 5 minutes. Not blocking
anything today.

### Scheduled task lattice partially registered (post-reboot)

**Status (2026-04-25 EOD):** The 14:31 CDT TeamViewer-initiated reboot
dropped four of the five newly registered Phoenix scheduled tasks.
Currently registered: `PhoenixLearner` only.

**Pending:**
- `PhoenixGrading` (16:00 CT Mon-Fri)
- `PhoenixRiskGate` (on-boot)
- `PhoenixMorningRitual` (06:30 CT Mon-Fri)
- `PhoenixPostSessionDebrief` (16:05 CT Mon-Fri)
- `PhoenixWeeklyEvolution` (Sun 18:00 CT)

**Fix:** Re-run all `scripts/register_*.ps1` scripts as Administrator.
Each is idempotent. Em-dash / schtasks / python-alias issues all fixed.

## 🟡 LOW PRIORITY / WATCHLIST

### CPCV / DSR / PBO validation harness — Phase C dependency

**Status (2026-04-25):** `tools/routines/weekly_evolution.py` ships with
a `VALIDATION_STATUS_TEMPLATE` constant that emits CPCV / DSR / PBO
checkboxes reading "NOT YET RUN (Phase C dependency)" in every weekly
commit body. Three unit tests enforce this in
`tests/test_routines/test_weekly_evolution.py`.

**Action:** When Phase C produces enough trades for statistical
validation (rough heuristic: ~200 sim trades per strategy minimum), wire
in actual CPCV folds + DSR p-value + PBO computation. Update the
checkboxes to `[x]` once each metric is computed. DO NOT MERGE any
weekly evolution PR with these still unchecked.

### CalendarRisk fetch consistently fails

Log shows repeated warnings: `[CALENDAR] Fetch failed (non-blocking): No
module named 'core.external_data'`. Calendar awareness is broken; bot
operating without news event blackouts.

**Action:** Defer. Non-blocking; Finnhub is now active and will catch
Tier-1 events through the calendar window logic.

### COTFeed URL encoding error

`[COT] CFTC API failed: URL can't contain control characters`. COT data
not flowing.

**Action:** Defer. Low value, high maintenance data source.

### Level 2 depth — only summary forwarded

**Status (2026-04-17 audit):** TickStreamer.cs tracks 5 bid + 5 ask
levels via `OnMarketDepth`, but only sums them into `bid_stack` /
`ask_stack` before sending (throttled 500ms). Per-level size over time
NOT preserved → iceberg detection and deep footprint patterns limited.

**Action:** Enhance TickStreamer to send per-level DOM arrays in a later
dedicated session. Non-blocking.

### NT8 trade arrow display missing — DIAGNOSED 2026-04-17

**Root cause:** Phoenix places orders via NT8's built-in ATI (reads
OIFs from `incoming/`). ATI executes trades correctly but does NOT draw
chart arrows. Arrows are normally drawn by a NinjaScript Strategy, not
by ATI-placed external orders.

**Fix option 1 (try first — zero code):** Right-click chart →
Properties → enable "Show executions" checkbox.

**Fix option 2:** Build custom `TradeMarker.cs` indicator that watches
`outgoing/` folder for fills and draws arrows. ~2 hours of work,
deferred.

**Not a blocker** — trades execute correctly, just a display issue.

## ✅ RECENTLY RESOLVED

### NT8 multi-stream issue — RECURRING (closed again 2026-04-25, root cause documented)

**The 2026-04-19 "single client confirmed" diagnosis was incomplete.** The
issue recurred today (2026-04-25) and we now have the real root cause and
a definitive cleanup playbook.

**Root cause:** three independent failure modes layered together:

1. **Stale legacy NT8 source files** still installed and compiled into
   `NinjaTrader.Custom.dll`:
   - `Indicators\JenTradingBotV1DataFeed.cs` (V1-era WebSocket indicator,
     `IsSuspendedWhileInactive=false`, broadcasting synthetic
     mom/prec/conf fields plus a secondary series whose price scale
     produced the corrupt ~7,196 stream)
   - `Strategies\OLDDONTUSEMarketDataBroadcasterv2.cs` (V2-era WebSocket
     strategy, competing OIF writer at the ATI path)
2. **NT8 `<ShowDefaultWorkspaces>true</ShowDefaultWorkspaces>` setting**
   auto-loading a workspace (e.g. `Jen's Fav.xml`, `Jen's indicators.xml`)
   that contained 9+ hidden MNQM6 charts plus ESM6/AUDUSD/SuperDOM
   windows. Hidden via `IsWindowVisible=false` — invisible in taskbar,
   no Window menu in this NT8 build to reveal them.
3. **`PHOENIX_STREAM_VALIDATOR=0`** — bridge-level defense was off
   (default-off was reasonable on day 1, but should now be ON).

**Cleanup playbook (definitive):**

1. Quarantine both legacy `.cs` files:
   `Move-Item <file>.cs <file>.cs.disabled_<date>`
2. Run `tools/nt8_unhide_all_windows.ps1` from elevated PS to surface all
   hidden NT8 windows via Win32 `ShowWindow(SW_SHOWNORMAL)`.
3. Manually close every unwanted chart; keep one MNQM6 with TickStreamer.
4. Save clean workspace, set as new baseline.
5. Tools → Options → General → uncheck "Show default workspaces on
   startup".
6. NinjaScript Editor → F5 to recompile (purges legacy classes from
   cached DLL).
7. Full NT8 restart. With no chart open, `(Get-NetTCPConnection
   -LocalPort 8765 -State Established).Count` must return 0.
8. Set `PHOENIX_STREAM_VALIDATOR=1` in `.env` permanently.

**Diagnostic that breaks the case in future recurrences:**
- Bridge health endpoint `nt8_last_heartbeat_age_s` matches TickStreamer's
  `HEARTBEAT_MS=3000` timer (~3s) — proves connection is TickStreamer not
  legacy V2 (which uses `HEARTBEAT_BARS=30`, silent on closed market).
- Win32 `EnumWindows` + filter on PID = NinjaTrader.exe with
  `IsWindowVisible=false` reveals hidden charts that GUI hunting can't
  find.
- `tools/diagnose_nt8_client.py` connects as a spy bot to `:8766`,
  captures fanout, identifies component by message field set.

**Status (2026-04-25 ~15:30 CDT):** RESOLVED via Jennifer's full cleanup.
Bridge confirms `nt8_status: disconnected`, 0 connections on `:8765`
with NT8 closed. Sunday 17:00 CT market open is the first real test —
expect exactly 1 TickStreamer connection from one chart.

**2026-04-25 ~16:10 CDT — bridge-side enforcement added (defense in depth):**
`bridge/bridge_server.py::handle_nt8_tcp` now rejects any 2nd+ concurrent
NT8 connection at the socket-accept layer when
`PHOENIX_BRIDGE_SINGLE_STREAM=1` (the default). First-writer-wins; the
2nd connection is closed immediately and a `bridge_alert.json` heartbeat
is emitted for watcher_agent. Recovery is automatic — when client #1's
TCP socket dies, the existing `finally:` block resets `nt8_connected =
False` and the next incoming connection succeeds. Set
`PHOENIX_BRIDGE_SINGLE_STREAM=0` to disable for multi-stream A/B testing.

Verified via:
- `tests/test_bridge_single_stream.py` (3 tests: rejection, opt-out,
  recovery-after-disconnect — all green)
- Live test against running bridge: client #2 got EOF immediately

This means even if NT8's workspace silently spawns multiple TickStreamer
instances again, only the FIRST one connects. The duplicate streams
that drove today's incident are now structurally impossible regardless
of NT8-side state.

### Phantom $40K trades (price-scale bug) — RESOLVED 2026-04-25 (morning)

Built `PriceSanity` guard to catch corrupt 7,150 prices. Pre-OIF price
sanity layer + FMP cross-check now intercept these before they reach
NT8's ATI.

### Spring_setup halt — RESOLVED 2026-04-25 (Sprint 2)

Strategy retired per §4 fixes. No longer loads, no longer halts.

### ANTHROPIC_API_KEY empty — RESOLVED 2026-04-21

Resolved via commit `eac5ae4` (`load_dotenv override=True`). Key was
never missing — 108-char value on line 19 of `.env`. Root cause: host
OS had `ANTHROPIC_API_KEY=""` set by Claude Code's OAuth shim;
`load_dotenv()` default behavior skips any key already present in
`os.environ` even if empty. Fix: `override=True` across all
`load_dotenv` call sites. Direct Claude test post-fix returned "OK".

### NT8 indicator install path discrepancy — RESOLVED 2026-04-18

NT8 data folder migrated out of OneDrive. Active install is now at
`C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\`.
CLAUDE.md updated.

### Bias_momentum hotfix — VERIFIED 2026-04-17

The hotfix (adding `price = market.get("close", 0.0)` and
`vwap = market.get("vwap", 0.0)` near line 66) is solid. Then
re-validated 2026-04-25 with SHORT mirror + VCR=1.2 lock-in tests.

### Lab bot 18% win rate reality — RESOLVED via decommission 2026-04-21

Lab bot decommissioned 2026-04-21 15:38 CDT. `bots/lab_bot.py`
preserved on disk as rollback safety net only. The 18% WR question is
moot.

### TradingView webhook ingestion — STRICKEN 2026-04-25

Premium $59.95/mo not approved. §3.1 stricken from active roadmap.
Existing HMAC-SHA256 scaffolding retained but not imported anywhere.
