# Backtest Warehouse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-file DuckDB analytics warehouse (`data/warehouse/phoenix.duckdb`) that ingests both the 55 legacy `backtest_results/*.csv` files and the 15 portfolio_framework CSVs (per `INVENTORY.md`) into a unified schema with content-hash provenance.

**Architecture:** Python library + CLI under `tools/warehouse/`. One DuckDB file, single-writer enforced via PID lock. Six CSV kinds (`trades`, `wfa_windows`, `wfa_summary`, `summary`, `mixed`, `derived`) detected by header sniff; per-file atomic transactions; never delete (re-ingest is hash-deduplicated). Convenience CSVs land in lazy `import_<name>` tables to be replaced by views in v2.

**Tech Stack:** Python 3.14.3, DuckDB (with JSON extension), pytest, psutil (already in requirements). No new top-level dependencies beyond `duckdb`.

**Spec:** [`docs/superpowers/specs/2026-05-31-backtest-warehouse-design.md`](../specs/2026-05-31-backtest-warehouse-design.md) — read it before starting. Every task here implements something in the spec; section numbers below reference spec sections.

**Working directory for every command:** `C:\Trading Project\phoenix_bot\` (canonical project root). Python interpreter: `C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe` (project standard). `python` on PATH should resolve to this.

---

## File structure (what each task produces)

```
tools/warehouse/
├── __init__.py              empty marker — Task 1
├── schema.sql               full DDL incl. INSTALL json; LOAD json; trades_ct view — Task 2
├── db.py                    open_db(), apply_schema() — Task 2
├── lock.py                  acquire_lock(), release_lock(), stale-PID recovery — Task 3
├── sidecar.py               load_sidecar(), compute_run_id() — Task 4
├── known_strategies.py      load_known_strategies() reading config/strategies.py — Task 5
├── sniff.py                 sniff_csv_kind(), sniff_strategy_from_filename(), safe_import_table_name() — Tasks 6-7
├── ingest.py                ingest_csv(), scan_dir(), per-kind insert helpers — Tasks 8-19
└── cli.py                   `python -m tools.warehouse <subcommand>` — Task 20

tests/warehouse/
├── __init__.py
├── conftest.py              shared pytest fixtures (in-memory DB, fixture dir) — Task 2
├── fixtures/                tiny CSVs + sidecars built lazily per test — created in each test task
└── test_*.py                one file per code module, plus test_smoke.py

data/warehouse/              created at first ingest; gitignored
docs/superpowers/specs/2026-05-31-warehouse-runbook.md — Task 22
```

---

### Task 1: Bootstrap — directories, requirements, gitignore

**Files:**
- Create: `tools/warehouse/__init__.py` (empty)
- Create: `tests/warehouse/__init__.py` (empty)
- Create: `tests/warehouse/fixtures/.gitkeep` (empty placeholder so dir is tracked)
- Modify: `requirements.txt` (append `duckdb>=1.1`)
- Modify: `.gitignore` (append warehouse artifacts)

- [ ] **Step 1: Confirm project root and Python interpreter**

Run: `python --version`
Expected: `Python 3.14.3`

Run: `cd "C:\Trading Project\phoenix_bot" && pwd` (or `Get-Location` in PowerShell)
Expected: path ends in `phoenix_bot`

- [ ] **Step 2: Create empty package markers and fixtures dir**

```bash
mkdir -p tools/warehouse tests/warehouse/fixtures
touch tools/warehouse/__init__.py tests/warehouse/__init__.py tests/warehouse/fixtures/.gitkeep
```

- [ ] **Step 3: Append duckdb to requirements.txt**

Add this line to the end of `requirements.txt`:

```
duckdb>=1.1            # 2026-05-31 backtest warehouse (docs/superpowers/specs/2026-05-31-backtest-warehouse-design.md)
```

- [ ] **Step 4: Install duckdb**

Run: `pip install "duckdb>=1.1"`
Expected: succeeds; `python -c "import duckdb; print(duckdb.__version__)"` prints a version >= 1.1.

- [ ] **Step 5: Append warehouse paths to .gitignore**

Append:

```
# Backtest warehouse (data/warehouse/ contents are local artifacts only)
data/warehouse/phoenix.duckdb
data/warehouse/phoenix.duckdb.wal
data/warehouse/.ingest.lock
data/warehouse/ingest_errors.log
.tmp/
```

- [ ] **Step 6: Commit**

```bash
git add tools/warehouse/__init__.py tests/warehouse/__init__.py tests/warehouse/fixtures/.gitkeep requirements.txt .gitignore
git commit -m "warehouse: bootstrap package structure and add duckdb dep"
```

---

### Task 2: Schema DDL + `open_db()` / `apply_schema()`

Implements spec §4 (schema) and the bootstrap pieces of §3 (file layout).

**Files:**
- Create: `tools/warehouse/schema.sql`
- Create: `tools/warehouse/db.py`
- Create: `tests/warehouse/conftest.py`
- Create: `tests/warehouse/test_schema.py`

- [ ] **Step 1: Write `tools/warehouse/schema.sql`** (copy from spec §4 verbatim; included here in full for self-contained reference)

```sql
-- tools/warehouse/schema.sql
-- Bootstrap: JSON extension is required for JSON column type and json_extract().
INSTALL json;
LOAD json;

-- runs: provenance layer. One row per ingested CSV (identity = content hash).
CREATE TABLE IF NOT EXISTS runs (
    run_id           VARCHAR PRIMARY KEY,
    source_filename  VARCHAR NOT NULL,
    csv_kind         VARCHAR NOT NULL,
    logical_group    VARCHAR,
    strategy         VARCHAR,
    params           JSON,
    code_sha         VARCHAR,
    seed             INTEGER,
    lookback_start   TIMESTAMP WITH TIME ZONE,
    lookback_end     TIMESTAMP WITH TIME ZONE,
    friction_applied BOOLEAN,
    ingested_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    sidecar_raw      JSON
);

CREATE TABLE IF NOT EXISTS trades (
    run_id          VARCHAR NOT NULL REFERENCES runs(run_id),
    strategy        VARCHAR NOT NULL,
    direction       VARCHAR NOT NULL,
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
    mae_ticks       DOUBLE,
    mfe_ticks       DOUBLE,
    regime          VARCHAR,
    tod_bucket      VARCHAR,
    entry_context   JSON
);
CREATE INDEX IF NOT EXISTS idx_trades_run   ON trades(run_id);
CREATE INDEX IF NOT EXISTS idx_trades_strat ON trades(strategy, entry_ts);
CREATE INDEX IF NOT EXISTS idx_trades_year  ON trades(year);

CREATE OR REPLACE VIEW trades_ct AS
SELECT
    *,
    (entry_ts AT TIME ZONE 'America/Chicago')::DATE AS session_date,
    EXTRACT(EPOCH FROM (
        (entry_ts AT TIME ZONE 'America/Chicago')
        - date_trunc('day', entry_ts AT TIME ZONE 'America/Chicago')
        - INTERVAL '8 hours 30 minutes'
    )) / 60.0 AS market_open_minutes,
    entry_ts AT TIME ZONE 'America/Chicago' AS entry_ts_ct,
    exit_ts  AT TIME ZONE 'America/Chicago' AS exit_ts_ct
FROM trades;

CREATE TABLE IF NOT EXISTS run_metrics (
    run_id        VARCHAR NOT NULL REFERENCES runs(run_id),
    metric_name   VARCHAR NOT NULL,
    metric_value  DOUBLE,
    label_value   VARCHAR,
    PRIMARY KEY (run_id, metric_name)
);

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
```

- [ ] **Step 2: Write `tools/warehouse/db.py`**

```python
# tools/warehouse/db.py
"""Database connection and schema helpers for the Phoenix backtest warehouse."""
from __future__ import annotations
from pathlib import Path
import duckdb

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def open_db(db_path: Path | str = ":memory:") -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection. Caller is responsible for closing."""
    return duckdb.connect(str(db_path))


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Apply schema.sql to the given connection. Idempotent (uses IF NOT EXISTS)."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    con.execute(sql)
```

- [ ] **Step 3: Write `tests/warehouse/conftest.py`**

```python
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
```

- [ ] **Step 4: Write the failing test `tests/warehouse/test_schema.py`**

```python
# tests/warehouse/test_schema.py
"""Schema sanity checks: tables exist, view exists, JSON extension loaded."""
from __future__ import annotations


EXPECTED_TABLES = {"runs", "trades", "run_metrics", "wfa_windows", "wfa_summary"}
EXPECTED_VIEWS = {"trades_ct"}


def test_all_tables_exist(db):
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
    ).fetchall()
    actual = {r[0] for r in rows}
    assert EXPECTED_TABLES.issubset(actual), f"missing tables: {EXPECTED_TABLES - actual}"


def test_trades_ct_view_exists(db):
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'VIEW'"
    ).fetchall()
    actual = {r[0] for r in rows}
    assert EXPECTED_VIEWS.issubset(actual)


def test_json_type_works(db):
    # If JSON extension didn't load, casting to JSON raises.
    result = db.execute("SELECT CAST('{\"a\":1}' AS JSON) AS j").fetchone()
    assert result is not None


def test_apply_schema_idempotent(db):
    from tools.warehouse.db import apply_schema
    apply_schema(db)  # second call should not raise
    apply_schema(db)  # third call either
```

- [ ] **Step 5: Run test to verify it fails**

Run: `pytest tests/warehouse/test_schema.py -v`
Expected: imports fail (`tools.warehouse.db` module not yet on import path) OR schema.sql not found. If imports succeed because Step 2 was already written, tests should PASS.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_schema.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add tools/warehouse/schema.sql tools/warehouse/db.py tests/warehouse/conftest.py tests/warehouse/test_schema.py
git commit -m "warehouse: add schema.sql with all tables, trades_ct view, JSON ext"
```

---

### Task 3: PID lock (`lock.py`)

Implements spec §5.6.

**Files:**
- Create: `tools/warehouse/lock.py`
- Create: `tests/warehouse/test_lock.py`

- [ ] **Step 1: Write `tools/warehouse/lock.py`**

```python
# tools/warehouse/lock.py
"""PID-file based lock for the warehouse single-writer constraint.

Cross-platform (Windows + POSIX). Stale locks (PID dead or host mismatch)
are detected on acquisition and overwritten with a warning.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
import socket
import psutil

log = logging.getLogger(__name__)


class LockHeldError(RuntimeError):
    """Raised when another live ingest holds the lock."""


def _read_lock(lock_path: Path) -> dict | None:
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_lock(lock_path: Path) -> None:
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload), encoding="utf-8")


def _is_alive(pid: int, host: str) -> bool:
    """True iff PID is alive on THIS host."""
    if host != socket.gethostname():
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def acquire(lock_path: Path) -> None:
    """Acquire the lock. Raises LockHeldError if a live process holds it.

    Stale locks (dead PID or different host) are overwritten with a warning.
    """
    existing = _read_lock(lock_path)
    if existing is not None:
        pid = int(existing.get("pid", -1))
        host = str(existing.get("host", ""))
        if _is_alive(pid, host):
            raise LockHeldError(
                f"another ingest is running (pid={pid}, started={existing.get('started_at')})"
            )
        log.warning("warehouse: stale lock from pid %d, recovering", pid)
    _write_lock(lock_path)


def release(lock_path: Path) -> None:
    """Release the lock. Safe to call if the file no longer exists."""
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def ingest_lock(lock_path: Path):
    """Context manager: acquire on enter, release in `finally`."""
    acquire(lock_path)
    try:
        yield
    finally:
        release(lock_path)
