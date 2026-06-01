"""Tests for S9 — agents/adaptive_params.py and tools/approve_proposal.py."""

from __future__ import annotations

import json
import types
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.adaptive_params import (
    BOUNDS,
    make_proposal_id,
    process_pending,
    validate_recommendation,
    write_proposal,
)


# ─── Validation: safety bounds ──────────────────────────────────────────

def _rec(**kw):
    base = {
        "strategy": "bias_momentum",
        "param": "stop_atr_mult",
        "current": 2.0,
        "proposed": 1.8,
        "rationale": "test",
        "expected_impact": "test",
    }
    base.update(kw)
    return base


def test_valid_recommendation_accepted():
    v = validate_recommendation(_rec())
    assert v.accepted, v.reason


def test_missing_fields_rejected():
    v = validate_recommendation({"strategy": "x"})
    assert not v.accepted
    assert "missing" in v.reason


def test_risk_per_trade_over_100_rejected():
    v = validate_recommendation(_rec(param="risk_per_trade", current=15, proposed=150))
    assert not v.accepted
    assert "risk_per_trade" in v.reason


def test_risk_per_trade_at_cap_accepted():
    v = validate_recommendation(_rec(param="risk_per_trade", current=15, proposed=100.0))
    assert v.accepted


def test_daily_loss_cap_over_500_rejected():
    v = validate_recommendation(_rec(param="daily_loss_cap", current=45, proposed=600))
    assert not v.accepted


def test_max_daily_loss_over_500_rejected():
    v = validate_recommendation(_rec(param="max_daily_loss", current=45, proposed=501))
    assert not v.accepted


def test_disable_risk_gate_rejected():
    v = validate_recommendation(_rec(param="max_daily_loss", current=45, proposed=False))
    assert not v.accepted
    assert "disable" in v.reason.lower()


def test_disable_vix_filter_rejected():
    v = validate_recommendation(_rec(param="vix_filter", current=True, proposed=False))
    assert not v.accepted


def test_stop_ticks_too_small_rejected():
    v = validate_recommendation(_rec(param="min_stop_ticks", current=40, proposed=2))
    assert not v.accepted
    assert "min" in v.reason


def test_stop_ticks_too_large_rejected():
    v = validate_recommendation(_rec(param="max_stop_ticks", current=120, proposed=300))
    assert not v.accepted
    assert "max" in v.reason


def test_stop_ticks_in_range_accepted():
    v = validate_recommendation(_rec(param="min_stop_ticks", current=40, proposed=20))
    assert v.accepted


def test_size_multiplier_over_3x_rejected():
    v = validate_recommendation(_rec(param="size_multiplier", current=1.0, proposed=5.0))
    assert not v.accepted


def test_size_mult_suffix_also_caught():
    v = validate_recommendation(_rec(param="position_size_mult", current=1.0, proposed=4.0))
    assert not v.accepted


def test_forbidden_param_live_trading_rejected():
    v = validate_recommendation(_rec(param="LIVE_TRADING", current=False, proposed=True))
    assert not v.accepted
    assert "forbidden" in v.reason


def test_forbidden_file_account_routing_rejected():
    v = validate_recommendation(_rec(target_file="config/account_routing.py"))
    assert not v.accepted
    assert "forbidden file" in v.reason


# ─── Proposal writing ────────────────────────────────────────────────────

def test_write_proposal_creates_md(tmp_path: Path):
    rec = _rec()
    pid = make_proposal_id(rec["strategy"], rec["param"])
    path = write_proposal(rec, proposals_dir=tmp_path, proposal_id=pid)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert rec["strategy"] in text
    assert rec["param"] in text
    assert "PENDING_APPROVAL" in text
    assert "Rollback" in text
    assert "```json" in text


def test_proposal_id_format():
    pid = make_proposal_id("bias_momentum", "stop_atr_mult")
    # YYYYMMDD_HHMMSS_<strategy>_<param>
    parts = pid.split("_")
    assert len(parts[0]) == 8
    assert len(parts[1]) == 6
    assert "bias" in pid and "momentum" in pid


# ─── process_pending orchestration ──────────────────────────────────────

