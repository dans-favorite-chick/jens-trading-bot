# Trading Bot Rebuild Plan: "Phoenix" 

## Context

The current setup is spread across 5+ bot trees (Jen V1, V2, V3, Research Bot, MNQ v5, Council of Seven), two project directories, and a GitHub repo that's months behind. The NT8 connection is fragile (MarketDataBroadcasterV3 disconnects intermittently), the dashboard lacks connection monitoring and live controls, and no single tree represents the "real" system. The user wants to consolidate all the good ideas into one rock-solid, clean project.

**Decisions locked in:**
- NT8 data: New lean tick-only indicator (Python does all derived math)
- Bot structure: TWO bots (Production + Experimental), shared bridge and dashboard
- Dashboard: Flask + HTML/JS to start (no build step, Claude Code-friendly), with planned migration to React components (MNQ v5 style) once stable
- Legacy: Archive old trees, Council of Seven preserved as optional advisory layer

---

## Phase 0: Project Setup & Git Safety Net

**Goal:** Create the new project directory, git init, archive old trees.

### Steps
1. Create `C:\Trading Project\phoenix_bot\` with this structure:
   ```
   phoenix_bot/
   ├── CLAUDE.md                    # New system description
   ├── .gitignore
   ├── requirements.txt
   ├── config/
   │   ├── settings.py              # All config in one place (ports, paths, limits, instruments)
   │   └── strategies.py            # Strategy parameters (togglable, slider-friendly)
   ├── bridge/
   │   ├── bridge_server.py         # WebSocket server :8765 (NT8) + :8766 (bots)
   │   └── oif_writer.py            # OIF trade file writer (proven reliable path)
   ├── ninjatrader/
   │   ├── TickStreamer.cs           # NEW lean tick-only indicator
   │   ├── MarketDataExporter.cs    # Plan B file-based fallback (copy from NinjaTrader/)
   │   └── INSTALL.md               # Step-by-step NT8 install guide
   ├── bots/
   │   ├── base_bot.py              # Shared bot logic (connect, receive ticks, risk checks)
   │   ├── prod_bot.py              # Production bot (validated strategies only)
   │   └── lab_bot.py               # Experimental bot (strategy sandbox)
   ├── strategies/
   │   ├── __init__.py
   │   ├── base_strategy.py         # Abstract strategy interface
   │   ├── bias_momentum.py         # Port from V3 BiasMomentumFollow
   │   ├── spring_setup.py          # Port from MNQ v5 Spring pattern
   │   ├── vwap_pullback.py         # Port from V3 VWAPPullback
   │   └── ... (more as needed)
   ├── core/
   │   ├── tick_aggregator.py       # Builds bars, ATR, VWAP, EMA, CVD from raw ticks
   │   ├── risk_manager.py          # Daily limits, VIX filter, recovery mode, position sizing
   │   ├── session_manager.py       # 8 market regimes, time windows
   │   ├── position_manager.py      # Track open positions, P&L, stop/target
   │   └── trade_memory.py          # Trade log + adaptive learning data
   ├── dashboard/
   │   ├── server.py                # Flask app, REST API endpoints
   │   ├── templates/
   │   │   └── dashboard.html       # Single-file dashboard (extend V3 base)
   │   └── static/                  # Alert sounds, icons
   ├── agents/                      # Optional AI advisory (Phase 2+)
   │   ├── council_gate.py          # Council of Seven (session start advisory)
   │   ├── pretrade_filter.py       # Gemini pre-trade check (3s timeout)
   │   └── session_debriefer.py     # End-of-session AI coaching
   ├── logs/
   ├── launch_prod.bat
   ├── launch_lab.bat
   └── launch_bridge.bat
   ```

2. `git init` in `phoenix_bot/`, add `.gitignore` (venv/, __pycache__/, logs/, *.pyc, tmp_*, .env), initial commit.

3. In `C:\Trading Project\`:
   - Rename `trading_bot_project/` to `archive_trading_bot_project/`
   - Rename `mnq_trading_bot/` to `archive_mnq_trading_bot/`
   - Tag current state: these folders become read-only reference

---

## Phase 1: NT8 Connection (The Critical Path)

**Goal:** A reliable, simple tick stream from NT8 to Python, with file-based fallback.

### 1A: New Lean Tick Indicator (`TickStreamer.cs`)

**Design principles (learned from V3 failures):**
- NinjaScript **Indicator** (NOT Strategy — strategies crash on ErrorHandling=Stop)
- WebSocket **CLIENT** connecting OUT to Python server (the direction flip that works)
- Tick-level only (CalculateOnBarClose = false, Calculate = Calculate.OnEachTick)
- Sends: `{"type":"tick","price":18527.5,"bid":18527.25,"ask":18527.75,"vol":1,"ts":"2026-04-09T10:30:00.123"}`
- Heartbeat every 3 seconds: `{"type":"heartbeat","ts":"..."}`
- Auto-reconnect with 2s backoff (fix V3's missing reconnect)
- NO derived math (no ATR, VWAP, CVD, confluence) — Python owns all computation
- ~80 lines of C#, not 300+

**Key code references for the build:**
- WebSocket CLIENT pattern: `Jen_Trading_Botv1/MarketDataBroadcasterV3.cs:51` (`private ClientWebSocket pythonSocket`)
- Connection method: `Jen_Trading_Botv1/NT8WebSocketRelayAddOn.cs:72-74` (ConnectAsync pattern)
- Tick data access: `mnq_trading_bot/ninjatrader/MNQDataBridge.cs:57` (OnEachTick, newline-delimited JSON)
- StringBuilder JSON (no Newtonsoft in NT8): V3.cs manual JSON building pattern

**Install path:** `C:\Users\Trading PC\AppData\Roaming\NinjaTrader 8\bin\Custom\Indicators\`
- Document in `ninjatrader/INSTALL.md`

### 1B: File-Based Fallback (`MarketDataExporter.cs`)

**Existing working code:** `trading_bot_project/NinjaTrader/MarketDataExporter.cs`
- Writes `C:\temp\mnq_data.json` with atomic `.tmp` → `.json` rename
- Python polls every 250ms (upgrade from 1s)
- Bridge auto-switches to file mode if WebSocket stale >10s

### 1C: Bridge Server (`bridge/bridge_server.py`)

**Ports (unchanged from legacy — proven working):**
- `:8765` — WebSocket SERVER, NT8 connects as client, sends ticks
- `:8766` — WebSocket SERVER, bots connect as clients, receive processed data

**New features over legacy bridge:**
- **Heartbeat tracking:** If no tick or heartbeat for 10s → mark NT8 disconnected, switch to file fallback
- **Message buffer:** Ring buffer of last 100 ticks, so late-connecting bots get recent context
- **Health endpoint:** HTTP `:8767/health` returns JSON: `{nt8_connected, nt8_last_tick_age_ms, bots_connected: ["prod","lab"], uptime_s}`
- **Structured logging:** `logs/bridge.log` with connection events, data flow stats every 60s

**OIF trade path (keep as-is — reliable):**
- `oif_writer.py` extracted from `Jen_Trading_Botv1/trading_controller.py:62-107`
- Writes to `C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\incoming\`
- Format: `PLACE;Sim101;MNQM6 06-26;BUY;1;MARKET;0;0;DAY;;;;`
- Reads fill confirmations from `outgoing/` folder
- Phase 1: MARKET orders only (no OCO brackets yet)

### 1D: Tick Aggregator (`core/tick_aggregator.py`)

**Builds from raw ticks (Python-side, not NT8):**
- 1-min, 5-min, 15-min, 60-min bars (OHLCV)
- ATR(14) per timeframe
- VWAP (manual: cumsum(typical_price * vol) / cumsum(vol), daily reset)
- EMA 9/21
- CVD (cumulative volume delta: uptick vol - downtick vol)
- Multi-TF bias votes (trend direction per TF)

**Why Python-side:** Single source of truth, easier to debug, no C# complexity, can be unit-tested.

---

## Phase 2: Bot Architecture

**Goal:** Two bots sharing the bridge, each with pluggable strategies.

### 2A: Base Bot (`bots/base_bot.py`)
- Connects to bridge on `:8766`
- Receives ticks, feeds to `tick_aggregator`
- On each new bar: runs strategy pipeline
- Reports state to dashboard via shared state dict or REST push

### 2B: Production Bot (`bots/prod_bot.py`)
- Extends base_bot
- Only runs strategies marked `validated: true` in `config/strategies.py`
- Session windows: primary 8:30-10:00 AM CST, secondary per config
- Risk limits: $20 max/trade, $45 daily stop, recovery mode at -$30
- Logs every trade with entry reasoning to `logs/trades.log` + `trade_memory`

### 2C: Lab Bot (`bots/lab_bot.py`)
- Extends base_bot
- Runs experimental strategies (including unvalidated ones)
- Can run 24/7 (afterhours regimes)
- Separate P&L tracking, does NOT affect prod daily limits
- Feeds performance data to `trade_memory` for strategy graduation

### 2D: Strategy Interface (`strategies/base_strategy.py`)
```python
class BaseStrategy:
    name: str
    enabled: bool = True
    validated: bool = False  # Only validated strategies run in prod
    
    def evaluate(self, bars: dict, tick: dict, session: SessionInfo) -> Signal | None:
        """Return a Signal(direction, stop, target, confidence, reason) or None."""
        raise NotImplementedError
    
    @property
    def params(self) -> dict:
        """Return current tunable parameters (for dashboard sliders)."""