```

- [ ] **Step 2: Write `tests/warehouse/test_lock.py`**

```python
# tests/warehouse/test_lock.py
from __future__ import annotations
from pathlib import Path
import json
import os
import pytest

from tools.warehouse.lock import acquire, release, ingest_lock, LockHeldError


def test_acquire_creates_file(tmp_path):
    lock = tmp_path / "test.lock"
    acquire(lock)
    assert lock.exists()
    data = json.loads(lock.read_text())
    assert data["pid"] == os.getpid()
    assert "host" in data and "started_at" in data
    release(lock)


def test_release_is_safe_on_missing(tmp_path):
    lock = tmp_path / "test.lock"
    release(lock)  # no error


def test_acquire_raises_on_live_lock(tmp_path):
    lock = tmp_path / "test.lock"
    acquire(lock)
    try:
        with pytest.raises(LockHeldError, match="another ingest is running"):
            acquire(lock)
    finally:
        release(lock)


def test_acquire_recovers_stale_dead_pid(tmp_path):
    lock = tmp_path / "test.lock"
    # Write a lock claiming an impossible PID on this host.
    import socket
    lock.write_text(json.dumps({
        "pid": 999_999_999,
        "host": socket.gethostname(),
        "started_at": "2026-01-01T00:00:00Z",
    }))
    acquire(lock)  # should succeed silently, overwriting
    assert json.loads(lock.read_text())["pid"] == os.getpid()
    release(lock)


def test_acquire_recovers_stale_different_host(tmp_path):
    lock = tmp_path / "test.lock"
    lock.write_text(json.dumps({
        "pid": 1,
        "host": "some-other-host-that-is-not-us",
        "started_at": "2026-01-01T00:00:00Z",
    }))
    acquire(lock)
    release(lock)


def test_ingest_lock_releases_in_finally(tmp_path):
    lock = tmp_path / "test.lock"

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with ingest_lock(lock):
            assert lock.exists()
            raise Boom()
    assert not lock.exists(), "lock must be released even on exception"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/warehouse/test_lock.py -v`
Expected: fails with ImportError if Step 1 not committed yet; passes once Step 1 file is on disk.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_lock.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/warehouse/lock.py tests/warehouse/test_lock.py
git commit -m "warehouse: add PID lock with stale recovery and try/finally release"
```

---

### Task 4: Sidecar load + `run_id` content hash

Implements spec §5.1 step 1 (hashing) + §5.5 (sidecar contract).

**Files:**
- Create: `tools/warehouse/sidecar.py`
- Create: `tests/warehouse/test_sidecar.py`
- Create: `tests/warehouse/fixtures/sidecar_full.run.json`
- Create: `tests/warehouse/fixtures/sidecar_full.csv`

- [ ] **Step 1: Write `tools/warehouse/sidecar.py`**

```python
# tools/warehouse/sidecar.py
"""Sidecar JSON loader and content-hash run_id computation.

Per spec §5.1 step 1:
  run_id = sha256(csv_bytes + b"\n" + canonical_sidecar_json)
where canonical = json.dumps(sorted_keys=True, separators=(',', ':')).
If no sidecar exists, only csv_bytes are hashed and sidecar_raw.meta records
sidecar_missing=true.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import base64
import hashlib
import json


SCHEMA_VERSION_SUPPORTED = 1


@dataclass
class SidecarResult:
    run_id: str
    sidecar: dict[str, Any]      # parsed sidecar, or {} if missing/unparseable
    sidecar_raw: dict[str, Any]  # what gets stored in runs.sidecar_raw (sidecar + meta envelope)


class UnsupportedSidecarSchema(ValueError):
    pass


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sidecar_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(csv_path.suffix + ".run.json")


def load_and_hash(csv_path: Path) -> SidecarResult:
    """Read CSV + sidecar (if present), validate schema_version, return run_id."""
    csv_bytes = csv_path.read_bytes()
    sc_path = _sidecar_path(csv_path)
    meta: dict[str, Any] = {}
    sidecar: dict[str, Any] = {}

    if sc_path.exists():
        raw_bytes = sc_path.read_bytes()
        try:
            sidecar = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            sidecar = {}
            meta["sidecar_parse_error"] = str(e)
            meta["parse_error_raw_b64"] = base64.b64encode(raw_bytes).decode("ascii")
        if sidecar:
            ver = sidecar.get("schema_version")
            if ver is not None and ver != SCHEMA_VERSION_SUPPORTED:
                raise UnsupportedSidecarSchema(
                    f"{sc_path.name}: schema_version={ver}; supported={SCHEMA_VERSION_SUPPORTED}"
                )
            meta["sidecar_present"] = True
            meta["missing_fields"] = [
                f for f in ("strategy", "params", "code_sha", "seed",
                            "lookback_start", "lookback_end")
                if f not in sidecar
            ]
    else:
        meta["sidecar_missing"] = True

    # Compute hash
    h = hashlib.sha256()
    h.update(csv_bytes)
    h.update(b"\n")
    h.update(_canonical(sidecar))
    run_id = h.hexdigest()

    sidecar_raw = {"sidecar": sidecar, "meta": meta}
    return SidecarResult(run_id=run_id, sidecar=sidecar, sidecar_raw=sidecar_raw)
```

- [ ] **Step 2: Create the test fixtures**

`tests/warehouse/fixtures/sidecar_full.csv`:

```csv
strategy,direction,entry_ts,entry_price,pnl_dollars
foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0
```

`tests/warehouse/fixtures/sidecar_full.csv.run.json`:

```json
{
  "schema_version": 1,
  "strategy": "foo",
  "params": {"ema_len": 21},
  "code_sha": "abc123",
  "seed": 42,
  "lookback_start": "2024-01-01T00:00:00Z",
  "lookback_end":   "2025-01-01T00:00:00Z",
  "friction_applied": true,
  "friction_per_rt_usd": 4.82
}
```

- [ ] **Step 3: Write `tests/warehouse/test_sidecar.py`**

```python
# tests/warehouse/test_sidecar.py
from __future__ import annotations
import json
import pytest

from tools.warehouse.sidecar import load_and_hash, UnsupportedSidecarSchema


def test_loads_full_sidecar_and_hashes(fixtures_dir):
    res = load_and_hash(fixtures_dir / "sidecar_full.csv")
    assert len(res.run_id) == 64
    assert res.sidecar["strategy"] == "foo"
    assert res.sidecar["friction_applied"] is True
    assert res.sidecar_raw["meta"]["sidecar_present"] is True
    assert res.sidecar_raw["meta"]["missing_fields"] == []


def test_missing_sidecar_records_meta_flag(tmp_path):
    csv = tmp_path / "lonely.csv"
    csv.write_text("a,b\n1,2\n")
    res = load_and_hash(csv)
    assert res.sidecar == {}
    assert res.sidecar_raw["meta"]["sidecar_missing"] is True


def test_run_id_is_stable_across_calls(fixtures_dir):
    r1 = load_and_hash(fixtures_dir / "sidecar_full.csv")
    r2 = load_and_hash(fixtures_dir / "sidecar_full.csv")
    assert r1.run_id == r2.run_id


def test_run_id_changes_when_csv_changes(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n"); r1 = load_and_hash(csv).run_id
    csv.write_text("a\n2\n"); r2 = load_and_hash(csv).run_id
    assert r1 != r2


def test_run_id_changes_when_sidecar_changes(tmp_path):
    csv = tmp_path / "x.csv"; csv.write_text("a\n1\n")
    sc = tmp_path / "x.csv.run.json"
    sc.write_text(json.dumps({"schema_version": 1, "seed": 1}))
    r1 = load_and_hash(csv).run_id
    sc.write_text(json.dumps({"schema_version": 1, "seed": 2}))
    r2 = load_and_hash(csv).run_id
    assert r1 != r2


def test_unsupported_schema_version_raises(tmp_path):
    csv = tmp_path / "x.csv"; csv.write_text("a\n1\n")
    sc = tmp_path / "x.csv.run.json"
    sc.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(UnsupportedSidecarSchema):
        load_and_hash(csv)


def test_parse_error_records_b64_and_proceeds(tmp_path):
    csv = tmp_path / "x.csv"; csv.write_text("a\n1\n")
    sc = tmp_path / "x.csv.run.json"
    sc.write_bytes(b"\xff\xfe not valid json")
    res = load_and_hash(csv)
    assert res.sidecar == {}
    assert "sidecar_parse_error" in res.sidecar_raw["meta"]
    assert "parse_error_raw_b64" in res.sidecar_raw["meta"]
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/warehouse/test_sidecar.py -v`
Expected: passes if Step 1 is in place; fails on import otherwise.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_sidecar.py -v`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add tools/warehouse/sidecar.py tests/warehouse/test_sidecar.py tests/warehouse/fixtures/sidecar_full.csv tests/warehouse/fixtures/sidecar_full.csv.run.json
git commit -m "warehouse: add sidecar loader and content-hash run_id"
```

---

### Task 5: Known strategies loader

Implements spec §5.4 (`known_strategies` dependency).

**Files:**
- Create: `tools/warehouse/known_strategies.py`
- Create: `tests/warehouse/test_known_strategies.py`

- [ ] **Step 1: Inspect `config/strategies.py` to confirm the public name**

Run: `grep -E "^[A-Z_]+\s*=" "C:\Trading Project\phoenix_bot\config\strategies.py" | head -20`

This identifies which module-level attribute holds the strategy registry (typically `STRATEGIES`, `STRATEGY_REGISTRY`, or `BACKTEST_STRATEGY_NAMES`). Use the result to drive the loader. If multiple candidates exist, prefer the one whose values are clearly the canonical key names used in the CSVs (e.g., `vwap_pullback_v2`, `bias_momentum`).

- [ ] **Step 2: Write `tools/warehouse/known_strategies.py`**

```python
# tools/warehouse/known_strategies.py
"""Load the canonical set of strategy keys for filename sniffing.

Reads from config/strategies.py. The module is imported (not parsed) so
typos surface as ImportError at startup, not silently at query time.
"""
from __future__ import annotations
from functools import lru_cache


# Adjust the import below if your project uses a different attribute name.
# Run `python -c "import config.strategies as s; print(dir(s))"` to find it.
_REGISTRY_CANDIDATES = ("STRATEGIES", "STRATEGY_REGISTRY", "BACKTEST_STRATEGY_NAMES", "ALL_STRATEGIES")


@lru_cache(maxsize=1)
def load_known_strategies() -> frozenset[str]:
    """Return the canonical strategy key set. Cached after first call."""
    import config.strategies as cs  # noqa: PLC0415
    for name in _REGISTRY_CANDIDATES:
        obj = getattr(cs, name, None)
        if obj is None:
            continue
        # Accept dict (keys are strategy names) or iterable of strings.
        if isinstance(obj, dict):
            return frozenset(obj.keys())
        try:
            return frozenset(str(s) for s in obj)
        except TypeError:
            continue
    raise RuntimeError(
        f"config.strategies has none of {_REGISTRY_CANDIDATES}; "
        "edit _REGISTRY_CANDIDATES in tools/warehouse/known_strategies.py"
    )
```

- [ ] **Step 3: Write `tests/warehouse/test_known_strategies.py`**

