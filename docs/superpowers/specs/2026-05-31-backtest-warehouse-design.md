# Phoenix Backtest Warehouse — Design Spec

| | |
|---|---|
| **Spec date** | 2026-05-31 |
| **Status** | Draft — pending operator review |
| **Author** | Brainstorm with operator (Claude assist) |
| **Implements** | Phase 1 of unified analytics DB (backtest results only) |
| **Implementation plan** | TBD — produced by `superpowers:writing-plans` after this spec is approved |
| **Canonical project root** | `C:\Trading Project\phoenix_bot\` |

---

## 1. Context

Phoenix bot has accumulated ~70 backtest result CSVs across two sources:

1. **Legacy `backtest_results/` (~55 CSVs)** — one-off backtests from prior sprints. 13-column schema (`strategy, direction, entry_ts, ..., pnl_dollars, pnl_ticks, hold_min, year`). Naming conventions vary. Most have no provenance metadata. **Verified 2026-05-31:** all sampled files emit `TIMESTAMPTZ` with explicit `+00:00` UTC suffix. P&L is gross (no friction) in the legacy set due to the historical B13 friction-bug era.
2. **`backtest_results/portfolio_framework/` (15 CSVs + `INVENTORY.md`)** — new portfolio backtest framework output. Documented in `INVENTORY.md` next to the CSVs. `macro_trades.csv` is the canonical extended-schema trade log (76,342 rows, friction-net, UTC `TIMESTAMPTZ`, with MAE/MFE/regime/tod_bucket). WFA windows / summary are first-class concepts.

Ad-hoc analysis across these CSVs is painful (one `pd.read_csv` per question, no joins across runs, no provenance). This spec lands a single-file DuckDB warehouse that ingests both eras into a unified queryable model, with content-hash provenance and an open extension point for novel summary metrics.

**Scope of this spec: backtest results only.** Two future subsystems get their own specs:
- Trade & signal log (live trades, OIF, exits — eventual freeze-lift dependency).
- Market data store (bars/ticks from `data/historical/` and the live NT8 feed).

Both will reuse `runs` as the provenance join point.

---

## 2. Decisions log

The choices made during brainstorming, captured here so future readers don't have to reconstruct them:

| Decision | Choice | Rationale |
|---|---|---|
| Build order | Backtest results first; trade-log and market-data later | Highest analytical leverage with zero impact on live bot. CSVs already exist; no engine changes needed for v1. |
| Run metadata capture | Sidecar JSON (`<csv-stem>.run.json`) | Decouples ingest from backtest engine. Future runs emit it; historical runs leave fields NULL. |
| Primary consumer | SQL-first (DuckDB CLI / notebook); Python helpers + dashboard later | Normalized schema makes both downstream layers easy. |
| Re-ingest policy | Content-hash dedup; never delete | Same hash → skip. Different hash → new `run_id`, both coexist. Defensible analytics warehouse. |
| Schema model | INVENTORY's `trades` / `wfa_windows` / `wfa_summary` + provenance layer (`runs`, `run_metrics`) | Honors existing work; adds re-ingest dedup; opens for future subsystems. |
| Timezone | **UTC canonical** with `trades_ct` view for CT consumers | Matches new framework, matches INVENTORY, exchange convention. Legacy CSVs already UTC (verified 2026-05-31). |
| Convenience tables (phase1/2/3, microstructure, multitier) | Ingest verbatim as `import_<name>`; replace with views later | Fastest path to usable DB; views come when SQL recomputation is trusted. |
| Lock mechanism | PID file at `data/warehouse/.ingest.lock` | Cross-platform (no `fcntl.flock` on Windows). Leaves a breadcrumb if a process dies mid-ingest. |

---

## 3. File layout & components

```
C:\Trading Project\phoenix_bot\
├── data\
│   └── warehouse\
│       ├── phoenix.duckdb              ← the database file (single-writer; gitignored)
│       ├── .ingest.lock                ← PID lock file, transient
│       └── ingest_errors.log           ← JSONL, append-only
├── tools\
│   └── warehouse\
│       ├── __init__.py
│       ├── schema.sql                  ← DDL: runs, trades, run_metrics, wfa_windows, wfa_summary, trades_ct view
│       ├── ingest.py                   ← library: ingest_csv, scan_dir
│       ├── sniff.py                    ← CSV-kind detection + WFA filename strategy sniff
│       ├── sidecar.py                  ← sidecar JSON load + hash
│       ├── lock.py                     ← PID lock file with stale-recovery
│       ├── cli.py                      ← `python -m tools.warehouse <subcommand>`
│       └── known_strategies.py         ← loads strategy keys from config/strategies.py
└── tests\
    └── warehouse\
        ├── fixtures\                   ← tiny CSV + sidecar pairs
        └── test_*.py
```

- `phoenix.duckdb` lives alongside `data/trade_memory.db`. Gitignored. Backups are filesystem-level (rsync / shadow copy).
- All warehouse code is namespaced under `tools/warehouse/` so it cannot drift into bot runtime imports.
- CLI is the only entrypoint a human uses; the library is importable for dashboards/scripts.
- DuckDB is single-writer; the PID lock makes that explicit.

---

## 4. Schema

`schema.sql` runs `INSTALL json; LOAD json;` before any DDL so the `JSON` column type, `json_extract()`, and `CAST(... AS JSON)` are available. The extension is bundled with DuckDB; `INSTALL` is a no-op after first run.

```sql
-- Bootstrap (run once per DB; idempotent).
INSTALL json;
LOAD json;

