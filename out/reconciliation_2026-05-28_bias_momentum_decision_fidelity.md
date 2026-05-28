# Reconciliation — bias_momentum (decision-fidelity basis) — SIGN-OFF CANDIDATE
_Generated 2026-05-28. Method: field-level + decision-level fidelity of the de-stubbed backtester vs live prod eval-log ground truth, replaying the bot's OWN recorded bars (`--bar-source recorded`). Tooling: `tools/replay_enrichment/{enrichment_audit,fidelity_vs_eval_logs}.py`._

## Verdict (honest)

The backtester reconstructs **every gate input for `bias_momentum` faithfully EXCEPT `cr_verdict`/`day_type`.** That single field is a material, documented residual. A *clean, statistically-meaningful decision-level pass for `bias_momentum` is not obtainable from current data* (reasons below). The defensible number on offer is the **field-level fidelity**; whether it suffices for a freeze lift is an operator judgment, with the `cr_verdict` caveat explicit.

## Field-level fidelity — clean-cr window 2026-05-27→05-28, recorded bars (n=702 minutes)

| Field | Result | Notes |
|---|---|---|
| price | 0.00% err, corr 1.000 | bars identical to live |
| vwap | 0.00% err, corr 1.000 | faithful |
| **tf_bias 1m** | **99.43%** | gate input — faithful |
| **tf_bias 5m** | **94.73%** | gate input — faithful |
| **regime** | **100.00%** | gate input (regime_veto) — faithful |
| atr_1m / atr_5m | ~4.6% / ~12% med rel err | sizing; acceptable |
| **cr_verdict** | **54.42%** | gate input (→ day_type skip) — **THE RESIDUAL** |
| dom_imbalance | not reconstructable | not in bar data; confluence only |
| cvd (cumulative) | scale-divergent | bar-approx; `cvd_health` uses recorded delta separately |

## Why no clean decision-level recall for bias_momentum

- `bias_momentum` is selective: on the genuinely-working-cr days (05-27/05-28) it produced **0 + 1 = 1 signal** → N too small for recall.
- The higher-N number quoted earlier (98.2% agreement, 3/8 recall over 05-26..05-27) was measured on **05-26, which was still broken-cr** (cr all `UNKNOWN` that day; the fix only took effect 05-27). So that recall is contaminated, not clean.
- Net: there is **no window with both working cr AND enough `bias_momentum` signals** in existing data.

## The cr_verdict residual (the one remaining lever)

`cr_verdict` matches live only ~54% even on the clean window. Likely cause: `recorded_day_cr` reads the **current/latest** `data/momentum_scores.json` value for *all* replay bars, whereas live used the momentum trajectory **as-of-each-day**. The fix exists but is unwired: `recorded_day_cr.isolated_momentum_file` + chronological `feed_trajectory` at each session EOD. Wiring it would likely raise cr_verdict fidelity. `dom`/`cvd` are not reconstructable from bar data (confluences only, not hard gates).

## What's already PROVEN faithful (commits 7826c56, ac1544b, c50c5a5)

Warmup, tf_bias (algorithm + bar-source), and regime gaps are fixed; the backtester replays the bot's own bars so price/vwap/tf_bias/regime match live at 94–100%. Since the backtester runs the IDENTICAL `strategy.evaluate()`, faithful inputs ⇒ faithful decisions — so for any strategy NOT gated on `cr_verdict`/`day_type`, the backtester is now a faithful proxy.

## Operator decision (the actual gate)

The freeze allows lifting on **sign-off of the divergence numbers vs documented tolerances**. The numbers above are that divergence number. Options:
1. **Sign off now** on the field-level evidence, accepting the documented ~54% `cr_verdict`/`day_type` residual as a known limitation.
2. **Wire the cr momentum-feed** (raise cr fidelity) before signing — the one remaining fixable lever.
3. **Validate a cr-INDEPENDENT strategy** instead (the backtester is already faithful for those).

## Scope note
Lifting `FREEZE_ACTIVE` permits shipping backtest-justified config changes. It does **NOT** flip `LIVE_TRADING` (still `False` / Sim101 paper) — the separate live-readiness checklist (≥200 trades, 4+ weeks stable, kill-switch verify, git tag) still governs real-money trading.
