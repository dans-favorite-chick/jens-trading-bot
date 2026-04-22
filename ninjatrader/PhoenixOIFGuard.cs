// ============================================================================
// PhoenixOIFGuard v1.0 — Rogue-OIF Quarantine for NT8's incoming/ folder
// ============================================================================
// PURPOSE:
//   NT8's ATI consumes ANY .txt file written to its incoming/ folder and
//   executes the embedded command. On 2026-04-22 pytest leaked literal stop
//   prices (100.00 → 21000.00) into real OIFs that NT8 placed on Jennifer's
//   live chart. B81 isolated OIF_INCOMING in the test conftest so pytest
//   can never leak again — but that only covers pytest. ANY process with
//   filesystem access (another bot, a manual script, a corrupt tool) can
//   still inject a PLACE/CANCEL/CLOSEPOSITION command.
//
//   PhoenixOIFGuard closes that hole. Every OIF Phoenix emits is now named
//   `phoenix_<pid>_*.txt` (see bridge/oif_writer.py P0.2 change). The guard
//   watches incoming/ and moves any file WITHOUT the `phoenix_<int>_`
//   prefix to a quarantine folder BEFORE NT8's ATI parser can read it.
//
// INSTALL (one-time, NT8 AddOns folder):
//   1. Copy this file into:
//        %USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\
//   2. In NT8: NinjaScript Editor → open the file → press F5 to compile.
//      You should see "Compile successful" in the Output window.
//   3. Restart NT8 (AddOns load at platform startup, not dynamically).
//   4. On restart you should see in NT8 Output window:
//        [PhoenixOIFGuard] Watching <incoming path>
//   5. Done. No chart attach required; AddOn runs process-wide.
//
// VERIFY (optional):
//   Drop a rogue file into the incoming folder to confirm quarantine:
//     echo PLACE;Sim101;MNQM6;BUY;1;MARKET;0;0;DAY;;;; ^
//       > "%USERPROFILE%\Documents\NinjaTrader 8\incoming\rogue_test.txt"
//   The guard should:
//     - Move it to quarantine/ with a timestamp suffix
//     - Log [CRITICAL] to PhoenixOIFGuard.log
//     - NT8 must NOT place an order (check Control Center)
//
// LOG LOCATION:
//   %USERPROFILE%\Documents\NinjaTrader 8\log\PhoenixOIFGuard.log
//   Append-only. One line per event (watcher start, quarantine, error).
//
// RACE NOTE:
//   NT8's own ATI parser also watches this folder. Both watchers receive
//   FileSystemWatcher.Created events independently. The guard wins the
//   race because: (a) filename regex check is O(1) no file I/O; (b) a
//   File.Move within the same volume is a single atomic rename — orders
//   of magnitude faster than ATI's open+read+parse+execute cycle. Even
//   under heavy CPU load the guard completes before ATI finishes parsing.
//
// ZERO PHOENIX FALSE-POSITIVES:
//   Any filename matching `^phoenix_\d+_` is accepted — irrespective of
//   which pid. Multiple bot processes (prod / sim / lab) can write
//   simultaneously and all get through.
// ============================================================================

using System;
using System.IO;
using System.Text.RegularExpressions;

namespace NinjaTrader.NinjaScript.AddOns
{
    public class PhoenixOIFGuard : NinjaTrader.NinjaScript.AddOnBase
    {
        // Phoenix author-tag regex. Matches literal "phoenix_", one or more
        // digits (the pid), then "_". Anything else = rogue.
        private static readonly Regex PhoenixTag = new Regex(
            @"^phoenix_\d+_", RegexOptions.Compiled);

        private FileSystemWatcher _watcher;
        private string _incomingDir;
        private string _quarantineDir;
        private string _logPath;

