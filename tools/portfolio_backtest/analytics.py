"""
analytics.py — performance, excursion, regime, time-of-day & drawdown analytics.

Pure functions over a *trades* DataFrame (the schema emitted by
``phoenix_real_backtest.analyze_results``) plus, where needed, the MNQ 1-minute
OHLCV DataFrame. No strategy evaluation here — this module only *measures*.

Trades schema expected (extra columns ignored):
    strategy, direction ('LONG'|'SHORT'), entry_ts (UTC ts), entry_price,
    stop_price, target_price, exit_ts (UTC ts), exit_price, exit_reason,
    pnl_dollars, pnl_ticks, hold_min

NO-LOOK-AHEAD: regime/volatility features are computed on completed daily bars
and ``.shift(1)``-ed so the label attached to a trade on day D depends only on
data available at the *start* of day D. (See ``classify_daily_regimes``.)

Spec coverage:
    1.2 MAE/MFE distributions + mathematically-derived stop/target
    1.3 Volatility-regime mapping (ATR percentile + Kaufman efficiency ratio;
        VIX optional drop-in)
    1.4 Time-of-day buckets (ET session windows)
    1.5 Consecutive losses, max $ drawdown, max time-under-water
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ET = ZoneInfo("America/New_York")
_CT = ZoneInfo("America/Chicago")

TICK_SIZE = 0.25      # MNQ
TICK_VALUE = 0.50     # $ per tick per contract (MNQ micro)
TRADING_DAYS = 252


# ════════════════════════════════════════════════════════════════════
# Section 1: MAE / MFE  (Phase 1.2)
# ════════════════════════════════════════════════════════════════════

def compute_mae_mfe(trades: pd.DataFrame, mnq_1m: pd.DataFrame,
                    tick_size: float = TICK_SIZE) -> pd.DataFrame:
    """Add ``mae_ticks`` and ``mfe_ticks`` columns by re-walking 1m bars over
    each trade's [entry_ts, exit_ts] holding window.

    MAE = Maximum Adverse Excursion: how far price moved AGAINST the position
          (always >= 0, in ticks).
    MFE = Maximum Favorable Excursion: best unrealized move IN FAVOUR
          (always >= 0, in ticks).

    Implementation uses ``searchsorted`` on the sorted bar timestamps so the
    whole trade set is O(T log B + total_bars_in_windows), not O(T * B).
    """
    out = trades.copy()
    if out.empty:
        out["mae_ticks"] = pd.Series(dtype="float64")
        out["mfe_ticks"] = pd.Series(dtype="float64")
        return out

    bars = mnq_1m.sort_values("ts")
    ts = bars["ts"].to_numpy()                 # datetime64[ns, UTC] -> ns
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")

    entry_ts = pd.to_datetime(out["entry_ts"], utc=True).to_numpy()
    exit_ts = pd.to_datetime(out["exit_ts"], utc=True).to_numpy()
    # Bars strictly after entry, up to and including exit (the fill walk in
    # phoenix_real_backtest uses ts > entry_ts, so we mirror that here).
    lo_idx = np.searchsorted(ts, entry_ts, side="right")
    hi_idx = np.searchsorted(ts, exit_ts, side="right")  # exclusive upper

    entry_px = out["entry_price"].to_numpy(dtype="float64")
    is_long = (out["direction"].to_numpy() == "LONG")

    mae = np.full(len(out), np.nan)
    mfe = np.full(len(out), np.nan)
    for i in range(len(out)):
        a, b = lo_idx[i], hi_idx[i]
        if b <= a:
            # No completed bar inside the window (e.g. instant time-exit).
            mae[i] = 0.0
            mfe[i] = 0.0
            continue
        hi = highs[a:b].max()
        lo = lows[a:b].min()
        if is_long[i]:
            adverse = entry_px[i] - lo
            favor = hi - entry_px[i]
        else:
            adverse = hi - entry_px[i]
            favor = entry_px[i] - lo
        mae[i] = max(0.0, adverse) / tick_size
        mfe[i] = max(0.0, favor) / tick_size

    out["mae_ticks"] = mae
    out["mfe_ticks"] = mfe
    return out


# ════════════════════════════════════════════════════════════════════
# Section 2: Headline performance metrics  (Phase 1.5 + Phase 3)
# ════════════════════════════════════════════════════════════════════

def profit_factor(pnl: np.ndarray) -> float:
    pnl = np.asarray(pnl, dtype="float64")
    gross_win = pnl[pnl > 0].sum()
    gross_loss = -pnl[pnl < 0].sum()
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return float(gross_win / gross_loss)


def win_rate(pnl: np.ndarray) -> float:
    pnl = np.asarray(pnl, dtype="float64")
    return float((pnl > 0).mean()) if len(pnl) else 0.0


def expectancy(pnl: np.ndarray) -> float:
    pnl = np.asarray(pnl, dtype="float64")
    return float(pnl.mean()) if len(pnl) else 0.0


def _daily_pnl(trades: pd.DataFrame, ts_col: str = "exit_ts") -> pd.Series:
    """Aggregate trade P&L to a per-(ET-calendar-day) series. Realized P&L is
    booked on the ET date of the *exit*."""
    if trades.empty:
        return pd.Series(dtype="float64")
    t = trades.dropna(subset=[ts_col]).copy()
    d = pd.to_datetime(t[ts_col], utc=True).dt.tz_convert(_ET).dt.date
    return t.groupby(d)["pnl_dollars"].sum()


def sharpe_ratio(trades: pd.DataFrame, periods_per_year: int = TRADING_DAYS) -> float:
    """Annualized Sharpe of the daily-aggregated P&L stream (risk-free = 0)."""
    daily = _daily_pnl(trades).to_numpy()
    if len(daily) < 2:
        return 0.0
    sd = daily.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(daily.mean() / sd * np.sqrt(periods_per_year))


def sortino_ratio(trades: pd.DataFrame, periods_per_year: int = TRADING_DAYS) -> float:
    """Annualized Sortino (downside deviation of daily P&L)."""
    daily = _daily_pnl(trades).to_numpy()
    if len(daily) < 2:
        return 0.0
    downside = daily[daily < 0]
    dd = np.sqrt((downside ** 2).mean()) if len(downside) else 0.0
    if dd == 0:
        return float("inf") if daily.mean() > 0 else 0.0
    return float(daily.mean() / dd * np.sqrt(periods_per_year))


def max_consecutive_losses(trades: pd.DataFrame) -> int:
    """Longest run of consecutive losing trades (chronological by exit)."""
    if trades.empty:
        return 0
    t = trades.dropna(subset=["exit_ts"]).sort_values("exit_ts")
    pnl = t["pnl_dollars"].to_numpy(dtype="float64")
    run = best = 0
    for x in pnl:
        if x < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return int(best)


# ════════════════════════════════════════════════════════════════════
# Section 3: Equity curve + drawdown duration / time-under-water  (Phase 1.5)
# ════════════════════════════════════════════════════════════════════

@dataclass
class DrawdownStats:
    max_drawdown_dollars: float
    max_drawdown_duration_trades: int     # trades from peak to recovery
    max_time_under_water_days: float      # calendar days peak -> recovery
    currently_under_water: bool
    max_consecutive_losses: int


def equity_curve(trades: pd.DataFrame) -> pd.DataFrame:
    """Chronological (by exit_ts) cumulative-P&L curve.
    Returns DataFrame with columns: ts, pnl, equity, running_peak, drawdown."""
    if trades.empty:
        return pd.DataFrame(columns=["ts", "pnl", "equity", "running_peak", "drawdown"])
    t = trades.dropna(subset=["exit_ts"]).sort_values("exit_ts").reset_index(drop=True)
    ts = pd.to_datetime(t["exit_ts"], utc=True)
    equity = t["pnl_dollars"].cumsum()
    peak = equity.cummax()
    return pd.DataFrame({
        "ts": ts,
        "pnl": t["pnl_dollars"].to_numpy(),
        "equity": equity.to_numpy(),
        "running_peak": peak.to_numpy(),
        "drawdown": (peak - equity).to_numpy(),
    })


def drawdown_analytics(trades: pd.DataFrame) -> DrawdownStats:
    """Max $ drawdown, drawdown DURATION (trades and calendar days spent
    below a prior equity peak before reclaiming it), and whether the curve
    ends under water. 'Time-under-water' = longest peak->recovery gap."""
    ec = equity_curve(trades)
    if ec.empty:
        return DrawdownStats(0.0, 0, 0.0, False, 0)

    equity = ec["equity"].to_numpy()
    peak = ec["running_peak"].to_numpy()
    dd = ec["drawdown"].to_numpy()
    ts = ec["ts"].to_numpy()

    max_dd = float(dd.max())

    # Walk the curve tracking the open underwater stretch. A new equity high
    # (equity >= peak with dd==0) closes the current stretch.
    best_dur_trades = 0
    best_tuw_days = 0.0
    cur_start_idx: Optional[int] = None
    for i in range(len(equity)):
        if dd[i] <= 1e-9:                       # at/above prior peak -> recovered
            if cur_start_idx is not None:
                dur = i - cur_start_idx
                tuw = (ts[i] - ts[cur_start_idx]) / np.timedelta64(1, "D")
                best_dur_trades = max(best_dur_trades, dur)
                best_tuw_days = max(best_tuw_days, float(tuw))
                cur_start_idx = None
        else:
            if cur_start_idx is None:
                cur_start_idx = i - 1 if i > 0 else i   # peak was the prior trade
    # Curve ends under water — count the still-open stretch.
    under_water = cur_start_idx is not None
    if under_water:
        dur = (len(equity) - 1) - cur_start_idx
        tuw = (ts[-1] - ts[cur_start_idx]) / np.timedelta64(1, "D")
        best_dur_trades = max(best_dur_trades, dur)
        best_tuw_days = max(best_tuw_days, float(tuw))

    return DrawdownStats(
        max_drawdown_dollars=max_dd,
        max_drawdown_duration_trades=int(best_dur_trades),
        max_time_under_water_days=round(best_tuw_days, 2),
        currently_under_water=bool(under_water),
        max_consecutive_losses=max_consecutive_losses(trades),
    )


# ════════════════════════════════════════════════════════════════════
# Section 4: Volatility-regime mapping  (Phase 1.3)
# ════════════════════════════════════════════════════════════════════

def _resample_daily(mnq_1m: pd.DataFrame) -> pd.DataFrame:
    """Collapse 1m bars to a per-CT-calendar-day OHLC frame."""
    df = mnq_1m.copy()
    df["ct_date"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(_CT).dt.date
    g = df.groupby("ct_date")
    daily = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    })
    return daily


def _wilder_atr(daily: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = daily["high"], daily["low"], daily["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _kaufman_efficiency(close: pd.Series, period: int = 10) -> pd.Series:
    """Kaufman Efficiency Ratio in [0,1]: |net change| / sum|changes| over
    ``period`` days. ~1 = clean trend, ~0 = chop."""
    direction = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(period).sum()
    return (direction / volatility.replace(0, np.nan)).clip(0, 1)


def classify_daily_regimes(
    mnq_1m: pd.DataFrame,
    atr_period: int = 14,
    er_period: int = 10,
    vol_lookback: int = 60,
    high_vol_pct: float = 0.66,
    trend_er: float = 0.35,
    vix_daily: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Per-CT-day regime label, NO-LOOK-AHEAD (``.shift(1)``): the label for
    day D uses only completed data through day D-1.

    Regimes:
        HIGH_VOLATILITY    — ATR in the top tercile of its trailing window
                             (or VIX above its trailing high-vol percentile,
                             if a VIX series is supplied).
        LOW_VOL_TREND      — not high-vol AND efficiency ratio >= trend_er.
        MEAN_REVERT_CHOP   — not high-vol AND efficiency ratio < trend_er.

    Returns DataFrame indexed by ct_date with columns:
        atr, atr_pct, er, regime
    """
    daily = _resample_daily(mnq_1m)
    atr = _wilder_atr(daily, atr_period)
    # Trailing percentile rank of ATR within the lookback window (no lookahead:
    # rank today's ATR against the trailing window then shift).
    atr_pct = atr.rolling(vol_lookback, min_periods=atr_period).apply(
        lambda w: (w[-1] >= w).mean(), raw=True
    )
    er = _kaufman_efficiency(daily["close"], er_period)

    feat = pd.DataFrame({"atr": atr, "atr_pct": atr_pct, "er": er})

    if vix_daily is not None:
        vix = vix_daily.reindex(feat.index).ffill()
        vix_pct = vix.rolling(vol_lookback, min_periods=20).apply(
            lambda w: (w[-1] >= w).mean(), raw=True
        )
        feat["vix_pct"] = vix_pct
        high_vol = (feat["atr_pct"] >= high_vol_pct) | (vix_pct >= high_vol_pct)
    else:
        high_vol = feat["atr_pct"] >= high_vol_pct

    regime = np.where(
        high_vol, "HIGH_VOLATILITY",
        np.where(feat["er"] >= trend_er, "LOW_VOL_TREND", "MEAN_REVERT_CHOP"),
    )
    feat["regime"] = regime
    # Shift so the label is knowable at the START of the day (uses prior close).
    feat["regime"] = feat["regime"].shift(1)
    feat["atr_pct"] = feat["atr_pct"].shift(1)
    feat["er"] = feat["er"].shift(1)
    return feat


