"""Tests for agents/strategy_oracle.py.

Task 5 of the Phoenix Strategy Oracle build (spec sec 4, 6, 7, 12).

These tests NEVER make a real Anthropic API call. The orchestrator
accepts an optional `client` parameter so the suite can inject a
``_StubAnthropicClient`` that replays scripted responses.

Coverage groups:
    TestModeDispatch     -- mode -> config lookup, token budgets, can_propose
    TestTools            -- per-tool happy/sad paths via the dispatcher
    TestAudit            -- audit.jsonl is JSON-per-line, one line per dispatch
    TestCIInvariant      -- AST scan rejects trade-path imports
    TestRegimeHalt       -- weekly halts on regime instability, daily skips
    TestPipelineIntegration -- full run() with stub LLM emits all four files
    TestPreflight        -- pre-flight failures map to structured statuses
    TestNoForbiddenImports -- file-level scan against the live module
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# Defensive: tests must import the orchestrator without hitting the trade
# path at import time. Package __init__ pulls in council_gate/session_debriefer
# which are heavy; we test the module file directly.
from agents import strategy_oracle as so


# ---------------------------------------------------------------------------
# Stub Anthropic client
# ---------------------------------------------------------------------------

class _StubMessage:
    """Mimics the shape of anthropic.types.Message that the orchestrator reads.

    We expose ``content`` (a list of blocks), ``stop_reason``, and ``usage``
    so the loop body can inspect them. Each block is a SimpleNamespace-like
    object with ``type`` plus type-specific fields.
    """

    def __init__(self, content: list[dict], stop_reason: str = "end_turn",
                 input_tokens: int = 100, output_tokens: int = 50):
        self.content = [_StubBlock(b) for b in content]
        self.stop_reason = stop_reason
        self.usage = _StubUsage(input_tokens, output_tokens)


class _StubBlock:
    def __init__(self, d: dict):
        self.type = d["type"]
        if d["type"] == "text":
            self.text = d.get("text", "")
        elif d["type"] == "tool_use":
            self.id = d.get("id", "tool_use_stub")
            self.name = d["name"]
            self.input = d.get("input", {})
        elif d["type"] == "thinking":
            self.thinking = d.get("thinking", "")


class _StubUsage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _StubMessages:
    """Mirrors ``client.messages.create`` -- pops scripted responses."""

    def __init__(self, scripted: list[_StubMessage]):
        self.scripted = list(scripted)
        self.calls: list[dict] = []

    def create(self, **kwargs: Any) -> _StubMessage:
        self.calls.append(kwargs)
        if not self.scripted:
            # Default: emit an end_turn final text block so the loop terminates.
            return _StubMessage(
                [{"type": "text", "text": "Done."}],
                stop_reason="end_turn",
            )
        return self.scripted.pop(0)


class _StubAnthropicClient:
    """Drop-in replacement for ``anthropic.Anthropic``.

    Usage in tests::

        client = _StubAnthropicClient([
            _StubMessage([{'type': 'text', 'text': 'final narrative'}],
                         stop_reason='end_turn'),
        ])
        so.run('weekly', client=client, ...)
    """

    def __init__(self, scripted: list[_StubMessage] | None = None):
        self.messages = _StubMessages(scripted or [])


# ---------------------------------------------------------------------------
# Fixtures: synthetic facts panel, audit handle, tmp logs root, env
# ---------------------------------------------------------------------------

@pytest.fixture
def synth_facts() -> dict:
    """A minimal facts dict the orchestrator would produce in compute phase."""
    return {
        "run_mode": "weekly",
        "run_date": "2026-05-31",
        "window_start": "2026-05-24",
        "window_end": "2026-05-30",
        "regime": {"stable": True, "z_score": 0.42, "warning": None,
                   "mode_skipped": False, "baseline_n_months": 6,
                   "latest_month": "2026-05", "latest_sharpe_proxy": 0.12},
        "n_trials_effective": 5,
        "strategies": {
            "bias_momentum": {
                "metrics": {
                    "n_trades": 122, "psr": 0.93, "dsr": 0.76, "min_trl": 87,
                    "hlz_t_stat": 3.4, "bhy_p_adjusted": 0.018,
                    "profit_factor": 1.82, "sortino": 1.41, "calmar": 0.93,
                    "max_drawdown_dollars": -1840.5,
                    "oos_pf": 1.62, "is_pf": 2.21, "wfe_ratio": 0.73,
                    "win_rate": 0.55,
                },
                "gates": {
                    "n_floor": True, "n_medium": True, "n_high": False,
                    "psr_0_90": True, "dsr_0_90": True, "dsr_0_95": False,
                    "hlz_3_0": True, "min_trl_met": True, "wfa_pass": True,
                    "all_pass_for_proposal": False,
                    "failed_gates": ["dsr_0_95"],
                },
                "gate_thresholds": {
                    "dsr_high": 0.95, "dsr_luck_floor": 0.90, "psr": 0.90,
                    "hlz_t_stat": 3.0, "n_floor": 30, "n_medium": 100,
                    "n_high": 200, "wfe_ratio_min": 0.6,
                },
            },
            "orb_fade": {
                "metrics": {
                    "n_trades": 45, "psr": 0.71, "dsr": 0.42, "min_trl": 200,
                    "hlz_t_stat": 1.8, "bhy_p_adjusted": 0.15,
                    "profit_factor": 1.21, "sortino": 0.61, "calmar": 0.31,
                    "max_drawdown_dollars": -820.0,
                    "oos_pf": 0.95, "is_pf": 1.50, "wfe_ratio": 0.63,
                    "win_rate": 0.48,
                },
                "gates": {
                    "n_floor": True, "n_medium": False, "n_high": False,
                    "psr_0_90": False, "dsr_0_90": False, "dsr_0_95": False,
                    "hlz_3_0": False, "min_trl_met": False, "wfa_pass": True,
                    "all_pass_for_proposal": False,
                    "failed_gates": ["psr_0_90", "dsr_0_95", "hlz_3_0",
                                     "min_trl_met"],
                },
                "gate_thresholds": {
                    "dsr_high": 0.95, "dsr_luck_floor": 0.90, "psr": 0.90,
                    "hlz_t_stat": 3.0, "n_floor": 30, "n_medium": 100,
                    "n_high": 200, "wfe_ratio_min": 0.6,
                },
            },
        },
        "findings": [],
        "prior_findings_loaded": [],
        "delta_vs_prior": {"is_baseline": True},
    }


@pytest.fixture
def tmp_logs_root(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the orchestrator's logs/oracle/ root to a tmp path."""
    root = tmp_path / "logs" / "oracle"
    root.mkdir(parents=True)
    monkeypatch.setattr(so, "LOGS_ORACLE_ROOT", root)
    return root


