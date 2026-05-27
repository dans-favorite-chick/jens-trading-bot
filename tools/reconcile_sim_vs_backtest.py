"""
P1-1 Stage 2 — Sim vs Backtest Reconciliation Harness
======================================================

For each persisted sim trade in a given strategy + date window, this tool
re-runs the deterministic backtester's enrichment + strategy-evaluation
pipeline at the same minute as the live sim signal and compares:

    direction, entry_price, stop_price, target_price, exit_reason, net_pnl

It is intentionally narrow:

  - READ-ONLY against logs/, data/, strategies/, base_bot, oif_writer.
  - Writes ONLY to out/reconciliation_<date>_<strategy>.md (+ a JSON sidecar
    when --json is passed).
  - Does NOT place orders. Does NOT touch trade_memory*.json.

The strategy-blocking gap identified in Stage 1 (`day_type`, `cr_verdict`/
`cr_mom_score`, `cvd_health`/`cvd_health_short`, `intermarket`/`es_nq_rs`)
is handled by classifying each sim trade as either:

  - REPLAYED — backtester fired a signal in the same direction within
    the entry_time tolerance, with comparable entry/stop;
  - SIM_ONLY — sim recorded a trade but backtester emitted no signal in
    the lookahead window;
  - BACKTEST_ONLY — backtester fired but sim has no matching trade;
  - BLOCKED — the sim record was missing a field the harness cannot
    safely default (currently the 4 listed above).

Exit code: 0 if every REPLAYED pair is within tolerance AND the blocked
count is 0; non-zero otherwise.

Wrapper note
------------
The backtester's evaluation entry point IS callable in isolation —
`tools/phoenix_real_backtest.py::CSVEnrichmentPipeline` plus
`strategies.bias_momentum.BiasMomentumFollow(cfg).evaluate(...)`. The
pipeline yields per-1m-bar enriched market dicts and bar windows. We
iterate it bounded to a small lookaround window per sim trade and
capture the first signal whose direction matches the sim record. This
is the "honest stub" path — it inherits the backtester's known
stubs/approximations documented in `tools/phoenix_real_backtest.py`
top-of-file (CVD approx, MACD stub, DOM stub, opening-type approx,
day_type defaulted to BALANCED). Where the backtester defaults a
strategy-blocking field, the harness counts the trade as BLOCKED, not
REPLAYED, so we never falsely claim agreement on a stubbed input.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.trade_memory import load_all_trades  # canonical reader
from config.settings import TICK_SIZE

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("reconcile")
logger.setLevel(logging.INFO)

# MNQ dollar value per tick per contract (from config/settings.py).
TICK_VALUE = 0.50

# Fields whose absence on the sim trade record (Stage-1 gap list) means
# we cannot deterministically reconstruct the strategy's branch logic.
# These are recoverable by enriching the live snapshot (Stage 1 §7); for
# now their absence trips the BLOCKED bucket.
STRATEGY_BLOCKING_FIELDS = (
    "day_type",
    "cr_verdict",
    "cvd_health",
    "es_nq_rs",
)

# Strategies that branch on these fields (Stage 1 §3). For other
# strategies the blocking set may be narrower; we keep the same list
# until each strategy is audited.
PER_STRATEGY_BLOCKING = {
    "bias_momentum": STRATEGY_BLOCKING_FIELDS,
}


# ════════════════════════════════════════════════════════════════════
# Trade record helpers
# ════════════════════════════════════════════════════════════════════

def _parse_trade_dt(t: dict) -> Optional[datetime]:
    """Return entry_time as a UTC datetime, or None if unparseable.

    Handles both epoch float (newer) and ISO string (older) forms. See
    the trade_memory canonical-reader gotcha in memory/.
    """
    et = t.get("entry_time")
    if et is None:
        return None
    if isinstance(et, (int, float)):
        try:
            return datetime.fromtimestamp(float(et), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        import dateutil.parser as dp
        return dp.parse(str(et)).astimezone(timezone.utc)
    except Exception:
        return None


def _parse_exit_dt(t: dict) -> Optional[datetime]:
    et = t.get("exit_time")
    if et is None:
        return None
    if isinstance(et, (int, float)):
        try:
            return datetime.fromtimestamp(float(et), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        import dateutil.parser as dp
        return dp.parse(str(et)).astimezone(timezone.utc)
    except Exception:
        return None


def _sim_net_pnl(t: dict) -> float:
    """Prefer net P&L; fall back to gross then pnl_dollars."""
    for k in ("pnl_dollars_net", "pnl_dollars", "pnl_dollars_gross", "gross_pnl"):
        v = t.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _blocking_field_status(t: dict, strategy: str) -> list[str]:
    """Return the list of strategy-blocking fields MISSING from the
    sim trade's market_snapshot. Empty list => not blocked."""
    blockers = PER_STRATEGY_BLOCKING.get(strategy, STRATEGY_BLOCKING_FIELDS)
    ms = t.get("market_snapshot") or {}
    missing = []
    for f in blockers:
        if f not in ms or ms.get(f) in (None, ""):
            missing.append(f)
    return missing