def attach_regime(trades: pd.DataFrame, daily_regimes: pd.DataFrame) -> pd.DataFrame:
    """Map each trade's entry CT-date to its regime label."""
    out = trades.copy()
    if out.empty:
        out["regime"] = pd.Series(dtype="object")
        return out
    ct_date = pd.to_datetime(out["entry_ts"], utc=True).dt.tz_convert(_CT).dt.date
    out["regime"] = ct_date.map(daily_regimes["regime"]).fillna("UNKNOWN")
    return out


# ════════════════════════════════════════════════════════════════════
# Section 5: Time-of-day buckets  (Phase 1.4)
# ════════════════════════════════════════════════════════════════════

def time_of_day_bucket(ts_utc) -> str:
    """Classify an ET wall-clock time into a session bucket.

    Opening Drive   09:30-11:00 ET
    Mid-Day Lull    11:30-13:30 ET
    Power Hour      15:00-16:00 ET
    Globex Overnight 18:00-09:30 ET (next day)
    Other RTH       the remaining cash-session gaps (11:00-11:30, 13:30-15:00,
                    16:00-18:00 settlement)
    """
    et = pd.Timestamp(ts_utc).tz_convert(_ET)
    minute = et.hour * 60 + et.minute
    if 18 * 60 <= minute or minute < 9 * 60 + 30:
        return "Globex Overnight"
    if 9 * 60 + 30 <= minute < 11 * 60:
        return "Opening Drive"
    if 11 * 60 + 30 <= minute < 13 * 60 + 30:
        return "Mid-Day Lull"
    if 15 * 60 <= minute < 16 * 60:
        return "Power Hour"
    return "Other RTH"


