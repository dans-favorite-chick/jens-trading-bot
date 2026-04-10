# Phoenix Trading Bot — Project Export Prompt

Copy this entire file into a new Claude Code session to fully brief it on the project.

---

## Purpose

Phoenix Bot is a local-first automated trading system for MNQ (Micro E-mini Nasdaq-100) futures. It streams real-time tick data from NinjaTrader 8 into a Python pipeline that computes indicators, evaluates strategies, manages risk, and executes trades back into NT8 via file-based order instructions. A Flask dashboard provides live monitoring, connection health alerts, and runtime strategy controls.

The system was rebuilt from 5 legacy bot trees (Jen V1/V2/V3, MNQ v5 Elite, Council of Seven) into one clean, modular project. The old trees are archived at `C:\Trading Project\archive_*` for reference.

## Architecture

```
NinjaTrader 8 (TickStreamer.cs indicator on MNQM6 chart)
  │  TCP :8765 — newline-delimited JSON ticks + heartbeat every 3s
  ▼
bridge_server.py (Central Hub)
  │  Listens :8765 (NT8 TCP), :8766 (bots WS), :8767 (health HTTP)
  │  Ring buffer of 100 ticks for late-connecting bots
  │  File fallback: C:\temp\mnq_data.json if TCP stale >30s
  ▼
prod_bot.py ←WS :8766→ bridge     lab_bot.py ←WS :8766→ bridge
  │  Validated strategies only       │  ALL strategies, aggressive thresholds
  │  8:30-10:00 AM CST primary       │  24/7 capable, separate P&L
  ▼                                   ▼
tick_aggregator.py — Builds from raw ticks:
  • 1m/5m/15m/60m OHLCV bars    • ATR(14) per timeframe
  • VWAP (daily reset)          • EMA 9/21 (on 5m)
  • CVD (cumulative vol delta)  • Multi-TF bias votes (4 TFs)

Strategy Pipeline (evaluates on every 1m + 5m bar):
  → bias_momentum.py (VALIDATED) — multi-TF alignment + momentum scoring
  → spring_setup.py (VALIDATED) — Rule of Three: wick ≥6t + VWAP reclaim + CVD flip
  → vwap_pullback.py (lab) — first pullback to VWAP in trending market
  → high_precision.py (lab) — 4/4 TF alignment + tight candle precision

Risk Manager gates every entry:
  • $20 max/trade, $45 daily stop, $150 weekly stop
  • VIX filters (>40 = NO TRADE, 30-40 = 50% size)
  • Dynamic sizing: A++ ($15), B ($12), C ($8), SKIP (<30 score)
  • Recovery mode at -$30 daily → 50% size reduction
  • ATR regime adjusts RR targets and time stops

Trade Execution:
  → OIF files written to: C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\incoming\
  → Format: PLACE;Sim101;MNQM6 06-26;BUY;1;MARKET;0;0;DAY;;;;
  → NT8 reads & executes, writes fill to outgoing/ folder
  → Bot reads fill confirmation

Dashboard (Flask :5000):
  • Health bar: NT8/bridge/bot connection status with audio alerts
  • Connection log: timestamped events, color-coded
  • Bot tabs: Prod/Lab with Start/Stop buttons
  • Strategy controls: Safe/Balanced/Aggressive profiles + live sliders
  • Trade log: expandable "Why This Trade?" cards
  • Market data: price, ATR, VWAP, CVD, TF bias, session regime
```

## Session Regimes (8 time windows, CST)

| Regime | Time | Size Mult | Notes |
|--------|------|-----------|-------|
| OVERNIGHT_RANGE | 22:00-7:00 | 0.5x | Fade extremes only |
| PREMARKET_DRIFT | 7:00-8:30 | 0.5x | Light drift |
| OPEN_MOMENTUM | 8:30-9:30 | 1.0x | BEST window — full edge |
| MID_MORNING | 9:30-11:00 | 1.0x | First pullback territory |
| AFTERNOON_CHOP | 11:00-13:00 | 0.5x | Death zone |
| LATE_AFTERNOON | 13:00-15:00 | 0.8x | Institutional repositioning |
| CLOSE_CHOP | 15:00-16:15 | 0.3x | Avoid in prod |
| AFTERHOURS | 16:15-22:00 | 0.3x | Mean reversion only |

