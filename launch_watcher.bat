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

REM 2026-06-04 FINDING-2026-06-04-MULTI-PID: runtime guard refuses
REM to start under the WindowsApps shim. See docs/operator/safe_launch.md.
%PY% tools\check_interpreter.py
if errorlevel 1 (
    echo [WatcherAgent] Interpreter check FAILED — see stderr above.
    exit /b 2
)

echo [WatcherAgent] Starting at %date% %time%
%PY% tools\watcher_agent.py
set RC=%ERRORLEVEL%
echo [WatcherAgent] Exited with code %RC% at %date% %time%
exit /b %RC%