@pytest.fixture
def audit_fh(tmp_path: Path):
    """Open a writable audit handle that the dispatcher can append to."""
    p = tmp_path / "audit.jsonl"
    fh = open(p, "w", encoding="utf-8")
    yield fh, p
    if not fh.closed:
        fh.close()


@pytest.fixture
def ctx(synth_facts, tmp_logs_root, audit_fh) -> "so._RunCtx":
    """Run context the tool dispatcher consumes."""
    fh, _path = audit_fh
    return so._RunCtx(
        mode="weekly",
        facts=synth_facts,
        audit_fh=fh,
        run_date="2026-05-31",
        pending_proposals=[],
    )


# ===========================================================================
# TestModeDispatch
# ===========================================================================

class TestModeDispatch:
    """Mode -> config lookup; token budgets; tool gating."""

    def test_research_budget(self):
        assert so.MODE_CONFIG["research"]["token_budget"] == 200_000

    def test_weekly_budget(self):
        assert so.MODE_CONFIG["weekly"]["token_budget"] == 80_000

    def test_daily_budget(self):
        assert so.MODE_CONFIG["daily"]["token_budget"] == 15_000

    def test_daily_skips_regime_gate(self):
        assert so.MODE_CONFIG["daily"]["skip_regime_gate"] is True

    def test_daily_cannot_propose(self):
        assert so.MODE_CONFIG["daily"]["can_propose"] is False

    def test_weekly_can_propose(self):
        assert so.MODE_CONFIG["weekly"]["can_propose"] is True

    def test_research_window_is_5_years(self):
        assert so.MODE_CONFIG["research"]["window_days"] == 1825


