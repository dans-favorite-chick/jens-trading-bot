"""Database connection and schema helpers.

Thin facade over `_ensure_schema()` in ingest.py and direct DuckDB connection.
Exists primarily so test code can do `from tools.warehouse.db import apply_schema`.
"""
from __future__ import annotations
from pathlib import Path
import duckdb

from tools.warehouse.ingest import _ensure_schema


def open_db(db_path: Path | str = ":memory:") -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection. Caller is responsible for closing."""
    return duckdb.connect(str(db_path))


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Apply schema.sql to the given connection. Idempotent."""
    _ensure_schema(con)