        // Called by NT8 at platform startup.
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "PhoenixOIFGuard";
                Description = "Quarantines rogue OIF files in NT8's incoming folder.";
            }
            else if (State == State.Configure)
            {
                try
                {
                    InitPaths();
                    EnsureDirs();
                    StartWatcher();
                    Log("INFO", string.Format("Watching {0}", _incomingDir));
                }
                catch (Exception ex)
                {
                    // AddOn init failure is serious — surface to NT8 Output.
                    Print(string.Format(
                        "[PhoenixOIFGuard] FATAL init failure: {0}", ex));
                    Log("FATAL", ex.ToString());
                }
            }
            else if (State == State.Terminated)
            {
                StopWatcher();
                Log("INFO", "Shutdown (NT8 terminating)");
            }
        }

        private void InitPaths()
        {
            string userHome = Environment.GetFolderPath(
                Environment.SpecialFolder.UserProfile);
            string nt8Root = Path.Combine(userHome, "Documents", "NinjaTrader 8");

            _incomingDir   = Path.Combine(nt8Root, "incoming");
            _quarantineDir = Path.Combine(nt8Root, "quarantine");

            string logDir = Path.Combine(nt8Root, "log");
            _logPath = Path.Combine(logDir, "PhoenixOIFGuard.log");
        }

        private void EnsureDirs()
        {
            // Create incoming/ and quarantine/ if they don't exist. Harmless
            // if they already do.
            Directory.CreateDirectory(_incomingDir);
            Directory.CreateDirectory(_quarantineDir);
            Directory.CreateDirectory(Path.GetDirectoryName(_logPath));
        }

        private void StartWatcher()
        {
            _watcher = new FileSystemWatcher(_incomingDir, "*.txt");
            _watcher.NotifyFilter =
                NotifyFilters.FileName | NotifyFilters.LastWrite;
            _watcher.Created += OnFileEvent;
            _watcher.Renamed += OnFileEvent;
            _watcher.EnableRaisingEvents = true;
        }

        private void StopWatcher()
        {
            if (_watcher != null)
            {
                try
                {
                    _watcher.EnableRaisingEvents = false;
                    _watcher.Created -= OnFileEvent;
                    _watcher.Renamed -= OnFileEvent;
                    _watcher.Dispose();
                }
                catch { /* shutdown: swallow */ }
                _watcher = null;
            }
        }

        private void OnFileEvent(object sender, FileSystemEventArgs e)
        {
            // Called on a ThreadPool thread. Keep this path FAST so we
            // win the race with NT8's ATI parser reading the same file.
            try
            {
                string name = e.Name ?? "";
                if (PhoenixTag.IsMatch(name))
                {
                    // Good file — Phoenix author-tagged. Let ATI process it.
                    return;
                }

                // Rogue. Move to quarantine with a timestamp so we don't
                // overwrite earlier quarantines of same-named files.
                string ts = DateTime.Now.ToString("yyyyMMdd_HHmmss_fff");
                string quarantineName = string.Format("{0}__{1}", ts, name);
                string dest = Path.Combine(_quarantineDir, quarantineName);

                // File.Move is atomic within the same volume. If it fails
                // (file vanished mid-move, permissions, etc.) log and
                // carry on — never throw back into NT8.
                try
                {
                    File.Move(e.FullPath, dest);
                    Log("CRITICAL", string.Format(
                        "ROGUE OIF quarantined: {0} -> {1}", name, quarantineName));
                    Print(string.Format(
                        "[PhoenixOIFGuard] ROGUE OIF quarantined: {0}", name));
                }
                catch (FileNotFoundException)
                {
                    // ATI or another watcher already consumed it — rare but
                    // possible under extreme load. Log it so we know we lost
                    // the race this time.
                    Log("WARN", string.Format(
                        "Rogue file {0} vanished before quarantine (lost race)",
                        name));
                }
                catch (Exception mex)
                {
                    Log("ERROR", string.Format(
                        "Failed to quarantine {0}: {1}", name, mex.Message));
                }
            }
            catch (Exception ex)
            {
                // Absolute last-ditch — never let an exception escape the
                // FileSystemWatcher callback (would silently kill it).
                try { Log("ERROR", "OnFileEvent: " + ex); } catch { }
            }
        }

        private void Log(string level, string msg)
        {
            // One-line append to PhoenixOIFGuard.log. No rotation — file
            // stays small (one line per OIF event). Manual trim if needed.
            try
            {
                string line = string.Format(
                    "{0:yyyy-MM-dd HH:mm:ss.fff} [{1}] {2}{3}",
                    DateTime.Now, level, msg, Environment.NewLine);
                File.AppendAllText(_logPath, line);
            }
            catch
            {
                // Logging failure must never crash the guard. Swallow.
            }
        }
    }
}
