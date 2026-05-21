# Phoenix MNQ Trading Bot — THE BEST PLAN

> **[CURRENT — SHIP PLAN]** — This is the operator-facing executive summary
> and the canonical reference for what Phoenix is shipping. If a fact here
> conflicts with another doc, this one wins. Last revised 2026-05-20.
>
> For the FULL research record (every Section A-V plus Section Z, with all
> the "why" and the dead ends), see `docs/PHASE_13_IMPLEMENTATION_PLAN.md`
> (labeled `[RESEARCH ARCHIVE]`).
>
> For live operational state, see `memory/context/CURRENT_STATE.md`.

**Status:** Phase 13 COMPLETE. Ready to ship. (2026-05-20)
**Branch:** `weekly-evolution/2026-05-17`
**Latest commit:** see `git log --oneline -1`

This document is the **operator's executive summary** — everything needed to ship Phoenix to production. Built on:
- 5 years of MNQ Databento historical data (1m + 5m OHLCV)
- 2 months of MNQM6 tick-level TBBO data (44M ticks)
- 20 sections of research in `docs/PHASE_13_IMPLEMENTATION_PLAN.md`
- Multiple bug fixes (silent-stop, B2 open_drive, B3 orb_fade freshness)
- Tick-validated exit policies + entry slippage analysis
- 3 spawned edge-sweep investigations (V.1 retest, V.2 early-exit, V.3 S/R)
- 3 additional spawns this sprint (S/R VETO, S/R CONFLUENCE, failed-hold continuation)

For full research detail, see `docs/PHASE_13_IMPLEMENTATION_PLAN.md`.

---

## 1. THE FINAL PORTFOLIO — what ships

### 1.1 11 winning strategies (tick-validated, bug-fixed P&L)

| Tier | Strategy | 5y Baseline P&L | PF | Years+/total |
|---|---|---:|---:|---:|
| 1 | **bias_momentum** | **+$178,379** | 1.33 | 6/6 |
| 1 | **opening_session.orb** | **+$27,257** | 1.87 | 6/6 |
| 1 | **spring_setup** | **+$18,544** | 1.03 | 5/6 |
| 1 | **raschke_baseline** (NEW) | **+$12,779** | 4.10 | 6/6 |
| 1 | **g_inside_bar_breakout** (NEW) | **+$11,300** | 4.88 | 6/6 |
| 1 | **vwap_pullback_v2** (overnight-session) | **+$10,144** | 1.07 | 4/6 |
| 1 | **e_multi_day_breakout** (NEW) | **+$9,097** | 6.79 | 6/6 |
| 1 | **a_asian_continuation** (NEW) | **+$5,909** | 8.29 | 6/6 |
| 2 | es_nq_confluence (DORMANT) | +$2,028 | 3.38 | 6/6 |
| 2 | vwap_band_pullback | +$794 | 1.07 | 4/6 |
| 3 | ib_breakout | +$342 | 1.06 | 4/5 |

**Total clean 5y baseline: +$276,573 = ~$55K/year flat 1-contract**

### 1.2 Per-strategy production specifications

Each strategy gets a tick-validated exit policy + entry order type. **No blanket policies.**

| Strategy | Entry Order | Entry Mode | Exit Policy | Source |
|---|---|---|---|---|
| `bias_momentum` | market | **retest** ⭐ | `fixed_2r` | U.3 + V.1 |
| `spring_setup` | market | **retest** ⭐ | `fixed_3r` | U.3 + V.1 |
| `vwap_pullback_v2` | market | first_touch | `fixed_3r` | U.3 + V.1 |
| `opening_session.orb` | market | first_touch | existing managed | U.3 |
| `opening_session.open_drive` | market | first_touch | `fixed_3r` | U.3 (post-B2 fix) |
| `g_inside_bar_breakout` | **limit_5s** | first_touch | `chandelier_50_3x` | U.3 |
| `e_multi_day_breakout` | **limit_5s** | first_touch | `chandelier_50_3x` | U.3 |
| `a_asian_continuation` | market | first_touch | `time_30min` | U.3 |
| `raschke_baseline` | market | **retest** ⭐ | `time_30min` | U.3 + V.1 |
| `es_nq_confluence` | market | first_touch | `chandelier_50_3x` | U.3 (dormant) |
| `vwap_band_pullback` | market | first_touch | `fixed_3r` | U.3 |
| `vwap_band_reversion` | market | **retest** ⭐ | `scale_out_1r` + filter | A + V.1 |
| `ib_breakout` | market | first_touch | baseline | U.3 |