def load_sim_trades(
    strategy: str,
    since: datetime,
    until: datetime,
    bot_id: Optional[str] = "sim",
    logs_dir: str = "logs",
) -> list[dict]:
    """Load trades via the canonical reader, filtered to one strategy
    and entry_time window."""
    all_trades = load_all_trades(logs_dir=logs_dir)
    out = []
    for t in all_trades:
        if t.get("strategy") != strategy:
            continue
        if bot_id is not None and t.get("bot_id") != bot_id:
            continue
        dt = _parse_trade_dt(t)
        if dt is None or dt < since or dt > until:
            continue
        out.append(t)
    out.sort(key=lambda x: _parse_trade_dt(x) or datetime.min.replace(tzinfo=timezone.utc))
    return out


# ════════════════════════════════════════════════════════════════════
# Backtest replay (per-trade window)
# ════════════════════════════════════════════════════════════════════

@dataclass
class BacktestReplayResult:
    fired: bool
    direction: Optional[str] = None
    entry_ts: Optional[pd.Timestamp] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    exit_ts: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_dollars: float = 0.0
    pnl_ticks: int = 0
    reason: str = ""  # human-readable status when not fired


def _replay_one_trade(
    sim_trade: dict,
    pipeline_factory,
    strategy_obj,
    strategy_name: str,
    lookaround_min: int = 5,
) -> BacktestReplayResult:
    """Re-run the backtester pipeline across a small window around the
    sim trade's entry timestamp and capture the first matching signal.

    A fresh CSVEnrichmentPipeline is constructed per trade (well, per
    cached call — `pipeline_factory` may slice an in-memory cache) so
    warmup state is consistent and isolated. Correctness is paramount
    for Stage 2.
    """
    from tools.phoenix_real_backtest import simulate_trade

    sim_dt = _parse_trade_dt(sim_trade)
    if sim_dt is None:
        return BacktestReplayResult(fired=False, reason="sim entry_time unparseable")

    # Pipeline window: 4 hours of warmup before the sim signal, + a few
    # minutes after for the matching loop. 4h ~= 240 1m bars > the
    # backtester's default 100-bar warmup.
    pipeline_start = sim_dt - pd.Timedelta(hours=4)
    pipeline_end = sim_dt + pd.Timedelta(minutes=lookaround_min + 1)

    pipeline = pipeline_factory(
        start=pipeline_start,
        end=pipeline_end,
    )

    sim_direction = (sim_trade.get("direction") or "").upper()
    match_window_lo = sim_dt - pd.Timedelta(seconds=60 * lookaround_min)
    match_window_hi = sim_dt + pd.Timedelta(seconds=60 * lookaround_min)

    first_signal = None
    first_signal_ts = None
    first_signal_market = None
    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        if eval_ts < match_window_lo:
            continue
        if eval_ts > match_window_hi:
            break
        try:
            sig = strategy_obj.evaluate(market, bars_5m, bars_1m, session_info)
        except Exception as e:
            logger.debug(f"replay eval error at {eval_ts}: {e!r}")
            continue
        if sig is None:
            continue
        if sim_direction and sig.direction != sim_direction:
            # Backtest fired the opposite direction in-window — record
            # it as a non-match (BACKTEST_ONLY contender, but not a
            # direction-matched replay).
            if first_signal is None:
                first_signal = sig
                first_signal_ts = eval_ts
                first_signal_market = market
            continue
        # Direction-matched signal — use this.
        first_signal = sig
        first_signal_ts = eval_ts
        first_signal_market = market
        break

    if first_signal is None:
        return BacktestReplayResult(
            fired=False,
            reason=f"no signal in ±{lookaround_min}m window around {sim_dt}",
        )

    # Resolve entry/stop/target the same way run_backtest() does.
    entry_price = first_signal.entry_price if first_signal.entry_price else first_signal_market["price"]
    if first_signal.stop_price is not None and first_signal.target_price is not None:
        stop_price = first_signal.stop_price
        target_price = first_signal.target_price
    else:
        stop_dist = first_signal.stop_ticks * TICK_SIZE
        if first_signal.direction == "LONG":
            stop_price = entry_price - stop_dist
            target_price = entry_price + stop_dist * first_signal.target_rr
        else:
            stop_price = entry_price + stop_dist
            target_price = entry_price - stop_dist * first_signal.target_rr

    # Simulate the trade against the same MNQ 1m dataframe the pipeline
    # loaded (cached on the pipeline instance).
    tr = simulate_trade(
        signal_strategy=strategy_name,
        signal_direction=first_signal.direction,
        entry_ts=first_signal_ts,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        mnq_1m_df=pipeline.mnq_1m_df,
    )

    return BacktestReplayResult(
        fired=True,
        direction=first_signal.direction,
        entry_ts=first_signal_ts,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        exit_ts=tr.exit_ts,
        exit_price=tr.exit_price,
        exit_reason=tr.exit_reason,
        pnl_dollars=tr.pnl_dollars,
        pnl_ticks=tr.pnl_ticks,
    )


