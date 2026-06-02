#region Using declarations
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

// PhoenixTradeMarkers — 2026-06-02
//
// Tails <NT8 data root>/incoming/phoenix_markers.jsonl (written by
// core.nt8_chart_markers.ChartMarkerWriter from the Python bot) and
// draws per-trade markers on the chart.
//
// CRITICAL DESIGN RULES (mirrored from CLAUDE.md):
//   1) Indicator, not Strategy. ErrorHandling=Stop crashes Strategy.
//   2) No Newtonsoft.Json. Hand-parse the line — schema is fixed.
//   3) Reads only. NEVER places orders.
//   4) Independent of TickStreamer.cs — different file, different
//      lifecycle, can coexist with PhoenixTradeOverlay.
//
// Schema (per line, see core/nt8_chart_markers.py):
//   entry          {event:"entry",  trade_id, strategy, direction,
//                   entry_price, stop, target, color, symbol, ts}
//   exit           {event:"exit",   trade_id, exit_price, exit_reason, pnl, ts}
//   stop_update    {event:"stop_update",   trade_id, stop,   ts}
//   target_update  {event:"target_update", trade_id, target, ts}
//
// File-tail state: we track the byte offset in the JSONL file across
// OnBarUpdate calls so we never re-process old events. Reset to 0 on
// OnStateChange(DataLoaded) so a chart reload re-draws every marker.
namespace NinjaTrader.NinjaScript.Indicators
{
    public class PhoenixTradeMarkers : Indicator
    {
        // ── per-trade state we carry while a trade is open ─────────
        private class LiveTrade
        {
            public string TradeId;
            public string Strategy;
            public string Direction;   // LONG / SHORT
            public double EntryPrice;
            public double Stop;
            public double Target;
            public Brush Color;
            public string Symbol;
            public DateTime EntryTs;
            // tags of drawing objects we created (so we can remove on exit)
            public string EntryTag;
            public string StopTag;
            public string TargetTag;
        }

