<#
.SYNOPSIS
    Registers the PhoenixPostSessionDebrief scheduled task (16:05 CT Mon-Fri).
.DESCRIPTION
    Chains 5 minutes after PhoenixGrading (16:00 CT). Reads today's grade,
    computes risk metrics, scans logs for new error signatures, runs AI
    debrief, assembles PDF, drains the DigestQueue and sends ONE
    consolidated Telegram (folds in today's morning_ritual + any
    system-down events).
.NOTES
    Requires admin. Re-run any time the python path changes.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixPostSessionDebrief",
    [string]$ProjectRoot = "C:\Trading Project\phoenix_bot"
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

$scriptPath = Join-Path $ProjectRoot "tools\routines\post_session_debrief.py"
if (-not (Test-Path $scriptPath)) { Write-Error "post_session_debrief.py not found"; exit 1 }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $pyExe -Argument "`"$scriptPath`"" -WorkingDirectory $ProjectRoot
# Weekly trigger Mon-Fri at 16:05 CT (5 min after PhoenixGrading at 16:00).
# PowerShell's -Daily trigger does NOT expose a DaysOfWeek property; only
# -Weekly does. Listing all five weekdays produces a Mon-Fri trigger.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 4:05pm

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. '$TaskName' will run at 16:05 CT every Mon-Fri (5 min after PhoenixGrading)."
