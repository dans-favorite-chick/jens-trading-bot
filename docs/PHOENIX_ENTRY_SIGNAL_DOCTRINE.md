# Phoenix Entry Signal Doctrine

**Version:** 1.0 — 2026-05-22
**Sources:** Agents a16cf0ef (per-strategy 5y voter IC), a085e4d6 (live-data + TBBO), ab84603a (microstructure forensic), a9e3773f (per-strategy v2), ac705046 (archival/window audit).
**Status:** OPERATIONAL — applies to every signal Phoenix emits from pt8 forward.

This doc answers the operator's central question:

> "What confluences give us the highest success rate? What is your recommendation
> for the bot's entry signals?"

It's the synthesis of 4 sub-agent deep dives. Treat it as the canonical reference
for what the bot SHOULD use as entry signals and what it should ignore.

---

## Headline finding — the live-tape microstructure score is anti-predictive

The `msu_score` (microstructure_filter.py) had Information Coefficient = **−0.152**
across 12,000+ live trades. Higher score → lower win rate. That's not noise —
it's a 16-sigma signal pointing the wrong way.

The forensic in pt8 (`docs/OPERATOR_BRIEF_PT8_ADDENDUM.md` §1) traced it to the
formula rewarding the canonical adverse-selection trap: tight spread + bid-heavy
DOM under a LONG signal + CVD already positive + price rising into entry. That
pattern is exactly what informed sellers offload INTO. The retail trader sees
"perfect tape" and chases the top of the move.

**Lesson generalized:** when designing entry confluences, the right question is
not *"does this voter agree with my direction RIGHT NOW?"* — it's *"does this
voter predict the NEXT 30-60 minutes of price action?"* Most live-tape voters
(DOM imbalance, session CVD sign, momentary EMA-relation, 1m bias) describe
where price *just was*, not where it's about to go. The truly predictive voters
are **structural** (higher-timeframe bias, regime classification, prior-day
levels) and **inter-market** (NQ vs ES relative strength).

This single principle determines the tier-1 voter list below.

---

## TIER 1 — the voters that actually predict (USE these)

These are the only voters with statistically significant positive IC across the
5-year MNQ Databento backtest (a16cf0ef research, 36k+ trades). Every one of
them is now wired as a hard gate in the strategies it helps.

### 1.1 `tf_60m` — higher-timeframe bias agreement
- **IC:** +0.18 average across 6 RTH directional strategies
- **Mechanism:** 60-minute bar EMA stack alignment with trade direction. Slow
  enough to capture true intraday trend, fast enough not to lag.
- **Lift when used as a hard gate:** +13-17 percentage points of WR.
- **Currently gates:** bias_momentum, spring_setup, vwap_pullback_v2,
  raschke_baseline, opening_session.orb, opening_session.open_drive (pt6).
- **Source field:** `market["tf_bias"]["60m"]` or `market["tf_bias_60m"]`,
  value in `{"BULL", "BEAR", "NEUTRAL"}`.
- **Helper:** `core.confluence_gates.tf60m_es_gate()`.

### 1.2 `es_correlation` — NQ vs ES relative strength sign
- **IC:** +0.13 average across the same 6 strategies
- **Mechanism:** If NQ is outperforming ES on a 5m relative basis, NQ-specific
  flow is buying tech. If NQ is underperforming, broader risk is selling tech
  harder than the index baseline. Either way: a strong directional vote
  independent of NQ's own momentum (which `tf_60m` already captures).
- **Lift when paired with `tf_60m`:** WR 38-47% → 51-58% baseline lift; the
  12,039 trades (33% of total volume) in the both-agree subset produce 83%
  of total 5y P&L = +$257K.
- **Currently gates:** same 6 strategies as `tf_60m`, via the combined helper.
- **Source field:** `market["es_nq_rs"]` (sign matters — positive = NQ
  outperforming = bullish bias). Populated every 2 min by `core/market_intel.py`
  from Alpaca + yfinance polls, so it's available even when NT8 has no MES feed.
- **Helper:** same `tf60m_es_gate()`.

