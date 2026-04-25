"""
RiskConfig - frozen dataclass holding the policy knobs the gate enforces.

Loaded once at gate start; immutable for the process lifetime. Operators
can reload by restarting the gate (intended). Tests instantiate with
explicit values to bypass env loading.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class RiskConfig:
    # ── Account / instrument allowlists ──────────────────────────
    allowed_accounts: Tuple[str, ...] = ("Sim101",)
    allowed_instruments: Tuple[str, ...] = ("MNQ 06-26", "MNQM6")
    # ── Trading window (CT) ─────────────────────────────────────
    trading_open_ct: str = "08:30"
    trading_close_ct: str = "15:00"
    # ── Hard caps (the new Phase B+ defaults Jennifer set) ──────
    daily_loss_cap_usd: float = 300.0
    max_position_contracts: int = 2
    max_orders_per_minute: int = 6
    max_consecutive_losses: int = 3
    # ── Price sanity ────────────────────────────────────────────
    price_sanity_band_pct: float = 0.02   # 2% off bridge ref → REFUSE
    bridge_health_url: str = "http://127.0.0.1:8767/health"
    # ── Files / paths ───────────────────────────────────────────
    oif_outgoing_dir: str = r"C:\Users\Trading PC\Documents\NinjaTrader 8\incoming"
    killswitch_marker_path: str = r"C:\Trading Project\phoenix_bot\memory\.HALT"
    heartbeat_path: str = r"C:\Trading Project\phoenix_bot\heartbeat\risk_gate.hb"
    # ── Pipe ────────────────────────────────────────────────────
    pipe_path: str = r"\\.\pipe\phoenix_risk_gate"

    @classmethod
    def from_env(cls) -> "RiskConfig":
        """Load with env overrides. Unset env keeps the default."""
        return cls(
            allowed_accounts=tuple(
                a.strip() for a in os.environ.get("RISK_ALLOWED_ACCOUNTS", "Sim101").split(",")
                if a.strip()
            ),
            allowed_instruments=tuple(
                i.strip() for i in os.environ.get("RISK_ALLOWED_INSTRUMENTS",
                                                  "MNQ 06-26,MNQM6").split(",")
                if i.strip()
            ),
            daily_loss_cap_usd=float(os.environ.get("RISK_DAILY_LOSS_CAP_USD", "300")),
            max_position_contracts=int(os.environ.get("RISK_MAX_POSITION_CONTRACTS", "2")),
            max_orders_per_minute=int(os.environ.get("RISK_MAX_ORDERS_PER_MINUTE", "6")),
            max_consecutive_losses=int(os.environ.get("RISK_MAX_CONSECUTIVE_LOSSES", "3")),
        )
