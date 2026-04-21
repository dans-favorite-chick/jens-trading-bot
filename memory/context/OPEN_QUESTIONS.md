# Phoenix Bot — Open Questions

_User follow-ups and architectural questions that haven't been resolved._
_New Claude sessions: these are things the user wants progress on._

## 🔴 Active questions

### Weekend rebuild: is the bot Monday-ready?

Three-session rebuild Fri/Sat/Sun delivers ~20h of infrastructure work. Tier 1 (Friday tonight) is foundation only. Tier 2 (Saturday) is risk mgmt + signal foundation. Tier 3 (Sunday) is composite + integration. Monday activation state pending Sunday WFO validation.

### Will simple_sizing satisfy without Kelly?

Account at $300. Kelly math doesn't work below $1500 (can't fractionally size MNQ contracts). Replaced with `simple_sizing.py` reading `small_account_config.yaml`. When account grows, we can add Kelly. Until then, fixed 1-contract sizing.

### Should all 7 strategies stay active, or concentrate?

User chose "informed generalist across 7 strategies" in earlier session. April 25 validation review will surface whether any strategies are dragging P&L (`high_precision_only` at conf=30 is a likely demotion candidate).

## 🟡 Deferred to April 25 session

- Reflector agent (propose-only daily debrief)
- Strategy concentration analysis (which strategies earn vs drag)
- Kelly sizing activation gate (if account reaches $1500)
- 2-week shadow validation review

## 🟡 Deferred from roadmap v4 sprint (2026-04-19)

Items consciously skipped / simplified during the Apr 19 6-day condensed sprint.
Updated 2026-04-19 after Option B ORB Chandelier implementation. All within the
4%-deviation budget — flagged here so they don't get forgotten.

1. **Non-conforming strategy rewrites (v2 adapter debt)** — multiple strategy
   files have been rewritten as standalone classes that no longer inherit
   `BaseStrategy` and emit their own Signal shape:
   - `strategies/bias_momentum_v2.py` → `BiasMomentumV2Signal`
   - `strategies/vwap_pullback.py` → `VWAPPullbackSignal`
     (discovered 2026-04-19 when lab bot crashed on startup with
     `AttributeError: 'VWAPPullback' object has no attribute 'validated'`).

   The base_bot `load_strategies()` loader now **defensively skips** any class
   that doesn't inherit `BaseStrategy` with a WARN log — the lab bot stays up
   with the conforming strategies rather than crashing on startup. Any skipped
   strategy is silently out of service until an adapter is written.

   **Promotion Gate (MUST READ before any v2 prod promotion):**
   Before any of these v2 rewrites replaces its v1 in prod — or is re-enabled
   in lab — a Signal adapter must be written that translates the v2 Signal
   shape to the Phoenix canonical `Signal` (including `entry_type`, `stop_type`,
   `target_type`, `entry_price`, `stop_price`, `target_price`, `scale_out_rr`,
   `exit_trigger`, `trail_config`, `eod_flat_time_et`, `metadata`). Without
   this, entry_type wiring silently breaks, bracket orders won't receive their
   correct order types, and managed exits (Chandelier, Noise Area, universal
   EoD) will never fire for these strategies' trades.
   **Do not promote these without resolving this item.**

2. *(Removed 2026-04-19 — resolved by Option B Chandelier implementation.)*
   ORB now runs the spec-accurate path: partial 50% at 1.0R (via Signal.scale_out_rr)
   + Chandelier 3×ATR trail on runner (`core/chandelier_exit.py`) + universal EoD
   flat hook. Verified by `tests/test_orb_chandelier.py` (18 tests).

3. **Finnhub blackout window** — roadmap says "±2 min Tier-1 blackout"; existing
   `core/calendar_risk.py` uses ±5 min (30min reduce / 5min block / 15min widen).
   Functionally identical lock-out; not narrowing to ±2 min.

   **Decision (2026-04-19):** Keep ±5min (wider is strictly safer). Revisit only
   if trade log shows a pattern of would-have-won trades blocked by the wider
   window. No action required pre-Monday.

4. **Unused warmup artifacts** — after switching to `tools/load_sigma_open_warmup.py`
   + `data/sigma_open_table.json` (27 real MNQ 1m sessions), these became
   unreferenced: `tools/warmup_noise_area.py`, `tools/backfill_noise_area.py`,
   `memory/noise_area_warmup.json`. Moved to `archive/pre_load_sigma_open/`
   2026-04-19. Safe to delete after 2 weeks of stable operation.

5. **ORB missing v3 doc** — v3 roadmap referenced in v4 was missing. ORB built
   from the PARAMS block + v4 matrix + `strategies/ib_breakout.py` precedent.
   Audit against paper after first 10 live trades for spec drift.

6. **ORB clock anchor** — ORB uses "first 15 bars received" not a literal 9:30 ET
   clock. Phoenix's session-window gate covers this indirectly, but ORB itself has
   no clock guard. Audit if bot restart behavior ever violates this assumption.

7. **ORB stop cap behavior** — Spec says 25pt stop is a CLAMP (clamp stop distance
   to 25pt max, still take trade). Phoenix REJECTS the trade if stop > 25pt
   (`orb.py:148-149`). More conservative but loses trades the paper would take on
   gap/high-vol days. Revisit after 10+ live trades to see if we're missing
   meaningful setups.

### Promotion runbook — add to any future runbook file

Before promoting ANY strategy from lab (`validated: False`) to prod
(`validated: True`), review `memory/context/OPEN_QUESTIONS.md` and resolve any
items tagged to that strategy. The bias_momentum_v2 gate (item #1 above) is the
canonical example.

## 🟢 Deferred to later weeks / months

- Context-aware candlestick scoring (v2)
- Triangle patterns + pattern target projection (v2)
- NT8 Order Flow+ volumetric bars (C# mod — dedicated session)
- Microstructure (tick rate, spread analysis, aggressor ratio deep)
- Cross-asset composite score (NQ/ES spread, DXY inverse, yield curve)
- CalendarRisk fetch fix + pre/post-event gates
- agents/reflector.py
- Regime-tagged memory buckets (need more data)
- User dispute button (needs reflector live first)
- UOA / options flow
- Level 2 tape-reader wired into strategies (footprint built, not yet gated)
- SQLite migration of trade_memory.json
- Weekly / multi-day context module
- Unified feature pool across strategies (Renaissance-style)

## ❓ Questions for user at next morning check-in

_(auto-populated by reflector when active; manual entries until then)_

1. Did MQBridge redeploy successfully? Check `C:\temp\menthorq_levels.json` timestamp after NT8 restart.
2. Are you seeing Telegram notifications reliably now (after HTML fix)?
