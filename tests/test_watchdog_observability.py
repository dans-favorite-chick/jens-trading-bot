"""Regression test for FINDING-2026-06-04-WATCHDOG-OPACITY consumer side.

The dashboard side (Phase 1 of this sprint) surfaces interpreter,
parent_pid, external, and shim_warn fields under
`/api/bot/status:processes.<bot>`. This test pins the WATCHDOG'S
consumption of those fields:

  - watchdog parses the `processes` dict from /api/bot/status
  - watchdog logs a LOUD warning on shim_warn=True transition
  - watchdog surfaces interpreter info in its periodic status line

Without this, a misconfigured dashboard (one started under the
WindowsApps shim) would silently propagate the shim to every
watchdog-triggered restart — which was the latent risk called
out in `out/next_sprint_outline_watchdog.md`.
"""
from __future__ import annotations

import logging

import pytest


SAFE = (
    r"C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe"
)
SHIM = (
    r"C:\Users\Trading PC\AppData\Local\Microsoft\WindowsApps\python.exe"
)


@pytest.fixture
def wd(monkeypatch):
    """Construct a Watchdog instance with HTTP I/O monkeypatched."""
    from tools import watchdog as w
    inst = w.Watchdog(bot_names=["prod", "sim"], auto_restart=False)
    return inst


def test_t1_check_dashboard_parses_processes_field(wd, monkeypatch):
    """check_dashboard captures the `processes` dict from
    /api/bot/status into the watchdog's own state."""
    from tools import watchdog as w

    fake_response = {
        "prod": "running",
        "sim": "running",
        "processes": {
            "prod": {
                "status": "running",
                "interpreter": SAFE,
                "parent_pid": 12345,
                "external": True,
                "shim_warn": False,
            },
            "sim": {
                "status": "running",
                "interpreter": SAFE,
                "parent_pid": 12345,
                "external": True,
                "shim_warn": False,
            },
        },
    }

    monkeypatch.setattr(w, "_fetch_json", lambda *a, **kw: fake_response)

    wd.check_dashboard()

    assert wd.dashboard_alive is True
    # New attribute: surface the processes dict from /api/bot/status
    assert hasattr(wd, "_dashboard_processes"), (
        "Watchdog must expose _dashboard_processes after check_dashboard"
    )
    assert wd._dashboard_processes["prod"]["interpreter"] == SAFE
    assert wd._dashboard_processes["prod"]["shim_warn"] is False


def test_t2_shim_warn_transition_logs_loud_warning(wd, monkeypatch, caplog):
    """When shim_warn flips from False to True for any bot, watchdog
    emits a single LOUD ERROR-level log. This is the breadcrumb that
    catches a dashboard-restart-under-shim regression."""
    from tools import watchdog as w

    # Start clean (no shim)
    safe_resp = {
        "prod": "running", "sim": "running",
        "processes": {
            "prod": {"status": "running", "interpreter": SAFE,
                      "parent_pid": 1, "external": True,
                      "shim_warn": False},
            "sim":  {"status": "running", "interpreter": SAFE,
                      "parent_pid": 1, "external": True,
                      "shim_warn": False},
        },
    }
    monkeypatch.setattr(w, "_fetch_json", lambda *a, **kw: safe_resp)
    wd.check_dashboard()

    # Transition: prod is now under the shim
    shim_resp = {
        "prod": "running", "sim": "running",
        "processes": {
            "prod": {"status": "running", "interpreter": SHIM,
                      "parent_pid": 9999, "external": True,
                      "shim_warn": True},
            "sim":  {"status": "running", "interpreter": SAFE,
                      "parent_pid": 1, "external": True,
                      "shim_warn": False},
        },
    }
    monkeypatch.setattr(w, "_fetch_json", lambda *a, **kw: shim_resp)

    with caplog.at_level(logging.WARNING, logger="Watchdog"):
        wd.check_dashboard()

    # At least one log record should mention WindowsApps shim for prod
    shim_logs = [
        r for r in caplog.records
        if "shim" in r.getMessage().lower() and "prod" in r.getMessage()
    ]
    assert shim_logs, (
        "expected a LOUD WARN on shim_warn transition; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    # Severity should be ERROR or WARNING (not INFO)
    assert any(
        r.levelno >= logging.WARNING for r in shim_logs
    ), "shim warning should be WARNING or higher severity"


def test_t3_dashboard_down_clears_processes(wd, monkeypatch):
    """When /api/bot/status is unreachable, _dashboard_processes must
    reset to None so stale data doesn't leak into print_status."""
    from tools import watchdog as w

    monkeypatch.setattr(w, "_fetch_json", lambda *a, **kw: None)
    wd.check_dashboard()

    assert wd.dashboard_alive is False
    assert getattr(wd, "_dashboard_processes", "missing") in (None, {}), (
        "dashboard down should clear stale processes view"
    )
