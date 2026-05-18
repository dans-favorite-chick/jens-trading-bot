# Phoenix Strategy Specifications — Build-Ready Reference

**Status:** PRE-BUILD specification (Phase 13 deliverable). Hand this to engineering / use as the operator's mental model.

**Companion:** `ninjatrader/PhoenixTradeOverlay.cs` — NT8 chart visualizer (color-coded entry/exit markers + live SL/TP lines).

---

## Table of Contents

1. [How a Phoenix bot decides to enter (the universal flow)](#1-how-a-phoenix-bot-decides-to-enter)
2. [Multi-timeframe roles — how 1m / 5m / 15m work together](#2-multi-timeframe-roles)
3. [Per-strategy mechanical specs (entry / stop / management / exit)](#3-per-strategy-mechanical-specs)
4. [Stop management — when to move it, when to take profit](#4-stop-management-and-profit-taking)
5. [Color codes + chart visualization](#5-color-codes--chart-visualization)

---

## 1. How a Phoenix bot decides to enter

**Universal flow** (applies to every strategy):

```
On every 1m bar close, base_bot runs this loop:
  for each enabled strategy:
      ① TIME-WINDOW VETO         — is this strategy allowed to fire right now?
      ② REGIME VETO              — is the market regime compatible?
      ③ DATA FRESHNESS CHECK     — is all required data current?
      ④ TRIGGER CONDITION        — the "now" event (5m close beyond OR, pullback to EMA, etc.)
      ⑤ CONFIRMATION SCORE       — sum of factors that agree (CVD, volume, TF bias)
      ⑥ SCORE THRESHOLD          — must meet strategy's min_confluence
      ⑦ EMIT SIGNAL              — Signal(direction, entry, stop, target, confidence)
      ⑧ DEDUP CHECK              — has same signal already fired this bar?
      ⑨ POSITION MANAGER         — open trade? add to position? skip if at limit?
      ⑩ OIF FILE WRITE           — order goes to NT8
      ⑪ NT8 FILL                 — actual entry at fill price
```

**Steps ①-⑥ are STRATEGY-SPECIFIC.** Steps ⑦-⑪ are universal infrastructure.

**The "moment of entry" = step ④, the TRIGGER condition.** For most Phoenix strategies, this fires on a **bar close** (1m or 5m) — meaning the bot waits for the bar to fully form, then evaluates. NEVER on a tick mid-bar. This prevents look-ahead bias and matches real-world bot behavior.

### Why bar-close (not tick-by-tick)?
- A 5m bar gives the market 5 minutes to "vote" on direction
- Tick-by-tick fires on every wiggle → false signals
- Bar-close is the standard in production trading bots (QuantConnect, NautilusTrader, Freqtrade all default to bar-close)

### The entry PRICE is:
- **Default:** last 1m bar's close (= `market["price"]`)
- **Reality:** in NT8 live, you'll fill at next-tick after the OIF write — typically 0-3 ticks worse than the backtest close-price assumption
- **Mitigation:** budget 1 tick of slippage per side in your mental P&L expectations

---

## 2. Multi-timeframe roles

Phoenix tracks 3 native timeframes from MNQ. Each plays a different role:

| TF | Role | Used for |
|---|---|---|
| **15m** | TREND BIAS (longest signal) | "Should I be looking for longs or shorts today?" Slow EMAs (50, 200), prior-day pivots, regime classification |
| **5m** | STRUCTURE & TRIGGER | Patterns, key levels, EMA21, VWAP bands, ORB high/low, inside bars, pullback detection. **Most Phoenix triggers fire on 5m bars.** |
| **1m** | TIMING & EXECUTION | Entry precision, freshness checks, micro-volume confirmation, current price for SL/TP comparison |

**Multi-TF principles:**

1. **The TRIGGER timeframe is usually 5m.** 5m has the right signal-to-noise for swing/intraday on MNQ. (Validated by our failed 1m lab — 1m destroyed every edge.)
2. **The CONFIRMATION timeframe is one TF UP from trigger.** If trigger is on 5m, confirmation looks at 15m bias. This prevents fighting the dominant trend.
3. **The EXECUTION timeframe is one TF DOWN from trigger.** 1m timing refines entry — wait for 1m volume spike or pullback to optimal price.
4. **NEVER use the same TF for trigger AND confirmation.** That's circular (looking at price to confirm a price move).

### Multi-TF role per strategy:

| Strategy | 15m role | 5m role | 1m role |
|---|---|---|---|
| `opening_session.orb` | Skip if VIX>30 (regime VETO) | OR-break TRIGGER on 5m close | Last 1m price for entry, freshness |
| `raschke_baseline` | — | EMA21-EMA50 trend + pullback + TRIGGER | Entry execution |
| `g_inside_bar_breakout` | — | Inside-bar pattern + break TRIGGER | Entry execution |
| `e_multi_day_breakout` | — | 3-day H/L break TRIGGER | Entry execution |
| `a_asian_continuation` | — | Overnight range break TRIGGER | Entry execution |
| `vwap_pullback_v2` | TF bias VETO | VWAP touch + bounce TRIGGER | Entry execution + freshness |
| `spring_setup` | — | Wyckoff spring pattern TRIGGER | Entry execution |
| `es_nq_confluence` | — | MES-MNQ boost TRIGGER (5m returns) | Entry execution |
| `bias_momentum` | 15m EMA bias CONFIRMATION | EMA stack TRIGGER | Entry execution |
| `vwap_band_pullback` | — | Band touch + bounce TRIGGER | Entry execution |
| `vwap_band_reversion` | TREND-day VETO | 2.1σ band touch + reversal TRIGGER | Entry execution |
| `ib_breakout` | — | IB break TRIGGER | Entry execution |

**Insight:** Most strategies don't need 15m at all. Only `vwap_pullback_v2`, `bias_momentum`, and `opening_session` use 15m, and only for VETO/CONFIRMATION — never as trigger. **Adding more TF complexity rarely helps.** The KISS principle wins.

---

## 3. Per-strategy mechanical specs

Each strategy's section answers: what's the entry trigger? Where does the stop go? How is it managed?

### 3.1 `opening_session.orb` 🟡 — Yellow
- **Window:** 08:45-14:30 CT (skip lunch 10-15 CT per Phase 13 plan)
- **Trigger:** 5m close > 15-min RTH OR high (LONG) or < OR low (SHORT)
- **Required confirmations:**
  - CVD aligned (5-bar delta sum same sign as breakout)
  - OR width 11-80 pts (filters too-tight & too-wide ORs)
  - One direction per day (first break wins)
- **Stop:** Opposite OR side ± 2 ticks, with confirmation-bar fallback if > 60t
- **Initial target:** 50% of OR width beyond entry (RR ~1.5-2)
- **Management:** Strategy-internal managed exit — already optimized (existing logic stays)
- **For ≥5 contracts:** reserve last contract as BE+trail runner
- **Expected: WR ~50%, +$31,894/5y baseline**

### 3.2 `raschke_baseline` 🔵 — Cyan
- **Window:** RTH 08:30-15:00 CT, evaluated on 5m bar boundaries (minute % 5 == 0)
- **Trigger sequence:**
  1. TREND filter: `EMA21 - EMA50 > 0.3 × ATR_5m` for LONG (mirror for SHORT)
  2. PULLBACK: in last 3 5m bars (excluding current), find bar where `bar.low ≤ EMA21 + 2 ticks` AND `bar.close > EMA21`
  3. ENTRY: current 5m bar closes > pullback bar's high + 1 tick (LONG)
- **Stop:** Pullback bar's low - 1 tick (LONG), pullback bar's high + 1 tick (SHORT). Clamped 6-40 ticks.
- **Initial target:** 2× stop distance (2R fixed)
- **Management:** scale_out_1r RECOMMENDED (close 50% at 1R, BE stop on runner, 2R final target)
- **Why this works:** Trades WITH NQ's trend bias. EMA21 is institutional reference. Pullback = re-entry on continuation.
- **Expected: WR 67.7%, +$12,779/5y**

### 3.3 `g_inside_bar_breakout` 🟣 — Magenta
- **Window:** 08:45-14:00 CT, evaluated on 5m bar boundaries
- **Trigger sequence:**
  1. PRIOR 5m bar fully inside the bar before it (inside bar pattern)
  2. Inside bar range ≥ 4 ticks (not noise)
  3. Inside bar range ≤ 85% of parent bar range (real compression)
  4. CURRENT 5m close > inside bar high + 1 tick (LONG) or < low - 1 tick (SHORT)
- **Stop:** Opposite extreme of inside bar ± 1 tick. Clamped 6-30 ticks.
- **Initial target:** 2× stop distance
- **Management:** Fixed 2R target (current). Test scale_out_1r in next experiment.
- **Expected: WR 70%, +$11,300/5y**

### 3.4 `e_multi_day_breakout` 🟢 — Lime
- **Window:** 08:45-13:00 CT, once per day, 5m bar boundaries
- **Trigger:**
  - 5m close > max(last 3 RTH session highs) + 1 tick → LONG
  - 5m close < min(last 3 RTH session lows) - 1 tick → SHORT
- **Stop:** Opposite extreme of the 5m breakout bar ± 2 ticks. Clamped 6-30 ticks.
- **Initial target:** 2× stop distance
- **Management:** Fixed 2R + ES/NQ SIZE BOOST (1.3× when aligned per Section P).
- **Expected: WR 78%, +$9,097/5y**

### 3.5 `a_asian_continuation` 🟪 — Purple
- **Window:** 03:00-08:00 CT (sub-RTH), once per day, 5m boundaries
- **Setup tracking:** Overnight range from 17:00 prev-day through 03:00 CT
- **Trigger:**
  - 5m close > overnight high + 0.5 × ATR_5m → LONG
  - 5m close < overnight low - 0.5 × ATR_5m → SHORT
  - Overnight range must be > 8 ticks (filters chop)
- **Stop:** min(distance to opposite ON edge, 14 ticks). Floor 6 ticks.
- **Initial target:** 2× stop distance
- **Expected: WR 80%, +$5,909/5y**

### 3.6 `vwap_pullback_v2` 🟧 — Orange
- **Window:** **17:00-04:59 CT ONLY** (per Phase 13 plan — RTH variant loses money)
- **Trigger sequence:**
  1. Price bounces at VWAP within 60 ticks
  2. Pullback excursion ≥ 8 ticks recently (real pullback, not drift)
  3. Bounce candle (close > open for LONG)
  4. EMA9 > EMA21 on 5m (trend filter)
  5. CVD sign matches direction
- **Stop:** ATR_5m × 2.0; fallback to confirmation-bar if > 200t. Clamped 16-200t.
- **Initial target:** RR 1.8:1
- **Management:** scale_out_1r RECOMMENDED (proven 30pp WR lift)
- **Expected post-Phase-13: WR ~70%, +$10,144/5y**

### 3.7 `spring_setup` 🟢 — DarkGreen
- **Window:** All day, RTH preferred
- **Trigger sequence:**
  1. Long lower wick ≥ 6 ticks (bullish spring) or upper wick (bearish)
  2. Close near opposite high (LONG) — spring rejection
  3. Wick ≥ 1.5× body size
  4. VWAP reclaim (price > VWAP for LONG)
  5. CVD flip (positive for LONG)
  6. TF alignment ≥ 3/4
- **Stop:** ATR_5m × 1.1 from wick extreme. Clamped 40-120t.
- **Initial target:** Fixed 2× stop distance (per Phase 13 exit experiments — 3.4× lift)
- **Management:** Fixed 2R target
- **Expected post-Phase-13: WR 70%, ~$7,750/5y (with fixed_2x_target)**

### 3.8 `es_nq_confluence` ⚪ — White (DORMANT pending MES feed)
- **Window:** RTH, 5m boundaries
- **Trigger:**
  - (MNQ_5m_return - MES_5m_return) × 10000 > 25 basis points (LONG)
  - Rolling-50 5m correlation > 0.85
- **Stop:** Fixed 24 ticks (backtested optimal)
- **Initial target:** 96 ticks (4:1 RR fixed)
- **Management:** scale_out_1r (proven 81% WR in 2025 exit experiments)
- **Expected post-Phase-13: WR 81%, ~$2,092/5y (REQUIRES MES feed wired)**

### 3.9 `bias_momentum` 🔴 — Red
- **Window:** RTH, golden regimes only (OPEN_MOMENTUM, MID_MORNING)
- **Trigger:** EMA9 > EMA21 stack on 5m, with regime gate
- **Confirmation:** CVD aligned, momentum score ≥ 20
- **Stop:** ATR_5m × 2.0, clamp 40-120t
- **Initial target:** RR 5.0:1 (note: very wide; gets hit rarely)
- **Management:** time_15min — exit at market 15 min after entry (proven 40× lift)
- **Expected post-Phase-13: WR 52%, ~$963/5y (with time_15min)**

### 3.10 `vwap_band_pullback` 🔷 — SkyBlue
- **Window:** All day
- **Trigger:**
  - Bar low/high touches VWAP ± 1σ band
  - Bar close bounces above/below band
  - RSI(2) < 30 (LONG) or > 70 (SHORT)
- **Filter (NEW):** ema_counter — only trade against EMA trend (proven +$1.5k lift)
- **Stop:** lower_2σ - 0.5 × ATR. Clamp 40-120t.
- **Initial target:** RR 2.0:1
- **Management:** trail_atr_1x (proven WR 64%)
- **Expected post-Phase-13: WR 64%, ~$626/5y**

### 3.11 `vwap_band_reversion` 🌸 — Pink
- **Window:** All day EXCEPT 08:30-09:30 CT (open vol skip)
- **Trigger:**
  - Bar high touches VWAP + 2.1σ band (SHORT)
  - Bar low touches VWAP - 2.1σ band (LONG)
  - Reversal candle on the touch bar
- **Filter (REQUIRED):** combo_ema_vol — ema_counter + volume > 1.5× avg (without this filter, strategy LOSES money)
- **Veto:** Skip TREND day (regime gate)
- **Stop:** 2.5σ outer band ± 0.5 × ATR
- **Initial target:** VWAP (the mean)
- **Management:** scale_out_1r (proven 63% WR)
- **Expected post-Phase-13: WR 63%, ~$4,256/5y (with filter)**

### 3.12 `ib_breakout` 🟡 — Gold
- **Window:** 09:30 ET cash open +60 min, golden regimes only (OPEN_MOMENTUM, MID_MORNING)
- **Trigger:** 1m close outside Initial Balance (first 30 min RTH range)
- **Confirmation:** CVD aligned, IB width < 1.5 × ATR
- **Stop:** IB midpoint, clamp ≤ 120t
- **Initial target:** Fixed 2× stop distance (per Phase 13 exit experiments — 5× lift)
- **Management:** Fixed 2R target
- **Expected post-Phase-13: WR 46%, ~$200/5y**

---

## 4. Stop management and profit taking

The single biggest WR/expectancy lever is **how you exit a winning trade**. Phase 13 ships `scale_out_1r` as the default for most strategies because the 2025 experiments showed +20-38pp WR lift.

### 4.1 The scale_out_1r mechanism (default for most strategies)

```
At entry:
  - Open full position (N contracts) at entry_price
  - Submit OCO bracket: stop at stop_price, target at entry + (2 × stop_distance)

When price reaches entry + (1 × stop_distance):  ← THE "1R" CHECKPOINT
  - Close 50% of position at market (lock profit)
  - Move stop on remaining 50% to entry_price (break-even — can't lose)
  - Keep target at 2R for the runner

When 2R hit OR BE stop hit:
  - Close remaining 50%
  - Trade complete
```

**Result:** ~70% WR (vs ~40% baseline) because the 1R close locks the trade as a "winner" even if the runner gets stopped at BE.

### 4.2 Per-strategy exit policy (Phase 13 ship)

| Strategy | Exit policy | Why this one |
|---|---|---|
| `opening_session.orb` | Keep existing managed | Already optimized (+$31k baseline) |
| `raschke_baseline` | scale_out_1r | Untested but expected to lift WR 75-85% |
| `inside_bar_breakout` | Fixed 2R (default) → test scale_out_1r | Already 70% WR; scale_out may push to 85% |
| `multi_day_breakout` | Fixed 2R | Already 78% WR; high-confidence |
| `asian_continuation` | Fixed 2R | Already 80% WR; high-confidence |
| `vwap_pullback_v2` | **scale_out_1r** | Proven 30pp WR lift |
| `spring_setup` | **fixed_2x_target** | Proven 3.4× P&L lift |
| `es_nq_confluence` | **scale_out_1r** | Proven 81% WR |
| `bias_momentum` | **time_15min** | Proven 40× P&L lift (WR drops but $ explodes) |
| `vwap_band_pullback` | **trail_atr_1x** | Proven 17pp WR lift |
| `vwap_band_reversion` | **scale_out_1r** + combo filter | Proven 63% WR with filter |
| `ib_breakout` | **fixed_2x_target** | Proven 5× P&L lift |

### 4.3 Multi-contract scale-out (for accounts large enough)

When sizing to N > 1 contracts, the scale-out distribution from Phase 13 Section I.5:

| N | Tranches |
|---:|---|
| 1 | 100% at 2R |
| 2 | 1 @ 1R + 1 @ 2R |
| 3 | 1 @ 1R, 1 @ 2R, 1 runner trail BE+1R |
| 5 | 1 @ 0.75R, 2 @ 1.5R, 1 @ 2R, 1 runner |
| 10+ | Proportional 20%/30%/30%/20% across quarter/half/full target/runner |

### 4.4 When to MOVE the stop (BE & trailing)

**Move stop to BREAK-EVEN** when:
- Price reaches +1R AND scale_out_1r is the policy
- After first scale-out partial close completes

**Trail stop** (only for `vwap_band_pullback` per Phase 13 plan):
- Compute new_stop = current_price - 1.0 × ATR_5m (LONG)
- Move stop ONLY if new_stop > current_stop (one-way ratchet — never widen)
- Re-evaluate every 1m bar

**Never widen a stop.** The stop only moves AWAY from price in the trade's direction. Period.

### 4.5 Profit-taking sequence (the operator's mental model)

1. **Entry:** position open, stop at initial_stop, target at 2× stop distance
2. **Watch:** wait for price to move
3. **At +1R:** close 50%, move stop to BE on rest
4. **At +2R or BE stop:** exit remaining
5. **Done:** log trade, dashboard updates

**For ALL strategies, NEVER override the bot's exit.** Phoenix has a "manual override" emergency stop, but using it routinely defeats the entire backtested edge.

---

## 5. Color codes + chart visualization

You asked: "can you add something on my chart so I know where each bot buys in and sells out at? Color-coded?"

Yes — building `ninjatrader/PhoenixTradeOverlay.cs` (separate file from TickStreamer for clean separation).

### 5.1 Color assignments (12 strategies)

| Strategy | Color | Hex | Vibe |
|---|---|---|---|
| `opening_session.orb` | 🟡 Yellow | #FFD700 | Sun — opening |
| `raschke_baseline` | 🔵 Cyan | #00FFFF | Wave-rider |
| `g_inside_bar_breakout` | 🟣 Magenta | #FF00FF | Compressed |
| `e_multi_day_breakout` | 🟢 Lime | #00FF00 | Multi-day green |
| `a_asian_continuation` | 🟪 Purple | #9370DB | Night/Asian |
| `vwap_pullback_v2` | 🟧 Orange | #FF8C00 | Auction warm |
| `spring_setup` | 🟢 DarkGreen | #006400 | Wyckoff spring |
| `es_nq_confluence` | ⚪ White | #FFFFFF | Cross-asset pure |
| `bias_momentum` | 🔴 Red | #FF0000 | Momentum power |
| `vwap_band_pullback` | 🔷 SkyBlue | #87CEEB | Soft pullback |
| `vwap_band_reversion` | 🌸 Pink | #FF69B4 | Mean-revert pink |
| `ib_breakout` | 🟡 Gold | #DAA520 | IB break |

### 5.2 Chart markers (what you'll see)

**On entry (when bot fires):**
- Triangle pointing UP for LONG, DOWN for SHORT
- Color from the table above
- Strategy name label below (small text)

**While trade is active:**
- Horizontal RED dashed line at stop_price (live — moves when stop moves)
- Horizontal GREEN dashed line at target_price
- Faint shaded zone between current price and stop (red) and current price and target (green)

**On exit:**
- X marker (color = strategy color) at exit price
- Lines removed
- Small text annotation: P&L $X.XX, reason: "target" / "stop" / "BE" / "time"

### 5.3 How it works (data flow)

```
Phoenix bot emits signal
  ↓
core/signal_visualizer.py writes JSONL event:
  C:\Users\Trading PC\Documents\NinjaTrader 8\phoenix_signals.jsonl
  ↓
NT8 PhoenixTradeOverlay indicator polls file every 1s
  ↓
Parses events, updates internal state
  ↓
Draws markers + lines on chart
```

JSONL event schema (append-only):
```json
{"ts": "2026-05-18T09:35:00", "event": "signal", "id": "abc123",
 "strategy": "raschke_baseline", "direction": "LONG",
 "entry": 17500.25, "stop": 17495.00, "target": 17510.75}
{"ts": "2026-05-18T09:35:30", "event": "fill", "id": "abc123",
 "fill_price": 17500.50}
{"ts": "2026-05-18T09:42:15", "event": "stop_moved", "id": "abc123",
 "new_stop": 17500.50, "reason": "scale_out_1r_BE"}
{"ts": "2026-05-18T09:50:00", "event": "exit", "id": "abc123",
 "exit_price": 17510.75, "exit_reason": "target_hit", "pnl": 52.50}
```

### 5.4 Implementation (3 files)

1. **`core/signal_visualizer.py`** (NEW Python module) — writes JSONL events
2. **`bots/base_bot.py`** (MODIFIED) — calls signal_visualizer at signal/fill/exit events
3. **`ninjatrader/PhoenixTradeOverlay.cs`** (NEW NT8 indicator) — reads JSONL, draws markers + lines

Each is small (~100-300 LOC). See companion C# file for the NT8 indicator code.

---

## Honest caveats

1. **The PERFECT entry doesn't exist.** Even the best-tuned strategy fires false signals 20-40% of the time. The point is positive expectancy over many trades, not avoiding every loser.

2. **Multi-TF won't fix a weak signal.** If the 5m trigger has no edge, adding 15m filters just reduces fire rate without rescuing edge.

3. **Stop placement is more art than science** in production. The backtest numbers assume perfect fills. Live, your stop may slip 1-3 ticks on a fast move.

4. **Visualization is for monitoring, not decision-making.** Don't manually override the bot just because the chart shows a "scary" position. The bot's exit logic is backtested; your eyes are not.

5. **For the NT8 overlay to work,** Phoenix must consistently write to `phoenix_signals.jsonl`. If the bot crashes or the path changes, the chart goes dark.

---

This spec is ready to build. Next session: implement `core/signal_visualizer.py`, `bots/base_bot.py` integration, and `ninjatrader/PhoenixTradeOverlay.cs`.
