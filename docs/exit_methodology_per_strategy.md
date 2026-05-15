# Exit Methodology Per Strategy — 2026-05-15

_Research-backed mapping of stop-loss + take-profit + trail methods to each
Phoenix strategy. Use this when tuning a strategy that's losing on its
exits (today's noise_area example) or when designing a new one._

## TL;DR — there's no "one best" exit, it depends on the strategy type

The literature consensus is unambiguous: a strategy's exit methodology
should match its **edge profile**, not be picked by personal preference.

| Strategy type | Stop loss | Take profit | Trail | Key source |
|---|---|---|---|---|
| **Trend-follower** | ATR-anchored 1.5-2× ATR at swing low/high | None — let runners run | ATR-trail / stall detector | Clenow (*Following the Trend*); Carver (*Systematic Trading*) |
| **Mean-reversion** | Beyond setup swing extreme | Back to mean (VWAP / POC) | None — fixed target | Raschke (*Street Smarts*) |
| **Band reversion** | Beyond opposite band | Back to VWAP center | None | Bollinger; standard MR doctrine |
| **Range breakout** | Midpoint of OR/IB | 50% partial at 1R + Chandelier 3×ATR runner | Le Beau Chandelier | Zarattini ORB paper (the published spec) |
| **Squeeze breakout** | Opposite side of squeeze | None — ride trail | ATR-trail or Chandelier | TTM Squeeze (Carter); Volatility Box |
| **Noise-cone breakout** | Opposite cone boundary (1000t disaster anchor) | Bar-close return inside cone + 5-min min-hold | None (managed) | Zarattini noise paper — but with **confirmation**, not tick-touch |
| **Order-flow reversal** | Beyond confluence level | Structural (next HTF level) | CVD-flow reversal | Beggs / Greenblatt; footprint trader school |

---

## Strategy-by-strategy mapping in Phoenix

| Strategy | Type | Stop | Target | Trail | Phoenix implementation |
|---|---|---|---|---|---|
| `bias_momentum` | Trend-follower | 2.0× ATR_5m anchored to last 5m wick | 2.5R fixed (rare; usually trailed out) | ATR/stall-detector | ✅ Implemented |
| `vwap_pullback` | Mean-reversion | 1.5× ATR_5m anchored at swing | 2.5R fixed | None | ✅ Implemented (#1b tightened from 2.0× to 1.5×) |
| `dom_pullback` | Trend-follower | 2.0× ATR_5m | 2.5R + 20:1 runner via rider mode | ATR/stall-detector | ✅ Implemented |
| `vwap_band_pullback` | Band reversion | Beyond opposite 2σ band | 2.0R fixed back toward VWAP | None | ✅ Implemented |
| `vwap_band_reversion` | Band reversion | Beyond 2.1σ band | Back to VWAP | None | ✅ Implemented |
| `orb` | Range breakout | Midpoint of OR | 2.0R + Chandelier 3×ATR(14) | ✅ Chandelier | ✅ Implemented (Zarattini spec) |
| `ib_breakout` | Range breakout | Midpoint of IB | 1.5× IB extension | (no formal trail) | ⚠️ Could add Chandelier per Zarattini |
| `compression_breakout` | Squeeze breakout | Opposite side of squeeze | 5R | Built into max_hold | ⚠️ Could add ATR-trail explicitly |
| `noise_area` | Noise-cone breakout | Opposite cone boundary (1000t structural) | Bar-close return inside cone OR VWAP cross OR EoD | None (managed) | ✅ Implemented + **2026-05-15 fix**: bar-close confirmation + 5-min min-hold |
| `footprint_cvd_reversal` | Order-flow reversal | Beyond confluence level + buffer | Structural target (next HTF level) | cvd_flip / cvd_divergence | ✅ Implemented; #19 priority added 2026-05-13 |
| `noise_area` (pre-2026-05-15) | — | — | **EXITED ON TICK PRICE** crossing UB/LB/VWAP — caused 3/4 today to exit within 2 min | — | ❌ THE BUG that prompted this doc |

---

## The research, briefly

### Trend-followers — Clenow / Carver

- **Initial stop**: 3× daily ATR from entry (Clenow); 4× daily volatility (Carver)
- **No take-profit**. Trend strategies make most of their money on the long-tail winner — taking profit caps the upside.
- **Exit only on**: signal reversal, trailing stop hit, or position-size rebalance.
- **Win rate ~30%, profit factor ~1.5**. The math only works if you let winners run.

### Mean-reversion — Raschke, Bollinger

- **Initial stop**: beyond the setup's confirming swing extreme (e.g., behind the lower wick of a VWAP-bounce setup). Structural, not ATR.
- **Take profit at the MEAN.** For VWAP-pullback, target VWAP. For band reversions, target VWAP center.
- **Time stop is critical** — Raschke: "if a mean-reversion trade doesn't work in 2/3 of the expected hold time, exit." Phoenix's `max_hold_min` enforces this.
- **Win rate ~60-70%, payoff <1**. Many small wins, occasional big losers on structural breaks.

### Breakouts — Zarattini, Carter, Le Beau

- **Initial stop**: midpoint of the breakout range (OR or IB).
- **Scale out 50% at 1R** (Carter, Zarattini). Capture base hit, let runner ride.
- **Runner trails with Chandelier 3× ATR(14) on 5m bars** (Le Beau via Zarattini).
- **The key insight from Zarattini**: range breakouts have a "trend-day" tail. Most break days produce a 1R move (the scale-out target) but ~20% extend 5-10R. The trailing runner captures that fat tail.

### Squeeze breakouts — TTM Squeeze / Volatility Box

- **Initial stop**: opposite side of the squeeze coil.
- **No fixed target.** Volatility Box's 18-year study on 30Y Treasury futures showed squeezes release with 5R+ moves on average — fixed targets cap the edge.
- **Trail**: ATR-trail or Chandelier, same as breakouts.
- Phoenix's `target_rr=5.0` is a target floor, not a take-profit — `max_hold_min=90` is the actual time stop.

### Noise-cone (Zarattini noise paper) — the exact strategy that broke today

- **Initial stop**: opposite cone boundary (wide; ~150-1000t on MNQ — structural disaster anchor).
- **Sizing uses risk-reference stop (40t)**, NOT the structural stop. This is the B21 managed-exit-sizing pattern.
- **Take profit / exit**: "Confirmed return to cone OR signal flip on VWAP" per the paper. The **CRITICAL** word is *confirmed* — i.e., bar close, not a tick. This is what we fixed 2026-05-15.
- **Min-hold**: not in the paper but a practical addition on MNQ. 5 minutes gives the trade room to breathe past entry-tick noise. Otherwise the "confirmation" can fire within seconds of entry on the FIRST closed bar that retraced.

### Order-flow reversal — footprint / CVD school

- **Initial stop**: beyond the confluence level (e.g., HTF support + footprint absorption + CVD divergence) + a buffer.
- **Take profit**: next structural level (HTF resistance, prior-day high, etc.).
- **Exit signal**: CVD flip + footprint reversal. Real-time order flow tells you when your edge is gone.

---

## What's NOT a good exit (anti-patterns)

1. **Tick-touch on a level.** Single-tick comparisons across VWAP / band / cone boundary fire on noise, not signal. Always require bar-close confirmation. (Fixed in noise_area 2026-05-15.)
2. **No min-hold window.** Entry tick noise alone can trigger exit triggers within seconds. (Fixed in noise_area 2026-05-15.)
3. **Fixed take-profit on trend strategies.** Caps the right-tail that's the whole edge.
4. **No time-stop on mean-reversion.** A stuck mean-reversion trade is usually wrong about the mean.
5. **One-size-fits-all exit.** Phoenix's mistake earlier with the universal `_sanity_check_entry` at 200t was the same family — applying one rule across strategy types with different stop physics. Fixed by adding `is_managed_exit` parameter.

---

## When to revisit this doc

After each batch of ~30 trades per strategy, run `tools/validation_tracker.py
--check-promotion` and review the per-strategy stats. Specifically look for:

- **Exit reason concentration**: if >50% of losses exit via a single reason
  (today: noise_area's 3 of 4 losses on `signal_flip_*`), that reason
  is over-firing.
- **MFE-vs-realized gap**: now persisted via #2's MAE/MFE tracking.
  If winners realize 30% of MFE, the take-profit is too eager.
- **Hold-time distribution**: if median hold << configured `max_hold_min`,
  the exit logic is too aggressive.

## References

- Andreas Clenow — *Following the Trend* (2013)
- Robert Carver — *Systematic Trading* (2015)
- Linda Raschke & Lawrence Connors — *Street Smarts* (1996)
- John Carter — *Mastering the Trade* (2nd ed., 2012)
- Charles Le Beau & David Lucas — *Technical Traders Guide to Computer Analysis* (1992) — Chandelier exit
- Welles Wilder — *New Concepts in Technical Trading Systems* (1978) — ATR
- Carlo Zarattini, Andrew Aziz, Daniel Barbon — *Beat the Market: An Effective Intraday Momentum Strategy* (SSRN 4824172, 2024) — noise-cone
- Carlo Zarattini, Daniel Barbon, Andrew Aziz — *Opening Range Breakout* (SSRN 4729284, 2024) — ORB
- Marcos Lopez de Prado — *Advances in Financial Machine Learning* (2018) — Triple Barrier Method
- Lance Beggs — *Your Trading Coach* — order-flow exit methodology
