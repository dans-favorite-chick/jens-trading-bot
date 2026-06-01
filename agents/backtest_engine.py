#!/usr/bin/env python3
"""
agents/backtest_engine.py - Phase 2 backtest engine for the analytics warehouse.

Replays Databento MNQ 1m bars through Phoenix's CSVEnrichmentPipeline + real
strategy classes, applies per-session-day warmup + $45 daily-loss halt gates,
writes a friction-net trades CSV with MAE/MFE/regime/tod_bucket enrichment,
emits a warehouse sidecar, and auto-ingests into the warehouse.

USAGE
-----
    python agents/backtest_engine.py --strategy bias_momentum --days 5

ARCHITECTURE
------------
The spec's literal "import tick_aggregator.py / base_bot.py directly" rules
match a tick-replay design. Phoenix's 5y Databento corpus is 1m OHLCV bars,
not raw ticks - the bar-replay equivalent of TickAggregator already lives in
``tools/phoenix_real_backtest.CSVEnrichmentPipeline``, which produces the
same enriched ``market`` dict a strategy's ``evaluate()`` consumes. The
"strategy eval methods" that need to run unchanged live in the strategies/*
classes, not in base_bot.py (which is 2,431 lines welded to NT8/WS/OIF
runtime). This engine therefore:

  - Uses CSVEnrichmentPipeline as the bar aggregator,
  - Loads strategy classes via ``prb.instantiate_strategies``,
  - Owns its own cycle loop so the Phase 2 gates (warmup, daily-loss halt,
    per-day reset) are local rather than baked into shared infra,
  - Re-uses ``prb.simulate_trade`` for stop/target resolution (with friction),
  - Post-run: enriches with MAE/MFE/regime/tod_bucket via
    ``tools.portfolio_backtest.analytics`` (the same path the canonical
    portfolio backtest CSVs were built with).

OUTPUT
------
CSV + sidecar are written to ``data/warehouse/agent_runs/`` and auto-ingested
into the warehouse DB at ``data/warehouse/phoenix.duckdb``. The CSVs are kept
for provenance (warehouse no-delete policy).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import date as _date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

import tools.phoenix_real_backtest as prb  # noqa: E402
from tools.portfolio_backtest import paths as pb_paths  # noqa: E402

from agents.backtest_sidecar_emitter import emit_engine_sidecar  # noqa: E402

# ── Phase 2 gate constants (from spec critical_rules) ──────────────
WARMUP_BARS_PER_SESSION = 25     # 1m bars before signals allowed each session day
DAILY_LOSS_HALT_USD     = 45.0   # Halt strategy after -$45 in a single session day

# Output location: co-located with the warehouse so artifacts live with the DB.
OUT_DIR = ROOT / "data" / "warehouse" / "agent_runs"

# Lock friction ON at module-load. The engine writes friction-net pnl_dollars
# (the sidecar declares friction_applied=true to match).
prb.APPLY_EXECUTION_DECAY = True

_CT = ZoneInfo("America/Chicago")

# NOTE: agents/signal_predictor.py:DEFAULT_NUMERIC_KEYS is the consumer of
# this snapshot. When adding a key here, add it there too (and re-train).
#
# Numeric subset of the market snapshot, captured at signal-emit time, persisted
# into the trade record's entry_context JSON column. Consumed by
# agents/signal_predictor.py for training the signal-quality model.
ENTRY_CONTEXT_KEYS = (
    "atr_1m", "atr_5m", "atr_15m",
    "cvd", "cvd_session", "bar_delta",
    "vwap", "vwap_std", "vwap_upper1", "vwap_lower1",
    "ema5", "ema9", "ema21", "ema9_15m", "ema21_15m",
    "vol_climax_ratio", "avg_vol_5m",
    "tf_votes_bullish", "tf_votes_bearish",
    "dom_imbalance", "dom_bid_stack", "dom_ask_stack",
    "rsi", "macd_histogram", "macd_line", "macd_signal",
)


def _capture_entry_context(market: dict, signal) -> dict:
    """Snapshot the inference-time-knowable numeric market state for ML training.

    The returned dict goes into the trade record's entry_context JSON column and
    feeds agents/signal_predictor.py training. Adding new keys is forward
    compatible (XGBoost ignores extra columns at inference time).
    """
    out: dict = {
        # Identifying context
        "strategy":    getattr(signal, "strategy", None) or "",
        "direction":   getattr(signal, "direction", None) or "",
        "entry_score": getattr(signal, "entry_score", None),
        "stop_ticks":  getattr(signal, "stop_ticks", None),
        "target_rr":   getattr(signal, "target_rr", None),
        "confidence":  getattr(signal, "confidence", None),
    }
    for k in ENTRY_CONTEXT_KEYS:
        v = market.get(k)
        try:
            if v is None:
                out[k] = None
            elif isinstance(v, bool):
                out[k] = int(v)
            else:
                out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = None
    # market_open_minutes isn't in TickAggregator's snapshot today — we have
    # session_info now_ct via market["now_ct"]; compute the minutes-from-08:30 CT.
    nct = market.get("now_ct")
    if nct is not None:
        try:
            out["market_open_minutes"] = float(
                (nct.hour * 60 + nct.minute) - (8 * 60 + 30)
            )
        except Exception:
            out["market_open_minutes"] = None
    return out


# ────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────

def _resolve_lookback(days: int,
                      mnq_1m_df: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Last `days` of the loaded MNQ 1m corpus, anchored on the last bar."""
    end_ts   = mnq_1m_df.ts.max()
    start_ts = end_ts - pd.Timedelta(days=days)
    return start_ts, end_ts


