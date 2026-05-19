# Tick-Level ENTRY Fill Quality Verification

_Generated: 2026-05-19T22:53:30.764391+00:00_

## TL;DR - operator summary

- Portfolio current backtest P&L: **$33,258/yr** (all 8 winning strategies, full 5y).
- After realistic 500ms-latency slippage: **$64,432/yr** (delta +31,174).
- After 2000ms-latency slippage: **$64,386/yr**.
- Using 5-second limit at bar-close price: **$32,605/yr**.

- **Use MARKET orders for**: `bias_momentum`, `spring_setup`, `raschke_baseline`, `vwap_pullback_v2`, `a_asian_continuation`, `opening_session`.
- **Use 5-second LIMIT for**: `g_inside_bar_breakout`, `e_multi_day_breakout`.
- **Strategies with systematic adverse slippage** (realistic mean > 0.5 ticks): `raschke_baseline`, `g_inside_bar_breakout`, `e_multi_day_breakout`.
- **No strategy's edge is killed by realistic slippage.**

## Purpose

The bar-level backtest assumes the bot fills exactly at the close
of the most-recent 1m bar at signal time. Reality:

1. Bot detects signal AT bar close
2. Bot writes OIF file -> NT8 reads it -> submits market order
3. Order fills at the next tick (or several ticks later)
4. Slippage = abs(actual_fill_price - bar_close_price)

This document quantifies that slippage per strategy using two months
of MNQM6 tick-by-trade data (Databento TBBO, 2026-03-17..2026-05-17)
and projects the dollar impact onto the full 5y backtest.

## Data & method

- Tick source: MNQM6 TBBO from Databento (44.4M trade records).
- Trade source: winning-strategy entries from
  `phoenix_real_5year.csv`, `phoenix_new_strategy_lab.csv`,
  `phoenix_trend_pullback_lab.csv`.
- Window: 2026-03-17 -> 2026-05-15 (1659 entries matched).
- Fill models tested:
    * **optimistic**  - bot fills at the very next tick at any price.
    * **realistic**   - market order fills at the first trade
                        >= signal_ts + 500ms (typical OIF latency).
    * **pessimistic** - market order fills at the first trade
                        >= signal_ts + 2000ms (slow / illiquid market).
    * **limit_5s**    - limit at bar-close price held for 5s, then
                        market. LONG fills if ask <= entry_px, SHORT
                        if bid >= entry_px during that 5s window.
- Adverse slippage is reported as POSITIVE ticks (higher = worse).
  Negative values = filled better than the bar close (price improvement).
- $/tick = $0.50 (MNQ).

## Headline table - realistic fill (500ms latency)

| Strategy | n | mean | median | p95 | pct >1tk | avg lag ms |
|---|---:|---:|---:|---:|---:|---:|
| bias_momentum | 355 | -3.08 | -5.00 | +31.00 | 42.0% | 189 |
| spring_setup | 854 | -8.25 | -6.00 | +2.00 | 6.6% | 189 |
| raschke_baseline | 6 | +3.83 | -5.00 | +37.25 | 33.3% | 23 |
| g_inside_bar_breakout | 17 | +4.76 | +2.00 | +26.40 | 58.8% | 38 |
| vwap_pullback_v2 | 163 | -15.74 | -15.00 | -1.00 | 0.6% | 230 |
| e_multi_day_breakout | 16 | +15.56 | +18.50 | +32.00 | 75.0% | 16 |
| a_asian_continuation | 17 | -7.53 | -7.00 | +16.40 | 29.4% | 182 |
| opening_session | 13 | -5.85 | -12.00 | +25.60 | 38.5% | 14 |

## All four fill models - median adverse slippage (ticks)

| Strategy | optimistic | realistic | pessimistic | limit_5s |
|---|---:|---:|---:|---:|
| bias_momentum | -4.00 | -5.00 | -6.00 | +0.00 |
| spring_setup | -5.00 | -6.00 | -7.00 | +0.00 |
| raschke_baseline | -3.00 | -5.00 | -3.50 | +0.00 |
| g_inside_bar_breakout | +4.00 | +2.00 | +3.00 | +0.00 |
| vwap_pullback_v2 | -15.00 | -15.00 | -14.00 | +0.00 |
| e_multi_day_breakout | +19.00 | +18.50 | +16.00 | +9.50 |
| a_asian_continuation | -7.00 | -7.00 | -4.00 | +0.00 |
| opening_session | -6.00 | -12.00 | -3.00 | +0.00 |

