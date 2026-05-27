# Phoenix Bot — Tactical Roadmap v4 (ORB + Noise Area Full Integration)

**Owner:** Jennifer (Frisco, TX) | **Target window:** April 20 – May 31, 2026
**Account path:** $2K sim → $2K live (if gates pass) → scale toward $10K
**Strategy lineup:** 8 strategies total (original 6 + ORB + Noise Area)

---

## ⚠️ Pre-flight

1. **MenthorQ API key rotated** — still pending 🔐
2. **STOP → STOPMARKET fix committed** — your P5b blocker
3. **$2K sim account loaded in NT8 Sim101** — upgraded from $300

---

## 🆕 PART A — Noise Area Strategy: Complete Spec

### Source
**Zarattini, Aziz & Barbon (2024)** — "Beat the Market: An Effective Intraday Momentum Strategy for the S&P500 ETF (SPY)." Swiss Finance Institute Research Paper No. 24-97, SSRN 4824172. Authors' own Python reference implementation at concretumgroup.com.

### Core concept (one-paragraph summary)
Each day at the open, compute a "noise cone" — upper and lower price boundaries — using the 14-day rolling average of intraday moves at each specific time of day. Boundaries are anchored to max(today's open, yesterday's close) for upper, min(today's open, yesterday's close) for lower. The cone *expands* through the session because average intraday moves grow over the day. When price breaks above the upper boundary AND price > VWAP, go long. When price breaks below the lower boundary AND price < VWAP, go short. Check signal at 30-minute intervals. Exit when price returns inside the noise area OR at end of session.

### The math (exactly as published)

```python
# Step 1: Per-minute metric (rolling 14-day history)
move_open[t] = abs(close[t] / open_of_day - 1)   # Absolute % move from open

# Step 2: For each minute-of-day bucket (e.g., minute 31 = 10:00 ET), 
#         compute rolling 14-day mean, then shift by 1 day (use yesterday's value):
sigma_open[t] = rolling_14_day_mean(move_open at same minute-of-day).shift(1)

# Step 3: Compute noise boundaries for each minute of today's session
UB[t] = max(today_open, prev_close_adjusted) * (1 + band_mult * sigma_open[t])
LB[t] = min(today_open, prev_close_adjusted) * (1 - band_mult * sigma_open[t])

# Step 4: Entry signal at top-of-hour / bottom-of-hour (every 30 min)
if close > UB AND close > VWAP:
    signal = LONG
elif close < LB AND close < VWAP:
    signal = SHORT
else:
    signal = FLAT

# Step 5: Position sizing (volatility-targeted, capped at max leverage)
if spx_vol is NaN:
    contracts = round(AUM / open_price * max_leverage)
else:
    contracts = round(AUM / open_price * min(target_vol / spx_vol, max_leverage))
```

### Adaptations for MNQ futures

Four changes from the published SPY implementation:

1. **No dividend adjustment.** Futures don't pay dividends. Use raw prev_close.
2. **"Open" = 9:30 ET cash open**, not overnight open. MNQ trades 23 hours; we care about the cash session open.
3. **Contract-based sizing, not share-based.** $2 per NQ point. At $2K AUM, 1 MNQ contract is effectively the only sizable position regardless of formula (formula will round to 1).
4. **VWAP anchored to 9:30 ET cash open**, not session start. Your existing `anchored_vwap.py` already handles this.

### Session handling for Phoenix Bot (the key decision)

The original Noise Area strategy trades the full 6.5-hour US session (9:30-16:00 ET). Your current prod session is 9:30-11:00 ET (90 min). Three options:

| Option | Pros | Cons |
|---|---|---|
| **A.** Keep 90-min prod session, 2 signal checks (9:30, 10:00, 10:30) | Maintains discipline of your existing window | Only 1-2 Noise Area trades per day possible |
| **B.** Extend prod session to 9:30-12:00 ET for Noise Area only | More trading opportunities | Breaks your current "disciplined short window" structure |
| **C.** Noise Area in lab_bot 24/7, prod_bot restricted to 9:30-11:00 ET | Collects full-day data for analysis, prod stays disciplined | Lab vs prod divergence requires careful attribution |

**Recommendation: Option C.** This is exactly what your `lab_bot` vs `prod_bot` architecture was designed for. Run Noise Area 9:30-16:00 ET in lab (full Zarattini methodology), and restrict to 9:30-11:00 ET in prod. Compare lab vs prod attribution in Week 2 triage.

### Noise Area implementation — `strategies/noise_area.py`