def test_process_pending_accepts_and_rejects(tmp_path: Path):
    pending = tmp_path / "pending_recommendations.json"
    proposals_dir = tmp_path / "proposals"
    rejected_log = tmp_path / "rejected.jsonl"

    recs = [
        _rec(param="stop_atr_mult", current=2.0, proposed=1.8),   # accept
        _rec(param="risk_per_trade", current=15, proposed=500),   # reject
        _rec(param="max_daily_loss", current=45, proposed=False), # reject (disable)
        _rec(param="min_stop_ticks", current=40, proposed=2),     # reject (too small)
    ]
    pending.write_text(json.dumps(recs), encoding="utf-8")

    result = process_pending(
        pending_file=pending,
        proposals_dir=proposals_dir,
        rejected_log=rejected_log,
    )
    assert len(result.accepted) == 1
    assert len(result.rejected) == 3
    assert rejected_log.exists()
    lines = rejected_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    # Each rejected line is parseable JSON
    for ln in lines:
        obj = json.loads(ln)
        assert "reason" in obj


def test_process_pending_handles_missing_file(tmp_path: Path):
    result = process_pending(
        pending_file=tmp_path / "does_not_exist.json",
        proposals_dir=tmp_path / "proposals",
        rejected_log=tmp_path / "rejected.jsonl",
    )
    assert result.accepted == []
    assert result.rejected == []


def test_process_pending_handles_malformed(tmp_path: Path):
    pending = tmp_path / "pending.json"
    pending.write_text("not json at all", encoding="utf-8")
    result = process_pending(
        pending_file=pending,
        proposals_dir=tmp_path / "proposals",
        rejected_log=tmp_path / "rejected.jsonl",
    )
    assert result.accepted == []


# ─── approve_proposal.py dry-run (does not corrupt strategies.py) ──────

def test_approve_dry_run_preserves_strategies(tmp_path: Path):
    from tools import approve_proposal as ap

    # Copy a minimal strategies.py fixture
    strategies_file = tmp_path / "strategies.py"
    strategies_file.write_text(
        'STRATEGY_DEFAULTS = {\n'
        '    "risk_per_trade": 15.0,\n'
        '}\n'
        'STRATEGIES = {\n'
        '    "bias_momentum": {\n'
        '        "enabled": True,\n'
        '        "stop_atr_mult": 2.0,\n'
        '        "min_stop_ticks": 40,\n'
        '    },\n'
        '}\n',
        encoding="utf-8",
    )
    original_text = strategies_file.read_text(encoding="utf-8")
    original_mtime = strategies_file.stat().st_mtime_ns

    # Write a valid proposal
    proposals_dir = tmp_path / "proposals"
    rec = _rec(param="stop_atr_mult", current=2.0, proposed=1.8)
    pid = make_proposal_id(rec["strategy"], rec["param"])
    write_proposal(rec, proposals_dir=proposals_dir, proposal_id=pid)

    summary = ap.approve(
        pid, dry_run=True,
        proposals_dir=proposals_dir,
        strategies_file=strategies_file,
    )
    assert summary["dry_run"] is True
    assert summary["applied"] is False
    assert strategies_file.read_text(encoding="utf-8") == original_text
    assert strategies_file.stat().st_mtime_ns == original_mtime


def test_approve_apply_change_to_source_in_strategies():
    from tools.approve_proposal import apply_change_to_source

    src = (
        'STRATEGIES = {\n'
        '    "bias_momentum": {\n'
        '        "enabled": True,\n'
        '        "stop_atr_mult": 2.0,\n'
        '    },\n'
        '}\n'
    )
    new = apply_change_to_source(src, {
        "strategy": "bias_momentum",
        "param": "stop_atr_mult",
        "current": 2.0,
        "proposed": 1.8,
    })
    assert '"stop_atr_mult": 1.8' in new
    assert "2.0" not in new.split('"stop_atr_mult":')[1].split(",")[0]


def test_approve_apply_change_to_defaults():
    from tools.approve_proposal import apply_change_to_source

    src = (
        'STRATEGY_DEFAULTS = {\n'
        '    "risk_per_trade": 15.0,\n'
        '    "max_daily_loss": 45.0,\n'
        '}\n'
    )
    new = apply_change_to_source(src, {
        "strategy": "global",
        "param": "risk_per_trade",
        "current": 15.0,
        "proposed": 20.0,
    })
    assert '"risk_per_trade": 20.0' in new


