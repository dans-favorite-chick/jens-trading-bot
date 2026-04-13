@echo off
title Phoenix Lab Bot
cd /d "%~dp0"
set PY="%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist %PY% set PY=python
echo ============================================
echo   PHOENIX LAB BOT
echo   All strategies (experimental)
echo ============================================
echo.
%PY% bots\lab_bot.py
pause