-- runs: provenance layer. One row per ingested CSV (identity = content hash).
CREATE TABLE runs (
    run_id           VARCHAR PRIMARY KEY,                     -- sha256(csv_bytes + canonical_sidecar)
    source_filename  VARCHAR NOT NULL,                        -- 'macro_trades.csv', '_det_5y_rest_run1.csv'
    csv_kind         VARCHAR NOT NULL,                        -- 'trades' | 'summary' | 'mixed' | 'wfa_windows' | 'wfa_summary' | 'derived'
    logical_group    VARCHAR,                                 -- e.g. 'phase13_wfa' so shard CSVs can be queried as one set
    strategy         VARCHAR,                                 -- single-strategy run; NULL for multi-strategy CSVs
    params           JSON,                                    -- from sidecar
    code_sha         VARCHAR,
    seed             INTEGER,
    lookback_start   TIMESTAMP WITH TIME ZONE,
    lookback_end     TIMESTAMP WITH TIME ZONE,
    friction_applied BOOLEAN,                                 -- see §10 Cross-run P&L caveat
    ingested_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    sidecar_raw      JSON                                     -- full sidecar JSON + ingester-attached meta
);

-- trades: canonical fact table. Superset of legacy 13-col + INVENTORY's macro_trades schema.
CREATE TABLE trades (
    run_id          VARCHAR NOT NULL REFERENCES runs(run_id),
    strategy        VARCHAR NOT NULL,
    direction       VARCHAR NOT NULL,                         -- normalized to upper on ingest ('LONG' | 'SHORT')
    entry_ts        TIMESTAMP WITH TIME ZONE NOT NULL,        -- UTC
    entry_price     DOUBLE NOT NULL,
    stop_price      DOUBLE,
    target_price    DOUBLE,
    exit_ts         TIMESTAMP WITH TIME ZONE,                 -- UTC
    exit_price      DOUBLE,
    exit_reason     VARCHAR,                                  -- 'target','stop','time_exit','no_data_after_entry','no_data_in_window'
    pnl_dollars     DOUBLE,                                   -- friction status governed by runs.friction_applied
    pnl_ticks       DOUBLE,                                   -- gross; friction never deducted from this
    hold_minutes    DOUBLE,
    year            INTEGER,
    -- Extended cols from INVENTORY; NULL for legacy CSVs.
    mae_ticks       DOUBLE,                                   -- Maximum Adverse Excursion
    mfe_ticks       DOUBLE,                                   -- Maximum Favorable Excursion
    regime          VARCHAR,                                  -- 'LOW_VOL_TREND','MEAN_REVERT_CHOP','HIGH_VOLATILITY','UNKNOWN' (no-look-ahead)
    tod_bucket      VARCHAR,                                  -- 'Opening Drive','Mid-Day Lull','Power Hour','Globex Overnight','Other RTH'
    entry_context   JSON                                      -- ATR/CVD/VWAP/DOM/confluences at signal time; populated once engine emits it
);
CREATE INDEX idx_trades_run    ON trades(run_id);
CREATE INDEX idx_trades_strat  ON trades(strategy, entry_ts);
CREATE INDEX idx_trades_year   ON trades(year);

-- trades_ct: consumer-facing CT view. session_date and market_open_minutes are computed on read.
-- Note: market_open_minutes returns NEGATIVE values for pre-market / Globex trades. That's correct
-- and useful. Session-hours-only queries should filter `WHERE market_open_minutes BETWEEN 0 AND 90`.
CREATE VIEW trades_ct AS
SELECT
    *,
    (entry_ts AT TIME ZONE 'America/Chicago')::DATE                                     AS session_date,
    EXTRACT(EPOCH FROM (
        (entry_ts AT TIME ZONE 'America/Chicago')
        - date_trunc('day', entry_ts AT TIME ZONE 'America/Chicago')
        - INTERVAL '8 hours 30 minutes'
    )) / 60.0                                                                           AS market_open_minutes,
    entry_ts AT TIME ZONE 'America/Chicago'                                             AS entry_ts_ct,
    exit_ts  AT TIME ZONE 'America/Chicago'                                             AS exit_ts_ct
FROM trades;

-- run_metrics: open key/value extension for arbitrary summary metrics.
CREATE TABLE run_metrics (
    run_id        VARCHAR NOT NULL REFERENCES runs(run_id),
    metric_name   VARCHAR NOT NULL,
    metric_value  DOUBLE,                                     -- numeric metrics
    label_value   VARCHAR,                                    -- non-numeric metrics (e.g. 'verdict' = 'pass')
    PRIMARY KEY (run_id, metric_name)
);

-- wfa_windows: first-class WFA fact table. INVENTORY schema verbatim + run_id.
CREATE TABLE wfa_windows (
    run_id       VARCHAR NOT NULL REFERENCES runs(run_id),
    strategy     VARCHAR NOT NULL,
    window_idx   INTEGER NOT NULL,
    is_start     DATE,
    is_end       DATE,
    oos_start    DATE,
    oos_end      DATE,
    best_params  JSON,                                        -- INVENTORY stores as TEXT; parsed to JSON on ingest
    is_pf        DOUBLE,
    is_trades    INTEGER,
    oos_pf       DOUBLE,
    oos_trades   INTEGER,
    oos_net      DOUBLE,
    wfe          DOUBLE,                                      -- oos_pf / is_pf
    degraded     BOOLEAN,
    PRIMARY KEY (run_id, strategy, window_idx)
);
CREATE INDEX idx_wfa_strat ON wfa_windows(strategy);

