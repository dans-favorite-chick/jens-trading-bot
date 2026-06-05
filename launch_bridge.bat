@echo off
title Phoenix Bridge Server
cd /d "%~dp0"
set PY="%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist %PY% set PY=python
echo ============================================
echo   PHOENIX BRIDGE SERVER
echo   NT8 :8765 / Bots :8766 / Health :8767
echo ============================================
echo.
REM 2026-06-04 FINDING-2026-06-04-MULTI-PID: runtime guard refuses
REM to start if PY resolves to the WindowsApps shim (which produces
REM the zombie PID pair documented in
REM out/bot_health_forensics_report_2026-06-04.md §3).
%PY% tools\check_interpreter.py
if errorlevel 1 (
    echo ERROR: Phoenix interpreter check FAILED — see stderr above.
    echo See docs\operator\safe_launch.md for the safe-launch pattern.
    pause
    exit /b 1
)
%PY% bridge\bridge_server.py
pause
