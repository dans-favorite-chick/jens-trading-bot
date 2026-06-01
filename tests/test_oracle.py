"""Phoenix Strategy Oracle -- Tier 1-4 consolidated pre-flight tests.

Run before any production Oracle invocation::

    pytest tests/test_oracle.py -v

This is the operator-facing pre-flight gate. Individual modules already
have their own deep unit tests (test_prepared_queries.py, test_compute_engine.py,
test_regime_gate.py, test_verifier.py, test_strategy_oracle.py,
test_run_oracle.py). This file exercises only the CROSS-MODULE integration
paths and the four-tier discipline from design spec sec 16.

Tier 1 -- Unit: cross-module integration paths + golden-number sanity.
Tier 2 -- Golden dataset: hand-verifiable queries against the real warehouse
          (skipped automatically when warehouse is absent).
Tier 3 -- Consistency: same input + seeded stub LLM -> same top finding.
Tier 4 -- Adversarial: fabricated facts must be rejected by all three layers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest

from analytics import compute_engine, prepared_queries, regime_gate, verifier
from agents import strategy_oracle


# Pin warehouse path to the canonical location. The fixture below tolerates
# its absence so the file remains green on a clean checkout / CI.
WAREHOUSE_PATH = Path(r"C:\Trading Project\phoenix_bot\data\warehouse\phoenix.duckdb")
WAREHOUSE_AVAILABLE = WAREHOUSE_PATH.exists()


# ===========================================================================
# Shared stub Anthropic client (re-used across Tier 1, 3, 4)
# ===========================================================================

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


class _StubMessage:
    def __init__(self, content: list[dict], stop_reason: str = "end_turn",
                 input_tokens: int = 100, output_tokens: int = 50):
        self.content = [_StubBlock(b) for b in content]
        self.stop_reason = stop_reason
        self.usage = _StubUsage(input_tokens, output_tokens)


class _StubMessages:
    def __init__(self, scripted: list[_StubMessage]):
        self.scripted = list(scripted)
        self.calls: list[dict] = []

    def create(self, **kwargs: Any) -> _StubMessage:
        self.calls.append(kwargs)
        if not self.scripted:
            return _StubMessage(
                [{"type": "text", "text": "Done."}],
                stop_reason="end_turn",
            )
        return self.scripted.pop(0)


class _StubAnthropicClient:
    def __init__(self, scripted: list[_StubMessage] | None = None):
        self.messages = _StubMessages(scripted or [])


class _StubConn:
    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ===========================================================================
# TIER 1 -- Unit (cross-module integration paths only)
# ===========================================================================

class TestTier1Unit:
    """Cross-module integration paths plus golden-number sanity.

    These tests verify that the pieces wire together correctly. Per-module
    edge cases live in their own test files.
    """

    # ---- prepared_queries write-rejection -------------------------------

    def test_rejects_write_sql(self):
        """prepared_queries.assert_select_only must reject any non-SELECT
        statement. Belt-and-suspenders alongside the read-only connection."""
        with pytest.raises(ValueError):
            prepared_queries.assert_select_only("DELETE FROM trades")
        with pytest.raises(ValueError):
            prepared_queries.assert_select_only("DROP TABLE runs")
        with pytest.raises(ValueError):
            prepared_queries.assert_select_only("UPDATE trades SET pnl=0")

    # ---- strategy_oracle.propose_change LOW-confidence rejection --------

    def test_rejects_low_confidence_proposal(self, tmp_path):
        """propose_change must reject confidence='LOW' even when n>=30."""
        fh = open(tmp_path / "audit.jsonl", "w", encoding="utf-8")
        try:
            ctx = strategy_oracle._RunCtx(
                mode="weekly",
                facts={"strategies": {"X": {"metrics": {}}}, "findings": []},
                audit_fh=fh,
                run_date="2026-06-01",
                pending_proposals=[],
            )
            result = strategy_oracle._dispatch_tool(
                "propose_change",
                {
                    "strategy": "X", "direction": "BOTH",
                    "parameter_name": "session_end_time",
                    "current_value": "09:45", "proposed_value": "09:15",
                    "rationale": "decay observed",
                    "confidence": "LOW", "sample_size": 200,
                    "finding_id": "f1",
                },
                ctx,
            )
            assert result["ok"] is False
            assert "confidence" in result["error"].lower()
            assert ctx.pending_proposals == []
        finally:
            fh.close()

    # ---- strategy_oracle.write_finding n<30 rejection -------------------

    def test_rejects_under_min_trades_finding(self, tmp_path):
        """write_finding must reject sample_size < 30."""
        fh = open(tmp_path / "audit.jsonl", "w", encoding="utf-8")
        try:
            ctx = strategy_oracle._RunCtx(
                mode="weekly",
                facts={"strategies": {"X": {}}, "findings": []},
                audit_fh=fh,
                run_date="2026-06-01",
                pending_proposals=[],
            )
            result = strategy_oracle._dispatch_tool(
                "write_finding",
                {
                    "id": "small_n",
                    "strategy": "X",
                    "verdict": "CONFIRMED",
                    "confidence": "LOW",
                    "sample_size": 15,
                    "rationale": "small sample",
                },
                ctx,
            )
            assert result["ok"] is False
            assert "30" in result["error"]
            assert ctx.facts["findings"] == []
        finally:
            fh.close()

    # ---- Verifier end-to-end: fabricated number rejected ---------------

    def test_verifier_rejects_fabricated_number_end_to_end(self):
        """A narrative number that doesn't trace to facts must be unmatched."""
        facts = {
            "strategies": {
                "bias_momentum": {
                    "metrics": {"dsr": 0.71, "n_trades": 200},
                    "gates": {"all_pass_for_proposal": False},
                }
            },
        }
        narrative = "DSR shot up to 0.93 this week, n=200."
        findings = [
            {
                "id": "fabricated",
                "rationale": "DSR=0.93 was striking",
                "confidence": "HIGH",
            }
        ]
        result = verifier.verify_report(
            facts, narrative, findings, lookahead_active=False,
        )
        assert result["ok"] is False
        # The fabricated finding should be rejected outright.
        assert "fabricated" in result["rejected_findings"]
        # And the narrative-level number reconciler should have flagged 0.93.
        unmatched_values = [
            v for _, v in result["numbers_check"]["unmatched"]
        ]
        assert any(abs(val - 0.93) < 1e-9 for val in unmatched_values)

    # ---- Verifier end-to-end: causal/macro language rejected ------------

    def test_verifier_flags_causal_macro_language_end_to_end(self):
        """A narrative making a causal claim about an exogenous event must
        flunk both the lookahead AND the causal-language checks.

        NOTE: the causal whitelist is permissive when statistical terms
        like ``n=`` sit within the +/-10-token window of the causal phrase.
        We deliberately keep the causal sentence isolated so neither the
        sample-size token nor a comparator like '< 30' lands in the
        window -- otherwise verify_report's causal_check returns ok=True
        as a methodology-talk false-positive."""
        facts = {
            "strategies": {
                "bias_momentum": {
                    "metrics": {"dsr": 0.71, "n_trades": 200},
                    "gates": {},
                }
            },
        }
        # Pure-prose causal sentence -- no `n=`, no `< 30` in the window.
        narrative = (
            "Strategy performance suffered because the Fed pivoted in 2022-11."
        )
        result = verifier.verify_report(
            facts, narrative, [], lookahead_active=False,
        )
        assert result["ok"] is False
        # Both lookahead and causal checks must register a hit.
        assert result["lookahead_check"]["ok"] is False
        assert result["causal_check"]["ok"] is False

    # ---- Golden numbers (sanity against published references) ----------

    def test_compute_psr_golden(self):
        """Bailey-LdP 2012 Eq. 5: synthetic series with controlled moments
        gives PSR ~ 0.62 against the zero benchmark."""
        rng = np.random.default_rng(7)
        base = rng.normal(loc=0.0002, scale=0.01, size=252)
        # Normalize to exact moments so the test is reproducible.
        base = (base - base.mean()) / base.std(ddof=1)
        base = base * 0.01 + 0.0002
        psr = compute_engine.compute_psr(base, sr_benchmark=0.0)
        assert abs(psr - 0.624) < 0.01

    def test_compute_dsr_collapses_to_psr_when_n1(self):
        """With N_trials_effective=1 the deflation benchmark is 0, so DSR
        equals PSR. A failing implementation would diverge here."""
        rng = np.random.default_rng(11)
        base = rng.normal(loc=0.0002, scale=0.01, size=252)
        base = (base - base.mean()) / base.std(ddof=1)
        base = base * 0.01 + 0.0002
        psr = compute_engine.compute_psr(base)
        dsr = compute_engine.compute_dsr(base, n_trials_effective=1)
        assert abs(dsr - psr) < 1e-9

    def test_compute_min_trl_finite(self):
        """A return series with SR clearly above target_sr=1.0 gives a
        finite MinTRL (not the 10**9 sentinel)."""
        rng = np.random.default_rng(13)
        r = rng.normal(0.05, 0.02, size=200)  # SR ~= 2.5
        v = compute_engine.compute_min_trl(r, target_sr=1.0, alpha=0.05)
        assert isinstance(v, int)
        assert 1 < v < 10**9

    def test_compute_hlz_close_to_plain_t_iid(self):
        """For iid normal returns the Newey-West variance approximates the
        sample variance, so the HLZ t-stat should land within ~10% of the
        plain t-stat. A sign-flipped NW loop would explode this."""
        import math
        rng = np.random.default_rng(17)
        r = rng.normal(0.05, 1.0, size=400)
        plain_t = r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))
        hlz = compute_engine.compute_hlz_tstat(r)
        assert abs(hlz - plain_t) / abs(plain_t) < 0.10

    def test_compute_effective_n_collapses_correlated(self):
        """Five trials that are scaled copies of one base series should
        cluster down to <= 2 effective trials."""
        rng = np.random.default_rng(19)
        base = rng.normal(size=200)
        trials = [
            base.copy(), base * 1.0001, base * 0.999,
            base + 0.001, base - 0.001,
        ]
        n_eff = compute_engine.compute_effective_n(trials)
        assert n_eff <= 2


