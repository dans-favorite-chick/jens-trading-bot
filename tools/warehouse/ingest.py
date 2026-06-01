"""
tools.warehouse.ingest — Core ingestion library for the Phoenix backtest warehouse.

Public API:
    ingest_csv(csv_path, *, db_path, mark_friction_applied=None) -> IngestResult
    scan_dir(dir_path, *, db_path, glob="*.csv", recursive=False, logical_group=None,
             mark_friction_applied=None, dry_run=False) -> list[IngestResult]

All errors are caught per-file; a bad file never aborts a batch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import duckdb

from tools.warehouse import DB_PATH, ERROR_LOG, LOCK_PATH, SCHEMA_SQL
from tools.warehouse.lock import acquire_lock, release_lock
from tools.warehouse.sidecar import canonical_sidecar, friction_applied, load_sidecar
from tools.warehouse.sniff import (
    CsvKind,
    safe_import_table_name,
    sniff_kind,
    sniff_strategy_from_filename,
)

log = logging.getLogger(__name__)

# Columns present in either the legacy 13-col schema or the extended INVENTORY schema
SOURCE_COLS = {
    "strategy", "direction", "entry_ts", "entry_price",
    "stop_price", "target_price", "exit_ts", "exit_price",
    "exit_reason", "pnl_dollars", "pnl_ticks",
    "hold_min", "hold_minutes",   # hold_min is the legacy name
    "year", "mae_ticks", "mfe_ticks", "regime", "tod_bucket", "entry_context",
}

# Glob skip-list directories
SKIP_DIRS = {"tests", "fixtures", ".pytest_cache", "__pycache__", "node_modules"}


@dataclass
class IngestResult:
    csv_path: Path
    run_id: str | None
    status: Literal["inserted", "skipped_duplicate", "error"]
    csv_kind: str | None
    rows_inserted: int
    metrics_inserted: int
    error: str | None
    extra: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────

def ingest_csv(
    csv_path: Path,
    *,
    db_path: Path = DB_PATH,
    mark_friction_applied: bool | None = None,
    logical_group: str | None = None,
    skip_lock: bool = False,
    dry_run: bool = False,
) -> IngestResult:
    """Ingest a single CSV into the warehouse.

    Parameters
    ----------
    csv_path              : path to the CSV to ingest
    db_path               : path to the DuckDB file (override in tests)
    mark_friction_applied : CLI --mark-friction-applied flag override
    logical_group         : tag all runs in this batch with a group label
    skip_lock             : True if the caller already holds the PID lock
    dry_run               : report what would happen; make no DB writes
    """
    csv_path = Path(csv_path).resolve()
    if not csv_path.exists():
        return IngestResult(
            csv_path=csv_path, run_id=None, status="error",
            csv_kind=None, rows_inserted=0, metrics_inserted=0,
            error=f"file not found: {csv_path}",
        )

    lock = acquire_lock(LOCK_PATH, skip_lock=skip_lock)
    try:
        return _ingest_one(csv_path, db_path=db_path,
                           mark_friction_applied=mark_friction_applied,
                           logical_group=logical_group, dry_run=dry_run)
    finally:
        release_lock(LOCK_PATH, skip_lock=skip_lock)


def scan_dir(
    dir_path: Path,
    *,
    db_path: Path = DB_PATH,
    glob: str = "*.csv",
    recursive: bool = False,
    logical_group: str | None = None,
    mark_friction_applied: bool | None = None,
    dry_run: bool = False,
) -> list[IngestResult]:
    """Ingest all CSVs in a directory (one lock held for the whole batch)."""
    dir_path = Path(dir_path).resolve()

    pattern = f"**/{glob}" if recursive else glob
    files = sorted(dir_path.glob(pattern))

    # Filter skip-dirs
    def _in_skip_dir(p: Path) -> bool:
        return any(part in SKIP_DIRS for part in p.parts)

    files = [f for f in files if not _in_skip_dir(f)]

    if not files:
        log.info("scan_dir: no CSVs found in %s (glob=%r, recursive=%r)", dir_path, glob, recursive)
        return []

    results: list[IngestResult] = []
    acquire_lock(LOCK_PATH)
    try:
        for f in files:
            r = _ingest_one(f, db_path=db_path,
                            mark_friction_applied=mark_friction_applied,
                            logical_group=logical_group, dry_run=dry_run,
                            skip_lock=True)
            results.append(r)
            _log_result(r)
    finally:
        release_lock(LOCK_PATH)

    return results


# ──────────────────────────────────────────────────────────────
# Core per-file pipeline
# ──────────────────────────────────────────────────────────────

def _ingest_one(
    csv_path: Path,
    *,
    db_path: Path,
    mark_friction_applied: bool | None,
    logical_group: str | None,
    dry_run: bool,
    skip_lock: bool = False,
) -> IngestResult:
    """Internal single-file ingest. Caller must hold the lock."""
    # ── 1. Load sidecar (may raise ValueError on unknown schema_version) ──
    try:
        sidecar = load_sidecar(csv_path)
    except ValueError as exc:
        err = str(exc)
        _log_error(csv_path, "sidecar_schema_mismatch", err)
        return IngestResult(csv_path=csv_path, run_id=None, status="error",
                            csv_kind=None, rows_inserted=0, metrics_inserted=0, error=err)

    # ── 2. Compute content hash ──
    csv_bytes = csv_path.read_bytes()
    sc_bytes  = canonical_sidecar(sidecar)
    run_id    = hashlib.sha256(csv_bytes + b"\n" + sc_bytes).hexdigest()

    # ── 3. Sniff kind ──
    kind, header = sniff_kind(csv_path)
    if kind == "error":
        err = "unknown_csv_kind"
        _log_error(csv_path, err, f"header={header}")
        return IngestResult(csv_path=csv_path, run_id=None, status="error",
                            csv_kind=None, rows_inserted=0, metrics_inserted=0, error=err)

    if dry_run:
        log.info("DRY RUN  %s  kind=%s  run_id=%s...", csv_path.name, kind, run_id[:12])
        return IngestResult(csv_path=csv_path, run_id=run_id, status="inserted",
                            csv_kind=kind, rows_inserted=0, metrics_inserted=0, error=None,
                            extra={"dry_run": True})

    # ── 4. Connect to DB, apply schema ──
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    _ensure_schema(con)

    try:
        # ── 5. Dedup check ──
        hit = con.execute("SELECT 1 FROM runs WHERE run_id = ?", [run_id]).fetchone()
        if hit:
            log.debug("skipped_duplicate %s  run_id=%s...", csv_path.name, run_id[:12])
            return IngestResult(csv_path=csv_path, run_id=run_id, status="skipped_duplicate",
                                csv_kind=kind, rows_inserted=0, metrics_inserted=0, error=None)

        # ── 6. Ingest in transaction ──
        rows_inserted   = 0
        metrics_inserted = 0

        con.begin()
        try:
            _insert_runs_row(con, csv_path, run_id, kind, sidecar,
                             logical_group=logical_group,
                             mark_friction_applied=mark_friction_applied)

            if kind == "trades":
                meta_updates: dict | None = None
                if "sub_name" in set(header) and "strategy" not in set(header):
                    # opening_session_sub_breakdown has a sub_name column that carries
                    # per-row breakdown data; capture its presence in the run metadata
                    # so future queries know the variant breakdown exists in the source.
                    meta_updates = {"sub_name_column_present": True}
                rows_inserted = _ingest_trades(
                    con, csv_path, run_id, header, meta_updates=meta_updates
                )
            elif kind == "wfa_windows":
                rows_inserted = _ingest_wfa_windows(con, csv_path, run_id)
            elif kind == "wfa_summary":
                rows_inserted = _ingest_wfa_summary(con, csv_path, run_id)
            elif kind == "summary":
                metrics_inserted = _ingest_summary(con, csv_path, run_id, header)
            elif kind == "mixed":
                rows_inserted, metrics_inserted = _ingest_mixed(con, csv_path, run_id, header)
            elif kind == "derived":
                rows_inserted = _ingest_derived(con, csv_path, run_id)

            con.commit()
        except Exception:
            con.rollback()
            raise

        return IngestResult(csv_path=csv_path, run_id=run_id, status="inserted",
                            csv_kind=kind, rows_inserted=rows_inserted,
                            metrics_inserted=metrics_inserted, error=None)

    except Exception as exc:
        import traceback
        err = str(exc)
        _log_error(csv_path, type(exc).__name__, err, traceback=traceback.format_exc())
        return IngestResult(csv_path=csv_path, run_id=None, status="error",
                            csv_kind=kind, rows_inserted=0, metrics_inserted=0, error=err)
    finally:
        con.close()


# ──────────────────────────────────────────────────────────────
# Schema bootstrap
# ──────────────────────────────────────────────────────────────

_schema_applied: set[str] = set()   # keyed by db_path str; avoid re-applying per connection

def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    sql = SCHEMA_SQL.read_text()
    con.execute(sql)


# ──────────────────────────────────────────────────────────────
# runs row insertion
# ──────────────────────────────────────────────────────────────

def _insert_runs_row(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    run_id: str,
    kind: CsvKind,
    sidecar: dict,
    *,
    logical_group: str | None,
    mark_friction_applied: bool | None,
) -> None:
    meta = sidecar.get("meta", {})
    sidecar_missing = meta.get("sidecar_missing", False)

    strategy = sidecar.get("strategy") or sniff_strategy_from_filename(csv_path)
    params    = sidecar.get("params")
    code_sha  = sidecar.get("code_sha")
    seed      = sidecar.get("seed")

    def _parse_ts(val):
        if val is None:
            return None
        if isinstance(val, str):
            val = val.replace("Z", "+00:00")
            return datetime.fromisoformat(val)
        return val

    lookback_start = _parse_ts(sidecar.get("lookback_start"))
    lookback_end   = _parse_ts(sidecar.get("lookback_end"))
    fa             = friction_applied(sidecar, cli_override=mark_friction_applied)
    lg             = logical_group or sidecar.get("logical_group")

    # Attach ingest meta to sidecar_raw
    sidecar_with_meta = dict(sidecar)
    sidecar_with_meta["meta"] = {
        **meta,
        "ingested_by": "tools.warehouse.ingest",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    sidecar_json = json.dumps(sidecar_with_meta, default=str)

    con.execute(
        """
        INSERT INTO runs
            (run_id, source_filename, csv_kind, logical_group, strategy, params,
             code_sha, seed, lookback_start, lookback_end, friction_applied, sidecar_raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            csv_path.name,
            kind,
            lg,
            strategy,
            json.dumps(params, default=str) if params is not None else None,
            code_sha,
            seed,
            lookback_start,
            lookback_end,
            fa,
            sidecar_json,
        ],
    )


