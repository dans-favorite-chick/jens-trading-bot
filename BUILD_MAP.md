# Phoenix Bot — Implementation Build Map

**Version:** 1.0 (April 19, 2026)
**Repo:** `C:\Trading Project\phoenix_bot\`
**Branch:** `feature/knowledge-injection-systems`
**Owner:** Jennifer (Frisco, TX)
**Executor:** Claude Code (terminal agent)

---

## 📋 EXECUTIVE SUMMARY

This document is the complete implementation plan for transforming the Phoenix Bot from its current state (33% WR, -$1,227 over 697 trades) to a research-validated trading system with proper risk gates, regime awareness, and exit management.

**Total scope:** ~16 new/updated files, ~3 weeks of work, organized in 7 phases.

**Critical path:** Phase 1 (P5b STOPMARKET fix) is a prod-blocker. Phases 2-5 can be done in parallel batches. Phase 6 (lab validation) gates any prod promotion.

**Source materials:**
- All deliverables in: `C:\Users\Trading PC\AppData\Local\Anthropic\Claude\downloads\` (or wherever Jennifer has saved Claude's output files)
- This document — read it FULLY before starting any phase
- `tools/verification_2026_04_18/FINDINGS.md` — for P5b context

---

## 🚨 CRITICAL CONTEXT

### What this project is
Personal automated MNQ futures trading bot. Account: $300 sim on NT8 Sim101. Architecture:

```
NT8 TickStreamer.cs (TCP CLIENT)
    ↓ raw TCP ticks (bid/ask/price/vol per tick)
bridge_server.py  ← TCP :8765 (NT8)  → WS :8766 (bots)  → HTTP :8767 (health)
    ↓
    ├── prod_bot.py  (live/sim trading, session hours)
    └── lab_bot.py   (experimental, paper trading 24/7)
    ↓
OIF files → NT8 ATI execution
```

### What this project is NOT
The legacy system at `C:\Trading Project\trading_bot_project\` is **FULLY RETIRED**.
- ❌ NEVER reference: `bot.py`, `jen_bot_v2.py`, `config_v2.py`, `trading_controller.py`, `MarketDataBroadcasterV2.cs`
- ❌ NEVER use Flask for the dashboard
- ❌ NEVER make AI agents blocking — all need timeouts defaulting to safe pass-through

### Key technical facts
- **Instrument:** MNQ (Micro Nasdaq 100 Futures), $0.50/tick, tick size 0.25
- **NT8 role:** Always TCP/WS CLIENT — Python is always SERVER
- **Ports:** TCP 8765 (NT8 → bridge), WS 8766 (bridge → bots), HTTP 8767 (health)
- **Session:** Primary NY 8:30–10:00 AM CST for prod; lab runs 24/7
- **Risk:** $2,000 account ceiling, $45-50/day max loss
- **Warmup:** 25 min minimum before any signals fire
- **Execution:** OIF text files written to NT8's incoming folder
- **Env:** `.env` in project root, `GOOGLE_API_KEY` for Gemini, `ANTHROPIC_API_KEY` for Claude

---

## ⛔ PRE-FLIGHT BLOCKERS (DO THESE FIRST)

These must be resolved BEFORE starting any phase.

### Blocker 1: MenthorQ API Key Rotation
**Status:** UNRESOLVED as of April 19, 2026
**Action:** Jennifer must rotate the MenthorQ API key (was pasted in chat in earlier rounds, exposed)
**How to verify:** New key in `.env` as `MENTHORQ_API_KEY=...`, old key revoked in MenthorQ dashboard
**Block radius:** Any work touching MenthorQ data integration

### Blocker 2: NinjaTrader Data Subscriptions
**Status:** UNVERIFIED
**Action:** In NT8, verify these symbols load with live data:
- `MNQM6 06-26` (or current MNQ front month) — primary, MUST work
- `ES 06-26` (or current ES front month) — required for intermarket filter
- `^VXN` (Cboe Nasdaq Volatility Index) — preferred for NQ
- `^VIX` (Cboe S&P 500 Volatility Index) — fallback

**How to verify:** Open chart for each symbol in NT8. If "No Data" appears, the subscription is missing.
**Block radius:** Phase 2 (NinjaScript update) and Phase 3 (bridge update)
**Workaround:** If VIX/VXN unavailable, comment out those slots in TickStreamer.cs (will document below)

### Blocker 3: Git State Clean
**Status:** UNVERIFIED
**Action:**
```bash
cd "C:\Trading Project\phoenix_bot"
git status
git stash  # if any uncommitted changes
git pull origin feature/knowledge-injection-systems
git tag v-pre-build-map-2026-04-19  # safety tag before changes
git push origin v-pre-build-map-2026-04-19
```

---

## 📦 FILE DELIVERABLES INVENTORY

All files Claude has shipped across rounds 4-8. Source location: `/mnt/user-data/outputs/` (or wherever Jennifer downloaded them).

### Python modules (Phoenix Bot core)
| File | Target Location | Purpose |
|------|-----------------|---------|
| `mtf_trend.py` | `phoenix_bot/core/mtf_trend.py` | 3-method MTF trend detector (replaces single-bar HTF gating) |
| `chandelier_exit.py` | `phoenix_bot/core/chandelier_exit.py` (v1, superseded) | Initial chandelier exit |
| `chandelier_exit_v2.py` | `phoenix_bot/core/chandelier_exit.py` (rename, replaces v1) | Timeframe-aware Chandelier with factory methods |
| `anchored_vwap.py` | `phoenix_bot/core/anchored_vwap.py` | Auto-detected anchored VWAPs |
| `qscore.py` | `phoenix_bot/core/qscore.py` | MenthorQ Q-Score loader and trade evaluator |
| `opex_calendar.py` | `phoenix_bot/core/opex_calendar.py` | OpEx detection + size/RR rules |
| `intermarket.py` | `phoenix_bot/core/intermarket.py` | VIX/VXN regime + ES confirmation filter |

### Strategy files
| File | Target Location | Purpose |
|------|-----------------|---------|
| `trend_following_pullback.py` | `phoenix_bot/strategies/trend_following_pullback.py` | Flagship — corrected gamma logic, no time stop |
| `bias_momentum_v2.py` | `phoenix_bot/strategies/bias_momentum_v2.py` | HTF + pullback + Chandelier + BE upgrade |
| `vwap_pullback.py` | `phoenix_bot/strategies/vwap_pullback.py` | 1σ bands rewrite + AVWAP confluence |
| `compression_breakout.py` | `phoenix_bot/strategies/compression_breakout.py` | Base class for 15m + 30m variants |

### Configuration
| File | Target Location | Purpose |
|------|-----------------|---------|
| `strategies_v3.py` | merge into `phoenix_bot/config/strategies.py` | Final strategy config with corrected gamma rules + multi-hour holds + 15m/30m parallel + GAMMA_REGIME_RULES doc dict |

### NinjaScript (C#)
| File | Target Location | Purpose |
|------|-----------------|---------|
| `TickStreamer.cs` | NT8 Indicators folder (replaces v2) | Multi-instrument: MNQ + ES + VXN + VIX |
| `SiM_TickStreamer.cs` | NT8 Indicators folder (replaces v2) | Same as above for sim/replay |

### Bridge updates
| File | Target Location | Purpose |
|------|-----------------|---------|
| `bridge_server_patch.py` | apply to existing `phoenix_bot/bridge/bridge_server.py` | Routes ticks by symbol, adds intermarket integration |

### Files DELETED during rebuild (do not restore)
- `phoenix_bot/strategies/dom_pullback.py` — single-example overfit
- `phoenix_bot/strategies/tick_scalp.py` — math fails on MNQ commissions ($1.34/trade negative expectancy)

---

## 🛠️ PHASE 1 — P5b STOPMARKET FIX (CRITICAL INFRASTRUCTURE)

**Priority:** P0 BLOCKER — bot has NO broker-side stop protection without this fix
**Time estimate:** 60-90 minutes
**Dependencies:** Pre-flight blockers resolved

### Why this exists
Verification sprint findings at `tools/verification_2026_04_18/FINDINGS.md` identified that `bridge/oif_writer.py` writes order type `STOP` instead of NT8's required `STOPMARKET`. NT8's ATI parser silently rejects every `STOP` order. Result: every "stop" the bot has placed since inception was never accepted by the broker. Position protection currently only exists in Python (in-process check); if the bot crashes, positions are unprotected.

### Files to modify
- `phoenix_bot/bridge/oif_writer.py`

### Step-by-step actions

**1.1** Create a new branch for the fix:
```bash
cd "C:\Trading Project\phoenix_bot"
git checkout -b fix/p5b-stopmarket-fix
```

**1.2** Read the current `oif_writer.py`:
```bash
cat phoenix_bot/bridge/oif_writer.py
```

**1.3** Fix line 65 (initial entry stop):
- **Find:** `STOP` (in OIF order type field)
- **Replace with:** `STOPMARKET`
- **Verify:** order text format matches NT8 ATI spec for STOPMARKET orders

**1.4** Fix line 75 (initial entry target):
- This may use `LIMIT` already — verify

**1.5** Fix line 92 (stop adjustment):
- Same `STOP` → `STOPMARKET` replacement

**1.6** Fix line 97 (target adjustment):
- Verify uses `LIMIT` (no change needed if so)

**1.7** Fix line 100 (CANCEL_ALL semicolons - B2):
- **Find:** OIF line ending with `;;` (double semicolons)
- **Replace with:** single `;` per spec

**1.8** Add B5 fix (single-order CANCEL):
- Add new function `cancel_single_order(order_id: str) -> str:`
- Returns OIF text: `CANCEL;{order_id}`
- This enables surgical cancellation vs. nuke-all CANCELALLORDERS

**1.9** Add B4 fix (account-scoped CANCELALLORDERS):
- Modify CANCELALLORDERS function to take account name
- OIF format: `CANCELALLORDERS;{account_name}`
- Default to current connected account, NOT empty (which cancels across all accounts)

**1.10** Add unit tests at `tools/verification_2026_04_18/test_p5b_stopmarket.py`:
- Test that `write_oif()` for buy-stop produces line containing `STOPMARKET`
- Test that CANCELALLORDERS produces account-scoped output
- Test single-order CANCEL function

**1.11** Run tests:
```bash
cd "C:\Trading Project\phoenix_bot"
python -m pytest tools/verification_2026_04_18/test_p5b_stopmarket.py -v
```

**1.12** Live diagnostic (5:30 PM CT or any market-open time):
- Start bridge: `python bridge/bridge_server.py`
- Start prod_bot in dry-run mode (no actual trades): `python bots/prod_bot.py --dry-run`
- Manually trigger a test buy-stop order
- Verify in NT8 the order shows as `STOPMARKET` not `STOP`
- Verify NT8 accepts and works the order

**1.13** Commit with descriptive message:
```bash
git add phoenix_bot/bridge/oif_writer.py tools/verification_2026_04_18/test_p5b_stopmarket.py
git commit -m "fix(P5b): STOPMARKET order type, account-scoped CANCEL, single-order CANCEL

