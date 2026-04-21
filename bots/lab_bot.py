"""
Phoenix Bot — Lab (Experimental) Bot

ZERO GATES. Observes EVERYTHING. The lab bot's sole purpose is to
aggressively test every theory and gather maximum data. It evaluates
every signal from every strategy in every regime — long AND short.

OBSERVE-ONLY DURING MARKET HOURS:
  - Zero NT8 communication. No OIF files written. No bridge sends.
  - Paper trades are tracked INTERNALLY (positions, P&L, stop/target,
    expectancy, MAE/MFE, history) — full analytics, zero execution.
  - All data flows to the dashboard, history logs, and RAG exactly
    as if the trades had been real. Gives clean backtest-quality data.

No TF vote minimums. No momentum gates. No confluence filters.
No regime restrictions. No session window limits.
Wider daily loss limit. Higher max trades.

"Anyone can check the tape after the fact. We're looking through
the windshield, not the rearview mirror."
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

logging.basicConfig(
    # Lab runs at DEBUG to surface Fix 5 [EVAL] BLOCKED/SKIP/NO_SIGNAL
    # reject-reason logs for strategy observability. Prod stays at INFO
    # (see bots/prod_bot.py) so production logs stay quiet.
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("LabBot")

# ─── ZERO GATE Settings ──────────────────────────────────────────
# Every threshold at absolute minimum. The lab bot trades EVERYTHING.
LAB_ZERO_GATE = {
    "min_confluence": 0.0,          # No confluence gate
    "min_momentum": 0,              # No momentum gate
    "min_momentum_confidence": 0,   # No confidence gate
    "min_precision": 0,             # No precision gate
    "risk_per_trade": 15.0,         # Standard risk per trade
    "max_daily_loss": 1000.0,       # Data-collection mode: wide cap to absorb lab exploration. Adjust after 50+ trades/strategy collected and validation triggered.
}

# Strategy overrides: all gates REMOVED
LAB_STRATEGY_OVERRIDES = {
    "bias_momentum": {
        "min_confluence": 0.0,       # No confluence gate
        "min_tf_votes": 1,           # Just 1 TF agreeing is enough
        "min_momentum": 0,           # No momentum gate
        "skip_regime_overrides": True, # Bypass hardcoded regime gates
        "stop_ticks": 14,            # Floor (ATR stop overrides when warm — typically 20-40t)
        "target_rr": 20.0,           # 20:1 — reversal+stall exit drives this, not the OCO bracket
        "max_ema_dist_ticks": 999,   # Lab: disable extension gate — collect data at all distances
    },
    "spring_setup": {
        "min_wick_ticks": 3,            # Tiny wicks count
        "require_vwap_reclaim": False,  # No VWAP gate
        "require_delta_flip": False,    # No delta gate
        "require_tf_alignment": False,  # NO TF gate — lab observes counter-trend springs
        "skip_regime_overrides": True,
        "stop_multiplier": 1.5,         # Fallback only — ATR stop runs if ATR_5m available
        "target_rr": 5.0,              # Raised from 1.5 — spring setups can run 20-50pts
        "atr_stop_multiplier": 1.1,
        # NQ research clamps aligned with Fix 7 main config (2026-04-20)
        "max_stop_ticks": 120,
        "min_stop_ticks": 40,
    },
    "vwap_pullback": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,           # Just 1 TF
        "skip_regime_overrides": True,
        "stop_ticks": 14,            # Legacy — kept for any pre-B14 readers. ATR stop supersedes.
        "max_vwap_dist_ticks": 60,   # B14: permissive VWAP proximity gate for lab data collection
        "target_rr": 20.0,           # 20:1 — reversal+stall exit drives this
    },
    "high_precision_only": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,           # Down from 4 to 1
        "min_precision": 0,          # No precision gate
        "skip_regime_overrides": True,
        "stop_ticks": 14,
        "target_rr": 5.0,            # Raised from 1.5 — high precision needs big targets
    },
    "ib_breakout": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,
        "skip_regime_overrides": True,
        "stop_ticks": 10,
        "target_rr": 5.0,            # IB breakouts naturally run 50-200pts — let them
        "ib_minutes": 15,
        "max_ib_width_atr_mult": 5.0,
        "max_stop_ticks": 120,       # Fix 8 ceiling guard — skip signal if structural stop > 120t
        "all_regimes": True,
        "require_cvd_confirm": False,
    },
    "dom_pullback": {
        # Lab: loosen DOM threshold so we collect data across all absorption levels
        "min_dom_strength": 10,        # Very loose — capture any DOM absorption signal
        "max_ema_dist_ticks": 28,      # Matches prod (data-validated P25 zone)
        "max_vwap_dist_ticks": 20,
        "skip_regime_overrides": True,
        "stop_ticks": 10,
        "target_rr": 20.0,           # 20:1 — reversal+stall exit drives this
    },
    "vwap_band_pullback": {
        # Lab: loosen HTF alignment so the 1σ-band algorithm can fire
        # across more setups for data collection. Preserves RSI(2) +
        # volume gates (those are the algorithm's core).
        "skip_regime_overrides": True,
        "min_volume_ratio": 0.5,     # Lab: broader volume tolerance
        "target_rr": 5.0,            # 5:1 — exit on stall or trend break
    },
    "opening_session": {
        # Lab mirrors prod defaults — thresholds are already tuned
        # research values (Fix 6 stops, volume/wick ratios per paper).
        # Empty override = use config/strategies.py values as-is.
    },
}


class LabBot(BaseBot):
    bot_name = "lab"
    only_validated = False  # Runs ALL strategies, including unvalidated

    def load_strategies(self):
        """Load all strategies with ZERO gates."""
        super().load_strategies()

        # Apply per-strategy zero-gate overrides
        for strat in self.strategies:
            if strat.name in LAB_STRATEGY_OVERRIDES:
                for k, v in LAB_STRATEGY_OVERRIDES[strat.name].items():
                    strat.config[k] = v
                logger.info(f"[LAB] ZERO GATE override: {strat.name}")

        # Set zero-gate runtime params
        self._runtime_params.update(LAB_ZERO_GATE)

        # Push risk params immediately
        self.risk.set_risk_per_trade(LAB_ZERO_GATE["risk_per_trade"])
        self.risk.set_daily_limit(LAB_ZERO_GATE["max_daily_loss"])
        self.risk.set_max_trades(200)  # Allow up to 200 trades per day

        # Disable cooloff — lab bot never stops
        from config.settings import COOLOFF_AFTER_CONSECUTIVE_LOSSES
        # Override in risk state directly
        self.risk.state.cooloff_until = 0

        logger.info(f"[LAB] {len(self.strategies)} strategies loaded — ZERO GATES ACTIVE")
        logger.info(f"[LAB] Max daily loss: ${LAB_ZERO_GATE['max_daily_loss']}")
        logger.info(f"[LAB] Max trades: 200")
        logger.info(f"[LAB] All regime restrictions BYPASSED")

    def _evaluate_strategies(self):
        """OVERRIDE: Run strategies with ALL gates bypassed.

        Differences from BaseBot._evaluate_strategies():
        1. No prod trading window check (trades 24/7)
        2. No session.is_strategy_allowed() check (all strategies in all regimes)
        3. Strategy configs already zeroed out (min_tf_votes=1, min_momentum=0, etc.)
        4. Risk tier SKIP bypassed (entry_score floor = 30)
        5. Takes EVERY signal, not just the best one (queues first valid signal)
        """
        if not self.positions.is_flat:
            return  # Already in a trade — wait for exit

        # Re-enforce zero gates on every eval (in case dashboard tried to tighten)
        for strat in self.strategies:
            if strat.name in LAB_STRATEGY_OVERRIDES:
                for k, v in LAB_STRATEGY_OVERRIDES[strat.name].items():
                    strat.config[k] = v

        # Session info (for logging, NOT for gating)
        session_info = self.session.to_dict()

        # Build eval record for dashboard
        self._last_eval = {
            "ts": datetime.now().isoformat(),
            "regime": session_info.get("regime", "?"),
            "risk_blocked": None,
            "strategies": [],
            "best_signal": None,
            "lab_mode": "ZERO_GATES",
        }

        # Minimal warmup — just 1 bar
        bars_5m = list(self.aggregator.bars_5m.completed)
        bars_1m = list(self.aggregator.bars_1m.completed)
        if len(bars_1m) < 1:
            self._last_eval["risk_blocked"] = f"Warming up ({len(bars_1m)} bars)"
            return

        market = self.aggregator.snapshot()

        # Enrich market snapshot (same as base)
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

        # Risk gate — ONLY check kill switch and daily loss (no cooloff, no max trades)
        if self.risk.state.killed:
            self._last_eval["risk_blocked"] = f"Kill switch: {self.risk.state.kill_reason}"
            return
        if self.risk.state.daily_pnl <= -self.risk._daily_limit:
            self._last_eval["risk_blocked"] = f"Daily loss limit (${self.risk.state.daily_pnl:.2f})"
            return

        # Reset cooloff every eval (lab never cools off)
        self.risk.state.cooloff_until = 0

        logger.info(f"[LAB EVAL] price={market.get('price',0):.2f} "
                     f"regime={session_info.get('regime','?')} "
                     f"bars_1m={len(bars_1m)} bars_5m={len(bars_5m)}")

        best_signal = None
        for strat in self.strategies:
            if not strat.enabled:
                self._last_eval["strategies"].append({"name": strat.name, "result": "SKIP_DISABLED"})
                continue

            # NO regime check — lab trades in ALL regimes
            try:
                signal = strat.evaluate(market, bars_5m, bars_1m, session_info)
                if signal:
                    # Force entry_score to at least 30 (avoids risk tier SKIP)
                    signal.entry_score = max(30, signal.entry_score)
                    logger.info(f"  [LAB:{strat.name}] SIGNAL: {signal.direction} "
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
                else:
                    reject = getattr(strat, '_last_reject', '')
                    if reject:
                        logger.info(f"  [LAB:{strat.name}] REJECTED: {reject}")
                        self._last_eval["strategies"].append({"name": strat.name, "result": "REJECTED", "reason": reject})
                        strat._last_reject = ''
                    else:
                        self._last_eval["strategies"].append({"name": strat.name, "result": "NO_SIGNAL"})
            except Exception as e:
                logger.error(f"  [LAB:{strat.name}] ERROR: {e}")
                self._last_eval["strategies"].append({"name": strat.name, "result": "ERROR", "reason": str(e)})

        # ── Capture HTF state unconditionally (even when no signal) ─────
        # This mirrors the fix in base_bot so lab logs always have htf_state.
        try:
            htf_state = self.htf_scanner.get_state()
            self._last_eval["htf_state"] = htf_state
        except Exception:
            pass

        if best_signal:
            # Apply confluence boosts (SMC, RSI, HTF — same as base)
            try:
                smc_conf = self.smc.get_confluence_score(best_signal.direction)
                if smc_conf["aligned_count"] > 0 and smc_conf["score"] > 30:
                    smc_boost = min(20, int(smc_conf["score"] / 4))
                    best_signal.confidence = min(100, best_signal.confidence + smc_boost)
                    best_signal.confluences.append(f"SMC {smc_conf['strongest_pattern']} +{smc_boost}")
            except Exception:
                pass

            # HTF confluence boost (same as base_bot for data consistency)
            try:
                htf_conf = self.htf_scanner.get_confluence_score(best_signal.direction)
                htf_boost = 0
                if htf_conf.get("aligned_count", 0) >= 2 and htf_conf.get("score", 0) > 30:
                    htf_boost = min(15, int(htf_conf["score"] / 5))
                    best_signal.confidence = min(100, best_signal.confidence + htf_boost)
                    best_signal.confluences.append(
                        f"HTF {htf_conf.get('strongest', '')} ({htf_conf.get('strongest_tf', '')}) +{htf_boost}"
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
            # ── OBSERVE ONLY — paper enter, never touch NT8 ─────────
            # Do NOT set self._pending_signal (which would trigger
            # _enter_trade → ws.send → bridge → OIF → NT8).
            # Instead, open a paper position internally right now.
            self._paper_enter(best_signal, market)
            logger.info(f"[LAB PAPER] {best_signal.direction} via {best_signal.strategy} "
                         f"conf={best_signal.confidence:.0f} — INTERNAL ONLY, no NT8")
        else:
            self.last_signal = None
            # DON'T clear _pending_signal here — a prior eval may have set it
            # and the tick loop hasn't consumed it yet. Clearing it causes a
            # race condition where rapid 1m+5m bar evals wipe signals.

        self.history.log_eval(self._last_eval, market)

    # ─── Paper Trade Implementation ───────────────────────────────────
    # These replace _enter_trade / _exit_trade for the lab bot.
    # All internal analytics still run (positions, expectancy, history,
    # strategy_tracker, trade_memory, RAG). Zero NT8 communication.

    def _paper_enter(self, signal: Signal, market: dict):
        """Open a paper position internally at current price. No NT8, no OIF."""
        if not self.positions.is_flat:
            return  # Already in a paper position

        price = market.get("price", 0)
        if not price:
            return

        tid = signal.trade_id
        stop_ticks = max(4, signal.stop_ticks)   # Floor at 4t
        contracts  = 1                            # Lab: always 1 contract

        risk_dollars = stop_ticks * TICK_SIZE * contracts * 2  # MNQ: $2/tick/contract

        # Stop and target from signal direction
        if signal.direction == "LONG":
            stop_price   = round(price - stop_ticks * TICK_SIZE, 2)
            target_price = round(price + stop_ticks * TICK_SIZE * signal.target_rr, 2)
        else:
            stop_price   = round(price + stop_ticks * TICK_SIZE, 2)
            target_price = round(price - stop_ticks * TICK_SIZE * signal.target_rr, 2)

        # Tag market snapshot with entry context (same fields as live trades)
        market["regime"]       = self.session.get_current_regime()
        market["signal_price"] = price
        market["paper_trade"]  = True   # Marker: never executed in NT8

        # Open position internally
        self.positions.open_position(
            trade_id=tid,
            direction=signal.direction,
            entry_price=price,
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            strategy=signal.strategy,
            reason=signal.reason,
            market_snapshot=market,
        )
        self.status = "IN_TRADE"

        # Start expectancy tracking (MAE/MFE accumulates on every tick)
        try:
            self.expectancy.start_tracking(
                trade_id=tid,
                direction=signal.direction,
                entry_price=price,
                signal_price=price,
                stop_price=stop_price,
                target_price=target_price,
                strategy=signal.strategy,
                regime=self.session.get_current_regime(),
            )
        except Exception as e:
            logger.debug(f"[LAB PAPER:{tid}] expectancy start error (non-blocking): {e}")

        # Consume any active regime transition bonus (same as live)
        try:
            self.regime_transitions.mark_signal_used()
        except Exception:
            pass

        # Record signal as TAKEN (paper fill = taken for tracking purposes)
        self.tracker.record_signal(
            strategy=signal.strategy,
            direction=signal.direction,
            confidence=signal.confidence,
            taken=True,
            regime=self.session.get_current_regime(),
            trade_id=tid,
        )

        # Log history entry (same schema as live trades)
        self.history.log_entry(
            signal, price, contracts, stop_price, target_price,
            risk_dollars, "PAPER_LAB", market,
        )

        logger.info(
            f"[LAB PAPER FILL:{tid}] {signal.direction} {contracts}x @ {price:.2f} | "
            f"SL={stop_price:.2f}  TP={target_price:.2f}  "
            f"risk=${risk_dollars:.2f}  strat={signal.strategy}  "
            f"INTERNAL ONLY — NT8 NOT TOUCHED"
        )

    async def _exit_trade(self, ws, price: float, reason: str):
        """Paper exit — close position internally. Zero NT8 / bridge / OIF communication."""
        if self.positions.is_flat:
            return

        pos = self.positions.position
        tid = pos.trade_id
        self.status = "EXIT_PENDING"
        logger.info(
            f"[LAB PAPER EXIT:{tid}] {pos.direction} @ {price:.2f}  "
            f"reason={reason}  INTERNAL ONLY"
        )

        # Close Python position (no NT8 send first — paper trade has no NT8 position)
        trade = self.positions.close_position(price, reason)
        if not trade:
            self.status = "IDLE"
            return

        self.risk.record_trade(trade["pnl_dollars"])
        self.trade_memory.record(trade)
        self.tracker.record_trade(trade)

        # MAE/MFE analysis
        exp_analysis = None
        try:
            exp_analysis = self.expectancy.close_trade(
                exit_price=price,
                pnl_ticks=trade["pnl_ticks"],
                result=trade["result"],
            )
        except Exception as e:
            logger.debug(f"[LAB PAPER EXIT:{tid}] expectancy close error: {e}")

        # Log exit with MAE/MFE (same schema as live)
        market_snap = self.aggregator.snapshot()
        market_snap["paper_trade"] = True
        if exp_analysis:
            market_snap["mae_ticks"]      = exp_analysis.get("mae_ticks")
            market_snap["mfe_ticks"]      = exp_analysis.get("mfe_ticks")
            market_snap["capture_ratio"]  = exp_analysis.get("edge_captured_pct")
            market_snap["went_red_first"] = exp_analysis.get("went_red_first")
            market_snap["mae_time_s"]     = exp_analysis.get("mae_time_s")
            market_snap["mfe_time_s"]     = exp_analysis.get("mfe_time_s")
        self.history.log_exit(trade, market_snap)

        # RAG vector storage
        try:
            rag_outcome = {
                "mae_ticks":    market_snap.get("mae_ticks", 0),
                "mfe_ticks":    market_snap.get("mfe_ticks", 0),
                "capture_ratio": market_snap.get("capture_ratio", 0),
                "hold_seconds": trade.get("hold_time_s", 0),
                "exit_reason":  trade.get("exit_reason", ""),
            }
            self.trade_rag.add_trade(trade, market_snap, rag_outcome)
        except Exception as e:
            logger.debug(f"[LAB PAPER EXIT:{tid}] RAG add error (non-blocking): {e}")

        # Loss fingerprinting and counter-edge learning
        if trade["result"] == "LOSS":
            try:
                self.no_trade_fp.learn_from_trade(trade, self.aggregator.snapshot())
            except Exception:
                pass
            try:
                self.counter_edge.learn_from_loss(trade)
            except Exception:
                pass

        # Execution quality (fill_latency=0 for paper)
        try:
            self.execution_quality.record(
                trade_id=trade.get("trade_id", ""),
                signal_price=trade["entry_price"],
                entry_price=trade["entry_price"],
                exit_price=trade["exit_price"],
                pnl_ticks=trade["pnl_ticks"],
                fill_latency_ms=0,
                strategy=trade["strategy"],
                regime=market_snap.get("regime", "UNKNOWN"),
            )
        except Exception:
            pass

        pnl = trade["pnl_dollars"]
        result = trade["result"]
        logger.info(
            f"[LAB PAPER CLOSED:{tid}] {result}  P&L=${pnl:+.2f}  "
            f"exit={price:.2f}  reason={reason}  "
            f"hold={trade.get('hold_time_s', 0):.0f}s"
        )

        # Telegram notification (informational — marks as paper)
        import core.telegram_notifier as tg
        asyncio.ensure_future(tg.notify_exit(
            trade_id=trade.get("trade_id", ""),
            direction=trade["direction"],
            strategy=f"[PAPER] {trade['strategy']}",
            entry_price=trade["entry_price"],
            exit_price=trade["exit_price"],
            pnl_dollars=pnl,
            pnl_ticks=trade["pnl_ticks"],
            result=result,
            exit_reason=reason,
            hold_time_s=trade["hold_time_s"],
        ))

        self.status = "IDLE"

    def set_profile(self, profile_name: str):
        """IGNORED — Lab bot has zero gates."""
        logger.info(f"[LAB] Profile '{profile_name}' ignored — ZERO GATE mode")
        self._runtime_params.update(LAB_ZERO_GATE)

    def update_runtime_params(self, updates: dict):
        """Accept updates but re-enforce zero gates."""
        self._runtime_params.update(updates)
        # Clamp back to zero-gate minimums
        for key, zero_val in LAB_ZERO_GATE.items():
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


def main():
    bot = LabBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Lab bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
