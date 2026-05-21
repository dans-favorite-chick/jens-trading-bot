"""
Phoenix Bot — Tier-Based Compounding Sizer (F-001)
==================================================

Production port of the `_tier_sizing` policy from
`tools/phoenix_compounding_backtest.py`. Implements the compounding sizing
policy specified in `docs/PHOENIX_BEST_PLAN.md` §I.4 and §5.3:

  * 1 contract per $3,000 equity (configurable: ``DOLLARS_PER_CONTRACT``)
  * Hard cap at 30 contracts (CME / MNQ liquidity reality)
  * Per-strategy multipliers (``STRATEGY_SIZE_MULT``) so Tier-1 winners
    (bias_momentum, opening_session, …) get 1.5×; Tier-3 marginal strats
    (vwap_band_*) get 0.5×
  * **ATH scale-down**: if current equity < 85 % of all-time-high, drop
    one tier off the computed base
  * **4 % daily circuit breaker**: if today's loss exceeds 4 % of the
    *session-start* equity, halt new entries until the next session
  * **3-consecutive-loss halving**: after 3 losing trades in a row, the
    next trade is sized at half (floor at 1 contract). Resets on any win.

Default-OFF behaviour
---------------------
``config.settings.SIZING_MODE`` defaults to ``"flat_1"`` — every entry
sized at 1 contract, identical to today's behaviour. The operator flips
to ``"tier_3000"`` to activate F-001. See ``docs/OPERATOR_BRIEF_PT2.md``
F-001 Activation section for the exact steps.

State persistence
-----------------
Equity state lives in ``data/equity_state.json``. The file is read on
init and written on every trade close. The schema:

    {
      "starting_equity":      1500.00,
      "current_equity":       1500.00,
      "equity_ath":           1500.00,
      "session_start_equity": 1500.00,
      "session_date":         "2026-05-20",
      "session_pnl":          0.00,
      "consecutive_losses":   0,
      "last_updated_iso":     "2026-05-20T14:33:00-05:00",
      "history": []
    }

``history`` keeps the last 200 trade closes for forensics.

Logging
-------
Per Phoenix lesson I-002 (silent failures = #1 historical bug class)
every sizing decision and every guard hit is logged LOUDLY:

* INFO on every compute_contracts call
* WARNING on ATH scale-down, 3-loss halving, halt-by-daily-breaker
* INFO on session-day rollover and ATH break

Public API
----------
* ``get_tier_sizer()`` — module-level singleton
* ``compute_contracts(strategy, score, ...)`` — main entry point
* ``record_trade_close(pnl_dollars, was_winner)`` — wire at trade close
* ``is_halted_today() -> (bool, reason)`` — query for entry gate
* ``reset_tier_sizer()`` — for tests
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("TierSizer")

# ─── Constants (mirror tools/phoenix_compounding_backtest.py) ────────
DEFAULT_DOLLARS_PER_CONTRACT = 3000.0   # 1 contract per $3k equity
DD_SCALE_DOWN_PCT            = 0.85     # drop tier if equity < 85% ATH
DAILY_CIRCUIT_PCT            = 0.04     # halt day if loss > 4% of equity
CONSECUTIVE_LOSS_LIMIT       = 3        # 3 losses in a row → halve next
MAX_CONTRACTS_CAP            = 30       # physical cap
MIN_CONTRACTS                = 1
HISTORY_KEEP                 = 200      # last N trade closes retained

PHOENIX_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = PHOENIX_ROOT / "data" / "equity_state.json"


# Per-strategy multipliers. Mirrors STRATEGY_SIZE_MULT in
# tools/phoenix_compounding_backtest.py (2026-05-19 Phase 13 §U revision).
STRATEGY_SIZE_MULT: dict[str, float] = {
    # TIER 1 (top contributors = >90% of compounded P&L)
    "bias_momentum":         1.5,
    "opening_session":       1.5,
    "spring_setup":          1.3,
    "g_inside_bar_breakout": 1.3,
    "e_multi_day_breakout":  1.3,
    "vwap_pullback_v2":      1.2,
    "a_asian_continuation":  1.2,
    # TIER 2 (proven, normal weight)
    "es_nq_confluence":      1.0,
    "ib_breakout":           1.0,
    "raschke_baseline":      1.0,
    # TIER 3 (small contributors — half size pending validation)
    "vwap_band_pullback":    0.5,
    "vwap_band_reversion":   0.5,
}


# ════════════════════════════════════════════════════════════════════
# State container
# ════════════════════════════════════════════════════════════════════

@dataclass
class EquityState:
    starting_equity:      float = 1500.0
    current_equity:       float = 1500.0
    equity_ath:           float = 1500.0
    session_start_equity: float = 1500.0
    session_date:         str = ""
    session_pnl:          float = 0.0
    consecutive_losses:   int = 0
    last_updated_iso:     str = ""
    history:              list = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "EquityState":
        # Defensive: fill missing keys with defaults rather than crash
        defaults = cls()
        for k in defaults.__dataclass_fields__:
            if k in d:
                setattr(defaults, k, d[k])
        return defaults


# ════════════════════════════════════════════════════════════════════
# Sizer
# ════════════════════════════════════════════════════════════════════

class TierSizer:
    """Stateful tier-based compounding sizer. Singleton via get_tier_sizer()."""

    def __init__(
        self,
        state_path: Optional[Path | str] = None,
        starting_equity: Optional[float] = None,
        dollars_per_contract: float = DEFAULT_DOLLARS_PER_CONTRACT,
        max_contracts_cap: int = MAX_CONTRACTS_CAP,
    ):
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.dollars_per_contract = float(dollars_per_contract)
        self.max_contracts_cap = int(max_contracts_cap)
        # RLock — compute_contracts() holds the lock then calls
        # is_halted_today() which also acquires it. A plain Lock would
        # deadlock on the re-entry.
        self._lock = threading.RLock()
        self._strategy_mult = dict(STRATEGY_SIZE_MULT)

        # Load or initialize state
        self.state = self._load_or_init(starting_equity)
        logger.info(
            "[TIER_SIZER] init: state_path=%s starting=$%.2f current=$%.2f "
            "ATH=$%.2f session_start=$%.2f consec_losses=%d $/contract=$%.0f cap=%d",
            self.state_path, self.state.starting_equity, self.state.current_equity,
            self.state.equity_ath, self.state.session_start_equity,
            self.state.consecutive_losses, self.dollars_per_contract,
            self.max_contracts_cap,
        )

    # ─── state I/O ────────────────────────────────────────────────────

    def _load_or_init(self, starting_equity: Optional[float]) -> EquityState:
        # Resolve starting equity from arg → settings.STARTING_EQUITY →
        # settings.PER_STRATEGY_ACCOUNT_SIZE → hardcoded 1500.
        if starting_equity is None:
            try:
                from config.settings import STARTING_EQUITY as _se
                starting_equity = float(_se)
            except Exception:
                try:
                    from config.settings import PER_STRATEGY_ACCOUNT_SIZE as _se
                    starting_equity = float(_se)
                except Exception:
                    starting_equity = 1500.0

        if self.state_path.exists():
            try:
                with open(self.state_path, "r") as f:
                    raw = json.load(f)
                st = EquityState.from_json(raw)
                # If file was init'd with a different start, KEEP it — don't
                # silently overwrite the operator's deliberate seed.
                logger.info(
                    "[TIER_SIZER] loaded state from %s (starting=$%.2f current=$%.2f)",
                    self.state_path, st.starting_equity, st.current_equity,
                )
                # Roll session if the date has changed
                today = datetime.now().date().isoformat()
                if st.session_date != today:
                    self._roll_session(st, today)
                return st
            except Exception as e:
                logger.warning(
                    "[TIER_SIZER] failed to load %s (%r), reinitializing",
                    self.state_path, e,
                )

        # Fresh state
        today = datetime.now().date().isoformat()
        st = EquityState(
            starting_equity=starting_equity,
            current_equity=starting_equity,
            equity_ath=starting_equity,
            session_start_equity=starting_equity,
            session_date=today,
            session_pnl=0.0,
            consecutive_losses=0,
            last_updated_iso=datetime.now().isoformat(),
            history=[],
        )
        self._persist(st)
        logger.info(
            "[TIER_SIZER] initialized fresh state: starting=$%.2f -> %s",
            starting_equity, self.state_path,
        )
        return st

    def _roll_session(self, st: EquityState, today_iso: str) -> None:
        """Roll session-start equity at day boundary."""
        prev_date = st.session_date
        prev_pnl = st.session_pnl
        st.session_start_equity = st.current_equity
        st.session_date = today_iso
        st.session_pnl = 0.0
        logger.info(
            "[TIER_SIZER] session roll %s -> %s (yesterday pnl=$%.2f, "
            "today start equity=$%.2f)",
            prev_date or "(new)", today_iso, prev_pnl, st.session_start_equity,
        )
        self._persist(st)

    def _persist(self, st: Optional[EquityState] = None) -> None:
        """Atomically write state to disk."""
        st = st or self.state
        st.last_updated_iso = datetime.now().isoformat()
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(st.to_json(), f, indent=2, default=str)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning("[TIER_SIZER] persist failed (%r) — state is dirty", e)

    # ─── tier math ────────────────────────────────────────────────────

    def _base_tier(self, equity: float) -> int:
        """1 contract per $X equity, with ATH scale-down + hard cap."""
        if equity <= 0:
            return MIN_CONTRACTS
        base = max(MIN_CONTRACTS, int(equity / self.dollars_per_contract))
        ath = self.state.equity_ath
        if ath > 0 and equity < DD_SCALE_DOWN_PCT * ath:
            old_base = base
            base = max(MIN_CONTRACTS, base - 1)
            logger.warning(
                "[TIER_SIZER] DD_SCALE_DOWN active: equity=$%.2f < 85%% of "
                "ATH=$%.2f → base %d → %d",
                equity, ath, old_base, base,
            )
        return min(base, self.max_contracts_cap)

    def _strategy_multiplier(self, strategy: Optional[str]) -> float:
        if not strategy:
            return 1.0
        return float(self._strategy_mult.get(strategy, 1.0))

    # ─── public: halt query ───────────────────────────────────────────

    def is_halted_today(self) -> tuple[bool, str]:
        """
        Has today's daily circuit breaker been tripped?

        Returns (halted: bool, reason: str).
        """
        with self._lock:
            # Re-check date roll first (cheap)
            today = datetime.now().date().isoformat()
            if self.state.session_date != today:
                self._roll_session(self.state, today)
                return False, ""

            ses_start = self.state.session_start_equity
            if ses_start <= 0:
                return False, ""
            loss_limit = DAILY_CIRCUIT_PCT * ses_start
            if self.state.session_pnl < -loss_limit:
                reason = (
                    f"daily circuit breaker tripped: "
                    f"session_pnl=${self.state.session_pnl:.2f} < "
                    f"-{DAILY_CIRCUIT_PCT*100:.1f}% of "
                    f"session_start=${ses_start:.2f} (limit=$-{loss_limit:.2f})"
                )
                return True, reason
            return False, ""

    # ─── public: contracts for an entry ───────────────────────────────

    def compute_contracts(
        self,
        strategy: Optional[str] = None,
        score: Optional[float] = None,
        equity: Optional[float] = None,
    ) -> int:
        """
        Compute contracts for the upcoming entry.

        Args:
            strategy: strategy name (e.g. "bias_momentum"). Used for the
                per-strategy multiplier lookup. None = 1.0×.
            score: optional entry quality score (not used today — present
                for forward-compat; logged for the audit trail).
            equity: override current equity (mostly for tests). When None,
                uses state.current_equity.

        Returns:
            int contracts in [1, MAX_CONTRACTS_CAP], OR 0 if the daily
            circuit breaker has tripped (caller MUST check 0 = skip entry).
        """
        with self._lock:
            # Daily breaker check FIRST — if halted, skip outright.
            halted, reason = self.is_halted_today()
            if halted:
                logger.warning(
                    "[TIER_SIZER] HALT strategy=%s score=%s reason=%s",
                    strategy, score, reason,
                )
                return 0

            eq = float(equity) if equity is not None else self.state.current_equity
            base = self._base_tier(eq)
            mult = self._strategy_multiplier(strategy)
            scaled = max(MIN_CONTRACTS, int(round(base * mult)))
            scaled = min(scaled, self.max_contracts_cap)

            # 3-consecutive-loss halving
            halved = False
            if self.state.consecutive_losses >= CONSECUTIVE_LOSS_LIMIT:
                pre = scaled
                scaled = max(MIN_CONTRACTS, scaled // 2)
                halved = True
                logger.warning(
                    "[TIER_SIZER] 3-LOSS HALVING: consec_losses=%d -> "
                    "contracts %d -> %d (strategy=%s)",
                    self.state.consecutive_losses, pre, scaled, strategy,
                )

            logger.info(
                "[TIER_SIZER] compute strategy=%s equity=$%.2f ATH=$%.2f "
                "base=%d mult=%.2f -> %d (halved=%s score=%s)",
                strategy, eq, self.state.equity_ath,
                base, mult, scaled, halved, score,
            )
            return scaled

    # ─── public: record trade close ───────────────────────────────────

    def record_trade_close(
        self,
        pnl_dollars: float,
        was_winner: Optional[bool] = None,
        strategy: Optional[str] = None,
        trade_id: Optional[str] = None,
    ) -> None:
        """
        Update equity + ATH + consecutive-loss counter on trade close.

        Args:
            pnl_dollars: NET P&L (already includes commissions/slippage).
                Positive = winner, negative = loser, exactly 0 = scratch.
            was_winner: optional override. When None, sign of pnl_dollars
                determines win/loss (0 = neither → counter unchanged).
            strategy: optional, recorded in history for forensics.
            trade_id: optional, recorded in history for forensics.
        """
        with self._lock:
            try:
                pnl = float(pnl_dollars)
            except (TypeError, ValueError):
                logger.warning(
                    "[TIER_SIZER] record_trade_close: bad pnl=%r — ignoring",
                    pnl_dollars,
                )
                return

            # Roll session if needed (catches midnight crossings between
            # entry and exit, and the first record of a new day).
            today = datetime.now().date().isoformat()
            if self.state.session_date != today:
                self._roll_session(self.state, today)

            prev_equity = self.state.current_equity
            prev_ath = self.state.equity_ath
            self.state.current_equity = prev_equity + pnl
            self.state.session_pnl = self.state.session_pnl + pnl

            ath_break = False
            if self.state.current_equity > self.state.equity_ath:
                self.state.equity_ath = self.state.current_equity
                ath_break = True

            # Consecutive-loss counter
            is_win = was_winner if was_winner is not None else (pnl > 0)
            is_loss = (not is_win) if was_winner is not None else (pnl < 0)
            if is_win:
                if self.state.consecutive_losses > 0:
                    logger.info(
                        "[TIER_SIZER] consec_losses reset %d -> 0 (win)",
                        self.state.consecutive_losses,
                    )
                self.state.consecutive_losses = 0
            elif is_loss:
                self.state.consecutive_losses += 1
            # else (scratch): leave counter unchanged

            # Append to history (rolling window)
            self.state.history.append({
                "iso":        datetime.now().isoformat(),
                "strategy":   strategy,
                "trade_id":   trade_id,
                "pnl":        round(pnl, 4),
                "equity":     round(self.state.current_equity, 4),
                "ath":        round(self.state.equity_ath, 4),
                "consec":     self.state.consecutive_losses,
            })
            if len(self.state.history) > HISTORY_KEEP:
                self.state.history = self.state.history[-HISTORY_KEEP:]

            self._persist()

            # Loud logs
            ath_str = " ATH-BREAK" if ath_break else ""
            logger.info(
                "[TIER_SIZER] trade_closed pnl=$%.2f equity=$%.2f -> $%.2f "
                "ATH=$%.2f%s session_pnl=$%.2f consec_losses=%d strategy=%s",
                pnl, prev_equity, self.state.current_equity,
                self.state.equity_ath, ath_str, self.state.session_pnl,
                self.state.consecutive_losses, strategy,
            )
            if ath_break and self.state.equity_ath > prev_ath:
                logger.info(
                    "[TIER_SIZER] NEW ATH $%.2f (prev $%.2f)",
                    self.state.equity_ath, prev_ath,
                )

    # ─── test/operator helpers ────────────────────────────────────────

    def reset_session(self) -> None:
        """Force a session roll. For tests / operator manual day-end."""
        with self._lock:
            today = datetime.now().date().isoformat()
            self._roll_session(self.state, today)

    def force_equity(self, new_equity: float, also_ath: bool = True) -> None:
        """
        For operator manual reconciliation. Updates current_equity and
        optionally bumps ATH if the new value exceeds it.
        """
        with self._lock:
            self.state.current_equity = float(new_equity)
            if also_ath and self.state.current_equity > self.state.equity_ath:
                self.state.equity_ath = self.state.current_equity
            self._persist()
            logger.warning(
                "[TIER_SIZER] MANUAL force_equity=$%.2f (ATH=$%.2f)",
                self.state.current_equity, self.state.equity_ath,
            )


# ════════════════════════════════════════════════════════════════════
# Singleton + convenience
# ════════════════════════════════════════════════════════════════════

_sizer: Optional[TierSizer] = None
_singleton_lock = threading.Lock()


def get_tier_sizer() -> TierSizer:
    global _sizer
    with _singleton_lock:
        if _sizer is None:
            _sizer = TierSizer()
        return _sizer


def reset_tier_sizer() -> None:
    """For tests: drop the singleton so the next get_tier_sizer() reloads
    fresh state from disk (or the configured starting equity)."""
    global _sizer
    with _singleton_lock:
        _sizer = None


def compute_contracts(
    strategy: Optional[str] = None,
    score: Optional[float] = None,
    equity: Optional[float] = None,
) -> int:
    """Module-level shortcut — see TierSizer.compute_contracts."""
    return get_tier_sizer().compute_contracts(strategy, score, equity)


def record_trade_close(
    pnl_dollars: float,
    was_winner: Optional[bool] = None,
    strategy: Optional[str] = None,
    trade_id: Optional[str] = None,
) -> None:
    """Module-level shortcut — see TierSizer.record_trade_close."""
    get_tier_sizer().record_trade_close(pnl_dollars, was_winner, strategy, trade_id)


def is_halted_today() -> tuple[bool, str]:
    """Module-level shortcut — see TierSizer.is_halted_today."""
    return get_tier_sizer().is_halted_today()