# ===========================================================================
# TestTools
# ===========================================================================

class TestTools:
    """Per-tool behavior via the dispatcher."""

    def test_think_passthrough(self, ctx):
        result = so._dispatch_tool("think", {"reasoning": "step 1"}, ctx)
        assert result["ok"] is True

    def test_fetch_strategy_stats_known(self, ctx):
        result = so._dispatch_tool(
            "fetch_strategy_stats",
            {"strategy": "bias_momentum"},
            ctx,
        )
        assert result["ok"] is True
        assert result["panel"]["metrics"]["n_trades"] == 122

    def test_fetch_strategy_stats_unknown(self, ctx):
        result = so._dispatch_tool(
            "fetch_strategy_stats",
            {"strategy": "no_such_strategy"},
            ctx,
        )
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_check_regime_mirrors_facts(self, ctx):
        result = so._dispatch_tool("check_regime", {}, ctx)
        assert result["ok"] is True
        assert result["regime"]["stable"] is True
        assert result["regime"]["z_score"] == pytest.approx(0.42)

    def test_write_finding_rejects_small_n(self, ctx):
        result = so._dispatch_tool(
            "write_finding",
            {
                "id": "test_tiny_n",
                "strategy": "bias_momentum",
                "verdict": "CONFIRMED",
                "confidence": "LOW",
                "sample_size": 10,
                "rationale": "n=10 trades observed",
            },
            ctx,
        )
        assert result["ok"] is False
        assert "30" in result["error"]
        assert ctx.facts["findings"] == []

    def test_write_finding_accepts_valid(self, ctx):
        result = so._dispatch_tool(
            "write_finding",
            {
                "id": "bias_momentum_dsr_2026-05-31",
                "strategy": "bias_momentum",
                "verdict": "CONFIRMED",
                "confidence": "MEDIUM",
                "sample_size": 122,
                "rationale": "DSR=0.76 with n=122",
            },
            ctx,
        )
        assert result["ok"] is True
        assert len(ctx.facts["findings"]) == 1
        assert ctx.facts["findings"][0]["id"] == "bias_momentum_dsr_2026-05-31"

    def test_propose_change_rejected_in_daily(self, synth_facts, audit_fh):
        fh, _ = audit_fh
        ctx_daily = so._RunCtx(
            mode="daily",
            facts=synth_facts,
            audit_fh=fh,
            run_date="2026-05-31",
            pending_proposals=[],
        )
        result = so._dispatch_tool(
            "propose_change",
            {
                "strategy": "bias_momentum", "direction": "BOTH",
                "parameter_name": "session_end_time",
                "current_value": "09:45", "proposed_value": "09:15",
                "rationale": "decay after 09:15",
                "confidence": "MEDIUM", "sample_size": 122,
                "finding_id": "bias_momentum_dsr_2026-05-31",
            },
            ctx_daily,
        )
        assert result["ok"] is False
        assert "daily mode" in result["error"].lower()
        assert ctx_daily.pending_proposals == []

    def test_propose_change_rejected_low_confidence(self, ctx):
        result = so._dispatch_tool(
            "propose_change",
            {
                "strategy": "bias_momentum", "direction": "BOTH",
                "parameter_name": "session_end_time",
                "current_value": "09:45", "proposed_value": "09:15",
                "rationale": "decay",
                "confidence": "LOW", "sample_size": 122,
                "finding_id": "bias_momentum_dsr_2026-05-31",
            },
            ctx,
        )
        assert result["ok"] is False
        assert "confidence" in result["error"].lower()

    def test_propose_change_accepted_medium(self, ctx):
        result = so._dispatch_tool(
            "propose_change",
            {
                "strategy": "bias_momentum", "direction": "BOTH",
                "parameter_name": "session_end_time",
                "current_value": "09:45", "proposed_value": "09:15",
                "rationale": "Post-09:15 WR collapses",
                "confidence": "MEDIUM", "sample_size": 122,
                "finding_id": "bias_momentum_dsr_2026-05-31",
            },
            ctx,
        )
        assert result["ok"] is True
        assert len(ctx.pending_proposals) == 1
        prop = ctx.pending_proposals[0]
        assert prop["strategy"] == "bias_momentum"
        assert prop["finding_id"] == "bias_momentum_dsr_2026-05-31"
        assert prop["status"] == "PENDING_HUMAN_REVIEW"
        assert prop["approved"] is False

    def test_propose_change_rejects_small_n(self, ctx):
        result = so._dispatch_tool(
            "propose_change",
            {
                "strategy": "bias_momentum", "direction": "BOTH",
                "parameter_name": "session_end_time",
                "current_value": "09:45", "proposed_value": "09:15",
                "rationale": "tiny",
                "confidence": "MEDIUM", "sample_size": 10,
                "finding_id": "f1",
            },
            ctx,
        )
        assert result["ok"] is False
        assert "30" in result["error"]

    def test_unknown_tool_returns_error(self, ctx):
        result = so._dispatch_tool("not_a_tool", {}, ctx)
        assert result["ok"] is False
        assert "unknown" in result["error"].lower()