```python
# tests/warehouse/test_known_strategies.py
from __future__ import annotations
import sys
import types
import pytest


def _install_fake_module(monkeypatch, attr_name: str, value):
    fake = types.ModuleType("config.strategies")
    setattr(fake, attr_name, value)
    fake_pkg = types.ModuleType("config")
    monkeypatch.setitem(sys.modules, "config", fake_pkg)
    monkeypatch.setitem(sys.modules, "config.strategies", fake)


@pytest.fixture(autouse=True)
def _clear_cache():
    from tools.warehouse.known_strategies import load_known_strategies
    load_known_strategies.cache_clear()
    yield
    load_known_strategies.cache_clear()


def test_loads_from_dict(monkeypatch):
    _install_fake_module(monkeypatch, "STRATEGIES", {"a_asian": object(), "g_inside_bar_breakout": object()})
    from tools.warehouse.known_strategies import load_known_strategies
    s = load_known_strategies()
    assert s == frozenset({"a_asian", "g_inside_bar_breakout"})


def test_loads_from_iterable(monkeypatch):
    _install_fake_module(monkeypatch, "STRATEGY_REGISTRY", ["foo", "bar"])
    from tools.warehouse.known_strategies import load_known_strategies
    assert load_known_strategies() == frozenset({"foo", "bar"})


def test_raises_when_no_attr(monkeypatch):
    _install_fake_module(monkeypatch, "UNKNOWN_ATTR", {})
    from tools.warehouse.known_strategies import load_known_strategies
    with pytest.raises(RuntimeError, match="config.strategies has none of"):
        load_known_strategies()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_known_strategies.py -v`
Expected: 3 passed.

- [ ] **Step 5: Smoke-check the loader against the REAL config module**

Run: `python -c "from tools.warehouse.known_strategies import load_known_strategies; print(sorted(load_known_strategies())[:5], '... total:', len(load_known_strategies()))"`
Expected: prints 5 strategy names and a count > 0. If it raises `RuntimeError`, add the actual attribute name from `config/strategies.py` to `_REGISTRY_CANDIDATES` and commit that adjustment.

- [ ] **Step 6: Commit**

```bash
git add tools/warehouse/known_strategies.py tests/warehouse/test_known_strategies.py
git commit -m "warehouse: load known strategies from config/strategies.py"
```

---

### Task 6: CSV kind sniffer

Implements spec §5.2 (sniff table) for all 7 kinds.

**Files:**
- Create: `tools/warehouse/sniff.py` (partial — only sniff_csv_kind for now; the other helpers come in Task 7)
- Create: `tests/warehouse/test_sniff_kind.py`

- [ ] **Step 1: Write the CSV-kind sniffer in `tools/warehouse/sniff.py`**

```python
# tools/warehouse/sniff.py
"""Header-only sniffers for the warehouse ingester.

This module is import-cheap (no DuckDB, no pandas). It only inspects the
first line of a CSV (and, for derived, the filename).
"""
from __future__ import annotations
from pathlib import Path
import csv as csv_mod
import re
from typing import Literal


CsvKind = Literal["trades", "wfa_windows", "wfa_summary", "summary", "mixed", "derived", "error"]


_TRADE_SIG    = {"entry_ts", "entry_price", "pnl_dollars"}
_TRADE_LIGHT  = {"entry_ts", "entry_price"}   # for the mixed-kind check
_WFA_WIN_SIG  = {"window_idx", "oos_pf", "is_pf"}
_WFA_SUM_SIG  = {"strategy", "mean_oos_pf", "pct_windows_degraded"}
_METRIC_NAMES = {"profit_factor", "sharpe", "win_rate", "max_dd", "n_trades"}
_DERIVED_PREFIXES = ("phase1_", "microstructure_", "phase3_", "phase2_")


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv_mod.reader(f)
        try:
            return [c.strip() for c in next(reader)]
        except StopIteration:
            return []


def sniff_csv_kind(path: Path) -> tuple[CsvKind, list[str]]:
    """Return (kind, header). Kind 'error' means none of the positive rules matched."""
    header = _read_header(path)
    cols = set(header)

    if _TRADE_SIG.issubset(cols):
        # Mixed if it ALSO has at least one metric column.
        if cols & _METRIC_NAMES:
            return ("mixed", header)
        return ("trades", header)

    if _WFA_WIN_SIG.issubset(cols):
        return ("wfa_windows", header)

    if _WFA_SUM_SIG.issubset(cols):
        return ("wfa_summary", header)

    if (cols & _TRADE_LIGHT) and (cols & _METRIC_NAMES):
        return ("mixed", header)

    # summary: first column 'strategy' or 'name', remaining numeric-named, no entry_ts
    if header and header[0] in {"strategy", "name"} and "entry_ts" not in cols:
        return ("summary", header)

    if path.name.startswith(_DERIVED_PREFIXES) or path.stem in {"microstructure_lift"}:
        return ("derived", header)

    return ("error", header)
```

- [ ] **Step 2: Create fixtures for each kind**

`tests/warehouse/fixtures/kind_trades.csv`:
```csv
strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year
foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,84,30.0,2025
```

`tests/warehouse/fixtures/kind_trades_extended.csv`:
```csv
strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year,mae_ticks,mfe_ticks,regime,tod_bucket
foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,84,30.0,2025,12.0,20.0,LOW_VOL_TREND,Opening Drive
```

`tests/warehouse/fixtures/kind_wfa_windows.csv`:
```csv
strategy,window_idx,is_start,is_end,oos_start,oos_end,best_params,is_pf,is_trades,oos_pf,oos_trades,oos_net,wfe,degraded
foo,0,2021-01-01,2021-12-31,2022-01-01,2022-03-31,"{""ema_len"":21}",1.5,100,1.2,30,500.0,0.8,false
```

`tests/warehouse/fixtures/kind_wfa_summary.csv`:
```csv
strategy,n_windows,mean_is_pf,mean_oos_pf,median_oos_pf,pct_windows_degraded,robust
foo,15,1.6,1.3,1.25,0.2,true
```

`tests/warehouse/fixtures/kind_summary.csv`:
```csv
strategy,profit_factor,sharpe,win_rate,max_dd
foo,1.5,1.2,0.55,-1200.0
```

`tests/warehouse/fixtures/kind_mixed.csv`:
```csv
strategy,direction,entry_ts,entry_price,pnl_dollars,profit_factor,n_trades
foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,1.5,100
foo,SHORT,2025-01-03 14:30:00+00:00,21100.0,-20.0,1.5,100
```

`tests/warehouse/fixtures/kind_derived_phase1_strategy_summary.csv`:
```csv
strategy,n,net_pnl,win_rate
foo,100,5000.0,0.55
```
Wait — `derived` rule depends on filename prefix. Move/rename so it starts with `phase1_`. Actual filename: `tests/warehouse/fixtures/phase1_strategy_summary_sample.csv` with the same content. (Adjust the test paths accordingly.)

`tests/warehouse/fixtures/kind_unknown.csv`:
```csv
unrelated_col_a,unrelated_col_b
1,2
```

- [ ] **Step 3: Write `tests/warehouse/test_sniff_kind.py`**

```python
# tests/warehouse/test_sniff_kind.py
from __future__ import annotations
from tools.warehouse.sniff import sniff_csv_kind


def test_legacy_trades(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_trades.csv")
    assert kind == "trades"


def test_extended_trades(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_trades_extended.csv")
    assert kind == "trades"


def test_wfa_windows(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_wfa_windows.csv")
    assert kind == "wfa_windows"


def test_wfa_summary(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_wfa_summary.csv")
    assert kind == "wfa_summary"


def test_summary(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_summary.csv")
    assert kind == "summary"


def test_mixed(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_mixed.csv")
    assert kind == "mixed"


def test_derived_by_filename(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "phase1_strategy_summary_sample.csv")
    assert kind == "derived"


def test_unknown(fixtures_dir):
    kind, _ = sniff_csv_kind(fixtures_dir / "kind_unknown.csv")
    assert kind == "error"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_sniff_kind.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/warehouse/sniff.py tests/warehouse/test_sniff_kind.py tests/warehouse/fixtures/
git commit -m "warehouse: add CSV kind sniffer for 7 kinds"
```

---

### Task 7: WFA filename strategy sniff + safe import name

Implements spec §5.3 (`safe_import_table_name`) and §5.4 (`sniff_strategy_from_filename`).

**Files:**
- Modify: `tools/warehouse/sniff.py` (add two functions)
- Create: `tests/warehouse/test_sniff_filename.py`

- [ ] **Step 1: Append to `tools/warehouse/sniff.py`**

```python
# (Appended to tools/warehouse/sniff.py)
import logging

log = logging.getLogger(__name__)


WFA_P13_RE = re.compile(r"^wfa_windows_p13_(?P<strategy>[a-z][a-z0-9_]*)\.csv$")
SAFE_IDENT = re.compile(r"[^A-Za-z0-9_]")
SAFE_IDENT_FULL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sniff_strategy_from_filename(path: Path, known_strategies: frozenset[str]) -> str | None:
    """Match Phase 13 WFA filenames to a canonical strategy key."""
    m = WFA_P13_RE.match(path.name)
    if not m:
        return None
    candidate = m.group("strategy")
    if candidate in known_strategies:
        return candidate
    suffix_matches = [s for s in known_strategies if s == candidate or s.endswith("_" + candidate)]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    log.warning(
        "wfa filename sniff: %s candidate=%r matches=%r -> strategy=NULL",
        path.name, candidate, suffix_matches,
    )
    return None


def safe_import_table_name(csv_path: Path) -> str:
    """Derive a safe SQL identifier (`import_<stem>`) for a derived CSV.

    Raises ValueError if the result somehow fails the identifier shape check
    (defense-in-depth; the sanitization should already guarantee it).
    """
    stem = csv_path.stem.lower()
    sanitized = SAFE_IDENT.sub("_", stem)
    if not sanitized or not sanitized[0].isalpha():
        sanitized = "f_" + sanitized
    name = f"import_{sanitized}"
    if not SAFE_IDENT_FULL.match(name):
        raise ValueError(f"derived table name not a safe identifier: {name!r}")
    return name
```

- [ ] **Step 2: Write `tests/warehouse/test_sniff_filename.py`**