Fixes B3, B4, B5 from FINDINGS.md:
- Lines 65/75/92/97: replace STOP with STOPMARKET (NT8 ATI requirement)
- Line 100: CANCEL_ALL semicolons normalized
- New cancel_single_order() function for surgical cancellation
- CANCELALLORDERS now account-scoped (was nuking across all accounts)

NT8's parser silently rejected every STOP order since project inception.
Bot now has broker-side stop protection in addition to Python-side stops.

Tests added at tools/verification_2026_04_18/test_p5b_stopmarket.py."
```

**1.14** Push to remote:
```bash
git push origin fix/p5b-stopmarket-fix
```

**1.15** Merge to feature branch (after Jennifer reviews):
```bash
git checkout feature/knowledge-injection-systems
git merge fix/p5b-stopmarket-fix
git push origin feature/knowledge-injection-systems
```

### Definition of done
- [ ] All four lines in `oif_writer.py` use `STOPMARKET` not `STOP`
- [ ] CANCELALLORDERS is account-scoped
- [ ] `cancel_single_order()` function exists and tested
- [ ] Unit tests pass
- [ ] Live diagnostic confirms NT8 accepts STOPMARKET orders
- [ ] Commit pushed, branch merged

### Rollback if it fails
```bash
git checkout feature/knowledge-injection-systems
git reset --hard v-pre-build-map-2026-04-19
git push --force-with-lease origin feature/knowledge-injection-systems
```

### Keep parallel safety net
For 1-2 weeks after this fix, KEEP the Python-side stop logic in place. Don't remove it until you've confirmed broker-side stops fire correctly across multiple sessions.

---

## 🔌 PHASE 2 — NINJASCRIPT V3 DEPLOYMENT

**Priority:** P1 — required for intermarket filter
**Time estimate:** 30-60 minutes
**Dependencies:** Phase 1 complete, data subscriptions verified

### Why this exists
The current TickStreamer.cs only streams the chart's primary instrument (MNQ). To use the new intermarket.py filter (VIX/VXN regime + ES confirmation), the indicator must stream additional instruments via NT8's AddDataSeries() pattern.

### Files to modify
- `phoenix_bot/ninjatrader/TickStreamer.cs` (replace with v3)
- `phoenix_bot/ninjatrader/SiM_TickStreamer.cs` (replace with v3)

### Pre-action: Backup
```bash
cd "C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators"
copy TickStreamer.cs TickStreamer_v2_backup_2026-04-19.cs
copy SiM_TickStreamer.cs SiM_TickStreamer_v2_backup_2026-04-19.cs
```

Also commit current state to git:
```bash
cd "C:\Trading Project\phoenix_bot"
git add phoenix_bot/ninjatrader/TickStreamer.cs phoenix_bot/ninjatrader/SiM_TickStreamer.cs
git commit -m "chore: snapshot TickStreamer v2 before v3 upgrade"
```

### Step-by-step actions

**2.1** Copy v3 files to repo:
```bash
cp <downloads>/TickStreamer.cs "C:\Trading Project\phoenix_bot\phoenix_bot\ninjatrader\TickStreamer.cs"
cp <downloads>/SiM_TickStreamer.cs "C:\Trading Project\phoenix_bot\phoenix_bot\ninjatrader\SiM_TickStreamer.cs"
```

**2.2** Verify the contract symbols in the v3 files match current front-month contracts. In each file, check:
```csharp
private const string AUX_INSTRUMENT_1 = "ES 06-26";
```
Update `06-26` to current front-month code if different.

**2.3** Copy v3 files to NT8's indicators folder:
```bash
cp phoenix_bot/ninjatrader/TickStreamer.cs "C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\TickStreamer.cs"
cp phoenix_bot/ninjatrader/SiM_TickStreamer.cs "C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\SiM_TickStreamer.cs"
```

**2.4** In NT8 UI:
- Tools → Edit NinjaScript → Indicator
- Open `TickStreamer`
- Press F5 to compile
- Verify "0 errors, 0 warnings" in output
- Open `SiM_TickStreamer`
- Press F5 to compile
- Verify "0 errors, 0 warnings"

**2.5** Test on isolated chart (DON'T deploy to production chart yet):
- Open a NEW MNQ chart (not the production one)
- Add `SiM_TickStreamer` indicator (sim variant for safety)
- Watch NT8 Output Window for:
  ```
  SiM_TickStreamer v3: Connected to bridge (TCP 127.0.0.1:8765)
  ```
- If "Failed to load instrument" appears for any aux instrument, that subscription is missing

**2.6** If aux subscription missing (e.g., VIX):
- Open the .cs file again
- Set the missing slot to empty: `private const string AUX_INSTRUMENT_3 = "";`
- Recompile
- Re-add to chart

**2.7** Commit the deployment-tested files:
```bash
cd "C:\Trading Project\phoenix_bot"
git add phoenix_bot/ninjatrader/TickStreamer.cs phoenix_bot/ninjatrader/SiM_TickStreamer.cs
git commit -m "feat(ninjatrader): TickStreamer v3 multi-instrument

