"""
Phoenix - Level 2 Data ROI Audit

Decision-grade analysis: is the ~$100/month L2 (DOM) data earning its keep?

Three orthogonal views:
  1. Statistical lift   - do DOM features predict outcome?
  2. Architectural deps - which strategies use DOM, how heavily?
  3. Economic ROI       - cost per DOM-influenced trade, estimated edge

Output: out/l2_roi_audit_<today>.md

Read-only. No mutations.

Usage:
  python tools/audit_l2_roi.py                      # default $100/mo
  python tools/audit_l2_roi.py --monthly-cost 105   # actual amount
  python tools/audit_l2_roi.py --post-b13-only      # cleaner baseline
  python tools/audit_l2_roi.py --since 2026-04-01   # date filter
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CT = ZoneInfo("America/Chicago")


def _data_root() -> Path:
    """Detect phoenix_bot root via existence of any trade_memory file
    (legacy `trade_memory.json` OR per-bot `trade_memory_<bot>.json`).
    Post-2026-05-12 split, the legacy file may be absent in fresh
    checkouts while per-bot files accumulate."""
    def _has_trade_memory(p: Path) -> bool:
        logs = p / "logs"
        if not logs.is_dir():
            return False
        if (logs / "trade_memory.json").exists():
            return True
        try:
            for f in logs.iterdir():
                if f.name.startswith("trade_memory_") and f.name.endswith(".json"):
                    return True
        except OSError:
            pass
        return False
    cwd = Path.cwd()
    if _has_trade_memory(cwd):
        return cwd
    if _has_trade_memory(ROOT):
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


def load_trades(post_b13_only=False, since=None,
                data_root: Path | None = None) -> list[dict]:
    """Load + filter trades via core.trade_memory.load_all_trades().

    2026-05-13 audit: previously raw-read logs/trade_memory.json which
    became stale after the 2026-05-12 per-bot split. Now merges legacy
    + every per-bot file so the L2 ROI computation reflects current
    DOM-keyword usage in recent trades."""
    from core.trade_memory import load_all_trades
    root = data_root or _data_root()
    raw = load_all_trades(logs_dir=str(root / "logs"))
    if not isinstance(raw, list):
        return []
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        if post_b13_only and not is_post_b13(t):
            continue
        if since:
            ts = trade_ts(t)
            if ts is None or ts < since:
                continue
        out.append(t)
    return out


# ─── DOM field discovery ─────────────────────────────────────────────

# Compiled patterns: any of these in a field name → DOM-related.
# Includes order-flow / CVD which are L2-derived signals.
DOM_PATTERN = re.compile(
    r"^dom_|^market\.dom_|bid_stack|ask_stack|bid_heavy|ask_heavy|"
    r"dom_imbalance|^cvd|market\.cvd|order_flow|orderflow",
    re.IGNORECASE,
)


def is_dom_field(field_name: str) -> bool:
    return bool(DOM_PATTERN.search(field_name))


def extract_dom_features(t: dict) -> dict:
    """Pull every DOM-related field from a trade record. Looks at both
    market_snapshot (Phoenix's actual schema) and a top-level scan."""
    feats = {}
    market = (t.get("market_snapshot")
              or t.get("market")
              or t.get("entry_market")
              or {})
    if isinstance(market, dict):
        for k, v in market.items():
            full_key = f"market.{k}"
            if is_dom_field(full_key) and isinstance(v, (str, int, float, bool)):
                feats[full_key] = v
    for k, v in t.items():
        if is_dom_field(k) and isinstance(v, (str, int, float, bool)):
            feats[k] = v
    return feats


# ─── analysis ────────────────────────────────────────────────────────

def compute_lift(trades: list[dict], dom_field: str) -> dict | None:
    """For a binary/categorical/numeric DOM field, compute WR with vs
    without (binary) or above-vs-below median (numeric). Returns None
    when cohort size or value variation is insufficient."""
    pairs = []
    for t in trades:
        feats = extract_dom_features(t)
        if dom_field not in feats:
            continue
        v = feats[dom_field]
        won = safe_pnl_net(t) > 0
        pairs.append((v, won, safe_pnl_net(t)))
    if not pairs:
        return None

    # Boolean field: with vs without
    if all(isinstance(v, bool) for v, _, _ in pairs):
        with_true = [(won, pnl) for v, won, pnl in pairs if v is True]
        with_false = [(won, pnl) for v, won, pnl in pairs if v is False]
        if not with_true or not with_false:
            return None
        n_t, w_t = len(with_true), sum(1 for w, _ in with_true if w)
        n_f, w_f = len(with_false), sum(1 for w, _ in with_false if w)
        wr_t = w_t / n_t
        wr_f = w_f / n_f
        ci_t = wilson_ci(w_t, n_t)
        ci_f = wilson_ci(w_f, n_f)
        return {
            "field": dom_field,
            "type": "binary",
            "n_with_true": n_t, "wr_true": wr_t, "ci_true": ci_t,
            "n_with_false": n_f, "wr_false": wr_f, "ci_false": ci_f,
            "lift_pp": round(100 * (wr_t - wr_f), 1),
            "significant": not cis_overlap(ci_t, ci_f),
            "tier": sample_tier(min(n_t, n_f)),
        }

    # Numeric field: median split (booleans already handled above; here
    # we know we have ints/floats and we drop bools to be safe).
    numeric_pairs = [(v, won, pnl) for v, won, pnl in pairs
                     if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(numeric_pairs) < 8 or len({v for v, _, _ in numeric_pairs}) < 2:
        return None
    values = sorted(v for v, _, _ in numeric_pairs)
    median = values[len(values) // 2]
    above = [(won, pnl) for v, won, pnl in numeric_pairs if v > median]
    below = [(won, pnl) for v, won, pnl in numeric_pairs if v <= median]
    if not above or not below:
        return None
    n_a, w_a = len(above), sum(1 for w, _ in above if w)
    n_b, w_b = len(below), sum(1 for w, _ in below if w)
    wr_a = w_a / n_a
    wr_b = w_b / n_b
    ci_a = wilson_ci(w_a, n_a)
    ci_b = wilson_ci(w_b, n_b)
    return {
        "field": dom_field,
        "type": "numeric_median_split",
        "median": median,
        "n_above": n_a, "wr_above": wr_a, "ci_above": ci_a,
        "n_below": n_b, "wr_below": wr_b, "ci_below": ci_b,
        "lift_pp": round(100 * (wr_a - wr_b), 1),
        "significant": not cis_overlap(ci_a, ci_b),
        "tier": sample_tier(min(n_a, n_b)),
    }


_DOM_DEP_PATTERN = re.compile(
    r"dom_imbalance|dom_bid_heavy|dom_ask_heavy|dom_bid_stack|"
    r"dom_ask_stack|bid_stack|ask_stack|order_?flow|"
    r"\bcvd\b|cvd_",
    re.IGNORECASE,
)


def find_dom_dependent_strategies(data_root: Path) -> dict:
    """Grep strategy code for DOM/CVD references. Returns
    {strategy_module_name: [(line_no, line_text), ...]}."""
    deps: dict[str, list[tuple[int, str]]] = defaultdict(list)
    strategy_dir = data_root / "strategies"
    if not strategy_dir.exists():
        # Fall back to project ROOT if cwd doesn't have it (test isolation)
        strategy_dir = ROOT / "strategies"
    if not strategy_dir.exists():
        return deps
    for f in strategy_dir.glob("*.py"):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if _DOM_DEP_PATTERN.search(line):
                    deps[f.stem].append((i, line.strip()[:120]))
        except Exception:
            continue
    return deps


_DOM_KW = ("dom", "bid_heavy", "ask_heavy", "imbalance", "cvd",
           "order flow", "orderflow")


def count_dom_confluences_in_trades(trades: list[dict]) -> int:
    """How many trades had a DOM-related confluence in their entry?
    Scans both `confluences` field and the `entry_reason` text."""
    count = 0
    for t in trades:
        text_blobs = []
        confs = t.get("confluences") or []
        if isinstance(confs, str):
            text_blobs.append(confs.lower())
        elif isinstance(confs, list):
            for c in confs:
                if isinstance(c, str):
                    text_blobs.append(c.lower())
        # Entry reason often references DOM in dom_pullback strategy
        er = t.get("entry_reason") or ""
        if isinstance(er, str):
            text_blobs.append(er.lower())
        joined = " ".join(text_blobs)
        if any(kw in joined for kw in _DOM_KW):
            count += 1
    return count


# ─── main ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monthly-cost", type=float, default=100.0,
                    help="Monthly L2 data cost in USD (default $100)")
    ap.add_argument("--post-b13-only", action="store_true")
    ap.add_argument("--since", help="YYYY-MM-DD filter")
    args = ap.parse_args()

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=CT)

    data_root = _data_root()
    trades = load_trades(post_b13_only=args.post_b13_only, since=since,
                          data_root=data_root)
    today = datetime.now(CT).date()
    out_path = data_root / f"out/l2_roi_audit_{today}.md"
    out_path.parent.mkdir(exist_ok=True)

    L = []
    L.append(f"# Phoenix Level 2 Data ROI Audit - {today}")
    L.append("")
    L.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
    L.append(f"_Monthly cost: ${args.monthly_cost:.2f}_")
    L.append(f"_Trades analyzed: {len(trades)}_")
    if args.post_b13_only:
        L.append("_Filter: post-B13 only_")
    if args.since:
        L.append(f"_Filter: since {args.since}_")
    L.append("")

    if not trades:
        L.append("**No trades loaded - check filters.**")
        out_path.write_text("\n".join(L), encoding="utf-8")
        print(f"\nWrote {out_path} (no trades)")
        return 0

    # ─── 1. DOM field discovery ─────────────────────────────────────
    L.append("## 1. DOM field discovery")
    L.append("")
    field_presence: Counter = Counter()
    for t in trades:
        feats = extract_dom_features(t)
        for k in feats:
            field_presence[k] += 1

    has_dom_data = bool(field_presence)
    if not has_dom_data:
        L.append("**WARN: No DOM fields found in any trade record.**")
        L.append("")
        L.append("This means one of:")
        L.append("- DOM data is captured but not persisted to trade_memory")
        L.append("- The bot isn't reading DOM data at all")
        L.append("- Field names don't match patterns (`dom_*`, `bid_stack`,")
        L.append("  `cvd`, `order_flow`, etc.)")
        L.append("")
        L.append("**If DOM fields are absent, the L2 subscription is paying**")
        L.append("**for data that's not even reaching trade decisions.**")
        L.append("Verify via `grep -rn 'dom_' bots/ strategies/ core/`.")
        L.append("")
    else:
        L.append(f"Found **{len(field_presence)} DOM fields** in trade records:")
        L.append("")
        L.append("| Field | Present in N trades | % coverage |")
        L.append("|---|---:|---:|")
        for k, c in sorted(field_presence.items(), key=lambda kv: -kv[1]):
            L.append(f"| `{k}` | {c:,} | {100*c/len(trades):.0f}% |")
        L.append("")

    # ─── 2. View 1: Statistical lift ────────────────────────────────
    L.append("## 2. View 1 - Statistical lift")
    L.append("")
    L.append("_Does each DOM feature predict win/loss outcome? "
             "Lift = WR_present - WR_absent (binary) or above-median - "
             "below-median (numeric)._")
    L.append("")
    lift_results = []
    for field in field_presence:
        result = compute_lift(trades, field)
        if result:
            lift_results.append(result)
    if not lift_results:
        L.append("_No DOM field had sufficient cohort sizes for lift analysis._")
        L.append("")
    else:
        L.append("| Field | Type | Lift pp | Significant | Tier |")
        L.append("|---|---|---:|---|---|")
        for r in sorted(lift_results, key=lambda r: -abs(r["lift_pp"])):
            sig = "YES" if r["significant"] else "-"
            sign = "+" if r["lift_pp"] >= 0 else ""
            L.append(f"| `{r['field']}` | {r['type']} | "
                     f"{sign}{r['lift_pp']} | {sig} | {r['tier']} |")
        L.append("")
        L.append("### Detail per field")
        L.append("")
        for r in sorted(lift_results, key=lambda r: -abs(r["lift_pp"])):
            L.append(f"**`{r['field']}`** ({r['type']}, {r['tier']}):")
            if r["type"] == "binary":
                L.append(f"- True:  n={r['n_with_true']}, "
                         f"WR={100*r['wr_true']:.1f}% "
                         f"(CI {100*r['ci_true'][0]:.0f}-"
                         f"{100*r['ci_true'][1]:.0f}%)")
                L.append(f"- False: n={r['n_with_false']}, "
                         f"WR={100*r['wr_false']:.1f}% "
                         f"(CI {100*r['ci_false'][0]:.0f}-"
                         f"{100*r['ci_false'][1]:.0f}%)")
            else:
                L.append(f"- Above median ({r['median']}): n={r['n_above']}, "
                         f"WR={100*r['wr_above']:.1f}% "
                         f"(CI {100*r['ci_above'][0]:.0f}-"
                         f"{100*r['ci_above'][1]:.0f}%)")
                L.append(f"- Below median: n={r['n_below']}, "
                         f"WR={100*r['wr_below']:.1f}% "
                         f"(CI {100*r['ci_below'][0]:.0f}-"
                         f"{100*r['ci_below'][1]:.0f}%)")
            L.append("")

    # ─── 3. View 2: Architectural dependency ────────────────────────
    L.append("## 3. View 2 - Architectural dependency")
    L.append("")
    L.append("_Which strategy code references DOM/CVD/order-flow? "
             "Heavy reference = lock-in._")
    L.append("")
    deps = find_dom_dependent_strategies(data_root)
    has_dom_deps = bool(deps)
    if not has_dom_deps:
        L.append("**Zero strategies reference DOM/CVD in their code.**")
        L.append("")
        L.append("If true, the L2 subscription provides NO architectural value -")
        L.append("the bot doesn't even consult it. Verify by reading the grep")
        L.append("output below.")
        L.append("")
    else:
        L.append(f"Found {len(deps)} strategies with DOM/CVD references:")
        L.append("")
        L.append("| Strategy | # DOM refs | Sample reference |")
        L.append("|---|---:|---|")
        for strat, refs in sorted(deps.items(), key=lambda kv: -len(kv[1])):
            sample = refs[0][1][:80] if refs else ""
            sample_clean = sample.replace("|", "\\|")
            L.append(f"| `{strat}` | {len(refs)} | `{sample_clean}` |")
        L.append("")
        # Heavy users (>5 lines)
        heavy = [s for s, r in deps.items() if len(r) > 5]
        if heavy:
            L.append(f"**Heavy DOM users (>5 lines): {heavy}**")
            L.append("These strategies' decision logic depends on DOM. ")
            L.append("Cancelling L2 would break or degrade them.")
            L.append("")
        else:
            L.append("_No heavy DOM users — references are <=5 lines per "
                     "strategy (soft confluence, not hard dependency)._")
            L.append("")

    # ─── 4. View 3: Economic ROI ────────────────────────────────────
    L.append("## 4. View 3 - Economic ROI")
    L.append("")
    n_trades = len(trades)
    timestamps = [trade_ts(t) for t in trades if trade_ts(t)]
    if timestamps:
        span_days = max((max(timestamps) - min(timestamps)).days, 1)
        trades_per_month = n_trades * 30 / span_days
    else:
        span_days = 1
        trades_per_month = n_trades

    dom_confluence_count = count_dom_confluences_in_trades(trades)
    cost_per_total_trade = args.monthly_cost / max(trades_per_month, 1)
    if dom_confluence_count > 0:
        dom_trades_per_month = dom_confluence_count * 30 / span_days
        cost_per_dom_trade = args.monthly_cost / max(dom_trades_per_month, 1)
    else:
        dom_trades_per_month = 0.0
        cost_per_dom_trade = float("inf")

    L.append(f"- Total trades in dataset: **{n_trades:,}** over "
             f"**{span_days} days**")
    L.append(f"- Estimated trades/month: **{trades_per_month:.0f}**")
    L.append(f"- Trades with DOM-keyword confluence/reason: "
             f"**{dom_confluence_count:,}** "
             f"({100*dom_confluence_count/n_trades:.1f}%)")
    L.append(f"- Cost per trade (all trades): **${cost_per_total_trade:.2f}**")
    if cost_per_dom_trade != float("inf"):
        L.append(f"- Cost per DOM-influenced trade: "
                 f"**${cost_per_dom_trade:.2f}**")
    else:
        L.append("- Cost per DOM-influenced trade: **N/A (no DOM-tagged "
                 "trades)**")
    L.append("")

    estimated_edge = 0.0
    has_significant_positive_lift = False
    has_significant_negative_lift = False
    sample_is_preliminary = True
    sig_lifts = []
    if lift_results:
        sig_lifts = [r for r in lift_results if r["significant"]]
        has_significant_positive_lift = any(r["lift_pp"] > 0
                                              for r in sig_lifts)
        has_significant_negative_lift = any(r["lift_pp"] < 0
                                              for r in sig_lifts)
        # Sample tier — at least one TENTATIVE+ result needed to escape
        # PRELIMINARY label.
        any_tentative_plus = any(
            r["tier"] in ("TENTATIVE", "VALIDATED", "HIGH_CONF")
            for r in lift_results
        )
        sample_is_preliminary = not any_tentative_plus

    if sig_lifts:
        positive_sigs = [r for r in sig_lifts if r["lift_pp"] > 0]
        if positive_sigs:
            best = max(positive_sigs, key=lambda r: r["lift_pp"])
            pnl_magnitudes = [abs(safe_pnl_net(t)) for t in trades
                              if safe_pnl_net(t) != 0]
            avg_trade_size = (sum(pnl_magnitudes) / len(pnl_magnitudes)
                              if pnl_magnitudes else 25.0)
            estimated_edge = (best["lift_pp"] / 100) * dom_trades_per_month * avg_trade_size
            L.append(f"- Best significant lift: **+{best['lift_pp']}pp** on "
                     f"`{best['field']}`")
            L.append(f"- Avg trade size: **${avg_trade_size:.2f}**")
            L.append(f"- DOM trades/month (estimated): "
                     f"**{dom_trades_per_month:.0f}**")
            L.append(f"- Estimated monthly edge from DOM: "
                     f"**${estimated_edge:.2f}**")
            L.append(f"- Monthly cost: **${args.monthly_cost:.2f}**")
            L.append(f"- **Net ROI: ${estimated_edge - args.monthly_cost:+.2f}"
                     f"/month**")
            L.append("")

    # ─── 5. Recommendation ──────────────────────────────────────────
    L.append("## 5. Recommendation")
    L.append("")
    L.append("### Decision matrix")
    L.append("")
    L.append("| Question | Answer |")
    L.append("|---|---|")
    L.append(f"| DOM data captured in trade records? | "
             f"{'YES' if has_dom_data else 'NO'} |")
    L.append(f"| Strategies have DOM code dependencies? | "
             f"{'YES (' + str(len(deps)) + ')' if has_dom_deps else 'NO'} |")
    heavy_deps = bool(deps and any(len(r) > 5 for r in deps.values()))
    L.append(f"| Heavy DOM dependency in any strategy? | "
             f"{'YES' if heavy_deps else 'NO'} |")
    L.append(f"| Statistically significant + lift? | "
             f"{'YES' if has_significant_positive_lift else 'NO'} |")
    L.append(f"| Statistically significant - lift (contra)? | "
             f"{'YES (DOM may be misleading)' if has_significant_negative_lift else 'NO'} |")
    L.append(f"| Sample is PRELIMINARY tier or smaller? | "
             f"{'YES (findings are hypotheses)' if sample_is_preliminary else 'NO (TENTATIVE+)'} |")
    L.append("")

    L.append("### Verdict")
    L.append("")

    # Decision tree:
    # 1. No data + no deps -> CANCEL (nothing uses it)
    # 2. No data + has deps -> INVESTIGATE (data flow may be broken)
    # 3. Significant negative lift -> CANCEL (DOM is misleading)
    # 4. Heavy DOM deps + profitable -> KEEP (lock-in, even w/o lift proof)
    # 5. Significant positive lift + TENTATIVE+ -> KEEP
    # 6. Significant positive lift + PRELIMINARY -> KEEP-ONE-WEEK
    # 7. Has deps but no lift evidence -> KEEP-ONE-WEEK
    # 8. Default ambiguous -> KEEP-ONE-WEEK (never cancel on weak evidence)
    if not has_dom_data and not has_dom_deps:
        verdict = "CANCEL — STRONG EVIDENCE"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("The L2 data is not captured in trade records AND no strategy")
        L.append("code references it. You are paying "
                 f"${args.monthly_cost:.2f}/month")
        L.append("for data the bot doesn't use. Cancel before tomorrow's "
                 "charge.")
    elif not has_dom_data and has_dom_deps:
        verdict = "INVESTIGATE BEFORE DECISION"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("Strategies reference DOM, but the data isn't reaching trade")
        L.append("records. Either the data flow is broken (bug), or the")
        L.append("strategies reference DOM in conditional branches that")
        L.append("never fire. Run:")
        L.append("```")
        L.append("grep -rn 'dom_imbalance\\|dom_bid_heavy' strategies/ bots/")
        L.append("```")
        L.append("Identify whether DOM is actually being consulted at runtime.")
        L.append("Until clarified, KEEP one more month and re-audit.")
    elif has_significant_negative_lift:
        verdict = "CANCEL — DOM IS A CONTRA-INDICATOR"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("DOM features show statistically significant NEGATIVE lift —")
        L.append("they predict losses more than wins. Either remove DOM as a")
        L.append("confluence (and cancel the data) or invert the sign of how")
        L.append("strategies read it. Cancel saves "
                 f"${args.monthly_cost:.2f}/month")
        L.append("AND removes a misleading signal.")
    elif heavy_deps:
        verdict = "KEEP — STRATEGIES HARD-DEPEND ON DOM"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("At least one strategy references DOM heavily (>5 lines of")
        L.append("decision code). Cancelling L2 would break or degrade those")
        L.append("strategies. Even if statistical lift isn't yet at TENTATIVE")
        L.append("tier, architectural lock-in trumps statistical confidence")
        L.append("until the operator explicitly migrates those strategies off")
        L.append("DOM. KEEP and re-audit weekly.")
    elif has_significant_positive_lift and not sample_is_preliminary:
        verdict = "KEEP — DOM IS PROVEN PREDICTIVE"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("DOM features show statistically significant POSITIVE lift")
        L.append("at TENTATIVE+ tier. The estimated monthly edge "
                 f"({'$' + format(estimated_edge, '.2f')}) ")
        L.append("relative to the "
                 f"${args.monthly_cost:.2f} cost makes renewing correct.")
    elif has_significant_positive_lift and sample_is_preliminary:
        verdict = "KEEP-ONE-WEEK + RE-AUDIT"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("DOM features show positive lift but at PRELIMINARY tier")
        L.append("(small sample). Findings are directional, not confirmed.")
        L.append("Renew this month, re-run this audit weekly. If lift")
        L.append("strengthens at TENTATIVE tier (n>=100), KEEP. If lift")
        L.append("weakens or flips, CANCEL next billing cycle.")
    elif has_dom_deps and not has_significant_positive_lift:
        verdict = "KEEP-ONE-WEEK + RE-AUDIT"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("Strategies depend on DOM (light references) but lift is not")
        L.append("yet significant. This is the most ambiguous case — can't")
        L.append("justify cancel (strategies break) but can't confirm value")
        L.append("(no proven edge). Renew this month, accumulate >=100 trades")
        L.append("on DOM-dependent strategies, re-run audit. Decision next")
        L.append("billing cycle.")
    else:
        verdict = "KEEP-ONE-WEEK + RE-AUDIT — DEFAULT TO SAFE"
        L.append(f"**{verdict}**")
        L.append("")
        L.append("Insufficient evidence to confidently cancel. Renew, gather")
        L.append("more data, re-audit weekly.")
    L.append("")

    L.append("### Re-audit cadence")
    L.append("")
    L.append("If KEEP recommendation: re-run this audit weekly.")
    L.append("```")
    L.append("python tools/audit_l2_roi.py --post-b13-only")
    L.append("```")
    L.append("If 30 days pass without lift reaching significant + TENTATIVE,")
    L.append("the L2 subscription is not earning its keep. Cancel.")
    L.append("")

    out_path.write_text("\n".join(L), encoding="utf-8")

    # ─── stdout summary ─────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"L2 ROI AUDIT - {today}")
    print(f"{'='*60}")
    print(f"Trades analyzed:     {n_trades:,} over {span_days} days")
    print(f"DOM fields found:    {len(field_presence)}")
    print(f"Strategies w/ DOM:   {len(deps)}")
    print(f"Heavy DOM deps:      {sum(1 for r in deps.values() if len(r) > 5)}")
    print(f"DOM confluences:     {dom_confluence_count} "
          f"({100*dom_confluence_count/max(n_trades,1):.1f}%)")
    print(f"Significant + lift:  "
          f"{sum(1 for r in lift_results if r['significant'] and r['lift_pp'] > 0)}")
    print(f"Significant - lift:  "
          f"{sum(1 for r in lift_results if r['significant'] and r['lift_pp'] < 0)}")
    print()
    print(f"VERDICT: {verdict}")
    print()
    print(f"FULL REPORT: {out_path}")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
