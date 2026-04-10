@echo off
title Phoenix Prod Bot
cd /d "%~dp0"
echo ============================================
echo   PHOENIX PRODUCTION BOT
echo   Validated strategies only
echo ============================================
echo.
python bots\prod_bot.py
pause
