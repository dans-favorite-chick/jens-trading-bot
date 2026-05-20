"""
Phoenix Failed-Hold Continuation Strategy Lab
=============================================

Tests the OPPOSITE trade to the failed S/R-bounce strategy (Phase 13 V.3):
instead of fading rejection AT a zone, ride the BREAKTHROUGH when price
closes clean THROUGH a strong S/R zone.

Hypothesis (Phase 13 V.3 followup):
  S/R bounce strategy failed because MNQ noise destroys reversal setups.
  BUT — when a strong S/R level BREAKS (price closes through it),
  the move often continues strongly in the direction of the break.

  Logic: the same noise that defeated 2R-target reversals also means a
  level that DOES give way is doing so for a reason. Trade with the
  break, not against it.

Variants tested:
  failed_hold_strict      strength >= 0.7, n_tests >= 3, target 2R
  failed_hold_moderate    strength >= 0.5, n_tests >= 2, target 2R
  failed_hold_3r          strength >= 0.5, n_tests >= 2, target 3R
  failed_hold_round       round-number zones only,        target 2R
  failed_hold_chandelier  strength >= 0.5, n_tests >= 2, chandelier exit

Entry logic (per variant, at each 5m bar close 08:45-14:30 CT):
  1. Detect S/R zones from rolling 5m window (300 bars)
  2. Find nearest qualifying zone within `near_proximity` ticks of price
  3. Wait for BREAK BAR: price closes BEYOND zone by `break_buffer` ticks
     - resistance broken: close > zone + break_buffer*TICK -> bullish setup
     - support broken:    close < zone - break_buffer*TICK -> bearish setup
  4. Wait for CONFIRMATION BAR (next 5m bar closes same side)
  5. Enter at confirmation close
  6. Stop: 2-4 ticks BACK inside the broken zone ("level holds again")
  7. Target: 2R or 3R fixed (variant-dependent) OR chandelier trail
  8. Dedup: one trade per zone-bucket per day. Max 4 trades/day.

Output:
  backtest_results/phoenix_failed_hold_lab.csv
  backtest_results/phoenix_failed_hold_summary.csv
  stdout: per-variant + per-year breakdown + Wilson 95% CI

NOTE: Pure addition. Touches NO production strategy code. Uses the same
bug-fixed simulate_trade from tools/phoenix_real_backtest.py.
"""
from __future__ import annotations

import logging
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sr_zones import detect_sr_zones, SRZone, TICK  # noqa: E402
from tools.phoenix_real_backtest import CSVEnrichmentPipeline, simulate_trade  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("failed_hold_lab")
logger.setLevel(logging.INFO)

_CT = ZoneInfo("America/Chicago")
TICK_VALUE = 0.50

# Recompute S/R zones every 3 5m bars (~15min). Bug-fix lesson from V.3:
# use bars_5m[-1].end_time, not len(bars_5m) (deque saturates at maxlen).
ZONE_RECOMPUTE_EVERY_N_5M_BARS = 3
MAX_TRADES_PER_DAY = 4

# Window
RTH_START = "08:45"
RTH_END = "14:30"

# Setup tuning (shared across variants unless overridden by VariantConfig)
NEAR_PROXIMITY_TICKS = 12   # zone must be within this many ticks of last close
BREAK_BUFFER_TICKS = 2      # close must be this many ticks beyond zone
STOP_INSIDE_TICKS = 3       # stop sits this many ticks back inside the broken zone
MIN_STOP_TICKS = 4          # reject if computed stop_dist < this (too tight)
MAX_STOP_TICKS = 30         # reject if computed stop_dist > this (too wide)
# Confirmation bar must be in the same direction; allow only the
# IMMEDIATELY following 5m close to confirm (else the setup is stale).
CONFIRM_MAX_BARS = 1


# Chandelier params (for failed_hold_chandelier variant). Mirrors the
# production policy in core/exit_policies.py: lookback_bars=50, atr_mult=3.0,
# activate_r=1.0.
CHANDELIER_LOOKBACK = 50
CHANDELIER_ATR_MULT = 3.0
CHANDELIER_ACTIVATE_R = 1.0


# ════════════════════════════════════════════════════════════════════
# Variant configs
# ════════════════════════════════════════════════════════════════════

@dataclass
class VariantConfig:
    name: str
    min_strength: float
    min_tests: int
    target_r: float = 2.0       # fixed-RR target multiple
    source_filter: Optional[set] = None  # if set, only fire on these sources
    use_chandelier: bool = False


