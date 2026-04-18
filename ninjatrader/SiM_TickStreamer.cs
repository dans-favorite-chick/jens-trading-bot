// ============================================================================
// SiM_TickStreamer — Weekend / Simulation Edition
// ============================================================================
// Identical to TickStreamer except the State.Realtime guard is removed so
// it fires during Historical, Transition, and Realtime states. Use this on
// a sim/playback chart to test the full bridge → bot pipeline on weekends.
//
// Load TickStreamer on your LIVE chart.
// Load SiM_TickStreamer on a separate sim chart for testing.
//
// Both write to the same C:\temp\mnq_data.json fallback file — the bridge
// reads whichever is freshest.
// ============================================================================

#region Using declarations
using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class SiM_TickStreamer : Indicator
    {
        // ── CONFIG ────────────────────────────────────────────────────────
        private const string HOST            = "127.0.0.1";
        private const int    PORT            = 8765;
        private const int    HEARTBEAT_MS    = 3000;
        private const int    RECONNECT_MS    = 5000;
        private const int    CONNECT_TIMEOUT = 3000;

        // ── FILE FALLBACK ─────────────────────────────────────────────────
        // Same file as production TickStreamer — bridge backup reads this file.
        private const string FALLBACK_FILE  = @"C:\temp\mnq_data.json";
        private const int    FILE_WRITE_MS  = 1000;
        private DateTime     lastFileWrite  = DateTime.MinValue;

        // ── STATE ─────────────────────────────────────────────────────────
        private TcpClient client;
        private NetworkStream stream;
        private Timer heartbeatTimer;
        private DateTime lastConnectAttempt = DateTime.MinValue;
        private volatile bool isConnected  = false;
        private volatile bool isConnecting = false;
        private string instrumentName      = "";
        private readonly object sendLock   = new object();

        // ── DOM DEPTH STATE ────────────────────────────────────────────────
        private const int DOM_LEVELS = 5;
        private double[] domBidVols  = new double[DOM_LEVELS];
        private double[] domAskVols  = new double[DOM_LEVELS];
        private DateTime lastDomSend = DateTime.MinValue;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "SiM: Streams ticks to Python bridge in any market state (sim/playback/live)";
                Name        = "SiM_TickStreamer";
                Calculate   = Calculate.OnEachTick;
                IsOverlay   = true;
            }
            else if (State == State.DataLoaded)
            {
                instrumentName = Instrument.FullName;
                try { Directory.CreateDirectory(@"C:\temp"); } catch { }
                TryConnect();
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
            // ── No State.Realtime guard — fires in Historical, Transition, Realtime ──
            if (State == State.Undefined || State == State.SetDefaults ||
                State == State.Configure || State == State.DataLoaded  ||
                State == State.Terminated) return;

            double price = Close[0];
            double bid   = GetCurrentBid();
            double ask   = GetCurrentAsk();
            long   vol   = (long)Volume[0];
            string ts    = DateTime.UtcNow.ToString("o");

            // ── Primary: send over TCP ───────────────────────────────
            if (isConnected)
            {
                var sb = new StringBuilder(256);
                sb.Append("{\"type\":\"tick\"");
                sb.Append(",\"price\":").Append(price);
                sb.Append(",\"bid\":").Append(bid);
                sb.Append(",\"ask\":").Append(ask);
                sb.Append(",\"vol\":").Append(vol);
                sb.Append(",\"ts\":\"").Append(ts).Append("\"");
                sb.Append("}");
                Send(sb.ToString());
            }

            // ── Backup: write file fallback (always, throttled 1s) ───
            if ((DateTime.Now - lastFileWrite).TotalMilliseconds >= FILE_WRITE_MS)
            {
                lastFileWrite = DateTime.Now;
                try
                {
                    var fb = new StringBuilder(256);
                    fb.Append("{\"price\":").Append(price);
                    fb.Append(",\"close\":").Append(price);
                    fb.Append(",\"bid\":").Append(bid);
                    fb.Append(",\"ask\":").Append(ask);
                    fb.Append(",\"volume\":").Append(vol);
                    fb.Append(",\"instrument\":\"").Append(instrumentName).Append("\"");
                    fb.Append(",\"ts\":\"").Append(ts).Append("\"");
                    fb.Append("}");
                    File.WriteAllText(FALLBACK_FILE, fb.ToString());
                }
                catch { }
            }
        }

        // ── DOM DEPTH (Level 2) ────────────────────────────────────────
        protected override void OnMarketDepth(MarketDepthEventArgs e)
        {
            if (e.MarketDataType != MarketDataType.Ask &&
                e.MarketDataType != MarketDataType.Bid) return;

            int pos = e.Position;
            if (pos < 0 || pos >= DOM_LEVELS) return;

            if (e.MarketDataType == MarketDataType.Bid)
                domBidVols[pos] = e.Volume;
            else
                domAskVols[pos] = e.Volume;

            if (!isConnected) return;
            if ((DateTime.Now - lastDomSend).TotalMilliseconds < 500) return;
            lastDomSend = DateTime.Now;

            try
            {
                double bidTotal = 0.0, askTotal = 0.0;
                for (int i = 0; i < DOM_LEVELS; i++) bidTotal += domBidVols[i];
                for (int i = 0; i < DOM_LEVELS; i++) askTotal += domAskVols[i];

                var sb = new StringBuilder(128);
                sb.Append("{\"type\":\"dom\"");
                sb.Append(",\"bid_stack\":").Append((long)bidTotal);
                sb.Append(",\"ask_stack\":").Append((long)askTotal);
                sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                sb.Append("}");
                Send(sb.ToString());
            }
            catch (Exception ex)
            {
                Print("SiM_TickStreamer OnMarketDepth error: " + ex.Message);
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
                    CleanupSocket();

                    client = new TcpClient();
                    client.NoDelay    = true;
                    client.SendTimeout = 2000;

                    var connectResult = client.BeginConnect(HOST, PORT, null, null);
                    bool connected    = connectResult.AsyncWaitHandle.WaitOne(CONNECT_TIMEOUT);

                    if (connected && client.Connected)
                    {
                        client.EndConnect(connectResult);
                        stream      = client.GetStream();
                        isConnected = true;
                        Print("SiM_TickStreamer: Connected to Python bridge (TCP " + HOST + ":" + PORT + ")");

                        var sb = new StringBuilder(128);
                        sb.Append("{\"type\":\"connect\"");
                        sb.Append(",\"instrument\":\"").Append(instrumentName).Append("\"");
                        sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                        sb.Append("}");
                        SendInternal(sb.ToString());

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
                    Print("SiM_TickStreamer: Connect failed — " + ex.Message);
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
            lock (sendLock) { CleanupSocket(); }
            Print("SiM_TickStreamer: Disconnected");
        }

        // ── SEND ──────────────────────────────────────────────────────
        private void Send(string json)
        {
            lock (sendLock) { SendInternal(json); }
        }

        private void SendInternal(string json)
        {
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
                Print("SiM_TickStreamer: Send failed, will reconnect");
                CleanupSocket();
            }
        }
    }
}
