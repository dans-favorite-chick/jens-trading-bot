# TickStreamer — NinjaTrader 8 Installation Guide

## Prerequisites
- NinjaTrader 8 running with live or sim data
- MNQM6 chart open (any timeframe — indicator uses OnEachTick)
- Python bridge running first (`python bridge/bridge_server.py`)

## Install Steps

1. Open NinjaTrader 8
2. Go to **Tools > Edit NinjaScript > Indicator**
3. Right-click in the list > **New Indicator**
4. Name it `TickStreamer`
5. Replace all generated code with the contents of `TickStreamer.cs`
6. Press **F5** to compile (should show 0 errors)
7. Open your **MNQM6** chart
8. Right-click chart > **Indicators** > find **TickStreamer** > **Add** > **OK**

## Verify Connection

In the Python bridge terminal, you should see:
```
[NT8] Client connected from ('127.0.0.1', ...)
[NT8] Instrument: MNQM6 06-26
[NT8] Ticks flowing...
```

In NinjaTrader's Output window (Tools > Output Window):
```
TickStreamer: Connected to Python bridge at 127.0.0.1:8765 (TCP)
```

## Connection Protocol
- **TCP** (raw socket, NOT WebSocket) on port 8765
- Newline-delimited JSON messages
- Heartbeat every 3 seconds
- Python bridge is the SERVER, NT8 indicator connects OUT as client

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Connect failed" in NT8 output | Start Python bridge FIRST, then load indicator |
| No ticks in bridge | Make sure chart has live data (not historical) |
| Indicator removed after restart | Re-add it to chart; NT8 doesn't persist custom indicators automatically |
| Compile errors | Make sure you copied the ENTIRE file including `#region Using declarations` |

## Startup Order (Critical)
1. Start Python bridge: `python bridge/bridge_server.py`
2. Open NinjaTrader 8, open MNQM6 chart
3. Add TickStreamer indicator to chart
4. Start bot(s): `python bots/prod_bot.py`

## File-Based Fallback (Plan B)
TickStreamer writes to `C:\temp\mnq_data.json` every 1 second as a backup.
The bridge auto-switches to file polling if TCP goes stale for >30 seconds
(configurable via `DISCONNECT_THRESHOLD_S` in `config/settings.py`).

## Historical Data Export (for Backtesting)
Also install `HistoricalExporter.cs` to export 1-min bar data to CSV:
1. Same install process as TickStreamer (New Indicator, paste, F5)
2. Add to a MNQM6 1-min chart with 90+ days of data loaded
3. CSV writes automatically to `C:\temp\mnq_historical.csv`