```python
# tests/warehouse/test_sniff_filename.py
from __future__ import annotations
from pathlib import Path
import pytest

from tools.warehouse.sniff import sniff_strategy_from_filename, safe_import_table_name


KNOWN = frozenset({
    "raschke_baseline",
    "g_inside_bar_breakout",
    "a_asian_continuation",
    "e_multi_day_breakout",
    "vwap_pullback_v2",
})


def test_exact_match():
    p = Path("wfa_windows_p13_raschke_baseline.csv")
    assert sniff_strategy_from_filename(p, KNOWN) == "raschke_baseline"


def test_suffix_match_unambiguous():
    p = Path("wfa_windows_p13_asian.csv")
    # 'asian' matches only 'a_asian_continuation' as a suffix? No — suffix rule is `endswith("_" + candidate)`,
    # so 'a_asian_continuation' ends with '_continuation', not '_asian'. Use a candidate that DOES suffix-match.
    # Adjust expectation: 'inside_bar_breakout' suffix-matches 'g_inside_bar_breakout'.
    p2 = Path("wfa_windows_p13_inside_bar_breakout.csv")
    assert sniff_strategy_from_filename(p2, KNOWN) == "g_inside_bar_breakout"


def test_no_match_returns_none():
    p = Path("wfa_windows_p13_does_not_exist.csv")
    assert sniff_strategy_from_filename(p, KNOWN) is None


def test_ambiguous_returns_none():
    known = frozenset({"x_foo", "y_foo"})
    p = Path("wfa_windows_p13_foo.csv")
    assert sniff_strategy_from_filename(p, known) is None


def test_multi_strategy_wfa_file_returns_none():
    # Filename doesn't match the p13 regex at all.
    p = Path("wfa_windows.csv")
    assert sniff_strategy_from_filename(p, KNOWN) is None
    p2 = Path("wfa_windows_shardA.csv")
    assert sniff_strategy_from_filename(p2, KNOWN) is None


def test_safe_import_table_simple():
    assert safe_import_table_name(Path("phase1_strategy_summary.csv")) == "import_phase1_strategy_summary"


def test_safe_import_table_strips_special_chars():
    assert safe_import_table_name(Path("weird-name.v2.csv")) == "import_weird_name_v2"


def test_safe_import_table_handles_leading_digit():
    name = safe_import_table_name(Path("123_foo.csv"))
    assert name.startswith("import_f_")
    assert name == "import_f_123_foo"


def test_safe_import_table_rejects_unrecoverable(monkeypatch):
    # Force the defense-in-depth assert by mocking the regex sub to a no-op.
    import tools.warehouse.sniff as sniff_mod
    monkeypatch.setattr(sniff_mod.SAFE_IDENT, "sub", lambda repl, s: s)
    with pytest.raises(ValueError, match="not a safe identifier"):
        safe_import_table_name(Path("bad name with spaces.csv"))
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_sniff_filename.py -v`
Expected: 9 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/sniff.py tests/warehouse/test_sniff_filename.py
git commit -m "warehouse: add WFA filename sniff and safe import table name"
```

---

### Task 8: Ingest engine skeleton + `IngestResult` + `insert_run`

Implements spec §5.1 steps 4-5 (transaction + insert into `runs`) and the dataclass from §5.

**Files:**
- Create: `tools/warehouse/ingest.py`
- Create: `tests/warehouse/test_ingest_runs.py`

- [ ] **Step 1: Write `tools/warehouse/ingest.py` skeleton**

```python
# tools/warehouse/ingest.py
"""Warehouse ingester. One transaction per file; atomic; idempotent on re-ingest."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
import json
import logging

import duckdb

from tools.warehouse.sidecar import load_and_hash, SidecarResult, UnsupportedSidecarSchema
from tools.warehouse.sniff import sniff_csv_kind, CsvKind

log = logging.getLogger(__name__)

Status = Literal["inserted", "skipped_duplicate", "error"]


@dataclass
class IngestResult:
    csv_path: Path
    run_id: str | None
    status: Status
    csv_kind: CsvKind | None
    rows_inserted: int = 0
    metrics_inserted: int = 0
    error: str | None = None


def _exists(con: duckdb.DuckDBPyConnection, run_id: str) -> bool:
    row = con.execute("SELECT 1 FROM runs WHERE run_id = ?", [run_id]).fetchone()
    return row is not None


def _resolve_friction(sc: SidecarResult, override: bool | None) -> bool:
    """Per spec §5.5 friction resolution rules."""
    if override is not None:
        return override
    sidecar = sc.sidecar
    if not sidecar:                                # no sidecar -> legacy default
        return False
    if sidecar.get("friction_applied") is True:
        return True
    fr = sidecar.get("friction_per_rt_usd")
    if isinstance(fr, (int, float)) and fr > 0:
        return True
    return False


def insert_run(
    con: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    source_filename: str,
    csv_kind: CsvKind,
    sidecar_result: SidecarResult,
    logical_group: str | None,
    friction_applied: bool,
    strategy: str | None,
) -> None:
    sc = sidecar_result.sidecar
    con.execute(
        """
        INSERT INTO runs (
            run_id, source_filename, csv_kind, logical_group, strategy,
            params, code_sha, seed, lookback_start, lookback_end,
            friction_applied, sidecar_raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            source_filename,
            csv_kind,
            logical_group,
            strategy,
            json.dumps(sc.get("params")) if sc.get("params") is not None else None,
            sc.get("code_sha"),
            sc.get("seed"),
            sc.get("lookback_start"),
            sc.get("lookback_end"),
            friction_applied,
            json.dumps(sidecar_result.sidecar_raw),
        ],
    )
```

- [ ] **Step 2: Write `tests/warehouse/test_ingest_runs.py`**

```python
# tests/warehouse/test_ingest_runs.py
from __future__ import annotations
import json
from tools.warehouse.ingest import insert_run, _resolve_friction
from tools.warehouse.sidecar import load_and_hash


def test_insert_run_writes_row(db, fixtures_dir):
    sc = load_and_hash(fixtures_dir / "sidecar_full.csv")
    insert_run(
        db,
        run_id=sc.run_id, source_filename="sidecar_full.csv", csv_kind="trades",
        sidecar_result=sc, logical_group=None,
        friction_applied=_resolve_friction(sc, None),
        strategy=sc.sidecar.get("strategy"),
    )
    row = db.execute("SELECT source_filename, strategy, friction_applied FROM runs").fetchone()
    assert row == ("sidecar_full.csv", "foo", True)


def test_friction_resolution_no_sidecar(tmp_path):
    csv = tmp_path / "no_sidecar.csv"; csv.write_text("a\n1\n")
    sc = load_and_hash(csv)
    assert _resolve_friction(sc, None) is False


def test_friction_resolution_sidecar_explicit_true(fixtures_dir):
    sc = load_and_hash(fixtures_dir / "sidecar_full.csv")
    assert _resolve_friction(sc, None) is True


def test_friction_resolution_cli_override_true(tmp_path):
    csv = tmp_path / "x.csv"; csv.write_text("a\n1\n")
    sc = load_and_hash(csv)
    assert _resolve_friction(sc, override=True) is True


def test_friction_resolution_sidecar_without_friction_field(tmp_path):
    csv = tmp_path / "x.csv"; csv.write_text("a\n1\n")
    sc_path = csv.with_suffix(".csv.run.json")
    sc_path.write_text(json.dumps({"schema_version": 1, "seed": 1}))
    sc = load_and_hash(csv)
    assert _resolve_friction(sc, None) is False
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_runs.py -v`
Expected: 5 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_runs.py
git commit -m "warehouse: ingester skeleton with runs-row insert and friction resolver"
```

---

### Task 9: Trades kind ingest (dynamic SELECT)

Implements spec §5.3 `trades` kind.

**Files:**
- Modify: `tools/warehouse/ingest.py` (add `_ingest_trades`)
- Create: `tests/warehouse/test_ingest_trades.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
# (Appended)

# Columns the ingester knows how to project from a trades-kind CSV.
_TRADES_SOURCE_COLS = {
    "strategy", "direction", "entry_ts", "entry_price", "stop_price", "target_price",
    "exit_ts", "exit_price", "exit_reason", "pnl_dollars", "pnl_ticks",
    "hold_min", "hold_minutes", "year",
    "mae_ticks", "mfe_ticks", "regime", "tod_bucket", "entry_context",
}


def _ingest_trades(
    con: duckdb.DuckDBPyConnection,
    *,
    csv_path: Path,
    run_id: str,
    header: list[str],
) -> int:
    """Insert all rows of a trades-kind CSV. Returns rows_inserted."""
    present = set(header) & _TRADES_SOURCE_COLS

    def col_or_null(name: str, fallback: str | None = None) -> str:
        if name in present:
            return name
        if fallback and fallback in present:
            return fallback
        return "NULL"

    entry_ctx = "TRY_CAST(entry_context AS JSON)" if "entry_context" in present else "NULL"

    # DuckDB's read_csv_auto requires a quoted path. We control csv_path (it's
    # operator input, but the CLI lock-step plus path resolution to an absolute
    # filesystem path makes a path-injection here moot). Pass via parameter.
    sql = f"""
        INSERT INTO trades SELECT
            ?                                   AS run_id,
            strategy,
            upper(direction)                    AS direction,
            entry_ts,
            entry_price,
            {col_or_null("stop_price")}         AS stop_price,
            {col_or_null("target_price")}       AS target_price,
            {col_or_null("exit_ts")}            AS exit_ts,
            {col_or_null("exit_price")}         AS exit_price,
            {col_or_null("exit_reason")}        AS exit_reason,
            {col_or_null("pnl_dollars")}        AS pnl_dollars,
            {col_or_null("pnl_ticks")}          AS pnl_ticks,
            {col_or_null("hold_minutes", fallback="hold_min")} AS hold_minutes,
            {col_or_null("year")}               AS year,
            {col_or_null("mae_ticks")}          AS mae_ticks,
            {col_or_null("mfe_ticks")}          AS mfe_ticks,
            {col_or_null("regime")}             AS regime,
            {col_or_null("tod_bucket")}         AS tod_bucket,
            {entry_ctx}                         AS entry_context
        FROM read_csv_auto(?, header=true, timestampformat='%Y-%m-%d %H:%M:%S%z')
    """
    before = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    con.execute(sql, [run_id, str(csv_path)])
    after = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return after - before
```

- [ ] **Step 2: Write `tests/warehouse/test_ingest_trades.py`**

```python
# tests/warehouse/test_ingest_trades.py
from __future__ import annotations
from tools.warehouse.ingest import insert_run, _ingest_trades, _resolve_friction
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def _ingest(db, csv_path):
    sc = load_and_hash(csv_path)
    kind, header = sniff_csv_kind(csv_path)
    assert kind == "trades"
    insert_run(
        db, run_id=sc.run_id, source_filename=csv_path.name, csv_kind=kind,
        sidecar_result=sc, logical_group=None,
        friction_applied=_resolve_friction(sc, None),
        strategy=sc.sidecar.get("strategy"),
    )
    return _ingest_trades(db, csv_path=csv_path, run_id=sc.run_id, header=header)


def test_legacy_trades_ingest(db, fixtures_dir):
    n = _ingest(db, fixtures_dir / "kind_trades.csv")
    assert n == 1
    row = db.execute(
        "SELECT strategy, direction, entry_price, pnl_dollars, mae_ticks "
        "FROM trades"
    ).fetchone()
    assert row[0] == "foo"
    assert row[1] == "LONG"
    assert row[2] == 21000.0
    assert row[3] == 42.0
    assert row[4] is None        # mae_ticks absent in legacy schema


def test_extended_trades_ingest(db, fixtures_dir):
    n = _ingest(db, fixtures_dir / "kind_trades_extended.csv")
    assert n == 1
    row = db.execute(
        "SELECT mae_ticks, regime, tod_bucket FROM trades"
    ).fetchone()
    assert row == (12.0, "LOW_VOL_TREND", "Opening Drive")


def test_direction_normalized_uppercase(db, tmp_path):
    csv = tmp_path / "lower.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        "foo,long,2025-01-02 14:30:00+00:00,21000.0,42.0,84,30.0,2025\n"
    )
    _ingest(db, csv)
    row = db.execute("SELECT direction FROM trades").fetchone()
    assert row == ("LONG",)


def test_hold_min_falls_back_to_hold_minutes(db, tmp_path):
    csv = tmp_path / "newcol.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_minutes,year\n"
        "foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,84,45.0,2025\n"
    )
    _ingest(db, csv)
    row = db.execute("SELECT hold_minutes FROM trades").fetchone()
    assert row == (45.0,)
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_trades.py -v`
Expected: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_trades.py
git commit -m "warehouse: trades-kind ingest with dynamic SELECT for legacy + extended"
```

---

### Task 10: `wfa_windows` kind ingest

Implements spec §5.3 `wfa_windows` kind.

**Files:**
- Modify: `tools/warehouse/ingest.py` (add `_ingest_wfa_windows`)
- Create: `tests/warehouse/test_ingest_wfa.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
# (Appended)