def attach_time_of_day(trades: pd.DataFrame) -> pd.DataFrame:
    out = trades.copy()
    if out.empty:
        out["tod_bucket"] = pd.Series(dtype="object")
        return out
    ets = pd.to_datetime(out["entry_ts"], utc=True)
    out["tod_bucket"] = [time_of_day_bucket(t) for t in ets]
    return out


# ════════════════════════════════════════════════════════════════════
# Section 6: MAE/MFE -> mathematically-derived stop & target  (Phase 1.2)
# ════════════════════════════════════════════════════════════════════

@dataclass
class StopTargetSuggestion:
    n_trades: int
    n_winners: int
    suggested_stop_ticks: float
    suggested_target_ticks: float
    stop_rationale: str
    target_rationale: str
    winner_mae_p50: float
    winner_mae_p90: float
    mfe_p50: float
    mfe_p75: float


def optimal_stop_target(trades: pd.DataFrame,
                        winner_mae_pctile: float = 0.90,
                        tick_size: float = TICK_SIZE) -> StopTargetSuggestion:
    """Derive a NON-ARBITRARY baseline stop & target from the realized
    MAE/MFE distributions.

    Stop:  set just beyond the MAE that most *winning* trades survived — i.e.
           the ``winner_mae_pctile`` percentile of winners' MAE. A stop there
           would have prematurely killed only (1 - pctile) of the winners.
    Target: scan candidate targets over the realized MFE distribution and pick
           the one maximizing a simple expectancy proxy
           ``P(MFE >= T)*T - P(MFE < T)*stop`` (capture vs. give-back).

    CAVEAT: MAE/MFE are path-agnostic (they don't encode whether the adverse
    move came before the favorable one), so this is a principled *baseline*,
    not a path-exact optimizer. The path-exact version re-simulates candidate
    (stop, target) pairs via phoenix_real_backtest.simulate_trade — see wfa.py.
    """
    need = {"mae_ticks", "mfe_ticks", "pnl_dollars"}
    if not need.issubset(trades.columns):
        raise KeyError(f"optimal_stop_target needs columns {need}; "
                       f"run compute_mae_mfe first.")
    t = trades.dropna(subset=["mae_ticks", "mfe_ticks"])
    n = len(t)
    if n == 0:
        return StopTargetSuggestion(0, 0, 0, 0, "no trades", "no trades",
                                    0, 0, 0, 0)
    winners = t[t["pnl_dollars"] > 0]
    nw = len(winners)
    w_mae = winners["mae_ticks"].to_numpy() if nw else np.array([0.0])
    mfe = t["mfe_ticks"].to_numpy()

    stop = float(np.percentile(w_mae, winner_mae_pctile * 100)) if nw else \
        float(np.percentile(t["mae_ticks"].to_numpy(), 50))
    stop = max(stop, tick_size / tick_size)  # >= 1 tick

    # Expectancy scan for the target over candidate MFE quantiles.
    candidates = np.percentile(mfe, np.arange(40, 96, 5)) if n else np.array([])
    best_t, best_exp = stop, -1e18
    for cand in candidates:
        if cand <= 0:
            continue
        p_hit = float((mfe >= cand).mean())
        exp = p_hit * cand - (1 - p_hit) * stop
        if exp > best_exp:
            best_exp, best_t = exp, float(cand)

    return StopTargetSuggestion(
        n_trades=n,
        n_winners=nw,
        suggested_stop_ticks=round(stop, 1),
        suggested_target_ticks=round(best_t, 1),
        stop_rationale=(f"{int(winner_mae_pctile*100)}th pctile of winners' MAE "
                        f"({nw} winners) - stops here spare {int(winner_mae_pctile*100)}% of winners"),
        target_rationale=(f"expectancy-max over MFE quantiles "
                          f"(P(hit)={float((mfe>=best_t).mean()):.2f}, exp={best_exp:.1f} ticks)"),
        winner_mae_p50=round(float(np.percentile(w_mae, 50)), 1) if nw else 0.0,
        winner_mae_p90=round(float(np.percentile(w_mae, 90)), 1) if nw else 0.0,
        mfe_p50=round(float(np.percentile(mfe, 50)), 1),
        mfe_p75=round(float(np.percentile(mfe, 75)), 1),
    )


