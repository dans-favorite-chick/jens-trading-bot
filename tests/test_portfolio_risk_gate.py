"""P1-3 — Portfolio risk gate tests (F-07 + F-20 mitigation).

Covers:
 1. Empty state → ACCEPT (no prior exposure)
 2. Accept under cap → ACCEPT (rolling window has room)
 3. WARN-mode never blocks (passthrough, even when projected > cap)
 4. BLOCK-mode REFUSES over cap
 5. BLOCK-mode REDUCES under correlation
 6. Correlation cache absent → correlation gate is a no-op
 7. Rolling window expires old entries (post-window exposure clears)
 8. record_entry then check_entry feed each other correctly
 9. Gate-internal exception → safe ACCEPT (fail-open in the gate's own
    error path; the bot's outer exception handler still catches)
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.portfolio_risk_gate import PortfolioRiskGate


class _FakeBot:
    bot_name = "test_bot"


@pytest.fixture(autouse=True)
def _clear_block_env(monkeypatch):
    """Default to WARN mode (env unset) for every test."""
    monkeypatch.delenv("PHOENIX_PORTFOLIO_CAP_BLOCK", raising=False)
    yield


@pytest.fixture
def block_mode(monkeypatch):
    """Helper: flip BLOCK mode on."""
    monkeypatch.setenv("PHOENIX_PORTFOLIO_CAP_BLOCK", "1")
    yield


@pytest.fixture
def empty_cache(tmp_path):
    """Gate pointed at a tmp path with no correlation cache → matrix
    is empty so the correlation branch is inert."""
    cache = tmp_path / "no_cache.json"
    return cache


@pytest.fixture
def populated_cache(tmp_path):
    """Cache with one highly-correlated pair: (alpha, beta) jaccard=0.85."""
    cache = tmp_path / "corr.json"
    payload = {
        "generated_at": time.time(),
        "pairs": [
            {"a": "alpha", "b": "beta", "jaccard": 0.85},
            {"a": "alpha", "b": "gamma", "jaccard": 0.30},
        ],
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")
    return cache


# ─────────────────────────── Case 1: empty state ──────────────────────


def test_empty_state_accepts(empty_cache):
    gate = PortfolioRiskGate(
        _FakeBot(),
        directional_cap=5,
        correlation_threshold=0.7,
        cache_path=empty_cache,
    )
    r = gate.check_entry("bias_momentum", "LONG", 1, 21000.0)
    assert r["decision"] == "ACCEPT"
    assert r["contracts"] == 1


# ─────────────────────────── Case 2: under cap ────────────────────────


def test_accept_under_cap(empty_cache):
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=5, cache_path=empty_cache,
    )
    # Two prior LONGs, 1 contract each → exposure=2. New 2-contract
    # LONG → projected=4 < cap=5 → ACCEPT.
    gate.record_entry("bias_momentum", "LONG", 1, 21000.0)
    gate.record_entry("vwap_pullback", "LONG", 1, 21001.0)
    r = gate.check_entry("orb", "LONG", 2, 21002.0)
    assert r["decision"] == "ACCEPT"
    assert r["contracts"] == 2


# ─────────────────────────── Case 3: WARN never blocks ────────────────


def test_warn_mode_never_blocks_over_cap(empty_cache):
    """WARN is the default (no env). Even with projected > cap, return
    ACCEPT with the original contracts and a WARN: reason."""
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=2, cache_path=empty_cache,
    )
    gate.record_entry("alpha", "LONG", 2, 21000.0)  # at cap already
    r = gate.check_entry("beta", "LONG", 1, 21001.0)
    assert r["decision"] == "ACCEPT"
    assert r["contracts"] == 1
    assert r["reason"].startswith("WARN:")


# ─────────────────────────── Case 4: BLOCK refuses over cap ───────────


def test_block_mode_refuses_over_cap(empty_cache, block_mode):
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=3, cache_path=empty_cache,
    )
    gate.record_entry("alpha", "LONG", 2, 21000.0)
    gate.record_entry("beta", "LONG", 1, 21001.0)  # exposure=3
    r = gate.check_entry("gamma", "LONG", 1, 21002.0)
    assert r["decision"] == "REFUSE"
    assert r["contracts"] == 0
    assert "directional cap LONG" in r["reason"]


def test_block_mode_opposite_direction_does_not_count(
    empty_cache, block_mode,
):
    """SHORT exposure doesn't block new LONG entries (and vice versa)."""
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=2, cache_path=empty_cache,
    )
    gate.record_entry("alpha", "SHORT", 2, 21000.0)
    # New LONG → LONG exposure is 0, not 2 → ACCEPT
    r = gate.check_entry("beta", "LONG", 2, 21002.0)
    assert r["decision"] == "ACCEPT"


# ─────────────────────────── Case 5: BLOCK reduces under corr ─────────


def test_block_mode_reduces_under_correlation(populated_cache, block_mode):
    """alpha & beta have Jaccard 0.85 > 0.7 threshold. With beta already
    open in LONG direction, a new LONG from alpha should REDUCE."""
    gate = PortfolioRiskGate(
        _FakeBot(),
        directional_cap=10,  # well above; correlation branch is the gate
        correlation_threshold=0.7,
        cache_path=populated_cache,
    )
    gate.record_entry("beta", "LONG", 1, 21000.0)
    r = gate.check_entry("alpha", "LONG", 4, 21001.0)
    assert r["decision"] == "REDUCE"
    assert r["contracts"] == 2  # 4 // 2
    assert "correlation LONG" in r["reason"]