def _ingest_wfa_windows(
    con: duckdb.DuckDBPyConnection,
    *,
    csv_path: Path,
    run_id: str,
) -> int:
    """Insert wfa_windows rows. best_params handled via TRY_CAST + COALESCE so
    per-row cast failures never raise — they fall back to {"_raw": "<original>"}.

    A side-channel SELECT identifies the first failing row (if any) so we can
    log a warning. The bulk INSERT itself is one statement and never partially
    commits — safe inside the orchestrator's transaction.
    """
    before = con.execute("SELECT COUNT(*) FROM wfa_windows").fetchone()[0]

    # Detect failures up front for logging (does not affect insert correctness).
    first_bad = con.execute(
        "SELECT strategy, window_idx, best_params "
        "FROM read_csv_auto(?, header=true) "
        "WHERE best_params IS NOT NULL AND TRY_CAST(best_params AS JSON) IS NULL "
        "LIMIT 1",
        [str(csv_path)],
    ).fetchone()
    if first_bad is not None:
        log.warning(
            "wfa_windows: best_params parse failure in %s (first offending row: strategy=%r window=%r raw=%r); "
            "storing as {\"_raw\": ...}",
            csv_path, first_bad[0], first_bad[1], first_bad[2],
        )

    con.execute(
        """
        INSERT INTO wfa_windows
        SELECT
            ? AS run_id,
            strategy, window_idx, is_start, is_end, oos_start, oos_end,
            COALESCE(
                TRY_CAST(best_params AS JSON),
                CAST(json_object('_raw', CAST(best_params AS VARCHAR)) AS JSON)
            ) AS best_params,
            is_pf, is_trades, oos_pf, oos_trades, oos_net, wfe, degraded
        FROM read_csv_auto(?, header=true)
        """,
        [run_id, str(csv_path)],
    )

    after = con.execute("SELECT COUNT(*) FROM wfa_windows").fetchone()[0]
    return after - before
```

- [ ] **Step 2: Write `tests/warehouse/test_ingest_wfa.py`**

```python
# tests/warehouse/test_ingest_wfa.py
from __future__ import annotations
from tools.warehouse.ingest import insert_run, _ingest_wfa_windows, _resolve_friction
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def _setup(db, csv_path, kind_expected):
    sc = load_and_hash(csv_path)
    kind, _ = sniff_csv_kind(csv_path)
    assert kind == kind_expected
    insert_run(db, run_id=sc.run_id, source_filename=csv_path.name, csv_kind=kind,
               sidecar_result=sc, logical_group=None,
               friction_applied=_resolve_friction(sc, None),
               strategy=None)
    return sc


def test_wfa_windows_happy_path(db, fixtures_dir):
    sc = _setup(db, fixtures_dir / "kind_wfa_windows.csv", "wfa_windows")
    n = _ingest_wfa_windows(db, csv_path=fixtures_dir / "kind_wfa_windows.csv", run_id=sc.run_id)
    assert n == 1
    row = db.execute(
        "SELECT strategy, window_idx, best_params->>'$.ema_len' FROM wfa_windows"
    ).fetchone()
    assert row[0] == "foo"
    assert row[1] == 0
    assert row[2] == "21"      # JSON path returns text


def test_wfa_windows_handles_python_repr(db, tmp_path):
    csv = tmp_path / "wfa_repr.csv"
    csv.write_text(
        "strategy,window_idx,is_start,is_end,oos_start,oos_end,best_params,"
        "is_pf,is_trades,oos_pf,oos_trades,oos_net,wfe,degraded\n"
        "foo,0,2021-01-01,2021-12-31,2022-01-01,2022-03-31,"
        "\"{'ema_len': 21}\",1.5,100,1.2,30,500.0,0.8,false\n"
    )
    sc = _setup(db, csv, "wfa_windows")
    n = _ingest_wfa_windows(db, csv_path=csv, run_id=sc.run_id)
    assert n == 1
    bp = db.execute("SELECT best_params->>'$._raw' FROM wfa_windows").fetchone()
    assert bp[0] is not None       # raw payload was preserved
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_wfa.py -v`
Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_wfa.py
git commit -m "warehouse: wfa_windows ingest with best_params JSON + repr fallback"
```

---

### Task 11: `wfa_summary` kind ingest

**Files:**
- Modify: `tools/warehouse/ingest.py`
- Modify: `tests/warehouse/test_ingest_wfa.py` (add a test)

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
def _ingest_wfa_summary(
    con: duckdb.DuckDBPyConnection,
    *,
    csv_path: Path,
    run_id: str,
) -> int:
    before = con.execute("SELECT COUNT(*) FROM wfa_summary").fetchone()[0]
    con.execute(
        """
        INSERT INTO wfa_summary
        SELECT
            ? AS run_id,
            strategy, n_windows, mean_is_pf, mean_oos_pf, median_oos_pf,
            pct_windows_degraded, robust
        FROM read_csv_auto(?, header=true)
        """,
        [run_id, str(csv_path)],
    )
    after = con.execute("SELECT COUNT(*) FROM wfa_summary").fetchone()[0]
    return after - before
```

- [ ] **Step 2: Append to `tests/warehouse/test_ingest_wfa.py`**

```python
def test_wfa_summary(db, fixtures_dir):
    from tools.warehouse.ingest import _ingest_wfa_summary
    sc = _setup(db, fixtures_dir / "kind_wfa_summary.csv", "wfa_summary")
    n = _ingest_wfa_summary(db, csv_path=fixtures_dir / "kind_wfa_summary.csv", run_id=sc.run_id)
    assert n == 1
    row = db.execute(
        "SELECT strategy, mean_oos_pf, robust FROM wfa_summary"
    ).fetchone()
    assert row == ("foo", 1.3, True)
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_wfa.py -v`
Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_wfa.py
git commit -m "warehouse: wfa_summary ingest"
```

---

### Task 12: `summary` kind ingest (unpivot to `run_metrics`)

Implements spec §5.3 `summary` kind.

**Files:**
- Modify: `tools/warehouse/ingest.py`
- Create: `tests/warehouse/test_ingest_summary.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
def _ingest_summary(
    con: duckdb.DuckDBPyConnection,
    *,
    csv_path: Path,
    run_id: str,
    header: list[str],
) -> int:
    """Unpivot every numeric column into (metric_name, metric_value).
    String columns go to label_value. The 'strategy'/'name' identity column is skipped."""
    # Use DuckDB to read the file with inferred types, then iterate row 0 to
    # classify each non-identity column.
    df = con.execute(
        "SELECT * FROM read_csv_auto(?, header=true)", [str(csv_path)]
    ).fetchdf()
    id_col = header[0]    # 'strategy' or 'name' per sniff rule
    if id_col not in df.columns:
        raise ValueError(f"summary CSV missing identity column {id_col!r}")
    metric_cols = [c for c in df.columns if c != id_col]

    inserted = 0
    for _, row in df.iterrows():
        for col in metric_cols:
            val = row[col]
            metric_name = f"{row[id_col]}.{col}"
            is_num = isinstance(val, (int, float)) and not isinstance(val, bool)
            con.execute(
                "INSERT INTO run_metrics VALUES (?, ?, ?, ?)",
                [run_id, metric_name,
                 float(val) if is_num else None,
                 None if is_num else (str(val) if val is not None else None)],
            )
            inserted += 1
    return inserted
```

- [ ] **Step 2: Write `tests/warehouse/test_ingest_summary.py`**

```python
# tests/warehouse/test_ingest_summary.py
from __future__ import annotations
from tools.warehouse.ingest import insert_run, _ingest_summary, _resolve_friction
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def test_summary_unpivot(db, fixtures_dir):
    csv = fixtures_dir / "kind_summary.csv"
    sc = load_and_hash(csv)
    kind, header = sniff_csv_kind(csv)
    assert kind == "summary"
    insert_run(db, run_id=sc.run_id, source_filename=csv.name, csv_kind=kind,
               sidecar_result=sc, logical_group=None,
               friction_applied=_resolve_friction(sc, None), strategy=None)
    n = _ingest_summary(db, csv_path=csv, run_id=sc.run_id, header=header)
    assert n == 4    # profit_factor, sharpe, win_rate, max_dd
    rows = db.execute(
        "SELECT metric_name, metric_value FROM run_metrics ORDER BY metric_name"
    ).fetchall()
    names = {r[0] for r in rows}
    assert names == {"foo.profit_factor", "foo.sharpe", "foo.win_rate", "foo.max_dd"}
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_summary.py -v`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_summary.py
git commit -m "warehouse: summary-kind unpivot into run_metrics"
```

---

### Task 13: `mixed` kind ingest

Implements spec §5.3 `mixed` kind: trade rows go to `trades`, the (constant) aggregate columns become one set of `run_metrics`.

**Files:**
- Modify: `tools/warehouse/ingest.py`
- Create: `tests/warehouse/test_ingest_mixed.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
def _ingest_mixed(
    con: duckdb.DuckDBPyConnection,
    *,
    csv_path: Path,
    run_id: str,
    header: list[str],
) -> tuple[int, int]:
    """Returns (rows_inserted_into_trades, metrics_inserted)."""
    _METRIC_NAMES = {"profit_factor", "sharpe", "win_rate", "max_dd", "n_trades"}
    metric_cols = [c for c in header if c in _METRIC_NAMES]

    # Verify the metric columns are constant across all rows; otherwise this
    # CSV is malformed for the mixed-kind contract.
    if metric_cols:
        distinct_q = ", ".join(f"COUNT(DISTINCT {c})" for c in metric_cols)
        counts = con.execute(
            f"SELECT {distinct_q} FROM read_csv_auto(?, header=true)", [str(csv_path)]
        ).fetchone()
        if any(c > 1 for c in counts):
            raise ValueError(
                f"mixed-kind CSV has non-constant aggregate columns {metric_cols} in {csv_path}"
            )

    rows_inserted = _ingest_trades(con, csv_path=csv_path, run_id=run_id, header=header)

    metrics_inserted = 0
    if metric_cols:
        col_list = ", ".join(metric_cols)
        first = con.execute(
            f"SELECT {col_list} FROM read_csv_auto(?, header=true) LIMIT 1", [str(csv_path)]
        ).fetchone()
        for col, val in zip(metric_cols, first):
            is_num = isinstance(val, (int, float)) and not isinstance(val, bool)
            con.execute(
                "INSERT INTO run_metrics VALUES (?, ?, ?, ?)",
                [run_id, col, float(val) if is_num else None,
                 None if is_num else (str(val) if val is not None else None)],
            )
            metrics_inserted += 1
    return rows_inserted, metrics_inserted
```

- [ ] **Step 2: Write `tests/warehouse/test_ingest_mixed.py`**

```python
# tests/warehouse/test_ingest_mixed.py
from __future__ import annotations
import pytest

from tools.warehouse.ingest import insert_run, _ingest_mixed, _resolve_friction
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def test_mixed_inserts_both(db, fixtures_dir):
    csv = fixtures_dir / "kind_mixed.csv"
    sc = load_and_hash(csv)
    kind, header = sniff_csv_kind(csv)
    assert kind == "mixed"
    insert_run(db, run_id=sc.run_id, source_filename=csv.name, csv_kind=kind,
               sidecar_result=sc, logical_group=None,
               friction_applied=_resolve_friction(sc, None), strategy=None)
    n_trades, n_metrics = _ingest_mixed(db, csv_path=csv, run_id=sc.run_id, header=header)
    assert n_trades == 2
    assert n_metrics == 2  # profit_factor + n_trades
    trade_count = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    metric_count = db.execute("SELECT COUNT(*) FROM run_metrics").fetchone()[0]
    assert trade_count == 2
    assert metric_count == 2


