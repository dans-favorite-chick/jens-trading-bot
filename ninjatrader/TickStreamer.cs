// ============================================================================
// TickStreamer v2.0 — TCP Edition (replaces WebSocket)
// ============================================================================
// WHY TCP:  .NET Framework 4.8's ClientWebSocket (WinHTTP) has a known bug
//           where SendAsync silently succeeds but data never reaches the server.
//           NT8's threading model (indicator threads paused, timers drift) makes
//           async WebSocket unreliable. Raw TCP with synchronous Write() is
//           atomic, blocking, and proven to work in NT8 (MNQDataBridge.cs).
//
// PROTOCOL: Newline-delimited JSON over TCP to 127.0.0.1:8765
//   Tick:      {"type":"tick","price":18527.5,"bid":18527.25,"ask":18527.75,"vol":1,"ts":"..."}\n
//   Heartbeat: {"type":"heartbeat","ts":"..."}\n
//   Connect:   {"type":"connect","instrument":"MNQM6 06-26","ts":"..."}\n
//
// INSTALL:  Same as before — paste into NinjaScript Indicator editor, F5.
// ============================================================================

#region Using declarations
using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class TickStreamer : Indicator
    {
        // ── CONFIG ────────────────────────────────────────────────────────
        private const string HOST           = "127.0.0.1";
        private const int    PORT           = 8765;
        private const int    HEARTBEAT_MS   = 3000;   // Heartbeat every 3 seconds
        private const int    RECONNECT_MS   = 5000;   // Retry after 5 seconds
        private const int    CONNECT_TIMEOUT = 3000;  // TCP connect timeout

        // ── STATE ─────────────────────────────────────────────────────────
        private TcpClient client;
        private NetworkStream stream;
        private Timer heartbeatTimer;
        private DateTime lastConnectAttempt = DateTime.MinValue;
        private volatile bool isConnected = false;
        private volatile bool isConnecting = false;
        private string instrumentName = "";
        private readonly object sendLock = new object();

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Streams raw ticks to Python bridge via TCP";
                Name = "TickStreamer";
                Calculate = Calculate.OnEachTick;
                IsOverlay = true;
            }
            else if (State == State.DataLoaded)
            {
                instrumentName = Instrument.FullName;
                TryConnect();

                // Heartbeat timer — fires every 3s, handles heartbeat + reconnect
                heartbeatTimer = new Timer(HeartbeatCallback, null, HEARTBEAT_MS, HEARTBEAT_MS);
            }
            else if (State == State.Terminated)
            {
                if (heartbeatTimer != null)
                {
                    heartbeatTimer.Dispose();
                    heartbeatTimer = null;
                }
                Disconnect();
            }
        }

        protected override void OnBarUpdate()
        {
            if (State != State.Realtime) return;

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
                Send(sb.ToString());
            }
        }

        // ── HEARTBEAT TIMER ────────────────────────────────────────────
        private void HeartbeatCallback(object state)
        {
            if (isConnected)
            {
                var sb = new StringBuilder(64);
                sb.Append("{\"type\":\"heartbeat\"");
                sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                sb.Append("}");
                Send(sb.ToString());
            }
            else if (!isConnecting && (DateTime.Now - lastConnectAttempt).TotalMilliseconds >= RECONNECT_MS)
            {
                TryConnect();
            }
        }

        // ── CONNECTION ─────────────────────────────────────────────────
        private void TryConnect()
        {
            lock (sendLock)
            {
                if (isConnecting) return;
                isConnecting = true;
                lastConnectAttempt = DateTime.Now;

                try
                {
                    // Clean up old connection
                    CleanupSocket();

                    client = new TcpClient();
                    client.NoDelay = true;  // Disable Nagle — send immediately
                    client.SendTimeout = 2000;

                    // Connect with timeout
                    var connectResult = client.BeginConnect(HOST, PORT, null, null);
                    bool connected = connectResult.AsyncWaitHandle.WaitOne(CONNECT_TIMEOUT);

                    if (connected && client.Connected)
                    {
                        client.EndConnect(connectResult);
                        stream = client.GetStream();
                        isConnected = true;
                        Print("TickStreamer: Connected to Python bridge (TCP " + HOST + ":" + PORT + ")");

                        // Send connect message
                        var sb = new StringBuilder(128);
                        sb.Append("{\"type\":\"connect\"");
                        sb.Append(",\"instrument\":\"").Append(instrumentName).Append("\"");
                        sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                        sb.Append("}");
                        SendInternal(sb.ToString());

                        // Immediate heartbeat
                        var hb = new StringBuilder(64);
                        hb.Append("{\"type\":\"heartbeat\"");
                        hb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                        hb.Append("}");
                        SendInternal(hb.ToString());
                    }
                    else
                    {
                        CleanupSocket();
                    }
                }
                catch (Exception ex)
                {
                    isConnected = false;
                    Print("TickStreamer: Connect failed — " + ex.Message);
                    CleanupSocket();
                }
                finally
                {
                    isConnecting = false;
                }
            }
        }

        private void CleanupSocket()
        {
            isConnected = false;
            try { stream?.Close(); } catch { }
            try { client?.Close(); } catch { }
            stream = null;
            client = null;
        }

        private void Disconnect()
        {
            lock (sendLock)
            {
                CleanupSocket();
            }
            Print("TickStreamer: Disconnected");
        }

        // ── SEND (synchronous, blocking, atomic) ──────────────────────
        private void Send(string json)
        {
            lock (sendLock)
            {
                SendInternal(json);
            }
        }

        private void SendInternal(string json)
        {
            // Must be called inside sendLock
            if (!isConnected || stream == null) return;

            try
            {
                byte[] data = Encoding.UTF8.GetBytes(json + "\n");
                stream.Write(data, 0, data.Length);
                stream.Flush();
            }
            catch (Exception)
            {
                isConnected = false;
                Print("TickStreamer: Send failed, will reconnect");
                CleanupSocket();
            }
        }
    }
}
