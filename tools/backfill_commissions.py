"""
Phoenix B13 — Historical P&L recompute (read-only) + baseline quality.

Walks logs/trade_memory.json and computes:
  - Net P&L for trades pre-dating B13
  - Trade count, contract-size distribution
  - Date range, schema-drift detection
  - Outlier flags (catastrophic losses, suspicious gross)
  - Legacy-data flags (unknown strategies, off-platform contracts)

Writes to out/historical_pnl_recompute_<today>.md.
NEVER modifies existing logs.

Usage:
    python tools/backfill_commissions.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent  # for imports only
sys.path.insert(0, str(ROOT))
from config.settings import (
    TICK_SIZE,
    COMMISSION_PER_SIDE,
    EXCHANGE_FEES_PER_SIDE,
    SLIPPAGE_TICKS_PER_SIDE,
)

# Data root: cwd by default (live: project root; tests: tmp_path).
# Falls back to ROOT if cwd has no logs/trade_memory.json but ROOT does —
# keeps "I ran the tool from a random dir" working without surprise.
def _data_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "logs" / "trade_memory.json").exists():
        return cwd
    if (ROOT / "logs" / "trade_memory.json").exists():
        return ROOT
    return cwd  # for empty-tmp tests — will print "no trade_memory" and exit cleanly

DOLLAR_PER_TICK = TICK_SIZE * 2
CT = ZoneInfo("America/Chicago")

# A trade is "suspicious" if its gross loss exceeds this, suggesting
# either multi-contract sizing OR not-MNQ contract.
SUSPICIOUS_LOSS_DOLLARS = 200.0
# A trade is "outlier" if its gross magnitude exceeds this (top 1%).
OUTLIER_DOLLARS = 500.0


def cost_round_turn(contracts: int) -> float:
    commission = 2 * COMMISSION_PER_SIDE * contracts
    exchange   = 2 * EXCHANGE_FEES_PER_SIDE * contracts
    slippage   = 2 * SLIPPAGE_TICKS_PER_SIDE * DOLLAR_PER_TICK * contracts
    return commission + exchange + slippage


def known_strategies():
    """Return set of strategy names currently in config — used for legacy detection."""
    try:
        from config.strategies import STRATEGIES
        return set(STRATEGIES.keys())
    except Exception:
        return set()


def parse_ts(t):
    for k in ("ts", "entry_ts_ct", "entry_time", "exit_time", "recorded_at", "timestamp"):
        v = t.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(v, CT)
            s = str(v).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CT)
            return dt.astimezone(CT)
        except Exception:
            continue
    return None


def main():
    DATA = _data_root()
    trades_file = DATA / "logs" / "trade_memory.json"
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
        trades = trades.get("trades", []) if isinstance(trades, dict) else []

    known = known_strategies()

    # Per-strategy stats
    by_strat = defaultdict(lambda: {
        "n": 0, "gross": 0.0, "net": 0.0,
        "wins_gross": 0, "wins_net": 0,
        "max_gross_loss": 0.0, "max_gross_win": 0.0,
        "contract_sizes": Counter(),
        "suspicious_count": 0,
        "outlier_count": 0,
        "first_ts": None, "last_ts": None,
    })

    total_n = 0
    total_gross = 0.0
    total_net = 0.0
    total_contracts = 0
    schema_keys = Counter()
    contract_size_global = Counter()
    legacy_strategies = Counter()
    no_contracts_field = 0
    parse_failures = 0

    for t in trades:
        if not isinstance(t, dict):
            continue
        for k in t:
            schema_keys[k] += 1

        # Contracts (default 1, flag absent)
        if "contracts" not in t or t.get("contracts") is None:
            contracts = 1
            no_contracts_field += 1
        else:
            try:
                contracts = int(t.get("contracts") or 1)
            except Exception:
                contracts = 1
                parse_failures += 1

        # Detect post-B13 trades (already have explicit gross). Pre-B13:
        # pnl_dollars was gross (no slippage/exchange yet, only commission).
        # We re-derive gross by adding back legacy commission.
        if "pnl_dollars_gross" in t and t.get("pnl_dollars_gross") is not None:
            try:
                gross = float(t["pnl_dollars_gross"])
            except Exception:
                gross = 0.0
                parse_failures += 1
        else:
            try:
                legacy_pnl = float(t.get("pnl_dollars", 0.0) or 0.0)
                legacy_commission = float(t.get("commission", 0.0) or 0.0)
                gross = legacy_pnl + legacy_commission
            except Exception:
                gross = 0.0
                parse_failures += 1

        net = gross - cost_round_turn(contracts)

        strat = t.get("strategy", "unknown")
        if known and strat not in known and strat != "unknown":
            legacy_strategies[strat] += 1

        s = by_strat[strat]
        s["n"] += 1
        s["gross"] += gross
        s["net"] += net
        if gross > 0: s["wins_gross"] += 1
        if net   > 0: s["wins_net"]   += 1
        s["contract_sizes"][contracts] += 1
        if gross < -SUSPICIOUS_LOSS_DOLLARS:
            s["suspicious_count"] += 1
        if abs(gross) > OUTLIER_DOLLARS:
            s["outlier_count"] += 1
        if gross < s["max_gross_loss"]: s["max_gross_loss"] = gross
        if gross > s["max_gross_win"]:  s["max_gross_win"]  = gross

        ts = parse_ts(t)
        if ts is not None:
            if s["first_ts"] is None or ts < s["first_ts"]: s["first_ts"] = ts
            if s["last_ts"]  is None or ts > s["last_ts"]:  s["last_ts"]  = ts

        total_n += 1
        total_gross += gross
        total_net += net
        total_contracts += contracts
        contract_size_global[contracts] += 1

    today = datetime.now(CT).date()
    out = DATA / f"out/historical_pnl_recompute_{today}.md"
    out.parent.mkdir(exist_ok=True)
    rt = cost_round_turn(1)

    L = []
    L.append(f"# Phoenix B13 Historical Recompute + Baseline Quality - {today}")
    L.append("")
    L.append(f"Cost assumption: ${rt:.2f} round-turn per 1 contract")
    L.append(f"  ({COMMISSION_PER_SIDE:.2f} comm + {EXCHANGE_FEES_PER_SIDE:.2f} "
             f"fees + {SLIPPAGE_TICKS_PER_SIDE} ticks slippage, per side, x2)")
    L.append("")

    # Top-level
    L.append("## Overall")
    L.append("")
    L.append(f"- Total trades:        {total_n:,}")
    L.append(f"- Total contracts:     {total_contracts:,}")
    if total_n:
        L.append(f"- Avg contracts/trade: {total_contracts/total_n:.2f}")
    L.append(f"- Gross P&L:           ${total_gross:+,.2f}")
    L.append(f"- Net P&L:             ${total_net:+,.2f}")
    L.append(f"- Cost burden:         ${total_gross - total_net:+,.2f}")
    if total_n:
        L.append(f"- Avg gross / trade:   ${total_gross/total_n:+,.2f}")
        L.append(f"- Avg net   / trade:   ${total_net/total_n:+,.2f}")
    L.append("")

    # Date range (compute first so we can include it before the warnings list)
    all_first = [s["first_ts"] for s in by_strat.values() if s["first_ts"]]
    all_last  = [s["last_ts"]  for s in by_strat.values() if s["last_ts"]]
    span_days = None
    if all_first and all_last:
        first = min(all_first); last = max(all_last)
        span_days = (last - first).days
        L.append(f"- Date range: {first.date()} -> {last.date()} ({span_days} days)")
        L.append("")

    # ──────── BASELINE QUALITY ASSESSMENT ────────
    L.append("## Baseline Quality Assessment")
    L.append("")
    quality_warnings = []

    if no_contracts_field > 0:
        quality_warnings.append(
            f"**{no_contracts_field:,} trades** "
            f"({100*no_contracts_field/total_n:.0f}%) "
            f"missing `contracts` field - defaulted to 1. If real size was higher, "
            f"cost burden is understated.")

    if legacy_strategies and known:
        names = ", ".join(f"`{k}`={v}" for k, v in legacy_strategies.most_common(5))
        quality_warnings.append(
            f"**Legacy/unknown strategies present:** {names}. These trades may "
            f"pre-date the current Phoenix architecture. Consider excluding from "
            f"validation baseline.")

    if parse_failures > 0:
        quality_warnings.append(
            f"**{parse_failures} parse failures** during recompute. Some fields "
            f"could not be coerced to numeric.")

    suspicious_total = sum(s["suspicious_count"] for s in by_strat.values())
    if suspicious_total > 0:
        quality_warnings.append(
            f"**{suspicious_total} trades** with gross loss > "
            f"${SUSPICIOUS_LOSS_DOLLARS:.0f} - suggests multi-contract sizing OR "
            f"a non-MNQ contract. Audit these before trusting historical mean.")

    if total_n:
        avg_gross_mag = abs(total_gross / total_n)
        avg_contracts = total_contracts / total_n
        if avg_gross_mag > 100 and avg_contracts < 2:
            quality_warnings.append(
                f"**Avg |gross|/trade = ${avg_gross_mag:.2f}** with "
                f"avg {avg_contracts:.2f} contracts/trade. For 1-contract "
                f"MNQ this is suspicious - typical 50-tick MNQ trade is ~$25. "
                f"Possible legacy NQ data (where 1 tick = $5, not $0.50).")

    big_contracts = sum(c for sz, c in contract_size_global.items() if sz >= 5)
    if big_contracts > 0:
        quality_warnings.append(
            f"**{big_contracts} trades with contracts >= 5** - verify these aren't "
            f"legacy contract-size errors. Phoenix prod is 1-contract.")

    if span_days is not None and span_days > 365:
        quality_warnings.append(
            f"**Date range spans {span_days} days.** Historical baseline "
            f"includes potentially multiple Phoenix architectural eras. "
            f"Recommend filtering trades to last 90 days for validation comparisons.")

    if quality_warnings:
        for w in quality_warnings:
            L.append(f"- WARN: {w}")
    else:
        L.append("- OK: No baseline quality flags raised.")
    L.append("")

    # Contract-size distribution
    L.append("## Contract Size Distribution")
    L.append("")
    L.append("| contracts | trade count | % of total |")
    L.append("|---:|---:|---:|")
    for sz, c in sorted(contract_size_global.items()):
        pct = 100 * c / total_n if total_n else 0
        L.append(f"| {sz} | {c:,} | {pct:.1f}% |")
    L.append("")

    # Per strategy
    L.append("## Per Strategy")
    L.append("")
    L.append("| Strategy | N | Gross $ | Net $ | WR-net | Max Loss $ | "
             "Max Win $ | Outliers | Suspicious |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for strat, s in sorted(by_strat.items(), key=lambda kv: kv[1]["net"]):
        wr_n = 100 * s["wins_net"] / s["n"] if s["n"] else 0
        L.append(f"| `{strat}` | {s['n']} | ${s['gross']:+,.2f} | "
                 f"${s['net']:+,.2f} | {wr_n:.0f}% | "
                 f"${s['max_gross_loss']:+,.2f} | ${s['max_gross_win']:+,.2f} | "
                 f"{s['outlier_count']} | {s['suspicious_count']} |")
    L.append("")

    # Schema-key coverage
    L.append("## Schema-key Coverage (top 25)")
    L.append("")
    L.append("| key | trade count | % present |")
    L.append("|---|---:|---:|")
    for k, c in sorted(schema_keys.items(), key=lambda kv: -kv[1])[:25]:
        pct = 100 * c / total_n if total_n else 0
        L.append(f"| `{k}` | {c:,} | {pct:.0f}% |")
    L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Trades: {total_n:,}  contracts: {total_contracts:,}  "
          f"gross ${total_gross:+,.2f}  net ${total_net:+,.2f}  "
          f"warnings: {len(quality_warnings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