def test_block_mode_uncorrelated_pair_does_not_reduce(
    populated_cache, block_mode,
):
    """alpha & gamma have Jaccard 0.30 < 0.7 → no reduction."""
    gate = PortfolioRiskGate(
        _FakeBot(),
        directional_cap=10,
        correlation_threshold=0.7,
        cache_path=populated_cache,
    )
    gate.record_entry("gamma", "LONG", 1, 21000.0)
    r = gate.check_entry("alpha", "LONG", 4, 21001.0)
    assert r["decision"] == "ACCEPT"
    assert r["contracts"] == 4


# ─────────────────────────── Case 6: missing cache = no-op ────────────


def test_missing_correlation_cache_makes_corr_gate_inert(tmp_path, block_mode):
    """A missing cache file should not block trades — directional cap
    still runs, correlation check is a no-op."""
    nonexistent = tmp_path / "does_not_exist.json"
    assert not nonexistent.exists()
    gate = PortfolioRiskGate(
        _FakeBot(),
        directional_cap=10,
        cache_path=nonexistent,
    )
    gate.record_entry("any_strat", "LONG", 1, 21000.0)
    r = gate.check_entry("other_strat", "LONG", 2, 21001.0)
    assert r["decision"] == "ACCEPT"


# ─────────────────────────── Case 7: window expires ───────────────────


def test_rolling_window_expires_old_entries(empty_cache, block_mode):
    """Entries older than rolling_window_s drop out of the cap math."""
    gate = PortfolioRiskGate(
        _FakeBot(),
        directional_cap=2,
        rolling_window_s=10,
        cache_path=empty_cache,
    )
    # Record exposure 5 minutes ago — should fall outside 10s window.
    old_ts = time.time() - 300.0
    gate.record_entry("old_strat", "LONG", 5, 21000.0, timestamp=old_ts)
    r = gate.check_entry("new_strat", "LONG", 2, 21001.0)
    assert r["decision"] == "ACCEPT"  # old entries pruned, fresh exposure=0


# ─────────────────────────── Case 8: record + check ─────────────────


def test_record_then_check_round_trip(empty_cache, block_mode):
    """A successful flow: gate accepts, bot records, next entry sees
    the new exposure."""
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=3, cache_path=empty_cache,
    )
    # First entry: cap=3, projecting 2 → ACCEPT
    r1 = gate.check_entry("alpha", "LONG", 2, 21000.0)
    assert r1["decision"] == "ACCEPT"
    gate.record_entry("alpha", "LONG", 2, 21000.0)
    # Second entry: cap=3, current=2 + new=2 = 4 > 3 → REFUSE
    r2 = gate.check_entry("beta", "LONG", 2, 21001.0)
    assert r2["decision"] == "REFUSE"
    # Third entry, single contract: 2 + 1 = 3 not > 3 → ACCEPT
    r3 = gate.check_entry("beta", "LONG", 1, 21002.0)
    assert r3["decision"] == "ACCEPT"


# ─────────────────────────── Case 9: fail-open on internal error ──────


def test_gate_internal_exception_returns_accept(empty_cache, block_mode):
    """If something inside the gate throws, return ACCEPT so the entry
    path is never crashed by an observability layer."""
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=5, cache_path=empty_cache,
    )

    # Force the impl to raise.
    def _boom(*a, **kw):
        raise RuntimeError("induced failure")

    gate._check_entry_impl = _boom  # type: ignore[assignment]
    r = gate.check_entry("alpha", "LONG", 2, 21000.0)
    assert r["decision"] == "ACCEPT"
    assert r["contracts"] == 2
    assert "gate-error-passthrough" in r["reason"]


# ─────────────────────────── Snapshot helper ─────────────────────────


def test_record_exit_frees_window_capacity(empty_cache, block_mode):
    """record_exit(trade_id) drops the matching entry so subsequent
    check_entry calls see the freed exposure."""
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=3, cache_path=empty_cache,
    )
    gate.record_entry("alpha", "LONG", 2, 21000.0, trade_id="T1")
    gate.record_entry("beta", "LONG", 1, 21001.0, trade_id="T2")  # exposure=3
    # New 1-contract LONG → projected=4 > 3 → REFUSE
    r1 = gate.check_entry("gamma", "LONG", 1, 21002.0)
    assert r1["decision"] == "REFUSE"
    # T1 closes — frees 2 contracts
    gate.record_exit("T1")
    r2 = gate.check_entry("gamma", "LONG", 2, 21003.0)
    assert r2["decision"] == "ACCEPT"
    assert r2["contracts"] == 2


def test_record_exit_unknown_trade_id_is_noop(empty_cache):
    gate = PortfolioRiskGate(
        _FakeBot(), directional_cap=5, cache_path=empty_cache,
    )
    gate.record_entry("alpha", "LONG", 1, 21000.0, trade_id="T1")
    # Unknown id — should not raise, should not affect existing entries.
    gate.record_exit("DOES_NOT_EXIST")
    gate.record_exit("")  # empty id no-op
    gate.record_exit(None)  # type: ignore[arg-type]
    assert len(gate._recent) == 1


def test_snapshot_reports_mode_and_exposure(empty_cache, block_mode):
    gate = PortfolioRiskGate(
        _FakeBot(),
        directional_cap=5,
        correlation_threshold=0.7,
        cache_path=empty_cache,
    )
    gate.record_entry("alpha", "LONG", 2, 21000.0)
    gate.record_entry("beta", "SHORT", 1, 21000.0)
    snap = gate.snapshot()
    assert snap["mode"] == "BLOCK"
    assert snap["directional_cap"] == 5
    assert snap["long_exposure"] == 2
    assert snap["short_exposure"] == 1
    assert snap["recent_entries"] == 2
