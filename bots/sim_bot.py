"""
Phoenix Bot — Sim Bot (Phase C, 2026-04-21)

LIVE sim trading, 24/7, per-strategy NT8 sub-accounts, real OIF writes.

Purpose: gather real NT8 slippage + validation data per strategy on
dedicated accounts. Each of the 16 strategies trades concurrently on
its own $2,000 Sim account with a $200/day loss cap and $1,500 floor
kill-switch. Hits the floor → halt + alert, manual re-enable only.

This replaces the prior lab_bot.py paper-only flow:
  - `_paper_enter` / `_paper_exit` → base_bot's live _enter_trade /
    _exit_trade (WS → bridge → oif_writer with per-strategy account).
  - Bot-level RiskManager → StrategyRiskRegistry (16 isolated RMs).
  - 15:54 CT daily flatten (inherited from BaseBot, B84) with a 15:53
    CT no-new-entries gate and 15:54:45 CT fill-confirmation watcher.
  - Overnight holds allowed during 5 PM – 4 PM next-day globex session.

Opening-session sub-strategies self-gate via is_in_window() — no
bot-level session logic required.

ZERO_GATE behavior is preserved from lab_bot so strategies still
fire across all regimes for data collection; the gate lives on the
floor/halt side now rather than paper mode.
"""

import asyncio
import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bots.base_bot import BaseBot
from strategies.base_strategy import Signal
from config.settings import TICK_SIZE
from core.strategy_risk_registry import StrategyRiskRegistry
from bots.daily_flatten import DailyFlattener