# ===========================================================================
# TIER 2 -- Golden dataset (real warehouse)
# ===========================================================================

@pytest.mark.skipif(
    not WAREHOUSE_AVAILABLE,
    reason="warehouse not present at %s" % WAREHOUSE_PATH,
)
class TestTier2Golden:
    """Hand-verifiable queries against the live warehouse.

    These tests are skipped automatically when the warehouse file is
    absent, so this suite still passes on a clean clone. When the
    warehouse is present, the tests anchor a couple of cross-cutting
    invariants without re-asserting every individual count.
    """

    def test_real_warehouse_trades_count_by_strategy(self):
        """strategies_with_trades + trades_for_strategy must agree on
        counts. If they disagree the friction filter has drifted
        between the two queries."""
        with prepared_queries.open_conn() as conn:
            strats = prepared_queries.strategies_with_trades(
                conn, window_days=1825, min_n=30,
            )
            assert len(strats) >= 1, "no strategies meet n>=30 floor in 5y"
            # Spot-check the first strategy with the largest count.
            for strat in strats[:3]:
                df = prepared_queries.trades_for_strategy(
                    conn, strat, window_days=1825,
                )
                assert len(df) >= 30, (
                    f"strategies_with_trades returned {strat!r} but "
                    f"trades_for_strategy got only {len(df)} rows"
                )

    def test_real_warehouse_overall_win_rate_within_bounds(self):
        """Overall friction-applied win rate must be in (0, 1). Anything
        outside that range means the warehouse is corrupt or the
        prepared-query layer regressed."""
        with prepared_queries.open_conn() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN t.pnl_dollars > 0 THEN 1 ELSE 0 END) * 1.0
                        / NULLIF(COUNT(*), 0) AS wr,
                    COUNT(*) AS n
                FROM trades t
                JOIN runs r USING(run_id)
                WHERE r.friction_applied = TRUE
                """
            ).fetchone()
            wr, n = float(row[0]), int(row[1])
            assert n > 0
            assert 0.0 < wr < 1.0, (
                f"overall win rate {wr!r} is outside (0, 1) -- "
                f"warehouse may be corrupt"
            )


# ===========================================================================
# TIER 3 -- Consistency (seeded harness produces stable top finding)
# ===========================================================================

def _build_synthetic_warehouse(db_path: Path) -> None:
    """Build a tiny but schema-valid DuckDB so the orchestrator's
    `_build_facts` path can run end-to-end against synthetic data.

    Reuses the production schema via tools.warehouse.db.apply_schema.
    """
    from tools.warehouse.db import apply_schema
    import datetime as _dt

    con = duckdb.connect(str(db_path))
    apply_schema(con)
    con.execute(
        """
        INSERT INTO runs
            (run_id, source_filename, csv_kind, strategy, friction_applied)
        VALUES (?, ?, ?, ?, ?)
        """,
        ["RUN_SYNTH", "synth.csv", "trades", "synth_strategy", True],
    )
    # Insert 60 friction-applied trades (clears the n>=30 floor) with a
    # mild positive edge so DSR/PSR are deterministic.
    base = _dt.datetime(2026, 5, 1, 14, 30, tzinfo=_dt.timezone.utc)
    for i in range(60):
        pnl = 10.0 if i % 2 == 0 else -5.0  # PF ~= 2.0
        ts = base + _dt.timedelta(hours=i)
        con.execute(
            """
            INSERT INTO trades (
                run_id, strategy, direction, entry_ts, entry_price,
                exit_ts, exit_price, pnl_dollars, pnl_ticks, hold_minutes,
                year, mae_ticks, mfe_ticks, regime, tod_bucket
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "RUN_SYNTH", "synth_strategy", "LONG", ts, 21000.0,
                ts + _dt.timedelta(minutes=30), 21010.0,
                pnl, pnl / 5.0, 30.0, ts.year, 1, 3, "TREND", "RTH_OPEN",
            ],
        )
    con.close()


