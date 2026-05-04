"""
Phoenix Dashboard — pre/post-B13 trade accessor helpers.

Pre-B13 trades in trade_memory.json have only `pnl_dollars` (which was
gross-minus-commission, the legacy approximation of net). Post-B13
trades have the full breakdown:
  - pnl_dollars_gross
  - pnl_dollars_net (== pnl_dollars)
  - cost_total_dollars
  - commission_dollars / exchange_fees_dollars / slippage_dollars

If the dashboard sums or averages these fields naively across a mixed
window, it gets sparse None values mixed with real values → wrong
totals → wrong decisions during the validation phase.

These helpers centralize the safe-access pattern. Pre-B13 trades:
  - safe_pnl_net()    → falls back to pnl_dollars
  - safe_pnl_gross()  → falls back to pnl_dollars (gross was effectively
                        what was logged, since cost was unaccounted)
  - safe_cost_total() → 0.0 (no cost data; do NOT return None — sums break)
  - is_post_b13()     → False

Post-B13 trades surface the explicit fields.
"""
from __future__ import annotations


def is_post_b13(trade: dict) -> bool:
    """True if the trade has the B13 cost fields.

    A trade is "post-B13" iff it carries both `cost_total_dollars` AND
    `pnl_dollars_gross` — the two fields added by core/position_manager.py
    in commit 9b82f25.
    """
    return "cost_total_dollars" in trade and "pnl_dollars_gross" in trade


def safe_pnl_net(trade: dict) -> float:
    """Net P&L (post-cost). Pre-B13 falls back to pnl_dollars.

    For pre-B13 trades, `pnl_dollars` is "gross minus legacy commission"
    — close enough to net for legacy reporting purposes. The right way
    to make truly clean comparisons is `--post-b13-only` filtering in
    validation_tracker, not silently shifting the meaning of a field.
    """
    return float(
        trade.get("pnl_dollars_net", trade.get("pnl_dollars", 0.0)) or 0.0
    )


def safe_pnl_gross(trade: dict) -> float:
    """Gross P&L (pre-cost). Pre-B13 falls back to pnl_dollars.

    Pre-B13 trades have no explicit gross field. The recorded
    `pnl_dollars` IS the closest thing to gross, since the post-B13
    cost wedge (slippage + exchange fees) wasn't being subtracted.
    """
    if "pnl_dollars_gross" in trade and trade.get("pnl_dollars_gross") is not None:
        return float(trade["pnl_dollars_gross"])
    return float(trade.get("pnl_dollars", 0.0) or 0.0)


def safe_cost_total(trade: dict) -> float:
    """Cost dollars subtracted from gross. Pre-B13 returns 0 (not None).

    Returning 0 is the correct semantic for "this era did not account
    for costs" — sums do the right thing, averages reflect that pre-B13
    cost was effectively unmeasured.
    """
    return float(trade.get("cost_total_dollars", 0.0) or 0.0)


def split_pre_post(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition trades into (pre_b13, post_b13). Order preserved within each."""
    pre, post = [], []
    for t in trades:
        if is_post_b13(t):
            post.append(t)
        else:
            pre.append(t)
    return pre, post
