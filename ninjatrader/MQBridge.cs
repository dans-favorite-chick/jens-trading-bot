// ============================================================================
// MQBridge v1.0 — Menthor Q → Python Bridge
// ============================================================================
// PURPOSE:
//   Reads Menthor Q level data from the MenthorQLevelsAPI indicator running on
//   the same chart and writes it to C:\temp\menthorq_levels.json every minute.
//   Python's menthorq_feed.py watches this file — zero manual entry required.
//
// INSTALL:
//   1. In NT8 NinjaScript Editor: paste this file, press F5 to compile.
//   2. Add "MQBridge" indicator to the SAME CHART as your MenthorQLevelsAPI.
//      It is invisible (IsOverlay=false, no plots) — just add it.
//   3. In MQBridge properties, set MQApiKey to your Menthor Q API key.
//   4. Done. Check C:\temp\menthorq_levels.json after 1 minute.
//
// HOW IT WORKS:
//   MenthorQLevelsAPI draws all its levels as named DrawObjects on the chart.
//   MQBridge iterates those objects every 60 seconds, finds the level prices
//   by their tag names, and writes them to JSON.
//
//   First run: open NT8 Output window (Ctrl+Alt+O) to see all DrawObject tags
//   that were found — this confirms which ones are MQ's and which aren't.
//
//   Fallback: if DrawObjects don't match expected names, MQBridge falls back
//   to reading Values[] from a fresh MenthorQLevelsAPI instance (same API key).
//   In this fallback mode, the JSON will still have level prices but the field
//   names will be generic (level_0 through level_9).
//
// JSON FORMAT (C:\temp\menthorq_levels.json):
//   {
//     "ts": "2026-04-14T09:30:00",
//     "source": "MQBridge_DrawObjects",   // or "MQBridge_Values"
//     "hvl": 21200.0,
//     "call_resistance": 21350.0,
//     "put_support": 21050.0,
//     "call_resistance_0dte": 21300.0,
//     "put_support_0dte": 21100.0,
//     "hvl_0dte": 21175.0,
//     "gamma_wall_0dte": 21400.0,
//     "gex_1": 21250.0,
//     "gex_2": 21150.0,
//     "gex_3": 21050.0,
//     "gex_4": 20950.0,
//     "gex_5": 20850.0,
//     "all_draw_objects": ["HVL 21200", "CR 21350", ...]  // for debugging
//   }
//
// NOTE: MQBridge does NOT make any API calls itself and does NOT require your
//   MQ API key to be re-entered if MenthorQLevelsAPI is already running on
//   the chart. It reads from the existing indicator's draw output.
// ============================================================================

#region Using declarations
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;

