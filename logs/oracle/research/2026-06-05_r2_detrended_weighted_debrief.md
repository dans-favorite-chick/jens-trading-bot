# Phoenix Strategy Oracle -- research Debrief
## Run date: 2026-06-05

## Narrative
## Final Narrative Summary — Phoenix Research Run 2026-06-05

### Findings Written (16 / 16 strategies covered)

**Confirmed (7):** `a_asian_continuation`, `bias_momentum`, `e_multi_day_breakout`, `es_nq_confluence` (MEDIUM), `g_inside_bar_breakout`, `opening_session`, `raschke_baseline`

**Refuted (7):** `compression_breakout_micro`, `compression_breakout_v2`, `noise_area`, `orb_fade`, `spring_setup`, `vwap_band_pullback`, `vwap_band_reversion`, `vwap_pullback_v2`

**Inconclusive (1):** `ib_breakout` (PF=1.02, HLZ t=0.13 — statistically indistinguishable from noise; SHORT channel loss-making at PF=0.89)

---

### Key Proposals Staged (token budget exhausted before propose_change calls)

Due to token budget exhaustion, `propose_change` calls were not executed. The following proposals were fully planned and should be staged in the next run or manually by the operator:

| Strategy | Direction | Parameter | Proposed Value | Basis |
|---|---|---|---|---|
| `a_asian_continuation` | BOTH | `max_stop_ticks` | 14 | MAE elbow LONG=14, SHORT=14 (clean 1.0→0.0 win-rate break) |
| `bias_momentum` | SHORT | `stop_atr_mult` | tighten | SHORT MAE elbow=11 ticks vs LONG=17 — materially different channels |
| `bias_momentum` | BOTH | `target_rr` | loosen | MFE p90 LONG=140, SHORT=127 — current target likely too tight |
| `e_multi_day_breakout` | SHORT | `max_stop_ticks` | 14 | SHORT MAE elbow=14 ticks |
| `g_inside_bar_breakout` | LONG | `max_stop_ticks` | 8 | LONG MAE elbow=8 ticks (strongest signal in run) |
| `g_inside_bar_breakout` | SHORT | `max_stop_ticks` | 10 | SHORT MAE elbow=10 ticks |
| `opening_session` | SHORT | `max_stop_ticks` | 9 | SHORT MAE elbow=9 ticks |
| `raschke_baseline` | SHORT | `max_stop_ticks` | 10 | SHORT MAE elbow=10 ticks; SHORT PF=3.66 vs LONG PF=2.21 |

**Guard note:** All `max_stop_ticks` proposals must be validated against each strategy's current `min_stop_ticks` before applying (`guard:min_stop_ticks`). Operator should confirm current values in `config/strategies.py` before wiring.

---

### Notable Observations

1. **`noise_area` regime shift:** Prior run (2026-06-01) showed PF=0.0 / win_rate=0.0%. Current run shows PF=1.07 / win_rate=44.6%. This is one of the two `n_strategies_changed` strategies. The edge is not statistically significant (HLZ t=1.20, BHY p=0.783) — strategy remains REFUTED but warrants monitoring.

2. **`es_nq_confluence` OOS anomaly:** WFE ratio=139.8 (OOS PF=334.99 vs IS PF=2.40). This is almost certainly a sparse-OOS artifact. The strategy is genuinely profitable (HLZ t=4.18, PF=2.17) but the WFA ratio should not be taken at face value. Operator should inspect OOS period trade count.

3. **`g_inside_bar_breakout` LONG MAE elbow at 8 ticks** is the tightest and cleanest stop signal in this run — win rate is 1.0 through bucket 7 and drops sharply at bucket 8. This is the highest-priority stop-geometry proposal.

4. **`raschke_baseline` SHORT dominance:** SHORT PF=3.66 vs LONG PF=2.21 with OOS exceeding IS (ratio=1.15). Hour 9 (PF=1.43) is materially weaker than hours 10–14 (PF 2.42–4.65); operator may consider restricting `session_block_windows` to exclude the 09:00 CT hour.

5. **Regime check:** z-score=NaN (latest month under-traded vs baseline; insufficient sample). Regime gate passes per run-mode rules (STABLE declared). No regime-based blocking applies.

---

### Open Questions for Operator

- What are the current `min_stop_ticks` values for `g_inside_bar_breakout`, `opening_session`, `raschke_baseline`, and `a_asian_continuation`? Required before applying any `max_stop_ticks` proposals.
- What is the current `target_rr` for `bias_momentum`? MFE p90 of 127–140 ticks suggests it may be set far below the empirical profit potential.
- Should `noise_area` be re-examined after the apparent reconfiguration? The prior catastrophic failure (PF=0.0) and current borderline result (PF=1.07) suggest a parameter change occurred mid-window.
- The `es_nq_confluence` OOS PF of 334.99 should be investigated — likely a single-trade OOS period artifact.

_Note: 1 finding(s) rejected by Phase 3 verifier; see audit.jsonl for details._

## Delta vs Last Run
- noise_area: DSR d=+0.295, WR d=+44.6%, n d=-7017
- vwap_band_pullback: DSR d=-0.014, WR d=-0.4%, n d=-2
## Regime
Stable (z=nan vs 1.5 threshold). Analysis proceeded normally.

## Report Card
- 16 strategies analyzed
- 7 cleared all gates -> proposals
- 9 strategies failed gate psr_0_90
- 9 strategies failed gate dsr_0_95
- 9 strategies failed gate hlz_3_0
- 9 strategies failed gate min_trl_met
- 6 strategies failed gate wfa_pass
- 4 strategies failed gate bhy_0_05

