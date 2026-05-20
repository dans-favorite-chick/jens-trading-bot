# Entry Retest vs First-Touch Analysis

_Generated: 2026-05-20T01:30:55.441975+00:00_

## TL;DR - operator summary

- Window: 2026-03-17 -> 2026-05-15 (2,141 trades analysed across 21 strategies).
- Retest definition: price first runs >= 4 ticks in trade direction, THEN returns to within +/- 2 ticks of the signal level.
- Retest wait window: 30 minutes after first touch.
- Retest fill assumed 1 tick(s) worse than the touch price (conservative).
- Outcomes resolved via tick walk to stop/target with a 240-minute hard timeout.

Headline verdicts (strategies with n >= 30):

  - **bias_momentum** (n=497): verdict = `retest_only`, retest_rate=99.2%, fill_improvement=-3.00 tk (median)
  - **spring_setup** (n=870): verdict = `retest_only`, retest_rate=98.9%, fill_improvement=-3.00 tk (median)
  - **vwap_pullback_v2** (n=190): verdict = `first_touch`, retest_rate=98.4%, fill_improvement=-3.00 tk (median)
  - **raschke_ema9_ref** (n=30): verdict = `retest_only`, retest_rate=100.0%, fill_improvement=-3.00 tk (median)
  - **noise_area** (n=326): verdict = `first_touch`, retest_rate=99.7%, fill_improvement=-3.00 tk (median)
  - **vwap_band_reversion** (n=57): verdict = `retest_only`, retest_rate=96.5%, fill_improvement=-3.00 tk (median)

Aggregate across all n>=30 strategies (tick-window only):

  - MODE A FIRST-TOUCH all-in:    $17,472.00
  - MODE B RETEST-ONLY (skips):   $18,094.52  (opportunity cost of skipped trades: $-395.50)
  - MODE C HYBRID 50/50:          $17,783.26

Headline delta: RETEST-ONLY vs FIRST-TOUCH = **$+622.52** (+3.6%)
                HYBRID vs FIRST-TOUCH      = **$+311.26**

**Honest read**: the marginal dollar improvement is real but small relative to the strategy P&L itself. With conservative 1-tick chase slippage, waiting for a retest buys back roughly 3 ticks of FILL cost (= -$1.50/contract) but selects against the few signals that don't retest. On `bias_momentum` and `spring_setup` (the largest contributors), retest mode is +$25 to +$530 over 60 days vs first-touch. That's ~$150-$3000/yr extrapolated, NOT a transformational edge but a defensible micro-tweak.

## Method

For each historical trade in the tick window:

1. SIGNAL LEVEL = `entry_price` (the bar-close price the bot fired at).
2. Walk ticks for 30 minutes. A RETEST is a TWO-STAGE event:
   (a) price first RUNS at least 4 ticks in the trade's direction (away from the signal level), then
   (b) the marketable side comes back to within +/- 2 ticks of the level (ask <= level+band for LONG, bid >= level-band for SHORT).
   Without the run-first rule, mean-reversion entries would mark a false retest on the very first post-close tick because the bar close was itself an extreme.
3. Simulate a WAIT entry filled 1 tick worse than the touch (limit chase).
4. From each candidate fill price, walk ticks forward until the ORIGINAL stop or target is hit, or 240 minutes elapse.
5. Stop/target priority: if both are hit in the same window, the one that hits FIRST (lowest tick index) wins; ties go to STOP.

## Per-strategy headline table (n >= 30)

| Strategy | n | retest rate | fill impr med (tk) | A first-touch $ | B retest-only $ | C hybrid $ | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| spring_setup | 870 | 98.9% | -3.00 | $4,388 | $4,413 | $4,401 | retest_only |
| bias_momentum | 497 | 99.2% | -3.00 | $9,804 | $10,336 | $10,070 | retest_only |
| noise_area | 326 | 99.7% | -3.00 | $-310 | $-346 | $-328 | first_touch |
| vwap_pullback_v2 | 190 | 98.4% | -3.00 | $3,386 | $3,242 | $3,314 | first_touch |
| vwap_band_reversion | 57 | 96.5% | -3.00 | $-324 | $-198 | $-261 | retest_only |
| raschke_ema9_ref | 30 | 100.0% | -3.00 | $528 | $647 | $588 | retest_only |

## Win-rate comparison (n >= 30)

