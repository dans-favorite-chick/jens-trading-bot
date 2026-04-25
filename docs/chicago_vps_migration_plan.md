# Chicago VPS Migration Plan -- Phoenix Trading Bot

**Status:** PLAN ONLY. No live infrastructure has been touched. No production
hosts have been provisioned. This document drives the eventual cutover; the
current production execution path remains the dev PC at
`C:\Trading Project\phoenix_bot\` with NT8 connecting locally.

**Primary author:** Section 5 / 7-section parallel build, 2026-04-25.
**Owner-on-execute:** dans.favorite.chick@gmail.com.
**Suggested cutover window (next available Saturdays):** 2026-05-02 (preferred)
or 2026-05-09 (fallback if VPS provisioning slips).

---

## 0. TL;DR

- Move the production execution stack (`bots/prod_bot.py` + `bridge/bridge_server.py`
  + NT8 with TickStreamer indicator) to a **QuantVPS Chicago Pro** host.
- Keep the dev PC fully installed and offline-capable as a hot-spare/rollback.
- Connect dev PC <-> VPS via **Tailscale**, with the bridge ports
  `:8765` / `:8766` bound exclusively to the `tailscale0` interface IP
  (NEVER `0.0.0.0`).
- Cutover during a Saturday maintenance window. Pre-cutover: 3 sessions of
  shadow mode on Sim101.

The single most important caveat surfaced while writing this plan:
**MNQ / NQ matches in CME Group's Aurora, IL data center, NOT in
NJ-metro / NY4 / NY5.** Renting an NJ-region "low-latency" VPS for MNQ is
strictly worse than the dev PC -- every order packet round-trips ~1,200 km
extra. Aurora-adjacent providers (QuantVPS, Speedy Trading Servers,
CyberWurx) are the only sensible options.

---

## a. Provider selection rationale

### Recommendation: **QuantVPS -- Chicago Pro plan**

| Spec                  | Chicago Pro                                  |
| --------------------- | -------------------------------------------- |
| Price                 | ~$99/mo monthly, ~$70/mo on annual prepay    |
| vCPU                  | 6 cores                                      |
| RAM                   | 16 GB                                        |
| Storage               | NVMe SSD (size varies; >=150 GB on Pro tier)  |
| OS                    | Windows Server 2022 Standard                 |
| Network to CME Aurora | Sub-1 ms (cross-connect or short metro fiber)|
| Backups               | Daily snapshot included                      |

Why QuantVPS over the alternatives:

| Provider                  | Region            | Pros                                              | Cons / risk                                                |
| ------------------------- | ----------------- | ------------------------------------------------- | ---------------------------------------------------------- |
| **QuantVPS**              | Chicago / Aurora  | Trader-focused, NT8 prebuilt images, sub-1ms CME  | Shared hardware in some tiers (NT MachineID collision risk)|
| Speedy Trading Servers    | Chicago           | Long history with NT8 community, decent support   | Older hardware on some plans, pricing less transparent     |
| CyberWurx                 | Chicago metro     | Cheap, flexible specs                             | Generalist DC, no NT8/futures-specific support             |
| AWS Local Zone (Chicago)  | Chicago           | API-driven, can scale                             | Higher latency than Aurora-adjacent; per-hour billing adds up; Windows licensing extra |
| Generic NJ "low-latency"  | Secaucus / NY4    | Cheap, plentiful                                  | **Wrong venue.** MNQ/NQ matches in Aurora, not NY4. Strict regression. |

**CRITICAL CALLOUT -- DO NOT CO-LOCATE IN NJ:**
The CME Globex matching engine for the Nasdaq-100 complex (NQ, MNQ, etc.) lives
at `350 E Cermak Rd` in Chicago and the CME data center in Aurora, IL. NY4 /
Secaucus / Carteret host the *equities* and *Nasdaq cash* matching engines.
Renting a "low-latency NY4 trading VPS" for MNQ adds a Chicago <-> NJ round trip
to every order packet (~=14 ms RTT minimum, often 16-20 ms with ISP variance).
This is materially worse than running on the dev PC in our home office. Several
providers happily up-sell traders into the wrong region -- read the data center
city carefully.

### Hardware sizing rationale

Phoenix's hot path is the bridge -> tick aggregator -> strategy loop. Empirically
on the dev PC the steady-state load is:

- 1 core pinned ~30% by `bridge_server.py` during NQ open.
- 1 core ~20% by `prod_bot.py`.
- NT8 itself uses 1.5-2 cores during open with TickStreamer + DOM enabled.
- Dashboard (`dashboard/server.py`) is negligible.

6 cores / 16 GB on QuantVPS Pro leaves comfortable headroom for the AI agents
(`agents/`), ChromaDB queries, and any sim ladder we run alongside. The 4-core
"Lite" tier is too tight once NT8 + bots + agents are co-resident.

---

## b. NinjaTrader licensing

NinjaTrader 8 personal license terms (as of NT8 license agreement, current
generation):

- The license key activates on multiple machines, but **only one instance may
  be running at a time** (non-concurrent use).
- The activation is keyed to the email + license key, with a per-machine
  MachineID hash.

### Plan

1. **Production runs on the VPS.** NT8 on the VPS has the license activated
   and runs continuously during market hours.
2. **Dev PC NT8 stays installed but idle** during prod sessions. It can be
   launched outside RTH for chart work, indicator development, and as a
   warm-spare for rollback.
3. **Brokerage account session is the bigger constraint.** The funded /
   sim account login itself permits one active session at a time. If we
   want NT8 dev sessions on the dev PC concurrent with VPS production:
   - Open a ticket with NT brokerage support requesting an additional
     *trading login* (sub-account login) -- typical turnaround 1-2 business
     days, free for paper/sim subaccounts.
   - Alternative: use "Connection: Playback" on the dev PC for replay /
     backtesting work, which does not consume a brokerage session.

### MachineID collision contingency

QuantVPS hosts may use shared hypervisor hardware. The NT8 MachineID hash is
derived from MAC address + disk serial + Windows install ID. Two scenarios to
plan for:

- **Activation refused on first boot.** Open a NinjaTrader support ticket
  citing "VPS migration, MachineID collision suspected." NT support
  routinely re-issues activation tokens for this case; turnaround ~24 h.
- **Activation succeeds, then revokes mid-session** (rare, hypervisor
  reshuffle). Mitigation:
  - Pre-stage the NT8 installer + license email on the VPS desktop.
  - Have the rollback path (Section f) ready -- flip the env var, dev PC
    takes over, fix license at leisure.

### Pre-cutover checklist (license)

- [ ] Confirm current NT8 license is on the *Multi-Broker* tier (required for
      Sim101 + funded account on same install).
- [ ] On the VPS, install NT8 from official installer (do NOT clone the dev
      PC's `Documents\NinjaTrader 8\` tree blindly -- license/state in
      `bin\Custom\` differs).
- [ ] Activate license on VPS during a Saturday window so a failure doesn't
      block Monday open.
- [ ] Save license email + machine activation receipt to password manager
      tagged `phoenix-vps`.

---

## c. Tunnel design

### Recommendation: **Tailscale** (preferred), WireGuard fallback

Tailscale gives us:
- NAT-traversal without port forwarding on the dev PC.
- ACLs as JSON, version-controlled.
- MagicDNS -- refer to hosts as `phoenix-vps`, `phoenix-dev` rather than IPs.
- Per-device key rotation and easy revocation.

The userspace WireGuard implementation has measurable overhead vs. kernel
WireGuard, but on a 16 GB / 6-core box pushing < 1 Mbps of bridge traffic,
it is well below the noise floor. **We will benchmark this in shadow mode
(Section e) -- if median round-trip latency over the tunnel exceeds 5 ms, we
fall back to native WireGuard.**

### Critical interface binding rule

The bridge listens on TCP `:8765` (NT8 -> bridge) and `:8766` (bots -> bridge).
**Never bind these to `0.0.0.0`** on the VPS. Bind only to the
`tailscale0` interface IP, otherwise the ports are reachable from the
public internet (Windows Firewall is a last line of defense, not the first).

In `config/settings.py` we currently have implicit `0.0.0.0` binding
inherited from `websockets.serve(...)` defaults. The migration adds:

```python
# config/settings.py -- VPS overlay
import os
import socket