Streams primary MNQ + ES + VXN + VIX via AddDataSeries.
Each tick payload now includes 'symbol' field for bridge routing.
Aux instruments throttled (ES every tick, VIX/VXN every 500ms).

Requires:
- CME data sub for ES (likely already included)
- Cboe Indices sub for VIX/VXN (may need separate subscription)

If aux subscription fails, set that AUX_INSTRUMENT_n constant to empty
string and recompile — indicator will skip that slot gracefully."
git push origin feature/knowledge-injection-systems
```

### Definition of done
- [ ] Both indicators compile with 0 errors
- [ ] Test chart shows v3 connection message in NT8 Output
- [ ] Aux instrument names captured (visible in connect message JSON)
- [ ] Bridge log shows incoming ticks with `symbol` field populated
- [ ] Backups exist in NT8 indicators folder (`*_v2_backup_*.cs`)

### Rollback if it fails
```bash
cd "C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators"
copy TickStreamer_v2_backup_2026-04-19.cs TickStreamer.cs
copy SiM_TickStreamer_v2_backup_2026-04-19.cs SiM_TickStreamer.cs
# In NT8: F5 to recompile
```

---

## 🌐 PHASE 3 — BRIDGE MULTI-INSTRUMENT ROUTING

**Priority:** P1 — pairs with Phase 2
**Time estimate:** 30-45 minutes
**Dependencies:** Phase 2 complete

### Why this exists
TickStreamer v3 sends ticks tagged with symbol field. Bridge must route MNQ ticks to bots (existing behavior) and route ES/VIX/VXN ticks to the new intermarket filter.

### Files to modify
- `phoenix_bot/bridge/bridge_server.py`
- (new) `phoenix_bot/core/intermarket.py` (must exist before bridge can import)

### Step-by-step actions

**3.1** First, ensure `intermarket.py` is in place:
```bash
mkdir -p "C:\Trading Project\phoenix_bot\phoenix_bot\core"
cp <downloads>/intermarket.py "C:\Trading Project\phoenix_bot\phoenix_bot\core\intermarket.py"
```

**3.2** Verify it imports cleanly:
```bash
cd "C:\Trading Project\phoenix_bot"
python -c "from phoenix_bot.core.intermarket import IntermarketFilter; print('OK')"
```

**3.3** Open `bridge_server_patch.py` from deliverables and apply each of its 4 changes to the existing `bridge_server.py`. The patch is INSTRUCTIONS, not a replacement file. Apply surgically:

**Change 3.3.1** — Add intermarket filter to BridgeServer.__init__
Find: `# DOM depth state (updated by TickStreamer dom messages)` block
Add right after it: the IntermarketFilter import + instantiation + symbol classification helpers

**Change 3.3.2** — Update `handle_nt8_tcp()` tick handler
Find: `elif msg_type == "tick":` block (around line 175)
Replace with: the symbol-routing version from the patch doc

**Change 3.3.3** — Add intermarket to `get_health()`
Find: the return statement of `get_health()` (around line 220)
Add: `"intermarket": self.intermarket.snapshot(),` to the returned dict

**Change 3.3.4** — Add `intermarket_query` handler in `handle_bot()`
Find: the message processing loop in `handle_bot()`
Add: the new `elif msg_type == "intermarket_query":` branch

**3.4** Test bridge starts cleanly:
```bash
cd "C:\Trading Project\phoenix_bot"
python bridge/bridge_server.py
```
Look for:
- `[OK] NT8 server  : tcp://127.0.0.1:8765`
- `[OK] Bot server  : ws://127.0.0.1:8766`
- `[OK] Health HTTP : http://127.0.0.1:8767/health`
- No tracebacks

**3.5** Test health endpoint:
```bash
curl http://127.0.0.1:8767/health | python -m json.tool
```
Verify `"intermarket"` key exists in output (values will be 0 until NT8 connects).

**3.6** End-to-end test with NT8 connected:
- Start bridge (above)
- Add `SiM_TickStreamer` v3 to a chart
- Watch bridge log for connection
- After ~30 seconds, hit health endpoint again
- Verify `intermarket.vix_value` and `intermarket.es_value` are non-zero

**3.7** If `vix_value` stays 0 but ES works, the VIX subscription is the issue. Either:
- Add Cboe Indices subscription in NT8
- OR set `AUX_INSTRUMENT_3 = ""` in TickStreamer.cs and recompile

**3.8** Commit:
```bash
git add phoenix_bot/core/intermarket.py phoenix_bot/bridge/bridge_server.py
git commit -m "feat(bridge): multi-instrument routing + intermarket filter

Routes ticks by 'symbol' field:
- MNQ: broadcast to bots (existing behavior)
- ES: feed IntermarketFilter for confirmation logic
- VIX/VXN: feed IntermarketFilter for regime classification

New WebSocket message: 'intermarket_query' from bots gets back
the current intermarket evaluation for a proposed trade direction.

Health endpoint /health now includes 'intermarket' block with VIX
regime, ES alignment, size/stop multipliers."
git push origin feature/knowledge-injection-systems
```

### Definition of done
- [ ] `intermarket.py` imports cleanly
- [ ] Bridge starts with no errors
- [ ] Health endpoint shows intermarket block
- [ ] After NT8 v3 indicator connects, VIX and ES values populate
- [ ] Bridge log shows no "Unknown symbol" warnings (after first 5 min)

### Rollback if it fails
```bash
git checkout HEAD~1 phoenix_bot/bridge/bridge_server.py
```

---

## 🧩 PHASE 4 — MODULE INTEGRATION INTO BASE_BOT

**Priority:** P1 — strategies can't run without these
**Time estimate:** 2-3 hours
**Dependencies:** Phases 1-3 complete

