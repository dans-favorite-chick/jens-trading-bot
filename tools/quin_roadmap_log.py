"""
Phoenix x QUIN — Daily roadmap capture + post-session calibration.

Two phases of the same data flow:

  morning  (--capture)    : take a JSON file describing today's QUIN
                            roadmap and persist it as
                            logs/quin_roadmap/<date>.json. Read-only
                            against the bot's state.

  evening  (--reconcile)  : after market close, read the morning capture
                            + Phoenix's actual P&L from
                            out/daily_summary_<date>.md (Sprint C tool)
                            and write
                            out/quin_calibration_<date>.json plus a
                            row in out/quin_phoenix_calibration.csv.

Both phases are READ-ONLY against the bot. They never touch positions,
strategies, OIFs, or NT8.

Schema reference: see Phoenix x QUIN Roadmap May 4, 2026 doc.

Usage:
  # Morning: capture the day's roadmap from a JSON file
  python tools/quin_roadmap_log.py --capture --input data/quin_today.json

  # Evening: reconcile predictions against actuals
  python tools/quin_roadmap_log.py --reconcile

  # Both
  python tools/quin_roadmap_log.py --capture --input data/quin_today.json --reconcile

  # Inspect last N calibrations
  python tools/quin_roadmap_log.py --summary
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CT = ZoneInfo("America/Chicago")


def _data_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "out").exists() or (cwd / "logs").exists():
        return cwd
    if (ROOT / "logs").exists():
        return ROOT
    return cwd


# ─── schema validation ───────────────────────────────────────────────

REQUIRED_TOP_KEYS = {
    "date", "regime", "net_gex_M", "total_gex_M", "iv_30d_pct",
    "q_score", "conviction", "levels", "predicted_range",
    "predicted_strategy_fit",
}
REQUIRED_LEVEL_KEYS = {"hvl", "call_resistance", "put_support"}
REQUIRED_RANGE_KEYS = {"low", "high", "probability"}
REQUIRED_QSCORE_KEYS = {"momentum", "seasonality", "volatility", "options"}

VALID_REGIMES = {
    "POSITIVE_STRONG", "POSITIVE_NORMAL", "NEUTRAL",
    "NEGATIVE_NORMAL", "NEGATIVE_STRONG",
}
VALID_FITS = {"ideal", "favorable", "mixed", "watch_open",
              "conditional", "hostile", "disabled"}


def validate_capture(d: dict) -> list[str]:
    """Return a list of validation errors (empty list if valid)."""
    errs: list[str] = []
    missing = REQUIRED_TOP_KEYS - set(d.keys())
    if missing:
        errs.append(f"missing top-level keys: {sorted(missing)}")
    if "regime" in d and d["regime"] not in VALID_REGIMES:
        errs.append(f"regime {d['regime']!r} not in {VALID_REGIMES}")
    levels = d.get("levels", {})
    miss_lv = REQUIRED_LEVEL_KEYS - set(levels.keys())
    if miss_lv:
        errs.append(f"missing levels keys: {sorted(miss_lv)}")
    pred_range = d.get("predicted_range", {})
    miss_pr = REQUIRED_RANGE_KEYS - set(pred_range.keys())
    if miss_pr:
        errs.append(f"missing predicted_range keys: {sorted(miss_pr)}")
    if "probability" in pred_range:
        p = pred_range["probability"]
        if not (isinstance(p, (int, float)) and 0.0 <= p <= 1.0):
            errs.append(f"predicted_range.probability {p!r} not in [0,1]")
    qscore = d.get("q_score", {})
    miss_qs = REQUIRED_QSCORE_KEYS - set(qscore.keys())
    if miss_qs:
        errs.append(f"missing q_score keys: {sorted(miss_qs)}")
    fits = d.get("predicted_strategy_fit", {})
    for strat, fit in fits.items():
        if fit not in VALID_FITS:
            errs.append(f"strategy {strat!r} fit {fit!r} not in {VALID_FITS}")
    return errs


# ─── capture phase ───────────────────────────────────────────────────

def capture(input_path: Path, data_root: Path) -> Path:
    """Load + validate the day's roadmap JSON; persist to
    logs/quin_roadmap/<date>.json. Returns the output path."""
    if not input_path.exists():
        raise FileNotFoundError(f"input file not found: {input_path}")
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"input must be a JSON object, got {type(raw).__name__}")
    errs = validate_capture(raw)
    if errs:
        raise ValueError("schema validation failed:\n  - " + "\n  - ".join(errs))
    # Add metadata if absent
    raw.setdefault("fetched_ts_ct", datetime.now(CT).isoformat(timespec="seconds"))
    out_dir = data_root / "logs" / "quin_roadmap"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{raw['date']}.json"
    out_path.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    return out_path


# ─── reconcile phase ─────────────────────────────────────────────────

def parse_daily_summary(md_path: Path) -> dict:
    """Extract per-strategy fills/wins/losses/net_pnl from the Sprint C
    daily_summary markdown report. Returns
        {bot: {strategy: {signals, fills, wins, losses, net_pnl}}}.
    Empty dict if file missing or unparseable."""
    if not md_path.exists():
        return {}
    text = md_path.read_text(encoding="utf-8")
    # Each bot section starts with "## Bot: `<name>`" then has a
    # "### Per strategy" subsection containing a table:
    #     | strategy | signals | fills | wins | losses | net P&L |
    out: dict = {}
    bot_blocks = re.split(r"^## Bot: `(\w+)`", text, flags=re.MULTILINE)
    # bot_blocks: [pre, bot1_name, bot1_body, bot2_name, bot2_body, ...]
    for i in range(1, len(bot_blocks), 2):
        bot_name = bot_blocks[i]
        body = bot_blocks[i + 1] if i + 1 < len(bot_blocks) else ""
        # Find the per-strategy table
        per_strat = re.search(
            r"### Per strategy\s*\n\n\|.*?\|\s*\n\|[-:|\s]+\|\s*\n((?:\|.*?\|\s*\n)*)",
            body, re.DOTALL,
        )
        if not per_strat:
            continue
        rows_text = per_strat.group(1)
        bot_strats: dict = {}
        for line in rows_text.splitlines():
            line = line.strip()
            if not line or not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 6:
                continue
            strat_cell, sig, fil, won, lost, pnl = cells[:6]
            strat_name = strat_cell.strip("`")
            try:
                # P&L cell: "$+12.50" / "$-5.00" / "$+0.00"
                pnl_clean = pnl.replace("$", "").replace(",", "").strip()
                bot_strats[strat_name] = {
                    "signals": int(sig),
                    "fills": int(fil),
                    "wins": int(won),
                    "losses": int(lost),
                    "net_pnl": float(pnl_clean),
                }
            except (ValueError, TypeError):
                continue
        if bot_strats:
            out[bot_name] = bot_strats
    return out


# Map QUIN strategy names → Phoenix STRATEGY_ACCOUNT_MAP keys
# (bias_momentum is the same on both sides; opening_session in QUIN
# spans Phoenix's sub-strategies). Unknown→fall through.
STRATEGY_ALIAS = {
    "noise_area_momentum": "noise_area",  # QUIN doc uses long name
}


def normalize_strategy(name: str) -> str:
    return STRATEGY_ALIAS.get(name, name)


def actual_pnl_for_strategy(daily: dict, strat: str) -> dict:
    """Sum across both bots for a given strategy. Returns
    {signals, fills, wins, losses, net_pnl} (zeros if absent)."""
    target = normalize_strategy(strat)
    agg = {"signals": 0, "fills": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
    for bot_strats in daily.values():
        s = bot_strats.get(target)
        if s:
            for k in ("signals", "fills", "wins", "losses"):
                agg[k] += int(s.get(k, 0) or 0)
            agg["net_pnl"] += float(s.get("net_pnl", 0.0) or 0.0)
    agg["net_pnl"] = round(agg["net_pnl"], 2)
    return agg


# Map predicted fit -> expected sign of net P&L.
# "ideal"/"favorable" → expect non-negative (we're explicitly betting
#                       this strategy works in this regime)
# "hostile" → expect negative (predicted to struggle)
# "mixed"/"conditional"/"watch_open" → no expectation (skip from
#                                       calibration)
# "disabled" → strategy was off-config, skip
FIT_EXPECTATION = {
    "ideal":      "non_negative",
    "favorable":  "non_negative",
    "hostile":    "negative",
    "mixed":      "skip",
    "conditional":"skip",
    "watch_open": "skip",
    "disabled":   "skip",
}


def is_consistent(fit: str, actual: dict) -> bool | None:
    """Return True/False/None (None if predicted fit is non-evaluable
    or signals are too few to assess)."""
    expectation = FIT_EXPECTATION.get(fit, "skip")
    if expectation == "skip":
        return None
    if actual["fills"] == 0:
        # Strategy didn't fire enough to evaluate; not a failure of
        # the prediction (could just be a quiet day).
        return None
    pnl = actual["net_pnl"]
    if expectation == "non_negative":
        return pnl >= 0
    if expectation == "negative":
        return pnl < 0
    return None


def reconcile(date_str: str, data_root: Path) -> dict:
    """Read the morning capture + daily summary; produce calibration."""
    capture_path = data_root / "logs" / "quin_roadmap" / f"{date_str}.json"
    if not capture_path.exists():
        raise FileNotFoundError(f"no morning capture at {capture_path}")
    capture_data = json.loads(capture_path.read_text(encoding="utf-8"))
    summary_path = data_root / "out" / f"daily_summary_{date_str}.md"
    daily = parse_daily_summary(summary_path)

    fits = capture_data.get("predicted_strategy_fit", {})
    per_strat: dict = {}
    consistent_count = 0
    evaluable_count = 0
    for strat, fit in fits.items():
        actual = actual_pnl_for_strategy(daily, strat)
        cons = is_consistent(fit, actual)
        per_strat[strat] = {
            **actual,
            "predicted_fit": fit,
            "consistent": cons,
        }
        if cons is True:
            consistent_count += 1
            evaluable_count += 1
        elif cons is False:
            evaluable_count += 1
    calibration_score = (
        consistent_count / evaluable_count if evaluable_count > 0 else None
    )

    # Range / regime checks need user-supplied actuals — for now we
    # leave them as None placeholders; future hook can read NT8 OHLC.
    out_data = {
        "date": date_str,
        "reconciled_ts_ct": datetime.now(CT).isoformat(timespec="seconds"),
        "predicted_range":      capture_data.get("predicted_range"),
        "actual_range":         None,
        "predicted_range_held": None,
        "regime_held_through_session": None,
        "hvl_held": None,
        "per_strategy_actual":  per_strat,
        "evaluable_count":      evaluable_count,
        "consistent_count":     consistent_count,
        "calibration_score":    calibration_score,
    }
    out_path = data_root / "out" / f"quin_calibration_{date_str}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_data, indent=2, default=str),
                        encoding="utf-8")

    # Append a row to the running CSV
    csv_path = data_root / "out" / "quin_phoenix_calibration.csv"
    csv_exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not csv_exists:
            w.writerow([
                "date", "regime", "net_gex_M", "iv_30d_pct",
                "predicted_range_low", "predicted_range_high",
                "predicted_range_prob", "evaluable_strategies",
                "consistent_strategies", "calibration_score",
            ])
        w.writerow([
            date_str,
            capture_data.get("regime", "?"),
            capture_data.get("net_gex_M", ""),
            capture_data.get("iv_30d_pct", ""),
            (capture_data.get("predicted_range") or {}).get("low", ""),
            (capture_data.get("predicted_range") or {}).get("high", ""),
            (capture_data.get("predicted_range") or {}).get("probability", ""),
            evaluable_count,
            consistent_count,
            f"{calibration_score:.3f}" if calibration_score is not None else "",
        ])

    return out_data


# ─── summary phase ───────────────────────────────────────────────────

def summary(data_root: Path, last_n: int = 10) -> str:
    """Pretty-print the trailing N calibrations from the running CSV."""
    csv_path = data_root / "out" / "quin_phoenix_calibration.csv"
    if not csv_path.exists():
        return "No calibration history yet. Run --reconcile after a session."
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    if not rows:
        return "Calibration CSV exists but is empty."
    L = [
        f"Last {min(last_n, len(rows))} calibration row(s):",
        f"{'date':<12} {'regime':<18} {'gex':>6} {'iv':>5} "
        f"{'range':>14} {'eval':>5} {'cons':>5} {'score':>6}",
    ]
    for r in rows[-last_n:]:
        rng = f"{r.get('predicted_range_low','')}-{r.get('predicted_range_high','')}"
        L.append(
            f"{r.get('date',''):<12} {r.get('regime',''):<18} "
            f"{r.get('net_gex_M',''):>6} {r.get('iv_30d_pct',''):>5} "
            f"{rng:>14} {r.get('evaluable_strategies',''):>5} "
            f"{r.get('consistent_strategies',''):>5} "
            f"{r.get('calibration_score',''):>6}"
        )
    # Aggregate score across the trailing window
    scores = [float(r["calibration_score"]) for r in rows[-last_n:]
              if r.get("calibration_score") not in (None, "", "None")]
    if scores:
        avg = sum(scores) / len(scores)
        L.append("")
        L.append(f"Trailing-{len(scores)} avg calibration score: {avg:.3f}")
        L.append("  (>= 0.70 over 10+ days suggests regime classifier deserves authority)")
    return "\n".join(L)


# ─── main ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture", action="store_true",
                    help="Capture today's roadmap from --input")
    ap.add_argument("--input",
                    help="Path to JSON file with today's QUIN roadmap (for --capture)")
    ap.add_argument("--reconcile", action="store_true",
                    help="Reconcile predictions against actuals")
    ap.add_argument("--date",
                    help="Date YYYY-MM-DD; defaults to today (CT)")
    ap.add_argument("--summary", action="store_true",
                    help="Print trailing calibration summary and exit")
    ap.add_argument("--last-n", type=int, default=10,
                    help="--summary window size (default 10)")
    args = ap.parse_args()

    data_root = _data_root()
    date_str = args.date or datetime.now(CT).date().isoformat()

    if args.summary:
        print(summary(data_root, last_n=args.last_n))
        return 0

    if not args.capture and not args.reconcile:
        ap.print_help()
        return 1

    if args.capture:
        if not args.input:
            print("ERROR: --capture requires --input <path>")
            return 2
        try:
            out = capture(Path(args.input), data_root)
            print(f"CAPTURED -> {out}")
        except Exception as e:
            print(f"CAPTURE FAILED: {e}")
            return 3

    if args.reconcile:
        try:
            result = reconcile(date_str, data_root)
            cs = result.get("calibration_score")
            cs_str = f"{cs:.2f}" if cs is not None else "n/a (no evaluable strategies)"
            print(
                f"RECONCILED -> out/quin_calibration_{date_str}.json\n"
                f"  evaluable strategies:   {result['evaluable_count']}\n"
                f"  consistent w/ predict:  {result['consistent_count']}\n"
                f"  calibration score:      {cs_str}"
            )
        except Exception as e:
            print(f"RECONCILE FAILED: {e}")
            return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
