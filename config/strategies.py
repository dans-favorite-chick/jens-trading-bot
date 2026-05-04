"""
Phoenix Trading Bot — Strategy Parameters

Dashboard sliders modify these at runtime. Click "Save to Config" on
the dashboard to persist changes to this file. Values reset to these
defaults on bot restart.

Format is intentionally flat dict — easy for Claude Code to read/edit.
"""

STRATEGY_DEFAULTS = {
    # ─── Global Thresholds (dashboard sliders) ──────────────────────
    "min_confluence": 5.0,            # Slider: 2.0 – 7.0, step 0.1 (raised from 3.5 — fewer, better trades)
    "min_momentum_confidence": 80,   # Slider: 40 – 90, step 5 (raised from 60 — require strong momentum)
    "min_precision": 48,             # Slider: 30 – 60, step 2
    "risk_per_trade": 15.0,          # Slider: $5 – $20, step $1
    "max_daily_loss": 45.0,          # Slider: $20 – $60, step $5
    "base_rr_ratio": 5.0,            # Default risk:reward (raised from 1.5 — targeting 20+ point moves)

    # ─── Aggression Profiles (dashboard buttons) ────────────────────
    # These override the above sliders when selected
    "profiles": {
        "safe": {
            "min_confluence": 6.0,
            "min_momentum_confidence": 85,
            "min_precision": 55,
            "risk_per_trade": 8.0,
            "max_daily_loss": 25.0,
        },
        "balanced": {
            "min_confluence": 5.0,
            "min_momentum_confidence": 80,
            "min_precision": 48,
            "risk_per_trade": 15.0,
            "max_daily_loss": 45.0,
        },
        "aggressive": {
            "min_confluence": 4.0,
            "min_momentum_confidence": 70,
            "min_precision": 35,
            "risk_per_trade": 20.0,
            "max_daily_loss": 50.0,
        },
    },
}

# ─── AI Pre-Trade Filter Mode (S6 / Phase H-4B) ─────────────────────
# Per-strategy ``ai_filter_mode`` values:
#   "advisory" — AI verdict is logged only; trade always proceeds (default).
#   "blocking" — if verdict is SIT_OUT, the bot skips the trade.
# Default is "advisory" across the board — we collect data before ever
# actually blocking a trade on an AI signal.
DEFAULT_AI_FILTER_MODE = "advisory"


# ─── Individual Strategy Configs ────────────────────────────────────
# Each strategy reads its own section. Dashboard toggles `enabled`.

