"""
tools.warehouse — Phoenix Backtest Warehouse (Phase 1)

Public API:
    from tools.warehouse.ingest import ingest_csv, scan_dir, IngestResult

CLI:
    python -m tools.warehouse ingest <path>
    python -m tools.warehouse status
"""

from pathlib import Path

WAREHOUSE_DIR = Path(__file__).parent.parent.parent / "data" / "warehouse"
DB_PATH       = WAREHOUSE_DIR / "phoenix.duckdb"
LOCK_PATH     = WAREHOUSE_DIR / ".ingest.lock"
ERROR_LOG     = WAREHOUSE_DIR / "ingest_errors.log"
SCHEMA_SQL    = Path(__file__).parent / "schema.sql"
