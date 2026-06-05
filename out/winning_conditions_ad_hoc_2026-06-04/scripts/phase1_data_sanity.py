"""
Diagnose why pnl_dollars_net is only 41% populated across bias_momentum
closed non-RECONCILED trades. Find alternate field names and compute
fallback pnl from prices where missing.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(r"C:\Trading Project\phoenix_bot")
sys.path.insert(0, str(REPO))

from core.trade_memory import load_all_trades


def _is_closed_non_reconciled(t: dict) -> bool:
    status = (t.get("status") or t.get("state") or "").upper()
    if t.get("exit_price") is None and t.get("exit_time") is None:
        return False
    if status == "RECONCILED" or t.get("reconciled") is True:
        return False
    if (t.get("source") or "").lower() == "reconciliation":
        return False
    if (t.get("origin") or "").lower() == "reconciliation":
        return False
    return True


def main() -> None:
    trades = load_all_trades()
    bm = [t for t in trades if (t.get("strategy") or "") == "bias_momentum"
          and _is_closed_non_reconciled(t)]
    print(f"bias_momentum closed non-RECONCILED: {len(bm)}")

    # Field coverage
    fields_to_check = [
        "pnl_dollars_net", "pnl_dollars_gross", "pnl_dollars", "gross_pnl",
        "pnl", "pnl_ticks", "result", "entry_price", "exit_price",
        "contracts", "direction", "stop_price", "target_price",
        "r_multiple", "r_distance",
        "commission_dollars", "commission",
        "bot_id", "account",
    ]
    cov = Counter()
    for t in bm:
        for f in fields_to_check:
            if t.get(f) is not None:
                cov[f] += 1
    print("\n=== Field coverage (bias_momentum closed non-RECONCILED) ===")
    for f in fields_to_check:
        print(f"  {f:24s} {cov[f]:4d}/{len(bm)}  ({100*cov[f]/len(bm):5.1f}%)")

    # Bucket by bot_id and account, see if missing pnl correlates
    print("\n=== Bot ID breakdown ===")
    bots = Counter((t.get("bot_id"), t.get("pnl_dollars_net") is not None) for t in bm)
    for (bot, has_pnl_net), n in bots.most_common():
        print(f"  bot={bot!r:30s}  has_pnl_dollars_net={has_pnl_net}  n={n}")

    # Inspect 3 records that LACK pnl_dollars_net but have exit_price
    no_pnl = [t for t in bm if t.get("pnl_dollars_net") is None]
    print(f"\n=== {len(no_pnl)} trades missing pnl_dollars_net — what do they have? ===")
    for i, t in enumerate(no_pnl[:3]):
        print(f"\n--- record {i+1} ---")
        print("  bot_id:", t.get("bot_id"))
        print("  entry_time:", t.get("entry_time"))
        print("  exit_time:", t.get("exit_time"))
        print("  direction:", t.get("direction"))
        print("  entry_price:", t.get("entry_price"))
        print("  exit_price:", t.get("exit_price"))
        print("  stop_price:", t.get("stop_price"))
        print("  contracts:", t.get("contracts"))
        print("  result:", t.get("result"))
        print("  pnl_ticks:", t.get("pnl_ticks"))
        for alt in ("pnl", "pnl_dollars", "gross_pnl", "pnl_dollars_gross",
                    "pnl_dollars_net", "commission", "commission_dollars"):
            print(f"  {alt:20s}: {t.get(alt)}")
        print("  top-level keys:", sorted(t.keys())[:30], "...")

    # See if `result` field values are meaningful (win/loss labels)
    print("\n=== Distribution of 'result' field ===")
    res = Counter(t.get("result") for t in bm)
    for r, n in res.most_common():
        print(f"  {r!r:30s} {n}")


if __name__ == "__main__":
    main()
