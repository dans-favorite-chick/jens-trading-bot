# Phoenix Bot — Open Questions

_User follow-ups, decisions pending, and architectural questions that haven't been resolved._
_New Claude sessions: these are things to make progress on, in priority order._

_Last refreshed: 2026-05-13 LATE-NIGHT (post-roadmap-batch, post-self-audit)._

---

## 🔴 Active decisions waiting on operator input

### 1. vwap_pullback bleed — fix or kill?

**State** (per `tools/diagnose_vwap_pullback.py` 2026-05-13):

- 52 trades post-B13, **65.4% WR** but **net −$169.64**
- Avg winner: $26.50, avg loser: −$59.49
- Realized R:R = 0.446 vs configured target_rr = 1.8
- Break-even WR needed at current R:R: **69.2%** (currently 3.8pp short)
- Single exit-reason holds the entire bleed: `stop_loss` (18 trades, 0% WR, $-1,070.76)
- Winners exit cleanly via `ema_dom_exit` (33 trades, 100% WR, $+830.94)
- `target_hit` fires almost never (1 of 52)

**Partial fixes shipped tonight (await verification with new trade data):**
- `#1b` (commit `4e75d82`) stop_atr_mult 2.0 → 1.5 — tighter stop reduces avg loser by ~25%
- `#8` (commit `e6ad6da`) skip_on_stop_clamp now active on vwap_pullback — vol-mismatch skips
- `#1c` (commit `d76b8cb`) ema_dom_exit dynamic 70%-of-target floor — should hold winners longer

**Re-run** `python tools/diagnose_vwap_pullback.py` after 10+ new trades; if avg loser drops below ~$48 we hit break-even at current 65.4% WR.

### 2. Live trading flip — still gated at account ≥ $2,000

**State**: real live account at $300. Prod stays Sim101 (`LIVE_TRADING=False`) until $2,000.
Today's structural fixes (prod 24/7 evaluation, RiskManager hydration, etc.) all apply to Sim101
paper-trading. No live-money exposure changes.

**No action**: gate stays. Revisit when account grows.

### 3. NT8 silent-stall — recurring all day 2026-05-13

**State**: tracked in [KNOWN_ISSUES.md](KNOWN_ISSUES.md) as 🟠 OPEN.
- Heartbeats fresh (every 3s) but tick stream dies for 60s+ at a time.
- Caused today's bots to cycle the WS watchdog repeatedly (the "106s cycle").
- Workaround: manual NT8 data-feed disconnect/reconnect OR full NT8 restart.

**Decision needed**: invest the day to wire NT8 auto-recovery (kill + relaunch NinjaTrader.exe
when stall exceeds 5min), OR keep manual-recovery as the answer.

---

## 🟡 Forward-looking work on the radar

### Sprint M Tier 2 (scheduled 2026-05-19)

Per `memory/context/SPRINT_M_TIER_2_SCHEDULED.md`. Tier 2.3 (tape reader, observation only)
shipped 2026-05-12 in commit `14deff5`. Tier 2.1 / 2.2 / 2.4 still scheduled.

### CPCV / DSR / PBO validation harness — Phase C dependency

`weekly_evolution.py` still emits these checkboxes as "NOT YET RUN". Need ~200 sim trades per
strategy minimum. Sim is now generating dataset continuously. Revisit when `out/grades/` has
8+ weekly aggregates with consistent data.

### Strategy promotion candidates

After today's data-integrity audit, `validation_tracker` finally sees ALL trades (legacy +
per-bot files merged). Re-run `python tools/validation_tracker.py --post-b13-only` weekly.

### Diagnostic-pattern reuse

`tools/diagnose_vwap_pullback.py` works for any strategy via `--strategy NAME`. Worth running
against other strategies showing positive WR but suspect P&L (e.g., once `noise_area` or
`opening_session` have enough post-B13 trades).

---

## 🟢 Deferred to later weeks / months (low priority)

- Context-aware candlestick scoring (v2)
- Triangle patterns + pattern target projection (v2)
- Microstructure deep-dive (tick rate, spread analysis, aggressor ratio deep)
- Cross-asset composite score (NQ/ES spread, DXY inverse, yield curve)
- CalendarRisk fetch fix + pre/post-event gates
- Regime-tagged memory buckets (needs more data)
- UOA / options flow
- SQLite migration of `trade_memory*.json` (the per-bot JSON split is fine for now)
- Weekly / multi-day context module
- Unified feature pool across strategies (Renaissance-style)
- TradeMarker.cs custom indicator (NT8 trade-arrow display — UX nicety, not blocking)
- Phoenix-specific skills under `.claude/skills/` (deferred from Phase B+)

---

## ✅ Recently resolved (closed since last refresh, included for audit trail)

