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
All within the 4%-deviation budget — flagged here so they don't get forgotten.

- **bias_momentum_v2 adapter** — `strategies/bias_momentum_v2.py` emits its own
  `BiasMomentumV2Signal` dataclass rather than the canonical `Signal`. Per-strategy
  entry_type wiring couldn't apply. Not in prod (`bias_momentum` is). Needs an
  adapter layer before v2 can replace v1. Low priority — v1 is shipping.
- **ORB chandelier trail on runner** — roadmap v4 matrix says "Partial 1R + trail".
  Phoenix's global `SCALE_OUT_RR=1.5` applies (exits partial at 1.5R, not 1.0R).
  Within tolerance. Revisit if ORB shows pattern-specific trade-quality drag.
- **Finnhub blackout window** — roadmap says "±2 min Tier-1 blackout"; existing
  `core/calendar_risk.py` uses ±5 min (30min reduce / 5min block / 15min widen).
  Functionally identical lock-out; not narrowing to ±2 min.
- **Unused warmup artifacts** — after switching to `tools/load_sigma_open_warmup.py`
  + `data/sigma_open_table.json` (27 real MNQ 1m sessions), these became
  unreferenced: `tools/warmup_noise_area.py`, `tools/backfill_noise_area.py`,
  `memory/noise_area_warmup.json`. Kept in tree as fallback / reference; delete
  when confident the main path is stable.
- **ORB missing v3 doc** — v3 roadmap referenced in v4 was missing. ORB built
  from the PARAMS block + matrix + `strategies/ib_breakout.py` precedent. Review
  after first 10 live trades for spec drift.

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
