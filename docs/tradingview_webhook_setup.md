# TradingView Webhook Setup (Phase B+ Section 3.1)

This document is the operator-facing runbook for the hardened TradingView
webhook receiver introduced in Phoenix Phase B+ Section 3.1.

The receiver lives at `bridge/tradingview_webhook.py` and is launched via
`tools/tradingview_webhook_runner.py`. Signals it accepts flow through the
same `OIFSink` Protocol (`phoenix_bot/orchestrator/oif_writer.py`) used by
the in-process strategies, so the Risk Gate fail-closed semantics apply
uniformly.

## 1. Generate the HMAC secret

The secret is shared between the upstream relay and the Phoenix receiver.
A 32-byte random hex string is sufficient:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output and paste it into `.env` as the value of
`TRADINGVIEW_WEBHOOK_SECRET`. Until that placeholder is replaced, the
receiver returns HTTP 503 on every request (fail-closed).

```
TRADINGVIEW_WEBHOOK_SECRET=<paste-the-64-hex-chars-here>
```

## 2. Strategy allowlist

The receiver rejects any signal whose `strategy` field is not in
`TRADINGVIEW_ALLOWED_STRATEGIES`. Default empty = reject everything.

```
TRADINGVIEW_ALLOWED_STRATEGIES=tv_breakout_v1,tv_pullback_v2
```

Add a strategy here only AFTER the corresponding Pine script has been
back-tested and reviewed.

## 3. IP allowlist

`TRADINGVIEW_ALLOWED_IPS` defaults to TradingView's published webhook
source IPs (`52.89.214.238`, `34.212.75.30`, `54.218.53.128`,
`52.32.178.7`) plus `127.0.0.1` for local probes. Re-check TradingView's
published list periodically; rotate this env var when they change.

## 4. TradingView alert message template

Pine alert configurations let you place a JSON message body in the
"Message" box. The receiver expects exactly these fields:

```json
{
  "strategy": "tv_breakout_v1",
  "action": "BUY",
  "qty": 1,
  "instrument": "MNQ 06-26",
  "price": {{close}},
  "ts": "{{timenow}}",
  "nonce": "{{strategy.order.id}}-{{time}}"
}
```

`action` is one of `BUY`, `SELL`, or `CLOSE`. `qty` is an integer >= 1.
`nonce` MUST be unique per signal — TradingView's `{{strategy.order.id}}`
combined with `{{time}}` gives 24-hour uniqueness within one alert.

## 5. The HMAC relay (TradingView cannot sign)

TradingView Pine alerts cannot compute SHA-256, so a thin relay is
required. The relay receives the TradingView webhook, computes the
HMAC-SHA256 of the raw body using the shared secret, attaches it as the
`X-Phoenix-Signature: sha256=<hex>` header, and forwards to the Phoenix
receiver.

Recommended relay implementations:

  - **Cloudflare Workers** — sub-50ms latency, free tier sufficient.
  - **AWS Lambda + API Gateway** — solid SLA, ~150ms cold-start.
  - **Self-hosted nginx + lua-resty-hmac** — fully owned, no vendor.

A minimal Cloudflare Worker outline:

```js
addEventListener("fetch", event => event.respondWith(handle(event.request)));

async function handle(req) {
  const body = await req.text();
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(SECRET),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  const hex = Array.from(new Uint8Array(sig))
    .map(b => b.toString(16).padStart(2, "0")).join("");
  return fetch(PHOENIX_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Phoenix-Signature": `sha256=${hex}`,
    },
    body,
  });
}
```

The same SECRET goes in the Worker's environment AND in Phoenix's
`.env::TRADINGVIEW_WEBHOOK_SECRET`. Rotate both together.

## 6. Local smoke test with curl

With the receiver running on `127.0.0.1:5050`:

```bash
SECRET=$(grep TRADINGVIEW_WEBHOOK_SECRET .env | cut -d= -f2)
BODY='{"strategy":"tv_breakout_v1","action":"BUY","qty":1,"instrument":"MNQ 06-26","price":18500.25,"ts":"2026-04-25T12:00:00Z","nonce":"test-001"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -X POST http://127.0.0.1:5050/webhook/tradingview \
  -H "Content-Type: application/json" \
  -H "X-Phoenix-Signature: sha256=$SIG" \
  --data "$BODY"
```

Expected response on success:

```json
{"ok": true, "decision": "ACCEPT", "oif_path": "...", "reason": null}
```

A second invocation with the same `nonce` returns HTTP 409
(replay-protected). Modifying any byte of the body without recomputing
the signature returns HTTP 401.

## 7. Run as a Windows scheduled task

Register the runner with Task Scheduler so it survives logoff /
auto-starts on boot. From an elevated PowerShell:

```powershell
$exe = "C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$arg = "C:\Trading Project\phoenix_bot\tools\tradingview_webhook_runner.py --port 5050"
$action = New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory "C:\Trading Project\phoenix_bot"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName "Phoenix-TV-Webhook" `
  -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited
```

To run interactively for debugging:

```bash
python tools/tradingview_webhook_runner.py --port 5050
```

The startup banner reports the bind address, configured strategy
allowlist, and whether the secret is configured. If the banner shows
`Secret: MISSING (fail-closed: every request 503)`, set the env var and
restart.

## 8. Operational checks

  - Rotating log: `logs/tradingview_webhook.log` (5MB x 5 backups).
  - Health probe: `GET http://127.0.0.1:5050/webhook/tradingview/health`
    returns `{"ok": true}` (does NOT reveal whether the secret is set).
  - Bind address: 127.0.0.1 by default. Do NOT change to 0.0.0.0;
    expose via Tailscale or a reverse-proxy port-forward instead.
  - Rate limit: 10 requests / minute per source IP. Bursts above that
    return 429.
  - Replay window: 24 hours. The in-memory cache holds up to 10,000
    nonces; oldest evicted first.
