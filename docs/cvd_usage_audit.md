# CVD Usage Audit — 2026-05-13 (#12 of roadmap)

_Cross-strategy inventory of how Cumulative Volume Delta is read,
filtered, and exited on. Single source-of-truth so when the operator
asks "where is CVD used?" the answer is in one place, not 7 files._

## TL;DR

| CVD use | Strategies that use it |
|---|---|
| **Sign confirmation** (cvd > 0 long, < 0 short) | `bias_momentum`, `vwap_pullback`, `spring_setup`, `high_precision`, `ib_breakout` |
| **Hard sign gate** (block trade if CVD opposes) | `ib_breakout` (require_cvd_confirm), `bias_momentum` (chop regimes only) |
| **Trend-health veto** (price+CVD slope agreement, 6-bar window) | `bias_momentum` |
| **Mid-trade per-bar flip exit** (N consecutive flipped bars) | `base_bot` (bot-level, all strategies) |
| **Classic swing-pivot divergence** (bear/bull at confirmed pivots) | `base_bot` (bot-level, all strategies) |
| **Multi-bar regular divergence** (price + CVD opposite swings) | `footprint_cvd_reversal` (entry confluence) |
| **Single-bar delta divergence** (close vs delta opposite) | `footprint_cvd_reversal` (entry confluence) |

## 1. Bot-level CVD machinery (post-2026-05-13 #14)

`bots/base_bot.py` instantiates three detector classes in `__init__`,
fed from snapshots in `_on_bar()` and read by all strategies via the
enriched `market` dict:

| Detector | Class | Used as | Config knobs |
|---|---|---|---|
| `self.cvd_health` | `core.cvd_trend_health.CVDTrendHealth` | Entry filter (veto) — exposed to strategies via `market["cvd_health"]` / `market["cvd_health_short"]` | `lookback_bars=6`, `veto_threshold=-0.3` |
| `self.cvd_flip` | `core.cvd_bar_flip.BarDeltaFlipDetector` | Mid-trade exit signal — checked in the position loop post-grace | `lookback=5` (config: `cvd_flip_min_consecutive`) |
| `self.cvd_div` | `core.cvd_swing_divergence.SwingDivergenceDetector` | Mid-trade exit signal — bull/bear divs at confirmed swing pivots | depends on swing definition |

Grace window: the tick-loop suppresses both `cvd_flip` and `cvd_div`
exits during the first N seconds after fill (`trend_stall_grace_s`)
so a single noisy bar doesn't bounce a fresh position.

## 2. Per-strategy usage (entry side)

### bias_momentum
- Reads `market["cvd"]` (session cumulative).
- **Chop-regime gate** (lines ~279-291): in `LATE_AFTERNOON`,
  `CLOSE_CHOP`, `AFTERNOON_CHOP`, blocks trades whose CVD sign opposes
  direction.
- **Confluence score**: ±1 confluence point for sign-aligned CVD.
- **CVD trend-health veto** (#9 of CVD detectors, 2026-05-13): reads
  `market["cvd_health"]` (LONG) or `market["cvd_health_short"]` (SHORT)
  and skips on `veto=True`. Toggleable via `cvd_health_enabled` config.

### footprint_cvd_reversal
- Uses **multi-bar regular divergence** + **single-bar delta divergence**
  as one of its 4 confluence components (`_score_cvd_divergence`).
- Post-#14 (2026-05-13): emits `cvd_div_type` (multi_bar | single_bar |
  both | none) + `cvd_div_magnitude` in trade metadata + reason field
  for post-hoc grouping.

### vwap_pullback
- Sign confirmation only: ±1 confluence for sign-aligned CVD.
- No hard gate. No divergence logic.

### spring_setup
- Sign confirmation as a "delta_confirmed" boolean in the reason string.
- No hard gate. Single-bar only.

### ib_breakout
- **Hard CVD gate** via `require_cvd_confirm` config (default True).
  Forensic origin: 2026-04-14 10:05 SHORT with CVD=+6.05M → -164t loss.
- Sign confirmation as additional confluence.

### high_precision (RETIRED 2026-05-13)
- Sign confirmation only. No hard gate.

### dom_pullback, vwap_band_pullback, vwap_band_reversion, orb, noise_area, compression_breakout
- **No CVD usage** in strategy code. Inherit only the bot-level
  cvd_flip / cvd_div mid-trade exits.

## 3. Gaps surfaced by this audit

1. **vwap_pullback** and **spring_setup** use CVD sign only as a
   "+1 confluence" weak signal. Could mirror `ib_breakout`'s hard-gate
   pattern if forensic data shows opposed-CVD entries are predictably
   losing (worth investigating once #2 R-multiples accumulate).

2. **vwap_band_pullback** is a mean-reversion entry but does NOT
   consult CVD at all. A "CVD-divergent" reversion (price extends,
   CVD doesn't) is the textbook high-quality reversion setup —
   missing this gate is a known TODO that #19 (flow-reversal exit)
   touches.

3. **The CVD trend-health veto is only wired into bias_momentum.**
   The other ATR-anchored entry strategies (vwap_pullback,
   dom_pullback) could use the same gate. Roadmap candidate.

4. **No strategy reads `cvd_div_type` post-trade.** Once #14's
   instrumentation accumulates 30+ trades, a `--groupby cvd_div_type`
   pass in `validation_tracker` would tell us which div type carries
   the edge.

## 4. Source pointers (for direct navigation)

| Concept | File:line |
|---|---|
| `cvd_health` instantiation | `bots/base_bot.py:779` |
| `cvd_flip` instantiation | `bots/base_bot.py:780` |
| `cvd_div` instantiation | `bots/base_bot.py:781` |
| Tick-loop exit checks | `bots/base_bot.py:~1950-1980` |
| bias_momentum chop CVD gate | `strategies/bias_momentum.py:279-291` |
| bias_momentum cvd_health veto | `strategies/bias_momentum.py:777-790` |
| ib_breakout hard gate | `strategies/ib_breakout.py:160-175` |
| footprint CVD divergence | `strategies/footprint_cvd_reversal.py:352-405` |
| footprint cvd_div instrumentation (#14) | `strategies/footprint_cvd_reversal.py:1453-1485` |
| CVDTrendHealth class | `core/cvd_trend_health.py` |
| BarDeltaFlipDetector class | `core/cvd_bar_flip.py` |
| SwingDivergenceDetector class | `core/cvd_swing_divergence.py` |

## 5. Last verified

- 2026-05-13 — file generated as part of roadmap #12. Re-run the
  `cvd|CVD` grep across `strategies/` and `bots/` to refresh.