# ════════════════════════════════════════════════════════════════════
# Comparison + classification
# ════════════════════════════════════════════════════════════════════

@dataclass
class ComparisonRow:
    trade_id: str
    sim_entry_dt: str
    sim_direction: str
    classification: str  # REPLAYED | SIM_ONLY | BACKTEST_ONLY | BLOCKED | ERROR
    sim_entry_price: float = 0.0
    sim_stop_price: float = 0.0
    sim_exit_reason: str = ""
    sim_net_pnl: float = 0.0
    bt_direction: Optional[str] = None
    bt_entry_dt: Optional[str] = None
    bt_entry_price: Optional[float] = None
    bt_stop_price: Optional[float] = None
    bt_exit_reason: Optional[str] = None
    bt_pnl_dollars: Optional[float] = None
    delta_entry_seconds: Optional[float] = None
    delta_entry_ticks: Optional[float] = None
    delta_stop_ticks: Optional[float] = None
    delta_pnl_pct: Optional[float] = None
    delta_pnl_abs: Optional[float] = None
    exit_reason_match: Optional[bool] = None
    within_tolerance: Optional[bool] = None
    notes: list[str] = field(default_factory=list)


def compare_one(
    sim_trade: dict,
    replay: BacktestReplayResult,
    tolerances: dict,
    strategy: str,
) -> ComparisonRow:
    sim_dt = _parse_trade_dt(sim_trade)
    sim_dir = (sim_trade.get("direction") or "").upper()
    sim_entry = float(sim_trade.get("entry_price") or 0.0)
    sim_stop = float(sim_trade.get("stop_price") or 0.0)
    sim_exit = (sim_trade.get("exit_reason") or "").lower()
    sim_pnl = _sim_net_pnl(sim_trade)

    row = ComparisonRow(
        trade_id=sim_trade.get("trade_id") or "<no-id>",
        sim_entry_dt=sim_dt.isoformat() if sim_dt else "<unparseable>",
        sim_direction=sim_dir,
        classification="ERROR",
        sim_entry_price=sim_entry,
        sim_stop_price=sim_stop,
        sim_exit_reason=sim_exit,
        sim_net_pnl=sim_pnl,
    )

    # Blocking check first — even if replay fired, the sim record might
    # not have enough info to know whether the strategy SHOULD have
    # taken the same branch. Flag as BLOCKED so we don't claim agreement
    # on a missing input.
    missing = _blocking_field_status(sim_trade, strategy)
    if missing:
        row.classification = "BLOCKED"
        row.notes.append(f"missing strategy-blocking fields: {','.join(missing)}")
        # Still record the replay outcome (if any) for diagnostic value.
        if replay.fired:
            row.bt_direction = replay.direction
            row.bt_entry_dt = replay.entry_ts.isoformat() if replay.entry_ts is not None else None
            row.bt_entry_price = replay.entry_price
            row.bt_stop_price = replay.stop_price
            row.bt_exit_reason = replay.exit_reason
            row.bt_pnl_dollars = replay.pnl_dollars
        return row

    if not replay.fired:
        row.classification = "SIM_ONLY"
        row.notes.append(replay.reason or "backtester emitted no signal")
        return row

    row.bt_direction = replay.direction
    row.bt_entry_dt = replay.entry_ts.isoformat() if replay.entry_ts is not None else None
    row.bt_entry_price = replay.entry_price
    row.bt_stop_price = replay.stop_price
    row.bt_exit_reason = replay.exit_reason
    row.bt_pnl_dollars = replay.pnl_dollars

    # Direction mismatch (backtester fired the opposite way in-window)
    if replay.direction != sim_dir:
        row.classification = "BACKTEST_ONLY"
        row.notes.append(f"backtest direction={replay.direction} vs sim {sim_dir}")
        return row

    # Direction-matched: compute deltas and apply tolerances.
    row.classification = "REPLAYED"

    if sim_dt is not None and replay.entry_ts is not None:
        row.delta_entry_seconds = abs(
            (pd.Timestamp(sim_dt) - replay.entry_ts).total_seconds()
        )
    if sim_entry > 0 and replay.entry_price is not None:
        row.delta_entry_ticks = abs(sim_entry - replay.entry_price) / TICK_SIZE
    if sim_stop > 0 and replay.stop_price is not None:
        row.delta_stop_ticks = abs(sim_stop - replay.stop_price) / TICK_SIZE
    if replay.pnl_dollars is not None:
        row.delta_pnl_abs = abs(sim_pnl - replay.pnl_dollars)
        if abs(sim_pnl) > 0.01:
            row.delta_pnl_pct = (row.delta_pnl_abs / abs(sim_pnl)) * 100.0
        else:
            # Sim P&L ≈ 0; report a sentinel rather than divide by zero.
            row.delta_pnl_pct = None

    row.exit_reason_match = (sim_exit == (replay.exit_reason or "").lower())

    # Tolerance gates.
    in_tol = True
    tol_entry_s = float(tolerances.get("entry_time_seconds", 60))
    tol_entry_t = float(tolerances.get("entry_price_ticks", 2))
    tol_stop_t = float(tolerances.get("stop_price_ticks", 2))
    tol_exit_must = bool(tolerances.get("exit_reason_must_match", False))
    tol_pnl_pct = float(tolerances.get("net_pnl_pct", 25.0))

    if row.delta_entry_seconds is not None and row.delta_entry_seconds > tol_entry_s:
        in_tol = False
        row.notes.append(f"entry_time delta {row.delta_entry_seconds:.0f}s > {tol_entry_s}s")
    if row.delta_entry_ticks is not None and row.delta_entry_ticks > tol_entry_t:
        in_tol = False
        row.notes.append(f"entry_price delta {row.delta_entry_ticks:.1f}t > {tol_entry_t}t")
    if row.delta_stop_ticks is not None and row.delta_stop_ticks > tol_stop_t:
        in_tol = False
        row.notes.append(f"stop_price delta {row.delta_stop_ticks:.1f}t > {tol_stop_t}t")
    if tol_exit_must and row.exit_reason_match is False:
        in_tol = False
        row.notes.append(f"exit_reason {sim_exit} != {replay.exit_reason}")
    if (row.delta_pnl_pct is not None
            and row.delta_pnl_pct > tol_pnl_pct):
        in_tol = False
        row.notes.append(f"pnl delta {row.delta_pnl_pct:.0f}% > {tol_pnl_pct}%")

    row.within_tolerance = in_tol
    return row


