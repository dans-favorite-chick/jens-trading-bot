<#
.SYNOPSIS
    Registers the PhoenixWatcher Windows scheduled task.

.DESCRIPTION
    Creates (or replaces) a scheduled task named "PhoenixWatcher" that:
      - Triggers at system startup AND at user logon
      - Runs launch_watcher.bat (which invokes tools/watcher_agent.py)
      - Restarts on failure (up to 5 times, 1-min interval)
      - Runs whether the user is logged on or not
      - Runs at HIGHEST privileges so psutil / file ops work unrestricted

    KillSwitch.bat uses 'schtasks /Change /DISABLE' to stop this task
    without removing the registration. PhoenixStart.bat re-enables it.

.NOTES
    Requires admin privileges. Re-run any time launch_watcher.bat moves.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixWatcher",
    [string]$ProjectRoot = "C:\Trading Project\phoenix_bot"
)

$ErrorActionPreference = "Stop"

if (-not [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$batPath = Join-Path $ProjectRoot "launch_watcher.bat"
if (-not (Test-Path $batPath)) {
    Write-Error "launch_watcher.bat not found at $batPath"
    exit 1
}

Write-Host "Registering scheduled task '$TaskName' -> $batPath"

# Unregister existing if present. Use Get-ScheduledTask (pure PowerShell)
# rather than schtasks /Query, because PowerShell 7 with
# $ErrorActionPreference=Stop treats native-command stderr as a terminating
# error, and schtasks writes "ERROR: The system cannot find the file
# specified." to stderr when the task doesn't exist yet.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

# Build triggers + action + settings via New-ScheduledTask*
#
# 2026-04-24: run python.exe directly, NOT via `cmd /c launch_watcher.bat`.
# When Task Scheduler invokes `cmd /c batchfile`, cmd exits as soon as the
# batch file's last command returns, and on some hosts the batch gets
# spawned without an attached console such that the child python process
# inherits an invalid handle and exits on first blocking I/O. Invoking
# python.exe directly keeps the process alive indefinitely and makes the
# task debuggable in Task Scheduler's "Last Run Result" pane.
# Python resolution — robust across user shells (admin / non-admin / different
# user accounts). The Microsoft Store App Execution Alias shim at
# C:\Users\<u>\AppData\Local\Microsoft\WindowsApps\python.exe is REJECTED
# because invoking it does nothing useful — it just opens the Store.
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
$watcherScript = Join-Path $ProjectRoot "tools\watcher_agent.py"
if (-not (Test-Path $watcherScript)) {
    Write-Error "watcher_agent.py not found at $watcherScript"
    exit 1
}
$action = New-ScheduledTaskAction -Execute $pyExe -Argument "`"$watcherScript`"" -WorkingDirectory $ProjectRoot

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

# Use the current user as the principal with highest privileges so psutil +
# file ops work. SYSTEM would also work but would miss user-level env vars.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. Task '$TaskName' is ENABLED and will run at next boot/logon."
Write-Host "To run immediately:      schtasks /Run /TN $TaskName"
Write-Host "To disable (KillSwitch): schtasks /Change /TN $TaskName /DISABLE"
Write-Host "To remove entirely:      schtasks /Delete /TN $TaskName /F"
