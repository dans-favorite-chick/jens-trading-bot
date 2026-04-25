<#
.SYNOPSIS
  Position Phoenix trading-bot cmd windows at pixel-exact coordinates.

.DESCRIPTION
  Works around FancyZones not snapping cmd.exe reliably. Uses Win32
  SetWindowPos directly.

  Two modes:

    .\window_layout.ps1 -Capture    Read each target window's current
                                    position + save to window_layout.json.
                                    Run this ONCE after you've manually
                                    arranged everything the way you want.

    .\window_layout.ps1             Read window_layout.json + move each
                                    matching window to its saved position.
                                    Run this at the end of launch_all.bat.

  Windows are matched by title regex. Each launch_*.bat already sets a
  distinct title via `start "Title"` or `title Title` - no changes needed
  to your launchers, though if two entries collide you can tighten the
  regex below.

.PARAMETER Capture
  Snapshot mode: read + save positions, don't move anything.

.PARAMETER ConfigPath
  Override default path to window_layout.json.

.PARAMETER WaitSeconds
  How long to poll for a not-yet-open window before giving up. Default 5s.

.EXAMPLE
  # First run - arrange windows manually, then:
  powershell -ExecutionPolicy Bypass -File tools\window_layout.ps1 -Capture

.EXAMPLE
  # Every subsequent launch (add this to the end of launch_all.bat):
  powershell -ExecutionPolicy Bypass -File tools\window_layout.ps1
#>
[CmdletBinding()]
param(
    [switch]$Capture,
    [string]$ConfigPath,
    [int]$WaitSeconds = 5
)

# Resolve ConfigPath default in the body, not in param default - Windows
# PowerShell 5.1 sometimes leaves $PSScriptRoot empty during param binding
# depending on how the script is invoked.
if (-not $ConfigPath) {
    $scriptDir = if ($PSScriptRoot) {
        $PSScriptRoot
    } elseif ($MyInvocation.MyCommand.Path) {
        Split-Path -Parent $MyInvocation.MyCommand.Path
    } else {
        (Get-Location).Path
    }
    $ConfigPath = Join-Path $scriptDir 'window_layout.json'
}

# ── Target windows (title regex, anchored where safe) ──
# Order matters only for log readability. Add/remove freely.
$Targets = @(
    @{ Name = 'launcher';  Title = '^Phoenix Trading Bot - Launcher' }
    @{ Name = 'bridge';    Title = '^Phoenix Bridge' }       # matches "Bridge" and "Bridge Server"
    @{ Name = 'dashboard'; Title = '^Phoenix Dashboard' }
    @{ Name = 'watchdog';  Title = '^Phoenix Watchdog' }
    @{ Name = 'prod';      Title = '^Phoenix Prod' }         # matches "Prod" and "Prod Bot"
    @{ Name = 'sim';       Title = '^Phoenix Sim' }
)

# Win32 bindings. RECT is declared as a CLASS (reference type) instead of
# struct so Windows PowerShell 5.1's marshaller correctly populates it
# on return from GetWindowRect. The struct form silently left Left/Top/
# Right/Bottom unpopulated, producing blank x=/y=/w=/h= in the captured
# JSON.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
namespace Win {
    [StructLayout(LayoutKind.Sequential)]
    public class RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    public static class Api {
        [DllImport("user32.dll")]
        public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);
        [DllImport("user32.dll")]
        public static extern int GetWindowTextLength(IntPtr hWnd);
        [DllImport("user32.dll")]
        public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")]
        public static extern bool GetWindowRect(IntPtr hWnd, [In, Out] RECT lpRect);
        [DllImport("user32.dll")]
        public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
            int X, int Y, int cx, int cy, uint uFlags);
        [DllImport("user32.dll")]
        public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    }
}
'@ -ErrorAction SilentlyContinue

$SWP_NOZORDER     = 0x0004
$SWP_NOACTIVATE   = 0x0010
$SWP_FRAMECHANGED = 0x0020
$SW_RESTORE       = 9       # un-minimize first

function Get-AllVisibleWindows {
    $results = New-Object System.Collections.ArrayList
    $cb = [Win.EnumWindowsProc]{
        param($hwnd, $lParam)
        if ([Win.Api]::IsWindowVisible($hwnd)) {
            $len = [Win.Api]::GetWindowTextLength($hwnd)
            if ($len -gt 0) {
                $sb = New-Object System.Text.StringBuilder ($len + 1)
                [Win.Api]::GetWindowText($hwnd, $sb, $sb.Capacity) | Out-Null
                [void]$results.Add([pscustomobject]@{ Hwnd = $hwnd; Title = $sb.ToString() })
            }
        }
        return $true
    }
    [Win.Api]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
    return $results
}

