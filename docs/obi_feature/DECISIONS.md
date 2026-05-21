# OBI Feature — Decisions

**This file is write-once. Do not edit predictions or criteria after seeing results.** The whole point is pre-commitment. If you find yourself wanting to change the criteria after data comes in, that is exactly the failure mode this document exists to prevent.

## Hypothesis (committed before any data)

Adding Order Book Imbalance features as a filter to phoenix_bot's strategies will improve their net expectancy by **≥ 10%** when measured over 4 weeks of forward-test, comparing OBI-on vs OBI-off decisions on the same generated bar signals.

## Decision rule (committed before any data)

After Phase 3 (4-week log-mode validation):

- **GO** to Phase 4 if and only if:
  - OBI-on hypothetical expectancy is ≥ 10% higher than OBI-off actual expectancy
  - Both samples have ≥ 200 trades (statistical significance per project standards)
  - Max drawdown of OBI-on ≤ Max drawdown of OBI-off + 25% (realistic-conditions adjustment)
  - No critical failures in Phase 2 or Phase 3 (data quality, OOO rate, heartbeat reliability)

- **KILL** the feature if any of the above fail. Disable OBI in production. Either:
  - (a) Document what was learned, re-engineer based on findings, restart from Phase 1, OR
  - (b) Abandon and move on. The recorder (Phase 0b) keeps running — the data has other uses.

- **NO extensions.** "Let's give it another month" is the failure mode. 4 weeks is the budget.

## Why these specific numbers

- **10% expectancy improvement**: smaller than this is in the noise band for 200-trade samples; not worth the operational complexity of maintaining OBI infrastructure.
- **200 trades minimum**: matches `references/backtest-validation.md` rules already established for the project.
- **+25% max DD tolerance**: matches the project's realistic-conditions adjustment for backtest → live transition.
- **4 weeks**: enough to capture varied market regimes (trend / range / news), short enough to force a decision.

## Signed-off by

- User (Jennifer): _________________ Date: _________
- (Optional countersign — a trusted second party can help with the pre-commitment)

## Status log

| Date | Status | Notes |
|---|---|---|
| (today) | Hypothesis committed | Pre-Phase-0a |

(Update only with phase transitions and the final GO/KILL decision. Do NOT add notes that re-litigate the hypothesis.)

## Common rationalization traps to watch for

When Phase 3 data comes in, the temptation will be:

- "The 4 weeks happened to include unusual conditions — let's extend." → NO. The criteria account for variety; that's why 200 trades + 4 weeks.
- "9% improvement is close to 10% — let's count it." → NO. Numbers were chosen with margin; 9% means kill.
- "But the wall-aware stops in Phase 5 would be the real value, let's just go straight to that." → NO. The kill criterion exists because we can't trust unvalidated optimism. If Phase 3 doesn't show value, Phase 5 won't either.
- "Let me re-run with different OBI threshold values…" → That's curve-fitting. The hypothesis was tested with the design parameters; changing them post-hoc is goalpost moving.

The discipline to honor the kill criterion is more valuable than any feature.