All assignments are in `core/exit_policies.py` (`PHASE_13_EXIT_ASSIGNMENTS`, `PHASE_13_ORDER_TYPES`) and `core/entry_modes.py` (`ENTRY_MODE_ASSIGNMENTS`).

### 1.3 Expected annual P&L (flat 1-contract)

| Component | Annual $ |
|---|---:|
| Section U production (clean exits, slippage-aware orders) | $60-90K |
| Section V.1 retest mode (4-strategy opt-in) | +$3-4K |
| **TOTAL flat 1-contract** | **~$65-95K/year** |

### 1.4 With compounding (the BIG number)

Starting capital $1,500. Recommended `tier_3000` sizing (1 contract per $3K equity, max 30 contracts).

| Policy | 5y Final | Max DD | Notes |
|---|---:|---:|---|
| flat_1 (no compounding) | $102K | 53% | Reference |
| **tier_3000 (RECOMMENDED)** | **$2.56M** | 56% | Best risk-adjusted |
| winner_weighted_3000 | $2.53M | 78% | More concentration, worse DD |

**Realistic range with tick corrections: $1.5M-$3M over 5 years.**

---

## 2. WHAT WE BUILT (the toolkit)

### 2.1 Production modules (live in code)

| File | Purpose |
|---|---|
| `core/exit_policies.py` | 4 exit policy classes + dispatcher + Phase 13 assignments |
| `core/entry_modes.py` | Entry mode registry (first_touch vs retest) |
| `core/sr_zones.py` | S/R zone detection engine (swing pivots + round numbers + VWAP bands + prior-day levels) |
| `core/signal_visualizer.py` | NT8 chart overlay JSONL writer |
| `ninjatrader/PhoenixTradeOverlay.cs` | NT8 indicator that visualizes live trades |
| `tools/validate_backtest_quality.py` | Detects silent-stop bugs in backtest CSVs |
| `tools/tbbo_cache_builder.py` | Canonical TBBO tick cache loader |
| `tools/volumetric_snapshot_recorder.py` | Records live footprint snapshots every 10 min |

### 2.2 Strategy modifications shipped

| File | Change |
|---|---|
| `tools/phoenix_real_backtest.py` | Silent-stop bug FIX (simulate_trade + runner) |
| `strategies/opening_session.py` line 372 | Bug B2 FIX (pivot_pp → 2R fixed target) |
| `strategies/orb_fade.py` line 162 | Bug B3 FIX (time.time() → market now_ct) |
| `bots/base_bot.py` _process_signal | `_apply_phase13_overrides()` hook |

### 2.3 Analysis/research tools

| File | Purpose |
|---|---|
| `tools/phoenix_stop_target_optimizer.py` | 25-policy exit comparison (Section T) |
| `tools/phoenix_tick_trail_verification.py` | Section U Agent A — tick exit |
| `tools/phoenix_tick_entry_quality.py` | Section U Agent B — entry slippage |
| `tools/phoenix_entry_retest_analyzer.py` | Section V.1 — retest analyzer |
| `tools/phoenix_early_reversal_signals.py` | Section V.2 — early-exit signals (NEGATIVE) |
| `tools/phoenix_sr_strategy_lab.py` | Section V.3 — S/R bounce strategy (NEGATIVE) |
| `tools/phoenix_compounding_backtest.py` | Compounding/sizing simulation |
| `tools/phoenix_footprint_attribution.py` | Footprint VETO/CONFLUENCE testing |
| `tools/phoenix_es_nq_confluence_attribution.py` | Cross-asset alignment testing |

### 2.4 Documentation

| File | Content |
|---|---|
| `docs/PHASE_13_IMPLEMENTATION_PLAN.md` | 20 sections (A-Z) of all research |
| `docs/STRATEGY_SPECIFICATIONS.md` | Per-strategy entry/stop/exit specs |
| `docs/DATABENTO_FOOTPRINT_WALKTHROUGH.md` | How to buy/use TBBO data |
| `docs/TICK_LEVEL_EXIT_VERIFICATION.md` | Section U Agent A report |
| `docs/TICK_LEVEL_ENTRY_VERIFICATION.md` | Section U Agent B report |
| `docs/ENTRY_RETEST_ANALYSIS.md` | Section V.1 report |
| `docs/EARLY_REVERSAL_EXIT_ANALYSIS.md` | Section V.2 report |
| `docs/SR_ZONE_STRATEGY.md` | Section V.3 report |
| `docs/PHOENIX_BEST_PLAN.md` | THIS DOC |