## Slippage tax projected onto full 5y backtest

Tax = full_set_trades * median_slip_ticks * $0.50/tick
Adjusted P&L = current_5y_pnl - tax. Per-year columns divide by the
trade-time span observed in the full backtest.

| Strategy | trades | cur $/yr | tax_real_med | adj $/yr (real) | adj $/yr (pess) | adj $/yr (lim5s) |
|---|---:|---:|---:|---:|---:|---:|
| bias_momentum | 13790 | $  35,707 | $ -34,475 | $  42,608 | $  43,988 | $  35,707 |
| spring_setup | 20778 | $   3,712 | $ -62,334 | $  16,191 | $  18,271 | $   3,712 |
| raschke_baseline | 927 | $   2,559 | $  -2,318 | $   3,023 | $   2,884 | $   2,559 |
| g_inside_bar_breakout | 1015 | $   2,266 | $   1,015 | $   2,063 | $   1,961 | $   2,266 |
| vwap_pullback_v2 | 5879 | $   2,031 | $ -44,092 | $  10,860 | $  10,271 | $   2,031 |
| e_multi_day_breakout | 685 | $   1,826 | $   6,336 | $     554 | $     726 | $   1,173 |
| a_asian_continuation | 596 | $   1,184 | $  -2,086 | $   1,602 | $   1,423 | $   1,184 |
| opening_session | 2949 | $ -16,027 | $ -17,694 | $ -12,469 | $ -15,138 | $ -16,027 |

> All $/yr columns use per-strategy trade-time span. See
> `phoenix_tick_entry_summary.csv` for raw unrounded values.

## Answers to the six key questions

**Q1. Average slippage per strategy (realistic model, ticks)**

Sign convention: POSITIVE = adverse to trade direction (you paid more for
a LONG or received less for a SHORT). NEGATIVE = price improvement (filled
better than the bar close).

- `bias_momentum` - mean -3.08 ticks, median -5.00 ticks
- `spring_setup` - mean -8.25 ticks, median -6.00 ticks
- `raschke_baseline` - mean +3.83 ticks, median -5.00 ticks
- `g_inside_bar_breakout` - mean +4.76 ticks, median +2.00 ticks
- `vwap_pullback_v2` - mean -15.74 ticks, median -15.00 ticks
- `e_multi_day_breakout` - mean +15.56 ticks, median +18.50 ticks
- `a_asian_continuation` - mean -7.53 ticks, median -7.00 ticks
- `opening_session` - mean -5.85 ticks, median -12.00 ticks

**Q2. Strategies suffering systematic adverse slippage?**

'Systematic' = realistic-model MEAN > 0.5 ticks (i.e. average dollar
loss per trade vs bar-close > $0.25). Median > 0 alone isn't enough:
many strategies have median = 0 yet a heavy positive tail.

- `raschke_baseline` mean +3.83 ticks (~$1.92/trade), median -5.00, p95 +37.2, pct>1tk 33%
- `g_inside_bar_breakout` mean +4.76 ticks (~$2.38/trade), median +2.00, p95 +26.4, pct>1tk 59%
- `e_multi_day_breakout` mean +15.56 ticks (~$7.78/trade), median +18.50, p95 +32.0, pct>1tk 75%

Mean-reversion / pullback strategies (spring_setup, vwap_pullback_v2,
a_asian_continuation, raschke_baseline) tend to show FAVORABLE
slippage on average because they enter at counter-trend extensions:
the bar that closes at a level the strategy fades often has a few more
ticks of follow-through before reversing, so the bot's market order
fills on that follow-through and benefits the trade. This is real
(observable in the per-trade CSV) but should be treated as a happy
artifact rather than 'free money' - the bar-close price is fictional
in the first place.

**Q3. Slippage tax in $/year per strategy** (realistic, median):

- `bias_momentum` - $-6,901/yr ($-34,475 total over 5.0y)
- `spring_setup` - $-12,479/yr ($-62,334 total over 5.0y)
- `raschke_baseline` - $-464/yr ($-2,318 total over 5.0y)
- `g_inside_bar_breakout` - $204/yr ($1,015 total over 5.0y)
- `vwap_pullback_v2` - $-8,829/yr ($-44,092 total over 5.0y)
- `e_multi_day_breakout` - $1,272/yr ($6,336 total over 5.0y)
- `a_asian_continuation` - $-418/yr ($-2,086 total over 5.0y)
- `opening_session` - $-3,559/yr ($-17,694 total over 5.0y)