-- wfa_summary: INVENTORY schema verbatim + run_id.
CREATE TABLE wfa_summary (
    run_id               VARCHAR NOT NULL REFERENCES runs(run_id),
    strategy             VARCHAR NOT NULL,
    n_windows            INTEGER,
    mean_is_pf           DOUBLE,
    mean_oos_pf          DOUBLE,
    median_oos_pf        DOUBLE,
    pct_windows_degraded DOUBLE,
    robust               BOOLEAN,
    PRIMARY KEY (run_id, strategy)
);

-- Convenience tables: created lazily by the ingester when it first encounters a matching CSV
-- (e.g. import_phase1_strategy_summary, import_microstructure_lift, import_phase3_multitier_comparison).
-- Schema = read_csv_auto inference + run_id column. v2 replaces these with views over `trades`.
```

**Notes:**
- `run_id` is full 64-char SHA-256 hex; eliminates any collision concern.
- `friction_applied` lives on `runs`, not on `trades` (per-trade duplication adds nothing). See §10.
- `best_params` is parsed from TEXT to JSON on ingest so `json_extract()` works directly. Parse failure path documented in §7.
- No `ON DELETE CASCADE` — runs are never deleted (content-hash policy).
- `direction` is normalized to uppercase on ingest. Legacy CSVs may emit `long`/`short`; INVENTORY emits `LONG`/`SHORT`. The DB sees `LONG`/`SHORT` only.

---

## 5. Ingester component

**Two entry points, one engine:**

```python
# tools/warehouse/ingest.py

def ingest_csv(csv_path: Path, *, db_path: Path, mark_friction_applied: bool | None = None) -> IngestResult: ...
def scan_dir(dir_path: Path, *, db_path: Path, glob: str = "*.csv", recursive: bool = False) -> list[IngestResult]: ...

@dataclass
class IngestResult:
    csv_path: Path
    run_id: str | None
    status: Literal["inserted", "skipped_duplicate", "error"]
    csv_kind: str | None
    rows_inserted: int
    metrics_inserted: int
    error: str | None
```

**CLI:**
```
python -m tools.warehouse ingest <path>            # file or dir
python -m tools.warehouse ingest <path> --recursive
python -m tools.warehouse ingest <path> --logical-group phase13_wfa
python -m tools.warehouse ingest <path> --mark-friction-applied
python -m tools.warehouse ingest <path> --dry-run  # report what would happen; no writes
python -m tools.warehouse status                   # row counts per table, last ingest timestamp
```

### 5.1 Per-file pipeline (atomic per file)

1. **Hash.** Read CSV bytes + sidecar JSON (canonical form: `json.dumps(sorted_keys=True, separators=(',',':'))`). `run_id = sha256(csv_bytes + b"\n" + canonical_sidecar)`. No sidecar → hash CSV only; note `sidecar_raw.meta.sidecar_missing = true`.
2. **Dedup check.** `SELECT 1 FROM runs WHERE run_id = ?`. Hit → `status='skipped_duplicate'`, return.
3. **Kind sniff** (see §5.2).
4. **Begin transaction.**
5. **Insert into `runs`** with strategy/params/SHA/seed/lookback/friction from sidecar (NULL if no sidecar).
6. **Insert payload** by kind (see §5.3).
7. **Commit.** Return `IngestResult`.
8. **On any exception inside steps 4–7** → rollback, append JSONL error to `data/warehouse/ingest_errors.log`, return `status='error'`. Other files in a batch continue.

### 5.2 CSV-kind sniffer

Header-only sniff in `tools/warehouse/sniff.py`. Positive rules tried in order:

| Kind | Signature |
|---|---|
| `trades` | Has `entry_ts` AND `entry_price` AND `pnl_dollars`. May also have extended cols. |
| `wfa_windows` | Has `window_idx` AND `oos_pf` AND `is_pf`. |
| `wfa_summary` | Has `strategy` AND `mean_oos_pf` AND `pct_windows_degraded`. |
| `summary` | First column is `strategy` or `name`; remaining columns are numeric metric names; no `entry_ts`. |
| `mixed` | Has trade signature (`entry_ts` OR `entry_price`) AND at least one aggregate-metric column (`profit_factor`, `sharpe`, `win_rate`, `max_dd`, `n_trades`). Aggregate values must be constant across rows of the file; otherwise `error`. |
| `derived` | Filename matches a known convenience pattern (`phase1_*.csv`, `microstructure_lift.csv`, `phase3_*.csv`) AND none of the above matched. |
| `error` | Matched none of the above. Log full header to `ingest_errors.log`. |

### 5.3 Per-kind ingest SQL

All trade ingestion uses server-side `read_csv_auto` to keep Python row-loops out of the hot path.

**`trades` kind:**

The ingester reads the CSV header first (already done during sniff in §5.2) and builds the SELECT list dynamically in Python, substituting `NULL` literals for columns absent from the source. `read_csv_auto`'s `union_by_name=true` does NOT solve this for a single file — it only applies when reading multiple files with a glob. The dynamic-SELECT approach handles both legacy and extended schemas uniformly:

```python
# Pseudo-code; one-file path
SOURCE_COLS = {
    "strategy", "direction", "entry_ts", "entry_price", "stop_price", "target_price",
    "exit_ts", "exit_price", "exit_reason", "pnl_dollars", "pnl_ticks",
    "hold_min", "hold_minutes", "year",
    "mae_ticks", "mfe_ticks", "regime", "tod_bucket", "entry_context",
}
present = set(csv_header) & SOURCE_COLS

def col_or_null(name: str, *, fallback: str | None = None) -> str:
    if name in present: return name
    if fallback and fallback in present: return fallback
    return "NULL"