VARIANTS: list[VariantConfig] = [
    VariantConfig("failed_hold_strict",     min_strength=0.70, min_tests=3, target_r=2.0),
    VariantConfig("failed_hold_moderate",   min_strength=0.50, min_tests=2, target_r=2.0),
    VariantConfig("failed_hold_3r",         min_strength=0.50, min_tests=2, target_r=3.0),
    VariantConfig("failed_hold_round",      min_strength=0.0,  min_tests=1, target_r=2.0,
                  source_filter={"round"}),
    VariantConfig("failed_hold_chandelier", min_strength=0.50, min_tests=2, target_r=2.0,
                  use_chandelier=True),
]


# ════════════════════════════════════════════════════════════════════
# State
# ════════════════════════════════════════════════════════════════════

@dataclass
class PendingSetup:
    """A break bar has fired; waiting for the confirmation bar."""
    variant_name: str
    direction: str           # "LONG" or "SHORT"
    zone_price: float
    zone_bucket: int
    break_bar_close: float
    break_bar_ts: pd.Timestamp
    bars_waited: int = 0


@dataclass
class LabState:
    # Per-variant active trade tracking
    active: dict = field(default_factory=dict)               # variant_name -> {exit_ts}
    trades_today: dict = field(default_factory=lambda: defaultdict(int))  # (variant, date) -> count
    fired_zone: dict = field(default_factory=dict)           # (variant, date, zone_bucket) -> True

    # Pending setups waiting for confirmation bar (one slot per variant)
    pending: dict = field(default_factory=dict)              # variant_name -> PendingSetup

    # Cached zones
    cached_zones: list = field(default_factory=list)
    last_zone_compute_bar_ts: float = -1.0


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _ct(ts: pd.Timestamp) -> datetime:
    return ts.tz_convert(_CT).to_pydatetime()


def _in_window(now_ct: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, end_hhmm.split(":"))
    t = now_ct.time()
    return dtime(sh, sm) <= t < dtime(eh, em)


def _is_5m_close(now_ct: datetime) -> bool:
    """5m bars close at :00, :05, :10, ..., :55."""
    return now_ct.minute % 5 == 0


def _zone_bucket(price: float, bucket_ticks: int = 8) -> int:
    """Round price into a bucket so close-by zones share a key (dedup)."""
    return int(round(price / (bucket_ticks * TICK)))


def _qualifies(z: SRZone, cfg: VariantConfig) -> bool:
    if z.strength < cfg.min_strength:
        return False
    if z.n_tests < cfg.min_tests:
        return False
    if cfg.source_filter and z.source not in cfg.source_filter:
        return False
    return True


def _find_break(cfg: VariantConfig, zones: list, prev_close: float,
                 cur_bar) -> Optional[tuple[str, SRZone]]:
    """Detect a clean break on the current 5m bar.

    A break requires:
      - For RESISTANCE: prev_close was AT/BELOW zone + small slop,
                        current close is ABOVE zone + BREAK_BUFFER_TICKS.
      - For SUPPORT:    prev_close was AT/ABOVE zone - small slop,
                        current close is BELOW zone - BREAK_BUFFER_TICKS.

    Returns (direction, zone) if one valid break found; else None.

    If multiple zones break in the same bar, the one CLOSEST to the
    current close wins (most actionable level).
    """
    c = float(cur_bar.close)

    best = None
    best_dist = float("inf")
    best_dir = None

    for z in zones:
        if not _qualifies(z, cfg):
            continue
        zp = z.price

        # Must have been "in range" on prior close (within proximity)
        if abs(prev_close - zp) > NEAR_PROXIMITY_TICKS * TICK:
            continue

        if z.type == "resistance":
            # Prev close must NOT have already been clearly above
            if prev_close > zp + BREAK_BUFFER_TICKS * TICK:
                continue
            # Current close must be clearly above
            if c <= zp + BREAK_BUFFER_TICKS * TICK:
                continue
            d = abs(c - zp)
            if d < best_dist:
                best_dist = d
                best = z
                best_dir = "LONG"
        elif z.type == "support":
            if prev_close < zp - BREAK_BUFFER_TICKS * TICK:
                continue
            if c >= zp - BREAK_BUFFER_TICKS * TICK:
                continue
            d = abs(c - zp)
            if d < best_dist:
                best_dist = d
                best = z
                best_dir = "SHORT"

    if best is None:
        return None
    return (best_dir, best)


