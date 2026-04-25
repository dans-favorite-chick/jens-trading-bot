<#
.SYNOPSIS
    Registers the PhoenixFredMacros scheduled task — polls FRED macro
    series (FFR, CPI, UNRATE, T10Y2Y) every 60 minutes and fires
    Telegram alerts on any detected regime shift.
.DESCRIPTION
    Trigger: AtLogOn (daemon). The script's --interval-min 60 flag
    keeps an internal sleep loop running between polls. Restart-on-
    failure handles transient FRED API blips.

    fred_poll.py records each snapshot to RegimeHistory and emits a
    Telegram FRED_REGIME_SHIFT alert when any series moves outside
    its rolling band. Cadence is hourly (FRED data updates daily
    or weekly anyway — anything finer is wasted API quota).

    Output:
      logs/fred_macros.log         — runtime log
      data/macros/regime_history/  — snapshot history (RegimeHistory)

.NOTES
    Requires admin. FRED_API_KEY must be set in .env.

    Verify after install:
        Get-ScheduledTask -TaskName PhoenixFredMacros
        schtasks /Run /TN PhoenixFredMacros
        Get-Process python | ? { $_.CommandLine -match 'fred_poll' }
        Get-Content logs\fred_macros.log -Tail 20
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixFredMacros",
    [string]$ProjectRoot = "C:\Trading Project\phoenix_bot",
    [int]$IntervalMin = 60,
    [string]$TaskUser = "TradingPC\Trading PC"
)

$ErrorActionPreference = "Stop"

if (-not [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$pyExe = $null
$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
    "C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe",
    "C:\Windows\py.exe",
    "C:\Program Files\Python314\python.exe",
    "C:\Program Files\Python312\python.exe"
)
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $pyExe = $c; break } }
if (-not $pyExe) {
    $cmd = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($cmd -and $cmd -notmatch 'WindowsApps') { $pyExe = $cmd }
}
if (-not $pyExe) { Write-Error "python.exe not found."; exit 1 }
Write-Host "Using python: $pyExe"

$scriptPath = Join-Path $ProjectRoot "tools\fred_poll.py"
if (-not (Test-Path $scriptPath)) {
    Write-Error "fred_poll.py not found at $scriptPath"
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

# --interval-min runs the script's internal sleep loop between polls,
# matching the daemon model used by watcher_agent and finnhub_news_runner.
$action = New-ScheduledTaskAction `
    -Execute $pyExe `
    -Argument "`"$scriptPath`" --interval-min $IntervalMin" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser

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

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. '$TaskName' will poll FRED every $IntervalMin minutes (daemon, auto-restart)."
Write-Host "  Start now:        schtasks /Run /TN $TaskName"
Write-Host "  Tail logs:        Get-Content logs\fred_macros.log -Tail 20 -Wait"
Write-Host "  Disable:          schtasks /Change /TN $TaskName /DISABLE"
Write-Host "  Remove entirely:  schtasks /Delete /TN $TaskName /F"
