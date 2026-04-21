# Phoenix Bot — Known Issues

_Open issues that haven't been resolved yet. Resolved issues moved to semantic/lessons_learned.md._

## 🔴 OPEN

### ANTHROPIC_API_KEY empty — Claude agents DEGRADED (2026-04-21 evening) — RESOLVED

**Status**: RESOLVED 2026-04-21 via commit `eac5ae4` (B42 `load_dotenv
override=True`). Key was never missing — 108-char value on line 19 of
`.env`. Root cause: host OS had `ANTHROPIC_API_KEY=""` set by Claude
Code's OAuth shim; `load_dotenv()` default behavior skips any key already
present in os.environ even if empty. Fix: `override=True` across all
load_dotenv call sites. Direct Claude test post-fix returned "OK".
Remaining text preserved for audit:

**Symptom**: `.env` has `ANTHROPIC_API_KEY=` (name present, value empty).
Every Claude call today returned `outcome: degraded, error_msg:
"ANTHROPIC_API_KEY missing"`. Agents fall back to deterministic templates.

**Affected agents**:
- 4C Session Debriefer → emits fallback template (today's
  `logs/ai_debrief/2026-04-21.md` header says "AI unavailable
  (claude-returned-none); deterministic fallback emitted")
- 4D Historical Learner → will emit empty recommendations list on
  weekly run until fixed

**NOT affected**: Council Gate (4A), Pre-Trade Filter (4B), Adaptive
Params (4E) — they use Gemini or are deterministic.

**Fix**: Jennifer pastes a valid Anthropic API key into `.env` root
under `ANTHROPIC_API_KEY=`. No bot restart required — agents re-read
env on each call via safe_call / importlib path.

**Verification after fix**:
```powershell
python -c "from dotenv import load_dotenv; import os; load_dotenv(); print('ANTHROPIC_API_KEY chars:', len(os.environ.get('ANTHROPIC_API_KEY','')))"
```
Should print a number > 50.

---

### NT8 indicator install path discrepancy — RESOLVED 2026-04-18

CLAUDE.md says NT8 indicators folder is `C:\Users\Trading PC\AppData\Roaming\NinjaTrader 8\bin\Custom\Indicators\` but the active install on this machine is at `C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\bin\Custom\Indicators\` (OneDrive path). All future NT8 indicator operations should use OneDrive path.

**Action:** Update CLAUDE.md at next convenient opportunity.

> **Update (2026-04-18):** RESOLVED. Two events closed this issue:
> 1. NT8 data folder migrated out of OneDrive — active install is now at `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\` (not OneDrive, not AppData).
> 2. CLAUDE.md updated in the same migration PR to reflect the current path. The earlier recommendation "use OneDrive path" no longer applies — use the `Documents\` path.

### Bias_momentum hotfix VERIFIED 2026-04-17 18:55

**Confirmed working.** Post-hotfix log analysis:
- 0 errors (was crashing every evaluation before)
- 109 REJECTED messages with specific reasons (MOMENTUM score=13 need=20, price chasing, RANGE day suppression, CVD flow opposing)
- 0 signals fired — correct behavior, no qualifying setups today
- Lab bot bias_momentum also evaluating cleanly

The hotfix (adding `price = market.get("close", 0.0)` and `vwap = market.get("vwap", 0.0)` near line 66) is solid. Zero signals ≠ silent failure — it's the gates working as designed.

### Lab bot 18% win rate reality

Actual lab stats: 10W / 45L across 55+ trades (~18% WR) but telegram showed mostly wins due to Markdown parse bug (now fixed).

**Not actually bad** — avg win much larger than avg loss keeps P&L slightly green. But the `high_precision_only` strategy at conf=30 is generating mostly losing trades that are dragging the average. Candidate for demotion.

**Action:** April 25 validation review session will analyze per-strategy P&L properly.

### Level 2 depth — only summary forwarded

**Status (2026-04-17 audit):** TickStreamer.cs tracks 5 bid + 5 ask levels via `OnMarketDepth`, but only sums them into `bid_stack`/`ask_stack` before sending (throttled 500ms). Per-level size over time NOT preserved → iceberg detection and deep footprint patterns limited.

**Impact:** Sunday's footprint builder works with price + bid/ask + volume per tick (sufficient for aggressor classification), but per-level depth patterns (iceberg refills, stacked imbalance depth) require TickStreamer C# enhancement.

**Action:** Enhance TickStreamer to send per-level DOM arrays in a later dedicated session. Non-blocking for weekend build.

## 🟡 LOW PRIORITY

### NT8 trade arrow display missing — DIAGNOSED 2026-04-17

**Root cause:** Phoenix places orders via NT8's built-in ATI (reads OIFs from `incoming/`). ATI executes trades correctly but does NOT draw chart arrows. Arrows are normally drawn by a NinjaScript Strategy, not by ATI-placed external orders.

**Fix option 1 (try first — zero code):** Right-click chart → Properties → enable "Show executions" checkbox. NT8 will display fill arrows for any execution regardless of source.

**Fix option 2 (if option 1 insufficient):** Build custom `TradeMarker.cs` indicator that watches `outgoing/` folder for fills and draws green/red arrows. ~2 hours of work — deferred to a later session.

**Not a blocker** — trades execute correctly, just a display issue.

### CalendarRisk fetch consistently fails

Log shows repeated warnings: `[CALENDAR] Fetch failed (non-blocking): No module named 'core.external_data'`. Calendar awareness is broken; bot operating without news event blackouts.

**Action:** Defer to next week. Non-blocking.

### COTFeed URL encoding error

`[COT] CFTC API failed: URL can't contain control characters`. COT data not flowing.

**Action:** Defer. Low value, high maintenance data source.

### NT8 tick-stall silent failure mode

On 2026-04-16 from 07:56 to 11:11 CDT, NT8 showed "connected" but was forwarding 0 ticks/second. Bot's watchdog detected `NT8:live ticks:0/s` but took no action — prod missed entire primary trading window. Bot kept running, waiting for ticks that never came.

**Action:** Planned for Saturday — circuit breaker anomaly detection module will catch this going forward.

## ✅ RECENTLY RESOLVED (leave for reference, move to lessons_learned.md monthly)

_(none yet — this file seeded 2026-04-17)_
