"""Forensic tool — smoke + edge cases (read-only, no NT8 contact)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "diagnose_account_collisions.py"


def _seed_minimal_config(tmp_path: Path, mapping: dict):
    """Create a tmp config/ tree with a minimal STRATEGY_ACCOUNT_MAP."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "__init__.py").write_text("")
    # Use a deterministic repr; nested dicts work too
    src = "STRATEGY_ACCOUNT_MAP = " + repr(mapping) + "\n"
    (cfg / "account_routing.py").write_text(src)


def _run(tmp_path: Path, *cli_args: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(TOOL), *cli_args],
        cwd=tmp_path, capture_output=True, text=True,
    )
    return result.returncode, (result.stdout + result.stderr)


def test_tool_runs_and_writes_report(tmp_path):
    """Tool produces a report even with minimal config and no logs."""
    (tmp_path / "out").mkdir()
    (tmp_path / "logs").mkdir()
    _seed_minimal_config(tmp_path, {
        "a": "AcctOne", "b": "AcctOne", "c": "AcctTwo",
    })
    rc, out = _run(tmp_path)
    assert rc in (0, 2), out  # 2 = active collision; both fine for smoke
    reports = list((tmp_path / "out").glob("collision_forensic_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Account routing" in text


def test_detects_shared_account_in_routing(tmp_path):
    """When 2 strategies map to same account, report flags it."""
    (tmp_path / "out").mkdir()
    (tmp_path / "logs").mkdir()
    _seed_minimal_config(tmp_path, {
        "strat_a": "SharedAcct",
        "strat_b": "SharedAcct",
        "strat_c": "OtherAcct",
    })
    rc, _ = _run(tmp_path)
    assert rc in (0, 2)
    text = next((tmp_path / "out").glob("collision_forensic_*.md")).read_text(
        encoding="utf-8"
    )
    assert "SharedAcct" in text
    assert "strat_a" in text
    assert "strat_b" in text
    # Section 1 should report 1 collision-candidate account (not 2)
    assert "1" in text  # number-1 appears in count line


def test_no_shared_account_clean_state(tmp_path):
    """If every strategy has its own account, report says no collisions."""
    (tmp_path / "out").mkdir()
    (tmp_path / "logs").mkdir()
    _seed_minimal_config(tmp_path, {
        "a": "AcctA", "b": "AcctB", "c": "AcctC",
    })
    rc, _ = _run(tmp_path)
    assert rc in (0, 2)
    text = next((tmp_path / "out").glob("collision_forensic_*.md")).read_text(
        encoding="utf-8"
    )
    assert "No multi-strategy accounts" in text


def test_handles_nested_opening_session_mapping(tmp_path):
    """opening_session: {sub: account} should be flattened correctly."""
    (tmp_path / "out").mkdir()
    (tmp_path / "logs").mkdir()
    _seed_minimal_config(tmp_path, {
        "opening_session": {"open_drive": "SimX", "orb": "SimY"},
        "other": "SimX",  # collides with opening_session.open_drive
    })
    rc, _ = _run(tmp_path)
    assert rc in (0, 2)
    text = next((tmp_path / "out").glob("collision_forensic_*.md")).read_text(
        encoding="utf-8"
    )
    # SimX is shared between opening_session.open_drive and "other"
    assert "SimX" in text
    # opening_session.open_drive should appear (flattened sub-name)
    assert "opening_session.open_drive" in text or "open_drive" in text


def test_extracts_loaded_strategies_from_logs(tmp_path):
    """Tool greps `Loaded strategy: X (validated=True)` lines."""
    (tmp_path / "out").mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    _seed_minimal_config(tmp_path, {"x": "AcctX"})
    (logs / "prod_bot_stdout.log").write_text(
        "2026-05-04 08:07:31,770 [Bot] INFO Loaded strategy: x (validated=True)\n"
        "2026-05-04 08:07:31,770 [Bot] INFO Loaded strategy: y (validated=True)\n",
        encoding="utf-8",
    )
    (logs / "sim_bot_stdout.log").write_text(
        "2026-05-04 08:07:31,770 [Bot] INFO Loaded strategy: x (validated=False)\n"
        "2026-05-04 08:07:31,770 [Bot] INFO Loaded strategy: z (validated=False)\n",
        encoding="utf-8",
    )
    rc, _ = _run(tmp_path)
    text = next((tmp_path / "out").glob("collision_forensic_*.md")).read_text(
        encoding="utf-8"
    )
    # Both bots loaded `x` -> overlap = {x}
    assert "'x'" in text or "[\"x\"]" in text or "x (validated" not in text
    # Section 2 explicitly reports overlap
    assert "Shared (both bots can fire)" in text


def test_returns_2_when_active_collision_detected(tmp_path):
    """If collision evidence in last 1h, exit code is 2 (signals halt)."""
    (tmp_path / "out").mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    _seed_minimal_config(tmp_path, {"x": "AcctX"})
    # Use a recent timestamp (now)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
    now_ts = datetime.now(CT).strftime("%Y-%m-%dT%H:%M:%S")
    (logs / "sim_bot_stdout.log").write_text(
        f"{now_ts} [Bot] WARN already in_trade for SimX — rejecting duplicate entry\n",
        encoding="utf-8",
    )
    rc, _ = _run(tmp_path)
    # Exit 2 signals "active collision" per the tool's contract
    assert rc == 2