def _session_date_ct(market: dict) -> Optional[_date]:
    nct = market.get("now_ct")
    return nct.date() if nct is not None else None


def _entry_session_date(tr: prb.TradeResult) -> _date:
    """CT session date a trade was entered in (used for daily-loss attribution)."""
    return tr.entry_ts.tz_convert(_CT).date()


def _enrich_with_mae_mfe_regime_tod(df: pd.DataFrame,
                                     mnq_1m_df: pd.DataFrame) -> pd.DataFrame:
    """Attach mae_ticks/mfe_ticks/regime/tod_bucket (canonical portfolio path).

    Failures degrade to the bare 13-col schema; the warehouse handles extended
    cols via dynamic SELECT so a missing column lands as NULL rather than an
    ingest error.
    """
    if df.empty:
        return df
    try:
        from tools.portfolio_backtest import analytics
        df = analytics.compute_mae_mfe(df, mnq_1m_df)
        regimes = analytics.classify_daily_regimes(mnq_1m_df)
        df = analytics.attach_regime(df, regimes)
        df = analytics.attach_time_of_day(df)
    except Exception as exc:
        print(f"[engine] WARN: extended-column enrichment failed: {exc!r}; "
              f"writing bare 13-col schema", file=sys.stderr)
    return df


# ────────────────────────────────────────────────────────────────────
# Engine
# ────────────────────────────────────────────────────────────────────