# ════════════════════════════════════════════════════════════════════
# Section 7: Summaries + bucket tables
# ════════════════════════════════════════════════════════════════════

def summarize(trades: pd.DataFrame) -> dict:
    """Headline metric dict for a (already-filtered) set of trades."""
    if trades.empty:
        return {"n": 0, "net_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "sharpe": 0.0, "sortino": 0.0,
                "max_dd": 0.0, "max_dd_dur_trades": 0, "max_tuw_days": 0.0,
                "max_consec_losses": 0}
    pnl = trades["pnl_dollars"].to_numpy(dtype="float64")
    dd = drawdown_analytics(trades)
    return {
        "n": int(len(trades)),
        "net_pnl": round(float(pnl.sum()), 2),
        "win_rate": round(win_rate(pnl), 4),
        "profit_factor": round(profit_factor(pnl), 3),
        "expectancy": round(expectancy(pnl), 3),
        "sharpe": round(sharpe_ratio(trades), 3),
        "sortino": round(sortino_ratio(trades), 3),
        "max_dd": round(dd.max_drawdown_dollars, 2),
        "max_dd_dur_trades": dd.max_drawdown_duration_trades,
        "max_tuw_days": dd.max_time_under_water_days,
        "max_consec_losses": dd.max_consecutive_losses,
    }