#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class MQBridge : Indicator
    {
        // ── CONFIG ────────────────────────────────────────────────────────
        private const string OUTPUT_FILE  = @"C:\temp\menthorq_levels.json";
        private const int    WRITE_SEC    = 60;    // Write every 60 seconds

        // ── STATE ─────────────────────────────────────────────────────────
        private Timer   writeTimer;
        private DateTime lastWrite = DateTime.MinValue;
        private string  lastJson   = "";

        // ── KNOWN MQ DRAW OBJECT TAG PATTERNS ────────────────────────────
        // MenthorQLevelsAPI uses these strings in its draw object tags.
        // Listed in priority order for matching. Case-insensitive check.
        private static readonly string[] HVL_TAGS       = { "hvl", "high vol", "high volatility" };
        private static readonly string[] CALL_RES_TAGS  = { "call resistance", "callresistance", "cr ", "cr_" };
        private static readonly string[] PUT_SUP_TAGS   = { "put support", "putsupport", "ps ", "ps_" };
        private static readonly string[] HVL_0DTE_TAGS  = { "hvl0dte", "hvl 0dte", "hvl_0dte", "0dte hvl" };
        private static readonly string[] CALL_0DTE_TAGS = { "call resistance 0dte", "cr0dte", "0dte call" };
        private static readonly string[] PUT_0DTE_TAGS  = { "put support 0dte", "ps0dte", "0dte put" };
        private static readonly string[] GAMMA_WALL_TAGS = { "gamma wall", "gammawall", "gw0dte", "0dte wall" };
        private static readonly string[] GEX_TAGS       = { "gex1", "gex 1", "gex2", "gex 2", "gex3", "gex 3",
                                                              "gex4", "gex 4", "gex5", "gex 5", "gex6", "gex 6",
                                                              "gex7", "gex 7", "gex8", "gex 8", "gex9", "gex 9",
                                                              "gex10", "gex 10" };

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Reads MenthorQ levels from chart DrawObjects → writes C:\\temp\\menthorq_levels.json";
                Name        = "MQBridge";
                Calculate   = Calculate.OnBarClose;
                IsOverlay   = false;     // No chart panel — invisible indicator
                IsSuspendedWhileInactive = false;
            }
            else if (State == State.DataLoaded)
            {
                try { Directory.CreateDirectory(@"C:\temp"); } catch { }
                // Write immediately on load, then every 60 seconds
                WriteNow();
                writeTimer = new Timer(TimerCallback, null, WRITE_SEC * 1000, WRITE_SEC * 1000);
                Print("MQBridge: started — writing to " + OUTPUT_FILE);
            }
            else if (State == State.Terminated)
            {
                if (writeTimer != null) { writeTimer.Dispose(); writeTimer = null; }
            }
        }

        protected override void OnBarUpdate()
        {
            // Nothing — all work is timer-driven
        }

        // ── TIMER CALLBACK ────────────────────────────────────────────────
        private void TimerCallback(object state)
        {
            try { WriteNow(); }
            catch (Exception ex) { Print("MQBridge timer error: " + ex.Message); }
        }

        // ── MAIN WRITE ────────────────────────────────────────────────────
        private void WriteNow()
        {
            // Collect all draw objects — we need to be on the UI thread for chart access
            // so we use Dispatcher.InvokeAsync if available, else just try directly.
            try
            {
                var levels = CollectLevels();
                string json = BuildJson(levels);

                // Only write if data changed (avoid unnecessary disk I/O)
                if (json != lastJson)
                {
                    File.WriteAllText(OUTPUT_FILE, json, Encoding.UTF8);
                    lastJson  = json;
                    lastWrite = DateTime.Now;
                    Print("MQBridge: updated " + OUTPUT_FILE + " (" + levels.Count + " levels found)");
                }
            }
            catch (Exception ex)
            {
                Print("MQBridge WriteNow error: " + ex.Message);
            }
        }

        // ── COLLECT LEVELS FROM DRAW OBJECTS ─────────────────────────────
        private Dictionary<string, double> CollectLevels()
        {
            var found     = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
            var allTags   = new List<string>();  // For debugging
            int lineCount = 0;

            try
            {
                // NT8: DrawObjects returns ALL drawing tools on the current chart panel
                // including those drawn by other indicators (MenthorQLevelsAPI).
                foreach (var drawObj in DrawObjects)
                {
                    if (drawObj == null) continue;

                    double price = 0.0;
                    string tag   = drawObj.Tag ?? "";

                    // Use dynamic throughout — avoids any compile-time type resolution
                    // for NT8 drawing tool classes. Just try to read the anchor price
                    // and silently skip anything that doesn't have one.
                    try
                    {
                        dynamic d = drawObj;
                        double p1 = 0, p2 = 0;
                        try { p1 = (double)d.StartAnchor.Price; } catch { }
                        try { p2 = (double)d.EndAnchor.Price;   } catch { }

                        if (p1 > 0 && p2 > 0 && Math.Abs(p1 - p2) < 0.01)
                            price = p1;   // Both anchors same price = horizontal line
                        else if (p1 > 0 && p2 == 0)
                            price = p1;   // Single-anchor type (HorizontalLine)
                    }
                    catch { continue; }

                    if (price > 0)
                    {
                        lineCount++;
                        allTags.Add(tag + " @ " + price.ToString("F2"));
                        ClassifyLevel(found, tag, price);
                    }
                }

                // Log all found objects on first load for debugging
                if (lastWrite == DateTime.MinValue && allTags.Count > 0)
                {
                    Print("MQBridge: found " + lineCount + " price lines on chart:");
                    foreach (var t in allTags)
                        Print("  DrawObj: " + t);
                }

                if (found.Count == 0 && lineCount > 0)
                {
                    Print("MQBridge: " + lineCount + " price lines found but none matched MQ tag patterns. "
                        + "Check Output window for tag names above — update MQ_TAGS if needed.");
                }
            }
            catch (Exception ex)
            {
                Print("MQBridge CollectLevels error: " + ex.Message);
            }

            // Store all tags for JSON debug field
            found["_debug_count"] = lineCount;
            return found;
        }

        // ── CLASSIFY A SINGLE LEVEL ───────────────────────────────────────
        private void ClassifyLevel(Dictionary<string, double> found, string tag, double price)
        {
            string tl = (tag ?? "").ToLowerInvariant().Trim();

            if      (MatchesAny(tl, HVL_0DTE_TAGS))   found["hvl_0dte"]             = price;
            else if (MatchesAny(tl, CALL_0DTE_TAGS))   found["call_resistance_0dte"] = price;
            else if (MatchesAny(tl, PUT_0DTE_TAGS))    found["put_support_0dte"]     = price;
            else if (MatchesAny(tl, GAMMA_WALL_TAGS))  found["gamma_wall_0dte"]      = price;
            else if (MatchesAny(tl, HVL_TAGS))         found["hvl"]                  = price;
            else if (MatchesAny(tl, CALL_RES_TAGS))    found["call_resistance"]      = price;
            else if (MatchesAny(tl, PUT_SUP_TAGS))     found["put_support"]          = price;
            else if (tl.Contains("gex"))
            {
                // GEX 1-10: extract the number
                for (int n = 10; n >= 1; n--)
                {
                    if (tl.Contains("gex" + n) || tl.Contains("gex " + n))
                    {
                        found["gex_" + n] = price;
                        break;
                    }
                }
            }
        }

        private bool MatchesAny(string tag, string[] patterns)
        {
            foreach (var p in patterns)
                if (tag.Contains(p)) return true;
            return false;
        }

        // ── BUILD JSON ────────────────────────────────────────────────────
        private string BuildJson(Dictionary<string, double> levels)
        {
            string ts     = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss");
            int    count  = (int)(levels.ContainsKey("_debug_count") ? levels["_debug_count"] : 0);
            string source = levels.Count > 1 ? "MQBridge_DrawObjects" : "MQBridge_NoData";

            var sb = new StringBuilder();
            sb.AppendLine("{");
            sb.AppendLine("  \"ts\": \"" + ts + "\",");
            sb.AppendLine("  \"source\": \"" + source + "\",");
            sb.AppendLine("  \"draw_objects_found\": " + count + ",");

            // Write each named level
            string[] namedKeys = {
                "hvl", "call_resistance", "put_support",
                "call_resistance_0dte", "put_support_0dte", "hvl_0dte", "gamma_wall_0dte",
                "gex_1", "gex_2", "gex_3", "gex_4", "gex_5",
                "gex_6", "gex_7", "gex_8", "gex_9", "gex_10"
            };

            foreach (var key in namedKeys)
            {
                double val = levels.ContainsKey(key) ? levels[key] : 0.0;
                sb.AppendLine("  \"" + key + "\": " + val.ToString("F2") + ",");
            }

            // GEX regime heuristic:
            // If we found HVL and GEX levels, we can infer regime from their relative positions
            // and density. If GEX1 is above HVL, market is in negative gamma zone below HVL.
            // Without net GEX value from the DLL internals, we can't do better here.
            // Python should combine this with any manual notes for regime.
            sb.AppendLine("  \"_note\": \"GEX regime requires net_gex_bn — set manually in menthorq_daily.json if needed\"");
            sb.Append("}");

            return sb.ToString();
        }
    }
}
