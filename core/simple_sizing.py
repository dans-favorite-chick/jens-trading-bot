"""
Phoenix Bot — Simple Position Sizing

Fixed 1-contract sizing appropriate for small accounts (< $1500).
Reads memory/procedural/small_account_config.yaml (seeded Saturday build).
Kelly sizing intentionally NOT used — below $1500 account you can't
fractionally size MNQ contracts, so Kelly math becomes cosmetic.

When account grows ≥ $1500, core/kelly_sizing.py (to be built) can
replace this module. For now, it's the sizing source of truth.

Behavior:
  - Always returns 1 contract for entries
  - Enforces max loss per trade (config-driven, default $5 for small account)
  - Loss-streak cooldown: 2 consecutive losses → skip next 5 min of signals
  - Returns dict with sizing + stop + reason-trail
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("SimpleSizing")

PHOENIX_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PHOENIX_ROOT / "memory" / "procedural" / "small_account_config.yaml"

# Fallback defaults if YAML not yet present (Saturday will create the file)
DEFAULT_CONFIG = {
    "max_loss_per_trade_usd": 5.0,
    "max_daily_loss_usd": 15.0,
    "contracts_per_trade": 1,
    "max_trades_per_day": 4,
    "veto_low_conviction_threshold": 80,
    "loss_streak_cooldown_minutes": 5,
}


def _load_config() -> dict:
    """Read small_account_config.yaml with fallback to defaults."""
    if not CONFIG_PATH.exists():
        logger.debug(f"[SIZING] Config not found at {CONFIG_PATH}, using defaults")
        return DEFAULT_CONFIG.copy()
    try:
        try:
            import yaml
            with open(CONFIG_PATH, "r") as f:
                full = yaml.safe_load(f) or {}
            # Accept either flat dict or nested "small_account_mode" key
            cfg = full.get("small_account_mode", full)
            # Merge with defaults for any missing keys
            merged = DEFAULT_CONFIG.copy()
            merged.update({k: v for k, v in cfg.items() if v is not None})
            return merged
        except ImportError:
            # YAML not installed — parse minimally for the flat key=value lines we need
            logger.warning("[SIZING] PyYAML not installed, using defaults")
            return DEFAULT_CONFIG.copy()
    except Exception as e:
        logger.warning(f"[SIZING] Config load failed ({e}), using defaults")
        return DEFAULT_CONFIG.copy()


class SimpleSizer:
    """Stateful sizer tracking recent losses for cooldown logic."""

    def __init__(self):
        self.config = _load_config()
        self._recent_outcomes: list[tuple[float, str]] = []  # (timestamp, "WIN"|"LOSS")
        self._cooldown_until: Optional[float] = None
        logger.info(f"[SIZING] SimpleSizer initialized with config: "
                    f"max_loss=${self.config['max_loss_per_trade_usd']}, "
                    f"max_daily=${self.config['max_daily_loss_usd']}, "
                    f"max_trades={self.config['max_trades_per_day']}/day")

    def record_trade_outcome(self, outcome: str) -> None:
        """Call after each closed trade. outcome in ('WIN', 'LOSS')."""
        now = time.time()
        self._recent_outcomes.append((now, outcome))
        # Prune to last 10 trades
        self._recent_outcomes = self._recent_outcomes[-10:]

        # Check for 2 consecutive losses → start cooldown
        if len(self._recent_outcomes) >= 2:
            last_two = [o for _, o in self._recent_outcomes[-2:]]
            if last_two == ["LOSS", "LOSS"]:
                cooldown_s = self.config["loss_streak_cooldown_minutes"] * 60
                self._cooldown_until = now + cooldown_s
                logger.warning(
                    f"[SIZING] 2 consecutive losses → cooldown active for "
                    f"{self.config['loss_streak_cooldown_minutes']} min"
                )

    def in_cooldown(self) -> bool:
        """Returns True if currently in loss-streak cooldown."""
        if self._cooldown_until is None:
            return False
        if time.time() >= self._cooldown_until:
            self._cooldown_until = None
            return False
        return True

    def cooldown_remaining_s(self) -> float:
        """Seconds remaining in cooldown, or 0."""
        if not self.in_cooldown():
            return 0
        return max(0, self._cooldown_until - time.time())

    def size_trade(self, signal_score: int, daily_pnl: float = 0.0,
                   trades_today: int = 0) -> dict:
        """
        Decide whether to take a signal and what size.

        Args:
            signal_score: Strategy composite score 0-100
            daily_pnl: Current day's P&L in USD (negative for losses)
            trades_today: Count of trades already taken today

        Returns:
            {
                "take_trade": bool,
                "contracts": int,
                "max_loss_dollars": float,
                "reason": str,  # if take_trade=False, the reason
                "stop_multiplier_hint": float,  # regime-aware (Saturday enhancement)
            }
        """
        cfg = self.config

        # Gate 1: cooldown
        if self.in_cooldown():
            return {
                "take_trade": False,
                "contracts": 0,
                "max_loss_dollars": 0,
                "reason": f"cooldown active, {self.cooldown_remaining_s():.0f}s remaining",
                "stop_multiplier_hint": 1.0,
            }

        # Gate 2: daily loss limit
        if daily_pnl <= -cfg["max_daily_loss_usd"]:
            return {
                "take_trade": False,
                "contracts": 0,
                "max_loss_dollars": 0,
                "reason": f"daily loss limit hit (${daily_pnl:.2f} <= -${cfg['max_daily_loss_usd']:.2f})",
                "stop_multiplier_hint": 1.0,
            }

        # Gate 3: max trades per day
        if trades_today >= cfg["max_trades_per_day"]:
            return {
                "take_trade": False,
                "contracts": 0,
                "max_loss_dollars": 0,
                "reason": f"max trades/day hit ({trades_today}/{cfg['max_trades_per_day']})",
                "stop_multiplier_hint": 1.0,
            }

        # Gate 4: signal conviction threshold
        if signal_score < cfg["veto_low_conviction_threshold"]:
            return {
                "take_trade": False,
                "contracts": 0,
                "max_loss_dollars": 0,
                "reason": f"score {signal_score} < threshold {cfg['veto_low_conviction_threshold']}",
                "stop_multiplier_hint": 1.0,
            }

        # All gates passed — size the trade
        return {
            "take_trade": True,
            "contracts": int(cfg["contracts_per_trade"]),
            "max_loss_dollars": float(cfg["max_loss_per_trade_usd"]),
            "reason": f"approved: score={signal_score}, daily_pnl=${daily_pnl:.2f}, trades_today={trades_today}",
            "stop_multiplier_hint": 1.0,  # Saturday: regime-aware override
        }


# ─── Singleton + convenience functions ─────────────────────────────────
_sizer_instance: Optional[SimpleSizer] = None


def get_sizer() -> SimpleSizer:
    global _sizer_instance
    if _sizer_instance is None:
        _sizer_instance = SimpleSizer()
    return _sizer_instance


def reset_sizer() -> None:
    """For tests: reset singleton."""
    global _sizer_instance
    _sizer_instance = None


if __name__ == "__main__":
    # Quick self-test
    logging.basicConfig(level=logging.INFO)
    sizer = get_sizer()

    print("\nTest 1 — high-conviction signal, fresh day:")
    print("  ", sizer.size_trade(signal_score=85, daily_pnl=0, trades_today=0))

    print("\nTest 2 — low-conviction signal (below threshold):")
    print("  ", sizer.size_trade(signal_score=70, daily_pnl=0, trades_today=0))

    print("\nTest 3 — at daily loss limit:")
    print("  ", sizer.size_trade(signal_score=85, daily_pnl=-15, trades_today=2))

    print("\nTest 4 — max trades hit:")
    print("  ", sizer.size_trade(signal_score=85, daily_pnl=-5, trades_today=4))

    print("\nTest 5 -- 2 losses cooldown:")
    sizer.record_trade_outcome("LOSS")
    sizer.record_trade_outcome("LOSS")
    print("  ", sizer.size_trade(signal_score=85, daily_pnl=-10, trades_today=2))
    print(f"  Cooldown remaining: {sizer.cooldown_remaining_s():.0f}s")
