# Operator Brief — pt5 → pt7 (Confluence-Voter Sprint Close)

**Date:** 2026-05-22 00:10 CT
**Branch:** `weekly-evolution/2026-05-17`
**Commits shipped this sprint:** `f0dbce1` (pt5) → `c7b495a` (pt6) → `06c55b7` (pt7)
**Operator action required before tomorrow's open:** see §6 (NT8 chart reload)

---

## TL;DR

1. **Universal alpha gate shipped to 7 strategies.** Per `a16cf0ef`'s 5y backtest
   of every voter on every RTH strategy, `tf_60m + es_correlation` is a universal
   edge — agreement lifts WR from 38-47% baseline → 51-58% across the 6 RTH
   directional strategies, plus a +18pp lift on `e_multi_day_breakout` (77.8% → 96%).
   12,039 of 36,234 trades in the 5y backtest (33% of volume) produced 83% of
   total 5y P&L = **+$257K**. The gates now route every trade through this filter
   before signal emission.

2. **No more sim_bot ZERO_GATE incidents.** pt4 closed `B-030` (sim_bot was
   silently overwriting production gates with lab-harness 0-threshold values).
   pt5-pt7 sat 7 additional defensive gates on top. Yesterday's 8.3% WR / 11L
   was the visible failure of B-030 — backtest WR projects 38-40% with the new
   gates active.

