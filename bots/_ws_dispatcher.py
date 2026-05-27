"""WS dispatcher — extracted from base_bot.py 2026-05-24 (P4-1 Stage 4).

Connects to bridge on :8766, receives messages, dispatches by type:
- wsping  → stamps WS watchdog (P1-6)
- dom     → aggregator.process_dom
- trade_ack → bridge ack tracking
- tick    → t_bridge_in stash (P4-3) + tick processing → exit checks + signal exec

Original location: bots/base_bot.py async def _connect_and_listen
(line range 1910-2574 prior to extraction, 665 LOC).

Behaviorally verbatim. `self.X` in the original now reads `self.bot.X`
(BaseBot reference). Module-level helpers and adapters defined in
base_bot.py are imported lazily inside `connect_and_listen` to avoid a
circular import on module load.

Preserved in-method edits made this session:
  - P1-6 wsping handling (`if msg_type == "wsping":` block stamping
    `self.bot._ws_watchdog.last_wsping_received_time`)
  - P4-3 t_bridge_in stash (the `_t_bridge_in = tick.get("t_bridge_in")`
    block right after `if msg_type != "tick":`)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import websockets

from config.settings import BOT_WS_PORT, TICK_SIZE
from config.strategies import STRATEGY_DEFAULTS

logger = logging.getLogger("WSDispatcher")


class WSDispatcher:
    """Wraps the WS receive + per-tick dispatch loop on behalf of BaseBot."""

    def __init__(self, bot):
        self.bot = bot

    async def connect_and_listen(self) -> None:
        """Body of BaseBot._connect_and_listen, behaviorally verbatim."""
        # Lazy imports of base_bot module-level helpers + flags to avoid
        # circular import (base_bot imports this module at top-level).
        from bots.base_bot import (
            SCALE_OUT_ENABLED,
            SCALE_OUT_RR,
            TREND_RIDER_ENABLED,
            _PolicyBarAdapter,
            _PolicyPosAdapter,
            _move_nt8_stop,
            _should_scale_out,
            _trail_stop,
            should_suppress_trend_stall,
        )

        bot = self.bot
        uri = f"ws://127.0.0.1:{BOT_WS_PORT}"
        logger.info(f"Connecting to bridge at {uri}...")

        async with websockets.connect(
            uri,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            max_queue=1024,
        ) as ws:
            # Identify ourselves to the bridge
            await ws.send(json.dumps({
                "type": "identify",
                "name": bot.bot_name,
            }))
            bot._ws = ws
            logger.info(f"Connected to bridge as '{bot.bot_name}'")
            bot.status = "SCANNING"

            async for message in ws:
                # 2026-05-12: stamp last-message time on EVERY frame
                # received (any type), independent of dispatch outcome.
                # Drives _ws_watchdog_loop's silent-half-close detection.
                # Set here BEFORE json parsing so even a malformed message
                # counts as proof the WS is alive.
                bot._last_ws_message_time = time.time()

                # BUG-TL2 guard: the dispatch phase (json.loads + msg_type
                # routing) has its own inner json guard; non-tick messages
                # early-return via `continue`. Wrap the whole dispatch just
                # to catch any other unexpected error without kicking the WS.
                try:
                    tick = json.loads(message)
                except json.JSONDecodeError:
                    continue
                except Exception as _dispatch_err:
                    logger.error(
                        f"[WS DISPATCH] error decoding message "
                        f"(keeping WS alive): {type(_dispatch_err).__name__}: {_dispatch_err}"
                    )
                    continue

                msg_type = tick.get("type")

                if msg_type == "wsping":
                    # 2026-05-24 P1-6 (F-11): bridge sends a `wsping` every
                    # 30s as dedicated proof-of-life. The WS watchdog uses
                    # this (NOT _last_ws_message_time) as its staleness
                    # sentinel, so a quiet market with no ticks but still-
                    # arriving pings does not trip a defensive reconnect.
                    # Control message only — do not feed downstream.
                    bot._ws_watchdog.last_wsping_received_time = time.time()
                    continue

                if msg_type == "dom":
                    try:
                        bot.aggregator.process_dom(tick)
                    except Exception as _dom_err:
                        logger.debug(f"[DOM] process failed: {_dom_err}")
                    continue

                if msg_type == "trade_ack":
                    # Bridge confirms it wrote OIF files (or didn't)
                    ack_files = tick.get("files", [])
                    ack_action = tick.get("action", "")
                    if not ack_files and ack_action not in ("CANCEL_ALL", "CANCELALLORDERS"):
                        logger.error(f"[BRIDGE ACK] Bridge wrote 0 OIF files for {ack_action}! "
                                      f"NT8 will NOT see this order.")
                        bot._last_bridge_ack_ok = False
                    else:
                        bot._last_bridge_ack_ok = True
                    continue

                if msg_type != "tick":
                    continue

                # P4-3 (2026-05-24): stash bridge-in timestamp for latency
                # tracking. Only stamp for real ticks — control msgs
                # (wsping/dom/trade_ack) don't represent a market event.
                _t_bridge_in = tick.get("t_bridge_in")
                if _t_bridge_in is not None:
                    try:
                        bot._last_t_bridge_in = float(_t_bridge_in)
                    except (TypeError, ValueError):
                        pass

                # Yield to event loop — lets websockets handle ping/pong
                # Without this, rapid tick processing starves keepalive
                await asyncio.sleep(0)

                # Tick loop heartbeat — detect frozen loops
                bot._last_tick_time = time.time()

                # BUG-TL2 guard: aggregator.process_tick is the highest-risk
                # call on the tick path — raw tick dict parsing, bar builder,
                # all indicators. If this raises, the unhandled exception
                # bubbles out of `async for`, websockets sends code=1011 to
                # the bridge, and the bot is kicked off (observed 22:50 CT
                # 2026-04-19). Subsequent per-tick work below (footprint,
                # strategies, exits) already has narrow try/except guards.
                try:
                    snapshot = bot.aggregator.process_tick(tick)
                except Exception as _agg_err:
                    logger.error(
                        f"[TICK AGGREGATOR] process_tick failed "
                        f"(keeping WS alive): {type(_agg_err).__name__}: {_agg_err}"
                    )
                    continue

                # 2026-05-06 Sprint J: removed startup-regime log block
                # (used MenthorQ classify_regime; subscription retired).

                # NEW (shadow): feed footprint builders on every tick (fast path, no branching)
                try:
                    bot.footprint_1m.process_tick(tick)
                    bot.footprint_5m.process_tick(tick)
                except Exception:
                    pass  # Footprint errors must not break tick loop

                # Sprint M Tier 2.3: feed tape reader on every tick.
                # record_tick is internally try-safe but wrap defensively
                # so any future expansion can't break the tick loop.
                try:
                    bot.tape_reader.record_tick(tick)
                except Exception:
                    pass

                # NEW (shadow): feed volume profile
                try:
                    from datetime import datetime as _dt
                    _price = float(snapshot.get("price", 0) or 0)
                    _vol = float(tick.get("vol", 0) or 0)
                    if _price > 0 and _vol > 0:
                        bot.volume_profile.update_tick(_price, _vol, _dt.now())
                except Exception:
                    pass

                # NEW (shadow): feed circuit breakers tick-rate detector
                try:
                    bot.circuit_breakers.record_tick()
                except Exception:
                    pass

                # Phase 6b: Feed microstructure filter on every tick
                bot.microstructure_filter.update_tick(snapshot.get("price", 0))

                # Track intra-bar price extremes (for EMA+DOM smart exit wick detection)
                bot._stall_detector.update_tick_price(snapshot.get("price", 0))

                # Check position exits on every tick
                if not bot.positions.is_flat:
                    price = snapshot.get("price", 0)
                    # Phase 6: Track MAE/MFE on every tick
                    bot.expectancy.update_tick(price)
                    # Market close auto-exit
                    if hasattr(bot, '_pending_exit_reason') and bot._pending_exit_reason:
                        reason = bot._pending_exit_reason
                        bot._pending_exit_reason = None
                        await bot._exit_trade(ws, price, reason)
                        continue

                    # ── Trend Rider: runner management (single & multi-contract) ──
                    # Phase C (2026-04-21): iterate ALL active positions so
                    # rider-mode/scale-out logic applies correctly when multiple
                    # strategies hold concurrent positions. list(...) snapshots
                    # the set so in-loop exits don't break iteration.
                    for pos in list(bot.positions.active_positions):
                        # 2026-05-13 (#2): update MAE/MFE on every tick for
                        # every active position. Cheap (3 comparisons). Drives
                        # the persisted MAE/MFE/R-multiple analytics at close.
                        try:
                            pos.update_mae_mfe(price)
                        except Exception:
                            pass  # don't let MAE-tracking break the exit loop

                        if not TREND_RIDER_ENABLED:
                            break

                        if pos.rider_mode:
                            # ── RIDER MODE ─────────────────────────────────────────────
                            # Break-even stop: day-type aware trigger.
                            #
                            # TREND day: BE at 1R (full stop distance). Trend moves extend
                            #   well beyond 1R — no need to lock in early.
                            #
                            # RANGE/VOLATILE/UNKNOWN: BE at 0.5R (half the stop distance).
                            #   Data: choppy day extension P50 = 25 ticks. A 40-tick BE
                            #   trigger would NEVER fire before reversal. 0.5R = ~20t on
                            #   a 40t stop = activates at +10 pts, protecting the gain
                            #   before the inevitable chop-day reversal hits.
                            if not pos.be_stop_active:
                                # 2026-05-13 fast-abort fix: use INITIAL stop_dist,
                                # not the live stop_price (which may have already
                                # been trailed close to entry by a stall tighten
                                # earlier in this tick loop). Pre-fix, the
                                # compounding TRAIL→BE sequence within 1s of entry
                                # would shrink stop_dist to ~1 tick, making BE
                                # trigger at +0.5 tick of profit, locking in a
                                # 2-tick "stop" that exited on entry noise.
                                _init_stop = getattr(pos, "initial_stop_price", 0) \
                                             or pos.stop_price
                                stop_dist = abs(pos.entry_price - _init_stop)
                                if stop_dist > 0:
                                    # BE trigger: 1R on trend days, 0.5R otherwise
                                    be_mult = 1.0 if bot._day_type == "TREND" else 0.5
                                    be_trigger = (pos.entry_price + stop_dist * be_mult
                                                  if pos.direction == "LONG"
                                                  else pos.entry_price - stop_dist * be_mult)
                                    # 2026-05-13 (#18): bar-close confirmation gate.
                                    # Previously: a single noisy tick crossing the
                                    # trigger armed BE, then a retracement could
                                    # stop us out on entry noise. Now: require the
                                    # most-recent COMPLETED 1m bar's close to also
                                    # be past the trigger. Falls back to tick mode
                                    # when no bar yet (first minute of session).
                                    _be_bar_close = bool(
                                        STRATEGY_DEFAULTS.get("be_on_bar_close", True)
                                    )
                                    _bar_confirms = True  # default if no bar yet
                                    if _be_bar_close:
                                        try:
                                            _bars = bot.aggregator.bars_1m.completed
                                            _last_bar = _bars[-1] if _bars else None
                                        except Exception:
                                            _last_bar = None
                                        if _last_bar is not None:
                                            _bar_close = float(getattr(_last_bar, "close", price))
                                            _bar_confirms = (
                                                (pos.direction == "LONG"
                                                 and _bar_close >= be_trigger)
                                                or (pos.direction == "SHORT"
                                                    and _bar_close <= be_trigger)
                                            )
                                    if (((pos.direction == "LONG" and price >= be_trigger) or
                                            (pos.direction == "SHORT" and price <= be_trigger))
                                            and _bar_confirms):
                                        be_stop = (round(pos.entry_price + TICK_SIZE * 2, 2)
                                                   if pos.direction == "LONG"
                                                   else round(pos.entry_price - TICK_SIZE * 2, 2))
                                        _old_stop_px = pos.stop_price
                                        pos.stop_price = be_stop
                                        pos.be_stop_active = True
                                        logger.info(f"[RIDER:{pos.trade_id}] BE STOP "
                                                    f"({bot._day_type}, {be_mult:.0%}R) — "
                                                    f"stop moved to {be_stop:.2f} "
                                                    f"(price={price:.2f}, +{(price-pos.entry_price)/TICK_SIZE:.0f}t)")
                                        # B76: actually move NT8 stop via cancel+replace
                                        _move_nt8_stop(pos, _old_stop_px, be_stop)

                            # Stall detector drives exit — check every tick (already rate-limited inside)
                            stall = bot._stall_detector.check(snapshot, pos.direction)
                            # Fix A (2026-05-03): trend_stall grace period.
                            # The audit found 12+ duration=0 trades exiting via
                            # trend_stall on the same tick as entry. Per-strategy
                            # config knob `trend_stall_grace_s` (default 60) blocks
                            # the stall exit for the first N seconds of a position.
                            _grace_s = 0
                            for _strat in bot.strategies:
                                if getattr(_strat, "name", None) == pos.strategy:
                                    _grace_s = int(_strat.config.get("trend_stall_grace_s", 0) or 0)
                                    break
                            _held_s = time.time() - getattr(pos, "entry_time", time.time())
                            _in_grace = should_suppress_trend_stall(_held_s, _grace_s)
                            if stall["exit_signal"] and _in_grace:
                                # Within grace window — log once-per-position and
                                # skip the exit. _trend_stall_grace_logged flag
                                # avoids spamming the log every tick.
                                if not getattr(pos, "_trend_stall_grace_logged", False):
                                    logger.debug(
                                        f"[STALL_GRACE:{pos.trade_id}] trend_stall "
                                        f"suppressed — held {_held_s:.1f}s < grace {_grace_s}s "
                                        f"(strategy={pos.strategy})"
                                    )
                                    pos._trend_stall_grace_logged = True
                            elif stall["exit_signal"]:
                                logger.info(f"[RIDER:{pos.trade_id}] Trend stall STRONG "
                                            f"— exiting runner. Reasons: {stall['reasons']}")
                                await bot._exit_trade(ws, price, "trend_stall",
                                                       trade_id=pos.trade_id)
                            elif stall["tighten_stop"] and _in_grace:
                                # 2026-05-13 fast-abort fix: extend the grace
                                # window suppression to tighten_stop too. Pre-fix
                                # only exit_signal was gated, so within seconds of
                                # entry the stall detector's MODERATE signal would
                                # fire TRAIL → BE_STOP → exit, killing trades
                                # before they could develop. The _trail_stop
                                # function has its own min-profit guard now, but
                                # honoring grace here is the cleaner fix at the
                                # caller level. STRONG exit is still suppressed
                                # by the original gate above.
                                if not getattr(pos, "_trend_tighten_grace_logged", False):
                                    logger.debug(
                                        f"[STALL_GRACE:{pos.trade_id}] tighten_stop "
                                        f"suppressed — held {_held_s:.1f}s < grace {_grace_s}s "
                                        f"(strategy={pos.strategy})"
                                    )
                                    pos._trend_tighten_grace_logged = True
                            elif stall["tighten_stop"]:
                                _trail_stop(pos, price)

                            # ── 2026-05-13: CVD-based exit signals (after grace) ──
                            # Two additional exit triggers, both respect the same
                            # 60s grace window as the stall detector (don't fire
                            # within first N seconds of entry — let the trade
                            # breathe past entry-tick noise):
                            #
                            #   1. cvd_flip: per-bar CVD delta flipped against
                            #      position for `min_consecutive` consecutive
                            #      bars OR one big-magnitude bar of opposing
                            #      delta. Energy fading — exit before the stop
                            #      catches the full retrace.
                            #
                            #   2. cvd_div: classic bear/bull divergence at a
                            #      confirmed swing point. Price made a new
                            #      extreme but CVD didn't confirm — institutional
                            #      flow isn't there to defend the level.
                            #
                            # Both are advisory; they fire in addition to the
                            # existing stall/BE/trail machinery. Configurable
                            # per-strategy (cvd_exit_enabled, etc.).
                            if not _in_grace and bot.cvd_health.lookback > 0:
                                # Find this position's strategy config (for the
                                # per-strategy toggles + thresholds).
                                _strat_cfg = {}
                                for _s in bot.strategies:
                                    if getattr(_s, "name", None) == pos.strategy:
                                        _strat_cfg = _s.config
                                        break

                                if _strat_cfg.get("cvd_exit_enabled", True):
                                    # 1. Bar-delta flip exit
                                    _flip_min_consecutive = int(
                                        _strat_cfg.get("cvd_flip_min_consecutive", 2)
                                    )
                                    flip = bot.cvd_flip.check_flip_against(
                                        pos.direction,
                                        min_consecutive=_flip_min_consecutive,
                                    )
                                    if flip["flipped"]:
                                        logger.info(
                                            f"[CVD_FLIP:{pos.trade_id}] exit on "
                                            f"flow flip — {flip['reason']}"
                                        )
                                        await bot._exit_trade(
                                            ws, price, "cvd_flip",
                                            trade_id=pos.trade_id,
                                        )

                                    # 2. Swing-divergence exit
                                    div_sig = bot.cvd_div.check_divergence(
                                        trade_direction=pos.direction
                                    )
                                    if div_sig is not None:
                                        logger.info(
                                            f"[CVD_DIV:{pos.trade_id}] {div_sig.kind} "
                                            f"divergence — price "
                                            f"{div_sig.prior_price:.2f}->"
                                            f"{div_sig.new_price:.2f}, "
                                            f"cvd {div_sig.prior_cvd:.0f}->"
                                            f"{div_sig.new_cvd:.0f}, "
                                            f"{div_sig.bars_between} bars apart"
                                        )
                                        await bot._exit_trade(
                                            ws, price, "cvd_divergence",
                                            trade_id=pos.trade_id,
                                        )

                            # 2026-05-15: Big-Move exhaustion exit. Scores
                            # peak-reversal probability for THIS position's
                            # direction using volume exhaustion + CVD
                            # divergence + DOM flip + TF vote shift. When
                            # score >= 70 (3 of 4 signals aligned) AND past
                            # grace, exit. This is the "catch the peak"
                            # behavior the operator wants — exit BEFORE the
                            # round-trip back to entry.
                            if not _in_grace:
                                try:
                                    # B2-3 FIX (2026-05-25): was `market`,
                                    # which is never defined in this scope —
                                    # raised NameError every bar, swallowed
                                    # silently at DEBUG. Big-Move exhaustion
                                    # exit has been dead since it shipped
                                    # 2026-05-15. The correct source is the
                                    # aggregator snapshot, matching
                                    # _trade_exit.py:258 / _signal_router.py:101.
                                    _exh = bot.big_move.detect_exhaustion(
                                        list(bot.aggregator.bars_1m.completed),
                                        bot.aggregator.snapshot(), pos.direction,
                                    )
                                    _exh_threshold = int(
                                        _strat_cfg.get("big_move_exhaustion_threshold", 70)
                                    )
                                    if _exh.score >= _exh_threshold:
                                        logger.info(
                                            f"[BIG_MOVE_EXIT:{pos.trade_id}] "
                                            f"exhaustion score={_exh.score} "
                                            f"({_exh.reason})"
                                        )
                                        await bot._exit_trade(
                                            ws, price, "big_move_exhaustion",
                                            trade_id=pos.trade_id,
                                        )
                                except Exception as _exh_err:
                                    # B2-3 (2026-05-25): upgraded debug→warning
                                    # to match B-006 chandelier policy. Silent
                                    # failure here means a peak-reversal exit
                                    # got skipped — operator must see it.
                                    logger.warning(
                                        f"[BIG_MOVE_EXIT] check err (non-blocking): "
                                        f"{_exh_err!r}"
                                    )

                        elif SCALE_OUT_ENABLED and not pos.scaled_out and pos.original_contracts >= 2:
                            # Original multi-contract scale-out path.
                            # Per-signal override: ORB et al. can supply
                            # Signal.scale_out_rr (stashed on Position) to
                            # override the global (Zarattini ORB = 1.0R).
                            _scale_rr = getattr(pos, "scale_out_rr", None) or SCALE_OUT_RR
                            if _should_scale_out(pos, price, _scale_rr):
                                await bot._scale_out_trade(ws, price)

                    # ── Smart Exit: EMA extension + DOM reversal + candle wick ──
                    # Fires when: (1) held 120s+ (2) in profit N ticks+ (3) extended from
                    # EMA9 (4) DOM sellers stacking AND candle wicking (BOTH required).
                    # SKIPPED when pos.rider_mode=True — on TREND day runners, DOM wobbles
                    # are noise, not reversals. Stall detector (above) handles those exits.
                    #
                    # 2026-05-13 (#1c): dynamic min_profit_ticks = 70% of the
                    # position's target distance. Previous static 20/40 floor
                    # was either too tight (small-target strategies fired the
                    # exit early-cycle, leaving money on the table) or too
                    # loose (big-target strategies kept the exit gate open
                    # for the entire move). 70% means: "we've captured most
                    # of the planned move, NOW be willing to bank it if the
                    # microstructure flips." Static 20/40 are the fallback
                    # when target_price is missing/invalid.
                    # Phase C: smart-exit per position (iterate all; rider-mode
                    # positions are skipped as before).
                    for _pos in list(bot.positions.active_positions):
                        if _pos.rider_mode:
                            continue
                        from config.settings import TICK_SIZE as _TICK_SIZE
                        _static_floor = 40 if bot._day_type == "TREND" else 20
                        _target_px = getattr(_pos, "target_price", None) or 0.0
                        _entry_px = getattr(_pos, "entry_price", None) or 0.0
                        if _target_px > 0 and _entry_px > 0:
                            _target_ticks = abs(_target_px - _entry_px) / _TICK_SIZE
                            _dynamic = int(_target_ticks * 0.70)
                            # Floor at the static value so we don't fire
                            # before clearing the noise band entirely.
                            _min_profit = max(_static_floor, _dynamic)
                        else:
                            _min_profit = _static_floor
                        smart = bot._stall_detector.check_ema_dom_exit(
                            snapshot, _pos.direction,
                            tick_size=_TICK_SIZE,
                            entry_price=_pos.entry_price,
                            entry_time=_pos.entry_time,
                            min_profit_ticks=_min_profit,
                        )
                        if smart["exit_signal"]:
                            logger.info(f"[SMART EXIT:{_pos.trade_id}] {smart['reason']}")
                            await bot._exit_trade(ws, price, "ema_dom_exit",
                                                   trade_id=_pos.trade_id)

                    # ── Universal EoD flat hook ─────────────────────────────────
                    # Any position whose Signal set eod_flat_time_et gets
                    # auto-flattened when current ET time >= that value.
                    # Single code path — works for ORB, Noise Area, and any
                    # future strategy that opts in.
                    # Phase C: EoD flat per position.
                    try:
                        from zoneinfo import ZoneInfo
                        _now_et = datetime.now(ZoneInfo("America/New_York"))
                        _now_hm = _now_et.strftime("%H:%M")
                        for _pos in list(bot.positions.active_positions):
                            _eod = getattr(_pos, "eod_flat_time_et", None)
                            if _eod and _now_hm >= _eod:
                                logger.info(
                                    f"[EOD_FLAT:{_pos.trade_id}] "
                                    f"{_now_hm} ET >= {_eod} — closing"
                                )
                                await bot._exit_trade(ws, price, "eod_flat_universal",
                                                       trade_id=_pos.trade_id)
                    except Exception as e:
                        logger.debug(f"[EOD_FLAT] check error (non-blocking): {e}")

                    # ── Chandelier trail update + exit ──────────────────────────
                    # Strategies with exit_trigger starting "chandelier_trail"
                    # get a trail-state update each bar; bracket stop stays
                    # ALSO active as a disaster stop (whichever fires first).
                    # Phase C: chandelier trail per position.
                    #
                    # B2-3 FIX (2026-05-25): `market` was never defined in
                    # this scope (pre-dates the decomposition — present in
                    # HEAD `base_bot.py:2562` too). Every bar with a
                    # chandelier-using position raised NameError, swallowed
                    # at `logger.warning` (B-006 upgrade) — the warning
                    # has been firing into the void. Chandelier trail exit
                    # has been silently broken for `opening_session.orb`.
                    # Hoist a single snapshot for the whole block so the
                    # fix is also cleaner than a per-line .snapshot() call.
                    try:
                        _bars = list(bot.aggregator.bars_1m.completed)
                        _market = bot.aggregator.snapshot()
                        for _pos in list(bot.positions.active_positions):
                            if getattr(_pos, "trail_state", None) is None:
                                continue
                            if not str(getattr(_pos, "exit_trigger", "")).startswith("chandelier_trail"):
                                continue
                            _cfg = _pos.trail_config or {}
                            _atr_key = f"atr_{_cfg.get('atr_timeframe', '5m')}"
                            _atr = _market.get(_atr_key, 0) or 0
                            if _bars and _atr > 0:
                                _last = _bars[-1]
                                _pos.trail_state.update(_last.high, _last.low, _atr)
                                if _pos.trail_state.should_exit(price):
                                    logger.info(
                                        f"[CHANDELIER:{_pos.trade_id}] price {price:.2f} "
                                        f"violated trail {_pos.trail_state.current_trail:.2f} — exiting"
                                    )
                                    await bot._exit_trade(ws, price, "chandelier_trail_hit",
                                                           trade_id=_pos.trade_id)
                    except Exception as e:
                        # 2026-05-20 SHIP AUDIT pt2 (B-006): was logger.debug.
                        # Legacy chandelier (opening_session.orb) — failure
                        # here means the strategy keeps holding past its
                        # trail level. Operator must know.
                        logger.warning(f"[CHANDELIER] update error: {e!r}")

                    # ── 2026-05-20 PHASE 13 SHIP-AUDIT FIX ──────────────────────
                    # Per-bar Phase 13 exit-policy enforcement. Without this,
                    # ChandelierPolicy.should_exit() and TimeExitPolicy.should_exit()
                    # are NEVER called for the 5 strategies that depend on them:
                    #   - g_inside_bar_breakout (chandelier_50_3x)
                    #   - e_multi_day_breakout  (chandelier_50_3x)
                    #   - es_nq_confluence      (chandelier_50_3x)
                    #   - a_asian_continuation  (time_exit 30m)
                    #   - raschke_baseline      (time_exit 30m)
                    # Before this fix they only ever exited on the wide-bracket
                    # placeholder target (10R chandelier / 5R time) or the
                    # structural stop — losing the entire trailing-exit edge.
                    try:
                        from core.exit_policies import (
                            get_policy as _ph13_get_policy,
                            PHASE_13_EXIT_ASSIGNMENTS as _PH13_EXITS,
                        )
                        _bars_phase13 = list(bot.aggregator.bars_1m.completed)
                        if _bars_phase13:
                            _last_bar = _bars_phase13[-1]
                            for _pos in list(bot.positions.active_positions):
                                _strat = getattr(_pos, "strategy", None)
                                # Finding 2 fix: sub-strategy aware lookup.
                                # Position carries sub_strategy on
                                # opening_session.* positions.
                                _pos_sub = getattr(_pos, "sub_strategy", None)
                                _dotted_key = (f"{_strat}.{_pos_sub}"
                                               if _pos_sub else None)
                                if _dotted_key and _dotted_key in _PH13_EXITS:
                                    _lookup_key = _dotted_key
                                elif _strat in _PH13_EXITS:
                                    _lookup_key = _strat
                                else:
                                    continue
                                _pname, _params = _PH13_EXITS[_lookup_key]
                                # Only enforce the policies that have per-bar
                                # should_exit logic. fixed_rr + managed_existing
                                # are handled by the OCO bracket / strategy.
                                if _pname not in ("chandelier", "time_exit"):
                                    continue
                                # Skip if the legacy chandelier-trail path is
                                # already handling this position (avoid double-fire).
                                if (_pname == "chandelier" and
                                        getattr(_pos, "trail_state", None) is not None and
                                        str(getattr(_pos, "exit_trigger", "")).startswith("chandelier_trail")):
                                    continue
                                # Lazy-init / cache the policy instance + state
                                if getattr(_pos, "_phase13_policy", None) is None:
                                    _pos._phase13_policy = _ph13_get_policy(_pname, _params)
                                if not hasattr(_pos, "_phase13_policy_state"):
                                    _pos._phase13_policy_state = {}
                                # Build pos adapter matching policy's expected
                                # field names (initial_stop, entry_ts, policy_state).
                                _adapter = _PolicyPosAdapter(_pos)
                                # Build bar adapter — last 1m bar (matches the
                                # existing chandelier-trail code's choice).
                                _bar_adapter = _PolicyBarAdapter(_last_bar)
                                _decision = _pos._phase13_policy.should_exit(_adapter, _bar_adapter)
                                if _decision is not None:
                                    logger.info(
                                        f"[PHASE13_EXIT:{_pos.trade_id}] {_lookup_key} "
                                        f"policy={_pname} fired exit_reason="
                                        f"{_decision.exit_reason} at price={_decision.exit_price:.2f}"
                                    )
                                    await bot._exit_trade(
                                        ws, _decision.exit_price,
                                        _decision.exit_reason,
                                        trade_id=_pos.trade_id,
                                    )
                    except Exception as e:
                        # 2026-05-20 SHIP AUDIT pt2 (B-006): was logger.debug.
                        # If the Phase 13 per-bar enforcement loop dies, the
                        # 5 strategies that depend on it (g_inside_bar,
                        # e_multi_day, es_nq_confluence, asian, raschke)
                        # silently lose their entire exit edge. Promoted to
                        # WARNING so operator sees the failure.
                        logger.warning(f"[PHASE13_EXIT] per-bar enforcement error: {e!r}")

                    # Phase C: managed-exit hook per position — strategies with
                    # dynamic exits (Noise Area) get a chance to close their own
                    # position on a signal flip before bracket checks fire.
                    for _pos in list(bot.positions.active_positions):
                        if not getattr(_pos, "exit_trigger", None):
                            continue
                        _strat_obj = next((s for s in bot.strategies if s.name == _pos.strategy), None)
                        if _strat_obj is None:
                            continue
                        try:
                            _snap = bot.aggregator.snapshot()
                            _sess = bot.session.to_dict()
                            should_exit, exit_reason_mgd = _strat_obj.check_exit(
                                _pos, _snap,
                                list(bot.aggregator.bars_1m.completed),
                                _sess,
                            )
                            if should_exit:
                                logger.info(
                                    f"[MANAGED_EXIT:{_pos.trade_id}] "
                                    f"{_pos.strategy} → {exit_reason_mgd}"
                                )
                                await bot._exit_trade(ws, price, exit_reason_mgd,
                                                       trade_id=_pos.trade_id)
                        except Exception as e:
                            logger.debug(f"[MANAGED_EXIT] check_exit error (non-blocking): {e}")

                    # Normal stop/target/time exits
                    _max_hold = None
                    if bot.positions.position:
                        _strat_name = bot.positions.position.strategy
                        for _s in bot.strategies:
                            if _s.name == _strat_name:
                                _max_hold = _s.config.get("max_hold_min")
                                break
                    # Phase C (2026-04-21): iterate all open positions so
                    # stops/targets fire correctly in multi-position mode.
                    # In single-position mode this yields 0 or 1 trigger and
                    # behaves identically to pre-Phase-C.
                    exit_triggers = bot.positions.check_exits_all(price, max_hold_min=_max_hold)
                    for _tid, _exit_reason in exit_triggers:
                        await bot._exit_trade(ws, price, _exit_reason, trade_id=_tid)

                # Execute pending signals from strategy evaluation
                # HARD TIMEOUT: entire signal→trade path must complete in 15s
                # or we abandon it. This prevents the tick loop from ever freezing.
                # Phase C: per-strategy flat check unlocks concurrent entries
                # for different strategies (each on its own NT8 sub-account).
                signal = None
                pending_signals = getattr(bot, "_pending_signals", None)
                if isinstance(pending_signals, list) and pending_signals:
                    # Drop stale queued signals for strategies that already
                    # have a live position; then process the next eligible one.
                    while pending_signals:
                        candidate = pending_signals.pop(0)
                        if bot.positions.is_flat_for(candidate.strategy):
                            signal = candidate
                            break
                    bot._pending_signals = pending_signals
                elif hasattr(bot, '_pending_signal') and bot._pending_signal and \
                        bot.positions.is_flat_for(bot._pending_signal.strategy):
                    signal = bot._pending_signal
                    bot._pending_signal = None

                if signal is not None:
                    try:
                        await asyncio.wait_for(
                            bot._process_signal(ws, signal),
                            timeout=15.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error(f"[SIGNAL TIMEOUT] Signal processing took >15s — "
                                      f"abandoned {signal.direction} via {signal.strategy}. "
                                      f"Tick loop continues.")
                        bot.last_rejection = "Signal processing timeout (15s)"
                    except Exception as e:
                        logger.error(f"[SIGNAL ERROR] {e}")
                        bot.last_rejection = f"Signal error: {e}"
