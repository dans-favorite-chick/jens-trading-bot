"""
Phoenix Trading Bot — Strategy Parameters

Dashboard sliders modify these at runtime. Click "Save to Config" on
the dashboard to persist changes to this file. Values reset to these
defaults on bot restart.

Format is intentionally flat dict — easy for Claude Code to read/edit.
"""

STRATEGY_DEFAULTS = {
    # ─── Global Thresholds (dashboard sliders) ──────────────────────
    "min_confluence": 3.5,           # Slider: 2.0 – 5.0, step 0.1
    "min_momentum_confidence": 60,   # Slider: 40 – 90, step 5
    "min_precision": 48,             # Slider: 30 – 60, step 2
    "risk_per_trade": 15.0,          # Slider: $5 – $20, step $1
    "max_daily_loss": 45.0,          # Slider: $20 – $60, step $5
    "base_rr_ratio": 1.5,            # Default risk:reward target

    # ─── Aggression Profiles (dashboard buttons) ────────────────────
    # These override the above sliders when selected
    "profiles": {
        "safe": {
            "min_confluence": 4.5,
            "min_momentum_confidence": 80,
            "min_precision": 55,
            "risk_per_trade": 8.0,
            "max_daily_loss": 25.0,
        },
        "balanced": {
            "min_confluence": 3.5,
            "min_momentum_confidence": 60,
            "min_precision": 48,
            "risk_per_trade": 15.0,
            "max_daily_loss": 45.0,
        },
        "aggressive": {
            "min_confluence": 2.5,
            "min_momentum_confidence": 45,
            "min_precision": 35,
            "risk_per_trade": 20.0,
            "max_daily_loss": 50.0,
        },
    },
}

# ─── Individual Strategy Configs ────────────────────────────────────
# Each strategy reads its own section. Dashboard toggles `enabled`.

STRATEGIES = {
    "bias_momentum": {
        "enabled": True,
        "validated": True,    # Runs in prod bot
        "stop_ticks": 9,
        "target_rr": 2.0,
        "min_confluence": 3.0,
        "min_tf_votes": 3,
        "max_hold_min": 25,
        "min_momentum": 55,
    },
    "spring_setup": {
        "enabled": True,
        "validated": True,    # Runs in prod bot
        "stop_multiplier": 1.5,  # 1.5x wick size
        "target_rr": 1.5,
        "min_wick_ticks": 6,
        "require_vwap_reclaim": True,
        "require_delta_flip": True,
        "max_hold_min": 15,
    },
    "vwap_pullback": {
        "enabled": True,
        "validated": False,   # Lab only
        "stop_ticks": 8,
        "target_rr": 1.8,
        "min_confluence": 3.2,
        "min_tf_votes": 3,
        "max_hold_min": 18,
    },
    "high_precision_only": {
        "enabled": True,
        "validated": False,   # Lab only
        "stop_ticks": 8,
        "target_rr": 1.5,
        "min_confluence": 3.5,
        "min_tf_votes": 4,
        "min_precision": 55,
        "max_hold_min": 15,
    },
    "tick_scalp": {
        "enabled": True,      # Enabled for lab bot
        "validated": False,
        "stop_ticks": 5,
        "target_rr": 1.5,
        "min_confluence": 2.5,
        "min_tf_votes": 3,
        "max_hold_min": 8,
    },
}
