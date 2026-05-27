<#
.SYNOPSIS
    Registers the PhoenixNT8Recovery scheduled task — runs
    tools/nt8_silent_stall_recovery.py continuously, detecting NT8
    "live but zero ticks" silent stalls and (if PHOENIX_NT8_AUTO_RECOVERY=1)
    auto-recovering by killing + relaunching NinjaTrader.exe.

.DESCRIPTION
    Trigger: AtLogOn for the trading user (NT8 needs an interactive
    session, same convention as PhoenixWatcher).

    Behavior:
      - Polls http://127.0.0.1:8767/health every 30s.
      - When nt8_status==live AND tick_rate_10s==0 for >180s:
          1. Stop-Process -Name NinjaTrader -Force
          2. Wait 30s
          3. Start-Process PhoenixStart.bat
          4. Wait 60s
          5. POST {"type":"halt_new_entries","duration_s":60} to
             dashboard /api/commands
      - Backoff: 5 min after a recovery before another can fire.
      - SAFETY: takes no action unless env PHOENIX_NT8_AUTO_RECOVERY=1
        is set. Without the flag it logs "WOULD RESTART NT8" only.

    The recovery is OUTSIDE the bot process by design — if Python is
    the thing that hung, an in-process recovery would never run. All
    NT8 process operations go through PowerShell subprocess calls so
    they work even when the bot is deaf.

    Auto-restart: if the Python daemon crashes, the task restarts
    after 1 minute, up to 999 times. ExecutionTimeLimit is unlimited.

.NOTES
    Requires admin to register. Re-run after changing the daemon
    location or the Python path.

    To actually enable auto-recovery in production:
      [System.Environment]::SetEnvironmentVariable(
          'PHOENIX_NT8_AUTO_RECOVERY', '1', 'User')
    Then restart the task: schtasks /End /TN PhoenixNT8Recovery
                           schtasks /Run /TN PhoenixNT8Recovery

    Without that env var the daemon detects + logs + telegrams only.
    The operator must explicitly opt in.

    Verify after install:
        Get-ScheduledTask -TaskName PhoenixNT8Recovery
        schtasks /Run /TN PhoenixNT8Recovery
        Get-Process python | ? { $_.CommandLine -match 'nt8_silent_stall' }
        Get-Content C:\Trading` Project\phoenix_bot\logs\nt8_silent_stall_recovery.log -Tail 20

    Smoke test the trigger without acting:
        python tools/nt8_silent_stall_recovery.py --simulate-stall --dry-run

    Disable / remove:
        schtasks /Change /TN PhoenixNT8Recovery /DISABLE
        schtasks /Delete /TN PhoenixNT8Recovery /F
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixNT8Recovery",
    [string]$ProjectRoot = "C:\Trading Project\phoenix_bot",
    [string]$TaskUser = "TradingPC\Trading PC"
)

$ErrorActionPreference = "Stop"

if (-not [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

# Robust python resolver — same pattern as register_watcher_task.ps1.
$pyExe = $null
$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
    "C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe",
    "C:\Windows\py.exe",
    "C:\Program Files\Python314\python.exe",
    "C:\Program Files\Python312\python.exe"
)
foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { $pyExe = $c; break }
}
if (-not $pyExe) {
    $cmd = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($cmd -and $cmd -notmatch 'WindowsApps') { $pyExe = $cmd }
}
if (-not $pyExe) {
    Write-Error "python.exe not found."
    exit 1
}
Write-Host "Using python: $pyExe"

$scriptPath = Join-Path $ProjectRoot "tools\nt8_silent_stall_recovery.py"
if (-not (Test-Path $scriptPath)) {
    Write-Error "nt8_silent_stall_recovery.py not found at $scriptPath"
    exit 1
}

# Replace existing
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

# Action: run the daemon in continuous mode (no flags = production loop).
# The PHOENIX_NT8_AUTO_RECOVERY=1 env var must be set OUT-OF-BAND
# (User scope) for the daemon to actually act — see .NOTES above.
$action = New-ScheduledTaskAction `
    -Execute $pyExe `
    -Argument "`"$scriptPath`"" `
    -WorkingDirectory $ProjectRoot

# Trigger: at user logon. NT8 needs an interactive session for its
# data feed; this daemon needs the same context so PowerShell
# Stop-Process can see the NT8 process started by that session.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser

# Settings:
#   - Auto-restart on failure (up to 999 times, 1 min apart) — matches
#     the watcher_agent pattern; daemon must never stay down.
#   - ExecutionTimeLimit ZERO = unlimited (it's a daemon).
#   - MultipleInstances IgnoreNew: don't spawn a 2nd recovery daemon
#     if one is alive (otherwise two daemons could double-fire the
#     kill-and-relaunch sequence).
#   - StartWhenAvailable: catch up if the logon was missed.
#   - Battery flags: keep monitoring on laptop too.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId $TaskUser `
    -LogonType Interactive `
    -RunLevel Highest

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName'. Will run at every user logon and stay alive as a daemon."
Write-Host ""
Write-Host "IMPORTANT — auto-recovery is OFF by default. To enable:"
Write-Host "    [System.Environment]::SetEnvironmentVariable('PHOENIX_NT8_AUTO_RECOVERY','1','User')"
Write-Host "    schtasks /End /TN $TaskName"
Write-Host "    schtasks /Run /TN $TaskName"
Write-Host ""
Write-Host "Without that env var the daemon DETECTS + LOGS + TELEGRAMS only — no NT8 restart."
Write-Host ""
Write-Host "  Start now (don't wait for next logon):  schtasks /Run /TN $TaskName"
Write-Host "  Verify it's running:                    Get-Process python | ? { `$_.CommandLine -match 'nt8_silent_stall' }"
Write-Host "  Tail the log:                           Get-Content '$ProjectRoot\logs\nt8_silent_stall_recovery.log' -Tail 20"
Write-Host "  Disable:                                schtasks /Change /TN $TaskName /DISABLE"
Write-Host "  Remove entirely:                        schtasks /Delete /TN $TaskName /F"