# ════════════════════════════════════════════════════════════════════
# Reporting
# ════════════════════════════════════════════════════════════════════

def _summarize(rows: list[ComparisonRow]) -> dict:
    out = {
        "total": len(rows),
        "replayed": 0,
        "sim_only": 0,
        "backtest_only": 0,
        "blocked": 0,
        "error": 0,
        "within_tolerance": 0,
        "outside_tolerance": 0,
        "blocking_field_counts": {},
        # Informational counts on BLOCKED trades (backtester still ran).
        "blocked_with_backtest_signal": 0,
        "blocked_no_backtest_signal": 0,
        "blocked_direction_match": 0,
        "blocked_direction_mismatch": 0,
    }
    deltas_entry_s = []
    deltas_entry_t = []
    deltas_stop_t = []
    deltas_pnl_pct = []
    deltas_pnl_abs = []
    # Informational deltas across BLOCKED-but-direction-matched trades.
    info_dt_e = []
    info_de_t = []
    info_ds_t = []
    info_dpnl_pct = []
    info_dpnl_abs = []
    for r in rows:
        c = r.classification
        if c == "REPLAYED":
            out["replayed"] += 1
            if r.within_tolerance:
                out["within_tolerance"] += 1
            else:
                out["outside_tolerance"] += 1
            if r.delta_entry_seconds is not None:
                deltas_entry_s.append(r.delta_entry_seconds)
            if r.delta_entry_ticks is not None:
                deltas_entry_t.append(r.delta_entry_ticks)
            if r.delta_stop_ticks is not None:
                deltas_stop_t.append(r.delta_stop_ticks)
            if r.delta_pnl_pct is not None:
                deltas_pnl_pct.append(r.delta_pnl_pct)
            if r.delta_pnl_abs is not None:
                deltas_pnl_abs.append(r.delta_pnl_abs)
        elif c == "SIM_ONLY":
            out["sim_only"] += 1
        elif c == "BACKTEST_ONLY":
            out["backtest_only"] += 1
        elif c == "BLOCKED":
            out["blocked"] += 1
            for note in r.notes:
                if note.startswith("missing strategy-blocking fields:"):
                    fields = note.split(":", 1)[1].strip().split(",")
                    for f in fields:
                        f = f.strip()
                        out["blocking_field_counts"][f] = (
                            out["blocking_field_counts"].get(f, 0) + 1
                        )
            # Informational view: did the backtester fire anyway?
            if r.bt_direction is not None:
                out["blocked_with_backtest_signal"] += 1
                if r.bt_direction == r.sim_direction:
                    out["blocked_direction_match"] += 1
                    # Compute informational deltas.
                    try:
                        import dateutil.parser as dp
                        sd = dp.parse(r.sim_entry_dt)
                        bd = dp.parse(r.bt_entry_dt) if r.bt_entry_dt else None
                        if bd is not None:
                            info_dt_e.append(abs((sd - bd).total_seconds()))
                    except Exception:
                        pass
                    if r.sim_entry_price and r.bt_entry_price:
                        info_de_t.append(
                            abs(r.sim_entry_price - r.bt_entry_price) / TICK_SIZE
                        )
                    if r.sim_stop_price and r.bt_stop_price:
                        info_ds_t.append(
                            abs(r.sim_stop_price - r.bt_stop_price) / TICK_SIZE
                        )
                    if r.bt_pnl_dollars is not None:
                        info_dpnl_abs.append(abs(r.sim_net_pnl - r.bt_pnl_dollars))
                        if abs(r.sim_net_pnl) > 0.5:
                            info_dpnl_pct.append(
                                abs(r.sim_net_pnl - r.bt_pnl_dollars)
                                / abs(r.sim_net_pnl) * 100.0
                            )
                else:
                    out["blocked_direction_mismatch"] += 1
            else:
                out["blocked_no_backtest_signal"] += 1
        else:
            out["error"] += 1

    def _pct(arr, p):
        if not arr:
            return None
        arr = sorted(arr)
        k = max(0, min(len(arr) - 1, int(round(p / 100.0 * (len(arr) - 1)))))
        return arr[k]

    def _stats(arr):
        if not arr:
            return None
        return {
            "n": len(arr),
            "mean": sum(arr) / len(arr),
            "p50": _pct(arr, 50),
            "p90": _pct(arr, 90),
            "max": max(arr),
        }

    out["deltas"] = {
        "entry_seconds": _stats(deltas_entry_s),
        "entry_ticks": _stats(deltas_entry_t),
        "stop_ticks": _stats(deltas_stop_t),
        "pnl_pct": _stats(deltas_pnl_pct),
        "pnl_abs_dollars": _stats(deltas_pnl_abs),
    }
    out["informational_deltas"] = {
        "entry_seconds": _stats(info_dt_e),
        "entry_ticks": _stats(info_de_t),
        "stop_ticks": _stats(info_ds_t),
        "pnl_pct": _stats(info_dpnl_pct),
        "pnl_abs_dollars": _stats(info_dpnl_abs),
    }
    return out


