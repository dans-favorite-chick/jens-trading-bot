<#
.SYNOPSIS
    Registers/replaces the PhoenixBoot scheduled task — runs PhoenixStart.bat
    on system boot, which orchestrates the entire Phoenix stack startup.
.DESCRIPTION
    PhoenixBoot fires AT BOOT (MSFT_TaskBootTrigger), runs
    cmd.exe /c PhoenixStart.bat, which:
      1. Clears any KillSwitch marker
      2. ENABLEs every Phoenix scheduled task
      3. Runs the daemons (Watcher, RiskGate, RiskWatchdog, FinnhubNews,
         FredMacros) one-shot to bring them up immediately
      4. Calls launch_all.bat to start bridge + dashboard + watchdog + bots

    Without this task, the entire Phoenix stack stays down across reboots
    until an operator manually launches launch_all.bat.

    Why this script exists: PhoenixBoot was originally registered with
    Principal=dbren (the elevation admin), but dbren is never the
    interactive console user. The task fired at boot but couldn't run
    PhoenixStart.bat because of the user-context mismatch. Re-registering
    with Principal=Trading PC fixes it.

.NOTES
    Requires admin. PhoenixStart.bat must exist at the project root.
    Re-run if PhoenixStart.bat moves or the trading user changes.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixBoot",
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

$startBat = Join-Path $ProjectRoot "PhoenixStart.bat"
if (-not (Test-Path $startBat)) {
    Write-Error "PhoenixStart.bat not found at $startBat"
    exit 1
}

# Replace existing
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

# Action: cmd.exe /c PhoenixStart.bat (matches the original PhoenixBoot
# definition Jennifer set up earlier)
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$startBat`"" `
    -WorkingDirectory $ProjectRoot

# Trigger: AT BOOT (system startup, fires before any user logs in)
$trigger = New-ScheduledTaskTrigger -AtStartup

# Settings:
#   - StartWhenAvailable: catch up if boot was missed
#   - ExecutionTimeLimit Zero: PhoenixStart.bat may run a long time
#     (calls launch_all.bat which keeps cmd windows open)
#   - MultipleInstances IgnoreNew: don't fire twice if boot triggered twice
#   - 30 sec delay so other system services come up first
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$trigger.Delay = "PT30S"

# Principal: run under Trading PC (the daily console user, NOT dbren the
# elevation admin). RunLevel Highest so it can call schtasks /Run for
# admin-owned tasks. -LogonType S4U lets the task fire at boot before
# any user logs in interactively.
$principal = New-ScheduledTaskPrincipal `
    -UserId $TaskUser `
    -LogonType S4U `
    -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. '$TaskName' will fire at every system boot under $TaskUser."
Write-Host "  Run now (test):    schtasks /Run /TN $TaskName"
Write-Host "  Disable:           schtasks /Change /TN $TaskName /DISABLE"
Write-Host "  Remove entirely:   schtasks /Delete /TN $TaskName /F"
