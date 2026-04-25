# =================================================================
# nt8_unhide_all_windows.ps1
# 2026-04-25 diagnostic
#
# Forces every HIDDEN window owned by NinjaTrader.exe (charts,
# Market Analyzers, SuperDOMs, etc.) to become VISIBLE on screen.
#
# Use case: NT8 auto-loaded a workspace whose charts are alive in
# memory (and connecting to bridge :8765) but whose UI is hidden,
# making them invisible in the taskbar / no Window menu present.
# After running this, every hidden chart pops up — you can see
# what's actually loaded and close what you don't want.
#
# Read-only effect: the charts themselves aren't modified, just
# their visibility state is flipped. Closing them after they pop
# up is a normal close.
#
# Usage: .\tools\nt8_unhide_all_windows.ps1
# =================================================================

$ErrorActionPreference = 'Continue'

Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinUnhide {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc proc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int  GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder buf, int cap);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", SetLastError=true)] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
'@ -PassThru | Out-Null

$nt8 = Get-Process -Name 'NinjaTrader' -ErrorAction SilentlyContinue
if (-not $nt8) { Write-Error "NinjaTrader.exe is not running."; exit 1 }
$nt8Pid = $nt8.Id
Write-Host "NinjaTrader.exe PID: $nt8Pid"
Write-Host ""

$SW_SHOWNORMAL = 1

$shown = 0
$skipped = 0
[WinUnhide]::EnumWindows({
    param($hwnd, $lparam)
    $wpid = [uint32]0
    [WinUnhide]::GetWindowThreadProcessId($hwnd, [ref]$wpid) | Out-Null
    if ($wpid -ne $nt8Pid) { return $true }

    $len = [WinUnhide]::GetWindowTextLength($hwnd)
    if ($len -le 0) { return $true }
    $sb = New-Object System.Text.StringBuilder($len + 1)
    [WinUnhide]::GetWindowText($hwnd, $sb, $sb.Capacity) | Out-Null
    $title = $sb.ToString()

    # Filter out internal noise (IME/Cicero/system frames — not real UI)
    if ($title -match 'IME|Cicero|MSCTFIME|MediaContext|SystemResource|GDI\+|NotifyWindow|BroadcastEvent') {
        $script:skipped++
        return $true
    }

    $vis = [WinUnhide]::IsWindowVisible($hwnd)
    if (-not $vis) {
        Write-Host "Showing hidden window: $title"
        [WinUnhide]::ShowWindow($hwnd, $SW_SHOWNORMAL) | Out-Null
        $script:shown++
    }
    return $true
}, [IntPtr]::Zero) | Out-Null

Write-Host ""
Write-Host "Forced visible: $shown hidden window(s)"
Write-Host "Skipped (system/internal): $skipped"
Write-Host ""
Write-Host "All previously-hidden NT8 windows should now be on your taskbar."
Write-Host "Look for: Chart - MNQM6 (x9 expected), Chart - ESM6, Chart - AUDUSD, etc."
Write-Host "Close the ones you don't need, save a clean workspace, restart NT8."
