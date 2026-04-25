<#
.SYNOPSIS
    Registers the PhoenixWeeklyEvolution scheduled task (Sunday 18:00 CT).
.DESCRIPTION
    Runs weekly_evolution.py — aggregates the week's grades, drafts
    adaptive-params proposals with Claude review, auto-creates a git
    branch (NEVER auto-pushes, NEVER auto-merges) with a validation-
    checkbox commit body, fires a Sunday-evening Telegram alert.
.NOTES
    Requires admin. Re-run any time the python path changes.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixWeeklyEvolution",
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

$scriptPath = Join-Path $ProjectRoot "tools\routines\weekly_evolution.py"
if (-not (Test-Path $scriptPath)) { Write-Error "weekly_evolution.py not found"; exit 1 }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $pyExe -Argument "`"$scriptPath`"" -WorkingDirectory $ProjectRoot

# Sunday 18:00 CT only
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 6:00pm

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. '$TaskName' will run every Sunday at 18:00 CT."
