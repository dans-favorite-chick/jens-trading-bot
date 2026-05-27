# BASE_BOT Decomposition Plan

**Audit Date:** 2026-05-24
**File:** bots/base_bot.py (5,951 lines)
**Classes:** 3 (_PolicyPosAdapter, _PolicyBarAdapter, BaseBot)
**Methods:** 57 total | **Instance State:** 70+ fields

---

## 1. INVENTORY — BaseBot Methods (57 total)

### Data/Enrichment (8)
_on_bar(), _enrich_market_with_gamma(), _session_levels_refresh_task(), _resolve_exit_pending_positions(), _reconcile_positions_from_nt8(), _resolve_reconciliation_scope()

### Strategy & Signal Dispatch (5)
_evaluate_strategies() [800 lines], _process_signal(), _on_trade_closed(), toggle_strategy(), module-level: should_reject_on_rsi_div(), should_suppress_trend_stall()

### Risk Gates & Position Management (7)
_enter_trade(), _scale_out_trade(), _exit_trade(), _move_nt8_stop(), _trail_stop(), _should_scale_out(), _sanity_check_entry()

### OIF Write Path (6)
_sink_submit_place(), _sink_submit_protect(), _sink_submit_exit(), _sink_submit_partial_exit(), _sink_submit_modify_stop(), module-level: _get_oif_sink(), recompute_phase13_target()

### Daily Flatten & Scheduling (5)
_daily_flatten_loop(), _watch_flatten_grace_window(), _emit_grace_end_warn_if_open(), _log_session_close_event(), _is_no_new_entries_window()

### WebSocket & Watchdog (3)
_connect_and_listen(), _ws_watchdog_loop(), _get_ws_send_fn()

### AI Agents & Analysis (3)
_run_council(), _run_debrief(), _decay_monitor_loop()

### Observability (6)
to_dict(), _menthorq_to_dict(), _cr_to_dict(), _handle_dashboard_command(), _heartbeat_loop(), _dashboard_loop()

### Core Runtime (2)
run(), _runtime_reconciliation_loop()

### Config (4)
__init__(), load_strategies(), set_profile(), update_runtime_params()

### News (1)
_news_scanner_loop()

---

## 2. INSTANCE STATE FIELDS (70+)

**Data/Enrichment:** aggregator, session, history, tape_reader, _aggregator_state_path

**Strategy Layer:** strategies, tracker, _last_rsi_divergence, _last_htf_confluence, _last_structural_bias, _last_footprint_signals, _last_chart_patterns_v1, _last_climax_warning, _last_sweep_event, _last_pinning_state, _last_opex_status, _last_es_confirmation, _last_vix_term

**Risk/Position:** risk, positions, trade_memory, trade_clustering, position_scaler, _flattener, _flatten_grace_logged_for

**AI Agents:** _council_result, _council_ran_today, _filter_verdict, _debrief_ran_today, _last_regime

**WebSocket/Execution:** _ws, _last_ws_message_time, _shutdown_reconciliation

**Shadow Detectors (Apr 2026):** swing_state_5m, volume_profile, reversal_detector, sweep_watcher, gamma_flip_detector, pinning_detector, footprint_1m, footprint_5m, decay_monitor, tca_tracker, circuit_breakers, simple_sizer

**Phase 6-8 Arsenal:** expectancy, no_trade_fp, regime_transitions, microstructure_filter, crowding_detector, counter_edge, execution_quality, rsi_divergence, htf_scanner, hmm_regime, smc, trade_rag, calendar_risk, playbook_mgr, intermarket, edge_miner, knowledge_rag, pandas_ta, chart_patterns, cot_feed

**Cockpit/Monitoring:** cockpit, equity_tracker, telegram_commands

**Trend Rider:** _stall_detector, _rider_active, _day_classifier, _price_bar_highs, _price_bar_lows

**CVD Detectors:** cvd_health, cvd_flip, cvd_div

**Status/Config:** bot_name, status, last_signal, last_rejection, _last_eval, _runtime_params, _shutdown_requested

---

## 3. CROSS-LAYER COUPLING (CRITICAL PAIN POINTS)

**Strategy → Execution:** _process_signal() calls _enter_trade() directly; _evaluate_strategies() DIRECTLY MUTATES self.positions

**Execution → OIF:** Multiple call sites — _enter_trade() → _sink_submit_place(); _scale_out_trade() → _sink_submit_partial_exit(); _exit_trade() → _sink_submit_exit()

**Data → All:** _on_bar() feeds 15+ detectors (300+ lines); _evaluate_strategies() reads aggregator directly

**Risk Gates Read Strategy State:** _enter_trade() reads strategy instance from self.strategies

**AI Agents (Pervasive):** _run_council() called from _process_signal(); _filter_verdict read in _enter_trade()

---

## 4. TEST COVERAGE

### Tests Importing BaseBot (13 files)
test_base_bot.py, test_rsi_div_hard_gate.py, test_trend_stall_grace.py, test_phase13_overrides.py, test_runtime_reconciliation.py, test_flatten_alignment_b84.py, test_close_position_verification.py, test_orb_chandelier.py, test_auto_retry_flatten.py, test_fast_abort_fix.py, test_alert_dedup_recovery_mode.py, test_alert_dedup_exit_timeout.py

### SEVERELY UNCOVERED (NO DIRECT TESTS)
_enrich_market_with_gamma(), _evaluate_strategies(), _process_signal(), _enter_trade(), _scale_out_trade(), _exit_trade(), _move_nt8_stop(), _trail_stop(), _session_levels_refresh_task(), _heartbeat_loop(), _news_scanner_loop(), _run_council(), _run_debrief(), _decay_monitor_loop(), _ws_watchdog_loop(), _dashboard_loop(), _on_bar() [300+ lines], _on_trade_closed(), ALL OIF sink wrappers