```python
class NoiseAreaMomentum(BaseStrategy):
    """
    Noise Area intraday momentum — Zarattini et al. 2024.
    
    Mechanism:
    - Dynamic noise cone expands through session based on 14-day
      historical average intraday move at each time-of-day.
    - Entry when price breaks outside cone AND on correct side of VWAP.
    - Exit when price returns inside cone OR EoD.
    - Check signal every 30 minutes (top/bottom of hour).
    
    Published results (SPY, 2007-2024): 19.6% annual, Sharpe 1.33
    NQ backtest (Quantitativo): 24.3% annual, Sharpe 1.67, 38% WR, payoff 2.25
    """
    
    PARAMS = {
        "lookback_days": 14,              # Rolling window for sigma_open
        "band_mult": 1.0,                 # Noise boundary multiplier
        "trade_freq_minutes": 30,         # Signal check interval
        "target_vol_daily": 0.02,         # 2% daily vol target
        "max_leverage": 4.0,              # Hard cap
        "require_vwap_confluence": True,  # Dual condition per paper
        "eod_flat_time_et": "15:55",      # Full session mode
        "prod_eod_flat_time_et": "10:55", # Your 90-min window
        "min_noise_history_days": 10,     # Need at least this much history to fire
    }
    
    def __init__(self, config):
        super().__init__(config)
        self.sigma_open_table = {}         # {minute_of_day: [14-day history of |move_open|]}
        self.daily_noise_area_cache = {}   # {date: {minute: (UB, LB)}}
        self.session_vwap_mgr = None       # Injected by base_bot
    
    def evaluate(self, market, bars_1m, session_info, **kwargs):
        now_et = session_info['current_time_et']
        current_price = market['price']
        
        # 1. Check if we have enough noise history
        if len(self.sigma_open_table) < self.PARAMS['min_noise_history_days']:
            return None  # Warmup: need 10+ days of history
        
        # 2. Only evaluate at 30-min signal windows (9:30, 10:00, 10:30, 11:00...)
        minute_of_hour = now_et.minute
        if minute_of_hour not in [0, 30]:
            return None
        
        # 3. Check EoD flat time (different for lab vs prod bot)
        eod_time = (self.PARAMS['prod_eod_flat_time_et'] 
                    if self.is_prod_bot 
                    else self.PARAMS['eod_flat_time_et'])
        if now_et.strftime('%H:%M') > eod_time:
            return None  # No new entries near close
        
        # 4. Compute current noise boundaries
        minute_of_day = self._minute_of_day(now_et)
        sigma_open = self._get_sigma_open(minute_of_day)
        if sigma_open is None:
            return None  # Insufficient history for this minute
        
        today_open = session_info['today_open_price']
        prev_close = session_info['prev_close_price']
        
        UB = max(today_open, prev_close) * (1 + self.PARAMS['band_mult'] * sigma_open)
        LB = min(today_open, prev_close) * (1 - self.PARAMS['band_mult'] * sigma_open)
        
        # 5. Get session VWAP from anchored_vwap module
        vwap = self.session_vwap_mgr.get_session_vwap(
            anchor='9:30_ET', bars=bars_1m
        )
        
        # 6. Signal logic — dual condition
        if (current_price > UB) and (current_price > vwap):
            return Signal(
                direction="LONG",
                entry_type="LIMIT",           # Already broke out, join at current price
                entry_price=current_price + 0.25,  # 1 tick wiggle for fill
                stop_type="STOP_MARKET",
                stop_price=LB - 0.50,          # Below lower boundary
                target_structure="dynamic_exit_on_signal_flip",
                exit_trigger="price_returns_inside_noise_area",
                eod_flat_time_et=eod_time,
                strategy_name="noise_area",
                metadata={
                    "UB": UB, "LB": LB, 
                    "vwap": vwap,
                    "sigma_open": sigma_open
                }
            )
        elif (current_price < LB) and (current_price < vwap):
            return Signal(
                direction="SHORT",
                entry_type="LIMIT",
                entry_price=current_price - 0.25,
                stop_type="STOP_MARKET",
                stop_price=UB + 0.50,
                target_structure="dynamic_exit_on_signal_flip",
                exit_trigger="price_returns_inside_noise_area",
                eod_flat_time_et=eod_time,
                strategy_name="noise_area",
                metadata={"UB": UB, "LB": LB, "vwap": vwap, "sigma_open": sigma_open}
            )
        
        return None
    
    def _minute_of_day(self, now_et):
        """Returns minutes since 9:30 ET open. 9:30 = 0, 10:00 = 30, etc."""
        open_time = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        return int((now_et - open_time).total_seconds() / 60)
    
    def _get_sigma_open(self, minute_of_day):
        """Returns 14-day rolling mean of |move_open| at this minute, shifted 1 day."""
        if minute_of_day not in self.sigma_open_table:
            return None
        history = self.sigma_open_table[minute_of_day]
        if len(history) < 13:  # Need at least 13 days
            return None
        return sum(history[-14:-1]) / min(13, len(history) - 1)  # Shift-1, 14-day mean
    
    def on_bar_close(self, bar, bars_today):
        """Called every minute. Update sigma_open_table for this minute-of-day."""
        now_et = bar.timestamp_et
        minute_of_day = self._minute_of_day(now_et)
        today_open = bars_today[0].open  # First bar of session
        
        if today_open == 0:
            return
        
        move_open = abs(bar.close / today_open - 1)
        
        # Store in sigma_open_table keyed by minute-of-day
        if minute_of_day not in self.sigma_open_table:
            self.sigma_open_table[minute_of_day] = []
        self.sigma_open_table[minute_of_day].append(move_open)
        
        # Keep only last 30 days per minute to prevent memory bloat
        if len(self.sigma_open_table[minute_of_day]) > 30:
            self.sigma_open_table[minute_of_day] = self.sigma_open_table[minute_of_day][-30:]
```

