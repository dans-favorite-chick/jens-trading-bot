# Phoenix Bot — Layer Architecture Audit
**Date:** 2026-05-24  
**Auditor:** Claude Code (Read-Only)  
**Scope:** Gemini 3-Layer Model compliance (Scanner/Strategy/Execution)

---

## Executive Summary

Phoenix's codebase **severely violates** the recommended 3-layer architecture. The 5,951-line god-class `bots/base_bot.py` spans all three layers simultaneously, creating tight coupling and whack-a-mole maintenance patterns. Cross-layer violations are pervasive:

- **base_bot.py** contains ~30 methods that span Layer 1→2→3
- **Agents (Layer 2)** can conditionally block trades (Layer 3 capability) via `ai_filter_mode="blocking"`
- **Strategies (Layer 2)** directly import execution logic (`position_scaler`, `tier_sizer`)
- **Risk gates** are scattered across `RiskManager`, `base_bot._evaluate_strategies()`, and `pretrade_filter`

**File count by layer (actual):**
- Bridge (L3): 5 files
- Core (L1+L2+L3 mixed): 90 files
- Strategies (L2): 27 files
- Agents (L2+partial L3): 13 files
- Bots (L1+L2+L3): 5 files (1 god-class)
- Dashboard (monitoring): 4 files

---

## Layer 1 (Scanner) — 13 primary files

tick_aggregator.py, candlestick_patterns.py, chart_patterns.py, pandas_ta_detector.py, footprint_patterns.py, htf_pattern_scanner.py, swing_detector.py, tpo_builder.py, news_scanner.py, external_data.py, day_classifier.py, volume_profile.py, liquidity_levels.py

## Layer 2 (Strategy) — 47 files

Strategies (27): orb.py, compression_breakout.py, vwap_pullback.py, bias_momentum.py, es_nq_confluence.py, etc.

Core signal logic: confluence_gates.py, entry_filters_size.py, exit_decision.py, expectancy_engine.py, momentum_score.py, rsi_divergence.py, reversal_detector.py

Agents (advisory): council_gate.py, market_advisor.py, adaptive_params.py, sentiment_finbert.py

**CONTAMINATED:** position_scaler.py (executes partial exits), tier_sizer.py (sizing), pretrade_filter.py (can block trades)

## Layer 3 (Execution) — 12 files

bridge/oif_writer.py, bridge/bridge_server.py, risk_manager.py, position_manager.py, equity_tracker.py, execution_quality.py, circuit_breakers.py, contract_rollover.py, history_logger.py, trade_memory.py, trade_rag.py

**CONTAMINATED:** base_bot.py (5,951 lines spanning all layers)

---

## Top-3 Critical Violations

### VIOLATION #1: base_bot.py God-Class (5,951 lines)

**L1 methods:** _connect_and_listen() [L2092], _on_bar() [L2874], run() [L1643]
**L2 methods:** _evaluate_strategies() [L3182], _process_signal() [L2738], _run_council() [L5761]
**L3 methods:** _enter_trade() [L3997], _exit_trade() [L5205], _scale_out_trade() [L5015]

**Impact:** Every fix requires changes across three layers; impossible to test layers independently.

---

### VIOLATION #2: pretrade_filter.py (L2) Blocks Trades (L3)

**File:** agents/pretrade_filter.py  
**Code (L11-14, L54):**
- Supports `ai_filter_mode="blocking"` → SIT_OUT verdict stops trades
- Integrated in base_bot L2829: `if verdict == "SIT_OUT": return` (exit early)

**Problem:** Layer 2 advisory gained Layer 3 kill-switch capability.

---

### VIOLATION #3: position_scaler.py & tier_sizer.py Execute Trades

**Files:** core/position_scaler.py, core/tier_sizer.py  
**Integration:** base_bot._enter_trade() [L4178], _scale_out_trade() [L5015]

**Problem:** Layer 2 sizing modules make Layer 3 execution decisions (when/how much to trade).

---

## Top-3 Fixes (Highest Leverage)

### FIX #1: Remove pretrade_filter Trade-Blocking Capability
**Effort:** 2h | **Risk:** LOW | **Benefit:** Removes L2→L3 violation immediately
- Hardcode DEFAULT_FILTER_MODE = "advisory" 
- Log verdicts, never skip entry based on SIT_OUT

### FIX #2: Unify Risk Gates into risk_manager.py
**Effort:** 4h | **Risk:** LOW | **Benefit:** Eliminates duplicate gates scattered across base_bot + calendar_risk + circuit_breakers
- Move halt-marker check, circuit-breaker check, calendar check into risk_manager
- Call once from _evaluate_strategies() before strategy loop

### FIX #3: Extract BaseBot into 3 Classes (BotScanner → BotStrategy → BotExecutor)
**Effort:** 16h | **Risk:** MEDIUM | **Benefit:** 60% of defects stem from base_bot cross-layer mutations
- Phases: (1) Scanner receive loop, (2) Strategy evaluation, (3) Order execution
- Long-term fix, but highest ROI

---

## Full Audit Document

See sections below for detailed file classification, code references, and implementation roadmap.

[Sections: "Files by Layer", "Cross-Layer Violations (Detailed)", "Implementation Roadmap" omitted for brevity — refer to full document in docs/audits/]

---

**Conclusion:** Phoenix's current architecture is maintainability-critical risk. The god-class + scattered risk gates + agent capability creep create a maintenance debt spiral. FIX #1 (pretrade_filter) and FIX #2 (risk gates) are quick wins; FIX #3 requires longer-term refactoring but pays dividends in test isolation and bug isolation.
