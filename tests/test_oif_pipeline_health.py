"""OIF pipeline health diagnostic.

Sprint D defensive observability. Verifies diagnose_oif_pipeline_health()
returns the right shape + correctly flags:
  - missing folders (NT8 install path drift)
  - stale files in incoming/ (NT8 ATI off, indicator dead, or guard
    quarantining everything)
  - clean state (folders exist, no stale files)
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def patched_folders(tmp_path, monkeypatch):
    """Redirect OIF_INCOMING / OIF_OUTGOING to tmp_path subdirs."""
    inc = tmp_path / "incoming"
    out = tmp_path / "outgoing"
    inc.mkdir()
    out.mkdir()
    # Patch via monkeypatch on the module itself
    import bridge.oif_writer as mod
    monkeypatch.setattr(mod, "OIF_INCOMING", str(inc))
    monkeypatch.setattr(mod, "OIF_OUTGOING", str(out))
    return inc, out


def test_health_clean_when_folders_empty(patched_folders):
    from bridge.oif_writer import diagnose_oif_pipeline_health
    result = diagnose_oif_pipeline_health()
    assert result["healthy"] is True
    assert result["reasons"] == []
    assert result["metrics"]["incoming_count"] == 0
    assert result["metrics"]["outgoing_count"] == 0
    assert result["metrics"]["incoming_oldest_age_s"] is None


def test_health_clean_with_fresh_outgoing_files(patched_folders):
    """outgoing/ commonly has many position files — those don't gate health."""
    inc, out = patched_folders
    (out / "MNQM6 Globex_Sim101_position.txt").write_text("FLAT;0;0")
    (out / "Live.txt").write_text("CONNECTED")
    from bridge.oif_writer import diagnose_oif_pipeline_health
    result = diagnose_oif_pipeline_health()
    assert result["healthy"] is True
    assert result["metrics"]["outgoing_count"] == 2


def test_health_unhealthy_when_incoming_has_stale_file(patched_folders):
    """Any file >5min old in incoming/ flags unhealthy."""
    inc, _ = patched_folders
    stale = inc / "oif_stale.txt"
    stale.write_text("CLOSEPOSITION;test;MNQM6;GTC;;;;;;;;;")
    # Backdate mtime to 10 minutes ago
    old = time.time() - 600
    os.utime(stale, (old, old))
    from bridge.oif_writer import diagnose_oif_pipeline_health
    result = diagnose_oif_pipeline_health()
    assert result["healthy"] is False
    assert any("stale" in r for r in result["reasons"])
    assert result["metrics"]["incoming_oldest_age_s"] >= 595


def test_health_threshold_is_configurable(patched_folders):
    """Custom threshold lets caller decide what 'stale' means."""
    inc, _ = patched_folders
    f = inc / "oif_x.txt"
    f.write_text("test")
    old = time.time() - 30  # 30 seconds old
    os.utime(f, (old, old))
    from bridge.oif_writer import diagnose_oif_pipeline_health
    # Default threshold (300s): 30s file is OK
    r = diagnose_oif_pipeline_health()
    assert r["healthy"] is True
    # Tight threshold (10s): now unhealthy
    r2 = diagnose_oif_pipeline_health(stale_incoming_threshold_s=10.0)
    assert r2["healthy"] is False


def test_health_unhealthy_when_incoming_missing(tmp_path, monkeypatch):
    """Folder absent (NT8 install path drift) → unhealthy."""
    out = tmp_path / "outgoing"
    out.mkdir()
    import bridge.oif_writer as mod
    monkeypatch.setattr(mod, "OIF_INCOMING",
                        str(tmp_path / "does_not_exist"))
    monkeypatch.setattr(mod, "OIF_OUTGOING", str(out))
    from bridge.oif_writer import diagnose_oif_pipeline_health
    result = diagnose_oif_pipeline_health()
    assert result["healthy"] is False
    assert any("missing" in r for r in result["reasons"])


def test_health_metrics_include_oldest_age(patched_folders):
    """Even when healthy, oldest_age_s is reported for trending."""
    inc, _ = patched_folders
    f = inc / "oif_recent.txt"
    f.write_text("x")
    old = time.time() - 60  # 1 minute old (not stale)
    os.utime(f, (old, old))
    from bridge.oif_writer import diagnose_oif_pipeline_health
    result = diagnose_oif_pipeline_health()
    assert result["healthy"] is True
    age = result["metrics"]["incoming_oldest_age_s"]
    assert age is not None
    assert 50 <= age <= 70


def test_health_returns_serializable_dict(patched_folders):
    """Result must be JSON-serializable for log lines + telegram."""
    import json
    from bridge.oif_writer import diagnose_oif_pipeline_health
    result = diagnose_oif_pipeline_health()
    # round-trip; fails if any non-serializable types leaked in
    json.dumps(result)


# ─── forensic tool smoke test ────────────────────────────────────────

def test_forensic_tool_runs_without_error():
    """tools/diagnose_stuck_exits.py runs to completion against the live
    repo state and produces a markdown report."""
    import subprocess
    import sys
    ROOT = Path(__file__).resolve().parent.parent
    # Run with cwd at repo root so it reads real state, not tmp_path
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "diagnose_stuck_exits.py")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    # Today's report should exist
    out_dir = ROOT / "out"
    reports = list(out_dir.glob("stuck_exits_forensic_*.md"))
    assert len(reports) >= 1
