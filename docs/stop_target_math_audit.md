# Stop/Target Math Audit — Exit Sprint S1 (2026-04-21)

Read-only walkthrough of every strategy's entry/stop/target math. Each
strategy's formula is extracted verbatim from `_build_signal()` /
`evaluate()`. The PASS/FAIL sanity check asks two questions:

  1. Geometry: for LONG, `stop < entry < target`; for SHORT mirror.
  2. Stop distance: between 5 and 200 MNQ ticks.

---

## Jennifer's flagged trades — root causes

**bias_momentum "600-point stop" (trade 92f7adad, 24dfff54):**
Not a stop bug. The 27434 number is the **target** in the trade log
(`limit=26834.5 stop=26804.25 target=27434.5`). Stop distance is 30.25
pts (121t) on the correct side of entry. The 600-point figure is
`target - entry`, driven by `base_bot.py` lines 1913–1919 that force
`_RIDER_STRATEGIES = {"bias_momentum", "dom_pullback"}` to `target_rr=20.0`
unconditionally. With `stop_ticks=120` (clamped to `max_stop_ticks`) and
`tick=0.25` and `target_rr=20`, the bracket target lands at entry ± 600 pts.
That is intentional-but-extreme: managed exits (stall detector, reversal
detector) are supposed to take the trade out long before the OCO target
is hit. The OCO target is a "safety net", not a goal. Still worth
flagging to Jennifer — 20:1 is an absurd bracket RR.

**noise_area `target_rr=0`:**
Not a commission leak as worded. `noise_area._evaluate` sets
`target_rr=0.0` **and** `target_price=None` deliberately, because it
uses a managed exit (`exit_trigger="price_returns_inside_noise_area"`).
The real bug is in `base_bot._execute_trade` at lines 2370–2375: when
`signal.target_price is None` it falls through to the formula path and
computes `target = entry + stop_ticks * tick * target_rr`. With
`target_rr=0`, **target = entry**. Line 2644 then attaches an OCO with
target *at entry price* → every fill's TP leg sits at the entry price
and can trigger immediately. Fix: in base_bot, if `signal.target_price
is None` and `target_rr == 0`, the trade is managed-exit — do not
compute a formula target, and do not attach a TP leg in the OCO.

---

## Per-strategy audit

### bias_momentum (`strategies/bias_momentum.py`)

- **Entry:** `price = market['close']` (LIMIT with 1-tick offset unless
  overridden).
- **Stop:** `strategies/_nq_stop.compute_atr_stop(...)` — anchored to
  last 5m bar wick ± `stop_atr_mult × atr_5m`, clamped
  `[min_stop_ticks=40, max_stop_ticks=120]`. Fallback 64t if ATR unavailable.
- **Target:** `target_rr=5.0` in config; **but** base_bot overrides to
  20.0 in `_RIDER_STRATEGIES` block.
- Typical stop distance: 40–120 ticks (10–30 pts). **PASS** geometry,
  **PASS** distance.

### bias_momentum_v2
File not present.

### spring_setup (`strategies/spring_setup.py`)
- Entry: current price.
- Stop (ATR path): `last_bar.low/high ± atr_stop_multiplier × atr_5m`,
  clamped `[40, 120]t`.
- Stop (fallback): min/max of last 2 bars' low/high ± `structure_buffer_ticks=2`.
- Target: `target_rr=1.5`.
- Refuses signal if `stop_distance <= 0` (price past wick).
- **PASS**.

### vwap_pullback (`strategies/vwap_pullback.py`)
- Entry: current price.
- Stop: `_nq_stop.compute_atr_stop`, same clamps.
- Target: `target_rr=20.0` config; base_bot's rider override does not
  apply (not in _RIDER_STRATEGIES), so RR stays from config. With the
  day-classifier override also possible.
- **PASS**.

### vwap_band_pullback (`strategies/vwap_band_pullback.py`)
- Entry: bar close on the bounce bar.
- Stop: `lower_2sigma - 0.5×ATR` (LONG) / `upper_2sigma + 0.5×ATR` (SHORT).
- Ceiling guard: if `stop_ticks > max_stop_ticks=120` → SKIP. Min clamp
  to 40t (reprices stop and target together).
- Target: `entry ± stop_distance × target_rr (2.0)`.
- **PASS**.

