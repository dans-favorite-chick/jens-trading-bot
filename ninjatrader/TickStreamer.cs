// ============================================================================
// TickStreamer v3.0 — Multi-Instrument Edition
// ============================================================================
// CHANGES FROM v2.0:
//   ✅ Multi-instrument support: streams primary chart instrument PLUS up to
//      3 additional instruments (defaults: ES, ^VXN, ^VIX) on same TCP conn
//   ✅ Each tick payload now includes "symbol" field for bridge routing
//   ✅ Each fallback file includes symbol-tagged variants for resilience
//
// HOW IT WORKS:
//   - Primary instrument (chart instrument) — same as before, OnEachTick
//   - Additional instruments — added via AddDataSeries() in State.Configure
//   - BarsInProgress tells us which series fired in OnBarUpdate
//   - All ticks flow through one TCP connection, tagged by symbol
//
// DATA REQUIREMENTS:
//   You MUST have data subscriptions for each additional instrument:
//   - ES (CME E-mini S&P 500)
//   - ^VXN (Cboe Nasdaq Volatility Index)
//   - ^VIX (Cboe S&P 500 Volatility Index)
//
// PROTOCOL: Newline-delimited JSON over TCP to 127.0.0.1:8765
//   Tick:      {"type":"tick","symbol":"MNQ","price":...,"bid":...,"ask":...,"vol":...,"ts":"..."}\n
//   Heartbeat: {"type":"heartbeat","ts":"..."}\n
//   Connect:   {"type":"connect","instrument":"MNQM6 06-26","aux_instruments":["ES 06-26","^VXN","^VIX"],"ts":"..."}\n
// ============================================================================