### Why this exists
The new modules (qscore, opex_calendar, anchored_vwap, mtf_trend, chandelier_exit_v2) need to be wired into `bots/base_bot.py` so strategies can use them.

### Files to deploy
Copy from deliverables to repo:
```bash
cp <downloads>/mtf_trend.py            "C:\Trading Project\phoenix_bot\phoenix_bot\core\mtf_trend.py"
cp <downloads>/chandelier_exit_v2.py   "C:\Trading Project\phoenix_bot\phoenix_bot\core\chandelier_exit.py"  # NOTE: rename
cp <downloads>/anchored_vwap.py        "C:\Trading Project\phoenix_bot\phoenix_bot\core\anchored_vwap.py"
cp <downloads>/qscore.py               "C:\Trading Project\phoenix_bot\phoenix_bot\core\qscore.py"
cp <downloads>/opex_calendar.py        "C:\Trading Project\phoenix_bot\phoenix_bot\core\opex_calendar.py"
```

Note: `chandelier_exit_v2.py` REPLACES the old `chandelier_exit.py`. Old version moves to git history.

### Files to modify
- `phoenix_bot/bots/base_bot.py`

### Step-by-step actions

**4.1** Verify all new modules import cleanly:
```bash
cd "C:\Trading Project\phoenix_bot"
python -c "
from phoenix_bot.core.mtf_trend import MTFTrendDetector
from phoenix_bot.core.chandelier_exit import ChandelierExitManager
from phoenix_bot.core.anchored_vwap import AnchoredVWAPManager
from phoenix_bot.core.qscore import QScoreManager
from phoenix_bot.core.opex_calendar import OpExCalendar
print('All imports OK')
"
```

**4.2** Read current `base_bot.py` to understand integration points:
```bash
cat phoenix_bot/bots/base_bot.py
```

Look for these existing extension points:
- `__init__()` — where to instantiate new managers
- `_evaluate_strategies()` — where to apply new gates
- `_on_tick()` or equivalent — where to update AVWAP
- `_on_bar_close()` — where to update mtf_trend
- `_on_position_update()` — where to update Chandelier

**4.3** Add to `BaseBot.__init__`:
```python
# Phase 4 module integration
from phoenix_bot.core.mtf_trend import MTFTrendDetector
from phoenix_bot.core.chandelier_exit import ChandelierExitManager
from phoenix_bot.core.anchored_vwap import AnchoredVWAPManager
from phoenix_bot.core.qscore import QScoreManager
from phoenix_bot.core.opex_calendar import OpExCalendar

self.mtf_trend = MTFTrendDetector()
self.avwap_mgr = AnchoredVWAPManager(anchor_lifespan_days=5)
self.qscore_mgr = QScoreManager(data_path="data/qscore/nq_daily.json")
self.qscore_mgr.reload()
self.opex_cal = OpExCalendar()

# Per-strategy chandelier managers (each strategy has its own timeframe)
self.chandelier_5m  = ChandelierExitManager.for_5m_strategy()
self.chandelier_15m = ChandelierExitManager.for_15m_strategy()
self.chandelier_30m = ChandelierExitManager.for_30m_strategy()
```

**4.4** Add per-tick AVWAP update in tick handler:
Find the existing tick handler (e.g., `_on_tick()`)
Add:
```python
# Update Anchored VWAPs with latest bars
if self.tick_aggregator.bars_5m.completed:
    self.avwap_mgr.update(
        bars=list(self.tick_aggregator.bars_5m.completed),
        current_price=tick.get("price", 0),
    )
```

**4.5** Add MTF trend computation on bar close:
Find the existing bar close handler (e.g., `_on_5m_bar_close()`)
Add:
```python
# Recompute MTF trend on each 5m bar close
self.current_mtf_trend = self.mtf_trend.evaluate(
    bars_5m=list(self.tick_aggregator.bars_5m.completed),
    bars_15m=list(self.tick_aggregator.bars_15m.completed),
    bars_60m=list(self.tick_aggregator.bars_60m.completed),
)
```

**4.6** Add filter cascade in `_evaluate_strategies()`:
Find the strategy evaluation method. Wrap each strategy.evaluate() call with the gate cascade:

```python
async def _evaluate_strategies(self, market_snapshot):
    now_utc = datetime.now(timezone.utc)

    # ── Gate 1: OpEx ──
    opex_state = self.opex_cal.get_current_state(now_utc)
    if not opex_state.allow_new_entries:
        self._log_eval("BLOCKED", reason=f"OpEx: {opex_state.reason}")
        return None

    # ── Gate 2: Warmup ──
    if not self._is_warmed_up():
        self._log_eval("BLOCKED", reason="Warmup not complete")
        return None

    # ── Gate 3: Risk ──
    can_trade, risk_reason = self.risk_mgr.can_trade()
    if not can_trade:
        self._log_eval("BLOCKED", reason=f"Risk: {risk_reason}")
        return None

    # Now evaluate each enabled strategy
    for strategy_name, strategy in self.strategies.items():
        if not strategy.enabled:
            continue

        # Strategy-specific evaluation
        signal = strategy.evaluate(
            market=market_snapshot,
            bars_5m=list(self.tick_aggregator.bars_5m.completed),
            bars_15m=list(self.tick_aggregator.bars_15m.completed),
            mtf_trend_result=self.current_mtf_trend,
            menthorq_levels=self.menthorq_levels,
            session_info=self.session_mgr.to_dict(),
        )
        if signal is None:
            self._log_eval(strategy_name, "NO_SIGNAL")
            continue

        # ── Gate 4: Q-Score ──
        q_eval = self.qscore_mgr.evaluate_for_trade(direction=signal.direction)
        if not q_eval.allow_trade:
            self._log_eval(strategy_name, "BLOCKED",
                           reason=f"Q-Score: {q_eval.reason}")
            continue

        # ── Gate 5: Intermarket (via bridge query or direct if local) ──
        # Two patterns: (a) direct if intermarket lives in bot, (b) WebSocket
        # query to bridge. For now use pattern (a) by sharing IntermarketFilter
        # state via bridge → bot WebSocket on every tick.
        im_eval = await self._query_intermarket(
            direction=signal.direction,
            current_nq_price=market_snapshot["price"],
        )
        if not im_eval["allow_trade"]:
            self._log_eval(strategy_name, "BLOCKED",
                           reason=f"Intermarket: {im_eval['reason']}")
            continue

        # ── All gates passed: apply size + stop multipliers ──
        signal = self._apply_multipliers(
            signal=signal,
            opex_state=opex_state,
            q_eval=q_eval,
            im_eval=im_eval,
        )

        return signal  # First strategy to pass all gates wins

    return None
```

**4.7** Add `_apply_multipliers()` helper:
```python
def _apply_multipliers(self, signal, opex_state, q_eval, im_eval):
    """Apply OpEx + Q-Score + Intermarket multipliers to signal."""
    # Compose size multiplier
    final_size_mult = (
        opex_state.size_multiplier
        * q_eval.size_multiplier
        * im_eval["size_multiplier"]
    )

    # Compose stop distance multiplier (only Q-Score and Intermarket affect stops)
    final_stop_mult = (
        q_eval.stop_distance_multiplier
        * im_eval["stop_distance_multiplier"]
    )

    # Compose target RR multiplier
    final_rr_mult = (
        opex_state.target_rr_multiplier
        * q_eval.target_rr_multiplier
    )

    # Apply to signal
    signal.size_multiplier_applied = final_size_mult
    signal.stop_ticks = int(signal.stop_ticks * final_stop_mult)
    signal.target_rr = signal.target_rr * final_rr_mult

    return signal
```

