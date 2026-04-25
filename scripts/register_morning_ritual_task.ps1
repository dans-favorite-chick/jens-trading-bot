<#
.SYNOPSIS
    Registers the PhoenixMorningRitual scheduled task (06:30 CT Mon-Fri).
.DESCRIPTION
    Runs tools\routines\morning_ritual.py via the resolved python.exe.
    Verdict is deterministic; only RED verdicts fire an interrupting
    Telegram. Other reports go to out/morning_ritual/ and are folded
    into the post-session-debrief consolidated digest at 16:05.
.NOTES
    Requires admin. Re-run any time the python path changes.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixMorningRitual",
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

# Robust python resolver — same pattern as register_watcher_task.ps1 + register_phoenix_grading_task.ps1.
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

$scriptPath = Join-Path $ProjectRoot "tools\routines\morning_ritual.py"
if (-not (Test-Path $scriptPath)) {
    Write-Error "morning_ritual.py not found at $scriptPath"
    exit 1
}

# Replace existing
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $pyExe -Argument "`"$scriptPath`"" -WorkingDirectory $ProjectRoot

# Weekly trigger Mon-Fri at 06:30 CT. PowerShell's -Daily trigger does
# NOT expose a DaysOfWeek property; only -Weekly does. Listing all five
# weekdays produces a single trigger that fires Mon-Fri only.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 6:30am

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $TaskUser `
    -LogonType Interactive `
    -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. '$TaskName' will run at 06:30 CT every Mon-Fri."
Write-Host "  Run now:   schtasks /Run /TN $TaskName"
Write-Host "  Disable:   schtasks /Change /TN $TaskName /DISABLE"
Write-Host "  Remove:    schtasks /Delete /TN $TaskName /F"