def test_mixed_rejects_inconsistent_metric(db, tmp_path):
    csv = tmp_path / "bad_mixed.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,profit_factor\n"
        "foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,1.5\n"
        "foo,LONG,2025-01-03 14:30:00+00:00,21100.0,10.0,9.9\n"   # PF differs!
    )
    sc = load_and_hash(csv)
    kind, header = sniff_csv_kind(csv)
    assert kind == "mixed"
    insert_run(db, run_id=sc.run_id, source_filename=csv.name, csv_kind=kind,
               sidecar_result=sc, logical_group=None,
               friction_applied=_resolve_friction(sc, None), strategy=None)
    with pytest.raises(ValueError, match="non-constant aggregate"):
        _ingest_mixed(db, csv_path=csv, run_id=sc.run_id, header=header)
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_mixed.py -v`
Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_mixed.py
git commit -m "warehouse: mixed-kind ingest (trades + constant metrics)"
```

---

### Task 14: `derived` kind ingest (lazy `import_<name>`)

Implements spec §5.3 `derived` kind, including the schema-drift handling rules.

**Files:**
- Modify: `tools/warehouse/ingest.py`
- Create: `tests/warehouse/test_ingest_derived.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
def _ingest_derived(
    con: duckdb.DuckDBPyConnection,
    *,
    csv_path: Path,
    run_id: str,
) -> tuple[str, int]:
    """Lazy CREATE TABLE import_<name>, then INSERT all rows tagged with run_id.
    Returns (table_name, rows_inserted)."""
    from tools.warehouse.sniff import safe_import_table_name
    table = safe_import_table_name(csv_path)

    # Check existence.
    exists_row = con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchone()

    if exists_row is None:
        # First ingest: define schema from inference + add run_id column.
        con.execute(
            f"CREATE TABLE {table} AS "
            f"SELECT *, NULL::VARCHAR AS run_id FROM read_csv_auto(?, header=true) WHERE 1=0",
            [str(csv_path)],
        )
    else:
        # Existing table: compare columns; ALTER ADD COLUMN for additive drift, reject otherwise.
        existing_cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name=?", [table]
        ).fetchall()}
        incoming = con.execute(
            "DESCRIBE SELECT * FROM read_csv_auto(?, header=true)", [str(csv_path)]
        ).fetchdf()
        incoming_cols = set(incoming["column_name"])
        removed = existing_cols - incoming_cols - {"run_id"}
        added = incoming_cols - existing_cols
        if removed:
            raise ValueError(
                f"derived schema drift: column(s) removed {sorted(removed)} in {csv_path}; "
                "drop the import table manually if you want to re-import"
            )
        for col in added:
            dtype = incoming[incoming["column_name"] == col]["column_type"].iloc[0]
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")

    before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.execute(
        f"INSERT INTO {table} BY NAME "
        f"SELECT *, ? AS run_id FROM read_csv_auto(?, header=true)",
        [run_id, str(csv_path)],
    )
    after = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return table, after - before
```

- [ ] **Step 2: Create fixture `tests/warehouse/fixtures/phase1_strategy_summary_sample.csv` if not already created in Task 6**

Verify it exists. If missing:
```csv
strategy,n,net_pnl,win_rate
foo,100,5000.0,0.55
```

- [ ] **Step 3: Write `tests/warehouse/test_ingest_derived.py`**

```python
# tests/warehouse/test_ingest_derived.py
from __future__ import annotations
import pytest

from tools.warehouse.ingest import insert_run, _ingest_derived, _resolve_friction
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def _ingest(db, csv_path):
    sc = load_and_hash(csv_path)
    kind, _ = sniff_csv_kind(csv_path)
    assert kind == "derived"
    insert_run(db, run_id=sc.run_id, source_filename=csv_path.name, csv_kind=kind,
               sidecar_result=sc, logical_group=None,
               friction_applied=_resolve_friction(sc, None), strategy=None)
    return _ingest_derived(db, csv_path=csv_path, run_id=sc.run_id)


def test_derived_creates_table_and_inserts(db, fixtures_dir):
    table, n = _ingest(db, fixtures_dir / "phase1_strategy_summary_sample.csv")
    assert table == "import_phase1_strategy_summary_sample"
    assert n == 1
    row = db.execute(f"SELECT strategy, net_pnl, run_id FROM {table}").fetchone()
    assert row[0] == "foo" and row[1] == 5000.0 and row[2] is not None


def test_derived_additive_drift_alter(db, tmp_path):
    csv1 = tmp_path / "phase1_drift.csv"
    csv1.write_text("strategy,n\nfoo,100\n")
    _ingest(db, csv1)
    # Now write a new file with the same name+content but with an added column.
    # We use a different stem so we'd create a different import table — instead,
    # write to a tmp dir with same stem.
    tmp2 = tmp_path / "round2"
    tmp2.mkdir()
    csv2 = tmp2 / "phase1_drift.csv"
    csv2.write_text("strategy,n,extra\nbar,200,x\n")
    _ingest(db, csv2)
    cols = {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='import_phase1_drift'"
    ).fetchall()}
    assert "extra" in cols


def test_derived_destructive_drift_rejected(db, tmp_path):
    csv1 = tmp_path / "phase1_destr.csv"
    csv1.write_text("strategy,n,gone\nfoo,100,x\n")
    _ingest(db, csv1)
    tmp2 = tmp_path / "round2"; tmp2.mkdir()
    csv2 = tmp2 / "phase1_destr.csv"
    csv2.write_text("strategy,n\nbar,200\n")
    with pytest.raises(ValueError, match="column.s. removed"):
        _ingest(db, csv2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_derived.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_derived.py
git commit -m "warehouse: derived-kind lazy import_<name> with drift handling"
```

---

### Task 15: `ingest_csv()` orchestrator + content-hash dedup + per-file transaction

Wires everything from Tasks 8-14 into a single `ingest_csv()` entry point. Adds dedup check (spec §5.1 step 2), per-file BEGIN/COMMIT, and error capture.

**Files:**
- Modify: `tools/warehouse/ingest.py` (add `ingest_csv`)
- Create: `tests/warehouse/test_ingest_csv_orchestration.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
def ingest_csv(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    *,
    logical_group: str | None = None,
    mark_friction_applied: bool | None = None,
    known_strategies: frozenset[str] | None = None,
) -> IngestResult:
    """Atomic per-file ingest. Returns an IngestResult (never raises for
    expected error classes — caller decides whether to abort the batch)."""
    from tools.warehouse.sniff import sniff_strategy_from_filename

    csv_path = Path(csv_path).resolve()

    try:
        sc = load_and_hash(csv_path)
    except UnsupportedSidecarSchema as e:
        return IngestResult(csv_path=csv_path, run_id=None, status="error",
                            csv_kind=None, error=str(e))

    if _exists(con, sc.run_id):
        kind, _ = sniff_csv_kind(csv_path)
        return IngestResult(csv_path=csv_path, run_id=sc.run_id,
                            status="skipped_duplicate", csv_kind=kind)

    kind, header = sniff_csv_kind(csv_path)
    if kind == "error":
        return IngestResult(csv_path=csv_path, run_id=sc.run_id, status="error",
                            csv_kind=kind, error="unknown_csv_kind")

    # Determine run-level denormalized strategy.
    strategy = sc.sidecar.get("strategy")
    if strategy is None and kind == "wfa_windows" and known_strategies is not None:
        strategy = sniff_strategy_from_filename(csv_path, known_strategies)

    friction = _resolve_friction(sc, mark_friction_applied)
    lg = logical_group if logical_group is not None else sc.sidecar.get("logical_group")

    con.execute("BEGIN")
    try:
        insert_run(con, run_id=sc.run_id, source_filename=csv_path.name,
                   csv_kind=kind, sidecar_result=sc, logical_group=lg,
                   friction_applied=friction, strategy=strategy)
        rows = 0
        metrics = 0
        if kind == "trades":
            rows = _ingest_trades(con, csv_path=csv_path, run_id=sc.run_id, header=header)
        elif kind == "wfa_windows":
            rows = _ingest_wfa_windows(con, csv_path=csv_path, run_id=sc.run_id)
        elif kind == "wfa_summary":
            rows = _ingest_wfa_summary(con, csv_path=csv_path, run_id=sc.run_id)
        elif kind == "summary":
            metrics = _ingest_summary(con, csv_path=csv_path, run_id=sc.run_id, header=header)
        elif kind == "mixed":
            rows, metrics = _ingest_mixed(con, csv_path=csv_path, run_id=sc.run_id, header=header)
        elif kind == "derived":
            _, rows = _ingest_derived(con, csv_path=csv_path, run_id=sc.run_id)
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        return IngestResult(csv_path=csv_path, run_id=sc.run_id, status="error",
                            csv_kind=kind, error=f"{type(e).__name__}: {e}")

    return IngestResult(
        csv_path=csv_path, run_id=sc.run_id, status="inserted", csv_kind=kind,
        rows_inserted=rows, metrics_inserted=metrics,
    )
```

- [ ] **Step 2: Write `tests/warehouse/test_ingest_csv_orchestration.py`**