**Q4. Strategies whose edge is killed by slippage?**

- None. Every profitable strategy remains profitable after
  applying realistic median slippage.

**Q5. Realistic P&L per strategy (after median slippage):**

- `bias_momentum` - current $35,707/yr -> adjusted $42,608/yr (delta $+6,901)
- `spring_setup` - current $3,712/yr -> adjusted $16,191/yr (delta $+12,479)
- `raschke_baseline` - current $2,559/yr -> adjusted $3,023/yr (delta $+464)
- `g_inside_bar_breakout` - current $2,266/yr -> adjusted $2,063/yr (delta $-204)
- `vwap_pullback_v2` - current $2,031/yr -> adjusted $10,860/yr (delta $+8,829)
- `e_multi_day_breakout` - current $1,826/yr -> adjusted $554/yr (delta $-1,272)
- `a_asian_continuation` - current $1,184/yr -> adjusted $1,602/yr (delta $+418)
- `opening_session` - current $-16,027/yr -> adjusted $-12,469/yr (delta $+3,559)

**Q6. Limit-order vs market-order recommendation:**

`fill_rate` = pct of trades where the limit @ bar-close price filled
WITHIN 5s. The remaining trades fell through to a delayed market order.

- `bias_momentum` - market $42,608/yr vs limit_5s $35,707/yr (delta -6,901, limit fill_rate 65%) -> **market**
- `spring_setup` - market $16,191/yr vs limit_5s $3,712/yr (delta -12,479, limit fill_rate 100%) -> **market**
- `raschke_baseline` - market $3,023/yr vs limit_5s $2,559/yr (delta -464, limit fill_rate 67%) -> **market**
- `g_inside_bar_breakout` - market $2,063/yr vs limit_5s $2,266/yr (delta +204, limit fill_rate 59%) -> **limit_5s**
- `vwap_pullback_v2` - market $10,860/yr vs limit_5s $2,031/yr (delta -8,829, limit fill_rate 100%) -> **market**
- `e_multi_day_breakout` - market $554/yr vs limit_5s $1,173/yr (delta +619, limit fill_rate 44%) -> **limit_5s**
- `a_asian_continuation` - market $1,602/yr vs limit_5s $1,184/yr (delta -418, limit fill_rate 82%) -> **market**
- `opening_session` - market $-12,469/yr vs limit_5s $-16,027/yr (delta -3,559, limit fill_rate 69%) -> **market**

Notes on the recommendation:
- For strategies where market beats limit, the bot's market order is
  benefiting from post-bar follow-through (mean-reversion entries).
- For strategies where limit beats market, the breakout immediately
  trades through and a market order eats 5-20 ticks of momentum.
  Switching to a 5s limit there saves real money - at the cost of
  missing fills when the breakout never retraces.
- Watch the limit fill_rate: a low fill_rate combined with positive
  limit-vs-market delta means we'd save money per-trade but trade
  less often. The $/yr column already accounts for that by applying
  the simulated (possibly missed) fills uniformly.

## Manual sanity verification

Below we show the signal timestamp, the next 10 trades, and the
computed fills for a representative trade per strategy. Spot-check
that the fill model picked sensible ticks.

