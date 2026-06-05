@echo off
title Phoenix Dashboard
cd /d "%~dp0"
set PY="%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist %PY% set PY=python
echo ============================================
echo   PHOENIX DASHBOARD
echo   http://127.0.0.1:5000
echo ============================================
echo.
REM 2026-06-04 FINDING-2026-06-04-MULTI-PID: runtime guard refuses
REM to start under the WindowsApps shim. See docs/operator/safe_launch.md.
%PY% tools\check_interpreter.py
if errorlevel 1 (
    echo ERROR: Phoenix interpreter check FAILED — see stderr above.
    pause
    exit /b 1
)
start http://127.0.0.1:5000
%PY% dashboard\server.py
pause
