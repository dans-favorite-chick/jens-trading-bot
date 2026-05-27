# Phoenix External Heartbeat Probe

External dead-man's switch for the Phoenix trading bot. Pings Phoenix's
health endpoint (`http://<trading-pc>:8767/health`) from **off** the Trading
PC and SMS-alerts the operator if the endpoint goes silent.

## Why this exists

All current Phoenix alerting (Telegram, Twilio SMS) **originates on the
Trading PC**. If the Trading PC drops network, loses power, or BSODs, the
alerts die with it — the bot can be silently flat for hours and the operator
will never know.

This was flagged as **F-22** in
[`docs/audits/SYNTHESIS_2026-05-24.md`](../../docs/audits/SYNTHESIS_2026-05-24.md)
("No external dead-man's switch; alerts originate from the trading PC")
and is the **P1-5** mitigation in the same doc.

The probe runs from an independent device, treats Phoenix as a remote
service, and alerts when the service stops answering.

## What it does

- Polls `PHOENIX_HEALTH_URL` every `PHOENIX_PROBE_INTERVAL_S` (default 60s).
- After `PHOENIX_PROBE_FAIL_THRESHOLD` consecutive failures (default 3 — so
  ~3 minutes at the default interval), fires a **DOWN** alert via Telegram
  AND Twilio SMS, then re-fires every subsequent failed probe with the
  duration-since-down updated.
- After `PHOENIX_PROBE_RECOVERY_THRESHOLD` consecutive successes (default 2)
  while in the DOWN state, fires a **RESOLVED** alert via Telegram **only**
  (no SMS — avoid alert fatigue).
- All probes log to stdout.

stdlib only — Python 3.10+. No `pip install`.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PHOENIX_HEALTH_URL` | `http://127.0.0.1:8767/health` | Endpoint to probe |
| `PHOENIX_PROBE_INTERVAL_S` | `60` | Seconds between probes |
| `PHOENIX_PROBE_FAIL_THRESHOLD` | `3` | Consecutive fails before DOWN |
| `PHOENIX_PROBE_RECOVERY_THRESHOLD` | `2` | Consecutive OKs to fire RESOLVED |
| `TELEGRAM_BOT_TOKEN` | *(none)* | Bot token; required for Telegram |
| `TELEGRAM_CHAT_ID` | *(none)* | Chat id; required for Telegram |
| `TWILIO_ACCOUNT_SID` | *(none)* | Twilio account SID; required for SMS |
| `TWILIO_AUTH_TOKEN` | *(none)* | Twilio auth token; required for SMS |
| `TWILIO_FROM` | *(none)* | E.164 sender; required for SMS |
| `TWILIO_TO` | *(none)* | E.164 operator number; required for SMS |

If Telegram OR Twilio creds are missing, that channel is skipped silently
(logged at INFO). If both are missing the probe still runs and logs alerts
to stdout — useful for testing.

**Creds are env-only. Never hardcode. Never commit a `.env` here.**

---

## Deployment Option A — second device on the LAN

E.g. a Raspberry Pi sitting on the same home network as the Trading PC.

1. Copy `probe.py` to the Pi (`scp` or git clone).
2. Set env vars in `~/.profile` or a systemd unit:

   ```bash
   export PHOENIX_HEALTH_URL="http://<trading-pc-lan-ip>:8767/health"
   export TELEGRAM_BOT_TOKEN="..."
   export TELEGRAM_CHAT_ID="..."
   export TWILIO_ACCOUNT_SID="..."
   export TWILIO_AUTH_TOKEN="..."
   export TWILIO_FROM="+1XXXXXXXXXX"
   export TWILIO_TO="+1YYYYYYYYYY"
   ```

3. Run under systemd so it auto-restarts:

   ```ini
   # /etc/systemd/system/phoenix-probe.service
   [Unit]
   Description=Phoenix External Heartbeat Probe
   After=network-online.target

   [Service]
   Type=simple
   EnvironmentFile=/etc/phoenix-probe.env
   ExecStart=/usr/bin/python3 /opt/phoenix-probe/probe.py
   Restart=always
   RestartSec=10
   StandardOutput=append:/var/log/phoenix-probe.log
   StandardError=append:/var/log/phoenix-probe.log

   [Install]
   WantedBy=multi-user.target
   ```

   ```
   sudo systemctl daemon-reload
   sudo systemctl enable --now phoenix-probe
   journalctl -u phoenix-probe -f
   ```

**Caveat:** this only catches Trading-PC-specific failures. If your whole
home loses power/internet, the Pi is also dead. Pair with Option B for
true off-site coverage.

---

## Deployment Option B — Cloudflare Worker (off-network)

A Worker runs in Cloudflare's edge, so it survives any single-site outage.
Workers can't reach `127.0.0.1:8767`, so we expose Phoenix's health
endpoint over **Tailscale** and let the Worker hit a Tailscale-reachable
URL (via a Tailscale-Funnel hostname, a `cloudflared` tunnel, or a tiny
reverse-proxy on a Tailscale node).

### Recommended: Tailscale + `cloudflared` tunnel

1. **Install Tailscale on the Trading PC**
   - Download from <https://tailscale.com/download/windows>
   - Sign in with the operator's account
   - Confirm the node appears in the admin console; note its Tailscale IP
     (e.g. `100.x.y.z`) and MagicDNS name (e.g. `trading-pc.tail-scale.ts.net`)

2. **Restrict :8767 to Tailscale**
   - Phoenix's bridge already binds `127.0.0.1` only. To expose to
     Tailscale: either run a tiny reverse proxy that binds to the Tailscale
     interface and forwards `:8767 -> 127.0.0.1:8767`, OR run
     `cloudflared` locally and have it forward.
   - Do **NOT** open `:8767` on the public internet or on the home router.

