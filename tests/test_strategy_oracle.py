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
        monkeypatch.setattr(
            so, "_build_facts",
            lambda conn, mode, regime, root=None: synth_facts,
        )

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
        monkeypatch.setattr(
            so, "_build_facts",
            lambda conn, mode, regime, root=None: synth_facts,
        )

        client = _StubAnthropicClient([
            _StubMessage([{"type": "text", "text": "Daily preliminary scan."}],
                          stop_reason="end_turn"),
        ])
        result = so.run("daily", client=client)
        assert result["status"] == "complete"

    def test_research_threshold_passes_at_z_2_97(self, monkeypatch,
                                                    tmp_logs_root, synth_facts,
                                                    tmp_path):
        """Research mode uses a LOOSER z-threshold of 3.0. At z=2.97 (today's
        actual reading on 2026-06-01) the gate reports STABLE and analysis
        proceeds normally -- no halt, no special warning needed.

        Operator-authorized 2026-06-01 (second pass): preferred over the
        prior halt_on_unstable_regime=False approach because it surfaces
        false-stable warnings only when z really crosses 3 sigma.
        """
        # Stub returns the verdict the real regime gate would return at
        # z=2.97 with threshold=3.0: stable=True.
        stable_under_3 = {
            "stable": True, "z_score": 2.97, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.115,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "warehouse.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: stable_under_3)
        facts_with_regime = dict(synth_facts)
        facts_with_regime["regime"] = stable_under_3
        monkeypatch.setattr(
            so, "_build_facts",
            lambda conn, mode, regime, root=None: facts_with_regime,
        )

        client = _StubAnthropicClient([
            _StubMessage([{"type": "text",
                             "text": "Research narrative on stable regime."}],
                          stop_reason="end_turn"),
        ])
        result = so.run("research", client=client)
        assert result["status"] == "complete"
        assert result["regime"]["stable"] is True
        assert result["regime"]["z_score"] == 2.97

    def test_research_halts_at_z_4(self, monkeypatch,
                                    tmp_logs_root, synth_facts, tmp_path):
        """Research mode still halts at extreme z. With threshold 3.0,
        z=4.0 triggers the halt path -- the gate hasn't been declawed."""
        unstable_extreme = {
            "stable": False, "z_score": 4.0, "warning": "extreme regime shift",
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.2,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "warehouse.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: unstable_extreme)
        monkeypatch.setattr(
            so, "_build_facts",
            lambda conn, mode, regime, root=None: synth_facts,
        )
        result = so.run("research", client=_StubAnthropicClient())
        assert result["status"] == "halted_regime_unstable"
        assert result["regime"]["z_score"] == 4.0

    def test_check_regime_gate_passes_z_threshold_for_research(self, monkeypatch):
        """The _check_regime_gate wrapper must thread MODE_CONFIG's
        z_threshold override down to check_regime_stability."""
        captured = {}

        def fake_check(conn, mode, **kwargs):
            captured["mode"] = mode
            captured["kwargs"] = kwargs
            return {"stable": True, "z_score": 0.0, "warning": None,
                    "mode_skipped": False, "baseline_n_months": 6,
                    "latest_month": "2026-05", "latest_sharpe_proxy": 0.0}

        monkeypatch.setattr(so.regime_gate, "check_regime_stability", fake_check)

        so._check_regime_gate(None, "research")
        assert captured["kwargs"].get("z_threshold") == 3.0

        captured.clear()
        so._check_regime_gate(None, "weekly")
        # weekly has z_threshold=None -> wrapper omits the kwarg so the
        # default (1.5) inside check_regime_stability is used
        assert "z_threshold" not in captured["kwargs"]

    def test_mode_config_thresholds_and_halts(self):
        """Pin the per-mode configuration so a refactor cannot silently
        change which modes halt or what their z-thresholds are."""
        assert so.MODE_CONFIG["research"]["halt_on_unstable_regime"] is True
        assert so.MODE_CONFIG["weekly"]["halt_on_unstable_regime"] is True
        assert so.MODE_CONFIG["daily"]["halt_on_unstable_regime"] is False
        assert so.MODE_CONFIG["research"]["z_threshold"] == 3.0
        assert so.MODE_CONFIG["weekly"]["z_threshold"] is None
        assert so.MODE_CONFIG["daily"]["z_threshold"] is None


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


def _stub_build_facts(_conn, mode, regime, _root=None):
    """Synthesize a minimal facts dict for pipeline tests.

    Accepts an optional ``_root`` argument to match the production
    signature (which threads the logs root in for prior-findings lookup).
    Tests ignore it -- the stub returns a synthetic panel directly.
    """
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


# ===========================================================================
# TestCodeReviewFixes -- regression coverage for Task 5 code review fixes
# ===========================================================================

class _OSErrorAuditStub:
    """Stub audit handle that raises OSError on every write.

    Simulates disk-full / closed-handle / permission-revoked conditions.
    """

    def __init__(self):
        self.closed = False

    def write(self, _payload):
        raise OSError("simulated audit IO failure")

    def flush(self):
        return None

    def close(self):
        self.closed = True


class TestCodeReviewFixes:
    """Regression tests for the Task 5 code review fixes."""

    # --- Critical 1: _write_audit must swallow OSError --------------------

    def test_write_audit_swallows_oserror(self):
        """OSError from the fh.write must not propagate -- the orchestrator
        catches it and logs a warning so the LLM loop survives."""
        fh = _OSErrorAuditStub()
        # Must not raise.
        so._write_audit(fh, {"tool": "think", "input": {"reasoning": "x"}})

    def test_dispatch_tool_survives_audit_oserror(self, synth_facts):
        """Even when the audit handle is broken, dispatching a tool must
        still return the tool's normal result dict."""
        ctx = so._RunCtx(
            mode="weekly",
            facts=synth_facts,
            audit_fh=_OSErrorAuditStub(),
            run_date="2026-05-31",
            pending_proposals=[],
        )
        result = so._dispatch_tool("think", {"reasoning": "ok"}, ctx)
        assert result["ok"] is True

    # --- Critical 2: max_tokens must terminate the loop -------------------

    def test_max_tokens_exits_loop_after_one_turn(self, monkeypatch,
                                                    tmp_logs_root, tmp_path):
        """A max_tokens response (even with a tool_use block) must terminate
        the LLM loop immediately rather than spinning for 25 iterations."""
        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)

        client = _StubAnthropicClient([
            _StubMessage(
                [
                    {"type": "text", "text": "Partial narrative truncated."},
                    {"type": "tool_use", "name": "think", "id": "tu1",
                     "input": {"reasoning": "more to think"}},
                ],
                stop_reason="max_tokens",
            ),
        ])
        result = so.run("weekly", client=client)
        # Run completes (not halted) and only one API call was made.
        assert result["status"] == "complete"
        assert len(client.messages.calls) == 1

    def test_stop_sequence_also_exits_loop(self, monkeypatch,
                                             tmp_logs_root, tmp_path):
        """Any canonical terminal stop_reason should exit -- spot-check
        stop_sequence."""
        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)

        client = _StubAnthropicClient([
            _StubMessage(
                [{"type": "text", "text": "Stopped at sequence."}],
                stop_reason="stop_sequence",
            ),
        ])
        result = so.run("weekly", client=client)
        assert result["status"] == "complete"
        assert len(client.messages.calls) == 1

    def test_terminal_stop_reasons_constant_includes_required_values(self):
        """The exported constant must include max_tokens at minimum."""
        for required in ("end_turn", "max_tokens", "stop_sequence",
                          "pause_turn", "refusal"):
            assert required in so.TERMINAL_STOP_REASONS

    # --- Critical 3: TOCTOU single-pass strategy_trades --------------------

    def test_build_facts_calls_trades_for_strategy_once_per_strategy(
            self, monkeypatch, tmp_path):
        """Single warehouse round-trip per strategy -- no double scan."""
        import pandas as pd

        # Tally how many times trades_for_strategy is asked for each strat.
        call_counts: dict[str, int] = {}

        def fake_trades(_conn, strat, _window):
            call_counts[strat] = call_counts.get(strat, 0) + 1
            return pd.DataFrame({
                "pnl_dollars": [10.0, -5.0, 3.0] * 15,  # 45 rows
            })

        def fake_strategies(_conn, _window, min_n=30):
            return ["a", "b", "c"]

        def fake_wfa(_conn, _strat):
            return {"mean_oos_pf": 1.0, "mean_is_pf": 1.0,
                    "wfa_pass": True}

        def fake_metrics(_trades, _wfa, _n_eff):
            return {"metrics": {"n_trades": 45}, "gates": {}}

        def fake_eff_n(_returns):
            return 5

        def fake_delta(_facts, _prior):
            return {"is_baseline": True}

        monkeypatch.setattr(so.prepared_queries, "strategies_with_trades",
                             fake_strategies)
        monkeypatch.setattr(so.prepared_queries, "trades_for_strategy",
                             fake_trades)
        monkeypatch.setattr(so.prepared_queries, "wfa_summary_for_strategy",
                             fake_wfa)
        monkeypatch.setattr(so.compute_engine, "compute_effective_n",
                             fake_eff_n)
        monkeypatch.setattr(so.compute_engine, "compute_strategy_metrics",
                             fake_metrics)
        monkeypatch.setattr(so.compute_engine, "compute_delta_vs_prior",
                             fake_delta)

        regime = {"stable": True}
        facts = so._build_facts(_StubConn(), "weekly", regime, tmp_path)

        # Each strategy queried exactly once (TOCTOU fix).
        assert call_counts == {"a": 1, "b": 1, "c": 1}
        assert set(facts["strategies"].keys()) == {"a", "b", "c"}

    # --- Important 4: unknown-mode early return has full 11-key shape -----

    def test_unknown_mode_returns_full_shape(self, monkeypatch, tmp_logs_root):
        """run('bogus') must return the same 11 keys every other halt
        branch returns so callers can treat the dict uniformly."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        result = so.run("bogus")
        expected = {
            "status", "mode", "reason",
            "facts_path", "debrief_path", "audit_path",
            "pending_changes_path",
            "n_findings", "n_proposals_staged",
            "n_findings_rejected_by_verifier",
            "regime", "verifier_result",
        }
        missing = expected - set(result.keys())
        assert not missing, f"unknown-mode return missing keys: {missing}"
        assert result["status"] == "halted_preflight_failure"

    # --- Important 5: budget nudge merged into single user message --------

    def test_budget_nudge_does_not_duplicate_user_message(
            self, monkeypatch, tmp_logs_root, tmp_path):
        """When the token budget is exceeded, the wrap-up nudge must be
        merged into the existing tool_result user message (not appended
        as a second consecutive user message, which would 400 the API)."""
        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)
        # Force the budget to be exceeded after the first turn.
        monkeypatch.setitem(so.MODE_CONFIG["weekly"], "token_budget", 100)

        client = _StubAnthropicClient([
            # Turn 1: emit a tool_use with HUGE usage to blow the budget.
            _StubMessage(
                [{"type": "tool_use", "name": "think", "id": "tu1",
                  "input": {"reasoning": "step 1"}}],
                stop_reason="tool_use",
                input_tokens=500, output_tokens=500,
            ),
            # Turn 2: respond with a terminal stop_reason so the loop exits.
            _StubMessage(
                [{"type": "text", "text": "Wrapping up."}],
                stop_reason="end_turn",
                input_tokens=10, output_tokens=10,
            ),
        ])
        result = so.run("weekly", client=client)
        assert result["status"] == "complete"
        # Inspect the messages array on the SECOND API call -- it should
        # have valid alternation (no two consecutive user messages).
        second_call = client.messages.calls[1]
        msgs = second_call["messages"]
        roles = [m["role"] for m in msgs]
        for i in range(1, len(roles)):
            assert roles[i] != roles[i - 1], (
                f"consecutive same-role messages at {i}: {roles}"
            )
        # And the merged user message must contain BOTH a tool_result
        # block AND the budget-nudge text block.
        user_after_assist = msgs[2]  # user, assistant, user(tool_result+nudge)
        assert user_after_assist["role"] == "user"
        types_in_user = [b["type"] for b in user_after_assist["content"]]
        assert "tool_result" in types_in_user
        assert "text" in types_in_user

    # --- Important 8: _load_prior_findings reads `root` parameter --------

    def test_load_prior_findings_uses_root_param(self, tmp_path):
        """_load_prior_findings must read from the explicit `root`
        argument, not the module-level LOGS_ORACLE_ROOT."""
        # Build a synthetic logs/oracle/weekly/<date>_facts.json under a
        # path that is NOT the module-level constant.
        custom_root = tmp_path / "alt_logs" / "oracle"
        (custom_root / "weekly").mkdir(parents=True)
        facts_path = custom_root / "weekly" / "2026-05-30_facts.json"
        facts_path.write_text(json.dumps({
            "run_mode": "weekly",
            "run_date": "2026-05-30",
            "findings": [
                {"id": "test_finding_1", "strategy": "x",
                 "rationale": "test rationale",
                 "confidence": "MEDIUM",
                 "expires_after_days": 30},
            ],
        }), encoding="utf-8")

        found = so._load_prior_findings(custom_root)
        assert len(found) == 1
        assert found[0]["id"] == "test_finding_1"

    # --- Minor 11: lookahead note gated by date ---------------------------

    def test_lookahead_note_suppressed_when_window_after_cutoff(
            self, monkeypatch, tmp_logs_root, tmp_path):
        """If the analysis window ends strictly after LOOKAHEAD_CUTOFF_DATE,
        the lookahead note should NOT be emitted in the debrief."""
        # Push the cutoff to a date BEFORE the stub's window_end of
        # 2026-05-30 so the suppression branch fires.
        monkeypatch.setattr(so, "LOOKAHEAD_CUTOFF_DATE", "2026-01-31")

        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)

        client = _StubAnthropicClient([
            _StubMessage(
                [{"type": "text", "text": "Narrative."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        text = Path(result["debrief_path"]).read_text(encoding="utf-8")
        assert "Look-Ahead Note" not in text

    def test_lookahead_note_present_when_window_overlaps_cutoff(
            self, monkeypatch, tmp_logs_root, tmp_path):
        """When window_end is at or before the cutoff, the note must
        be rendered."""
        # Cutoff set well after the stub's window_end of 2026-05-30
        # -> window_end <= cutoff -> note SHOULD render.
        monkeypatch.setattr(so, "LOOKAHEAD_CUTOFF_DATE", "2026-12-31")

        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)

        client = _StubAnthropicClient([
            _StubMessage(
                [{"type": "text", "text": "Narrative."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        text = Path(result["debrief_path"]).read_text(encoding="utf-8")
        assert "Look-Ahead Note" in text


# ===========================================================================
# TestFinalCritiqueFixes -- regression coverage for the final critique pass
# ===========================================================================

class TestFinalCritiqueFixes:
    """Regression tests for the Phoenix Strategy Oracle final critique pass."""

    # --- Critical 1: lookahead_active computed from window vs cutoff -----

    def test_lookahead_downgrades_suppressed_post_cutoff(
            self, monkeypatch, tmp_logs_root, tmp_path):
        """With window_end strictly after LOOKAHEAD_CUTOFF_DATE, the verifier
        is called with lookahead_active=False and no downgrades fire."""
        captured: dict[str, Any] = {}

        def fake_verify(*, facts, narrative_md, findings, lookahead_active):
            captured["lookahead_active"] = lookahead_active
            return {"ok": True, "rejected_findings": [], "downgrades": [],
                    "numbers_check": {}, "lookahead_check": {},
                    "causal_check": {}, "rejection_reasons": {}}

        monkeypatch.setattr(so.verifier, "verify_report", fake_verify)
        # Cutoff = 2026-01-31; stub window_end = 2026-05-30 (after cutoff)
        monkeypatch.setattr(so, "LOOKAHEAD_CUTOFF_DATE", "2026-01-31")

        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)

        client = _StubAnthropicClient([
            _StubMessage([{"type": "text", "text": "Done."}],
                          stop_reason="end_turn"),
        ])
        result = so.run("weekly", client=client)
        assert result["status"] == "complete"
        assert captured["lookahead_active"] is False

    def test_lookahead_downgrades_active_pre_cutoff(
            self, monkeypatch, tmp_logs_root, tmp_path):
        """With window_end at/before LOOKAHEAD_CUTOFF_DATE, the verifier
        is called with lookahead_active=True so INTERPRETATION findings
        are downgraded."""
        captured: dict[str, Any] = {}

        def fake_verify(*, facts, narrative_md, findings, lookahead_active):
            captured["lookahead_active"] = lookahead_active
            return {"ok": True, "rejected_findings": [], "downgrades": [],
                    "numbers_check": {}, "lookahead_check": {},
                    "causal_check": {}, "rejection_reasons": {}}

        monkeypatch.setattr(so.verifier, "verify_report", fake_verify)
        # Push cutoff past the stub's window_end (2026-05-30) so the
        # condition window_end <= cutoff holds.
        monkeypatch.setattr(so, "LOOKAHEAD_CUTOFF_DATE", "2026-12-31")

        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)

        client = _StubAnthropicClient([
            _StubMessage([{"type": "text", "text": "Done."}],
                          stop_reason="end_turn"),
        ])
        result = so.run("weekly", client=client)
        assert result["status"] == "complete"
        assert captured["lookahead_active"] is True

    # --- Critical 2: splits dict populated per strategy -------------------

    def test_build_facts_populates_splits(self, monkeypatch, tmp_path):
        """_build_facts must emit a `splits` dict with all six T1 keys
        for each strategy."""
        import pandas as pd

        def fake_strategies(_conn, _window, min_n=30):
            return ["bias_momentum"]

        def fake_trades(_conn, _strat, _window):
            return pd.DataFrame({"pnl_dollars": [1.0] * 40})

        def fake_wfa(_conn, _strat):
            return {"mean_oos_pf": 1.0, "mean_is_pf": 1.0}

        def fake_metrics(_trades, _wfa, _n_eff):
            return {"metrics": {"n_trades": 40}, "gates": {}}

        # All six split queries return small DataFrames.
        def fake_hour(_conn, _strat, _window):
            return pd.DataFrame({"hour_ct": [9, 10], "n_trades": [20, 20]})

        def fake_regime(_conn, _strat, _window):
            return pd.DataFrame({"regime": ["trending"], "n_trades": [40]})

        def fake_direction(_conn, _strat, _window):
            return pd.DataFrame({"direction": ["LONG"], "n_trades": [40]})

        def fake_mae_mfe(_conn, _strat, _direction, _window):
            return pd.DataFrame({"bucket_ticks": [0, 1], "n_trades": [20, 20]})

        def fake_conflu(_conn, _strat, _window):
            return pd.DataFrame({"confluence_count": [0], "n_trades": [40]})

        monkeypatch.setattr(so.prepared_queries, "strategies_with_trades",
                             fake_strategies)
        monkeypatch.setattr(so.prepared_queries, "trades_for_strategy",
                             fake_trades)
        monkeypatch.setattr(so.prepared_queries, "wfa_summary_for_strategy",
                             fake_wfa)
        monkeypatch.setattr(so.prepared_queries, "panel_by_hour_ct",
                             fake_hour)
        monkeypatch.setattr(so.prepared_queries, "panel_by_regime",
                             fake_regime)
        monkeypatch.setattr(so.prepared_queries, "panel_by_direction",
                             fake_direction)
        monkeypatch.setattr(so.prepared_queries, "mae_mfe_distribution",
                             fake_mae_mfe)
        monkeypatch.setattr(so.prepared_queries, "confluence_lift",
                             fake_conflu)
        monkeypatch.setattr(so.compute_engine, "compute_strategy_metrics",
                             fake_metrics)
        monkeypatch.setattr(so.compute_engine, "compute_effective_n",
                             lambda _r: 1)
        monkeypatch.setattr(so.compute_engine, "compute_delta_vs_prior",
                             lambda _f, _p: {"is_baseline": True})

        regime = {"stable": True}
        facts = so._build_facts(_StubConn(), "weekly", regime, tmp_path)
        panel = facts["strategies"]["bias_momentum"]
        assert "splits" in panel
        for key in ("by_hour_ct", "by_regime", "by_direction",
                    "mae_mfe_long", "mae_mfe_short", "confluence_lift"):
            assert key in panel["splits"], f"splits missing {key}"
        # Each split is a list of records (aggregate-only).
        assert isinstance(panel["splits"]["by_hour_ct"], list)
        assert isinstance(panel["splits"]["by_regime"], list)

    def test_build_facts_splits_failure_recovers(self, monkeypatch, tmp_path):
        """If a split query raises, splits is reset to {} but the rest of
        the facts panel survives."""
        import pandas as pd

        monkeypatch.setattr(so.prepared_queries, "strategies_with_trades",
                             lambda _c, _w, min_n=30: ["s1"])
        monkeypatch.setattr(so.prepared_queries, "trades_for_strategy",
                             lambda _c, _s, _w:
                             pd.DataFrame({"pnl_dollars": [1.0] * 40}))
        monkeypatch.setattr(so.prepared_queries, "wfa_summary_for_strategy",
                             lambda _c, _s: {"mean_oos_pf": 1, "mean_is_pf": 1})

        def boom(_conn, _strat, _window):
            raise RuntimeError("DuckDB exploded")

        monkeypatch.setattr(so.prepared_queries, "panel_by_hour_ct", boom)
        monkeypatch.setattr(so.compute_engine, "compute_strategy_metrics",
                             lambda _t, _w, _n: {"metrics": {"n_trades": 40},
                                                  "gates": {}})
        monkeypatch.setattr(so.compute_engine, "compute_effective_n",
                             lambda _r: 1)
        monkeypatch.setattr(so.compute_engine, "compute_delta_vs_prior",
                             lambda _f, _p: {"is_baseline": True})

        regime = {"stable": True}
        facts = so._build_facts(_StubConn(), "weekly", regime, tmp_path)
        assert facts["strategies"]["s1"]["splits"] == {}

    # --- Critical 3: current_value pulled from AST parse ------------------

    def test_propose_change_overrides_current_value_with_ast(
            self, monkeypatch, ctx):
        """The orchestrator must replace the LLM-supplied current_value
        with the AST-parsed value from config/strategies.py."""
        monkeypatch.setattr(
            so.prepared_queries, "current_param_value",
            lambda strat, param: "09:30",  # the real value per AST
        )
        result = so._dispatch_tool(
            "propose_change",
            {
                "strategy": "bias_momentum", "direction": "BOTH",
                "parameter_name": "session_end_time",
                "current_value": "bogus_value",  # LLM lies
                "proposed_value": "09:15",
                "rationale": "decay observed",
                "confidence": "MEDIUM", "sample_size": 122,
                "finding_id": "bm_finding",
            },
            ctx,
        )
        assert result["ok"] is True
        proposal = ctx.pending_proposals[0]
        assert proposal["current_value"] == "09:30"

    def test_propose_change_falls_back_on_ast_error(self, monkeypatch, ctx):
        """If AST lookup raises KeyError / FileNotFoundError / ValueError,
        fall back to the LLM-supplied value (with a warning log)."""
        def boom(_strat, _param):
            raise KeyError("not in STRATEGIES")

        monkeypatch.setattr(so.prepared_queries, "current_param_value", boom)
        result = so._dispatch_tool(
            "propose_change",
            {
                "strategy": "bias_momentum", "direction": "BOTH",
                "parameter_name": "session_end_time",
                "current_value": "fallback_value",
                "proposed_value": "09:15",
                "rationale": "decay observed",
                "confidence": "MEDIUM", "sample_size": 122,
                "finding_id": "bm_finding",
            },
            ctx,
        )
        assert result["ok"] is True
        proposal = ctx.pending_proposals[0]
        assert proposal["current_value"] == "fallback_value"

    # --- Important 1: audit.jsonl is append-only across opens -------------

    def test_open_audit_appends_across_opens(self, tmp_path):
        """Two consecutive opens of the same (mode, date) audit file
        accumulate -- neither truncates the prior content."""
        root = tmp_path / "logs" / "oracle"
        root.mkdir(parents=True)

        fh1 = so._open_audit("weekly", "2026-05-31", root)
        fh1.write('{"event": "first"}\n')
        fh1.flush()
        fh1.close()

        fh2 = so._open_audit("weekly", "2026-05-31", root)
        fh2.write('{"event": "second"}\n')
        fh2.flush()
        fh2.close()

        path = root / "weekly" / "2026-05-31_audit.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert '"first"' in lines[0]
        assert '"second"' in lines[1]

    # --- Important 2: _write_audit flushes after each write ---------------

    def test_write_audit_flushes_immediately(self, tmp_path):
        """Each event hits disk before the next dispatch call."""
        path = tmp_path / "audit.jsonl"
        fh = open(path, "a", encoding="utf-8")
        try:
            so._write_audit(fh, {"event": "alpha"})
            # Read from a SECOND handle without closing fh: only works
            # if fh.flush() pushed the buffer.
            with open(path, "r", encoding="utf-8") as fh_r:
                content = fh_r.read()
            assert "alpha" in content
        finally:
            fh.close()

    # --- Important 3: pending_changes.json tmp filename is per-process ---

    def test_pending_changes_tmp_filename_is_unique(self, monkeypatch,
                                                    tmp_path):
        """Two concurrent saves must use distinct tmp paths so they
        cannot race-overwrite each other before os.replace."""
        captured_tmps: list[str] = []

        real_write_text = Path.write_text

        def tracking_write_text(self, *args, **kwargs):
            if ".pending_changes." in self.name and self.name.endswith(".tmp"):
                captured_tmps.append(self.name)
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", tracking_write_text)

        root = tmp_path / "logs" / "oracle"
        root.mkdir(parents=True)
        so._save_pending_proposals([{"strategy": "x", "p": 1}], root)
        # Sleep 2ms so the time-based suffix changes.
        import time as _t
        _t.sleep(0.002)
        so._save_pending_proposals([{"strategy": "y", "p": 2}], root)

        assert len(captured_tmps) == 2
        assert captured_tmps[0] != captured_tmps[1]
        # Both proposals must end up in the final file.
        pending = json.loads(
            (root / "pending_changes.json").read_text(encoding="utf-8")
        )
        assert len(pending["pending"]) == 2

    # --- Important 4: write_finding validates verdict/confidence ----------

    def test_write_finding_normalizes_lowercase_confidence(self, ctx):
        """confidence='medium' is normalized to 'MEDIUM' and accepted."""
        result = so._dispatch_tool(
            "write_finding",
            {
                "id": "lower_conf_test",
                "strategy": "bias_momentum",
                "verdict": "confirmed",  # also lowercase
                "confidence": "medium",
                "sample_size": 50,
                "rationale": "DSR=0.76",
            },
            ctx,
        )
        assert result["ok"] is True
        finding = ctx.facts["findings"][0]
        assert finding["confidence"] == "MEDIUM"
        assert finding["verdict"] == "CONFIRMED"

    def test_write_finding_rejects_garbage_confidence(self, ctx):
        result = so._dispatch_tool(
            "write_finding",
            {
                "id": "garbage_conf_test",
                "strategy": "bias_momentum",
                "verdict": "CONFIRMED",
                "confidence": "garbage",
                "sample_size": 50,
                "rationale": "DSR=0.76",
            },
            ctx,
        )
        assert result["ok"] is False
        assert "confidence" in result["error"].lower()
        assert ctx.facts["findings"] == []

    def test_write_finding_rejects_garbage_verdict(self, ctx):
        result = so._dispatch_tool(
            "write_finding",
            {
                "id": "garbage_verdict_test",
                "strategy": "bias_momentum",
                "verdict": "MAYBE",
                "confidence": "MEDIUM",
                "sample_size": 50,
                "rationale": "DSR=0.76",
            },
            ctx,
        )
        assert result["ok"] is False
        assert "verdict" in result["error"].lower()

    def test_propose_change_normalizes_lowercase_confidence(
            self, monkeypatch, ctx):
        monkeypatch.setattr(
            so.prepared_queries, "current_param_value",
            lambda strat, param: "09:30",
        )
        result = so._dispatch_tool(
            "propose_change",
            {
                "strategy": "bias_momentum", "direction": "BOTH",
                "parameter_name": "session_end_time",
                "current_value": "09:45", "proposed_value": "09:15",
                "rationale": "decay observed",
                "confidence": "medium",  # lowercase
                "sample_size": 122,
                "finding_id": "bm_finding",
            },
            ctx,
        )
        assert result["ok"] is True
        assert ctx.pending_proposals[0]["confidence"] == "MEDIUM"

    # --- Important 6: proposals from rejected findings are stripped -------

    def test_proposal_with_rejected_finding_id_is_stripped(
            self, monkeypatch, tmp_logs_root, tmp_path):
        """When the verifier rejects a finding, any proposal linked to it
        via finding_id must not end up in pending_changes.json."""
        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)
        # AST stub for the propose_change pathway.
        monkeypatch.setattr(
            so.prepared_queries, "current_param_value",
            lambda strat, param: "09:30",
        )
        # The verifier will reject "bad_finding".
        monkeypatch.setattr(
            so.verifier, "verify_report",
            lambda *, facts, narrative_md, findings, lookahead_active: {
                "ok": False,
                "rejected_findings": ["bad_finding"],
                "rejection_reasons": {"bad_finding": "test-reject"},
                "downgrades": [],
                "numbers_check": {}, "lookahead_check": {},
                "causal_check": {},
            },
        )

        client = _StubAnthropicClient([
            _StubMessage(
                [
                    {"type": "tool_use", "name": "write_finding", "id": "tu1",
                     "input": {
                         "id": "bad_finding",
                         "strategy": "bias_momentum",
                         "verdict": "CONFIRMED",
                         "confidence": "MEDIUM",
                         "sample_size": 122,
                         "rationale": "rejected by verifier (n=122)",
                     }},
                    {"type": "tool_use", "name": "propose_change", "id": "tu2",
                     "input": {
                         "strategy": "bias_momentum", "direction": "BOTH",
                         "parameter_name": "session_end_time",
                         "current_value": "09:45", "proposed_value": "09:15",
                         "rationale": "decay",
                         "confidence": "MEDIUM", "sample_size": 122,
                         "finding_id": "bad_finding",
                     }},
                ],
                stop_reason="tool_use",
            ),
            _StubMessage(
                [{"type": "text", "text": "n=122 done."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        assert result["status"] == "complete"
        # The proposal should have been stripped before persistence.
        assert result["n_proposals_staged"] == 0
        pending = json.loads(
            Path(result["pending_changes_path"]).read_text(encoding="utf-8")
        )
        assert all(p.get("finding_id") != "bad_finding"
                   for p in pending["pending"])

    # --- Minor 1/2: debrief includes delta + proposals sections -----------

    def test_debrief_renders_proposals_section(self, monkeypatch,
                                                 tmp_logs_root, tmp_path):
        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)
        monkeypatch.setattr(so, "_build_facts", _stub_build_facts)
        monkeypatch.setattr(
            so.prepared_queries, "current_param_value",
            lambda strat, param: "09:30",
        )

        client = _StubAnthropicClient([
            _StubMessage(
                [
                    {"type": "tool_use", "name": "propose_change", "id": "tu1",
                     "input": {
                         "strategy": "bias_momentum", "direction": "BOTH",
                         "parameter_name": "session_end_time",
                         "current_value": "09:45", "proposed_value": "09:15",
                         "rationale": "decay observed at 09:15",
                         "confidence": "MEDIUM", "sample_size": 122,
                         "finding_id": "bm_finding",
                     }}
                ],
                stop_reason="tool_use",
            ),
            _StubMessage(
                [{"type": "text", "text": "n=122 narrative."}],
                stop_reason="end_turn",
            ),
        ])
        result = so.run("weekly", client=client)
        text = Path(result["debrief_path"]).read_text(encoding="utf-8")
        assert "## Proposals" in text
        assert "bias_momentum.session_end_time" in text
        # The Why line cites the rationale.
        assert "decay observed" in text

    def test_debrief_delta_section_renders_when_strategies_changed(
            self, monkeypatch, tmp_logs_root, tmp_path):
        """When delta_vs_prior reports materially-changed strategies, the
        debrief must include a 'Delta vs Last Run' section."""
        regime = {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        }
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        monkeypatch.setattr(so, "_run_preflight",
                             lambda mode: {"ok": True, "warehouse_path":
                                            str(tmp_path / "wh.duckdb")})
        monkeypatch.setattr(so, "_open_warehouse_conn",
                             lambda path: _StubConn())
        monkeypatch.setattr(so, "_check_regime_gate",
                             lambda conn, mode: regime)

        def stub_with_delta(_conn, mode, regime, _root=None):
            facts = _stub_build_facts(_conn, mode, regime, _root)
            facts["delta_vs_prior"] = {
                "is_baseline": False,
                "strategies": {
                    "bias_momentum": {
                        "dsr_delta": 0.12,
                        "wr_delta": 0.04,
                        "n_delta": 50,
                        "materially_changed": True,
                        "tier_change": "UP",
                    },
                },
                "summary": {"n_strategies_changed": 1},
            }
            return facts

        monkeypatch.setattr(so, "_build_facts", stub_with_delta)

        client = _StubAnthropicClient([
            _StubMessage([{"type": "text", "text": "Narrative."}],
                          stop_reason="end_turn"),
        ])
        result = so.run("weekly", client=client)
        text = Path(result["debrief_path"]).read_text(encoding="utf-8")
        assert "Delta vs Last Run" in text
        assert "bias_momentum" in text
        assert "(UP)" in text
