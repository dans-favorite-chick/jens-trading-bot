---
name: strategy-es_nq_confluence
description: Phoenix strategy es_nq_confluence (Phase 12C, MNQ-leads-MES boost detection). Triggers when modifying or debugging es_nq_confluence, or analyzing signals from this strategy. Read this file before editing strategies/es_nq_confluence.py.
---

# Strategy: es_nq_confluence

## What it does
Phase 12C ship — detects MNQ outperforming MES by ≥ 25 basis-points × 100 on the just-closed 5-min bar, with rolling-50 correlation ≥ 0.85. LONG-only. Selected from 108-config sweep + 30 exit-methodology comparator. **Backtested 5 years (2021-05-17 → 2026-05-17): 131 trades / 50.4% WR / $1,548 / PF 2.63 / max DD $72 / 6/6 years positive INCLUDING 2022 bear (-33% NQ: +$1,032).**

## Trigger condition
Just-closed 5m bar. Compute `boost = (mnq_5m_return - mes_5m_return) × 10000`. If `boost ≥ 25` AND rolling-50 Pearson correlation `≥ 0.85`, enter LONG at market.

## Entry gates
- **boost_threshold=25.0** (99.9th percentile of |MNQ - MES| 5m return diff)
- **corr_threshold=0.85** (rolling-50 Pearson of returns)
- **corr_lookback=50** (~4 hours of 5m bar context)
- LONG-only (no SHORT path)

## Stop / target
- Stop: 24 ticks below entry (= $12 risk on 1 MNQ)
- Target: 96 ticks above entry (= $48 reward on 1 MNQ)
- RR: 4:1 (asymmetric capture for rare high-conviction moves)

## Known issues / data dependency (CRITICAL)
**DORMANT pending MES feed.** Strategy requires `market["mes_bars_5m"]` (parallel MES 5-min bars). As of Phase 12C ship (2026-05-18), TickStreamer streams only MNQ; the backtest worked because Databento delivered MES bars. Live Phoenix gets zero MES context, so the strategy logs `DATA_NOT_AVAILABLE` once-per-process then `SKIP data_not_available` every eval. Same dormant-state pattern as `footprint_cvd_reversal` pre-volumetric.

ZERO behavioral risk to live trading — strategy is correctly fail-safe. `validated=False` until MES feed lands AND 30+ live trades confirm backtest.

### Sequence to make this strategy fire live
1. Operator: load TickStreamer indicator on a MES chart in NT8 (same C# code, different instrument)
2. Code: `bridge/bridge_server.py` fan out MES ticks under `mes_*` JSON keys
3. Code: `core/tick_aggregator.py` builds parallel `mes_bars_5m` (plus 1m for fill-precision parity)
4. Code: `bots/base_bot.py` enriches `market["mes_bars_5m"]` from the aggregator
5. `strategies/es_nq_confluence.py` — no change required (already reads `market["mes_bars_5m"]`)

## Reference files
- `strategies/es_nq_confluence.py:1-50` — full docstring (backtest evidence + regime profile)
- `config/strategies.py:849-870` — config block
- `memory/context/KNOWN_ISSUES.md` — open MES-feed blocker
- `backtest_results/backtest_v3_sweep_results.csv` — 108-config sweep ranking
- `backtest_results/exit_methodology_v3_results.csv` — 30 exit-methodology comparator

## DO NOT
- Do NOT flip `validated=True` until (a) MES feed lands AND (b) 30+ live trades confirm the backtest. Wilson-CI guardrail applies.
- Do NOT add a SHORT path — the 5y backtest selected LONG-only as the regime-robust configuration (including 2022 bear delivering +$1,032).
- Do NOT lower `corr_threshold` below 0.85 — the rolling correlation filter is what gives the strategy regime robustness.
- Do NOT add MES-feed plumbing without coordinating with bridge_server.py / tick_aggregator.py / base_bot.py — see the 5-step sequence in KNOWN_ISSUES.md.
- Do NOT change the 24/96 stop/target ratio without re-running the 5y exit-methodology comparator.