**Coverage:** ~15% methods have direct unit tests; 70% integration-only

---

## 5. DECOMPOSITION (11 NEW MODULES)

### STAGE 1: Low-Risk (Safe)
1. **_decay_monitor.py** (100L) — observational only — GREEN
2. **_ws_watchdog.py** (80L) — defensive only — GREEN
3. **_heartbeat_sender.py** (60L) — keep-alive only — GREEN

### STAGE 2: Medium-Risk (Needs Tests)
4. **_session_levels_refresher.py** (50L) — scheduler timing — YELLOW
5. **_daily_flatten.py** [refactor existing] — time-critical — YELLOW
6. **_market_enricher.py** (400L from _on_bar 300+) — detector integration — YELLOW

### STAGE 3: High-Risk (🛑 STOP — SIGN-OFF REQUIRED)
7. **_strategy_dispatch.py** (300L from _evaluate_strategies 800L) — RED
   - Largest method; tightly coupled to detectors & risk gates
   - REQUIRES: 1-week A/B validation on 1000+ trades
   
8. **_oif_emitter.py** (200L: all _sink_submit_* functions) — 🛑 RED CRITICAL
   - Execution backbone; silent failures if wrong
   - REQUIRES: 2-week PAIRED-RUN with byte-compare of EVERY OIF file
   
9. **_risk_gates.py** (250L from _enter_trade gating) — RED
   - Safety-critical; 9 independent gates + 4 multiplier paths
   - REQUIRES: 135+ unit tests (15 per gate) before sign-off
   
10. **_position_tracker.py** (150L from reconciliation + lifecycle) — RED
    - Canonical position state; corruption undetectable without external reconciliation
    - REQUIRES: daily reconciliation harness (real fills vs position manager)
    
11. **_signal_processor.py** (150L from _process_signal; depends on Stage 3) — RED
    - Decision hub; every gate & AI agent touches it
    - REQUIRES: Stage 3 completion first

### STAGE 4: Supporting (Post-Stage 3)
12. **_market_snapshot.py** (100L: aggregation + enrichment) — YELLOW
    - Data flow only; no decision logic

---

## 6. EXTRACTION SEQUENCE

**Stage 1-2 (PARALLEL):** 6d (low) + 16d (medium) = 22 days concurrent

**Stage 3 (SEQUENTIAL):** 49 days + 3 weeks validation
- _risk_gates.py: 14d + 2-week validation
- _oif_emitter.py: 21d + 2-week paired-run (CRITICAL)
- _strategy_dispatch.py: 14d + 1-week validation

**Stage 4 (POST-STAGE-3):** 29 days
- _position_tracker.py: 14d + ongoing reconciliation
- _signal_processor.py: 10d
- _market_snapshot.py: 5d

**GRAND TOTAL: ~100 days** (critical path: Stage 3-4 sequential with validation)

---

## 7. 🛑 STOP CONDITIONS (SIGN-OFF REQUIRED FOR STAGE 3)

1. **Test Harness Ready:** Unit tests for Stage 1-2; A/B paired-run harness for signal & OIF
2. **Validation Infrastructure:** Signal parity validator (1000+ trades); OIF byte-compare harness; position reconciliation dashboard
3. **Risk Assessment Sign-Off:** CTO (all 9 risk gates tested); Execution (OIF fills vs live NT8); Operations (position reconciliation passed)
4. **A/B Validation:**
   - Old vs new signal path: 1 week → MUST match 100%
   - Old vs new OIF path: 2 weeks → MUST match 100% on ACKs
   - Position tracking: ongoing → MUST reconcile daily
5. **Rollback Plan:** Feature flags for Stage 3 modules; automated revert trigger; on-call escalation

---

## 8. SUCCESS CRITERIA

| Metric | Current | Target |
|--------|---------|--------|
| base_bot.py lines | 5,951 | 1,500 |
| BaseBot methods | 57 | 12 |
| Unit test coverage | ~15% | 70% |
| Max method complexity | 120+ | 20 |
| Time to debug OIF bug | 3 days | 1 day |

---

## 9. CONCLUSION

**Executive Summary:**
- **Proposed:** 11 new modules; base_bot.py shrinks 5,951 → 1,500 lines
- **Stages:** 3 low-risk (6d), 3 medium-risk (16d), 5 high-risk (49d + 3wk validation)
- **Biggest Pain Points:**
  1. **Strategy dispatch** (_evaluate_strategies 800L) — tightly coupled to detectors & risk gates
  2. **OIF execution** (6 wrappers, 120L) — silent failures; REQUIRES byte-compare validation
  3. **Position state** (canonical; corruption undetectable without external reconciliation)
  4. **Signal processor** (every gate & AI agent touches; requires coordinated refactoring)

- **🛑 STOP FLAGS (Stage 3 only):**
  - OIF emitter: 2-week paired-run byte-compare before go-live
  - Risk gates: 135+ unit tests before sign-off
  - Strategy dispatch: 1-week A/B validation on 1000+ trades before sign-off

**Timeline:** Stage 1-2 (22 days) → Test setup (14 days) → Stage 3 (49 days + 3 weeks validation) → Stage 4 (29 days) = ~100 days total

---

**Generated:** 2026-05-24
**Audit Scope:** Read-only inventory of base_bot.py, decomposition planning, NO code modifications