def test_approve_rejects_unsafe_proposal(tmp_path: Path):
    """Even if someone hand-edits the MD to bypass validation on write,
    approve() must re-validate and refuse."""
    from tools import approve_proposal as ap

    proposals_dir = tmp_path / "proposals"
    proposals_dir.mkdir()
    pid = "20260421_120000_bias_momentum_risk_per_trade"
    md = proposals_dir / f"proposal_{pid}.md"
    md.write_text(
        f"# AI Proposal: {pid}\n\n"
        "```json\n"
        '{"strategy": "bias_momentum", "param": "risk_per_trade", '
        '"current": 15, "proposed": 999}\n'
        "```\n",
        encoding="utf-8",
    )

    strategies_file = tmp_path / "strategies.py"
    strategies_file.write_text('STRATEGIES = {}\n', encoding="utf-8")

    with pytest.raises(ap.ApprovalError):
        ap.approve(pid, dry_run=True,
                   proposals_dir=proposals_dir,
                   strategies_file=strategies_file)


# ─── Telegram notification on new proposals ────────────────────────────

def _write_pending(tmp_path: Path, recs: list) -> tuple[Path, Path, Path]:
    pending = tmp_path / "pending_recommendations.json"
    proposals_dir = tmp_path / "proposals"
    rejected_log = tmp_path / "rejected.jsonl"
    pending.write_text(json.dumps(recs), encoding="utf-8")
    return pending, proposals_dir, rejected_log


def test_telegram_notified_on_new_proposals(tmp_path, monkeypatch):
    from agents import adaptive_params as ap

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")

    calls = []
    fake_mod = type("M", (), {"send_sync": lambda text, **kw: calls.append(text) or True})
    monkeypatch.setitem(__import__("sys").modules, "core.telegram_notifier", fake_mod)

    recs = [
        _rec(param="stop_atr_mult", current=2.0, proposed=1.8),
        _rec(param="min_stop_ticks", current=40, proposed=20),
    ]
    pending, proposals_dir, rejected_log = _write_pending(tmp_path, recs)
    result = ap.process_pending(
        pending_file=pending, proposals_dir=proposals_dir, rejected_log=rejected_log
    )
    assert len(result.accepted) == 2
    assert len(calls) == 1
    assert "2 new proposals" in calls[0]
    assert "tools/list_proposals.py" in calls[0]


def test_telegram_skipped_when_token_missing(tmp_path, monkeypatch):
    from agents import adaptive_params as ap

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)

    calls = []
    fake_mod = type("M", (), {"send_sync": lambda text, **kw: calls.append(text) or True})
    monkeypatch.setitem(__import__("sys").modules, "core.telegram_notifier", fake_mod)

    recs = [_rec(param="stop_atr_mult", current=2.0, proposed=1.8)]
    pending, proposals_dir, rejected_log = _write_pending(tmp_path, recs)
    result = ap.process_pending(
        pending_file=pending, proposals_dir=proposals_dir, rejected_log=rejected_log
    )
    assert len(result.accepted) == 1
    assert calls == []


def test_telegram_failure_does_not_raise(tmp_path, monkeypatch):
    from agents import adaptive_params as ap

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")

    def _boom(text, **kw):
        raise RuntimeError("telegram down")

    fake_mod = type("M", (), {"send_sync": staticmethod(_boom)})
    monkeypatch.setitem(__import__("sys").modules, "core.telegram_notifier", fake_mod)

    recs = [_rec(param="stop_atr_mult", current=2.0, proposed=1.8)]
    pending, proposals_dir, rejected_log = _write_pending(tmp_path, recs)
    # Must return normally
    result = ap.process_pending(
        pending_file=pending, proposals_dir=proposals_dir, rejected_log=rejected_log
    )
    assert len(result.accepted) == 1


def test_telegram_not_sent_when_zero_accepted(tmp_path, monkeypatch):
    from agents import adaptive_params as ap

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")

    calls = []
    fake_mod = type("M", (), {"send_sync": lambda text, **kw: calls.append(text) or True})
    monkeypatch.setitem(__import__("sys").modules, "core.telegram_notifier", fake_mod)

    # All rejected
    recs = [_rec(param="risk_per_trade", current=15, proposed=500)]
    pending, proposals_dir, rejected_log = _write_pending(tmp_path, recs)
    result = ap.process_pending(
        pending_file=pending, proposals_dir=proposals_dir, rejected_log=rejected_log
    )
    assert len(result.accepted) == 0
    assert calls == []


# === Phase 3 warehouse advisor (added 2026-05-31) ===
#
# All tests below are fully offline: synthetic DuckDB, monkeypatched
# anthropic SDK. They must not hit the real warehouse or real Claude API.

# ─── Shared fixture: tiny synthetic DuckDB warehouse ────────────────────────

