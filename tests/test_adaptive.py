"""Tests for S9 — agents/adaptive_params.py and tools/approve_proposal.py."""

from __future__ import annotations

import json
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
