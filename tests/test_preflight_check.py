"""Unit tests for tools/preflight_check.py (2026-05-25).

Two tests per check:
  - test_<check>_passes_on_current_repo: confirms the check is green
    against the actual codebase state at session-end.
  - test_<check>_fails_when_corrupted: monkeypatches the relevant
    config/file/module to break the invariant and asserts the check
    returns a FAIL result with a non-empty hint.

We mock by patching attributes on the imported module (preferred — no
filesystem mutation) or by writing temp files that the check reads via
ROOT / "relative" / "path".

The driver `run_all` is also tested at a coarse level: the whole repo
should preflight to exit 0 today, and corrupting any single check
should bump it to exit 2.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import the module under test.
preflight = importlib.import_module("tools.preflight_check")


# ─── Helpers ─────────────────────────────────────────────────────────
def _all_ok(results) -> bool:
    return all(r.ok for r in results)


def _find_fail(results, name_prefix: str):
    """Return the first FAIL whose name starts with name_prefix, or
    None if no such FAIL exists."""
    for r in results:
        if not r.ok and r.name.startswith(name_prefix):
            return r
    return None


# ─── Check 1: live canary gate ───────────────────────────────────────
def test_check_live_canary_gate_passes_on_current_repo():
    results = preflight.check_live_canary_gate()
    assert _all_ok(results), [r.render() for r in results]


def test_check_live_canary_gate_fails_when_allowlist_corrupted(monkeypatch):
    """Mutate LIVE_STRATEGY_ALLOWLIST and confirm the check FAILs."""
    import config.settings as settings
    monkeypatch.setattr(
        settings, "LIVE_STRATEGY_ALLOWLIST", ("bias_momentum", "rogue"),
        raising=True,
    )
    results = preflight.check_live_canary_gate()
    fail = _find_fail(results, "live_canary_gate.allowlist_contents")
    assert fail is not None, "Expected a FAIL on allowlist_contents"
    assert fail.hint  # actionable hint present


def test_check_live_canary_gate_fails_when_live_trading_true(monkeypatch):
    import config.settings as settings
    monkeypatch.setattr(settings, "LIVE_TRADING", True, raising=True)
    results = preflight.check_live_canary_gate()
    fail = _find_fail(results, "live_canary_gate.LIVE_TRADING_is_false")
    assert fail is not None
    assert "LIVE_TRADING" in fail.hint


# ─── Check 2: production freeze ──────────────────────────────────────
def test_check_production_freeze_passes_on_current_repo():
    results = preflight.check_production_freeze()
    assert _all_ok(results), [r.render() for r in results]


def test_check_production_freeze_fails_when_freeze_lifted(monkeypatch):
    import config.strategies as strategies
    monkeypatch.setattr(strategies, "FREEZE_ACTIVE", False, raising=True)
    results = preflight.check_production_freeze()
    fail = _find_fail(results, "production_freeze.FREEZE_ACTIVE_is_true")
    assert fail is not None
    assert "FREEZE_ACTIVE" in fail.hint


# ─── Check 3: walk_forward_gate ──────────────────────────────────────
def test_check_walk_forward_gates_passes_on_current_repo():
    results = preflight.check_walk_forward_gates()
    assert _all_ok(results), [r.render() for r in results]


def test_check_walk_forward_gates_fails_when_bias_gate_wrong(monkeypatch):
    """If bias_momentum's gate flips to 'informational' (the lax setting
    for sim-only strategies) the live canary loses its statistical
    backstop. That MUST FAIL preflight."""
    import config.strategies as strategies
    new_strats = {
        name: dict(cfg) for name, cfg in strategies.STRATEGIES.items()
    }
    new_strats["bias_momentum"]["walk_forward_gate"] = "informational"
    monkeypatch.setattr(strategies, "STRATEGIES", new_strats, raising=True)
    results = preflight.check_walk_forward_gates()
    fail = _find_fail(results, "walk_forward_gate.bias_momentum")
    assert fail is not None
    assert "hard_block" in fail.hint


def test_check_walk_forward_gates_fails_when_dom_pullback_missing(monkeypatch):
    import config.strategies as strategies
    new_strats = {
        name: dict(cfg) for name, cfg in strategies.STRATEGIES.items()
        if name != "dom_pullback"
    }
    monkeypatch.setattr(strategies, "STRATEGIES", new_strats, raising=True)
    results = preflight.check_walk_forward_gates()
    fail = _find_fail(results, "walk_forward_gate.dom_pullback")
    assert fail is not None
    assert "missing" in fail.hint.lower()


# ─── Check 4: dom_pullback re-add ────────────────────────────────────
def test_check_dom_pullback_readd_passes_on_current_repo():
    results = preflight.check_dom_pullback_readd()
    assert _all_ok(results), [r.render() for r in results]


def test_check_dom_pullback_readd_fails_when_entry_missing(monkeypatch):
    import config.strategies as strategies
    new_strats = {
        name: dict(cfg) for name, cfg in strategies.STRATEGIES.items()
        if name != "dom_pullback"
    }
    monkeypatch.setattr(strategies, "STRATEGIES", new_strats, raising=True)
    results = preflight.check_dom_pullback_readd()
    fail = _find_fail(results, "dom_pullback.entry_present")
    assert fail is not None
    assert "dom_pullback" in fail.hint


def test_check_dom_pullback_readd_fails_when_change_log_missing(monkeypatch):
    """Patch _KNOWN_STRATEGIES to omit dom_pullback."""
    import tools.strategy_change_log as cl
    pruned = tuple(s for s in cl._KNOWN_STRATEGIES if s != "dom_pullback")
    monkeypatch.setattr(cl, "_KNOWN_STRATEGIES", pruned, raising=True)
    results = preflight.check_dom_pullback_readd()
    fail = _find_fail(results, "dom_pullback.in_change_log")
    assert fail is not None
    assert "_KNOWN_STRATEGIES" in fail.hint or "change_log" in fail.hint


# ─── Check 5: B2 opening_session fix ─────────────────────────────────
def test_check_b2_opening_session_passes_on_current_repo():
    results = preflight.check_b2_opening_session()
    assert _all_ok(results), [r.render() for r in results]


def test_check_b2_opening_session_fails_when_marker_missing(monkeypatch, tmp_path):
    """Point preflight.ROOT at a tmp_path with a stripped-down
    opening_session.py that omits both B2 markers."""
    fake_root = tmp_path / "phoenix_bot"
    (fake_root / "strategies").mkdir(parents=True)
    (fake_root / "strategies" / "opening_session.py").write_text(
        "# corrupted — B2 markers removed\n", encoding="utf-8",
    )
    # The check imports the module by name AND reads the source via
    # preflight.ROOT. Module import still goes to the real module — the
    # source read is what fails the marker checks, so we only need to
    # redirect ROOT.
    monkeypatch.setattr(preflight, "ROOT", fake_root, raising=True)
    results = preflight.check_b2_opening_session()
    fail_marker = _find_fail(results, "b2.target_method_source")
    fail_r1 = _find_fail(results, "b2.R1_formula_present")
    assert fail_marker is not None, [r.render() for r in results]
    assert fail_r1 is not None, [r.render() for r in results]


# ─── Check 6: B2-3 NameError fixes ───────────────────────────────────
def test_check_b2_3_nameerror_fixes_passes_on_current_repo():
    results = preflight.check_b2_3_nameerror_fixes()
    assert _all_ok(results), [r.render() for r in results]


def test_check_b2_3_nameerror_fixes_fails_when_old_token_present(
    monkeypatch, tmp_path,
):
    """Synthesize a corrupted _ws_dispatcher.py that still has the
    pre-fix `(market, pos.direction` token, and a _strategy_dispatch.py
    missing the fixed call. Both should FAIL."""
    fake_root = tmp_path / "phoenix_bot"
    (fake_root / "bots").mkdir(parents=True)
    # Strategy dispatch: missing the fixed token entirely.
    (fake_root / "bots" / "_strategy_dispatch.py").write_text(
        "# corrupted — no cr_assess call here\n", encoding="utf-8",
    )
    # WS dispatcher: contains the pre-fix tokens.
    (fake_root / "bots" / "_ws_dispatcher.py").write_text(
        "result = cr_assess(market, pos.direction, traj)\n"
        "atr = market.get(_atr_key, 0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight, "ROOT", fake_root, raising=True)
    results = preflight.check_b2_3_nameerror_fixes()
    assert _find_fail(results, "b2_3.strategy_dispatch.fixed_call") is not None
    assert _find_fail(
        results, "b2_3.ws_dispatcher.old_market_token_absent",
    ) is not None
    assert _find_fail(
        results, "b2_3.ws_dispatcher.old_atr_token_absent",
    ) is not None


def test_check_b2_3_word_boundary_does_not_false_match_underscore_market(
    monkeypatch, tmp_path,
):
    """Regression: `_market.get(_atr_key` must NOT trigger the
    'old_atr_token_absent' FAIL. The check uses a word-boundary regex
    so `_market` isn't matched as `market`."""
    fake_root = tmp_path / "phoenix_bot"
    (fake_root / "bots").mkdir(parents=True)
    (fake_root / "bots" / "_strategy_dispatch.py").write_text(
        "x = cr_assess(market, None, traj)\n", encoding="utf-8",
    )
    (fake_root / "bots" / "_ws_dispatcher.py").write_text(
        "_market = bot.aggregator.snapshot()\n"
        "result = call(bot.aggregator.snapshot(), pos.direction, traj)\n"
        "atr = _market.get(_atr_key, 0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight, "ROOT", fake_root, raising=True)
    results = preflight.check_b2_3_nameerror_fixes()
    assert _all_ok(results), [r.render() for r in results]


# ─── Check 7: AI agents disabled ─────────────────────────────────────
def test_check_ai_agents_disabled_passes_on_current_repo():
    results = preflight.check_ai_agents_disabled()
    assert _all_ok(results), [r.render() for r in results]


def test_check_ai_agents_disabled_fails_when_council_enabled(monkeypatch):
    import config.settings as settings
    monkeypatch.setattr(
        settings, "AGENT_COUNCIL_ENABLED", True, raising=True,
    )
    results = preflight.check_ai_agents_disabled()
    fail = _find_fail(results, "ai_agents.AGENT_COUNCIL_ENABLED")
    assert fail is not None
    assert "False" in fail.hint


# ─── Driver-level smoke tests ────────────────────────────────────────
def test_run_all_passes_on_current_repo():
    """Whole tool returns exit 0 right now."""
    results, code = preflight.run_all()
    assert code == 0, [r.render() for r in results if not r.ok]


def test_run_all_fails_when_any_invariant_corrupted(monkeypatch):
    """Corrupt one invariant; driver must exit 2."""
    import config.strategies as strategies
    monkeypatch.setattr(strategies, "FREEZE_ACTIVE", False, raising=True)
    results, code = preflight.run_all()
    assert code == 2
    # And the FAIL should be specifically the freeze.
    assert _find_fail(results, "production_freeze.FREEZE_ACTIVE_is_true") \
        is not None


def test_check_result_render_format():
    """The render() output is what the operator sees — guard the format."""
    ok = preflight.CheckResult("foo", True)
    fail = preflight.CheckResult("foo", False, "do the thing")
    assert ok.render() == "[OK] foo"
    assert fail.render() == "[FAIL] foo: do the thing"
