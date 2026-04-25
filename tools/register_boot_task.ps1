<#
.SYNOPSIS
    Registers the PhoenixBoot Windows scheduled task.

.DESCRIPTION
    Creates (or replaces) a scheduled task named "PhoenixBoot" that
    brings up the ENTIRE Phoenix trading stack at system startup and
    at user logon by invoking PhoenixStart.bat:
      1. Clears any lingering KillSwitch marker
      2. Enables and triggers the PhoenixWatcher task
      3. Runs launch_all.bat (bridge + dashboard + watchdog)
      4. Watchdog then spawns sim_bot + prod_bot

    This is the "machine rebooted overnight, come back up automatically"
    task. PhoenixWatcher is a separate task (registered via
    register_watcher_task.ps1) that only runs the WatcherAgent daemon.

    KillSwitch.bat stops this task via 'schtasks /Change /DISABLE'.
    PhoenixStart.bat re-enables it.

.NOTES
    Requires admin privileges. Run ONCE after registering PhoenixWatcher.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixBoot",
    [string]$ProjectRoot = "C:\Trading Project\phoenix_bot"
)

$ErrorActionPreference = "Stop"

if (-not [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$batPath = Join-Path $ProjectRoot "PhoenixStart.bat"
if (-not (Test-Path $batPath)) {
    Write-Error "PhoenixStart.bat not found at $batPath"
    exit 1
}

Write-Host "Registering scheduled task '$TaskName' -> $batPath"

# Replace existing task if present (use native PowerShell cmdlets, not schtasks,
# to avoid PowerShell 7's treatment of native-command stderr as terminating)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

# Run PhoenixStart.bat via cmd /c -- this is intentional here because
# PhoenixStart.bat is a one-shot bootstrap (not a long-lived process like
# the watcher). cmd /c exits cleanly once the batch completes, and Task
# Scheduler's "Last Run Result" will correctly show 0 on success.
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`"" -WorkingDirectory $ProjectRoot

$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. Task '$TaskName' is ENABLED and will run at next boot/logon."
Write-Host "This runs PhoenixStart.bat which starts the whole stack."
Write-Host ""
Write-Host "To run immediately:      schtasks /Run /TN $TaskName"
Write-Host "To disable (KillSwitch): schtasks /Change /TN $TaskName /DISABLE"
Write-Host "To remove entirely:      schtasks /Delete /TN $TaskName /F"
