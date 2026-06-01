"""
tests/warehouse/test_ingest.py

Layer 1 fixture-based unit tests for the Phoenix backtest warehouse.
All tests run against duckdb.connect(':memory:') — fast, isolated, zero cleanup.

Run: pytest tests/warehouse/test_ingest.py -v
Smoke test (real CSVs): pytest tests/warehouse/test_ingest.py -v -m smoke
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

# ── Fixtures directory ──
FIXTURES = Path(__file__).parent / "fixtures"

# ── Helpers ──

def make_db() -> duckdb.DuckDBPyConnection:
    """Return a fresh in-memory DuckDB with the warehouse schema applied."""
    from tools.warehouse import SCHEMA_SQL
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA_SQL.read_text())
    return con


def ingest_fixture(filename: str, *, sidecar: bool = True, **kwargs):
    """Run ingest_csv against a fixture file using an in-memory DB."""
    from tools.warehouse.ingest import ingest_csv, _ensure_schema
    import duckdb as _duckdb

    csv_path = FIXTURES / filename

    # Patch DB_PATH and LOCK_PATH to avoid touching real files
    tmp_db = Path("/tmp") / f"test_{filename}.duckdb"
    tmp_lock = Path("/tmp") / f"test_{filename}.lock"

    with patch("tools.warehouse.ingest.DB_PATH", tmp_db), \
         patch("tools.warehouse.ingest.LOCK_PATH", tmp_lock), \
         patch("tools.warehouse.lock.LOCK_PATH", tmp_lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", Path("/tmp/test_ingest_errors.log")):
        result = ingest_csv(csv_path, db_path=tmp_db, **kwargs)

    # Cleanup temp files
    for p in [tmp_db, tmp_lock, Path(str(tmp_db) + ".wal")]:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════════
# 1. Happy-path tests
# ═══════════════════════════════════════════════════════════════

class TestTradesLegacy:
    """Legacy 13-col CSV (no sidecar) → friction_applied=false, extended cols NULL."""

    def test_status_inserted(self):
        r = ingest_fixture("trades_legacy_3rows.csv")
        assert r.status == "inserted", r.error

    def test_rows_inserted(self):
        r = ingest_fixture("trades_legacy_3rows.csv")
        assert r.rows_inserted == 3

    def test_csv_kind(self):
        r = ingest_fixture("trades_legacy_3rows.csv")
        assert r.csv_kind == "trades"

    def test_friction_applied_false_for_legacy(self, tmp_path):
        """No sidecar → friction_applied must be False."""
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        row = con.execute("SELECT friction_applied FROM runs").fetchone()
        con.close()
        assert row[0] is False

    def test_extended_cols_null(self, tmp_path):
        """Legacy CSV → mae_ticks / mfe_ticks / regime / tod_bucket all NULL."""
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        rows = con.execute("SELECT mae_ticks, mfe_ticks, regime, tod_bucket FROM trades").fetchall()
        con.close()
        for row in rows:
            assert all(v is None for v in row), f"Expected all NULL, got {row}"

    def test_direction_normalized_upper(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        dirs = {row[0] for row in con.execute("SELECT DISTINCT direction FROM trades").fetchall()}
        con.close()
        assert dirs <= {"LONG", "SHORT"}, f"Non-uppercase direction found: {dirs}"


class TestTradesMacro:
    """Extended schema CSV with sidecar → friction_applied=true, extended cols populated."""

    def test_status_inserted(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "trades_macro_3rows.csv", db_path=db)
        assert r.status == "inserted", r.error
        assert r.rows_inserted == 3

    def test_friction_applied_true_with_sidecar(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_macro_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        row = con.execute("SELECT friction_applied FROM runs").fetchone()
        con.close()
        assert row[0] is True

    def test_extended_cols_populated(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_macro_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        rows = con.execute("SELECT mae_ticks, mfe_ticks, regime, tod_bucket FROM trades").fetchall()
        con.close()
        for row in rows:
            assert all(v is not None for v in row), f"Expected all populated, got {row}"


class TestIdempotency:
    """Ingest same file twice → second run is skipped_duplicate, row count unchanged."""

    def test_duplicate_skipped(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"

        kwargs = dict(db_path=db)
        patches = dict(
            db=patch("tools.warehouse.ingest.DB_PATH", db),
            lock1=patch("tools.warehouse.ingest.LOCK_PATH", lock),
            lock2=patch("tools.warehouse.lock.LOCK_PATH", lock),
            err=patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"),
        )
        with patches["db"], patches["lock1"], patches["lock2"], patches["err"]:
            r1 = ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)
            r2 = ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)

        assert r1.status == "inserted"
        assert r2.status == "skipped_duplicate"

    def test_row_count_unchanged_after_duplicate(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"

        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)
            ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        count = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        runs  = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        con.close()
        assert count == 3   # 3 rows, not 6
        assert runs  == 1   # 1 run, not 2


# ═══════════════════════════════════════════════════════════════
# 2. trades_ct view — UTC → CT correctness
# ═══════════════════════════════════════════════════════════════

class TestTradesCtView:
    """market_open_minutes: 08:30 CT → 0, 09:00 CT → 30, Globex pre-market → negative."""

    def _load_trades_ct(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_macro_3rows.csv", db_path=db)
        return duckdb.connect(str(db))

    def test_market_open_minutes_at_open(self, tmp_path):
        """08:30 CT (= 13:30 UTC) → market_open_minutes == 0."""
        con = self._load_trades_ct(tmp_path)
        # trades_macro_3rows.csv row 1: entry_ts = 2024-08-15 13:30:00+00:00
        rows = con.execute(
            "SELECT market_open_minutes FROM trades_ct "
            "WHERE entry_ts = '2024-08-15 13:30:00+00:00'::TIMESTAMPTZ"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        assert abs(rows[0][0]) < 0.01, f"Expected 0, got {rows[0][0]}"

    def test_market_open_minutes_30_in(self, tmp_path):
        """09:00 CT (= 14:00 UTC) → market_open_minutes ≈ 30."""
        con = self._load_trades_ct(tmp_path)
        rows = con.execute(
            "SELECT market_open_minutes FROM trades_ct "
            "WHERE entry_ts = '2024-08-15 14:15:00+00:00'::TIMESTAMPTZ"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        assert abs(rows[0][0] - 45.0) < 0.5, f"Expected ~45, got {rows[0][0]}"

    def test_globex_overnight_negative(self, tmp_path):
        """Pre-market Globex trade (05:00 UTC = 00:00 CT) → market_open_minutes negative."""
        # Uses legacy fixture: entry_ts 2021-05-17 05:00:00+00:00 = 00:00 CT
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "trades_legacy_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        rows = con.execute(
            "SELECT market_open_minutes FROM trades_ct "
            "WHERE entry_ts = '2021-05-17 05:00:00+00:00'::TIMESTAMPTZ"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        assert rows[0][0] < 0, f"Expected negative (pre-market), got {rows[0][0]}"

    def test_session_date_column_exists(self, tmp_path):
        con = self._load_trades_ct(tmp_path)
        cols = [row[0] for row in con.execute("DESCRIBE trades_ct").fetchall()]
        con.close()
        assert "session_date" in cols
        assert "market_open_minutes" in cols


# ═══════════════════════════════════════════════════════════════
# 3. WFA tables
# ═══════════════════════════════════════════════════════════════

class TestWfaWindows:
    def test_ingest_wfa_windows(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "wfa_windows_3rows.csv", db_path=db)
        assert r.status == "inserted", r.error
        assert r.rows_inserted == 3
        assert r.csv_kind == "wfa_windows"

    def test_best_params_json_roundtrip(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "wfa_windows_3rows.csv", db_path=db)

        con = duckdb.connect(str(db))
        rows = con.execute("SELECT best_params FROM wfa_windows").fetchall()
        con.close()
        for row in rows:
            parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            assert isinstance(parsed, dict), f"best_params not a dict: {row[0]}"


class TestWfaWindowsFilenameSniff:
    """wfa_windows_p13_inside_bar_3rows.csv — exercises WFA filename strategy sniff."""

    def test_ingest_p13_shard(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "wfa_windows_p13_inside_bar_3rows.csv", db_path=db)
        assert r.status == "inserted", r.error
        assert r.rows_inserted == 3


class TestWfaSummary:
    def test_ingest_wfa_summary(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "wfa_summary_3rows.csv", db_path=db)
        assert r.status == "inserted", r.error
        assert r.rows_inserted == 3
        assert r.csv_kind == "wfa_summary"


# ═══════════════════════════════════════════════════════════════
# 4. Summary → run_metrics unpivot
# ═══════════════════════════════════════════════════════════════

class TestSummaryUnpivot:
    def test_summary_inserts_metrics(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "summary_5cols.csv", db_path=db)
        assert r.status == "inserted", r.error
        assert r.csv_kind == "summary"
        assert r.metrics_inserted > 0

    def test_metric_names_correct(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "summary_5cols.csv", db_path=db)

        con = duckdb.connect(str(db))
        names = {row[0] for row in con.execute("SELECT DISTINCT metric_name FROM run_metrics").fetchall()}
        con.close()
        # metric_name is namespaced as "<strategy>.<col>" to avoid PK collisions
        assert any("profit_factor" in n for n in names), f"profit_factor not found in {names}"
        assert any("win_rate" in n for n in names), f"win_rate not found in {names}"


# ═══════════════════════════════════════════════════════════════
# 5. Error paths (spec §7)
# ═══════════════════════════════════════════════════════════════

class TestErrorPaths:
    def test_unknown_csv_kind(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "unknown_header.csv", db_path=db)
        assert r.status == "error"
        assert "unknown_csv_kind" in (r.error or "")

    def test_bad_sidecar_parse_continues(self, tmp_path):
        """Sidecar JSON parse failure → treat as sidecar_missing, ingest continues."""
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "bad_sidecar.csv", db_path=db)
        # Should still ingest (sidecar treated as missing)
        assert r.status == "inserted", f"Expected inserted, got {r.status}: {r.error}"

    def test_friction_false_when_sidecar_unparseable(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / "bad_sidecar.csv", db_path=db)

        con = duckdb.connect(str(db))
        row = con.execute("SELECT friction_applied FROM runs").fetchone()
        con.close()
        assert row[0] is False

    def test_file_not_found(self, tmp_path):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            r = ingest_csv(FIXTURES / "does_not_exist.csv", db_path=db)
        assert r.status == "error"


# ═══════════════════════════════════════════════════════════════
# 6. friction_applied variants (spec §5.5)
# ═══════════════════════════════════════════════════════════════

class TestFrictionApplied:
    """Three sidecar scenarios: no sidecar, sidecar with friction, sidecar without friction."""

    def _ingest(self, tmp_path, csv_name, **kwargs):
        from tools.warehouse.ingest import ingest_csv
        db = tmp_path / "test.duckdb"
        lock = tmp_path / ".lock"
        with patch("tools.warehouse.ingest.DB_PATH", db), \
             patch("tools.warehouse.ingest.LOCK_PATH", lock), \
             patch("tools.warehouse.lock.LOCK_PATH", lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
            ingest_csv(FIXTURES / csv_name, db_path=db, **kwargs)
        con = duckdb.connect(str(db))
        row = con.execute("SELECT friction_applied FROM runs LIMIT 1").fetchone()
        con.close()
        return row[0]

    def test_no_sidecar_false(self, tmp_path):
        assert self._ingest(tmp_path, "trades_legacy_3rows.csv") is False

    def test_sidecar_with_friction_true(self, tmp_path):
        assert self._ingest(tmp_path, "trades_macro_3rows.csv") is True

    def test_cli_override_forces_true(self, tmp_path):
        # Legacy CSV (no sidecar) + --mark-friction-applied → True
        assert self._ingest(tmp_path, "trades_legacy_3rows.csv", mark_friction_applied=True) is True


# ═══════════════════════════════════════════════════════════════
# 7. Lock semantics
# ═══════════════════════════════════════════════════════════════

class TestLock:
    def test_lock_created_and_released(self, tmp_path):
        from tools.warehouse.lock import acquire_lock, release_lock
        lock_path = tmp_path / ".lock"
        acquire_lock(lock_path)
        assert lock_path.exists()
        release_lock(lock_path)
        assert not lock_path.exists()

    def test_lock_released_in_finally_after_exception(self, tmp_path):
        """Lock file must be gone even when an exception fires inside the guarded block."""
        from tools.warehouse.lock import acquire_lock, release_lock
        lock_path = tmp_path / ".lock"
        try:
            acquire_lock(lock_path)
            assert lock_path.exists()
            raise RuntimeError("deliberate test exception")
        except RuntimeError:
            pass
        finally:
            release_lock(lock_path)

        assert not lock_path.exists(), "Lock file was NOT cleaned up in finally block"

    def test_stale_pid_detected_and_recovered(self, tmp_path):
        """Stale PID (dead process) → lock is overwritten, not rejected."""
        import json
        from tools.warehouse.lock import acquire_lock, release_lock

        lock_path = tmp_path / ".lock"
        # Write a lock with a guaranteed-dead PID (PID 1 is init; we can't kill it,
        # but PID 99999999 almost certainly doesn't exist)
        stale = {"pid": 99999999, "host": socket.gethostname(), "started_at": "2000-01-01T00:00:00Z"}
        lock_path.write_text(json.dumps(stale))

        with patch("psutil.pid_exists", return_value=False):
            acquire_lock(lock_path)   # Should NOT raise

        assert lock_path.exists()
        release_lock(lock_path)

    def test_live_lock_raises(self, tmp_path):
        """Live PID on same host → RuntimeError."""
        import json
        from tools.warehouse.lock import acquire_lock

        lock_path = tmp_path / ".lock"
        live = {"pid": os.getpid(), "host": socket.gethostname(), "started_at": "2026-01-01T00:00:00Z"}
        lock_path.write_text(json.dumps(live))

        with pytest.raises(RuntimeError, match="another ingest is running"):
            acquire_lock(lock_path)


# ═══════════════════════════════════════════════════════════════
# 8. Sniffer unit tests
# ═══════════════════════════════════════════════════════════════

class TestSniff:
    def test_sniff_trades_legacy(self):
        from tools.warehouse.sniff import sniff_kind
        kind, _ = sniff_kind(FIXTURES / "trades_legacy_3rows.csv")
        assert kind == "trades"

    def test_sniff_trades_macro(self):
        from tools.warehouse.sniff import sniff_kind
        kind, _ = sniff_kind(FIXTURES / "trades_macro_3rows.csv")
        assert kind == "trades"

    def test_sniff_wfa_windows(self):
        from tools.warehouse.sniff import sniff_kind
        kind, _ = sniff_kind(FIXTURES / "wfa_windows_3rows.csv")
        assert kind == "wfa_windows"

    def test_sniff_wfa_summary(self):
        from tools.warehouse.sniff import sniff_kind
        kind, _ = sniff_kind(FIXTURES / "wfa_summary_3rows.csv")
        assert kind == "wfa_summary"

    def test_sniff_summary(self):
        from tools.warehouse.sniff import sniff_kind
        kind, _ = sniff_kind(FIXTURES / "summary_5cols.csv")
        assert kind == "summary"

    def test_sniff_mixed(self):
        from tools.warehouse.sniff import sniff_kind
        kind, _ = sniff_kind(FIXTURES / "mixed_summary_with_trade_row.csv")
        assert kind == "mixed"

    def test_sniff_unknown(self):
        from tools.warehouse.sniff import sniff_kind
        kind, _ = sniff_kind(FIXTURES / "unknown_header.csv")
        assert kind == "error"

    def test_wfa_p13_filename_sniff_no_config(self):
        """Without config/strategies.py loaded, sniff returns None (safe fallback)."""
        from tools.warehouse.sniff import sniff_strategy_from_filename
        with patch("tools.warehouse.sniff.get_known_strategies", return_value=frozenset()):
            result = sniff_strategy_from_filename(
                Path("wfa_windows_p13_inside_bar.csv")
            )
        assert result is None  # no known strategies → NULL is safe

    def test_safe_import_table_name(self):
        from tools.warehouse.sniff import safe_import_table_name
        assert safe_import_table_name(Path("phase1_strategy_summary.csv")) == "import_phase1_strategy_summary"
        assert safe_import_table_name(Path("microstructure_lift.csv")) == "import_microstructure_lift"


# ═══════════════════════════════════════════════════════════════
# 9. Smoke test — real portfolio_framework CSVs
# ═══════════════════════════════════════════════════════════════

PORTFOLIO_DIR = Path(__file__).parent.parent.parent / "backtest_results" / "portfolio_framework"

@pytest.mark.smoke
class TestSmoke:
    """Smoke tests against real CSV files. Run with: pytest -m smoke"""

    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path):
        self.db = tmp_path / "smoke_phoenix.duckdb"
        self.lock = tmp_path / ".smoke_lock"
        self.err_log = tmp_path / "smoke_errors.log"

    def _ingest_dir(self):
        from tools.warehouse.ingest import scan_dir
        with patch("tools.warehouse.ingest.DB_PATH", self.db), \
             patch("tools.warehouse.ingest.LOCK_PATH", self.lock), \
             patch("tools.warehouse.lock.LOCK_PATH", self.lock), \
             patch("tools.warehouse.ingest.ERROR_LOG", self.err_log):
            return scan_dir(PORTFOLIO_DIR, db_path=self.db,
                            logical_group="portfolio_framework")

    def test_all_csvs_insert_or_duplicate(self):
        results = self._ingest_dir()
        errors = [r for r in results if r.status == "error"]
        assert not errors, f"Ingest errors: {[(r.csv_path.name, r.error) for r in errors]}"

    def test_trade_count_floor(self):
        self._ingest_dir()
        con = duckdb.connect(str(self.db))
        count = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        con.close()
        assert count >= 70_000, f"Trade count {count} below floor of 70,000"

    def test_wfa_windows_floor(self):
        # 2026-06-01: floor lowered 210 -> 30. The prior 210-row floor
        # corresponded to the stale pre-2026-06-01 wfa fixtures.
        # Phase 1 of the fresh-WFA work today archived those fixtures
        # to _archived_2026_06_01/ and replaced them with sharded outputs
        # (4 shard CSVs + merged) totaling ~66 rows. Floor set generously
        # at 30 so a future shard reshuffle doesn't false-positive while
        # still catching "everything got deleted accidentally."
        self._ingest_dir()
        con = duckdb.connect(str(self.db))
        count = con.execute("SELECT COUNT(*) FROM wfa_windows").fetchone()[0]
        con.close()
        assert count >= 30, f"wfa_windows count {count} below floor of 30"

    def test_wfa_summary_floor(self):
        # 2026-06-01: floor lowered 14 -> 10. Same reason as
        # test_wfa_windows_floor above. Fresh wfa_summary has 11 rows
        # (one per strategy in the sharded run); 10 is generous.
        self._ingest_dir()
        con = duckdb.connect(str(self.db))
        count = con.execute("SELECT COUNT(*) FROM wfa_summary").fetchone()[0]
        con.close()
        assert count >= 10, f"wfa_summary count {count} below floor of 10"

    def test_no_new_error_log_entries(self):
        self._ingest_dir()
        if self.err_log.exists():
            lines = self.err_log.read_text().strip().splitlines()
            assert not lines, f"ingest_errors.log has {len(lines)} entries"

    def test_trades_ct_recent_query(self):
        self._ingest_dir()
        con = duckdb.connect(str(self.db))
        count = con.execute(
            "SELECT COUNT(*) FROM trades_ct WHERE session_date >= '2024-01-01'"
        ).fetchone()[0]
        con.close()
        assert count > 0, "No trades found after 2024-01-01 in trades_ct"