# ──────────────────────────────────────────────────────────────
# Per-kind ingest helpers
# ──────────────────────────────────────────────────────────────

def _col_or_null(name: str, present: set[str], *, fallback: str | None = None) -> str:
    if name in present:
        return name
    if fallback and fallback in present:
        return fallback
    return "NULL"


def strategy_from_filename(csv_path: Path) -> str | None:
    """Derive a strategy name from the CSV filename for trade-shape files that lack a
    'strategy' column.

    Returns a string literal to splice into the SELECT, or None if no rule matches
    (caller should fall back to the existing error path).
    """
    name = csv_path.name.lower()
    if name.startswith("opening_session_sub_breakdown"):
        return "opening_session"
    if name.startswith("backtest_v3_trades"):
        return "backtest_v3"
    if name.startswith("phoenix_sr_confluence_per_trade"):
        return "sr_confluence"
    return None


def _ingest_trades(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    run_id: str,
    header: list[str],
    *,
    meta_updates: dict | None = None,
) -> int:
    """Ingest a trades-kind CSV.

    If the 'strategy' column is missing, attempts to derive it from the filename via
    strategy_from_filename(). Raises if direction is missing or no strategy can be
    derived.

    meta_updates: if provided, extra keys are merged into the run's sidecar_raw.meta
    JSON in the runs table (used to record variant column presence, etc.).
    """
    present = set(header)
    present_source = present & SOURCE_COLS
    csv_str = _safe_csv_path(csv_path)

    # --- direction sanity check ---
    if "direction" not in present:
        raise ValueError(
            f"trades CSV is missing required 'direction' column: {csv_path.name}"
        )

    # --- strategy resolution ---
    if "strategy" in present:
        strategy_expr = "strategy"
    else:
        derived_strategy = strategy_from_filename(csv_path)
        if derived_strategy is None:
            # No rule matched — let DuckDB raise its original BinderException so the
            # error message is meaningful.
            strategy_expr = "strategy"  # will trigger BinderException as before
        else:
            strategy_expr = f"'{derived_strategy}'"
            log.info(
                "trades CSV %s missing 'strategy' column; derived from filename: %r",
                csv_path.name, derived_strategy,
            )

    # --- optional meta update (e.g. sub_name_column_present) ---
    if meta_updates:
        _merge_run_meta(con, run_id, meta_updates)

    def col(name, *, fallback=None):
        return _col_or_null(name, present_source, fallback=fallback)

    entry_context_expr = (
        "TRY_CAST(entry_context AS JSON)"
        if "entry_context" in present_source
        else "NULL"
    )

    sql = f"""
    INSERT INTO trades
    SELECT
        ? AS run_id,
        {strategy_expr}                                    AS strategy,
        upper(direction)                                    AS direction,
        entry_ts,
        entry_price,
        {col("stop_price")}                                AS stop_price,
        {col("target_price")}                              AS target_price,
        {col("exit_ts")}                                   AS exit_ts,
        {col("exit_price")}                                AS exit_price,
        {col("exit_reason")}                               AS exit_reason,
        {col("pnl_dollars")}                               AS pnl_dollars,
        {col("pnl_ticks")}                                 AS pnl_ticks,
        {col("hold_minutes", fallback="hold_min")}         AS hold_minutes,
        {col("year")}                                      AS year,
        {col("mae_ticks")}                                 AS mae_ticks,
        {col("mfe_ticks")}                                 AS mfe_ticks,
        {col("regime")}                                    AS regime,
        {col("tod_bucket")}                                AS tod_bucket,
        {entry_context_expr}                               AS entry_context
    -- DuckDB 1.5.3: an explicit `timestampformat='%Y-%m-%d %H:%M:%S%z'` here strips
    -- the +00:00 offset and stores the value as a naive timestamp (verified 2026-05-31).
    -- Auto-detection handles `+00:00` correctly. Do not add `timestampformat` back.
    FROM read_csv_auto({csv_str}, header=true)
    """
    con.execute(sql, [run_id])
    count = con.execute("SELECT COUNT(*) FROM trades WHERE run_id = ?", [run_id]).fetchone()[0]
    return count