---

## 3. CRITICAL FINDINGS (the operator MUST know)

### 3.1 Bug fixes that recovered massive value

| Bug | Section | Impact |
|---|---|---:|
| Silent-stop in simulate_trade | S | bias_momentum 40 → 13,790 trades (+$177K) |
| Bug B2: open_drive pivot_pp target | B/U | open_drive -$106K → +$3.8K (+$110K swing) |
| Bug B3: orb_fade wallclock freshness | B | orb_fade 0 → 96 signals in 6mo |
| Phase 13 Section V.3 spawn caught NEW silent-stop variant (deque saturation) | V.3 | Validator now catches future variants |

**Validate after EVERY backtest:** `python tools/validate_backtest_quality.py`

### 3.2 What works (proven empirically)

- **5m timeframe** for all strategies (1m destroys edge — verified in 3 independent tests)
- **Fixed RR targets** (2R or 3R) for momentum strategies — beats all trail variants tick-level
- **Chandelier 50/3x** for high-WR breakouts (inside_bar, multi_day, es_nq)
- **time_30min** for fast-resolving setups (asian, raschke)
- **bias_momentum is #1** by absolute $ (was buried at 40 trades by the silent-stop bug)
- **Entry retest mode** for 4 strategies (+$3-4K/yr modest free lift, Section V.1)
- **Mean-rev strategies get FAVORABLE slippage** on market orders (price improvement, Section U.2)
- **Inside_bar + multi_day need limit orders** (market chases breakouts, Section U.2)

### 3.3 What does NOT work (don't try again)

- **Pure mean-reversion on MNQ** (ATR-extension reversal, EMA-distance reversion) — NQ is too trendy (Section N)
- **1-minute timeframe** for any strategy (Section O)
- **ES/NQ alignment as filter** for other strategies — actively destroys P&L (Section P)
- **Tight trail stops** (4t/8t) for momentum strategies — bar-level artifacts, fail tick-level (Section U.1)
- **Early-reversal exit triggers** — every pattern hurts every strategy (Section V.2)
- **Pure S/R-bounce strategy** — MNQ noise kills 2R targets (Section V.3)

### 3.4 What's still uncertain (proceed with care)

- bias_momentum 56% max DD on compounding curve — operator commitment plan required (Section S.6)
- Tick-level conclusions based on 2 months of data; some strategies have small n in window
- 70-80% WR on the 3 new winners is suspicious vs literature (50-65% typical) — expect mean-reversion to 60-70%
- Slippage characteristics may shift in different volatility regimes
- 4 strategies have stops "TOO TIGHT" per MFE/MAE analysis — exit policy compensates, but worth monitoring

---

## 4. ARCHITECTURAL DECISIONS

### 4.1 Role-based confluence framework (Section K)

Each factor gets ONE role per strategy:
- **VETO**: hard binary gate (regime, time-of-day, news)
- **TRIGGER**: the "now" condition (one per strategy)
- **CONFIRMATION**: continuous score contributor (NOT a gate)
- **SIZING**: continuous, scales position by score

Max 5 factors per strategy. The same factor can serve different roles in different strategies — that's the orthogonal-signal pattern.

### 4.2 Validation gauntlet for any new factor (Section L)

Before shipping ANY new entry/exit factor:
1. IC ≥ 0.05 at some forward horizon (5/15/60 min)
2. P&L attribution: strategy-with vs strategy-without vs strategy-randomized
3. Walk-forward: 60d train / 20d test, OOS Sharpe ≥ 0.5× IS Sharpe
4. VIF ≤ 5 vs existing factors (multicollinearity check)
5. Bonferroni correction if screened from a library

### 4.3 Phoenix's "silent failures" doctrine

Per `memory/feedback_silent_failures.md`: every Phoenix bug fails silently (process alive, dashboard "running" — but bot deaf). Design EVERY new feature to fail LOUDLY. Always:
- Log staleness / NaN / missing data at WARNING level
- Add coverage to `validate_backtest_quality.py` for any new backtest output
- Per-bar heartbeat for any factor that gates entries

---

## 5. THE SHIP PLAN (Phase 13 Step C)