def _build_conflict_recap_section(today_ct, trades: list[dict]) -> str | None:
    """B71: build the directional-conflicts section for the 17:00 CT
    Telegram recap. Returns None if there is no conflict activity.

    Reads logs/conflicts/YYYY-MM-DD.jsonl and attributes per-event P&L
    from the supplied `trades` list by matching (trade_id -> pnl_dollars).
    Conflict cost = sum of P&L contributed by both halves of each pair.
    """
    import json as _json
    import os as _os
    conflicts_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "logs", "conflicts", f"{today_ct}.jsonl",
    )
    if not _os.path.exists(conflicts_path):
        return (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔀 DIRECTIONAL CONFLICTS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "No directional conflicts today."
        )
    events = []
    try:
        with open(conflicts_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(_json.loads(line))
                except Exception:
                    pass
    except Exception:
        return None
    opened = [e for e in events if e.get("event") == "conflict_opened"]
    if not opened:
        return (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔀 DIRECTIONAL CONFLICTS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "No directional conflicts today."
        )
    # Build pnl map from today's trades.
    pnl_by_tid = {}
    for t in trades:
        tid = t.get("trade_id")
        if tid:
            pnl_by_tid[tid] = float(t.get("pnl_dollars") or 0)
    total_overlap_s = 0.0
    lines = []
    involved_tids = set()
    for ev in opened:
        for c in ev.get("conflicts", []):
            total_overlap_s = max(total_overlap_s,
                                  float(c.get("overlap_seconds") or 0))
            involved_tids.add(c.get("trade_id_a"))
            involved_tids.add(c.get("trade_id_b"))
            pa = pnl_by_tid.get(c.get("trade_id_a"))
            pb = pnl_by_tid.get(c.get("trade_id_b"))
            pa_s = f"${pa:+.2f}" if pa is not None else "open"
            pb_s = f"${pb:+.2f}" if pb is not None else "open"
            lines.append(
                f"• {c.get('strategy_a')} {c.get('dir_a')} ({pa_s}) "
                f"vs {c.get('strategy_b')} {c.get('dir_b')} ({pb_s})"
            )
    conflict_cost = sum(pnl_by_tid.get(t, 0.0) for t in involved_tids)
    total_overlap_min = total_overlap_s / 60.0
    header = (
        "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔀 DIRECTIONAL CONFLICTS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{len(opened)} conflict events today (peak overlap: {total_overlap_min:.1f} min)\n"
    )
    body = "\n".join(lines[:10])
    tail = f"\n\nCumulative conflict cost today: ${conflict_cost:+.2f}"
    return header + body + tail


logging.basicConfig(
    # Sim runs at DEBUG to surface Fix 5 [EVAL] BLOCKED/SKIP/NO_SIGNAL
    # reject-reason logs for strategy observability. Prod stays at INFO
    # (see bots/prod_bot.py) so production logs stay quiet.
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
# B23: silence third-party DEBUG noise while preserving bot-level DEBUG
# for strategy debugging. Without this, websockets.client alone produces
# ~10 log lines/second of raw tick dumps and yfinance/peewee emit dozens
# of lines per intermarket cycle — drowning [EVAL] reject logs.
for _noisy in ("websockets.client", "websockets.server", "yfinance",
               "httpcore", "httpcore.connection", "httpcore.http11",
               "peewee", "chromadb"):
    logging.getLogger(_noisy).setLevel(logging.INFO)
logger = logging.getLogger("SimBot")

# ─── ZERO GATE Settings ──────────────────────────────────────────
# Every threshold at absolute minimum. The sim bot trades EVERYTHING so
# the per-strategy Sim account receives real NT8 slippage data across
# the full spectrum of setups each strategy can see. Floor/halt + daily
# cap (via StrategyRiskRegistry) are the real risk boundaries — the
# lab-era single bot-wide max_daily_loss is no longer the control.
SIM_ZERO_GATE = {
    "min_confluence": 0.0,
    "min_momentum": 0,
    "min_momentum_confidence": 0,
    "min_precision": 0,
    "risk_per_trade": 15.0,
    # NOTE: the bot-wide max_daily_loss is intentionally wide — the real
    # per-strategy cap ($200/day) lives in StrategyRiskRegistry. This
    # prevents a single strategy's daily loss from halting the whole bot.
    "max_daily_loss": 10000.0,
}

# Strategy overrides — same ZERO_GATE config as lab, preserved verbatim.
SIM_STRATEGY_OVERRIDES = {
    "bias_momentum": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,
        "min_momentum": 0,
        "skip_regime_overrides": True,
        "stop_ticks": 14,
        "target_rr": 20.0,
        "max_ema_dist_ticks": 999,
    },
    "spring_setup": {
        "min_wick_ticks": 3,
        "require_vwap_reclaim": False,
        "require_delta_flip": False,
        "require_tf_alignment": False,
        "skip_regime_overrides": True,
        "stop_multiplier": 1.5,
        "target_rr": 5.0,
        "atr_stop_multiplier": 1.1,
        "max_stop_ticks": 120,
        "min_stop_ticks": 40,
    },
    "vwap_pullback": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,
        "skip_regime_overrides": True,
        "stop_ticks": 14,
        "max_vwap_dist_ticks": 60,
        "target_rr": 20.0,
    },
    "high_precision_only": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,
        "min_precision": 0,
        "skip_regime_overrides": True,
        "stop_ticks": 14,
        "target_rr": 5.0,
    },
    "ib_breakout": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,
        "skip_regime_overrides": True,
        "stop_ticks": 10,
        "target_rr": 5.0,
        "ib_minutes": 15,
        "max_ib_width_atr_mult": 5.0,
        "max_stop_ticks": 120,
        "all_regimes": True,
        "require_cvd_confirm": False,
    },
    "dom_pullback": {
        "min_dom_strength": 10,
        "max_ema_dist_ticks": 28,
        "max_vwap_dist_ticks": 20,
        "skip_regime_overrides": True,
        "stop_ticks": 10,
        "target_rr": 20.0,
    },
    "vwap_band_pullback": {
        "skip_regime_overrides": True,
        "min_volume_ratio": 0.5,
        "target_rr": 5.0,
    },
    "opening_session": {
        # Thresholds are already tuned research values (Fix 6 stops,
        # volume/wick ratios per paper). Empty override = use
        # config/strategies.py values as-is.
    },
}


