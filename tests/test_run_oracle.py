"""Tests for tools/run_oracle.py.

Task 6 of the Phoenix Strategy Oracle build.

These tests NEVER call the real ``strategy_oracle.run()``. Each test
monkeypatches the orchestrator entry point with a stub that captures
positional + keyword args and returns a scripted result dict.

The CLI surface under test:
    python -m tools.run_oracle research [--save-baseline | --no-save-baseline]
    python -m tools.run_oracle weekly
    python -m tools.run_oracle daily
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tools import run_oracle


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

class _StubRun:
    """Capture-and-return stub for strategy_oracle.run.

    Records each invocation's args/kwargs for later assertion. Returns
    whichever result dict the test set in ``result``.
    """

    def __init__(self, result: dict[str, Any] | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self.result: dict[str, Any] = result or {
            "status": "complete",
            "mode": "research",
            "facts_path": "logs/oracle/research/x_facts.json",
            "debrief_path": "logs/oracle/research/x_debrief.md",
            "audit_path": "logs/oracle/research/x_audit.jsonl",
            "pending_changes_path": "logs/oracle/pending_changes.json",
            "n_findings": 0,
            "n_proposals_staged": 0,
            "n_findings_rejected_by_verifier": 0,
            "regime": {"stable": True},
            "verifier_result": None,
        }

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def stub_run(monkeypatch):
    """Install a stub on the strategy_oracle module the CLI imported."""
    stub = _StubRun()
    monkeypatch.setattr(run_oracle.strategy_oracle, "run", stub)
    return stub


# ---------------------------------------------------------------------------
# 1. Mode dispatch -- positional argument passes through
# ---------------------------------------------------------------------------

def test_main_research_calls_run_with_research(stub_run, capsys):
    rc = run_oracle.main(["research"])
    assert rc == 0
    assert len(stub_run.calls) == 1
    args, kwargs = stub_run.calls[0]
    # Mode reaches the orchestrator (positional or kwarg -- accept both).
    if args:
        assert args[0] == "research"
    else:
        assert kwargs.get("mode") == "research"
    assert kwargs.get("save_baseline") is True


def test_main_weekly_calls_run_with_weekly(stub_run):
    rc = run_oracle.main(["weekly"])
    assert rc == 0
    args, kwargs = stub_run.calls[0]
    if args:
        assert args[0] == "weekly"
    else:
        assert kwargs.get("mode") == "weekly"
    assert kwargs.get("save_baseline") is True


def test_main_daily_calls_run_with_daily(stub_run):
    rc = run_oracle.main(["daily"])
    assert rc == 0
    args, kwargs = stub_run.calls[0]
    if args:
        assert args[0] == "daily"
    else:
        assert kwargs.get("mode") == "daily"
    assert kwargs.get("save_baseline") is True


# ---------------------------------------------------------------------------
# 2. --save-baseline / --no-save-baseline flag wiring
# ---------------------------------------------------------------------------

def test_main_research_no_save_baseline_passes_false(stub_run):
    rc = run_oracle.main(["research", "--no-save-baseline"])
    assert rc == 0
    _args, kwargs = stub_run.calls[0]
    assert kwargs.get("save_baseline") is False


def test_main_research_save_baseline_passes_true(stub_run):
    rc = run_oracle.main(["research", "--save-baseline"])
    assert rc == 0
    _args, kwargs = stub_run.calls[0]
    assert kwargs.get("save_baseline") is True


# ---------------------------------------------------------------------------
# 3. argparse rejects bogus / missing input
# ---------------------------------------------------------------------------

def test_main_bogus_mode_exits_nonzero(stub_run):
    # argparse exits via SystemExit when a choice is invalid.
    with pytest.raises(SystemExit) as exc:
        run_oracle.main(["bogus"])
    # Exit code should be non-zero (argparse uses 2).
    assert exc.value.code != 0
    # Stub must NOT have been called.
    assert stub_run.calls == []


def test_main_missing_positional_raises_systemexit(stub_run):
    with pytest.raises(SystemExit):
        run_oracle.main([])
    assert stub_run.calls == []


# ---------------------------------------------------------------------------
# 4. Halt statuses map to exit code 1
# ---------------------------------------------------------------------------

def test_main_returns_1_on_halted_regime_unstable(monkeypatch):
    stub = _StubRun(result={"status": "halted_regime_unstable", "mode": "research"})
    monkeypatch.setattr(run_oracle.strategy_oracle, "run", stub)
    rc = run_oracle.main(["research"])
    assert rc == 1


def test_main_returns_1_on_halted_no_api_key(monkeypatch):
    stub = _StubRun(result={"status": "halted_no_api_key", "mode": "weekly"})
    monkeypatch.setattr(run_oracle.strategy_oracle, "run", stub)
    rc = run_oracle.main(["weekly"])
    assert rc == 1


def test_main_returns_1_on_halted_preflight_failure(monkeypatch):
    stub = _StubRun(result={
        "status": "halted_preflight_failure",
        "mode": "daily",
        "reason": "warehouse not found",
    })
    monkeypatch.setattr(run_oracle.strategy_oracle, "run", stub)
    rc = run_oracle.main(["daily"])
    assert rc == 1


# ---------------------------------------------------------------------------
# 5. Output shape -- JSON-parseable, ASCII-only, single line
# ---------------------------------------------------------------------------

def test_main_stdout_is_json_parseable(stub_run, capsys):
    rc = run_oracle.main(["research"])
    assert rc == 0
    captured = capsys.readouterr()
    # The orchestrator result dict must round-trip through json.loads.
    parsed = json.loads(captured.out)
    assert parsed["status"] == "complete"
    assert parsed["mode"] == "research"


def test_main_stdout_is_ascii_only(stub_run, capsys):
    rc = run_oracle.main(["research"])
    assert rc == 0
    captured = capsys.readouterr()
    # cp1252 console safety: no non-ASCII bytes anywhere in stdout.
    encoded = captured.out.encode("ascii")  # raises UnicodeEncodeError if any
    assert len(encoded) > 0


def test_main_stdout_includes_non_ascii_input_as_escaped(monkeypatch, capsys):
    """If a result somehow contains non-ASCII chars (e.g. an em-dash),
    the JSON output must escape it rather than emit raw UTF-8 bytes."""
    stub = _StubRun(result={
        "status": "complete",
        "mode": "research",
        "reason": "delta vs prior — baseline",  # em-dash
    })
    monkeypatch.setattr(run_oracle.strategy_oracle, "run", stub)
    rc = run_oracle.main(["research"])
    assert rc == 0
    captured = capsys.readouterr()
    # ascii-encoding must succeed: json.dumps default escapes non-ASCII.
    captured.out.encode("ascii")
    parsed = json.loads(captured.out)
    assert "—" in parsed["reason"]


def test_main_stdout_is_single_line(stub_run, capsys):
    rc = run_oracle.main(["research"])
    assert rc == 0
    captured = capsys.readouterr()
    # Strip trailing newline from print, then assert no embedded newlines.
    body = captured.out.rstrip("\n")
    assert "\n" not in body


# ---------------------------------------------------------------------------
# 6. Help output
# ---------------------------------------------------------------------------

def test_main_help_lists_three_modes(capsys):
    with pytest.raises(SystemExit) as exc:
        run_oracle.main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    help_text = captured.out
    assert "research" in help_text
    assert "weekly" in help_text
    assert "daily" in help_text


# ---------------------------------------------------------------------------
# 7. Module importability
# ---------------------------------------------------------------------------

def test_module_is_importable():
    from tools import run_oracle as ro
    assert hasattr(ro, "main")
    assert callable(ro.main)