sql = f"""
INSERT INTO trades SELECT
    '{run_id}'           AS run_id,
    strategy,
    upper(direction)     AS direction,
    entry_ts, entry_price,
    {col_or_null("stop_price")}     AS stop_price,
    {col_or_null("target_price")}   AS target_price,
    {col_or_null("exit_ts")}        AS exit_ts,
    {col_or_null("exit_price")}     AS exit_price,
    {col_or_null("exit_reason")}    AS exit_reason,
    {col_or_null("pnl_dollars")}    AS pnl_dollars,
    {col_or_null("pnl_ticks")}      AS pnl_ticks,
    {col_or_null("hold_minutes", fallback="hold_min")} AS hold_minutes,
    {col_or_null("year")}           AS year,
    {col_or_null("mae_ticks")}      AS mae_ticks,
    {col_or_null("mfe_ticks")}      AS mfe_ticks,
    {col_or_null("regime")}         AS regime,
    {col_or_null("tod_bucket")}     AS tod_bucket,
    {"TRY_CAST(entry_context AS JSON)" if "entry_context" in present else "NULL"} AS entry_context
FROM read_csv_auto('{path}', header=true, timestampformat='%Y-%m-%d %H:%M:%S%z')
"""
```

Identifier and path quoting must be handled safely (DuckDB parameter binding for `run_id`; path passed through `duckdb.escape_identifier` / a safe quoter — never f-string a raw operator-supplied path). The same dynamic-SELECT pattern applies to `wfa_windows` and `wfa_summary` if their CSVs ever ship optional columns.

**`wfa_windows` kind:**
```sql
INSERT INTO wfa_windows
SELECT
    ? AS run_id,                                              -- parameterized
    strategy, window_idx, is_start, is_end, oos_start, oos_end,
    CAST(best_params AS JSON) AS best_params,
    is_pf, is_trades, oos_pf, oos_trades, oos_net, wfe, degraded
FROM read_csv_auto(?, header=true);                           -- parameterized
```
Same safety rule as `trades` kind: `run_id` is parameter-bound (it is always a SHA-256 hex string with no injection risk in practice, but binding makes the contract explicit and consistent). The CSV path is passed through a safe quoter, never f-string-interpolated. On `CAST(best_params AS JSON)` failure for a row, store `{"_raw": "<original-string>"}` instead and log a warning once per file with the first offending row.

**`wfa_summary` kind:** `INSERT INTO wfa_summary SELECT ? AS run_id, * FROM read_csv_auto(?, header=true)`. Same parameterized-binding and path-quoting rules.

**`summary` kind:** unpivot — every numeric column becomes a `(metric_name, metric_value)` row in `run_metrics`. String columns go to `label_value`. `run_id` parameter-bound.

**`mixed` kind:** both `trades` and `summary` paths run within the same transaction. Aggregate column values are taken from row 0 and inserted once into `run_metrics`.

**`derived` kind (lazy `import_<name>`):**

```sql
-- First ingest only: define schema from inference
CREATE TABLE IF NOT EXISTS <safe_table_name> AS
  SELECT *, NULL::VARCHAR AS run_id FROM read_csv_auto(?, header=true) WHERE 1=0;

INSERT INTO <safe_table_name>
  SELECT *, ? AS run_id FROM read_csv_auto(?, header=true);
```

**`<safe_table_name>` derivation is security-sensitive** because it comes from the filename, not from a controlled allowlist. DuckDB does not support parameter binding for table names — the identifier is substituted into the SQL string. Rule:

```python
SAFE_IDENT = re.compile(r"[^A-Za-z0-9_]")

def safe_import_table_name(csv_path: Path) -> str:
    stem = csv_path.stem.lower()
    sanitized = SAFE_IDENT.sub("_", stem)        # strip dots, dashes, spaces, etc.
    if not sanitized or not sanitized[0].isalpha():
        sanitized = "f_" + sanitized              # force leading letter
    return f"import_{sanitized}"
```

Verify the result matches `^[A-Za-z_][A-Za-z0-9_]*$` before substituting; reject the ingest with `error='unsafe_import_table_name'` if the regex check fails (defense-in-depth — the sanitization above should already guarantee it, but the assert is cheap).

The existing portfolio_framework convenience CSVs already produce safe names (`import_phase1_strategy_summary`, `import_microstructure_lift`, `import_phase3_multitier_comparison`) — this rule just ensures any future filename can't smuggle SQL into the identifier slot.

On subsequent ingest with new columns, `ALTER TABLE <safe_table_name> ADD COLUMN <new_col>` for nullable additions. On column removal or retype, refuse and require operator to drop the import table manually.

### 5.4 WFA filename strategy sniff

```python
# tools/warehouse/sniff.py
WFA_P13_RE = re.compile(r"^wfa_windows_p13_(?P<strategy>[a-z][a-z0-9_]*)\.csv$")

def sniff_strategy_from_filename(path: Path, known_strategies: set[str]) -> str | None:
    m = WFA_P13_RE.match(path.name)
    if not m:
        return None  # Multi-strategy WFA file; runs.strategy stays NULL by design.
    candidate = m.group("strategy")
    if candidate in known_strategies:
        return candidate
    # Suffix match: 'asian' → 'a_asian_continuation'
    suffix_matches = [s for s in known_strategies if s == candidate or s.endswith("_" + candidate)]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    log.warning("wfa filename sniff: %s candidate=%r matches=%r → strategy=NULL",
                path.name, candidate, suffix_matches)
    return None
