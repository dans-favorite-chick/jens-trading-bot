-- Phoenix Backtest Warehouse — DDL
-- Run once per DB (all statements are idempotent via CREATE IF NOT EXISTS / CREATE OR REPLACE).
-- DuckDB 1.x bundled JSON extension; INSTALL is a no-op after first run.

INSTALL json;
LOAD json;

-- ──────────────────────────────────────────────────────────────
-- runs: provenance layer. One row per ingested CSV.
-- Identity = content hash (sha256 of csv_bytes + canonical_sidecar).
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    run_id           VARCHAR PRIMARY KEY,
    source_filename  VARCHAR NOT NULL,
    csv_kind         VARCHAR NOT NULL,   -- 'trades'|'summary'|'mixed'|'wfa_windows'|'wfa_summary'|'derived'
    logical_group    VARCHAR,
    strategy         VARCHAR,            -- NULL for multi-strategy CSVs
    params           JSON,
    code_sha         VARCHAR,
    seed             INTEGER,
    lookback_start   TIMESTAMP WITH TIME ZONE,
    lookback_end     TIMESTAMP WITH TIME ZONE,
    friction_applied BOOLEAN,            -- see §10 of spec
    ingested_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    sidecar_raw      JSON
);

-- ──────────────────────────────────────────────────────────────
-- trades: canonical fact table.
-- Superset of legacy 13-col + portfolio_framework extended schema.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    run_id          VARCHAR NOT NULL REFERENCES runs(run_id),
    strategy        VARCHAR NOT NULL,
    direction       VARCHAR NOT NULL,                 -- always UPPER: 'LONG' | 'SHORT'
    entry_ts        TIMESTAMP WITH TIME ZONE NOT NULL,
    entry_price     DOUBLE NOT NULL,
    stop_price      DOUBLE,
    target_price    DOUBLE,
    exit_ts         TIMESTAMP WITH TIME ZONE,
    exit_price      DOUBLE,
    exit_reason     VARCHAR,
    pnl_dollars     DOUBLE,
    pnl_ticks       DOUBLE,
    hold_minutes    DOUBLE,
    year            INTEGER,
    -- Extended cols — NULL for legacy CSVs
    mae_ticks       DOUBLE,
    mfe_ticks       DOUBLE,
    regime          VARCHAR,
    tod_bucket      VARCHAR,
    entry_context   JSON
);

CREATE INDEX IF NOT EXISTS idx_trades_run   ON trades(run_id);
CREATE INDEX IF NOT EXISTS idx_trades_strat ON trades(strategy, entry_ts);
CREATE INDEX IF NOT EXISTS idx_trades_year  ON trades(year);

-- ──────────────────────────────────────────────────────────────
-- trades_ct: consumer view — UTC → Chicago, session_date, market_open_minutes.
-- market_open_minutes is NEGATIVE for pre-market / Globex trades (correct, useful).
-- Session-hours-only filter: WHERE market_open_minutes BETWEEN 0 AND 90.
-- ──────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW trades_ct AS
SELECT
    *,
    (entry_ts AT TIME ZONE 'America/Chicago')::DATE                       AS session_date,
    EXTRACT(EPOCH FROM (
        (entry_ts AT TIME ZONE 'America/Chicago')
        - date_trunc('day', entry_ts AT TIME ZONE 'America/Chicago')
        - INTERVAL '8 hours 30 minutes'
    )) / 60.0                                                              AS market_open_minutes,
    entry_ts AT TIME ZONE 'America/Chicago'                                AS entry_ts_ct,
    exit_ts  AT TIME ZONE 'America/Chicago'                                AS exit_ts_ct
FROM trades;

-- ──────────────────────────────────────────────────────────────
-- run_metrics: open key/value extension for arbitrary summary metrics.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id        VARCHAR NOT NULL REFERENCES runs(run_id),
    metric_name   VARCHAR NOT NULL,
    metric_value  DOUBLE,
    label_value   VARCHAR,
    PRIMARY KEY (run_id, metric_name)
);

-- ──────────────────────────────────────────────────────────────
-- wfa_windows: first-class WFA fact table. INVENTORY schema + run_id.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wfa_windows (
    run_id       VARCHAR NOT NULL REFERENCES runs(run_id),
    strategy     VARCHAR NOT NULL,
    window_idx   INTEGER NOT NULL,
    is_start     DATE,
    is_end       DATE,
    oos_start    DATE,
    oos_end      DATE,
    best_params  JSON,
    is_pf        DOUBLE,
    is_trades    INTEGER,
    oos_pf       DOUBLE,
    oos_trades   INTEGER,
    oos_net      DOUBLE,
    wfe          DOUBLE,
    degraded     BOOLEAN,
    PRIMARY KEY (run_id, strategy, window_idx)
);

CREATE INDEX IF NOT EXISTS idx_wfa_strat ON wfa_windows(strategy);

-- ──────────────────────────────────────────────────────────────
-- wfa_summary: INVENTORY schema + run_id.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wfa_summary (
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

-- ──────────────────────────────────────────────────────────────
-- market_state_bars: per-5m-bar classifier output (Phase 8, 2026-06-01).
-- Populated by tools/warehouse/backfill_market_state.py.
-- One row per 5m bar; PRIMARY KEY = bar_ts so the backfill is idempotent.
-- See core/market_state.py for the signal definitions and label priority.
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_state_bars (
    bar_ts            TIMESTAMP WITH TIME ZONE PRIMARY KEY,
    label             VARCHAR NOT NULL,
    realized_vol      DOUBLE,
    trend_strength    DOUBLE,
    choppiness_index  DOUBLE
);

CREATE INDEX IF NOT EXISTS idx_market_state_label ON market_state_bars(label);

-- import_<name> tables are created lazily by the ingester on first encounter.
-- They follow the pattern: CREATE TABLE IF NOT EXISTS import_<safe_name> AS
--   SELECT *, NULL::VARCHAR AS run_id FROM read_csv_auto(...) WHERE 1=0;
