@echo off
title Phoenix Dashboard
cd /d "%~dp0"
echo ============================================
echo   PHOENIX DASHBOARD
echo   http://127.0.0.1:5000
echo ============================================
echo.
start http://127.0.0.1:5000
python dashboard\server.py
pause
