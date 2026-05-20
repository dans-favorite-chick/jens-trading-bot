# pandas 3.0 datetime precision audit

**Date:** 2026-05-19
**Branch:** weekly-evolution/2026-05-17
**Trigger:** Two Phase 13 spawn agents (Sprint A `3a62d23`, Sprint B `c92b931`)
independently hit a wrong-by-1000x epoch conversion bug in pandas 3.0.

## The bug

pandas 3.0 changed the default datetime precision from nanoseconds to
**microseconds**. The classic idiom

```python
df["ts"].astype("int64") // 10**9
```

assumes ns precision and returns values 1000x too small under pandas 3.0.

Empirical reproduction (this machine, pandas 3.0.2):

```text
ts dtype:          datetime64[us, UTC]   (NOT [ns, UTC])
ts.astype(int64):  1_768_487_400_000_000 (microseconds, not nanoseconds)
// 1_000_000_000:  1_768_487           (microseconds // 1e9 = wrong)
correct ns value:  1_768_487_400_000_000_000
correct epoch sec: 1_768_487_400
```

The bug is silent — no exception, no warning. Downstream code that
treats the result as epoch seconds will compute timedeltas of microseconds
and report holds as "0.0001 min" or similar nonsense.

## Safe vs unsafe idioms

| Idiom | Safe under pandas 3.0? | Notes |
|---|---|---|
| `df["ts"].astype("int64") // 10**9` | NO | bug |
| `df["ts"].astype("int64") // 1_000_000_000` | NO | same bug |
| `df["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9` | YES | explicit ns cast |
| `df["ts"].apply(lambda t: t.timestamp())` | YES | per-row, slower but bulletproof |
| `df["ts"].dt.tz_convert("UTC").astype("datetime64[ns, UTC]").astype("int64")` | YES | for tz-aware to ns |
| `np.datetime64(ts.tz_convert("UTC").tz_localize(None), "ns").astype("int64")` | YES | explicit `"ns"` unit |
| `ser.values.astype("datetime64[ns]").view("int64")` | YES | numpy cast pins ns |

## Methodology

1. ripgrep for every variant of the unsafe idiom across the repo
2. For each hit, read the surrounding context to determine whether the
   source dtype is pinned to ns before the `int64` cast
3. Empirical verification on the actual pandas version installed
   (3.0.2 on Windows 11, Python 3.14)
4. Classify by blast radius (production > backtest > analysis > script)
5. Patch HIGH/MEDIUM/CRITICAL inline; defer LOW to operator

## Findings table

| File | Line | Pattern | Pinned ns? | Severity | Action |
|---|---|---|---|---|---|
| `tools/phoenix_sr_confluence_analyzer.py` | 169 | `df["ts"].astype("int64") // 1_000_000_000` | NO | **HIGH** | **FIXED** — added `.astype("datetime64[ns, UTC]")` precast |
| `tools/phoenix_sr_veto_analyzer.py` | 153 | `bm["entry_ts"].apply(lambda t: t.timestamp())` | n/a (safe form) | SAFE | already fixed Sprint A (commit `3a62d23`) |
| `tools/phoenix_entry_retest_analyzer.py` | 173 | `.astype("datetime64[ns, UTC]").astype("int64")` | YES | SAFE | no action |
| `tools/phoenix_tick_entry_quality.py` | 127, 211 | `.astype("datetime64[ns, UTC]").astype("int64")` | YES | SAFE | no action |
| `tools/phoenix_tick_entry_quality.py` | 129, 168 | `.astype("int64")` on already-int64 column | YES (no-op) | SAFE | no action |
| `tools/phoenix_tick_entry_quality.py` | 149 | `pd.to_datetime(..., utc=True).astype("int64")` | NO | **MEDIUM** | **FIXED** — added explicit ns precast |
| `tools/phoenix_early_reversal_signals.py` | 151 | `df.index.values.astype("datetime64[ns]").view("int64")` | YES (numpy ns cast) | SAFE | no action |
| `tools/phoenix_early_reversal_signals.py` | 167-168 | `np.datetime64(..., "ns").astype("int64")` | YES (explicit "ns") | SAFE | no action |
| `tools/phoenix_tick_trail_verification.py` | 144 | `ticks["ts_event"].values.astype("datetime64[ns]").view("int64")` | YES | SAFE | no action |
| `tools/phoenix_tick_trail_verification.py` | 148-149 | `np.datetime64(..., "ns").astype("int64")` | YES | SAFE | no action |
| `tools/tbbo_cache_builder.py` | 264 | `.astype("datetime64[ns, UTC]").astype("int64")` | YES | SAFE | no action |
| `core/` (production bot path) | — | (no matches) | — | — | clean |
| `bots/` | — | (no matches) | — | — | clean |
| `bridge/` | — | (no matches) | — | — | clean |
| `strategies/` | — | (no matches) | — | — | clean |

