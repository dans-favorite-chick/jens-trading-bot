// ============================================================================
// TickStreamer v3.0 — Multi-Instrument Edition  (05/04/2026)
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
using NinjaTrader.NinjaScript.BarsTypes;
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

        // ── SINGLETON ENFORCEMENT (2026-05-08) ────────────────────────────
        // Workspace pollution can leave duplicate TickStreamer instances on
        // hidden charts (see 2026-04-25 incident + recurrence on 2026-05-07
        // Jen's Fav.xml: 4 TickStreamer references = 2+ chart instances).
        // Each duplicate retries TCP connection every RECONNECT_MS=5000ms
        // and gets bounced by the bridge's PHOENIX_BRIDGE_SINGLE_STREAM
        // defense — producing tens of thousands of rejection log lines per
        // day. The bridge defense is correct; this is the client-side
        // companion. The first instance to reach State.DataLoaded claims
        // the slot; subsequent instances log once and skip TryConnect /
        // heartbeat entirely. Released on State.Terminated.
        private static int _activeInstanceCount = 0;
        private bool _claimedSlot = false;

        // ── DOM DEPTH STATE (primary instrument only) ──────────────────────
        private const int DOM_LEVELS = 5;
        private double[] domBidVols  = new double[DOM_LEVELS];
        private double[] domAskVols  = new double[DOM_LEVELS];
        private DateTime lastDomSend = DateTime.MinValue;

        // ── VOLUMETRIC BAR EMITTER (Sprint H v3 Phase 2a) ──────────────────
        // On each volumetric bar close, emit aggregated bid/ask/delta/POC +
        // imbalance list + stacked-imbalance flags + session CVD as JSON.
        // The bridge persists each emission to data/volumetric_latest.json
        // and logs/volumetric_history.jsonl for strategies/footprint_cvd_reversal.
        //
        // Operator must configure this chart as 1,500-tick Volumetric (Order
        // Flow+) bars with BidAsk delta type and ticks_per_level=1. If the
        // chart is NOT a volumetric series, EmitVolumetricBar() logs ONCE
        // and silently no-ops — strategy stays dormant logging
        // DATA_NOT_AVAILABLE without crashing the tick loop.
        //
        // Sprint M Tier 1.1 (2026-05-12): IMBALANCE_RATIO is now adaptive
        // per-bar based on the bar's price range (volatility proxy). A
        // fixed 3.0 ratio meant quiet evening bars produced almost no
        // imbalances (real institutional flow drowned in low-noise tape)
        // while volatile RTH-open bars produced false-positive stacks
        // from noise. Scaling with range catches more on quiet days and
        // filters more on chaotic ones. Thresholds chosen from MNQ
        // 1,500-tick bars: <5pt range = thin overnight tape;
        // 5-15pt = normal session; 15-30pt = elevated; 30pt+ = volatile.
        // The chosen ratio is emitted in the JSON as "imbalance_ratio"
        // so downstream consumers can record what threshold each bar used.
        private const int    STACKED_THRESHOLD   = 3;     // 3+ consecutive same-side
        private const int    VOLUMETRIC_BAR_SIZE = 1500;  // emitted in JSON for context

        // Adaptive imbalance ratio: pure function of bar range (in points).
        // Kept private+static so unit-testable from a separate harness
        // without instantiating the full indicator.
        private static double GetAdaptiveImbalanceRatio(double rangePoints)
        {
            if (rangePoints < 5.0)  return 2.5;   // very quiet
            if (rangePoints < 15.0) return 3.0;   // normal (legacy default)
            if (rangePoints < 30.0) return 3.5;   // elevated
            return 4.0;                            // volatile
        }
        private long         sessionCvd          = 0;     // cumulative session delta
        private DateTime     sessionCvdResetDate = DateTime.MinValue;
        private int          lastEmittedVolBar   = -1;    // dedupe guard
        // 2026-05-22 pt8 (B-032 hardening): was `bool volumetricWarned` —
        // a one-shot flag that silently no-op'd forever after the first
        // warning. Caused the 2026-05-19 23:03 CT cliff to go unnoticed
        // for 4 days. Replaced with a 5-minute rate-limited timestamp
        // so the operator's NT8 Output window keeps re-displaying the
        // "chart is NOT a Volumetric bars series" message until they
        // fix the chart configuration. See feedback_silent_failures.md.
        private DateTime     volumetricWarnedAt  = DateTime.MinValue;

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

                // 2026-05-08: singleton claim. Atomically increment the
                // process-wide active count. If we're not first, refuse to
                // connect and log once. The first instance owns the slot;
                // duplicates become passive (no TCP, no heartbeat, no file
                // fallback). On chart close, the first instance's
                // State.Terminated releases the slot so the next reload
                // can claim it.
                int slot = Interlocked.Increment(ref _activeInstanceCount);
                if (slot > 1)
                {
                    Print(string.Format(
                        "[TickStreamer] DUPLICATE INSTANCE refusing to connect " +
                        "(active count={0}, this chart={1}). Only ONE TickStreamer " +
                        "can run per NT8 process — the bridge enforces this anyway " +
                        "via PHOENIX_BRIDGE_SINGLE_STREAM. Close the duplicate chart " +
                        "or run tools/nt8_unhide_all_windows.ps1 to find a hidden one.",
                        slot, primaryInstrumentName));
                    Interlocked.Decrement(ref _activeInstanceCount);
                    return;  // skip TryConnect + heartbeat
                }
                _claimedSlot = true;

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
                // Release the singleton slot only if this instance actually
                // claimed it. Duplicate instances that returned early in
                // DataLoaded never incremented past their own decrement.
                if (_claimedSlot)
                {
                    Interlocked.Decrement(ref _activeInstanceCount);
                    _claimedSlot = false;
                }
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
            // Sprint H v3 Phase 2a: volumetric bar emit on bar close.
            // IsFirstTickOfBar is set true on the first tick AFTER a bar
            // closes, so the just-closed bar is at CurrentBar - 1. Wrapped
            // in try/catch — emitter failure must never break the tick loop.
            if (IsFirstTickOfBar && CurrentBar >= 1)
            {
                try
                {
                    EmitVolumetricBar(CurrentBar - 1);
                }
                catch (Exception ex)
                {
                    Print("TickStreamer EmitVolumetricBar error: " + ex.Message);
                    // Caught + logged. Tick loop continues normally.
                }
            }

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

        // ── VOLUMETRIC EMITTER (Sprint H v3 Phase 2a) ──────────────────
        // Cast Bars.BarsSeries.BarsType as VolumetricBarsType, iterate price
        // levels with GetBidVolumeForPrice / GetAskVolumeForPrice, build
        // imbalances list, detect stacked + max ratio, emit JSON via Send().
        // Returns silently (with one-time log) if the chart isn't a
        // Volumetric series — operator configuration error, strategy stays
        // dormant.
        private void EmitVolumetricBar(int barIdx)
        {
            if (!isConnected) return;            // bridge offline — drop bar
            if (barIdx < 0) return;
            if (barIdx == lastEmittedVolBar) return;   // dedupe
            lastEmittedVolBar = barIdx;

            var volumetricBarsType = Bars.BarsSeries.BarsType as
                NinjaTrader.NinjaScript.BarsTypes.VolumetricBarsType;
            if (volumetricBarsType == null)
            {
                // 2026-05-22 pt8 (B-032): rate-limited 5-min re-emit so silent
                // failures stay LOUD until operator fixes the chart configuration.
                if ((DateTime.Now - volumetricWarnedAt).TotalMinutes >= 5.0)
                {
                    Print("TickStreamer: chart is NOT a Volumetric bars series — " +
                          "footprint_cvd_reversal strategy will stay dormant AND " +
                          "data/volumetric_latest.json will go stale. " +
                          "Configure chart per Sprint H v3 spec (1,500-tick " +
                          "Volumetric, BidAsk delta, ticks_per_level=1).");
                    volumetricWarnedAt = DateTime.Now;
                }
                return;
            }

            var vol = volumetricBarsType.Volumes[barIdx];
            if (vol == null) return;

            double barOpen  = Bars.GetOpen(barIdx);
            double barHigh  = Bars.GetHigh(barIdx);
            double barLow   = Bars.GetLow(barIdx);
            double barClose = Bars.GetClose(barIdx);

            // Sprint M Tier 1.1: per-bar adaptive imbalance ratio.
            double barRangePoints = barHigh - barLow;
            double imbalanceRatio = GetAdaptiveImbalanceRatio(barRangePoints);

            // Daily session-CVD reset on date boundary.
            DateTime barTime = Bars.GetTime(barIdx);
            if (barTime.Date != sessionCvdResetDate)
            {
                sessionCvd = 0;
                sessionCvdResetDate = barTime.Date;
            }

            // Iterate every price level in the bar [low..high] in TickSize
            // increments. Aggregate buy/sell volume + find POC + build
            // imbalance list + scan for 3+ consecutive same-side stacks.
            //
            // Imbalance definition: same-level ratio with both sides > 0.
            //   Buy imbalance:  ask >= IMBALANCE_RATIO × bid (and bid > 0)
            //   Sell imbalance: bid >= IMBALANCE_RATIO × ask (and ask > 0)
            // The "both sides > 0" guard prevents the "infinite ratio when
            // one side is zero" false-positive (NT8 forum thread 1091161).
            long buyVolTotal  = 0;   // aggressive lift-ask
            long sellVolTotal = 0;   // aggressive hit-bid
            double poc        = 0;
            long pocVolume    = 0;

            var imbalancesJson = new StringBuilder(256);
            imbalancesJson.Append("[");
            bool firstImb = true;
            double maxRatio = 0;
            int consecutiveBuy  = 0;
            int consecutiveSell = 0;
            bool stackedBuy  = false;
            bool stackedSell = false;

            for (double price = barLow;
                 price <= barHigh + (TickSize / 2);
                 price = Instrument.MasterInstrument.RoundToTickSize(price + TickSize))
            {
                long bidVol = (long)vol.GetBidVolumeForPrice(price);
                long askVol = (long)vol.GetAskVolumeForPrice(price);

                buyVolTotal  += askVol;
                sellVolTotal += bidVol;

                long levelTotal = bidVol + askVol;
                if (levelTotal > pocVolume)
                {
                    pocVolume = levelTotal;
                    poc = price;
                }

                // No volume on one side → can't compute ratio + breaks
                // the stacked-consecutive run.
                if (bidVol == 0 || askVol == 0)
                {
                    consecutiveBuy = 0;
                    consecutiveSell = 0;
                    continue;
                }

                double ratio;
                string side;
                if (askVol >= imbalanceRatio * bidVol)
                {
                    ratio = (double)askVol / (double)bidVol;
                    side = "buy";
                    consecutiveBuy++;
                    consecutiveSell = 0;
                    if (consecutiveBuy >= STACKED_THRESHOLD) stackedBuy = true;
                }
                else if (bidVol >= imbalanceRatio * askVol)
                {
                    ratio = (double)bidVol / (double)askVol;
                    side = "sell";
                    consecutiveSell++;
                    consecutiveBuy = 0;
                    if (consecutiveSell >= STACKED_THRESHOLD) stackedSell = true;
                }
                else
                {
                    consecutiveBuy = 0;
                    consecutiveSell = 0;
                    continue;   // not imbalanced — don't emit a row
                }

                if (ratio > maxRatio) maxRatio = ratio;

                if (!firstImb) imbalancesJson.Append(",");
                firstImb = false;
                imbalancesJson.Append("{\"price\":").Append(price)
                              .Append(",\"bid_vol\":").Append(bidVol)
                              .Append(",\"ask_vol\":").Append(askVol)
                              .Append(",\"ratio\":").Append(ratio.ToString("F2"))
                              .Append(",\"side\":\"").Append(side).Append("\"}");
            }
            imbalancesJson.Append("]");

            long delta = buyVolTotal - sellVolTotal;
            long totalVolume = buyVolTotal + sellVolTotal;
            sessionCvd += delta;

            // Build the JSON message. Fields match bridge_server.py
            // VOLUMETRIC_REQUIRED set + a few decorative extras (bar_size_ticks,
            // max_imbalance_ratio) that the strategy reads but the bridge
            // schema gate doesn't require.
            var msg = new StringBuilder(512);
            msg.Append("{\"type\":\"volumetric_bar\"");
            msg.Append(",\"ts\":\"").Append(barTime.ToString("o")).Append("\"");
            msg.Append(",\"instrument\":\"")
               .Append(EscapeJson(primaryInstrumentName)).Append("\"");
            msg.Append(",\"bar_size_ticks\":").Append(VOLUMETRIC_BAR_SIZE);
            msg.Append(",\"open\":").Append(barOpen);
            msg.Append(",\"high\":").Append(barHigh);
            msg.Append(",\"low\":").Append(barLow);
            msg.Append(",\"close\":").Append(barClose);
            msg.Append(",\"delta\":").Append(delta);
            msg.Append(",\"total_volume\":").Append(totalVolume);
            msg.Append(",\"buy_volume\":").Append(buyVolTotal);
            msg.Append(",\"sell_volume\":").Append(sellVolTotal);
            msg.Append(",\"poc\":").Append(poc);
            msg.Append(",\"imbalances\":").Append(imbalancesJson.ToString());
            msg.Append(",\"stacked_buy\":").Append(stackedBuy ? "true" : "false");
            msg.Append(",\"stacked_sell\":").Append(stackedSell ? "true" : "false");
            msg.Append(",\"max_imbalance_ratio\":").Append(maxRatio.ToString("F2"));
            msg.Append(",\"imbalance_ratio\":").Append(imbalanceRatio.ToString("F2"));
            msg.Append(",\"cvd_session\":").Append(sessionCvd);
            msg.Append("}");

            Send(msg.ToString());
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
