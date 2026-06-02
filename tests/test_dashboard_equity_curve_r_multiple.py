"""
Finding D / downstream — dashboard /api/equity-curve must compute r_multiple
against initial_stop_price, NOT the trailed stop_price.

Pins the invariant added by the 2026-06-02 audit (Finding C's dormant
correctness story is activated by Finding D supplying initial_stop_price
on every closed trade).

The endpoint under test lives at `dashboard/server.py` lines 974–1061:

    init_stop = t.get("initial_stop_price") or t.get("stop_price")
    ...
    r_mult = pnl_net / (abs(entry - init_stop) * dollars_per_point * contracts)

If the endpoint ever regressed to using `stop_price` directly, a trade
whose stop had been trailed up would show an inflated r_multiple
(because the post-trail risk is smaller than the entry-time risk).
This test seeds two trades with identical pnl_dollars but different
stop_price vs initial_stop_price and asserts the endpoint returns
the SAME r_multiple for both — the only way that can be true is if
the endpoint is reading initial_stop_price.

Run: pytest tests/test_dashboard_equity_curve_r_multiple.py -v
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


# ─── Finding D / downstream test ─────────────────────────────────────────────

class TestEquityCurveRMultipleUsesInitialStopPrice:
    """The /api/equity-curve endpoint must measure R against the immutable
    entry-time risk (initial_stop_price), never against the live/trailed
    stop_price."""

    def test_r_multiple_uses_initial_stop_not_trailed_stop(self, monkeypatch):
        """Two trades, same P&L, same initial_stop_price, DIFFERENT stop_price.

        Trade A — no trail:  stop_price == initial_stop_price == 20080
        Trade B — trailed:   initial_stop_price == 20080, stop_price == 20095

        Both have entry=20100, pnl_dollars_net=+100, contracts=1, so the
        entry-time risk for both is |20100 - 20080| = 20 points. With
        $2/point on MNQ × 1 contract = $40 risk. r_multiple must be
        +100 / $40 = 2.5 for BOTH trades.

        If the endpoint regressed to using stop_price, Trade B's r_multiple
        would compute against |20100 - 20095| = 5 points = $10 risk →
        r_multiple = 10.0, and the equality assertion below would fail
        loudly.
        """
        # Two synthetic trades — LONG MNQ, identical economics, only
        # the (post-open, post-trail) stop_price differs.
        seeded_trades = [
            {
                "trade_id": "A",
                "bot_id": "test",
                "strategy": "bias_momentum",
                "direction": "LONG",
                "entry_price": 20100.0,
                "initial_stop_price": 20080.0,
                "stop_price": 20080.0,          # no trail
                "exit_price": 20120.0,
                "exit_time": 1717340000.0,
                "exit_reason": "test_exit",
                "contracts": 1,
                "pnl_dollars": 100.0,
                "pnl_dollars_net": 100.0,
            },
            {
                "trade_id": "B",
                "bot_id": "test",
                "strategy": "bias_momentum",
                "direction": "LONG",
                "entry_price": 20100.0,
                "initial_stop_price": 20080.0,  # frozen at open
                "stop_price": 20095.0,          # trailed up post-open
                "exit_price": 20120.0,
                "exit_time": 1717340001.0,
                "exit_reason": "test_exit",
                "contracts": 1,
                "pnl_dollars": 100.0,
                "pnl_dollars_net": 100.0,
            },
        ]

        # The endpoint imports load_all_trades INSIDE the function body
        # (`from core.trade_memory import load_all_trades`), so patching
        # the symbol on its source module is the cleanest interception.
        import core.trade_memory as trade_memory_mod

        def _fake_load_all_trades(logs_dir=None):  # noqa: ARG001
            # Return a fresh copy so the endpoint can't mutate our fixture.
            return [dict(row) for row in seeded_trades]

        monkeypatch.setattr(trade_memory_mod, "load_all_trades", _fake_load_all_trades)

        # Now hit the endpoint via Flask's test client.
        from dashboard.server import app

        client = app.test_client()
        resp = client.get("/api/equity-curve")
        assert resp.status_code == 200, (
            f"/api/equity-curve returned HTTP {resp.status_code}; "
            f"body={resp.data!r}"
        )

        payload = json.loads(resp.data)
        assert "points" in payload, (
            f"response missing 'points' key; got keys={list(payload.keys())}"
        )
        points = payload["points"]
        assert len(points) == 2, (
            f"expected 2 equity-curve points (one per seeded trade); "
            f"got {len(points)}: {points!r}"
        )

        # Map by trade_id so we don't depend on sort order (the endpoint
        # sorts by exit_time, but defensive lookup is clearer to read).
        by_id = {p.get("trade_id"): p for p in points}
        assert "A" in by_id and "B" in by_id, (
            f"expected trade_ids 'A' and 'B' in points; got {list(by_id.keys())}"
        )

        r_a = by_id["A"]["r_multiple"]
        r_b = by_id["B"]["r_multiple"]

        # Sanity: both r_multiples must be present and positive.
        assert r_a is not None, (
            "Trade A r_multiple is None — endpoint failed to compute R for "
            "the un-trailed trade (check entry_price / initial_stop_price / "
            "pnl_dollars_net plumbing)."
        )
        assert r_b is not None, (
            "Trade B r_multiple is None — endpoint failed to compute R for "
            "the trailed trade."
        )
        assert r_a > 0 and r_b > 0, (
            f"both r_multiples should be positive (both trades are +$100 PnL); "
            f"got r_a={r_a}, r_b={r_b}"
        )

        # THE INVARIANT: identical entry-time risk → identical r_multiple,
        # regardless of where the trailed stop ended up.
        assert r_a == pytest.approx(r_b), (
            f"r_multiple differs between Trade A (no trail) and Trade B "
            f"(trailed): r_a={r_a}, r_b={r_b}. The endpoint is computing R "
            f"against the trailed `stop_price` instead of the immutable "
            f"`initial_stop_price` — this regresses Finding C/D from the "
            f"2026-06-02 audit. With identical entry, initial_stop, "
            f"contracts, and pnl, R must be identical."
        )

        # Bonus: pin the exact expected value so a silent broken-everywhere
        # regression (both trades end up with the same WRONG r_multiple)
        # would still trip the test. With pnl=$100, risk=20pts × $2/pt × 1
        # contract = $40, r_multiple = 100/40 = 2.5.
        assert r_a == pytest.approx(2.5), (
            f"expected r_multiple == 2.5 for both trades "
            f"(pnl=$100, risk=20pts × $2/pt × 1 contract = $40); "
            f"got r_a={r_a}. If r_b also drifted by the same factor the "
            f"equality assertion above would have passed but the math would "
            f"be wrong — this assertion guards against that case."
        )