### Why `phoenix_tick_entry_quality.py:149` qualifies as MEDIUM

The branch is hit only when a cached dataframe lacks a `ts_ns` column and
has to be rebuilt from a string/datetime `ts_recv`/`ts_event`. In that
case `pd.to_datetime(df[cand], utc=True)` returns `datetime64[us, UTC]`
under pandas 3.0, and `.astype("int64")` would yield microsecond counts.
Downstream, those `ts_ns` values would be 1000x too small, so the entire
fill-quality report would compute slippages against random ticks. Fix is
the same one-liner.

## Fixes applied

### 1. `tools/phoenix_sr_confluence_analyzer.py` (HIGH)

```diff
-    df["epoch"] = df["ts"].astype("int64") // 1_000_000_000
+    df["epoch"] = df["ts"].astype("datetime64[ns, UTC]").astype("int64") // 1_000_000_000
```

Comment added explaining the precision pin. The S/R CONFLUENCE analyzer
output (`backtest_results/phoenix_sr_confluence_*.csv`) is now trustworthy
against future re-runs.

### 2. `tools/phoenix_tick_entry_quality.py` (MEDIUM, defensive)

```diff
-                df["ts_ns"] = ts.astype("int64")
+                # pandas 3.0 returns datetime64[us, UTC] by default; force ns first
+                df["ts_ns"] = ts.astype("datetime64[ns, UTC]").astype("int64")
```

Defensive fix — the live invocation path goes through line 127 which is
already safe. This protects against future cached-file shapes that hit
the `_normalize_ticks_df` recovery branch.

## Regression test

`tests/test_pandas_30_datetime_precision.py` — fails fast under pandas
3.0+ if anyone reintroduces the unsafe idiom. Covers:

1. The buggy pattern returns the wrong order of magnitude
2. The safe pattern returns correct epoch seconds
3. The fixed confluence-analyzer loader returns plausible epoch values
   (between 1.5e9 and 2.5e9 — i.e. anywhere between 2017 and 2049)

## LOW-severity items deferred to operator

None. Every match in the repo is either fixed or already safe.

The audit also confirmed that no production code path (`bots/`, `core/`,
`bridge/`, `strategies/`) does pandas-to-epoch conversion at all — the
trade loop uses raw NT8 timestamps that arrive over WebSocket as ns
already. The bug is confined to backtest/analysis tools.

## Future-proofing recommendation

A pre-commit grep hook for the regex `astype\(.int64.\)\s*//\s*10\*\*9`
or `astype\(.int64.\)\s*//\s*1_?000_?000_?000` would catch reintroductions
at PR time. Not added in this pass — operator call whether the friction
is worth it given how few new tools touch pandas datetimes.

## Related commits

- Sprint A fix: `3a62d23` (phoenix_sr_veto_analyzer switched to .timestamp())
- Sprint B fix: `c92b931` (phoenix_sr_confluence — initial work, missed line 169)
- This audit fix: (this commit) — closes the SR CONFLUENCE gap and a
  defensive MEDIUM in tick_entry_quality
