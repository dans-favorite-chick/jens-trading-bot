# databento_tbbo cache directory

## Canonical tick cache (USE THIS)

**`mnq_ticks_clean.parquet`** + **`mnq_ticks_clean.metadata.json`**

Built by `tools/tbbo_cache_builder.py`. Single source of truth for MNQ tick
data over 2026-03-17..2026-05-15. Hygiene-filtered (spreads dropped,
dominant-symbol-per-day picked, intra-session jumps verified <500 ticks).

Future tools should load via:

```python
from tools.tbbo_cache_builder import load_clean_ticks
df = load_clean_ticks()  # or with symbol_filter / start / end
```

## Legacy caches (DEPRECATED — kept for back-compat)

These two parquets are regenerated from `mnq_ticks_clean.parquet` so they
have the SAME clean row set with their old schemas. Existing tooling that
references them by name will keep working but will consume clean data.

* `mnq_ticks.parquet`       — schema used by `tools/phoenix_tick_trail_verification.py`
                              (columns: `ts_event, price, size, side, bid_px_00, ask_px_00`,
                               float32 prices, no `symbol` column)
* `mnq_ticks_slim.parquet`  — schema used by `tools/phoenix_tick_entry_quality.py`
                              (columns: `ts_ns, price, bid, ask, side`)

**Do not write NEW tools against these.** Load `mnq_ticks_clean.parquet`
through `load_clean_ticks()` and you get the `symbol` column, UTC index,
and provenance for free.

## Other files

* `mnq_tbbo_2026-03-17_2026-05-17.dbn.zst` — raw Databento source, do not modify.
* `mnq_footprint_5m.csv`, `*_footprint_sparse.parquet` — derived footprint stats.

## How to rebuild after a fresh DBN download

```
python tools/tbbo_cache_builder.py --rebuild
```

That regenerates `mnq_ticks_clean.parquet` + metadata. Then if any legacy
tool still references the deprecated names, regenerate those too:

```
python -c "
import pandas as pd, sys
sys.path.insert(0, 'tools')
from tbbo_cache_builder import CLEAN_PARQUET, LEGACY_PARQUET_A, LEGACY_PARQUET_B
clean = pd.read_parquet(CLEAN_PARQUET)
a = clean.reset_index()[['ts_event','price','size','side','bid_px_00','ask_px_00']].copy()
for c in ('price','bid_px_00','ask_px_00'): a[c] = a[c].astype('float32')
a['size'] = a['size'].astype('uint32')
a.to_parquet(LEGACY_PARQUET_A, compression='snappy', index=False)
b = pd.DataFrame({
    'ts_ns': clean.index.astype('datetime64[ns, UTC]').astype('int64'),
    'price': clean['price'].astype('float64').values,
    'bid':   clean['bid_px_00'].astype('float64').values,
    'ask':   clean['ask_px_00'].astype('float64').values,
    'side':  clean['side'].astype('string').values,
})
b.to_parquet(LEGACY_PARQUET_B, compression='snappy', index=False)
"
```

## Hygiene gotchas (why this directory has a README)

The raw DBN was downloaded with `MNQ.FUT` continuous symbology
(`stype_in='parent'`). That pulls in:

1. **Multiple expirations** — MNQH6, MNQM6, MNQU6, MNQZ6, MNQH7, MNQM7 outrights.
2. **Calendar spread instruments** — e.g., `MNQH6-MNQM6` quoted at ~$215
   vs the ~$24,800 outright. Mixing these into a fill simulator produces
   $49K single-tick fake P&L.
3. **Rollover overlap** — front month switches around expiry. Naive
   chronological sorts will interleave H6 and M6 ticks.

The canonical builder defends against all three by (a) dropping any symbol
with a hyphen, (b) keeping only `^MNQ[HMUZ]\d+$` outrights, and
(c) picking the dominant-by-volume symbol per UTC date. See the docstring
in `tools/tbbo_cache_builder.py` for the gritty details.
