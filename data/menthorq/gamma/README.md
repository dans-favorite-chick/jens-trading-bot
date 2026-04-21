# MenthorQ Gamma Data Directory

Jennifer's daily (and optional intraday) paste target for MenthorQ
gamma levels and blind spots. Feeds the B14 gamma integration:
regime classification, entry-wall filter, and natural stop discovery.

## File format

One line per file. Key/value pairs separated by commas, prefixed by
the contract symbol:

```
$NQM2026: Key1, Value1, Key2, Value2, ...
```

Values are numeric (floats preserved, e.g. `26500.77`). Key names
are case-insensitive with whitespace tolerated. Unknown keys are
logged and skipped — they do not fail the parse.

## Filename convention

```
YYYY-MM-DD_levels.txt   # Tier-1 + Tier-2 gamma levels
YYYY-MM-DD_blind.txt    # BL 1..10 blind spots
```

The date is the trading date (Central time) the data applies to.
The bot picks the most-recent file whose mtime is within
`MENTHORQ_MAX_DATA_AGE_HOURS` (default 30h).

## What to paste each morning

**Levels file** — Tier 1 fields are required for the gate to function;
Tier 2 fields improve precision but are optional:

| Tier | Field                    | Why                                        |
|------|--------------------------|--------------------------------------------|
| 1    | HVL                      | Monthly regime boundary                    |
| 1    | HVL 0DTE                 | Today's regime boundary (preferred)        |
| 1    | Call Resistance          | Upper wall for LONG entries                |
| 1    | Call Resistance 0DTE     | Today's upper wall (preferred)             |
| 1    | Put Support              | Lower wall for SHORT entries               |
| 1    | Put Support 0DTE         | Today's lower wall (preferred)             |
| 1    | Gamma Wall 0DTE          | Dominant 0DTE pin                          |
| 2    | 1D Min / 1D Max          | Session range walls                        |
| 2    | GEX 1..10                | Secondary gamma peaks                      |

**Blind spots file** — paste BL 1 through BL 10. These combine with
levels to surface HIGH-conviction clusters (multiple levels within
5 ticks of each other).

## Update cadence

- **Minimum:** once per morning before 08:30 CDT prod window opens.
- **Ideal:** repaste intraday when the MenthorQ dashboard refreshes
  0DTE values mid-session. The bot file-watches this directory and
  reloads within 60 seconds of any new file.

## Stale-data warning

If the most-recent file is older than **30 hours**, the bot logs a
WARN and treats gamma levels as unavailable (regime = `UNKNOWN`,
no gamma gating applied). Adjust via `MENTHORQ_MAX_DATA_AGE_HOURS`
in `config/settings.py`.

## Using the paste helper

Instead of hand-editing files, use the interactive CLI:

```bash
python tools/menthorq_paste_helper.py
```

It validates each paste before saving and reports the resulting
regime vs. current NQ price.

## Net GEX regime classification (B27)

**Net GEX is the primary regime signal.** MenthorQ's Net GEX magnitude
directly reflects dealer hedging direction — the ground-truth "am I in
a positive-gamma or negative-gamma day" question. HVL position tells
you *where price sits within the regime structure*, which is a
different (and correlated but weaker) signal.

### Classifier thresholds (`config/settings.py`)

| Regime             | Net GEX range               |
|--------------------|-----------------------------|
| `POSITIVE_STRONG`  | `> +3M`                     |
| `POSITIVE_NORMAL`  | `+500K … +3M`               |
| `NEUTRAL`          | `−500K … +500K`             |
| `NEGATIVE_NORMAL`  | `−3M … −500K`               |
| `NEGATIVE_STRONG`  | `< −3M`                     |

If Net GEX is **absent from the paste**, the classifier falls back to
the HVL proxy (price vs. `hvl_0dte || hvl` with an 8-tick transition
band) and emits a DEBUG log line `classifier using HVL proxy (no Net
GEX in paste)`. The HVL fallback can only produce `POSITIVE_NORMAL`,
`NEGATIVE_NORMAL`, or `NEUTRAL` — it cannot distinguish `STRONG` from
`NORMAL` magnitudes.

### How to include Net GEX in the daily paste

Append three key/value pairs to the end of the `_levels.txt` line:

```
, Net GEX, <value>, Total GEX, <value>, IV, <pct>
```

Values accept signed integers and K / M / B suffixes (case-
insensitive):

- `3920000`  → 3,920,000
- `+3.92M`   → 3,920,000
- `-4.5M`    → −4,500,000
- `500K`     → 500,000
- `2.5B`     → 2,500,000,000

### Example

Old line (regime via HVL proxy only):

```
$NQM2026: Call Resistance, 26500, Put Support, 25000, HVL, 25275, ..., GEX 10, 26200
```

New line (regime via Net GEX, authoritative):

```
$NQM2026: Call Resistance, 26500, Put Support, 25000, HVL, 25275, ..., GEX 10, 26200, Net GEX, 3920000, Total GEX, 9330000, IV, 19.42
```

Net GEX / Total GEX / IV are **enrichment only** — they do not affect
`is_complete`, which continues to require only the Tier 1 wall levels.