def _tailscale_ip() -> str:
    """Return the IPv4 of the tailscale0 interface, or raise."""
    # Tailscale on Windows exposes the IP via `tailscale ip -4`; we pin it.
    ip = os.environ.get("PHOENIX_TAILSCALE_IP")
    if not ip:
        raise RuntimeError(
            "PHOENIX_TAILSCALE_IP env var not set. Refusing to bind bridge "
            "to 0.0.0.0 on a public VPS."
        )
    # Sanity: Tailscale CGNAT range is 100.64.0.0/10
    first = int(ip.split(".")[0])
    second = int(ip.split(".")[1])
    if not (first == 100 and 64 <= second <= 127):
        raise RuntimeError(f"PHOENIX_TAILSCALE_IP={ip!r} not in 100.64.0.0/10")
    return ip

BRIDGE_BIND_HOST = _tailscale_ip() if os.environ.get("PHOENIX_PROD_HOST") else "127.0.0.1"
BRIDGE_NT8_PORT  = 8765
BRIDGE_BOT_PORT  = 8766
```

And in `bridge/bridge_server.py` the `websockets.serve(...)` call passes
`host=BRIDGE_BIND_HOST` explicitly. Same change in any bot that exposes a
listener.

> NOTE: NT8 itself runs *on* the VPS, so the NT8 -> bridge link is loopback --
> binding `:8765` to `tailscale0` is technically over-restrictive. We use
> `127.0.0.1` for `:8765` and `tailscale0` for `:8766` (the bot-side port,
> which the dev PC dashboard would consume during diagnostics).

### Tailscale ACL snippet

ACL stored in Tailscale admin console (also committed under
`docs/tailscale_acl.json` post-cutover, gitignored if it contains tags
that leak account info):

```json
{
  "tagOwners": {
    "tag:phoenix-prod": ["dans.favorite.chick@gmail.com"],
    "tag:phoenix-dev":  ["dans.favorite.chick@gmail.com"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:phoenix-dev"],
      "dst": ["tag:phoenix-prod:8766,8767,3389"]
    },
    {
      "action": "accept",
      "src": ["tag:phoenix-prod"],
      "dst": ["tag:phoenix-dev:*"]
    }
  ],
  "ssh": [
    {
      "action": "check",
      "src": ["autogroup:member"],
      "dst": ["tag:phoenix-prod"],
      "users": ["administrator"]
    }
  ]
}
```

Notes:
- Only the dev PC and a single backup laptop will ever wear `tag:phoenix-dev`.
- `:3389` is RDP into the VPS; gated behind Tailscale, never exposed publicly.
- `:8766` is the bot fanout port (dashboard consumption from dev PC).
- `:8767` is the bridge health HTTP endpoint, useful for the watcher.
- The bridge's `:8765` is NOT in the ACL -- it's loopback-only inside the VPS.

### WireGuard fallback (if Tailscale benchmarks poorly)

Same binding rule applies: bind to the `wg0` interface IP, never `0.0.0.0`.
Static config, single peer, key rotation manual every 90 days. Tailscale
buys us ~80% of the operational sanity for ~5% of the latency tax -- only
fall back if measurements demand it.

---

## d. Data continuity checklist

Source of truth on dev PC; destination on VPS. All paths assume the
project lives at `C:\Trading Project\phoenix_bot\` on both machines.

### d.1 Trade history

Phoenix currently writes `logs/trade_memory.json` as a *JSON array*, not
strict JSONL (one record per line). The verifier tool we ship in
`tools/verify_jsonl_continuity.py` handles JSONL, which is the format we
should migrate toward (append-only, line-addressable, no rewrite of the
whole file on each commit). For the migration window we treat
`logs/trade_memory.json` as a single blob and verify by total record count
and last-trade timestamp.

> ACTION: post-migration, plan a follow-up to rotate `trade_memory.json`
> to true JSONL. Tracked separately; not in scope for this cutover.

Sync command (from dev PC, with VPS mounted as `\\phoenix-vps\Phoenix`
over Tailscale-routed SMB or accessed via robocopy + UNC):

```cmd
robocopy "C:\Trading Project\phoenix_bot\logs" ^
         "\\phoenix-vps\Phoenix\logs" ^
         /MIR /Z /R:3 /W:5 ^
         /XF "*.log" "bridge_stderr.log" "bridge_stdout.log" ^
         /LOG:"C:\Trading Project\phoenix_bot\logs\migration\robo_logs.log"