def _ingest_wfa_windows(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    run_id: str,
) -> int:
    """Row-by-row insert (WFA files are small; avoids all_varchar+column-ref DuckDB quirk)."""
    import csv as csv_mod
    count = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv_mod.DictReader(fh)
        for row in reader:
            def _f(k): return _safe_float(row.get(k))
            def _i(k): return _safe_int(row.get(k))
            def _d(k): return _safe_date(row.get(k))
            def _b(k): return _safe_bool(row.get(k))

            bp_raw = (row.get("best_params") or "").strip()
            try:
                best_params = json.dumps(json.loads(bp_raw))
            except (json.JSONDecodeError, ValueError):
                best_params = json.dumps({"_raw": bp_raw}) if bp_raw else None

            con.execute(
                """INSERT INTO wfa_windows
                   (run_id,strategy,window_idx,is_start,is_end,oos_start,oos_end,
                    best_params,is_pf,is_trades,oos_pf,oos_trades,oos_net,wfe,degraded)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [run_id, row.get("strategy"), _i("window_idx"),
                 _d("is_start"), _d("is_end"), _d("oos_start"), _d("oos_end"),
                 best_params, _f("is_pf"), _i("is_trades"), _f("oos_pf"),
                 _i("oos_trades"), _f("oos_net"), _f("wfe"), _b("degraded")],
            )
            count += 1
    return count


def _ingest_wfa_summary(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    run_id: str,
) -> int:
    """Row-by-row insert for wfa_summary (small file; avoids all_varchar quirk)."""
    import csv as csv_mod
    count = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv_mod.DictReader(fh)
        for row in reader:
            def _f(k): return _safe_float(row.get(k))
            def _i(k): return _safe_int(row.get(k))
            def _b(k): return _safe_bool(row.get(k))
            con.execute(
                """INSERT INTO wfa_summary
                   (run_id,strategy,n_windows,mean_is_pf,mean_oos_pf,
                    median_oos_pf,pct_windows_degraded,robust)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [run_id, row.get("strategy"), _i("n_windows"),
                 _f("mean_is_pf"), _f("mean_oos_pf"), _f("median_oos_pf"),
                 _f("pct_windows_degraded"), _b("robust")],
            )
            count += 1
    return count