```

- `known_strategies` is loaded from `config/strategies.py` at ingester startup (`tools/warehouse/known_strategies.py`).
- Multi-strategy WFA files (`wfa_windows.csv`, `wfa_windows_shardA.csv`, `wfa_windows_shardB.csv`) deliberately don't match — `runs.strategy = NULL`. Per-row strategy identity lives in `wfa_windows.strategy`.
- Zero or ambiguous matches: NULL with warning; ingest still succeeds. The data is intact in `wfa_windows`; only the run-level denormalization is missing.

### 5.5 Sidecar contract

```json
{
  "schema_version": 1,
  "strategy": "vwap_pullback_v2",
  "params": { "ema_len": 21, "atr_mult": 1.5 },
  "code_sha": "a1b2c3d...",
  "seed": 42,
  "lookback_start": "2021-05-17T00:00:00Z",
  "lookback_end":   "2026-05-15T00:00:00Z",
  "engine_version": "phoenix_portfolio_backtest@2026-05-31",
  "friction_per_rt_usd": 4.82,
  "friction_applied": true,
  "logical_group": "phase13_wfa",
  "notes": ""
}
```

All fields optional except `schema_version`. Ingester:
- Records missing fields in `sidecar_raw.meta.missing_fields[]`.
- Sets `runs.friction_applied = true` if either `friction_applied: true` is present OR `friction_per_rt_usd > 0`.
- Sets `runs.friction_applied = false` if sidecar is present but says neither.
- Sets `runs.friction_applied = false` if sidecar is absent (legacy default; B13 era).
- `--mark-friction-applied` CLI flag overrides to `true` (operator backfill).

### 5.6 PID lock

`tools/warehouse/lock.py`. Lock file: `data/warehouse/.ingest.lock`.

```jsonc
// .ingest.lock contents
{"pid": 12345, "host": "WORKSTATION-1", "started_at": "2026-05-31T20:42:11Z"}
```

- On acquire: if file absent, write and proceed. If present, read PID. If PID alive on same host → exit with `another ingest is running (pid=N, started=T)`. If PID dead or host differs → log `stale lock from pid N, recovering` and overwrite.
- **Lock release is always in a `try/finally` block, not only in error paths.** Additionally registered via `atexit.register()` as belt-and-suspenders.
- Library callers may pass `skip_lock=True` if they've already acquired the lock (e.g., for batched ingest from a single CLI call).
- Cross-platform: no `fcntl`/`msvcrt` dependency; works on Windows.

### 5.7 Glob safety

`scan_dir` defaults:
- Default glob is `*.csv` (one level). Recursion requires explicit `recursive=True` / `--recursive`.
- Hard skip list (regardless of recursion): any path component in `{"tests", "fixtures", ".pytest_cache", "__pycache__", "node_modules"}` → skip with debug log.

### 5.8 Out of scope for v1

- No filesystem watcher / auto-ingest.
- No parallel ingest (single-writer DB).
- No schema migrations beyond ALTER ADD COLUMN for `import_<name>` tables. `schema_version` on sidecar gates future migration work.

---

## 6. Data flow — four lifecycles

All four go through the same `ingest_csv` engine. Only the sniff result and target table differ.

### Lifecycle A — New backtest (forward-looking happy path)

```
backtest engine (tools/portfolio_backtest/run_portfolio_backtest.py, etc.)
   ├─ writes macro_trades.csv (UTC, extended schema, friction-net)
   └─ writes macro_trades.run.json (strategy=NULL multi, params={per_strategy:{...}}, code_sha, seed, lookback,
                                    friction_applied=true, friction_per_rt_usd=4.82)
                ↓
python -m tools.warehouse ingest backtest_results/portfolio_framework/macro_trades.csv
                ↓
1. hash(csv + sidecar)               → run_id
2. SELECT 1 FROM runs WHERE run_id=? → miss
3. sniff_kind(header)                → 'trades'
4. BEGIN
5. INSERT INTO runs (...friction_applied=true...)
6. INSERT INTO trades SELECT ... FROM read_csv_auto(...)
7. COMMIT
                ↓
SELECT * FROM trades_ct WHERE session_date='2026-05-30' AND tod_bucket='Opening Drive';
```

### Lifecycle B — Legacy CSV

55 files in `backtest_results/` with no sidecar, no extended cols. **Verified 2026-05-31: all sampled files are UTC `+00:00`** — no CT→UTC cast required. The legacy path is identical to Lifecycle A except: no sidecar, `friction_applied=false`, extended columns (`mae_ticks`, `mfe_ticks`, `regime`, `tod_bucket`, `entry_context`) all NULL, `strategy` parsed from filename when possible.

### Lifecycle C — WFA shard glob

```
python -m tools.warehouse ingest 'backtest_results/portfolio_framework/wfa_windows_p13_*.csv' \
       --logical-group phase13_wfa --recursive
                ↓
glob expands → wfa_windows_p13_raschke.csv, wfa_windows_p13_inside_bar.csv, wfa_windows_p13_asian.csv
              (multi_day shard lands later; ingests then.)

For each shard:
1-3. hash, dedup, sniff           → csv_kind='wfa_windows'
4.   runs row: logical_group='phase13_wfa', strategy=<sniff_strategy_from_filename(...)>
5.   INSERT INTO wfa_windows ... CAST(best_params AS JSON) ... FROM read_csv_auto(...)
                ↓
SELECT * FROM wfa_windows w JOIN runs r USING(run_id) WHERE r.logical_group='phase13_wfa';
```

### Lifecycle D — Convenience CSV

```
1-3. hash, dedup, sniff           → csv_kind='derived', target_table='import_phase1_strategy_summary'
4.   runs row: strategy=NULL, params records source_filename
5.   CREATE TABLE IF NOT EXISTS import_phase1_strategy_summary ...
     INSERT ... SELECT *, '<run_id>' ...