function Find-WindowByTitle([string]$pattern) {
    Get-AllVisibleWindows | Where-Object { $_.Title -match $pattern } | Select-Object -First 1
}

function Get-WindowRectOrNull($hwnd) {
    $r = New-Object Win.RECT
    if (-not [Win.Api]::GetWindowRect($hwnd, $r)) { return $null }
    return [pscustomobject]@{
        X = $r.Left; Y = $r.Top
        W = ($r.Right - $r.Left); H = ($r.Bottom - $r.Top)
    }
}

function Move-Window($hwnd, [int]$x, [int]$y, [int]$w, [int]$h) {
    # Un-minimize first; SetWindowPos alone doesn't restore a minimized window.
    [Win.Api]::ShowWindow($hwnd, $SW_RESTORE) | Out-Null
    $flags = $SWP_NOZORDER -bor $SWP_NOACTIVATE -bor $SWP_FRAMECHANGED
    [Win.Api]::SetWindowPos($hwnd, [IntPtr]::Zero, $x, $y, $w, $h, $flags) | Out-Null
}

# ════════════════════════════════════════════════════════════════════
# CAPTURE MODE
# ════════════════════════════════════════════════════════════════════
if ($Capture) {
    Write-Host ''
    Write-Host "Capturing window positions to: $ConfigPath" -ForegroundColor Cyan
    Write-Host ''

    $layout = [ordered]@{}
    $missing = @()
    foreach ($t in $Targets) {
        $win = Find-WindowByTitle $t.Title
        if (-not $win) {
            # Not an error - just a target that's not open right now.
            # (e.g. launcher exits after spawning, prod/sim may not be
            # started yet.) Collect for a single tidy summary at the end.
            $missing += $t.Name
            continue
        }
        $rect = Get-WindowRectOrNull $win.Hwnd
        if (-not $rect) {
            Write-Warning "[$($t.Name)] '$($win.Title)' found but GetWindowRect failed - SKIPPED"
            continue
        }
        $layout[$t.Name] = [ordered]@{
            X     = $rect.X
            Y     = $rect.Y
            W     = $rect.W
            H     = $rect.H
            Title = $win.Title
        }
        "{0,-10} '{1,-35}' -> x={2,-5} y={3,-5} w={4,-5} h={5,-5}" -f `
            $t.Name, $win.Title, $rect.X, $rect.Y, $rect.W, $rect.H | Write-Host
    }

    if ($layout.Count -eq 0) {
        Write-Error 'No matching windows captured. Make sure your launchers are running.'
        exit 1
    }

    $layout | ConvertTo-Json -Depth 3 | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Host ''
    Write-Host "Saved $($layout.Count) window position(s)." -ForegroundColor Green
    if ($missing.Count -gt 0) {
        Write-Host ("Targets not currently open (skipped, fine to re-capture later): " +
                    ($missing -join ', ')) -ForegroundColor DarkGray
    }
    exit 0
}

# ════════════════════════════════════════════════════════════════════
# APPLY MODE
# ════════════════════════════════════════════════════════════════════
if (-not (Test-Path $ConfigPath)) {
    Write-Error "No layout file at $ConfigPath. Run once with -Capture to create it."
    exit 1
}

$layout = Get-Content -Path $ConfigPath -Raw | ConvertFrom-Json

Write-Host ''
Write-Host "Applying layout from: $ConfigPath" -ForegroundColor Cyan

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$pending  = New-Object System.Collections.ArrayList
foreach ($t in $Targets) {
    if ($layout.PSObject.Properties.Name -contains $t.Name) {
        [void]$pending.Add($t)
    }
}

# Poll-until-seen loop. Each tick, snap anything newly-visible; exit
# early once everything is placed.
while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
    $stillPending = New-Object System.Collections.ArrayList
    foreach ($t in $pending) {
        $win = Find-WindowByTitle $t.Title
        if (-not $win) { [void]$stillPending.Add($t); continue }

        $pos = $layout.($t.Name)
        Move-Window $win.Hwnd $pos.X $pos.Y $pos.W $pos.H
        "{0,-10} '{1,-35}' -> x={2,-5} y={3,-5} w={4,-5} h={5,-5}" -f `
            $t.Name, $win.Title, $pos.X, $pos.Y, $pos.W, $pos.H | Write-Host
    }
    $pending = $stillPending
    if ($pending.Count -gt 0) { Start-Sleep -Milliseconds 250 }
}

if ($pending.Count -gt 0) {
    $names = ($pending | ForEach-Object { $_.Name }) -join ', '
    Write-Warning "Gave up waiting for: $names (window not open / title not matching)"
}
