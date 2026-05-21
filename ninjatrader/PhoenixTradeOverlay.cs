// ============================================================================
// PhoenixTradeOverlay v1.0 — Live trade visualization indicator (2026-05-18)
// ============================================================================
//
// PURPOSE:
//   Visualizes Phoenix bot trade activity on the chart with:
//     - Color-coded entry triangle (one color per strategy)
//     - Live red dashed line at stop loss (moves with stop adjustments)
//     - Live green dashed line at take-profit target
//     - Strategy name label below entry marker
//     - X marker on exit with P&L annotation
//
// DATA SOURCE:
//   Reads append-only JSONL stream from:
//     C:\Users\Trading PC\Documents\NinjaTrader 8\phoenix_signals.jsonl
//
//   Phoenix writes 4 event types:
//     signal     — bot decided to enter; entry/stop/target prices known
//     fill       — actual NT8 fill at fill_price
//     stop_moved — bot moved stop (e.g., scale_out_1r BE adjustment)
//     exit       — trade closed; pnl + exit_reason logged
//
//   Each line is a single JSON object terminated by \n.
//
// REQUIREMENTS:
//   - No Newtonsoft.Json (not bundled with NT8). Hand-rolled JSON parser
//     handles the simple flat schema we use.
//   - File read is non-blocking (FileShare.ReadWrite) so Phoenix can write
//     while NT8 reads.
//   - State tracked per-trade by unique "id" string from Phoenix.
//
// INSTALL:
//   1. Copy this file to:
//      C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\
//   2. In NT8, NinjaScript Editor → Compile (F5)
//   3. On chart: Indicators → Add → PhoenixTradeOverlay
//
// LIMITATIONS:
//   - Lines are drawn at the timestamp of the event. If chart spans hours
//     of historical data, lines for a 5-minute-old trade may scroll off.
//     Use chart with reasonable time window (1-4 hours typical).
//   - Indicator processes the JSONL file from offset 0 every chart load.
//     For large files (>10MB), startup may take 1-2s. Truncate file weekly
//     if needed.
// ============================================================================

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    // Color map per strategy (RGB hex from STRATEGY_SPECIFICATIONS.md §5.1)
    //
    // 2026-05-20 SHIP AUDIT FIX: rewrote from collection-initializer to
    // a static constructor so each brush is .Freeze()'d before publication.
    // Without freezing, the static field initializer creates WPF brushes
    // bound to the thread that loaded the assembly — NT8's NinjaScript
    // load happens on a worker thread, so any attempt to use the brushes
    // from OnBarUpdate (a different worker thread) throws InvalidOperation
    // and the indicator silently fails to appear in the Indicators picker.
    // Built-in Brushes.* are pre-frozen, so we use them where the
    // STRATEGY_SPECIFICATIONS color happens to match a named WPF color.
    public static class StrategyColors
    {
        public static readonly Dictionary<string, Brush> Map;
        public static readonly Brush DefaultColor;

        static StrategyColors()
        {
            DefaultColor = Brushes.LightGray;  // built-in: already frozen
            Map = new Dictionary<string, Brush>(StringComparer.OrdinalIgnoreCase);

            // Plan §1.1 winners — use built-in Brushes.* where the color
            // matches the spec exactly; Make() for non-standard hex.
            Map["opening_session"]       = Brushes.Yellow;       // canonical Yellow
            Map["raschke_baseline"]      = Brushes.Cyan;
            Map["g_inside_bar_breakout"] = Brushes.Magenta;
            Map["inside_bar_breakout"]   = Brushes.Magenta;      // alias
            Map["e_multi_day_breakout"]  = Brushes.Lime;
            Map["multi_day_breakout"]    = Brushes.Lime;          // alias
            Map["a_asian_continuation"]  = Brushes.MediumPurple;
            Map["asian_continuation"]    = Brushes.MediumPurple;  // alias
            Map["vwap_pullback_v2"]      = Brushes.DarkOrange;
            Map["spring_setup"]          = Brushes.DarkGreen;
            Map["es_nq_confluence"]      = Brushes.White;
            Map["bias_momentum"]         = Brushes.Red;
            Map["vwap_band_pullback"]    = Brushes.SkyBlue;
            Map["vwap_band_reversion"]   = Brushes.HotPink;
            Map["ib_breakout"]           = Brushes.Goldenrod;

            // Disabled / dormant strategies — keep colors so sim_bot
            // overlays are still readable, but use muted Gray for plan-
            // killed ones so the operator can spot them at a glance.
            Map["big_move_signal"]            = Brushes.OrangeRed;
            // Map["dom_pullback"]            deleted 2026-05-21
            Map["nq_lsr"]                     = Brushes.Turquoise;
            Map["orb_fade"]                   = Brushes.Gray;       // killed
            Map["orb_v2"]                     = Brushes.SlateBlue;
            Map["compression_breakout_v2"]    = Brushes.Gray;       // killed
            Map["compression_breakout_micro"] = Brushes.Gray;       // killed
            Map["footprint_cvd_reversal"]     = Brushes.Gray;       // dormant
            Map["orb"]                        = Brushes.Yellow;     // top-level
        }

        public static Brush Get(string strategy)
        {
            if (string.IsNullOrEmpty(strategy)) return DefaultColor;
            Brush b;
            return Map.TryGetValue(strategy, out b) ? b : DefaultColor;
        }
    }

    // Per-trade state held in memory
    public class ActiveTrade
    {
        public string Id;
        public string Strategy;
        public string Direction;       // "LONG" or "SHORT"
        public DateTime SignalTime;
        public double EntryPrice;
        public double StopPrice;
        public double TargetPrice;
        public double FillPrice = double.NaN;
        public DateTime FillTime;
        public bool IsClosed = false;
    }

    public class PhoenixTradeOverlay : Indicator
    {
        // ── CONFIG ────────────────────────────────────────────────────────
        private const string SIGNAL_FILE_PATH =
            @"C:\Users\Trading PC\Documents\NinjaTrader 8\phoenix_signals.jsonl";

        // ── STATE ─────────────────────────────────────────────────────────
        private long lastFileOffset = 0;
        private readonly Dictionary<string, ActiveTrade> trades = new Dictionary<string, ActiveTrade>();

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Visualizes Phoenix bot signals + live stops/targets on chart";
                Name = "PhoenixTradeOverlay";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
                DisplayInDataBox = false;
                DrawOnPricePanel = true;
                PaintPriceMarkers = false;
                ScaleJustification = NinjaTrader.Gui.Chart.ScaleJustification.Right;
                IsSuspendedWhileInactive = true;
            }
            else if (State == State.Configure)
            {
                lastFileOffset = 0;
                trades.Clear();
                // 2026-05-20 SHIP AUDIT: visible load-confirmation Print so
                // operator can verify the indicator is at least running.
                // Look for this line in Tools -> NinjaScript -> Output Window.
                Print("[PhoenixTradeOverlay] Configure: loaded, watching " +
                      SIGNAL_FILE_PATH + " (" + StrategyColors.Map.Count +
                      " strategy colors mapped)");
            }
            else if (State == State.Realtime)
            {
                Print("[PhoenixTradeOverlay] Realtime: processing existing events");
                // On entering realtime, read any existing events so we sync state
                ProcessNewEvents();
            }
        }

        protected override void OnBarUpdate()
        {
            // Process new JSONL events every bar close
            try
            {
                ProcessNewEvents();
                UpdateActiveLineDrawings();
            }
            catch (Exception ex)
            {
                Print($"[PhoenixTradeOverlay] OnBarUpdate err: {ex.Message}");
            }
        }

        // ─────────────────────────────────────────────────────────────────
        // File reading + event processing
        // ─────────────────────────────────────────────────────────────────
        private void ProcessNewEvents()
        {
            if (!File.Exists(SIGNAL_FILE_PATH)) return;

            try
            {
                using (var fs = new FileStream(SIGNAL_FILE_PATH, FileMode.Open,
                                                FileAccess.Read, FileShare.ReadWrite))
                {
                    if (lastFileOffset > fs.Length)
                    {
                        // File was truncated/rotated; start over
                        lastFileOffset = 0;
                    }
                    fs.Seek(lastFileOffset, SeekOrigin.Begin);
                    using (var sr = new StreamReader(fs, Encoding.UTF8))
                    {
                        string line;
                        while ((line = sr.ReadLine()) != null)
                        {
                            line = line.Trim();
                            if (line.Length == 0) continue;
                            HandleEvent(line);
                        }
                        lastFileOffset = fs.Position;
                    }
                }
            }
            catch (Exception ex)
            {
                Print($"[PhoenixTradeOverlay] file read err: {ex.Message}");
            }
        }

        private void HandleEvent(string jsonLine)
        {
            var parsed = ParseJson(jsonLine);
            if (parsed == null) return;
            string evt = GetStr(parsed, "event");
            string id = GetStr(parsed, "id");
            if (string.IsNullOrEmpty(evt) || string.IsNullOrEmpty(id)) return;

            switch (evt)
            {
                case "signal":
                    HandleSignalEvent(parsed, id);
                    break;
                case "fill":
                    HandleFillEvent(parsed, id);
                    break;
                case "stop_moved":
                    HandleStopMovedEvent(parsed, id);
                    break;
                case "exit":
                    HandleExitEvent(parsed, id);
                    break;
            }
        }

        private void HandleSignalEvent(Dictionary<string, string> p, string id)
        {
            var trade = new ActiveTrade
            {
                Id         = id,
                Strategy   = GetStr(p, "strategy"),
                Direction  = GetStr(p, "direction"),
                SignalTime = ParseTimestamp(GetStr(p, "ts")),
                EntryPrice = GetDouble(p, "entry"),
                StopPrice  = GetDouble(p, "stop"),
                TargetPrice= GetDouble(p, "target"),
            };
            trades[id] = trade;
            DrawEntryMarker(trade);
        }

        private void HandleFillEvent(Dictionary<string, string> p, string id)
        {
            // 2026-05-20 SHIP AUDIT: out-var declarations rewritten to explicit
            // types so the file compiles on older NT8 C# 5/6 toolchains.
            ActiveTrade trade;
            if (!trades.TryGetValue(id, out trade)) return;
            trade.FillPrice = GetDouble(p, "fill_price");
            trade.FillTime  = ParseTimestamp(GetStr(p, "ts"));
            // No additional drawing — entry marker already placed at signal
        }

        private void HandleStopMovedEvent(Dictionary<string, string> p, string id)
        {
            ActiveTrade trade;
            if (!trades.TryGetValue(id, out trade)) return;
            trade.StopPrice = GetDouble(p, "new_stop");
            // Stop line will be re-drawn on next UpdateActiveLineDrawings call
            // (existing drawing object overwritten by same name tag)
        }

        private void HandleExitEvent(Dictionary<string, string> p, string id)
        {
            ActiveTrade trade;
            if (!trades.TryGetValue(id, out trade)) return;
            trade.IsClosed = true;
            double exitPrice  = GetDouble(p, "exit_price");
            string exitReason = GetStr(p, "exit_reason");
            double pnl        = GetDouble(p, "pnl");
            DateTime exitTime = ParseTimestamp(GetStr(p, "ts"));
            DrawExitMarker(trade, exitTime, exitPrice, exitReason, pnl);
            RemoveActiveLines(trade);
        }

        // ─────────────────────────────────────────────────────────────────
        // Chart drawing
        // ─────────────────────────────────────────────────────────────────
        private void DrawEntryMarker(ActiveTrade trade)
        {
            try
            {
                Brush color = StrategyColors.Get(trade.Strategy);
                string tag = "ENTRY_" + trade.Id;
                int barsAgo = BarsAgoForTime(trade.SignalTime);

                if (trade.Direction == "LONG")
                {
                    Draw.TriangleUp(this, tag, false, barsAgo, trade.EntryPrice - 2 * TickSize, color);
                }
                else
                {
                    Draw.TriangleDown(this, tag, false, barsAgo, trade.EntryPrice + 2 * TickSize, color);
                }

                // Strategy name label below the marker
                string labelTag = "LABEL_" + trade.Id;
                Draw.Text(this, labelTag, trade.Strategy, barsAgo,
                          trade.EntryPrice - 6 * TickSize, color);
            }
            catch (Exception ex)
            {
                Print($"[PhoenixTradeOverlay] DrawEntry err: {ex.Message}");
            }
        }

        private void DrawExitMarker(ActiveTrade trade, DateTime exitTime,
                                     double exitPrice, string reason, double pnl)
        {
            try
            {
                Brush color = StrategyColors.Get(trade.Strategy);
                string tag = "EXIT_" + trade.Id;
                int barsAgo = BarsAgoForTime(exitTime);
                Draw.Diamond(this, tag, false, barsAgo, exitPrice, color);

                // P&L annotation
                string labelTag = "EXITLABEL_" + trade.Id;
                string pnlText = (pnl >= 0 ? "+$" : "-$") + Math.Abs(pnl).ToString("0.00")
                                 + " " + reason;
                Brush textColor = pnl >= 0 ? Brushes.LimeGreen : Brushes.OrangeRed;
                Draw.Text(this, labelTag, pnlText, barsAgo,
                          exitPrice + (trade.Direction == "LONG" ? 4 : -4) * TickSize, textColor);
            }
            catch (Exception ex)
            {
                Print($"[PhoenixTradeOverlay] DrawExit err: {ex.Message}");
            }
        }

        private void UpdateActiveLineDrawings()
        {
            // For each active (not closed) trade, draw horizontal stop + target lines
            foreach (var trade in trades.Values)
            {
                if (trade.IsClosed) continue;
                try
                {
                    string stopTag   = "STOP_"   + trade.Id;
                    string targetTag = "TARGET_" + trade.Id;
                    // Horizontal lines at stop / target — extend across chart
                    Draw.HorizontalLine(this, stopTag,   trade.StopPrice,   Brushes.Red,   DashStyleHelper.Dash, 1);
                    Draw.HorizontalLine(this, targetTag, trade.TargetPrice, Brushes.LimeGreen, DashStyleHelper.Dash, 1);
                }
                catch (Exception ex)
                {
                    Print($"[PhoenixTradeOverlay] UpdateLines err: {ex.Message}");
                }
            }
        }

        private void RemoveActiveLines(ActiveTrade trade)
        {
            try
            {
                RemoveDrawObject("STOP_"   + trade.Id);
                RemoveDrawObject("TARGET_" + trade.Id);
            }
            catch (Exception ex)
            {
                Print($"[PhoenixTradeOverlay] RemoveLines err: {ex.Message}");
            }
        }

        private int BarsAgoForTime(DateTime targetTime)
        {
            // Walk backward from current bar to find the bar containing targetTime
            for (int i = 0; i < Bars.Count; i++)
            {
                DateTime barTime = Time[i];
                if (barTime <= targetTime) return i;
            }
            return 0;  // default to current bar if not found
        }

        // ─────────────────────────────────────────────────────────────────
        // Hand-rolled JSON parser — no Newtonsoft.Json available in NT8
        // Handles flat key:value pairs only (our schema is flat)
        // ─────────────────────────────────────────────────────────────────
        private Dictionary<string, string> ParseJson(string jsonLine)
        {
            try
            {
                var dict = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                // Strip { } at ends
                int start = jsonLine.IndexOf('{');
                int end   = jsonLine.LastIndexOf('}');
                if (start < 0 || end <= start) return null;
                string body = jsonLine.Substring(start + 1, end - start - 1);
                // Tokenize by , — but only outside quotes
                var sb = new StringBuilder();
                bool inQuote = false;
                var pairs = new List<string>();
                for (int i = 0; i < body.Length; i++)
                {
                    char c = body[i];
                    if (c == '"' && (i == 0 || body[i - 1] != '\\')) inQuote = !inQuote;
                    if (c == ',' && !inQuote)
                    {
                        pairs.Add(sb.ToString());
                        sb.Clear();
                    }
                    else
                    {
                        sb.Append(c);
                    }
                }
                if (sb.Length > 0) pairs.Add(sb.ToString());

                foreach (var pair in pairs)
                {
                    int colon = pair.IndexOf(':');
                    if (colon < 0) continue;
                    string key = pair.Substring(0, colon).Trim().Trim('"');
                    string val = pair.Substring(colon + 1).Trim().Trim('"');
                    dict[key] = val;
                }
                return dict;
            }
            catch
            {
                return null;
            }
        }

        private string GetStr(Dictionary<string, string> dict, string key)
        {
            string v;
            return dict.TryGetValue(key, out v) ? v : "";
        }

        private double GetDouble(Dictionary<string, string> dict, string key)
        {
            string v;
            if (!dict.TryGetValue(key, out v)) return double.NaN;
            double d;
            if (double.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out d))
                return d;
            return double.NaN;
        }

        private DateTime ParseTimestamp(string ts)
        {
            if (string.IsNullOrEmpty(ts)) return DateTime.MinValue;
            DateTime dt;
            if (DateTime.TryParse(ts, CultureInfo.InvariantCulture,
                                   DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                                   out dt))
            {
                return dt.ToLocalTime();
            }
            return DateTime.MinValue;
        }
    }
}
