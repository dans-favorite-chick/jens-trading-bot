"""
Phoenix Bot — Opening Range Breakout (ORB)

Source: Zarattini, Barbon & Aziz (2024) — SSRN 4729284.
Entry on 5-minute close outside the 15-minute opening range.

Published results (QQQ 2016-2023): 46% annualized, Sharpe 2.4
NQ backtest (TradeThatSwing): 74% WR, PF 2.51, 12% max DD

Mechanics:
- 15-minute opening range = high/low of first 15 1m bars of RTH session
- Entry trigger: 5-minute bar close outside the OR
- Entry order: STOPMARKET at OR extremum (one tick beyond) — executes on break
- Stop: opposite side of OR (STOPMARKET)
- Target: partial 1R + runner with chandelier trail (base_bot handles scale-out)
- Skip: OR < 10 points (low-vol day) or > 60 points (news-gap day)
- Max: 1 trade per day
- Entry cutoff: 60 min after session open (10:30 ET / 9:30 CST)
- EoD flat: 16:54 ET (lab/sim = 15:54 CT, B84) / 10:55 ET (prod 90-min)
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import logging

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)
from config.settings import TICK_SIZE

# Explicit ET zone — session boundaries + entry cutoff are clock-anchored
# to the cash-equity day. Using zoneinfo means bots can run on any host TZ
# (including UTC-hosted cloud VMs) without drift.
_ET = ZoneInfo("America/New_York")


class OpeningRangeBreakout(BaseStrategy):
    """15-min OR, 5-min close confirmation, STOPMARKET breakout."""

    name = "orb"

    def __init__(self, config: dict):
        super().__init__(config)
        # Per-day state
        self._or_high: float | None = None
        self._or_low: float | None = None
        self._or_set: bool = False
        self._or_date: str | None = None
        self._or_bars_1m: list = []         # 1m bars during OR window
        # 2026-05-13 (#13): session_start_ts is derived from
        # _or_bars_1m[0].start_time. We persist it separately so that
        # after a restart (where _or_bars_1m is NOT restored — bar
        # objects can't survive JSON), Step 3's max_entry_delay_min
        # cutoff can still fire correctly. Without this, post-restart
        # the window check silently passes via IndexError -> pass.
        self._or_session_start_ts: float | None = None
        self._traded_today: bool = False
        self._last_5m_checked_ts: float = 0  # Dedup: check each new 5m bar once
        # Prod vs lab session window — set by bot via is_prod_bot attribute
        self.is_prod_bot: bool = config.get("is_prod_bot", False)
        # 2026-05-13 (#13): restore state across bot restarts so a mid-
        # session crash/restart doesn't lose the OR range that was
        # observed before the restart. Opt-in via config["bot_name"];
        # legacy tests / ad-hoc usage that don't supply a bot_name get
        # the old in-memory-only behavior.
        self._state_path: Path | None = None
        _bot_name = config.get("bot_name")
        if _bot_name:
            try:
                from config.settings import PROJECT_ROOT  # type: ignore
                _root = Path(PROJECT_ROOT)
            except Exception:
                _root = Path(__file__).resolve().parent.parent
            self._state_path = _root / "logs" / f"orb_state_{_bot_name}.json"
            self._load_state()

    # ── State persistence (#13, 2026-05-13) ──────────────────────────
    def _load_state(self) -> None:
        """Restore state from disk if it matches today's date in ET. If
        the saved date is stale (different day), ignore — the bot will
        reset fresh when the OR window opens."""
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            import json
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[ORB:state] load failed (non-blocking): {e}")
            return
        today_et = datetime.now(_ET).strftime("%Y-%m-%d")
        if data.get("or_date") != today_et:
            return  # Different day — silently discard
        self._or_high = data.get("or_high")
        self._or_low = data.get("or_low")
        self._or_set = bool(data.get("or_set", False))
        self._or_date = data.get("or_date")
        self._traded_today = bool(data.get("traded_today", False))
        # #13: restore session-start timestamp so post-restart the
        # max_entry_delay_min cutoff still works (Step 3 in evaluate).
        _sst = data.get("or_session_start_ts")
        self._or_session_start_ts = float(_sst) if _sst is not None else None
        logger.info(
            f"[ORB:state] restored {today_et} — OR=[{self._or_low}, "
            f"{self._or_high}] set={self._or_set} "
            f"traded={self._traded_today} "
            f"session_start_ts={self._or_session_start_ts}"
        )

    def _save_state(self) -> None:
        """Persist current OR state. Called from _reset_daily,
        post-OR-set, and post-trade. Best-effort — never blocks the
        strategy's evaluate() path."""
        if self._state_path is None:
            return
        try:
            import json
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({
                    "or_high": self._or_high,
                    "or_low": self._or_low,
                    "or_set": self._or_set,
                    "or_date": self._or_date,
                    "traded_today": self._traded_today,
                    "or_session_start_ts": self._or_session_start_ts,
                }),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"[ORB:state] save failed (non-blocking): {e}")

    def _reset_daily(self, today: str):
        self._or_high = None
        self._or_low = None
        self._or_set = False
        self._or_date = today
        self._or_bars_1m = []
        self._or_session_start_ts = None
        self._traded_today = False
        self._last_5m_checked_ts = 0
        self._save_state()

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:

        if self._traded_today:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:already_traded_today")
            return None

        price = market.get("price", 0) or 0
        if price <= 0 or len(bars_1m) < 1:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        # ── Config ──────────────────────────────────────────────────
        or_duration = int(self.config.get("or_duration_minutes", 15))
        min_or_size_pts = float(self.config.get("min_or_size_points", 10))
        max_or_size_pts_floor = float(self.config.get("max_or_size_points", 80))
        # 2026-04-24: ATR-adaptive max width. Old fixed 60-pt cap blocked 98%
        # of evals (`gate:or_too_wide`) on current MNQ volatility. New formula:
        #   adaptive_max = max(floor, atr_5m * mult), clamped to hard_cap.
        # That accommodates wider ORs on high-vol days while still rejecting
        # true gap-and-go days (>4× ATR is structurally unreachable).
        max_or_size_atr_mult = float(self.config.get("max_or_size_atr_mult", 4.0))
        max_or_size_hard_cap = float(self.config.get("max_or_size_hard_cap_points", 150))
        atr_5m = float(market.get("atr_5m", 0) or 0)
        if atr_5m > 0:
            max_or_size_pts = min(
                max(max_or_size_pts_floor, atr_5m * max_or_size_atr_mult),
                max_or_size_hard_cap,
            )
        else:
            max_or_size_pts = max_or_size_pts_floor
        max_entry_delay_min = int(self.config.get("max_entry_delay_minutes", 60))
        max_stop_points = float(self.config.get("max_stop_points", 25))
        stop_buffer_ticks = int(self.config.get("stop_buffer_ticks", 2))
        target_rr = float(self.config.get("target_rr", 2.0))
        # 2026-04-25 §4.1: advisor-guided RR tier override. See bias_momentum
        # for the policy rationale. ORB defaults to 2:1; advisor can widen
        # to 3:1 on trending regime or tighten to 1.5:1 on overextended.
        _adv = market.get("advisor_guidance") or {}
        _adv_rr = _adv.get("suggested_rr_tier")
        if _adv_rr and float(_adv_rr) > 0:
            _orig_rr = target_rr
            target_rr = float(_adv_rr)
            if abs(target_rr - _orig_rr) >= 0.5:
                logger.debug(
                    f"[EVAL] {self.name}: advisor RR override "
                    f"{_orig_rr:.1f} -> {target_rr:.1f} "
                    f"(regime={_adv.get('market_regime')})"
                )

        # ── Detect date, reset daily (anchored to ET calendar) ───────
        last_bar = bars_1m[-1]
        try:
            bar_dt = datetime.fromtimestamp(last_bar.end_time, tz=_ET)
        except (OSError, ValueError, TypeError):
            bar_dt = datetime.now(tz=_ET)
        today = bar_dt.strftime("%Y-%m-%d")
        if self._or_date != today:
            self._reset_daily(today)

        # ── Step 1: Build the Opening Range (first 15 1m bars) ──────
        if not self._or_set:
            seen_count = len(self._or_bars_1m)
            if len(bars_1m) > seen_count:
                for bar in bars_1m[seen_count:]:
                    self._or_bars_1m.append(bar)

            for bar in self._or_bars_1m:
                if self._or_high is None or bar.high > self._or_high:
                    self._or_high = bar.high
                if self._or_low is None or bar.low < self._or_low:
                    self._or_low = bar.low

            if len(self._or_bars_1m) >= or_duration:
                self._or_set = True
                # #13: snapshot first-bar start_time before persisting
                # so post-restart Step 3 can still enforce the cutoff.
                if self._or_bars_1m:
                    try:
                        self._or_session_start_ts = float(
                            self._or_bars_1m[0].start_time
                        )
                    except (AttributeError, TypeError, ValueError):
                        self._or_session_start_ts = None
                self._save_state()  # #13: persist once OR is finalized
            else:
                logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
                return None

        # ── Step 2: Validate OR size ────────────────────────────────
        or_size = self._or_high - self._or_low
        if or_size < min_or_size_pts:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:or_too_tight")
            return None  # Too tight — low-vol day, skip
        if or_size > max_or_size_pts:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED gate:or_too_wide "
                f"(or_size={or_size:.1f}pt > adaptive_cap={max_or_size_pts:.1f}pt, "
                f"atr_5m={atr_5m:.1f})"
            )
            return None  # Too wide — gap day, skip

        # ── Step 3: Check entry window cutoff ───────────────────────
        # Session start = first OR bar start. Cutoff = start + max_entry_delay.
        # #13 (2026-05-13): after a bot restart, _or_bars_1m is empty
        # (bar objects don't survive JSON) — fall back to the persisted
        # _or_session_start_ts so the cutoff still fires. Without that
        # fallback, the previous IndexError-pass let trades fire well
        # past the entry-window after any restart.
        _session_start_ts_value: float | None = None
        if self._or_bars_1m:
            try:
                _session_start_ts_value = float(self._or_bars_1m[0].start_time)
            except (AttributeError, TypeError, ValueError):
                _session_start_ts_value = None
        if _session_start_ts_value is None:
            _session_start_ts_value = self._or_session_start_ts
        if _session_start_ts_value is not None:
            try:
                session_start = datetime.fromtimestamp(
                    _session_start_ts_value, tz=_ET,
                )
                minutes_since_open = (bar_dt - session_start).total_seconds() / 60
                if minutes_since_open > max_entry_delay_min:
                    logger.debug(f"[EVAL] {self.name}: BLOCKED gate:entry_window_expired")
                    return None  # Missed the window — no new OR trades
            except (OSError, ValueError, TypeError):
                pass

        # ── Step 4: 5-minute close confirmation ─────────────────────
        # Require a completed 5m bar whose close is outside the OR.
        if len(bars_5m) < 1:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None
        last_5m = bars_5m[-1]
        if last_5m.end_time == self._last_5m_checked_ts:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None  # Already checked this 5m bar — dedup
        self._last_5m_checked_ts = last_5m.end_time

        direction = None
        if last_5m.close > self._or_high:
            direction = "LONG"
        elif last_5m.close < self._or_low:
            direction = "SHORT"
        if direction is None:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_5m_close_outside_or")
            return None

        # ── Step 5: Compute entry/stop/target prices ────────────────
        buf = stop_buffer_ticks * TICK_SIZE
        if direction == "LONG":
            entry_price = round(self._or_high + TICK_SIZE, 2)  # STOPMARKET trigger
            stop_price = round(self._or_low - buf, 2)
            stop_distance = entry_price - stop_price
        else:
            entry_price = round(self._or_low - TICK_SIZE, 2)
            stop_price = round(self._or_high + buf, 2)
            stop_distance = stop_price - entry_price

        # Cap stop distance
        if stop_distance > max_stop_points:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:stop_distance_too_wide")
            return None  # Too wide — rejects oversized OR setups that slipped past size filter
        if stop_distance <= 0:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL invalid_stop_distance")
            return None

        stop_ticks = max(4, int(stop_distance / TICK_SIZE))
        target_price = (
            round(entry_price + stop_distance * target_rr, 2)
            if direction == "LONG"
            else round(entry_price - stop_distance * target_rr, 2)
        )

        # ── Step 6: Mark traded, emit signal ────────────────────────
        self._traded_today = True
        self._save_state()  # #13: persist so a restart can't re-trade

        # Confidence from OR size relative to ATR
        atr_5m = market.get("atr_5m", 0) or 0
        confidence = 65.0
        confluences = [
            f"OR size: {or_size:.2f}pts",
            f"5m close: {last_5m.close:.2f} {'>' if direction == 'LONG' else '<'} OR {'high' if direction == 'LONG' else 'low'}",
        ]
        if atr_5m > 0:
            or_atr_ratio = or_size / atr_5m
            if or_atr_ratio < 1.0:
                confidence += 10
                confluences.append(f"Narrow OR ({or_atr_ratio:.2f}x ATR)")
            confluences.append(f"ATR_5m: {atr_5m:.2f}")

        eod_time = "10:55" if self.is_prod_bot else "16:54"   # B84: lab/sim = 15:54 CT

        logger.info(f"[EVAL] {self.name}: SIGNAL {direction} entry={entry_price:.2f}")
        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=confidence,
            entry_score=55.0,
            strategy=self.name,
            reason=(
                f"ORB {direction} — 5m close {last_5m.close:.2f} broke OR "
                f"[{self._or_low:.2f}, {self._or_high:.2f}] "
                f"({or_size:.2f}pt range)"
            ),
            confluences=confluences,
            atr_stop_override=True,  # We computed exact stop_price; don't overwrite
            entry_type="STOPMARKET",
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            eod_flat_time_et=eod_time,
            # Zarattini 2024 spec: partial 50% at 1.0R, remainder rides
            # with a Chandelier 3×ATR(14) trail on 5m bars.
            scale_out_rr=1.0,
            exit_trigger="chandelier_trail_3atr",
            trail_config={"atr_mult": 3.0, "atr_period": 14, "atr_timeframe": "5m"},
            metadata={
                "or_high": self._or_high,
                "or_low": self._or_low,
                "or_size_pts": or_size,
                "5m_close": last_5m.close,
            },
        )
