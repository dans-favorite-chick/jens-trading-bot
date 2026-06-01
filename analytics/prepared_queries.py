"""Prepared SELECT queries against the Phoenix DuckDB warehouse.

Task 1 of the Phoenix Strategy Oracle build.

DESIGN PHILOSOPHY
-----------------
The Spider 2.0 benchmark shows LLMs are about 10% accurate writing SQL
against real enterprise schemas. The Phoenix Strategy Oracle therefore
NEVER authors SQL. Every query the orchestrator can issue lives in this
file as a named, parameterized Python function.

INVARIANTS
----------
1. friction_applied filter (NON-NEGOTIABLE)
   `friction_applied` is a column on the `runs` table, NOT `trades`.
   Every query that touches `pnl_dollars` MUST join `runs` and add
   `WHERE r.friction_applied = TRUE`. Forgetting this inflates P&L by
   including unrealistic frictionless runs.

2. Use `trades_ct` (the view) whenever you need Chicago-time fields
   (session_date, market_open_minutes, entry_ts_ct).

3. Read-only DuckDB connection always.

4. ALL queries use parameter binding (`?` placeholders). NEVER f-string
   strategy/window/etc. into the SQL string.

5. ALL queries are SELECT-only. `assert_select_only` is applied to every
   query string as belt-and-suspenders defense alongside the read-only
   connection.

6. Windows cp1252-safe — no box-drawing characters or emoji in any
   `print()` output. Comments and logs are fine.
"""
from __future__ import annotations

import ast
import logging
import re
from typing import Any

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

WAREHOUSE_PATH = r"C:\Trading Project\phoenix_bot\data\warehouse\phoenix.duckdb"

BANNED_SQL_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE",
    "ALTER", "TRUNCATE", "REPLACE", "MERGE",
})