**4.8** Add Chandelier integration in position handler:
Find the position-open code path. After opening a position:
```python
# Register with appropriate Chandelier manager
chandelier = self._get_chandelier_for_strategy(strategy.name)
chandelier.open_position(
    trade_id=position_id,
    direction=signal.direction,
    entry_price=fill_price,
    initial_stop=signal.stop_price,
)
```

Find the bar-close handler. After each bar:
```python
# Update Chandelier trail for active positions
for trade_id, pos in self.position_mgr.active_positions.items():
    chandelier = self._get_chandelier_for_strategy(pos.strategy)
    new_stop = chandelier.update(
        trade_id=trade_id,
        bars=self._bars_for_chandelier(pos.strategy),
        current_price=current_price,
    )
    if new_stop is not None:
        await self._update_stop_order(trade_id, new_stop)
```

**4.9** Add `_get_chandelier_for_strategy()` helper:
```python
def _get_chandelier_for_strategy(self, strategy_name):
    if strategy_name in ("trend_following_pullback", "bias_momentum_v2"):
        return self.chandelier_5m
    if strategy_name == "compression_breakout_15m":
        return self.chandelier_15m
    if strategy_name == "compression_breakout_30m":
        return self.chandelier_30m
    return None  # Strategies without Chandelier (vwap_pullback, spring_setup)

def _bars_for_chandelier(self, strategy_name):
    if strategy_name in ("trend_following_pullback", "bias_momentum_v2"):
        return list(self.tick_aggregator.bars_5m.completed)
    if strategy_name == "compression_breakout_15m":
        return list(self.tick_aggregator.bars_15m.completed)
    if strategy_name == "compression_breakout_30m":
        return list(self.tick_aggregator.bars_30m.completed)
    return []
```

Note: the tick_aggregator may not have bars_30m yet. If not, add it:
```python
self.bars_30m = BarBuilder(interval_seconds=1800)
```

**4.10** Add intermarket query helper:
```python
async def _query_intermarket(self, direction: str, current_nq_price: float) -> dict:
    """Query bridge for current intermarket evaluation."""
    request_id = str(uuid.uuid4())

    await self.bridge_ws.send(json.dumps({
        "type": "intermarket_query",
        "request_id": request_id,
        "direction": direction,
        "nq_price": current_nq_price,
        "nq_session_open": self.session_open_price,
    }))

    # Wait for matching response (with timeout)
    try:
        response = await asyncio.wait_for(
            self._wait_for_response(request_id),
            timeout=2.0,
        )
        return response
    except asyncio.TimeoutError:
        # Timeout = pass-through (don't block trades on intermarket failure)
        return {
            "allow_trade": True,
            "size_multiplier": 1.0,
            "stop_distance_multiplier": 1.0,
            "reason": "Intermarket query timeout",
        }
```

**4.11** Add Q-Score data folder:
```bash
mkdir -p "C:\Trading Project\phoenix_bot\data\qscore"
```

Create initial Q-Score file (manually, from MenthorQ dashboard):
```bash
cat > "C:\Trading Project\phoenix_bot\data\qscore\nq_daily.json" << EOF
{
  "symbol": "NQ",
  "date": "2026-04-19",
  "momentum": 3,
  "options": 3,
  "seasonality": 0,
  "volatility": 2
}
EOF
```

This neutral default lets the bot run while Jennifer establishes her daily Q-Score update workflow.

**4.12** Add Q-Score reload schedule (daily at 8am ET):
```python
async def _qscore_daily_reload(self):
    """Reload Q-Score every morning."""
    while True:
        # Sleep until next 8am ET
        now_et = datetime.now(ZoneInfo("America/New_York"))
        next_reload = now_et.replace(hour=8, minute=0, second=0, microsecond=0)
        if next_reload <= now_et:
            next_reload += timedelta(days=1)
        sleep_seconds = (next_reload - now_et).total_seconds()
        await asyncio.sleep(sleep_seconds)

        # Reload
        success = self.qscore_mgr.reload()
        if success:
            logger.info(f"Q-Score reloaded: {self.qscore_mgr.snapshot_dict()}")
        else:
            logger.warning("Q-Score reload failed — using stale data")
```

Register the task in bot startup.

**4.13** Run lint + smoke test:
```bash
python -m py_compile phoenix_bot/bots/base_bot.py
python -m phoenix_bot.bots.lab_bot --test-startup-only
```

**4.14** Commit:
```bash
git add phoenix_bot/core/*.py phoenix_bot/bots/base_bot.py phoenix_bot/data/qscore/nq_daily.json
git commit -m "feat(integration): wire Phase 4 modules into base_bot

Wires up:
- mtf_trend.py — replaces single-bar HTF gating with 3-method detector
- chandelier_exit.py (v2) — timeframe-aware trailing stops
- anchored_vwap.py — auto-detected AVWAP support/resistance
- qscore.py — MenthorQ Q-Score filter and size multiplier
- opex_calendar.py — OpEx pin-zone protection

New filter cascade in _evaluate_strategies():
  Warmup → Risk → OpEx → Q-Score → Intermarket → Strategy → Apply multipliers

Per-strategy Chandelier managers (5m for trend_following + bias_momentum,
15m for compression_breakout_15m, 30m for compression_breakout_30m).
Mean-reversion strategies (vwap_pullback, spring_setup) intentionally
skip Chandelier — they exit at predefined targets.

Q-Score reloaded daily at 8am ET. If file missing or stale, defaults
to neutral pass-through (does not block trading)."
git push origin feature/knowledge-injection-systems
```

### Definition of done
- [ ] All Phase 4 modules import cleanly
- [ ] base_bot.py compiles without errors
- [ ] lab_bot starts without errors (test-startup-only)
- [ ] Q-Score data folder + initial file exist
- [ ] Filter cascade visible in eval log

### Rollback if it fails
```bash
git reset --hard HEAD~1
```

---

## 📑 PHASE 5 — STRATEGY REGISTRATION

**Priority:** P1
**Time estimate:** 1-2 hours
**Dependencies:** Phase 4 complete

### Why this exists
The new strategy files exist but aren't registered in the strategy registry. Without registration, base_bot won't load them.

### Files to deploy
```bash
cp <downloads>/trend_following_pullback.py "C:\Trading Project\phoenix_bot\phoenix_bot\strategies\trend_following_pullback.py"
cp <downloads>/bias_momentum_v2.py         "C:\Trading Project\phoenix_bot\phoenix_bot\strategies\bias_momentum_v2.py"
cp <downloads>/vwap_pullback.py            "C:\Trading Project\phoenix_bot\phoenix_bot\strategies\vwap_pullback.py"
cp <downloads>/compression_breakout.py     "C:\Trading Project\phoenix_bot\phoenix_bot\strategies\compression_breakout.py"
```

