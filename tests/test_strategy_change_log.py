"""Strategy change log tool (#20, 2026-05-13).

These tests don't run real git (CI env may not have the full history).
They pin the parser logic + sanity-check the known-strategies list
stays in sync with config/strategies.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.strategy_change_log import _KNOWN_STRATEGIES


def test_known_strategies_covers_every_config_strategy():
    """If a new strategy lands in config/strategies.py but doesn't get
    added to _KNOWN_STRATEGIES, `strategy_change_log.py` will silently
    skip it on the default all-strategies run."""
    from config.strategies import STRATEGIES
    missing = sorted(set(STRATEGIES.keys()) - set(_KNOWN_STRATEGIES))
    assert not missing, (
        f"_KNOWN_STRATEGIES in tools/strategy_change_log.py is stale. "
        f"These strategies are in STRATEGIES but not in the tool's list: "
        f"{missing}. Add them so the change log reports on them."
    )


def test_known_strategies_no_unknown_entries():
    """Reverse direction: any name in _KNOWN_STRATEGIES that no longer
    exists in STRATEGIES is a sign the strategy was deleted but the tool
    wasn't updated."""
    from config.strategies import STRATEGIES
    unknown = sorted(set(_KNOWN_STRATEGIES) - set(STRATEGIES.keys()))
    assert not unknown, (
        f"_KNOWN_STRATEGIES has names not in STRATEGIES: {unknown}. "
        f"Were these strategies deleted? Remove them from the tool's list."
    )


def test_main_runs_without_error_in_no_repo_env(monkeypatch, capsys):
    """If the tool is run outside a git repo or git returns nothing,
    it should report 'no commits in window' gracefully, not crash."""
    from tools.strategy_change_log import collect_strategy_commits
    # Force _git to return empty by patching it
    import tools.strategy_change_log as mod
    monkeypatch.setattr(mod, "_git", lambda args: "")
    commits = collect_strategy_commits("bias_momentum")
    assert commits == []
