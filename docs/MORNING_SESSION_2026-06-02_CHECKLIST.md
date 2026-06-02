# Morning Session Checklist — 2026-06-02

_Overnight master run (Phases A → J) completed on `weekly-evolution/2026-05-24`._

## 1. Sim live-mode confirmation (the only thing that matters before market open)

| Setting | State | Source |
|---|---|---|
| `LIVE_TRADING` | **False** | `config/settings.py` |
| `LIVE_STRATEGY_ALLOWLIST` | `("bias_momentum",)` | `config/settings.py` |
| `FREEZE_ACTIVE` | True (unchanged tonight) | `config/strategies.py:52` |
| Branch | `weekly-evolution/2026-05-24` | pushed to origin |

No live trades will fire. Sim mode only. All 11 enabled strategies
will trade in sim regardless of the LIVE_STRATEGY_ALLOWLIST (the
allowlist only gates LIVE).

## 2. Strategies that WILL trade in sim today (11 total)

| Strategy | validated | max_stop | target_rr | session_blocks | Tonight's change |
|---|---|---|---|---|---|
| **a_asian_continuation** | True | 14 | 2.0 | – | – |
| **bias_momentum** ⭐LIVE-CANARY | True | 200 (fallback 64) | 2.5 | 04:00-04:59 | – |
| **e_multi_day_breakout** | True | 30 | **2.5** ← from 2.0 | – | **Phase B-1** |
| **es_nq_confluence** | False | – | – (target_ticks=96) | – | – |
| **g_inside_bar_breakout** | True | 30 | **3.0** ← from 2.0 | – | **Phase B-2** |
| **ib_breakout** | True | 200 | – | – | – |
| **nq_lsr** | False | 30 | – | – | – |
| **opening_session** | True | 200 | – | 11:00-11:59 | – |
| **orb_v2** | False | 60 | 2.0 | – | – |
| **raschke_baseline** | True | 40 | **3.5** ← from 2.0 | 08:00-09:59, 12:00-15:00 | **Phase B-3** |
| **vwap_band_pullback** | True | 200 | 2.0 | – | – |

Three target_rr edits land tonight (Phase B). Each is the trivial
single-line edit derived from the Oracle's v3 MFE-p90 / MAE-elbow
ratio analysis. The per-direction max_stop_ticks tightenings the
Oracle proposed are NOT shipped — they're queued for operator review
because the underlying knobs are global and per-direction values
need adjudication.

## 3. Strategies disabled (13 total)

| Strategy | Disabled by | Reason |
|---|---|---|
| spring_setup | Phase 2A (efc6d5e, OPERATOR-APPROVED 2026-06-01) | 5y: t=-7.77, max_dd=-$83,614, PF 0.88 |
| compression_breakout_v2 | Phase 2B (54edcf4) | 5y: t=-6.23, max_dd=-$3,465 |
| compression_breakout_micro | Phase 2B (b2ba160) | 5y: t=-6.14, max_dd=-$1,248 |
| vwap_pullback_v2 | Phase 2B (0ae8961) | 5y: t=-3.61, max_dd=-$18,981 |
| vwap_band_reversion | Phase 2B (6b60c50) | 5y: t=-3.75, max_dd=-$16,206 |
| orb_fade | Phase 2B (f4216e5) | 5y: WR=13.5%, PF=0.81 |
| noise_area | Pre-existing | Tied to harness contamination bug; post-fix re-backtest shows PF=1.07 / WR=44.6% but kept disabled pending operator review |
| big_move_signal | Pre-existing | sim-only, never validated |
| compression_breakout | Pre-existing | Pre-Phoenix legacy |
| footprint_cvd_reversal | Pre-existing | Lab-only |
| high_precision_only | Pre-existing | Pre-Phoenix legacy |
| orb | Pre-existing | Pre-Phoenix legacy |
| vwap_pullback | Pre-existing | Superseded by v2 (which itself just got killed) |

## 4. Dashboard new features

Two new tiles / endpoints landed tonight.

