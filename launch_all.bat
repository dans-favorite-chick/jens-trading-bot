@echo off
title Phoenix Trading Bot

echo ========================================
echo   Phoenix Trading Bot - Launcher
echo ========================================
echo.

echo Starting Phoenix Bridge...
start "Phoenix Bridge" cmd /k python bridge\bridge_server.py
timeout /t 2 /nobreak >nul

echo Starting Phoenix Dashboard...
start "Phoenix Dashboard" cmd /k python dashboard\server.py
timeout /t 1 /nobreak >nul

echo Opening browser to dashboard...
start http://127.0.0.1:5000

echo.
echo ========================================
echo   Bridge and Dashboard running.
echo   Start NinjaTrader and load TickStreamer indicator.
echo   Then run launch_prod.bat or launch_lab.bat to start a bot.
echo ========================================
echo.
pause
