<#
.SYNOPSIS
    Registers the PhoenixWatcher scheduled task — runs WatcherAgent
    continuously, escalating critical events to Twilio SMS + Telegram.
.DESCRIPTION
    Trigger: AtLogOn (fires when current user logs in). NT8 requires an
    interactive session for its data feed, so the bot stack runs under a
    logged-on user; PhoenixWatcher follows the same convention so it has
    the same env (.env keys, paths, etc.) the bots have.

    The task runs `python tools/watcher_agent.py` with no flags — that's
    the continuous loop mode (NOT --once, NOT --dry-run). The watcher
    does spot checks every ~10s and deep checks every ~5min; on RED_ALERT
    findings (3-strike restart failure, NT8 SILENT_STALL, price_sanity
    fmp_primary persistence >10min, etc.) it pages via Twilio SMS to
    TWILIO_TO_NUMBER and Telegram to TELEGRAM_CHAT_ID.

    Auto-restart: if the Python process crashes, the task restarts after
    1 minute, up to 999 times. ExecutionTimeLimit is unlimited (the
    watcher is a daemon; it should never time out).

.NOTES
    Requires admin. Re-run any time the python path changes or the
    register script itself is updated.

    Verify after install:
        Get-ScheduledTask -TaskName PhoenixWatcher
        schtasks /Run /TN PhoenixWatcher    # start it now (don't wait for next logon)
        Get-Process python | Where-Object { $_.CommandLine -match 'watcher_agent' }

    Run a one-time SMS test (manual, NOT registered as a task):
        python tools/watcher_agent.py --once
        # — or, to test SMS path without firing real findings —
        python -c "from tools.watcher_agent import Alerter; Alerter().sms('Phoenix watcher SMS test 2026-04-25')"
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixWatcher",
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

# Robust python resolver — same pattern as the other register_*.ps1 scripts.
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

$scriptPath = Join-Path $ProjectRoot "tools\watcher_agent.py"
if (-not (Test-Path $scriptPath)) {
    Write-Error "watcher_agent.py not found at $scriptPath"
    exit 1
}

# Replace existing
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

# Action: run watcher_agent.py in continuous mode (no flags = daemon loop)
$action = New-ScheduledTaskAction -Execute $pyExe -Argument "`"$scriptPath`"" -WorkingDirectory $ProjectRoot

# Trigger: at user logon. NT8 needs interactive session for its data feed,
# so the bot stack runs under the logged-on user; watcher follows.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser

# Settings:
#   - Auto-restart on failure (up to 999 times, 1 min apart)
#   - ExecutionTimeLimit ZERO = unlimited (watcher is a daemon)
#   - MultipleInstances IgnoreNew: don't spawn a 2nd watcher if one is alive
#   - StartWhenAvailable: catch up if logon was missed
#   - Battery flags: keep monitoring on laptop too
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

Write-Host "Registered. '$TaskName' will run at every user logon and stay alive as a daemon."
Write-Host "  Start now (don't wait for next logon):  schtasks /Run /TN $TaskName"
Write-Host "  Verify it's running:                    Get-Process python | ? { `$_.CommandLine -match 'watcher_agent' }"
Write-Host "  Disable:                                schtasks /Change /TN $TaskName /DISABLE"
Write-Host "  Remove entirely:                        schtasks /Delete /TN $TaskName /F"
