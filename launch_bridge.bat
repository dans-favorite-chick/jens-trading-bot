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
%PY% bridge\bridge_server.py
pause
