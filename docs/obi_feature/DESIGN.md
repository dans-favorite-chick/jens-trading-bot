# OBI Feature — Full Design

Design for adding Order Book Imbalance (passive-order) signals to phoenix_bot, complementing the existing CVD (active-order) instrumentation.

## Why OBI

**CVD** answers: who's pushing right now? (aggressive flow)
**OBI** answers: who's defending? where will price find resistance or absorption? (passive flow)

Together they form a complete picture. The academic literature is genuinely strong:

- Cont, Kukanov & Stoikov: order flow imbalance has a near-linear relationship with short-horizon price changes. Aggregated over brief intervals, the link to subsequent returns remains strong, especially within tens of seconds.
- Gould & Bonart: queue imbalance predicts the next move in mid-price, with strongest performance in large-tick instruments.
- Empirical finding: OBI's price-impact coefficient is inversely proportional to market depth — meaning OBI has *more* impact per unit on thinner books like MNQ. (Double-edged: stronger signal but noisier.)

## Hard rule: filter, not trigger

OBI signal half-life is seconds. Bot-to-exchange latency is 50–200ms. We cannot race OBI signals. The design commits to using OBI only as:

- A **filter** on bar-close signals already produced by strategies
- **Context** in the PreTradeFilter (5A) prompt
- **Structural** placement of stops and targets relative to walls

NOT:
- A trigger for entries
- A reason to race a depleting offer
- A standalone signal to take a trade

This discipline is what keeps the latency hole closed. Every code review must check for it.

## Architecture

The OBI logic lives in three places, in order of upstream-to-downstream:

### 1. `MarketDataBroadcasterV3.cs` (NinjaScript Indicator)

New responsibilities:

- Subscribe to `OnMarketDepth()` events
- Maintain locked, in-memory bid/ask book state (top N levels)
- Track per-level age and peak size (for spoofing filter)
- Compute OBI features on a 250ms timer (NOT inside the event handler)
- Broadcast OBI features alongside existing fields in WS payload
- Emit heartbeat every 1s regardless of book activity

Key implementation notes:

- Lock the book state during reads (per NT8 forum guidance — in-flight updates corrupt naive reads)
- Use `e.Time` from `MarketDepthEventArgs`, NOT `DateTime.Now`, for event ordering
- Set `IsSuspendedWhileInactive = false` (otherwise stops firing when chart minimized)
- Use `SortedDictionary<double, long>` for the book — O(log n) ops, ordered iteration

### 2. `trading_controller.py` (Python WS server)

New responsibilities:

- Pass-through OBI fields to bots in the data fan-out
- Maintain `obi_state.degraded` flag, monitored by watchdog thread
- Set `degraded=True` if heartbeat age > 5s for 30 consecutive seconds
- Log all OBI-driven decision changes (for Phase 3 validation)

### 3. Bot consumers

Each component uses OBI differently:

| Component | Use |
|---|---|
| Strategies | Bias dampener: long signals require more confluence if `wtob5 < -0.3` (and inverse). NOT a hard skip. |
| **5A PreTradeFilter** | OBI snapshot in prompt. Returns `CAUTION` if signal direction opposes fresh strong OBI; `SIT_OUT` if `regime == "EVENT"`. |
| Stop placement | If a `WALL_BID` exists between entry and proposed long stop, tighten stop to just below the wall. Inverse for shorts. **Highest-EV use of OBI.** |
| Target placement | If `WALL_ASK` between entry and target on a long, scale out at the wall. |
| Risk sizing | If `book_thickness < threshold`, halve size. |

If `obi_state.degraded`, all bots fall back to pre-OBI behavior. No crashes, just graceful degradation.

## Features computed

