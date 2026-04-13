@echo off
title Phoenix Prod Bot
cd /d "%~dp0"
set PY="%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist %PY% set PY=python
echo ============================================
echo   PHOENIX PRODUCTION BOT
echo   Validated strategies only
echo ============================================
echo.
%PY% bots\prod_bot.py
pause