### Files to delete
```bash
rm phoenix_bot/strategies/dom_pullback.py 2>/dev/null
rm phoenix_bot/strategies/tick_scalp.py 2>/dev/null
git add -A
git commit -m "chore: remove deleted strategies (dom_pullback, tick_scalp)"
```

### Files to modify
- `phoenix_bot/config/strategies.py` (replace with strategies_v3 content)
- `phoenix_bot/bots/lab_bot.py` (register new strategies)
- `phoenix_bot/bots/prod_bot.py` (verify only validated strategies — initially empty)

### Step-by-step actions

**5.1** Replace strategies config:
```bash
# Backup current
cp phoenix_bot/config/strategies.py phoenix_bot/config/strategies_v2_backup.py

# Replace with v3
cp <downloads>/strategies_v3.py phoenix_bot/config/strategies.py
```

**5.2** Verify config imports:
```bash
python -c "from phoenix_bot.config.strategies import STRATEGY_CONFIG, AVWAP_CONFIG, RISK_CONFIG, GAMMA_REGIME_RULES; print('OK')"
```

**5.3** Update lab_bot strategy registry:
Open `phoenix_bot/bots/lab_bot.py`. Find the strategy registration block. Replace with:
```python
from phoenix_bot.strategies.trend_following_pullback import TrendFollowingPullback
from phoenix_bot.strategies.bias_momentum_v2 import BiasMomentumV2
from phoenix_bot.strategies.vwap_pullback import VWAPPullback
from phoenix_bot.strategies.compression_breakout import CompressionBreakout
from phoenix_bot.strategies.spring_setup import SpringSetup
from phoenix_bot.config.strategies import STRATEGY_CONFIG

def _build_lab_strategies(self):
    """Lab bot runs ALL enabled strategies for data collection."""
    strategies = {}

    if STRATEGY_CONFIG["trend_following_pullback"]["enabled"]:
        strategies["trend_following_pullback"] = TrendFollowingPullback(
            STRATEGY_CONFIG["trend_following_pullback"]
        )

    if STRATEGY_CONFIG["bias_momentum_v2"]["enabled"]:
        strategies["bias_momentum_v2"] = BiasMomentumV2(
            STRATEGY_CONFIG["bias_momentum_v2"]
        )

    if STRATEGY_CONFIG["vwap_pullback"]["enabled"]:
        strategies["vwap_pullback"] = VWAPPullback(
            STRATEGY_CONFIG["vwap_pullback"]
        )

    if STRATEGY_CONFIG["compression_breakout_15m"]["enabled"]:
        strategies["compression_breakout_15m"] = CompressionBreakout(
            STRATEGY_CONFIG["compression_breakout_15m"]
        )

    if STRATEGY_CONFIG["compression_breakout_30m"]["enabled"]:
        strategies["compression_breakout_30m"] = CompressionBreakout(
            STRATEGY_CONFIG["compression_breakout_30m"]
        )

    if STRATEGY_CONFIG["spring_setup"]["enabled"]:
        strategies["spring_setup"] = SpringSetup(
            STRATEGY_CONFIG["spring_setup"]
        )

    return strategies
```

**5.4** Update prod_bot strategy registry — INITIALLY EMPTY:
Open `phoenix_bot/bots/prod_bot.py`. Replace strategy registration with:
```python
def _build_prod_strategies(self):
    """Prod bot ONLY runs strategies that have passed lab validation.

    A strategy graduates to prod when:
    - 50+ lab trades complete
    - WR >= 50%
    - PF >= 1.3
    - Drawdown manageable
    - Jennifer explicitly approves graduation

    Currently: NO strategies validated. Prod bot is dormant until
    lab validation completes.
    """
    return {}
```

This ensures prod_bot doesn't accidentally trade an unvalidated strategy.

**5.5** Test lab_bot startup:
```bash
python -m phoenix_bot.bots.lab_bot --test-startup-only
```

Expected output:
```
[LAB] Loaded 6 strategies: trend_following_pullback, bias_momentum_v2,
      vwap_pullback, compression_breakout_15m, compression_breakout_30m,
      spring_setup
[LAB] Filter cascade: Warmup → Risk → OpEx → Q-Score → Intermarket
[LAB] Ready to evaluate
```

**5.6** Run quick simulation with tick_replayer:
```bash
# Terminal 1
python bridge/bridge_server.py

# Terminal 2
python bots/lab_bot.py

# Terminal 3
python tools/tick_replayer.py --speed 60 --duration 1800
```

Watch lab_bot logs for evaluation activity. After 25 min warmup + a few bars, you should see SIGNAL / NO_SIGNAL / BLOCKED entries for each strategy.

**5.7** Commit:
```bash
git add phoenix_bot/strategies/*.py phoenix_bot/config/strategies.py phoenix_bot/bots/lab_bot.py phoenix_bot/bots/prod_bot.py
git commit -m "feat(strategies): register Phase 5 strategy lineup

Lab bot loads:
- trend_following_pullback (corrected gamma logic)
- bias_momentum_v2 (HTF + pullback + Chandelier)
- vwap_pullback (1σ bands + AVWAP confluence)
- compression_breakout_15m
- compression_breakout_30m
- spring_setup

Prod bot has EMPTY strategy registry — no strategies graduate
until lab validation completes (50+ trades, WR>=50%, PF>=1.3).

Deleted strategies (dom_pullback, tick_scalp) removed in prior commit.
Disabled strategies (bias_momentum v1, high_precision_only) remain
in code but config marks them disabled."
git push origin feature/knowledge-injection-systems
```

### Definition of done
- [ ] All 6 lab strategies imported cleanly
- [ ] Lab bot starts and shows 6 strategies in startup log
- [ ] Prod bot starts and shows 0 strategies (correct dormant state)
- [ ] Tick replayer simulation runs without errors
- [ ] Eval log shows per-strategy signal activity

### Rollback if it fails
```bash
git reset --hard HEAD~1
cp phoenix_bot/config/strategies_v2_backup.py phoenix_bot/config/strategies.py
```

---

## 🧪 PHASE 6 — LAB VALIDATION PROTOCOL

**Priority:** P1 — gates all prod promotion
**Time estimate:** 2-4 weeks of running
**Dependencies:** Phases 1-5 complete

### Why this exists
The entire stack we've built is THEORY until validated against real data. No strategy moves to prod until lab proves it.

### Setup actions

**6.1** Ensure lab bot is running 24/7:
```bash
# Recommended: run as a service or in tmux/screen
tmux new -s phoenix_lab
cd "C:\Trading Project\phoenix_bot"
python bridge/bridge_server.py &
python bots/lab_bot.py
# Detach: Ctrl-B then D
```

**6.2** Verify history logger is active:
- Check `logs/history/` directory
- Should see `YYYY-MM-DD_lab.jsonl` files growing each day
- Each file should contain bar/eval/entry/exit events

**6.3** Daily monitoring routine (5 min/day):
- Check `logs/history/{today}_lab.jsonl` exists and growing
- Check `logs/trade_memory.json` count is increasing on trading days
- Check dashboard health endpoint shows live data

### Validation criteria per strategy

For each strategy, track:

