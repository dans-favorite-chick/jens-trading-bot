# Phase 0a — Verify L2 Data Quality

**Goal**: Confirm that the NT8 data feed provides usable Level 2 (Market Depth) data on MNQ before investing any further work in OBI features.

**Duration**: 1 hour of RTH observation + ~15 minutes of setup/review.

**Risk**: Zero. This phase only reads data and prints stats. No trading. No production changes.

**Outcome**: Clear GO / NO-GO decision.

## Why this comes first

Without confirmed streaming L2 data, the entire OBI feature is unbuildable. Some retail feeds aggregate or sample L2 rather than providing every event. Some don't include L2 at all without an upgrade. Spending a week building feature engineering on top of bad data would be wasted work.

This is a 1-hour check that produces a Go/No-Go.

## Acceptance criteria

Run the test indicator on MNQ during US RTH (8:30 AM - 3:15 PM CST) for at least 1 hour. The feed PASSES if:

- [ ] Total events seen ≥ **180,000** (= 50 events/sec × 3600 sec)
- [ ] Unique price levels seen ≥ **30**
- [ ] No "OnMarketDepth is not subscribed" errors in the NT8 Log tab
- [ ] Median top-5 book thickness ≥ **50 contracts/side**
- [ ] OOO rate (events arriving more than 50ms before previous event) ≤ **5%**

If all 5 pass → **GO**. Proceed to Phase 0b.

If 1-2 fail → contact broker about L2 subscription tier. Likely need an upgrade ($10-50/mo for retail-grade streaming L2).

If 3+ fail → reconsider the feature entirely. The feed may not support the use case at any reasonable cost.

## Step-by-step

### Step 1: Verify ATI / data subscription is active

1. Open NinjaTrader 8
2. **Tools → Options → ATI tab** — confirm ATI is enabled (you already have this for OIF execution)
3. **Connections** — confirm your data feed is connected and shows "Connected" status
4. **Control Center → Connections → right-click your data connection → Properties** — note the data provider name (Kinetick, CQG, Rithmic, etc.). You'll need this if Phase 0a fails and you need to call about L2 subscription.

### Step 2: Create the test indicator

Save the code below as `PhoenixL2Probe.cs` in:

```
C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\bin\Custom\Indicators\PhoenixL2Probe.cs
```

