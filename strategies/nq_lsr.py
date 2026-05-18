"""
Phoenix Bot — NQ Liquidity Sweep Reversal (LSR) Strategy
=========================================================

THE THESIS
----------
Institutions need liquidity to fill large orders. Retail stops cluster
at obvious levels (PDH, PDL, ORH, ORL, swing extremes). Price sweeps
those levels to trigger the stops, absorbs the resulting market orders,
then reverses to take the opposite side of the auction.

This strategy detects the sweep + the reversal in a single 1-minute bar
and enters in the reversal direction.

CONFLUENCE STACK — required for entry
--------------------------------------
1. Sweep on a tracked level (PDH/PDL/PSH/PSL/ORH/ORL/SwingH/SwingL)
   AND wick rejection (close back inside ≥50% of bar range)
2. Volume confirmation: bar volume ≥ 1.5× avg of last 20 bars
3. CVD divergence: recent 5-bar bar_delta sum opposes the wick direction
   (price made new low but delta was positive — buyers absorbing — etc.)
4. (Optional, falls open if missing) ES divergence: ES did NOT also make
   a new extreme — NQ leading without broader market confirmation
5. (Optional) BigMoveDetector pre-move score >= 50 — extra confluence

ENTRY
-----
MARKET at the close of the rejection bar.

STOP
----
Structural: just beyond the sweep extreme + 2-tick buffer (typically
8-25 ticks on NQ). SKIP signal if structural stop would exceed
max_stop_ticks (default 30).

TARGETS (HVN/LVN-aware)
-----------------------
T1 (50% off): VWAP — the strongest institutional magnet
T2 (50% off): adapts to volume profile:
  - swept level is NEAR HVN: target = nearest HVN above (LONG) / below (SHORT)
  - swept level is NEAR LVN: target = far HVN beyond the LVN (air pocket)
  - neutral: target = opposite extreme of recent range

TPO DAY-TYPE FILTER
-------------------
- D-day (balanced): take all sweeps, target POC
- P-day (trend up): only LONG sweeps, target single prints / range high
- b-day (trend down): only SHORT sweeps
- B-day (bimodal): conservative — only sweep from one VA edge to other VA edge
- Trend day: SKIP — pure trend, sweep strategy fails

SESSION FILTER
--------------
Only trade during liquidity-rich windows in CT:
  08:30 - 11:00 CT (NY morning — primary)
  13:30 - 15:00 CT (NY afternoon — secondary)

DEPENDENCIES
------------
- strategies/base_strategy.py — BaseStrategy + Signal
- core/liquidity_levels.py — Phase A: level tracker + sweep detector
- core/volume_profile.py — Phase B: HVN/LVN (optional; falls back to ATR target)
- core/tpo_builder.py — Phase D: TPO day-type (optional; defaults to D-day if missing)
- core/big_move_detector.py — existing: BigMoveDetector pre-move + exhaustion scoring

EXTERNAL CONTEXT (provided by base_bot enrichment)
--------------------------------------------------
- market["volume_profile_5d"] — VolumeProfile dict (optional)
- market["tpo_profile"] — TPOProfile dict (optional, from aggregator snapshot)
- market["big_move_pre"] — PreMoveAssessment dict (optional)
- market["es_session_high"], market["es_session_low"] — ES feed (optional)

GRACEFUL DEGRADATION
--------------------
This strategy works at multiple levels of available data:
- Bare minimum: bars_1m + price + cvd + vwap (baseline 55-60% WR expected)
- + HVN/LVN: smarter T2 targeting (60-65% WR expected)
- + TPO: day-type filter prevents fading trend days (65-70% WR expected)
- + ES divergence: confluence bonus (65-72% WR expected)
- + BigMoveDetector: composite quality scoring (peak performance)

If any optional input is missing, the strategy still fires using the
remaining signals. Logs explicitly indicate what was used.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal

# Local imports — these are the Phoenix project's helpers
from core.liquidity_levels import (
    LiquidityLevelTracker,
    detect_sweep,
    filter_bars_yesterday_rth,
    filter_bars_premarket,
    filter_bars_or,
    TICK_SIZE,
)

logger = logging.getLogger(__name__)
_CT = ZoneInfo("America/Chicago")


# ────────────────────────────────────────────────────────────────────
# Defaults — all overridable via config block
# ────────────────────────────────────────────────────────────────────
DEFAULT_SESSION_WINDOWS_CT = [("08:30", "11:00"), ("13:30", "15:00")]
DEFAULT_MAX_TRADES_PER_DAY = 4
DEFAULT_MAX_STOP_TICKS = 30
DEFAULT_MIN_STOP_TICKS = 8
DEFAULT_MIN_WICK_PCT = 0.50
DEFAULT_MIN_VOLUME_RATIO = 1.5
DEFAULT_LEVEL_COOLOFF_MIN = 60
DEFAULT_T1_TARGET = "vwap"
DEFAULT_T2_TARGET_RR = 2.5            # used when no HVN/LVN context
DEFAULT_TIME_EXIT_MINUTES = 30
DEFAULT_BAR_FRESHNESS_SEC = 90        # skip if last 1m bar is older than this
DEFAULT_VOLUME_LOOKBACK = 20
DEFAULT_CVD_DIVERGENCE_LOOKBACK = 5
DEFAULT_NEAR_HVN_LVN_TICKS = 5        # how close (in pts) to count as "near" HVN/LVN
DEFAULT_BIGMOVE_BONUS_THRESHOLD = 50  # BigMoveDetector pre-move score for confluence bonus
DEFAULT_ES_DIVERGENCE_BONUS = 15      # confidence bonus when ES divergence confirms
DEFAULT_TPO_TREND_SKIP = True         # skip on TPO-detected trend days


# ────────────────────────────────────────────────────────────────────
def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def _ct_in_any_window(now_ct: datetime, windows: list) -> bool:
    t = now_ct.time()
    for start_s, end_s in windows:
        start = _parse_hhmm(start_s)
        end = _parse_hhmm(end_s)
        if start <= t < end:
            return True
    return False


# ────────────────────────────────────────────────────────────────────
class NQLiquiditySweepReversal(BaseStrategy):
    """LSR — Liquidity Sweep Reversal on NQ/MNQ."""

    name = "nq_lsr"
    computes_own_stop = True
    computes_own_target = True

    def __init__(self, config: dict):
        super().__init__(config)
        # Level tracker (per-bot instance; persisted to disk for restart safety)
        self._levels = LiquidityLevelTracker(
            level_cooloff_minutes=config.get("level_cooloff_minutes", DEFAULT_LEVEL_COOLOFF_MIN),
        )
        self._trades_today: int = 0
        self._trade_date: Optional[str] = None
        self._last_signal_bar_ts: float = 0
        self._last_levels_refresh_minute: int = -1

        # State persistence (per-bot)
        self._state_path: Optional[Path] = None
        bot_name = config.get("bot_name")
        if bot_name:
            try:
                from config.settings import PROJECT_ROOT
                _root = Path(PROJECT_ROOT)
            except Exception:
                _root = Path(__file__).resolve().parent.parent
            self._state_path = _root / "logs" / f"lsr_state_{bot_name}.json"
            self._load_state()

    # ── State persistence ─────────────────────────────────────────
    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[LSR:state] load failed (non-blocking): {e}")
            return
        today_ct = datetime.now(_CT).strftime("%Y-%m-%d")
        if data.get("trade_date") != today_ct:
            return  # different day — start fresh
        self._trade_date = data.get("trade_date")
        self._trades_today = int(data.get("trades_today", 0))
        self._last_signal_bar_ts = float(data.get("last_signal_bar_ts", 0))
        levels_dict = data.get("levels") or {}
        if levels_dict:
            self._levels = LiquidityLevelTracker.from_dict(
                levels_dict,
                level_cooloff_minutes=self.config.get("level_cooloff_minutes", DEFAULT_LEVEL_COOLOFF_MIN),
            )

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({
                "trade_date": self._trade_date,
                "trades_today": self._trades_today,
                "last_signal_bar_ts": self._last_signal_bar_ts,
                "levels": self._levels.to_dict(),
            }), encoding="utf-8")
        except OSError as e:
            logger.debug(f"[LSR:state] save failed (non-blocking): {e}")

    def _maybe_reset_daily(self, now_ct: datetime) -> None:
        today = now_ct.strftime("%Y-%m-%d")
        if self._trade_date != today:
            self._trade_date = today
            self._trades_today = 0
            self._last_signal_bar_ts = 0
            self._levels = LiquidityLevelTracker(
                level_cooloff_minutes=self.config.get("level_cooloff_minutes", DEFAULT_LEVEL_COOLOFF_MIN),
            )

    # ── Level refresh (call this lazily inside evaluate) ──────────
    def _refresh_levels_if_due(self, now_ct: datetime, bars_1m: list, price: float) -> None:
        """Refresh levels at most once per minute to keep CPU low."""
        cur_minute = now_ct.hour * 60 + now_ct.minute
        if cur_minute == self._last_levels_refresh_minute:
            return
        self._last_levels_refresh_minute = cur_minute

        # PDH/PDL — yesterday's RTH (only set once per day)
        if self._levels.get("PDH") is None:
            ydr = filter_bars_yesterday_rth(bars_1m, today_ct=now_ct)
            if ydr:
                self._levels.update_pdh_pdl(ydr)

        # PSH/PSL — today's premarket (set after 08:30 once premarket complete)
        if self._levels.get("PSH") is None and now_ct.time() >= dtime(8, 30):
            pm = filter_bars_premarket(bars_1m, date_ct=now_ct)
            if pm:
                self._levels.update_psh_psl(pm)

        # ORH/ORL — today's OR (set after 08:45)
        if self._levels.get("ORH") is None and now_ct.time() >= dtime(8, 45):
            or_bars = filter_bars_or(bars_1m, date_ct=now_ct)
            if or_bars:
                self._levels.update_orh_orl(or_bars)

        # Swing levels — refresh every minute using the most recent ~80 bars
        recent = bars_1m[-80:] if len(bars_1m) >= 30 else bars_1m
        if recent:
            self._levels.refresh_swing_levels(recent, current_price=price)

    # ── Main evaluate ──────────────────────────────────────────────
    def evaluate(self,
                 market: dict,
                 bars_5m: list,
                 bars_1m: list,
                 session_info: dict) -> Optional[Signal]:

        # ── Time / session gates ───────────────────────────────────
        now_ct = market.get("now_ct")
        if not isinstance(now_ct, datetime):
            # Compute it ourselves if base_bot didn't enrich
            now_ct = datetime.now(_CT)

        self._maybe_reset_daily(now_ct)

        windows = self.config.get("session_windows_ct", DEFAULT_SESSION_WINDOWS_CT)
        if not _ct_in_any_window(now_ct, windows):
            return None  # outside session windows — silent

        max_trades = int(self.config.get("max_trades_per_day", DEFAULT_MAX_TRADES_PER_DAY))
        if self._trades_today >= max_trades:
            logger.debug(f"[EVAL] {self.name}: BLOCKED daily_max ({self._trades_today}/{max_trades})")
            return None

        # ── Data sanity ────────────────────────────────────────────
        if not bars_1m or len(bars_1m) < 25:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete (bars_1m={len(bars_1m) if bars_1m else 0})")
            return None

        price = float(market.get("price", 0) or 0)
        vwap = float(market.get("vwap", 0) or 0)
        atr_5m = float(market.get("atr_5m", 0) or 0)
        if price <= 0:
            return None
        # CRITICAL: reject NaN/Inf prices to prevent garbage stops/targets
        import math as _math
        if not _math.isfinite(price):
            logger.warning(f"[EVAL] {self.name}: SKIP non_finite_price={price}")
            return None
        if not _math.isfinite(vwap):
            vwap = 0  # tolerate bad VWAP — strategy can use other targets
        if not _math.isfinite(atr_5m):
            atr_5m = 0

        last_bar = bars_1m[-1]
        try:
            last_bar_ts = float(last_bar.end_time)
        except (AttributeError, TypeError, ValueError):
            logger.debug(f"[EVAL] {self.name}: SKIP bad_bar_ts")
            return None

        # Freshness check — skip if we're late
        import time
        bar_freshness = self.config.get("bar_freshness_sec", DEFAULT_BAR_FRESHNESS_SEC)
        if (time.time() - last_bar_ts) > bar_freshness:
            logger.debug(f"[EVAL] {self.name}: SKIP stale_bar age={time.time()-last_bar_ts:.0f}s")
            return None

        # Per-bar dedup
        if last_bar_ts == self._last_signal_bar_ts:
            return None

        # ── Refresh tracked levels ─────────────────────────────────
        self._refresh_levels_if_due(now_ct, bars_1m, price)

        # ── Optional: TPO day-type filter ──────────────────────────
        tpo_skip_trend = bool(self.config.get("tpo_trend_skip", DEFAULT_TPO_TREND_SKIP))
        tpo_profile = market.get("tpo_profile") or {}
        tpo_day_type = tpo_profile.get("day_type", "unknown")
        if tpo_skip_trend and tpo_day_type == "trend":
            logger.debug(f"[EVAL] {self.name}: SKIP tpo_trend_day")
            return None

        # ── Look for sweep on any active level ─────────────────────
        min_wick_pct = float(self.config.get("min_wick_pct", DEFAULT_MIN_WICK_PCT))
        active = self._levels.active_levels()
        if not active:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_active_levels")
            return None

        # Pre-compute volume baseline — used for both gates and confluence
        vol_lookback = int(self.config.get("volume_lookback", DEFAULT_VOLUME_LOOKBACK))
        recent_for_vol = bars_1m[-(vol_lookback + 1):-1] if len(bars_1m) > vol_lookback else bars_1m[:-1]
        avg_vol = (sum(float(getattr(b, "volume", 0) or 0) for b in recent_for_vol)
                   / max(1, len(recent_for_vol)))
        min_vol_ratio = float(self.config.get("min_volume_ratio", DEFAULT_MIN_VOLUME_RATIO))

        sweep = None
        for level in active:
            ev = detect_sweep(level, last_bar, min_wick_pct=min_wick_pct)
            if ev is not None:
                sweep = ev
                break  # take the first sweep this bar — only one level can be swept per bar

        if sweep is None:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_sweep ({len(active)} levels active)")
            return None

        # ── Gate 1: volume confirmation ────────────────────────────
        if avg_vol > 0 and sweep.bar_volume < (avg_vol * min_vol_ratio):
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED low_volume "
                f"({sweep.bar_volume:.0f} < {min_vol_ratio}× {avg_vol:.0f})"
            )
            return None

        # ── Gate 2: CVD divergence ─────────────────────────────────
        # Sum bar_delta over recent N 1m bars; for LONG sweep at low,
        # we need bar_delta sum >= 0 (buyers absorbing despite new low)
        #
        # CRITICAL: filter to TODAY's session only. Otherwise cvd_lookback
        # can include yesterday's bars when today's session just started,
        # making the gate evaluate the wrong context.
        cvd_lookback = int(self.config.get("cvd_divergence_lookback", DEFAULT_CVD_DIVERGENCE_LOOKBACK))
        today_date = now_ct.date()
        today_bars_recent = []
        for b in reversed(bars_1m):
            try:
                bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if bt.date() != today_date:
                break  # crossed into yesterday — stop
            today_bars_recent.append(b)
            if len(today_bars_recent) >= cvd_lookback:
                break
        today_bars_recent.reverse()

        # If we don't have enough today-bars yet, skip (early session)
        if len(today_bars_recent) < min(cvd_lookback, 3):
            logger.debug(
                f"[EVAL] {self.name}: SKIP cvd_insufficient_today_bars "
                f"({len(today_bars_recent)}/{cvd_lookback})"
            )
            return None

        recent_deltas = [
            float(getattr(b, "delta", getattr(b, "bar_delta", 0)) or 0)
            for b in today_bars_recent
        ]
        # CRITICAL: detect NaN values in deltas. NaN comparisons are ALL False,
        # so a NaN delta_sum would pass both direction checks.
        import math as _math
        if any(_math.isnan(d) for d in recent_deltas):
            logger.warning(
                f"[EVAL] {self.name}: SKIP cvd_data_corrupt — "
                f"NaN in delta values; order-flow gate cannot evaluate"
            )
            return None

        delta_sum = sum(recent_deltas)

        # Detect "delta gate is a no-op" condition.
        # If every recent bar has exactly 0 delta, the data feed isn't
        # populating it. The gate can't filter and falls open — better
        # to skip the signal than to fire blind.
        nonzero_count = sum(1 for d in recent_deltas if d != 0)
        if nonzero_count == 0:
            logger.warning(
                f"[EVAL] {self.name}: SKIP cvd_data_missing — all {len(recent_deltas)} "
                f"bars have delta=0; order-flow gate cannot evaluate"
            )
            return None

        if sweep.direction == "LONG" and delta_sum < 0:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED cvd_no_div_long delta_sum={delta_sum:.0f} "
                f"(need >= 0 for buyer absorption)"
            )
            return None
        if sweep.direction == "SHORT" and delta_sum > 0:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED cvd_no_div_short delta_sum={delta_sum:.0f}"
            )
            return None

        # ── Gate 3: structural stop within budget ──────────────────
        max_stop = int(self.config.get("max_stop_ticks", DEFAULT_MAX_STOP_TICKS))
        min_stop = int(self.config.get("min_stop_ticks", DEFAULT_MIN_STOP_TICKS))
        if sweep.structural_stop_ticks > max_stop:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED stop_too_wide "
                f"({sweep.structural_stop_ticks}t > {max_stop}t)"
            )
            return None
        # Stops below min get bumped up to min (don't reject — just widen slightly)
        stop_ticks = max(min_stop, sweep.structural_stop_ticks)

        # ── Optional: TPO day-type directional filter ──────────────
        if tpo_day_type == "P" and sweep.direction == "SHORT":
            logger.debug(f"[EVAL] {self.name}: BLOCKED tpo_P_day_no_short")
            return None
        if tpo_day_type == "b" and sweep.direction == "LONG":
            logger.debug(f"[EVAL] {self.name}: BLOCKED tpo_b_day_no_long")
            return None

        # ── Build the signal ───────────────────────────────────────
        direction = sweep.direction
        # Entry & stop must be tick-grid-aligned or NT8 rejects the order
        from core.confirmation_stop import snap_to_tick
        entry_price = snap_to_tick(sweep.bar_close, TICK_SIZE)

        # Stop price
        if direction == "LONG":
            stop_price = snap_to_tick(sweep.bar_low - 2 * TICK_SIZE, TICK_SIZE)
        else:
            stop_price = snap_to_tick(sweep.bar_high + 2 * TICK_SIZE, TICK_SIZE)

        # Stop distance in price units
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return None

        # ── Compute targets (HVN/LVN-aware) ────────────────────────
        target_price, target_rr, target_reason = self._compute_target(
            direction=direction,
            entry_price=entry_price,
            stop_distance=stop_distance,
            vwap=vwap,
            atr_5m=atr_5m,
            market=market,
            tpo_profile=tpo_profile,
            sweep_level=sweep.level.price,
        )

        # ── Confidence / confluence scoring ────────────────────────
        confluences = [
            f"Swept {sweep.level.name}@{sweep.level.price:.2f}",
            f"Wick {sweep.wick_pct_of_range:.0%} of range, depth={sweep.wick_depth_ticks}t",
            f"Volume {sweep.bar_volume:.0f} >= {min_vol_ratio}×avg{avg_vol:.0f}",
            f"CVD div confirmed: 5-bar delta_sum={delta_sum:+.0f}",
        ]
        confidence = 60.0
        entry_score = 45.0

        # Bonus: TPO day-type aligned
        if tpo_day_type == "D":
            confluences.append("TPO D-day (rotational)")
            confidence += 5
        elif tpo_day_type == "P" and direction == "LONG":
            confluences.append("TPO P-day aligned LONG")
            confidence += 10
        elif tpo_day_type == "b" and direction == "SHORT":
            confluences.append("TPO b-day aligned SHORT")
            confidence += 10

        # Bonus: BigMoveDetector pre-move score
        big_move = market.get("big_move_pre") or {}
        bm_score = int(big_move.get("score", 0) or 0)
        bm_dir = str(big_move.get("likely_direction", "UNKNOWN"))
        bm_threshold = int(self.config.get("bigmove_bonus_threshold", DEFAULT_BIGMOVE_BONUS_THRESHOLD))
        if bm_score >= bm_threshold and bm_dir == direction:
            confluences.append(f"BigMoveDetector pre-move={bm_score} aligned")
            confidence += min(20, (bm_score - bm_threshold) * 0.5)
            entry_score += 10

        # Bonus: ES divergence confirmation
        es_high = market.get("es_session_high")
        es_low = market.get("es_session_low")
        es_price = market.get("es_price")
        es_bonus = float(self.config.get("es_divergence_bonus", DEFAULT_ES_DIVERGENCE_BONUS))
        if es_high is not None and es_low is not None and es_price is not None:
            if direction == "SHORT":
                # NQ swept its HIGH. Did ES also make a new session high?
                es_made_new_high = float(es_price) >= float(es_high) * 0.9995  # within 0.05% tolerance
                if not es_made_new_high:
                    confluences.append(f"ES divergence confirmed (ES did not break high)")
                    confidence += es_bonus
                else:
                    confluences.append(f"ES confirmed high (no divergence — weaker signal)")
                    confidence -= 5
            elif direction == "LONG":
                es_made_new_low = float(es_price) <= float(es_low) * 1.0005
                if not es_made_new_low:
                    confluences.append(f"ES divergence confirmed (ES did not break low)")
                    confidence += es_bonus
                else:
                    confluences.append(f"ES confirmed low (no divergence — weaker signal)")
                    confidence -= 5
        else:
            confluences.append("ES feed not available — confluence skipped")

        confluences.append(target_reason)
        confluences.append(f"Stop: {stop_ticks}t (structural)")

        confidence = max(0.0, min(100.0, confidence))
        entry_score = max(0.0, min(60.0, entry_score))

        # Mark trade-event state
        self._trades_today += 1
        self._levels.mark_level_consumed(sweep.level.name)
        self._last_signal_bar_ts = last_bar_ts
        self._save_state()

        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} entry={entry_price:.2f} "
            f"stop={stop_price:.2f} ({stop_ticks}t) target={target_price:.2f} "
            f"rr={target_rr:.2f} conf={confidence:.0f} level={sweep.level.name}"
        )

        # Time-exit timestamp
        time_exit_min = int(self.config.get("time_exit_minutes", DEFAULT_TIME_EXIT_MINUTES))
        exit_dt = now_ct + timedelta(minutes=time_exit_min)
        eod_flat = f"{exit_dt.hour:02d}:{exit_dt.minute:02d}"

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=confidence,
            entry_score=entry_score,
            strategy=self.name,
            reason=(
                f"LSR {direction} — swept {sweep.level.name}@{sweep.level.price:.2f}, "
                f"wick {sweep.wick_pct_of_range:.0%}, CVD div confirmed"
            ),
            confluences=confluences,
            atr_stop_override=True,        # we computed structural stop; don't override
            entry_type="MARKET",
            entry_price=snap_to_tick(entry_price, TICK_SIZE),
            stop_price=snap_to_tick(stop_price, TICK_SIZE),
            target_price=snap_to_tick(target_price, TICK_SIZE),
            eod_flat_time_et=eod_flat,     # bot interprets this as time-exit
            metadata={
                "swept_level_name": sweep.level.name,
                "swept_level_price": sweep.level.price,
                "wick_depth_ticks": sweep.wick_depth_ticks,
                "wick_pct_of_range": sweep.wick_pct_of_range,
                "tpo_day_type": tpo_day_type,
                "bigmove_pre_score": bm_score,
                "delta_sum_5bar": delta_sum,
                "target_reason": target_reason,
                "time_exit_minutes": time_exit_min,
            },
        )

    # ── Target computation ─────────────────────────────────────────
    def _compute_target(self,
                        *,
                        direction: str,
                        entry_price: float,
                        stop_distance: float,
                        vwap: float,
                        atr_5m: float,
                        market: dict,
                        tpo_profile: dict,
                        sweep_level: float) -> tuple[float, float, str]:
        """Compute the T2 target price and its R:R.

        Priority cascade:
          1. HVN/LVN-aware (if volume_profile_5d enriched)
          2. TPO POC target (if D-day)
          3. VWAP-relative (default)
          4. R:R-based (fallback)

        Returns (target_price, target_rr, reason_string).
        """
        vp = market.get("volume_profile_5d") or {}
        hvns = list(vp.get("hvn_levels") or [])
        lvns = list(vp.get("lvn_levels") or [])
        vp_poc = vp.get("poc")
        near_ticks = float(self.config.get("near_hvn_lvn_ticks", DEFAULT_NEAR_HVN_LVN_TICKS))
        near_dist = near_ticks  # already in price units (assuming 1pt buckets)

        # Step 1: classify swept level relative to HVN/LVN
        swept_near_hvn = any(abs(sweep_level - h) <= near_dist for h in hvns)
        swept_near_lvn = any(abs(sweep_level - l) <= near_dist for l in lvns)

        # Step 2: choose target
        # CASE A: swept level IS an HVN → conservative target (POC or VWAP)
        # CRITICAL: target must be on the correct side of entry for the
        # trade direction. POC may be below entry on a LONG sweep (or above
        # entry on a SHORT) — in which case it's not a valid target.
        if swept_near_hvn:
            if vp_poc is not None:
                target = float(vp_poc)
                target_valid = (
                    (direction == "LONG" and target > entry_price) or
                    (direction == "SHORT" and target < entry_price)
                )
                if target_valid:
                    rr = abs(target - entry_price) / stop_distance
                    if rr >= 1.2:
                        return target, rr, f"T2=VP POC {target:.2f} (HVN sweep, conservative)"
            if vwap > 0:
                vwap_valid = (
                    (direction == "LONG" and vwap > entry_price) or
                    (direction == "SHORT" and vwap < entry_price)
                )
                if vwap_valid:
                    rr = abs(vwap - entry_price) / stop_distance
                    if rr >= 1.2:
                        return vwap, rr, f"T2=VWAP {vwap:.2f} (HVN sweep)"

        # CASE B: swept level near LVN → extended target (next HVN through the air pocket)
        if swept_near_lvn and hvns:
            if direction == "LONG":
                # Look for HVN above entry, prefer farthest reasonable
                above = [h for h in hvns if h > entry_price + stop_distance]
                if above:
                    target = float(min(above, key=lambda h: abs(h - entry_price - 2 * stop_distance)))
                    rr = (target - entry_price) / stop_distance
                    return target, rr, f"T2=HVN {target:.2f} above (LVN air pocket)"
            else:
                below = [h for h in hvns if h < entry_price - stop_distance]
                if below:
                    target = float(max(below, key=lambda h: abs(h - entry_price + 2 * stop_distance)))
                    rr = (entry_price - target) / stop_distance
                    return target, rr, f"T2=HVN {target:.2f} below (LVN air pocket)"

        # CASE C: TPO POC on a D-day
        tpo_day_type = tpo_profile.get("day_type", "")
        tpo_poc = tpo_profile.get("poc")
        if tpo_day_type == "D" and tpo_poc is not None:
            target = float(tpo_poc)
            if direction == "LONG" and target > entry_price:
                rr = (target - entry_price) / stop_distance
                if rr >= 1.2:
                    return target, rr, f"T2=TPO POC {target:.2f} (D-day mean revert)"
            elif direction == "SHORT" and target < entry_price:
                rr = (entry_price - target) / stop_distance
                if rr >= 1.2:
                    return target, rr, f"T2=TPO POC {target:.2f} (D-day mean revert)"

        # CASE D: VWAP target (default)
        if vwap > 0:
            if direction == "LONG" and vwap > entry_price:
                rr = (vwap - entry_price) / stop_distance
                if rr >= 1.0:
                    return vwap, rr, f"T2=VWAP {vwap:.2f} (default magnet)"
            elif direction == "SHORT" and vwap < entry_price:
                rr = (entry_price - vwap) / stop_distance
                if rr >= 1.0:
                    return vwap, rr, f"T2=VWAP {vwap:.2f} (default magnet)"

        # CASE E: fallback to R:R-based target
        rr = float(self.config.get("t2_target_rr", DEFAULT_T2_TARGET_RR))
        import math as _math
        if rr <= 0 or not _math.isfinite(rr):
            rr = DEFAULT_T2_TARGET_RR
        if direction == "LONG":
            target = entry_price + (stop_distance * rr)
        else:
            target = entry_price - (stop_distance * rr)
        return target, rr, f"T2={rr:.1f}R fallback ({target:.2f})"

    # ── Optional managed exit hook ─────────────────────────────────
    def check_exit(self, position, market: dict, bars_1m: list,
                   session_info: dict) -> tuple[bool, str]:
        """Use BigMoveDetector exhaustion to exit early when the move tops out.

        Only fires if BigMoveDetector is wired up and the exhaustion score
        exceeds the configured threshold.
        """
        exh = market.get("big_move_exhaustion") or {}
        exh_score = int(exh.get("score", 0) or 0)
        threshold = int(self.config.get("exhaustion_exit_threshold", 70))
        if exh_score >= threshold:
            return True, f"big_move_exhaustion score={exh_score}>={threshold}"
        return False, ""
