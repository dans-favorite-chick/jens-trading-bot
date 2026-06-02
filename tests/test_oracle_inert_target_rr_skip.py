"""BUG #4 option 2 — Oracle must skip ``target_rr`` proposals on
strategies whose ``PHASE_13_EXIT_ASSIGNMENTS`` entry is
``chandelier``, ``time_exit``, or ``managed_existing``.

The fix is split across two defenses:

  1. Schema filter — ``_build_strategy_schema_block`` drops
     ``target_rr`` from the per-strategy parameter list emitted into
     the LLM prompt. The LLM never sees it as tunable.
  2. Runtime guard — ``_tool_propose_change`` rejects the proposal
     with an explanatory error if the LLM proposes it anyway from
     memory of the config.

The tests pin both behaviors. Strategies whose policy is ``fixed_rr``
or similar — i.e., a hard target — must continue to expose
``target_rr`` as tunable.

Spec: ``logs/oracle/research/2026-06-02_bug4_inert_target_rr.md``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agents.strategy_oracle as so


# ─── helper lookup ───────────────────────────────────────────────────────

def test_inert_lookup_returns_chandelier_strategies():
    """The helper that drives both defenses must pick up at least the
    documented chandelier/time_exit strategies from the live exit
    policy mapping.
    """
    inert = so._strategies_with_inert_target_rr()
    # Names drawn from the canonical mapping in
    # core/exit_policies.py:PHASE_13_EXIT_ASSIGNMENTS as of 2026-06-02.
    expected_chandelier = {
        "g_inside_bar_breakout",
        "e_multi_day_breakout",
        "es_nq_confluence",
    }
    expected_time_exit = {"a_asian_continuation", "raschke_baseline"}
    expected_managed = {"opening_session.orb"}
    for s in expected_chandelier | expected_time_exit | expected_managed:
        assert s in inert, (
            f"{s} expected in inert-target_rr set; got {sorted(inert)}"
        )


def test_inert_lookup_excludes_fixed_rr_strategies():
    inert = so._strategies_with_inert_target_rr()
    # bias_momentum + vwap_band_pullback are fixed_rr — must still
    # expose target_rr.
    for s in ("bias_momentum", "vwap_band_pullback", "ib_breakout"):
        assert s not in inert, (
            f"{s} uses fixed_rr — must remain tunable; inert set was {sorted(inert)}"
        )


# ─── schema filter ───────────────────────────────────────────────────────

def _facts_with_strategies(*names: str) -> dict:
    return {"strategies": {n: {} for n in names}}


def test_schema_omits_target_rr_for_chandelier_strategy():
    block = so._build_strategy_schema_block(
        _facts_with_strategies("g_inside_bar_breakout")
    )
    # Find the line for the strategy and assert target_rr is absent
    # from its parameter list.
    line = next(
        (ln for ln in block.splitlines() if "`g_inside_bar_breakout`" in ln),
        None,
    )
    assert line is not None, (
        f"strategy row missing from schema block:\n{block}"
    )
    assert "target_rr" not in line, (
        f"target_rr must NOT appear on the chandelier-policy strategy's row: {line}"
    )


def test_schema_omits_target_rr_for_time_exit_strategy():
    block = so._build_strategy_schema_block(
        _facts_with_strategies("raschke_baseline")
    )
    line = next(
        (ln for ln in block.splitlines() if "`raschke_baseline`" in ln),
        None,
    )
    assert line is not None
    assert "target_rr" not in line


def test_schema_keeps_target_rr_for_fixed_rr_strategy():
    """bias_momentum uses fixed_rr — target_rr must stay exposed as a
    tunable knob.
    """
    block = so._build_strategy_schema_block(
        _facts_with_strategies("bias_momentum")
    )
    line = next(
        (ln for ln in block.splitlines() if "`bias_momentum`" in ln),
        None,
    )
    assert line is not None
    assert "target_rr" in line, (
        f"target_rr must REMAIN on fixed_rr-policy strategy's row: {line}"
    )


# ─── runtime guard in _tool_propose_change ───────────────────────────────

def _propose(strategy: str, parameter_name: str = "target_rr") -> dict:
    """Build a complete propose_change args dict (all required fields)."""
    return {
        "strategy": strategy,
        "direction": "BOTH",
        "parameter_name": parameter_name,
        "current_value": 3.0,
        "proposed_value": 2.5,
        "rationale": "test",
        "confidence": "HIGH",
        "sample_size": 100,
        "finding_id": "f-test",
    }


def _ctx(mode: str = "research"):
    return so._RunCtx(
        mode=mode, facts={"strategies": {}},
        audit_fh=None, run_date="2026-06-02",
        pending_proposals=[],
    )


def test_runtime_rejects_target_rr_on_chandelier_strategy():
    result = so._tool_propose_change(
        _propose("g_inside_bar_breakout"), _ctx(),
    )
    assert result["ok"] is False
    assert "inert" in result["error"].lower()
    assert "g_inside_bar_breakout" in result["error"]


def test_runtime_rejects_target_rr_on_time_exit_strategy():
    result = so._tool_propose_change(
        _propose("raschke_baseline"), _ctx(),
    )
    assert result["ok"] is False
    assert "inert" in result["error"].lower()


def test_runtime_allows_target_rr_on_fixed_rr_strategy(monkeypatch):
    """The runtime guard must NOT reject target_rr on fixed_rr policies.

    Monkey-patch the current-value lookup so the test doesn't depend on
    the live config blob.
    """
    import analytics.prepared_queries as pq
    monkeypatch.setattr(
        pq, "current_param_value",
        lambda strategy, param: 2.0,
    )
    monkeypatch.setattr(so, "_proposal_metrics_snapshot", lambda *a, **k: {})

    ctx = _ctx()
    result = so._tool_propose_change(_propose("bias_momentum"), ctx)
    assert result.get("ok") is True, (
        f"target_rr proposal on fixed_rr strategy was rejected: {result}"
    )
    assert ctx.pending_proposals, "proposal should be staged"


def test_runtime_allows_other_params_on_chandelier_strategy(monkeypatch):
    """Only target_rr is inert — other knobs on a chandelier strategy
    (e.g., stop_atr_mult) must still be tunable.
    """
    import analytics.prepared_queries as pq
    monkeypatch.setattr(
        pq, "current_param_value",
        lambda strategy, param: 1.5,
    )
    monkeypatch.setattr(so, "_proposal_metrics_snapshot", lambda *a, **k: {})

    ctx = _ctx()
    result = so._tool_propose_change(
        _propose("g_inside_bar_breakout", parameter_name="stop_atr_mult"),
        ctx,
    )
    assert result.get("ok") is True, (
        f"stop_atr_mult on chandelier strategy was wrongly rejected: {result}"
    )