#region Using declarations
using System;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class TickStreamer : Indicator
    {
        // ── CONFIG ────────────────────────────────────────────────────────
        private const string HOST            = "127.0.0.1";
        private const int    PORT            = 8765;
        private const int    HEARTBEAT_MS    = 3000;
        private const int    RECONNECT_MS    = 5000;
        private const int    CONNECT_TIMEOUT = 3000;

        // ── ADDITIONAL INSTRUMENTS (configurable) ─────────────────────────
        // To stream additional instruments alongside the chart's primary,
        // set the strings below to valid symbols (e.g. "ES 06-26") and
        // recompile. Empty string = disabled. TryAddAuxSeries() short-
        // circuits on IsNullOrEmpty so disabling is zero-cost at runtime.
        //
        // 2026-04-26: ALL THREE DISABLED. Audit confirmed no code in the
        // active bot stack consumes ES/VXN/VIX ticks from the bridge —
        // VIX is fetched externally via yfinance/Alpaca in market_intel.py,
        // and the only intermarket consumer (memory/wip-bundles/.../
        // intermarket.py) was never wired up. The aux ticks were getting
        // rejected by core/price_sanity.py (which is symbol-naive) as
        // "MNQ outliers" — log noise without operational benefit.
        // To re-enable: change "" to a valid NT8 symbol + recompile.
        private const string AUX_INSTRUMENT_1 = "";
        private const string AUX_INSTRUMENT_2 = "";
        private const string AUX_INSTRUMENT_3 = "";

        // Bar type for aux subscriptions. Tick bars give every aux update.
        // Only used when at least one AUX_INSTRUMENT_* above is non-empty.
        private const BarsPeriodType AUX_BAR_TYPE  = BarsPeriodType.Tick;
        private const int            AUX_BAR_VALUE = 1;

        // ── FILE FALLBACK ─────────────────────────────────────────────────
        private const string FALLBACK_FILE_PRIMARY = @"C:\temp\mnq_data.json";
        private const string FALLBACK_FILE_AUX     = @"C:\temp\aux_data.json";
        private const int    FILE_WRITE_MS         = 1000;
        private DateTime     lastFileWritePrimary  = DateTime.MinValue;
        private DateTime     lastFileWriteAux      = DateTime.MinValue;

        // ── STATE ─────────────────────────────────────────────────────────
        private TcpClient client;
        private NetworkStream stream;
        private Timer heartbeatTimer;
        private DateTime lastConnectAttempt = DateTime.MinValue;
        private volatile bool isConnected  = false;
        private volatile bool isConnecting = false;
        private string primaryInstrumentName = "";
        private string[] auxInstrumentNames  = new string[3];
        private readonly object sendLock     = new object();

        // ── DOM DEPTH STATE (primary instrument only) ──────────────────────
        private const int DOM_LEVELS = 5;
        private double[] domBidVols  = new double[DOM_LEVELS];
        private double[] domAskVols  = new double[DOM_LEVELS];
        private DateTime lastDomSend = DateTime.MinValue;

        // ── AUX INSTRUMENT THROTTLING ─────────────────────────────────────
        // VIX/VXN don't need every tick — throttle to ~2/sec to reduce load.
        // ES we want every tick for tight NQ correlation tracking.
        private DateTime[] lastAuxSend = new DateTime[3];
        private readonly int[] auxThrottleMs = { 0, 500, 500 };  // ES=0, VXN=500, VIX=500

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Streams MNQ + ES + VIX/VXN ticks to Python bridge via TCP";
                Name        = "TickStreamer";
                Calculate   = Calculate.OnEachTick;
                IsOverlay   = true;
            }
            else if (State == State.Configure)
            {
                // Subscribe to additional instruments so OnBarUpdate fires for them.
                // BarsInProgress will be: 0=primary, 1=aux1 (ES), 2=aux2 (VXN), 3=aux3 (VIX)
                //
                // 2026-04-23: each aux series is now guarded. A bad symbol
                // (e.g. ^VXN not present in the Instrument Manager for a
                // given data provider) used to throw from AddDataSeries and
                // kill the indicator — leaving TickStreamer Disconnected so
                // the bots stopped getting ticks entirely. Missing symbols
                // now log + skip; primary and any available aux series keep
                // working.
                //
                // 2026-04-26: AUX_INSTRUMENT_* constants are all "" so these
                // calls short-circuit inside TryAddAuxSeries (IsNullOrEmpty
                // guard). Aux feeds are effectively disabled at zero runtime
                // cost. Set the constants above to a valid symbol to re-enable.
                TryAddAuxSeries(AUX_INSTRUMENT_1);
                TryAddAuxSeries(AUX_INSTRUMENT_2);
                TryAddAuxSeries(AUX_INSTRUMENT_3);
            }
            else if (State == State.DataLoaded)
            {
                primaryInstrumentName = Instrument.FullName;

                // Capture aux instrument names by their BarsArray index
                // BarsArray[0] is primary, BarsArray[1+] are aux series
                for (int i = 1; i < BarsArray.Length && i <= 3; i++)
                {
                    if (BarsArray[i] != null && BarsArray[i].Instrument != null)
                        auxInstrumentNames[i - 1] = BarsArray[i].Instrument.FullName;
                }

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

        // 2026-04-23: guarded aux-series subscription. Wraps AddDataSeries
        // so a symbol unavailable in the current data provider's Instrument
        // Manager (e.g. ^VXN on some retail feeds) logs and skips instead
        // of throwing out of OnStateChange and leaving the indicator
        // Disconnected with no ticks flowing. Only call this from
        // State.Configure.
        //
        // 2026-04-26: also short-circuits on empty/null symbol so the
        // AUX_INSTRUMENT_*="" "disabled" mode works without ever calling
        // AddDataSeries at all.
        private void TryAddAuxSeries(string symbol)
        {
            if (string.IsNullOrEmpty(symbol)) return;
            try
            {
                AddDataSeries(symbol, AUX_BAR_TYPE, AUX_BAR_VALUE);
            }
            catch (Exception ex)
            {
                Print(string.Format(
                    "[TickStreamer] aux symbol '{0}' unavailable - skipping. {1}",
                    symbol, ex.Message));
            }
        }

        protected override void OnBarUpdate()
        {
            if (State != State.Realtime) return;
            if (CurrentBars[BarsInProgress] < 1) return;

            // Route based on which series fired
            if (BarsInProgress == 0)
            {
                // Primary instrument (MNQ) — full processing including DOM
                ProcessPrimaryTick();
            }
            else
            {
                // Aux instrument (ES, VXN, VIX) — simpler payload
                ProcessAuxTick(BarsInProgress);
            }
        }

        // ── PRIMARY TICK PROCESSING (MNQ) ──────────────────────────────────
        private void ProcessPrimaryTick()
        {
            double price = Closes[0][0];
            double bid   = GetCurrentBid(0);
            double ask   = GetCurrentAsk(0);
            long   vol   = (long)Volumes[0][0];
            string ts    = DateTime.UtcNow.ToString("o");

            // ── Primary: send over TCP ───────────────────────────────
            if (isConnected)
            {
                var sb = new StringBuilder(256);
                sb.Append("{\"type\":\"tick\"");
                sb.Append(",\"symbol\":\"").Append(EscapeJson(primaryInstrumentName)).Append("\"");
                sb.Append(",\"price\":").Append(price);
                sb.Append(",\"bid\":").Append(bid);
                sb.Append(",\"ask\":").Append(ask);
                sb.Append(",\"vol\":").Append(vol);
                sb.Append(",\"ts\":\"").Append(ts).Append("\"");
                sb.Append("}");
                Send(sb.ToString());
            }

            // ── Backup: write file fallback (throttled 1s) ───────────
            if ((DateTime.Now - lastFileWritePrimary).TotalMilliseconds >= FILE_WRITE_MS)
            {
                lastFileWritePrimary = DateTime.Now;
                try
                {
                    var fb = new StringBuilder(256);
                    fb.Append("{\"price\":").Append(price);
                    fb.Append(",\"close\":").Append(price);
                    fb.Append(",\"bid\":").Append(bid);
                    fb.Append(",\"ask\":").Append(ask);
                    fb.Append(",\"volume\":").Append(vol);
                    fb.Append(",\"instrument\":\"").Append(EscapeJson(primaryInstrumentName)).Append("\"");
                    fb.Append(",\"ts\":\"").Append(ts).Append("\"");
                    fb.Append("}");
                    File.WriteAllText(FALLBACK_FILE_PRIMARY, fb.ToString());
                }
                catch { }
            }
        }

        // ── AUX TICK PROCESSING (ES, VXN, VIX) ─────────────────────────────
        private void ProcessAuxTick(int barsInProgress)
        {
            int auxIndex = barsInProgress - 1;
            if (auxIndex < 0 || auxIndex >= 3) return;

            // Throttle if configured (0 = no throttle)
            int throttleMs = auxThrottleMs[auxIndex];
            if (throttleMs > 0)
            {
                if ((DateTime.Now - lastAuxSend[auxIndex]).TotalMilliseconds < throttleMs)
                    return;
                lastAuxSend[auxIndex] = DateTime.Now;
            }

            string symbol = auxInstrumentNames[auxIndex];
            if (string.IsNullOrEmpty(symbol)) return;

            double price = Closes[barsInProgress][0];
            string ts    = DateTime.UtcNow.ToString("o");

            if (!isConnected) return;

            // Aux payload is leaner — no DOM, may not have bid/ask for indices
            var sb = new StringBuilder(192);
            sb.Append("{\"type\":\"tick\"");
            sb.Append(",\"symbol\":\"").Append(EscapeJson(symbol)).Append("\"");
            sb.Append(",\"price\":").Append(price);
            sb.Append(",\"ts\":\"").Append(ts).Append("\"");
            sb.Append("}");
            Send(sb.ToString());
        }

        // ── DOM DEPTH (primary instrument only) ────────────────────────
        protected override void OnMarketDepth(MarketDepthEventArgs e)
        {
            if (State != State.Realtime) return;
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

                var sb = new StringBuilder(160);
                sb.Append("{\"type\":\"dom\"");
                sb.Append(",\"symbol\":\"").Append(EscapeJson(primaryInstrumentName)).Append("\"");
                sb.Append(",\"bid_stack\":").Append((long)bidTotal);
                sb.Append(",\"ask_stack\":").Append((long)askTotal);
                sb.Append(",\"ts\":\"").Append(DateTime.UtcNow.ToString("o")).Append("\"");
                sb.Append("}");
                Send(sb.ToString());
            }
            catch (Exception ex)
            {
                Print("TickStreamer OnMarketDepth error: " + ex.Message);
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
                    client.NoDelay     = true;
                    client.SendTimeout = 2000;

                    var connectResult = client.BeginConnect(HOST, PORT, null, null);
                    bool connected    = connectResult.AsyncWaitHandle.WaitOne(CONNECT_TIMEOUT);

                    if (connected && client.Connected)
                    {
                        client.EndConnect(connectResult);
                        stream      = client.GetStream();
                        isConnected = true;
                        Print("TickStreamer v3: Connected to bridge (TCP " + HOST + ":" + PORT + ")");

                        // Build aux instruments JSON array
                        var auxArr = new StringBuilder("[");
                        bool first = true;
                        for (int i = 0; i < 3; i++)
                        {
                            if (string.IsNullOrEmpty(auxInstrumentNames[i])) continue;
                            if (!first) auxArr.Append(",");
                            auxArr.Append("\"").Append(EscapeJson(auxInstrumentNames[i])).Append("\"");
                            first = false;
                        }
                        auxArr.Append("]");

                        var sb = new StringBuilder(256);
                        sb.Append("{\"type\":\"connect\"");
                        sb.Append(",\"instrument\":\"").Append(EscapeJson(primaryInstrumentName)).Append("\"");
                        sb.Append(",\"aux_instruments\":").Append(auxArr.ToString());
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
            lock (sendLock) { CleanupSocket(); }
            Print("TickStreamer: Disconnected");
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
                Print("TickStreamer: Send failed, will reconnect");
                CleanupSocket();
            }
        }

        // ── HELPERS ────────────────────────────────────────────────────
        private static string EscapeJson(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private TickStreamer[] cacheTickStreamer;
		public TickStreamer TickStreamer()
		{
			return TickStreamer(Input);
		}

		public TickStreamer TickStreamer(ISeries<double> input)
		{
			if (cacheTickStreamer != null)
				for (int idx = 0; idx < cacheTickStreamer.Length; idx++)
					if (cacheTickStreamer[idx] != null &&  cacheTickStreamer[idx].EqualsInput(input))
						return cacheTickStreamer[idx];
			return CacheIndicator<TickStreamer>(new TickStreamer(), input, ref cacheTickStreamer);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.TickStreamer TickStreamer()
		{
			return indicator.TickStreamer(Input);
		}

		public Indicators.TickStreamer TickStreamer(ISeries<double> input )
		{
			return indicator.TickStreamer(input);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.TickStreamer TickStreamer()
		{
			return indicator.TickStreamer(Input);
		}

		public Indicators.TickStreamer TickStreamer(ISeries<double> input )
		{
			return indicator.TickStreamer(input);
		}
	}
}

#endregion