```

**Strategies to port (Phase 2):**

| Strategy | Source | Validated | Notes |
|----------|--------|-----------|-------|
| BiasMomentumFollow | V3 `strategies_v3.py:76` | Yes | Baseline, proven |
| SpringSetup | MNQ v5 `tech_bot.py:43` | Yes | Rule-of-Three (wick + VWAP + delta) |
| VWAPPullback | V3 `strategies_v3.py` | No | Needs more testing |
| HighPrecisionOnly | V3 `strategies_v3.py` | No | Lab candidate |
| TickScalp | V3 `strategies_v3.py` | No | Lab candidate |

### 2E: Risk Manager (`core/risk_manager.py`)

**Consolidated from all bots — best rules from each:**

| Rule | Source | Value |
|------|--------|-------|
| Max loss per trade | V1 config.py | $20 |
| Daily stop | V1 config.py | -$45 |
| Recovery mode trigger | V1 elite_v1_engine.py | -$30 → 50% size, raise thresholds |
| VIX extreme threshold | V1 config.py | >40 → NO TRADE |
| VIX high threshold | V1 config.py | 30-40 → 50% size |
| Dynamic risk sizing (entry quality) | MNQ v5 Elite Upgrade #1 | A++ $15, B $12, C $8 |
| Volatility regime adaptation | MNQ v5 Elite Upgrade | ATR-based target/hold adjustment |
| Max trades per session | V2 config_v2.py | 6 |
| Cooloff after 3 losses | V3 session_manager.py | 5 min pause |

### 2F: Session Manager (`core/session_manager.py`)

**Port from V3 `session_manager.py` — 8 market regimes:**
- OVERNIGHT_RANGE, PREMARKET_DRIFT, OPEN_MOMENTUM, MID_MORNING
- AFTERNOON_CHOP, LATE_AFTERNOON, CLOSE_CHOP, AFTERHOURS
- Each regime defines: allowed strategies, min confluence override, position size multiplier

---

## Phase 3: Dashboard

**Goal:** Single Flask dashboard monitoring both bots, with live controls and connection alerting.

### 3A: Foundation (extend V3 `v3_dashboard.html` pattern)
**File:** `dashboard/templates/dashboard.html`
**Server:** `dashboard/server.py` (Flask, port 5000)

### 3B: Panels (what it shows)

1. **Connection Health Bar** (TOP, always visible — LOUD)
   - NT8 feed: GREEN/YELLOW(stale >5s)/RED(dead >10s) with age counter
   - Bridge :8765 (NT8 side): status + last message age
   - Bridge :8766 (bot side): status + connected bot count
   - Prod bot: status + current state (IDLE/SCANNING/IN_TRADE)
   - Lab bot: status + current state
   - **Audio alert** on any RED transition (browser notification sound)
   - **Red banner overlay** on connection loss (sticky until restored)

2. **Connection Log** (scrollable, persistent within session)
   - Timestamped events: "10:31:02 NT8 connected", "10:35:17 NT8 heartbeat stale (8s)", etc.
   - Color-coded by severity
   - Last 200 events kept in memory

3. **Bot Tabs** (switch between Prod and Lab — from V3)
   - Each tab shows: position bar, P&L, trade log, active strategies

4. **Strategy Control Panel** (new)
   - Toggle switches per strategy (on/off)
   - **Aggression profile** buttons: Safe / Balanced / Aggressive
   - **Live sliders** for key parameters:
     - Min confluence threshold (2.0 – 5.0, step 0.1)
     - Momentum confidence minimum (40 – 90, step 5)
     - Risk per trade ($5 – $20, step $1)
     - Max daily loss ($20 – $60, step $5)
   - Sliders update `config/strategies.py` values at runtime via POST API
   - Changes are **session-only by default** (reset on restart), with a "Save to Config" button that writes to disk

5. **Trade Log** (from V2 — the user's favorite feature)
   - Expandable "Why This Trade?" cards with:
     - Strategy name
     - Entry reason text
     - Confluences met (bulleted)
     - Market snapshot at entry (price, ATR, VIX, bias, regime)

6. **Market Data Panel**
   - Current price, ATR, VWAP, bias, TF alignment votes
   - Volume ratio, CVD direction
   - Session regime pill (OPEN_MOMENTUM, etc.)

7. **Daily Stats**
   - Balance, session P&L, daily loss used (progress bar), win rate, wins/losses

### 3C: API Endpoints

```
GET  /api/status           — full bot state (both bots)
GET  /api/system-health    — connection status for all components
GET  /api/connection-log   — last 200 connection events
GET  /api/trades           — trade history
GET  /api/strategies       — current strategy states and params
POST /api/runtime-controls/profile     — set Safe/Balanced/Aggressive
POST /api/runtime-controls/strategy    — toggle strategy on/off
POST /api/runtime-controls/params      — update slider values
POST /api/runtime-controls/save        — persist current params to disk
POST /api/test-trade                   — paper test (lab bot only)
```

### 3D: Code Organization for Claude Code

The dashboard HTML will be ONE file (like V2/V3) but with clear section markers:
```html
<!-- ===== SECTION: Health Bar ===== -->
<!-- ===== SECTION: Connection Log ===== -->
<!-- ===== SECTION: Bot Tabs ===== -->
<!-- ===== SECTION: Strategy Controls ===== -->
<!-- ===== SECTION: Trade Log ===== -->
<!-- ===== SECTION: JavaScript ===== -->
```

Server endpoints organized by concern in `server.py` with clear route groupings.
`config/strategies.py` is the file Claude Code edits for strategy parameter changes — clean dict format, no complex nesting.

---

## Phase 4: Council of Seven (Optional Advisory Layer)

**NOT in the hot path.** Runs at session start, writes journal, never blocks trades.

### 4A: Session Start Advisory (`agents/council_gate.py`)
- Port from `Afterhours_Test_Bot_v3/council_gate.py`
- 7 agents evaluate morning conditions with live market data
- Output: session journal to `logs/council_journal_YYYY-MM-DD.txt`
- Dashboard shows: "Council says: BULLISH, 6/7 agree" (informational only)
- Re-evaluates every 2 hours or on regime shift

### 4B: Pre-Trade Filter (`agents/pretrade_filter.py`)
- Port from V3 `ai_advisor.py:52-126`
- Gemini-Flash quick check before entry (3s timeout → default CLEAR)
- Can be toggled off via dashboard

### 4C: Session Debriefer (`agents/session_debriefer.py`)
- Runs at session end
- Analyzes today's trades, writes coaching notes
- Saved to `logs/ai_debrief_YYYY-MM-DD.txt`

---

## Phase 5: Future (NOT Phase 1)

- OCO bracket orders via OIF (stop + target on entry)
- Research Bot pipeline (automated backtesting + optimization)
- Trade clustering analysis (MNQ v5 Upgrade #4 — after 30+ trades)
- AI parameter tuner (5D — suggestions after N trades)
- Cockpit 12-layer grading system (MNQ v5 — visual go/no-go)

---

## Implementation Order

| Step | What | Files | Est. Complexity |
|------|------|-------|-----------------|
| **0.1** | Create directory structure + git init | All dirs, .gitignore, CLAUDE.md | Low |
| **0.2** | Archive old trees | Rename dirs | Low |
| **1.1** | Write TickStreamer.cs | ninjatrader/TickStreamer.cs | Medium |
| **1.2** | Write bridge_server.py + oif_writer.py | bridge/ | Medium |
| **1.3** | Write tick_aggregator.py | core/tick_aggregator.py | Medium |
| **1.4** | Write config/settings.py | config/ | Low |
| **1.5** | Test: NT8 → bridge → console output | Manual integration test | Medium |
| **2.1** | Write base_bot.py | bots/ | Medium |
| **2.2** | Write risk_manager.py + session_manager.py + position_manager.py | core/ | Medium |
| **2.3** | Write base_strategy.py + first 2 strategies | strategies/ | Medium |
| **2.4** | Write prod_bot.py + lab_bot.py | bots/ | Low |
| **2.5** | Test: full signal → paper trade loop | Manual test | Medium |
| **3.1** | Write dashboard server.py + dashboard.html | dashboard/ | Medium-High |
| **3.2** | Wire health monitoring + connection log | dashboard/ | Medium |
| **3.3** | Wire strategy controls + sliders | dashboard/ + config/ | Medium |
| **3.4** | Wire trade log with "Why This Trade?" | dashboard/ | Low |
| **4.1** | Port council_gate.py (optional) | agents/ | Low |

---

## Verification Plan

### Phase 1 Smoke Test
1. Start bridge: `python bridge/bridge_server.py` → should print listening on :8765, :8766, :8767
2. Load TickStreamer.cs in NT8 on MNQM6 chart → bridge console should show ticks arriving
3. Verify heartbeat: pause chart (no ticks) → heartbeat messages every 3s in bridge log
4. Kill NT8 indicator → bridge should detect disconnect within 10s, log event, switch to file fallback
5. Write test OIF: `python bridge/oif_writer.py --test` → check `incoming/` folder for file

### Phase 2 Bot Test
6. Start prod_bot: `python bots/prod_bot.py` → connects to bridge, receives bars
7. Verify tick_aggregator builds correct 1m/5m bars from tick stream
8. Force a signal (paper test API) → OIF file written → NT8 fills → outgoing file → bot logs fill

### Phase 3 Dashboard Test
9. Open `localhost:5000` → health bar shows all GREEN when everything connected
10. Kill bridge → health bar goes RED within 5s, audio alert fires, red banner appears
11. Move aggression slider → verify config value changes, bot picks up new threshold on next bar
12. Execute paper trade → trade appears in log with expandable reasoning

---

## Key Design Decisions (Do NOT Change)

| Decision | Why | Reference |
|----------|-----|-----------|
| NT8 Indicator, not Strategy | Strategies crash with ErrorHandling=Stop | PROJECT_MEMORY.md:134 |
| Python is WS SERVER, NT8 connects OUT | NT8 HttpListener failed; client model works | CLAUDE.md:23 |
| OIF files for trade execution | File path is "consistent and reliable" per user | trading_controller.py:62 |
| OneDrive NT8 path must not move | NT8 won't boot without it | PROJECT_MEMORY.md:159 |
| No Newtonsoft.Json in C# | Not bundled with NT8 | Use StringBuilder |
| VWAP calculated in Python | Order Flow+ license required in NT8 | Manual cumsum formula |
