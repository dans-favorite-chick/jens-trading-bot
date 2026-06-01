# tests/warehouse/conftest.py
"""Shared pytest fixtures for warehouse tests."""
from __future__ import annotations
from pathlib import Path
import pytest
import duckdb

from tools.warehouse.db import apply_schema


@pytest.fixture
def db() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB with schema applied."""
    con = duckdb.connect(":memory:")
    apply_schema(con)
    yield con
    con.close()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
