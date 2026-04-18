"""
Tests for NT8 path validation at bot startup (base_bot._validate_nt8_paths).

A bot pointed at a nonexistent NT8 data folder is worse than one that
refuses to start — OIF writes silently drop, fill-ACK polls read nothing,
and the bot keeps "running" with no way to execute trades. These tests
lock in fail-fast behaviour.
"""

import logging
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bots import base_bot


# ─── Helpers ─────────────────────────────────────────────────────────

def _set_paths(monkeypatch, root, incoming, outgoing):
    monkeypatch.setattr(base_bot, "NT8_DATA_ROOT", str(root))
    monkeypatch.setattr(base_bot, "OIF_INCOMING", str(incoming))
    monkeypatch.setattr(base_bot, "OIF_OUTGOING", str(outgoing))


# ─── Happy path ──────────────────────────────────────────────────────

def test_validation_passes_when_all_paths_exist(tmp_path, monkeypatch):
    """All three NT8 paths exist on disk → returns quietly, no exit."""
    root = tmp_path / "NinjaTrader 8"
    incoming = root / "incoming"
    outgoing = root / "outgoing"
    for d in (root, incoming, outgoing):
        d.mkdir(parents=True, exist_ok=True)

    _set_paths(monkeypatch, root, incoming, outgoing)

    # Must not raise or exit
    base_bot._validate_nt8_paths()


# ─── Fail-fast behaviour ─────────────────────────────────────────────

def test_validation_exits_when_nt8_data_root_missing(tmp_path, monkeypatch, caplog):
    """NT8_DATA_ROOT missing → CRITICAL log mentioning the constant + sys.exit(1)."""
    missing = tmp_path / "does-not-exist"
    _set_paths(monkeypatch, missing, missing / "incoming", missing / "outgoing")

    with caplog.at_level(logging.CRITICAL, logger="Bot"):
        with pytest.raises(SystemExit) as exc:
            base_bot._validate_nt8_paths()

    assert exc.value.code == 1
    assert "NT8_DATA_ROOT" in caplog.text
    assert str(missing) in caplog.text


def test_validation_exits_when_only_incoming_missing(tmp_path, monkeypatch, caplog):
    """OIF_INCOMING missing even though root and outgoing exist → still fails."""
    root = tmp_path / "NinjaTrader 8"
    outgoing = root / "outgoing"
    for d in (root, outgoing):
        d.mkdir(parents=True, exist_ok=True)

    # incoming deliberately not created
    _set_paths(monkeypatch, root, root / "incoming", outgoing)

    with caplog.at_level(logging.CRITICAL, logger="Bot"):
        with pytest.raises(SystemExit) as exc:
            base_bot._validate_nt8_paths()

    assert exc.value.code == 1
    assert "OIF_INCOMING" in caplog.text


def test_validation_exits_when_only_outgoing_missing(tmp_path, monkeypatch, caplog):
    """OIF_OUTGOING missing even though root and incoming exist → still fails."""
    root = tmp_path / "NinjaTrader 8"
    incoming = root / "incoming"
    for d in (root, incoming):
        d.mkdir(parents=True, exist_ok=True)

    _set_paths(monkeypatch, root, incoming, root / "outgoing")

    with caplog.at_level(logging.CRITICAL, logger="Bot"):
        with pytest.raises(SystemExit) as exc:
            base_bot._validate_nt8_paths()

    assert exc.value.code == 1
    assert "OIF_OUTGOING" in caplog.text


# ─── Robustness: Telegram failures must not mask the real exit ───────

def test_telegram_failure_does_not_mask_sysexit(tmp_path, monkeypatch):
    """If the Telegram alert raises, validation still sys.exit(1)s."""
    missing = tmp_path / "nope"
    _set_paths(monkeypatch, missing, missing, missing)

    # base_bot uses `tg.send_sync` where tg = core.telegram_notifier
    with patch("core.telegram_notifier.send_sync", side_effect=RuntimeError("boom")):
        with pytest.raises(SystemExit) as exc:
            base_bot._validate_nt8_paths()
        assert exc.value.code == 1


def test_multiple_missing_paths_all_logged(tmp_path, monkeypatch, caplog):
    """When everything is missing, every missing path surfaces in the log."""
    missing = tmp_path / "nothing"
    _set_paths(monkeypatch, missing, missing / "incoming", missing / "outgoing")

    with caplog.at_level(logging.CRITICAL, logger="Bot"):
        with pytest.raises(SystemExit):
            base_bot._validate_nt8_paths()

    # Each constant name should appear in the CRITICAL log output
    for name in ("NT8_DATA_ROOT", "OIF_INCOMING", "OIF_OUTGOING"):
        assert name in caplog.text, f"{name} missing from critical log output"