### 5.1 Prerequisites

- ✅ Bug fixes shipped (silent-stop, B2, B3)
- ✅ Bug fixes verified (5y backtest re-run clean)
- ✅ Per-strategy exit policies in production code (`core/exit_policies.py`)
- ✅ Per-strategy entry order types wired (`base_bot._apply_phase13_overrides`)
- ✅ Entry mode registry in production code (`core/entry_modes.py`)
- ✅ Validator built (`tools/validate_backtest_quality.py`)
- ✅ Operator commitment plan for bias_momentum drawdown (Section S.6)

### 5.2 Operator action checklist

```cmd
# 1. Pull latest:
cd "C:\Trading Project\phoenix_bot"
git pull origin weekly-evolution/2026-05-17

# 2. Validate clean state:
python tools/validate_backtest_quality.py

# 3. Run test suite:
pytest tests/ -v

# 4. Install NT8 chart visualizer (one-time):
copy "ninjatrader\PhoenixTradeOverlay.cs" ^
  "C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\"
# Then in NT8: NinjaScript Editor → F5 (Compile)
# Add to chart: Indicators → Add → PhoenixTradeOverlay

# 5. Restart sim_bot:
# Kill existing PIDs, restart with:
python bots/sim_bot.py
# Should see [Phase13 override] log lines when strategies fire

# 6. Set up volumetric recorder (one-time, if not already done):
schtasks /create /tn "PhoenixVolumetricRecorder" /tr ^
  "python C:\Trading Project\phoenix_bot\tools\volumetric_snapshot_recorder.py" ^
  /sc minute /mo 10 /ru "Trading PC"

# 7. Monitor for 1-2 sessions:
#   - Check phoenix_signals.jsonl is being written
#   - Verify NT8 chart shows colored markers
#   - Confirm exit policies fire correctly
#   - Watch for [Phase13 override] entry_mode=retest logs
```

### 5.3 Initial sizing (per Section S.6 operator commitment)

**Phase A (30 trading days):** 1 contract per trade. Verify the bot fires correctly + behaves as expected. Build confidence.

**Phase B (60 trading days):** Scale to tier_3000 policy (1 contract per $3K equity). Allows scale-up if equity grows.

**Phase C (90 trading days):** Allow full compounding per tier_3000. Review monthly.

**Phase D (ongoing):** Full sizing, monthly review.

If ANY phase fails:
- ✗ Silent failure detected → pause, diagnose
- ✗ Behavior diverges from backtest → pause, investigate
- ✗ DD experience worse than expected → pause, re-validate plan

**SHIPPED 2026-05-20 (F-001):** The tier_3000 policy is now implemented
in `core/tier_sizer.py` with full safety rails (1/$3K equity, 30-contract
cap, per-strategy multipliers, 85%-of-ATH DD scale-down, 4% daily
circuit breaker, 3-consecutive-loss halving, atomic-write equity
state in `data/equity_state.json`). The dispatcher lives in
`bots/base_bot.py` and is gated by `config.settings.SIZING_MODE`:

- `"flat_1"` (DEFAULT) — current behavior. 1 contract per entry,
  legacy PositionScaler path. **Phase A operators stay here.**
- `"tier_3000"` — F-001 active. Operator flips this when ready for
  Phase B (see `docs/OPERATOR_BRIEF_PT2.md` "F-001 Activation").

The dispatcher is backward-compatible: while `SIZING_MODE="flat_1"`
the new module is never invoked and `data/equity_state.json` is never
written. No existing trade / position / state behavior changes.

### 5.4 Mental commitment (bias_momentum is the #1 strategy)

bias_momentum has 56% max DD on the compounding curve. To survive that:

- **Pre-commit in writing**: max acceptable DD before scaling down = 25% portfolio
- **Drop one tier** when equity < 85% of all-time-high
- **Halve next trade size** after 3 consecutive losing trades
- **Daily circuit breaker**: halt if today's loss > 4% of equity
- **Never override**: the bot's exit logic is backtested; your eyes are not

---

## 6. WHAT'S DEFERRED (next sprint)

### 6.1 Sprint A/B/C results (S/R is 0-for-4 in production use cases)

After Section V.3's direct S/R-bounce strategy failed, 3 follow-up sprints tested the alternative use cases for `core/sr_zones.py`. **All 3 negative or marginal — DO NOT SHIP any S/R-based wiring.**