| Strategy | A first-touch WR | B retest-only WR | C hybrid WR |
|---|---:|---:|---:|
| spring_setup | 42.8% | 43.4% | 43.1% |
| bias_momentum | 40.6% | 41.2% | 41.4% |
| noise_area | 0.0% | 4.3% | 4.0% |
| vwap_pullback_v2 | 46.3% | 46.5% | 46.3% |
| vwap_band_reversion | 33.3% | 34.5% | 33.3% |
| raschke_ema9_ref | 70.0% | 83.3% | 83.3% |

## FOMO premium and fill improvement (n >= 30)

- **FOMO premium**: among trades that retest, how far did price move FAVORABLY before coming back? This is how many ticks of MFE first-touch entries _saw_ before the level was re-offered. Higher = first-touch enters earlier on the move.
- **Fill improvement**: how much better did the retest fill come in than the first-touch fill (in trade direction)? Positive = retest is cheaper.
- Note: median fill improvement is **-3.00 ticks across the board** because the retest mechanic is band-bounded: price returns to within the +/- 2-tick band then a 1-tick chase prices the fill 3 ticks ADVERSE to the level. The retest is paying a FILL premium of 3 ticks for the privilege of holding a position that has _already proven_ the level by running 4+ ticks first. This is the right framing: not 'better fill' (worse) but 'higher-quality signal at worse fill'.

| Strategy | n_retested | FOMO premium med (tk) | FOMO mean (tk) | Fill impr med (tk) | Fill impr mean (tk) |
|---|---:|---:|---:|---:|---:|
| spring_setup | 860 | +6.00 | +10.68 | -3.00 | -2.30 |
| bias_momentum | 493 | +10.00 | +21.72 | -3.00 | -1.71 |
| noise_area | 325 | +11.00 | +28.73 | -3.00 | -2.13 |
| vwap_pullback_v2 | 187 | +5.00 | +8.96 | -3.00 | -1.84 |
| vwap_band_reversion | 55 | +8.00 | +17.93 | -3.00 | -2.62 |
| raschke_ema9_ref | 30 | +12.00 | +21.23 | -3.00 | -2.77 |

## Per-strategy verdict + recommendation

### spring_setup (n=870)

- Retest rate: **98.9%** (860/870 trades re-touched the signal level within 30min)
- Median fill improvement on retest: **-3.00 ticks** (mean -2.30)
- A first-touch:  $4,388.50  (WR 42.8%)
- B retest-only:  $4,413.00  (WR 43.4%)   delta vs A: $+24.50
- C hybrid 50/50: $4,400.75  (WR 43.1%)   delta vs A: $+12.25
- Opportunity cost of skipping non-retesting trades: $-262.00
- **VERDICT: RETEST wins.** Worth adding a 'wait for retest' mode. First-touch is paying a FOMO premium without commensurate edge.

### bias_momentum (n=497)

- Retest rate: **99.2%** (493/497 trades re-touched the signal level within 30min)
- Median fill improvement on retest: **-3.00 ticks** (mean -1.71)
- A first-touch:  $9,804.00  (WR 40.6%)
- B retest-only:  $10,336.50  (WR 41.2%)   delta vs A: $+532.50
- C hybrid 50/50: $10,070.25  (WR 41.4%)   delta vs A: $+266.25
- Opportunity cost of skipping non-retesting trades: $92.00
- **VERDICT: RETEST wins.** Worth adding a 'wait for retest' mode. First-touch is paying a FOMO premium without commensurate edge.

### noise_area (n=326)

- Retest rate: **99.7%** (325/326 trades re-touched the signal level within 30min)
- Median fill improvement on retest: **-3.00 ticks** (mean -2.13)
- A first-touch:  $-310.50  (WR 0.0%)
- B retest-only:  $-346.00  (WR 4.3%)   delta vs A: $-35.50
- C hybrid 50/50: $-328.25  (WR 4.0%)   delta vs A: $-17.75
- Opportunity cost of skipping non-retesting trades: $0.00
- **VERDICT: FIRST-TOUCH wins.** Leave entries as-is. Waiting for a retest loses money because (a) the strategy enters at the right level already and/or (b) the trades that never retest are the BEST trades (strong momentum).

### vwap_pullback_v2 (n=190)

- Retest rate: **98.4%** (187/190 trades re-touched the signal level within 30min)
- Median fill improvement on retest: **-3.00 ticks** (mean -1.84)
- A first-touch:  $3,386.50  (WR 46.3%)
- B retest-only:  $3,242.50  (WR 46.5%)   delta vs A: $-144.00
- C hybrid 50/50: $3,314.50  (WR 46.3%)   delta vs A: $-72.00
- Opportunity cost of skipping non-retesting trades: $-28.50
- **VERDICT: FIRST-TOUCH wins.** Leave entries as-is. Waiting for a retest loses money because (a) the strategy enters at the right level already and/or (b) the trades that never retest are the BEST trades (strong momentum).

