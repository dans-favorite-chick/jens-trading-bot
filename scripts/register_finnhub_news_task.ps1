<#
.SYNOPSIS
    Registers the PhoenixFinnhubNews scheduled task — runs the Finnhub
    news feed continuously, persisting events to logs/finnhub_news.jsonl.
.DESCRIPTION
    Trigger: AtLogOn (daemon). Finnhub uses a hybrid WebSocket + REST
    feed; the WS connection wants to stay open continuously to receive
    push events. Restart-on-failure ensures it stays alive across
    transient network blips or Finnhub-side disconnects.

    Default invocation has no flags — auto-detects WS vs REST, polls
    REST every 60s when in REST-only fallback. The script reads
    FINNHUB_API_KEY from .env and exits non-zero if missing.

    Output:
      logs/finnhub_news.jsonl  — one JSON event per line (consumers:
                                   sentiment_flow_agent, council_gate)
      logs/finnhub_news.log    — runtime log

.NOTES
    Requires admin. Re-run any time the python path changes.

    Verify after install:
        Get-ScheduledTask -TaskName PhoenixFinnhubNews
        schtasks /Run /TN PhoenixFinnhubNews
        Get-Process python | ? { $_.CommandLine -match 'finnhub_news_runner' }
        Get-Content logs\finnhub_news.log -Tail 20
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PhoenixFinnhubNews",
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

$scriptPath = Join-Path $ProjectRoot "tools\finnhub_news_runner.py"
if (-not (Test-Path $scriptPath)) {
    Write-Error "finnhub_news_runner.py not found at $scriptPath"
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing task found -- replacing..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $pyExe -Argument "`"$scriptPath`"" -WorkingDirectory $ProjectRoot

# Daemon — fire at user logon, stay alive forever.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Registered. '$TaskName' will run at every user logon (daemon, auto-restart on failure)."
Write-Host "  Start now:        schtasks /Run /TN $TaskName"
Write-Host "  Tail logs:        Get-Content logs\finnhub_news.log -Tail 20 -Wait"
Write-Host "  Disable:          schtasks /Change /TN $TaskName /DISABLE"
Write-Host "  Remove entirely:  schtasks /Delete /TN $TaskName /F"
