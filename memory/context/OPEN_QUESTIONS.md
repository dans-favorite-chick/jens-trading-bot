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
