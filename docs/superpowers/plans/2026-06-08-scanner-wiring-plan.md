# Market Scanner Wiring — Stages 2-4 Roadmap

_Phase I deliverable from the 2026-06-01 → 2026-06-02 overnight master run._
_Design-only. No code changes tonight. Stages 1 already shipped (see §1)._

## 0. Background

`core/market_state.py` (Phase 8 of the Master Fix, commit `275d6ac`)
classifies each 5m bar into one of six composite labels:

```
WHIPSAW_HIGH_VOL  CHOPPY  COMPRESSED
TRENDING_HIGH_VOL TRENDING_NORMAL  NEUTRAL
```

The 5-year backfill (`tools/warehouse/backfill_market_state.py`)
populated 354,270 rows in `market_state_bars` covering 2021-05-17 → 2026-05-15.

The Strategy Oracle's v3 research run uses `splits.by_market_state`
for every strategy panel, so the per-strategy/per-state performance
data is already visible to the analyst at run time. What's NOT yet
wired: the live bot **using** the classification to influence
trading. This roadmap covers that wiring in four stages.

## 1. Stage 1 — Observation only (SHIPPED tonight)

Phase D + D.5 partial deliverables (commits `b255d09`, `55a542b`).

| Component | Shipped? |
|---|---|
| Dashboard `Market State` tile (color-coded, 30s refresh) | ✓ |
| `GET /api/market_state` endpoint (live / warehouse_fallback / stub) | ✓ |
| `GET /api/market_state/per_strategy` per-state PnL endpoint | ✓ |
| `core.trade_memory.set_market_state_source(callable)` hook + auto-stamp | DEFERRED — `core/trade_memory.py` is protected; needs OPERATOR-APPROVED |

The deferred trade_memory wiring is a Monday item too — small change,
same operator-review cadence as the freeze lift.

## 2. Stage 2 — Binary regime gating (target: week of 2026-06-08)

Mirror the `allowed_directions` infrastructure shipped in Phase 4
(commit `f7ed5e7`).

### 2.1 Code changes

Add field to `strategies/base_strategy.py::BaseStrategy`:

```python
class BaseStrategy:
    ...
    allowed_market_states: list[str] | None = None
```