```

### Cross-lifecycle query example

```sql
-- "TOD P&L attribution for strategies that are robust in Phase 13 WFA,
--  restricted to friction-net runs only."
SELECT t.tod_bucket, t.strategy,
       SUM(t.pnl_dollars) AS net_pnl,
       COUNT(*)           AS n_trades
FROM trades_ct t
JOIN runs r          ON t.run_id = r.run_id
JOIN wfa_summary s   ON s.strategy = t.strategy
JOIN runs rs         ON s.run_id = rs.run_id
WHERE rs.logical_group = 'phase13_wfa'
  AND s.robust = true
  AND r.friction_applied = true
  AND t.session_date >= '2024-01-01'
GROUP BY 1, 2
ORDER BY net_pnl DESC;
```

---

## 7. Error handling

The ingester is a **best-effort batch processor**: a bad file fails alone, the rest of the batch continues, every failure is logged with enough context to retry.

| Class | Trigger | Behavior |
|---|---|---|
| Bad CSV header | Sniffer returns `error` | Per-file rollback. `status='error'`, `error='unknown_csv_kind'`, log full header to `ingest_errors.log`. |
| Malformed CSV row | `read_csv_auto` fails mid-stream | Per-file rollback. Error message captures DuckDB's row number. Other files continue. |
| Sidecar JSON parse failure | `json.loads` raises on present file | Treat as `sidecar_missing=true`. Store raw bytes in `sidecar_raw.meta.parse_error_raw_b64`. Warning logged. Ingest proceeds. |
| Sidecar schema mismatch | `schema_version` unknown to ingester | Per-file rollback. Refuse until ingester is updated. Prevents silently dropping new fields. |
| `best_params` JSON cast failure | INVENTORY says JSON, cell is Python `repr()` | Per-row: store `{"_raw": "<string>"}` as JSON in `wfa_windows.best_params`. Warn once per file with first offending row. |
| Duplicate `run_id` | Content hash already in `runs` | `status='skipped_duplicate'`. Idempotent. |
| Stale lock file | `.ingest.lock` exists, PID dead or different host | Log `stale lock from pid N, recovering`, overwrite. |
| Live lock | `.ingest.lock` exists, PID alive on same host | Exit with `another ingest is running`. |
| `import_<name>` schema drift, additive | New nullable column on a later ingest | `ALTER TABLE ... ADD COLUMN`. Ingest continues. |
| `import_<name>` schema drift, destructive | Column removed or retyped | Refuse. Operator drops the import table manually. |
| WFA filename sniff miss | Filename matches `WFA_P13_RE` but candidate doesn't match exactly one known strategy | Warning. `runs.strategy = NULL`. Data still ingests; per-row identity in `wfa_windows.strategy`. |
| DB-level corruption / disk full | DuckDB raises `IOException` | Whole batch aborts. **Lock release runs from `finally`/`atexit`** regardless. Process exits non-zero. |
| Sidecar friction claim vs reality | Sidecar `friction_per_rt_usd=0` on a file the operator knows is friction-net | Out of scope to auto-detect. Use `--mark-friction-applied` override. |

**Lock release is always in a `try/finally` block, not only in error paths.** If the process is SIGKILL'd or the machine loses power, the stale-PID detection on next ingest recovers automatically.

**Error log format** (`data/warehouse/ingest_errors.log`, JSONL, grep/jq-friendly, append-only):

```json
{"ts":"2026-05-31T20:42:11Z","level":"error","file":"backtest_results/foo.csv",
 "error_class":"unknown_csv_kind","header":["strategy","weird_col","..."],
 "run_id":null,"traceback":"..."}