```python
# tests/warehouse/test_ingest_csv_orchestration.py
from __future__ import annotations

from tools.warehouse.ingest import ingest_csv


def test_dedup_on_second_call(db, fixtures_dir):
    r1 = ingest_csv(db, fixtures_dir / "kind_trades.csv")
    assert r1.status == "inserted"
    r2 = ingest_csv(db, fixtures_dir / "kind_trades.csv")
    assert r2.status == "skipped_duplicate"
    assert r2.run_id == r1.run_id
    n = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert n == 1


def test_unknown_csv_returns_error_no_rollback_needed(db, fixtures_dir):
    r = ingest_csv(db, fixtures_dir / "kind_unknown.csv")
    assert r.status == "error"
    assert r.error == "unknown_csv_kind"
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_error_inside_transaction_rolls_back(db, tmp_path):
    # Malformed trades CSV: bad timestamp will trip read_csv_auto inside the tx.
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        "foo,LONG,not-a-timestamp,21000.0,42.0,84,30.0,2025\n"
    )
    r = ingest_csv(db, csv)
    assert r.status == "error"
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_logical_group_persisted(db, fixtures_dir):
    r = ingest_csv(db, fixtures_dir / "kind_wfa_windows.csv", logical_group="phase13_wfa")
    assert r.status == "inserted"
    lg = db.execute("SELECT logical_group FROM runs").fetchone()[0]
    assert lg == "phase13_wfa"
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_ingest_csv_orchestration.py -v`
Expected: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_ingest_csv_orchestration.py
git commit -m "warehouse: ingest_csv orchestrator with dedup + per-file transaction"
```

---

### Task 16: JSONL error log

Implements spec §7 (error log format).

**Files:**
- Modify: `tools/warehouse/ingest.py` (add `log_error_jsonl`)
- Create: `tests/warehouse/test_error_log.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
def log_error_jsonl(log_path: Path, result: IngestResult, header: list[str] | None = None) -> None:
    """Append a JSONL record describing an error result to log_path."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": "error",
        "file": str(result.csv_path),
        "error_class": result.error,
        "csv_kind": result.csv_kind,
        "run_id": result.run_id,
        "header": header or [],
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
```

- [ ] **Step 2: Write `tests/warehouse/test_error_log.py`**

```python
# tests/warehouse/test_error_log.py
from __future__ import annotations
import json

from tools.warehouse.ingest import IngestResult, log_error_jsonl
from pathlib import Path


def test_appends_jsonl(tmp_path):
    log = tmp_path / "errors.log"
    r = IngestResult(csv_path=Path("foo.csv"), run_id="abc", status="error",
                     csv_kind="trades", error="unknown_csv_kind")
    log_error_jsonl(log, r, header=["a", "b"])
    log_error_jsonl(log, r, header=["a", "b"])
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert obj["error_class"] == "unknown_csv_kind"
        assert obj["header"] == ["a", "b"]
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_error_log.py -v`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_error_log.py
git commit -m "warehouse: JSONL error log"
```

---

### Task 17: `scan_dir()` with glob safety

Implements spec §5.7 (glob defaults + skip list).

**Files:**
- Modify: `tools/warehouse/ingest.py`
- Create: `tests/warehouse/test_scan_dir.py`

- [ ] **Step 1: Append to `tools/warehouse/ingest.py`**

```python
_SKIP_PATH_COMPONENTS = {"tests", "fixtures", ".pytest_cache", "__pycache__", "node_modules"}


def scan_dir(
    con: duckdb.DuckDBPyConnection,
    dir_path: Path,
    *,
    glob: str = "*.csv",
    recursive: bool = False,
    logical_group: str | None = None,
    mark_friction_applied: bool | None = None,
    known_strategies: frozenset[str] | None = None,
    error_log: Path | None = None,
) -> list[IngestResult]:
    """Ingest every CSV in dir_path matching `glob`. Skips test/fixture paths."""
    dir_path = Path(dir_path).resolve()
    pattern = ("**/" + glob) if recursive else glob
    results: list[IngestResult] = []
    for path in sorted(dir_path.glob(pattern)):
        if any(p in _SKIP_PATH_COMPONENTS for p in path.parts):
            log.debug("scan_dir: skipping %s (matched skip list)", path)
            continue
        if not path.is_file():
            continue
        r = ingest_csv(con, path, logical_group=logical_group,
                       mark_friction_applied=mark_friction_applied,
                       known_strategies=known_strategies)
        results.append(r)
        if r.status == "error" and error_log is not None:
            log_error_jsonl(error_log, r)
    return results
```

- [ ] **Step 2: Write `tests/warehouse/test_scan_dir.py`**

```python
# tests/warehouse/test_scan_dir.py
from __future__ import annotations

from tools.warehouse.ingest import scan_dir


def _make_trades_csv(p, val=42.0):
    p.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        f"foo,LONG,2025-01-02 14:30:00+00:00,21000.0,{val},84,30.0,2025\n"
    )


def test_default_glob_one_level(db, tmp_path):
    _make_trades_csv(tmp_path / "a.csv", val=1.0)
    sub = tmp_path / "sub"; sub.mkdir()
    _make_trades_csv(sub / "b.csv", val=2.0)
    rs = scan_dir(db, tmp_path)
    files = {r.csv_path.name for r in rs}
    assert files == {"a.csv"}, "default scan must not recurse"


def test_recursive_flag(db, tmp_path):
    _make_trades_csv(tmp_path / "a.csv", val=1.0)
    sub = tmp_path / "sub"; sub.mkdir()
    _make_trades_csv(sub / "b.csv", val=2.0)
    rs = scan_dir(db, tmp_path, recursive=True)
    files = {r.csv_path.name for r in rs}
    assert files == {"a.csv", "b.csv"}


def test_skip_components(db, tmp_path):
    _make_trades_csv(tmp_path / "a.csv", val=1.0)
    fix = tmp_path / "fixtures"; fix.mkdir()
    _make_trades_csv(fix / "b.csv", val=2.0)
    rs = scan_dir(db, tmp_path, recursive=True)
    files = {r.csv_path.name for r in rs}
    assert files == {"a.csv"}, "fixtures path must be skipped"


def test_error_log_written(db, tmp_path):
    bad = tmp_path / "junk.csv"
    bad.write_text("nope_col\n1\n")          # unknown_csv_kind
    log = tmp_path / "errors.log"
    scan_dir(db, tmp_path, error_log=log)
    assert log.exists()
    assert "unknown_csv_kind" in log.read_text()
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_scan_dir.py -v`
Expected: 4 passed.

- [ ] **Step 4: Commit**

```bash
git add tools/warehouse/ingest.py tests/warehouse/test_scan_dir.py
git commit -m "warehouse: scan_dir with glob + skip-list defaults"
```

---

### Task 18: `trades_ct` view correctness

The view is defined in `schema.sql` (Task 2). This task only verifies it produces correct CT-derived columns for the documented edge cases.

**Files:**
- Create: `tests/warehouse/test_trades_ct.py`

- [ ] **Step 1: Write `tests/warehouse/test_trades_ct.py`**

```python
# tests/warehouse/test_trades_ct.py
"""Verify trades_ct view yields correct CT-derived columns."""
from __future__ import annotations
from datetime import date

from tools.warehouse.ingest import ingest_csv


def _trade_csv(tmp_path, entry_ts):
    p = tmp_path / "trade.csv"
    p.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        f"foo,LONG,{entry_ts},21000.0,42.0,84,30.0,2025\n"
    )
    return p


def test_session_open_zero(db, tmp_path):
    # 08:30 CST = 14:30 UTC on a winter day (CT = UTC-6 in winter when CST applies).
    p = _trade_csv(tmp_path, "2025-01-02 14:30:00+00:00")
    ingest_csv(db, p)
    row = db.execute(
        "SELECT session_date, market_open_minutes FROM trades_ct"
    ).fetchone()
    assert row[0] == date(2025, 1, 2)
    assert abs(row[1] - 0.0) < 1e-6, f"expected ~0, got {row[1]}"


def test_session_open_plus_30(db, tmp_path):
    p = _trade_csv(tmp_path, "2025-01-02 15:00:00+00:00")   # 09:00 CT
    ingest_csv(db, p)
    val = db.execute("SELECT market_open_minutes FROM trades_ct").fetchone()[0]
    assert abs(val - 30.0) < 1e-6


def test_globex_negative_minutes(db, tmp_path):
    # 06:00 CT = 12:00 UTC (winter). market_open_minutes should be ~-150.
    p = _trade_csv(tmp_path, "2025-01-02 12:00:00+00:00")
    ingest_csv(db, p)
    val = db.execute("SELECT market_open_minutes FROM trades_ct").fetchone()[0]
    assert val < 0
    assert abs(val - (-150.0)) < 1e-6


def test_session_date_uses_ct_calendar(db, tmp_path):
    # 23:30 CT on 2025-01-02 = 05:30 UTC on 2025-01-03. session_date must be 2025-01-02.
    p = _trade_csv(tmp_path, "2025-01-03 05:30:00+00:00")
    ingest_csv(db, p)
    sd = db.execute("SELECT session_date FROM trades_ct").fetchone()[0]
    assert sd == date(2025, 1, 2)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_trades_ct.py -v`
Expected: 4 passed. **If any fail**, this likely means DuckDB's `AT TIME ZONE` on your platform doesn't honor `America/Chicago` DST correctly — verify the timezone DB is installed (DuckDB ships its own ICU on most platforms). On Windows, DuckDB uses ICU which understands `America/Chicago`.

- [ ] **Step 3: Commit**

```bash
git add tests/warehouse/test_trades_ct.py
git commit -m "warehouse: verify trades_ct CT conversion incl. Globex negatives"
```

---

### Task 19: CLI

Implements spec §5 CLI surface.

**Files:**
- Create: `tools/warehouse/__main__.py`
- Create: `tools/warehouse/cli.py`
- Create: `tests/warehouse/test_cli.py`

- [ ] **Step 1: Write `tools/warehouse/cli.py`**

```python
# tools/warehouse/cli.py
"""CLI for the Phoenix backtest warehouse.

Usage:
    python -m tools.warehouse ingest <path> [--recursive] [--logical-group NAME]
                                            [--mark-friction-applied] [--dry-run]
    python -m tools.warehouse status
"""
from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path

from tools.warehouse.db import open_db, apply_schema
from tools.warehouse.ingest import ingest_csv, scan_dir
from tools.warehouse.lock import ingest_lock
from tools.warehouse.known_strategies import load_known_strategies


DEFAULT_DB = Path("data/warehouse/phoenix.duckdb")
DEFAULT_LOCK = Path("data/warehouse/.ingest.lock")
DEFAULT_ERRLOG = Path("data/warehouse/ingest_errors.log")


def _cmd_ingest(args) -> int:
    path = Path(args.path).resolve()
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        print(f"[dry-run] Would open {db_path} and ingest {path}")
        return 0

    known = None
    try:
        known = load_known_strategies()
    except Exception as e:
        print(f"warning: could not load known_strategies ({e}); WFA filename sniff disabled",
              file=sys.stderr)

    with ingest_lock(DEFAULT_LOCK):
        con = open_db(db_path)
        try:
            apply_schema(con)
            if path.is_dir():
                results = scan_dir(
                    con, path,
                    recursive=args.recursive,
                    logical_group=args.logical_group,
                    mark_friction_applied=args.mark_friction_applied or None,
                    known_strategies=known,
                    error_log=DEFAULT_ERRLOG,
                )
            else:
                results = [ingest_csv(
                    con, path,
                    logical_group=args.logical_group,
                    mark_friction_applied=args.mark_friction_applied or None,
                    known_strategies=known,
                )]
        finally:
            con.close()

    inserted = sum(1 for r in results if r.status == "inserted")
    skipped = sum(1 for r in results if r.status == "skipped_duplicate")
    errors = sum(1 for r in results if r.status == "error")
    print(f"ingested={inserted} skipped_duplicate={skipped} errors={errors}")
    return 1 if errors else 0


def _cmd_status(args) -> int:
    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"no warehouse at {db_path}")
        return 0
    con = open_db(db_path)
    try:
        for table in ("runs", "trades", "wfa_windows", "wfa_summary", "run_metrics"):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:>14}: {n:>10,}")
        last = con.execute(
            "SELECT max(ingested_at) FROM runs"
        ).fetchone()[0]
        print(f"  last_ingest: {last}")
    finally:
        con.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.warehouse")
    p.add_argument("--db", default=str(DEFAULT_DB), help="DuckDB file path")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Ingest a CSV file or directory")
    ing.add_argument("path")
    ing.add_argument("--recursive", action="store_true")
    ing.add_argument("--logical-group", default=None)
    ing.add_argument("--mark-friction-applied", action="store_true")
    ing.add_argument("--dry-run", action="store_true")
    ing.set_defaults(func=_cmd_ingest)

    st = sub.add_parser("status", help="Show table row counts")
    st.set_defaults(func=_cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)
```

- [ ] **Step 2: Write `tools/warehouse/__main__.py`**

```python
# tools/warehouse/__main__.py
from tools.warehouse.cli import main
import sys
sys.exit(main())
```

- [ ] **Step 3: Write `tests/warehouse/test_cli.py`**

```python
# tests/warehouse/test_cli.py
from __future__ import annotations
import sys
from pathlib import Path

from tools.warehouse.cli import main


def _make_trades_csv(p):
    p.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        "foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,84,30.0,2025\n"
    )


def test_cli_ingest_file(tmp_path, monkeypatch):
    db = tmp_path / "phx.duckdb"
    lock = tmp_path / "lock"
    monkeypatch.setattr("tools.warehouse.cli.DEFAULT_LOCK", lock)
    monkeypatch.setattr("tools.warehouse.cli.DEFAULT_ERRLOG", tmp_path / "err.log")
    csv = tmp_path / "t.csv"
    _make_trades_csv(csv)
    rc = main(["--db", str(db), "ingest", str(csv)])
    assert rc == 0
    assert db.exists()


def test_cli_status_on_empty(tmp_path):
    rc = main(["--db", str(tmp_path / "missing.duckdb"), "status"])
    assert rc == 0


def test_cli_dry_run_no_write(tmp_path, capsys):
    csv = tmp_path / "t.csv"; _make_trades_csv(csv)
    rc = main(["--db", str(tmp_path / "x.duckdb"), "ingest", str(csv), "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "x.duckdb").exists()
    out = capsys.readouterr().out
    assert "[dry-run]" in out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/warehouse/test_cli.py -v`
Expected: 3 passed.

- [ ] **Step 5: Smoke-check the actual CLI manually**

Run: `python -m tools.warehouse --help`
Expected: prints usage with `ingest` and `status` subcommands.

Run: `python -m tools.warehouse status`
Expected: `no warehouse at ...phoenix.duckdb` (database doesn't exist yet — correct).

- [ ] **Step 6: Commit**

```bash
git add tools/warehouse/cli.py tools/warehouse/__main__.py tests/warehouse/test_cli.py
git commit -m "warehouse: CLI with ingest and status subcommands"
```

---

### Task 20: Real-CSV smoke test (Layer 2)

Implements spec §8.2.

**Files:**
- Create: `tests/warehouse/test_smoke.py`

- [ ] **Step 1: Write `tests/warehouse/test_smoke.py`**

```python
# tests/warehouse/test_smoke.py
"""Layer 2 smoke test — runs against the LIVE backtest_results dir.

