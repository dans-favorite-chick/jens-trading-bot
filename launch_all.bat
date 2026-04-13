@echo off
title Phoenix Trading Bot
cd /d "%~dp0"

REM Use the real Python — Windows Store python.exe spawns duplicates
set PY="%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist %PY% set PY=python

echo ========================================
echo   Phoenix Trading Bot - Launcher
echo ========================================
echo.

REM Check if bridge is already running on port 8765
netstat -ano | findstr "127.0.0.1:8765.*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Bridge already running on :8765 — skipping.
) else (
    echo Starting Phoenix Bridge...
    start "Phoenix Bridge" cmd /k %PY% bridge\bridge_server.py
    timeout /t 3 /nobreak >nul
)

REM Check if dashboard is already running on port 5000
netstat -ano | findstr "127.0.0.1:5000.*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Dashboard already running on :5000 — skipping.
) else (
    echo Starting Phoenix Dashboard...
    start "Phoenix Dashboard" cmd /k %PY% dashboard\server.py
    timeout /t 2 /nobreak >nul
)

REM Check if watchdog is already running on port 5001
netstat -ano | findstr "127.0.0.1:5001.*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Watchdog already running on :5001 — skipping.
) else (
    echo Starting Phoenix Watchdog...
    start "Phoenix Watchdog" cmd /k %PY% tools\watchdog.py
    timeout /t 1 /nobreak >nul
)

echo Opening browser to dashboard...
start http://127.0.0.1:5000

echo.
echo ========================================
echo   Bridge, Dashboard, and Watchdog running.
echo   Start NinjaTrader and load TickStreamer indicator.
echo   Then run launch_prod.bat or launch_lab.bat to start a bot.
echo   (Or let the Watchdog auto-restart bots for you.)
echo ========================================
echo.
pause