```

Then verify:

```cmd
python tools/verify_jsonl_continuity.py ^
  --source      "C:\Trading Project\phoenix_bot\logs\trade_memory.json" ^
  --destination "\\phoenix-vps\Phoenix\logs\trade_memory.json" ^
  --out-json    "logs\migration\trade_memory_continuity.json"
```

Pass criteria: row count match, last `ts` match (or both null), MD5 match
on the last 1000 rows.

### d.2 ChromaDB vector stores

Two collections live at:

- `data/knowledge_vectors/` (sqlite3 + at least one HNSW UUID dir)
- `data/trade_vectors/`     (sqlite3 + HNSW UUID dir)

These are **NOT safe to robocopy live**. The HNSW per-collection segment
files are mmap'd by the running process; copying them while the bot is
running can yield torn reads. Procedure:

1. **Stop both bots and the dashboard** on the dev PC. Confirm via
   `tasklist | findstr python`.
2. **Stop the bridge** as well, so nothing is appending to journals.
3. Copy the entire persist directory:
   ```cmd
   robocopy "C:\Trading Project\phoenix_bot\data\knowledge_vectors" ^
            "\\phoenix-vps\Phoenix\data\knowledge_vectors" ^
            /MIR /Z /COPY:DAT
   robocopy "C:\Trading Project\phoenix_bot\data\trade_vectors" ^
            "\\phoenix-vps\Phoenix\data\trade_vectors" ^
            /MIR /Z /COPY:DAT
   ```
4. On the VPS, smoke-test before allowing the bot to start:
   ```python
   import chromadb
   client = chromadb.PersistentClient(path=r"C:\Trading Project\phoenix_bot\data\knowledge_vectors")
   for coll in client.list_collections():
       print(coll.name, coll.get(limit=5))
   client = chromadb.PersistentClient(path=r"C:\Trading Project\phoenix_bot\data\trade_vectors")
   for coll in client.list_collections():
       print(coll.name, coll.get(limit=5))
   ```
   Both must return without exception and show non-empty `ids` lists.

### d.3 NT8 workspace

NT8 stores everything under
`C:\Users\Trading PC\Documents\NinjaTrader 8\` on the dev PC. The clean
migration path is:

1. On dev PC: NT8 -> Tools -> Backup -> save `phoenix_pre_vps.zip` to a
   shared location.
2. On VPS: install NT8 fresh, then NT8 -> Tools -> Restore -> point at
   `phoenix_pre_vps.zip`.
3. Re-enter:
   - License key (separate machine activation; see Section b).
   - Brokerage credentials (do NOT trust the backup to carry them).
   - Confirm rollover: front-month MNQ contract code (`MNQM6` -> next
     `MNQU6` etc., depending on cutover date).
4. Re-add data feed connections; verify Connection: Sim101 connects.
5. Load TickStreamer indicator on the MNQ chart and confirm it logs
   `[TickStreamer] connected ws://127.0.0.1:8765` to the NT8 output window.