### Market State tile (Phase D)
- New section in Trading view, between Tape Reader and Signal Activity.
- Color-coded label across the six composite states (TRENDING_NORMAL
  = green, COMPRESSED = blue, CHOPPY = yellow, WHIPSAW_HIGH_VOL = red,
  TRENDING_HIGH_VOL = bright green, NEUTRAL = gray).
- 30-second auto-refresh.
- Source attribution (`live` | `warehouse_fallback` | `stub`) helps you
  see at a glance whether the bot is pushing live state yet or the tile
  is falling back to the warehouse backfill (will be `warehouse_fallback`
  today because base_bot doesn't push market_state until Monday).
- Observation only — no strategy reads this. No size or stop/target
  adjustments.

### Per-strategy / per-market-state PnL view (Phase D.5 partial)
- New endpoint `GET /api/market_state/per_strategy` aggregates
  trailing-30-day trades by `(strategy, entry_market_state)` and reports
  n / WR / PF / gross_w / gross_l.
- Until the trade_memory wiring lands (Monday item — protected file
  needs operator sign-off), every new trade has `entry_market_state`
  absent → bucketed as `(unknown)`. The endpoint still renders cleanly.

## 5. Bias_momentum sim-readiness verdict — READY

Full audit at `logs/oracle/research/2026-06-02_bias_momentum_sim_readiness.md`.
Highlights:
- No TODO / FIXME / SIM-ONLY markers in `strategies/bias_momentum.py` source
- 38 / 38 scoped pytests pass
- live_canary_gate posture satisfied (validated=True, enabled=True)
- Oracle 2026-06-01 v3 PF=1.28, OOS>IS, gate-clean
- Per-direction stop_fallback_ticks (Oracle: SHORT=11, LONG=17) NOT
  applied — queued for operator decision since the knob is global

## 6. Master Fix commits (yesterday, for reference)

| SHA | Phase | Subject |
|---|---|---|
| `1042b00` | 9 | Oracle max_tokens 4096→16384, token_budget 200K→600K, directive workflow |
| `275d6ac` | 8 | Market state classifier + 354,270-row backfill |
| `a1fbac0` | 7 | Verifier sign-symmetric leaf matching |
| `3a7e529` | 6 | MAE elbow + MFE percentile helpers |
| `7c78cbe` | 5 | Oracle schema injection + vocab + guards |
| `f7ed5e7` | 4 | allowed_directions infra |
| `f4d7675` | 3 | raschke_baseline hour-priority |
| `8aa5ec0` | 3 | opening_session Hour 11 block |
| `bfcc900` | 3 | bias_momentum Hour 4 block |
| `945f185` | 3 | e_multi_day_breakout window narrow 10-13 |
| `609a000` | 3 | a_asian_continuation window narrow 04-06 |
| `f4216e5` | 2B | orb_fade disable |
| `6b60c50` | 2B | vwap_band_reversion disable |
| `0ae8961` | 2B | vwap_pullback_v2 disable |
| `b2ba160` | 2B | compression_breakout_micro disable |
| `54edcf4` | 2B | compression_breakout_v2 disable |
| `efc6d5e` | 2A | spring_setup disable (OPERATOR-APPROVED) |
| `7de51ee` | 1.5B | Harness target-synthesis bug fix |
| (full list in `logs/oracle/research/2026-06-01_master_fix_report.md`) | | |

## 7. Overnight commits (this run's deliverables)

| SHA | Phase | Subject |
|---|---|---|
| `10ebca4` | F | strategy_load_audit tool + WILL_LOAD/FENCED report |
| `55a542b` | D.5 partial | Per-strategy/per-state PnL endpoint |
| `b255d09` | D | Market state dashboard tile |
| `9512271` | C3 | Wire strategy.check_exit into simulator (Fix B) |
| `832a845` | C1 | Archive contaminated noise_area + re-ingest fresh |
| `4222e07` | B closeout | Phase B disposition script for queue |
| `a662fbf` | B-3 | raschke_baseline target_rr 2.0 → 3.5 |
| `a208a7b` | B-2 | g_inside_bar_breakout target_rr 2.0 → 3.0 |
| `b406fb1` | B-1 | e_multi_day_breakout target_rr 2.0 → 2.5 |
| `a3dd69d` | B-pre | Test sync for token_budget and bias_momentum blocks |
| `8522d0a` | A | Transcribe 11 v3 narrative proposals to queue |

11 commits. All on `weekly-evolution/2026-05-24`. All pushed.

## 8. Open items for operator awareness

### Monday execution (designs ready)
- **Freeze lift workstream.** Full design at
  `docs/superpowers/plans/2026-06-08-freeze-lift-plan.md` (Phase H).
  ~2 days of effort (1 day to build reconciliation_harness.py + 0.5 day
  to validate on bias_momentum + 0.5 day operator review).
- **Scanner Stage 2 (binary regime gating).** Design at
  `docs/superpowers/plans/2026-06-08-scanner-wiring-plan.md` (Phase I).
  Stage 1 (observation tile + per-state breakdown) ships tonight; Stage
  2 (`allowed_market_states` schema add + gate in base_bot) is the
  Monday workstream.

### Items for operator decision
- 11-vs-7 strategy load roster mismatch — 4 extra loaders (ib_breakout,
  vwap_band_pullback, nq_lsr, orb_v2) intentional but flagged in
  `logs/oracle/research/2026-06-02_strategy_load_audit.md`. Trivial
  kill if you want telemetry focused on the 7 winners only.
- Per-direction stop tightenings the Oracle proposed remain queued in
  `logs/oracle/pending_changes.json`. The trivial bundled target_rr
  edits ship tonight; the global-vs-per-direction max_stop_ticks
  choices need operator picks.
- bias_momentum stop_fallback_ticks (LONG=17, SHORT=11 Oracle proposal)
  remains at global 64. **LIVE CANARY ALERT applies** — any
  bias_momentum stop geometry change requires fresh WFA + operator
  sign-off before going live.

### Items DEFERRED to Monday operator review (protected files)
- `core/trade_memory.py` — auto-stamp `entry_market_state` at record
  time. Phase D.5 partial ships the dashboard endpoint, but the field-
  write needs OPERATOR-APPROVED tag.

### Items DEFERRED indefinitely (out of overnight scope)
- `tests/test_adaptive.py` — 2 pre-existing failures + 4 errors. Pre-
  Master-Fix. Excluded from overnight pytest baseline.
- `tests/test_enriched_market_persistence.py::test_enter_trade_merges_enrichment_fields`
  — source-pattern test drifted out of date. Excluded from overnight
  pytest baseline.
- `allowed_directions` size-tilt mechanism — Phase 4 of Master Fix
  built the hard-gate path. Per-direction size tilt would need
  weighted-sizing scaffolding in core/risk_manager.py (protected). Out
  of overnight scope; would be a deliberate sprint.

## 9. How to start the system

Standard sequence (unchanged):

```
1. bridge_server (Python -m bridge.bridge_server)
2. prod_bot      (Python -m bots.prod_bot)
3. lab_bot       (Python -m bots.lab_bot) -- or sim_bot if you renamed
4. dashboard     (Python -m dashboard.server)  -> http://localhost:5000
```

Dashboard now shows the Market State tile in the Trading view between
Tape Reader and Signal Activity. The new endpoint
`/api/market_state/per_strategy` is wired but the per-strategy table
isn't surfaced in dashboard.html yet — visit the endpoint directly
to see today's accumulating data.

## 10. Total tests

**3160 passing** on the post-overnight head (excluding the 2 pre-existing
`test_adaptive.py` failures + 4 errors and the 1 drifted source-pattern
test in `test_enriched_market_persistence.py`).

## Final note

The bot is in a clean state for sim trading. Two design docs are ready
for Monday review. The per-state observation infrastructure is wired
and accumulating data. The Oracle's intended proposals are queued with
explicit dispositions. Sleep well.
