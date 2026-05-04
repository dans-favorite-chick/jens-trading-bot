"""Sprint H v3 — bridge volumetric_bar message handling.

TickStreamer.cs emits 1,500-tick volumetric bars as typed JSON
messages over TCP. The bridge's async dispatcher routes
type=="volumetric_bar" to _handle_volumetric_bar(), which:

  - validates the schema (drops malformed without corrupting state)
  - atomically writes data/volumetric_latest.json (current bar)
  - appends to logs/volumetric_history.jsonl (replay history)

The strategy footprint_cvd_reversal reads both files; until
TickStreamer.cs is updated to emit, those files stay absent and
the strategy logs DATA_NOT_AVAILABLE and stays dormant.

Tests use asyncio.run() rather than pytest.mark.asyncio because
Phoenix doesn't ship pytest-asyncio (matches existing patterns in
tests/test_agent_base.py et al.).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.bridge_server import _handle_volumetric_bar, VOLUMETRIC_REQUIRED


def _valid_volumetric_msg() -> dict:
    return {
        "type": "volumetric_bar",
        "ts": "2026-05-04T12:34:56-05:00",
        "instrument": "MNQM6",
        "bar_size_ticks": 1500,
        "delta": -47,
        "total_volume": 1832,
        "buy_volume": 892,
        "sell_volume": 940,
        "poc": 27845.25,
        "open": 27845.50,
        "high": 27851.0,
        "low": 27842.75,
        "close": 27847.25,
        "imbalances": [],
        "stacked_buy": False,
        "stacked_sell": True,
        "max_imbalance_ratio": 1.5,
        "cvd_session": -1247,
    }


def test_volumetric_required_set_includes_critical_fields():
    """Schema sanity: the required set must include every field the
    strategy reads, otherwise a partial message could pass and the
    strategy crashes downstream."""
    must_include = {
        "ts", "open", "high", "low", "close",
        "delta", "total_volume", "stacked_buy", "stacked_sell",
        "cvd_session",
    }
    assert must_include.issubset(VOLUMETRIC_REQUIRED), (
        f"VOLUMETRIC_REQUIRED missing strategy-critical fields: "
        f"{must_include - VOLUMETRIC_REQUIRED}"
    )


def test_volumetric_bar_writes_latest_and_history(tmp_path: Path):
    """Happy path: a valid bar lands in both files."""
    msg = _valid_volumetric_msg()
    asyncio.run(_handle_volumetric_bar(msg, root=tmp_path))

    latest_path = tmp_path / "data" / "volumetric_latest.json"
    history_path = tmp_path / "logs" / "volumetric_history.jsonl"
    assert latest_path.exists()
    assert history_path.exists()

    latest = json.loads(latest_path.read_text())
    assert latest["cvd_session"] == -1247
    assert latest["bar_size_ticks"] == 1500

    history_lines = history_path.read_text().strip().splitlines()
    assert len(history_lines) == 1
    assert json.loads(history_lines[0])["cvd_session"] == -1247


def test_volumetric_bar_drops_malformed(tmp_path: Path):
    """Malformed messages are dropped without ever creating the file —
    the strategy treats absent latest.json as DATA_NOT_AVAILABLE."""
    bad = {"type": "volumetric_bar", "ts": "x"}  # missing required keys
    asyncio.run(_handle_volumetric_bar(bad, root=tmp_path))

    latest_path = tmp_path / "data" / "volumetric_latest.json"
    assert not latest_path.exists(), (
        "Malformed bar should NOT create the latest file — strategy "
        "treats missing file as DATA_NOT_AVAILABLE"
    )


def test_volumetric_bar_atomic_write_protects_previous_good(tmp_path: Path):
    """A second malformed message must NOT corrupt the previous good
    latest file. .tmp → rename pattern guarantees atomicity."""
    asyncio.run(_handle_volumetric_bar(_valid_volumetric_msg(), root=tmp_path))
    latest_path = tmp_path / "data" / "volumetric_latest.json"
    initial = json.loads(latest_path.read_text())
    assert initial["cvd_session"] == -1247

    # Now drop a malformed bar — latest should be unchanged.
    asyncio.run(_handle_volumetric_bar(
        {"type": "volumetric_bar", "ts": "x"}, root=tmp_path,
    ))
    after_bad = json.loads(latest_path.read_text())
    assert after_bad["cvd_session"] == -1247  # unchanged


def test_volumetric_history_appends_each_bar(tmp_path: Path):
    """Multiple valid bars accumulate in history (strategy reads the
    last N for compression baseline + divergence lookback)."""
    for cvd in (-100, -150, -200):
        msg = _valid_volumetric_msg()
        msg["cvd_session"] = cvd
        asyncio.run(_handle_volumetric_bar(msg, root=tmp_path))

    history_lines = (tmp_path / "logs" / "volumetric_history.jsonl") \
        .read_text().strip().splitlines()
    assert len(history_lines) == 3
    cvds = [json.loads(line)["cvd_session"] for line in history_lines]
    assert cvds == [-100, -150, -200]


def test_volumetric_default_root_uses_module_constant(monkeypatch, tmp_path: Path):
    """When root=None, _handle_volumetric_bar uses the module-level
    _VOLUMETRIC_ROOT — verified by monkeypatching the module attr."""
    import bridge.bridge_server as bs
    monkeypatch.setattr(bs, "_VOLUMETRIC_ROOT", tmp_path)
    asyncio.run(_handle_volumetric_bar(_valid_volumetric_msg()))
    assert (tmp_path / "data" / "volumetric_latest.json").exists()
