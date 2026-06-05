[FREEZE-BLOCKED — RESEARCH OUTPUT ONLY. NOT DEPLOYABLE UNTIL FREEZE-LIFT SPRINT SHIPS. See config/strategies.py:40-45 for lift preconditions.]

[PRE-RECONCILIATION — Phase 5 counterfactual PnL and Phase 13 envelope dollar numbers are DIRECTIONAL ONLY until the sim↔backtest reconciliation harness runs and produces a defensible per-strategy divergence number. See config/strategies.py:40-45 freeze-lift precondition #1. The most recent reconciliation attempt (out/reconciliation_2026-06-04_bias_momentum.md) produced verdict=FAIL — zero direction-matched signals.]

# Winning Conditions Sprint — Counterfactual PnL Report
*Phases 5 + 6 deliverable | 2026-06-04*

## 1 · Eval log window

- Window: 2026-05-27 → 2026-06-04 (9 calendar days)

- bias_momentum eval entries: 4127


### Category counts (bias_momentum)

| Category | N | Notes |
|---|---:|---|
| `SKIP_DAY_TYPE` | 2534 | RANGE day skip (new gate; not in pre-Apr-18). NOT simulated — cannot honestly determine next-gate outcome without re-running pre-Apr-18 code. |
| `OTHER_REJECT` | 905 | Reject reasons not matching any pattern above. |
| `CVD_CHOP_GATE` | 207 | Added 2026-04-15. Pre-Apr-18 didn't have this. Could be Category-A but direction-inference is noisy in chop. |
| `VWAP_GATE` | 180 | Existed pre-Apr-18; not a candidate. |
| `EMA_STACK` | 90 | Direction gate. Existed pre-Apr-18; not a candidate. |
| `SESSION_BLOCK` | 72 | 04:00-04:59 CT block (added 2026-06-01). Clean Category-A candidate — simulated. |
| `REGIME_VETO` | 70 | OVERNIGHT_RANGE veto (added 2026-05-22). Existed in some form pre-Apr-18; not simulated. |
| `TF_VOTES` | 38 | min_tf_votes=2 fail. Pre-Apr-18 required 3 — these would have been STRICTER, not looser. |
| `SIGNAL` | 31 | Actually fired. Re-simulated at counterfactual target_rr=1.5. |


## 2 · Pre-Apr-18 vs current `bias_momentum` config diff

Source: pre-Apr-18 commit `a9f61f6` (parent of `73921e4`, the 2026-04-18 tightening).

| param | pre-Apr-18 | current | change |
|---|---|---|---|
| `min_confluence` | 3.5 | 5.5 | TIGHTENED +2.0 |
| `min_momentum` | 55 | 80 | TIGHTENED +25 |
| `target_rr` | 2.0 | 2.5 | RAISED — more ambitious target |
| `max_hold_min` | 25 | 60 | LOOSENED +35min |
| `min_tf_votes` | 3 | 2 | LOOSENED −1 |
| `stop method` | stop_ticks=9 | atr_anchored 2.0×ATR, floor=24t, cap=200t | REWRITTEN — fixed-tick → adaptive |
| `session_block_windows` | — | 04:00-04:59 CT | NEW (2026-06-01) |
| `short_extra_gates` | — | True (with enabled-flag) | NEW (2026-05-03) |
| `rsi_div_hard_gate` | — | True | NEW (2026-05-03) |
| `max_ema_dist_ticks` | — | 60 | NEW (anti-chase) |
| `cvd_health_veto` | — | threshold=-0.4 | NEW |
| `cvd chop-regime veto` | — | LATE_AFTERNOON / CHOP | NEW (2026-04-15) |
| `skip_on_stop_clamp` | — | True | NEW (reject when ATR stop > cap) |
| `walk_forward_gate` | — | 'hard_block' | NEW (F-25 2026-05-25) |
| `explosive_close_pos` | 0.75 / 0.25 hardcoded | 0.65 / 0.35 | LOOSENED — broader bypass |
| `vcr_threshold` | 1.5 hardcoded | 1.2 | LOOSENED |


### Per-parameter isolation note (spec 5.3.f)

Spec 5.3(f) asks to isolate each changed param's contribution to lift. In this eval window, **none of the 1,822 REJECTED bias_momentum evals surface a threshold reject reason** (no `low_confluence` or `low_momentum`). Rejections happen at STRUCTURAL gates (EMA_STACK, VWAP, TF_VOTES, CVD-chop, session_block) that come earlier in the gate stack. Therefore the per-param isolation for `min_confluence` and `min_momentum` yields **zero candidate trades** — the lift attributable to relaxing those thresholds alone is **$0** in the visible window, and the per-param contribution table cannot be populated. This is itself a finding: the post-Apr-18 tightening on confluence/momentum is not what's gating today's bias_momentum activity — structural gates are.


## 3 · Category A — SESSION_BLOCK (04:00-04:59 CT) counterfactual

These are bias_momentum evaluations rejected ONLY by the session_block_window gate added 2026-06-01.  Pre-Apr-18 strategy had no such gate; assuming all other gates would have cleared (verified via gate-stack analysis at eval time), these trades would have fired.

- n candidates: 72 (in eval log)
- n simulated: 58 (skipped 14 for missing direction)
- simulation params: pre-Apr-18 stop_ticks=9 (2.25 pts), target_rr=1.5, max_hold=25min
- **Total counterfactual PnL: $-229.50**
- Mean per fire: $-3.96
- Median per fire: $-9.00
- Win-rate: 22.4%
- n wins: 13, n losses: 45
- Max win: $+13.50, max loss: $-9.00


## 4 · Category B — SIGNAL trades re-simulated at target_rr=1.5

39 bias_momentum SIGNAL fires in the eval window.  Same entry / stop as actually placed, target moved from current `target_rr=2.5` to counterfactual `target_rr=1.5` — closer target hits faster but caps winning trade size.

- n simulated: 31
- **Total counterfactual PnL: $-99.00**
- Mean per fire: $-3.19
- Win-rate: 25.8%
- Exit reason distribution:
  - stop: 23
  - target: 8


## 5 · Random-baseline falsification (Phase 5.2.5)

Sampled 58 random non-eval moments from the same window, randomly assigned LONG/SHORT, simulated at the same pre-Apr-18 params.  If Category A's counterfactual lift exceeds P95 of the random baseline, the lift is real.

- baseline n=58
- baseline total PnL: $-139.50  mean: $-2.41  median: $-9.00  P95: $+13.50
- baseline win-rate: 29.3%
- Category A median: $-9.00 vs baseline P95: $+13.50
- **Falsification verdict:** Category A median does NOT exceed baseline P95. Lift indistinguishable from random — DECLINE the signal.

> **Phase 11 red-team caveat:** baseline and Category A medians are both `$-9.00` (a single losing-trade outcome at 9-tick stop, common to both distributions). At n=58 the empirical P95 is one observation away from a different verdict; the DECLINE is correct but the falsification test is THIN evidence. Stronger evidence would require bootstrap CI on P95 (n_boot≥10 000) or a permutation test against matched-timestamp random samples. The conclusion holds directionally; the test should be re-run with bootstrapping if the operator wants more confidence.

> **R10.2 audit caveat:** the script's `_simulate_one` does NOT actually verify "all other gates would have cleared" for the 58 Category A candidates — the script's inline comment claims this but no code performs the gate-stack re-evaluation. Several of the 58 candidates likely had tf_votes_bullish/bearish well below pre-Apr-18 `min_tf_votes=3` and would have been rejected by that gate even with session_block lifted. Conservative reading: Category A is an UPPER BOUND on the lift, and the true counterfactual is strictly less profitable than the reported -$229.50. (Doesn't flip the DECLINE.)

> **R10.2 timestamp caveat:** the `phase5_simulations.csv` file's `entry_ts` values carry a `+00:00` UTC suffix but the underlying eval-log strings are CT-naive. The simulator's bar-lookup is internally aligned (it labels bars the same way), so the dollar arithmetic is correct, but any human reading of timestamps in the CSV should treat them as CT, not UTC.


## 6 · SKIP_DAY_TYPE caveat (2534 evals)

The 64% of bias_momentum evals that were rejected by `SKIP_DAY_TYPE: RANGE day` belong to a gate that did NOT exist pre-Apr-18. Removing the gate would expose these moments to the rest of the gate stack — but **without re-running the pre-Apr-18 strategy code over the recorded market snapshots, we cannot honestly determine whether the next-stage gates (EMA_STACK, VWAP, TF_VOTES) would have cleared**. The pre-Apr-18 `min_tf_votes=3` requirement is STRICTER than today's `min_tf_votes=2`, so a non-trivial fraction of these would have been rejected on TF_VOTES alone. The naive simulation of all 3,310 as fires would inflate the counterfactual.  Spec 5.7 explicitly cautions against this.

**Action:** the directional signal is real — bias_momentum is heavily filtered by the modern day_type gate — but the dollar number cannot be produced honestly without a re-run of strategy code at pre-Apr-18 configuration.  Recommendation: spin a follow-up sprint (out of scope for Q5) that replays the eval log through a re-loaded pre-Apr-18 strategy module.


## 7 · Phase 5.7 caveats (always-on)

- The counterfactual ASSUMES the bot would have fired at every eligible moment. It does NOT account for slot-interlock (one position at a time per strategy), NT8 fill latency, slippage, or PHANTOM-NT8 silent rejections. Real-world fill rate would be lower than the counterfactual count.
- Simulation uses volumetric-history-derived 1m bars (variable-tick aggregation, not classical 1m). Bar high/low approximations may differ from true minute extremes by a tick.
- Direction for SESSION_BLOCK evals is INFERRED from EMA stack at the moment (not the strategy's actual chosen direction). Some inferred directions may be wrong vs what the strategy would have picked, in which case the simulated PnL has wrong sign.


---
*Generated by `C:\tmp\winning_conditions\phase5_counterfactual.py`*


---
## Phase 6 · Retroactive current-config rejection of historical trades

Per the Phase 0.6 PARTIAL verdict, we evaluate the gates that CAN be tested from persisted market_snapshot fields. Gates we cannot test: `min_confluence` (field never persisted), `SHORT-asymmetric tf_bias` (1m/5m not stored separately), `cr_mom_score`-based min_momentum (only 16% populated).

- bias_momentum historical winners (full dataset): 84
- bias_momentum historical losers: 256

### Per-gate fail rate (% of trades where the gate would reject today)

| gate | win fail/eval'd | win fail % | loss fail/eval'd | loss fail % |
|---|---|---:|---|---:|
| `regime_veto` | 18/84 | 21.4% | 67/256 | 26.2% |
| `session_block` | 0/84 | 0.0% | 6/256 | 2.3% |
| `ema_stack` | 7/84 | 8.3% | 8/256 | 3.1% |
| `vwap_gate` | 4/84 | 4.8% | 13/256 | 5.1% |
| `min_tf_votes_2` | 11/84 | 13.1% | 88/256 | 34.4% |
| `max_ema_dist_60t` | 45/84 | 53.6% | 101/256 | 39.5% |

### Aggregate rejection (any-gate-fail)

- **Winners rejected by ≥1 current gate: 59 / 84 = 70.2%**
- **Losers rejected by ≥1 current gate: 205 / 256 = 80.1%**

### Interpretation (post red-team revision)

**Directional verdict:** the current gate stack filters losses at a higher rate (80.1 %) than winners (70.2 %). The 9.9-pp gap suggests tightening is in the right direction, but the aggregate number is dominated by individual gates that act asymmetrically (some reject more winners, e.g. `max_ema_dist_60t`).

Per-gate fail rate (winners vs losers) is the load-bearing column — read that table above, not an aggregate. Notable patterns:

- `session_block` 04:00-04:59 (added 2026-06-01): low fire rate at historical winners → not over-rejecting.
- `max_ema_dist_60t` (anti-chase): rejects winners at non-trivial rate; not a free win.
- `min_tf_votes_2`: rejects more losers than winners — clear positive signal.

### ~~Rough implied PnL delta~~ — RETRACTED per Phase 11 red-team finding #1

The earlier draft of this section reported a `+$981.84` "implied lift" computed by summing PnL of all trades that fail ANY current gate retroactively. **That number is withdrawn.** It is an accounting artifact:

1. The any-gate-fail aggregation double-counts when multiple gates reject the same trade.
2. The dollar weighting mixes asymmetric stop/target distances across trades, so the sum doesn't equal a realizable PnL change.
3. Phase 6's premise — that historical trades would have produced the same outcome at restored config — is partially tautological: rejected-trade fills exist BECAUSE the trade fired; under restored config the slot-interlock interaction and the price at re-evaluation would differ. Fill-realism caveat (same as Phase 5.7) applies here too.

What we CAN honestly say:
- Per-gate rejection rates (table above) are interpretable.
- The 9.9-pp win-vs-loss gap is directional evidence the tightening filters losses more than wins.
- **No deployable dollar lift number can be produced from this analysis.** The reliable lift estimate would require a full strategy re-run at restored config on a holdout window — that's Phase 13.6, not Phase 6.
