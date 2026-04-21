# Monday Readiness Report — 2026-04-17 through 2026-04-20

_Generated Sunday Session 3 of 3 at end of weekend rebuild._

## Bot state going into Monday 2026-04-20 open

### ✅ LIVE (behavior changed, takes effect Monday)

- **Telegram HTML notifier** — lab messages now deliver reliably (was 22/29 dropped on Markdown parsing errors)
- **MQBridge.cs deployed + running** — C:\temp\menthorq_levels.json writing 55 draw objects every 60s with today's real levels (HVL 25290, CR 26500, PS 24000, GEX 1-10)
- **BOM fix in menthorq_feed.py** — MQ bridge file reads with utf-8-sig now, zero-value bug eliminated
- **bias_momentum hotfix** — `price` + `vwap` variables properly scoped (was crashing every eval)
- **Git tag `v-pre-rebuild-2026-04-17`** — rollback baseline preserved
- **memory/ directory + hooks** — SessionStart auto-loads context, SessionEnd auto-writeback + commit
- **Contract rollover watch** — auto-detects MNQM6 → MNQU6 rollover 8 trading days before June 19 expiration
- **Emergency halt tool** — `python tools/emergency_halt.py` creates .HALT marker, circuit breakers detect within ~5s

### ⚠️ SHADOW (built but not wired into strategy gates yet)

All Saturday + Sunday modules exist, import cleanly, unit-tested, available via dashboard API. But strategies continue using old `tf_bias` until WFO validation approves cutover (minimum 2 weeks live observation):

- core/simple_sizing.py (Kelly-free position sizing for $300 account)
- core/contract_rollover.py
- core/swing_detector.py (ATR-ZigZag pivots)
- core/volume_profile.py (POC/HVN/LVN/VAH/VAL + TPO-lite)
- core/reversal_detector.py (climax warnings + secondary-test entries, HARD RULE enforced)
- core/liquidity_sweep.py (failed-BOS reclassification)
- core/strategy_decay_monitor.py (observe mode 2 weeks)
- core/tca_tracker.py (baseline building)
- core/circuit_breakers.py (observe mode 2 weeks)
- core/chart_patterns_v1.py (wrapper around existing detector + context weighting)
- core/vix_term_structure.py (CBOE-ready, yfinance fallback)
- core/gamma_flip_detector.py (skeleton + full detection logic)
- core/session_tagger.py (6 session buckets for lab 24/7 analysis)
- core/pinning_detector.py (0DTE pin risk last 90 min RTH)
- core/opex_calendar.py (3rd Friday detection + triple witching handling)
- core/es_confirmation.py (NQ vs ES gamma alignment, manual file-based)
- core/footprint_patterns.py (stacked imbalance, absorption, exhaustion, delta div)
- bridge/footprint_builder.py (aggressor-classified per-bar footprint)
- core/structural_bias.py (composite engine integrating ALL of the above)
- memory/procedural/ YAMLs (small_account_config, regime_matrix, regime_params, targets)

### 🚫 NOT LIVE

- **LIVE_TRADING=False** — prod bot stays in Sim101 account until user's real account reaches $2,000
- No new strategies gated by structural_bias (dual-write only)
- No auto-demotion by decay monitor (observe mode 2 weeks)
- No auto-halt by circuit breakers (observe mode 2 weeks)
- Kelly sizing — intentionally not built; waiting on account ≥ $1500

## WFO baseline (placeholder strategy, for reference)

Run against 5 clean days of history (2026-04-13, 14, 15, 16, 17):

| Metric | Value |
|---|---|
| Trades | 18 |
| Win rate | 50% |
| Profit factor | 1.10 |
| Sharpe | 0.039 |
| Sortino | 0.083 |
| Break-even WR (given R:R) | 47.6% |
| Monte Carlo risk of ruin (2000 iterations) | 10.7% |
| OOS Sharpe | -0.109 (degraded vs in-sample) |

**Interpretation:** The placeholder EMA-crossover strategy is a baseline for the harness itself, NOT a real strategy. OOS degradation is the harness correctly identifying an overfit. Real strategies will replace this on April 25 validation session.

## Monday pre-open checklist

- [ ] 07:32 CDT: MenthorQ morning refresh scheduled task fires. Paste today's MQ analysis (GEX, levels, Q-Score).
- [ ] Verify MQBridge still running: `C:\temp\menthorq_levels.json` timestamp updated within last 2 minutes.
- [ ] Verify bots UP: `tail logs/watchdog.log` shows prod:UP and lab:UP.
- [ ] Verify zero errors since startup: `grep ERROR logs/prod_bot_stdout.log | tail`.
- [ ] Verify LIVE_TRADING=False in config/settings.py (should be False).
- [ ] 08:15 CDT: verify bias_momentum ran at least one evaluation cleanly.
- [ ] 08:30 CDT: primary window opens, bot begins trading in Sim101.

## Known issues going into Monday

See `memory/context/KNOWN_ISSUES.md` for full details. Headlines:
- NT8 trade arrow display missing — diagnosed, non-blocking. Try "Show executions" chart toggle as first fix.
- Level 2 depth only summary forwarded — sufficient for v1 footprint. Per-level enhancement deferred.
- CalendarRisk fetch fails, non-blocking. News blackout currently ineffective.
- COTFeed URL encoding error, non-blocking. COT data not flowing.

## What the next Claude session should do first

1. Read `memory/context/CURRENT_STATE.md` (auto-loaded via hook)
2. Read this file (MONDAY_READINESS.md)
3. Read `memory/context/RECENT_CHANGES.md` for dated log of what was built
4. Verify no regressions: `python -m unittest tests.test_new_modules` should show 41+ passing
5. Verify bots healthy, MQ flowing
6. Ask user what they want to tackle next

## April 25 validation review agenda

See scheduled task `phoenix-strategy-layer-apr25`. After 5 trading days of live shadow data:
- Compare structural_bias vs tf_bias (how often agreed, accuracy vs actual moves)
- Per-strategy P&L review, demotion candidates
- Reflector agent introduction (propose-only)
- Kelly activation gate (if account ≥ $1500)
- Blind spot discovery from live operation

## Scope that was intentionally NOT built this weekend

Documented in OPEN_QUESTIONS.md:
- NT8 Order Flow+ C# enhancement (v1 uses bridge-side footprint = ~85% of value)
- Measured-move target projection for chart patterns (v2, 2 weeks)
- Reflector agent full implementation (April 25)
- Regime-tagged memory buckets (bootstrap problem — need 2+ weeks data)
- User dispute button (needs reflector first)
- UOA / live options flow integration (deferred 4-6 weeks)
- SQLite migration of trade_memory.json (non-blocking)
- Weekly / multi-day context module