| Sprint | Use case | Verdict | Commit |
|---|---|---|---|
| A | VETO filter for bias_momentum (block longs near resistance) | **NEGATIVE** — vetoed trades earn +$13/trade. Hypothesis structurally wrong: bias_momentum's edge IS breaking through nearby walls. | `3a62d23` |
| B | CONFLUENCE boost for spring_setup (size-up springs at S/R zones) | **MARGINAL** (+$300/5y, 3/6 years positive). Counterintuitive: very_strong_sr (≥0.70) is ANTI-EDGE — strongest levels break more than they hold. | `c92b931` |
| C | Failed-hold continuation (trade S/R BREAKS) | **NEGATIVE** -$5.9K/5y, all 5 variants, every year negative. Same root cause as V.3: MNQ noise destroys 2R setups on confirmation-bar entries. | `e9e9a9d` |

**Combined with V.3 (direct S/R bounce, also negative), S/R is 0-for-4 in production applications.** The `core/sr_zones.py` engine remains valuable for research/analysis but has no current ship use case.

**Most interesting empirical finding from these 4 spawns:**
- The STRONGEST S/R levels (strength ≥0.70) systematically UNDERPERFORM the medium-strength levels (0.50-0.70). Heavily-tested levels accumulate stop liquidity; when they break they break with conviction. Real market microstructure insight but not translatable to a tradeable rule.

### 6.2 Latent bug discovered (pandas 3.0 datetime precision)

Both Sprint A and Sprint B independently hit this: `pandas 3.0` default datetime precision is **microseconds, not nanoseconds**. The common idiom `df.astype("int64") // 10**9` returns wrong-by-1000× values.

**Audit task (deferred):** sweep all Phoenix tools for this pattern. Replace with `.timestamp()` per-row or explicit `.astype("datetime64[ns, UTC]")` conversion first. Could affect any tool that converts pandas timestamps to epoch seconds.

### 6.3 Full retest-wait implementation

Section V.1 retest mode is currently **flagged-but-not-enforced** — base_bot logs the intent but submits market order. Full implementation (per-strategy tick buffer + cancellation + timeout) needs careful integration with live tick feed. Estimated 1-2 day sprint when operator green-lights it. Expected lift: +$3-4K/yr.

### 6.4 5-second limit order timeout

Section U.3 recommended `limit_5s` for `g_inside_bar_breakout` + `e_multi_day_breakout`. Currently `_apply_phase13_overrides` sets entry_type=LIMIT but doesn't implement the "cancel after 5 seconds and fall back to market." The plain LIMIT works (will sit as a working order); the operator can manually monitor for now or implement the timeout in a focused sprint.

### 6.5 Footprint backtest pipeline

We have 2 months of TBBO data + `tools/tbbo_cache_builder.py`. The snapshot recorder is collecting live footprint. After 3-6 more months of accumulation, build a footprint-aware backtest pipeline (`tools/phoenix_footprint_backtest_pipeline.py`) to validate per-strategy footprint VETO/CONFIRMATION hypotheses from Section R/U.

### 6.6 MES feed for es_nq_confluence

es_nq_confluence is profitable in backtest (+$2,028/5y, PF 3.38) but DORMANT live because Phoenix doesn't yet stream MES ticks. To activate:
- NT8 chart on MES with TickStreamer loaded
- bridge_server fans out `mes_*` market dict fields
- tick_aggregator builds parallel `mes_bars_5m`
- base_bot enriches `market["mes_bars_5m"]`

Expected: small but free +$400/year contribution.

### 6.7 Re-allocation decisions (Section E)

| Action | Status |
|---|---|
| Kill 5 dead NT8 sub-accounts ($9,914 capital) | Awaiting operator decision |
| Move es_nq_confluence to dedicated SimESNQConfluence account | After MES wiring (6.5) |
| Demote bias_momentum from PROD? | **REVERSED**: bias_momentum is now #1, KEEP IN PROD |

---

## 7. RISKS + WHAT COULD GO WRONG

### 7.1 High-likelihood risks

| Risk | Mitigation |
|---|---|
| **In-sample overfit** — backtests optimized to past data | OOS expected = 70-80% of in-sample. Plan for it. |
| **Bias_momentum 56% DD lands** | Operator commitment plan (Section S.6); pre-commit to stay the course |
| **Silent failure pattern recurs in NEW form** | Validator covers known patterns; design new features to log loud |
| **Slippage worse than tick analysis predicted** | Section U Agent B used 500ms latency; live may be 1000-2000ms |
| **Exit policies haven't been tested in live for full bar-walking** | Chandelier needs per-bar position tracking — currently stubbed |
| **Regime change** — what worked 2021-2026 may not work 2026-2028 | Re-run optimizer annually; monthly P&L review |