3. **Stand up a Cloudflare Tunnel**
   - `cloudflared tunnel login` (one-time)
   - `cloudflared tunnel create phoenix-health`
   - In the tunnel config, route hostname `phoenix-health.<your-zone>` to
     `http://127.0.0.1:8767`
   - `cloudflared tunnel run phoenix-health` (run as a Windows service)
   - This gives you `https://phoenix-health.<your-zone>/health` reachable
     from anywhere, with Cloudflare auth in front (use Cloudflare Access
     to limit who can hit it).

4. **Deploy the Worker** — equivalent of `probe.py` in JS:

   ```js
   // worker.js — runs every minute via a Cron Trigger
   export default {
     async scheduled(event, env, ctx) {
       const url = env.PHOENIX_HEALTH_URL;
       const state = JSON.parse((await env.KV.get("probe_state")) || "{}");
       state.consecutive_fail ??= 0;
       state.consecutive_ok ??= 0;
       state.down ??= false;
       let ok = false, detail = "";
       try {
         const r = await fetch(url, {
           headers: { "CF-Access-Client-Id": env.CF_ID, "CF-Access-Client-Secret": env.CF_SECRET },
         });
         ok = r.status === 200;
         detail = `HTTP ${r.status}`;
       } catch (e) { detail = `NetError ${e.message}`; }

       if (ok) {
         state.consecutive_fail = 0;
         state.consecutive_ok++;
         if (state.down && state.consecutive_ok >= 2) {
           await sendTelegram(env, `[PHOENIX RESOLVED] back up — ${detail}`);
           state.down = false; state.down_since = null;
         }
       } else {
         state.consecutive_ok = 0;
         state.consecutive_fail++;
         if (!state.down && state.consecutive_fail >= 3) {
           state.down = true; state.down_since = Date.now();
         }
         if (state.down) {
           const dur = Math.floor((Date.now() - state.down_since) / 1000);
           const msg = `[PHOENIX DOWN] ${state.consecutive_fail} misses (${dur}s) — ${detail}`;
           await sendTelegram(env, msg);
           await sendTwilio(env, msg);
         }
       }
       await env.KV.put("probe_state", JSON.stringify(state));
     }
   };

   async function sendTelegram(env, text) {
     await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
       method: "POST",
       headers: { "Content-Type": "application/x-www-form-urlencoded" },
       body: new URLSearchParams({ chat_id: env.TELEGRAM_CHAT_ID, text }),
     });
   }
   async function sendTwilio(env, body) {
     const auth = btoa(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`);
     await fetch(`https://api.twilio.com/2010-04-01/Accounts/${env.TWILIO_ACCOUNT_SID}/Messages.json`, {
       method: "POST",
       headers: { Authorization: `Basic ${auth}`, "Content-Type": "application/x-www-form-urlencoded" },
       body: new URLSearchParams({ From: env.TWILIO_FROM, To: env.TWILIO_TO, Body: body }),
     });
   }
   ```

   Bind a Workers KV namespace as `KV`. Add Secret env vars for all the
   `TELEGRAM_*`, `TWILIO_*`, `CF_*` values. Cron: `* * * * *`.

---

## Verification test (the one that matters)

1. Start the probe (Option A or B) and confirm a few green log lines.
2. **Pull the Trading PC's ethernet cable** (or run
   `Disable-NetAdapter Ethernet` from elevated PowerShell).
3. Wait ~4 minutes.
4. SMS **must** arrive on the operator's phone with text starting
   `[PHOENIX DOWN]`.
5. Reconnect the cable. Within 2 probe intervals, a Telegram-only
   `[PHOENIX RESOLVED]` should arrive.

If the SMS does not arrive, the dead-man's switch is broken. Treat it like
a P0 — the whole point of this script is that this test passes.

---

## Failure modes

- **Probe device itself offline.** Single-device probe = single point of
  failure. Mitigation: run the probe on TWO independent devices (e.g., Pi
  on LAN + Cloudflare Worker off-site). Both fire on the same outage —
  duplicate alerts are cheap, missed alerts are catastrophic.
- **Telegram / Twilio API down.** Each channel fails independently. As long
  as one channel reaches the operator, the alert lands. If both are out at
  once, the probe logs ERROR to stdout — a sibling supervisor of the probe
  itself should alert on probe stderr.
- **Health endpoint says 200 but bot is actually frozen.** Out of scope for
  this probe — that's what `WS-watchdog` (P1-6) handles. This probe only
  checks "is the HTTP listener answering."

---

## Local verification without exposing :8767 externally

To smoke-test on the Trading PC itself before standing up the off-site
deployment:

```powershell
# Terminal 1: ensure Phoenix bridge is running and :8767 responds
Invoke-WebRequest http://127.0.0.1:8767/health   # should 200

# Terminal 2: run the probe in foreground with a short interval
$env:PHOENIX_HEALTH_URL = "http://127.0.0.1:8767/health"
$env:PHOENIX_PROBE_INTERVAL_S = "5"
$env:PHOENIX_PROBE_FAIL_THRESHOLD = "3"
python "C:\Trading Project\phoenix_bot\tools\external_heartbeat\probe.py"

# Terminal 3: kill the bridge process to simulate Phoenix going dark
# After 3 * 5s = 15s, you should see an ALERT[DOWN] log line.
# Restart the bridge, wait 2 * 5s, you should see ALERT[RESOLVED].
```

Without Telegram/Twilio creds set, the probe will log the alert but skip
the actual SMS/Telegram send — perfect for dry-run.

## Tests

```
pytest tools/external_heartbeat/test_probe.py -v
```

All tests are pure-Python with urllib mocked — no real HTTP.