| Feature | Formula | Notes |
|---|---|---|
| **TOB_OBI** | `(bid₀ − ask₀) / (bid₀ + ask₀)` | Classic queue imbalance, range −1 to +1 |
| **WTOB_OBI** (5-level weighted) | `Σ wᵢ(bidᵢ − askᵢ) / Σ wᵢ(bidᵢ + askᵢ)`, `wᵢ = exp(−0.5·i)` | Multi-level OFI captures more contemporaneous price-impact info than top alone |
| **OBI_EMA_3s** | EMA of WTOB_OBI with ~3-second half-life | Academic standard: 1–10s windows. Smooths flicker. |
| **WALL_BID / WALL_ASK** | Price of any level with size > 3× rolling-avg level size AND age > 5s | Spoof-resistant |
| **PULL_RATE** | Levels removed per second (rolling 10s) | High pull + quiet price = layered spoofing or imminent flush |
| **REFILL_RATIO_BID / ASK** | (sum of size-added events) / (sum of size-removed events), top 3 levels per side, rolling 30s | Dynamic imbalance — institutional absorption signal |
| **BOOK_THICKNESS** | Total volume across top 5 levels each side | Regime detector |
| **OBI_REGIME** | `"NORMAL" / "THIN" / "EVENT"` | EVENT mode = book collapsed (news / halt-near) |
| **ICEBERG_RECENT_BID / ASK** | Price of level where traded volume > 2× peak displayed size in last 5s | Behavioral iceberg detection — best we can do without MBO data |
| **FRESHNESS_MS** | ms since last book update | Critical: bot must reject OBI if > 1500ms |
| **OOO_RATE** | Out-of-order event rate (rolling) | Data-quality health gate |

## Broadcast JSON (additive — existing bots ignore new fields)

```json
{
  "type": "bar",
  "tf": "5m",
  "_existing_fields": "...",
  "obi": {
    "tob": 0.18,
    "wtob5": 0.22,
    "ema3s": 0.19,
    "wall_bid": 19820.00,
    "wall_ask": 19847.50,
    "wall_bid_size": 145,
    "wall_ask_size": 80,
    "pull_rate": 4.2,
    "refill_ratio_bid": 1.8,
    "refill_ratio_ask": 0.6,
    "book_thickness": 380,
    "regime": "NORMAL",
    "iceberg_recent_bid": null,
    "iceberg_recent_ask": null,
    "freshness_ms": 42,
    "ooo_rate": 0.003
  }
}
```

## The 8 holes and their fixes

### Hole #1 — No historical L2 backtest data
NT8 doesn't store L2 history by default. Cannot backtest OBI strategies on existing historical data.

**Fix**: Build `PhoenixL2Recorder.cs` (Phase 0b) — a separate, zero-risk indicator that does nothing except append L2 events to disk in compressed binary form. Run it now, regardless of when we build the rest. After 4 weeks we have backtest-ready data.

- Path: `C:\Trading Project\phoenix_bot\data\l2_recordings\YYYY-MM-DD_<contract>.bin`
- Format: `[event_time:8][operation:1][side:1][price:8][volume:8]` = 26 bytes/event
- Compression: LZ4 streaming → ~5-20MB/day per instrument
- Storage outside OneDrive (avoid sync interference)
- Capture MNQ + NQ in parallel for cross-validation use case

### Hole #2 — Out-of-order events
NT8 L2 events can arrive several seconds out of chronological order.

**Fix**: 
- Use `e.Time` from event, not `DateTime.Now`
- Tolerance: 50ms jitter accepted
- Drop events more than 2s late
- Track `OOO_RATE`, broadcast it
- Health gate: if > 1% sustained for 5 minutes, controller sets `obi_state.degraded=True`

### Hole #3 — 200ms data refresh limit
NT8 default refresh on L2 is 200ms. Can't fix from inside indicator.

**Fix**: Route around — all OBI features use windows ≥ 1s. Never expose sub-200ms metric. The academic literature already shows OBI works at 1-10s windows — we lose no real edge.

### Hole #4 — Latency budget pile-up
End-to-end NT8 → bot → exchange = 50–200ms. Raw OBI signal may decay in that window.

**Fix**: Two layers.

1. **Measurement**: stamp every WS message at indicator/controller/bot, surface end-to-end latency in dashboard. Real numbers, not guesses.
2. **Structural**: filter-not-trigger discipline (see top of doc). Use OBI_EMA_3s, not raw TOB_OBI, for any action. Reject signals with `freshness_ms > 1500`.

### Hole #5 — AI council token cost
~150 extra tokens per PreTradeFilter call. Gemini-Flash cost ≈ $0.0001/call. ~$0.01/day at typical volume. Trivial.