class SimBot(BaseBot):
    """Live sim trading bot — 16 strategies on 16 dedicated NT8 accounts.

    Differences from LabBot (which is being superseded by this module):
      - Real NT8 execution (WS → bridge → oif_writer with account=...).
      - StrategyRiskRegistry provides per-strategy risk isolation.
      - DailyFlattener issues EXIT at 4pm CT for all open positions.
      - bot_name = "sim" → history logs write to _sim.jsonl streams.
    """

    bot_name = "sim"
    only_validated = False  # Runs ALL strategies, including unvalidated

    def __init__(self):
        super().__init__()
        # Per-strategy risk registry (replaces single bot-wide self.risk
        # for strategy-isolated accounting). self.risk still exists on
        # the base class and is kept for backward-compat wiring that
        # reads from it; sim_bot overrides eval and close paths to
        # consult the registry instead.
        self.risk_registry = StrategyRiskRegistry()
        # B84: self._flattener is provided by BaseBot.__init__ (shared
        # infra so prod and sim both get the 15:54 CT flatten). sim_bot
        # only adds post-flatten hooks (debrief + recap) by overriding
        # _daily_flatten_loop below.
        # [COUNCIL-AUTO] S5 4A CouncilGate auto-trigger state.
        # Session-open 08:30 CT trigger dropped per Jennifer 2026-04-21;
        # regime-shift is the only auto-trigger that ships.
        # _last_regime_council_ts: monotonic ts of the last regime-shift
        #                          fire — used for the 15-min debounce.
        # _last_seen_regime: last regime observed by the poll loop; used
        #                    to detect transitions (prev != new).
        self._last_regime_council_ts: float = 0.0
        self._last_seen_regime: str | None = None

    def load_strategies(self):
        """Load all strategies with ZERO gates + report registry state."""
        super().load_strategies()

        # Apply per-strategy zero-gate overrides
        for strat in self.strategies:
            if strat.name in SIM_STRATEGY_OVERRIDES:
                for k, v in SIM_STRATEGY_OVERRIDES[strat.name].items():
                    strat.config[k] = v
                logger.info(f"[SIM] ZERO GATE override: {strat.name}")

        # Set zero-gate runtime params (bot-wide — for dashboard slider
        # compatibility; the real per-strategy caps are in the registry).
        self._runtime_params.update(SIM_ZERO_GATE)
        self.risk.set_risk_per_trade(SIM_ZERO_GATE["risk_per_trade"])
        self.risk.set_daily_limit(SIM_ZERO_GATE["max_daily_loss"])
        self.risk.set_max_trades(999)

        # Disable bot-wide cooloff — per-strategy cooloff lives in registry
        from config.settings import COOLOFF_AFTER_CONSECUTIVE_LOSSES  # noqa: F401
        self.risk.state.cooloff_until = 0

        n_halted = sum(1 for k in self.risk_registry.known_keys()
                       if self.risk_registry.is_halted(*_split_key(k)))

        # Count actual NT8 account destinations (not strategy files):
        # opening_session dispatches to 6 subs; compression_breakout has
        # 15m + 30m timeframes; everything else is 1:1.
        try:
            from config.account_routing import STRATEGY_ACCOUNT_MAP
            n_destinations = 0
            for k, v in STRATEGY_ACCOUNT_MAP.items():
                if k == "_default":
                    continue
                if isinstance(v, dict):
                    n_destinations += len(v)
                else:
                    n_destinations += 1
        except Exception:
            n_destinations = len(self.strategies)

        logger.info(f"[SIM] {len(self.strategies)} strategies → "
                    f"{n_destinations} account destinations loaded — LIVE execution")
        logger.info(f"[SIM] Per-strategy: $2000 start / $200 daily cap / $1500 floor")
        logger.info(f"[SIM] Registry: {len(self.risk_registry.known_keys())} keys tracked "
                    f"({n_halted} halted from prior session)")
        logger.info(
            f"[SIM] Daily flatten: 15:54 CT (Phoenix PRIMARY); "
            f"15:55 CT (NT8 Auto Close safety net); no new entries after 15:53 CT"
        )

    async def run(self):
        """Sim-specific extras over BaseBot.run(): schedule the council
        regime-shift poller. DailyFlattener is now scheduled by BaseBot
        (B84)."""
        # [COUNCIL-AUTO] Launch council regime-shift auto-trigger poller.
        # Same 30s cadence as the flatten poller, same safe-by-default
        # semantics. (Session-open 08:30 CT trigger dropped 2026-04-21.)
        asyncio.ensure_future(self._council_regime_shift_loop())
        await super().run()

    async def _daily_flatten_loop(self):
        """B84: override of BaseBot._daily_flatten_loop. Runs the same
        15:54 CT flatten + grace-window watcher the parent provides,
        then adds the sim-specific post-flatten debrief + 17:00 daily
        recap hooks. Polls every 30s."""
        from datetime import datetime as _dt, date as _date  # noqa: F401
        from zoneinfo import ZoneInfo as _ZI
        ct = _ZI("America/Chicago")
        try:
            from config.settings import FILL_CONFIRMATION_GRACE_SECONDS as _GRACE_S
        except Exception:
            _GRACE_S = 45

        self._debrief_fired_for: Optional["date"] = None  # type: ignore[name-defined]
        self._recap_fired_for: Optional["date"] = None  # type: ignore[name-defined]
        while True:
            try:
                # Hook the ws sender lazily — _ws is set by the base's
                # _connect_and_listen and may rotate on reconnect.
                self._flattener.ws_send = self._get_ws_send_fn()
                now_ct = _dt.now(ct)
                n = await self._flattener.check_and_flatten(now_ct)
                if n:
                    logger.info(
                        f"[DAILY_FLATTEN] closed {n} position(s) at "
                        f"{self._flattener.flatten_hour:02d}:"
                        f"{self._flattener.flatten_minute:02d} CT"
                    )
                    await self._log_session_close_event(now_ct, n)

                await self._watch_flatten_grace_window(now_ct, _GRACE_S)

                # [AI-DEBRIEF-HOOK] S7 4C: fire post-flatten debrief once per
                # day after the 15:54 CT flatten, pre-17:00 globex reopen.
                await self._maybe_run_debrief()

                # B54 daily recap: fire a concise Telegram summary once
                # per day at 17:00 CT (post-debrief, pre-globex-reopen).
                await self._maybe_send_daily_recap()
            except Exception as e:
                logger.warning(f"[DAILY_FLATTEN] poll error: {e}")
            await asyncio.sleep(30)

    async def _maybe_send_daily_recap(self):
        """B54 daily 17:00 CT recap — one consolidated Telegram with the
        day's trades, P&L, win rate. Fires once per day, non-blocking."""
        try:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            now_ct = _dt.now(_ZI("America/Chicago"))
            today_ct = now_ct.date()
            if now_ct.hour < 17:
                return
            if getattr(self, "_recap_fired_for", None) == today_ct:
                return

            # Pull today's trades from trade_memory.
            trades = []
            try:
                from core.trade_memory import TradeMemory  # type: ignore
                if hasattr(self, "trade_memory") and self.trade_memory:
                    all_trades = list(getattr(self.trade_memory, "trades", []))
                    for t in all_trades:
                        ts = t.get("exit_time") or t.get("ts") or ""
                        if str(ts).startswith(str(today_ct)):
                            trades.append(t)
            except Exception:
                pass

            wins = sum(1 for t in trades if t.get("pnl_dollars", 0) > 0)
            losses = sum(1 for t in trades if t.get("pnl_dollars", 0) < 0)
            pnl = sum(float(t.get("pnl_dollars", 0) or 0) for t in trades)
            n = len(trades)
            wr = (wins / n * 100) if n else 0.0

            from core import telegram_notifier as tg
            await tg.notify_daily_summary(
                daily_pnl=pnl, trades=n, wins=wins, losses=losses,
                win_rate=wr,
                recovery_mode=bool(getattr(self.risk.state, "recovery_mode", False)),
            )

            # B71: directional-conflicts section of the 17:00 CT briefing.
            try:
                section = _build_conflict_recap_section(today_ct, trades)
                if section:
                    from core.telegram_notifier import send_sync as _tg_send
                    _tg_send(section, dedup_key=f"conflict_recap:{today_ct}")
            except Exception as _e:
                logger.warning(f"[DAILY_RECAP] conflict section skipped: {_e}")

            self._recap_fired_for = today_ct
            logger.info(f"[DAILY_RECAP] sent: pnl=${pnl:.2f} "
                         f"{n}t {wins}W/{losses}L wr={wr:.0f}%")
        except Exception as e:
            logger.warning(f"[DAILY_RECAP] skipped: {e}")

    async def _maybe_run_debrief(self):
        """[AI-DEBRIEF-HOOK] Run the S7 session debriefer once per day,
        post-flatten. Safe-by-default — never raises into the poll loop.

        B84: fire after the 15:54 CT flatten (was 16:00 CT pre-B84).
        Gate reads the flatten time from the live DailyFlattener so the
        debrief window auto-tracks any future reschedule.
        """
        try:
            from datetime import datetime as _dt, date as _date, time as _time
            from zoneinfo import ZoneInfo as _ZI
            now_ct = _dt.now(_ZI("America/Chicago"))
            today_ct = now_ct.date()
            # Only after the daily flatten has fired, and only once per day.
            flatten_cutoff = _time(
                self._flattener.flatten_hour, self._flattener.flatten_minute,
            )
            if now_ct.time() < flatten_cutoff:
                return
            if getattr(self, "_debrief_fired_for", None) == today_ct:
                return
            from agents.session_debriefer import run_session_debrief
            path = await run_session_debrief(target_date=today_ct, bot_name="sim")
            self._debrief_fired_for = today_ct
            if path:
                logger.info(f"[AI-DEBRIEF-HOOK] debrief written: {path}")
        except Exception as e:
            logger.warning(f"[AI-DEBRIEF-HOOK] debrief skipped: {e}")

    # ─── [COUNCIL-AUTO] Auto-trigger CouncilGate ───────────────────
    # One poller fires agents.council_gate.CouncilGate:
    #   Regime shift — when SessionManager.get_current_regime() changes,
    #   debounced at 15 min so rapid flips don't spam.
    # Wraps CouncilGate().run(ctx) in try/except so the bot never
    # crashes on council failure (safe_call semantics).
    # (Daily 08:30 CT session-open trigger was dropped per Jennifer
    # 2026-04-21 before ship.)

    COUNCIL_REGIME_DEBOUNCE_S = 15 * 60  # 15 minutes

    def _build_council_ctx(self, trigger: str) -> dict:
        """Build a minimal ctx for CouncilGate — market snapshot if the
        aggregator has one, else empty."""
        market: dict = {}
        try:
            agg = getattr(self, "aggregator", None)
            if agg is not None and hasattr(agg, "snapshot"):
                snap = agg.snapshot()
                if isinstance(snap, dict):
                    market = snap
        except Exception:
            market = {}
        return {"market": market, "trigger": trigger}

    async def _fire_council(self, trigger: str, reason: str) -> None:
        """Fire the CouncilGate once. Never raises."""
        try:
            from agents.council_gate import CouncilGate
            ctx = self._build_council_ctx(trigger)
            logger.info(f"[COUNCIL] fired: {reason}")
            gate = CouncilGate()
            result = await gate.run(ctx)
            verdict = (result or {}).get("verdict", "?") if isinstance(result, dict) else "?"
            score = (result or {}).get("score", "?") if isinstance(result, dict) else "?"
            logger.info(f"[COUNCIL] verdict={verdict} score={score} (trigger={trigger})")
        except Exception as e:
            logger.warning(f"[COUNCIL] auto-fire failed ({reason}): {e}")

    async def _council_regime_shift_loop(self):
        """Poll every 30s; on regime transition fire CouncilGate with a
        15-min debounce so rapid flips can't spam the council."""
        import time as _time_mod
        while True:
            try:
                sm = getattr(self, "session", None)
                if sm is not None:
                    new_regime = sm.get_current_regime()
                    prev = self._last_seen_regime
                    # Seed on first observation — don't fire on cold start.
                    if prev is None:
                        self._last_seen_regime = new_regime
                    elif new_regime != prev:
                        now_mono = _time_mod.monotonic()
                        since = now_mono - self._last_regime_council_ts
                        if since >= self.COUNCIL_REGIME_DEBOUNCE_S:
                            self._last_regime_council_ts = now_mono
                            self._last_seen_regime = new_regime
                            await self._fire_council(
                                trigger="regime_shift",
                                reason=f"regime-shift fired: {prev}->{new_regime}",
                            )
                        else:
                            logger.debug(
                                f"[COUNCIL] regime_shift {prev}->{new_regime} "
                                f"debounced ({since:.0f}s < "
                                f"{self.COUNCIL_REGIME_DEBOUNCE_S}s)"
                            )
                            # Still update last_seen so we don't re-fire on
                            # the same transition once the debounce lifts.
                            self._last_seen_regime = new_regime
            except Exception as e:
                logger.warning(f"[COUNCIL] regime-shift poll error: {e}")
            await asyncio.sleep(30)

    def _get_ws_send_fn(self):
        """Build a trade-send closure bound to the current ws, or None.

        The DailyFlattener calls this with (trade_id, reason) to issue
        an EXIT for a specific position. Uses base_bot's _exit_trade
        semantics — send WS trade-msg for EXIT with the correct account.
        """
        ws = getattr(self, "_ws", None)
        if ws is None:
            return None

        import json

        async def _send(trade_id: str, reason: str = "daily_flatten_1554CT"):
            pos = self.positions.get_position(trade_id)
            if pos is None:
                return
            # B75: skip pre-exit CANCEL_ALL (NT8 ATI ignores account
            # scoping on CANCELALLORDERS → wipes OCOs on every connected
            # account). EXIT MARKET flattens the position; NT8 OCO
            # auto-cancels the orphan stop+target legs.
            try:
                await ws.send(json.dumps({
                    "type": "trade", "trade_id": trade_id,
                    "action": "EXIT", "qty": pos.contracts,
                    "reason": reason,
                    "account": pos.account,
                    "sub_strategy": pos.sub_strategy,
                }))
            except Exception as e:
                logger.error(f"[DAILY_FLATTEN] WS EXIT send failed for {trade_id}: {e}")

        return _send

    def _evaluate_strategies(self):
        """OVERRIDE: Run strategies with zero bot-level gates; per-strategy
        halt + cap checks come from the registry.

        Differences from BaseBot._evaluate_strategies():
        1. No prod trading window check (trades 24/7).
        2. No session.is_strategy_allowed() check.
        3. Strategy configs already zeroed via SIM_STRATEGY_OVERRIDES.
        4. Per-strategy halt check — skip halted strategies, emit log.
        5. Per-strategy flat check — a strategy can enter if ITS slot
           is free, regardless of other strategies' open positions
           (Phase C concurrent execution).
        6. Queues signal via self._pending_signal; base tick-loop
           dispatches to _enter_trade which performs real NT8 execution
           with the per-strategy account.
        """
        # Re-enforce zero gates on every eval (guard against dashboard
        # tightening at runtime).
        for strat in self.strategies:
            if strat.name in SIM_STRATEGY_OVERRIDES:
                for k, v in SIM_STRATEGY_OVERRIDES[strat.name].items():
                    strat.config[k] = v

        session_info = self.session.to_dict()

        self._last_eval = {
            "ts": datetime.now().isoformat(),
            "regime": session_info.get("regime", "?"),
            "risk_blocked": None,
            "strategies": [],
            "best_signal": None,
            "sim_mode": "ZERO_GATES_PER_STRATEGY_RISK",
        }

        bars_5m = list(self.aggregator.bars_5m.completed)
        bars_1m = list(self.aggregator.bars_1m.completed)
        if len(bars_1m) < 1:
            self._last_eval["risk_blocked"] = f"Warming up ({len(bars_1m)} bars)"
            return

        market = self.aggregator.snapshot()

        # Standard snapshot enrichment (same as base)
        market["rsi"] = self.rsi_divergence.get_current_rsi()
        market["rsi_divergence"] = self._last_rsi_divergence
        market["htf_patterns"] = self.htf_scanner.get_state().get("active_patterns", [])
        try:
            smc_state = self.smc.get_state()
            market["smc_structure"] = smc_state.get("structure")
            market["smc_recent"] = smc_state.get("recent_signals", [])[-3:]
        except Exception:
            pass
        try:
            hmm_state = self.hmm_regime.get_state()
            market["hmm_regime"] = hmm_state.get("regime")
            market["hmm_confidence"] = hmm_state.get("confidence", 0)
            market["hmm_change_point"] = hmm_state.get("change_point", False)
        except Exception:
            pass
        # B14 gamma enrichment (same path as base_bot).
        try:
            self._enrich_market_with_gamma(market)
        except Exception:
            pass

        # Bot-wide kill switch still honored (emergency halt).
        if self.risk.state.killed:
            self._last_eval["risk_blocked"] = f"Kill switch: {self.risk.state.kill_reason}"
            return

        # Reset bot-wide cooloff every eval (sim never cools off at bot level).
        self.risk.state.cooloff_until = 0

        logger.info(f"[SIM EVAL] price={market.get('price',0):.2f} "
                    f"regime={session_info.get('regime','?')} "
                    f"bars_1m={len(bars_1m)} bars_5m={len(bars_5m)} "
                    f"active_positions={self.positions.active_count}")

        best_signal = None
        pending_signals = []
        for strat in self.strategies:
            if not strat.enabled:
                self._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_DISABLED"})
                continue

            # Per-strategy halt check — registry is source of truth.
            strat_key = strat.name
            if self.risk_registry.is_halted(strat_key):
                reason = self.risk_registry.halt_reason(strat_key) or "halted"
                self._last_eval["strategies"].append({
                    "name": strat.name, "result": "HALTED", "reason": reason,
                })
                continue

            # Per-strategy flat check — strategy can't open a new trade if
            # its slot is already occupied (separate from other strategies).
            if not self.positions.is_flat_for(strat.name):
                self._last_eval["strategies"].append({
                    "name": strat.name, "result": "SKIP_IN_TRADE",
                })
                continue

            # Per-strategy daily cap (from registry).
            rm = self.risk_registry.get(strat_key)
            can_trade, reason = rm.can_trade()
            if not can_trade:
                self._last_eval["strategies"].append({
                    "name": strat.name, "result": "DAILY_CAP", "reason": reason,
                })
                continue

            try:
                signal = strat.evaluate(market, bars_5m, bars_1m, session_info)
                if signal:
                    signal.entry_score = max(30, signal.entry_score)
                    logger.info(f"  [SIM:{strat.name}] SIGNAL: {signal.direction} "
                                f"conf={signal.confidence:.0f} — {signal.reason}")
                    self._last_eval["strategies"].append({
                        "name": strat.name, "result": "SIGNAL",
                        "direction": signal.direction,
                        "confidence": signal.confidence,
                        "reason": signal.reason,
                        "confluences": signal.confluences,
                    })
                    if signal.confidence > (best_signal.confidence if best_signal else 0):
                        best_signal = signal
                    pending_signals.append(signal)
                else:
                    reject = getattr(strat, "_last_reject", "")
                    if reject:
                        logger.info(f"  [SIM:{strat.name}] REJECTED: {reject}")
                        self._last_eval["strategies"].append({
                            "name": strat.name, "result": "REJECTED", "reason": reject,
                        })
                        strat._last_reject = ""
                    else:
                        self._last_eval["strategies"].append({
                            "name": strat.name, "result": "NO_SIGNAL",
                        })
            except Exception as e:
                logger.error(f"  [SIM:{strat.name}] ERROR: {e}")
                self._last_eval["strategies"].append({
                    "name": strat.name, "result": "ERROR", "reason": str(e),
                })

        # Capture HTF state unconditionally (even when no signal)
        try:
            self._last_eval["htf_state"] = self.htf_scanner.get_state()
        except Exception:
            pass

        if best_signal:
            # SMC + HTF confluence boost (same as lab/base)
            try:
                smc_conf = self.smc.get_confluence_score(best_signal.direction)
                if smc_conf["aligned_count"] > 0 and smc_conf["score"] > 30:
                    smc_boost = min(20, int(smc_conf["score"] / 4))
                    best_signal.confidence = min(100, best_signal.confidence + smc_boost)
                    best_signal.confluences.append(f"SMC {smc_conf['strongest_pattern']} +{smc_boost}")
            except Exception:
                pass
            try:
                htf_conf = self.htf_scanner.get_confluence_score(best_signal.direction)
                if htf_conf.get("aligned_count", 0) >= 2 and htf_conf.get("score", 0) > 30:
                    htf_boost = min(15, int(htf_conf["score"] / 5))
                    best_signal.confidence = min(100, best_signal.confidence + htf_boost)
                    best_signal.confluences.append(
                        f"HTF {htf_conf.get('strongest','')} ({htf_conf.get('strongest_tf','')}) +{htf_boost}"
                    )
                self._last_eval["htf_confluence"] = htf_conf
            except Exception:
                pass

            self._last_eval["best_signal"] = {
                "direction": best_signal.direction,
                "strategy": best_signal.strategy,
                "confidence": best_signal.confidence,
                "reason": best_signal.reason,
            }
            self.last_signal = {
                "direction": best_signal.direction,
                "strategy": best_signal.strategy,
                "confidence": best_signal.confidence,
                "entry_score": best_signal.entry_score,
                "reason": best_signal.reason,
                "confluences": best_signal.confluences,
            }

            # Queue every eligible signal for live execution. Sim mode is for
            # per-strategy validation, so letting only the single "best" signal
            # through creates survivorship bias and suppresses otherwise valid
            # strategy/account activity.
            existing = getattr(self, "_pending_signals", [])
            if not isinstance(existing, list):
                existing = []
            queued_strategies = {sig.strategy for sig in existing}
            for signal in sorted(pending_signals, key=lambda s: s.confidence, reverse=True):
                if signal.strategy in queued_strategies:
                    continue
                existing.append(signal)
                queued_strategies.add(signal.strategy)
            self._pending_signals = existing
        else:
            self.last_signal = None

        self.history.log_eval(self._last_eval, market)

    def _record_trade_result_per_strategy(self, trade: dict):
        """Mirror trade close into the per-strategy registry.

        Called by _on_trade_closed. Computes the net P&L and updates the
        registry (which may fire the floor-kill halt).
        """
        pnl = trade.get("pnl_dollars", 0.0)
        strategy = trade.get("strategy")
        sub_strategy = trade.get("sub_strategy")
        if not strategy:
            return
        floor_hit = self.risk_registry.record_trade_result(
            strategy, pnl, sub_strategy=sub_strategy,
        )
        if floor_hit:
            logger.critical(
                f"[FLOOR_HIT] strategy='{strategy}'"
                f"{('/' + sub_strategy) if sub_strategy else ''} "
                f"halted — balance fell below $1500. "
                f"Re-enable via tools/reenable_strategy.py after review."
            )

    def _on_trade_closed(self, trade: dict):
        """Override to plumb per-strategy accounting on every close."""
        super()._on_trade_closed(trade)
        try:
            self._record_trade_result_per_strategy(trade)
        except Exception as e:
            logger.error(f"[SIM] registry update failed on trade close: {e}")

    def to_dict(self) -> dict:
        """Extend base state with per-strategy risk registry snapshot."""
        result = super().to_dict()
        try:
            result["strategy_risk"] = self.risk_registry.snapshot()
        except Exception as e:
            logger.debug(f"to_dict: strategy_risk failed: {e}")
            result["strategy_risk"] = {}
        return result

    def set_profile(self, profile_name: str):
        """IGNORED — Sim bot has zero gates at bot level; per-strategy
        risk is governed by the registry."""
        logger.info(f"[SIM] Profile '{profile_name}' ignored — ZERO GATE mode")
        self._runtime_params.update(SIM_ZERO_GATE)

    def update_runtime_params(self, updates: dict):
        """Accept dashboard updates but re-enforce zero gates."""
        self._runtime_params.update(updates)
        for key, zero_val in SIM_ZERO_GATE.items():
            current = self._runtime_params.get(key)
            if current is not None:
                if key in ("risk_per_trade", "max_daily_loss"):
                    self._runtime_params[key] = max(current, zero_val)
                else:
                    self._runtime_params[key] = min(current, zero_val)
        if "risk_per_trade" in updates:
            self.risk.set_risk_per_trade(self._runtime_params["risk_per_trade"])
        if "max_daily_loss" in updates:
            self.risk.set_daily_limit(self._runtime_params["max_daily_loss"])


def _split_key(key: str) -> tuple[str, str | None]:
    """Split registry key into (strategy, sub_strategy) tuple.

    'opening_session.orb' → ('opening_session', 'orb')
    'bias_momentum'       → ('bias_momentum', None)
    """
    if "." in key:
        strat, sub = key.split(".", 1)
        return strat, sub
    return key, None


def main():
    bot = SimBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Sim bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