## Critical Design Rules (DO NOT CHANGE)

1. **NT8 Indicator, not Strategy** — Strategies crash with ErrorHandling=Stop
2. **Python is server, NT8 connects OUT** — reverse direction failed (months of debugging)
3. **TCP not WebSocket for NT8** — .NET Framework 4.8 ClientWebSocket has silent send bug
4. **OIF files for trade execution** — consistent, reliable, proven path
5. **OneDrive NT8 path cannot move** — NT8 won't boot without it
6. **No Newtonsoft.Json in C#** — not bundled with NT8, use StringBuilder
7. **VWAP calculated in Python** — Order Flow+ license required in NT8

## Project Layout

```
C:\Trading Project\phoenix_bot\
├── config/settings.py          # Ports, paths, limits, instruments
├── config/strategies.py        # Strategy params (slider-friendly flat dicts)
├── bridge/bridge_server.py     # TCP :8765 + WS :8766 + HTTP :8767
├── bridge/oif_writer.py        # OIF trade file writer
├── ninjatrader/TickStreamer.cs  # TCP client indicator (v2.0)
├── bots/base_bot.py            # Shared: tick processing, strategy pipeline, dashboard push
├── bots/prod_bot.py            # Validated strategies, tight risk
├── bots/lab_bot.py             # All strategies, aggressive overrides
├── strategies/base_strategy.py # Signal dataclass + BaseStrategy ABC
├── strategies/bias_momentum.py # Multi-TF momentum follow (validated)
├── strategies/spring_setup.py  # Rule of Three spring reversal (validated)
├── strategies/vwap_pullback.py # VWAP pullback (lab)
├── strategies/high_precision.py# High TF alignment (lab)
├── core/tick_aggregator.py     # Bars, ATR, VWAP, EMA, CVD from raw ticks
├── core/risk_manager.py        # Daily limits, VIX, recovery mode, sizing tiers
├── core/session_manager.py     # 8 regimes, time windows, strategy filtering
├── core/position_manager.py    # Position tracking, P&L, stop/target exits
├── core/trade_memory.py        # JSON trade persistence for learning
├── dashboard/server.py         # Flask + bot process management
├── dashboard/templates/dashboard.html  # Single-file UI
├── agents/                     # Phase 4: Council of Seven, AI advisory (not yet)
├── logs/                       # bridge.log, connection.log, trades.log
├── launch_all.bat              # Desktop shortcut: bridge + dashboard
└── launch_{bridge,prod,lab,dashboard}.bat
```

## Ports & Protocols

| Port | Protocol | Listener | Client | Purpose |
|------|----------|----------|--------|---------|
| 8765 | TCP | bridge | NT8 TickStreamer | Tick ingress (newline JSON) |
| 8766 | WebSocket | bridge | prod_bot, lab_bot | Tick distribution to bots |
| 8767 | HTTP | bridge | dashboard | Health status polling |
| 5000 | HTTP | dashboard | browser + bots | UI + REST API |

## Current State (as of 2026-04-10)

- **Working:** NT8 TCP connection stable (6000+ ticks/sec), bridge running, bots connected, strategies evaluating, trades executing, dashboard serving
- **Phase 4 (not started):** Council of Seven advisory, Gemini pre-trade filter, session debriefer
- **Phase 5 (future):** OCO brackets, Research Bot backtesting, trade clustering, AI param tuner

## Git History

```
6a60d26 Add bot-to-dashboard state push loop
43b518a Fix trade execution pipeline + aggressive lab bot
27b028d Fix NT8 connection: replace WebSocket with raw TCP
ec50957 Phoenix Trading Bot — initial scaffold (Phase 0-3)
```

## Key Config Values (config/settings.py)

- Instrument: MNQM6 06-26, Account: Sim101, LIVE_TRADING = False
- Tick size: 0.25 ($0.50/tick for MNQ)
- Stale threshold: 30s, heartbeat: 3s
- Daily loss: $45, weekly: $150, max/trade: $20
- VIX extreme: >40 (no trade), high: 30-40 (50% size)
- Prod window: 8:30-10:00 AM CST

## User Profile

MNQ futures trader, single contract, $500-$1500 account. Prefers local-first (no cloud). Wants simple, maintainable code. Dashboard-driven workflow — needs clear connectivity status and live strategy adjustment. Comfortable with Claude Code for code-level changes.
