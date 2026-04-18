# Phoenix Bot — Lessons Learned

_Curated, long-term knowledge. Distinct from `RECENT_CHANGES.md` (operational log)._
_Durable observations that should survive across many sessions._

---

## Meta / process lessons

### Scope sprawl is a documented failure pattern

Over 11 research rounds on 2026-04-17, the weekend rebuild grew from "add memory so the bot remembers" (original ask) to a 20+ hour infrastructure overhaul. Every individual addition was justified. Cumulatively the scope was near-overwhelming. Discipline going forward: commit → execute → observe 2-4 weeks → iterate, not continuous research.

### The "bot forgets" problem wasn't a memory problem — it was a write-back problem

Root cause: settings.json had zero hooks configured, so no SessionStart auto-loading of memory and no SessionEnd auto-writeback. Claude relied on user memory to know what to write back. Fixing this via hooks (installed 2026-04-17) is the actual solution.

### MenthorQ data staleness is a critical silent failure

2026-04-15 → 2026-04-17: `C:\temp\menthorq_levels.json` went 2 days without updating because NT8 MQBridge indicator was uninstalled/removed from chart. The bot continued trading with stale gamma levels. Staleness checks (file age > 24h → regime=UNKNOWN) were added in weekend build. Still need UI indicator on dashboard to flag staleness visually.

## Market / strategy lessons

### 97% of retail algo traders lose money

Research across multiple 2026 sources converges: win rate isn't the problem, risk management is. Renaissance Medallion operates at ~51% WR. Profit factor ≥ 2.0 + Sharpe > 1 matters more than high WR.

### Chasing 80%+ WR is a retail trap

Achieved only by tiny profit targets + wide stops. One bad streak = blowup. Target instead: 55-65% WR with 1:1.5 to 1:2 R:R → profit factor 2.0+.

### Candlestick patterns in isolation are ~55% reliable

Barely better than coin flip. Context (at S/R, after N trending bars, with volume confirmation) raises to 73%+. Pattern detectors without context weighting are weak. V1 patterns ship without weighting; v2 adds context.

### Positive gamma days are fundamentally different from negative gamma days

Pos GEX → dealer counter-trend flow → mean reversion → tight ATR stops, small targets
Neg GEX → dealer procyclical flow → trend acceleration → wide stops, large targets
Same strategies, different parameters. Encoded in `memory/procedural/regime_matrix.yaml` (weekend build).

### The "catch the top/bottom" framing is dangerous

40% of all stocks never recover from a -70% drawdown. Successful reversal traders wait for confirmation (secondary test, BOS, CVD divergence) — they don't enter on the climax bar. Hard architectural rule in `core/reversal_detector.py`: entry ONLY on secondary test.

### MNQ liquidity has sessions

- 08:30-11:30 CDT (US open): 40% of daily volume — highest edge window
- 13:00-15:00 CDT: secondary institutional window
- Overnight (Asia/London): thinner, wider spreads, gap risk
- Lab bot runs 24/7 for data gathering; prod RTH-only windows

## Technical / infrastructure lessons

### NT8 indicator state is fragile

Indicators can be removed from a chart without warning. `TickStreamer.cs` showed up in OneDrive install; `MQBridge.cs` had been silently uninstalled. Both need to be confirmed applied each morning pre-open.

> **Update (2026-04-18):** The "OneDrive install" reference reflects the pre-migration layout. NT8 data folder has since moved to `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\`. The underlying lesson — fragile indicator state, confirm each morning pre-open — still applies.

### Watchdog detecting "NT8:live ticks:0/s" is insufficient

The bot considered itself "connected" but received zero data. Bot kept running, waiting forever. Need anomaly detection that triggers remediation (restart indicator, alert user) not just observes. Planned for Saturday.

### Kelly sizing requires granular position sizing

Below ~$1500 account size, you can't fractionally size MNQ contracts (minimum 1). Kelly math becomes cosmetic. `simple_sizing.py` with fixed 1-contract + small_account_config is the right abstraction for small accounts.

### Markdown in Telegram breaks on user-generated text

Strategy names with underscores (`bias_momentum`, `high_precision_only`) break `parse_mode="Markdown"`. HTML is much more forgiving. Switched on 2026-04-17. 22/29 dropped messages yesterday is the cost of learning this.

## User-specific preferences

- Local-first architecture (per `user_profile.md` — no cloud dependencies, no Google Sheets/Docs as truth)
- Single-contract MNQ trading (account size appropriate, Kelly inappropriate)
- Target 60% WR + 1:1.5 R:R (decided 2026-04-17)
- Shadow mode 1-2 weeks before activating any new signal gate
- User approval required for strategy demotion/promotion
- Prod LIVE_TRADING=False until account ≥ $2,000
