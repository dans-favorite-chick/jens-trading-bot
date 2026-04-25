@echo off
REM Phoenix Bot -- KillSwitch
REM Stops + disables the PhoenixWatcher scheduled task, then kills every
REM Phoenix-related Python process. Writes .KILL_SWITCH_ENGAGED so the
REM watcher_launcher.bat refuses to come back even if something triggers
REM the task. Run PhoenixStart.bat (or remove the marker) to resume.

title Phoenix KillSwitch
cd /d "%~dp0"

echo ========================================
echo   PHOENIX KILLSWITCH
echo ========================================
echo.
echo Disabling and stopping scheduled tasks (Watcher + Boot + RiskGate + RiskWatchdog + Routines)...
schtasks /End    /TN "PhoenixWatcher"             >nul 2>&1
schtasks /Change /TN "PhoenixWatcher"             /DISABLE >nul 2>&1
schtasks /End    /TN "PhoenixBoot"                >nul 2>&1
schtasks /Change /TN "PhoenixBoot"                /DISABLE >nul 2>&1
schtasks /End    /TN "PhoenixRiskGate"            >nul 2>&1
schtasks /Change /TN "PhoenixRiskGate"            /DISABLE >nul 2>&1
schtasks /End    /TN "PhoenixRiskWatchdog"        >nul 2>&1
schtasks /Change /TN "PhoenixRiskWatchdog"        /DISABLE >nul 2>&1
schtasks /End    /TN "PhoenixMorningRitual"       >nul 2>&1
schtasks /Change /TN "PhoenixMorningRitual"       /DISABLE >nul 2>&1
schtasks /End    /TN "PhoenixPostSessionDebrief"  >nul 2>&1
schtasks /Change /TN "PhoenixPostSessionDebrief"  /DISABLE >nul 2>&1
schtasks /End    /TN "PhoenixWeeklyEvolution"     >nul 2>&1
schtasks /Change /TN "PhoenixWeeklyEvolution"     /DISABLE >nul 2>&1

echo Writing kill-switch marker...
if not exist "memory" mkdir "memory"
echo KillSwitch engaged at %date% %time% > "memory\.KILL_SWITCH_ENGAGED"

REM Phase B+ section 3.2: flatten NT8 BEFORE killing the Python stack so
REM no working orders / open positions are stranded after Phoenix exits.
REM oif_killswitch.py writes CANCELALLORDERS + CLOSEPOSITION OIFs into
REM NT8's incoming/ folder for every configured account.
set "PY=C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe"
echo Flattening NT8 working orders + open positions...
"%PY%" tools\oif_killswitch.py 2>&1

echo Terminating Phoenix Python processes...
REM Kill by command-line match via PowerShell -- safer than blanket taskkill /IM python.exe
powershell -NoProfile -Command ^
    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'phoenix_bot|bridge_server|sim_bot\.py|prod_bot\.py|watchdog\.py|watcher_agent\.py|dashboard\\\\server\.py|dashboard/server\.py' } | ForEach-Object { Write-Host ('  killing PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo ========================================
echo   KillSwitch ENGAGED.
echo   - PhoenixWatcher + PhoenixBoot + PhoenixRiskGate + PhoenixRiskWatchdog scheduled tasks: DISABLED
echo   - All Phoenix processes terminated
echo   - Marker written: memory\.KILL_SWITCH_ENGAGED
echo.
echo   Double-click PhoenixStart.bat (or the desktop shortcut) to resume.
echo ========================================
echo.
pause