### Noise Area managed exit logic (not entry logic)

Unlike breakout strategies where stop + target are fixed at entry, Noise Area uses **dynamic exits**. On every minute bar, check:

```python
def check_exit(position, current_price, UB_now, LB_now, vwap_now, time_et):
    """Returns True if position should exit."""
    
    # 1. EoD flat
    if time_et >= position.eod_flat_time:
        return True, "eod_flat"
    
    # 2. Hard stop (disaster protection — 2% account)
    if position.pnl_pct <= -0.02:
        return True, "hard_stop_2pct"
    
    # 3. Signal flip — price returned to noise area
    if position.direction == "LONG":
        if current_price < UB_now or current_price < vwap_now:
            return True, "signal_flip_long"
    elif position.direction == "SHORT":
        if current_price > LB_now or current_price > vwap_now:
            return True, "signal_flip_short"
    
    return False, None
```

This is different from your other strategies' fixed stops. Your `base_bot.py` exit handler needs a `managed_exit` mode that calls `check_exit` on every bar for active Noise Area positions.

### Expected performance (realistic, friction-adjusted)

| Metric | Published (SPY) | NQ backtest (Quantitativo) | My realistic estimate for MNQ prod |
|---|---|---|---|
| Annualized return | 19.6% | 24.3% | 12-18% (post-friction, post-sim haircut) |
| Sharpe ratio | 1.33 | 1.67 | 0.6-1.0 live |
| Win rate | 50% | 38% | 40-50% |
| Profit factor | ~1.8 | ~2.25 | 1.3-1.6 |
| Max drawdown | ~10% | 24% | 15-25% |

Apply standard 40-60% sim-to-live degradation. The 38% WR on NQ is characteristic: few but larger winners.

---

## 🆕 PART B — ORB Strategy (carried forward from v3, tightened)

### Source
**Zarattini, Barbon & Aziz (2024)** — SSRN 4729284. Entry on 5-minute close outside 15-minute opening range.

### ORB spec — `strategies/orb.py`

```python
class OpeningRangeBreakout(BaseStrategy):
    """
    Opening Range Breakout — 15-min OR, 5-min close confirmation.
    
    Published results (QQQ, 2016-2023): 46% annualized, Sharpe 2.4
    NQ backtest (TradeThatSwing): 74% WR, PF 2.51, 12% max DD
    """
    
    PARAMS = {
        "or_duration_minutes": 15,
        "confirmation_close_minutes": 5,
        "max_entry_delay_minutes": 60,     # Cutoff at 10:30 ET
        "min_or_size_points": 10,          # Skip low-vol days
        "max_or_size_points": 60,          # Skip news-gap days
        "stop_placement": "opposite_or_side",
        "max_stop_points": 25,             # Hard cap = $50 on MNQ
        "target_structure": "partial_1R_runner",
        "partial_target_r": 1.0,
        "trail_method": "chandelier_3atr",
        "eod_flat_time_et": "15:55",
        "prod_eod_flat_time_et": "10:55",
        "one_per_day": True,               # Max 1 trade per day
    }
```