# ── scalar coercion helpers ──────────────────────────────────────

def _safe_float(val) -> float | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None

def _safe_int(val) -> int | None:
    f = _safe_float(val)
    return int(f) if f is not None else None

def _safe_date(val) -> str | None:
    if val is None or str(val).strip() == "":
        return None
    return str(val).strip()

def _safe_bool(val) -> bool | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None




def _ingest_summary(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    run_id: str,
    header: list[str],
) -> int:
    """Unpivot summary CSV → run_metrics rows.

    metric_name is namespaced as "<strategy_or_name>.<col>" to avoid PRIMARY KEY
    collisions when a summary CSV contains multiple strategy rows.
    """
    import csv as csv_mod
    # Determine the identity column
    id_col = None
    for candidate in ("strategy", "name"):
        if candidate in header:
            id_col = candidate
            break
    metric_cols = [c for c in header if c not in ("strategy", "name")]
    inserted = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv_mod.DictReader(fh)
        for row in reader:
            row_id = row.get(id_col, "").strip() if id_col else ""
            for col in metric_cols:
                val = row.get(col, "").strip()
                try:
                    numeric = float(val)
                    label   = None
                except (ValueError, TypeError):
                    numeric = None
                    label   = val or None
                # Namespace metric_name to avoid PK collisions across strategy rows
                metric_name = f"{row_id}.{col}" if row_id else col
                con.execute(
                    "INSERT OR IGNORE INTO run_metrics (run_id, metric_name, metric_value, label_value) VALUES (?, ?, ?, ?)",
                    [run_id, metric_name, numeric, label],
                )
                inserted += 1
    return inserted