def bucket_table(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    """Per-bucket summary table grouped by a column (e.g. 'regime',
    'tod_bucket', 'strategy')."""
    if trades.empty or by not in trades.columns:
        return pd.DataFrame()
    rows = []
    for key, sub in trades.groupby(by, dropna=False):
        d = summarize(sub)
        d[by] = key
        rows.append(d)
    cols = [by, "n", "net_pnl", "win_rate", "profit_factor", "expectancy",
            "sharpe", "sortino", "max_dd", "max_dd_dur_trades",
            "max_tuw_days", "max_consec_losses"]
    return pd.DataFrame(rows)[cols].sort_values("net_pnl", ascending=False)


# ════════════════════════════════════════════════════════════════════
# Self-test (synthetic data) — run:  python analytics.py
# ════════════════════════════════════════════════════════════════════

def _selftest() -> None:
    rng = np.random.default_rng(42)
    # Build a synthetic 1m bar frame over 3 trading days.
    start = pd.Timestamp("2026-04-01 13:30", tz="UTC")  # 08:30 CT
    n_bars = 3 * 390
    ts = pd.date_range(start, periods=n_bars, freq="1min", tz="UTC")
    price = 25000 + np.cumsum(rng.normal(0, 2, n_bars))
    bars = pd.DataFrame({
        "ts": ts,
        "open": price,
        "high": price + rng.uniform(0, 3, n_bars),
        "low": price - rng.uniform(0, 3, n_bars),
        "close": price + rng.normal(0, 1, n_bars),
        "volume": rng.integers(100, 1000, n_bars),
    })

    # Build synthetic trades: enter every 30 bars, exit 10 bars later.
    recs = []
    for i in range(0, n_bars - 20, 30):
        e = bars.iloc[i]
        x = bars.iloc[i + 10]
        direction = "LONG" if i % 60 == 0 else "SHORT"
        ep, xp = float(e.close), float(x.close)
        ticks = (xp - ep) / TICK_SIZE if direction == "LONG" else (ep - xp) / TICK_SIZE
        recs.append({
            "strategy": "synthetic", "direction": direction,
            "entry_ts": e.ts, "entry_price": ep,
            "stop_price": ep - 40 * TICK_SIZE, "target_price": ep + 80 * TICK_SIZE,
            "exit_ts": x.ts, "exit_price": xp, "exit_reason": "time_exit",
            "pnl_ticks": round(ticks), "pnl_dollars": round(ticks) * TICK_VALUE,
            "hold_min": 10.0,
        })
    trades = pd.DataFrame(recs)

    trades = compute_mae_mfe(trades, bars)
    assert (trades["mae_ticks"] >= 0).all(), "MAE must be non-negative"
    assert (trades["mfe_ticks"] >= 0).all(), "MFE must be non-negative"

    regimes = classify_daily_regimes(bars)
    trades = attach_regime(trades, regimes)
    trades = attach_time_of_day(trades)

    s = summarize(trades)
    dd = drawdown_analytics(trades)
    st = optimal_stop_target(trades)

    # Cross-check profit_factor against a manual computation.
    pnl = trades["pnl_dollars"].to_numpy()
    gw, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    pf_manual = gw / gl if gl else float("inf")
    assert abs(s["profit_factor"] - round(pf_manual, 3)) < 1e-6, "PF mismatch"

    print("=== analytics._selftest ===")
    print(f"trades={s['n']}  net=${s['net_pnl']}  PF={s['profit_factor']}  "
          f"WR={s['win_rate']:.2%}  Sharpe={s['sharpe']}  Sortino={s['sortino']}")
    print(f"drawdown: max=${dd.max_drawdown_dollars:.0f}  "
          f"dur={dd.max_drawdown_duration_trades} trades  "
          f"TUW={dd.max_time_under_water_days}d  "
          f"consec_losses={dd.max_consecutive_losses}")
    print(f"regimes present: {sorted(trades['regime'].unique())}")
    print(f"ToD buckets present: {sorted(trades['tod_bucket'].unique())}")
    print(f"stop/target suggestion: stop={st.suggested_stop_ticks}t  "
          f"target={st.suggested_target_ticks}t")
    print(f"  stop_rationale: {st.stop_rationale}")
    print(f"  target_rationale: {st.target_rationale}")
    print("bucket_table(by='tod_bucket'):")
    print(bucket_table(trades, "tod_bucket").to_string(index=False))
    print("OK - all asserts passed")


if __name__ == "__main__":
    _selftest()