Marked `smoke`; opt-in via `pytest -m smoke`. Skipped by default.
"""
from __future__ import annotations
from pathlib import Path
import shutil
import pytest

from tools.warehouse.cli import main as cli_main


PROJECT_ROOT = Path(r"C:\Trading Project\phoenix_bot")
PORTFOLIO_DIR = PROJECT_ROOT / "backtest_results" / "portfolio_framework"
LEGACY_DIR    = PROJECT_ROOT / "backtest_results"
TMP_DB        = PROJECT_ROOT / ".tmp" / "smoke_phoenix.duckdb"


@pytest.fixture
def fresh_db():
    if TMP_DB.exists():
        TMP_DB.unlink()
    if TMP_DB.with_suffix(".duckdb.wal").exists():
        TMP_DB.with_suffix(".duckdb.wal").unlink()
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    yield TMP_DB


@pytest.mark.smoke
def test_smoke_portfolio_framework(fresh_db):
    rc = cli_main(["--db", str(fresh_db), "ingest", str(PORTFOLIO_DIR)])
    assert rc == 0
    import duckdb
    con = duckdb.connect(str(fresh_db))
    try:
        n_trades = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n_trades >= 70_000, f"expected >=70k trades, got {n_trades}"
        n_wfa = con.execute("SELECT COUNT(*) FROM wfa_windows").fetchone()[0]
        assert n_wfa >= 210
        n_wfa_sum = con.execute("SELECT COUNT(*) FROM wfa_summary").fetchone()[0]
        assert n_wfa_sum >= 14
        recent = con.execute(
            "SELECT COUNT(*) FROM trades_ct WHERE session_date >= '2024-01-01'"
        ).fetchone()[0]
        assert recent > 0
    finally:
        con.close()


@pytest.mark.smoke
def test_smoke_legacy_csvs(fresh_db):
    # Ingest portfolio_framework first, then the 55 legacy CSVs at backtest_results/ root.
    cli_main(["--db", str(fresh_db), "ingest", str(PORTFOLIO_DIR)])
    rc = cli_main(["--db", str(fresh_db), "ingest", str(LEGACY_DIR), "--recursive"])
    assert rc == 0
    import duckdb
    con = duckdb.connect(str(fresh_db))
    try:
        runs = con.execute(
            "SELECT COUNT(*) FROM runs WHERE friction_applied = false"
        ).fetchone()[0]
        assert runs > 0, "expected at least one legacy run with friction_applied=false"
    finally:
        con.close()
```

- [ ] **Step 2: Register the `smoke` marker in `pytest.ini` (no-op if already there)**

Append to `pytest.ini` if a `markers =` block doesn't exist:

```
markers =
    smoke: end-to-end tests that touch real filesystem data
```

- [ ] **Step 3: Confirm Layer 1 tests still pass (regression check)**

Run: `pytest tests/warehouse -v --ignore=tests/warehouse/test_smoke.py`
Expected: all tests from Tasks 2-19 pass (~50+ tests).

- [ ] **Step 4: Run the smoke test manually**

Run: `pytest tests/warehouse/test_smoke.py -v -m smoke`
Expected: 2 passed, taking roughly 5-15 seconds. **If the trades floor fails**, capture the actual count and confirm whether multi_day landed early (raising the floor) or a regression dropped it.

- [ ] **Step 5: Commit**

```bash
git add tests/warehouse/test_smoke.py pytest.ini
git commit -m "warehouse: layer-2 smoke test against real CSVs (opt-in)"
```

---

### Task 21: Runbook

Implements spec §8.3 (operator runbook = the v1 acceptance gate).

**Files:**
- Create: `docs/superpowers/specs/2026-05-31-warehouse-runbook.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Phoenix Backtest Warehouse — v1 Runbook

> Acceptance procedure for the warehouse v1 cutover. See the spec:
> [`2026-05-31-backtest-warehouse-design.md`](2026-05-31-backtest-warehouse-design.md).

## Prereqs
- `pip install "duckdb>=1.1"` succeeded
- All Layer-1 tests pass: `pytest tests/warehouse -v --ignore=tests/warehouse/test_smoke.py`

## Step 1 — Ingest the portfolio_framework CSVs

```cmd
cd "C:\Trading Project\phoenix_bot"
python -m tools.warehouse ingest backtest_results\portfolio_framework
```

Expected: `ingested=15 skipped_duplicate=0 errors=0`.

## Step 2 — Ingest the legacy CSVs

```cmd
python -m tools.warehouse ingest backtest_results --recursive
```

Expected: `ingested=` matches the count of CSVs under `backtest_results\` minus the 15 already counted in Step 1 (subdirectories under `portfolio_framework/` re-hash to the same `run_id` and skip).
Expected: `errors=0`. Any errors mean a CSV doesn't match a known kind — inspect `data\warehouse\ingest_errors.log`.

## Step 3 — Inspect the warehouse

```cmd
python -m tools.warehouse status
```

Expected output (approximate):
```
         runs:         70
       trades:    76,000+
  wfa_windows:        255+
  wfa_summary:         14+
  run_metrics:        100+
  last_ingest: 2026-05-31 ...
```

## Step 4 — Run the four spec example queries

Open DuckDB CLI:
```cmd
duckdb data\warehouse\phoenix.duckdb
```

Paste the queries from `2026-05-31-backtest-warehouse-design.md` §9 and eyeball results:
1. "Best PF strategies in last 12 months" — top of the list should be familiar names.
2. "TOD session attribution" — `Opening Drive` and `Power Hour` rows should have the largest trade counts.
3. "Compare gross-PnL legacy era vs friction-net era" — `vwap_pullback_v2` should have rows for `friction_applied=true` AND `false`.
4. "WFA robust strategies" — should return the strategies INVENTORY documents as robust.

## Step 5 — Record the run

Write the row counts and any `ingest_errors.log` content into:
- `docs/RECENT_CHANGES.md` (or wherever new infra goes in your log)

## Step 6 — When multi_day lands

After the consolidation agent re-runs `_wfa_merge.py` and drops `wfa_windows_p13_multi_day.csv` (+ refreshed `wfa_summary.csv`):

```cmd
python -m tools.warehouse ingest backtest_results\portfolio_framework
```

Expected: `ingested=2 skipped_duplicate=13`. The two new files (multi_day window CSV + refreshed wfa_summary.csv) get new `run_id`s; the original 13 CSVs are content-hash-unchanged and skip cleanly.

Re-run Step 3 to confirm `wfa_windows` grew by ~15 rows and `wfa_summary` gained a second row per strategy.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-05-31-warehouse-runbook.md
git commit -m "warehouse: v1 acceptance runbook"
```

---

### Task 22: Run the runbook against real data and record results

This is the v1 acceptance gate. Not a code task — an operator procedure.

- [ ] **Step 1: Execute the runbook step by step**

Follow `docs/superpowers/specs/2026-05-31-warehouse-runbook.md` end to end against the real `C:\Trading Project\phoenix_bot\backtest_results\` directories.

- [ ] **Step 2: Record outcomes**

Append a section to the runbook documenting:
- Actual row counts after Step 1 and Step 2
- Any `ingest_errors.log` entries
- Output of the four spec queries (one-line summary each)

If anything failed:
- Investigate before declaring v1 done
- Common cause: a legacy CSV with a non-matching header (filed under `error_class='unknown_csv_kind'`)
- Fix: either add a sniff rule for it (with a test) or document it as out-of-scope

- [ ] **Step 3: Final commit**

```bash
git add docs/superpowers/specs/2026-05-31-warehouse-runbook.md
git commit -m "warehouse: record v1 runbook execution results"
```

---

## Spec coverage check (self-review)

| Spec section | Implemented by |
|---|---|
| §1 Context | n/a (motivation only) |
| §2 Decisions log | n/a (reference) |
| §3 File layout | Task 1 (dirs, .gitignore), Task 2 (schema.sql), Task 19 (cli.py) |
| §4 Schema | Task 2 (full DDL incl. trades_ct view, JSON ext bootstrap) |
| §5.1 Per-file pipeline | Task 15 (orchestrator) |
| §5.2 Kind sniffer | Task 6 |
| §5.3 trades kind | Task 9 |
| §5.3 wfa_windows kind | Task 10 |
| §5.3 wfa_summary kind | Task 11 |
| §5.3 summary kind | Task 12 |
| §5.3 mixed kind | Task 13 |
| §5.3 derived kind (lazy import_<name>, safe naming, drift) | Task 14 + Task 7 (safe_import_table_name) |
| §5.4 WFA filename sniff | Task 7 |
| §5.5 Sidecar contract | Task 4 |
| §5.6 PID lock with try/finally | Task 3 + Task 19 (CLI wires it via `with ingest_lock(...)`) |
| §5.7 Glob safety | Task 17 |
| §5.8 Out of scope for v1 | n/a (deliberately not implemented) |
| §6 Data flow — Lifecycles A/B/C/D | Task 15 (orchestrator) + Task 17 (scan_dir for shard globs) |
| §7 Error handling — all rows | Task 3 (lock), Task 14 (derived drift), Task 15 (rollback), Task 16 (JSONL log), Task 10 (best_params fallback) |
| §8.1 Layer-1 fixture tests | Tasks 2-19 (one test file each) |
| §8.2 Layer-2 smoke test | Task 20 |
| §8.3 Layer-3 manual runbook | Tasks 21-22 |
| §9 Example queries | Task 21 (runbook references them) |
| §10 Cross-run P&L caveat | n/a (documentation; surfaced by Task 21 runbook step 4) |
| §11 Cleanup queries | n/a (documented in spec; not part of code) |
| §12 Coordination items | n/a (external agent; status frozen in spec §12) |
| §13 Out of scope / future | n/a (intentionally deferred) |
| §14 Glossary | n/a (reference) |

No spec section is missing a task.