### d.4 OIF folders

The OIF (Order Instruction File) handshake folders are
**ephemeral** -- files in them represent in-flight or recently-processed
orders. They MUST NOT be migrated.

On the VPS, manually create empty directories:

```cmd
mkdir "C:\Users\Administrator\Documents\NinjaTrader 8\outgoing"
mkdir "C:\Users\Administrator\Documents\NinjaTrader 8\outgoing\processed"
mkdir "C:\Users\Administrator\Documents\NinjaTrader 8\incoming"
```

(Adjust the user path if the VPS account name is not `Administrator`. Update
`NT8_DATA_ROOT` in `config/settings.py` accordingly.)

Migrating stale `outgoing/*.txt` files would cause the indicator to reissue
already-filled orders on first NT8 startup -- a **catastrophic** failure
mode. Do not do this.

### d.5 Other data to migrate

- `data/*.json` aggregator state files (sim/lab/prod). Same /MIR copy as
  trade history, with bots stopped.
- `memory/audit_log.jsonl` and `memory/context/*.md` -- SessionEnd hook
  drives these; copy once, then VPS sessions take over write authority.
- `config/strategies.py` and `config/settings.py` -- version-controlled,
  copy via git pull, NOT robocopy. The VPS clones the repo fresh.

### d.6 Do NOT migrate

- `__pycache__/` directories (regenerate on VPS).
- `logs/*.log` rolling logs (start fresh).
- `logs/trade_memory.json.bak-*` backup files (already redundant with the
  current file).
- Anything under `archive/`.

---

## e. Shadow-mode runbook

Before the cutover commits real orders to the funded account, run **at
least 3 full sessions on Sim101** with the VPS as the execution host and
the dev PC silent. Goal: prove parity, not just functionality.

### Pre-flight (once, before first shadow session)

1. VPS NT8 connected to Sim101, TickStreamer active on MNQM6 chart.
2. `bridge/bridge_server.py` started on VPS, bound per Section c.
3. `bots/sim_bot.py` started on VPS (the 24/7 sim across 16 sub-accounts);
   `bots/prod_bot.py` started on VPS pointed at Sim101.
