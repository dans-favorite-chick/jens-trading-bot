// ============================================================================
// TickStreamer — Lean Tick-Only NT8 Indicator for Phoenix Bot
// ============================================================================
// PURPOSE:  Stream raw ticks from NinjaTrader 8 to Python via WebSocket.
//           Python owns all derived math (ATR, VWAP, CVD, bars, etc.).
//
// ARCHITECTURE:
//   This indicator is a WebSocket CLIENT connecting OUT to Python server.
//   Python runs the WebSocket SERVER on port 8765.
//   NT8 → ws://127.0.0.1:8765 → Python bridge_server.py
//
// MESSAGES SENT:
//   Tick:      {"type":"tick","price":18527.5,"bid":18527.25,"ask":18527.75,"vol":1,"ts":"..."}
//   Heartbeat: {"type":"heartbeat","ts":"..."}
//   Connect:   {"type":"connect","instrument":"MNQM6 06-26","ts":"..."}
//
// INSTALL:
//   1. NinjaTrader 8 → File → Utilities → Edit NinjaScript → Indicators
//   2. Create new file "TickStreamer.cs", paste this code
//   3. Save and compile (F5)
//   4. Add to any MNQM6 chart (right-click chart → Indicators → TickStreamer)
// ============================================================================

#region Using declarations
using System;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class TickStreamer : Indicator
    {
        // ── CONFIG ────────────────────────────────────────────────────────
        private const string PYTHON_WS_URI  = "ws://127.0.0.1:8765";
        private const int    HEARTBEAT_MS   = 3000;   // Heartbeat every 3 seconds
        private const int    RECONNECT_MS   = 2000;   // Retry connection after 2 seconds
        private const int    SEND_TIMEOUT_MS = 1000;  // Max wait for send to complete

        // ── STATE ─────────────────────────────────────────────────────────
        private ClientWebSocket socket;
        private CancellationTokenSource cts;
        private DateTime lastSendTime = DateTime.MinValue;
        private DateTime lastHeartbeat = DateTime.MinValue;
        private bool isConnected = false;
        private int sendErrors = 0;
        private string instrumentName = "";

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Streams raw ticks to Python bridge via WebSocket";
                Name = "TickStreamer";
                Calculate = Calculate.OnEachTick;
                IsOverlay = true;
            }
            else if (State == State.DataLoaded)
            {
                instrumentName = Instrument.FullName;
                cts = new CancellationTokenSource();
                TryConnect();
            }
            else if (State == State.Terminated)
            {
                Disconnect();
            }
        }

        protected override void OnBarUpdate()
        {
            if (State != State.Realtime) return;

            // ── Send tick data ─────────────────────────────────────────
            if (isConnected)
            {
                var sb = new StringBuilder(256);
                sb.Append("{\"type\":\"tick\"");
                sb.Append(",\"price\":").Append(Close[0]);
                sb.Append(",\"bid\":").Append(GetCurrentBid());
                sb.Append(",\"ask\":").Append(GetCurrentAsk());
                sb.Append(",\"vol\":").Append(Volume[0]);
                sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                sb.Append("}");

                SendMessage(sb.ToString());
            }

            // ── Heartbeat check ────────────────────────────────────────
            if ((DateTime.Now - lastHeartbeat).TotalMilliseconds >= HEARTBEAT_MS)
            {
                SendHeartbeat();
                lastHeartbeat = DateTime.Now;
            }

            // ── Auto-reconnect ─────────────────────────────────────────
            if (!isConnected && (DateTime.Now - lastSendTime).TotalMilliseconds >= RECONNECT_MS)
            {
                TryConnect();
            }
        }

        // ── CONNECTION ─────────────────────────────────────────────────
        private void TryConnect()
        {
            try
            {
                if (socket != null)
                {
                    try { socket.Dispose(); } catch { }
                }

                socket = new ClientWebSocket();
                var connectTask = socket.ConnectAsync(new Uri(PYTHON_WS_URI), cts.Token);
                if (connectTask.Wait(3000))
                {
                    isConnected = socket.State == WebSocketState.Open;
                    if (isConnected)
                    {
                        sendErrors = 0;
                        Print("TickStreamer: Connected to Python bridge at " + PYTHON_WS_URI);

                        // Send connect message with instrument info
                        var sb = new StringBuilder(128);
                        sb.Append("{\"type\":\"connect\"");
                        sb.Append(",\"instrument\":\"").Append(instrumentName).Append("\"");
                        sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                        sb.Append("}");
                        SendMessage(sb.ToString());
                    }
                }
                else
                {
                    isConnected = false;
                }
            }
            catch (Exception ex)
            {
                isConnected = false;
                Print("TickStreamer: Connect failed — " + ex.Message);
            }
            lastSendTime = DateTime.Now;
        }

        private void Disconnect()
        {
            isConnected = false;
            try
            {
                cts?.Cancel();
                if (socket != null && socket.State == WebSocketState.Open)
                {
                    socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "Indicator removed", CancellationToken.None).Wait(1000);
                }
                socket?.Dispose();
            }
            catch { }
            Print("TickStreamer: Disconnected");
        }

        // ── MESSAGING ──────────────────────────────────────────────────
        private void SendMessage(string json)
        {
            if (!isConnected || socket == null || socket.State != WebSocketState.Open)
            {
                isConnected = false;
                return;
            }

            try
            {
                var bytes = Encoding.UTF8.GetBytes(json);
                var segment = new ArraySegment<byte>(bytes);
                var sendTask = socket.SendAsync(segment, WebSocketMessageType.Text, true, cts.Token);
                if (!sendTask.Wait(SEND_TIMEOUT_MS))
                {
                    sendErrors++;
                    if (sendErrors > 5)
                    {
                        Print("TickStreamer: Too many send timeouts, reconnecting...");
                        isConnected = false;
                    }
                }
                else
                {
                    sendErrors = 0;
                    lastSendTime = DateTime.Now;
                }
            }
            catch (Exception)
            {
                isConnected = false;
            }
        }

        private void SendHeartbeat()
        {
            var sb = new StringBuilder(64);
            sb.Append("{\"type\":\"heartbeat\"");
            sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
            sb.Append("}");
            SendMessage(sb.ToString());
        }
    }
}