# ===========================================================================
# TestAudit
# ===========================================================================

class TestAudit:
    """audit.jsonl: one JSON line per dispatch, parseable."""

    def test_one_line_per_dispatch(self, ctx, audit_fh):
        fh, path = audit_fh
        so._dispatch_tool("think", {"reasoning": "r1"}, ctx)
        so._dispatch_tool("think", {"reasoning": "r2"}, ctx)
        so._dispatch_tool("check_regime", {}, ctx)
        fh.flush()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    def test_lines_are_json_parseable(self, ctx, audit_fh):
        fh, path = audit_fh
        so._dispatch_tool(
            "fetch_strategy_stats", {"strategy": "bias_momentum"}, ctx,
        )
        fh.flush()
        line = path.read_text(encoding="utf-8").strip()
        rec = json.loads(line)
        assert rec["tool"] == "fetch_strategy_stats"
        assert "ts" in rec
        assert rec["mode"] == "weekly"

    def test_failed_dispatch_still_audited(self, ctx, audit_fh):
        fh, path = audit_fh
        so._dispatch_tool("not_a_tool", {}, ctx)
        fh.flush()
        line = path.read_text(encoding="utf-8").strip()
        rec = json.loads(line)
        assert rec["result_ok"] is False


# ===========================================================================
# TestCIInvariant
# ===========================================================================

class TestCIInvariant:
    """The CI invariant scanner rejects trade-path imports."""

    def test_passes_against_live_module(self):
        # Must not raise.
        so._ci_invariant_check()

    def test_rejects_bots_import(self, tmp_path):
        src = "from bots.base_bot import BaseBot\n"
        with pytest.raises(RuntimeError, match="bots"):
            so._scan_source_for_forbidden_imports(src)

    def test_rejects_core_import(self):
        src = "from core.position_manager import PositionManager\n"
        with pytest.raises(RuntimeError, match="core"):
            so._scan_source_for_forbidden_imports(src)

    def test_rejects_bridge_import(self):
        src = "import bridge.ws_bridge\n"
        with pytest.raises(RuntimeError, match="bridge"):
            so._scan_source_for_forbidden_imports(src)

    def test_rejects_data_feeds_import(self):
        src = "from data_feeds.tick_stream import TickStream\n"
        with pytest.raises(RuntimeError, match="data_feeds"):
            so._scan_source_for_forbidden_imports(src)

    def test_accepts_clean_source(self):
        src = (
            "from analytics import compute_engine\n"
            "from anthropic import Anthropic\n"
            "import json, os\n"
        )
        # Must not raise.
        so._scan_source_for_forbidden_imports(src)

    def test_module_marker_set(self):
        assert so.__no_trade_path_imports__ is True


