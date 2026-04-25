@echo off
REM Phoenix Bot -- WatcherAgent launcher
REM Invoked by Windows Task Scheduler at boot AND on failure-restart.
REM Can also be double-clicked or run from a shortcut for manual launch.

title Phoenix WatcherAgent
cd /d "%~dp0"

REM Use the real Python (same pattern as launch_all.bat).
set PY="%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist %PY% set PY=python

REM Respect KillSwitch: if the marker is present, do not start.
if exist "memory\.KILL_SWITCH_ENGAGED" (
    echo [WatcherAgent] KillSwitch engaged -- not starting.
    echo [WatcherAgent] Clear memory\.KILL_SWITCH_ENGAGED or run PhoenixStart.bat to resume.
    exit /b 0
)

echo [WatcherAgent] Starting at %date% %time%
%PY% tools\watcher_agent.py
set RC=%ERRORLEVEL%
echo [WatcherAgent] Exited with code %RC% at %date% %time%
exit /b %RC%