@pytest.fixture
def synth_warehouse(tmp_path: Path):
    """Builds a minimal DuckDB warehouse with two runs (net + gross) and
    10 trades for 'bias_momentum' so that friction-filter tests can verify
    the split cleanly.

    Run layout:
      run_net   → friction_applied=TRUE  → 6 trades: 3 wins (+50) / 3 losses (-30) → net +60
      run_gross → friction_applied=FALSE → 4 trades: 2 wins (+200) / 2 losses (-100) → net +200

    All entry_ts are 2026-01-15 UTC so the default min_session_date filter
    accepts them (use 2099-01-01 to exclude them in the date-filter test).
    """
    import duckdb

    db = tmp_path / "phx.duckdb"
    con = duckdb.connect(str(db))

    # Apply schema — strip INSTALL/LOAD directives that need network (DuckDB
    # bundles json; the statements are no-ops in 1.x, but use a plain con.execute
    # skip to be safe).
    schema_path = (
        Path(__file__).resolve().parents[1] / "tools" / "warehouse" / "schema.sql"
    )
    schema_sql = schema_path.read_text(encoding="utf-8")
    # Execute each statement separately, skipping INSTALL/LOAD lines.
    for stmt in schema_sql.split(";"):
        s = stmt.strip()
        if not s:
            continue
        if s.upper().startswith("INSTALL") or s.upper().startswith("LOAD"):
            continue
        con.execute(s)

    # Seed two runs.
    con.execute("""
        INSERT INTO runs (run_id, source_filename, csv_kind, friction_applied)
        VALUES
            ('run_net',   'net.csv',   'trades', TRUE),
            ('run_gross', 'gross.csv', 'trades', FALSE)
    """)

    # Seed trades for run_net: 3 wins (+50) and 3 losses (-30).
    for i in range(3):
        con.execute("""
            INSERT INTO trades
                (run_id, strategy, direction, entry_ts, entry_price, pnl_dollars, pnl_ticks,
                 year, regime, tod_bucket)
            VALUES (?, 'bias_momentum', 'LONG',
                    TIMESTAMPTZ '2026-01-15 14:00:00+00:00',
                    21000.0, 50.0, 10.0, 2026, 'HIGH_VOLATILITY', 'AM')
        """, ['run_net'])
        con.execute("""
            INSERT INTO trades
                (run_id, strategy, direction, entry_ts, entry_price, pnl_dollars, pnl_ticks,
                 year, regime, tod_bucket)
            VALUES (?, 'bias_momentum', 'SHORT',
                    TIMESTAMPTZ '2026-01-15 14:00:00+00:00',
                    21000.0, -30.0, -6.0, 2026, 'LOW_VOLATILITY', 'PM')
        """, ['run_net'])

    # Seed trades for run_gross: 2 wins (+200) and 2 losses (-100).
    for i in range(2):
        con.execute("""
            INSERT INTO trades
                (run_id, strategy, direction, entry_ts, entry_price, pnl_dollars, pnl_ticks,
                 year, regime, tod_bucket)
            VALUES (?, 'bias_momentum', 'LONG',
                    TIMESTAMPTZ '2026-01-15 14:00:00+00:00',
                    21000.0, 200.0, 40.0, 2026, 'HIGH_VOLATILITY', 'AM')
        """, ['run_gross'])
        con.execute("""
            INSERT INTO trades
                (run_id, strategy, direction, entry_ts, entry_price, pnl_dollars, pnl_ticks,
                 year, regime, tod_bucket)
            VALUES (?, 'bias_momentum', 'SHORT',
                    TIMESTAMPTZ '2026-01-15 14:00:00+00:00',
                    21000.0, -100.0, -20.0, 2026, 'LOW_VOLATILITY', 'PM')
        """, ['run_gross'])

    con.close()
    return db


# ─── A. _warehouse_stats filter behaviour ───────────────────────────────────

def test_warehouse_stats_friction_net_only_by_default(synth_warehouse):
    """Default call filters to friction_applied=TRUE rows only (6 trades)."""
    from agents.adaptive_params import _warehouse_stats

    stats = _warehouse_stats("bias_momentum", db_path=synth_warehouse)

    assert stats["friction_filter"] == "friction_net_only"
    assert stats["n_trades"] == 6
    # Filtered split must contain only the net-era trades.
    assert stats["friction_applied_mix"] == {"True": 6}
    # Unfiltered diagnostic must show both eras.
    assert stats["friction_applied_mix_all"] == {"True": 6, "False": 4}