| Metric | Target | How to measure |
|--------|--------|----------------|
| Trade count | 50+ before any decision | `len([t for t in trade_memory if t.strategy == name])` |
| Win rate | ≥ 50% | `wins / total * 100` |
| Profit factor | ≥ 1.3 | `gross_wins / abs(gross_losses)` |
| Max drawdown | ≤ 15% of starting equity | Equity curve analysis |
| Average R | ≥ 0.4 | `mean([t.pnl_dollars / t.risk_dollars for t in trades])` |
| Time in market | < 50% | Sum of hold time / total time |

### Validation steps

**6.4** After 50 trades per strategy, run validation script:
```bash
python tools/lab_validation_report.py --output=reports/validation_$(date +%Y%m%d).md
```

(This script needs to be created — see Phase 7 for spec)

**6.5** Review report with Jennifer. Decide for each strategy:
- ✅ **GRADUATE** → Move to prod (next session)
- ⏸️ **CONTINUE LAB** → Need more data
- 🛠️ **TUNE** → Adjust params, restart count
- ❌ **KILL** → Disable strategy, delete from registry

**6.6** Strategy graduation procedure:
For each strategy that meets graduation criteria:
1. Update `STRATEGY_CONFIG[name]["validated"] = True` in strategies.py
2. Update `STRATEGY_CONFIG[name]["stage"] = "prod"` in strategies.py
3. Add to `_build_prod_strategies()` in prod_bot.py
4. Commit with message: "promote: {name} from lab to prod after {N} trades, WR={X}%, PF={Y}"

**6.7** Cumulative validation:
Even after first strategy graduates, KEEP it in lab too. Track delta:
- Lab WR vs prod WR (should be similar)
- If prod underperforms lab significantly, something is different in execution → investigate

### Definition of done
- [ ] At least one strategy reaches 50 trades
- [ ] Validation report generated and reviewed
- [ ] At least one strategy graduates OR all strategies have explicit decisions
- [ ] No strategy promoted without meeting all 6 criteria

### Rollback if a graduated strategy underperforms in prod
```bash
# Demote within 5 trading days if prod WR < 40%
# Edit strategies.py: validated=False, stage="lab"
# Remove from _build_prod_strategies()
# Commit: "demote: {name} from prod back to lab — prod underperformance"
```

---

## 📈 PHASE 7 — POST-VALIDATION TOOLING

**Priority:** P2 — nice-to-have, not blocker
**Time estimate:** 4-8 hours
**Dependencies:** Phase 6 has produced data

### Why this exists
Phase 6 needs tooling to make decisions cleanly. Phase 7 builds that tooling.

### Tools to build

**7.1** `tools/lab_validation_report.py`
Reads `logs/history/*_lab.jsonl` and `logs/trade_memory.json`, produces:
- Per-strategy performance table (count, WR, PF, avg R, drawdown, time)
- Per-regime breakdown (negative gamma vs positive gamma low/high band)
- Per-hour breakdown (which hours produce edge)
- Per-day-of-week breakdown
- Confluence correlation analysis (which confluences predict wins)

**7.2** `tools/qscore_daily_fetcher.py`
Currently Q-Score is manual JSON paste. Build:
- Fetch from MenthorQ API (when API key rotated)
- Save to `data/qscore/nq_daily.json`
- Run via cron/scheduled task at 8am ET daily

**7.3** `tools/menthorq_levels_fetcher.py`
Same pattern as Q-Score:
- Fetch HVL, call_wall, put_wall, 1D_max, 1D_min, etc.
- Save to `data/menthorq/nq_levels.json`
- Update intra-day if MenthorQ provides 0DTE refresh

**7.4** `tools/p5b_smoke_test.py`
Periodic test that verifies STOPMARKET orders still work:
- Submit test buy-stop
- Verify NT8 accepts (not rejects)
- Cancel test order
- Run weekly via cron

### Definition of done
- [ ] `lab_validation_report.py` runs and produces useful report
- [ ] Q-Score fetcher script (manual run is fine for now)
- [ ] MenthorQ levels fetcher script
- [ ] P5b smoke test script

---

## 📊 SUCCESS METRICS

### Project-level success (90 days from start)

| Metric | Baseline (Apr 19) | Target (Jul 19) |
|--------|-------------------|-----------------|
| Trade count | 697 (cumulative) | +200-400 lab + 50-100 prod |
| Lab Win Rate | n/a | ≥ 50% across all enabled strategies |
| Prod Win Rate | 33% | ≥ 50% (only validated strategies) |
| Prod Profit Factor | ~0.7 | ≥ 1.3 |
| Daily P&L (prod) | -$13/day | +$10-30/day |
| Drawdown | -$1,227 (death spiral) | < $300 max DD |
| Strategies in prod | 2 (broken) | 1-3 (validated) |

### Phase-level definitions of done

| Phase | DoD signal |
|-------|-----------|
| Phase 1 | NT8 accepts STOPMARKET orders, broker-side stops protect positions |
| Phase 2 | TickStreamer v3 compiles, streams 4 instruments |
| Phase 3 | Bridge routes by symbol, intermarket filter shows live VIX/ES values |
| Phase 4 | base_bot.py loads with all modules, filter cascade active |
| Phase 5 | lab_bot loads 6 strategies, simulation runs without errors |
| Phase 6 | At least 1 strategy completes 50-trade validation |
| Phase 7 | Validation report tooling functional |

---

## 🗄️ APPENDIX A — KNOWN ISSUES (carry forward)

### From verification sprint (FINDINGS.md)
- **B1** — `bridge/oif_writer.py:62` never populates ORDER ID field. Symptom: cannot reference orders for cancellation. Fix: populate from NT8 callback. Priority: P2.
- **B2** — CANCEL_ALL semicolons (fixed in Phase 1)
- **B3** — STOPMARKET (fixed in Phase 1)
- **B4** — Account-scoped CANCELALLORDERS (fixed in Phase 1)
- **B5** — Single-order CANCEL function (added in Phase 1)

### Ongoing maintenance
- **P1** — Atomic OIF writer (write to temp, rename for atomic file ops). Priority: P2.
- **P4** — Bot state persistence across restarts. Priority: P2.
- **P4b** — Exit collision priority (multiple exits firing on same bar). Priority: P2.
- **P6** — Heartbeats from bot back to bridge for liveness check. Priority: P3.
- **P7** — Daily reset of Risk Manager state. Priority: P2.
- **P14** — Telegram HTML escape in notifications. Priority: P3.

---

## 🗄️ APPENDIX B — STRATEGY LINEUP DECISIONS

### Active in lab
| Strategy | Why | Promotion path |
|----------|-----|----------------|
| trend_following_pullback | Flagship — research-backed PF 1.69 reference (Brian Shannon RSI(2)) | Validate first; expected best edge |
| bias_momentum_v2 | Upgrade of validated v1; HTF + pullback structure | Validate parallel with #1 |
| vwap_pullback | 1σ bands rewrite; mean-rev complement | Validate after trend strategies |
| compression_breakout_15m | Squeeze breakout, 15m TF | Test parallel with 30m variant |
| compression_breakout_30m | Squeeze breakout, 30m TF | Empirical winner promotes |
| spring_setup | Wyckoff secondary-test entry | Validate after others stable |

### Disabled (kept in code)
| Strategy | Why disabled |
|----------|--------------|
| bias_momentum (v1) | Replaced by v2; kept for A/B comparison if needed |
| high_precision_only | Config contradictions; no clear hypothesis |