# ===========================================================================
# TestRegimeHalt
# ===========================================================================

class TestRegimeHalt:
    """Regime-instability halt behavior per spec sec 4."""

    def test_weekly_halts_on_unstable(self, monkeypatch, tmp_logs_root,
                                       synth_facts, tmp_path):
        unstable = {
            "stable": False, "z_score": 2.3, "warning": "regime shift",
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 1.4,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "warehouse.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate", lambda conn, mode: unstable)
        monkeypatch.setattr(so, "_build_facts", lambda conn, mode, regime: synth_facts)

        result = so.run("weekly", client=_StubAnthropicClient())
        assert result["status"] == "halted_regime_unstable"
        assert result["regime"]["stable"] is False
        assert Path(result["debrief_path"]).exists()

    def test_daily_does_not_halt_on_unstable(self, monkeypatch,
                                              tmp_logs_root, synth_facts,
                                              tmp_path):
        # Daily mode short-circuits the regime gate to mode_skipped=True.
        skipped = {
            "stable": True, "z_score": float("nan"), "warning": None,
            "mode_skipped": True, "baseline_n_months": 0,
            "latest_month": None, "latest_sharpe_proxy": None,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "warehouse.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: skipped)
        monkeypatch.setattr(so, "_build_facts",
                             lambda conn, mode, regime: synth_facts)

        client = _StubAnthropicClient([
            _StubMessage([{"type": "text", "text": "Daily preliminary scan."}],
                          stop_reason="end_turn"),
        ])
        result = so.run("daily", client=client)
        assert result["status"] == "complete"


# ===========================================================================
# TestPreflight
# ===========================================================================

class TestPreflight:

    def test_missing_api_key(self, monkeypatch, tmp_logs_root):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = so.run("weekly")
        assert result["status"] == "halted_no_api_key"

    def test_missing_warehouse(self, monkeypatch, tmp_logs_root, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        bad_path = tmp_path / "definitely_not_here.duckdb"
        monkeypatch.setattr(so, "WAREHOUSE_PATH", str(bad_path))
        result = so.run("weekly")
        assert result["status"] == "halted_preflight_failure"


# ===========================================================================
# TestNoForbiddenImports
# ===========================================================================

class TestNoForbiddenImports:
    """AST scan of the actual module file rejects forbidden patterns."""

    def test_strategy_oracle_file_clean(self):
        src = Path(so.__file__).read_text(encoding="utf-8")
        # Must not raise.
        so._scan_source_for_forbidden_imports(src)

    def test_marker_at_module_top(self):
        src = Path(so.__file__).read_text(encoding="utf-8")
        assert "__no_trade_path_imports__ = True" in src


# ===========================================================================
# Helpers for integration tests
# ===========================================================================

class _StubConn:
    """Drop-in DuckDB connection used in integration tests."""

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _stub_build_facts(_conn, mode, regime):
    """Synthesize a minimal facts dict for pipeline tests."""
    return {
        "run_mode": mode,
        "run_date": "2026-05-31",
        "window_start": "2026-05-24",
        "window_end": "2026-05-30",
        "regime": regime,
        "n_trials_effective": 5,
        "strategies": {
            "bias_momentum": {
                "metrics": {
                    "n_trades": 122, "psr": 0.93, "dsr": 0.76, "min_trl": 87,
                    "hlz_t_stat": 3.4, "bhy_p_adjusted": 0.018,
                    "profit_factor": 1.82, "sortino": 1.41, "calmar": 0.93,
                    "max_drawdown_dollars": -1840.5,
                    "oos_pf": 1.62, "is_pf": 2.21, "wfe_ratio": 0.73,
                    "win_rate": 0.55,
                },
                "gates": {
                    "n_floor": True, "n_medium": True, "n_high": False,
                    "psr_0_90": True, "dsr_0_90": True, "dsr_0_95": False,
                    "hlz_3_0": True, "min_trl_met": True, "wfa_pass": True,
                    "all_pass_for_proposal": False,
                    "failed_gates": ["dsr_0_95"],
                },
                "gate_thresholds": {
                    "dsr_high": 0.95, "dsr_luck_floor": 0.90, "psr": 0.90,
                    "hlz_t_stat": 3.0, "n_floor": 30, "n_medium": 100,
                    "n_high": 200, "wfe_ratio_min": 0.6,
                },
            },
        },
        "findings": [],
        "prior_findings_loaded": [],
        "delta_vs_prior": {"is_baseline": True},
    }


# ===========================================================================
# TestPipelineIntegration
# ===========================================================================

class TestPipelineIntegration:
    """Full run() against stubbed external surfaces."""

    def _common_monkeypatch(self, monkeypatch, tmp_logs_root, tmp_path,
                             stable: bool = True):
        regime = {
            "stable": stable, "z_score": 0.42 if stable else 2.3,
            "warning": None if stable else "shift",
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "warehouse.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)

    def test_full_weekly_run_produces_all_files(self, monkeypatch, tmp_logs_root,
                                                  tmp_path):
        self._common_monkeypatch(monkeypatch, tmp_logs_root, tmp_path)
        client = _StubAnthropicClient([
            _StubMessage(
                [
                    {"type": "tool_use", "name": "write_finding", "id": "tu1",
                     "input": {
                         "id": "bm_finding_2026-05-31",
                         "strategy": "bias_momentum",
                         "verdict": "CONFIRMED",
                         "confidence": "MEDIUM",
                         "sample_size": 122,
                         "rationale": "DSR=0.76 with n=122 trades.",
                     }},
                ],
                stop_reason="tool_use",
            ),
            _StubMessage(
                [{"type": "text", "text": "Final weekly narrative: 122 trades observed."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        assert result["status"] == "complete"
        assert Path(result["debrief_path"]).exists()
        assert Path(result["facts_path"]).exists()
        assert Path(result["audit_path"]).exists()
        assert Path(result["pending_changes_path"]).exists()

    def test_facts_has_expected_top_level_keys(self, monkeypatch, tmp_logs_root,
                                                 tmp_path):
        self._common_monkeypatch(monkeypatch, tmp_logs_root, tmp_path)
        client = _StubAnthropicClient([
            _StubMessage(
                [{"type": "text", "text": "Done."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        with open(result["facts_path"], "r", encoding="utf-8") as fh:
            facts = json.load(fh)
        for key in ("run_mode", "run_date", "strategies", "findings"):
            assert key in facts, f"missing key {key} in facts.json"

    def test_debrief_has_report_card_section(self, monkeypatch, tmp_logs_root,
                                              tmp_path):
        self._common_monkeypatch(monkeypatch, tmp_logs_root, tmp_path)
        client = _StubAnthropicClient([
            _StubMessage(
                [{"type": "text", "text": "Some narrative."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        text = Path(result["debrief_path"]).read_text(encoding="utf-8")
        assert "Report Card" in text

    def test_pending_changes_is_append_only(self, monkeypatch, tmp_logs_root,
                                              tmp_path):
        self._common_monkeypatch(monkeypatch, tmp_logs_root, tmp_path)
        # Run 1: stage one proposal.
        client1 = _StubAnthropicClient([
            _StubMessage(
                [{"type": "tool_use", "name": "propose_change", "id": "tu1",
                  "input": {
                      "strategy": "bias_momentum", "direction": "BOTH",
                      "parameter_name": "session_end_time",
                      "current_value": "09:45", "proposed_value": "09:15",
                      "rationale": "n=122 DSR=0.76 decay observed",
                      "confidence": "MEDIUM", "sample_size": 122,
                      "finding_id": "bm_finding_1",
                  }}],
                stop_reason="tool_use",
            ),
            _StubMessage(
                [{"type": "text", "text": "Run 1 done."}],
                stop_reason="end_turn",
            ),
        ])
        r1 = so.run("weekly", client=client1)
        assert r1["n_proposals_staged"] == 1

        # Run 2: stage another. Should APPEND, not overwrite.
        client2 = _StubAnthropicClient([
            _StubMessage(
                [{"type": "tool_use", "name": "propose_change", "id": "tu2",
                  "input": {
                      "strategy": "bias_momentum", "direction": "LONG",
                      "parameter_name": "stop_atr_mult",
                      "current_value": 1.5, "proposed_value": 2.0,
                      "rationale": "MAE elbow at n=122",
                      "confidence": "HIGH", "sample_size": 122,
                      "finding_id": "bm_finding_2",
                  }}],
                stop_reason="tool_use",
            ),
            _StubMessage(
                [{"type": "text", "text": "Run 2 done."}],
                stop_reason="end_turn",
            ),
        ])
        r2 = so.run("weekly", client=client2)
        assert r2["n_proposals_staged"] == 1

        with open(r2["pending_changes_path"], "r", encoding="utf-8") as fh:
            pending = json.load(fh)
        # The shared pending_changes.json should now hold BOTH proposals.
        assert len(pending["pending"]) == 2

    def test_research_save_baseline(self, monkeypatch, tmp_logs_root, tmp_path):
        self._common_monkeypatch(monkeypatch, tmp_logs_root, tmp_path)
        client = _StubAnthropicClient([
            _StubMessage(
                [{"type": "text", "text": "Research narrative."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("research", client=client, save_baseline=True)
        baseline = tmp_logs_root / "research" / "baseline_facts.json"
        assert baseline.exists()

    def test_verifier_rejects_lookahead_finding(self, monkeypatch, tmp_logs_root,
                                                  tmp_path):
        """A lookahead/event-keyword finding rationale must NOT survive
        into the final debrief (verifier rejection)."""
        self._common_monkeypatch(monkeypatch, tmp_logs_root, tmp_path)
        client = _StubAnthropicClient([
            _StubMessage(
                [{"type": "tool_use", "name": "write_finding", "id": "tu1",
                  "input": {
                      "id": "bad_finding",
                      "strategy": "bias_momentum",
                      "verdict": "CONFIRMED",
                      "confidence": "MEDIUM",
                      "sample_size": 122,
                      "rationale": (
                          "The market crashed in 2022-10 which is why "
                          "DSR=0.76."),
                  }}],
                stop_reason="tool_use",
            ),
            _StubMessage(
                [{"type": "text", "text": "Final narrative for bias_momentum, n=122 trades."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        # The verifier must record at least one rejection.
        assert result["n_findings_rejected_by_verifier"] >= 1


# ===========================================================================
# Misc surface checks
# ===========================================================================

class TestPublicSurface:

    def test_run_callable(self):
        assert callable(so.run)

    def test_tools_count(self):
        names = [t["name"] for t in so.TOOLS]
        assert sorted(names) == sorted([
            "think", "fetch_strategy_stats", "check_regime",
            "write_finding", "propose_change",
        ])

    def test_each_tool_has_input_schema(self):
        for t in so.TOOLS:
            assert "input_schema" in t
            assert t["input_schema"]["type"] == "object"