# Pre-compiled regex: case-insensitive, word-boundary match so column names
# like `created_at` or `updated_at` do not false-positive.
_BANNED_RE = re.compile(
    r"\b(" + "|".join(sorted(BANNED_SQL_KEYWORDS)) + r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

def assert_select_only(sql: str) -> None:
    """Raise ValueError if `sql` contains any banned SQL keyword.

    Word-boundary matching only — `SELECT created_at FROM trades` is fine
    because `CREATE` is a substring of `created` but does not appear as a
    standalone word.
    """
    m = _BANNED_RE.search(sql)
    if m:
        raise ValueError(
            f"Banned SQL keyword '{m.group(1).upper()}' found in query: "
            f"{sql[:120]!r}"
        )


def open_conn(path: str = WAREHOUSE_PATH) -> duckdb.DuckDBPyConnection:
    """Open a read-only DuckDB connection to the warehouse."""
    return duckdb.connect(path, read_only=True)


def _run_query(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    """Execute a parameterized SELECT and return the result as a DataFrame."""
    assert_select_only(sql)
    return con.execute(sql, params or []).df()


# ---------------------------------------------------------------------------
# Strategy discovery
# ---------------------------------------------------------------------------

def strategies_with_trades(
    conn: duckdb.DuckDBPyConnection,
    window_days: int,
    min_n: int = 30,
) -> list[str]:
    """Return strategies with >= min_n friction-applied trades in the trailing window."""
    sql = """
        SELECT t.strategy AS strategy, COUNT(*) AS n
        FROM trades t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.entry_ts >= now() - (? * INTERVAL '1 day')
        GROUP BY t.strategy
        HAVING COUNT(*) >= ?
        ORDER BY t.strategy
    """
    df = _run_query(conn, sql, [int(window_days), int(min_n)])
    return df["strategy"].tolist()


# ---------------------------------------------------------------------------
# Per-strategy trades
# ---------------------------------------------------------------------------

def trades_for_strategy(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
    window_days: int,
) -> pd.DataFrame:
    """Return the friction-applied trades for `strategy` in the trailing window.

    Columns: entry_ts, exit_ts, direction, pnl_dollars, pnl_ticks,
             mae_ticks, mfe_ticks, regime, tod_bucket, session_date,
             market_open_minutes, hold_minutes.
    """
    sql = """
        SELECT
            t.entry_ts,
            t.exit_ts,
            t.direction,
            t.pnl_dollars,
            t.pnl_ticks,
            t.mae_ticks,
            t.mfe_ticks,
            t.regime,
            t.tod_bucket,
            t.session_date,
            t.market_open_minutes,
            t.hold_minutes
        FROM trades_ct t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.strategy = ?
          AND t.entry_ts >= now() - (? * INTERVAL '1 day')
        ORDER BY t.entry_ts
    """
    return _run_query(conn, sql, [strategy, int(window_days)])


# ---------------------------------------------------------------------------
# Monthly Sharpe proxy
# ---------------------------------------------------------------------------

def monthly_sharpe_proxy(
    conn: duckdb.DuckDBPyConnection,
    months_back: int = 6,
) -> pd.DataFrame:
    """Per-month aggregates used by the regime gate.

    Columns: month, trade_count, avg_pnl, pnl_stddev, sharpe_proxy, win_rate.
    `sharpe_proxy = avg_pnl / pnl_stddev` (NaN when stddev is 0 or NULL).
    """
    sql = """
        SELECT
            date_trunc('month', t.session_date)::DATE AS month,
            COUNT(*) AS trade_count,
            AVG(t.pnl_dollars) AS avg_pnl,
            STDDEV_SAMP(t.pnl_dollars) AS pnl_stddev,
            CASE
                WHEN STDDEV_SAMP(t.pnl_dollars) IS NULL
                  OR STDDEV_SAMP(t.pnl_dollars) = 0
                THEN NULL
                ELSE AVG(t.pnl_dollars) / STDDEV_SAMP(t.pnl_dollars)
            END AS sharpe_proxy,
            SUM(CASE WHEN t.pnl_dollars > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS win_rate
        FROM trades_ct t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.entry_ts >= now() - (? * INTERVAL '1 month')
        GROUP BY 1
        ORDER BY month
    """
    return _run_query(conn, sql, [int(months_back)])


# ---------------------------------------------------------------------------
# WFA tables (read directly — no friction filter needed; WFA tables already
# represent post-validation aggregates)
# ---------------------------------------------------------------------------

def wfa_summary_for_strategy(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
) -> dict:
    """Return the WFA summary row for `strategy` as a dict.

    Empty dict if no row exists. If multiple rows exist (multiple runs
    have produced WFA summaries for this strategy), the most-recent one
    is preferred via ORDER BY run_id ingested order — but since WFA
    rows are keyed (run_id, strategy) we take the first; callers needing
    multi-run handling should call lower-level APIs.
    """
    sql = """
        SELECT
            n_windows,
            mean_is_pf,
            mean_oos_pf,
            median_oos_pf,
            pct_windows_degraded,
            robust
        FROM wfa_summary
        WHERE strategy = ?
        ORDER BY run_id
        LIMIT 1
    """
    df = _run_query(conn, sql, [strategy])
    if df.empty:
        return {}
    row = df.iloc[0]
    return {
        "n_windows": int(row["n_windows"]) if pd.notna(row["n_windows"]) else None,
        "mean_is_pf": float(row["mean_is_pf"]) if pd.notna(row["mean_is_pf"]) else None,
        "mean_oos_pf": float(row["mean_oos_pf"]) if pd.notna(row["mean_oos_pf"]) else None,
        "median_oos_pf": float(row["median_oos_pf"]) if pd.notna(row["median_oos_pf"]) else None,
        "pct_windows_degraded": float(row["pct_windows_degraded"]) if pd.notna(row["pct_windows_degraded"]) else None,
        "robust": bool(row["robust"]) if pd.notna(row["robust"]) else None,
    }


def wfa_windows_for_strategy(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
) -> pd.DataFrame:
    """All WFA windows for `strategy`, ordered by window_idx."""
    sql = """
        SELECT
            run_id,
            window_idx,
            is_start,
            is_end,
            oos_start,
            oos_end,
            is_pf,
            is_trades,
            oos_pf,
            oos_trades,
            oos_net,
            wfe,
            degraded
        FROM wfa_windows
        WHERE strategy = ?
        ORDER BY run_id, window_idx
    """
    return _run_query(conn, sql, [strategy])


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def panel_by_hour_ct(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
    window_days: int,
) -> pd.DataFrame:
    """Aggregates by Chicago-time hour of entry.

    Columns: hour_ct, n_trades, wins, win_rate, profit_factor, avg_pnl.
    hour_ct is derived from market_open_minutes via trades_ct:
        market open is 08:30 CT == minute 0; minute 60 == 09:30 CT, etc.
    """
    sql = """
        SELECT
            CAST(8 + FLOOR((t.market_open_minutes + 30) / 60.0) AS INTEGER) AS hour_ct,
            COUNT(*) AS n_trades,
            SUM(CASE WHEN t.pnl_dollars > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN t.pnl_dollars > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS win_rate,
            CASE
                WHEN SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END) = 0
                THEN NULL
                ELSE SUM(CASE WHEN t.pnl_dollars > 0 THEN t.pnl_dollars ELSE 0 END)
                     / SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END)
            END AS profit_factor,
            AVG(t.pnl_dollars) AS avg_pnl
        FROM trades_ct t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.strategy = ?
          AND t.entry_ts >= now() - (? * INTERVAL '1 day')
        GROUP BY hour_ct
        ORDER BY hour_ct
    """
    return _run_query(conn, sql, [strategy, int(window_days)])


def panel_by_regime(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
    window_days: int,
) -> pd.DataFrame:
    """Aggregates by trades.regime."""
    sql = """
        SELECT
            t.regime,
            COUNT(*) AS n_trades,
            SUM(CASE WHEN t.pnl_dollars > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS win_rate,
            CASE
                WHEN SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END) = 0
                THEN NULL
                ELSE SUM(CASE WHEN t.pnl_dollars > 0 THEN t.pnl_dollars ELSE 0 END)
                     / SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END)
            END AS profit_factor,
            AVG(t.pnl_dollars) AS avg_pnl
        FROM trades t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.strategy = ?
          AND t.entry_ts >= now() - (? * INTERVAL '1 day')
        GROUP BY t.regime
        ORDER BY t.regime
    """
    return _run_query(conn, sql, [strategy, int(window_days)])


def panel_by_direction(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
    window_days: int,
) -> pd.DataFrame:
    """Aggregates by LONG / SHORT."""
    sql = """
        SELECT
            t.direction,
            COUNT(*) AS n_trades,
            SUM(CASE WHEN t.pnl_dollars > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS win_rate,
            CASE
                WHEN SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END) = 0
                THEN NULL
                ELSE SUM(CASE WHEN t.pnl_dollars > 0 THEN t.pnl_dollars ELSE 0 END)
                     / SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END)
            END AS profit_factor,
            AVG(t.pnl_dollars) AS avg_pnl
        FROM trades t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.strategy = ?
          AND t.entry_ts >= now() - (? * INTERVAL '1 day')
        GROUP BY t.direction
        ORDER BY t.direction
    """
    return _run_query(conn, sql, [strategy, int(window_days)])


def mae_mfe_distribution(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
    direction: str,
    window_days: int,
) -> pd.DataFrame:
    """Bins mae_ticks into integer-tick buckets for one direction.

    Columns: bucket_ticks, n_trades, win_rate.
    """
    sql = """
        SELECT
            CAST(FLOOR(t.mae_ticks) AS INTEGER) AS bucket_ticks,
            COUNT(*) AS n_trades,
            SUM(CASE WHEN t.pnl_dollars > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS win_rate
        FROM trades t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.strategy = ?
          AND t.direction = ?
          AND t.mae_ticks IS NOT NULL
          AND t.entry_ts >= now() - (? * INTERVAL '1 day')
        GROUP BY bucket_ticks
        ORDER BY bucket_ticks
    """
    return _run_query(conn, sql, [strategy, direction, int(window_days)])


# ---------------------------------------------------------------------------
# Graceful-degradation queries
# ---------------------------------------------------------------------------

_IB_REGIME_COLS = ["session_date", "ib_width_ticks", "atr_20d", "ib_regime"]


def daily_ib_regime(
    conn: duckdb.DuckDBPyConnection,
    window_days: int,  # noqa: ARG001 — kept for forward compatibility
) -> pd.DataFrame:
    """Daily IB-width-vs-ATR regime classification.

    NOTE: depends on a `bar_events` table that is not yet in the v1
    warehouse. If the table is missing or empty, returns an empty
    DataFrame with the correct columns and logs a warning. Does NOT raise.
    """
    has_bar_events = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'bar_events'"
    ).fetchone()[0]
    if not has_bar_events:
        logger.warning(
            "daily_ib_regime: bar_events table not in warehouse; "
            "returning empty DataFrame."
        )
        return pd.DataFrame(columns=_IB_REGIME_COLS)

    # If table exists but is empty, return empty frame with correct columns.
    n = conn.execute("SELECT COUNT(*) FROM bar_events").fetchone()[0]
    if n == 0:
        logger.warning(
            "daily_ib_regime: bar_events table exists but is empty; "
            "returning empty DataFrame."
        )
        return pd.DataFrame(columns=_IB_REGIME_COLS)

    # Schema for bar_events is not finalized at v1 — once it lands we will
    # implement the actual IB-width / 20d-ATR derivation here. Until then,
    # behave as the spec says: return empty frame with correct columns.
    logger.warning(
        "daily_ib_regime: bar_events present but derivation not yet "
        "implemented; returning empty DataFrame."
    )
    return pd.DataFrame(columns=_IB_REGIME_COLS)


_CONFLUENCE_COLS = ["confluence_count", "n_trades", "win_rate", "profit_factor"]


def confluence_lift(
    conn: duckdb.DuckDBPyConnection,
    strategy: str,
    window_days: int,
) -> pd.DataFrame:
    """Win-rate / PF lift by confluence count parsed from trades.entry_context.

    confluence_count is the number of keys in entry_context.confluences (a
    sub-object). If entry_context is NULL or has no 'confluences' key,
    confluence_count = 0. If the strategy's trades have NO confluence info
    AT ALL, returns an empty DataFrame.
    """
    # First check whether any trades in scope have a confluences sub-object.
    has_any_sql = """
        SELECT COUNT(*)
        FROM trades t
        JOIN runs r USING(run_id)
        WHERE r.friction_applied = TRUE
          AND t.strategy = ?
          AND t.entry_ts >= now() - (? * INTERVAL '1 day')
          AND t.entry_context IS NOT NULL
          AND json_extract(t.entry_context, '$.confluences') IS NOT NULL
    """
    assert_select_only(has_any_sql)
    n_with = conn.execute(has_any_sql, [strategy, int(window_days)]).fetchone()[0]
    if not n_with:
        logger.info(
            "confluence_lift(%s): no trades carry an entry_context.confluences "
            "sub-object; returning empty DataFrame.", strategy,
        )
        return pd.DataFrame(columns=_CONFLUENCE_COLS)

    sql = """
        WITH base AS (
            SELECT
                t.pnl_dollars,
                CASE
                    WHEN t.entry_context IS NULL THEN 0
                    WHEN json_extract(t.entry_context, '$.confluences') IS NULL THEN 0
                    ELSE CAST(
                        json_array_length(
                            json_keys(json_extract(t.entry_context, '$.confluences'))
                        ) AS INTEGER
                    )
                END AS confluence_count
            FROM trades t
            JOIN runs r USING(run_id)
            WHERE r.friction_applied = TRUE
              AND t.strategy = ?
              AND t.entry_ts >= now() - (? * INTERVAL '1 day')
        )
        SELECT
            confluence_count,
            COUNT(*) AS n_trades,
            SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS win_rate,
            CASE
                WHEN SUM(CASE WHEN pnl_dollars < 0 THEN -pnl_dollars ELSE 0 END) = 0
                THEN NULL
                ELSE SUM(CASE WHEN pnl_dollars > 0 THEN pnl_dollars ELSE 0 END)
                     / SUM(CASE WHEN pnl_dollars < 0 THEN -pnl_dollars ELSE 0 END)
            END AS profit_factor
        FROM base
        GROUP BY confluence_count
        ORDER BY confluence_count
    """
    return _run_query(conn, sql, [strategy, int(window_days)])


# ---------------------------------------------------------------------------
# Config introspection (AST — never imports)
# ---------------------------------------------------------------------------

def current_param_value(
    strategy: str,
    parameter_name: str,
    config_path: str = r"C:\Trading Project\phoenix_bot\config\strategies.py",
) -> Any:
    """Return STRATEGIES[strategy][parameter_name] from config/strategies.py.

    Implemented via ast.parse + ast.literal_eval. The module is NEVER
    imported (importing could execute arbitrary code, hit env-dependent
    branches, or cause cycle problems).

    Raises:
        ValueError: if config_path does not parse as valid Python.
        KeyError:   if STRATEGIES, strategy, or parameter_name not found.
        FileNotFoundError: if config_path does not exist.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ValueError(f"{config_path} did not parse as Python: {e}") from e

    strategies_node: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STRATEGIES":
                    strategies_node = node.value
                    break
        if strategies_node is not None:
            break

    if strategies_node is None:
        raise KeyError(
            f"Top-level STRATEGIES dict not found in {config_path}"
        )

    if not isinstance(strategies_node, ast.Dict):
        raise ValueError(
            f"STRATEGIES in {config_path} is not a dict literal "
            f"(got {type(strategies_node).__name__})."
        )

    # Find the entry for `strategy`.
    strat_dict_node: ast.AST | None = None
    for key, value in zip(strategies_node.keys, strategies_node.values):
        try:
            key_val = ast.literal_eval(key) if key is not None else None
        except (ValueError, SyntaxError):
            continue
        if key_val == strategy:
            strat_dict_node = value
            break

    if strat_dict_node is None:
        raise KeyError(f"Strategy {strategy!r} not found in STRATEGIES")

    if not isinstance(strat_dict_node, ast.Dict):
        raise ValueError(
            f"STRATEGIES[{strategy!r}] is not a dict literal "
            f"(got {type(strat_dict_node).__name__})."
        )

    for key, value in zip(strat_dict_node.keys, strat_dict_node.values):
        try:
            key_val = ast.literal_eval(key) if key is not None else None
        except (ValueError, SyntaxError):
            continue
        if key_val == parameter_name:
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError) as e:
                raise ValueError(
                    f"STRATEGIES[{strategy!r}][{parameter_name!r}] is not a "
                    f"literal value (got {type(value).__name__}): {e}"
                ) from e

    raise KeyError(
        f"Parameter {parameter_name!r} not found in STRATEGIES[{strategy!r}]"
    )
