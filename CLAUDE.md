# CLAUDE.md — Phoenix Trading Bot

## System Architecture

Phoenix is a local Python trading system for MNQ (Micro E-mini Nasdaq-100) futures, connected to NinjaTrader 8.

### Data Flow
```
NinjaTrader 8 (TickStreamer.cs indicator)
  → WebSocket CLIENT connects OUT to Python on :8765
  → bridge_server.py (WebSocket SERVER on :8765, fans out on :8766)
  → prod_bot.py / lab_bot.py (WebSocket CLIENTS on :8766)
  → Trade signals → OIF files → NT8 incoming/ folder → execution
```

### Critical Design Rules (DO NOT CHANGE)
1. **NT8 Indicator, not Strategy** — Strategies crash with ErrorHandling=Stop
2. **Python is WS SERVER, NT8 connects OUT** — reverse direction failed
3. **OIF files for trade execution** — file path is consistent and reliable
4. **NT8 data folder path is config-driven** — change `NT8_DATA_ROOT` in `config/settings.py`; migrated out of OneDrive 2026-04-18
5. **No Newtonsoft.Json in C#** — not bundled with NT8, use StringBuilder
6. **VWAP calculated in Python** — Order Flow+ license required in NT8

### Key Paths
- OIF incoming: `C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\`
- OIF outgoing: `C:\Users\Trading PC\Documents\NinjaTrader 8\outgoing\`
- File fallback: `C:\temp\mnq_data.json`
- NT8 indicators: `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\`

### Ports
- `:8765` — Bridge WS server (NT8 connects here)
- `:8766` — Bridge WS server (bots connect here)
- `:8767` — Bridge health HTTP endpoint
- `:5000` — Dashboard (Flask)

### Project Layout
```
phoenix_bot/
├── config/settings.py          # All config: ports, paths, limits, instruments
├── config/strategies.py        # Strategy params (toggleable, slider-friendly)
├── bridge/bridge_server.py     # WS server :8765 (NT8) + :8766 (bots)
├── bridge/oif_writer.py        # OIF trade file writer
├── ninjatrader/TickStreamer.cs  # Lean tick-only NT8 indicator
├── bots/base_bot.py            # Shared bot logic
├── bots/prod_bot.py            # Production bot (validated strategies)
├── bots/lab_bot.py             # Experimental bot (sandbox)
├── strategies/base_strategy.py # Strategy interface
├── strategies/*.py             # Individual strategy files
├── core/tick_aggregator.py     # Builds bars, ATR, VWAP, EMA, CVD from ticks
├── core/risk_manager.py        # Limits, VIX filter, recovery mode, sizing
├── core/session_manager.py     # 8 market regimes, time windows
├── core/position_manager.py    # Track positions, P&L, stop/target
├── core/trade_memory.py        # Trade log + learning data
├── dashboard/server.py         # Flask app, REST API
├── dashboard/templates/        # dashboard.html
├── agents/                     # Optional AI advisory (Council, pre-trade, debrief)
└── logs/
```

### Running
```bash
# 1. Start NinjaTrader 8, load TickStreamer on MNQM6 chart
# 2. Start bridge
python bridge/bridge_server.py

# 3. Start bot(s)
python bots/prod_bot.py    # Production
python bots/lab_bot.py     # Experimental (optional)

# 4. Open dashboard
python dashboard/server.py  # then visit localhost:5000
```

### Environment
```bash
pip install -r requirements.txt   # websockets, flask, numpy, aiofiles, python-dotenv, aiohttp
```

### Trading Parameters (defaults in config/settings.py)
- Instrument: MNQM6 06-26
- Account: Sim101 (LIVE_TRADING = False by default)
- Max loss per trade: $20
- Daily stop: -$45
- Recovery mode: -$30 daily → 50% size reduction
- Primary session: 8:30-10:00 AM CST
- Base RR: 1.5:1

## Daily Monitoring Workflow (Sprint C)

All four tools are read-only and write to `out/`. Run from project root.

| When | Command | Reads | Writes |
|------|---------|-------|--------|
| After each session | `python tools/daily_session_summary.py` | `logs/history/<today>_<bot>.jsonl` | `out/daily_summary_<today>.md` |
| Weekly (or after risk-code changes) | `python tools/verify_halt_signatures.py` | (synthetic triggers) | `out/halt_verify_<today>.md` |
| Weekly | `python tools/validation_tracker.py --post-b13-only` | `logs/trade_memory.json` | `out/validation_status_<today>.md` |
| As needed | `python tools/backfill_commissions.py` | `logs/trade_memory.json` | `out/historical_pnl_recompute_<today>.md` |

### Statistical tier reference

| Tier | Trades | Confidence | Decisions Allowed |
|------|-------:|-----------:|---|
| INSUFFICIENT_SAMPLE | < 30 | none | WATCH only |
| PRELIMINARY | 30–99 | ~70% | WATCH or KILL if PF<0.7 |
| TENTATIVE | 100–384 | ~90% | + GRADUATE candidate |
| VALIDATED | 385–665 | ~95% | + SCALE candidate |
| HIGH_CONFIDENCE | 666+ | ~99% | full confidence |

Phoenix's project 50-trade graduation gate sits inside PRELIMINARY —
enough to start making directional decisions, NOT enough to bet the
farm on. The validation_tracker tool surfaces this uncertainty
explicitly via Wilson 95% CI on win rate.

### Anomaly detection (daily_session_summary.py)

After each session, the tool flags two kinds of anomaly vs the
trailing 7-day baseline:

- `signal_volume_drop`: today's signals < 40% of the strategy's
  trailing average. **Early warning that a Sprint A gate may be
  rejecting too aggressively.**
- `silent_strategy`: trailing avg ≥ 1/day, today = 0. **Critical —
  investigate before next session.**