4. Dev PC bots **stopped** and `PHOENIX_PROD_HOST=phoenix-vps` exported in
   the dev PC environment so dashboard reads from the VPS.
5. Dev PC NT8 disconnected.

### Per-session steps (8:00 AM CDT pre-open through 10:30 AM CDT)

1. 08:00 -- confirm bridge `:8767` health endpoint returns 200 from dev PC
   over Tailscale.
2. 08:15 -- confirm last 100 ticks on dashboard show < 5 s lag vs. NT8
   chart timestamp.
3. 08:30 -- RTH open. Let bots trade Sim101 normally. Do not intervene.
4. 10:30 -- close session, run grading harness P1-P6 (see
   `tests/test_roadmap_v4.py` and the explicit P-suite tests).
5. Capture metrics:
   - Median fill latency (signal -> OIF write -> NT8 fill). Target:
     **<= 50% of dev PC's baseline median**, which logs show is currently
     ~120-180 ms. So target ~60-90 ms median on VPS. Worse than dev PC
     means rollback.
   - Order error rate. Target: zero new error classes vs. dev PC's last
     10 sessions.
   - Bridge disconnect events. Target: zero unexpected disconnects.
6. Compare today's `logs/trade_memory.json` deltas against the same
   strategies' Sim101 historical distributions. If win rate or PnL/trade
   diverges by more than 1 sigma, hold cutover and investigate.

### Pass criteria (must hit ALL across 3 sessions)

- [ ] Median fill latency <= 50% of dev PC baseline.
- [ ] No new error classes in `logs/connection.log` or
      `logs/disconnect_forensics.jsonl`.
- [ ] Grading harness P1-P6 still passes (`pytest tests/ -k "P1 or P2 or P3 or P4 or P5 or P6"`).
- [ ] No Tailscale reconnects observed during 8:30-10:30 RTH window.
- [ ] ChromaDB read/write smoke from VPS bot succeeds at session start
      and end.

If any single session fails a criterion, restart the 3-session counter.

---

## f. Rollback plan

The dev PC remains a **fully provisioned hot spare**. Specifically:

1. Dev PC keeps NT8 installed, license activated (deactivate just before
   activating on VPS, reactivate if rolling back -- license key supports
   transfer with one support email if needed).
2. Dev PC keeps the Phoenix git checkout up to date -- pull every Friday
   evening even after VPS goes prod.
3. Dev PC keeps a snapshot of `data/knowledge_vectors/` and
   `data/trade_vectors/` taken at cutover-eve. ChromaDB writes during VPS
   prod run are NOT replicated back to dev PC by default -- accept this
   gap; the cost of missing a few days of vector embeddings is far less
   than the cost of bidirectional replication going wrong.

### Rollback trigger conditions

Initiate rollback if **any** of:

- Two consecutive trading sessions on VPS fail grading harness.
- Tailscale or WireGuard tunnel down for > 5 minutes during RTH with no
  clear remediation path.
- NT8 license activation revoked mid-session and not restored within
  60 minutes.
- VPS provider declares an incident affecting the host hypervisor.
- Median fill latency regresses to dev-PC parity or worse for two
  consecutive sessions.

### Rollback steps

1. On dev PC: `set PHOENIX_PROD_HOST=` (unset, falls back to localhost).
2. On dev PC: launch NT8, connect Sim101, reload TickStreamer.
3. On dev PC: `python bridge/bridge_server.py`, then
   `python bots/prod_bot.py`.
4. On VPS: stop bots, stop bridge, leave NT8 running on a flat account
   so we can inspect post-mortem state without rushed teardown.
5. Update `memory/context/CURRENT_STATE.md` with "VPS rollback executed,
   dev PC is prod". Notify (Telegram channel `phoenix-ops`).
6. Open postmortem ticket. Do not re-attempt VPS cutover until root cause
   is in writing.

---

## g. Timing -- Saturday 4-hour cutover window

> **CALLOUT -- TIME-BOX FOR THE ACTUAL MIGRATION**
>
> **Saturday weekend operation, 4 hour window, do NOT do during a trading
> session.** Globex MNQ resumes Sunday 17:00 CT, so the window MUST close
> with several hours of buffer before Sunday's reopen.
>
> Concrete recommendation as of today (2026-04-25): execute on
> **Saturday 2026-05-02, 09:00-13:00 CDT**. Slip to **2026-05-09** if VPS
> provisioning, license activation, or any pre-flight check is not
> green by Friday EOD 2026-05-01.