### 1.3 `tf_5m` — 5-minute structure agreement (breakout-specific)
- **IC:** +0.21 on breakout strategies (5m close beyond prior extreme).
- **Mechanism:** 5m bias is the right horizon for "is this 5m breakout in the
  direction of the immediately preceding 5m structure?"
- **Lift on `e_multi_day_breakout`:** WR **77.8% → 95.97%** (n=273) — the
  biggest single-gate lift in the entire research. Same gate now also on
  `g_inside_bar_breakout` (pt8 — structurally identical setup, expected to
  show comparable lift).
- **Currently gates:** e_multi_day_breakout, g_inside_bar_breakout.
- **Helper:** `core.confluence_gates.tf5m_es_gate()` (also requires
  `es_correlation` agreement).

### 1.4 Regime classifier — `regime_veto` on adverse regimes
- **IC:** strongly bimodal — kills certain strategies in certain regimes.
- **Mechanism:** Phoenix's 8-regime classifier (`core/session_manager.py`) is
  trained on time-of-day + volatility + opening type. Some strategy-regime
  pairs have catastrophic per-trade drag in backtest:
  - `vwap_band_pullback` × `OPEN_MOMENTUM`: −$35.95/trade (most extreme drag
    in the entire research)
  - `vwap_band_reversion` × `OPEN_MOMENTUM`: same family (pt8 added)
  - `raschke_baseline` × `OPEN_MOMENTUM`: −$7.43/trade
  - `bias_momentum` × `OVERNIGHT_RANGE`: −$2.55/trade × 7,380 trades = −$18.8K
  - `opening_session.orb` × `AFTERNOON_CHOP`: −$9.40/trade
  - `opening_session.orb` × `LATE_AFTERNOON`: −$3.04/trade
- **Currently gates:** all 6 strategy-regime pairs above (pt6/pt8).
- **Helper:** `core.confluence_gates.regime_veto()`.

### 1.5 `bar_delta` — per-bar order flow direction
- **IC:** +0.08 on momentum strategies (the ONE CVD-family voter that survives).
- **Mechanism:** Delta of the CURRENT (or most recent closed) bar, not session-
  cumulative. Captures "did this bar's trade flow agree with the direction?"
  without being lagged by the day's earlier flow.
- **Currently used as:** scoring contributor in bias_momentum (+15/−10), not a
  hard gate. Stays scored after the pt8 noise-voter cleanup precisely because
  it has IC > 0.
- **Source field:** `market["bar_delta"]`.

### 1.6 Strategy-specific structural triggers
Each strategy has a primary structural trigger that is the *entry event itself*,
not a confluence. These are TIER 1 by construction:
- ORB: 5m close beyond 15m opening range, OR-size in [11, 110] pts
- e_multi_day_breakout: 5m close beyond prior 3-session H/L
- g_inside_bar_breakout: 5m close beyond inside-bar high/low
- spring_setup: wick ≥ 6t with reversal close
- raschke_baseline: pullback to EMA21 + break of swing bar high/low
- vwap_pullback_v2: VWAP touch + bounce candle (overnight 17:00-04:59 CT only)
- a_asian_continuation: 5m close beyond overnight range + 0.5×ATR
- vwap_band_reversion: 2.1σ band wick + reversal close inside

A signal does not exist without one of these. Confluences gate the signal
*after* it triggers; they don't generate signals.

---

## TIER 2 — situationally useful (USE selectively)

These voters have small positive IC in specific contexts. Currently used as
soft confluences (scoring weight, not hard gates) where they help.

### 2.1 `tick_rate_60s` — flow concentration
- **IC:** strong on `high_precision_only` only (the ONE strategy that needs
  active flow to read its microstructure pattern reliably).
- **Recommendation:** add as a hard gate (`tick_rate_60s ≥ 600`) on
  high_precision_only when re-enabling that strategy (currently disabled,
  retired 2026-05-13). Tracked as **B-034**.
- **NOT a universal gate** — most strategies are time-frame structural and
  don't care about tick density.