```

**Deliberately not handled:**
- No retries (nothing to wait for).
- No partial-row recovery within a CSV (all-or-nothing per file).
- No automatic cleanup of orphaned `runs` rows. A documented cleanup query lives in §11.

---

## 8. Testing

`tests/warehouse/`, runs via `pytest`. Three layers, scaled to risk.

### 8.1 Layer 1 — Fixture-based unit tests (the bulk)

`tests/warehouse/fixtures/`:
- `trades_legacy_3rows.csv` — 13-col legacy schema, UTC `+00:00`, no sidecar.
- `trades_macro_3rows.csv` + `.run.json` — extended schema with sidecar.
- `wfa_windows_3rows.csv` — JSON `best_params` round-trip.
- `wfa_windows_p13_inside_bar_3rows.csv` — exercises filename sniff regex.
- `summary_5cols.csv` — unpivot into `run_metrics`.
- `mixed_summary_with_trade_row.csv` — mixed-kind sniff.
- `malformed_ragged.csv` — error path.
- `bad_sidecar.json` (with valid CSV) — sidecar parse failure path.
- `unknown_header.csv` — `unknown_csv_kind` error path.

Tests run against `duckdb.connect(':memory:')` — fast, zero cleanup, full isolation. Schema applied via `tools/warehouse/schema.sql` at setup.

**Coverage targets:**
- Every §7 row has at least one test (happy + failure per kind).
- Idempotency: ingest same CSV twice → second is `skipped_duplicate`, row counts unchanged.
- UTC round-trip: legacy CSV → DB → `trades_ct` view yields correct CT.
- `friction_applied` defaults: legacy=false, new (with sidecar)=true, sidecar-present-without-friction=false. One test per case.
- WFA filename sniff: exact match, suffix match, ambiguous (multiple), no match.
- `market_open_minutes` correctness: 08:30 CT → 0, 09:00 CT → 30, 06:00 CT → −150 (Globex).
- Lock semantics: acquire / release / stale-PID detection (mock `psutil.pid_exists`).
- Lock release **runs from `finally`** — test asserts file is gone after a deliberate exception inside the ingest block.

### 8.2 Layer 2 — Smoke test against real CSVs

One pytest test, `@pytest.mark.smoke`, points the ingester at `backtest_results/portfolio_framework/` on the live filesystem and asserts:

- All 15 portfolio_framework CSVs ingest with status `inserted` or `skipped_duplicate`.
- **`assert trade_count >= 70000`** (concrete floor; INVENTORY documents 76,342 pre-multi_day; the floor catches partial-ingest regressions even if multi_day adds more rows).
- `wfa_windows` row count `>= 210` (matches INVENTORY's documented full-shard count).
- `wfa_summary` row count `>= 14`.
- No new lines in `ingest_errors.log` during the run.
- Sample query `SELECT COUNT(*) FROM trades_ct WHERE session_date >= '2024-01-01'` returns `>0`.

Runs against `.tmp/test_phoenix.duckdb` (deleted before each run). Not part of default `pytest`; opt-in via `pytest -m smoke`.

### 8.3 Layer 3 — Manual end-to-end runbook

`docs/superpowers/specs/2026-05-31-warehouse-runbook.md` (sibling to this spec):
1. `python -m tools.warehouse ingest backtest_results/portfolio_framework/`
2. `python -m tools.warehouse ingest backtest_results/ --recursive` (legacy)
3. Run the four example queries from this spec and eyeball results.
4. Note final row counts and any `ingest_errors.log` entries.

This is the gate for "spec implemented." Not CI (there is no CI for the bot) — a documented operator procedure.

### 8.4 Out of scope for v1 testing

- No concurrent-ingest tests (single-writer DB).
- No multi-host lock tests (single-host operator).
- No performance tests (76k rows is small).
- No property-based / mutation testing.

---

## 9. Example queries (for the runbook and future quick reference)

```sql
-- 1. Best PF strategies in last 12 months across all friction-net runs.
SELECT t.strategy,
       SUM(t.pnl_dollars) FILTER (WHERE t.pnl_dollars > 0) /
         NULLIF(-SUM(t.pnl_dollars) FILTER (WHERE t.pnl_dollars < 0), 0) AS profit_factor,
       COUNT(*) AS n
FROM trades_ct t
JOIN runs r USING(run_id)
WHERE r.friction_applied = true
  AND t.session_date >= CURRENT_DATE - INTERVAL '1 year'
GROUP BY 1
HAVING n >= 50
ORDER BY profit_factor DESC;

-- 2. Time-of-day session attribution, session-hours only.
SELECT tod_bucket, COUNT(*) AS n, SUM(pnl_dollars) AS net
FROM trades_ct
WHERE market_open_minutes BETWEEN 0 AND 90
GROUP BY 1 ORDER BY net DESC;

-- 3. Compare gross-PnL legacy era vs friction-net era for the same strategy.
SELECT r.friction_applied,
       COUNT(*)            AS n_trades,
       AVG(t.pnl_dollars)  AS avg_pnl,
       SUM(t.pnl_dollars)  AS total_pnl
FROM trades_ct t JOIN runs r USING(run_id)
WHERE t.strategy = 'vwap_pullback_v2'
GROUP BY r.friction_applied;

-- 4. WFA robust strategies with their per-window OOS PF distribution.
SELECT s.strategy, s.mean_oos_pf, s.pct_windows_degraded,
       quantile_cont(w.oos_pf, [0.25, 0.5, 0.75]) AS oos_pf_iqr
FROM wfa_summary s
JOIN wfa_windows w USING(strategy)
JOIN runs rs ON s.run_id = rs.run_id
WHERE s.robust = true
  AND rs.logical_group = 'phase13_wfa'
GROUP BY 1, 2, 3
ORDER BY s.mean_oos_pf DESC;
```

---

## 10. Cross-run P&L caveat (read before joining `pnl_dollars` across runs)

`pnl_dollars` semantics are **not uniform across all rows in `trades`**. Friction status is governed by `runs.friction_applied`:

| `runs.friction_applied` | `pnl_dollars` meaning |
|---|---|
| `true` | Net of $4.82 round-turn friction (commission + 2-tick slippage). Recover gross by `pnl_dollars + 4.82` per trade. |
| `false` | Gross P&L. No friction deducted. (Legacy / B13-bug era.) |
| `NULL` | Should not occur; if it does, treat as unknown and exclude. |

**Every cross-run P&L aggregation must either:**
- Filter to one era: `WHERE r.friction_applied = true` (or `WHERE r.friction_applied = false`). Note: a bare `r.friction_applied` boolean filter excludes NULL rows automatically (three-valued logic); **or**
- Explicitly normalize: `pnl_dollars - CASE WHEN r.friction_applied IS NOT TRUE THEN 4.82 ELSE 0 END`.

**SQL three-valued-logic trap.** The earlier draft used `CASE WHEN NOT r.friction_applied THEN 4.82 ELSE 0 END`, which is **wrong**: `NOT NULL` evaluates to `NULL`, falls into the `ELSE 0` branch, and silently treats NULL-friction rows as already friction-net. The correct predicate is `IS NOT TRUE`, which returns `TRUE` for both `FALSE` and `NULL` — meaning unresolved rows get friction subtracted (conservative) rather than silently passing through. Alternatively, add `AND r.friction_applied IS NOT NULL` to the filter and document the normalize formula as only valid after NULLs are excluded upstream.

`pnl_ticks` is unaffected — gross in both eras.

---

## 11. Cleanup queries (operator reference, not run automatically)

Documented for the rare case of corruption. Not part of the ingest pipeline.

```sql
-- Orphaned runs: a run row with no associated payload anywhere.
SELECT run_id FROM runs
WHERE run_id NOT IN (SELECT DISTINCT run_id FROM trades)
  AND run_id NOT IN (SELECT DISTINCT run_id FROM run_metrics)
  AND run_id NOT IN (SELECT DISTINCT run_id FROM wfa_windows)
  AND run_id NOT IN (SELECT DISTINCT run_id FROM wfa_summary);
