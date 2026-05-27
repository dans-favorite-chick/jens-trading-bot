"""P1-1 Stage 1 — verify enriched market fields persist into trade_memory.

Per `out/reconciliation_inspect_2026-05-24.md` §7, base_bot must stash
the strategy-time enriched market dict so _enter_trade can merge missing
enrichment keys (day_type, cr_verdict, cvd_health, cvd_health_short,
es_nq_rs, intermarket, advisor_guidance) into the persisted market_snapshot.

This test is observability-only — confirms the wiring is present without
spinning up the full bot. The reconciliation harness then consumes these
fields to deterministically replay sim trades.
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
from tests._bot_src_search import bot_combined_source
BASE_BOT_SRC = bot_combined_source()  # P4-1: search all bot modules


def test_last_enriched_market_field_initialized() -> None:
    """BaseBot.__init__ must declare the stash field."""
    assert "self._last_enriched_market: dict | None = None" in BASE_BOT_SRC, (
        "BaseBot.__init__ must declare self._last_enriched_market = None"
    )


def test_evaluate_strategies_stashes_enriched_market() -> None:
    """Right before _pending_signal is set, the enriched market dict must
    be stashed on self._last_enriched_market.

    2026-05-24 P4-1 Stage 3: _evaluate_strategies body moved to
    bots/_strategy_dispatch.py. The stash now writes self.bot.X (not self.X).
    """
    from pathlib import Path
    dispatch_src = (
        Path(__file__).parent.parent / "bots" / "_strategy_dispatch.py"
    ).read_text(encoding="utf-8")
    assert "self.bot._last_enriched_market = dict(market)" in dispatch_src, (
        "_strategy_dispatch.evaluate() must stash the enriched market dict "
        "before queuing the signal (see out/reconciliation_inspect_2026-05-24.md §7)"
    )


def test_enter_trade_merges_enrichment_fields() -> None:
    """_enter_trade must merge strategy-time enrichment fields that are
    missing from the fresh aggregator snapshot."""
    expected_keys = [
        '"day_type"', '"cr_verdict"', '"cvd_health"',
        '"cvd_health_short"', '"es_nq_rs"', '"intermarket"',
    ]
    for key in expected_keys:
        assert key in BASE_BOT_SRC, (
            f"_enter_trade's enrichment-merge block missing key {key} "
            "— see out/reconciliation_inspect_2026-05-24.md §3"
        )
    # Confirm the merge guard preserves fresh values (only-if-not-present).
    # 2026-05-24 P4-1 Stage 4: _enter_trade extracted to bots/_trade_entry.py,
    # which uses `self.bot._last_enriched_market` (the self.X→self.bot.X rewrite).
    assert (
        "if _k in self._last_enriched_market and _k not in market:" in BASE_BOT_SRC
        or "if _k in self.bot._last_enriched_market and _k not in market:" in BASE_BOT_SRC
    ), (
        "_enter_trade enrichment merge must use 'not in market' guard so "
        "that fresh values like price and ATR at execution time are preserved"
    )


def test_merge_does_not_blanket_overwrite_market() -> None:
    """Sanity: the merge block must NOT do `market.update(self._last_enriched_market)`
    or any other blanket overwrite — that would replace fresh price/ATR
    with stale enrichment-time values and corrupt execution math."""
    assert "market.update(self._last_enriched_market)" not in BASE_BOT_SRC, (
        "_enter_trade must NOT blanket-overwrite market with the stashed "
        "enriched dict — that would replace fresh execution-time values "
        "like price and ATR with stale strategy-time values"
    )
