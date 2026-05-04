"""
Phoenix B13 — Historical P&L recompute (read-only).

Walks logs/trade_memory.json and computes net P&L for trades that
pre-date the B13 fix. Writes report to
out/historical_pnl_recompute_<today>.md. Does NOT modify any
existing logs.

Usage:
    python tools/backfill_commissions.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import (
    TICK_SIZE,
    COMMISSION_PER_SIDE,
    EXCHANGE_FEES_PER_SIDE,
    SLIPPAGE_TICKS_PER_SIDE,
)

DOLLAR_PER_TICK = TICK_SIZE * 2
CT = ZoneInfo("America/Chicago")


def cost_round_turn(contracts: int) -> float:
    commission = 2 * COMMISSION_PER_SIDE * contracts
    exchange   = 2 * EXCHANGE_FEES_PER_SIDE * contracts
    slippage   = 2 * SLIPPAGE_TICKS_PER_SIDE * DOLLAR_PER_TICK * contracts
    return commission + exchange + slippage


def main():
    trades_file = ROOT / "logs" / "trade_memory.json"
    if not trades_file.exists():
        print(f"No trade_memory at {trades_file}")
        return 1
    raw = trades_file.read_text(encoding="utf-8")
    try:
        trades = json.loads(raw)
    except Exception as e:
        print(f"Could not parse trade_memory: {e}")
        return 2
    if not isinstance(trades, list):
        # Some installations wrap in {"trades": [...]}
        trades = trades.get("trades", []) if isinstance(trades, dict) else []

    by_strat = defaultdict(lambda: {
        "n": 0, "gross": 0.0, "net": 0.0,
        "wins_gross": 0, "wins_net": 0,
    })
    total_n, total_gross, total_net = 0, 0.0, 0.0

    for t in trades:
        if not isinstance(t, dict):
            continue
        contracts = t.get("contracts", 1) or 1
        # Post-B13: gross is explicit. Pre-B13: pnl_dollars was gross
        # (no slippage/exchange-fees yet, only commission was deducted).
        # We re-derive gross by adding back legacy commission too.
        gross = t.get("pnl_dollars_gross")
        if gross is None:
            legacy_pnl = t.get("pnl_dollars", 0.0) or 0.0
            legacy_commission = t.get("commission", 0.0) or 0.0
            gross = legacy_pnl + legacy_commission
        net = gross - cost_round_turn(contracts)
        strat = t.get("strategy", "unknown")
        s = by_strat[strat]
        s["n"]    += 1
        s["gross"] += gross
        s["net"]   += net
        if gross > 0: s["wins_gross"] += 1
        if net   > 0: s["wins_net"]   += 1
        total_n     += 1
        total_gross += gross
        total_net   += net

    today = datetime.now(CT).date()
    out = ROOT / f"out/historical_pnl_recompute_{today}.md"
    out.parent.mkdir(exist_ok=True)
    rt = cost_round_turn(1)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# Phoenix B13 Historical P&L Recompute - {today}\n\n")
        f.write(f"Cost assumption: ${rt:.2f} round-turn / contract\n")
        f.write(
            f"  ({COMMISSION_PER_SIDE:.2f} comm + "
            f"{EXCHANGE_FEES_PER_SIDE:.2f} fees + "
            f"{SLIPPAGE_TICKS_PER_SIDE} ticks slippage, per side, x2)\n\n"
        )
        f.write("## Overall\n\n")
        f.write(f"- Trades:       {total_n}\n")
        f.write(f"- Gross P&L:    ${total_gross:+,.2f}\n")
        f.write(f"- Net P&L:      ${total_net:+,.2f}\n")
        f.write(f"- Cost burden:  ${total_gross - total_net:+,.2f}\n\n")
        f.write("## Per strategy\n\n")
        f.write("| Strategy | N | Gross $ | Net $ | WR gross | WR net |\n")
        f.write("|---|---:|---:|---:|---:|---:|\n")
        for strat, s in sorted(by_strat.items(), key=lambda kv: kv[1]["net"]):
            wr_g = 100 * s["wins_gross"] / s["n"] if s["n"] else 0
            wr_n = 100 * s["wins_net"]   / s["n"] if s["n"] else 0
            f.write(
                f"| `{strat}` | {s['n']} | ${s['gross']:+,.2f} | "
                f"${s['net']:+,.2f} | {wr_g:.0f}% | {wr_n:.0f}% |\n"
            )
    print(f"Wrote {out}")
    print(
        f"Total: gross ${total_gross:+,.2f} -> net ${total_net:+,.2f} "
        f"(cost burden ${total_gross - total_net:+,.2f})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