def run_engine(strategy_name: str, days: int, *,
               out_dir: Path = OUT_DIR,
               verbose: bool = True) -> Path:
    """Run Phase 2 backtest for one strategy over the last `days` of MNQ data.

    Writes CSV+sidecar to `out_dir`, ingests into the warehouse, returns CSV path.
    """
    # ── 1. Build pipeline + strategy ────────────────────────────────
    if not pb_paths.MNQ_1M_CSV.exists():
        raise SystemExit(
            f"[engine] MNQ 1m CSV not found at {pb_paths.MNQ_1M_CSV}. "
            f"Resolve via PHOENIX_DATA_ROOT env var (see tools/portfolio_backtest/paths.py)."
        )

    pipeline = prb.CSVEnrichmentPipeline(
        mnq_1m_csv=str(pb_paths.MNQ_1M_CSV),
        mnq_5m_csv=str(pb_paths.MNQ_5M_CSV),
        mes_1m_csv=str(pb_paths.MES_1M_CSV) if pb_paths.MES_1M_CSV.exists() else None,
        mes_5m_csv=str(pb_paths.MES_5M_CSV) if pb_paths.MES_5M_CSV.exists() else None,
    )

    lookback_start, lookback_end = _resolve_lookback(days, pipeline.mnq_1m_df)
    if verbose:
        print(f"[engine] strategy={strategy_name}  lookback={lookback_start} -> "
              f"{lookback_end} ({days}d)")

    # Trim pipeline DataFrames to the lookback window.
    pipeline.mnq_1m_df = pipeline.mnq_1m_df[pipeline.mnq_1m_df.ts >= lookback_start]
    pipeline.mnq_5m_df = pipeline.mnq_5m_df[pipeline.mnq_5m_df.ts >= lookback_start]
    if pipeline.mes_1m_df is not None:
        pipeline.mes_1m_df = pipeline.mes_1m_df[pipeline.mes_1m_df.ts >= lookback_start]
    if pipeline.mes_5m_df is not None:
        pipeline.mes_5m_df = pipeline.mes_5m_df[pipeline.mes_5m_df.ts >= lookback_start]

    strats = prb.instantiate_strategies([strategy_name])
    if strategy_name not in strats:
        raise SystemExit(
            f"[engine] strategy '{strategy_name}' is not in the testable set. "
            f"Available: {sorted(prb.TESTABLE_STRATEGIES)}"
        )
    strat = strats[strategy_name]

    # ── 2. Cycle loop with Phase 2 gates ────────────────────────────
    # session_pnl / session_halted are keyed by the trade's ENTRY session date,
    # not its exit session. A trade entered today loses against today's halt
    # budget even if it closes tomorrow — matches the spec's intent of "per-
    # session daily loss halt" and prevents previous-day late wins from
    # giving today an artificial buffer (verified bug 2026-05-31).
    completed: list[prb.TradeResult] = []
    active: Optional[prb.TradeResult] = None
    current_session: Optional[_date] = None
    session_bar_count = 0
    session_pnl: dict[_date, float] = defaultdict(float)
    session_halted: dict[_date, bool] = defaultdict(bool)
    signals_emitted = 0

    mnq_1m_df = pipeline.mnq_1m_df.copy()
    t0 = time.time()

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        # Session-day rollover: reset only the warmup bar counter. P&L and
        # halt state are keyed per-session-date and accumulate independently.
        sess = _session_date_ct(market)
        if sess is None:
            continue
        if sess != current_session:
            current_session   = sess
            session_bar_count = 0
        session_bar_count += 1

        # Book any completed active trade against its ENTRY session's budget.
        if active is not None:
            if active.exit_ts is None or eval_ts >= active.exit_ts:
                es = _entry_session_date(active)
                session_pnl[es] += float(active.pnl_dollars or 0.0)
                if session_pnl[es] <= -DAILY_LOSS_HALT_USD:
                    session_halted[es] = True
                active = None
        if active is not None:
            continue  # one-trade-at-a-time per strategy (matches live)

        # Gate: per-session warmup. Block bars 1..25 (the first 25 minutes
        # after the session-date rollover); first signal allowed at bar 26
        # (eval_ts = session_start + 25 min). Spec section: "Warmup guard:
        # no signals for first 25 minutes (5 completed 5m bars + 5 completed
        # 1m bars)."
        if session_bar_count <= WARMUP_BARS_PER_SESSION:
            continue
        # Gate: daily-loss halt for this session.
        if session_halted[current_session]:
            continue

        # Evaluate the strategy. Same call shape as the live bot.
        try:
            sig = strat.evaluate(market, bars_5m, bars_1m, session_info)
        except Exception as exc:
            print(f"[engine] {strategy_name} eval exception at {eval_ts}: {exc!r}",
                  file=sys.stderr)
            continue
        if sig is None:
            continue
        signals_emitted += 1

        # Resolve entry/stop/target prices (same path as prb.run_backtest).
        entry_price = sig.entry_price if sig.entry_price else market["price"]
        if sig.stop_price is not None and sig.target_price is not None:
            stop_price   = sig.stop_price
            target_price = sig.target_price
        else:
            stop_dist = sig.stop_ticks * 0.25
            if sig.direction == "LONG":
                stop_price   = entry_price - stop_dist
                target_price = entry_price + stop_dist * sig.target_rr
            else:
                stop_price   = entry_price + stop_dist
                target_price = entry_price - stop_dist * sig.target_rr

        ctx = _capture_entry_context(market, sig)
        tr = prb.simulate_trade(
            signal_strategy = strategy_name,
            signal_direction= sig.direction,
            entry_ts        = eval_ts,
            entry_price     = entry_price,
            stop_price      = stop_price,
            target_price    = target_price,
            mnq_1m_df       = mnq_1m_df,
        )
        # Attach captured snapshot to the trade record for downstream ML training.
        tr.entry_context = ctx
        active = tr
        completed.append(tr)

    # Settle one final trade if it closed exactly at the corpus tail.
    if active is not None and active.exit_ts is not None:
        es = _entry_session_date(active)
        session_pnl[es] += float(active.pnl_dollars or 0.0)

    elapsed = time.time() - t0
    if verbose:
        print(f"[engine] {len(completed)} trades / {signals_emitted} signals "
              f"in {elapsed:.1f}s")

    # ── 3. Build DataFrame, enrich, write CSV ───────────────────────
    df = prb.analyze_results(completed)

    # Attach entry_context column from the engine-captured snapshots.
    # Stored as JSON-serialized strings so the warehouse ingester's
    # TRY_CAST(entry_context AS JSON) accepts them.
    import json as _json
    df["entry_context"] = [
        _json.dumps(getattr(t, "entry_context", None), default=str)
        if getattr(t, "entry_context", None) is not None else None
        for t in completed
    ]

    df = _enrich_with_mae_mfe_regime_tod(df, mnq_1m_df)

    out_dir.mkdir(parents=True, exist_ok=True)
    run_label = (
        f"{strategy_name}_"
        f"{lookback_start.strftime('%Y%m%d')}_"
        f"{lookback_end.strftime('%Y%m%d')}_"
        f"{int(time.time())}.csv"
    )
    csv_path = out_dir / run_label
    df.to_csv(csv_path, index=False)
    if verbose:
        print(f"[engine] wrote CSV: {csv_path}  rows={len(df)}")

    # ── 4. Emit sidecar (spec-conformant) ───────────────────────────
    sidecar_path = emit_engine_sidecar(
        csv_path,
        strategy        = strategy_name,
        params          = dict(strat.config),
        lookback_start  = lookback_start,
        lookback_end    = lookback_end,
        notes           = f"days={days}",
    )
    if verbose:
        print(f"[engine] wrote sidecar: {sidecar_path}")

    # ── 5. Auto-ingest into warehouse ───────────────────────────────
    from tools.warehouse.ingest import ingest_csv  # local import: avoid load cycle
    result = ingest_csv(csv_path)
    if verbose:
        print(f"[engine] warehouse ingest: status={result.status}  "
              f"run_id={(result.run_id or '')[:12]}...  rows={result.rows_inserted}")
        if result.status == "error":
            print(f"[engine] WARN: ingest error: {result.error}", file=sys.stderr)

    return csv_path


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agents.backtest_engine",
        description="Phoenix Phase 2 backtest engine (writes to the warehouse).",
    )
    ap.add_argument("--strategy", required=True,
                    help="Strategy key (e.g., bias_momentum). "
                         "Must be in tools.phoenix_real_backtest.TESTABLE_STRATEGIES.")
    ap.add_argument("--days", type=int, required=True,
                    help="Lookback window in days; ends at the latest 1m bar.")
    ap.add_argument("--out-dir", default=None,
                    help=f"Override output directory (default: {OUT_DIR}).")
    ap.add_argument("--quiet", action="store_true", help="Suppress progress logs.")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    run_engine(args.strategy, args.days, out_dir=out_dir, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