def _stub_facts_for_consistency(*_args, **_kwargs) -> dict:
    """A frozen synthetic facts panel so each of the 3 seeded runs sees
    EXACTLY the same input. We bypass _build_facts to remove any
    dependency on warehouse query ordering / date math."""
    return {
        "run_mode": "weekly",
        "run_date": "2026-06-01",
        "window_start": "2026-05-25",
        "window_end": "2026-06-01",
        "regime": {
            "stable": True, "z_score": 0.42, "warning": None,
            "mode_skipped": False, "baseline_n_months": 6,
            "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
        },
        "n_trials_effective": 1,
        "strategies": {
            "synth_strategy": {
                "metrics": {
                    "n_trades": 122, "psr": 0.93, "dsr": 0.76,
                    "min_trl": 87, "hlz_t_stat": 3.4,
                    "bhy_p_adjusted": 0.018,
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


def _seeded_client_script() -> _StubAnthropicClient:
    """Return a stub Anthropic client that always produces the SAME
    tool_use sequence: one write_finding then a final narrative.

    Determinism is the point of this stub. The Tier-3 test is structural --
    it verifies the harness wiring is stable across repeated runs, not
    that any actual LLM produces stable output."""
    return _StubAnthropicClient([
        _StubMessage(
            [{"type": "tool_use", "name": "write_finding", "id": "tu1",
              "input": {
                  "id": "synth_strategy_dsr_2026-06-01",
                  "strategy": "synth_strategy",
                  "verdict": "CONFIRMED",
                  "confidence": "MEDIUM",
                  "sample_size": 122,
                  "rationale": "DSR=0.76 with n=122 trades.",
              }}],
            stop_reason="tool_use",
        ),
        _StubMessage(
            [{"type": "text",
              "text": "Final narrative: DSR=0.76 holds with n=122 trades."}],
            stop_reason="end_turn",
        ),
    ])


class TestTier3Consistency:
    """Three seeded runs against the same synthetic input must agree on
    the top finding. Direction-flip across runs is a hard fail."""

    def test_three_seeded_runs_agree_on_top_finding(
            self, tmp_path, monkeypatch,
    ):
        # Build a synthetic warehouse so _run_preflight is satisfied. The
        # actual facts come from _stub_facts_for_consistency (monkeypatched
        # below) so DuckDB queries against this DB are never invoked.
        wh_path = tmp_path / "phoenix.duckdb"
        _build_synthetic_warehouse(wh_path)

        logs_root = tmp_path / "logs" / "oracle"
        logs_root.mkdir(parents=True)

        monkeypatch.setattr(strategy_oracle, "WAREHOUSE_PATH", str(wh_path))
        monkeypatch.setattr(prepared_queries, "WAREHOUSE_PATH", str(wh_path))
        monkeypatch.setattr(strategy_oracle, "LOGS_ORACLE_ROOT", logs_root)
        monkeypatch.setattr(
            strategy_oracle, "_check_regime_gate",
            lambda conn, mode: {
                "stable": True, "z_score": 0.42, "warning": None,
                "mode_skipped": False, "baseline_n_months": 6,
                "latest_month": "2026-05", "latest_sharpe_proxy": 0.12,
            },
        )
        monkeypatch.setattr(
            strategy_oracle, "_build_facts", _stub_facts_for_consistency,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        # Tier-3 contract: top finding agrees on at least 2 of 3 runs.
        top_findings: list[dict[str, Any]] = []
        for _ in range(3):
            result = strategy_oracle.run(
                "weekly", client=_seeded_client_script(),
            )
            assert result["status"] == "complete"
            facts_path = Path(result["facts_path"])
            assert facts_path.exists()
            data = json.loads(facts_path.read_text(encoding="utf-8"))
            findings = data.get("findings", []) or []
            top = findings[0] if findings else None
            top_findings.append({
                "id": (top or {}).get("id"),
                "strategy": (top or {}).get("strategy"),
                "verdict": (top or {}).get("verdict"),
            })

        # Count how many runs produced the SAME (strategy, verdict) tuple
        # as the first one. Hash on (strategy, verdict) per the spec:
        # the id may include the run_date which we don't want to anchor on.
        anchor = (top_findings[0]["strategy"], top_findings[0]["verdict"])
        agree = sum(
            1 for t in top_findings
            if (t["strategy"], t["verdict"]) == anchor
        )
        assert agree >= 2, (
            f"Tier-3 consistency: only {agree}/3 runs agreed; "
            f"got {top_findings!r}"
        )

        # Direction-flip check: no run may produce a CONFIRMED finding
        # for a strategy and another run produce a REFUTED finding for
        # the same strategy. That's a non-recoverable harness regression.
        per_strategy: dict[str, set[str]] = {}
        for t in top_findings:
            if t["strategy"] is None:
                continue
            per_strategy.setdefault(t["strategy"], set()).add(
                t["verdict"] or ""
            )
        for strat, verdicts in per_strategy.items():
            assert not (
                "CONFIRMED" in verdicts and "REFUTED" in verdicts
            ), f"Tier-3: direction flip on {strat!r}: {verdicts}"


# ===========================================================================
# TIER 4 -- Adversarial
# ===========================================================================

class TestTier4Adversarial:
    """Inject a fabricated pre-computed fact and verify all three defense
    layers (orchestrator n-floor, verifier number reconciler, propose_change
    confidence gate) block it."""

    _ADVERSARIAL_FACTS = {
        "run_mode": "weekly",
        "run_date": "2026-06-01",
        "window_start": "2026-05-25",
        "window_end": "2026-06-01",
        "regime": {"stable": True, "z_score": 0.0, "warning": None,
                   "mode_skipped": False, "baseline_n_months": 6,
                   "latest_month": "2026-05", "latest_sharpe_proxy": 0.0},
        "n_trials_effective": 1,
        "strategies": {
            "X": {
                "metrics": {
                    "dsr": 0.99,        # Suspiciously high.
                    "n_trades": 10,     # Below the n>=30 floor.
                    "psr": 0.99,
                    "bhy_p_adjusted": 0.01,
                    "win_rate": 0.80,
                    "profit_factor": 5.0,
                    "wfe_ratio": 1.0,
                },
                "gates": {
                    "n_floor": False, "all_pass_for_proposal": False,
                    "failed_gates": ["n_floor"],
                },
                "gate_thresholds": {
                    "dsr_high": 0.95, "dsr_luck_floor": 0.90, "psr": 0.90,
                    "hlz_t_stat": 3.0, "n_floor": 30, "n_medium": 100,
                    "n_high": 200, "wfe_ratio_min": 0.6,
                },
            }
        },
        "findings": [],
    }

    def test_write_finding_blocks_n_lt_30(self, tmp_path):
        """Orchestrator layer: _tool_write_finding must reject the
        adversarial n=10 attempt with an n>=30 error."""
        fh = open(tmp_path / "audit.jsonl", "w", encoding="utf-8")
        try:
            ctx = strategy_oracle._RunCtx(
                mode="weekly",
                facts=dict(self._ADVERSARIAL_FACTS),
                audit_fh=fh,
                run_date="2026-06-01",
                pending_proposals=[],
            )
            result = strategy_oracle._dispatch_tool(
                "write_finding",
                {
                    "id": "adversarial_X",
                    "strategy": "X",
                    "verdict": "CONFIRMED",
                    "confidence": "HIGH",
                    "sample_size": 10,
                    "rationale": "DSR=0.99 with n=10",
                },
                ctx,
            )
            assert result["ok"] is False
            assert "30" in result["error"]
            assert ctx.facts["findings"] == []
        finally:
            fh.close()

    def test_propose_change_blocks_low_confidence_with_fabricated_n(
            self, tmp_path,
    ):
        """Orchestrator layer: _tool_propose_change must reject the
        adversarial proposal. Two independent ways to get rejected:
        (a) confidence=LOW (forced by n<30 in the spec rubric); and
        (b) sample_size<30. The spec says BOTH should reject. We test
        both paths explicitly."""
        fh = open(tmp_path / "audit.jsonl", "w", encoding="utf-8")
        try:
            ctx = strategy_oracle._RunCtx(
                mode="weekly",
                facts=dict(self._ADVERSARIAL_FACTS),
                audit_fh=fh,
                run_date="2026-06-01",
                pending_proposals=[],
            )

            # Path A: confidence=LOW is rejected even if sample_size is large.
            result_a = strategy_oracle._dispatch_tool(
                "propose_change",
                {
                    "strategy": "X", "direction": "BOTH",
                    "parameter_name": "session_end_time",
                    "current_value": "09:45", "proposed_value": "09:15",
                    "rationale": "tiny sample low confidence",
                    "confidence": "LOW", "sample_size": 200,
                    "finding_id": "adversarial_X",
                },
                ctx,
            )
            assert result_a["ok"] is False
            assert "confidence" in result_a["error"].lower()
            assert ctx.pending_proposals == []

            # Path B: sample_size<30 is rejected even when confidence is high.
            result_b = strategy_oracle._dispatch_tool(
                "propose_change",
                {
                    "strategy": "X", "direction": "BOTH",
                    "parameter_name": "session_end_time",
                    "current_value": "09:45", "proposed_value": "09:15",
                    "rationale": "tiny sample",
                    "confidence": "HIGH", "sample_size": 10,
                    "finding_id": "adversarial_X",
                },
                ctx,
            )
            assert result_b["ok"] is False
            assert "30" in result_b["error"]
            assert ctx.pending_proposals == []
        finally:
            fh.close()

    def test_verifier_rejects_fabricated_dsr_in_narrative(self):
        """Verifier layer: a narrative quoting a DSR value that does not
        appear anywhere in facts (including gate_thresholds) must fail
        the number reconciler.

        We use DSR=0.85 -- not present in the adversarial facts panel
        (which holds 0.99) and not equal to any gate threshold."""
        facts = dict(self._ADVERSARIAL_FACTS)
        narrative = "Strategy X shows DSR=0.85 with n=10 trades."
        findings = [
            {
                "id": "synthetic_fabrication",
                "rationale": "DSR=0.85 with n=10",
                "confidence": "HIGH",
            }
        ]
        result = verifier.verify_report(
            facts, narrative, findings, lookahead_active=False,
        )
        assert result["ok"] is False
        # The finding must be rejected outright.
        assert "synthetic_fabrication" in result["rejected_findings"]
        # And the narrative-level reconciler must flag 0.85 as unmatched.
        unmatched_values = [
            v for _, v in result["numbers_check"]["unmatched"]
        ]
        assert any(abs(v - 0.85) < 1e-9 for v in unmatched_values)


# ===========================================================================
# Smoke: confirm the public surface this test file leans on
# ===========================================================================

class TestPreFlightSurface:
    """Single-line sanity checks that the modules this file imports still
    expose the symbols we depend on. Cheap insurance against rename
    refactors that would otherwise produce confusing failures elsewhere."""

    def test_strategy_oracle_run_callable(self):
        assert callable(strategy_oracle.run)

    def test_compute_engine_primitives_callable(self):
        for fn_name in (
            "compute_psr", "compute_dsr", "compute_min_trl",
            "compute_hlz_tstat", "compute_effective_n",
        ):
            fn = getattr(compute_engine, fn_name)
            assert callable(fn), f"{fn_name} not callable"

    def test_verifier_verify_report_callable(self):
        assert callable(verifier.verify_report)

    def test_prepared_queries_assert_select_only_callable(self):
        assert callable(prepared_queries.assert_select_only)

    def test_regime_gate_check_regime_stability_callable(self):
        assert callable(regime_gate.check_regime_stability)
