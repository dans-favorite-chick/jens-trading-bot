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
start http://127.0.0.1:5000
%PY% dashboard\server.py
pause