def _confirm_bar(pending: PendingSetup, cur_bar) -> bool:
    """Confirmation bar = next 5m close continues in the break direction.

    LONG (resistance broken):  current close > break_bar_close
    SHORT (support broken):    current close < break_bar_close
    """
    c = float(cur_bar.close)
    if pending.direction == "LONG":
        return c > pending.break_bar_close
    else:
        return c < pending.break_bar_close


# ════════════════════════════════════════════════════════════════════
# Chandelier exit simulation (for failed_hold_chandelier variant)
# ════════════════════════════════════════════════════════════════════

@dataclass
class ChandelierTradeResult:
    exit_ts: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_dollars: float = 0.0
    pnl_ticks: int = 0
    hold_min: float = 0.0


def _atr_from_window(highs: deque, lows: deque, closes: deque) -> float:
    """Wilder-approximated ATR (matches core/exit_policies.ChandelierPolicy)."""
    if len(highs) < 2:
        return 0.0
    trs = []
    prev_close = closes[0]
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        trs.append(tr)
        prev_close = closes[i]
    return sum(trs) / max(1, len(trs))


def simulate_chandelier_trade(direction: str, entry_ts: pd.Timestamp,
                                entry_price: float, stop_price: float,
                                mnq_1m_df: pd.DataFrame,
                                max_hold_min: int = 240,
                                tick_size: float = 0.25,
                                tick_value: float = 0.50,
                                lookback_bars: int = CHANDELIER_LOOKBACK,
                                atr_mult: float = CHANDELIER_ATR_MULT,
                                activate_r: float = CHANDELIER_ACTIVATE_R
                                ) -> ChandelierTradeResult:
    """Walk MNQ 1m bars forward; track a chandelier trail per bar.

    Conservative: if both trail and entry-stop hit in same bar, take stop.
    """
    res = ChandelierTradeResult()
    forward = mnq_1m_df[mnq_1m_df.ts > entry_ts]
    if forward.empty:
        # Silent-stop fix: still set exit so caller's lockout clears
        res.exit_ts = entry_ts
        res.exit_price = entry_price
        res.exit_reason = "no_data_after_entry"
        return res
    max_ts = entry_ts + pd.Timedelta(minutes=max_hold_min)
    forward = forward[forward.ts <= max_ts]

    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0:
        res.exit_ts = entry_ts
        res.exit_price = entry_price
        res.exit_reason = "bad_stop"
        return res
    activate_thresh = activate_r * stop_dist

    bar_highs: deque = deque(maxlen=lookback_bars)
    bar_lows: deque = deque(maxlen=lookback_bars)
    bar_closes: deque = deque(maxlen=lookback_bars)

    activated = False
    current_trail = stop_price
    last_row = None

    for row in forward.itertuples(index=False):
        last_row = row
        bar_highs.append(float(row.high))
        bar_lows.append(float(row.low))
        bar_closes.append(float(row.close))

        # Activation check
        if not activated:
            if direction == "LONG":
                if float(row.high) >= entry_price + activate_thresh:
                    activated = True
            else:
                if float(row.low) <= entry_price - activate_thresh:
                    activated = True

        # Update trail if activated AND have enough lookback
        if activated and len(bar_highs) >= min(10, lookback_bars):
            atr = _atr_from_window(bar_highs, bar_lows, bar_closes)
            if atr > 0:
                trail_buf = atr_mult * atr
                if direction == "LONG":
                    rolling_high = max(bar_highs)
                    new_trail = rolling_high - trail_buf
                    if new_trail > current_trail:
                        current_trail = new_trail
                else:
                    rolling_low = min(bar_lows)
                    new_trail = rolling_low + trail_buf
                    if new_trail < current_trail:
                        current_trail = new_trail

        # Check stop (conservative)
        if direction == "LONG":
            if float(row.low) <= current_trail:
                res.exit_ts = row.ts
                res.exit_price = current_trail
                res.exit_reason = ("chandelier_trail" if activated
                                    else "initial_stop")
                break
        else:
            if float(row.high) >= current_trail:
                res.exit_ts = row.ts
                res.exit_price = current_trail
                res.exit_reason = ("chandelier_trail" if activated
                                    else "initial_stop")
                break
    else:
        # No exit hit within max_hold window
        if last_row is not None:
            res.exit_ts = last_row.ts
            res.exit_price = float(last_row.close)
            res.exit_reason = "time_exit"
        else:
            res.exit_ts = entry_ts + pd.Timedelta(minutes=max_hold_min)
            res.exit_price = entry_price
            res.exit_reason = "no_data_in_window"

    if res.exit_ts is not None:
        ticks = ((res.exit_price - entry_price) / tick_size
                  if direction == "LONG"
                  else (entry_price - res.exit_price) / tick_size)
        res.pnl_ticks = int(round(ticks))
        res.pnl_dollars = res.pnl_ticks * tick_value
        res.hold_min = (res.exit_ts - entry_ts).total_seconds() / 60.0
    return res


