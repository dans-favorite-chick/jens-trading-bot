# Phoenix Bot — Open Questions

_User follow-ups and architectural questions that haven't been resolved._
_New Claude sessions: these are things the user wants progress on._

_Last refreshed: 2026-04-25 EOD._

## 🔴 Active questions

### When does live trading flip on?

**Status:** Real live account at $300. Live trading PAUSED on prod
(stays Sim101). Bot will graduate when account reaches $2,000. Sim
bot continues 24/7 live-sim execution to build the trade dataset
needed for Phase C statistical validation.

### When can CPCV / DSR / PBO checkboxes turn green?

**Status (2026-04-25):** `weekly_evolution.py` enforces these as
unchecked in every commit body. We need ~200 sim trades per strategy
minimum to compute statistically meaningful CPCV folds + DSR p-value
+ PBO. Sim bot is now generating the dataset. Revisit when
`out/grades/` has 8+ weekly aggregates.

### Should `high_precision_only` be demoted?

**Status:** Question still open from April but the lab bot was
decommissioned 2026-04-21, so the source of the 18% WR data is gone.
Sim bot per-strategy P&L will surface this once enough trades land.
The new weekly_evolution routine will auto-flag any strategy with
≥2 consistent failures per week as a proposal candidate.

## 🟡 Operational questions for the next session

1. **Re-register the four scheduled tasks dropped by the 14:31 reboot.**
   Run all five `scripts/register_*.ps1` as Administrator. Verify with
   `Get-ScheduledTask -TaskName Phoenix*`.
2. **Verify Monday 06:30 morning_ritual** — look for
   `out/morning_ritual/2026-04-27.md`, confirm verdict is
   GREEN/AMBER (not RED), and confirm RED would have triggered
   immediate Telegram.
3. **Verify Monday 16:05 consolidated digest** — Telegram should
   contain morning_ritual snippet + post_session_debrief in ONE
   message.
4. **First floor-kill test** — manually push a strategy to -$500
   cumulative; validate halt + persistence + Telegram alert path.

## ✅ Resolved this weekend (2026-04-25)

### Weekend rebuild: Monday-ready?

**RESOLVED** — yes. 1,221 tests passing, 6 strategy fixes locked in
by 20 regression tests, defaults all SAFE, scheduled task lattice
ready to be re-registered after reboot.

### Will simple_sizing satisfy without Kelly?

**RESOLVED** — yes for now. Account at $300, simple_sizing.py active.
Kelly stays gated until account reaches $1,500. Sim bot uses fixed
$2,000 × 16 strategies, not Kelly.

### Should all 7 strategies stay active?

**SUPERSEDED** — Phase C runs 10 strategies on 16 dedicated Sim
accounts (some strategies route to multiple accounts for sub-flavors).
Per-strategy halts make concentration unnecessary; bad strategies
auto-disable themselves at -$500 cumulative.

### Reflector agent (propose-only daily debrief)

**RESOLVED via Phoenix Routines** — `tools/routines/post_session_debrief.py`
fills this role with deterministic verdicts + AI commentary appendix.

### Strategy concentration analysis

**SUPERSEDED** — `tools/routines/weekly_evolution.py` does this
automatically every Sunday. Aggregates week, flags consistent failures
(≥ max(2, n_sessions // 2)), seeds proposals, AI reviews them, opens
a `weekly-evolution/YYYY-MM-DD` branch (NEVER auto-pushed).

### Kelly sizing activation gate

**RESOLVED** — gate is account ≥ $1,500, hardcoded. Until then
small_account_config applies.

### 2-week shadow validation review

**SUPERSEDED** — replaced by the weekly_evolution routine + future
CPCV/DSR/PBO harness.

### bias_momentum_v2 promotion gate

**RESOLVED** — bias_momentum LONG + SHORT mirror with VCR=1.2 is
now the canonical implementation. v2 adapter debt cleared. Lock-in
regression test in `tests/test_lock_in_epic_v1/`.

### Finnhub blackout window (±2 vs ±5 min)

**KEEP ±5 min** — wider is strictly safer. Decision logged
2026-04-19, no action required. Finnhub real client now active.

### ORB clock anchor / stop cap

**DEFERRED** — ORB now ATR-adaptive per §4 fixes. Audit after 10+
live trades; not blocking.

### ANTHROPIC_API_KEY missing

**RESOLVED** — fixed 2026-04-21 via `load_dotenv override=True`.

## 🟢 Deferred to later weeks / months

- Context-aware candlestick scoring (v2)
- Triangle patterns + pattern target projection (v2)
- NT8 Order Flow+ volumetric bars (C# mod — dedicated session)
- Microstructure (tick rate, spread analysis, aggressor ratio deep)
- Cross-asset composite score (NQ/ES spread, DXY inverse, yield curve)
- CalendarRisk fetch fix + pre/post-event gates
- Regime-tagged memory buckets (need more data)
- User dispute button (needs reflector live first)
- UOA / options flow
- Level 2 tape-reader wired into strategies (footprint built, not yet gated)
- SQLite migration of trade_memory.json
- Weekly / multi-day context module
- Unified feature pool across strategies (Renaissance-style)
- NT8 SILENT_STALL auto-restart hook (currently watcher-only escalation)
- TradeMarker.cs custom indicator (NT8 trade arrow display)
- Phoenix-specific skills under `.claude/skills/` (§3.4 deferred)

## ❓ Questions for user at next morning check-in

1. Did the four `register_*.ps1` scripts re-register cleanly? Confirm
   `Get-ScheduledTask -TaskName Phoenix*` shows all six (PhoenixLearner,
   PhoenixGrading, PhoenixRiskGate, PhoenixMorningRitual,
   PhoenixPostSessionDebrief, PhoenixWeeklyEvolution).
2. Is MQBridge alive? Check `C:\temp\menthorq_levels.json` timestamp.
3. Are Telegram notifications reliable since the HTML fix?