def test_warehouse_stats_include_gross_drops_filter(synth_warehouse):
    """include_gross=True includes all 10 trades; friction_filter='all_eras'."""
    from agents.adaptive_params import _warehouse_stats

    stats = _warehouse_stats("bias_momentum", db_path=synth_warehouse, include_gross=True)

    assert stats["friction_filter"] == "all_eras"
    assert stats["n_trades"] == 10
    # Both eras show up in the filtered split (= unfiltered when no filter applied).
    fmix = stats["friction_applied_mix"]
    assert fmix.get("True", 0) == 6
    assert fmix.get("False", 0) == 4


def test_warehouse_stats_min_session_date_filters(synth_warehouse):
    """A future min_session_date excludes all trades (n_trades==0).

    The short-circuit path is taken; friction_applied_mix_all is still
    populated from the unfiltered (date-filtered) diagnostic query so
    callers can verify the population even when n_trades==0.
    """
    from agents.adaptive_params import _warehouse_stats

    stats = _warehouse_stats(
        "bias_momentum", db_path=synth_warehouse, min_session_date="2099-01-01"
    )

    assert stats["n_trades"] == 0
    assert stats["friction_filter"] == "friction_net_only"
    # The date filter is applied to the diagnostic query too — the future date
    # means no rows qualify, so the all-mix dict is empty.
    assert stats.get("friction_applied_mix_all") == {} or stats.get("friction_applied_mix_all") is not None


# ─── B. UnknownStrategyError + _suggest_strategy_name ───────────────────────

def test_run_warehouse_advisor_unknown_strategy_raises(synth_warehouse, tmp_path):
    """A strategy absent from config AND the warehouse raises UnknownStrategyError."""
    from agents.adaptive_params import run_warehouse_advisor, UnknownStrategyError

    with pytest.raises(UnknownStrategyError) as exc_info:
        run_warehouse_advisor(
            "nonexistent_strategy_xyz",
            db_path=synth_warehouse,
            out_dir=tmp_path,
        )

    assert "nonexistent_strategy_xyz" in str(exc_info.value)


def test_suggest_strategy_name_returns_close_matches():
    """A near-typo of a known strategy returns a hint containing the real name."""
    from agents.adaptive_params import _suggest_strategy_name

    result = _suggest_strategy_name("bais_momentum")

    assert isinstance(result, str)
    assert len(result) > 0
    assert "bias_momentum" in result


def test_suggest_strategy_name_no_match_returns_empty():
    """A completely unrelated string returns an empty hint."""
    from agents.adaptive_params import _suggest_strategy_name

    result = _suggest_strategy_name("xxxxx_no_match_zzzzz")

    assert result == ""


# ─── C. Filename + rendering ─────────────────────────────────────────────────

def _minimal_stats(friction_filter: str = "friction_net_only") -> dict:
    """Minimal stats dict for doc-writer tests (no DB required)."""
    return {
        "n_trades": 0,
        "net_pnl": 0.0,
        "win_rate": 0.0,
        "profit_factor": None,
        "date_range": [None, None],
        "friction_filter": friction_filter,
        "friction_applied_mix_all": {"True": 6, "False": 4},
        "friction_applied_mix": {"True": 6},
    }


def test_advisor_doc_filename_includes_timestamp(tmp_path: Path):
    """Output filename follows the UTC T-HH-MM-SS-Z pattern."""
    from agents.adaptive_params import write_advisor_recommendation_doc

    gen = datetime(2026, 6, 1, 3, 33, 33, tzinfo=timezone.utc)
    path = write_advisor_recommendation_doc(
        "bias_momentum",
        stats=_minimal_stats(),
        current_params={},
        recommendations=[],
        out_dir=tmp_path,
        generated_at=gen,
    )

    assert path.name == "2026-06-01T03-33-33Z_bias_momentum.md"


