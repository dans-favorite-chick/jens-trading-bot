# Jennifer's Morning MenthorQ Ritual

Every trading morning, **two** files must be refreshed before the bot
enters its primary session window (8:30 AM CST):

1. **Paste the MenthorQ levels text** into
   `data/menthorq/YYYY-MM-DD_levels.txt`
   (and, when applicable, `YYYY-MM-DD_blind.txt`).

2. **Update the regime JSON** at
   `data/menthorq_daily.json`.

**Both are required.** If only the levels paste is refreshed, the bot
will log a CRITICAL warning on startup noting the JSON is stale (>24h
old). Strategies fall back to "UNKNOWN" regime and the HVL direction
gate degrades to permissive-defaults mode — longs are not blocked below
HVL.

## Why two files

- The levels TXT feeds the `tools/menthorq_loader.py` pipeline which
  writes per-day gamma-level snapshots used by historical replay and
  the gamma proximity engine.
- The daily JSON carries the **regime interpretation** (GEX sign,
  DEX, vanna, charm, CTA bias, stop multiplier, strategy-type hint).
  MQBridge.cs does NOT provide any of that — it only scrapes price
  levels off the NT8 chart.

## Format for `data/menthorq_daily.json`

Only five text fields really need changing each morning. Keep the
`_instructions` block; it is ignored by the loader but is a helpful
crib sheet at 7:45 AM. Example (for a positive-gamma / mean-reversion
day):

```json
{
  "_instructions": [
    "PRICES come AUTOMATICALLY from MQBridge.cs in NT8 — do not enter them here.",
    "You only need to fill in the REGIME INTERPRETATION below — 4-5 text fields."
  ],
  "_last_updated": "2026-04-21",
  "date": "2026-04-21",

  "gex": {
    "net_gex_bn": 5.84,
    "regime": "POSITIVE",
    "total_gex_bn": 14.37,
    "put_call_gex": 0.42
  },
  "dex":   { "value": "POSITIVE" },
  "hvl":   { "price": 0 },
  "flows": {
    "vanna":           "NEUTRAL",
    "charm":           "NEUTRAL",
    "cta_positioning": "NEUTRAL"
  },
  "regime_summary": {
    "direction_bias":  "NEUTRAL",
    "allow_longs":     true,
    "allow_shorts":    true,
    "stop_multiplier": 1.0,
    "strategy_type":   "BALANCED",
    "notes": ""
  }
}
```

Field reference:

| Field                            | Values                                  |
|----------------------------------|------------------------------------------|
| `gex.regime`                     | `POSITIVE` / `NEGATIVE`                  |
| `gex.net_gex_bn`                 | float, in billions (e.g. `-2.1`, `5.84`) |
| `dex.value`                      | `POSITIVE` / `NEGATIVE`                  |
| `flows.vanna` / `flows.charm`    | `BULLISH` / `BEARISH` / `NEUTRAL`        |
| `flows.cta_positioning`          | `BUYING` / `SELLING` / `NEUTRAL`         |
| `regime_summary.direction_bias`  | `LONG` / `SHORT` / `NEUTRAL`             |
| `regime_summary.stop_multiplier` | `1.0` (normal), `1.5` (neg gamma), `0.8` (pos gamma) |
| `regime_summary.strategy_type`   | `MOMENTUM` / `MEAN_REVERSION` / `BALANCED` |

## Staleness monitor

On bot startup, `core/menthorq_feed.py::load()` checks the mtime of
`data/menthorq_daily.json`. If it is more than **24 hours** old, a
CRITICAL line fires:

```
[MenthorQ] menthorq_daily.json is 39.2h old — Jennifer's morning
ritual was SKIPPED. Regime fields will be stale. Update
data/menthorq_daily.json now. (Non-blocking.)
```

The bot continues to start — we never let a stale regime file take
the system down. But the CRITICAL line gets routed to Telegram by the
watchdog, so you will see it on your phone by 8:00 AM if the file
wasn't touched.

## Quick reference

- Levels paste: `data/menthorq/<today>_levels.txt`
- Regime JSON:  `data/menthorq_daily.json`
- Both by:       8:15 AM CST (latest)
- Staleness:     > 24h → CRITICAL log (non-blocking)