`None` means "all states allowed" (today's behavior). A list of states
gates: in `bots/_signal_router.py` (or wherever the per-signal accept
check lives), after the lunch-zone skip:

```python
strat = self.bot.strategies[signal.strategy]
allowed = getattr(strat, "allowed_market_states", None)
if allowed is not None:
    current_ms = self.bot.market_state.current().get("label")
    if current_ms not in allowed:
        # log "blocked by allowed_market_states" — same dispatch
        # pattern as allowed_directions
        return None
```

### 2.2 Per-strategy initial defaults (STRAWMAN — operator adjusts)

Defaults SHOULD be set per strategy, based on 1-2 weeks of Stage 1
data (entry_market_state field on actual sim trades). Until that data
accumulates, **leave `allowed_market_states=None` everywhere** so no
behavior changes silently.

A defensible first-pass once data exists:

| Strategy | Tentative allowed states |
|---|---|
| `a_asian_continuation` | TRENDING_NORMAL, TRENDING_HIGH_VOL (short Asian-trend bias) |
| `bias_momentum` | TRENDING_NORMAL, TRENDING_HIGH_VOL — block CHOPPY and WHIPSAW |
| `e_multi_day_breakout` | TRENDING_NORMAL, TRENDING_HIGH_VOL |
| `g_inside_bar_breakout` | COMPRESSED, TRENDING_NORMAL (coiled-spring setup) |
| `opening_session` | All EXCEPT WHIPSAW_HIGH_VOL |
| `raschke_baseline` | TRENDING_NORMAL, TRENDING_HIGH_VOL |
| `es_nq_confluence` | TRENDING_NORMAL (single-state confluence is rare; small sample) |

### 2.3 Tests

New file: `tests/test_allowed_market_states.py`, mirroring the
`tests/test_allowed_directions.py` (Phase 4) pattern:

- Strategy without `allowed_market_states` (None) → behavior unchanged
- Strategy with list `[TRENDING_NORMAL]` → signal in TRENDING_NORMAL
  passes; signal in CHOPPY blocks with the canonical log line
- Empty list `[]` → block all (a deliberate kill-switch via gate)
- Invalid state name in the list → fail-closed (raise on bot startup
  so a typo doesn't silently disable the gate)
- Market state `None` (warm-up window) → fail-OPEN: skip the gate, log
  a `warning` so the operator sees warm-up trades happening (else
  warm-up effectively halts trading until the classifier has 20 bars).

### 2.4 Effort

| Sub-task | Hours |
|---|---|
| base_strategy field add + signal_router gate | 1-2 |
| Tests (5 cases) | 1-2 |
| Backtest verification on bias_momentum (compare baseline vs gated) | 4-6 |
| **Total** | **~1 day** |

## 3. Stage 3 — Per-state stop/target multipliers (target: week of 2026-06-15)

After Stage 2 has accumulated 1-2 weeks of binary-gate data, layer in
per-state stop/target geometry adjustments.

### 3.1 Schema addition

```python
# strategies/<name>.py config block:
"state_overrides": {
    "COMPRESSED":         {"stop_atr_mult_x": 0.7, "target_rr_x": 1.3},
    "TRENDING_HIGH_VOL":  {"stop_atr_mult_x": 1.3, "target_rr_x": 1.5},
    # missing state -> 1.0x both (no adjustment)
},
```

`_x` suffix denotes multiplier. Applied at signal-emission time:

```python
overrides = (self.config.get("state_overrides", {}) or {}).get(current_ms, {})
stop_atr_mult = self.config.get("stop_atr_mult", 2.0) * overrides.get("stop_atr_mult_x", 1.0)
target_rr     = self.config.get("target_rr", 2.0)     * overrides.get("target_rr_x", 1.0)
```

### 3.2 Rollout pattern: ONE strategy at a time

Start with `bias_momentum` (live canary). Stage 3 changes for any
strategy require:

1. A pinned baseline backtest (current geometry, no overrides) over a
   3-month window.
2. A Stage-3 backtest with overrides applied.
3. Comparison: PnL delta, max DD delta, n_trades delta.
4. Operator sign-off before the override block lands in config.

### 3.3 Tests

`tests/test_state_overrides.py`:
- Strategy without `state_overrides` → behavior unchanged
- Strategy with override for current state → stop and target multiplied
- Strategy with override for some OTHER state, current state not listed
  → no adjustment
- Override values <0.5 or >2.0 → fail-closed (sanity guard against
  config typos)

### 3.4 Effort per strategy

| Sub-task | Hours |
|---|---|
| Code (single-strategy override pull) | 1-2 |
| Tests | 1-2 |
| Backtest comparison | 4-8 |
| Operator review | 1-2 |
| **Total per strategy** | **~1 day + 0.5 day operator** |

7 winners × 1.5 days each = ~2 weeks for full rollout, sequential.

## 4. Stage 4 — Per-state sizing modulation (target: TBD after Stage 3)

The most aggressive lever. Touches `core/risk_manager.py`, which is
**PROTECTED** per `.claude/PROTECTED_FILES.md`. Every change here
requires OPERATOR-APPROVED.

### 4.1 Concept

```python
# core/risk_manager.compute_size():
state_size_x = STATE_SIZE_MULTIPLIERS.get(current_market_state, 1.0)
size = base_size * state_size_x
```

Suggested defaults (operator adjusts):

| State | Size multiplier |
|---|---|
| TRENDING_NORMAL | 1.0 (baseline) |
| TRENDING_HIGH_VOL | 1.0 (don't compound the implicit larger stops) |
| COMPRESSED | 0.7 (lower edge / chop risk) |
| CHOPPY | 0.5 (significant chop drag) |
| WHIPSAW_HIGH_VOL | 0.0 (effective block — overlaps Stage 2 gate) |
| NEUTRAL | 1.0 |

### 4.2 Why this is sensitive

- Touches the same file the daily/weekly loss caps live in.
- Affects 100% of strategies simultaneously (unlike Stage 2/3 which
  can roll out per-strategy).
- A miscalibrated multiplier can compound losses fast in a wrong-state
  call.

### 4.3 Effort

| Sub-task | Hours |
|---|---|
| Code (risk_manager protected edit) | 1 |
| Tests (size budget invariants must hold) | 3-4 |
| Full backtest sweep across 7 winners | 1-2 days |
| Operator review + sign-off | 1-2 days |
| **Total** | **~1 week** |

## 5. The "if-this-then-that" target end state

Once Stages 2-4 are all in place, each strategy's runtime behavior
becomes:

| Strategy | State | Allowed? | stop_x | target_x | size_x |
|---|---|---|---|---|---|
| bias_momentum | TRENDING_NORMAL | yes | 1.0 | 1.0 | 1.0 |
| bias_momentum | TRENDING_HIGH_VOL | yes | 1.3 | 1.5 | 1.0 |
| bias_momentum | COMPRESSED | no | — | — | — |
| bias_momentum | CHOPPY | no | — | — | — |
| bias_momentum | WHIPSAW | no | — | — | — |
| (repeat for each strategy × each state) | | | | | |

These cells are HYPOTHESES that Stage 1-3 validate, NOT first-pass
defaults. The Stage 1 per-state PF/WR data is the input.

## 6. Open questions for operator

1. **Initial allowed_market_states defaults** — the strawman in §2.2 was
   constructed from intuition (trending-strategy → allow trending; mean-
   reversion → allow choppy). Operator-review pass needed once Stage 1
   has 1-2 weeks of data.
2. **Stage 2 rollout: all strategies at once, or one at a time?** The
   parallel ship is faster but riskier (a bad default can silently
   reduce trade volume across the board). Sequential is safer.
3. **Stage 3 minimum sample.** When is per-state data "enough" to
   promote a per-state stop multiplier from hypothesis to production?
   Proposal: 50 trades in that state for that strategy. Operator picks.
4. **Stage 4 risk_manager edit** — given the protected status, should
   this happen before or after we ship the freeze lift? My read: AFTER.
   The freeze lift (Phase H plan) gates ALL backtest-derived changes;
   `state_size_multiplier` is backtest-derived. So Stage 4 inherits the
   "freeze lift first" constraint regardless of internal Stage ordering.

## 7. Execution gate

**Monday 2026-06-08** for Stage 2 if operator approves this design.

Stages 3 and 4 follow in sequence after Stage 2 is validated. Each
Stage's go/no-go decision rests on the data produced by the previous
Stage. Don't pre-commit to dates beyond Stage 2 — let the Stage 1
data drive it.

## 8. Out of scope (for this design)

- Stage 5+ ideas (multi-bar state hysteresis, regime-transition
  detection, adaptive thresholds). Earlier than that, we need the
  basics working.
- Cross-strategy state coupling (e.g., "if bias_momentum has 3 wins in
  TRENDING_NORMAL today, raschke_baseline's size goes to 1.2x"). Too
  much complexity before the simple per-strategy gating is proven.
- Forecast-driven gating (predicting state transitions). The classifier
  is observational by design; turning it into a forecaster is a separate
  research workstream.
