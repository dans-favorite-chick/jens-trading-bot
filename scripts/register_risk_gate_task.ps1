<#
.SYNOPSIS
    Registers the PhoenixRiskGate + PhoenixRiskWatchdog scheduled tasks.

.DESCRIPTION
    Creates (or replaces) two Windows scheduled tasks:
      - PhoenixRiskGate     -> python.exe tools\risk_gate_runner.py
      - PhoenixRiskWatchdog -> python.exe tools\watchdog_runner.py

    Both tasks:
      - Trigger at AtStartup AND AtLogOn
      - Run python.exe directly (NOT via cmd /c — same lesson as the
        watcher task: cmd /c spawns a child without an attached console
        on some hosts and the python child exits on first blocking I/O)
      - Restart on failure (5x with 1-min interval)
      - Highest privileges so file ops + named-pipe creation work

    KillSwitch.bat uses 'schtasks /Change /DISABLE' to stop these tasks
    without removing the registration. PhoenixStart.bat re-enables them.

.NOTES
    Requires admin privileges. Re-run any time risk_gate_runner.py or
    watchdog_runner.py move or python interpreter location changes.
#>
[CmdletBinding()]
param(
    [string]$GateTaskName = "PhoenixRiskGate",
    [string]$WatchdogTaskName = "PhoenixRiskWatchdog",
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

$gateScript = Join-Path $ProjectRoot "tools\risk_gate_runner.py"
$watchdogScript = Join-Path $ProjectRoot "tools\watchdog_runner.py"
if (-not (Test-Path $gateScript)) {
    Write-Error "risk_gate_runner.py not found at $gateScript"
    exit 1
}
if (-not (Test-Path $watchdogScript)) {
    Write-Error "watchdog_runner.py not found at $watchdogScript"
    exit 1
}

# Python resolution -- robust across user shells (admin / non-admin /
# different user accounts). The Microsoft Store App Execution Alias shim
# at C:\Users\<u>\AppData\Local\Microsoft\WindowsApps\python.exe is
# REJECTED because invoking it does nothing useful -- it just opens the
# Store. Same logic as tools\register_watcher_task.ps1.
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
    Write-Error "python.exe not found. Install Python or disable the Microsoft Store App Execution Alias (Settings -> Apps -> Advanced app settings -> App execution aliases -> turn OFF python.exe)."
    exit 1
}
Write-Host "Using python: $pyExe"

# Shared trigger/settings/principal definitions used by both tasks.
$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $TaskUser `
    -LogonType Interactive `
    -RunLevel Highest

function Register-PhoenixTask {
    param(
        [string]$TaskName,
        [string]$ScriptPath,
        [string]$Description
    )

    Write-Host ""
    Write-Host "Registering scheduled task '$TaskName' -> $ScriptPath"

    # Use Get-ScheduledTask (pure PowerShell) to detect existing tasks --
    # schtasks /Query writes to stderr when the task doesn't exist, which
    # PowerShell 7 with $ErrorActionPreference=Stop treats as terminating.
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  existing task found -- replacing..."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }

    $action = New-ScheduledTaskAction `
        -Execute $pyExe `
        -Argument "`"$ScriptPath`"" `
        -WorkingDirectory $ProjectRoot

    $task = New-ScheduledTask `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description $Description

    Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

    Write-Host "  Registered. Task '$TaskName' is ENABLED and will run at next boot/logon."
}

Register-PhoenixTask `
    -TaskName $GateTaskName `
    -ScriptPath $gateScript `
    -Description "Phoenix RiskGate -- central fail-closed OIF check chain. Runs the named-pipe server at \\.\pipe\phoenix_risk_gate."

Register-PhoenixTask `
    -TaskName $WatchdogTaskName `
    -ScriptPath $watchdogScript `
    -Description "Phoenix RiskGate Watchdog -- monitors gate heartbeat every 500ms; fires kill-switch on staleness."

Write-Host ""
Write-Host "Both tasks registered. Useful commands:"
Write-Host "  Run gate now:        schtasks /Run /TN $GateTaskName"
Write-Host "  Run watchdog now:    schtasks /Run /TN $WatchdogTaskName"
Write-Host "  Disable (KillSwitch): schtasks /Change /TN $GateTaskName /DISABLE"
Write-Host "                       schtasks /Change /TN $WatchdogTaskName /DISABLE"
Write-Host "  Remove entirely:     schtasks /Delete /TN $GateTaskName /F"
Write-Host "                       schtasks /Delete /TN $WatchdogTaskName /F"