-- (Operator decides whether to DELETE.)

-- All ingest history with row counts.
SELECT r.ingested_at, r.source_filename, r.csv_kind, r.friction_applied,
       (SELECT COUNT(*) FROM trades       t  WHERE t.run_id  = r.run_id) AS trades_n,
       (SELECT COUNT(*) FROM wfa_windows  w  WHERE w.run_id  = r.run_id) AS wfa_n,
       (SELECT COUNT(*) FROM run_metrics  m  WHERE m.run_id  = r.run_id) AS metrics_n
FROM runs r
ORDER BY r.ingested_at DESC;
```

---

## 12. Coordination items — parallel consolidation agent

Status as of 2026-05-31:

| # | Item | Status |
|---|---|---|
| 1 | **Sidecar emission** across `run_portfolio_backtest.py`, `_wfa_shard.py`, `_wfa_shard_phase13.py`, `_run_phase13_4.py`, `_wfa_merge.py`. Contract per §5.5. | ✅ **Completed 2026-05-31** (code_sha `090d030`) |
| 2 | **`macro_trades.csv` timestamp format** has explicit `+00:00` suffix. | ✅ **Verified 2026-05-31** against 3 representative files |
| 3 | **`wfa_windows*.best_params` is valid JSON** (double quotes, `true`/`false`/`null`), not Python `repr(dict)`. `CAST(best_params AS JSON)` works directly; no shim needed. | ✅ **Verified 2026-05-31** |
| 4 | **When multi_day lands:** emit sidecar; re-run `_wfa_merge.py`; overwrite `wfa_summary.csv` (warehouse keeps both versions under different `run_id`). | ⏳ Pending — multi_day backtest in flight |
| 5 | **Filename stability:** lock `wfa_windows_p13_*.csv` glob before multi_day, or notify of any rename. | ⏳ Pending — depends on item 4 |
| 6 | **Do not move/rename the 55 legacy CSVs** until after their first ingest. | ⏳ Pending — operator discipline during ingester build |

Items 1–3 being resolved means the ingester ships with **no compatibility shims** — the trades-kind SQL parses `+00:00` directly via `timestampformat='%Y-%m-%d %H:%M:%S%z'`, and the wfa-windows SQL casts `best_params` straight to JSON with no string-mangling pre-pass. Items 4–6 are external-event triggers, not blockers for v1 code.

---

## 13. Out of scope / future work

- **Live trade & signal log subsystem.** Own spec. Will add `signals`, `fills`, `live_trades` tables that JOIN `runs` on `run_id` (where applicable) and `trades` on `(strategy, entry_ts)` for backtest-vs-live attribution.
- **Market data store subsystem.** Own spec. Will add `bars` and `ticks` tables. Largest write volume of the three subsystems.
- **Convenience tables → views.** v2 replaces `import_phase1_*` / `import_microstructure_*` / `import_phase3_*` tables with DuckDB views that recompute from `trades`. Single source of truth. Out of v1 scope to verify SQL parity.
- **Materialized `session_date`.** `trades_ct` computes `session_date` and `market_open_minutes` via `AT TIME ZONE` on every row read. Invisible at 76k rows. When Databento 5yr tick data ingestion grows `trades` into the millions, `WHERE session_date = '...'` queries will do a full scan. At that point, materialize `session_date` as a generated column (or a separate indexed `trades_calendar` table) and add an index. Not a v1 concern; flagged so future-you isn't surprised in a query plan.
- **Dashboard integration.** Out of v1. Dashboard will consume the SQL layer via the same views; no schema changes expected.
- **Auto-watcher / scheduled ingest.** Out of v1. v1 is manual / operator-triggered.

---

## 14. Glossary

| Term | Meaning |
|---|---|
| Run | One ingested CSV (+ optional sidecar). Identity = `sha256(csv_bytes + canonical_sidecar)`. |
| Logical group | Optional label on `runs` letting multiple shard CSVs be queried as one logical concept (e.g., `phase13_wfa`). |
| Friction-net | `pnl_dollars` has $4.82 round-turn (commission + 2-tick slippage) deducted. Tracked on `runs.friction_applied`. |
| Friction-gross | `pnl_dollars` has no friction deducted. Legacy / B13-era default. |
| Extended trade schema | INVENTORY's `macro_trades.csv` schema: legacy 13 columns + `mae_ticks`, `mfe_ticks`, `regime`, `tod_bucket`. The warehouse `trades` table is a superset including `entry_context` JSON. |
| WFA | Walk-Forward Analysis. 15 windows per strategy (12-month IS, 3-month OOS). Per-window results in `wfa_windows`; per-strategy summary in `wfa_summary`. |
| MAE / MFE | Maximum Adverse / Favorable Excursion, in ticks. |
| `trades_ct` | Consumer view over `trades`. Adds `session_date`, `market_open_minutes`, `entry_ts_ct`, `exit_ts_ct`. CT-derived columns can be negative for Globex / pre-market trades. |