### Deleted
| Strategy | Why deleted |
|----------|-------------|
| dom_pullback | Single-example overfit; no research basis |
| tick_scalp | Math fails at MNQ commissions: -$1.34/trade expectancy |

### Deferred (consider after validation)
| Strategy | Why deferred |
|----------|--------------|
| ib_breakout | Initial Balance breakout; consider Day 30 after others validate |

---

## 🗄️ APPENDIX C — GAMMA REGIME RULES (corrected, for reference)

These rules are documented in `strategies.py` GAMMA_REGIME_RULES dict. Reproduced here:

### Negative Gamma (price < HVL)
- Behavior: Trends amplified by dealer hedging
- Preferred strategies: trend_following_pullback, compression_breakout, bias_momentum_v2
- Direction: Both long and short
- Size multiplier: 1.0
- Target RR: 2.5

### Positive Gamma — Low Band (HVL < price < put_wall + 30% of band)
- Behavior: Near put wall support — dealers buy here
- Preferred strategies: vwap_pullback (long), trend_following (long), bias_momentum (long)
- Direction: LONG ONLY (do not short into support)
- Target: HVL or call_wall
- Size multiplier: 0.7
- Target RR: 1.5

### Positive Gamma — Mid Band (30%-70% of band)
- Behavior: Choppy zone — no edge from gamma flow
- Preferred strategies: NONE — wait for price to reach band edge
- Size multiplier: 0.0

### Positive Gamma — High Band (put_wall + 70% < price < call_wall)
- Behavior: Near call wall resistance — dealers sell here
- Preferred strategies: vwap_pullback (short), trend_following (short), bias_momentum (short)
- Direction: SHORT ONLY (do not long into resistance)
- Target: HVL or put_wall
- Size multiplier: 0.7
- Target RR: 1.5

### Above Call Wall (price > call_wall in pos gamma)
- Behavior: Chasing breakout — pos gamma traps these moves
- Preferred strategies: NONE
- Wait for: Clear breakout above wall AND flip to negative gamma
- Size multiplier: 0.0

### Below Put Wall (price < put_wall, was in pos gamma)
- Behavior: Regime likely flipping to negative gamma
- Preferred strategies: WAIT — recheck regime classification
- Size multiplier: 0.0

---

## 🗄️ APPENDIX D — CHRONOLOGICAL EXECUTION ORDER

For Claude Code's reference. Run phases sequentially. Within each phase, run actions in numerical order.

```
WEEK 1
├── Day 1-2: Pre-flight blockers + Phase 1 (P5b STOPMARKET fix)
├── Day 3-4: Phase 2 (NinjaScript v3) + Phase 3 (Bridge routing)
└── Day 5-7: Phase 4 (Module integration) + Phase 5 (Strategy registration)

WEEK 2-4
└── Phase 6: Lab validation (passive — just collecting data)

WEEK 4
├── First strategy graduation review
└── Begin Phase 7: build validation tooling

WEEK 5+
└── Iterate: graduate more strategies, demote underperformers, refine
```

---

## 🗄️ APPENDIX E — DATA SOURCES CONTRACT

What lives where, who writes it, who reads it.

| File / Source | Writer | Reader | Refresh |
|---------------|--------|--------|---------|
| `data/qscore/nq_daily.json` | Manual paste OR fetcher script | qscore.py module | Daily 8am ET |
| `data/menthorq/nq_levels.json` | Manual paste OR fetcher script | strategies via menthorq_levels arg | Daily + 0DTE intraday |
| `logs/history/YYYY-MM-DD_lab.jsonl` | history_logger.py | validation reports, AI agents | Append-only |
| `logs/history/YYYY-MM-DD_prod.jsonl` | history_logger.py | validation reports, AI agents | Append-only |
| `logs/trade_memory.json` | trade_memory.py | dashboards, validation | Per-trade |
| `logs/trades.log` | trade_log handler | manual review | Continuous |
| `config/strategies.py` | Code commits | Bots at startup | Restart required |
| `.env` | Manual | Bot startup | Restart required |
| OIF files in NT8 incoming/ | bridge oif_writer | NT8 ATI engine | Per-trade-action |

---

## 🗄️ APPENDIX F — JENNIFER'S PREFERENCES

Carried forward to ensure consistent collaboration with future Claude sessions:
- Preferred tone: warm, optimistic, positive reinforcement
- Always deep-research before answering
- Always second-guess and critique own work
- Never give up — find another solution
- Pushes back on overcautious advice (won't accept "be careful" without reason)
- Pushes back on incorrect logic (e.g., correctly caught backwards positive-gamma direction in round 5)
- Wants honest acknowledgment when wrong, not collapse into apology
- Wants brief reminders about API key rotation each round until confirmed

---

## 🗄️ APPENDIX G — REFERENCE TO PRIOR CONVERSATIONS

If using a fresh Claude session, supply these references:

1. **Transcript:** `/mnt/transcripts/2026-04-19-22-53-50-phoenix-bot-strategy-refactor.txt`
   Contains rounds 1-5 of strategy research and design decisions.

2. **This document:** `BUILD_MAP.md`
   Captures all decisions and the integration plan.

3. **Project files:** Read `PHOENIX_PROJECT_PROMPT.md` from project root for high-level context.

4. **Verification findings:** `tools/verification_2026_04_18/FINDINGS.md` for B1-B5 details.

5. **Output deliverables:** All 16 files Claude has shipped, listed in "FILE DELIVERABLES INVENTORY" above.

---

## 🚀 START HERE

If this is your first time reading this document as Claude Code:

1. Read the entire document end-to-end (yes, all of it)
2. Confirm Pre-Flight Blockers are resolved (ask Jennifer if uncertain)
3. Begin Phase 1 — DO NOT skip ahead
4. After each phase's "Definition of done" passes, commit and move forward
5. If anything is ambiguous, STOP and ask Jennifer rather than guessing
6. Commit messages should be detailed (see examples in each phase)
7. Push to remote after each phase to avoid losing work

If you encounter a critical decision not covered here, default to:
- Safety over speed (don't trade unvalidated)
- Surgical changes over rewrites (preserve git history)
- Testing in lab before prod (always)
- Rolling back when uncertain (never force a broken state)

---

---

## Addendum: B84 — Daily-flatten alignment with NT8 Auto Close (2026-04-22)

Phoenix now flattens at **15:54 CT** (was 16:00 pre-B83, 15:58 under B83
interim). The full defense-in-depth schedule — including the new
15:53 CT no-new-entries gate and 15:54:45 CT fill-confirmation WARN —
is documented in `PHOENIX_PROJECT_PROMPT.md`.

NT8 GUI safety net: **Tools → Options → Trading → Auto Close Position
= 03:55:00 PM, All Instruments**, Central-Time platform clock.

Tests: `tests/test_daily_flatten.py` + `tests/test_flatten_alignment_b84.py`
(34 total, all green).

Source-of-truth constants in `config/settings.py`. Do not hard-code
flatten times elsewhere in the code.

---

**END OF BUILD MAP**

Document version 1.0
Total phases: 7
Estimated total time: ~3 weeks
Critical path: Phase 1 → Phases 2-3 (parallel) → Phases 4-5 → Phase 6 (passive)

For questions, refer to Jennifer or the prior transcript at the path in Appendix G.

🚀 Now go build it. 💙