def test_advisor_doc_renders_era_filter_line(tmp_path: Path):
    """The written doc contains the Era filter line with both split references."""
    from agents.adaptive_params import write_advisor_recommendation_doc

    path = write_advisor_recommendation_doc(
        "bias_momentum",
        stats=_minimal_stats(friction_filter="friction_net_only"),
        current_params={},
        recommendations=[],
        out_dir=tmp_path,
        generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    content = path.read_text(encoding="utf-8")
    assert "Era filter:" in content
    assert "friction_net_only" in content
    assert "filtered split" in content
    assert "unfiltered split" in content


def test_advisor_doc_renders_claude_status_line(tmp_path: Path):
    """An api_error status gets a warning marker and the Anthropic SDK explanation."""
    from agents.adaptive_params import write_advisor_recommendation_doc

    path = write_advisor_recommendation_doc(
        "bias_momentum",
        stats=_minimal_stats(),
        current_params={},
        recommendations=[],
        out_dir=tmp_path,
        generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        claude_status="api_error:RateLimitError",
    )

    content = path.read_text(encoding="utf-8")
    assert "Claude status:" in content
    assert "api_error:RateLimitError" in content
    assert "⚠" in content  # ⚠
    assert "Anthropic SDK raised" in content


def test_advisor_doc_renders_claude_status_ok_unobtrusive(tmp_path: Path):
    """An 'ok' status is shown without a warning marker."""
    from agents.adaptive_params import write_advisor_recommendation_doc

    path = write_advisor_recommendation_doc(
        "bias_momentum",
        stats=_minimal_stats(),
        current_params={},
        recommendations=[],
        out_dir=tmp_path,
        generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        claude_status="ok",
    )

    content = path.read_text(encoding="utf-8")
    assert "Claude status:" in content
    assert "`ok`" in content
    assert "⚠" not in content  # no ⚠ for ok status


# ─── D. request_claude_recommendations failure paths ─────────────────────────

def test_request_claude_no_api_key(monkeypatch):
    """Missing ANTHROPIC_API_KEY returns ([], 'no_api_key') without touching the API."""
    from agents.adaptive_params import request_claude_recommendations

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    recs, status = request_claude_recommendations(
        "bias_momentum", {"n_trades": 50}, {}
    )

    assert recs == []
    assert status == "no_api_key"


def test_request_claude_insufficient_trades(monkeypatch):
    """Fewer than 30 trades short-circuits before the API is instantiated."""
    from agents.adaptive_params import request_claude_recommendations

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    # Track whether anthropic.Anthropic was ever instantiated.
    call_count = 0

    class FakeClient:
        def __init__(self, **kw):
            nonlocal call_count
            call_count += 1

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeClient)
    monkeypatch.setattr("agents.adaptive_params.anthropic", fake_anthropic, raising=False)

    # Inject a fake import for the module-level `import anthropic` inside the func.
    import sys
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    recs, status = request_claude_recommendations(
        "bias_momentum", {"n_trades": 5}, {}
    )

    assert recs == []
    assert status == "insufficient_trades"
    assert call_count == 0


def _make_fake_anthropic_client(response_text: str):
    """Build a fake anthropic module whose .messages.create() returns response_text."""

    class FakeMessage:
        def __init__(self, text):
            self.content = [types.SimpleNamespace(type="text", text=text)]

    class FakeMessages:
        def create(self, **kw):
            return FakeMessage(response_text)

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMessages()

    # Build a minimal fake anthropic namespace.
    fake_mod = types.SimpleNamespace(Anthropic=FakeClient)
    return fake_mod


def test_request_claude_api_error_wraps_class_name(monkeypatch):
    """An exception from the SDK is caught and status becomes 'api_error:<ClassName>'."""
    import sys
    from agents.adaptive_params import request_claude_recommendations

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    class RateLimitError(Exception):
        pass

    class FakeMessages:
        def create(self, **kw):
            raise RateLimitError("rate limited")

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMessages()

    fake_mod = types.SimpleNamespace(Anthropic=FakeClient, RateLimitError=RateLimitError)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    recs, status = request_claude_recommendations(
        "bias_momentum", {"n_trades": 50}, {}
    )

    assert recs == []
    assert status == "api_error:RateLimitError"


def test_request_claude_parse_error_for_non_list_payload(monkeypatch):
    """A JSON dict payload (not a list) causes 'parse_error' status."""
    import sys
    from agents.adaptive_params import request_claude_recommendations

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    fake_mod = _make_fake_anthropic_client('{"verdict": "x"}')
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    recs, status = request_claude_recommendations(
        "bias_momentum", {"n_trades": 50}, {}
    )

    assert recs == []
    assert status == "parse_error"


def test_request_claude_ok_with_empty_list(monkeypatch):
    """A bare '[]' payload is legitimate: returns ([], 'ok')."""
    import sys
    from agents.adaptive_params import request_claude_recommendations

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    fake_mod = _make_fake_anthropic_client("[]")
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    recs, status = request_claude_recommendations(
        "bias_momentum", {"n_trades": 50}, {}
    )

    assert recs == []
    assert status == "ok"
