@echo off
REM Phoenix Bot -- PhoenixStart
REM Clears the KillSwitch marker, re-enables the PhoenixWatcher scheduled
REM task, triggers an immediate watcher run, and launches the rest of the
REM stack (bridge + dashboard + watchdog + sim_bot via launch_all.bat).

title Phoenix Start
cd /d "%~dp0"

echo ========================================
echo   PHOENIX START
echo ========================================
echo.

echo Clearing KillSwitch marker...
if exist "memory\.KILL_SWITCH_ENGAGED" (
    del "memory\.KILL_SWITCH_ENGAGED" >nul 2>&1
    echo   marker cleared.
) else (
    echo   no marker present.
)

echo Enabling scheduled tasks (Watcher + Boot + RiskGate + RiskWatchdog + Routines)...
schtasks /Change /TN "PhoenixWatcher"             /ENABLE >nul 2>&1
schtasks /Change /TN "PhoenixBoot"                /ENABLE >nul 2>&1
schtasks /Change /TN "PhoenixRiskGate"            /ENABLE >nul 2>&1
schtasks /Change /TN "PhoenixRiskWatchdog"        /ENABLE >nul 2>&1
schtasks /Change /TN "PhoenixMorningRitual"       /ENABLE >nul 2>&1
schtasks /Change /TN "PhoenixPostSessionDebrief"  /ENABLE >nul 2>&1
schtasks /Change /TN "PhoenixWeeklyEvolution"     /ENABLE >nul 2>&1

echo Triggering PhoenixWatcher immediately (one-shot)...
schtasks /Run /TN "PhoenixWatcher" >nul 2>&1

echo Triggering PhoenixRiskGate + PhoenixRiskWatchdog immediately (one-shot)...
schtasks /Run /TN "PhoenixRiskGate"     >nul 2>&1
schtasks /Run /TN "PhoenixRiskWatchdog" >nul 2>&1

echo Launching bridge + dashboard + watchdog...
call "%~dp0launch_all.bat"

echo.
echo ========================================
echo   PHOENIX is UP.
echo ========================================