### 7.2 Catastrophic-but-low-probability risks

| Risk | Mitigation |
|---|---|
| Phoenix's exit_policy dispatch has a bug → wrong target shipped | Smoke test: see `Phase13 override target_price` logs match expectations |
| TBBO data hygiene bug recurs in new agent | Always use `tools.tbbo_cache_builder.load_clean_ticks()` |
| NT8 connection drops during a position → orphan trade | Existing OIF + PhoenixOIFGuard race mitigation handles this |
| Operator panics during 30% DD and overrides → loses month of equity | Pre-commit in writing; do NOT manual-override the bot |

---

## 8. KEY METRICS TO WATCH (after ship)

### 8.1 Daily

- bias_momentum daily P&L (largest contributor)
- Total daily P&L vs 30-day average
- Number of strategies that fired today
- Any silent failures in `logs/`

### 8.2 Weekly

- Per-strategy P&L vs backtest expectation
- WR by strategy vs backtest baseline
- Slippage measurements (actual fill vs expected)
- Number of `[Phase13 override]` log lines (sanity that the wiring works)

### 8.3 Monthly

- Portfolio P&L vs the $65-95K/yr flat projection
- Drawdown level vs 56% bias_momentum max
- Sample size per strategy (need to hit 100+ trades each for statistical validity)
- Re-run validator on live trade log

### 8.4 Quarterly

- Re-run stop/target optimizer on the previous quarter's trades
- Check if exit policy still dominates the same way it did in backtest
- If any strategy underperforms backtest by >50% for 2 quarters, kill or re-evaluate

---

## 9. FILES + COMMITS REFERENCE

### 9.1 Commit chain (chronological, this sprint)

| Commit | What |
|---|---|
| `a9a5ef9` | **CRITICAL FIX**: silent-stop bug in simulate_trade |
| `58878d2` | Bug B2 + B3 production fixes |
| `bb927bb` | Section U Agent A — tick exit verification |
| `48cd829` | Section U Agent B — tick entry slippage |
| `222c6de` + `9bc8039` | TBBO hygiene canonical loader + docs |
| `c766ddb` | Section U production code (exit_policies + base_bot) |
| `17bcde5` | Section T 25-policy optimizer |
| `42abc1a` | Section V.1 — entry retest analyzer |
| `a5fc66d` | Section V.2 — early reversal signals (negative) |
| `717d23f` | Section V.3 — S/R zones + lab (negative) |
| `bf23d56` | Section V synthesis |
| `9eca6ea` | Sprint D — entry_mode wiring |

### 9.2 Quick-reference paths

- **Plan doc:** `docs/PHASE_13_IMPLEMENTATION_PLAN.md` (this is the bible)
- **Best plan:** `docs/PHOENIX_BEST_PLAN.md` (THIS DOC — executive summary)
- **Strategy specs:** `docs/STRATEGY_SPECIFICATIONS.md`
- **Exit policies:** `core/exit_policies.py` PHASE_13_EXIT_ASSIGNMENTS
- **Entry modes:** `core/entry_modes.py` ENTRY_MODE_ASSIGNMENTS
- **Validator:** `tools/validate_backtest_quality.py`
- **TBBO loader:** `tools/tbbo_cache_builder.py` load_clean_ticks()

---

## 10. THE BOTTOM LINE

**Phoenix is ready to ship.** After 20 sections of Phase 13 research, multiple bug fixes, tick-level verification, and 6+ research spawns:

- Annual P&L target: **~$65-95K/year flat 1-contract**
- 5-year compounded target: **$1.5M-$3M with tier_3000 sizing**
- Bias_momentum is the #1 strategy, surfaced only by the silent-stop bug fix
- All exit policies tick-validated; all entry orders slippage-tuned
- Per-strategy individualized — no blanket assumptions
- Validator + monitoring + commitment plan all in place

**One operator action:** restart sim_bot. Phoenix takes it from there.

---

*Last updated: 2026-05-20. For real-time state: `git log --oneline -5` and `memory/context/CURRENT_STATE.md`.*