3. **ORB ready for tomorrow.** `orb_max_range_pts` is 110 (was 80, lifted in pt5
   after yesterday's 96.2pt OR rejected all 414 evals). pt6 added
   `tf60m_es_gate` + regime veto on `AFTERNOON_CHOP` + `LATE_AFTERNOON`. Live
   state replay-on-restart confirmed working (the 2026-05-03 Sprint I fix);
   `rth_15min_high/low` will populate fresh tomorrow at 08:45 CT.

4. **Volumetric recorder unblocked (Python side).** B-032: scheduled task was
   failing silently with ERROR_FILE_NOT_FOUND for 4 days because `python` wasn't
   in the scheduled-task user's PATH. Updated to full interpreter path; first
   fresh snapshot landed in `data/historical/volumetric/2026-05-22.jsonl`.
   **Still needs operator action:** reload TickStreamer.cs on the MNQM6 chart
   in NT8 — the indicator stopped writing `volumetric_latest.json` on
   2026-05-19 23:03 CT.

5. **3x validation: GREEN.** pytest 2206/0/14 · halt_signatures PASS ·
   validation_tracker report written.

---

## 1. What pt5/pt6/pt7 actually changed

### pt5 (f0dbce1) — three operator-approved gate changes
| Change | File | Impact |
|---|---|---|
| `bias_momentum`: hard veto on `OVERNIGHT_RANGE` regime | `strategies/bias_momentum.py` | Removes -$2.55/trade drag (7,380 historical trades cost -$18.8K) |
| `bias_momentum`: inline `tf_60m + ES` agreement gate | `strategies/bias_momentum.py` | Per `CONFLUENCE_VOTER_RESEARCH_2026-05-21.md`: WR 38.8% → 51.6% on 12,039-trade subset |
| `orb_max_range_pts`: 80 → 110 | `config/strategies.py:570` | Today's 96.2pt OR no longer rejected; widens to normal-vol range |

### pt6 (c7b495a) — per-strategy confluence gates from `a16cf0ef` research
| Strategy | Gate added | Research delta |
|---|---|---|
| `bias_momentum` | refactored inline → shared helper (no behavior change) | DRY cleanup |
| `spring_setup` | `tf60m_es_gate` | WR 41.4% → 54.6%, **+$24K/yr** |
| `vwap_pullback_v2` | `tf60m_es_gate` | WR 39% → 54% |
| `e_multi_day_breakout` | `tf5m_es_gate` | WR 77.8% → **95.97%** (biggest single-gate lift, n=273) |
| `vwap_band_pullback` | `regime_veto(OPEN_MOMENTUM)` | Most extreme drag: -$35.95/trade |
| `raschke_baseline` | `regime_veto(OPEN_MOMENTUM)` + `tf60m_es_gate` | -$7.43/trade drag |
| `opening_session.orb` | `regime_veto(AFTERNOON_CHOP, LATE_AFTERNOON)` + `tf60m_es_gate` | -$9.40/-$3.04 drag |
| `opening_session.open_drive` | `tf60m_es_gate` | WR → ~55% |

All gates honor per-strategy back-out config flags (`require_tf60m_es_gate`,
`require_tf5m_es_gate`, `veto_regimes_enabled`, `orb_regime_veto_enabled`).
Graceful-degrade when voter data unavailable (cold start, MES feed dormant).

New shared helper: `core/confluence_gates.py` (258 LOC). 19 unit tests in
`tests/test_confluence_gates.py` pin the canonical behavior.

### pt7 (06c55b7) — B-032 volumetric recorder Python-side fix
PhoenixVolumetricRecorder scheduled task was firing every 10 min for 4 days
straight with `LastTaskResult: 2147942402` (ERROR_FILE_NOT_FOUND). Fix:
`Set-ScheduledTask` updated `Execute` from `"python"` to the full interpreter
path. Recorder now writes successfully every 10 min. NT8 side still needs
operator action (§6).

---

## 2. Research outputs — agent summary

### Agent `a16cf0ef` — per-strategy confluence voter (5y backtest)
**Scope:** every voter on every RTH directional strategy, IC + per-regime
P&L delta + WR uplift if used as a hard gate.

**Top universal findings:**
- `tf_60m + es_correlation` agreement is the dominant predictor across 6 RTH
  directional strategies. Lift +13-17pp WR; +$257K cumulative P&L on the 5y
  subset where both agree.
- `tf_5m + es_correlation` is the strongest single combo on breakout strategies
  (`e_multi_day_breakout`: 77.8% → 96% WR, n=273).
- Regime vetos are concentrated: `OPEN_MOMENTUM` is fatal for mean-revert
  setups (`vwap_band_pullback` -$35.95/trade); `AFTERNOON_CHOP` /
  `LATE_AFTERNOON` drag ORB; `OVERNIGHT_RANGE` drags momentum.

**Counter-intuitive findings (logged for follow-up — no live damage today):**
- `msu_score` (microstructure score) has IC **-0.152** — higher score → lower
  WR (anti-predictive; suspected adverse-selection trap). Currently advisory-
  only so no live $ damage, but a future "wire msu_score into a gate" PR would
  systematically lose money. Tracked as **B-031**.
- `tf_1m` and `orb_direction` are pure noise voters (IC ≈ 0). Currently in
  several confluence tallies — should be removed but no $ urgency. **B-033**.
- `dom_imbalance` sign on `spring_setup` may be inverted (worse fills when
  signal says "support is strong"). Needs live A/B before fixing. **B-035**.

### Agent `a085e4d6` — live-TBBO/DOM/footprint voter discovery
**Scope:** cross-validate the 5y findings against the 2-month live volumetric
capture (`data/historical/volumetric/`) and the 44M-tick TBBO replay
(`data/historical/databento_tbbo/`).

**Key finding:** the live volumetric capture is **broken** — only 1 file (648
bytes) from 2026-05-18 across 2 months. Cross-validation against 2026 live
microstructure is not possible until the recorder is fixed. **B-032** filed
HIGH priority; Python-side fixed in pt7, NT8 side still requires operator
action (§6).

**Secondary findings (deferred):**
- `high_precision_only` should gate on `tick_rate_60s ≥ 600` (WR 47% → 62%
  on the 35% of historical trades that pass). **B-034**.
- `ib_breakout` + `es_nq_confluence` have n<100 in 5y — Wilson CI too wide for
  defensible voter calls. Intentionally **defer** any new gates until ≥100
  trades. **B-036**.
- `opening_session.open_drive` B2 fix (target = entry ± 2R, no more pivot_pp)
  needs 5L+5S live samples to fully close confidence. **B-037**.

All deferred items recorded in `docs/BUGS_AND_TODOS.md`.

---

## 3. ORB readiness — tomorrow's open

### Code path (verified end-to-end)
```
08:30 CT  RTH open  → core/session_levels_aggregator captures rth_open_price
08:30-45  15-min OR → rth_15min_high/low populate via _on_bar_complete
08:45 CT  First 5m close that breaks 15m OR → orb_first_break_direction set
08:45-14:30  evaluate_orb runs every 5-min bar close:
              1. Required-fields guard (now logs WHICH field is None, Sprint I)
              2. OR-size floor (11pt) + cap (110pt — pt5) + %-cap (0.8% of open)
              3. 5m close beyond OR → direction
              4. One-trade-per-day check vs orb_first_break_direction
              5. NEW pt6: regime_veto(AFTERNOON_CHOP, LATE_AFTERNOON)
              6. NEW pt6: tf60m_es_gate
              7. CVD-aligned gate (orb_cvd_lookback_bars=5)
              8. Stop math (structural OR opposite, confirmation-fallback above 80t)
              9. T1 = 50% of OR · BE at 25% of OR · time-exit 14:30
```

### Config (config/strategies.py:567-575)
```python
"orb_window_min": 15,
"orb_max_range_pct": 0.008,           # 0.8% of RTH open
"orb_min_range_pts": 11,              # skip ultra-low-vol days
"orb_max_range_pts": 110,             # pt5: was 80
"orb_require_cvd_aligned": True,
"orb_cvd_lookback_bars": 5,
"orb_target_pct_of_or": 0.50,
"orb_be_pct_of_or": 0.25,
"orb_time_exit_ct": "14:30",
```

### Live state (prod_bot PID 34632, restarted 23:58 CT)
- ✅ session_levels replay-on-restart fired (`[REPLAY] session_levels backfilled
  from 400 bars`) — Sprint I machinery confirmed in logs.
- ✅ All 12 strategies loaded: `bias_momentum, spring_setup, ib_breakout,
  opening_session, vwap_band_pullback, vwap_band_reversion, vwap_pullback_v2,
  es_nq_confluence, a_asian_continuation, e_multi_day_breakout,
  g_inside_bar_breakout, raschke_baseline`.
- ✅ Currently in `OVERNIGHT_RANGE` regime (expected, post-midnight CT).
  `bias_momentum` correctly suppressed (RANGE day filter).
- ⏳ At 08:30 CT, RTH window opens; at 08:45 CT, ORB sub-evaluator becomes
  eligible and `rth_15min_high/low` lock in. First eligible 5m bar close
  thereafter that breaks the OR + passes regime/tf60m/CVD gates → signal.

### Expected behavior change vs yesterday
- Yesterday: 414 in-window ORB evals all rejected at `or_too_wide_pct` or
  `or_too_wide` (80pt cap vs 96.2pt OR). Zero signals fired.
- Tomorrow with same OR width: passes the 110pt cap, runs through CVD + new
  gates. **Estimate:** 1-2 signals fire on a normal directional day; 0 signals
  on a chop day or when tf_60m disagrees with the break direction (this is the
  desired behavior — quality over quantity).

---

## 4. 3x validation — green across the board

| Tool | Result | Output file |
|---|---|---|
| `pytest tests/` | **2206 passed, 14 skipped, 0 failed** (96.5s) | — |
| `tools/verify_halt_signatures.py` | **Overall: PASS** | `out/halt_verify_2026-05-22.md` |
| `tools/validation_tracker.py --post-b13-only` | report written | `out/validation_status_2026-05-22.md` |

### Notable from validation_tracker (post-B13 sample):
- `bias_momentum`: n=109 TENTATIVE, WR=33% [25-42%], net=+$467.62 — distorted
  by the B-030 incident; backtest WR is 38.8% and the new gates should pull
  live closer to that range.
- `vwap_pullback`: n=64 PRELIMINARY, WR=62% [50-73%], net=-$356 — high WR but
  small-trade losses; PF analysis pending more samples.
- `dom_pullback`: n=57 PRELIMINARY, WR=18% — confirms the pt3 DELETE call was
  correct.
- `spring_setup`, `opening_session`, `vwap_band_reversion`: all
  `INSUFFICIENT_SAMPLE` (n<30) — too early to validate; gates from pt6 should
  begin tightening signal volume in the right direction.

---

## 5. Open items (deferred — no $ urgency before tomorrow's open)

| ID | Item | Priority | Why deferred |
|---|---|---|---|
| **B-031** | `msu_score` IC -0.152 (anti-predictive) | MEDIUM | Advisory-only today, no live damage. Audit formula sign before any future "add msu_score to gate" PR. |
| **B-032 (NT8 side)** | TickStreamer.cs stopped writing volumetric_latest.json on 5/19 | **HIGH — needs YOU** | See §6. |
| **B-033** | `tf_1m` + `orb_direction` are noise voters in min_confluence tallies | LOW | No $ damage; just confluence noise. |
| **B-034** | `high_precision_only` needs `tick_rate_60s ≥ 600` gate | MEDIUM | +15pp WR available; standalone PR (no test churn). |
| **B-035** | `spring_setup` `dom_imbalance` sign may be inverted | MEDIUM | Needs 2-week live A/B before flipping the sign. |
| **B-036** | `ib_breakout` + `es_nq_confluence` small-n; no new gates | DEFER | Intentional discipline — Wilson CI too wide on n<100. |
| **B-037** | `open_drive` B2 fix needs 5L+5S live samples | INFO | New tf60m gate may slow signal volume — widen watch to 4 weeks. |

---

## 6. Operator action required before tomorrow's open

### NT8 chart reload (B-032 NT8 side)
**Symptom:** `data/volumetric_latest.json` mtime is **2026-05-19 23:03 CT** —
nothing's written it for 3 days. The Python recorder is now correctly polling
that file every 10 min, but it's dedup-skipping the same stale snapshot.

**Fix steps (~30 seconds):**
1. Open NinjaTrader 8.
2. Right-click the MNQM6 chart → **Indicators...**
3. Find **Phoenix TickStreamer** in the Configured list → click **Remove**.
4. Click **Apply**.
5. Click **+ Add** → select **Phoenix TickStreamer** again → **OK**.
6. Click **Apply** → **OK**.

The indicator's `OnBarUpdate` should fire on the next bar close and start
writing `volumetric_latest.json` again. Within 10 min, the recorder will
catch the fresh snapshot and start the data flow.

**Verification:**
```powershell
# Should show mtime within last 10 minutes
ls "C:\Trading Project\phoenix_bot\data\volumetric_latest.json"
# Should show new lines appearing every 10 min
Get-Content "C:\Trading Project\phoenix_bot\data\historical\volumetric\_recorder.log" -Wait
```

### Nothing else required
- Both bots are running with pt6+pt7 code loaded (prod_bot PID 34632,
  sim_bot PID 9628).
- ORB config is set for tomorrow's 96-110pt expected range.
- All 12 strategies are armed; new gates will filter signals as the day plays.

---

## 7. What to watch tomorrow

| Window | Watch for |
|---|---|
| 08:30-08:45 CT | `rth_15min_high/low` populates (look for `[BAR 1m]` lines with `or_high/or_low` non-None) |
| 08:45 CT first 5m close | ORB sub-evaluator becomes eligible — either fires or logs a specific `NO_SIGNAL` reason (the new `regime_veto` / `tf60m_es_gate_reject` reasons are informative) |
| 09:00-12:30 CT | Most signals concentrated here historically — watch for any strategy firing repeatedly without gate filtering (would suggest a config flag is off) |
| End-of-day | Run `python tools/daily_session_summary.py` → check WR vs trailing baseline; flagged anomalies surface here |

If ORB still does not fire on a normal-vol day (50-90pt OR), there's one more
suspect: the `orb_max_range_pct=0.008` % cap. On NQ at 29000, 0.8% = 232pt, so
this isn't tight. If you see `NO_SIGNAL orb or_too_wide_pct`, that's the culprit
— but the points cap (110) will trip first under any normal condition.

---

_Brief generated 2026-05-22 00:10 CT by main session. All artifacts committed
to `weekly-evolution/2026-05-17` (commits `f0dbce1`, `c7b495a`, `06c55b7`)
and pushed to origin. Memory write-back will fire on session end._