### compression_breakout (`strategies/compression_breakout.py`)
- Entry: STOPMARKET at `squeeze_high + 1t` (LONG) / `squeeze_low - 1t`.
- Stop: `stop_ticks = int(current_atr × stop_atr_mult=1.5 / tick)`,
  clamped `[min_stop_ticks=40, max_stop_ticks=120]`. (Note: stop_price
  not computed in the strategy — base_bot derives it from stop_ticks.
  There is no explicit `signal.stop_price`.)
- Target: `target_rr=5.0`, formula-derived in base_bot from stop_ticks.
- **PASS** — formula gives stop on correct side; rider override does
  not apply.

### noise_area (`strategies/noise_area.py`)
- Entry: LIMIT at `price ± 1t` (wiggle into the breakout).
- Stop: `lb - 2t` (LONG) / `ub + 2t` (SHORT); `stop_ticks = (entry -
  stop) / tick`, floored at 4. No NQ clamp, but managed-exit
  (`uses_managed_exit=True`) so risk_manager substitutes a risk-reference
  stop for sizing.
- Target: `target_price=None`, `target_rr=0.0` — managed exit.
- **FAIL (base_bot-side bug)**: base_bot.`_execute_trade` then computes
  `target = entry + stop_ticks × tick × 0 = entry` and attaches OCO.
  Fix: base_bot must skip the TP leg when `target_price is None`.

### dom_pullback (`strategies/dom_pullback.py`)
- Entry: current price.
- Stop: `_nq_stop.compute_atr_stop`, clamps `[40, 120]t`.
- Target: `target_rr=2.5` config, but base_bot rider override kicks in
  → `target_rr=20.0`. Same 600-pt bracket phenomenon as bias_momentum.
- **PASS** geometry/distance; flagged for RR sanity.

### ib_breakout (`strategies/ib_breakout.py`)
- Entry: current price on 1m close past IB band.
- Stop: opposite IB extreme (or IB midpoint if `stop_at_ib_midpoint`).
- Ceiling: if `stop_ticks > max_stop_ticks=120` → SKIP signal.
- Target: `ib_high/low ± ib_width × target_extension=1.5`. Target_rr
  computed from actual distances, not hard-coded.
- **PASS**.

### orb (`strategies/orb.py`)
- Entry: STOPMARKET at `or_high + 1t` / `or_low - 1t`.
- Stop: opposite OR side ± `stop_buffer_ticks=2`.
- Stop-distance cap: `max_stop_points=25` (100 ticks).
- Target: `entry ± stop_distance × target_rr=2.0`.
- Managed exit with chandelier trail; `scale_out_rr=1.0` partial.
- **PASS**.

### opening_session (`strategies/opening_session.py`) — all 6 sub-strategies
All run through `_apply_universal_stop_clamp` with
`min_stop_ticks=40`, `max_stop_ticks=100`. If structural distance >
max → SKIP. If < min → synthesize stop at `entry ± min_ticks × tick`.
Targets are scenario-specific (pivot_pp, POC, R1/S1, etc.) and always
land on the correct side of entry by construction.
- **Open Drive**: T1=pivot_pp, stop=5-min OR midpoint. **PASS**.
- **Open Test Drive**: T1=POC (LONG) or min(POC,VAL) (SHORT),
  stop=5-min OR extreme + buffer. **PASS**.
- **Open Auction In**: T1=prior-day POC, stop=IB extreme + 8t.
  **PASS**.
- **Open Auction Out**: T1 is R1/S1 (acceptance) or POC (rejection);
  stop is prior-day extreme or RTH open + buffer. **PASS**.
- **Premarket Breakout**: T1=pivot_pp, stop=PMH/PML + 8t. **PASS**.
- **ORB (15m)**: T1 at `0.50 × or_size` from entry, stop=opposite OR.
  **PASS**.

---

## Universal runtime guard

Added `_sanity_check_entry(...)` in `bots/base_bot.py` that runs in
`_execute_trade` just before OCO submission. It asserts LONG/SHORT
geometry and 5–200 tick distance. On failure: log `[STOP_SANITY_FAIL]`
CRITICAL, record rejection, and return early. No strategy path was
observed to fail the gate on current config — it is a defensive guard
against future regressions.

## Fix summary

1. base_bot: managed-exit path — when `signal.target_price is None`, do
   not synthesize a formula target and do not attach an OCO TP leg.
2. base_bot: `_sanity_check_entry` universal guard before OCO.
3. Tests: synthesize a signal per enabled strategy and assert sanity passes.
