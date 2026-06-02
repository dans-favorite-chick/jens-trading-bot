"""Phoenix market-state shadow gate (Phase 7, 2026-06-02 overnight).

OBSERVATIONAL ONLY. This module computes "would the bot block this
signal if the binary gate were on?" without applying it. The bot
still fires every signal that passes its other gates. The shadow
decision is stored alongside the trade so the operator can review,
over the trailing N sessions:

    - n_signals fired this session
    - n_signals the shadow gate "would have blocked"
    - PnL of those would-blocked signals (winners missed / losers avoided)

The hypothesis being measured: blocking signals in WHIPSAW_HIGH_VOL
reduces drawdown more than it costs in missed wins. Codex's
warehouse-JOIN preview suggested this is FALSE for bias_momentum
(WHIPSAW_HIGH_VOL PF=1.70, n=198). Shadow-gate logging will confirm
or refute in live data over the next 1-2 weeks.

The helper is data-only. It does NOT read or modify any global
state. Calling it is safe at any point in the signal-emission path.

Phase 7 ships:
    1. compute_market_state_gate_decision() helper + tests
    2. Module docstring (this file) + design note in Phase I roadmap

Phase 7 does NOT ship:
    - Wiring into bots/base_bot.py signal emission. That edit needs
      operator review since base_bot is a high-blast-radius file
      touched right before sim starts. Documented as Monday Phase J
      follow-up in logs/oracle/research/2026-06-02_overnight_completion.md.
    - Dashboard "shadow gate summary" view. Also queued for Monday.

Per-strategy customization (future): a strategy's config block can
override the conservative default by adding a `shadow_allowed_market_states`
list. When present, would_block=True iff current_state NOT in the
list. When absent, the global default applies (block WHIPSAW only).
"""
from __future__ import annotations

from typing import Any

# The six composite labels emitted by core.market_state. Mirrored
# here to avoid a circular import; if the labels in market_state.py
# ever change, update tests/test_shadow_gate.py too.
VALID_MARKET_STATES: frozenset[str] = frozenset({
    "WHIPSAW_HIGH_VOL", "CHOPPY", "COMPRESSED",
    "TRENDING_HIGH_VOL", "TRENDING_NORMAL", "NEUTRAL",
})

# Conservative default block-list. Codex's WAREHOUSE JOIN suggested
# this is wrong for bias_momentum, but we measure before we block --
# so the default applies to every strategy until per-strategy data
# accumulates enough to override.
_DEFAULT_BLOCK_STATES: frozenset[str] = frozenset({"WHIPSAW_HIGH_VOL"})


def compute_market_state_gate_decision(
    strategy_cfg: dict | None,
    current_state: str | None,
) -> dict[str, Any]:
    """Return the shadow-gate decision for a single signal.

    Parameters
    ----------
    strategy_cfg
        The strategy's config dict (typically ``STRATEGIES[name]``).
        May be ``None`` for a quick-eval against the global default.
    current_state
        The current ``MarketState.current()["label"]``. May be ``None``
        (warm-up window, classifier unavailable, etc.). In that case
        the gate fail-OPENS -- ``would_block=False`` with reason
        ``"market_state_unknown"`` -- so a warm-up doesn't silently
        suppress every signal.

    Returns
    -------
    dict with keys:
        ``would_block`` : bool
        ``reason`` : str (always non-empty; describes the decision)

    Resolution order:
        1. If ``current_state`` is ``None`` or not one of
           VALID_MARKET_STATES -> would_block=False, reason="market_state_unknown".
        2. If ``strategy_cfg["shadow_allowed_market_states"]`` is a list:
           would_block iff current_state NOT in that list.
        3. Otherwise use the global default block-list:
           would_block iff current_state in _DEFAULT_BLOCK_STATES.
    """
    if not current_state or current_state not in VALID_MARKET_STATES:
        return {"would_block": False, "reason": "market_state_unknown"}

    if isinstance(strategy_cfg, dict):
        allowed = strategy_cfg.get("shadow_allowed_market_states")
        if isinstance(allowed, (list, tuple, set, frozenset)):
            if current_state in allowed:
                return {
                    "would_block": False,
                    "reason": f"allowed_per_strategy:{current_state}",
                }
            return {
                "would_block": True,
                "reason": f"not_in_shadow_allowed_list:{current_state}",
            }

    # Global default.
    if current_state in _DEFAULT_BLOCK_STATES:
        return {
            "would_block": True,
            "reason": f"default_block:{current_state}",
        }
    return {
        "would_block": False,
        "reason": f"default_allow:{current_state}",
    }


# ─── Optional aggregation helpers for the dashboard view ──────────

def summarize_shadow_decisions(rows: list[dict]) -> dict:
    """Compute n_signals / n_would_block / PnL split from a list of
    trade rows that each carry ``shadow_would_block`` and ``pnl_dollars``.

    Returns:
        {
          "n_signals": int,
          "n_would_block": int,
          "would_block_pct": float (0..1),
          "would_block_pnl_total": float,   # negative = losers avoided
          "would_block_pnl_winners": float, # positive: trades the gate
                                             # would have blocked that
                                             # were winners (missed)
          "would_block_pnl_losers": float,  # positive magnitude of
                                             # blocked losers
        }

    Rows without ``shadow_would_block`` are counted only in n_signals.
    """
    n_signals = 0
    n_would_block = 0
    pnl_total = 0.0
    pnl_winners = 0.0
    pnl_losers = 0.0
    for r in rows or ():
        n_signals += 1
        sb = r.get("shadow_would_block")
        if not sb:
            continue
        n_would_block += 1
        pnl = float(r.get("pnl_dollars") or 0)
        pnl_total += pnl
        if pnl > 0:
            pnl_winners += pnl
        elif pnl < 0:
            pnl_losers += abs(pnl)
    return {
        "n_signals": n_signals,
        "n_would_block": n_would_block,
        "would_block_pct": (n_would_block / n_signals) if n_signals else 0.0,
        "would_block_pnl_total": round(pnl_total, 2),
        "would_block_pnl_winners": round(pnl_winners, 2),
        "would_block_pnl_losers": round(pnl_losers, 2),
    }
