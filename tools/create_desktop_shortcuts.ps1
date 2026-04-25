<#
.SYNOPSIS
    Creates desktop shortcuts for KillSwitch and PhoenixStart.

.DESCRIPTION
    Produces two .lnk files on the current user's desktop:
      * "Phoenix KillSwitch" -> KillSwitch.bat   (with a stop-sign icon)
      * "Phoenix Start"      -> PhoenixStart.bat (with a green-arrow icon)

    Re-running overwrites existing shortcuts.

.NOTES
    No admin required. Run whenever launch_watcher.bat moves.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = "C:\Trading Project\phoenix_bot"
)

$ErrorActionPreference = "Stop"
$desktop = [Environment]::GetFolderPath("Desktop")
$wsh = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param($Name, $Target, $IconResource, $Description)
    $lnkPath = Join-Path $desktop "$Name.lnk"
    $sc = $wsh.CreateShortcut($lnkPath)
    $sc.TargetPath = $Target
    $sc.WorkingDirectory = (Split-Path $Target -Parent)
    $sc.Description = $Description
    if ($IconResource) { $sc.IconLocation = $IconResource }
    $sc.Save()
    Write-Host "  created: $lnkPath -> $Target"
}

$killBat = Join-Path $ProjectRoot "KillSwitch.bat"
$startBat = Join-Path $ProjectRoot "PhoenixStart.bat"

if (-not (Test-Path $killBat))  { Write-Error "Missing: $killBat";  exit 1 }
if (-not (Test-Path $startBat)) { Write-Error "Missing: $startBat"; exit 1 }

Write-Host "Creating desktop shortcuts on $desktop ..."
# shell32.dll icon indices:
#   131 = circle with slash (stop sign)
#   239 = green circle with arrow (play)
New-Shortcut -Name "Phoenix KillSwitch" -Target $killBat `
    -IconResource "shell32.dll,131" `
    -Description "Stop PhoenixWatcher task and kill all Phoenix processes"
New-Shortcut -Name "Phoenix Start"      -Target $startBat `
    -IconResource "shell32.dll,239" `
    -Description "Clear KillSwitch and restart Phoenix stack"

Write-Host "Done."
