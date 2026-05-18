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

    # 2026-05-13 (#18): BE-stop confirmation gate. When True, BE arming
    # requires the most-recent CLOSED 1m bar to also be past the trigger
    # — protects against single-tick spikes that previously activated BE
    # then immediately reversed and stopped us out on entry noise.
    # Set False to revert to legacy tick-touch arming.
    "be_on_bar_close": True,

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
        # 2026-05-17: Phase 6 — V2 overhaul tightens NQ floor (40→24t = 6pt)
        # and widens cap (120→200t = 50pt) to absorb NQ 2026 vol regime.
        # Pairs with Phase 7 CODE PATCH 3 confirmation-stop fallback.
        "min_stop_ticks": 24,        # was 40 — NQ 6pt floor (V2 deployment)
        "max_stop_ticks": 200,       # was 120 — NQ vol regime fix (V2 deployment)
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
        # 2026-05-17: SIM TESTING — flipped True->False. The V2 deployment
        # raises max_stop_ticks to 200 (was 120) so clamps are far less
        # frequent. Phase 7 will replace the rejection with a confirmation-
        # bar fallback (stop_fallback_mode: "confirmation"). RESTORE before live.
        "skip_on_stop_clamp": False,
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
        # ── 2026-05-17: Phase 6 V2-deployment additions ───────────────
        # Confirmation-stop fallback (wired in Phase 7 CODE PATCH 3).
        # Falls back to next bar's close-side stop when raw ATR stop
        # exceeds max_stop_ticks, instead of rejecting the signal.
        "stop_fallback_mode": "confirmation",
        # Less-strict multi-TF alignment (3-of-N → 2-of-N).
        "min_tf_votes": 2,
        # New gate that controls the existing short_extra_gates (above)
        # behavior; Phase 7 CODE PATCH 1 routes through this flag.
        # False = honor existing short_extra_gates (redundant SHORT gate
        # is gated OFF by default in V2 deployment).
        "short_extra_gate_enabled": False,
        # Only veto on STRONG opposing CVD (was -0.3 — too aggressive).
        "cvd_health_veto_threshold": -0.4,
        # Early-session EMA fallback: use 1m EMAs before 09:00 CT when
        # 5m EMAs are still warming up. Wired in Phase 7 CODE PATCH 2.
        "ema_stack_early_session_fallback": True,
        "ema_stack_early_session_end_ct": "09:00",
    },
    "spring_setup": {
        # 2026-04-24 RETIRED: 48h log analysis showed 1,250 NO_SIGNAL events
        # (46% of evals reporting `no_spring_wick`). Spring wick + reverse pattern
        # is structurally rare in MNQ intraday — most MNQ moves are directional,
        # not wick-and-reject. Strategy fundamentally mismatched to current
        # market profile. Re-enable only after retooling the wick-criteria
        # spec (consider widening min_wick_ticks or combining with VWAP mean
        # reversion for confluence).
        # 2026-05-17: Phase 6 — UN-RETIRED per operator override "all
        # strategies firing." Wick pattern rare on MNQ but V2 patches
        # (min_tf_votes 3->2, require_vwap_reclaim True->False) loosen
        # the gates that pre-2026-05-17 were rejecting almost every
        # candidate. Re-evaluate after 30+ sim trades.
        "enabled": True,
        "validated": True,    # Was running in prod bot before retire
        "stop_multiplier": 1.5,  # fallback wick multiplier (used only if stop_at_structure=False)
        "target_rr": 1.5,
        "min_wick_ticks": 6,
        "require_vwap_reclaim": False,  # 2026-05-17: was True — too gating (V2)
        "require_delta_flip": True,
        "max_hold_min": 15,
        # v2 fixes (2026-04-14): TF gate + ATR-anchored stop
        "require_tf_alignment": True,   # Only fire WITH dominant trend (3/4 TF votes)
        "min_tf_votes": 2,              # 2026-05-17: was 3 — less strict (V2)
        # ATR stop (research-validated for reversal patterns):
        # Stop = wick_extreme ± (atr_stop_multiplier × ATR_5m)
        # 1.0-1.2× is validated range; 1.1 balanced (not too tight, not too wide)
        # Anchored to wick low/high — NOT entry price — so it's below the defended level
        "atr_stop_multiplier": 1.1,     # 1.1 × 5m ATR from wick extreme
        "structure_buffer_ticks": 2,    # Fallback buffer if ATR unavailable
        # NQ research clamps (Fix 7, 2026-04-20): raised from 8/40 → 40/120
        "min_stop_ticks": 40,
        "max_stop_ticks": 200,  # 2026-05-17: was 120 — NQ vol regime fix (V2)
        # 2026-05-17: Phase 6 V2-deployment addition.
        "stop_fallback_mode": "confirmation",
    },
    "vwap_pullback": {
        # 2026-05-17: Phase 5 — DISABLED, superseded by vwap_pullback_v2.
        # V2 widens max_stop_ticks 120→200 for NQ 2026 vol regime + uses
        # confirmation-stop fallback. Keep block for git-history reference.
        "enabled": False,
        "validated": False,   # Lab only
        # NQ-calibrated ATR-anchored stop (B14 2026-04-20). Replaces fixed 14t.
        "stop_method": "atr_anchored",
        # 2026-05-13 (#1b): tightened 2.0 → 1.5. vwap_pullback is a mean-
        # reversion entry into a tight range — the 2.0× multiplier gave it
        # the same stop width as the trend-following bias_momentum, which
        # is overcalibrated for the regime. 1.5× still clears noise but
        # cuts hold-to-stop loss by ~25%.
        "stop_atr_mult": 1.5,
        "min_stop_ticks": 40,
        "max_stop_ticks": 120,
        "stop_fallback_ticks": 64,
        # 2026-05-13 (#8): skip when natural ATR stop > max_stop_ticks.
        # Same forensic logic as bias_momentum (0W/5L on clamped stops).
        # 2026-05-17: SIM TESTING — flipped True->False for V2 overhaul.
        # RESTORE before live.
        "skip_on_stop_clamp": False,
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
        # 🚫 RETIRED 2026-05-13 (#5/#6 of roadmap).
        # 557 trades / 29% WR / -$1,082 net. The 14-tick fixed stop loses
        # the noise game on NQ; the strategy needs a structural rework
        # (atr_anchored stop + min_tf_votes=3 + min_precision=65 gate is
        # NOT enough — see validation_status_2026-05-13.md). Re-enable
        # only after a from-scratch redesign — flipping enabled=True
        # without that work will repeat the 557-trade loss pattern.
        "enabled": False,
        "validated": False,
        "retired": True,
        "retired_at": "2026-05-13",
        "retired_reason": (
            "557 trades / 29% WR / -$1,082 net. 14-tick fixed stop loses "
            "to NQ noise. Needs structural rework, not parameter tweaks."
        ),
        "stop_ticks": 14,
        "target_rr": 5.0,
        "min_confluence": 3.5,
        "min_tf_votes": 3,
        "min_precision": 65,
        "max_hold_min": 30,
    },
    "dom_pullback": {
        "enabled": True,
        "validated": True,    # 2026-05-17: was False — operator override (V2 deployment)
        # Entry: pullback to EMA9 or VWAP + sell orders being pulled/absorbed by buyers
        # NQ-calibrated ATR-anchored stop (B14 2026-04-20). Replaces fixed 10t — too tight.
        "stop_method": "atr_anchored",
        "stop_atr_mult": 2.0,
        "min_stop_ticks": 40,
        "max_stop_ticks": 200,  # 2026-05-17: was 120 — NQ vol regime fix (V2)
        "stop_fallback_ticks": 64,
        # 2026-05-13 (#8): skip when natural ATR stop > max_stop_ticks.
        # Same forensic logic as bias_momentum (0W/5L on clamped stops).
        # 2026-05-17: SIM TESTING — flipped True->False for V2 overhaul.
        # RESTORE before live.
        "skip_on_stop_clamp": False,
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
        # 2026-05-17: Phase 6 V2-deployment addition.
        "stop_fallback_mode": "confirmation",
    },
    "ib_breakout": {
        # 2026-05-15 fix: same ET-midnight anchor bug as ORB had been
        # producing 3,472 `gate:ib_too_wide` rejections in 50MB of
        # sim_stdout (the dominant failure mode). Fix: session_open_et
        # config + bar-window filter (mirrors ORB session-anchor fix).
        # max_ib_width_atr_mult relaxed 1.5 → 4.0 to match ORB's working
        # cap (MNQ 10-min IB at the open routinely runs 50-80pt, which
        # is ~2-3× the 5m ATR — the old 1.5× cap was tuned for SPY).
        "session_open_et": "09:30",
        "enabled": True,
        # 2026-05-13 (#22): DEMOTED from validated=True to False. Wilson
        # CI guardrail caught it: only 8 trades in the live record, which
        # is below TENTATIVE (n>=100). The 75% WR with 8 trades has a
        # 95% CI of 41-93% — that's noise, not evidence. Stay in lab
        # until n>=100. Re-promote with `--check-promotion`.
        # 2026-05-17: Phase 6 — operator override "all strategies firing"
        # bypasses Wilson-CI guardrail. Phase 10 restores the n>=100 rule.
        "validated": True,
        # 2026-04-24: dropped from 30 → 10 per Jennifer's request after the 48h
        # log analysis showed IB Breakout was 100% blocked on warmup_incomplete
        # — bot mid-session restarts couldn't ever build a 30-min IB before the
        # entry window expired. 10 minutes is half the published spec but keeps
        # the strategy live across mid-day restarts and after market gaps.
        "ib_minutes": 10,
        "target_extension": 1.5,
        "max_ib_width_atr_mult": 4.0,  # 2026-05-15: was 1.5 — too tight for MNQ
        "stop_at_ib_midpoint": False,  # False = stop at full IB opposite, True = tighter stop at midpoint
        # NQ research ceiling (Fix 8, 2026-04-20): structural stop must fit.
        # If (price - ib_low) or (ib_high - price) exceeds this in ticks → SKIP signal.
        # Complementary to max_ib_width_atr_mult (pre-filter on IB width).
        "max_stop_ticks": 200,  # 2026-05-17: was 120 — NQ vol regime fix (V2)
        "max_hold_min": 60,
        # v2 fix (2026-04-14): CVD must confirm breakout direction
        # Without this: SHORT at IB low with CVD=+6.05M → -164t loss (buyers absorbing)
        "require_cvd_confirm": True,   # CVD > 0 for LONG, CVD < 0 for SHORT — hard gate
        # 2026-05-17: Phase 6 V2-deployment addition.
        "stop_fallback_mode": "confirmation",
    },
    # ─── New strategies per roadmap v4 (Apr 23 2026) ───────────────────

    "orb": {
        # Opening Range Breakout — Zarattini, Barbon, Aziz (2024) SSRN 4729284
        # Published: QQQ 46% annual, Sharpe 2.4; NQ backtest 74% WR, PF 2.51.
        # 2026-05-17: Phase 5 — DISABLED, superseded by orb_v2.
        # V2 has CVD-alignment gate + OR-range floor + confirmation-stop
        # fallback. Keep block for git-history + Zarattini citation reference.
        "enabled": False,
        "validated": False,         # Lab only until 20+ live trades collected
        # 2026-05-15 fix: session_open_et anchors the daily reset to the
        # US cash open. Pre-fix the daily reset fired at ET-midnight,
        # which built the "OR" from arbitrary overnight bars (393pt wide
        # = 5× the cap → every breakout rejected as `or_too_wide`).
        # Now Zarattini's published 9:30 ET anchor is honored.
        "session_open_et": "09:30",
        "or_duration_minutes": 15,
        "confirmation_close_minutes": 5,
        "max_entry_delay_minutes": 60,     # Cutoff at 10:30 ET / 8:30 CT
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

    "big_move_signal": {
        # NEW 2026-05-15 — standalone entry on the Big-Move Detector
        # score >= 90 signature (all 4 of vol_collapse + cvd_divergence +
        # failed_break + dom_absorption fire simultaneously). Validation:
        # 15:11:19 today fired score=100 LONG and price ran +47pt in
        # 8 minutes (would have been a 2R win on a $50 budget).
        # Sim only until n>=30 trades + post-tune review.
        "enabled": True,
        "validated": True,           # 2026-05-17: was False — operator override (V2)
        # 2026-05-17: was 90 — at score=90 the all-flags-must-fire gate
        # produced < 1 signal/week. 70 catches strong 3-of-4 setups too.
        # The strategy reads "min_score" from config (see big_move_signal.py
        # constructor and tests/test_big_move_signal_strategy.py).
        "min_score": 70,
        "stop_atr_mult": 1.0,        # Tight stop — strategy enters at exhaustion
        "max_stop_ticks": 200,       # 2026-05-17: was 100 — V2 widens (V2 deployment)
        "min_stop_ticks": 20,        # Floor to avoid sub-noise stops
        "target_rr": 2.0,            # Target the move; exhaustion-exit fires before TP usually
        "ai_filter_mode": "advisory",
        # 2026-05-17: Phase 6 V2-deployment addition.
        "stop_fallback_mode": "confirmation",
    },

    "noise_area": {
        # RETIRED 2026-05-15 — data verdict from tools/mae_mfe_asymmetry.py:
        # 20 trades, 10% WR, -$693.90, avgMAE 165t / avgMFE 73t.
        # MFE/MAE ratio 0.44x — losers go 2.3× farther adverse than winners
        # go favorable. losC% = 98% (losers ride to full MAE). winC% = 89%
        # (winners realize most of MFE — but MFE itself is too small).
        # No exit tuning fixes "the average loser is 2× the average winner."
        # The Zarattini noise-cone strategy is designed for SPY (low vol);
        # MNQ's volatility profile makes the structural stop unworkable on
        # a $50/trade budget. Re-introduce only if the cone-detection
        # logic is replaced with something MNQ-native (e.g., gamma-aware
        # bands, regime-conditioned widths).
        "enabled": False,
        "retired": True,
        "retired_at": "2026-05-15",
        "retired_reason": (
            "10% WR, MFE/MAE=0.44x — losers go 2.3× farther adverse than "
            "winners go favorable. Strategy has anti-edge on MNQ. "
            "Retired per tools/mae_mfe_asymmetry.py verdict."
        ),
        # Noise Area Intraday Momentum — Zarattini, Aziz, Barbon (2024) SSRN 4824172
        # Published: SPY 19.6% annual, Sharpe 1.33; NQ 24.3% annual, Sharpe 1.67.
        # (NQ published number was a paper-backtest; live trading on MNQ
        # has not reproduced it across 20 trades.)
        "validated": False,
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
        # 2026-05-15 managed-exit fix: signal_flip triggers now require
        # (a) bar-close confirmation (not tick price) AND (b) min-hold
        # window from entry. Today's 4 noise_area trades all exited via
        # signal_flip within ~2 min of entry, turning normal price wobble
        # into 3 losses (and 1 winner that escaped via ema_dom_exit).
        # 5 min protective window per Zarattini paper's spirit (their
        # spec uses "confirmed return to cone" — interpreted as a bar
        # close, not a single tick).
        "min_hold_seconds_before_signal_flip": 300,
    },

    "compression_breakout": {
        # UN-RETIRED 2026-05-15 — re-armed in sim only (validated=False) with
        # MNQ-calibrated params + per-condition instrumentation. The prior
        # retirement was correct given the strict published params produced
        # 18 trades / 5 weeks (too rare). After today's deep-dive on 5,476
        # `squeeze_not_held_min_bars` events, we now know the bottleneck is
        # STAGE 1 — never accumulates enough consecutive compressed bars on
        # MNQ. The 2026-04-24 commit raised min_squeeze_bars 5→12 under the
        # assumption of "60 min on 5m bars," but evaluate() actually ticks
        # ~1.2×/min on this codebase, so 12 evals ≈ 10 min — and even that
        # threshold is rarely hit because conditions 2/3/4 are tuned for
        # equity ETFs (TTM Squeeze, Minervini VCP) not 23/5 futures.
        # New tuning relaxes the AND-of-4 to MNQ vol profile; instrumentation
        # logs WHICH condition is failing each eval so the operator can see
        # the firing distribution directly.
        # 2026-05-17: Phase 5 — DISABLED, superseded by compression_breakout_v2.
        # V2 uses Carter BB/KC squeeze + 3-of-4 voting + NQ-tuned 1.5 std (was
        # 2.0). Keep block for git-history + tuning-journey context.
        "enabled": False,
        "validated": False,         # Sim only until 30+ trades + post-tune review
        # Compression-condition tunings (MNQ-calibrated 2026-05-15)
        "atr_compression_ratio": 0.65,  # was 0.5 — MNQ rarely drops to half of avg ATR
        "range_atr_ratio": 1.8,         # was 1.5 — broader range tolerance
        # 2026-05-15 second-pass: require N of the 4 stage-1 conditions
        # (TTM, ATR, Volume, Range) rather than ALL 4. Carver's
        # "Systematic Trading" principle: scaled forecasts beat binary
        # AND gates. Conditions 1/2/4 measure overlapping volatility
        # signal; the 4-way AND was over-counted. 3-of-4 = "the market
        # is genuinely coiling on at least 3 axes."
        "min_compression_conditions": 3,
        # Volume threshold lives inline as 0.75 in code; that's a separate fix
        # to plumb through. For now the relaxed ATR/range pair widens the
        # firing window enough to start collecting MNQ-specific data.
        "min_coil_bars": None,
        "tight_mult":    None,
        "min_tf_votes": 2,
        "stop_buffer_ticks": 3,
        # 2026-05-15: dropped 12 → 6 evals. With ~1.2 evals/min effective
        # cadence, 6 ~= 5 minutes of continuous compression — meaningful
        # but achievable on MNQ. The 2026-04-24 calibration intended 60 min
        # but misread eval cadence as per-15m-bar; the actual effective
        # threshold was already ~10 min, well below the intent.
        "min_squeeze_bars": 6,
        # Stop management — NQ research clamps (Fix 7, 2026-04-20)
        "min_stop_ticks": 40,
        "max_stop_ticks": 120,
        "target_rr": 5.0,
        "max_hold_min": 90,
        # Per-condition diagnostic logging (added 2026-05-15) emits
        # `[EVAL] compression_breakout: NOT_COMPRESSED <flag list>` so the
        # operator can see which of TTM/ATR/Volume/Range is the bottleneck.
        # Re-review at n=30 trades — if any single condition dominates
        # rejections, that's the next knob to tune.
    },

    "opening_session": {
        # UN-RETIRED 2026-05-15 — re-armed in sim only (validated=False).
        # 2026-05-13 retirement rationale (lifetime stats: 4 trades / 25% WR /
        # -$59.58 net) was correct given the previous calibration. After
        # today's deep-dive on 80MB of stdout we now know exactly which
        # sub fires when:
        #   - premarket_breakout (08:30-08:45 CT, any opening type) — 86 SKIPs
        #   - orb (08:45-14:30 CT, any opening type) — 465 NO_SIGNAL + 676 SKIP
        #   - open_auction_in (09:30-12:30 CT, OPEN_AUCTION_IN type) — 215 NO_SIGNAL
        #   - open_auction_out (08:45-11:00 CT, OPEN_AUCTION_OUT type) — 306 NO_SIGNAL
        #   - open_drive (08:35-09:00 CT, OPEN_DRIVE type) — NEVER dispatched
        #     (classifier rarely returns OPEN_DRIVE on MNQ vol profile)
        #   - open_test_drive — also rarely dispatched
        #
        # The strategy is NOT broken — its classifier (classify_opening_type in
        # core/session_levels.py) and sub-evaluator gates are well-designed.
        # They are intentionally selective for high-probability setups.
        # Un-retiring lets data accumulate in the per-sub `[EVAL] opening_session:`
        # log lines so the operator can see the classification distribution
        # over time (Mon-Fri RTH) and decide which sub to lift to a focused
        # top-level strategy.
        #
        # Standing follow-up: open_drive is the "fires at open, runs to pivot"
        # behavior. If after 4 weeks the classifier never returns OPEN_DRIVE,
        # relax _DRIVE_DISPLACEMENT_POINTS (currently 15pt) — but only after
        # observing the actual displacement distribution from the logs.
        #
        # Opening-window family: 4 opening-type branches + Premarket Breakout
        # + 15-min ORB-in-router.
        "enabled": True,
        "stage": "lab",
        "validated": True,  # 2026-05-17: was False — operator override (V2 deployment)

        # Universal guards
        "max_trades_per_day": 4,  # 2026-05-17: was 2 (V2 deployment PATCH 2)
        "day_flat_time_ct": "14:30",
        "news_blackout_min": 5,

        # Universal stops (Fix 6 standard, locked 2026-04-20)
        # 2026-05-17: V2 deployment PATCH 2 — tighter floor (40→32t = 8pt),
        # wider cap (100→200t), confirmation fallback above 80t.
        "min_stop_ticks": 32,
        "max_stop_ticks": 200,
        "use_confirmation_stop_above_ticks": 80,

        # Open Drive — 2026-05-17 V2 PATCH 2: loosen displacement gate
        # (was 15pt — classifier never returned OPEN_DRIVE on MNQ).
        "open_drive_min_displacement_pts": 8,    # was 15
        "open_drive_max_pullback_pct": 0.40,     # was 0.30
        "open_drive_min_volume_ratio": 1.3,      # was 1.4
        "open_drive_entry_volume_ratio": 1.1,    # was 1.2
        "open_drive_trail_ticks": 20,

        # Open Test Drive — 2026-05-17 V2 PATCH 2
        "open_test_drive_test_buffer_ticks": 4,  # was 8
        "open_test_drive_reversal_volume_ratio": 1.3,
        "open_test_drive_stop_buffer_ticks": 4,
        "open_test_drive_time_exit_min": 75,

        # Open Auction In — 2026-05-17 V2 PATCH 2
        "open_auction_in_wick_pct_min": 0.50,    # was 0.60
        "open_auction_in_volume_ratio": 1.2,
        "open_auction_in_stop_buffer_ticks": 8,
        "open_auction_in_time_exit_ct": "12:30",

        # Open Auction Out — 2026-05-17 V2 PATCH 2: require CVD div
        "open_auction_out_wait_min": 15,
        "open_auction_out_stop_buffer_ticks": 8,
        "open_auction_out_time_exit_ct": "11:00",
        "open_auction_out_require_cvd_div": True,

        # Premarket Breakout — 2026-05-17 V2 PATCH 2: stricter range
        "premarket_breakout_min_range_pts": 15,  # was 10
        "premarket_breakout_volume_ratio": 1.4,
        "premarket_breakout_buffer_ticks": 2,
        "premarket_breakout_stop_buffer_ticks": 8,
        "premarket_breakout_time_exit_ct": "10:30",

        # ORB — 2026-05-17 V2 PATCH 2: range floor/cap in points + CVD-aligned
        "orb_window_min": 15,
        "orb_max_range_pct": 0.008,
        "orb_min_range_pts": 11,
        "orb_max_range_pts": 80,
        "orb_require_cvd_aligned": True,
        "orb_cvd_lookback_bars": 5,
        "orb_target_pct_of_or": 0.50,
        "orb_be_pct_of_or": 0.25,
        "orb_time_exit_ct": "14:30",

        # ORB-Fade sub-evaluator — 2026-05-17 V2 PATCH 2
        "orb_fade_enabled": True,
        "orb_fade_min_wick_pct": 0.50,
        "orb_fade_min_cvd_divergence": True,

        # 2026-05-17 V2 PATCH 2: confirmation-stop fallback.
        "stop_fallback_mode": "confirmation",
    },

    "vwap_band_pullback": {
        # 1σ/2σ VWAP-band pullback + RSI(2) — ported from b12 research.
        # Runs alongside vwap_pullback (proximity) for head-to-head lab data.
        # Author prediction (b12 header): PF 1.5-1.8 at WR 45-55%, RR 1.5-2:1.
        "enabled": True,
        "validated": True,    # 2026-05-17: was False — operator override (V2 deployment)
        "min_bars": 50,
        "rsi_period": 2,
        "rsi_long_threshold": 30,
        "rsi_short_threshold": 70,
        "atr_period": 14,
        "min_volume_ratio": 0.8,
        "target_rr": 2.0,
        # 2026-05-13 (#15): TF-alignment gate dropped 3→2. The mean-reversion
        # entry already has RSI(2) extreme + bar-reversal + volume confirmation,
        # so demanding 3-of-N timeframes align directionally was over-gating
        # — bands tend to touch on the LAST candle before a reversal, when
        # only the lowest TF has flipped. 2/N keeps the trend-day filter
        # (NEUTRAL → no signal) but lets early-reversal touches through.
        "min_tf_votes": 2,
        # NQ-research clamps (matches Fix 7 values). If natural 2σ-band
        # stop > max_stop_ticks, signal is skipped (Fix 8-style guard).
        "min_stop_ticks": 40,
        "max_stop_ticks": 200,  # 2026-05-17: was 120 — NQ vol regime fix (V2)
        "max_hold_min": 60,
        # 2026-05-17: Phase 6 V2-deployment addition.
        "stop_fallback_mode": "confirmation",
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
        "validated": True,    # 2026-05-17: was False — operator override (V2 deployment)
        "sigma": 2.1,                    # entry-band sigma
        "outer_sigma": 2.5,              # stop is just beyond this
        "atr_stop_buffer": 0.5,          # multiplier on ATR added to outer band
        "atr_period": 14,
        "min_bars": 30,
        "min_volume_ratio": 0.7,         # looser than band_pullback's 0.8
        "min_stop_ticks": 30,            # NQ noise floor (looser; reversion entries are tight)
        "max_stop_ticks": 200,           # 2026-05-17: was 100 — NQ vol regime fix (V2)
        "target_rr": 1.5,                # fallback when target_at_vwap=False
        "target_at_vwap": True,          # default: target VWAP itself, not opposite band
        # Time-of-day block (CT). 08:30-09:30 = open volatility (per
        # bias_momentum forensic finding §4 in trade_analysis_2026-05-03.md).
        "block_windows": [("08:30", "09:30")],
        "max_hold_min": 30,
        # 2026-05-17: Phase 6 V2-deployment addition.
        "stop_fallback_mode": "confirmation",
    },
    # ─── Sprint H v3 (2026-05-04): Footprint + CVD Reversal ────
    # Institutional 4-confluence reversal at MenthorQ HTF levels.
    # Operates on a 1,500-tick volumetric stream from NT8 (Order
    # Flow+ data emitted by TickStreamer.cs and persisted by
    # bridge_server.py:_handle_volumetric_bar).
    #
    # Stays DORMANT logging DATA_NOT_AVAILABLE until TickStreamer.cs
    # ships the volumetric emitter on the NT8 side. Lab-only until
    # 50+ trades + PF > 1.3.
    "footprint_cvd_reversal": {
        "enabled": True,
        "validated": True,            # 2026-05-17: was False — operator override (FCD-6 V2)
        # HTF level confluence
        "level_buffer_ticks": 8,
        "require_menthorq_level": True,
        # CVD divergence
        "divergence_lookback_bars": 10,
        # Footprint
        "oversized_imbalance_ratio": 10.0,
        "absorption_min_delta": 50.0,
        "absorption_max_range_ticks": 10.0,
        # Compression (5 sub-dimensions × 5pts each)
        "compression_lookback_bars": 3,
        "compression_baseline_bars": 20,
        "compression_size_threshold": 0.6,    # < 0.6× baseline = compressing
        "compression_volume_floor": 0.8,      # >= 0.8× baseline = volume holding
        "compression_effort_threshold": 1.5,  # > 1.5× baseline = effort spike
        # Entry
        "entry_threshold_iqs": 70,            # raise = pickier; lower = more signals
        # Stops / targets
        "stop_buffer_ticks": 4,
        "max_stop_ticks": 60,
        # 2026-05-17: FCD-6 (Phase 6) — add 8t floor to pair with FCD-4
        # which enforces min_stop_ticks via max(_min, ...) in Phase 7 patch.
        "min_stop_ticks": 8,
        "target_t1_rr": 1.0,                  # 50% scale-out
        "target_t2_rr": 2.0,                  # final target
        "scale_out_pct": 0.5,
        "time_stop_bars": 20,
        # Gates (lists in JSON; tuples internally)
        "lunch_block_start_ct": [10, 0],
        "lunch_block_end_ct":   [13, 29],
        "session_open_ct":      [8, 30],
        "session_close_ct":     [15, 0],
        "session_open_skip_min": 5,
        "session_close_skip_min": 5,
        "block_negative_strong_long": True,
        "block_positive_strong_short": True,
        "data_freshness_sec": 90,
        "min_history_bars": 25,
    },

    # ── 2026-05-17: V2 strategy overhaul deployment (Phase 4) ─────────
    # All 6 entries ship with validated=True per operator override.
    # Standard Wilson-CI guardrail bypassed — phase 10 (Restore) gates
    # final live promotion behind the n>=30 trades / WR>=50% / PF>=1.3 rule.
    # See docs/CLAUDE_CODE_DEPLOYMENT_PROMPT.md (Phase 4) for full context.

    "nq_lsr": {
        "enabled": True,
        "validated": True,   # FIRING (operator override — was lab-only)
        "session_windows_ct": [("08:30", "11:00"), ("13:30", "15:00")],
        "max_trades_per_day": 4,
        "max_stop_ticks": 30,
        "min_stop_ticks": 8,
        "min_wick_pct": 0.50,
        "min_volume_ratio": 1.5,
        "level_cooloff_minutes": 60,
        "time_exit_minutes": 30,
        "t2_target_rr": 2.5,
        "bar_freshness_sec": 90,
        "volume_lookback": 20,
        "cvd_divergence_lookback": 5,
        "near_hvn_lvn_ticks": 5,
        "bigmove_bonus_threshold": 50,
        "es_divergence_bonus": 15,
        "tpo_trend_skip": True,
        "exhaustion_exit_threshold": 70,
    },

    "orb_fade": {
        "enabled": True,
        "validated": True,
        "session_windows_ct": [("08:45", "12:00")],
        "max_trades_per_day": 2,
        "max_stop_ticks": 30,
        "min_stop_ticks": 8,
        "min_wick_pct": 0.50,
        "min_volume_ratio": 1.3,
        "lookback_for_breakout": 20,
        "cvd_lookback": 5,
        "volume_lookback": 20,
        "time_exit_minutes": 30,
        "bar_freshness_sec": 90,
    },

    "orb_v2": {
        "enabled": True,
        "validated": True,
        "or_duration_minutes": 15,
        "min_or_size_points": 11,
        "max_or_size_points": 80,
        "max_or_size_atr_mult": 4.0,
        "max_or_size_hard_cap_points": 150,
        "max_entry_delay_minutes": 60,
        "target_rr": 2.0,
        "max_stop_ticks": 60,
        "min_stop_ticks": 12,
        "cvd_lookback": 5,
        "require_cvd_aligned": True,
        "session_open_et": "09:30",
        "stop_buffer_ticks": 2,
    },

    "compression_breakout_v2": {
        "enabled": True,
        "validated": True,
        "max_trades_per_day": 3,
        "bb_period": 20,
        "bb_std": 1.5,            # NQ-tuned (was 2.0 Carter default)
        "kc_period": 20,
        "kc_atr_mult": 1.5,
        "atr_period": 14,
        "atr_smoothing": 50,
        "atr_compression_ratio": 0.60,
        "window_bars": 8,
        "min_compressed_in_window": 5,
        "min_compression_conditions": 3,
        "breakout_volume_mult": 1.5,
        "range_atr_ratio": 1.5,
        "min_breakout_dist_atr": 0.25,
        "stop_atr_mult": 1.5,
        "max_stop_ticks": 60,
        "min_stop_ticks": 12,
        "target_rr": 2.0,
    },

    "compression_breakout_micro": {
        "enabled": True,
        "validated": True,    # FIRING — catches fast 5-15pt breakouts on 1m TF
        "max_trades_per_day": 5,
        "bb_period": 20,
        "bb_std": 1.4,            # tighter still — 1m bars naturally tighter
        "kc_period": 20,
        "kc_atr_mult": 1.5,
        "atr_period": 14,
        "atr_smoothing": 30,      # 30 min context (vs V2's 50)
        "atr_compression_ratio": 0.65,
        "window_bars": 10,        # 10 min look-back
        "min_compressed_in_window": 6,    # 6 of 10 — tolerates 40% noise
        "min_compression_conditions": 3,
        "breakout_volume_mult": 1.4,
        "range_atr_ratio": 1.5,
        "min_breakout_dist_atr": 0.30,    # clearer break on 1m
        "stop_atr_mult": 1.5,
        "max_stop_ticks": 30,     # scalp range
        "min_stop_ticks": 8,
        "target_rr": 1.5,         # scalp R:R
    },

    "vwap_pullback_v2": {
        "enabled": True,
        "validated": True,
        "max_trades_per_day": 4,
        "max_vwap_dist_ticks": 60,
        "min_tf_votes": 2,
        "stop_atr_mult": 2.0,
        "max_stop_ticks": 200,    # was 120 in V1 — V2 widens for NQ 2026
        "min_stop_ticks": 16,
        "target_rr": 1.8,
    },
}

# Backfill default ai_filter_mode="advisory" on every strategy that
# hasn't set one explicitly. Keeps the S6 surface one line per strategy
# without editing each block.
for _name, _cfg in STRATEGIES.items():
    _cfg.setdefault("ai_filter_mode", DEFAULT_AI_FILTER_MODE)
del _name, _cfg