# ════════════════════════════════════════════════════════════════════
# Per-variant break + confirm logic
# ════════════════════════════════════════════════════════════════════

def _process_variant(cfg: VariantConfig, eval_ts: pd.Timestamp,
                      now_ct: datetime, prev_5m_close: Optional[float],
                      current_bar, current_price: float, zones: list,
                      state: LabState) -> Optional[dict]:
    """Return signal dict if a trade should fire on this bar for this variant.

    Two-phase:
      A. If we have a pending setup for this variant, check confirmation.
         If confirmed -> emit signal.
         If not confirmed -> drop pending (stale).
      B. Otherwise, scan zones for a break -> create pending setup.
    """
    date_str = now_ct.strftime("%Y-%m-%d")

    # Phase A — confirmation
    pending = state.pending.get(cfg.name)
    if pending is not None:
        pending.bars_waited += 1
        if _confirm_bar(pending, current_bar):
            # Daily cap check
            if state.trades_today[(cfg.name, date_str)] >= MAX_TRADES_PER_DAY:
                state.pending[cfg.name] = None
                return None
            # Zone dedup
            if state.fired_zone.get(
                (cfg.name, date_str, pending.zone_bucket)) is True:
                state.pending[cfg.name] = None
                return None

            zp = pending.zone_price
            entry_price = float(current_bar.close)

            if pending.direction == "LONG":
                # Stop sits STOP_INSIDE_TICKS back inside the broken zone
                # i.e. below the zone level (zone now acts as support)
                stop_price = zp - STOP_INSIDE_TICKS * TICK
                stop_dist = entry_price - stop_price
                if stop_dist < MIN_STOP_TICKS * TICK or stop_dist > MAX_STOP_TICKS * TICK:
                    state.pending[cfg.name] = None
                    return None
                target_price = entry_price + stop_dist * cfg.target_r
            else:  # SHORT
                stop_price = zp + STOP_INSIDE_TICKS * TICK
                stop_dist = stop_price - entry_price
                if stop_dist < MIN_STOP_TICKS * TICK or stop_dist > MAX_STOP_TICKS * TICK:
                    state.pending[cfg.name] = None
                    return None
                target_price = entry_price - stop_dist * cfg.target_r

            sig = {
                "direction": pending.direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "zone_price": zp,
                "zone_bucket": pending.zone_bucket,
                "note": (f"BRK {pending.direction} zone={zp:.2f} "
                          f"break_close={pending.break_bar_close:.2f} "
                          f"confirm_close={entry_price:.2f} "
                          f"stop_dist={stop_dist:.2f}"),
            }
            # Clear pending after consumption
            state.pending[cfg.name] = None
            return sig
        else:
            # Confirmation failed (or too old) — drop pending
            if pending.bars_waited >= CONFIRM_MAX_BARS:
                state.pending[cfg.name] = None
            # Fall through to Phase B (this bar might itself be a NEW break)

    # Phase B — scan for break (need prev close for "was at zone" check)
    if prev_5m_close is None:
        return None

    found = _find_break(cfg, zones, prev_5m_close, current_bar)
    if found is None:
        return None

    direction, zone = found
    zb = _zone_bucket(zone.price)
    if state.fired_zone.get((cfg.name, date_str, zb)) is True:
        return None

    # Stash pending setup for confirmation NEXT bar
    state.pending[cfg.name] = PendingSetup(
        variant_name=cfg.name,
        direction=direction,
        zone_price=zone.price,
        zone_bucket=zb,
        break_bar_close=float(current_bar.close),
        break_bar_ts=eval_ts,
        bars_waited=0,
    )
    return None


# ════════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════════