### 2026-05-13 LATE-NIGHT — 21-item roadmap batch (see [RECENT_CHANGES.md](RECENT_CHANGES.md))

Operator pasted a 25-item roadmap with "do it all". 21 shipped tonight (paper-trading items skipped per operator's "live sim only" directive). Commits `c14a3a1` → `3ddf7a9`:

- ✅ `#2` `c14a3a1` — MAE/MFE/R-multiple tracking on every closed trade
- ✅ `#3` `4d4e15d` — Anti-mutation invariant on `r_distance`
- ✅ `#4` `56eaf3b` — Outlier-stripped P&L flag (caught bias_momentum noise-vs-edge)
- ✅ `#5/#6` `f0e6863` — Retired high_precision_only, opening_session, compression_breakout
- ✅ `#7` `878165b` — `config/regime_matrix.py` typed loader
- ✅ `#8` `e6ad6da` — skip_on_stop_clamp wired into vwap_pullback + dom_pullback
- ✅ `#12` `4219719` — `docs/cvd_usage_audit.md`
- ✅ `#13` `52cede2` + audit-fix `3ddf7a9` — ORB state persistence across restarts
- ✅ `#14` `e701973` — footprint_cvd_reversal `cvd_div_type` instrumentation
- ✅ `#15` `30eb1f2` — vwap_band_pullback TF-vote 3→2
- ✅ `#17` `64c113a` — `tools/mae_stop_calibrator.py` framework
- ✅ `#18` `32e823f` — BE arms on bar-close, not tick-touch
- ✅ `#19` `7edaf9b` — Explicit flow_reversal priority in exit cascade
- ✅ `#1b` `4e75d82` — vwap_pullback stop_atr_mult 2.0→1.5
- ✅ `#1c` `d76b8cb` — ema_dom_exit dynamic 70%-of-target floor
- ✅ `#20` `a951dc9` — `tools/strategy_change_log.py`
- ✅ `#22` `477e31d` — Wilson-CI promotion guardrail + demoted ib_breakout
- ✅ `#23` `5a71566` — Tier-aware sizing in SimpleSizer
- ✅ `#25` `6af0689` — `tools/strategy_correlation_audit.py`

### 2026-05-13 earlier — original day's work (commit refs preserved)

- ✅ `dda680c` Graceful /shutdown via dashboard command queue
- ✅ `c9099d7` 12-file trade_memory reader audit (all readers route through `load_all_trades()`)
- ✅ `4d523bf` Dashboard `/api/today-pnl` per-bot file fix
- ✅ `4e29ce5` + `d7e081a` RiskManager hydrates daily counters on bot startup (with bot_id filter)
- ✅ `1e07000` Prod trading-window gate REMOVED — prod now evaluates 24/7
- ✅ `2b59342` `tools/diagnose_vwap_pullback.py` shipped, vwap_pullback bleed surfaced
- ✅ Operator side: Gemini AI investigator restored on a fresh GCP project (new GOOGLE_API_KEY)
- ✅ PhoenixWatcher scheduled task now has `Repetition: PT5M` — max 5-min alerting downtime
- ✅ `c209202` Sprint M Tier 1 C# side LIVE — TickStreamer recompiled, `imbalance_ratio` field
  flowing in `data/volumetric_latest.json`

### Older resolutions (collapsed — see git history)

- 2026-04-21: ANTHROPIC_API_KEY missing (`eac5ae4`)
- 2026-04-25: scheduled task lattice, watcher/finnhub/fred daemons, dual-stream incident cleanup
- 2026-05-04: Sprint G dashboard UX fixes (`0b4a9db`, `cbaddb7`)
- 2026-05-04: Sprint H opened up strategies for prod (`only_validated=False`)
- 2026-05-12: bulletproof subprocess launch (`8b471af`)

---

## ❓ Questions to ask the operator at next session

1. **Push `weekly-evolution/2026-05-10` to origin?** 24 commits ahead locally (21 roadmap + 1 audit + 2 housekeeping on top of this morning's bias_momentum fix). Branch verified clean: 1,912 pass / 4 skip / 0 fail.
2. **Wire `RegimeMatrix` into base_bot evaluate()?** #7 shipped the typed loader but didn't wire it in — that's a behavior change touching every strategy and warrants a separate review pass. Operator decides when to flip the switch.
3. **bias_momentum data review**: now that MAE/MFE/R-multiple are persisted (#2), after 10+ new closed trades run `python tools/mae_stop_calibrator.py --strategy bias_momentum` to see if the empirical stop should differ from the current `stop_atr_mult=2.0` config.
4. **Re-verify vwap_pullback bleed**: #1b/#1c/#8 each target a different aspect of the bleed. Re-run `tools/diagnose_vwap_pullback.py` after 10+ trades.
5. **NT8 silent-stall**: if it recurs tomorrow, invest the day to auto-recovery OR keep manual?
