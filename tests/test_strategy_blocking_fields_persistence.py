"""Regression tests for the strategy-blocking-field persistence wiring.

Background: 2026-05-24 P1-1 Stage 1 + 2026-05-28 commit 856f317
(`fix(reconcile): persist day_type/cr_verdict/cvd_health/es_nq_rs on sim
trades`) closed an observability gap where sim bias_momentum trades
were saved to trade_memory without the four fields that the
reconciliation harness needs to deterministically replay the trade:
``day_type``, ``cr_verdict``, ``cvd_health``, ``es_nq_rs``.

The fix shipped:

- ``bots/sim_bot.py`` lines 605-661 set the 4 fields in ``market``
  before strategy evaluation, mirroring base ``_strategy_dispatch.py``.
- ``bots/sim_bot.py`` line 789 stashes ``self._last_enriched_market = dict(market)``
  for ``_enter_trade`` to merge.
- ``bots/_trade_entry.py`` lines 177-186 merge those keys into the
  freshly-snapshotted market dict before ``positions.open_position``
  persists ``market_snapshot``.

This test file guards the wiring against future regression. Separate
from ``tests/test_enriched_market_persistence.py``, which checks the
*source-code* invariants of the stash/merge/no-overwrite pattern. This
file checks the *semantic* outcome: the harness sees the fields after a
trade flows through the merge.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.bias_momentum import BiasMomentumFollow
from tools.reconcile_sim_vs_backtest import (
    _blocking_field_status,
    STRATEGY_BLOCKING_FIELDS,
)


# Mirror of the merge-key list in bots/_trade_entry.py:178-184.
# Asserted to match the production source via
# `test_merge_keys_replica_matches_production` below — if the
# production loop changes, that test fails loud so this constant
# can't silently drift out of sync.
_MERGE_KEYS = (
    "day_type", "day_type_reason", "cr_verdict", "cr_mom_score",
    "cr_direction", "cr_confidence", "cr_at_resistance",
    "cr_at_support", "cvd_health", "cvd_health_short",
    "es_nq_rs", "intermarket", "advisor_guidance",
    "mq_direction_bias",
)


def _apply_trade_entry_merge(market: dict, stash: dict | None) -> dict:
    """Isolated replica of the merge loop at bots/_trade_entry.py:177-186.

    Tested in-process so the regression test stays fast (no bot spinup,
    no fixtures). Fail-loud: if the production loop diverges from this
    replica, the regression tests below will assert the production
    behavior and the operator will need to update _MERGE_KEYS.
    """
    if stash:
        for k in _MERGE_KEYS:
            if k in stash and k not in market:
                market[k] = stash[k]
    return market


def _make_bar(close=19000.0, open_=18990.0, high=19010.0, low=18980.0, volume=500):
    bar = types.SimpleNamespace()
    bar.close = close
    bar.open = open_
    bar.high = high
    bar.low = low
    bar.volume = volume
    return bar


def _market_with_blocking_fields() -> dict:
    """Synthetic market dict that carries all 4 blocking fields plus
    enough scaffolding for bias_momentum.evaluate() to run without raising.
    Derived from the existing test_bias_momentum_eval_no_nameerror.py
    fixture; the 4 blocking fields are the ones under test here."""
    return {
        "close": 19000.0,
        "price": 19000.0,
        "vwap": 18990.0,
        "ema9": 18995.0,
        "ema21": 18985.0,
        "ema9_15m": 18993.0,
        "ema21_15m": 18983.0,
        "atr_1m": 4.0,
        "atr_5m": 8.0,
        "cvd": 500_000,
        "bar_delta": 120,
        "tf_bias": {"1m": "BULLISH", "5m": "BULLISH", "60m": "BULLISH"},
        "tf_votes_bullish": 3,
        "tf_votes_bearish": 1,
        # The 4 strategy-blocking fields under test:
        "day_type": "TREND",
        "cr_verdict": "CONTINUATION",
        "cvd_health": {"veto": False, "agreement": 0.85, "reason": "aligned"},
        "es_nq_rs": 0.42,
        # Rest of the eval scaffolding:
        "mq_direction_bias": "NEUTRAL",
        "avg_vol_5m": 400.0,
        "vol_climax_ratio": 1.1,
        "vsa_signal_5m": "NEUTRAL",
        "delta_history_5m": [],
        "high_history_5m": [],
        "low_history_5m": [],
        "macd_histogram": 0.05,
        "macd_histogram_prev": 0.04,
        "macd_warm": True,
        "dom_imbalance": 0.55,
        "dom_signal": {},
        "vwap_std": 5.0,
        "vwap_upper1": 18995.0,
        "vwap_upper2": 19000.0,
        "vwap_lower1": 18985.0,
        "vwap_lower2": 18980.0,
        "avwap_pd_close": 18970.0,
        "mq_nearest_resistance": 0.0,
        "mq_nearest_support": 0.0,
        "mq_hvl": 0.0,
    }


# ════════════════════════════════════════════════════════════════════
# Case 1 — evaluate() must not drop the 4 fields from the market dict
# ════════════════════════════════════════════════════════════════════

def test_evaluate_preserves_blocking_fields_in_market() -> None:
    """bias_momentum.evaluate() must not mutate the market dict to drop
    day_type, cr_verdict, cvd_health, or es_nq_rs. The dispatch layer
    counts on these surviving so the post-signal stash captures them.

    Uses a typed catch on (KeyError, AttributeError, TypeError) — the
    expected fixture-shortcoming failure modes. If evaluate() throws an
    unexpected exception type, the test fails loud rather than passing
    vacuously (R5.3 audit fix 2026-06-04)."""
    strategy = BiasMomentumFollow(config={})
    market = _market_with_blocking_fields()
    bars_5m = [_make_bar() for _ in range(5)]
    bars_1m = [_make_bar() for _ in range(10)]
    session = {"regime": "MID_MORNING"}

    try:
        strategy.evaluate(market, bars_5m, bars_1m, session)
    except (KeyError, AttributeError, TypeError):
        # Expected — the synthetic fixture is intentionally minimal and
        # evaluate() may bail out at a gate that reads a market field we
        # didn't populate. What matters is that any partial work didn't
        # remove the 4 blocking fields from the market dict.
        pass

    for f in STRATEGY_BLOCKING_FIELDS:
        assert f in market, (
            f"bias_momentum.evaluate() removed '{f}' from market — "
            f"dispatch's _last_enriched_market stash would lose it"
        )
        assert market[f] not in (None, ""), (
            f"bias_momentum.evaluate() set '{f}' to None/'' — would "
            f"trip the reconcile harness's BLOCKED check at "
            f"tools/reconcile_sim_vs_backtest.py:_blocking_field_status"
        )


# ════════════════════════════════════════════════════════════════════
# Drift sentinel — assert _MERGE_KEYS matches the production tuple
# ════════════════════════════════════════════════════════════════════

def test_merge_keys_replica_matches_production_source() -> None:
    """Address R5.2 / R5.3 MEDIUM finding: ``_MERGE_KEYS`` is a hand-
    rolled mirror of the production tuple inside ``bots/_trade_entry.py``
    enter_trade(). If production gains/loses a key, the replica goes
    stale silently and tests 2/3 keep passing while the bug is live.

    This test parses the production source and asserts that every key
    in ``_MERGE_KEYS`` is present in the production loop's iterable AND
    every key in the production loop is present in ``_MERGE_KEYS``.
    Text-based on purpose — importing the merge loop is non-trivial
    without spinning up the full TradeEntry stack."""
    import re

    src_path = ROOT / "bots" / "_trade_entry.py"
    src = src_path.read_text(encoding="utf-8")

    # Find the merge-key iteration. Looks like:
    #   for _k in (
    #       "day_type", "day_type_reason", ...,
    #   ):
    #       if _k in self.bot._last_enriched_market and _k not in market:
    pattern = re.compile(
        r"for\s+_k\s+in\s+\((.*?)\):\s*\n\s*if\s+_k\s+in\s+self\.bot\._last_enriched_market",
        re.DOTALL,
    )
    m = pattern.search(src)
    assert m is not None, (
        "Could not locate the production merge loop in bots/_trade_entry.py. "
        "Either the loop was refactored (update this test's regex) or the "
        "merge was removed entirely (would break Path X persistence)."
    )

    raw_keys = re.findall(r'"([a-z_][a-z0-9_]*)"', m.group(1))
    production_keys = tuple(raw_keys)

    assert production_keys == _MERGE_KEYS, (
        f"_MERGE_KEYS drifted from bots/_trade_entry.py merge loop.\n"
        f"  Test replica: {_MERGE_KEYS}\n"
        f"  Production:   {production_keys}\n"
        f"Update _MERGE_KEYS in this file to match, then re-run."
    )


# ════════════════════════════════════════════════════════════════════
# Case 2 — _trade_entry merge populates the 4 fields when fresh dict
# lacks them and stash has them
# ════════════════════════════════════════════════════════════════════

def test_trade_entry_merge_populates_blocking_fields() -> None:
    """The merge loop in bots/_trade_entry.py:177-186 must move all 4
    blocking fields from _last_enriched_market into a fresh
    aggregator.snapshot() that lacks them."""
    fresh_market = {"price": 19000.0, "atr_5m": 8.0}  # post-aggregator snapshot
    stash = {
        "day_type": "TREND",
        "cr_verdict": "CONTINUATION",
        "cvd_health": {"veto": False, "agreement": 0.85},
        "es_nq_rs": 0.42,
        # Plus other merge keys — the loop should bring them all over:
        "intermarket": {"risk_off_score": 30},
        "advisor_guidance": {"sentiment": "BULLISH"},
    }

    merged = _apply_trade_entry_merge(fresh_market, stash)

    for f in STRATEGY_BLOCKING_FIELDS:
        assert f in merged, f"merge dropped {f}"
        assert merged[f] == stash[f], f"merge did not copy {f} faithfully"


# ════════════════════════════════════════════════════════════════════
# Case 2b — fresh values are preserved when both stash and fresh have them
# ════════════════════════════════════════════════════════════════════

def test_trade_entry_merge_preserves_fresh_when_stash_present() -> None:
    """If fresh market already has a field, the merge MUST NOT overwrite
    it with the (stale) stash value. The guard ``_k not in market`` is
    what protects fresh price/ATR from being clobbered by enrichment-time
    values."""
    fresh_market = {
        "price": 19000.0,
        "day_type": "TREND",  # fresh — must survive
        "cr_verdict": "CONTESTED",  # fresh — must survive
    }
    stash = {
        "day_type": "RANGE",  # stale — must NOT win
        "cr_verdict": "CONTINUATION",  # stale — must NOT win
        "cvd_health": {"veto": False},  # fresh missing — stash wins
        "es_nq_rs": 0.42,  # fresh missing — stash wins
    }

    merged = _apply_trade_entry_merge(fresh_market, stash)

    assert merged["day_type"] == "TREND", "fresh day_type was overwritten"
    assert merged["cr_verdict"] == "CONTESTED", "fresh cr_verdict was overwritten"
    assert merged["cvd_health"] == {"veto": False}, "stash cvd_health did not flow through"
    assert merged["es_nq_rs"] == 0.42, "stash es_nq_rs did not flow through"


# ════════════════════════════════════════════════════════════════════
# Case 3 — harness BLOCKED semantics
# ════════════════════════════════════════════════════════════════════

def test_blocking_field_check_matches_harness_semantics() -> None:
    """Sanity: a trade row with all 4 fields populated is not BLOCKED;
    rows with any field missing/None/'' are BLOCKED. This pins the
    contract the persistence pipeline must satisfy."""
    good_row = {
        "strategy": "bias_momentum",
        "market_snapshot": {
            "day_type": "TREND",
            "cr_verdict": "CONTINUATION",
            "cvd_health": {"veto": False},
            "es_nq_rs": 0.42,
        },
    }
    assert _blocking_field_status(good_row, "bias_momentum") == [], (
        "row with all 4 fields should not be BLOCKED"
    )

    # Each individual missing/None/'' value must trip BLOCKED.
    for f in STRATEGY_BLOCKING_FIELDS:
        # Absent key:
        ms = dict(good_row["market_snapshot"]); del ms[f]
        row_absent = {"strategy": "bias_momentum", "market_snapshot": ms}
        assert f in _blocking_field_status(row_absent, "bias_momentum")
        # None value:
        ms = dict(good_row["market_snapshot"]); ms[f] = None
        row_none = {"strategy": "bias_momentum", "market_snapshot": ms}
        assert f in _blocking_field_status(row_none, "bias_momentum")
        # Empty string value:
        ms = dict(good_row["market_snapshot"]); ms[f] = ""
        row_empty = {"strategy": "bias_momentum", "market_snapshot": ms}
        assert f in _blocking_field_status(row_empty, "bias_momentum")


# ════════════════════════════════════════════════════════════════════
# Case 4 — empirical regression check against on-disk trades
# ════════════════════════════════════════════════════════════════════

def test_recent_sim_bias_momentum_trades_carry_blocking_fields() -> None:
    """Empirical: of the 30 most-recent bias_momentum trades on disk,
    >=80% carry all 4 strategy-blocking fields. This is the production-
    evidence regression test — if a future refactor reintroduces the gap
    (deletes the stash, breaks the merge loop, drops fields in
    history_logger), the ratio falls and the test fails.

    Why "last 30 trades" rather than a fixed date window: the wiring
    shipped across two commits (~2026-05-24 P1-1 Stage 1 for the prod
    path, 856f317 on 2026-05-28 for sim_bot). The "last 30 trades" is a
    rolling window that always reflects current-code behavior on
    whatever the operator's data dir looks like at test time.

    Skips cleanly if the data dir has fewer than 10 bias_momentum trades —
    the test doesn't manufacture a failure on a fresh checkout."""
    from core.trade_memory import load_all_trades

    trades = load_all_trades(logs_dir=str(ROOT / "logs"))

    def _entry_epoch(t: dict) -> float:
        et = t.get("entry_time")
        if isinstance(et, (int, float)):
            try:
                return float(et)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    bm = [t for t in trades if t.get("strategy") == "bias_momentum"]
    bm.sort(key=_entry_epoch, reverse=True)
    eligible = bm[:30]

    if len(eligible) < 10:
        pytest.skip(
            f"only {len(eligible)} bias_momentum trades on disk — "
            f"empirical regression check skipped (not enough sample)"
        )

    not_blocked = sum(
        1 for t in eligible
        if not _blocking_field_status(t, "bias_momentum")
    )
    ratio = not_blocked / len(eligible)
    assert ratio >= 0.80, (
        f"Last {len(eligible)} bias_momentum trades: {not_blocked} carry "
        f"all 4 blocking fields ({ratio:.0%}) — below 80% floor. "
        f"Persistence wiring may have regressed; check bots/sim_bot.py "
        f"+ bots/_strategy_dispatch.py + bots/_trade_entry.py merge loop."
    )