def main():
    data_dir = ROOT / "data" / "historical"
    logger.info("[main] Loading pipeline (5 years)")
    pipeline = CSVEnrichmentPipeline(
        mnq_1m_csv=str(data_dir / "mnq_1min_databento.csv"),
        mnq_5m_csv=str(data_dir / "mnq_5min_databento.csv"),
        mes_1m_csv=None,
        mes_5m_csv=None,
        start="2021-05-17", end="2026-05-17",
    )

    mnq_1m_df = pipeline.mnq_1m_df.copy()
    state = LabState()
    trades: list[dict] = []
    cycle_count = 0
    signal_count: dict[str, int] = {v.name: 0 for v in VARIANTS}
    eval_count = 0
    t0 = time.time()

    # Track the prev 5m close (per evaluator) so the break-detector knows
    # whether prior bar was at/below/above the zone.
    prev_5m_close: Optional[float] = None
    prev_5m_bar_end_time: float = -1.0

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1
        if cycle_count < 300:
            continue

        now_ct = _ct(eval_ts)

        # Always clear stale active trades (regardless of bar boundary)
        for v in VARIANTS:
            act = state.active.get(v.name)
            if act is not None and act.get("exit_ts") is not None \
                    and eval_ts >= act["exit_ts"]:
                state.active[v.name] = None

        if not _is_5m_close(now_ct):
            continue
        if not _in_window(now_ct, RTH_START, RTH_END):
            # Reset pending on window exit
            state.pending.clear()
            continue

        if not bars_5m:
            continue
        current_bar = bars_5m[-1]
        current_price = float(getattr(current_bar, "close", market.get("price") or 0))

        # Determine whether this is a NEW 5m bar vs same bar revisited.
        # Use bar end_time (monotonic, V.3 lesson).
        last_5m_end_time = float(getattr(current_bar, "end_time", 0.0))
        is_new_5m_bar = last_5m_end_time > prev_5m_bar_end_time

        # Recompute S/R zones every N new bars
        recompute_interval_s = ZONE_RECOMPUTE_EVERY_N_5M_BARS * 300.0
        if last_5m_end_time - state.last_zone_compute_bar_ts >= recompute_interval_s:
            state.cached_zones = detect_sr_zones(
                bars_5m=bars_5m,
                current_price=current_price,
                lookback_bars=300,
                prior_day_high=market.get("prior_day_high"),
                prior_day_low=market.get("prior_day_low"),
                prior_day_poc=market.get("prior_day_poc"),
                vwap=market.get("vwap"),
                vwap_std=market.get("vwap_std"),
            )
            state.last_zone_compute_bar_ts = last_5m_end_time

        zones = state.cached_zones
        if not zones:
            # Still update prev_5m_close on new bars so we have history when zones appear
            if is_new_5m_bar:
                prev_5m_close = current_price
                prev_5m_bar_end_time = last_5m_end_time
            continue

        date_str = now_ct.strftime("%Y-%m-%d")
        eval_count += 1

        # Only process variants once per NEW 5m bar (avoid duplicate firing
        # when pipeline yields the same 5m close more than once)
        if not is_new_5m_bar:
            continue

        for v in VARIANTS:
            # Skip if active
            if state.active.get(v.name) is not None:
                continue

            sig = _process_variant(v, eval_ts, now_ct, prev_5m_close,
                                     current_bar, current_price, zones, state)
            if sig is None:
                continue

            signal_count[v.name] += 1

            # Simulate per variant: chandelier uses local sim; others use shared
            if v.use_chandelier:
                tr_c = simulate_chandelier_trade(
                    direction=sig["direction"],
                    entry_ts=eval_ts,
                    entry_price=sig["entry_price"],
                    stop_price=sig["stop_price"],
                    mnq_1m_df=mnq_1m_df,
                )
                exit_ts = tr_c.exit_ts
                exit_price = tr_c.exit_price
                exit_reason = tr_c.exit_reason
                pnl_dollars = tr_c.pnl_dollars
                pnl_ticks = tr_c.pnl_ticks
                hold_min = tr_c.hold_min
            else:
                tr = simulate_trade(
                    signal_strategy=v.name,
                    signal_direction=sig["direction"],
                    entry_ts=eval_ts,
                    entry_price=sig["entry_price"],
                    stop_price=sig["stop_price"],
                    target_price=sig["target_price"],
                    mnq_1m_df=mnq_1m_df,
                )
                exit_ts = tr.exit_ts
                exit_price = tr.exit_price
                exit_reason = tr.exit_reason
                pnl_dollars = tr.pnl_dollars
                pnl_ticks = tr.pnl_ticks
                hold_min = tr.hold_min

            state.active[v.name] = {"exit_ts": exit_ts}
            state.trades_today[(v.name, date_str)] += 1
            state.fired_zone[(v.name, date_str, sig["zone_bucket"])] = True

            trades.append({
                "strategy": v.name,
                "direction": sig["direction"],
                "entry_ts": eval_ts,
                "entry_price": sig["entry_price"],
                "stop_price": sig["stop_price"],
                "target_price": sig["target_price"],
                "exit_ts": exit_ts,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "pnl_dollars": pnl_dollars,
                "pnl_ticks": pnl_ticks,
                "hold_min": hold_min,
                "year": eval_ts.year,
                "hour_ct": now_ct.hour,
                "zone_price": sig["zone_price"],
                "note": sig["note"],
            })

        # Roll forward bar-history for next iteration
        prev_5m_close = current_price
        prev_5m_bar_end_time = last_5m_end_time

        if cycle_count % 100_000 == 0:
            elapsed = time.time() - t0
            logger.info(
                f"[runner] cycle={cycle_count:,} ts={eval_ts} "
                f"trades={len(trades)} elapsed={elapsed:.0f}s"
            )

    elapsed = time.time() - t0
    logger.info(
        f"[main] {cycle_count:,} cycles ({eval_count:,} 5m evals in window) "
        f"in {elapsed:.0f}s. Total trades: {len(trades)}, by variant: {signal_count}"
    )

    if not trades:
        print("(no trades generated)")
        return

    df = pd.DataFrame(trades)
    out_csv = ROOT / "backtest_results" / "phoenix_failed_hold_lab.csv"
    out_csv.parent.mkdir(exist_ok=True)
    df.to_csv(out_csv, index=False)
    logger.info(f"[main] wrote {len(df)} trades to {out_csv}")

    # ── Summary ─────────────────────────────────────────────────
    print()
    print("=" * 110)
    print("PHOENIX FAILED-HOLD CONTINUATION LAB - 5 YEAR BACKTEST  (2021-05-17 -> 2026-05-17)")
    print("=" * 110)
    print()
    print(f"Total trades:        {len(df)}")
    print(f"Total P&L:           ${df.pnl_dollars.sum():+,.0f}")
    print(f"Distinct years:      {sorted(df.year.unique())}")
    print()

    print("=== Per-variant ===")
    agg = df.groupby("strategy").agg(
        n=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda s: (s > 0).sum()),
        total=("pnl_dollars", "sum"),
        avg=("pnl_dollars", "mean"),
        max_dd=("pnl_dollars", lambda s: (s.cumsum().cummax() - s.cumsum()).max()),
        avg_hold=("hold_min", "mean"),
    ).round(2)
    agg["wr_pct"] = (agg.wins / agg.n * 100).round(1)
    gross_win = df[df.pnl_dollars > 0].groupby("strategy").pnl_dollars.sum()
    gross_loss = -df[df.pnl_dollars < 0].groupby("strategy").pnl_dollars.sum()
    agg["pf"] = (gross_win / gross_loss).round(2)

    # Wilson 95% CI for win rate (lower bound)
    def wilson_lower(wins, n, z=1.96):
        if n == 0:
            return 0.0
        p = wins / n
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
        return round((center - margin) * 100, 1)

    agg["wr_wilson_low"] = [
        wilson_lower(int(row.wins), int(row.n))
        for row in agg.itertuples()
    ]
    agg = agg.sort_values("total", ascending=False)
    print(agg[["n", "wr_pct", "wr_wilson_low", "total", "avg", "pf", "max_dd",
                "avg_hold"]].to_string())

    summary_csv = ROOT / "backtest_results" / "phoenix_failed_hold_summary.csv"
    agg.to_csv(summary_csv)
    logger.info(f"[main] wrote summary to {summary_csv}")

    print()
    print("=== Per-variant x per-year (P&L $) ===")
    pivot = df.pivot_table(
        index="strategy", columns="year", values="pnl_dollars",
        aggfunc="sum", fill_value=0,
    ).round(0).astype(int)
    print(pivot.to_string())

    print()
    print("=== Trade counts per year ===")
    pivot_n = df.pivot_table(
        index="strategy", columns="year", values="pnl_dollars",
        aggfunc="count", fill_value=0,
    ).astype(int)
    print(pivot_n.to_string())

    print()
    print("=== Exit reason breakdown ===")
    exit_pivot = df.pivot_table(
        index="strategy", columns="exit_reason", values="pnl_dollars",
        aggfunc="count", fill_value=0,
    ).astype(int)
    print(exit_pivot.to_string())


if __name__ == "__main__":
    main()
