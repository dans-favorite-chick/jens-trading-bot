#region Using declarations
using System;
using System.IO;
using System.Text;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    /// <summary>
    /// Phoenix Bot — Historical Data Exporter
    ///
    /// Exports 1-minute bar data from the chart to CSV for backtesting.
    /// Add to a MNQM6 1-minute chart with 90+ days of data loaded.
    /// Writes to C:\temp\mnq_historical.csv on chart load.
    ///
    /// Usage:
    ///   1. Tools > Edit NinjaScript > New Indicator > paste this > compile (F5)
    ///   2. Open MNQM6 1-min chart (right-click chart > Data Series > set days back to 90)
    ///   3. Right-click chart > Indicators > add HistoricalExporter
    ///   4. CSV writes automatically to C:\temp\mnq_historical.csv
    ///
    /// Does NOT use Newtonsoft.Json (not bundled with NT8).
    /// Does NOT interfere with TickStreamer or any other indicator.
    /// </summary>
    public class HistoricalExporter : Indicator
    {
        private bool _exported = false;
        private string _outputPath = @"C:\temp\mnq_historical.csv";

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Exports 1-min bar history to CSV for Phoenix Bot backtesting";
                Name = "HistoricalExporter";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
            }
            else if (State == State.Historical)
            {
                // Ensure output directory exists
                try
                {
                    string dir = Path.GetDirectoryName(_outputPath);
                    if (!Directory.Exists(dir))
                        Directory.CreateDirectory(dir);
                }
                catch (Exception ex)
                {
                    Print("HistoricalExporter: Cannot create output dir: " + ex.Message);
                }
            }
        }

        protected override void OnBarUpdate()
        {
            // Only export once, after all historical bars are loaded
            if (_exported)
                return;

            // Wait until we're processing the last historical bar
            if (State != State.Historical || CurrentBar < Count - 2)
                return;

            ExportAllBars();
            _exported = true;
        }

        private void ExportAllBars()
        {
            try
            {
                StringBuilder sb = new StringBuilder();

                // CSV header
                sb.AppendLine("timestamp,open,high,low,close,volume,tickCount");

                int barCount = 0;

                for (int i = 0; i < Count; i++)
                {
                    // Format: ISO 8601 timestamp, OHLCV, tick count
                    DateTime barTime = Time.GetValueAt(i);
                    double o = Open.GetValueAt(i);
                    double h = High.GetValueAt(i);
                    double l = Low.GetValueAt(i);
                    double c = Close.GetValueAt(i);
                    double v = Volume.GetValueAt(i);

                    // Tick count not directly available on bars, estimate from volume
                    // (MNQ typical: 1 tick = 1-3 volume units)
                    int tickEst = Math.Max(1, (int)(v / 2.0));

                    sb.Append(barTime.ToString("yyyy-MM-ddTHH:mm:ss"));
                    sb.Append(",");
                    sb.Append(o.ToString("F2"));
                    sb.Append(",");
                    sb.Append(h.ToString("F2"));
                    sb.Append(",");
                    sb.Append(l.ToString("F2"));
                    sb.Append(",");
                    sb.Append(c.ToString("F2"));
                    sb.Append(",");
                    sb.Append(((long)v).ToString());
                    sb.Append(",");
                    sb.AppendLine(tickEst.ToString());

                    barCount++;
                }

                File.WriteAllText(_outputPath, sb.ToString());

                Print("=== HistoricalExporter ===");
                Print("Exported " + barCount + " bars to " + _outputPath);
                Print("Date range: " + Time.GetValueAt(0).ToString("yyyy-MM-dd") +
                      " to " + Time.GetValueAt(Count - 1).ToString("yyyy-MM-dd"));
                Print("File size: " + (sb.Length / 1024) + " KB");
                Print("==========================");
            }
            catch (Exception ex)
            {
                Print("HistoricalExporter ERROR: " + ex.Message);
            }
        }
    }
}