Run on a Saturday (markets fully closed; Globex MNQ resumes Sunday 17:00 CT).
Target window: **Saturday 09:00-13:00 CDT**. Do NOT do this between the
Sunday open and the Friday close under any circumstances.

| Step | Window      | Owner | Duration | Activity                                                                                |
| ---- | ----------- | ----- | -------- | --------------------------------------------------------------------------------------- |
| 1    | 09:00-09:15 | human | 15 min   | Take dev PC snapshot (Windows restore point + git status clean).                        |
| 2    | 09:15-09:30 | human | 15 min   | Stop dashboard, bots, bridge on dev PC. Confirm via `tasklist`.                         |
| 3    | 09:30-09:45 | human | 15 min   | Run NT8 -> Tools -> Backup -> save `phoenix_pre_vps.zip`.                                  |
| 4    | 09:45-10:15 | human | 30 min   | Provision QuantVPS Chicago Pro, RDP in, install Windows updates, install Tailscale.     |
| 5    | 10:15-10:30 | human | 15 min   | Add VPS to tailnet with tag `phoenix-prod`, push ACL from Section c.                    |
| 6    | 10:30-10:45 | human | 15 min   | Install NT8 on VPS, restore from backup, activate license, connect Sim101.              |
| 7    | 10:45-11:15 | human | 30 min   | `git clone` Phoenix repo onto VPS, `pip install -r requirements.txt`.                   |
| 8    | 11:15-11:35 | human | 20 min   | Robocopy logs/, data/ from dev PC to VPS over Tailscale per Section d.                  |
| 9    | 11:35-11:50 | human | 15 min   | Run `tools/verify_jsonl_continuity.py` against trade history. Smoke-test ChromaDB.      |
| 10   | 11:50-12:05 | human | 15 min   | Set env vars on VPS: `PHOENIX_PROD_HOST=1`, `PHOENIX_TAILSCALE_IP=...`. Validate binds.  |
| 11   | 12:05-12:25 | human | 20 min   | Start bridge + sim_bot on VPS, watch logs for 20 min, confirm tick stream healthy.      |
| 12   | 12:25-12:45 | human | 20 min   | From dev PC: connect dashboard to VPS via Tailscale, confirm all panels render.         |
| 13   | 12:45-13:00 | human | 15 min   | Run pytest suite on VPS (`pytest tests/ -x`). Confirm green.                            |

Slack: 0 minutes. If any step runs over, push the next session's shadow
test to the *following* Saturday -- never compress into Sunday-evening
open.

### Post-cutover (next 3 weekdays, Sim101 only)

- Day 1 (Mon): shadow session #1 per Section e. Dev PC silent.
- Day 2 (Tue): shadow session #2.
- Day 3 (Wed): shadow session #3. If all 3 pass criteria, schedule funded-
  account cutover for the *following* Saturday with same checklist
  applied to the brokerage credentials swap.

---

## Appendix A -- files touched by this plan (planning only, not executed)

| File                                              | Status                                  |
| ------------------------------------------------- | --------------------------------------- |
| `docs/chicago_vps_migration_plan.md`              | NEW (this file)                         |
| `tools/verify_jsonl_continuity.py`                | NEW (companion verifier tool)           |
| `tests/test_verify_jsonl_continuity.py`           | NEW (pytest coverage for the verifier)  |
| `config/settings.py`                              | TO BE MODIFIED at cutover (interface bind) |
| `bridge/bridge_server.py`                         | TO BE MODIFIED at cutover (host kwarg)  |

This planning section makes **zero** modifications to live infrastructure.
It deliberately stops short of even the `config/settings.py` change so the
PR can be reviewed and reverted without affecting any running bot.

## Appendix B -- open questions for cutover-day reviewer

1. Is the funded-account brokerage login cleared for VPS use under the
   current funded-trader agreement? (Some funded-account programs prohibit
   cloud VPS usage entirely.)
2. Should `memory/audit_log.jsonl` continue to be authoritative on dev PC
   post-migration, or does authority transfer to the VPS? Current
   recommendation: authority transfers, dev PC's copy becomes archival.
3. Do we want a once-daily reverse-sync of `data/*_vectors/` from VPS
   back to dev PC for rollback warmth? Decision deferred until after
   shadow sessions show ChromaDB write rate.