def _ingest_mixed(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    run_id: str,
    header: list[str],
) -> tuple[int, int]:
    """Mixed file: ingest trades AND aggregate metrics from row-0 constants.

    Validates per spec §5.3 that the aggregate columns are constant across all
    rows; raises ValueError if any aggregate column varies. The orchestrator's
    per-file transaction catches this and produces status='error'.
    """
    import csv as csv_mod
    from tools.warehouse.sniff import AGGREGATE_METRIC_COLS

    agg_cols = [c for c in header if c in AGGREGATE_METRIC_COLS]

    # Validate constancy of aggregate columns BEFORE inserting any trades
    if agg_cols:
        seen: dict[str, str] = {}
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv_mod.DictReader(fh):
                for col in agg_cols:
                    val = (row.get(col) or "").strip()
                    if col in seen:
                        if seen[col] != val:
                            raise ValueError(
                                f"mixed-kind CSV has non-constant aggregate column "
                                f"{col!r} in {csv_path} ({seen[col]!r} vs {val!r})"
                            )
                    else:
                        seen[col] = val

    trades_n  = _ingest_trades(con, csv_path, run_id, header)

    metrics_n = 0
    if agg_cols:
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv_mod.DictReader(fh):
                for col in agg_cols:
                    val = (row.get(col) or "").strip()
                    try:
                        numeric = float(val)
                        label   = None
                    except (ValueError, TypeError):
                        numeric = None
                        label   = val or None
                    con.execute(
                        "INSERT OR IGNORE INTO run_metrics (run_id, metric_name, metric_value, label_value) VALUES (?, ?, ?, ?)",
                        [run_id, col, numeric, label],
                    )
                    metrics_n += 1
                break  # row 0 only

    return trades_n, metrics_n