        private Dictionary<string, LiveTrade> _live = new Dictionary<string, LiveTrade>();
        private long _fileOffset = 0;          // bytes consumed so far
        private string _jsonlPath;
        private int _markerCount = 0;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Phoenix bot trade markers (reads phoenix_markers.jsonl).";
                Name = "PhoenixTradeMarkers";
                Calculate = Calculate.OnEachTick;
                IsOverlay = true;
                DisplayInDataBox = false;
                DrawOnPricePanel = true;
                PaintPriceMarkers = false;
                IsSuspendedWhileInactive = true;
                // Single string property: where to find the JSONL feed.
                JsonlPath = @"C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\phoenix_markers.jsonl";
            }
            else if (State == State.Configure)
            {
                _jsonlPath = JsonlPath;
            }
            else if (State == State.DataLoaded)
            {
                // Fresh load (chart reload) → reprocess every marker.
                _fileOffset = 0;
                _live.Clear();
                _markerCount = 0;
            }
        }

        protected override void OnBarUpdate()
        {
            // Tail the file. Cheap when the file hasn't grown.
            if (string.IsNullOrEmpty(_jsonlPath) || !File.Exists(_jsonlPath))
                return;
            try
            {
                using (var fs = new FileStream(_jsonlPath, FileMode.Open,
                                               FileAccess.Read, FileShare.ReadWrite))
                {
                    if (fs.Length <= _fileOffset) return;
                    fs.Seek(_fileOffset, SeekOrigin.Begin);
                    using (var sr = new StreamReader(fs, Encoding.UTF8))
                    {
                        string line;
                        while ((line = sr.ReadLine()) != null)
                        {
                            if (line.Length > 0) ProcessLine(line);
                        }
                        _fileOffset = fs.Position;
                    }
                }
            }
            catch (IOException)
            {
                // Concurrent write — try again next bar. Never throw.
            }
            catch (Exception ex)
            {
                Print("PhoenixTradeMarkers ERR: " + ex.Message);
            }
        }

        // ── JSONL line dispatch ────────────────────────────────────
        private void ProcessLine(string line)
        {
            string evt = ExtractString(line, "event");
            string tid = ExtractString(line, "trade_id");
            if (string.IsNullOrEmpty(evt) || string.IsNullOrEmpty(tid)) return;

            switch (evt)
            {
                case "entry":         HandleEntry(line, tid); break;
                case "exit":          HandleExit(line, tid); break;
                case "stop_update":   HandleStopUpdate(line, tid); break;
                case "target_update": HandleTargetUpdate(line, tid); break;
            }
        }

        private void HandleEntry(string line, string tid)
        {
            if (_live.ContainsKey(tid)) return;  // dedupe replays

            var lt = new LiveTrade
            {
                TradeId   = tid,
                Strategy  = ExtractString(line, "strategy"),
                Direction = ExtractString(line, "direction"),
                EntryPrice = ExtractDouble(line, "entry_price"),
                Stop       = ExtractDouble(line, "stop"),
                Target     = ExtractDouble(line, "target"),
                Color      = ParseHexBrush(ExtractString(line, "color")),
                Symbol     = ExtractString(line, "symbol"),
                EntryTs    = UnixToLocal(ExtractDouble(line, "ts")),
            };
            int barIdx = NearestBarForTime(lt.EntryTs);
            if (barIdx < 0) return;
            lt.EntryTag = "PMK_E_" + tid;
            // ArrowUp for LONG, ArrowDown for SHORT — strategy symbol is
            // surfaced via color; we keep the geometry direction-aware so
            // a glance tells you the side.
            int barsAgo = CurrentBar - barIdx;
            if (barsAgo < 0) return;
            if (lt.Direction == "LONG")
                Draw.ArrowUp(this, lt.EntryTag, false, barsAgo, lt.EntryPrice - TickSize * 2, lt.Color);
            else
                Draw.ArrowDown(this, lt.EntryTag, false, barsAgo, lt.EntryPrice + TickSize * 2, lt.Color);

            // Stop + target lines (dashed, full chart width via ExtendRight).
            if (lt.Stop > 0)
            {
                lt.StopTag = "PMK_S_" + tid;
                var rstop = Draw.HorizontalLine(this, lt.StopTag, lt.Stop, Brushes.Red);
                if (rstop != null) rstop.Stroke.DashStyleHelper = DashStyleHelper.Dash;
            }
            if (lt.Target > 0)
            {
                lt.TargetTag = "PMK_T_" + tid;
                var rtgt = Draw.HorizontalLine(this, lt.TargetTag, lt.Target, Brushes.LimeGreen);
                if (rtgt != null) rtgt.Stroke.DashStyleHelper = DashStyleHelper.Dash;
            }
            _live[tid] = lt;
            _markerCount++;
        }

        private void HandleExit(string line, string tid)
        {
            LiveTrade lt;
            if (!_live.TryGetValue(tid, out lt)) return;
            double exitPrice = ExtractDouble(line, "exit_price");
            DateTime exitTs  = UnixToLocal(ExtractDouble(line, "ts"));
            int barIdx = NearestBarForTime(exitTs);
            if (barIdx >= 0)
            {
                int barsAgo = CurrentBar - barIdx;
                if (barsAgo >= 0)
                    Draw.Text(this, "PMK_X_" + tid, "x", barsAgo,
                              exitPrice + TickSize * 2, Brushes.Gray);
            }
            // Remove stop / target lines — trade is over.
            if (!string.IsNullOrEmpty(lt.StopTag))   RemoveDrawObject(lt.StopTag);
            if (!string.IsNullOrEmpty(lt.TargetTag)) RemoveDrawObject(lt.TargetTag);
            _live.Remove(tid);
        }

        private void HandleStopUpdate(string line, string tid)
        {
            LiveTrade lt;
            if (!_live.TryGetValue(tid, out lt)) return;
            double newStop = ExtractDouble(line, "stop");
            if (newStop <= 0) return;
            lt.Stop = newStop;
            if (!string.IsNullOrEmpty(lt.StopTag)) RemoveDrawObject(lt.StopTag);
            lt.StopTag = "PMK_S_" + tid;
            var r = Draw.HorizontalLine(this, lt.StopTag, lt.Stop, Brushes.Red);
            if (r != null) r.Stroke.DashStyleHelper = DashStyleHelper.Dash;
        }

        private void HandleTargetUpdate(string line, string tid)
        {
            LiveTrade lt;
            if (!_live.TryGetValue(tid, out lt)) return;
            double newTgt = ExtractDouble(line, "target");
            if (newTgt <= 0) return;
            lt.Target = newTgt;
            if (!string.IsNullOrEmpty(lt.TargetTag)) RemoveDrawObject(lt.TargetTag);
            lt.TargetTag = "PMK_T_" + tid;
            var r = Draw.HorizontalLine(this, lt.TargetTag, lt.Target, Brushes.LimeGreen);
            if (r != null) r.Stroke.DashStyleHelper = DashStyleHelper.Dash;
        }

        // ── tiny hand-rolled JSON extractors (schema-fixed) ────────
        // No Newtonsoft.Json (per CLAUDE.md). Each value lives at
        // `"key":` immediately followed by the value. Strings are
        // quoted; numbers are not. Adequate for our well-formed,
        // machine-emitted JSONL.
        private static string ExtractString(string line, string key)
        {
            string needle = "\"" + key + "\":\"";
            int i = line.IndexOf(needle);
            if (i < 0) return "";
            int start = i + needle.Length;
            int end = line.IndexOf('"', start);
            if (end < 0) return "";
            return line.Substring(start, end - start);
        }

        private static double ExtractDouble(string line, string key)
        {
            // Try unquoted-number form first: "key":<num>[,}]
            string needleNum = "\"" + key + "\":";
            int i = line.IndexOf(needleNum);
            if (i < 0) return 0.0;
            int start = i + needleNum.Length;
            if (start >= line.Length) return 0.0;
            // Skip whitespace (shouldn't exist in compact-encoded line,
            // but defensive).
            while (start < line.Length && (line[start] == ' ' || line[start] == '\t')) start++;
            if (start >= line.Length) return 0.0;
            // null → treat as 0
            if (line[start] == 'n') return 0.0;
            // Quoted number (we don't emit, but be lenient)
            if (line[start] == '"')
            {
                int qend = line.IndexOf('"', start + 1);
                if (qend < 0) return 0.0;
                double dq;
                return double.TryParse(line.Substring(start + 1, qend - start - 1),
                                       System.Globalization.NumberStyles.Float,
                                       System.Globalization.CultureInfo.InvariantCulture,
                                       out dq) ? dq : 0.0;
            }
            int end = start;
            while (end < line.Length)
            {
                char c = line[end];
                if (c == ',' || c == '}' || c == ' ' || c == '\n' || c == '\r') break;
                end++;
            }
            double d;
            if (double.TryParse(line.Substring(start, end - start),
                                System.Globalization.NumberStyles.Float,
                                System.Globalization.CultureInfo.InvariantCulture,
                                out d))
                return d;
            return 0.0;
        }

        // Map "#RRGGBB" → SolidColorBrush; fallback grey on parse error.
        private static Brush ParseHexBrush(string hex)
        {
            try
            {
                if (string.IsNullOrEmpty(hex) || hex[0] != '#' || hex.Length < 7)
                    return Brushes.LightGray;
                byte r = Convert.ToByte(hex.Substring(1, 2), 16);
                byte g = Convert.ToByte(hex.Substring(3, 2), 16);
                byte b = Convert.ToByte(hex.Substring(5, 2), 16);
                var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
                brush.Freeze();
                return brush;
            }
            catch { return Brushes.LightGray; }
        }

        // Unix epoch seconds → chart-local DateTime. ToLocalTime so the
        // bar lookup matches the timestamps NT8 stamps on bars.
        private static DateTime UnixToLocal(double unixSec)
        {
            try
            {
                return DateTimeOffset.FromUnixTimeMilliseconds((long)(unixSec * 1000.0))
                                     .LocalDateTime;
            }
            catch { return DateTime.Now; }
        }

        // Find the bar index whose Time is the largest <= target.
        // -1 if target predates the chart.
        private int NearestBarForTime(DateTime target)
        {
            for (int barsAgo = 0; barsAgo <= Math.Min(CurrentBar, 500); barsAgo++)
            {
                if (Time[barsAgo] <= target) return CurrentBar - barsAgo;
            }
            return -1;
        }

        // ── user-visible property ──────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "JSONL Path", Order = 1, GroupName = "Parameters",
                 Description = "Full path to phoenix_markers.jsonl")]
        public string JsonlPath { get; set; }
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
    public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
    {
        private PhoenixTradeMarkers[] cachePhoenixTradeMarkers;
        public PhoenixTradeMarkers PhoenixTradeMarkers(string jsonlPath)
        {
            return PhoenixTradeMarkers(Input, jsonlPath);
        }
        public PhoenixTradeMarkers PhoenixTradeMarkers(ISeries<double> input, string jsonlPath)
        {
            if (cachePhoenixTradeMarkers != null)
                for (int idx = 0; idx < cachePhoenixTradeMarkers.Length; idx++)
                    if (cachePhoenixTradeMarkers[idx] != null && cachePhoenixTradeMarkers[idx].JsonlPath == jsonlPath && cachePhoenixTradeMarkers[idx].EqualsInput(input))
                        return cachePhoenixTradeMarkers[idx];
            return CacheIndicator<PhoenixTradeMarkers>(new PhoenixTradeMarkers() { JsonlPath = jsonlPath }, input, ref cachePhoenixTradeMarkers);
        }
    }
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
    public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
    {
        public Indicators.PhoenixTradeMarkers PhoenixTradeMarkers(string jsonlPath)
        {
            return indicator.PhoenixTradeMarkers(Input, jsonlPath);
        }
        public Indicators.PhoenixTradeMarkers PhoenixTradeMarkers(ISeries<double> input, string jsonlPath)
        {
            return indicator.PhoenixTradeMarkers(input, jsonlPath);
        }
    }
}

namespace NinjaTrader.NinjaScript.Strategies
{
    public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
    {
        public Indicators.PhoenixTradeMarkers PhoenixTradeMarkers(string jsonlPath)
        {
            return indicator.PhoenixTradeMarkers(Input, jsonlPath);
        }
        public Indicators.PhoenixTradeMarkers PhoenixTradeMarkers(ISeries<double> input, string jsonlPath)
        {
            return indicator.PhoenixTradeMarkers(input, jsonlPath);
        }
    }
}

#endregion