### 2.2 Opening-type classification
- **IC:** moderate on `opening_session` subs only.
- **Mechanism:** Phoenix's `classify_opening_type` returns one of OPEN_DRIVE,
  OPEN_TEST_DRIVE, OPEN_AUCTION_IN, OPEN_AUCTION_OUT based on the first 5-min
  bar shape. Each sub-strategy is dispatched only when its matching type is
  classified.
- **Currently used as:** dispatch gate (open_drive sub only fires on OPEN_DRIVE
  classification). Working correctly.

### 2.3 `day_type` (TREND vs BALANCED vs VOLATILE)
- **IC:** strong on mean-reversion strategies (negative on TREND days).
- **Mechanism:** classified at session level from prior-day H/L + IB shape.
- **Currently used as:** hard gate on `vwap_band_reversion` (`if day_type ==
  "TREND": return None`). Also referenced in `vwap_band_pullback`'s logic.

### 2.4 Volume confirmation (≥ Xx 1m average)
- **IC:** weak positive on breakout strategies only.
- **Mechanism:** "did the entry bar have above-average volume?" cuts
  the lowest-conviction breakouts.
- **Currently used as:** hard gate on `opening_session.open_drive` (≥1.2×),
  `opening_session.orb` (CVD-aligned proxy), `e_multi_day_breakout`. Working.

---

## TIER 3 — NOISE or ANTI-PREDICTIVE (DO NOT USE)

**These voters either don't predict (|IC| < 0.02) or actively predict the
wrong direction. Pt8 demoted the worst offenders from scoring weight to
log-only labels. Do not wire these back into any gate or scoring formula
without a fresh backtest showing IC > +0.05 over a 1,000-trade window.**

### 3.1 `msu_score` (live microstructure composite) — ANTI-PREDICTIVE
- **IC:** **−0.152** (highest anti-edge magnitude in the entire research)
- **Mechanism:** rewards tight spread + DOM-supporting-direction + CVD-confirming
  + price-chasing-into-signal + dom_signal-aligned. Each of these is an adverse-
  selection signal at the moment of entry (top-of-move tape).
- **pt8 action:** inverted via `INVERT_PER_B031 = True` flag in
  `core/microstructure_filter.py`. Now PENALIZES adverse-selection conditions.
  Still advisory-only (no strategy gates on it). Watch IC over 1,000 live
  trades; expected to flip to ~+0.15. Until then, **do not gate on it**.

### 3.2 `VWAP_relation` (price vs session VWAP, raw boolean)
- **IC:** **−0.04** (~−$1.06/trade)
- **Why:** "price is above VWAP" describes where price already moved to. By
  the time the boolean flips, the edge has been spent — chasing into the
  extension is what the boolean rewards.
- **pt8 action:** demoted from `+20` momentum_score weight to log-only label
  in `bias_momentum`. Other strategies that use it as a hard gate (VWAP reclaim
  on spring_setup, VWAP bounce on vwap_pullback_v2) keep it because it's their
  primary structural trigger, not a generic confluence vote.

### 3.3 `cvd_sign` (session-cumulative CVD direction)
- **IC:** **+0.003** (functionally zero, ~−$0.14/trade)
- **Why:** session CVD can stay negative all day on a strongly bullish day if
  the open had heavy selling. It's not a real-time directional signal.
- **pt8 action:** demoted from `±10/−5` weight to log-only label in
  `bias_momentum`. Per-bar `bar_delta` (Tier 1.5) is what works.

### 3.4 `EMA9_relation` (price vs EMA9, raw boolean)
- **IC:** **−0.055**
- **Why:** same chasing-into-extension problem as VWAP_relation. EMA9 lags
  price by definition; "price is above EMA9" means "we already moved up some
  N ticks." Rewards that boolean = rewards late entries.
- **Status:** still scored as a +25 weight in `bias_momentum` (lines 363-376
  full-stack alignment). Pt8 did not touch this — flagged for next sprint.
  Less urgent than VWAP_relation because it's bundled with the EMA9 vs EMA21
  spread (which IS a real trend signal); decoupling is non-trivial.

### 3.5 `tf_1m` (1-minute bias direction)
- **IC:** **−0.012** (noise; 1m bias on MNQ flips every 2-3 bars)
- **Why:** MNQ on 1m is dominated by HFT noise, not directional flow. 1m bias
  is a measurement of the last 2-3 ticks dressed up as a signal.