Full spec detailed in v3 document. Implementation unchanged.

---

## 🆕 PART C — Complete 8-Strategy Lineup with Order Type Matrix

| # | Strategy | Entry Order | Stop | Target | Edge Evidence |
|---|---|---|---|---|---|
| 1 | trend_following_pullback | LIMIT at pullback | STOP_MARKET | Partial + Chandelier | Shannon (case studies) |
| 2 | bias_momentum_v2 | LIMIT or STOP_MARKET hybrid | STOP_MARKET | Partial + Chandelier | Mixed (composite) |
| 3 | vwap_pullback | LIMIT at 1σ band | STOP_MARKET | LIMIT at VWAP | Mean-reversion research |
| 4 | compression_breakout_15m | STOP_MARKET at range | STOP_MARKET | Partial + Chandelier | Squeeze breakouts |
| 5 | compression_breakout_30m | STOP_MARKET at range | STOP_MARKET | Partial + Chandelier | Squeeze breakouts |
| 6 | spring_setup | LIMIT at test level | STOP_MARKET | LIMIT | Wyckoff (discretionary) |
| 7 | **orb (new)** | **STOP_MARKET** above/below OR | **STOP_MARKET** | Partial 1R + trail | **Zarattini 2024 ORB paper** |
| 8 | **noise_area (new)** | **LIMIT** at current price | **STOP_MARKET** (disaster only) | **Dynamic exit** on signal flip | **Zarattini 2024 "Beat the Market" paper** |

**Universal rules:**
- All stop losses = STOP_MARKET (execution certainty over price precision)
- All take-profit targets = LIMIT (price precision over fill certainty)
- All entries atomic via bracket order submission (server-side stop+target attach on fill)

---

## 📅 Updated Week 1 (April 20-26)

### Monday April 20 — P5b Fix + Bracket Orders
Same as v3. Priority remains: STOPMARKET fix + atomic bracket order support + per-strategy entry_type wiring.

### Tuesday April 21 — Strategy → Order Type Wiring
Same as v3, now covering 8 strategies.

### Wednesday April 22 — Economic calendar + news blackout
Same as v3. Finnhub free tier, ±2 min blackout on Tier-1 releases.

### Thursday April 23 — Build ORB + Noise Area
- [ ] Create `strategies/orb.py` per spec (see v3 for full code)
- [ ] Create `strategies/noise_area.py` per spec (see Part A above)
- [ ] **Critical for Noise Area:** build the `sigma_open_table` with 14 days of warmup data
  - Run tick_replayer on last 14 sessions of MNQ data to populate the table
  - Verify rolling means match expected magnitudes (sigma_open ranges from ~0.001 at 9:31 ET to ~0.008 at 15:30 ET on SPY; NQ will be proportionally larger)
- [ ] Unit tests for both strategies
- [ ] Register both in `lab_bot.py` — lab uses full 9:30-16:00 ET window; `prod_bot.py` uses 9:30-11:00 ET

### Friday April 24 — Monitoring + kill switches
Same as v3. Telegram alerts, watchdog, CrossTrade NAM evaluation.

### Saturday April 25 — 8-strategy dry run
- [ ] All 8 strategies load in lab_bot
- [ ] Replay recent trending day — verify ORB fires correctly
- [ ] Replay recent choppy day — verify Noise Area fires 0-2 times
- [ ] Replay recent volatile day — verify Noise Area fires 3+ times with multiple entries
- [ ] Dashboard shows all 8 strategies per-strategy metrics

### Sunday April 26 — Live session prep
Same as v3.

### Week 1 go/no-go criteria
- ✅ STOPMARKET + atomic bracket orders working
- ✅ All 8 strategies running without crashes
- ✅ Correct entry order type per strategy matrix
- ✅ Noise Area `sigma_open_table` populated with 14+ days history
- ✅ News blackout functional
- ✅ ORB + Noise Area both firing on test replays

---

## 📅 Updated Week 2 (April 27 – May 3): 8-strategy parallel sim

### Expected trade frequency per strategy (daily)

