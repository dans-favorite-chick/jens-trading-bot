<#
.SYNOPSIS
    Registers the PhoenixGrading Windows scheduled task.

.DESCRIPTION
    Runs tools/grade_open_predictions.py at 16:00 CT, Mon-Fri.
    Emits JSON+MD+HTML report into out/grades/ and a one-line summary
    appended to logs/grading_summary.log. Best-effort Windows toast.

    Exit codes propagate to "Last Run Result" in Task Scheduler:
      0 = all predictions pass
      1 = at least one fails
      2 = grader / parser error

.NOTES
    Requires admin privileges. Re-run any time grade_open_predictions.py moves.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixGrading",
    [string]$ProjectRoot = "C:\Trading Project\phoenix_bot"
)

$ErrorActionPreference = "Stop"

if (-not [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

# Python resolution — try several locations and reject the Microsoft
# Store App Execution Alias shim. Order:
#   1. $env:LOCALAPPDATA (current user's install)
#   2. Trading PC user's known install (the one the bots use today)
#   3. py.exe launcher (system-wide, ships with the official installer)
#   4. Get-Command python.exe — but skip if it points at WindowsApps
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
    # Reject the Microsoft Store App Execution Alias shim
    if ($cmd -and $cmd -notmatch 'WindowsApps') { $pyExe = $cmd }
}
if (-not $pyExe) {
    Write-Error "python.exe not found. Install Python 3.12+ or disable the Microsoft Store App Execution Alias for python.exe (Settings -> Apps -> Advanced app settings -> App execution aliases)."
    exit 1
}
Write-Host "Using python: $pyExe"
$script = Join-Path $ProjectRoot "tools\grade_open_predictions.py"
$logPath = Join-Path $ProjectRoot "logs\sim_bot_stdout.log"
if (-not (Test-Path $script)) { Write-Error "grade_open_predictions.py not found at $script"; exit 1 }

Write-Host "Registering scheduled task '$TaskName' -> $script"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$argString = ('"{0}" --log "{1}" --session-date today --emit-json --emit-md --emit-html --notify' -f $script, $logPath)

$action = New-ScheduledTaskAction -Execute $pyExe -Argument $argString -WorkingDirectory $ProjectRoot

# Trigger: 16:00 CT (= 16:00 local on a Chicago-time machine) Mon-Fri.
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "16:00:00"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. Task '$TaskName' will run at 16:00 local Mon-Fri."
Write-Host ""
Write-Host "To run immediately:      schtasks /Run /TN $TaskName"
Write-Host "To disable:              schtasks /Change /TN $TaskName /DISABLE"
Write-Host "To remove entirely:      schtasks /Delete /TN $TaskName /F"