- **Status:** B-033 — used in `min_tf_votes` tallies on several strategies.
  Removing it requires touching the upstream `tf_bias` aggregator
  (`core/tf_bias.py`) which propagates to all consumers. Tracked for next
  sprint.

### 3.6 `orb_direction` (the direction of the first 15-min OR break)
- **IC:** **+0.018** (noise)
- **Why:** once the breakout has fired the trade, the direction of the FIRST
  break carries no additional information about continuation vs reversal.
- **Status:** correctly used inside `opening_session.orb` as a one-trade-per-
  day directional lock, NOT as a confluence vote. That use is fine. Should be
  removed from any other strategy's vote tally (B-033 scope).

### 3.7 `dom_imbalance` on spring_setup — POSSIBLY ANTI-PREDICTIVE
- **IC:** suspected inverted on this specific strategy.
- **Why:** spring_setup buys long wicks that swept liquidity. "Heavy bid stack"
  at the exact moment of the spring wick is — by definition — the side that
  just got swept. Acting on it as confluence means buying right when the
  absorbers have been hit.
- **Status:** B-035 — instrument first (2-week live A/B) before flipping the
  sign. Don't yolo it.

---

## Per-strategy recommended confluence stack (current state, post-pt8)

| Strategy | Primary trigger | Tier-1 gates wired | Notes |
|---|---|---|---|
| `bias_momentum` | EMA stack + momentum_score ≥ 80 (regime-tuned) | `regime_veto(OVERNIGHT_RANGE)`, `tf60m_es_gate` (non-TREND days) | Noise voters demoted pt8 |
| `spring_setup` | Wick ≥ 6t + reversal candle close | `tf60m_es_gate` | B-035 DOM sign instrumentation pending |
| `ib_breakout` | 1m close beyond IB extremes | CVD-confirm | No tf60m gate yet — small-n discipline (B-036) |
| `opening_session.orb` | 5m close beyond 15m OR | `regime_veto(AFTERNOON_CHOP, LATE_AFTERNOON)`, `tf60m_es_gate`, CVD-aligned | All 4 gates active |
| `opening_session.open_drive` | 1m volume spike + break of 5m OR | `tf60m_es_gate` | 2R fixed target post-B2 fix |
| `vwap_band_pullback` | 1σ/2σ band touch + reversal + RSI(2) extreme | `regime_veto(OPEN_MOMENTUM)`, `min_tf_votes ≥ 2` | OPEN_MOMENTUM is the killer |
| `vwap_band_reversion` | 2.1σ wick + reversal close | `regime_veto(OPEN_MOMENTUM)` + 08:30-09:30 block + `day_type != TREND` | pt8 added regime veto + fixed wallclock bug |
| `vwap_pullback_v2` | VWAP touch + bounce candle (overnight only) | `tf60m_es_gate`, `min_tf_votes ≥ 2` | 17:00-04:59 CT window enforced |
| `es_nq_confluence` | NQ-ES boost ≥ 25 + corr ≥ 0.85 | (self-contained) | DORMANT — needs MES bar feed |
| `a_asian_continuation` | 5m close beyond ON range + 0.5×ATR | (window 03:00-08:00 CT is the gate) | No tf60m gate — needs overnight-specific backtest |
| `e_multi_day_breakout` | 5m close beyond prior 3-session H/L | `tf5m_es_gate` | The +18pp WR gate |
| `g_inside_bar_breakout` | 5m close beyond inside-bar H/L | `tf5m_es_gate` (pt8) | New — mirrors e_multi_day_breakout |
| `raschke_baseline` | EMA21 pullback + break of swing high/low | `regime_veto(OPEN_MOMENTUM)`, `tf60m_es_gate` | Pure trend-pullback |

---

## The four entry-confidence questions (operator mental model)

Before any new strategy or any new gate is wired, the answer must be YES to
all four:

1. **Does the voter measure where price is GOING, not where it just was?**
   - YES: tf_60m, es_correlation, regime classification, prior-day H/L
   - NO: dom_supports_direction, cvd_sign, VWAP_relation, EMA9_relation, msu_score
2. **Does the IC test on 1,000+ trades show |IC| ≥ 0.10 with consistent sign
   across years?**
   - YES → can be wired as a hard gate
   - 0.05 ≤ |IC| < 0.10 → can be a scoring contributor (not hard gate)
   - |IC| < 0.05 → log-only label, no formula weight
3. **Does the voter graceful-degrade when its data is unavailable?**
   - All Tier 1 gates pass — they return `(True, None)` on missing data so a
     cold-start or dormant-feed bot doesn't perma-skip.
4. **Is the voter's failure mode safe?**
   - Hard gates that reject signals on disagreement are safe (no trade =
     no loss). Voters that boost confidence are dangerous (over-sized
     loser = blown daily limit).

The pt8 sweep ensures every Tier 1 gate passes all four. Tier 3 voters fail
question 1 by construction; pt8 demoted them out of any formula that gates
or sizes a trade.

---

## What to do NEXT (sprint roadmap)

These are tracked in `docs/BUGS_AND_TODOS.md`; ranked here by expected $ lift:

1. **B-032 NT8-side reload** (this week, operator action — see
   `docs/OPERATOR_BRIEF_PT8_ADDENDUM.md` §5). Unblocks live volumetric
   capture, which is prerequisite to re-enabling `footprint_cvd_reversal`
   and validating the inverted msu_score in live A/B.
2. **B-031 live A/B confirmation** (1,000 trades over ~6 weeks). If IC flips
   to +0.15 as predicted, msu_score becomes a Tier 1 candidate.
3. **B-033 tf_1m removal from tallies** (1 day of work). Touch
   `core/tf_bias.py` to drop tf_1m from `tf_votes_bullish/bearish` aggregation.
4. **B-035 spring_setup DOM-sign instrumentation** (2 weeks live A/B).
5. **B-034 high_precision_only tick_rate gate** (only if re-enabling that
   strategy — currently retired).
6. **MES bridge feed** (D4 infrastructure work) — unblocks `es_nq_confluence`
   strategy. Tracked separately as Phase 13 §D4 / §6.6.
7. **Physical archive of 11 legacy strategy files** (per agent ac705046).
   Requires updating 12+ test files. Cosmetic cleanup, not operational.

---

## Anti-patterns — things NOT to do

- **Do not wire `msu_score` as a hard gate** until live A/B confirms +IC.
- **Do not add a new "DOM agrees with direction" voter.** That entire class
  of signal is the adverse-selection trap; the IC -0.152 finding is the proof.
- **Do not add new voters from rule-of-thumb without an IC backtest.** Every
  voter that survived the pt5-pt8 cleanup has explicit 5y IC evidence.
- **Do not score the same information twice.** EMA stack alignment and
  EMA9_relation are the same information; counting both inflates noise.
  Pick one (the spread, not the boolean), score it once.
- **Do not gate small-n strategies** (n < 100 over 5y). Wilson 95% CI on the
  voter delta is wider than the delta itself — you're fitting to noise.
  Tracked as B-036.

---

## Citations

- `docs/CONFLUENCE_VOTER_RESEARCH_2026-05-21.md` — bias_momentum 5y IC table
- `docs/OPERATOR_BRIEF_PT5_PT7.md` — pt5/pt6/pt7 changelog
- `docs/OPERATOR_BRIEF_PT8_ADDENDUM.md` — pt8 changelog (this sprint)
- `docs/BUGS_AND_TODOS.md` — B-031 through B-037 detail
- `docs/PHASE_13_IMPLEMENTATION_PLAN.md` §1.1 / §A — winners + kills
- `core/confluence_gates.py` — the canonical gate helpers
- `core/microstructure_filter.py` — msu_score formula (inverted pt8)

---

_If you're reading this in the future and the trading edge has changed,
re-run agent a16cf0ef's per-strategy voter IC analysis on the most-recent
2 years of MNQ Databento data and update this doc. The methodology is the
permanent thing; the per-voter numbers are perishable._