def _format_stats(s: Optional[dict], unit: str = "") -> str:
    if not s:
        return "n/a"
    return (
        f"n={s['n']} mean={s['mean']:.2f}{unit} "
        f"p50={s['p50']:.2f}{unit} p90={s['p90']:.2f}{unit} max={s['max']:.2f}{unit}"
    )


def build_report(
    strategy: str,
    since: datetime,
    until: datetime,
    tolerances: dict,
    rows: list[ComparisonRow],
    summary: dict,
) -> str:
    lines: list[str] = []
    lines.append(f"# Reconciliation Report — {strategy}")
    lines.append("")
    lines.append(f"- **Date generated:** {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- **Strategy:** `{strategy}`")
    lines.append(f"- **Window:** {since.date()} → {until.date()} (UTC)")
    lines.append(f"- **Total sim trades evaluated:** {summary['total']}")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    repl = summary["replayed"]
    in_tol = summary["within_tolerance"]
    out_tol = summary["outside_tolerance"]
    blk = summary["blocked"]
    so = summary["sim_only"]
    bo = summary["backtest_only"]
    err = summary["error"]

    if repl > 0:
        pct_in_tol = in_tol / repl * 100.0
    else:
        pct_in_tol = 0.0

    if repl > 0 and out_tol == 0 and blk == 0:
        verdict = "PASS — every direction-matched replay is within tolerance and no trades were blocked."
    elif repl == 0 and blk == summary["total"]:
        verdict = "BLOCKED — no trades could be replayed because all are missing strategy-blocking fields."
    elif repl == 0:
        verdict = "FAIL — backtester emitted zero direction-matched signals across the sim window."
    else:
        verdict = (
            f"FAIL — {out_tol}/{repl} direction-matched replays exceed tolerance, "
            f"{blk} trades blocked by missing fields."
        )
    lines.append(f"- **Verdict:** {verdict}")
    lines.append(f"- **Replayed (direction-matched):** {repl} / {summary['total']} "
                 f"({(repl/summary['total']*100.0 if summary['total'] else 0):.1f}%)")
    lines.append(f"- **Within tolerance:** {in_tol} / {repl} ({pct_in_tol:.1f}% of replays)")
    lines.append(f"- **Outside tolerance:** {out_tol}")
    lines.append(f"- **Blocked (missing fields):** {blk}")
    lines.append(f"- **Sim-only (backtest emitted no signal):** {so}")
    lines.append(f"- **Backtest-only (opposite-direction signal in window):** {bo}")
    if err:
        lines.append(f"- **Errors:** {err}")
    lines.append("")

    lines.append("## Tolerance Configuration")
    lines.append("")
    lines.append("```yaml")
    lines.append(yaml.safe_dump(tolerances, sort_keys=True).rstrip())
    lines.append("```")
    lines.append("")

    lines.append("## Divergence Stats (REPLAYED trades only)")
    lines.append("")
    d = summary["deltas"]
    lines.append(f"- **Entry time delta:** {_format_stats(d['entry_seconds'], 's')}")
    lines.append(f"- **Entry price delta:** {_format_stats(d['entry_ticks'], 't')}")
    lines.append(f"- **Stop price delta:** {_format_stats(d['stop_ticks'], 't')}")
    lines.append(f"- **Net P&L delta (% of sim):** {_format_stats(d['pnl_pct'], '%')}")
    lines.append(f"- **Net P&L delta (abs $):** {_format_stats(d['pnl_abs_dollars'], '$')}")
    lines.append("")

    if summary["blocking_field_counts"]:
        lines.append("## Strategy-Blocking Field Impact")
        lines.append("")
        lines.append("Counts of trades blocked because the listed market_snapshot field was missing:")
        lines.append("")
        for f, n in sorted(summary["blocking_field_counts"].items(),
                            key=lambda x: -x[1]):
            lines.append(f"- `{f}`: {n}")
        lines.append("")

    # Even when every trade is BLOCKED, the backtester still produces a
    # replay output we can compare against. The numbers don't represent
    # "agreement" (we can't know which branch the live strategy took)
    # but they're a useful upper bound on the divergence we'd see if
    # the missing fields turn out to be irrelevant. Include them so the
    # operator sees the order of magnitude.
    if summary.get("blocked_with_backtest_signal", 0) > 0:
        lines.append("## Informational Divergence (BLOCKED trades, backtester ran anyway)")
        lines.append("")
        lines.append(
            f"- **Blocked trades where backtest fired a signal:** "
            f"{summary['blocked_with_backtest_signal']} / {summary['blocked']}"
        )
        lines.append(
            f"- **Blocked trades where backtest emitted no signal:** "
            f"{summary['blocked_no_backtest_signal']}"
        )
        lines.append(
            f"- **Direction-matched (informational):** "
            f"{summary['blocked_direction_match']}"
        )
        lines.append(
            f"- **Direction-mismatched (informational):** "
            f"{summary['blocked_direction_mismatch']}"
        )
        lines.append("")
        lines.append("Divergence stats across direction-matched BLOCKED trades "
                     "(treats the backtester output as a comparison anchor — "
                     "NOT a tolerance pass; many of these likely take a "
                     "different code branch in live sim because of the "
                     "missing strategy-blocking fields):")
        lines.append("")
        id_ = summary.get("informational_deltas", {})
        lines.append(f"- **Entry time delta:** {_format_stats(id_.get('entry_seconds'), 's')}")
        lines.append(f"- **Entry price delta:** {_format_stats(id_.get('entry_ticks'), 't')}")
        lines.append(f"- **Stop price delta:** {_format_stats(id_.get('stop_ticks'), 't')}")
        lines.append(f"- **Net P&L delta (% of sim):** {_format_stats(id_.get('pnl_pct'), '%')}")
        lines.append(f"- **Net P&L delta (abs $):** {_format_stats(id_.get('pnl_abs_dollars'), '$')}")
        lines.append("")

    lines.append("## Per-Trade Detail")
    lines.append("")
    lines.append(
        "| trade_id | sim_dt (UTC) | dir | class | Δt(s) | Δentry(t) | Δstop(t) | sim_pnl$ | bt_pnl$ | ΔPnL% | in_tol | notes |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for r in rows:
        def fmt(v, fmts):
            return fmts.format(v) if v is not None else "-"
        notes = "; ".join(r.notes) if r.notes else ""
        if len(notes) > 80:
            notes = notes[:77] + "..."
        lines.append(
            f"| {r.trade_id} | {r.sim_entry_dt[:19]} | {r.sim_direction} | "
            f"{r.classification} | {fmt(r.delta_entry_seconds, '{:.0f}')} | "
            f"{fmt(r.delta_entry_ticks, '{:.1f}')} | "
            f"{fmt(r.delta_stop_ticks, '{:.1f}')} | "
            f"{r.sim_net_pnl:+.2f} | "
            f"{fmt(r.bt_pnl_dollars, '{:+.2f}')} | "
            f"{fmt(r.delta_pnl_pct, '{:.0f}')} | "
            f"{'yes' if r.within_tolerance else ('no' if r.within_tolerance is False else '-')} | "
            f"{notes} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# ════════════════════════════════════════════════════════════════════
# Main pipeline
# ════════════════════════════════════════════════════════════════════

def run(
    strategy: str,
    since: datetime,
    until: datetime,
    tolerances: dict,
    out_path: Path,
    json_path: Optional[Path] = None,
    logs_dir: str = "logs",
    lookaround_min: int = 5,
    limit: Optional[int] = None,
) -> int:
    """Top-level harness: load trades, replay each, compare, write
    report. Returns exit code (0 if all REPLAYED within tolerance AND
    no blocks; non-zero otherwise)."""
    from tools.phoenix_real_backtest import (
        CSVEnrichmentPipeline,
        instantiate_strategies,
    )

    sim_trades = load_sim_trades(
        strategy=strategy, since=since, until=until, logs_dir=logs_dir,
    )
    logger.info(
        f"loaded {len(sim_trades)} sim {strategy} trades in window "
        f"{since.date()} → {until.date()}"
    )
    if limit is not None:
        sim_trades = sim_trades[:limit]
        logger.info(f"--limit applied: keeping first {len(sim_trades)}")

    # Pre-instantiate the strategy ONCE (config is stable across replays).
    strategies = instantiate_strategies([strategy])
    if strategy not in strategies:
        logger.error(f"could not instantiate strategy '{strategy}'")
        return 2
    strat_obj = strategies[strategy]

    # Load all four CSVs ONCE up-front, then build per-trade pipelines
    # by slicing the cached DataFrames. The naive per-trade approach
    # spent ~10s reloading 1.7M rows for each trade (≈24 min for 143).
    # Slicing the cache cuts per-trade cost to <1s.
    from tools.phoenix_real_backtest import _load_bars_from_csv
    data_dir = ROOT / "data" / "historical"
    logger.info("loading CSVs once into memory (cache for all replays)...")
    cache_mnq_1m = _load_bars_from_csv(str(data_dir / "mnq_1min_databento.csv"))
    cache_mnq_5m = _load_bars_from_csv(str(data_dir / "mnq_5min_databento.csv"))
    cache_mes_1m = _load_bars_from_csv(str(data_dir / "mes_1min_databento.csv"))
    cache_mes_5m = _load_bars_from_csv(str(data_dir / "mes_5min_databento.csv"))
    logger.info(
        f"cached: MNQ1m={len(cache_mnq_1m):,} MNQ5m={len(cache_mnq_5m):,} "
        f"MES1m={len(cache_mes_1m):,} MES5m={len(cache_mes_5m):,}"
    )

    def _factory(start, end):
        """Build a CSVEnrichmentPipeline by slicing cached DataFrames.

        Bypasses the constructor's CSV-load path by constructing the
        instance and assigning pre-sliced DataFrames + fresh
        EnrichmentState. This must mirror the constructor's behavior
        verbatim — if the upstream pipeline grows new init steps,
        update here too.
        """
        from tools.phoenix_real_backtest import EnrichmentState
        def _to_utc_ts(x):
            if isinstance(x, pd.Timestamp):
                return x.tz_convert("UTC") if x.tzinfo is not None else x.tz_localize("UTC")
            if isinstance(x, datetime):
                if x.tzinfo is None:
                    return pd.Timestamp(x, tz="UTC")
                return pd.Timestamp(x).tz_convert("UTC")
            # String path
            return pd.Timestamp(x, tz="UTC")
        start_ts = _to_utc_ts(start)
        end_ts = _to_utc_ts(end)
        pipe = CSVEnrichmentPipeline.__new__(CSVEnrichmentPipeline)
        pipe.mnq_1m_df = cache_mnq_1m[(cache_mnq_1m.ts >= start_ts) & (cache_mnq_1m.ts <= end_ts)].reset_index(drop=True)
        pipe.mnq_5m_df = cache_mnq_5m[(cache_mnq_5m.ts >= start_ts) & (cache_mnq_5m.ts <= end_ts)].reset_index(drop=True)
        pipe.mes_1m_df = cache_mes_1m[(cache_mes_1m.ts >= start_ts) & (cache_mes_1m.ts <= end_ts)].reset_index(drop=True)
        pipe.mes_5m_df = cache_mes_5m[(cache_mes_5m.ts >= start_ts) & (cache_mes_5m.ts <= end_ts)].reset_index(drop=True)
        pipe.mnq = EnrichmentState()
        pipe.mes = EnrichmentState()
        return pipe

    rows: list[ComparisonRow] = []
    for i, t in enumerate(sim_trades, 1):
        if i % 10 == 0 or i == 1:
            logger.info(f"replaying {i}/{len(sim_trades)}: trade_id={t.get('trade_id')}")
        try:
            # Re-instantiate strategy to clear any internal _last_reject state.
            strat_obj_fresh = instantiate_strategies([strategy])[strategy]
            replay = _replay_one_trade(
                sim_trade=t,
                pipeline_factory=_factory,
                strategy_obj=strat_obj_fresh,
                strategy_name=strategy,
                lookaround_min=lookaround_min,
            )
        except Exception as e:
            logger.warning(f"replay error trade {t.get('trade_id')}: {e!r}")
            replay = BacktestReplayResult(fired=False, reason=f"replay exception: {e!r}")

        row = compare_one(
            sim_trade=t, replay=replay, tolerances=tolerances, strategy=strategy,
        )
        rows.append(row)

    summary = _summarize(rows)

    report = build_report(
        strategy=strategy, since=since, until=until,
        tolerances=tolerances, rows=rows, summary=summary,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    logger.info(f"wrote report -> {out_path}")

    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(
                {
                    "strategy": strategy,
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "tolerances": tolerances,
                    "summary": summary,
                    "rows": [asdict(r) for r in rows],
                },
                indent=2, default=str,
            ),
            encoding="utf-8",
        )
        logger.info(f"wrote json sidecar -> {json_path}")

    # Exit code: 0 only when EVERY replay is in tolerance AND zero blocks.
    if summary["outside_tolerance"] == 0 and summary["blocked"] == 0 and summary["error"] == 0:
        return 0
    return 1


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD as a UTC datetime at midnight."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser(
        description="Reconcile sim trades against the deterministic backtester.",
    )
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD (inclusive, UTC)")
    ap.add_argument("--until", required=True, help="YYYY-MM-DD (inclusive, UTC)")
    ap.add_argument(
        "--tolerance-config",
        default="tests/reconciliation_tolerances.yaml",
        help="Path to tolerance YAML",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Output report path. Default: out/reconciliation_<today>_<strategy>.md",
    )
    ap.add_argument(
        "--json",
        default=None,
        help="Optional JSON sidecar path with full per-row data.",
    )
    ap.add_argument(
        "--logs-dir", default="logs", help="Override logs/ directory (tests).",
    )
    ap.add_argument(
        "--lookaround-min",
        type=int, default=5,
        help="±minutes around sim entry to search for backtest signal.",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Only replay first N trades (smoke runs).",
    )
    args = ap.parse_args()

    since = _parse_date(args.since)
    # Until is end-of-day UTC for inclusivity.
    until = _parse_date(args.until).replace(hour=23, minute=59, second=59)

    tol_path = Path(args.tolerance_config)
    if not tol_path.is_absolute():
        tol_path = ROOT / tol_path
    with open(tol_path, "r", encoding="utf-8") as f:
        tolerances = yaml.safe_load(f) or {}

    out_path = (
        Path(args.out) if args.out
        else ROOT / "out" / f"reconciliation_{datetime.now().strftime('%Y-%m-%d')}_{args.strategy}.md"
    )
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    json_path = None
    if args.json:
        json_path = Path(args.json)
        if not json_path.is_absolute():
            json_path = ROOT / json_path

    rc = run(
        strategy=args.strategy,
        since=since,
        until=until,
        tolerances=tolerances,
        out_path=out_path,
        json_path=json_path,
        logs_dir=args.logs_dir,
        lookaround_min=args.lookaround_min,
        limit=args.limit,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