### vwap_band_reversion (n=57)

- Retest rate: **96.5%** (55/57 trades re-touched the signal level within 30min)
- Median fill improvement on retest: **-3.00 ticks** (mean -2.62)
- A first-touch:  $-324.50  (WR 33.3%)
- B retest-only:  $-198.48  (WR 34.5%)   delta vs A: $+126.02
- C hybrid 50/50: $-261.49  (WR 33.3%)   delta vs A: $+63.01
- Opportunity cost of skipping non-retesting trades: $-197.00
- **VERDICT: RETEST wins.** Worth adding a 'wait for retest' mode. First-touch is paying a FOMO premium without commensurate edge.

### raschke_ema9_ref (n=30)

- Retest rate: **100.0%** (30/30 trades re-touched the signal level within 30min)
- Median fill improvement on retest: **-3.00 ticks** (mean -2.77)
- A first-touch:  $528.00  (WR 70.0%)
- B retest-only:  $647.00  (WR 83.3%)   delta vs A: $+119.00
- C hybrid 50/50: $587.50  (WR 83.3%)   delta vs A: $+59.50
- Opportunity cost of skipping non-retesting trades: $0.00
- **VERDICT: RETEST wins.** Worth adding a 'wait for retest' mode. First-touch is paying a FOMO premium without commensurate edge.

## Strategies below n=30 (descriptive only)

| Strategy | n | retest rate | A $ | B $ | C $ |
|---|---:|---:|---:|---:|---:|
| opening_session | 29 | 100.0% | $-2,132 | $-2,051 | $-2,091 |
| e_multi_day_breakout | 28 | 100.0% | $547 | $456 | $502 |
| a_asian_continuation | 20 | 95.0% | $7 | $332 | $169 |
| g_inside_bar_breakout | 18 | 100.0% | $119 | $96 | $107 |
| raschke_loose_trend | 16 | 100.0% | $156 | $218 | $187 |
| compression_breakout_micro | 11 | 90.9% | $-128 | $25 | $-51 |
| compression_breakout_v2 | 10 | 100.0% | $-42 | $217 | $88 |
| raschke_strict_trend | 7 | 100.0% | $144 | $147 | $145 |
| raschke_baseline | 7 | 100.0% | $144 | $147 | $145 |
| raschke_3r_target | 7 | 100.0% | $162 | $170 | $166 |
| raschke_1.5r_target | 7 | 100.0% | $103 | $104 | $104 |
| raschke_long_only | 5 | 100.0% | $117 | $110 | $114 |
| raschke_ema50_ref | 3 | 100.0% | $13 | $8 | $11 |
| raschke_short_only | 2 | 100.0% | $26 | $37 | $32 |
| b_rth_open_drive_scalp | 1 | 100.0% | $16 | $-10 | $3 |

## Final recommendation

Across 6 strategies with n>=30 in the tick window:

  - **2** prefer FIRST-TOUCH  (status quo)
  - **4** prefer RETEST-ONLY  (add wait-for-retest mode)
  - **0** prefer HYBRID 50/50 (split-fill mode)

**Overall: ADD wait-for-retest mode to specific strategies** (listed above).

Per-strategy opt-in for RETEST-ONLY: spring_setup, bias_momentum, vwap_band_reversion, raschke_ema9_ref

## Caveats

- Tick window is 60 days out of a 5y backtest. Retest rates and fill improvements may differ in other regimes (specifically low-volatility chop tends to retest more; trending days less).
- We resolve outcomes by walking trade prices only. Quote-only updates are not used to trigger stops (matches reality that a stop-market needs a counterparty).
- Stop/target conflicts within the same tick favour STOP (pessimistic).
- Retest fill assumes a 1-tick chase. Real-world limit fills could be even cheaper (price improvement) OR could miss entirely; this assumption is intentionally conservative.
- The HYBRID mode P&L assumes a fractional contract (0.5x). In practice this requires 2-contract base size to map cleanly. Use the dollar deltas as the decision input, not the win-rate column (which counts trades not contracts).

## Files produced

- `backtest_results/phoenix_entry_retest_per_trade.csv`
- `backtest_results/phoenix_entry_retest_summary.csv`
- `docs/ENTRY_RETEST_ANALYSIS.md` (this report)