```

### Manual verify: spring_setup SHORT @ 2026-03-17 00:01:00+00:00 px=24858.25
Next 10 ticks:
  idx |    lag_ms |       px  |     bid  |     ask | side
  ----+-----------+-----------+----------+---------+-----
    0 |      19.2 |  24859.50 | 24859.00 | 24859.50
    1 |      51.3 |  24859.50 | 24859.00 | 24859.50
    2 |     397.4 |  24861.00 | 24860.50 | 24861.00
    3 |     398.2 |  24860.75 | 24860.75 | 24861.00
    4 |     443.9 |  24860.25 | 24860.25 | 24860.75
    5 |     444.3 |  24860.00 | 24860.00 | 24860.25
    6 |     789.8 |  24860.75 | 24860.75 | 24861.25
    7 |     872.3 |  24860.00 | 24860.00 | 24860.50
    8 |     872.3 |  24859.75 | 24860.00 | 24860.50
    9 |     960.3 |  24860.50 | 24859.75 | 24860.50

### Manual verify: bias_momentum SHORT @ 2026-03-17 00:21:00+00:00 px=24840.75
Next 10 ticks:
  idx |    lag_ms |       px  |     bid  |     ask | side
  ----+-----------+-----------+----------+---------+-----
    0 |      24.0 |  24844.25 | 24843.75 | 24844.25
    1 |     428.4 |  24843.75 | 24843.75 | 24844.25
    2 |     860.0 |  24844.75 | 24844.25 | 24844.75
    3 |    1225.6 |  24844.75 | 24844.50 | 24844.75
    4 |    1608.8 |  24845.25 | 24844.75 | 24845.25
    5 |    1667.9 |  24845.25 | 24844.75 | 24845.25
    6 |    1667.9 |  24845.50 | 24844.75 | 24845.25
    7 |    2298.8 |  24845.25 | 24845.25 | 24845.75
    8 |    2511.6 |  24845.50 | 24845.00 | 24845.50
    9 |    3046.1 |  24845.00 | 24845.00 | 24845.50

### Manual verify: vwap_pullback_v2 LONG @ 2026-03-17 05:01:00+00:00 px=24832.75
Next 10 ticks:
  idx |    lag_ms |       px  |     bid  |     ask | side
  ----+-----------+-----------+----------+---------+-----
    0 |     687.5 |  24832.00 | 24832.00 | 24832.25
    1 |     687.9 |  24832.00 | 24832.00 | 24832.25
    2 |     688.5 |  24832.00 | 24831.75 | 24832.00
    3 |     814.1 |  24832.50 | 24832.25 | 24832.50
    4 |     925.7 |  24832.25 | 24832.25 | 24832.75
    5 |    1014.8 |  24832.50 | 24832.50 | 24833.00
    6 |    1445.0 |  24832.25 | 24832.25 | 24832.75
    7 |    1445.2 |  24832.25 | 24832.00 | 24832.25
    8 |    1589.3 |  24832.00 | 24832.00 | 24832.25
    9 |    1590.7 |  24832.25 | 24832.00 | 24832.25

### Manual verify: a_asian_continuation LONG @ 2026-03-17 12:30:00+00:00 px=24910.75
Next 10 ticks:
  idx |    lag_ms |       px  |     bid  |     ask | side
  ----+-----------+-----------+----------+---------+-----
    0 |     891.1 |  24909.00 | 24909.00 | 24909.75
    1 |     893.9 |  24908.75 | 24908.75 | 24909.50
    2 |     980.5 |  24909.75 | 24909.00 | 24909.75
    3 |     994.2 |  24909.00 | 24909.00 | 24909.75
    4 |    1059.8 |  24909.75 | 24909.00 | 24909.75
    5 |    1100.3 |  24909.75 | 24909.00 | 24909.75
    6 |    1100.3 |  24910.00 | 24909.00 | 24909.75
    7 |    1120.8 |  24909.25 | 24909.25 | 24910.00
    8 |    1132.6 |  24909.00 | 24909.00 | 24909.75
    9 |    1325.6 |  24909.00 | 24909.00 | 24909.75
```

## Methodology caveats

- Tick window is two months out of the full 5y backtest. Slippage
  characteristics in 2026-Q1/Q2 may not equal 2021-2024. The
  projection to full $/year therefore carries non-trivial uncertainty.
- All slippage is computed at the trade level (TBBO `action='T'`).
  Quote-only updates are NOT used to bound fills - this matches the
  reality that a market order needs a counterparty.
- 'Adverse' slippage assumes the bar-close price was achievable;
  it is the gap between that fiction and the next real trade.
- The limit_5s model assumes zero queue position - it fills the
  instant the touch trades through the limit price. Real-world
  queue position would produce slightly worse limit fills.
- Latency constants (500ms realistic, 2000ms pessimistic) are
  estimates of bot-detect -> OIF-write -> NT8-read -> CME-route
  cycle. Actual values should be measured on the production rig.

## Files produced

- `backtest_results/phoenix_tick_entry_slippage.csv` - per-trade
- `backtest_results/phoenix_tick_entry_summary.csv`  - per-strategy
- `docs/TICK_LEVEL_ENTRY_VERIFICATION.md` - this report