| Strategy | Expected/day | If 0 trades/day for 3 days | If 5+ trades/day |
|---|---|---|---|
| trend_following_pullback | 0-2 | Check HTF filter | Check pullback depth |
| bias_momentum_v2 | 0-1 | Check regime gate | Likely not filtering enough |
| vwap_pullback | 0-3 | Check AVWAP anchor | Band threshold too loose |
| compression_breakout_15m | 0-1 | Squeeze threshold | Range filter too loose |
| compression_breakout_30m | 0-1 | Squeeze threshold | Range filter too loose |
| spring_setup | 0-1 | Test detection too strict | False-positive tests |
| **orb** | **0-1 (max)** | **OR size filters too tight** | **Bug — should cap at 1** |
| **noise_area (prod)** | **0-2** | **Insufficient history or narrow session** | **Check dual VWAP condition** |
| **noise_area (lab, full day)** | **0-4** | **Narrow conditions that day** | **Check 30-min signal lock** |

### Weekend of May 2-3 — FIRST TRIAGE (updated for 8 strategies)

**Special triage considerations:**

- **ORB specifically:** Academic pedigree means minimum 20 trades before any disable. Strong priors that this works.
- **Noise Area specifically:**
  - Compare lab (full-day) vs prod (90-min) performance. If lab is strongly positive but prod is flat, that tells you most Noise Area edge happens AFTER 11:00 ET — consider extending prod session.
  - 38% WR on NQ is NORMAL. Do not interpret low WR as failure. Check PF and payoff ratio.
- **Spring_setup + bias_momentum_v2:** No strong published edge backing. Most likely to disable after first triage if underperforming.

---

## 📅 Weeks 3-6 (carried from v3)

No structural changes. The refined lineup at end of Week 2 feeds into Weeks 3-6.

---

## 📊 Strategy priority expectations (updated for 8 strategies)

| Tier | Strategy | Evidence Strength | Expected Survivor Probability |
|---|---|---|---|
| 🥇 **Tier 1** | **noise_area** | Strongest (17-year peer-reviewed study) | Very high |
| 🥇 **Tier 1** | **orb** | Strong (Zarattini 2024 ORB paper) | Very high |
| 🥈 **Tier 2** | compression_breakout_30m | Strong (published squeeze research) | High |
| 🥈 **Tier 2** | trend_following_pullback | Moderate (Shannon methodology) | Medium |
| 🥈 **Tier 2** | vwap_pullback | Moderate (mean-reversion research) | Medium |
| 🥉 **Tier 3** | bias_momentum_v2 | Unclear (composite) | Low-Medium |
| 🥉 **Tier 3** | compression_breakout_15m | Potentially noisy | Low-Medium |
| ⚫ **Tier 4** | spring_setup | Low (Wyckoff discretionary) | Low |

**My strongest prior:** At end of Week 6, the surviving lineup will be **ORB + Noise Area + compression_breakout_30m + one other**. The other four are likely to be triaged out or significantly tuned.

---

## 📊 Updated Week 6 success metrics

| Metric | Realistic Week 6 target |
|---|---|
| Top-3 strategies by PF | Noise Area, ORB, compression_breakout_30m |
| ORB specifically | 10-20 trades, PF 1.2+, WR 45-60% |
| Noise Area specifically (prod) | 10-30 trades, PF 1.2+, WR 38-50% |
| Overall PF (live) | 1.1-1.4 |
| Max drawdown | < 15% of equity |

---

## 📚 Key references

- **Zarattini, Barbon, Aziz (2024)** — ORB paper. SSRN 4729284.
- **Zarattini, Aziz, Barbon (2024)** — Noise Area / Beat the Market paper. SSRN 4824172. Swiss Finance Institute Research Paper 24-97.
- **Maróy (2025)** — Improvements to Intraday Momentum with VWAP exits (Sharpe 3.0+). SSRN 5095349.
- **Gao, Han, Li, Zhou (2018)** — Market Intraday Momentum. Journal of Financial Economics 129(2). [Not used in Phoenix due to session mismatch, but foundational.]
- **Baltussen, Da, Lammers, Martens (2021)** — Hedging Demand and Market Intraday Momentum. JFE 142. [Foundation for gamma regime rules.]
- **Concretum Group Python reference** — concretumgroup.com
- **Quantitativo blog** — NQ/ES implementation of Noise Area with friction costs
- **Kevin Davey** — Building Winning Algorithmic Trading Systems (Wiley 2014). Strategy Factory framework.
- **mlfinlab** (Hudson & Thames) — AFML Python implementation for Week 3+ validation

---

**Document version:** 4.0 (April 19, 2026)
**Key additions from v3:** Complete Noise Area spec with Python pseudocode, 8-strategy lineup, lab/prod session split for Noise Area, expected NQ performance envelope
**Strategy count:** 8 (was 7 in v3, 6 in v2)
**Status:** Ready for Monday April 20 execution

🚀 Let's build this thing.
