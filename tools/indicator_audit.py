"""
Phoenix - Indicator Predictive-Value Audit (READ-ONLY)

Discovers every indicator/confluence/feature in Phoenix's trade history
and ranks by actual predictive value (lift over base rate) with
Wilson 95% CIs and sample-size tier flags.

Outputs: out/indicator_audit_<today>.md

Never modifies state. Pure analysis.

Usage:
  python tools/indicator_audit.py
  python tools/indicator_audit.py --discover            # schema only
  python tools/indicator_audit.py --post-b13-only       # clean baseline
  python tools/indicator_audit.py --since 2026-04-01
  python tools/indicator_audit.py --strategy bias_momentum
  python tools/indicator_audit.py --min-sample 50
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CT = ZoneInfo("America/Chicago")


def _data_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "logs" / "trade_memory.json").exists():
        return cwd
    if (ROOT / "logs" / "trade_memory.json").exists():
        return ROOT
    return cwd


# ─── statistical helpers ─────────────────────────────────────────────

def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def sample_tier(n: int) -> str:
    if n < 30: return "INSUFFICIENT"
    if n < 100: return "PRELIMINARY"
    if n < 385: return "TENTATIVE"
    if n < 666: return "VALIDATED"
    return "HIGH_CONF"


def cis_overlap(ci_a, ci_b) -> bool:
    return not (ci_a[1] < ci_b[0] or ci_b[1] < ci_a[0])


# ─── data loading ────────────────────────────────────────────────────

def safe_pnl_net(t: dict) -> float:
    return float(t.get("pnl_dollars_net", t.get("pnl_dollars", 0.0)) or 0.0)


def is_post_b13(t: dict) -> bool:
    return "cost_total_dollars" in t


def trade_ts(t: dict):
    for k in ("ts", "exit_ts_ct", "exit_time", "entry_time", "recorded_at",
              "entry_ts_ct", "entry_ts"):
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


def load_all_trades(post_b13_only=False, since=None, strategy=None,
                    data_root: Path | None = None) -> list[dict]:
    root = data_root or _data_root()
    trades_file = root / "logs" / "trade_memory.json"
    if not trades_file.exists():
        return []
    raw = json.loads(trades_file.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("trades", [])
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        if post_b13_only and not is_post_b13(t):
            continue
        if strategy and t.get("strategy") != strategy:
            continue
        if since:
            ts = trade_ts(t)
            if ts is None or ts < since:
                continue
        out.append(t)
    return out


# ─── feature extraction ──────────────────────────────────────────────

def extract_features(t: dict) -> dict:
    feats: dict = {}
    # NOTE: `result` and `exit_reason` are POST-HOC outcomes, not
    # pre-trade predictors. Including them yields tautological "100% lift"
    # from result=WIN or exit_reason=target_hit — uninteresting for a
    # predictive-value audit. They're excluded from the keys we extract.
    for k in ("strategy", "tier", "direction", "account", "regime",
              "day_type", "sub_strategy"):
        if k in t:
            feats[k] = t[k]
    market = (t.get("market") or t.get("entry_market")
              or t.get("market_snapshot") or {})
    if isinstance(market, dict):
        for k, v in market.items():
            if isinstance(v, (str, int, float, bool)):
                feats[f"market.{k}"] = v
    metadata = t.get("metadata") or t.get("signal_metadata") or {}
    if isinstance(metadata, dict):
        for k, v in metadata.items():
            if isinstance(v, (str, int, float, bool)):
                feats[f"meta.{k}"] = v
    if "stop_ticks" in t:
        feats["stop_ticks"] = t["stop_ticks"]
    if "target_rr" in t:
        feats["target_rr"] = t["target_rr"]
    if "contracts" in t:
        feats["contracts"] = t["contracts"]
    ts = trade_ts(t)
    if ts:
        feats["hour_of_day"] = ts.hour
        feats["dow"] = ts.strftime("%a")
        bucket_min = (ts.minute // 30) * 30
        feats["time_bucket_ct"] = f"{ts.hour:02d}:{bucket_min:02d}"
    confluences = t.get("confluences") or []
    if isinstance(confluences, str):
        confluences = [confluences]
    if isinstance(confluences, list):
        for c in confluences:
            if isinstance(c, str) and c.strip():
                feats[f"conf:{c.strip()[:80]}"] = True
    return feats


# ─── discovery ───────────────────────────────────────────────────────

def discover_schema(trades: list[dict], min_presence: float = 0.05) -> dict:
    n = len(trades)
    if n == 0:
        return {}
    counts: Counter = Counter()
    types: dict[str, set] = defaultdict(set)
    samples: dict[str, list] = defaultdict(list)
    for t in trades:
        feats = extract_features(t)
        for k, v in feats.items():
            counts[k] += 1
            types[k].add(type(v).__name__)
            if len(samples[k]) < 5:
                samples[k].append(v)
    schema = {}
    for k, c in counts.items():
        if c / n < min_presence:
            continue
        schema[k] = {
            "presence_pct": round(100 * c / n, 1),
            "n_present": c,
            "value_types": sorted(types[k]),
            "samples": samples[k],
        }
    return schema


# ─── analysis ────────────────────────────────────────────────────────

def quartile_bin(values):
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    return {"q1_max": s[n // 4], "q2_max": s[n // 2], "q3_max": s[3 * n // 4]}


def bin_label(value, cuts):
    if value <= cuts["q1_max"]: return "Q1"
    if value <= cuts["q2_max"]: return "Q2"
    if value <= cuts["q3_max"]: return "Q3"
    return "Q4"


def is_numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def analyze_indicator(trades: list[dict], feat_name: str,
                      min_sample: int = 30) -> list[dict]:
    pairs = []
    trades_without_feat = []  # used for boolean-confluence comparison
    for t in trades:
        feats = extract_features(t)
        if feat_name not in feats:
            trades_without_feat.append(t)
            continue
        v = feats[feat_name]
        won = safe_pnl_net(t) > 0
        pairs.append((v, won, safe_pnl_net(t)))
    if not pairs:
        return []
    values_only = [v for v, _, _ in pairs]

    # Boolean confluence features (e.g. `conf:VWAP reclaim`) extract as
    # True when present and are simply absent otherwise. Within-feature
    # variation is zero, so the standard "value vs other values" pivot
    # collapses to nothing. Compare WITH-the-feature against trades that
    # don't have the feature at all (separate population). This is the
    # right comparison for binary-presence features.
    is_boolean = all(v is True for v in values_only)
    if is_boolean and trades_without_feat:
        n_with = len(pairs)
        wins_with = sum(1 for _, won, _ in pairs if won)
        gross_win = sum(pnl for _, won, pnl in pairs if won)
        gross_loss = sum(pnl for _, won, pnl in pairs if not won)
        n_without = len(trades_without_feat)
        wins_without = sum(
            1 for t in trades_without_feat if safe_pnl_net(t) > 0
        )
        if n_without == 0:
            return []
        wr_with = wins_with / n_with
        wr_without = wins_without / n_without
        ci_with = wilson_ci(wins_with, n_with)
        ci_without = wilson_ci(wins_without, n_without)
        lift_pp = round(100 * (wr_with - wr_without), 1)
        lift_rel = (round(wr_with / wr_without, 2)
                    if wr_without > 0 else float("inf"))
        sig = not cis_overlap(ci_with, ci_without)
        pf_with = (gross_win / abs(gross_loss)
                   if gross_loss < 0 else float("inf"))
        return [{
            "feature": feat_name, "value": "True",
            "n_with": n_with, "wins_with": wins_with,
            "wr_with": round(100 * wr_with, 1),
            "ci_with": (round(100 * ci_with[0], 1),
                        round(100 * ci_with[1], 1)),
            "n_without": n_without,
            "wr_without": round(100 * wr_without, 1),
            "ci_without": (round(100 * ci_without[0], 1),
                           round(100 * ci_without[1], 1)),
            "lift_pp": lift_pp, "lift_rel": lift_rel,
            "significant": sig,
            "sample_tier_with": sample_tier(n_with),
            "pf_with": round(pf_with, 2)
                       if pf_with != float("inf") else None,
        }]

    is_num = (all(is_numeric(v) for v in values_only)
              and len(set(values_only)) > 4)
    if is_num:
        cuts = quartile_bin(values_only)
        pairs = [(bin_label(v, cuts), won, pnl) for v, won, pnl in pairs]
    by_value: dict = defaultdict(
        lambda: {"n": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0}
    )
    for v, won, pnl in pairs:
        by_value[v]["n"] += 1
        if won:
            by_value[v]["wins"] += 1
            by_value[v]["gross_win"] += pnl
        else:
            by_value[v]["gross_loss"] += pnl
    total_n = sum(b["n"] for b in by_value.values())
    total_wins = sum(b["wins"] for b in by_value.values())
    rows = []
    for value, stats in by_value.items():
        n_with = stats["n"]
        wins_with = stats["wins"]
        n_without = total_n - n_with
        wins_without = total_wins - wins_with
        if n_without == 0:
            continue
        wr_with = wins_with / n_with if n_with else 0
        wr_without = wins_without / n_without
        ci_with = wilson_ci(wins_with, n_with)
        ci_without = wilson_ci(wins_without, n_without)
        lift_pp = round(100 * (wr_with - wr_without), 1)
        if wr_without > 0:
            lift_rel = round(wr_with / wr_without, 2)
        else:
            lift_rel = float("inf")
        sig = not cis_overlap(ci_with, ci_without)
        if stats["gross_loss"] < 0:
            pf_with = stats["gross_win"] / abs(stats["gross_loss"])
        else:
            pf_with = float("inf")
        rows.append({
            "feature": feat_name,
            "value": str(value),
            "n_with": n_with,
            "wins_with": wins_with,
            "wr_with": round(100 * wr_with, 1),
            "ci_with": (round(100 * ci_with[0], 1),
                        round(100 * ci_with[1], 1)),
            "n_without": n_without,
            "wr_without": round(100 * wr_without, 1),
            "ci_without": (round(100 * ci_without[0], 1),
                           round(100 * ci_without[1], 1)),
            "lift_pp": lift_pp,
            "lift_rel": lift_rel,
            "significant": sig,
            "sample_tier_with": sample_tier(n_with),
            "pf_with": round(pf_with, 2) if pf_with != float("inf") else None,
        })
    return rows


# ─── output ──────────────────────────────────────────────────────────

def emit_report(out_path: Path, schema: dict, all_rows: list[dict],
                trades: list[dict], filters: dict) -> None:
    L = []
    today = datetime.now(CT).date()
    L.append(f"# Phoenix Indicator Predictive-Value Audit - {today}")
    L.append("")
    L.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
    if filters.get("post_b13_only"):
        L.append("_Filter: post-B13 trades only_")
    if filters.get("since"):
        L.append(f"_Filter: trades on/after {filters['since']}_")
    if filters.get("strategy"):
        L.append(f"_Filter: strategy={filters['strategy']}_")
    L.append(f"_Trades analyzed: {len(trades)}_")
    L.append(f"_Min-sample threshold: {filters.get('min_sample', 30)}_")
    L.append("")
    L.append("## Methodology")
    L.append("")
    L.append("- **Lift (pp)** = WR_present - WR_absent (in percentage points).")
    L.append("- **Significant** = Wilson 95% CIs do NOT overlap.")
    L.append("- **Sample tier**: INSUFFICIENT(<30), PRELIMINARY(30-99), "
             "TENTATIVE(100-384), VALIDATED(385-665), HIGH_CONF(666+).")
    L.append("")
    L.append("**WARNING:** A high-magnitude lift on PRELIMINARY sample is a "
             "*hypothesis*, not a finding. Confirm at TENTATIVE+ before acting.")
    L.append("")

    significant = [
        r for r in all_rows
        if r["significant"] and r["sample_tier_with"] != "INSUFFICIENT"
    ]
    top_pos = sorted(
        [r for r in significant if r["lift_pp"] > 0],
        key=lambda r: -r["lift_pp"],
    )[:15]
    top_neg = sorted(
        [r for r in significant if r["lift_pp"] < 0],
        key=lambda r: r["lift_pp"],
    )[:15]

    L.append("## Top Predictive Indicators (positive lift, significant)")
    L.append("")
    if not top_pos:
        L.append("_None found above significance threshold._")
    else:
        L.append("| feature | value | n | WR present | CI | WR absent | "
                 "lift pp | PF | tier |")
        L.append("|---|---|---:|---:|---|---:|---:|---:|---|")
        for r in top_pos:
            pf = r["pf_with"] if r["pf_with"] is not None else "-"
            L.append(
                f"| `{r['feature']}` | `{r['value']}` | {r['n_with']} | "
                f"{r['wr_with']}% | {r['ci_with'][0]}-{r['ci_with'][1]}% | "
                f"{r['wr_without']}% | **+{r['lift_pp']}** | "
                f"{pf} | {r['sample_tier_with']} |"
            )
    L.append("")

    L.append("## Top Contra-Indicators (negative lift, significant)")
    L.append("")
    L.append("_Features present MORE often on losing trades. Either remove "
             "as confluences or invert sign._")
    L.append("")
    if not top_neg:
        L.append("_None found above significance threshold._")
    else:
        L.append("| feature | value | n | WR present | CI | WR absent | "
                 "lift pp | PF | tier |")
        L.append("|---|---|---:|---:|---|---:|---:|---:|---|")
        for r in top_neg:
            pf = r["pf_with"] if r["pf_with"] is not None else "-"
            L.append(
                f"| `{r['feature']}` | `{r['value']}` | {r['n_with']} | "
                f"{r['wr_with']}% | {r['ci_with'][0]}-{r['ci_with'][1]}% | "
                f"{r['wr_without']}% | **{r['lift_pp']}** | "
                f"{pf} | {r['sample_tier_with']} |"
            )
    L.append("")

    L.append("## Tier Classifier Validation")
    L.append("")
    L.append("_Does the A++/A/B/C tier prediction match outcome?_")
    L.append("")
    tier_rows = [r for r in all_rows if r["feature"] == "tier"]
    if tier_rows:
        L.append("| tier value | n | WR | CI | PF | sample tier |")
        L.append("|---|---:|---:|---|---:|---|")
        for r in sorted(tier_rows, key=lambda r: -r["wr_with"]):
            pf = r["pf_with"] if r["pf_with"] is not None else "-"
            L.append(
                f"| `{r['value']}` | {r['n_with']} | {r['wr_with']}% | "
                f"{r['ci_with'][0]}-{r['ci_with'][1]}% | {pf} | "
                f"{r['sample_tier_with']} |"
            )
        ranked = sorted(tier_rows, key=lambda r: -r["wr_with"])
        L.append("")
        if len(ranked) >= 2:
            best, worst = ranked[0], ranked[-1]
            L.append(f"**Verdict:** Best-WR tier `{best['value']}` "
                     f"({best['wr_with']}%); worst `{worst['value']}` "
                     f"({worst['wr_with']}%).")
            if (best["value"] in ("A++", "A")
                    and worst["value"] in ("B", "C")):
                L.append("OK: Tier ordering **consistent with classifier intent**.")
            else:
                L.append("WARN: Tier ordering **INCONSISTENT** with classifier "
                         "intent. Sprint B's tier-based-sizing proposal would "
                         "add noise.")
    else:
        L.append("_No tier field found in trade records._")
    L.append("")

    L.append("## Per-Strategy Indicator Significance")
    L.append("")
    L.append("_Strategies with n>=30 only._")
    L.append("")
    by_strat: dict = defaultdict(list)
    for t in trades:
        by_strat[t.get("strategy", "unknown")].append(t)
    for strat, strat_trades in sorted(by_strat.items(),
                                       key=lambda kv: -len(kv[1])):
        if len(strat_trades) < 30:
            continue
        L.append(f"### `{strat}` (n={len(strat_trades)})")
        L.append("")
        strat_schema = discover_schema(strat_trades)
        strat_rows = []
        for feat in strat_schema:
            strat_rows.extend(
                analyze_indicator(strat_trades, feat, min_sample=20)
            )
        sig_strat = [
            r for r in strat_rows
            if r["significant"] and r["sample_tier_with"] != "INSUFFICIENT"
        ]
        top_pos_s = sorted(
            [r for r in sig_strat if r["lift_pp"] > 0],
            key=lambda r: -r["lift_pp"],
        )[:5]
        top_neg_s = sorted(
            [r for r in sig_strat if r["lift_pp"] < 0],
            key=lambda r: r["lift_pp"],
        )[:5]
        if top_pos_s:
            L.append("**Top predictive:**")
            L.append("")
            for r in top_pos_s:
                L.append(f"- `{r['feature']}={r['value']}`: WR "
                         f"{r['wr_with']}% vs {r['wr_without']}% "
                         f"(lift +{r['lift_pp']}pp, n={r['n_with']})")
            L.append("")
        if top_neg_s:
            L.append("**Top contra-indicators:**")
            L.append("")
            for r in top_neg_s:
                L.append(f"- `{r['feature']}={r['value']}`: WR "
                         f"{r['wr_with']}% vs {r['wr_without']}% "
                         f"(lift {r['lift_pp']}pp, n={r['n_with']})")
            L.append("")
        if not top_pos_s and not top_neg_s:
            L.append("_No significant indicators at this sample size._")
            L.append("")

    L.append("## Per-Regime Indicator Significance")
    L.append("")
    by_regime: dict = defaultdict(list)
    for t in trades:
        regime = (
            t.get("regime")
            or (t.get("market") or {}).get("regime")
            or (t.get("market_snapshot") or {}).get("regime")
            or "unknown"
        )
        by_regime[regime].append(t)
    for regime, regime_trades in sorted(by_regime.items(),
                                          key=lambda kv: -len(kv[1])):
        if len(regime_trades) < 30:
            continue
        L.append(f"### `{regime}` (n={len(regime_trades)})")
        L.append("")
        regime_schema = discover_schema(regime_trades)
        regime_rows = []
        for feat in regime_schema:
            regime_rows.extend(
                analyze_indicator(regime_trades, feat, min_sample=20)
            )
        sig = [
            r for r in regime_rows
            if r["significant"] and r["sample_tier_with"] != "INSUFFICIENT"
        ]
        top_pos_r = sorted(
            [r for r in sig if r["lift_pp"] > 0],
            key=lambda r: -r["lift_pp"],
        )[:5]
        if top_pos_r:
            for r in top_pos_r:
                L.append(f"- `{r['feature']}={r['value']}`: WR "
                         f"{r['wr_with']}% (lift +{r['lift_pp']}pp, "
                         f"n={r['n_with']})")
            L.append("")
        else:
            L.append("_No significant indicators at this sample size._")
            L.append("")

    L.append("## Appendix: Discovered Schema")
    L.append("")
    L.append("| feature | presence | n | types | sample value |")
    L.append("|---|---:|---:|---|---|")
    for feat, info in sorted(schema.items(),
                              key=lambda kv: -kv[1]["n_present"])[:40]:
        sample = str(info["samples"][0])[:40] if info["samples"] else "-"
        L.append(f"| `{feat}` | {info['presence_pct']}% | "
                 f"{info['n_present']} | "
                 f"{','.join(info['value_types'])} | `{sample}` |")
    L.append("")
    out_path.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--post-b13-only", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--strategy")
    ap.add_argument("--min-sample", type=int, default=30)
    args = ap.parse_args()

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=CT)

    data_root = _data_root()
    trades = load_all_trades(post_b13_only=args.post_b13_only,
                              since=since, strategy=args.strategy,
                              data_root=data_root)
    if not trades:
        print("No trades loaded - check filters.")
        return 0

    print(f"Loaded {len(trades)} trades after filters.")
    schema = discover_schema(trades)
    print(f"Discovered {len(schema)} features present in >=5% of trades.")

    if args.discover:
        for feat, info in sorted(schema.items(),
                                   key=lambda kv: -kv[1]["n_present"]):
            print(f"  {feat:40s}  {info['presence_pct']:5.1f}%  "
                  f"n={info['n_present']:5d}  types={info['value_types']}")
        return 0

    print("Running per-indicator analysis...")
    all_rows = []
    for feat in schema:
        all_rows.extend(
            analyze_indicator(trades, feat, min_sample=args.min_sample)
        )
    print(f"Analyzed {len(all_rows)} (feature, value) combinations.")

    today = datetime.now(CT).date()
    out_path = data_root / f"out/indicator_audit_{today}.md"
    out_path.parent.mkdir(exist_ok=True)
    filters = {
        "post_b13_only": args.post_b13_only,
        "since": args.since,
        "strategy": args.strategy,
        "min_sample": args.min_sample,
    }
    emit_report(out_path, schema, all_rows, trades, filters)
    print(f"Wrote {out_path}")

    sig = [
        r for r in all_rows
        if r["significant"] and r["sample_tier_with"] != "INSUFFICIENT"
    ]
    pos = sorted([r for r in sig if r["lift_pp"] > 0],
                 key=lambda r: -r["lift_pp"])[:5]
    neg = sorted([r for r in sig if r["lift_pp"] < 0],
                 key=lambda r: r["lift_pp"])[:5]
    print()
    print("Top 5 predictive (significant):")
    for r in pos:
        val = str(r["value"])[:20]
        print(f"  {r['feature']}={val:20s} lift +{r['lift_pp']}pp  "
              f"WR {r['wr_with']}% (n={r['n_with']}, "
              f"{r['sample_tier_with']})")
    print()
    print("Top 5 contra-indicators (significant):")
    for r in neg:
        val = str(r["value"])[:20]
        print(f"  {r['feature']}={val:20s} lift {r['lift_pp']}pp   "
              f"WR {r['wr_with']}% (n={r['n_with']}, "
              f"{r['sample_tier_with']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