def _merge_run_meta(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    updates: dict,
) -> None:
    """Merge extra key-value pairs into the runs.sidecar_raw.meta JSON for run_id.

    This is used to record variant column presence (e.g. sub_name_column_present)
    that doesn't fit into the main trades schema but is useful for future queries.
    """
    row = con.execute(
        "SELECT sidecar_raw FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    if row is None:
        return
    raw = json.loads(row[0]) if row[0] else {}
    raw.setdefault("meta", {}).update(updates)
    con.execute(
        "UPDATE runs SET sidecar_raw = ? WHERE run_id = ?",
        [json.dumps(raw, default=str), run_id],
    )


def _ingest_derived(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    run_id: str,
) -> int:
    """Ingest a convenience/derived CSV into import_<safe_name> table.

    If the file is essentially empty (size < 16 bytes / no real header), records
    empty_file=True in the run's sidecar_raw meta and returns 0 without creating
    an import_* table.
    """
    import csv as csv_mod

    # --- Task C: empty-file guard ---
    file_size = csv_path.stat().st_size
    if file_size < 16:
        # File is essentially empty (just CRLF or blank). Record and skip gracefully.
        _merge_run_meta(con, run_id, {"empty_file": True})
        log.info("derived CSV %s is empty (%d bytes); skipping import table creation",
                 csv_path.name, file_size)
        return 0

    table = safe_import_table_name(csv_path)
    csv_str = _safe_csv_path(csv_path)

    # Read incoming CSV header
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv_mod.reader(fh)
        csv_header = [c.strip().lower() for c in next(reader)]

    # Create table lazily from schema inference (no data)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} AS
        SELECT *, NULL::VARCHAR AS run_id
        FROM read_csv_auto({csv_str}, header=true)
        WHERE 1=0
    """)

    # Fetch existing columns
    existing_cols = {
        row[0].lower()
        for row in con.execute(f"DESCRIBE {table}").fetchall()
    }

    # Destructive-drift check: refuse if any existing non-run_id column is missing
    incoming_set = set(csv_header)
    data_cols_existing = existing_cols - {"run_id"}
    removed = data_cols_existing - incoming_set
    if removed:
        raise ValueError(
            f"derived schema drift: column(s) removed {sorted(removed)} in {csv_path}; "
            f"drop the import table manually if you want to re-import"
        )

    # Handle additive column drift (new columns in incoming CSV)
    for col in csv_header:
        if col not in existing_cols and col != "run_id":
            con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} VARCHAR")

    result = con.execute(f"""
        INSERT INTO {table}
        SELECT *, ? AS run_id
        FROM read_csv_auto({csv_str}, header=true)
    """, [run_id])
    count = result.fetchone()[0]
    return count


# ──────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────

def _safe_csv_path(path: Path) -> str:
    """Return a DuckDB-safe single-quoted path string for use in SQL."""
    escaped = str(path).replace("'", "''")
    return f"'{escaped}'"


def _log_error(csv_path: Path, error_class: str, error_msg: str, *, traceback: str = "") -> None:
    import traceback as tb_mod
    record = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "level":       "error",
        "file":        str(csv_path),
        "error_class": error_class,
        "error":       error_msg,
    }
    if traceback:
        record["traceback"] = traceback
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    log.error("ingest error [%s] %s: %s", error_class, csv_path.name, error_msg)


def _log_result(r: IngestResult) -> None:
    if r.status == "inserted":
        log.info("✅ %s  kind=%-12s  rows=%d  metrics=%d  run_id=%s...",
                 r.csv_path.name, r.csv_kind, r.rows_inserted, r.metrics_inserted,
                 (r.run_id or "")[:12])
    elif r.status == "skipped_duplicate":
        log.info("⏭  %s  skipped_duplicate", r.csv_path.name)
    else:
        log.warning("❌ %s  error=%s", r.csv_path.name, r.error)