(Remember: NT8 data lives in OneDrive per Hard Rule #2. Don't move it.)

```csharp
#region Using declarations
using System;
using System.Collections.Generic;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    /// <summary>
    /// Phase 0a probe — verifies L2 data quality on the attached instrument.
    /// Read-only. Prints stats every 60 seconds to the Output window.
    /// </summary>
    public class PhoenixL2Probe : Indicator
    {
        private long _eventsSeen = 0;
        private long _oooCount = 0;
        private HashSet<double> _uniquePrices = new HashSet<double>();
        private DateTime _testStart;
        private DateTime _lastEventTime = DateTime.MinValue;
        private DateTime _lastPrintTime = DateTime.MinValue;

        // For book thickness sampling
        private SortedDictionary<double, long> _bids = new SortedDictionary<double, long>();
        private SortedDictionary<double, long> _asks = new SortedDictionary<double, long>();
        private readonly object _bookLock = new object();
        private List<long> _thicknessSamples = new List<long>();

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Phase 0a probe — read-only L2 quality check";
                Name = "PhoenixL2Probe";
                Calculate = Calculate.OnEachTick;
                IsOverlay = true;
                DisplayInDataBox = false;
                IsSuspendedWhileInactive = false;  // CRITICAL — keep firing if minimized
            }
            else if (State == State.DataLoaded)
            {
                _testStart = DateTime.Now;
                Print("");
                Print("==================================================");
                Print($"PhoenixL2Probe START on {Instrument.FullName}");
                Print($"Started at {_testStart:HH:mm:ss}");
                Print("Stats print every 60 seconds. Run for 1 hour during RTH.");
                Print("==================================================");
            }
        }

        protected override void OnMarketDepth(MarketDepthEventArgs e)
        {
            _eventsSeen++;
            _uniquePrices.Add(e.Price);

            // Out-of-order detection
            if (_lastEventTime != DateTime.MinValue && 
                e.Time < _lastEventTime.AddMilliseconds(-50))
            {
                _oooCount++;
            }
            if (e.Time > _lastEventTime) _lastEventTime = e.Time;

            // Maintain book for thickness sampling
            lock (_bookLock)
            {
                var sideMap = (e.MarketDataType == MarketDataType.Bid) ? _bids : _asks;
                switch (e.Operation)
                {
                    case Operation.Add:
                    case Operation.Update:
                        sideMap[e.Price] = e.Volume;
                        break;
                    case Operation.Remove:
                        sideMap.Remove(e.Price);
                        break;
                }
            }

            // Print every ~60s (driven by event flow, not a timer, to keep it simple)
            if ((DateTime.Now - _lastPrintTime).TotalSeconds >= 60)
            {
                PrintStats();
                _lastPrintTime = DateTime.Now;
            }
        }

        protected override void OnBarUpdate()
        {
            // Sample book thickness on bar updates
            lock (_bookLock)
            {
                long bidThickness = 0, askThickness = 0;
                int i = 0;
                foreach (var kvp in _bids) { if (++i > 5) break; bidThickness += kvp.Value; }
                i = 0;
                foreach (var kvp in _asks) { if (++i > 5) break; askThickness += kvp.Value; }
                if (bidThickness > 0 && askThickness > 0)
                    _thicknessSamples.Add((bidThickness + askThickness) / 2);
            }
        }

        private void PrintStats()
        {
            double elapsedSec = (DateTime.Now - _testStart).TotalSeconds;
            double eventsPerSec = elapsedSec > 0 ? _eventsSeen / elapsedSec : 0;
            double oooRate = _eventsSeen > 0 ? (double)_oooCount / _eventsSeen * 100 : 0;
            
            long medianThickness = 0;
            if (_thicknessSamples.Count > 0)
            {
                var sorted = new List<long>(_thicknessSamples);
                sorted.Sort();
                medianThickness = sorted[sorted.Count / 2];
            }

            Print("");
            Print($"--- L2 Probe @ {DateTime.Now:HH:mm:ss} (elapsed: {elapsedSec/60:F1}m) ---");
            Print($"  Total events:       {_eventsSeen:N0}");
            Print($"  Events/sec:         {eventsPerSec:F1}  [target ≥ 50]");
            Print($"  Unique prices:      {_uniquePrices.Count}  [target ≥ 30]");
            Print($"  Out-of-order rate:  {oooRate:F2}%  [target ≤ 5%]");
            Print($"  Median top-5 thickness: {medianThickness} contracts  [target ≥ 50]");
            Print($"  Samples in thickness median: {_thicknessSamples.Count}");
        }
    }
}
```

### Step 3: Compile and add to chart

1. In NT8: **F5** (or Tools → Edit NinjaScript → Compile) — should compile with no errors. If you get an error about `BarsPeriodType` or similar, you forgot `using NinjaTrader.Data;` at the top (Hard Rule #5).
2. Open a fresh chart on the current front-month MNQ contract (check `bridge/config/contract.json` for the exact symbol — do not hardcode)
3. Right-click chart → **Indicators** → **PhoenixL2Probe** → Apply
4. Open the **NinjaScript Output Window**: NT8 menu → New → NinjaScript Output

You should see immediately:
```
==================================================
PhoenixL2Probe START on MNQ <month> <year>
Started at HH:mm:ss
Stats print every 60 seconds. Run for 1 hour during RTH.
==================================================
```

If you DON'T see this within ~5 seconds of attaching, the indicator didn't load. Check the **Log** tab in the Control Center for errors.

### Step 4: Run for 1 hour during RTH

US RTH is 8:30 AM - 3:15 PM CST for index futures. Run for at least 60 minutes during active hours. Best times: 9:30-10:30 AM (open volatility) or 1:00-2:00 PM (post-lunch active).

**Do not run during overnight/Globex** — book behavior is different, won't represent the regime we'll trade in.

You'll see stats print every minute. After 60 minutes, compare the final numbers to the acceptance criteria above.

### Step 5: Interpret the results

Take a screenshot of the final stats and save it to `docs/obi_feature/phase_0a_results_<date>.png` (so it's preserved with the docs).

**All 5 criteria pass:**
✅ GO. Proceed to Phase 0b (deploy the L2 recorder for historical data banking). The feature is buildable.

**1-2 criteria fail:**
⚠️ Likely a subscription issue. Call your broker:
- Ask: "Does my current data subscription include streaming Level 2 / Market Depth on micros?"
- Ask: "What does it cost to upgrade to streaming L2?"
- Typical retail cost: $10-50/month
- After upgrade, re-run Phase 0a from Step 4.

**3+ criteria fail (or no L2 data at all):**
❌ STOP. The feature isn't viable on this data feed at reasonable cost. Three options:
1. Switch data provider (CQG or Rithmic both have solid streaming L2 — ~$100/mo total cost for retail)
2. Abandon OBI and pursue other features instead
3. Park OBI until data infrastructure changes for other reasons

Update `DECISIONS.md` status log with the outcome regardless of which path.

### Step 6: Clean up

After confirming results:
- The probe indicator can be left on a chart (it's harmless) or removed
- Do NOT compile it out of NT8 — we may want to re-run later
- Note: this probe maintains book state in memory; it will use a few MB of RAM during runtime. Fine to ignore.

## Common pitfalls

| Symptom | Likely cause | Fix |
|---|---|---|
| OnMarketDepth not firing at all | No L2 subscription, or chart not connected | Check Control Center → Connections → confirm green status |
| "OnMarketDepth not subscribed" error in Log | Feed doesn't include L2 | This IS the Phase 0a failure mode — call broker |
| 200ms-style chunky updates | Aggregated feed (Phase 0a likely fails on events/sec) | Need streaming-L2 subscription tier |
| Probe stops firing when chart minimized | `IsSuspendedWhileInactive` not set to false | Already set in the code above — but check you copied it correctly |
| Compile error: BarsPeriodType undefined | Missing `using NinjaTrader.Data;` | Already in the code above — confirm copy is complete |

## Time budget

- Setup: 15 min (copy code, compile, attach to chart)
- Observation: 60 min (passive — let it run during normal market hours)
- Review + decision: 15 min

**Total: ~1.5 hours, mostly passive observation.**

This is the smallest possible commitment to find out whether OBI is buildable on your stack.