### Hole #6 — Chart-must-be-open / watchdog
Indicator only runs while chart loaded. NT8 crashes or chart closure = no OBI.

**Fix**: Defense in depth.
- Layer 1: Heartbeat broadcast every 1s
- Layer 2: Python controller watchdog → `obi_state.degraded` flag
- Layer 3: Bot config flag `use_obi`, controller sets False on degraded
- Bonus: Windows scheduled task pings dashboard `/health` every 5min, emails on failure

### Hole #7 — News / event regime
During FOMC/CPI/NFP and similar, the book disappears. OBI becomes meaningless.

**Fix**: Two complementary detectors.

1. **Calendar-based**: `config/event_blackouts.json` with scheduled events + blackout windows (typical: 3-5min before, 15-30min after)
2. **Market-based**: book_thickness drops > 40% in 30s, OR 1min ATR > 3× 20min ATR → `regime = "EVENT"`

In EVENT mode: PreTradeFilter forces `SIT_OUT`. Re-evaluate after 5 minutes.

Auto-refresh calendar weekly from a macro feed (existing macro integration should provide this).

### Hole #8 — Iceberg orders
Large hidden orders show only portion of size. OBI assumes visible = total.

**Fix (mitigation, not full fix)**: Behavioral detection — track if traded volume at a price level exceeded peak displayed size. Broadcast `iceberg_recent_*` flags. Catches icebergs *after* effect, not before.

**Honest limitation**: Full real-time iceberg detection requires Market-By-Order (MBO / Level 3) data. NT8 doesn't natively provide. Would need feed upgrade ($50-150/mo). Not necessary for filter-only use case; would be necessary for any trigger-based approach (which we're not doing).

### Hole #9 — Data feed quality
Some retail feeds aggregate or sample L2 rather than streaming all events.

**Fix**: Phase 0a verification before any real work. Acceptance criteria:
- ≥ 50 events/second average during RTH
- ≥ 30 unique price levels seen in 1 hour
- No "OnMarketDepth not subscribed" errors

If feed fails: call broker about Level 2 subscription tier ($10-50/mo typical). Don't fight a partial feed.

### Hole #10 — Spent months and got no edge
Most academically-validated signals get competed away by HFTs. Real risk that OBI doesn't survive contact with live MNQ.

**Fix**: Pre-committed kill criterion in `DECISIONS.md`. Hypothesis written in advance, decision rule written in advance, 4-week clock. If Phase 3 log-mode shows < 10% expectancy improvement, OBI gets disabled. No goalpost-moving.

## Phased rollout

Each phase has an entry gate. Failing a gate ends the project — that's the discipline.

| Phase | Duration | Goal | Entry gate |
|---|---|---|---|
| **0a** | 1 day | Verify L2 feed quality on MNQ | (start) |
| **0b** | 1 day to build, then continuous | Deploy recorder to bank historical data | 0a passes |
| **1** | 1 week | Indicator additions: book state, OOO detection, heartbeat | 0b stable |
| **2** | 1 week | Feature engineering: OBI, EMA, walls, regime, iceberg | OOO rate < 1% sustained |
| **3** | 4 weeks | Log-mode validation — bots receive OBI but don't act on it | Feature distributions sane |
| **4** | If 3 passes | Enable as PreTradeFilter context only | Hypothesis from DECISIONS.md met |
| **5** | If 4 passes | Wall-aware stop placement | Measurable expectancy lift in 4 |

## What this doesn't change

- All existing critical rules (OIF format, Indicator class, Python-as-server, etc.) still apply
- Existing CVD / absorption / confluence logic is untouched
- V1 Bot (which uses MarketDataBroadcasterV2) is unaffected
- All new JSON fields are additive — old parsers ignore unknowns

## Open questions to answer in Phase 0a

1. Does the data feed provide streaming L2 on MNQ, or aggregated?
2. What's the median top-5 book thickness on MNQ during RTH?
3. What's the OOO event rate in practice?
4. Should we also subscribe to NQ L2 for cross-validation, or is MNQ alone sufficient?

Phase 0a is designed to answer 1-3 in a single 1-hour test session. Question 4 gets answered after we see the data quality.