STRATEGIES = {
    "bias_momentum": {
        "enabled": True,
        "validated": True,    # Runs in prod bot
        # NQ-calibrated ATR-anchored stop (B14 2026-04-20). Fixed-tick stops get
        # taken out by noise on NQ — use 1.5×ATR anchored to last 5m wick.
        "stop_method": "atr_anchored",
        "stop_atr_mult": 2.0,
        "min_stop_ticks": 40,        # 10 points floor (NQ noise)
        "max_stop_ticks": 120,       # 30 points cap (high-vol days)
        "stop_fallback_ticks": 64,   # 16 points if ATR unavailable
        # 2026-05-03 RECALIBRATION: was 5.0 (5:1 RR). Of 71 audit trades only
        # 9 hit target_hit. Most bias_momentum exits are managed (ema_dom_exit)
        # not target. 5×stop puts TP at 200-600 ticks (50-150 pts) — rarely
        # reached. Lowered to 2.5:1 — still aggressive but achievable. At
        # observed 38% LONG WR, 2.5:1 RR yields E[V] = 0.38×2.5 - 0.62 = +0.33
        # per trade (profitable). Evidence: out/bias_momentum_research_2026-05-03.md
        "target_rr": 2.5,
        # Defaults (regime overrides in bias_momentum.py typically take precedence)
        # Direction gate: 15m + 5m + 1m must ALL align (see bias_momentum.py evaluate())
        "min_confluence": 5.5,
        "max_hold_min": 60,   # Give it room to run — strong trends last 30-60 min
        "min_momentum": 80,
        # 2026-05-03 fix C: session-window block (CT, inclusive HH:MM ranges).
        # Forensic evidence (out/bias_momentum_research_2026-05-03.md §1):
        #   08:30-08:59:  1W/9L = 10% WR  (open volatility trap)
        #   10:00-13:29:  0W/7L = 0% WR   (mid-day chop)
        #   13:30+:       4W/1L = 80% WR  (afternoon momentum re-engages)
        #
        # ⚠️  Sprint H (2026-05-04): EMPTIED at operator request — they
        # want bias_momentum trading "all hours" for prod debug visibility.
        # The forensic data showing 10:00-13:29 = 0W/7L still holds; we're
        # accepting that loss exposure in exchange for activity. Pre-Sprint-H
        # blocked windows are preserved in this comment so they can be
        # restored before going live (or after operator changes their mind):
        #     "session_block_windows": [
        #         ("08:30", "08:59"),
        #         ("10:00", "13:29"),
        #     ],
        # ⚠️  LIVE-MODE IMPLICATION: when LIVE_TRADING=True is flipped,
        # bias_momentum will trade during the historically-losing windows
        # on real money. Operator should restore the windows above before
        # going live, or explicitly accept the risk.
        "session_block_windows": [],
        # 2026-05-03 fix B: SHORT-asymmetric quality requirement.
        # Bot-wide SHORT WR was 9% over 11 trades (NQ structural long-bias drift
        # makes symmetric momentum strategies underperform on the short side).
        # When True, SHORT entries require BOTH 1m AND 5m tf_bias = BEARISH
        # (in addition to standard EMA-stack + VWAP-side gate). LONG retains
        # current looser gate.
        "short_extra_gates": True,
        # 2026-05-03 fix A: prevent trend_stall exit from firing within N
        # seconds of entry. 12 trades had duration_s ≤ 0 (entry/exit gates
        # disagree on the same bar's data). 60s grace prevents instant unwind.
        "trend_stall_grace_s": 60,
        # 2026-05-03: skip signals when natural ATR stop would exceed
        # max_stop_ticks (forced clamping). Forensic evidence: clamped stops
        # were 0W/5L. Vol regime mismatch — better to skip than clamp.
        "skip_on_stop_clamp": True,
        # 2026-05-03: convert RSI bearish-divergence "warning" into a hard
        # gate. Evidence: opposing-RSI-div appears in 6 losers / 0 winners.
        "rsi_div_hard_gate": True,
        # 2026-04-24: Explosive bypass thresholds. The VWAP gate rejected 99% of
        # bias_momentum signals in the 48h prior because VCR rarely cleared the
        # 1.5x bar (and the close-position requirement of >=0.75 / <=0.25 was
        # also restrictive). Lowered to broaden the bypass window — strategy
        # already requires bar-delta in direction so corner cases stay covered.
        "vcr_threshold": 1.2,                  # was hardcoded 1.5
        "explosive_close_pos_long": 0.65,      # was 0.75
        "explosive_close_pos_short": 0.35,     # was 0.25
        # EMA9 extension gate (see bias_momentum.py): outside golden windows (OPEN_MOMENTUM,
        # MID_MORNING), reject if price is already > N ticks from EMA9. Prevents chasing
        # extended moves in LATE_AFTERNOON. 60t = 15pts — still allows re-entry close
        # to EMA9, blocks buying 130-200t above EMA9 in afternoon chop.
        "max_ema_dist_ticks": 60,
    },
    "spring_setup": {
        # 2026-04-24 RETIRED: 48h log analysis showed 1,250 NO_SIGNAL events
        # (46% of evals reporting `no_spring_wick`). Spring wick + reverse pattern
        # is structurally rare in MNQ intraday — most MNQ moves are directional,
        # not wick-and-reject. Strategy fundamentally mismatched to current
        # market profile. Re-enable only after retooling the wick-criteria
        # spec (consider widening min_wick_ticks or combining with VWAP mean
        # reversion for confluence).
        "enabled": False,
        "validated": True,    # Was running in prod bot before retire
        "stop_multiplier": 1.5,  # fallback wick multiplier (used only if stop_at_structure=False)
        "target_rr": 1.5,
        "min_wick_ticks": 6,
        "require_vwap_reclaim": True,
        "require_delta_flip": True,
        "max_hold_min": 15,
        # v2 fixes (2026-04-14): TF gate + ATR-anchored stop
        "require_tf_alignment": True,   # Only fire WITH dominant trend (3/4 TF votes)
        "min_tf_votes": 3,              # Min TF votes in direction to allow entry
        # ATR stop (research-validated for reversal patterns):
        # Stop = wick_extreme ± (atr_stop_multiplier × ATR_5m)
        # 1.0-1.2× is validated range; 1.1 balanced (not too tight, not too wide)
        # Anchored to wick low/high — NOT entry price — so it's below the defended level
        "atr_stop_multiplier": 1.1,     # 1.1 × 5m ATR from wick extreme
        "structure_buffer_ticks": 2,    # Fallback buffer if ATR unavailable
        # NQ research clamps (Fix 7, 2026-04-20): raised from 8/40 → 40/120
        "min_stop_ticks": 40,
        "max_stop_ticks": 120,
    },
    "vwap_pullback": {
        "enabled": True,
        "validated": False,   # Lab only
        # NQ-calibrated ATR-anchored stop (B14 2026-04-20). Replaces fixed 14t.
        "stop_method": "atr_anchored",
        "stop_atr_mult": 2.0,
        "min_stop_ticks": 40,
        "max_stop_ticks": 120,
        "stop_fallback_ticks": 64,
        # Max distance from VWAP to qualify as "near VWAP" (replaces hardcoded 6).
        # 60t = 15pts — a true VWAP pullback can be further out than 6 ticks on NQ.
        "max_vwap_dist_ticks": 60,
        # B78 (2026-04-21): dropped from 20.0 → 2.5 to match reality. VWAP
        # pullback is a mean-reversion strategy; no trailing / managed exit is
        # wired in strategies/vwap_pullback.py, so a 20:1 target was structurally
        # unreachable (40t stop × 20 = 200pts). 2.5:1 with a ~64t stop ≈ 160t
        # target (~40pts) is a realistic mean-reversion reach in 30-60 min and
        # makes the OCO bracket an actual exit path, not a placeholder.
        "target_rr": 2.5,
        "min_confluence": 3.2,
        "min_tf_votes": 3,
        "max_hold_min": 60,  # Give it room — VWAP pullbacks can run 30-80pts
    },
    "high_precision_only": {
        "enabled": False,
        "validated": False,   # Lab only — Research Bot found promise (64% WR solo)
        "stop_ticks": 14,
        "target_rr": 5.0,    # 5:1 — high precision setups deserve big targets
        "min_confluence": 3.5,
        "min_tf_votes": 3,
        "min_precision": 65,
        "max_hold_min": 30,
    },
    "dom_pullback": {
        "enabled": True,
        "validated": False,   # Lab only — replicates user's manual DOM absorption entry
        # Entry: pullback to EMA9 or VWAP + sell orders being pulled/absorbed by buyers
        # NQ-calibrated ATR-anchored stop (B14 2026-04-20). Replaces fixed 10t — too tight.
        "stop_method": "atr_anchored",
        "stop_atr_mult": 2.0,
        "min_stop_ticks": 40,
        "max_stop_ticks": 120,
        "stop_fallback_ticks": 64,
        "target_rr": 2.5,     # 2.5:1 = 25t = 6.25pts minimum capture
        # DOM absorption threshold — 0=any signal, 100=very strong only
        # 35 is moderate: absorption is visible but not overwhelming
        "min_dom_strength": 35,
        # Pullback detection: how close to EMA9 / VWAP to qualify as "at the level"
        # Data: P25 of EMA9 distance by regime = 26-40t. The P25 is the "normal close approach"
        # zone — a bar in the bottom quartile of EMA9 distance is a genuine touch.
        # 28t = 7pts — within 7 MNQ points of EMA9 qualifies as "at the level".
        "max_ema_dist_ticks": 28,   # Widened from 12t → 28t (data-validated P25 zone)
        "max_vwap_dist_ticks": 20,  # Widened from 10t → 20t (more realistic touch zone)
        "max_hold_min": 20,
    },
    "ib_breakout": {
        "enabled": True,
        "validated": True,    # Runs in prod bot — 96.2% IB break rate, 74.56% WR
        # 2026-04-24: dropped from 30 → 10 per Jennifer's request after the 48h
        # log analysis showed IB Breakout was 100% blocked on warmup_incomplete
        # — bot mid-session restarts couldn't ever build a 30-min IB before the
        # entry window expired. 10 minutes is half the published spec but keeps
        # the strategy live across mid-day restarts and after market gaps.
        "ib_minutes": 10,
        "target_extension": 1.5,
        "max_ib_width_atr_mult": 1.5,
        "stop_at_ib_midpoint": False,  # False = stop at full IB opposite, True = tighter stop at midpoint
        # NQ research ceiling (Fix 8, 2026-04-20): structural stop must fit.
        # If (price - ib_low) or (ib_high - price) exceeds this in ticks → SKIP signal.
        # Complementary to max_ib_width_atr_mult (pre-filter on IB width).
        "max_stop_ticks": 120,
        "max_hold_min": 60,
        # v2 fix (2026-04-14): CVD must confirm breakout direction
        # Without this: SHORT at IB low with CVD=+6.05M → -164t loss (buyers absorbing)
        "require_cvd_confirm": True,   # CVD > 0 for LONG, CVD < 0 for SHORT — hard gate
    },
    # ─── New strategies per roadmap v4 (Apr 23 2026) ───────────────────

    "orb": {
        # Opening Range Breakout — Zarattini, Barbon, Aziz (2024) SSRN 4729284
        # Published: QQQ 46% annual, Sharpe 2.4; NQ backtest 74% WR, PF 2.51.
        "enabled": True,
        "validated": False,         # Lab only until 20+ live trades collected
        "or_duration_minutes": 15,
        "confirmation_close_minutes": 5,
        "max_entry_delay_minutes": 60,     # Cutoff at 10:30 ET / 9:30 CST
        "min_or_size_points": 10,          # Skip low-vol days
        # 2026-04-24: was a hard 60-pt cap, blocked 98% of evals (`gate:or_too_wide`).
        # Now ATR-adaptive: max width = max(max_or_size_points, atr_5m * max_or_size_atr_mult)
        # capped by max_or_size_hard_cap_points. Lets a high-vol day produce a wider
        # OR while still rejecting true gap days (>4× ATR).
        "max_or_size_points": 80,                # Floor (use this if ATR unavailable)
        "max_or_size_atr_mult": 4.0,             # Adaptive cap = 4× ATR-5m
        "max_or_size_hard_cap_points": 150,      # Absolute ceiling — gap days still rejected
        "max_stop_points": 25,             # Hard cap = $50 on MNQ
        "stop_buffer_ticks": 2,
        "target_rr": 2.0,                  # Partial 1R + runner (SCALE_OUT handles partial)
        # Session window: eod_flat_time_et picked by strategy based on is_prod_bot
    },

    "noise_area": {
        # Noise Area Intraday Momentum — Zarattini, Aziz, Barbon (2024) SSRN 4824172
        # Published: SPY 19.6% annual, Sharpe 1.33; NQ 24.3% annual, Sharpe 1.67.
        "enabled": True,
        "validated": False,         # Lab only — new strategy, 10+ day warmup needed
        "lookback_days": 14,
        # 2026-04-24: dropped 1.0 → 0.7. Published spec is for SPY (low vol);
        # MNQ is more volatile and price rarely breaks the 1σ cone. Tightened
        # so price more often clears the band. If false-breakouts spike,
        # tighten further or add a 2nd confirm bar.
        "band_mult": 0.7,
        "trade_freq_minutes": 30,
        "require_vwap_confluence": True,
        "min_noise_history_days": 10,
        "eod_flat_time_et": "16:54",       # B84: 15:54 CT = 16:54 ET (matches bot-level flatten)
        "prod_eod_flat_time_et": "10:55",  # Prod 90-min window
    },

    "compression_breakout": {
        "enabled": True,
        "validated": False,   # Lab only — PRE-explosion entry, build sample before prod promotion
        # Coil detection — None = use regime default from _REGIME_PARAMS in the strategy file
        #   Primary (8:30-10:30): min_coil_bars=3, tight_mult=0.90
        #   Afternoon:            min_coil_bars=5, tight_mult=1.20-1.50
        "min_coil_bars": None,      # None = regime default
        "tight_mult":    None,      # None = regime default
        "min_tf_votes": 2,          # TF votes needed to confirm direction (exhaustion allows min-1)
        "stop_buffer_ticks": 3,     # Ticks beyond coil low/high for stop
        # 2026-04-24: raised from default 5 → 12. Per 48h analysis, squeeze
        # was released before the 5-bar minimum 68% of the time on current
        # MNQ vol regime. 12 bars (60 min on 5m) ensures the squeeze is
        # genuine and not a noise compression that resolves immediately.
        "min_squeeze_bars": 12,
        # Stop management — NQ research clamps (Fix 7, 2026-04-20)
        "min_stop_ticks": 40,       # 10pt floor (Propfolio noise floor)
        "max_stop_ticks": 120,      # 30pt ceiling (Steady Turtle NQ band)
        # atr_stop_mult stays strategy-internal (1.5× by default; trend breakout).
        # Targets — these moves run FAR, use wide RR
        # With 20-tick stop (5 pts): 5:1 = 100t = 25pts, 8:1 = 160t = 40pts
        # Explosion squeezes on MNQ routinely run 400t+ (100pts). Let it run.
        "target_rr": 5.0,           # 5:1 minimum — 100 ticks = 25 points minimum
        "max_hold_min": 90,         # Give it room to run — big squeezes last 45-90 min
        # Lab bot collects data — key questions to tune:
        #   Which signal (VRR / exhaustion / close-breakout) has highest win rate?
        #   Does ATR-declining alone add value without a directional signal?
        #   Is exhaustion_tf_min = min_tf_votes-1 the right relaxation?
    },

    "opening_session": {
        # Opening-window family: 4 opening-type branches + Premarket Breakout
        # + 15-min ORB. Lab only until Phase 4 wiring.
        "enabled": True,
        "stage": "lab",
        "validated": False,

        # Universal guards
        "max_trades_per_day": 2,
        "day_flat_time_ct": "14:30",
        "news_blackout_min": 5,

        # Universal stops (Fix 6 standard, locked 2026-04-20)
        "min_stop_ticks": 40,
        "max_stop_ticks": 100,

        # Open Drive
        "open_drive_min_displacement_pts": 15,
        "open_drive_max_pullback_pct": 0.30,
        "open_drive_min_volume_ratio": 1.4,
        "open_drive_entry_volume_ratio": 1.2,
        "open_drive_trail_ticks": 20,

        # Open Test Drive
        "open_test_drive_test_buffer_ticks": 8,
        "open_test_drive_reversal_volume_ratio": 1.3,
        "open_test_drive_stop_buffer_ticks": 4,
        "open_test_drive_time_exit_min": 75,

        # Open Auction In
        "open_auction_in_wick_pct_min": 0.60,
        "open_auction_in_volume_ratio": 1.2,
        "open_auction_in_stop_buffer_ticks": 8,
        "open_auction_in_time_exit_ct": "12:30",

        # Open Auction Out
        "open_auction_out_wait_min": 15,
        "open_auction_out_stop_buffer_ticks": 8,
        "open_auction_out_time_exit_ct": "11:00",

        # Premarket Breakout
        "premarket_breakout_min_range_pts": 10,
        "premarket_breakout_volume_ratio": 1.4,
        "premarket_breakout_buffer_ticks": 2,
        "premarket_breakout_stop_buffer_ticks": 8,
        "premarket_breakout_time_exit_ct": "10:30",

        # ORB
        "orb_window_min": 15,
        "orb_max_range_pct": 0.008,
        "orb_target_pct_of_or": 0.50,
        "orb_be_pct_of_or": 0.25,
        "orb_time_exit_ct": "14:30",
    },

    "vwap_band_pullback": {
        # 1σ/2σ VWAP-band pullback + RSI(2) — ported from b12 research.
        # Runs alongside vwap_pullback (proximity) for head-to-head lab data.
        # Author prediction (b12 header): PF 1.5-1.8 at WR 45-55%, RR 1.5-2:1.
        "enabled": True,
        "validated": False,   # Lab only — needs 50+ trades before prod promotion
        "min_bars": 50,
        "rsi_period": 2,
        "rsi_long_threshold": 30,
        "rsi_short_threshold": 70,
        "atr_period": 14,
        "min_volume_ratio": 0.8,
        "target_rr": 2.0,
        # NQ-research clamps (matches Fix 7 values). If natural 2σ-band
        # stop > max_stop_ticks, signal is skipped (Fix 8-style guard).
        "min_stop_ticks": 40,
        "max_stop_ticks": 120,
        "max_hold_min": 60,
    },
    "vwap_band_reversion": {
        # 2026-05-03: NEW pure mean-reversion strategy at 2.1σ. Distinct from
        # vwap_band_pullback (which is trend-aligned pullback into the 1σ
        # zone). This strategy SHORTs at upper-band touch and LONGs at lower-
        # band touch with bar-confirmation, regardless of HTF trend — but
        # SKIPS on TREND days (price walks one band, reversion fails).
        # Per operator request; lab-only until 50+ trades validate.
        # See strategies/vwap_band_reversion.py docstring for research basis.
        "enabled": True,
        "validated": False,   # Lab only
        "sigma": 2.1,                    # entry-band sigma
        "outer_sigma": 2.5,              # stop is just beyond this
        "atr_stop_buffer": 0.5,          # multiplier on ATR added to outer band
        "atr_period": 14,
        "min_bars": 30,
        "min_volume_ratio": 0.7,         # looser than band_pullback's 0.8
        "min_stop_ticks": 30,            # NQ noise floor (looser; reversion entries are tight)
        "max_stop_ticks": 100,           # ceiling — skip if natural stop wider
        "target_rr": 1.5,                # fallback when target_at_vwap=False
        "target_at_vwap": True,          # default: target VWAP itself, not opposite band
        # Time-of-day block (CT). 08:30-09:30 = open volatility (per
        # bias_momentum forensic finding §4 in trade_analysis_2026-05-03.md).
        "block_windows": [("08:30", "09:30")],
        "max_hold_min": 30,
    },
}

# Backfill default ai_filter_mode="advisory" on every strategy that
# hasn't set one explicitly. Keeps the S6 surface one line per strategy
# without editing each block.
for _name, _cfg in STRATEGIES.items():
    _cfg.setdefault("ai_filter_mode", DEFAULT_AI_FILTER_MODE)
del _name, _cfg
