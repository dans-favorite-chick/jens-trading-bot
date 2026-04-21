# Archive — pre-`load_sigma_open_warmup` warmup pipeline

Deprecated warmup artifacts from pre-April-19, 2026. Superseded by
`tools/load_sigma_open_warmup.py` (paired with `data/sigma_open_table.json`
produced by `tools/warmup_sigma_open.py`). Kept as historical reference.

**Safe to delete after 2 weeks of stable operation of the new pipeline.**

## Contents

| File | Role in the old pipeline |
|---|---|
| `warmup_noise_area.py` | Loaded `logs/history/YYYY-MM-DD_{prod\|lab}.jsonl` Phoenix bar events and produced a `sigma_open_table`. |
| `backfill_noise_area.py` | Pulled 60 days of NQ=F 5m bars from yfinance and wrote them to `memory/noise_area_warmup.json`. |
| `noise_area_warmup.json` | Persisted 60-day yfinance backfill output (49 trading days, 79 minute-buckets). |

## Why superseded

The new pipeline uses **real MNQ 1m bars** (27 sessions, 390 minute-buckets
all populated with ≥10 samples) via `data/sigma_open_table.json`, which is
both higher granularity and more accurate to the instrument Phoenix actually
trades. The yfinance-backed backfill used NQ=F 5m bars as a percentage
proxy — serviceable but strictly inferior.

## Restoring

If the new pipeline ever fails catastrophically:
```bash
git mv archive/pre_load_sigma_open/warmup_noise_area.py   tools/
git mv archive/pre_load_sigma_open/backfill_noise_area.py tools/
mv     archive/pre_load_sigma_open/noise_area_warmup.json memory/
# Then restore the prior bot loader wire-up from git history (commit 5072cc0).
```
