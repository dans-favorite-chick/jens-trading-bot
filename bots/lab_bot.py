"""
Phoenix Bot — Lab (Experimental) Bot

ZERO GATES. Trades EVERYTHING. The lab bot's sole purpose is to
aggressively test every theory and gather maximum data. It fires
on every signal from every strategy in every regime — long AND short.

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

logging.basicConfig(
    level=logging.INFO,
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
    "max_daily_loss": 200.0,        # Very high daily limit — let it trade
}

# Strategy overrides: all gates REMOVED
LAB_STRATEGY_OVERRIDES = {
    "bias_momentum": {
        "min_confluence": 0.0,       # No confluence gate
        "min_tf_votes": 1,           # Just 1 TF agreeing is enough
        "min_momentum": 0,           # No momentum gate
        "stop_ticks": 8,
        "target_rr": 1.5,
    },
    "spring_setup": {
        "min_wick_ticks": 3,         # Tiny wicks count
        "require_vwap_reclaim": False,  # No VWAP gate
        "require_delta_flip": False,    # No delta gate
        "stop_multiplier": 1.5,
        "target_rr": 1.5,
    },
    "vwap_pullback": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,           # Just 1 TF
        "stop_ticks": 8,
        "target_rr": 1.5,
    },
    "high_precision_only": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,           # Down from 4 to 1
        "min_precision": 0,          # No precision gate
        "stop_ticks": 8,
        "target_rr": 1.5,
    },
    "ib_breakout": {
        "min_confluence": 0.0,
        "min_tf_votes": 1,
        "stop_ticks": 10,
        "target_rr": 1.5,
        "ib_minutes": 15,            # Shorter IB window — faster signals
        "max_ib_width_atr_mult": 5.0,  # Almost never reject for width
        "all_regimes": True,          # Trade IB breakout in ALL regimes
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

            # Record signal
            self.tracker.record_signal(
                strategy=best_signal.strategy,
                direction=best_signal.direction,
                confidence=best_signal.confidence,
                taken=False,
                regime=session_info.get("regime", "UNKNOWN"),
                trade_id=best_signal.trade_id,
            )
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
            self._pending_signal = best_signal
            logger.info(f"[LAB TRADE QUEUED] {best_signal.direction} via {best_signal.strategy} "
                         f"conf={best_signal.confidence:.0f}")
        else:
            self.last_signal = None
            self._pending_signal = None

        self.history.log_eval(self._last_eval, market)

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
