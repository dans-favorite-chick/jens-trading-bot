# Databento Footprint Walkthrough — Get Historical Order-Flow Data

**Goal:** Buy historical TBBO (Trades + Best Bid/Offer) data from Databento so we can backtest footprint-based strategies and validate the per-strategy footprint-confluence hypothesis (Phase 13 Plan Section R.5).

**Cost ballpark:** $100-300 for 1 year of MNQ TBBO. $500-1500 for 5 years. Cost-estimate FIRST (free), download only what's worth it.

---

## Prerequisites

You already have a Databento account (used it for the 5y OHLCV bars). You'll need:
- Your **API key** (from https://databento.com/portal/keys)
- Python 3.10+ with `pip` available
- ~5-50 GB free disk space depending on date range

---

## Step 1 — Install the Databento SDK (one-time)

```cmd
pip install databento
```

Verify it installed:
```cmd
pip show databento
```

Should show version 0.40+ or newer. If it says "package not found," your pip is pointing at a different Python — use `py -m pip install databento` instead.

---

## Step 2 — Set your API key (one-time)

Find your key:
1. Go to https://databento.com/portal/keys
2. Copy a key (or create a new one — Databento allows multiple)
3. It looks like `db-AbcDef1234567890...`

Set it persistently (Windows):
```cmd
setx DATABENTO_API_KEY "db-paste-your-key-here"
```

**IMPORTANT:** `setx` only affects NEW terminal windows. Close your current terminal and open a fresh one before continuing.

Verify in a NEW terminal:
```cmd
echo %DATABENTO_API_KEY%
```

Should print your key (not `%DATABENTO_API_KEY%`).

---

## Step 3 — Estimate the cost (FREE, no charge)

**ALWAYS estimate before downloading.** Databento bills based on data volume, and TBBO is much heavier than OHLCV.

### Recommended starting estimate — 1 year:

```cmd
python tools/databento_footprint_download.py estimate --start 2025-05-17 --end 2026-05-17
```

Output will look like:
```
====================================================================
DATABENTO COST ESTIMATE — tbbo for ['MNQ.FUT']
====================================================================
  Dataset:  GLBX.MDP3
  Schema:   tbbo
  Symbols:  ['MNQ.FUT'] (stype_in=parent)
  Start:    2025-05-17
  End:      2026-05-17

  ESTIMATED COST:  $XXX.XX
  Billable size:   X,XXX,XXX,XXX bytes (X.XX GB)
```

### Then estimate longer/shorter ranges:
```cmd
# Just 1 month (cheapest to start — about $10-30)
python tools/databento_footprint_download.py estimate --start 2026-04-17 --end 2026-05-17

# 5 years (matches your existing OHLCV range)
python tools/databento_footprint_download.py estimate --start 2021-05-17 --end 2026-05-17
```

The estimate API call is **FREE** — call it as many times as you want.

---

## Step 4 — Decide: how much data to buy?

Three reasonable paths:

### Path A: "Validate first" — $10-30, 1 month
Cheapest. Lets you confirm the data quality + validate that footprint-confluence actually lifts WR on Phoenix's strategies before committing more capital.

```cmd
python tools/databento_footprint_download.py estimate --start 2026-04-17 --end 2026-05-17
```

If the estimate looks reasonable, proceed to download for that range. Run the footprint-confluence hypothesis test on 1 month of data — should give 1,500-3,000 trade samples per strategy, enough to see if footprint helps.

### Path B: "Solid backtest" — $100-300, 1 year (RECOMMENDED)
Year-scale data is enough to span multiple regimes (bull/bear/chop), gives ~10-30K trade samples per strategy, and matches the timeframe of Phoenix's existing 2025 exit experiments.

```cmd
python tools/databento_footprint_download.py estimate --start 2025-05-17 --end 2026-05-17
```

This is what I'd pick if budget allows.

### Path C: "Full historical match" — $500-1500, 5 years
Matches your existing OHLCV range. Lets you re-run ALL the Phase 13 backtests with footprint confluence layered on. Most rigorous but expensive.

```cmd
python tools/databento_footprint_download.py estimate --start 2021-05-17 --end 2026-05-17
```

Only worth it if Path B (1 year) shows clear footprint edge AND you want maximal statistical confidence.

---

## Step 5 — Download (THIS COSTS MONEY)

Once you've chosen a range:

```cmd
python tools/databento_footprint_download.py download --start 2025-05-17 --end 2026-05-17
```

The script will:
1. Re-confirm the cost
2. Prompt: `Type 'yes' to proceed with download (charges your account):`
3. If yes — start the download
4. Save to `data/historical/databento_tbbo/mnq_tbbo_2025-05-17_2026-05-17.dbn.zst`

Download takes 10-30 minutes for 1 year. Don't interrupt it.

**Format:** Databento Binary (DBN) compressed with Zstandard. Native to Databento's SDK — the convert step parses it.

---

## Step 6 — Convert to per-bar footprint (FREE local processing)

```cmd
python tools/databento_footprint_download.py convert
```

This reads all `.dbn.zst` files in `data/historical/databento_tbbo/` and produces:

1. **`mnq_footprint_5m.csv`** — per-5min-bar summary with:
   - open, high, low, close, total_volume
   - buy_volume, sell_volume, delta
   - POC (price level with max volume in that bar)
   - n_trades

2. **`mnq_tbbo_*_footprint_sparse.parquet`** — per-price-level detail:
   - bar_5m, price, level_buy, level_sell, level_total, level_imbalance
   - One row per (bar, price level) — sparse format

Conversion takes 5-15 minutes for 1 year of data.

---

## Step 7 — Use it in Phoenix backtest (PHASE 14 work — next sprint)

Once data is downloaded + converted, the next-sprint work is:

1. Build `tools/phoenix_footprint_backtest_pipeline.py` — extends the existing pipeline to load per-bar footprint
2. Re-run the 5 HIGH-benefit strategies (Section R.5.2) WITH and WITHOUT footprint confluence
3. Compare WR/PF/$ deltas — empirically validate the +5-15pp WR hypothesis
4. If lift confirms, wire into production via the role-based confluence framework (Section K)

That work is documented in Phase 13 Section R.6 as Phase 14 scope.

---

## Troubleshooting

### "ERROR: DATABENTO_API_KEY environment variable not set"
- You set it in a previous terminal. Open a fresh terminal.
- Or check it's set: `echo %DATABENTO_API_KEY%` (Windows) / `echo $DATABENTO_API_KEY` (Unix)

### "ERROR: Cost estimate failed: 401 Unauthorized"
- Your API key is wrong or expired. Get a fresh one from https://databento.com/portal/keys
- Re-run `setx DATABENTO_API_KEY "db-new-key"` and open a new terminal.

### "ERROR: Cost estimate failed: dataset 'GLBX.MDP3' not subscribed"
- Your Databento plan doesn't include this dataset. Upgrade at https://databento.com/portal/plan
- Most plans include GLBX.MDP3 by default; if you have a custom plan check coverage.

### Date range coverage error
- Databento backfill goes back to around 2017 for CME products. Don't request before 2017-01-01.
- Future dates (haven't happened yet) will error.

### Download interrupted / partial file
- DBN files are atomic — partial downloads aren't usable. Delete the partial `.dbn.zst` and re-run download.
- For very large downloads (>10 GB), consider splitting into yearly chunks.

### "Out of disk space" on convert
- TBBO uncompressed is ~5x the .dbn.zst size. Free up space or run convert on a different drive.

---

## What this enables (the payoff)

Once you have footprint data:

| Strategy | What you can test |
|---|---|
| `spring_setup` | Was there ACTUAL absorption on the wick? (Wyckoff spring is footprint-defined) |
| `vwap_band_reversion` | Did the outer-band touch see real bid absorption? |
| `opening_session.orb` | Was the OR-break bar a real institutional break (stacked bid) or a sweep? |
| `raschke_baseline` | Did the EMA21 pullback see absorption? |
| `vwap_pullback_v2` | Did the VWAP bounce have positive delta? |
| `multi_day_breakout` | Did the 3-day break have stacked imbalance or just retail FOMO? |
| `footprint_cvd_reversal` (currently un-backtestable) | The strategy can finally be backtested |

Per the literature, footprint-confirmed entries lift WR by 5-15pp. Conservative estimate: **+5-15% portfolio uplift** on top of Phase 13 baseline (~+$1.6k/year). And it unlocks an entire class of order-flow strategies that aren't currently possible.

---

**Last updated:** 2026-05-18 — Phase 13 Section R companion doc.
